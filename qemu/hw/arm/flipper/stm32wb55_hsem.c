/*
 * STM32WB55 hardware semaphores (RM0434 chapter 37)
 *
 *   read  RLRx           1-step lock: if free, lock for CPU1 (COREID 4,
 *                        PROCID 0); returns the semaphore status either way
 *   write Rx, LOCK=1     2-step lock (COREID/PROCID from the written value)
 *   write Rx, LOCK=0     release, if COREID and PROCID match
 *   read  Rx             status, no side effect
 *
 * The firmware relies on the read side effect: furi_hal_bt_init() does
 * furi_check(LL_HSEM_1StepLock(HSEM, CFG_HW_CLK48_CONFIG_SEMID) == 0) and
 * furi_hal_random_get() spins on RLR0 until it reads LOCK|COREID(CPU1).
 * Nothing else ever owns a semaphore here: CPU2 is behavioural.
 *
 * Copyright (c) 2026, Kara Zajac
 * SPDX-License-Identifier: BSD-3-Clause
 */
#include "qemu/osdep.h"
#include "qemu/log.h"
#include "hw/core/irq.h"
#include "migration/vmstate.h"
#include "wb55.h"

#define HSEM_LOCK      (1u << 31)
#define COREID_CPU1    4u
#define COREID_SHIFT   8
#define HSEM_KEY_SHIFT 16

static void hsem_update_irq(WB55HsemState *s)
{
    qemu_set_irq(s->irq, !!(s->c1isr & s->c1ier));
}

static void hsem_release(WB55HsemState *s, int i)
{
    if (s->r[i] & HSEM_LOCK) {
        s->r[i] = 0;
        s->c1isr |= 1u << i;            /* semaphore free event */
        s->c2isr |= 1u << i;
        hsem_update_irq(s);
    }
}

static uint64_t wb55_hsem_read(void *opaque, hwaddr addr, unsigned size)
{
    WB55HsemState *s = opaque;

    if (addr < 0x80) {                  /* R[i] */
        return s->r[addr / 4];
    }
    if (addr < 0x100) {                 /* RLR[i]: 1-step lock */
        int i = (addr - 0x80) / 4;
        if (!(s->r[i] & HSEM_LOCK)) {
            s->r[i] = HSEM_LOCK | (COREID_CPU1 << COREID_SHIFT);
        }
        return s->r[i];
    }
    switch (addr) {
    case 0x100: return s->c1ier;
    case 0x108: return s->c1isr;
    case 0x10C: return s->c1isr & s->c1ier;
    case 0x110: return s->c2ier;
    case 0x118: return s->c2isr;
    case 0x11C: return s->c2isr & s->c2ier;
    case 0x144: return s->keyr;
    case 0x3F0: return 0x00000820;      /* HWCFGR: 32 semaphores, 2 cores */
    case 0x3F4: return 0x00000020;      /* VERR */
    case 0x3F8: return 0x00100072;      /* IPIDR */
    case 0x3FC: return 0xA3C5DD01;      /* SIDR */
    }
    return 0;
}

static void wb55_hsem_write(void *opaque, hwaddr addr, uint64_t val64,
                            unsigned size)
{
    WB55HsemState *s = opaque;
    uint32_t val = val64;

    if (addr < 0x80) {
        int i = addr / 4;
        uint32_t id = val & 0xFFFF;     /* COREID | PROCID */
        if (val & HSEM_LOCK) {
            if (!(s->r[i] & HSEM_LOCK)) {
                s->r[i] = HSEM_LOCK | id;
            }
        } else if ((s->r[i] & 0xFFFF) == id) {
            hsem_release(s, i);
        }
        return;
    }
    if (addr < 0x100) {
        return;                         /* RLR is read-only */
    }
    switch (addr) {
    case 0x100:
        s->c1ier = val;
        hsem_update_irq(s);
        break;
    case 0x104:
        s->c1isr &= ~val;               /* C1ICR */
        hsem_update_irq(s);
        break;
    case 0x110:
        s->c2ier = val;
        break;
    case 0x114:
        s->c2isr &= ~val;
        break;
    case 0x140:                         /* CR: clear all of one COREID */
        if ((val >> HSEM_KEY_SHIFT) == s->keyr >> HSEM_KEY_SHIFT) {
            uint32_t core = val & (0xFu << COREID_SHIFT);
            for (int i = 0; i < 32; i++) {
                if ((s->r[i] & HSEM_LOCK) &&
                    (s->r[i] & (0xFu << COREID_SHIFT)) == core) {
                    hsem_release(s, i);
                }
            }
        }
        break;
    case 0x144:
        s->keyr = val & 0xFFFF0000u;
        break;
    default:
        qemu_log_mask(LOG_GUEST_ERROR, "wb55-hsem: write 0x%" HWADDR_PRIx "\n",
                      addr);
    }
}

static const MemoryRegionOps wb55_hsem_ops = {
    .read = wb55_hsem_read,
    .write = wb55_hsem_write,
    .endianness = DEVICE_LITTLE_ENDIAN,
    .impl.min_access_size = 4,
    .impl.max_access_size = 4,
    .valid.min_access_size = 4,
    .valid.max_access_size = 4,
};

static void wb55_hsem_reset_hold(Object *obj, ResetType type)
{
    WB55HsemState *s = WB55_HSEM(obj);

    memset(s->r, 0, sizeof(s->r));
    s->c1ier = s->c1isr = s->c2ier = s->c2isr = s->keyr = 0;
    qemu_set_irq(s->irq, 0);
}

static void wb55_hsem_init(Object *obj)
{
    WB55HsemState *s = WB55_HSEM(obj);

    memory_region_init_io(&s->mmio, obj, &wb55_hsem_ops, s, "hsem", 0x400);
    sysbus_init_mmio(SYS_BUS_DEVICE(obj), &s->mmio);
    sysbus_init_irq(SYS_BUS_DEVICE(obj), &s->irq);
}

static const VMStateDescription vmstate_wb55_hsem = {
    .name = "stm32wb55-hsem",
    .version_id = 1,
    .minimum_version_id = 1,
    .fields = (const VMStateField[]) {
        VMSTATE_UINT32_ARRAY(r, WB55HsemState, 32),
        VMSTATE_UINT32(c1ier, WB55HsemState),
        VMSTATE_UINT32(c1isr, WB55HsemState),
        VMSTATE_UINT32(c2ier, WB55HsemState),
        VMSTATE_UINT32(c2isr, WB55HsemState),
        VMSTATE_UINT32(keyr, WB55HsemState),
        VMSTATE_END_OF_LIST()
    }
};

static void wb55_hsem_class_init(ObjectClass *klass, const void *data)
{
    RESETTABLE_CLASS(klass)->phases.hold = wb55_hsem_reset_hold;
    DEVICE_CLASS(klass)->vmsd = &vmstate_wb55_hsem;
}

static const TypeInfo wb55_hsem_info = {
    .name = TYPE_WB55_HSEM,
    .parent = TYPE_SYS_BUS_DEVICE,
    .instance_size = sizeof(WB55HsemState),
    .instance_init = wb55_hsem_init,
    .class_init = wb55_hsem_class_init,
};

static void wb55_hsem_register(void)
{
    type_register_static(&wb55_hsem_info);
}
type_init(wb55_hsem_register)
