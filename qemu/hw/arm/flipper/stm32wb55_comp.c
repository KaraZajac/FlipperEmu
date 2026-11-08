/*
 * STM32WB55 comparators COMP1/COMP2 (RM0434 chapter 21)
 *
 * The analog side is reduced to one fact per comparator, given by the board
 * with an exact virtual time: is input+ above input- (wb55_comp_input)?  The
 * digital side is modelled: EN, POLARITY, the read-only VALUE bit, LOCK, the
 * EXTI lines (20 for COMP1, 21 for COMP2) and a board hook for the output,
 * which the SoC routes to TIM2's TI4 when TIM2_OR selects a comparator.
 *
 * On the Flipper, COMP1 compares the demodulated 125 kHz RFID antenna signal
 * (PC5, RFID_RF_IN) with VREFINT/2: furi_hal_rfid reads tags by capturing
 * its output edges with TIM2, and iButton's Cyfral/Metakom readers use its
 * interrupt.
 *
 * Copyright (c) 2026, Kara Zajac
 * SPDX-License-Identifier: BSD-3-Clause
 */
#include "qemu/osdep.h"
#include "qemu/log.h"
#include "qemu/timer.h"
#include "hw/core/irq.h"
#include "wb55.h"

#define CSR_EN       (1u << 0)
#define CSR_POLARITY (1u << 15)
#define CSR_VALUE    (1u << 30)
#define CSR_LOCK     (1u << 31)

static void comp_update(WB55CompState *s, int n, int64_t when)
{
    bool out = (s->csr[n] & CSR_EN) &&
               (s->inp[n] != !!(s->csr[n] & CSR_POLARITY));

    if (out == s->out[n]) {
        return;
    }
    s->out[n] = out;
    qemu_set_irq(s->exti[n], out);
    if (s->out_changed) {
        s->out_changed(s->out_opaque, n, out, when);
    }
}

void wb55_comp_input(WB55CompState *s, int n, bool above, int64_t when)
{
    s->inp[n] = above;
    comp_update(s, n, when);
}

static uint64_t comp_read(void *opaque, hwaddr addr, unsigned size)
{
    WB55CompState *s = opaque;
    int n = addr / 4;

    if (n > 1) {
        return 0;
    }
    return (s->csr[n] & ~CSR_VALUE) | (s->out[n] ? CSR_VALUE : 0);
}

static void comp_write(void *opaque, hwaddr addr, uint64_t val, unsigned size)
{
    WB55CompState *s = opaque;
    int n = addr / 4;

    if (n > 1 || (s->csr[n] & CSR_LOCK)) {
        return;                         /* LOCK: read-only until reset */
    }
    if (size < 4) {
        val = deposit32(s->csr[n], (addr & 3) * 8, size * 8, val);
    }
    s->csr[n] = val & ~CSR_VALUE;
    comp_update(s, n, qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL));
}

static const MemoryRegionOps comp_ops = {
    .read = comp_read,
    .write = comp_write,
    .endianness = DEVICE_LITTLE_ENDIAN,
    .impl.min_access_size = 4, .impl.max_access_size = 4,
    .valid.min_access_size = 1, .valid.max_access_size = 4,
};

static void comp_reset_hold(Object *obj, ResetType type)
{
    WB55CompState *s = WB55_COMP(obj);

    s->csr[0] = s->csr[1] = 0;
}

static void comp_reset_exit(Object *obj, ResetType type)
{
    WB55CompState *s = WB55_COMP(obj);

    for (int n = 0; n < 2; n++) {
        comp_update(s, n, qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL));
    }
}

static void comp_init(Object *obj)
{
    WB55CompState *s = WB55_COMP(obj);

    memory_region_init_io(&s->mmio, obj, &comp_ops, s, "stm32wb55-comp", 0x200);
    sysbus_init_mmio(SYS_BUS_DEVICE(obj), &s->mmio);
    qdev_init_gpio_out_named(DEVICE(obj), s->exti, "exti", 2);
}

static void comp_class_init(ObjectClass *klass, const void *data)
{
    ResettableClass *rc = RESETTABLE_CLASS(klass);

    DEVICE_CLASS(klass)->user_creatable = false;
    rc->phases.hold = comp_reset_hold;
    rc->phases.exit = comp_reset_exit;
}

static const TypeInfo comp_types[] = {
    {
        .name = TYPE_WB55_COMP,
        .parent = TYPE_SYS_BUS_DEVICE,
        .instance_size = sizeof(WB55CompState),
        .instance_init = comp_init,
        .class_init = comp_class_init,
    },
};
DEFINE_TYPES(comp_types)
