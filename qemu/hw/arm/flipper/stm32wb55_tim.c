/*
 * STM32WB55 general-purpose / advanced timer (TIM2 today; TIM1/16/17 share
 * the register layout)
 *
 * RM0434 "General-purpose timers (TIM2)".  Modelled for what the firmware
 * uses the timers for:
 *  - an up-counter in QEMU virtual time, computed lazily: PSC, ARR, CNT, CEN,
 *    UG, one-pulse mode, update and compare events with their interrupts;
 *  - input capture from edges the board reports with an exact timestamp:
 *    CCxS direct/indirect selection, CCxP/CCxNP polarity, CCxIF/CCxOF;
 *  - the slave controller's reset mode on TI1FP1/TI2FP2/TI1F_ED (furi_hal_
 *    infrared measures marks and spaces that way).
 *  Not modelled (yet): output pins, DMA requests, encoder/gated modes,
 *  input prescalers and filters, down/centre counting.
 *
 * Copyright (c) 2026, Kara Zajac
 * SPDX-License-Identifier: BSD-3-Clause
 */
#include "qemu/osdep.h"
#include "qemu/log.h"
#include "qemu/timer.h"
#include "qapi/error.h"
#include "hw/core/irq.h"
#include "hw/core/qdev-clock.h"
#include "hw/core/qdev-properties.h"
#include "wb55.h"

#define R_CR1   0x00
#define R_SMCR  0x08
#define R_DIER  0x0C
#define R_SR    0x10
#define R_EGR   0x14
#define R_CCMR1 0x18
#define R_CCMR2 0x1C
#define R_CCER  0x20
#define R_CNT   0x24
#define R_PSC   0x28
#define R_ARR   0x2C
#define R_CCR1  0x34

#define CR1_CEN  (1u << 0)
#define CR1_UDIS (1u << 1)
#define CR1_URS  (1u << 2)
#define CR1_OPM  (1u << 3)
#define SR_UIF   (1u << 0)
#define SR_TIF   (1u << 6)
#define SR_IRQS  0xFFu                  /* UIF CC1-4IF COMIF TIF BIF */

#define REG(s, off) ((s)->regs[(off) / 4])

static uint32_t tim_arr(WB55TimState *s)
{
    return s->width == 32 ? REG(s, R_ARR) : (REG(s, R_ARR) & 0xFFFF);
}

static uint64_t tim_hz(WB55TimState *s)
{
    uint64_t hz = clock_get_hz(s->clk);

    return (hz ? hz : 64000000) / ((REG(s, R_PSC) & 0xFFFF) + 1);
}

/* ticks counted between base_ns and when (no wrapping) */
static uint64_t tim_ticks(WB55TimState *s, int64_t when)
{
    if (!s->running || when <= s->base_ns) {
        return 0;
    }
    return muldiv64(when - s->base_ns, tim_hz(s), NANOSECONDS_PER_SECOND);
}

static uint32_t tim_cnt_at(WB55TimState *s, int64_t when)
{
    uint64_t period = (uint64_t)tim_arr(s) + 1;
    uint64_t t = s->cnt_base + tim_ticks(s, when);

    return t % period;
}

/* first virtual time at which `ticks` ticks have elapsed (rounded up, so a
 * timer armed for it always finds the event when it fires) */
static int64_t tim_time_of_tick(WB55TimState *s, uint64_t ticks)
{
    return s->base_ns + muldiv64_round_up(ticks, NANOSECONDS_PER_SECOND,
                                          tim_hz(s));
}

static void tim_update_irq(WB55TimState *s)
{
    qemu_set_irq(s->irq, (REG(s, R_SR) & REG(s, R_DIER) & SR_IRQS) != 0);
}

/* restart counting from cnt at time when */
static void tim_rebase(WB55TimState *s, uint32_t cnt, int64_t when)
{
    s->cnt_base = cnt;
    s->base_ns = when;
    s->checked_ns = when;
}

static bool cc_is_output(WB55TimState *s, int ch)
{
    uint32_t ccmr = ch < 2 ? REG(s, R_CCMR1) : REG(s, R_CCMR2);

    return ((ccmr >> ((ch & 1) * 8)) & 3) == 0;
}

/*
 * Bring the event flags up to date with virtual time: every update
 * (overflow) and output-compare match between checked_ns and now.
 */
static void tim_catch_up(WB55TimState *s, int64_t now)
{
    uint64_t period = (uint64_t)tim_arr(s) + 1;
    uint64_t t0, t1;

    if (!s->running || now <= s->checked_ns) {
        s->checked_ns = MAX(s->checked_ns, now);
        return;
    }
    t0 = s->cnt_base + tim_ticks(s, s->checked_ns);
    t1 = s->cnt_base + tim_ticks(s, now);
    if (t1 / period > t0 / period) {        /* wrapped at least once */
        if (!(REG(s, R_CR1) & CR1_UDIS)) {
            REG(s, R_SR) |= SR_UIF;
        }
        if (REG(s, R_CR1) & CR1_OPM) {      /* one-pulse: stop at update */
            REG(s, R_CR1) &= ~CR1_CEN;
            s->running = false;
            tim_rebase(s, 0, now);
            return;
        }
    }
    for (int ch = 0; ch < 4; ch++) {
        uint64_t ccr = REG(s, R_CCR1 + 4 * ch);
        uint64_t first;

        if (!cc_is_output(s, ch) || ccr > period - 1) {
            continue;
        }
        /* first tick > t0 at which the counter equals ccr */
        first = (t0 / period) * period + ccr;
        if (first <= t0) {
            first += period;
        }
        if (first <= t1) {
            REG(s, R_SR) |= 1u << (ch + 1);
        }
    }
    s->checked_ns = now;
}

/* arm the QEMU timer for the next event that raises an interrupt */
static void tim_schedule(WB55TimState *s, int64_t now)
{
    uint64_t period = (uint64_t)tim_arr(s) + 1;
    uint32_t dier = REG(s, R_DIER);
    uint64_t t = s->cnt_base + tim_ticks(s, now), next = UINT64_MAX;

    timer_del(s->timer);
    if (!s->running) {
        return;
    }
    if (dier & SR_UIF) {
        next = (t / period + 1) * period;
    }
    for (int ch = 0; ch < 4; ch++) {
        uint64_t ccr = REG(s, R_CCR1 + 4 * ch), n;

        if (!(dier & (1u << (ch + 1))) || !cc_is_output(s, ch) ||
            ccr > period - 1) {
            continue;
        }
        n = (t / period) * period + ccr;
        if (n <= t) {
            n += period;
        }
        next = MIN(next, n);
    }
    if (next != UINT64_MAX) {
        timer_mod(s->timer, tim_time_of_tick(s, next - s->cnt_base));
    }
}

static void tim_tick(void *opaque)
{
    WB55TimState *s = opaque;
    int64_t now = qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL);

    tim_catch_up(s, now);
    tim_update_irq(s);
    tim_schedule(s, now);
}

/*
 * An edge on input TIx (0..3), at an exact virtual time.  Captures into the
 * channels that select that input, then runs the slave reset if this edge
 * is its trigger.
 */
void wb55_tim_input(WB55TimState *s, int ti, bool level, int64_t when)
{
    static const int8_t direct[4] = { 0, 1, 2, 3 }, indirect[4] = { 1, 0, 3, 2 };
    uint32_t ccer = REG(s, R_CCER), smcr = REG(s, R_SMCR);
    int sms = (smcr & 7) | ((smcr >> 13) & 8);
    int ts = ((smcr >> 4) & 7) | ((smcr >> 17) & 0x18);
    bool rising = level && !s->ti_level[ti];
    bool falling = !level && s->ti_level[ti];
    int64_t now = qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL);

    s->ti_level[ti] = level;
    if (!rising && !falling) {
        return;
    }
    tim_catch_up(s, MIN(when, now));
    uint32_t cnt = s->running ? tim_cnt_at(s, when) : s->cnt_base;

    for (int ch = 0; ch < 4; ch++) {
        uint32_t ccmr = ch < 2 ? REG(s, R_CCMR1) : REG(s, R_CCMR2);
        int ccs = (ccmr >> ((ch & 1) * 8)) & 3;
        int pol = ((ccer >> (4 * ch + 1)) & 1) | (((ccer >> (4 * ch + 3)) & 1) << 1);
        bool hit;

        if (!(ccer & (1u << (4 * ch))) ||
            !((ccs == 1 && direct[ch] == ti) || (ccs == 2 && indirect[ch] == ti))) {
            continue;
        }
        /* CCxNP:CCxP = 00 rising, 01 falling, 11 both */
        hit = pol == 3 ? true : pol == 1 ? falling : rising;
        if (!hit) {
            continue;
        }
        if (REG(s, R_SR) & (1u << (ch + 1))) {
            REG(s, R_SR) |= 1u << (ch + 9);     /* CCxOF: overcapture */
        }
        REG(s, R_SR) |= 1u << (ch + 1);
        REG(s, R_CCR1 + 4 * ch) = cnt;
    }

    /* slave controller, reset mode: TS 100 TI1F_ED, 101 TI1FP1, 110 TI2FP2 */
    if (sms == 4 && ((ts == 4 && ti == 0) || (ts == 5 && ti == 0) ||
                     (ts == 6 && ti == 1))) {
        int pol = ts == 4 ? 3 : ((ccer >> (4 * ti + 1)) & 1) |
                                (((ccer >> (4 * ti + 3)) & 1) << 1);
        bool trig = pol == 3 ? true : pol == 1 ? falling : rising;

        if (trig) {
            tim_rebase(s, 0, when);
            REG(s, R_SR) |= SR_TIF;
        }
    }
    tim_update_irq(s);
    tim_schedule(s, now);
}

bool wb55_tim_capture_pending(WB55TimState *s)
{
    return REG(s, R_SR) & REG(s, R_DIER) & 0x1E;  /* CCxIF with CCxIE */
}

static uint64_t tim_read(void *opaque, hwaddr addr, unsigned size)
{
    WB55TimState *s = opaque;
    int64_t now = qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL);

    if (addr >= sizeof(s->regs)) {
        return 0;
    }
    switch (addr) {
    case R_SR:
        tim_catch_up(s, now);
        tim_update_irq(s);
        break;
    case R_CNT:
        return s->running ? tim_cnt_at(s, now) : s->cnt_base;
    }
    return s->regs[addr / 4];
}

static void tim_write(void *opaque, hwaddr addr, uint64_t val64, unsigned size)
{
    WB55TimState *s = opaque;
    int64_t now = qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL);
    uint32_t val = val64;
    uint32_t cnt;

    if (addr >= sizeof(s->regs)) {
        return;
    }
    if (size < 4) {                     /* byte/halfword: read-modify-write */
        val = deposit32(s->regs[addr / 4], (addr & 3) * 8, size * 8, val);
        addr &= ~3;
    }
    tim_catch_up(s, now);
    cnt = s->running ? tim_cnt_at(s, now) : s->cnt_base;

    switch (addr) {
    case R_CR1:
        REG(s, R_CR1) = val;
        if ((val & CR1_CEN) && !s->running) {
            s->running = true;
            tim_rebase(s, cnt, now);
        } else if (!(val & CR1_CEN) && s->running) {
            s->running = false;
            tim_rebase(s, cnt, now);
        }
        break;
    case R_SR:
        REG(s, R_SR) &= val;            /* rc_w0 */
        break;
    case R_EGR:
        if (val & 1) {                  /* UG: reinitialise the counter */
            tim_rebase(s, 0, now);
            if (!(REG(s, R_CR1) & CR1_URS)) {
                REG(s, R_SR) |= SR_UIF;
            }
        }
        for (int ch = 0; ch < 4; ch++) {  /* CCxG */
            if (val & (1u << (ch + 1))) {
                if (!cc_is_output(s, ch)) {
                    REG(s, R_CCR1 + 4 * ch) = cnt;
                }
                REG(s, R_SR) |= 1u << (ch + 1);
            }
        }
        REG(s, R_SR) |= val & (SR_TIF | (1u << 5) | (1u << 7));
        break;
    case R_CNT:
        tim_rebase(s, s->width == 32 ? val : (val & 0xFFFF), now);
        break;
    case R_PSC:
    case R_ARR:
        REG(s, addr) = val;             /* preload ignored: effective now */
        tim_rebase(s, MIN(cnt, tim_arr(s)), now);
        break;
    default:
        s->regs[addr / 4] = val;
        break;
    }
    tim_update_irq(s);
    tim_schedule(s, now);
}

static const MemoryRegionOps tim_ops = {
    .read = tim_read,
    .write = tim_write,
    .endianness = DEVICE_LITTLE_ENDIAN,
    .impl.min_access_size = 1, .impl.max_access_size = 4,
    .valid.min_access_size = 1, .valid.max_access_size = 8,
};

static void tim_reset_hold(Object *obj, ResetType type)
{
    WB55TimState *s = WB55_TIM(obj);

    memset(s->regs, 0, sizeof(s->regs));
    REG(s, R_ARR) = s->width == 32 ? 0xFFFFFFFF : 0xFFFF;
    s->running = false;
    tim_rebase(s, 0, qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL));
    if (s->timer) {
        timer_del(s->timer);
    }
}

static void tim_reset_exit(Object *obj, ResetType type)
{
    tim_update_irq(WB55_TIM(obj));
}

static void tim_init(Object *obj)
{
    WB55TimState *s = WB55_TIM(obj);

    memory_region_init_io(&s->mmio, obj, &tim_ops, s, "stm32wb55-tim", 0x400);
    sysbus_init_mmio(SYS_BUS_DEVICE(obj), &s->mmio);
    sysbus_init_irq(SYS_BUS_DEVICE(obj), &s->irq);
    s->clk = qdev_init_clock_in(DEVICE(obj), "clk", NULL, NULL, 0);
}

static void tim_realize(DeviceState *dev, Error **errp)
{
    WB55TimState *s = WB55_TIM(dev);

    if (s->width != 16 && s->width != 32) {
        error_setg(errp, "stm32wb55-tim: width must be 16 or 32");
        return;
    }
    s->timer = timer_new_ns(QEMU_CLOCK_VIRTUAL, tim_tick, s);
}

static const Property tim_props[] = {
    DEFINE_PROP_UINT32("width", WB55TimState, width, 16),
};

static void tim_class_init(ObjectClass *klass, const void *data)
{
    DeviceClass *dc = DEVICE_CLASS(klass);
    ResettableClass *rc = RESETTABLE_CLASS(klass);

    dc->realize = tim_realize;
    dc->user_creatable = false;
    device_class_set_props(dc, tim_props);
    rc->phases.hold = tim_reset_hold;
    rc->phases.exit = tim_reset_exit;
}

static const TypeInfo tim_types[] = {
    {
        .name = TYPE_WB55_TIM,
        .parent = TYPE_SYS_BUS_DEVICE,
        .instance_size = sizeof(WB55TimState),
        .instance_init = tim_init,
        .class_init = tim_class_init,
    },
};
DEFINE_TYPES(tim_types)
