/*
 * STM32WB55 low-power timer (RM0434 chapter 30)
 *
 * Its clock is the one RCC_CCIPR picks for it (the SoC tells: PCLK1, LSI,
 * HSI16 or LSE), divided by CFGR.PRESC.
 *
 * LPTIM1 is the firmware's tickless-idle wakeup timer
 * (targets/f7/furi_hal/furi_hal_idle_timer.h): ENABLE, poll until enabled,
 * CMPMIE, CMP = n-4, ARR = n-1, SNGSTRT, then WFI with PRIMASK=1. After
 * waking it reads CNT, checks CMPM && ARRM, and stops the timer with an RCC
 * bus reset (APB1RSTR1.LPTIM1RST) - no LPTIM1 ISR is ever installed, so the
 * reset must drop the interrupt line, which device reset does here.
 *
 * LPTIM2 is one of the firmware's two PWM outputs (furi_hal_pwm.c): it runs
 * on and on from PCLK1, 64 MHz, with no interrupt.  So the counter is worked
 * out from the time when somebody asks, and a QEMU timer is set for the next
 * compare or reload only while one of the two has its interrupt on: a PWM
 * costs nothing while it runs.  What is on the output is not kept as a level;
 * wb55_lptim_pwm() tells the board the period and the high part.
 *
 * Copyright (c) 2026, Kara Zajac
 * SPDX-License-Identifier: BSD-3-Clause
 */
#include "qemu/osdep.h"
#include "qemu/log.h"
#include "hw/core/irq.h"
#include "migration/vmstate.h"
#include "wb55.h"

#define ISR_CMPM  (1u << 0)
#define ISR_ARRM  (1u << 1)
#define ISR_CMPOK (1u << 3)
#define ISR_ARROK (1u << 4)

#define CR_ENABLE  (1u << 0)
#define CR_SNGSTRT (1u << 1)
#define CR_CNTSTRT (1u << 2)
#define CR_COUNTRST (1u << 3)

#define CFGR_WAVE   (1u << 20)
#define CFGR_WAVPOL (1u << 21)

#define LSE_HZ 32768

static uint32_t lptim_clock(WB55LptimState *s)
{
    return s->clock_hz ? s->clock_hz(s->clock_opaque) : LSE_HZ;
}

static uint32_t lptim_hz(WB55LptimState *s)
{
    return MAX(lptim_clock(s) >> ((s->cfgr >> 9) & 7), 1);
}

/* is an interrupt on that the counter can raise? */
static bool lptim_wanted(WB55LptimState *s)
{
    return s->ier & (ISR_CMPM | ISR_ARRM);
}

static uint64_t lptim_ticks(WB55LptimState *s, int64_t now)
{
    return muldiv64(now - s->start_ns, lptim_hz(s), NANOSECONDS_PER_SECOND);
}

static int64_t lptim_tick_time(WB55LptimState *s, uint64_t tick)
{
    /* time at which the counter reaches `tick` (rounded up) */
    return s->start_ns + (int64_t)muldiv64(tick, NANOSECONDS_PER_SECOND,
                                           lptim_hz(s)) + 1;
}

static void lptim_update_irq(WB55LptimState *s)
{
    qemu_set_irq(s->irq, !!(s->isr & s->ier & 0x7F));
}

static uint32_t lptim_period(WB55LptimState *s)
{
    return (s->arr & 0xFFFF) + 1;
}

static void lptim_schedule(WB55LptimState *s)
{
    if (!s->running || !lptim_wanted(s)) {
        timer_del(s->timer);
        return;
    }
    int64_t now = qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL);
    uint64_t t = lptim_ticks(s, now);
    uint32_t cmp = s->cmp & 0xFFFF, arr = s->arr & 0xFFFF;
    uint64_t next;

    if (s->oneshot) {
        next = (t < cmp && cmp <= arr) ? cmp : arr;
        if (next <= t) {
            next = t + 1;
        }
    } else {
        uint32_t period = lptim_period(s);
        uint64_t base = t - (t % period);
        uint32_t pos = t % period;
        if (cmp > pos && cmp <= arr) {
            next = base + cmp;
        } else if (arr > pos) {
            next = base + arr;
        } else {
            next = base + period + MIN(cmp, arr);
        }
    }
    timer_mod(s->timer, lptim_tick_time(s, next));
}

static void lptim_events(WB55LptimState *s, uint64_t from, uint64_t to)
{
    uint32_t cmp = s->cmp & 0xFFFF, arr = s->arr & 0xFFFF;

    if (to <= from) {
        return;
    }
    if (s->oneshot) {
        if (from < cmp && cmp <= to && cmp <= arr) {
            s->isr |= ISR_CMPM;
        }
        if (to >= arr) {
            s->isr |= ISR_ARRM;
            if (cmp <= arr) {
                s->isr |= ISR_CMPM;
            }
            s->running = false;         /* one-shot: stop at ARR */
            s->stopped_cnt = 0;
        }
        return;
    }
    uint32_t period = lptim_period(s);
    if (to - from >= period) {
        s->isr |= ISR_ARRM | (cmp <= arr ? ISR_CMPM : 0);
        return;
    }
    uint32_t a = from % period, b = to % period;
    bool wrapped = b < a || (to - from) >= period;
    if ((!wrapped && a < cmp && cmp <= b) ||
        (wrapped && (cmp > a || cmp <= b))) {
        if (cmp <= arr) {
            s->isr |= ISR_CMPM;
        }
    }
    if ((!wrapped && a < arr && arr <= b) || (wrapped && arr > a)) {
        s->isr |= ISR_ARRM;
    }
}

static void lptim_timer_cb(void *opaque)
{
    WB55LptimState *s = opaque;
    int64_t now = qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL);
    uint64_t t;

    if (!s->running) {
        return;
    }
    t = lptim_ticks(s, now);
    lptim_events(s, s->stopped_cnt, t);
    if (s->running) {
        s->stopped_cnt = t;             /* last tick processed */
    }
    lptim_update_irq(s);
    lptim_schedule(s);
}

static uint32_t lptim_cnt(WB55LptimState *s)
{
    if (!s->running) {
        return s->stopped_cnt & 0xFFFF;
    }
    uint64_t t = lptim_ticks(s, qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL));
    if (s->oneshot) {
        return MIN(t, (uint64_t)(s->arr & 0xFFFF));
    }
    return t % lptim_period(s);
}

static void lptim_start(WB55LptimState *s, bool oneshot)
{
    s->oneshot = oneshot;
    s->running = true;
    s->start_ns = qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL);
    s->stopped_cnt = 0;                 /* used as "last processed tick" */
    lptim_schedule(s);
}

static uint64_t wb55_lptim_read(void *opaque, hwaddr addr, unsigned size)
{
    WB55LptimState *s = opaque;

    switch (addr) {
    case 0x00:
        if (s->running) {
            lptim_timer_cb(s);          /* bring flags up to date */
        }
        return s->isr;
    case 0x08: return s->ier;
    case 0x0C: return s->cfgr;
    case 0x10: return s->cr & CR_ENABLE;
    case 0x14: return s->cmp;
    case 0x18: return s->arr;
    case 0x1C:
        if (s->running && !lptim_wanted(s)) {
            lptim_timer_cb(s);          /* (a single run may be over) */
        }
        return lptim_cnt(s);
    case 0x20: return s->or_;
    }
    return 0;
}

/*
 * The output as a PWM: it is one while the counter runs on and on (CNTSTRT)
 * and CFGR.WAVE is 0.  A period is the counter's way from 0 to ARR, ARR + 1
 * ticks.  The output is set when the counter is past CMP and reset when it
 * is at 0 again (RM0434 30.4.10), so it is high for ARR - CMP ticks; with
 * the polarity turned (CFGR.WAVPOL) for the other CMP + 1.
 */
bool wb55_lptim_pwm(WB55LptimState *s, uint32_t *clock, uint32_t *divider,
                    uint32_t *period, uint32_t *high)
{
    uint32_t cmp = s->cmp & 0xFFFF, arr = s->arr & 0xFFFF;

    if (!s->running || s->oneshot || (s->cfgr & CFGR_WAVE)) {
        return false;
    }
    *clock = lptim_clock(s);
    *divider = 1u << ((s->cfgr >> 9) & 7);
    *period = arr + 1;
    *high = cmp < arr ? arr - cmp : 0;
    if (s->cfgr & CFGR_WAVPOL) {
        *high = *period - *high;
    }
    return true;
}

static void wb55_lptim_write(void *opaque, hwaddr addr, uint64_t val64,
                             unsigned size)
{
    WB55LptimState *s = opaque;
    uint32_t val = val64;

    switch (addr) {
    case 0x04:                          /* ICR */
        s->isr &= ~(val & 0x7F);
        break;
    case 0x08:
        s->ier = val & 0x7F;
        if (s->running) {
            lptim_timer_cb(s);          /* flags to date; the timer set or not */
        }
        break;
    case 0x0C:
        s->cfgr = val;
        break;
    case 0x10:
        if (!(val & CR_ENABLE)) {
            s->cr = 0;
            s->running = false;
            s->stopped_cnt = 0;
            timer_del(s->timer);
            break;
        }
        s->cr = CR_ENABLE;
        if (val & CR_COUNTRST) {
            s->start_ns = qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL);
            s->stopped_cnt = 0;
        }
        if (val & CR_SNGSTRT) {
            lptim_start(s, true);
        } else if (val & CR_CNTSTRT) {
            lptim_start(s, false);
        }
        break;
    case 0x14:
        s->cmp = val & 0xFFFF;
        s->isr |= ISR_CMPOK;
        lptim_schedule(s);
        break;
    case 0x18:
        s->arr = val & 0xFFFF;
        s->isr |= ISR_ARROK;
        lptim_schedule(s);
        break;
    case 0x20:
        s->or_ = val & 3;
        break;
    default:
        qemu_log_mask(LOG_GUEST_ERROR, "wb55-lptim: write 0x%" HWADDR_PRIx "\n",
                      addr);
    }
    lptim_update_irq(s);
    if (s->changed) {
        s->changed(s->changed_opaque);
    }
}

static const MemoryRegionOps wb55_lptim_ops = {
    .read = wb55_lptim_read,
    .write = wb55_lptim_write,
    .endianness = DEVICE_LITTLE_ENDIAN,
    .impl.min_access_size = 4,
    .impl.max_access_size = 4,
    .valid.min_access_size = 1,
    .valid.max_access_size = 4,
};

static void wb55_lptim_reset_hold(Object *obj, ResetType type)
{
    WB55LptimState *s = WB55_LPTIM(obj);

    s->isr = s->ier = s->cfgr = s->cr = s->cmp = s->or_ = 0;
    s->arr = 1;
    s->running = false;
    s->stopped_cnt = 0;
    timer_del(s->timer);
    qemu_set_irq(s->irq, 0);
    if (s->changed) {
        s->changed(s->changed_opaque);
    }
}

static void wb55_lptim_init(Object *obj)
{
    WB55LptimState *s = WB55_LPTIM(obj);

    memory_region_init_io(&s->mmio, obj, &wb55_lptim_ops, s, "lptim", 0x400);
    sysbus_init_mmio(SYS_BUS_DEVICE(obj), &s->mmio);
    sysbus_init_irq(SYS_BUS_DEVICE(obj), &s->irq);
    s->timer = timer_new_ns(QEMU_CLOCK_VIRTUAL, lptim_timer_cb, s);
}

static const VMStateDescription vmstate_wb55_lptim = {
    .name = "stm32wb55-lptim",
    .version_id = 1,
    .minimum_version_id = 1,
    .fields = (const VMStateField[]) {
        VMSTATE_TIMER_PTR(timer, WB55LptimState),
        VMSTATE_UINT32(isr, WB55LptimState),
        VMSTATE_UINT32(ier, WB55LptimState),
        VMSTATE_UINT32(cfgr, WB55LptimState),
        VMSTATE_UINT32(cr, WB55LptimState),
        VMSTATE_UINT32(cmp, WB55LptimState),
        VMSTATE_UINT32(arr, WB55LptimState),
        VMSTATE_UINT32(or_, WB55LptimState),
        VMSTATE_BOOL(running, WB55LptimState),
        VMSTATE_BOOL(oneshot, WB55LptimState),
        VMSTATE_INT64(start_ns, WB55LptimState),
        VMSTATE_UINT32(stopped_cnt, WB55LptimState),
        VMSTATE_END_OF_LIST()
    }
};

static void wb55_lptim_class_init(ObjectClass *klass, const void *data)
{
    RESETTABLE_CLASS(klass)->phases.hold = wb55_lptim_reset_hold;
    DEVICE_CLASS(klass)->vmsd = &vmstate_wb55_lptim;
}

static const TypeInfo wb55_lptim_info = {
    .name = TYPE_WB55_LPTIM,
    .parent = TYPE_SYS_BUS_DEVICE,
    .instance_size = sizeof(WB55LptimState),
    .instance_init = wb55_lptim_init,
    .class_init = wb55_lptim_class_init,
};

static void wb55_lptim_register(void)
{
    type_register_static(&wb55_lptim_info);
}
type_init(wb55_lptim_register)
