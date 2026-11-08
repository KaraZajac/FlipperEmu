/*
 * STM32WB55 general-purpose / advanced timers (TIM1 and TIM2; TIM16/17
 * share the register layout)
 *
 * RM0434 "General-purpose timers (TIM2)".  Modelled for what the firmware
 * uses the timers for:
 *  - an up-counter in QEMU virtual time, computed lazily: PSC, ARR, CNT, CEN,
 *    UG, one-pulse mode, update and compare events with their interrupts;
 *  - input capture from edges the board reports with an exact timestamp:
 *    CCxS direct/indirect selection, CCxP/CCxNP polarity, CCxIF/CCxOF;
 *  - the slave controller's reset mode on TI1FP1/TI2FP2/TI1F_ED (furi_hal_
 *    infrared measures marks and spaces that way).
 *  - update DMA requests (UDE) and output compare toggle at update
 *    (CCRx = 0): while those are in use, every update is processed at its
 *    exact time, with the DMA's write landing in that instant.  That is how
 *    furi_hal_subghz transmits: DMA feeds ARR from a circular buffer, and
 *    CH2 toggles the CC1101's data pin at each update.  ARR = 0 blocks the
 *    counter, which is how the firmware ends a transmission.
 *  - external clock mode 2 (SMCR.ECE) and mode 1 on ETRF, from a clock the
 *    board puts on ETR (wb55_tim_set_ext_clock): furi_hal_rfid emulates a tag
 *    with TIM2 counting the reader's 125 kHz carrier, DMA feeding ARR and
 *    CCR3 every period, and CH3 in PWM mode loading the antenna.  PWM
 *    channels (OCxM 110/111) report both edges of each period to the board.
 *  - TIM1's repetition counter: an update event every RCR+1 overflows, RCR
 *    reloaded at each, UG raising the update DMA request (unless URS); the
 *    infrared transmitter counts carrier periods per mark or space that way.
 *  With "batch-dma" (TIM2), while no timer interrupt is enabled (DMA only),
 *  updates are replayed in batches every TIM_BATCH_NS rather than one QEMU
 *  timer event each: a PSK tag reprograms the timer every 16 µs.  That is
 *  only safe for circular DMA; furi_hal_infrared re-arms its normal-mode
 *  channels from an ISR between two updates, so TIM1 replays each on time.
 *  Not modelled (yet): PWM output pins, DMA on compare events, encoder and
 *  gated modes, input prescalers and filters, down/centre counting, BDTR
 *  dead time and break.
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
#define R_RCR   0x30
#define R_CCR1  0x34

#define CR1_CEN  (1u << 0)
#define CR1_UDIS (1u << 1)
#define CR1_URS  (1u << 2)
#define CR1_OPM  (1u << 3)
#define SR_UIF   (1u << 0)
#define SR_TIF   (1u << 6)
#define SR_IRQS  0xFFu                  /* UIF CC1-4IF COMIF TIF BIF */

#define REG(s, off) ((s)->regs[(off) / 4])
#define DIER_UDE (1u << 8)
#define SMCR_ECE (1u << 14)
#define TIM_BATCH_NS (1 * SCALE_MS)
#define TIM_DMA_WAIT_NS (250 * SCALE_MS)    /* DMA left dry for good */
#define TIM_DMA_POLL_NS (20 * SCALE_US)

static int64_t tim_now(WB55TimState *s)
{
    return s->now_override ? s->now_override
                           : qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL);
}

static int oc_mode(WB55TimState *s, int ch)
{
    uint32_t ccmr = ch < 2 ? REG(s, R_CCMR1) : REG(s, R_CCMR2);
    int sh = (ch & 1) * 8;

    return ((ccmr >> (sh + 4)) & 7) | ((ccmr >> (sh + 13)) & 8);
}

/* channels that toggle their pin at every update (OCxM toggle, CCRx = 0) */
static uint32_t tim_update_toggles(WB55TimState *s)
{
    uint32_t mask = 0;

    for (int ch = 0; ch < 4; ch++) {
        uint32_t ccmr = ch < 2 ? REG(s, R_CCMR1) : REG(s, R_CCMR2);

        if (((ccmr >> ((ch & 1) * 8)) & 3) == 0 && oc_mode(s, ch) == 3 &&
            REG(s, R_CCR1 + 4 * ch) == 0 && (REG(s, R_CCER) & (1u << (4 * ch)))) {
            mask |= 1u << ch;
        }
    }
    return mask;
}

/* updates must be handled one by one, at their exact time */
static bool tim_event_driven(WB55TimState *s)
{
    return s->running && ((REG(s, R_DIER) & DIER_UDE) || tim_update_toggles(s));
}

static uint32_t tim_arr(WB55TimState *s)
{
    return s->width == 32 ? REG(s, R_ARR) : (REG(s, R_ARR) & 0xFFFF);
}

/* RCR, on the timers that have one (TIM1: 16 bits, TIM16/17: 8) */
static uint32_t tim_rcr(WB55TimState *s)
{
    return s->rcr_bits ? REG(s, R_RCR) & ((1u << s->rcr_bits) - 1) : 0;
}

/* counter ticks from one update event to the next: an update happens when
 * the repetition counter, reloaded from RCR at each one, runs out */
static uint64_t tim_uev_period(WB55TimState *s)
{
    return ((uint64_t)tim_arr(s) + 1) * ((uint64_t)s->rep + 1);
}

/* clocked from ETR: external clock mode 2, or mode 1 with TS = ETRF */
static bool tim_ext_clocked(WB55TimState *s)
{
    uint32_t smcr = REG(s, R_SMCR);
    int sms = (smcr & 7) | ((smcr >> 13) & 8);
    int ts = ((smcr >> 4) & 7) | ((smcr >> 17) & 0x18);

    return (smcr & SMCR_ECE) || (sms == 7 && ts == 7);
}

/* counter frequency; 0 while an external clock is selected but absent */
static uint64_t tim_hz(WB55TimState *s)
{
    uint64_t hz;

    if (tim_ext_clocked(s)) {
        hz = s->ext_hz >> ((REG(s, R_SMCR) >> 12) & 3);    /* ETPS */
    } else {
        hz = clock_get_hz(s->clk);
        hz = hz ? hz : 64000000;
    }
    return hz / ((REG(s, R_PSC) & 0xFFFF) + 1);
}

/* ticks counted between base_ns and when (no wrapping) */
static uint64_t tim_ticks(WB55TimState *s, int64_t when)
{
    if (!s->running || when <= s->base_ns || !tim_arr(s)) {
        return 0;                       /* stopped, or blocked by ARR = 0 */
    }
    return muldiv64(when - s->base_ns, tim_hz(s), NANOSECONDS_PER_SECOND);
}

/* counting at all: running, ARR != 0 and a clock */
static bool tim_counting(WB55TimState *s)
{
    return s->running && tim_arr(s) && tim_hz(s);
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

/*
 * TIM2 has one vector; TIM1 one per source: update (TIM1_UP_TIM16),
 * capture/compare (TIM1_CC), trigger and commutation (TIM1_TRG_COM_TIM17),
 * break (TIM1_BRK).
 */
static void tim_update_irq(WB55TimState *s)
{
    uint32_t act = REG(s, R_SR) & REG(s, R_DIER) & SR_IRQS;

    qemu_set_irq(s->irq, act != 0);
    qemu_set_irq(s->irq_up, act & SR_UIF);
    qemu_set_irq(s->irq_cc, act & 0x1E);
    qemu_set_irq(s->irq_trg, act & 0x60);
    qemu_set_irq(s->irq_brk, act & 0x80);
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
    uint64_t period = (uint64_t)tim_arr(s) + 1, uev = tim_uev_period(s);
    uint64_t t0, t1;

    if (!s->running || now <= s->checked_ns) {
        s->checked_ns = MAX(s->checked_ns, now);
        return;
    }
    t0 = s->cnt_base + tim_ticks(s, s->checked_ns);
    t1 = s->cnt_base + tim_ticks(s, now);
    if (t1 / uev > t0 / uev) {              /* an update event at least */
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
    if (!tim_counting(s)) {
        return;                         /* stopped, ARR = 0, or no clock */
    }
    if ((dier & SR_UIF) || tim_event_driven(s)) {
        uint64_t uev = tim_uev_period(s);

        next = (t / uev + 1) * uev;
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
    if (s->dma_wait) {
        timer_mod(s->timer, now + TIM_DMA_POLL_NS);
        return;
    }
    if (next != UINT64_MAX) {
        int64_t when = tim_time_of_tick(s, next - s->cnt_base);

        if (s->batch_dma && tim_event_driven(s) && !(dier & SR_IRQS)) {
            when = MAX(when, now + TIM_BATCH_NS);   /* DMA only: batch */
        }
        timer_mod(s->timer, when);
    }
}

/* a channel's output pin level changed at `when` */
static void tim_oc_set(WB55TimState *s, int ch, bool level, int64_t when)
{
    if (s->oc_level[ch] != level) {
        s->oc_level[ch] = level;
        if (s->oc_changed) {
            s->oc_changed(s->oc_opaque, ch, level, when);
        }
    }
}

/*
 * A PWM period starts at t (CNT = 0, with this period's ARR and CCRx, as
 * the update DMA has just written them): report both edges of each enabled
 * PWM channel.  Mode 1 is active while CNT < CCRx, mode 2 inactive; CCxP
 * inverts the pin.
 */
static void tim_pwm_period(WB55TimState *s, int64_t t)
{
    uint64_t period = (uint64_t)tim_arr(s) + 1;

    for (int ch = 0; ch < 4; ch++) {
        int m = oc_mode(s, ch);
        uint64_t first;
        bool pol, lead;

        if (!cc_is_output(s, ch) || (m != 6 && m != 7) ||
            !(REG(s, R_CCER) & (1u << (4 * ch)))) {
            continue;
        }
        first = MIN((uint64_t)REG(s, R_CCR1 + 4 * ch), period);
        pol = REG(s, R_CCER) & (1u << (4 * ch + 1));
        lead = (first > 0) == (m == 6);     /* level while CNT < CCRx */
        tim_oc_set(s, ch, lead != pol, t);
        if (first > 0 && first < period && tim_hz(s)) {
            tim_oc_set(s, ch, !lead != pol,
                       t + muldiv64_round_up(first, NANOSECONDS_PER_SECOND,
                                             tim_hz(s)));
        }
    }
}

/*
 * One update event at its exact time t: the counter wraps to 0, toggling
 * channels flip their pins, and an update DMA request lets the DMA write
 * (typically ARR, the next period) as if at t.
 */
/*
 * The update DMA request, served as if at time t.  The DMA runs
 * synchronously; its write to us acknowledges the request (tim_write), and
 * each channel on the request does one beat.
 */
static void tim_update_dma(WB55TimState *s, int64_t t)
{
    int64_t outer = s->now_override;

    s->now_override = t;
    s->dma_up_pending = true;
    qemu_set_irq(s->dma_up, 1);
    if (s->dma_up_pending) {            /* channel off: nobody took it */
        s->dma_up_pending = false;
        qemu_set_irq(s->dma_up, 0);
    }
    s->now_override = outer;
}

/* what every update event does, after the counter reset: the repetition
 * counter reloads, the DMA runs, PWM periods start, the board looks */
static void tim_uev_actions(WB55TimState *s, int64_t t)
{
    s->rep = tim_rcr(s);
    if (REG(s, R_DIER) & DIER_UDE) {
        tim_update_dma(s, t);
    }
    tim_pwm_period(s, t);
    if (s->update_hook) {
        s->update_hook(s->update_opaque, t);
    }
}

static void tim_update_event(WB55TimState *s, int64_t t)
{
    uint32_t toggles = tim_update_toggles(s);

    tim_rebase(s, 0, t);
    if (!(REG(s, R_CR1) & CR1_UDIS)) {
        REG(s, R_SR) |= SR_UIF;
    }
    for (int ch = 0; ch < 4; ch++) {
        if (toggles & (1u << ch)) {
            tim_oc_set(s, ch, !s->oc_level[ch], t);
        }
    }
    tim_uev_actions(s, t);
    if (REG(s, R_CR1) & CR1_OPM) {
        REG(s, R_CR1) &= ~CR1_CEN;
        s->running = false;
    }
}

/* updates the firmware feeds by DMA: replay each due one at its own time */
static void tim_run_updates(WB55TimState *s, int64_t now)
{
    if (s->in_updates) {
        return;
    }
    s->in_updates = true;
    s->dma_wait = false;
    for (int guard = 0; guard < 65536 && tim_event_driven(s) &&
                        tim_counting(s); guard++) {
        uint64_t uev = tim_uev_period(s);
        uint64_t t = s->cnt_base + tim_ticks(s, now);
        uint64_t next = (s->cnt_base / uev + 1) * uev;
        int64_t when;

        if (t < next) {
            break;                      /* the next update is still ahead */
        }
        when = tim_time_of_tick(s, next - s->cnt_base);
        /*
         * The firmware re-arms normal-mode DMA from an ISR between two
         * updates (furi_hal_infrared: every buffer of marks and spaces).  On
         * silicon that ISR runs within microseconds; here the vCPU can be
         * later than the next update.  So the update waits for it, keeping
         * its exact time; if the DMA stays dry, it was left that way.
         */
        if ((REG(s, R_DIER) & DIER_UDE) && s->dma_ready &&
            !s->dma_ready(s->dma_ready_opaque) && now - when < TIM_DMA_WAIT_NS) {
            s->dma_wait = true;
            break;
        }
        tim_update_event(s, when);
    }
    s->in_updates = false;
}

static void tim_tick(void *opaque)
{
    WB55TimState *s = opaque;
    int64_t now = qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL);

    tim_run_updates(s, now);
    tim_catch_up(s, now);
    tim_update_irq(s);
    tim_schedule(s, now);
}

void wb55_tim_set_ext_clock(WB55TimState *s, uint64_t hz)
{
    int64_t now = qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL);
    uint32_t cnt;

    if (hz == s->ext_hz) {
        return;
    }
    tim_run_updates(s, now);
    tim_catch_up(s, now);
    cnt = s->running ? tim_cnt_at(s, now) : s->cnt_base;
    s->ext_hz = hz;
    tim_rebase(s, cnt, now);
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
        s->cap_unread |= 1u << ch;
        s->cap_ns[ch] = when;
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

/*
 * A capture the firmware has not consumed yet: CCRx not read since it was
 * captured (only for channels with their interrupt enabled).  The flag is
 * not enough: the ISR clears CCxIF first and reads CCRx after, and an edge
 * in between would overwrite the value it is about to read.
 */
bool wb55_tim_capture_pending(WB55TimState *s)
{
    return s->cap_unread & (REG(s, R_DIER) >> 1) & 0xF;
}

/*
 * When a write to CNT takes effect.  furi_hal_rfid's capture ISR measures
 * pulses by restarting the counter in software: it clears CC4IF, writes
 * CNT = 0, then reads CCR4.  On silicon that runs a microsecond or two after
 * the edge.  Here the vCPU can reach it much later (and injected edges are
 * paced, so the next one may be timestamped before the write), which would
 * shorten or garble every measured duration.  So a CNT write made while a
 * capture is being serviced (its flag already cleared, CCRx not read yet)
 * restarts the counter at that capture's time, as a prompt ISR would.
 */
static int64_t tim_cnt_write_time(WB55TimState *s, int64_t now)
{
    for (int ch = 0; ch < 4; ch++) {
        if ((s->cap_unread & (1u << ch)) && !(REG(s, R_SR) & (1u << (ch + 1))) &&
            s->cap_ns[ch] <= now) {
            return s->cap_ns[ch];
        }
    }
    return now;
}

static uint64_t tim_read(void *opaque, hwaddr addr, unsigned size)
{
    WB55TimState *s = opaque;
    int64_t now = tim_now(s);
    uint32_t val;

    if (addr >= sizeof(s->regs)) {
        return 0;
    }
    if (!s->now_override) {
        tim_run_updates(s, now);        /* batched updates that are due */
    }
    switch (addr) {
    case R_SR:
        tim_catch_up(s, now);
        tim_update_irq(s);
        break;
    case R_CNT:
        return s->running ? tim_cnt_at(s, now) : s->cnt_base;
    case R_CCR1: case R_CCR1 + 4: case R_CCR1 + 8: case R_CCR1 + 12:
        val = s->regs[addr / 4];
        if (wb55_tim_capture_pending(s)) {
            s->cap_unread &= ~(1u << ((addr - R_CCR1) / 4));
            if (!wb55_tim_capture_pending(s)) {
                /* a paced edge source may now deliver its next edge (which
                 * can capture again: the value read is already taken) */
                notifier_list_notify(&s->cap_read, s);
            }
        } else {
            s->cap_unread &= ~(1u << ((addr - R_CCR1) / 4));
        }
        return val;
    }
    return s->regs[addr / 4];
}

static void tim_write(void *opaque, hwaddr addr, uint64_t val64, unsigned size)
{
    WB55TimState *s = opaque;
    int64_t now = tim_now(s);
    uint32_t val = val64;
    uint32_t cnt;

    if (addr >= sizeof(s->regs)) {
        return;
    }
    if (size < 4) {                     /* byte/halfword: read-modify-write */
        val = deposit32(s->regs[addr / 4], (addr & 3) * 8, size * 8, val);
        addr &= ~3;
    }
    if (s->dma_up_pending) {            /* the DMA's beat: request served */
        s->dma_up_pending = false;
        qemu_set_irq(s->dma_up, 0);
    }
    if (!s->now_override) {
        tim_run_updates(s, now);        /* batched updates that are due */
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
            if (!(REG(s, R_CR1) & CR1_UDIS)) {
                /* an update event: UIF and the DMA request unless URS
                 * restricts them to overflows (furi_hal_infrared primes
                 * RCR and CCMR2 by DMA with two UGs) */
                if (!(REG(s, R_CR1) & CR1_URS)) {
                    REG(s, R_SR) |= SR_UIF;
                    tim_uev_actions(s, now);
                } else {
                    s->rep = tim_rcr(s);
                }
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
        tim_rebase(s, s->width == 32 ? val : (val & 0xFFFF),
                   tim_cnt_write_time(s, now));
        break;
    case R_PSC:
    case R_ARR:
        REG(s, addr) = val;             /* preload ignored: effective now */
        tim_rebase(s, tim_arr(s) ? MIN(cnt, tim_arr(s)) : 0, now);
        break;
    case R_SMCR:                        /* may switch the counter's clock */
        REG(s, R_SMCR) = val;
        tim_rebase(s, cnt, now);
        break;
    default:
        s->regs[addr / 4] = val;
        break;
    }
    tim_update_irq(s);
    if (!s->now_override) {             /* inside an update: tim_tick rearms */
        tim_schedule(s, now);
    }
    if (s->changed) {
        s->changed(s->changed_opaque);
    }
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
    s->now_override = 0;
    s->dma_up_pending = false;
    s->in_updates = false;
    s->rep = 0;
    s->cap_unread = 0;
    memset(s->cap_ns, 0, sizeof(s->cap_ns));
    memset(s->oc_level, 0, sizeof(s->oc_level));
    tim_rebase(s, 0, qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL));
    if (s->timer) {
        timer_del(s->timer);
    }
}

static void tim_reset_exit(Object *obj, ResetType type)
{
    WB55TimState *s = WB55_TIM(obj);

    tim_update_irq(s);
    if (s->changed) {
        s->changed(s->changed_opaque);
    }
}

static void tim_init(Object *obj)
{
    WB55TimState *s = WB55_TIM(obj);

    notifier_list_init(&s->cap_read);
    memory_region_init_io(&s->mmio, obj, &tim_ops, s, "stm32wb55-tim", 0x400);
    /* a register access can replay batched updates, whose DMA writes ARR and
     * CCRx back into this timer: tim_write() handles that (now_override) */
    s->mmio.disable_reentrancy_guard = true;
    sysbus_init_mmio(SYS_BUS_DEVICE(obj), &s->mmio);
    sysbus_init_irq(SYS_BUS_DEVICE(obj), &s->irq);      /* 0: any (TIM2) */
    sysbus_init_irq(SYS_BUS_DEVICE(obj), &s->irq_up);   /* 1-4: TIM1's */
    sysbus_init_irq(SYS_BUS_DEVICE(obj), &s->irq_cc);
    sysbus_init_irq(SYS_BUS_DEVICE(obj), &s->irq_trg);
    sysbus_init_irq(SYS_BUS_DEVICE(obj), &s->irq_brk);
    s->clk = qdev_init_clock_in(DEVICE(obj), "clk", NULL, NULL, 0);
    qdev_init_gpio_out_named(DEVICE(obj), &s->dma_up, "dma-up", 1);
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
    DEFINE_PROP_UINT32("rcr-bits", WB55TimState, rcr_bits, 0),
    DEFINE_PROP_BOOL("batch-dma", WB55TimState, batch_dma, false),
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
