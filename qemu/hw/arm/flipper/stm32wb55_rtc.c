/*
 * STM32WB55 RTC (RM0434 chapter 27, "RTC2" register layout with ISR @0x0C)
 *
 * The whole RTC sits in the backup domain: a system reset leaves the
 * calendar running and BKP0R..BKP19R intact. The firmware keeps its boot mode,
 * settings flags and crash record there (furi_hal_rtc.c), and the OTA reboot
 * chain depends on them surviving resets. Only RCC_BDCR.BDRST clears it.
 *
 * On first power-on the calendar is seeded from the host clock (use
 * "-rtc base=localtime" for wall time) and the backup registers get Furi's
 * header (magic 0x10F1) plus the configured System register, so a fresh
 * virtual device can start with Settings > System > Sleep Method = Legacy
 * (a stackless CPU2 cannot do STOP2, see the CPU2 model).
 *
 * Copyright (c) 2026, Kara Zajac
 * SPDX-License-Identifier: BSD-3-Clause
 */
#include "qemu/osdep.h"
#include "qemu/log.h"
#include "qemu/cutils.h"
#include "hw/core/irq.h"
#include "hw/core/qdev-properties.h"
#include "system/rtc.h"
#include "migration/vmstate.h"
#include "system/runstate.h"
#include "wb55.h"

#define ISR_ALRAWF (1u << 0)
#define ISR_ALRBWF (1u << 1)
#define ISR_WUTWF  (1u << 2)
#define ISR_INITS  (1u << 4)
#define ISR_RSF    (1u << 5)
#define ISR_INITF  (1u << 6)
#define ISR_INIT   (1u << 7)
#define ISR_ALRAF  (1u << 8)
#define ISR_ALRBF  (1u << 9)
#define ISR_WUTF   (1u << 10)
#define ISR_W0C    0x0003FF20u      /* rc_w0 flags incl. RSF */

#define CR_ALRAE   (1u << 8)
#define CR_ALRBE   (1u << 9)
#define CR_WUTE    (1u << 10)
#define CR_ALRAIE  (1u << 12)
#define CR_ALRBIE  (1u << 13)
#define CR_WUTIE   (1u << 14)

static inline uint32_t bcd(unsigned v)
{
    return ((v / 10) << 4) | (v % 10);
}

static inline unsigned unbcd(uint32_t v)
{
    return ((v >> 4) & 0xF) * 10 + (v & 0xF);
}

static bool rtc_in_init(WB55RtcState *s)
{
    return s->isr_flags & ISR_INIT;
}

static int64_t rtc_now_s(WB55RtcState *s, int64_t *frac_ns)
{
    if (rtc_in_init(s)) {
        if (frac_ns) {
            *frac_ns = 0;
        }
        return s->base_s;
    }
    int64_t el = qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL) - s->base_ns;
    if (frac_ns) {
        *frac_ns = el % NANOSECONDS_PER_SECOND;
    }
    return s->base_s + el / NANOSECONDS_PER_SECOND;
}

static void rtc_set_calendar(WB55RtcState *s, int64_t secs)
{
    s->base_s = secs;
    s->base_ns = qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL);
}

/*
 * The calendar and the host's clock.  The calendar counts the machine's own
 * time; from one run to the next (and into a saved state) goes how far it
 * is from the host's clock, in whole seconds.  The part of a second is the
 * host's whenever the calendar is set from it: so a distance that is read
 * and set again stays what it was, and a Flipper does not lose half a second
 * with every start.
 */
static int64_t rtc_host_ns(void)
{
    struct tm tm;
    int64_t frac, again;

    do {                        /* (the two in one and the same second) */
        frac = qemu_clock_get_ns(QEMU_CLOCK_HOST) % NANOSECONDS_PER_SECOND;
        qemu_get_timedate(&tm, 0);
        again = qemu_clock_get_ns(QEMU_CLOCK_HOST) % NANOSECONDS_PER_SECOND;
    } while (again < frac);
    return mktimegm(&tm) * NANOSECONDS_PER_SECOND + frac;
}

static int64_t rtc_host_offset(WB55RtcState *s)
{
    int64_t frac, secs = rtc_now_s(s, &frac);
    int64_t d = secs * NANOSECONDS_PER_SECOND + frac - rtc_host_ns();

    /* the nearest second */
    d += d < 0 ? -(NANOSECONDS_PER_SECOND / 2) : NANOSECONDS_PER_SECOND / 2;
    return d / NANOSECONDS_PER_SECOND;
}

static void rtc_set_from_host(WB55RtcState *s, int64_t offset)
{
    int64_t host = rtc_host_ns();

    rtc_set_calendar(s, host / NANOSECONDS_PER_SECOND + offset);
    s->base_ns -= host % NANOSECONDS_PER_SECOND;
}

static void rtc_tm(WB55RtcState *s, struct tm *tm)
{
    time_t t = rtc_now_s(s, NULL);
    gmtime_r(&t, tm);
}

static uint32_t rtc_tr(WB55RtcState *s)
{
    struct tm tm;
    rtc_tm(s, &tm);
    return (bcd(tm.tm_hour) << 16) | (bcd(tm.tm_min) << 8) | bcd(tm.tm_sec);
}

static uint32_t rtc_dr(WB55RtcState *s)
{
    struct tm tm;
    rtc_tm(s, &tm);
    unsigned wd = tm.tm_wday ? tm.tm_wday : 7;
    return (bcd((tm.tm_year + 1900) % 100) << 16) | (wd << 13) |
           (bcd(tm.tm_mon + 1) << 8) | bcd(tm.tm_mday);
}

static void rtc_write_tr(WB55RtcState *s, uint32_t v)
{
    struct tm tm;
    rtc_tm(s, &tm);
    tm.tm_hour = unbcd((v >> 16) & 0x3F);
    if ((v & (1u << 22)) && (s->cr & (1u << 6)) && tm.tm_hour < 12) {
        tm.tm_hour += 12;               /* PM in 12h format */
    }
    tm.tm_min = unbcd((v >> 8) & 0x7F);
    tm.tm_sec = unbcd(v & 0x7F);
    rtc_set_calendar(s, mktimegm(&tm));
}

static void rtc_write_dr(WB55RtcState *s, uint32_t v)
{
    struct tm tm;
    rtc_tm(s, &tm);
    tm.tm_year = 100 + unbcd((v >> 16) & 0xFF);
    tm.tm_mon = unbcd((v >> 8) & 0x1F) - 1;
    tm.tm_mday = unbcd(v & 0x3F);
    rtc_set_calendar(s, mktimegm(&tm));
}

static void rtc_update_irq(WB55RtcState *s)
{
    uint32_t f = s->isr_flags;
    qemu_set_irq(s->alarm_irq,
                 ((f & ISR_ALRAF) && (s->cr & CR_ALRAIE)) ||
                 ((f & ISR_ALRBF) && (s->cr & CR_ALRBIE)));
    qemu_set_irq(s->wakeup_irq, (f & ISR_WUTF) && (s->cr & CR_WUTIE));
}

static bool rtc_alarm_match(WB55RtcState *s, uint32_t a)
{
    struct tm tm;
    rtc_tm(s, &tm);
    if (!(a & (1u << 7)) && unbcd(a & 0x7F) != tm.tm_sec) {
        return false;
    }
    if (!(a & (1u << 15)) && unbcd((a >> 8) & 0x7F) != tm.tm_min) {
        return false;
    }
    if (!(a & (1u << 23)) && unbcd((a >> 16) & 0x3F) != tm.tm_hour) {
        return false;
    }
    if (!(a & (1u << 31))) {
        unsigned v = unbcd((a >> 24) & 0x3F);
        if (a & (1u << 30)) {
            unsigned wd = tm.tm_wday ? tm.tm_wday : 7;
            if (v != wd) {
                return false;
            }
        } else if (v != tm.tm_mday) {
            return false;
        }
    }
    return true;
}

static int64_t wut_period_ns(WB55RtcState *s)
{
    uint32_t sel = s->cr & 7, w = s->wutr & 0xFFFF;
    if (sel < 4) {
        return (int64_t)(w + 1) * (16 >> sel) * NANOSECONDS_PER_SECOND / 32768;
    }
    return (int64_t)(w + 1 + (sel >= 6 ? 65536 : 0)) * NANOSECONDS_PER_SECOND;
}

static void rtc_schedule(WB55RtcState *s)
{
    int64_t now = qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL);
    int64_t next = INT64_MAX;

    if ((s->cr & (CR_ALRAE | CR_ALRBE)) && !rtc_in_init(s)) {
        /* next whole calendar second */
        int64_t frac;
        rtc_now_s(s, &frac);
        next = now + (NANOSECONDS_PER_SECOND - frac);
    }
    if ((s->cr & CR_WUTE) && s->wut_next_ns) {
        next = MIN(next, s->wut_next_ns);
    }
    if (next == INT64_MAX) {
        timer_del(s->tick);
    } else {
        timer_mod(s->tick, next);
    }
}

static void rtc_tick(void *opaque)
{
    WB55RtcState *s = opaque;
    int64_t now = qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL);
    int64_t secs = rtc_now_s(s, NULL);

    if (secs != s->last_alarm_s) {
        s->last_alarm_s = secs;
        if ((s->cr & CR_ALRAE) && rtc_alarm_match(s, s->alrmar)) {
            s->isr_flags |= ISR_ALRAF;
        }
        if ((s->cr & CR_ALRBE) && rtc_alarm_match(s, s->alrmbr)) {
            s->isr_flags |= ISR_ALRBF;
        }
    }
    if ((s->cr & CR_WUTE) && s->wut_next_ns && now >= s->wut_next_ns) {
        s->isr_flags |= ISR_WUTF;
        s->wut_next_ns = now + wut_period_ns(s);
    }
    rtc_update_irq(s);
    rtc_schedule(s);
}

static uint64_t wb55_rtc_read(void *opaque, hwaddr addr, unsigned size)
{
    WB55RtcState *s = opaque;
    int64_t frac;

    if (addr >= 0x50 && addr < 0xA0) {
        return s->bkp[(addr - 0x50) / 4];
    }
    switch (addr) {
    case 0x00: return rtc_tr(s);
    case 0x04: return rtc_dr(s);
    case 0x08: return s->cr;
    case 0x0C: {
        uint32_t v = (s->isr_flags & ~(ISR_INITF | ISR_ALRAWF | ISR_ALRBWF |
                                       ISR_WUTWF)) | ISR_RSF | ISR_INITS;
        if (v & ISR_INIT) {
            v |= ISR_INITF;
        }
        if (!(s->cr & CR_ALRAE)) {
            v |= ISR_ALRAWF;
        }
        if (!(s->cr & CR_ALRBE)) {
            v |= ISR_ALRBWF;
        }
        if (!(s->cr & CR_WUTE)) {
            v |= ISR_WUTWF;
        }
        return v;
    }
    case 0x10: return s->prer;
    case 0x14: return s->wutr;
    case 0x1C: return s->alrmar;
    case 0x20: return s->alrmbr;
    case 0x28: {
        uint32_t ps = s->prer & 0x7FFF;
        rtc_now_s(s, &frac);
        return ps - (uint32_t)muldiv64(frac, ps + 1, NANOSECONDS_PER_SECOND);
    }
    case 0x2C: return s->shiftr;
    case 0x3C: return s->calr;
    case 0x40: return s->tampcr;
    case 0x44: return s->alrmassr;
    case 0x48: return s->alrmbssr;
    case 0x4C: return s->or_;
    }
    return 0;
}

static void wb55_rtc_write(void *opaque, hwaddr addr, uint64_t val64,
                           unsigned size)
{
    WB55RtcState *s = opaque;
    uint32_t val = val64;

    if (addr >= 0x50 && addr < 0xA0) {
        s->bkp[(addr - 0x50) / 4] = val;
        return;
    }
    switch (addr) {
    case 0x00:
        if (rtc_in_init(s)) {
            rtc_write_tr(s, val);
        }
        break;
    case 0x04:
        if (rtc_in_init(s)) {
            rtc_write_dr(s, val);
        }
        break;
    case 0x08: {
        uint32_t old = s->cr;
        s->cr = val & 0x01FFFFFF;
        if ((val & CR_WUTE) && !(old & CR_WUTE)) {
            s->wut_next_ns = qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL) +
                             wut_period_ns(s);
        } else if (!(val & CR_WUTE)) {
            s->wut_next_ns = 0;
        }
        rtc_update_irq(s);
        rtc_schedule(s);
        break;
    }
    case 0x0C: {
        bool was_init = rtc_in_init(s);
        if (!was_init && (val & ISR_INIT)) {
            s->base_s = rtc_now_s(s, NULL);         /* freeze calendar */
        }
        s->isr_flags = (s->isr_flags & ~ISR_W0C) |
                       (s->isr_flags & val & ISR_W0C);
        s->isr_flags = (s->isr_flags & ~ISR_INIT) | (val & ISR_INIT);
        if (was_init && !(val & ISR_INIT)) {
            s->base_ns = qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL);
        }
        rtc_update_irq(s);
        rtc_schedule(s);
        break;
    }
    case 0x10:
        if (rtc_in_init(s)) {
            s->prer = val & 0x007F7FFF;
        }
        break;
    case 0x14: s->wutr = val & 0xFFFF; break;
    case 0x1C: s->alrmar = val; break;
    case 0x20: s->alrmbr = val; break;
    case 0x24: s->wpr = val & 0xFF; break;
    case 0x2C: s->shiftr = val; break;
    case 0x3C: s->calr = val; break;
    case 0x40: s->tampcr = val; break;
    case 0x44: s->alrmassr = val; break;
    case 0x48: s->alrmbssr = val; break;
    case 0x4C: s->or_ = val & 3; break;
    default:
        qemu_log_mask(LOG_GUEST_ERROR, "wb55-rtc: write 0x%" HWADDR_PRIx "\n",
                      addr);
    }
}

static const MemoryRegionOps wb55_rtc_ops = {
    .read = wb55_rtc_read,
    .write = wb55_rtc_write,
    .endianness = DEVICE_LITTLE_ENDIAN,
    .impl.min_access_size = 4,
    .impl.max_access_size = 4,
    .valid.min_access_size = 1,
    .valid.max_access_size = 4,
};

static void rtc_clear_domain(WB55RtcState *s)
{
    s->cr = 0;
    s->isr_flags = 0;
    s->prer = 0x007F00FF;
    s->wutr = 0xFFFF;
    s->alrmar = s->alrmbr = s->alrmassr = s->alrmbssr = 0;
    s->calr = s->tampcr = s->or_ = s->shiftr = 0;
    s->wpr = 0;
    s->wut_next_ns = 0;
    s->last_alarm_s = -1;
}

void wb55_rtc_backup_domain_reset(WB55RtcState *s)
{
    rtc_clear_domain(s);
    memset(s->bkp, 0, sizeof(s->bkp));
    rtc_set_calendar(s, 946684800);     /* 2000-01-01 00:00:00 */
    timer_del(s->tick);
    rtc_update_irq(s);
}

void wb55_rtc_export(WB55RtcState *s, WB55RtcBackup *b)
{
    b->calendar_offset = rtc_host_offset(s);
    b->cr = s->cr;
    b->prer = s->prer;
    b->wutr = s->wutr;
    b->alrmar = s->alrmar;
    b->alrmbr = s->alrmbr;
    b->alrmassr = s->alrmassr;
    b->alrmbssr = s->alrmbssr;
    b->calr = s->calr;
    b->tampcr = s->tampcr;
    b->or_ = s->or_;
    memcpy(b->bkp, s->bkp, sizeof(b->bkp));
}

/* Restore a saved backup domain before the first reset (the "coin cell"). */
void wb55_rtc_import(WB55RtcState *s, const WB55RtcBackup *b)
{
    rtc_clear_domain(s);
    rtc_set_from_host(s, b->calendar_offset);
    s->cr = b->cr & ~CR_WUTE;
    s->prer = b->prer;
    s->wutr = b->wutr;
    s->alrmar = b->alrmar;
    s->alrmbr = b->alrmbr;
    s->alrmassr = b->alrmassr;
    s->alrmbssr = b->alrmbssr;
    s->calr = b->calr;
    s->tampcr = b->tampcr;
    s->or_ = b->or_;
    memcpy(s->bkp, b->bkp, sizeof(s->bkp));
    s->backup_valid = true;
}

static void wb55_rtc_reset_hold(Object *obj, ResetType type)
{
    WB55RtcState *s = WB55_RTC(obj);

    if (s->backup_valid) {
        return;                         /* system reset: backup domain kept */
    }
    rtc_clear_domain(s);
    rtc_set_from_host(s, 0);
    memcpy(s->bkp, s->init_bkp, sizeof(s->bkp));
    s->backup_valid = true;
}

static void wb55_rtc_reset_exit(Object *obj, ResetType type)
{
    WB55RtcState *s = WB55_RTC(obj);

    rtc_update_irq(s);
    rtc_schedule(s);
}

static void wb55_rtc_init(Object *obj)
{
    WB55RtcState *s = WB55_RTC(obj);

    memory_region_init_io(&s->mmio, obj, &wb55_rtc_ops, s, "rtc", 0x400);
    sysbus_init_mmio(SYS_BUS_DEVICE(obj), &s->mmio);
    sysbus_init_irq(SYS_BUS_DEVICE(obj), &s->alarm_irq);
    sysbus_init_irq(SYS_BUS_DEVICE(obj), &s->wakeup_irq);
    sysbus_init_irq(SYS_BUS_DEVICE(obj), &s->tamp_irq);
    s->tick = timer_new_ns(QEMU_CLOCK_VIRTUAL, rtc_tick, s);
}

static const Property wb55_rtc_props[] = {
    DEFINE_PROP_UINT32("bkp0", WB55RtcState, init_bkp[0], 0x000010F1),
    DEFINE_PROP_UINT32("bkp1", WB55RtcState, init_bkp[1], 0x00002000),
};

/*
 * A saved state.  The calendar follows the host's clock, as it does from one
 * run to the next (wb55_rtc_export()): a state says how far the Flipper's
 * clock was from the host's, and after a load it is that far from it again.
 * So the Flipper's clock is right after a load, if it was right before, and
 * it is not the moment of the save.  It is set when the machine next runs:
 * a loaded machine may wait for that.  The wakeup timer counts the machine's
 * own time and goes on where it was.
 */
static int wb55_rtc_pre_save(void *opaque)
{
    WB55RtcState *s = opaque;

    if (!s->loaded) {           /* (a loaded state that has not run says it) */
        s->saved_offset = rtc_host_offset(s);
    }
    return 0;
}

static void wb55_rtc_loaded(void *opaque, bool running, RunState state)
{
    WB55RtcState *s = opaque;

    if (!running) {
        return;
    }
    qemu_del_vm_change_state_handler(s->loaded);
    s->loaded = NULL;
    if (!rtc_in_init(s)) {              /* (there the firmware is setting it) */
        rtc_set_from_host(s, s->saved_offset);
        rtc_schedule(s);
    }
}

static int wb55_rtc_post_load(void *opaque, int version_id)
{
    WB55RtcState *s = opaque;

    if (!s->loaded) {
        s->loaded = qemu_add_vm_change_state_handler(wb55_rtc_loaded, s);
    }
    return 0;
}

static const VMStateDescription vmstate_wb55_rtc = {
    .name = "stm32wb55-rtc",
    .version_id = 1,
    .minimum_version_id = 1,
    .pre_save = wb55_rtc_pre_save,
    .post_load = wb55_rtc_post_load,
    .fields = (const VMStateField[]) {
        VMSTATE_TIMER_PTR(tick, WB55RtcState),
        VMSTATE_BOOL(backup_valid, WB55RtcState),
        VMSTATE_INT64(base_s, WB55RtcState),
        VMSTATE_INT64(base_ns, WB55RtcState),
        VMSTATE_UINT32(cr, WB55RtcState),
        VMSTATE_UINT32(isr_flags, WB55RtcState),
        VMSTATE_UINT32(prer, WB55RtcState),
        VMSTATE_UINT32(wutr, WB55RtcState),
        VMSTATE_UINT32(alrmar, WB55RtcState),
        VMSTATE_UINT32(alrmbr, WB55RtcState),
        VMSTATE_UINT32(alrmassr, WB55RtcState),
        VMSTATE_UINT32(alrmbssr, WB55RtcState),
        VMSTATE_UINT32(calr, WB55RtcState),
        VMSTATE_UINT32(tampcr, WB55RtcState),
        VMSTATE_UINT32(or_, WB55RtcState),
        VMSTATE_UINT32(shiftr, WB55RtcState),
        VMSTATE_UINT32(wpr, WB55RtcState),
        VMSTATE_UINT32_ARRAY(bkp, WB55RtcState, 20),
        VMSTATE_INT64(wut_next_ns, WB55RtcState),
        VMSTATE_INT64(last_alarm_s, WB55RtcState),
        VMSTATE_INT64(saved_offset, WB55RtcState),
        VMSTATE_END_OF_LIST()
    }
};

static void wb55_rtc_class_init(ObjectClass *klass, const void *data)
{
    ResettableClass *rc = RESETTABLE_CLASS(klass);

    device_class_set_props(DEVICE_CLASS(klass), wb55_rtc_props);
    rc->phases.hold = wb55_rtc_reset_hold;
    rc->phases.exit = wb55_rtc_reset_exit;
    DEVICE_CLASS(klass)->vmsd = &vmstate_wb55_rtc;
}

static const TypeInfo wb55_rtc_info = {
    .name = TYPE_WB55_RTC,
    .parent = TYPE_SYS_BUS_DEVICE,
    .instance_size = sizeof(WB55RtcState),
    .instance_init = wb55_rtc_init,
    .class_init = wb55_rtc_class_init,
};

static void wb55_rtc_register(void)
{
    type_register_static(&wb55_rtc_info);
}
type_init(wb55_rtc_register)
