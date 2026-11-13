/*
 * STM32WB55 PWR: voltage scaling / regulator status and the CPU2 boot bit
 *
 * PWR_CR4.C2BOOT (bit 15) is set by HW_IPCC_Enable() (targets/f7/ble_glue/
 * hw_ipcc.c) and starts CPU2; it is forwarded to the CPU2 model in the IPCC.
 *
 * Copyright (c) 2026, Kara Zajac
 * SPDX-License-Identifier: BSD-3-Clause
 */
#include "qemu/osdep.h"
#include "hw/core/irq.h"
#include "migration/vmstate.h"
#include "wb55.h"

#define R_CR1    (0x00 / 4)
#define R_CR3    (0x08 / 4)
#define R_CR4    (0x0C / 4)
#define R_SR1    (0x10 / 4)
#define R_SR2    (0x14 / 4)
#define R_SCR    (0x18 / 4)
#define R_CR5    (0x1C / 4)
#define R_EXTSCR (0x88 / 4)

#define CR4_C2BOOT (1u << 15)

static uint64_t wb55_pwr_read(void *opaque, hwaddr addr, unsigned size)
{
    WB55PwrState *s = opaque;
    unsigned idx = addr / 4;

    if (idx >= ARRAY_SIZE(s->regs)) {
        return 0;
    }
    switch (idx) {
    case R_SR2:
        return 0;               /* VOSF=0, REGLPF=0: main regulator, ready */
    case R_EXTSCR:
        /* CPU2 sits in deepsleep (FUS idle); CPU1 never was in STOP/STBY */
        return s->regs[idx] | (1u << 15);
    default:
        return s->regs[idx];
    }
}

static void wb55_pwr_write(void *opaque, hwaddr addr, uint64_t val64,
                           unsigned size)
{
    WB55PwrState *s = opaque;
    unsigned idx = addr / 4;
    uint32_t val = val64;

    if (idx >= ARRAY_SIZE(s->regs)) {
        return;
    }
    switch (idx) {
    case R_SR1:
    case R_SR2:
        break;                  /* read-only */
    case R_SCR:
        s->regs[R_SR1] &= ~val; /* clear wakeup flags */
        break;
    case R_CR4:
        /* each write that sets it: a CPU2 that was told to reinit waits
         * for CPU1 to enable the IPCC again (stm32wb55_ipcc.c) */
        if (val & CR4_C2BOOT) {
            qemu_irq_raise(s->c2boot);
        }
        s->regs[idx] = val;
        break;
    case R_EXTSCR:
        s->regs[idx] &= ~(val & 3u);    /* C1CSSF/C2CSSF write-1-to-clear */
        break;
    default:
        s->regs[idx] = val;
        break;
    }
}

static const MemoryRegionOps wb55_pwr_ops = {
    .read = wb55_pwr_read,
    .write = wb55_pwr_write,
    .endianness = DEVICE_LITTLE_ENDIAN,
    .impl.min_access_size = 4,
    .impl.max_access_size = 4,
    .valid.min_access_size = 1,
    .valid.max_access_size = 4,
};

static void wb55_pwr_reset_hold(Object *obj, ResetType type)
{
    WB55PwrState *s = WB55_PWR(obj);

    memset(s->regs, 0, sizeof(s->regs));
    s->regs[R_CR1] = 0x00000200;        /* VOS range 1 */
    s->regs[R_CR3] = 0x00008000;
    s->regs[R_CR5] = 0x00004272;
    qemu_irq_lower(s->c2boot);
}

static void wb55_pwr_init(Object *obj)
{
    WB55PwrState *s = WB55_PWR(obj);

    memory_region_init_io(&s->mmio, obj, &wb55_pwr_ops, s, "pwr", 0x400);
    sysbus_init_mmio(SYS_BUS_DEVICE(obj), &s->mmio);
    qdev_init_gpio_out_named(DEVICE(obj), &s->c2boot, "c2boot", 1);
}

static const VMStateDescription vmstate_wb55_pwr = {
    .name = "stm32wb55-pwr",
    .version_id = 1,
    .minimum_version_id = 1,
    .fields = (const VMStateField[]) {
        VMSTATE_UINT32_ARRAY(regs, WB55PwrState, 0x90 / 4),
        VMSTATE_END_OF_LIST()
    }
};

static void wb55_pwr_class_init(ObjectClass *klass, const void *data)
{
    RESETTABLE_CLASS(klass)->phases.hold = wb55_pwr_reset_hold;
    DEVICE_CLASS(klass)->vmsd = &vmstate_wb55_pwr;
}

static const TypeInfo wb55_pwr_info = {
    .name = TYPE_WB55_PWR,
    .parent = TYPE_SYS_BUS_DEVICE,
    .instance_size = sizeof(WB55PwrState),
    .instance_init = wb55_pwr_init,
    .class_init = wb55_pwr_class_init,
};

static void wb55_pwr_register(void)
{
    type_register_static(&wb55_pwr_info);
}
type_init(wb55_pwr_register)
