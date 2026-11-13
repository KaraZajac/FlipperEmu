/*
 * STM32WB55 embedded flash: controller, 1 MiB main array, option bytes,
 * and the "info" block (system memory, OTP, unique ID, option bytes).
 *
 * Programming follows RM0434 section 3.3 and the firmware's own driver
 * (targets/f7/furi_hal/furi_hal_flash.c):
 *   - KEYR 0x45670123/0xCDEF89AB unlocks CR, OPTKEYR 0x08192A3B/0x4C5D6E7F OB
 *   - PG=1 then two 32-bit stores program one 64-bit double word
 *   - PER + PNB + STRT erases one 4 KiB page
 *   - FLASH->SR must read exactly 0 between operations (furi_check), so EOP
 *     only latches when EOPIE is set, as on silicon
 *   - OPTSTRT copies the OB registers to OB memory, OBL_LAUNCH reloads + resets
 * Code executed from freshly programmed pages (XIP app loaders, the OTA
 * updater) needs the TCG translation cache flushed for those addresses, which
 * memory_region_flush_rom_device() does.
 *
 * With a block backend ("drive" property) every program/erase is written
 * through to the image, so the flash survives restarts like the real chip.
 *
 * Copyright (c) 2026, Kara Zajac
 * SPDX-License-Identifier: BSD-3-Clause
 */
#include "qemu/osdep.h"
#include "qemu/log.h"
#include "qapi/error.h"
#include "hw/core/irq.h"
#include "hw/core/qdev-properties.h"
#include "hw/core/qdev-properties-system.h"
#include "system/block-backend.h"
#include "system/memory.h"
#include "hw/core/cpu.h"
#include "qemu/error-report.h"
#include "migration/vmstate.h"
#include "system/runstate.h"
#include "wb55.h"

#define R_ACR      0x00
#define R_KEYR     0x08
#define R_OPTKEYR  0x0C
#define R_SR       0x10
#define R_CR       0x14
#define R_ECCR     0x18
#define R_OPTR     0x20
#define R_IPCCBR   0x3C
#define R_C2ACR    0x5C
#define R_C2SR     0x60
#define R_C2CR     0x64
#define R_SFR      0x80
#define R_SRRVR    0x84

#define SR_EOP     (1u << 0)
#define SR_PROGERR (1u << 3)
#define SR_PGAERR  (1u << 5)
#define SR_SIZERR  (1u << 6)
#define SR_PGSERR  (1u << 7)
#define SR_ERRORS  0x0000C3FAu

#define CR_PG        (1u << 0)
#define CR_PER       (1u << 1)
#define CR_MER       (1u << 2)
#define CR_STRT      (1u << 16)
#define CR_OPTSTRT   (1u << 17)
#define CR_FSTPG     (1u << 18)
#define CR_EOPIE     (1u << 24)
#define CR_OBL_LAUNCH (1u << 27)
#define CR_OPTLOCK   (1u << 30)
#define CR_LOCK      (1u << 31)

/* option byte word index for each OB register (furi_hal_flash.c ob_reg_map) */
static const struct { uint16_t reg; uint8_t word; } ob_regs[] = {
    { R_OPTR, 0 }, { 0x24, 1 }, { 0x28, 2 }, { 0x2C, 3 }, { 0x30, 4 },
    { 0x34, 5 }, { 0x38, 6 }, { R_IPCCBR, 13 }, { R_SFR, 14 }, { R_SRRVR, 15 },
};

/* Factory option bytes of a Flipper Zero: the "OB reference" of the official
 * update manifest (update.fuf, firmware 1.5.1). SFR.SFSA = 0xD7 puts the
 * secure CPU2 area at 0x080D7000. */
static const uint32_t ob_factory[16] = {
    [0] = 0x0D8F79AA, [1] = 0x000001FF, [2] = 0x80000000, [3] = 0x000000FF,
    [4] = 0x000000FF, [5] = 0x000001FF, [6] = 0x00000000, [13] = 0x00000000,
    [14] = 0x000010D7, [15] = 0x964B5C00,
};

uint8_t *wb55_flash_info_ptr(WB55FlashState *s)
{
    return memory_region_get_ram_ptr(&s->info);
}

static uint8_t *flash_ptr(WB55FlashState *s)
{
    return memory_region_get_ram_ptr(&s->flash);
}

static void flash_commit(WB55FlashState *s, hwaddr off, unsigned len)
{
    memory_region_flush_rom_device(&s->flash, off, len);
    if (s->blk && blk_pwrite(s->blk, off, len, flash_ptr(s) + off, 0) < 0) {
        qemu_log_mask(LOG_GUEST_ERROR, "wb55-flash: image write failed\n");
    }
}

static void flash_ob_store(WB55FlashState *s)
{
    uint32_t *ob = (uint32_t *)(wb55_flash_info_ptr(s) +
                                (WB55_OB_BASE - WB55_INFO_BASE));
    for (int i = 0; i < 16; i++) {
        ob[2 * i] = cpu_to_le32(s->ob[i]);
        ob[2 * i + 1] = cpu_to_le32(~s->ob[i]);
    }
}

static void flash_op_done(WB55FlashState *s)
{
    if (s->cr & CR_EOPIE) {
        s->sr |= SR_EOP;
    }
    qemu_set_irq(s->irq, !!(s->sr & SR_EOP) && (s->cr & CR_EOPIE));
}

static uint32_t sfsa_page(WB55FlashState *s)
{
    return s->ob[14] & 0xFF;
}

static void flash_erase_page(WB55FlashState *s, unsigned page)
{
    if (page >= WB55_FLASH_SIZE / WB55_FLASH_PAGE) {
        s->sr |= SR_PGSERR;
        return;
    }
    if (page >= sfsa_page(s)) {
        qemu_log_mask(LOG_GUEST_ERROR,
                      "wb55-flash: erase of secure page %u refused\n", page);
        s->sr |= 1u << 4;               /* WRPERR */
        return;
    }
    memset(flash_ptr(s) + page * WB55_FLASH_PAGE, 0xFF, WB55_FLASH_PAGE);
    flash_commit(s, page * WB55_FLASH_PAGE, WB55_FLASH_PAGE);
}

static void flash_ctrl_write_cr(WB55FlashState *s, uint32_t val)
{
    if (s->cr & CR_LOCK) {
        if (!(val & CR_LOCK)) {
            qemu_log_mask(LOG_GUEST_ERROR, "wb55-flash: CR write while locked\n");
        }
        return;
    }
    /* OPTLOCK can only be set by software; LOCK sets both */
    if (val & CR_LOCK) {
        val |= CR_OPTLOCK;
    } else if (!(val & CR_OPTLOCK) && (s->cr & CR_OPTLOCK)) {
        val |= CR_OPTLOCK;              /* cleared only via OPTKEYR */
    }
    if (val & CR_LOCK) {
        s->key_state = 0;
        s->optkey_state = 0;
    }
    if (!(val & CR_PG)) {
        s->pg_pending = false;
    }
    s->cr = val & ~(CR_STRT | CR_OPTSTRT | CR_OBL_LAUNCH);

    if (val & CR_STRT) {
        if (val & CR_MER) {
            for (unsigned p = 0; p < sfsa_page(s); p++) {
                flash_erase_page(s, p);
            }
        } else if (val & CR_PER) {
            flash_erase_page(s, (val >> 3) & 0xFF);
        }
        flash_op_done(s);
    }
    if ((val & CR_OPTSTRT) && !(s->cr & CR_OPTLOCK)) {
        flash_ob_store(s);
        flash_op_done(s);
    }
    if ((val & CR_OBL_LAUNCH) && !(s->cr & CR_OPTLOCK)) {
        /* Option byte reload generates a system reset (RM0434 3.4.2) */
        if (s->rcc) {
            wb55_rcc_request_reset(s->rcc, 1u << 25);  /* OBLRSTF */
        }
    }
}

static uint64_t flash_ctrl_read(void *opaque, hwaddr addr, unsigned size)
{
    WB55FlashState *s = opaque;

    switch (addr) {
    case R_ACR:    return s->acr;
    case R_SR:     return s->sr;
    case R_CR:     return s->cr;
    case R_ECCR:   return s->eccr;
    case R_C2ACR:  return s->c2acr;
    case R_C2SR:   return s->c2sr;
    case R_C2CR:   return s->c2cr;
    case R_KEYR:
    case R_OPTKEYR:
        return 0;
    }
    for (int i = 0; i < ARRAY_SIZE(ob_regs); i++) {
        if (ob_regs[i].reg == addr) {
            return s->ob[ob_regs[i].word];
        }
    }
    return 0;
}

static void flash_ctrl_write(void *opaque, hwaddr addr, uint64_t val64,
                             unsigned size)
{
    WB55FlashState *s = opaque;
    uint32_t val = val64;

    switch (addr) {
    case R_ACR:
        /* ICRST/DCRST self-clear; EMPTY (bit 16) is 0: flash is programmed */
        s->acr = val & ~((1u << 11) | (1u << 12) | (1u << 16));
        return;
    case R_KEYR:
        if (s->key_state == 0 && val == 0x45670123) {
            s->key_state = 1;
        } else if (s->key_state == 1 && val == 0xCDEF89AB) {
            s->key_state = 2;
            s->cr &= ~CR_LOCK;
        } else {
            s->key_state = 0;
            qemu_log_mask(LOG_GUEST_ERROR, "wb55-flash: bad KEYR sequence\n");
        }
        return;
    case R_OPTKEYR:
        if (s->optkey_state == 0 && val == 0x08192A3B) {
            s->optkey_state = 1;
        } else if (s->optkey_state == 1 && val == 0x4C5D6E7F &&
                   !(s->cr & CR_LOCK)) {
            s->optkey_state = 2;
            s->cr &= ~CR_OPTLOCK;
        } else {
            s->optkey_state = 0;
        }
        return;
    case R_SR:
        s->sr &= ~(val & (SR_EOP | SR_ERRORS));    /* write-1-to-clear */
        qemu_set_irq(s->irq, !!(s->sr & SR_EOP) && (s->cr & CR_EOPIE));
        return;
    case R_CR:
        flash_ctrl_write_cr(s, val);
        return;
    case R_ECCR:
        s->eccr &= ~(val & (3u << 30));
        s->eccr = (s->eccr & (3u << 30)) | (val & (1u << 24));
        return;
    case R_C2ACR:
        s->c2acr = val;
        return;
    case R_C2CR:
        s->c2cr = val;
        return;
    }
    for (int i = 0; i < ARRAY_SIZE(ob_regs); i++) {
        if (ob_regs[i].reg == addr) {
            if (s->cr & CR_OPTLOCK) {
                qemu_log_mask(LOG_GUEST_ERROR,
                              "wb55-flash: OB write while OPTLOCK\n");
            } else if (addr == R_SFR || addr == R_SRRVR) {
                /* secure area is owned by CPU2/FUS, read-only to CPU1 */
            } else {
                s->ob[ob_regs[i].word] = val;
            }
            return;
        }
    }
    qemu_log_mask(LOG_UNIMP, "wb55-flash: write 0x%" HWADDR_PRIx "\n", addr);
}

static const MemoryRegionOps flash_ctrl_ops = {
    .read = flash_ctrl_read,
    .write = flash_ctrl_write,
    .endianness = DEVICE_LITTLE_ENDIAN,
    .impl.min_access_size = 4,
    .impl.max_access_size = 4,
    .valid.min_access_size = 1,
    .valid.max_access_size = 4,
};

/* Stores into the flash array: only legal while programming. */
static void flash_array_write(void *opaque, hwaddr off, uint64_t val,
                              unsigned size)
{
    WB55FlashState *s = opaque;

    if (!current_cpu) {
        /* Not a CPU store: QEMU's ELF loader zero-filling .bss at its flash
         * LMA. DFU programming never writes there, so neither do we. */
        return;
    }
    if (!(s->cr & (CR_PG | CR_FSTPG)) || (s->cr & CR_LOCK)) {
        qemu_log_mask(LOG_GUEST_ERROR,
                      "wb55-flash: store to flash 0x%08" HWADDR_PRIx
                      " outside a program sequence\n", WB55_FLASH_BASE + off);
        s->sr |= SR_PGSERR;
        return;
    }
    if (size != 4) {
        s->sr |= SR_SIZERR;
        s->pg_pending = false;
        return;
    }
    if (!s->pg_pending) {
        if (off & 7) {
            s->sr |= SR_PGAERR;
            return;
        }
        s->pg_pending = true;
        s->pg_addr = off;
        s->pg_lo = val;
        return;
    }
    if (off != s->pg_addr + 4) {
        s->sr |= SR_PGAERR;
        s->pg_pending = false;
        return;
    }
    s->pg_pending = false;
    if (off / WB55_FLASH_PAGE >= sfsa_page(s)) {
        s->sr |= 1u << 4;               /* WRPERR: secure area */
        return;
    }

    uint8_t *p = flash_ptr(s) + s->pg_addr;
    uint64_t cur = ldq_le_p(p);
    uint64_t data = (uint64_t)s->pg_lo | ((uint64_t)(uint32_t)val << 32);
    if (cur != UINT64_MAX && data != 0) {
        /* RM0434: programming a non-erased double word sets PROGERR */
        s->sr |= SR_PROGERR;
        return;
    }
    stq_le_p(p, data);
    flash_commit(s, s->pg_addr, 8);
    flash_op_done(s);
}

static uint64_t flash_array_read(void *opaque, hwaddr off, unsigned size)
{
    /* only reached while the region is in MMIO mode; reads normally go
     * straight to the RAM backing */
    WB55FlashState *s = opaque;
    return ldn_le_p(flash_ptr(s) + off, size);
}

static const MemoryRegionOps flash_array_ops = {
    .read = flash_array_read,
    .write = flash_array_write,
    .endianness = DEVICE_LITTLE_ENDIAN,
    .valid.min_access_size = 1,
    .valid.max_access_size = 8,
};

static void wb55_flash_reset_hold(Object *obj, ResetType type)
{
    WB55FlashState *s = WB55_FLASH(obj);
    const uint32_t *ob = (const uint32_t *)(wb55_flash_info_ptr(s) +
                                            (WB55_OB_BASE - WB55_INFO_BASE));

    s->acr = 0x00000600;
    s->sr = 0;
    s->cr = CR_LOCK | CR_OPTLOCK;
    s->eccr = 0;
    s->c2acr = 0x00000600;
    s->c2sr = 0;
    s->c2cr = CR_LOCK | CR_OPTLOCK;
    s->key_state = s->optkey_state = 0;
    s->pg_pending = false;
    /* option byte loading at reset: registers come from OB memory */
    for (int i = 0; i < 16; i++) {
        s->ob[i] = le32_to_cpu(ob[2 * i]);
    }
    qemu_set_irq(s->irq, 0);
}

static void wb55_flash_realize(DeviceState *dev, Error **errp)
{
    WB55FlashState *s = WB55_FLASH(dev);

    if (!memory_region_init_rom_device(&s->flash, OBJECT(dev), &flash_array_ops,
                                       s, "wb55.flash", WB55_FLASH_SIZE, errp)) {
        return;
    }
    memset(flash_ptr(s), 0xFF, WB55_FLASH_SIZE);

    if (s->blk) {
        uint64_t perm = BLK_PERM_CONSISTENT_READ |
                        (blk_supports_write_perm(s->blk) ? BLK_PERM_WRITE : 0);
        if (blk_set_perm(s->blk, perm, BLK_PERM_ALL, errp) < 0) {
            return;
        }
        int64_t len = blk_getlength(s->blk);
        if (len < 0) {
            error_setg(errp, "wb55-flash: cannot size flash image");
            return;
        }
        len = MIN(len, WB55_FLASH_SIZE);
        if (len > 0 && blk_pread(s->blk, 0, len, flash_ptr(s), 0) < 0) {
            error_setg(errp, "wb55-flash: cannot read flash image");
            return;
        }
    }

    if (!memory_region_init_rom(&s->info, OBJECT(dev), "wb55.info",
                                WB55_INFO_SIZE, errp)) {
        return;
    }
    uint8_t *info = wb55_flash_info_ptr(s);
    memset(info, 0xFF, WB55_INFO_SIZE);
    /* System bootloader stand-in (0x1FFF0000): a vector table whose reset
     * handler spins, so "DFU mode" parks the CPU instead of faulting. */
    stl_le_p(info + 0, 0x20003000);
    stl_le_p(info + 4, WB55_SYSMEM_BASE + 0x101);
    stw_le_p(info + 0x100, 0xE7FE);     /* b . */
    /* The factory's values for the ADC: what it reads of the internal
     * reference with VREF+ at 3.6 V, and of the temperature sensor at 30 and
     * at 130 degrees C with VREF+ at 3.0 V (stm32wb55_adc.c) */
    stw_le_p(info + (WB55_VREFINT_CAL_ADDR - WB55_INFO_BASE),
             WB55_ADC_CAL(WB55_VREFINT_UV, 3600));
    stw_le_p(info + (WB55_TS_CAL1_ADDR - WB55_INFO_BASE),
             WB55_ADC_CAL(WB55_TS_UV(30), 3000));
    stw_le_p(info + (WB55_TS_CAL2_ADDR - WB55_INFO_BASE),
             WB55_ADC_CAL(WB55_TS_UV(130), 3000));
    for (int i = 0; i < 16; i++) {
        s->ob[i] = ob_factory[i];
    }
    flash_ob_store(s);
}

static const Property wb55_flash_props[] = {
    DEFINE_PROP_DRIVE("drive", WB55FlashState, blk),
};

static void wb55_flash_init(Object *obj)
{
    WB55FlashState *s = WB55_FLASH(obj);

    memory_region_init_io(&s->mmio, obj, &flash_ctrl_ops, s, "flash-ctrl",
                          0x400);
    sysbus_init_mmio(SYS_BUS_DEVICE(obj), &s->mmio);
    sysbus_init_irq(SYS_BUS_DEVICE(obj), &s->irq);
}

/*
 * A saved state.  The flash's contents came with the machine's memory; the
 * image on the host has to say the same.  It takes no write while a state
 * is coming in, so it is written when the machine next runs, and a load that
 * fails leaves it as it was.
 */
static void wb55_flash_loaded(void *opaque, bool running, RunState state)
{
    WB55FlashState *s = opaque;

    if (!running) {
        return;
    }
    qemu_del_vm_change_state_handler(s->loaded);
    s->loaded = NULL;
    if (blk_pwrite(s->blk, 0, WB55_FLASH_SIZE, flash_ptr(s), 0) < 0) {
        error_report("wb55-flash: the image was not written after a load");
    }
}

static int wb55_flash_post_load(void *opaque, int version_id)
{
    WB55FlashState *s = opaque;

    if (s->blk && blk_supports_write_perm(s->blk) && !s->loaded) {
        s->loaded = qemu_add_vm_change_state_handler(wb55_flash_loaded, s);
    }
    return 0;
}

static const VMStateDescription vmstate_wb55_flash = {
    .name = "stm32wb55-flash",
    .version_id = 1,
    .minimum_version_id = 1,
    .post_load = wb55_flash_post_load,
    .fields = (const VMStateField[]) {
        VMSTATE_UINT32(acr, WB55FlashState),
        VMSTATE_UINT32(sr, WB55FlashState),
        VMSTATE_UINT32(cr, WB55FlashState),
        VMSTATE_UINT32(eccr, WB55FlashState),
        VMSTATE_UINT32(c2acr, WB55FlashState),
        VMSTATE_UINT32(c2sr, WB55FlashState),
        VMSTATE_UINT32(c2cr, WB55FlashState),
        VMSTATE_UINT32_ARRAY(ob, WB55FlashState, 16),
        VMSTATE_UINT32(key_state, WB55FlashState),
        VMSTATE_UINT32(optkey_state, WB55FlashState),
        VMSTATE_BOOL(pg_pending, WB55FlashState),
        VMSTATE_UINT64(pg_addr, WB55FlashState),
        VMSTATE_UINT32(pg_lo, WB55FlashState),
        VMSTATE_END_OF_LIST()
    }
};

static void wb55_flash_class_init(ObjectClass *klass, const void *data)
{
    DeviceClass *dc = DEVICE_CLASS(klass);

    dc->realize = wb55_flash_realize;
    device_class_set_props(dc, wb55_flash_props);
    RESETTABLE_CLASS(klass)->phases.hold = wb55_flash_reset_hold;
    dc->vmsd = &vmstate_wb55_flash;
}

static const TypeInfo wb55_flash_info = {
    .name = TYPE_WB55_FLASH,
    .parent = TYPE_SYS_BUS_DEVICE,
    .instance_size = sizeof(WB55FlashState),
    .instance_init = wb55_flash_init,
    .class_init = wb55_flash_class_init,
};

static void wb55_flash_register(void)
{
    type_register_static(&wb55_flash_info);
}
type_init(wb55_flash_register)
