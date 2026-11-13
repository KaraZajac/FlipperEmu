/*
 * STM32WB55 EXTI (RM0434 chapter 14) - CPU1 view
 *
 * WB55 layout differs from STM32L4: RTSR1 0x00, FTSR1 0x04, SWIER1 0x08,
 * PR1 0x0C, RTSR2 0x20, FTSR2 0x24, SWIER2 0x28, PR2 0x2C, C1IMR1 0x80,
 * C1EMR1 0x84, C1IMR2 0x90, C1EMR2 0x94, C2IMR1 0xC0 ... (stm32wb55xx.h).
 *
 * Configurable lines latch PRx on the selected edge only while the line is
 * unmasked for a CPU (C1IMR | C2IMR). Evidence from the firmware itself:
 * furi_hal_gpio_init_ex() only disarms RTSR/FTSR when the line's interrupt is
 * enabled, so on every Flipper furi_hal_rfid_pins_reset() drives PA2 high with
 * line 2 masked but still rising-armed. Tickless idle treats any PR1/PR2 bit
 * as "interrupt pending" (furi_hal_os_is_pending_irq), so if a masked edge
 * latched, real devices would never sleep. Direct lines have no pending bit
 * and follow their input.
 *
 * Outputs: one qemu_irq per line; the SoC ORs them into NVIC vectors.
 *
 * Copyright (c) 2026, Kara Zajac
 * SPDX-License-Identifier: BSD-3-Clause
 */
#include "qemu/osdep.h"
#include "qemu/log.h"
#include "hw/core/irq.h"
#include "migration/vmstate.h"
#include "wb55.h"

/* configurable (latching) lines: 0-21, 31, 33, 40, 41 */
static const uint64_t configurable = 0x3FFFFFull | (1ull << 31) | (1ull << 33) |
                                     (1ull << 40) | (1ull << 41);

static void exti_update(WB55ExtiState *s)
{
    uint64_t pr = s->pr[0] | ((uint64_t)s->pr[1] << 32);
    uint64_t imr = s->imr[0] | ((uint64_t)s->imr[1] << 32);
    uint64_t act = (pr | (s->level & ~configurable)) & imr;

    for (int i = 0; i < WB55_EXTI_LINES; i++) {
        qemu_set_irq(s->out[i], (act >> i) & 1);
    }
}

static void exti_set_line(void *opaque, int line, int level)
{
    WB55ExtiState *s = opaque;
    uint64_t bit = 1ull << line;
    bool old = s->level & bit;
    int r = line / 32;
    uint32_t b = 1u << (line % 32);

    if (level) {
        s->level |= bit;
    } else {
        s->level &= ~bit;
    }
    if ((configurable & bit) && old != !!level &&
        ((s->imr[r] | s->c2imr[r]) & b)) {
        if ((level && (s->rtsr[r] & b)) || (!level && (s->ftsr[r] & b))) {
            s->pr[r] |= b;
        }
    }
    exti_update(s);
}

static uint64_t wb55_exti_read(void *opaque, hwaddr addr, unsigned size)
{
    WB55ExtiState *s = opaque;

    switch (addr) {
    case 0x00: return s->rtsr[0];
    case 0x04: return s->ftsr[0];
    case 0x08: return s->swier[0];
    case 0x0C: return s->pr[0];
    case 0x20: return s->rtsr[1];
    case 0x24: return s->ftsr[1];
    case 0x28: return s->swier[1];
    case 0x2C: return s->pr[1];
    case 0x80: return s->imr[0];
    case 0x84: return s->emr[0];
    case 0x90: return s->imr[1];
    case 0x94: return s->emr[1];
    case 0xC0: return s->c2imr[0];
    case 0xC4: return s->c2emr[0];
    case 0xD0: return s->c2imr[1];
    case 0xD4: return s->c2emr[1];
    case 0x3F0: return 0x00004101;      /* HWCFGR5..: not used by firmware */
    }
    return 0;
}

static void wb55_exti_write(void *opaque, hwaddr addr, uint64_t val64,
                            unsigned size)
{
    WB55ExtiState *s = opaque;
    uint32_t val = val64;

    switch (addr) {
    case 0x00: s->rtsr[0] = val; break;
    case 0x04: s->ftsr[0] = val; break;
    case 0x08:
        s->swier[0] = 0;                /* SWIER sets PR and reads back 0 */
        s->pr[0] |= val & (uint32_t)configurable;
        break;
    case 0x0C: s->pr[0] &= ~val; break; /* write-1-to-clear */
    case 0x20: s->rtsr[1] = val; break;
    case 0x24: s->ftsr[1] = val; break;
    case 0x28:
        s->swier[1] = 0;
        s->pr[1] |= val & (uint32_t)(configurable >> 32);
        break;
    case 0x2C: s->pr[1] &= ~val; break;
    case 0x80: s->imr[0] = val; break;
    case 0x84: s->emr[0] = val; break;
    case 0x90: s->imr[1] = val; break;
    case 0x94: s->emr[1] = val; break;
    case 0xC0: s->c2imr[0] = val; break;
    case 0xC4: s->c2emr[0] = val; break;
    case 0xD0: s->c2imr[1] = val; break;
    case 0xD4: s->c2emr[1] = val; break;
    default:
        qemu_log_mask(LOG_GUEST_ERROR, "wb55-exti: write 0x%" HWADDR_PRIx "\n",
                      addr);
        return;
    }
    exti_update(s);
}

static const MemoryRegionOps wb55_exti_ops = {
    .read = wb55_exti_read,
    .write = wb55_exti_write,
    .endianness = DEVICE_LITTLE_ENDIAN,
    .impl.min_access_size = 4,
    .impl.max_access_size = 4,
    .valid.min_access_size = 4,
    .valid.max_access_size = 4,
};

static void wb55_exti_reset_hold(Object *obj, ResetType type)
{
    WB55ExtiState *s = WB55_EXTI(obj);

    memset(s->rtsr, 0, sizeof(s->rtsr));
    memset(s->ftsr, 0, sizeof(s->ftsr));
    memset(s->swier, 0, sizeof(s->swier));
    memset(s->pr, 0, sizeof(s->pr));
    memset(s->emr, 0, sizeof(s->emr));
    memset(s->c2emr, 0, sizeof(s->c2emr));
    /* RM0434 reset values: direct lines unmasked for CPU1 */
    s->imr[0] = 0x7FC00000;
    s->imr[1] = 0x0001FFFF & ~0x206u;
    s->c2imr[0] = 0;
    s->c2imr[1] = 0;
}

static void wb55_exti_reset_exit(Object *obj, ResetType type)
{
    exti_update(WB55_EXTI(obj));
}

static void wb55_exti_init(Object *obj)
{
    WB55ExtiState *s = WB55_EXTI(obj);

    memory_region_init_io(&s->mmio, obj, &wb55_exti_ops, s, "exti", 0x400);
    sysbus_init_mmio(SYS_BUS_DEVICE(obj), &s->mmio);
    qdev_init_gpio_in(DEVICE(obj), exti_set_line, WB55_EXTI_LINES);
    qdev_init_gpio_out(DEVICE(obj), s->out, WB55_EXTI_LINES);
}

/* A saved state: with the lines' levels, which nobody sends again */
static const VMStateDescription vmstate_wb55_exti = {
    .name = "stm32wb55-exti",
    .version_id = 1,
    .minimum_version_id = 1,
    .fields = (const VMStateField[]) {
        VMSTATE_UINT32_ARRAY(rtsr, WB55ExtiState, 2),
        VMSTATE_UINT32_ARRAY(ftsr, WB55ExtiState, 2),
        VMSTATE_UINT32_ARRAY(swier, WB55ExtiState, 2),
        VMSTATE_UINT32_ARRAY(pr, WB55ExtiState, 2),
        VMSTATE_UINT32_ARRAY(imr, WB55ExtiState, 2),
        VMSTATE_UINT32_ARRAY(emr, WB55ExtiState, 2),
        VMSTATE_UINT32_ARRAY(c2imr, WB55ExtiState, 2),
        VMSTATE_UINT32_ARRAY(c2emr, WB55ExtiState, 2),
        VMSTATE_UINT64(level, WB55ExtiState),
        VMSTATE_END_OF_LIST()
    }
};

static void wb55_exti_class_init(ObjectClass *klass, const void *data)
{
    ResettableClass *rc = RESETTABLE_CLASS(klass);

    rc->phases.hold = wb55_exti_reset_hold;
    rc->phases.exit = wb55_exti_reset_exit;
    DEVICE_CLASS(klass)->vmsd = &vmstate_wb55_exti;
}

static const TypeInfo wb55_exti_info = {
    .name = TYPE_WB55_EXTI,
    .parent = TYPE_SYS_BUS_DEVICE,
    .instance_size = sizeof(WB55ExtiState),
    .instance_init = wb55_exti_init,
    .class_init = wb55_exti_class_init,
};

static void wb55_exti_register(void)
{
    type_register_static(&wb55_exti_info);
}
type_init(wb55_exti_register)
