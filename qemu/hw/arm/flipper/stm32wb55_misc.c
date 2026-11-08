/*
 * STM32WB55 small blocks: RNG, DWT cycle counter, DBGMCU, and a register file
 * for peripherals that only need read-back (with a few status-bit behaviours).
 *
 * DWT: QEMU's Cortex-M PPB reads zero at 0xE0001000, but furi_delay_us()
 * busy-waits on DWT->CYCCNT (furi_hal_cortex.c) - even before the scheduler
 * starts (main(): furi_delay_ms(100) for button sampling). CYCCNT here counts
 * HCLK cycles of virtual time and follows clock changes (MSI 4 MHz at reset,
 * PLL 64 MHz after furi_hal_clock_init()).
 *
 * Copyright (c) 2026, Kara Zajac
 * SPDX-License-Identifier: BSD-3-Clause
 */
#include "qemu/osdep.h"
#include "qemu/log.h"
#include "qemu/guest-random.h"
#include "hw/core/irq.h"
#include "hw/core/qdev-clock.h"
#include "hw/core/qdev-properties.h"
#include "qemu/bitops.h"
#include "wb55.h"

/* ------------------------------------------------------------------- RNG */

static uint64_t wb55_rng_read(void *opaque, hwaddr addr, unsigned size)
{
    WB55RngState *s = opaque;
    uint32_t v = 0;

    switch (addr) {
    case 0x00:
        return s->cr;
    case 0x04:
        return (s->cr & (1u << 2)) ? 1 : 0;     /* DRDY while RNGEN */
    case 0x08:
        if (s->cr & (1u << 2)) {
            qemu_guest_getrandom_nofail(&v, sizeof(v));
        }
        return v;
    }
    return 0;
}

static void wb55_rng_write(void *opaque, hwaddr addr, uint64_t val,
                           unsigned size)
{
    WB55RngState *s = opaque;

    if (addr == 0x00) {
        s->cr = val & 0x2C;
        qemu_set_irq(s->irq, 0);
    }
}

static const MemoryRegionOps wb55_rng_ops = {
    .read = wb55_rng_read,
    .write = wb55_rng_write,
    .endianness = DEVICE_LITTLE_ENDIAN,
    .impl.min_access_size = 4, .impl.max_access_size = 4,
    .valid.min_access_size = 4, .valid.max_access_size = 4,
};

static void wb55_rng_reset_hold(Object *obj, ResetType type)
{
    WB55_RNG(obj)->cr = 0;
}

static void wb55_rng_init(Object *obj)
{
    WB55RngState *s = WB55_RNG(obj);

    memory_region_init_io(&s->mmio, obj, &wb55_rng_ops, s, "rng", 0x400);
    sysbus_init_mmio(SYS_BUS_DEVICE(obj), &s->mmio);
    sysbus_init_irq(SYS_BUS_DEVICE(obj), &s->irq);
}

static void wb55_rng_class_init(ObjectClass *klass, const void *data)
{
    RESETTABLE_CLASS(klass)->phases.hold = wb55_rng_reset_hold;
}

/* ------------------------------------------------------------------- DWT */

static uint64_t dwt_cycles(WB55DwtState *s)
{
    if (!(s->ctrl & 1)) {
        return s->base_cycles;
    }
    int64_t now = qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL);
    return s->base_cycles + muldiv64(now - s->base_ns, clock_get_hz(s->clk),
                                     NANOSECONDS_PER_SECOND);
}

static void dwt_rebase(WB55DwtState *s)
{
    s->base_cycles = dwt_cycles(s);
    s->base_ns = qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL);
}

static void dwt_clk_cb(void *opaque, ClockEvent event)
{
    dwt_rebase(opaque);                 /* before HCLK changes */
}

static uint64_t wb55_dwt_read(void *opaque, hwaddr addr, unsigned size)
{
    WB55DwtState *s = opaque;
    static const uint8_t id[12] = {     /* PID4..7, PID0..3, CID0..3 */
        0x04, 0, 0, 0, 0x02, 0xB0, 0x3B, 0x00, 0x0D, 0xE0, 0x05, 0xB1,
    };

    switch (addr) {
    case 0x000:
        return s->ctrl | 0x40000000;    /* NUMCOMP = 4 */
    case 0x004:
        return (uint32_t)dwt_cycles(s);
    }
    if (addr >= 0x020 && addr < 0x060) {
        return s->comp[(addr - 0x020) / 4];
    }
    if (addr >= 0xFD0) {
        return id[(addr - 0xFD0) / 4];
    }
    return 0;
}

static void wb55_dwt_write(void *opaque, hwaddr addr, uint64_t val,
                           unsigned size)
{
    WB55DwtState *s = opaque;

    switch (addr) {
    case 0x000:
        dwt_rebase(s);
        s->ctrl = val & 0x0FFFFFFF;
        return;
    case 0x004:
        s->base_cycles = (uint32_t)val;
        s->base_ns = qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL);
        return;
    }
    if (addr >= 0x020 && addr < 0x060) {
        s->comp[(addr - 0x020) / 4] = val;  /* watchpoints: not modelled */
    }
}

static const MemoryRegionOps wb55_dwt_ops = {
    .read = wb55_dwt_read,
    .write = wb55_dwt_write,
    .endianness = DEVICE_LITTLE_ENDIAN,
    .impl.min_access_size = 4, .impl.max_access_size = 4,
    .valid.min_access_size = 4, .valid.max_access_size = 4,
};

static void wb55_dwt_reset_hold(Object *obj, ResetType type)
{
    WB55DwtState *s = WB55_DWT(obj);

    s->ctrl = 0;
    s->base_cycles = 0;
    s->base_ns = qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL);
    memset(s->comp, 0, sizeof(s->comp));
}

static void wb55_dwt_init(Object *obj)
{
    WB55DwtState *s = WB55_DWT(obj);

    memory_region_init_io(&s->mmio, obj, &wb55_dwt_ops, s, "dwt", 0x1000);
    sysbus_init_mmio(SYS_BUS_DEVICE(obj), &s->mmio);
    s->clk = qdev_init_clock_in(DEVICE(obj), "clk", dwt_clk_cb, s,
                                ClockPreUpdate);
}

static void wb55_dwt_class_init(ObjectClass *klass, const void *data)
{
    RESETTABLE_CLASS(klass)->phases.hold = wb55_dwt_reset_hold;
}

/* ---------------------------------------------------------------- DBGMCU */

static uint64_t wb55_dbgmcu_read(void *opaque, hwaddr addr, unsigned size)
{
    WB55DbgmcuState *s = opaque;

    if (addr == 0) {
        return 0x20016495;              /* REV_ID 0x2001 (rev Y), DEV_ID 0x495 */
    }
    return addr / 4 < ARRAY_SIZE(s->regs) ? s->regs[addr / 4] : 0;
}

static void wb55_dbgmcu_write(void *opaque, hwaddr addr, uint64_t val,
                              unsigned size)
{
    WB55DbgmcuState *s = opaque;

    if (addr && addr / 4 < ARRAY_SIZE(s->regs)) {
        s->regs[addr / 4] = val;
    }
}

static const MemoryRegionOps wb55_dbgmcu_ops = {
    .read = wb55_dbgmcu_read,
    .write = wb55_dbgmcu_write,
    .endianness = DEVICE_LITTLE_ENDIAN,
    .impl.min_access_size = 4, .impl.max_access_size = 4,
    .valid.min_access_size = 1, .valid.max_access_size = 4,
};

static void wb55_dbgmcu_init(Object *obj)
{
    WB55DbgmcuState *s = WB55_DBGMCU(obj);

    memory_region_init_io(&s->mmio, obj, &wb55_dbgmcu_ops, s, "dbgmcu", 0x400);
    sysbus_init_mmio(SYS_BUS_DEVICE(obj), &s->mmio);
}

/* -------------------------------------------------------------- regfile */

static void regfile_adc_write(WB55RegfileState *s, hwaddr addr, uint32_t val)
{
    uint32_t *isr = &s->regs[0x00 / 4], *cr = &s->regs[0x08 / 4];

    switch (addr) {
    case 0x00:
        *isr &= ~val;                   /* write-1-to-clear */
        return;
    case 0x08:
        *cr = val & ~((1u << 31) | (1u << 2) | (1u << 4) | (1u << 5));
        if (val & (1u << 1)) {          /* ADDIS */
            *cr &= ~3u;
            *isr &= ~1u;
        } else if (val & 1u) {          /* ADEN -> ADRDY */
            *isr |= 1u;
        }
        if (val & (1u << 2)) {          /* ADSTART: conversion done at once */
            s->regs[0x40 / 4] = 1500;   /* ~1.2 V on a 3.3 V reference */
            *isr |= (1u << 1) | (1u << 2) | (1u << 3);
        }
        return;
    case 0x40:
        return;                         /* DR is read-only */
    }
    s->regs[addr / 4] = val;
}

static void regfile_tim_write(WB55RegfileState *s, hwaddr addr, uint32_t val)
{
    switch (addr) {
    case 0x10:
        s->regs[0x10 / 4] &= val;       /* SR: rc_w0 */
        return;
    case 0x14:
        if (val & 1u) {                 /* EGR.UG: update event */
            s->regs[0x24 / 4] = 0;
            if (!(s->regs[0x00 / 4] & (1u << 2))) {   /* !URS */
                s->regs[0x10 / 4] |= 1u;
            }
        }
        s->regs[0x10 / 4] |= val & 0x1Eu;
        return;
    }
    s->regs[addr / 4] = val;
}

static void regfile_aes_write(WB55RegfileState *s, hwaddr addr, uint32_t val)
{
    switch (addr) {
    case 0x04:
        return;                         /* SR read-only */
    case 0x08:                          /* DINR: a block completes per 4 words */
        s->regs[0x08 / 4] = val;
        s->regs[0x0C / 4] = val;
        if ((++s->regs[0xFC / 4] & 3) == 0) {
            s->regs[0x04 / 4] |= 1u;    /* CCF */
        }
        return;
    case 0x00:
        if (val & (1u << 7)) {          /* CCFC: clear CCF */
            s->regs[0x04 / 4] &= ~1u;
        }
        s->regs[0] = val & ~((1u << 7) | (1u << 8));
        return;
    }
    s->regs[addr / 4] = val;
}

static uint64_t wb55_regfile_read(void *opaque, hwaddr addr, unsigned size)
{
    WB55RegfileState *s = opaque;

    if (!s->warned) {
        s->warned = true;
        qemu_log_mask(LOG_UNIMP, "%s: register-file model in use\n", s->name);
    }
    if (addr / 4 >= s->size / 4) {
        return 0;
    }
    return extract32(s->regs[addr / 4], (addr & 3) * 8, size * 8);
}

static void wb55_regfile_write(void *opaque, hwaddr addr, uint64_t val,
                               unsigned size)
{
    WB55RegfileState *s = opaque;

    if (addr / 4 >= s->size / 4) {
        return;
    }
    if (size < 4) {                     /* byte/halfword: read-modify-write */
        val = deposit32(s->regs[addr / 4], (addr & 3) * 8, size * 8, val);
        addr &= ~3;
    }
    switch (s->kind) {
    case WB55_RF_ADC: regfile_adc_write(s, addr, val); break;
    case WB55_RF_TIM: regfile_tim_write(s, addr, val); break;
    case WB55_RF_AES: regfile_aes_write(s, addr, val); break;
    default:          s->regs[addr / 4] = val; break;
    }
    if (s->changed) {
        s->changed(s->changed_opaque);
    }
}

static const MemoryRegionOps wb55_regfile_ops = {
    .read = wb55_regfile_read,
    .write = wb55_regfile_write,
    .endianness = DEVICE_LITTLE_ENDIAN,
    .impl.min_access_size = 1, .impl.max_access_size = 4,
    /* LDRD/STRD arrive as one 8-byte access; split it like the bus does */
    .valid.min_access_size = 1, .valid.max_access_size = 8,
};

static void wb55_regfile_reset_hold(Object *obj, ResetType type)
{
    WB55RegfileState *s = WB55_REGFILE(obj);

    if (s->regs) {
        memset(s->regs, 0, s->size);
        if (s->kind == WB55_RF_ADC) {
            s->regs[0x08 / 4] = 1u << 29;   /* DEEPPWD */
        }
    }
}

static void wb55_regfile_reset_exit(Object *obj, ResetType type)
{
    WB55RegfileState *s = WB55_REGFILE(obj);

    if (s->changed) {
        s->changed(s->changed_opaque);
    }
}

static void wb55_regfile_realize(DeviceState *dev, Error **errp)
{
    WB55RegfileState *s = WB55_REGFILE(dev);

    s->regs = g_new0(uint32_t, s->size / 4);
    memory_region_init_io(&s->mmio, OBJECT(dev), &wb55_regfile_ops, s,
                          s->name ? s->name : "regfile", s->size);
    sysbus_init_mmio(SYS_BUS_DEVICE(dev), &s->mmio);
}

static const Property wb55_regfile_props[] = {
    DEFINE_PROP_STRING("name", WB55RegfileState, name),
    DEFINE_PROP_UINT32("size", WB55RegfileState, size, 0x400),
    DEFINE_PROP_UINT32("kind", WB55RegfileState, kind, WB55_RF_PLAIN),
};

static void wb55_regfile_class_init(ObjectClass *klass, const void *data)
{
    DeviceClass *dc = DEVICE_CLASS(klass);

    dc->realize = wb55_regfile_realize;
    device_class_set_props(dc, wb55_regfile_props);
    RESETTABLE_CLASS(klass)->phases.hold = wb55_regfile_reset_hold;
    RESETTABLE_CLASS(klass)->phases.exit = wb55_regfile_reset_exit;
}

static const TypeInfo wb55_misc_types[] = {
    {
        .name = TYPE_WB55_RNG,
        .parent = TYPE_SYS_BUS_DEVICE,
        .instance_size = sizeof(WB55RngState),
        .instance_init = wb55_rng_init,
        .class_init = wb55_rng_class_init,
    }, {
        .name = TYPE_WB55_DWT,
        .parent = TYPE_SYS_BUS_DEVICE,
        .instance_size = sizeof(WB55DwtState),
        .instance_init = wb55_dwt_init,
        .class_init = wb55_dwt_class_init,
    }, {
        .name = TYPE_WB55_DBGMCU,
        .parent = TYPE_SYS_BUS_DEVICE,
        .instance_size = sizeof(WB55DbgmcuState),
        .instance_init = wb55_dbgmcu_init,
    }, {
        .name = TYPE_WB55_REGFILE,
        .parent = TYPE_SYS_BUS_DEVICE,
        .instance_size = sizeof(WB55RegfileState),
        .class_init = wb55_regfile_class_init,
    },
};
DEFINE_TYPES(wb55_misc_types)
