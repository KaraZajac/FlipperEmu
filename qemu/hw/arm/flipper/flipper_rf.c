/*
 * Flipper Zero radio front-ends on SPI1: TI CC1101 (sub-GHz, CS=PD0,
 * GDO0=PA1).  The ST25R3916 (NFC, CS=PE4, IRQ=PA2) is in st25r3916.c.
 *
 * Register-level model: enough for the firmware's own bring-up
 * (furi_hal_subghz_init()'s GDO0 self-test) and for apps to configure it.
 *
 * Sub-GHz receive: a signal can be put "on the air" through the CC1101's
 * "rx" property (+high/-low µs, optionally at a frequency).  While the chip
 * is in RX, tuned within 500 kHz and set to asynchronous serial output on
 * GDO0 (IOCFG0 = 0x0D), GDO0 follows the carrier, and RSSI reads strong.
 * GDO0 is PA1 and TIM2's TI2, which is how furi_hal_subghz captures it.
 *
 * Sub-GHz transmit: in TX, GDO0 is the chip's data input, driven by TIM2 CH2
 * toggling at each DMA-fed update.  Each transmission is recorded and, when
 * the chip leaves TX, written to the "tx-dir" property's directory as a
 * Flipper .sub RAW file at the tuned frequency.  Its preset is a custom one
 * with the CC1101's live register values, so a real Flipper can replay it.
 *
 * Copyright (c) 2026, Kara Zajac
 * SPDX-License-Identifier: BSD-3-Clause
 */
#include "qemu/osdep.h"
#include <math.h>
#include "qemu/log.h"
#include "qemu/timer.h"
#include "qemu/cutils.h"
#include "qemu/error-report.h"
#include "qapi/error.h"
#include "hw/core/irq.h"
#include "hw/core/qdev-properties.h"
#include "hw/core/qdev-properties-system.h"
#include "migration/vmstate.h"
#include "wb55.h"
#include "flipper.h"

/* --------------------------------------------------------------- CC1101 */

static const uint8_t cc1101_defaults[0x2F] = {
    0x29, 0x2E, 0x3F, 0x07, 0xD3, 0x91, 0xFF, 0x04, 0x45, 0x00, 0x00, 0x0F,
    0x00, 0x1E, 0xC4, 0xEC, 0x8C, 0x22, 0x02, 0x22, 0xF8, 0x47, 0x07, 0x30,
    0x04, 0x36, 0x6C, 0x03, 0x40, 0x91, 0x87, 0x6B, 0xF8, 0x56, 0x10, 0xA9,
    0x0A, 0x20, 0x0D, 0x41, 0x00, 0x59, 0x7F, 0x3F, 0x88, 0x31, 0x0B,
};

#define MARC_SLEEP  0x00
#define MARC_IDLE   0x01
#define MARC_RX     0x0D
#define MARC_FSTXON 0x12
#define MARC_TX     0x13

#define AIR_MAX 65536

static uint32_t cc1101_freq_hz(Cc1101State *s)
{
    uint32_t f = s->regs[0x0D] << 16 | s->regs[0x0E] << 8 | s->regs[0x0F];

    return (uint64_t)f * 26000000 / 65536;
}

/* in RX and tuned to what is on the air */
static bool cc1101_hears(Cc1101State *s)
{
    return s->state == MARC_RX &&
           (!s->air_freq ||
            ABS((int64_t)cc1101_freq_hz(s) - (int64_t)s->air_freq) < 500000);
}

static int cc1101_gdo_level(Cc1101State *s, uint8_t cfg)
{
    int lvl;

    switch (cfg & 0x3F) {
    case 0x2F: lvl = 0; break;          /* HW to 0 */
    case 0x2E: lvl = 1; break;          /* high-Z: released (pull-up) */
    case 0x29: lvl = 0; break;          /* CHIP_RDYn */
    case 0x0D:                          /* async data: the air, or a bridge's */
        lvl = (cc1101_hears(s) && s->carrier) ||
              (s->state == MARC_RX && s->live_carrier);
        break;
    default:   lvl = 0; break;          /* no RF activity */
    }
    return (cfg & 0x40) ? !lvl : lvl;
}

static void cc1101_drive_gdo0(Cc1101State *s, bool level, int64_t when)
{
    if (level == s->gdo0_level) {
        return;
    }
    s->gdo0_level = level;
    qemu_set_irq(s->gdo0, level);
    if (s->tim) {
        wb55_tim_input(s->tim, 1, level, when);
    }
}

static void cc1101_update_gdo(Cc1101State *s)
{
    cc1101_drive_gdo0(s, cc1101_gdo_level(s, s->regs[0x02]),
                      qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL));
    qemu_set_irq(s->gdo2, cc1101_gdo_level(s, s->regs[0x00]));
}

/*
 * One edge of the air signal.  Like the IR receiver, an edge that would
 * reach the capture timer waits (up to 250 ms) until the firmware has read
 * the previous capture; the timestamp keeps the measured duration exact.
 */
static void cc1101_air_edge(void *opaque)
{
    Cc1101State *s = opaque;
    int64_t when = s->air_next_ns;
    int64_t now = qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL);
    bool level;

    if (s->tim && cc1101_hears(s) && wb55_tim_capture_pending(s->tim) &&
        now < when + 250 * SCALE_MS) {
        timer_mod(s->air_timer, now + 20 * SCALE_US);
        return;
    }
    if (s->air_pos >= s->air_len) {
        level = false;                  /* transmission over */
        g_free(s->air);
        s->air = NULL;
        s->air_len = s->air_pos = 0;
    } else {
        int32_t d = s->air[s->air_pos++];

        level = d > 0;
        s->air_next_ns = when + (int64_t)ABS(d) * 1000;
        timer_mod(s->air_timer, s->air_next_ns);
    }
    s->carrier = level;
    cc1101_drive_gdo0(s, cc1101_gdo_level(s, s->regs[0x02]), when);
}

static char *cc1101_get_rx(Object *obj, Error **errp)
{
    return g_strdup(CC1101(obj)->air ? "busy" : "");
}

/* "[@FREQ_HZ,]d,d,..." (+high/-low µs), or "file:PATH" holding the same */
static void cc1101_set_rx(Object *obj, const char *value, Error **errp)
{
    Cc1101State *s = CC1101(obj);
    g_autofree char *text = NULL;
    g_autofree int32_t *air = g_new0(int32_t, AIR_MAX);
    g_auto(GStrv) parts = NULL;
    uint32_t freq = 0;
    int n = 0;

    if (g_str_has_prefix(value, "file:")) {
        g_autoptr(GError) err = NULL;

        if (!g_file_get_contents(value + 5, &text, NULL, &err)) {
            error_setg(errp, "subghz rx: %s", err->message);
            return;
        }
        value = text;
    }
    parts = g_strsplit_set(value, " ,;\t\r\n", -1);
    for (int i = 0; parts[i]; i++) {
        const char *p = parts[i];
        int64_t v;

        if (!*p) {
            continue;
        }
        if (*p == '@') {
            uint64_t f;

            if (qemu_strtou64(p + 1, NULL, 10, &f) < 0 || f > UINT32_MAX) {
                error_setg(errp, "subghz rx: bad frequency '%s'", p);
                return;
            }
            freq = f;
            continue;
        }
        if (qemu_strtoi64(p, NULL, 10, &v) < 0 || !v ||
            v > 10000000 || v < -10000000) {
            error_setg(errp, "subghz rx: bad duration '%s'", p);
            return;
        }
        if (n && (air[n - 1] > 0) == (v > 0)) {
            air[n - 1] += v;            /* merge runs of one level */
        } else if (n < AIR_MAX) {
            air[n++] = v;
        } else {
            error_setg(errp, "subghz rx: more than %d durations", AIR_MAX);
            return;
        }
    }
    if (!n) {
        return;
    }
    timer_del(s->air_timer);
    g_free(s->air);
    s->air = g_steal_pointer(&air);
    s->air_len = n;
    s->air_pos = 0;
    s->air_freq = freq;
    s->air_next_ns = qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL) + 2 * SCALE_MS;
    timer_mod(s->air_timer, s->air_next_ns);
}

/*
 * ---- a radio bridge on the "link" chardev (docs/RADIO_LINK.md)
 *
 * A program on the other end, such as scripts/fe_sdr.py with a real SDR,
 * is the antenna.  The chip announces its receive set-up whenever it
 * changes ("cc1101 epoch=N state=rx freq=... mod=ook bw=... rate=... dev=..."),
 * and the bridge answers with what a CC1101 so set would demodulate: a
 * continuous timeline of "rx N +high -low ..." µs, and now and then the
 * band's power ("spectrum F0 STEP dBm..."), from which RSSI is read at any
 * frequency and bandwidth, as the frequency analyzer and the hopper sweep.
 * The epoch drops whatever was still on its way for an earlier set-up.
 * Every transmission also goes out ("tx freq=... raw=+d,-d,...").
 */

#define LIVE_MAX       (1 << 16)            /* durations queued */
#define LIVE_DELAY_NS  (60 * SCALE_MS)      /* jitter buffer before playing */
#define SPEC_MAX       8192
#define SPEC_FRESH_NS  (1500 * SCALE_MS)

static const char *cc1101_mod_name(Cc1101State *s)
{
    switch ((s->regs[0x12] >> 4) & 7) {     /* MDMCFG2.MOD_FORMAT */
    case 0: return "2fsk";
    case 1: return "gfsk";
    case 3: return "ook";
    case 4: return "4fsk";
    case 7: return "msk";
    default: return "unknown";
    }
}

/* MDMCFG4: the channel filter, 26 MHz / (8 (4 + CHANBW_M) 2^CHANBW_E) */
static uint32_t cc1101_bw_hz(Cc1101State *s)
{
    uint8_t m4 = s->regs[0x10];

    return 26000000u / ((8u * (4 + ((m4 >> 4) & 3))) << (m4 >> 6));
}

/* MDMCFG4/3: (256 + DRATE_M) 2^DRATE_E 26 MHz / 2^28 */
static uint32_t cc1101_rate_baud(Cc1101State *s)
{
    uint8_t m4 = s->regs[0x10], m3 = s->regs[0x11];

    return ((uint64_t)(256 + m3) << (m4 & 15)) * 26000000u >> 28;
}

/* DEVIATN: 26 MHz / 2^17 (8 + DEVIATION_M) 2^DEVIATION_E */
static uint32_t cc1101_dev_hz(Cc1101State *s)
{
    uint8_t d = s->regs[0x15];

    return ((uint64_t)(8 + (d & 7)) << ((d >> 4) & 7)) * 26000000u >> 17;
}

/* the bridge's air stops: nothing queued, the line low */
static void cc1101_live_stop(Cc1101State *s)
{
    timer_del(s->live_timer);
    s->live_rd = s->live_wr = 0;
    s->live_queued_us = 0;
    s->live_playing = false;
    if (s->live_carrier) {
        s->live_carrier = false;
        cc1101_update_gdo(s);
    }
}

static void cc1101_link_check(Cc1101State *s);

static void cc1101_link_announce(void *opaque)
{
    Cc1101State *s = opaque;

    if (!s->link_cfg) {
        cc1101_link_check(s);
    }
    flipper_link_send(&s->lk, "cc1101 epoch=%u %s\n", s->link_epoch, s->link_cfg);
}

/* after a register write or a state change: has the receive set-up changed? */
static void cc1101_link_check(Cc1101State *s)
{
    static const char *const names[] = {
        [MARC_SLEEP] = "sleep", [MARC_IDLE] = "idle", [MARC_RX] = "rx",
        [MARC_FSTXON] = "fstxon", [MARC_TX] = "tx",
    };
    /* AGCCTRL0.FILTER_LENGTH, for ASK/OOK: the decision boundary below the
     * peak, 4/8/12/16 dB (the Flipper's OOK presets: 8) */
    g_autofree char *cfg = g_strdup_printf(
        "state=%s freq=%u mod=%s bw=%u rate=%u dev=%u boundary=%u",
        s->state < ARRAY_SIZE(names) && names[s->state] ? names[s->state] : "idle",
        cc1101_freq_hz(s), cc1101_mod_name(s), cc1101_bw_hz(s),
        cc1101_rate_baud(s), cc1101_dev_hz(s), 4u * (1 + (s->regs[0x1B] & 3)));

    if (s->link_cfg && !strcmp(cfg, s->link_cfg)) {
        return;
    }
    g_free(s->link_cfg);
    s->link_cfg = g_steal_pointer(&cfg);
    s->link_epoch++;
    cc1101_live_stop(s);
    if (s->lk.open) {                 /* a preset is many writes: settle */
        timer_mod(s->link_timer, qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL) +
                                 SCALE_MS);
    }
}

/*
 * One step of the bridge's air.  It plays LIVE_DELAY_NS behind what came,
 * so the bridge's chunks join up; an edge waits for the firmware's capture
 * read as the one-shot air's do, keeping its timestamp.  A backlog (the
 * firmware fell behind, or the bridge sent a burst) is made up in silence.
 */
static void cc1101_live_edge(void *opaque)
{
    Cc1101State *s = opaque;
    int64_t when = s->live_next_ns;
    int64_t now = qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL);
    int64_t us;
    int32_t d;

    if (s->tim && s->state == MARC_RX && wb55_tim_capture_pending(s->tim) &&
        now < when + 250 * SCALE_MS) {
        timer_mod(s->live_timer, now + 20 * SCALE_US);
        return;
    }
    if (s->live_rd == s->live_wr) {     /* ran dry: the bridge is late */
        s->live_playing = false;
        if (s->live_carrier) {
            s->live_carrier = false;
            cc1101_drive_gdo0(s, cc1101_gdo_level(s, s->regs[0x02]), when);
        }
        return;
    }
    d = s->live[s->live_rd++ & (LIVE_MAX - 1)];
    us = ABS(d);
    s->live_queued_us -= us;
    if (d < 0 && us > 20000 && s->live_queued_us > 2 * LIVE_DELAY_NS / 1000) {
        us -= MIN(us - 10000, s->live_queued_us - LIVE_DELAY_NS / 1000);
    }
    s->live_carrier = d > 0;
    cc1101_drive_gdo0(s, cc1101_gdo_level(s, s->regs[0x02]), when);
    s->live_next_ns = when + us * 1000;
    timer_mod(s->live_timer, s->live_next_ns);
}

/* "rx EPOCH d d d ...": more of the timeline */
static void cc1101_link_rx(Cc1101State *s, const char *p)
{
    char *end;
    unsigned long epoch = strtoul(p, &end, 10);

    if (end == p || epoch != s->link_epoch || s->state != MARC_RX) {
        return;                         /* for an earlier set-up */
    }
    for (p = end; *p; p = end) {
        long v = strtol(p, &end, 10);

        if (end == p) {
            break;
        }
        if (!v || v > 10000000 || v < -10000000) {
            continue;
        }
        if (s->live_wr - s->live_rd >= LIVE_MAX) {
            break;                      /* full: the rest is lost */
        }
        s->live[s->live_wr++ & (LIVE_MAX - 1)] = v;
        s->live_queued_us += ABS(v);
    }
    if (!s->live_playing && s->live_rd != s->live_wr) {
        s->live_playing = true;
        s->live_next_ns = qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL) + LIVE_DELAY_NS;
        timer_mod(s->live_timer, s->live_next_ns);
    }
}

/* "spectrum F0 STEP dBm dBm ...": the band's power, bin by bin */
static void cc1101_link_spectrum(Cc1101State *s, const char *p)
{
    char *end;
    double f0 = strtod(p, &end), step;
    int n = 0;

    if (end == p) {
        return;
    }
    p = end;
    step = strtod(p, &end);
    if (end == p || step <= 0) {
        return;
    }
    if (!s->spec) {
        s->spec = g_new(float, SPEC_MAX);
    }
    for (p = end; n < SPEC_MAX; p = end) {
        double v = strtod(p, &end);

        if (end == p) {
            break;
        }
        s->spec[n++] = v;
    }
    s->spec_n = n;
    s->spec_f0 = f0;
    s->spec_step = step;
    s->spec_ns = qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL);
}

static void cc1101_link_line(void *opaque, char *line)
{
    Cc1101State *s = opaque;

    if (g_str_has_prefix(line, "rx ")) {
        cc1101_link_rx(s, line + 3);
    } else if (g_str_has_prefix(line, "spectrum ")) {
        cc1101_link_spectrum(s, line + 9);
    }
}

static void cc1101_link_opened(void *opaque, bool open)
{
    Cc1101State *s = opaque;

    if (open) {
        flipper_link_send(&s->lk, "hello flipperemu subghz 1\n");
        cc1101_link_announce(s);
    } else {
        cc1101_live_stop(s);
        s->spec_n = 0;
    }
}

/* RSSI in dBm: a CC1101 tuned as now, in the air the bridge reports */
static double cc1101_rssi_dbm(Cc1101State *s)
{
    int64_t now = qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL);

    if (cc1101_hears(s) && s->air) {
        return -40;                     /* the one-shot air: strong */
    }
    if (s->spec_n && now - s->spec_ns < SPEC_FRESH_NS) {
        double f = cc1101_freq_hz(s), bw = cc1101_bw_hz(s), p = 0;
        long lo = lround(ceil((f - bw / 2 - s->spec_f0) / s->spec_step));
        long hi = lround(floor((f + bw / 2 - s->spec_f0) / s->spec_step));

        if (hi < lo) {                  /* narrower than a bin: the nearest */
            lo = hi = lround((f - s->spec_f0) / s->spec_step);
        }
        if (hi < 0 || lo >= s->spec_n) {
            return -167 + 10 * log10(bw);   /* off the bridge's band: noise */
        }
        lo = MAX(lo, 0);
        hi = MIN(hi, s->spec_n - 1);
        for (long k = lo; k <= hi; k++) {
            p += pow(10, s->spec[k] / 10);
        }
        if ((hi - lo + 1) * s->spec_step < bw) {    /* partly off the band: */
            p += (bw - (hi - lo + 1) * s->spec_step) * pow(10, -16.7); /* noise */
        }
        return 10 * log10(p);
    }
    if (s->state == MARC_RX && s->live_carrier) {
        return -60;                     /* a bridge with no spectrum */
    }
    return -100;
}

/* ---- transmit capture */

void cc1101_tx_data(Cc1101State *s, bool level, int64_t when)
{
    if (s->tx_active && level != s->tx_level) {
        int64_t us = (when - s->tx_edge_ns) / 1000;

        if (us > 0) {
            int32_t d = s->tx_level ? us : -us;

            g_array_append_val(s->tx, d);
        }
        s->tx_edge_ns = when;
    }
    s->tx_level = level;
}

static void cc1101_tx_begin(Cc1101State *s)
{
    if (s->tx_active) {
        return;
    }
    s->tx_active = true;
    s->tx = g_array_new(false, false, sizeof(int32_t));
    s->tx_edge_ns = qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL);
}

/* the registers Flipper presets set, as .sub Custom_preset_data */
static void cc1101_preset_data(Cc1101State *s, GString *out)
{
    static const uint8_t regs[] = {
        0x02, 0x03, 0x08, 0x0B, 0x10, 0x11, 0x12, 0x13, 0x14, 0x15,
        0x18, 0x19, 0x1A, 0x1B, 0x1C, 0x1D, 0x20, 0x21, 0x22,
    };

    for (size_t i = 0; i < ARRAY_SIZE(regs); i++) {
        g_string_append_printf(out, "%02X %02X ", regs[i], s->regs[regs[i]]);
    }
    g_string_append(out, "00 00");
    for (int i = 0; i < 8; i++) {
        g_string_append_printf(out, " %02X", s->patable[i]);
    }
}

static void cc1101_tx_end(Cc1101State *s)
{
    int32_t *d;
    guint first = 0, n;
    uint32_t freq = cc1101_freq_hz(s);
    g_autoptr(GString) sub = g_string_new(NULL);
    g_autoptr(GString) last = g_string_new(NULL);

    if (!s->tx_active) {
        return;
    }
    cc1101_tx_data(s, !s->tx_level, qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL));
    s->tx_active = false;
    d = (int32_t *)s->tx->data;
    n = s->tx->len;
    while (first < n && d[first] < 0) {
        first++;                        /* the idle time before the first edge */
    }
    while (n > first && d[n - 1] < 0) {
        n--;                            /* and after the last */
    }
    if (n > first) {
        freq = (freq + 500) / 1000 * 1000;
        g_string_printf(sub, "Filetype: Flipper SubGhz RAW File\nVersion: 1\n"
                        "Frequency: %u\nPreset: FuriHalSubGhzPresetCustom\n"
                        "Custom_preset_module: CC1101\nCustom_preset_data: ", freq);
        cc1101_preset_data(s, sub);
        g_string_append(sub, "\nProtocol: RAW");
        g_string_printf(last, "@%u", freq);
        for (guint i = first; i < n; i++) {
            if ((i - first) % 512 == 0) {
                g_string_append(sub, "\nRAW_Data:");
            }
            g_string_append_printf(sub, " %d", d[i]);
            g_string_append_printf(last, ",%d", d[i]);
        }
        g_string_append_c(sub, '\n');
        g_free(s->tx_last);
        s->tx_last = g_strdup(last->str);
        flipper_link_send(&s->lk, "tx freq=%u mod=%s rate=%u dev=%u raw=%s\n", freq,
                         cc1101_mod_name(s), cc1101_rate_baud(s),
                         cc1101_dev_hz(s), strchr(last->str, ',') + 1);
        if (s->tx_dir) {
            g_autoptr(GError) err = NULL;
            g_autofree char *path = NULL;

            g_mkdir_with_parents(s->tx_dir, 0755);
            for (int k = 1; k < 100000; k++) {
                g_free(path);
                path = g_strdup_printf("%s/tx-%05d.sub", s->tx_dir, k);
                if (!g_file_test(path, G_FILE_TEST_EXISTS)) {
                    break;
                }
            }
            if (g_file_set_contents(path, sub->str, sub->len, &err)) {
                info_report("flipper-zero: Sub-GHz transmission at %.3f MHz, "
                            "%u levels -> %s", freq / 1e6, n - first, path);
            } else {
                warn_report("flipper-zero: %s", err->message);
            }
        }
    }
    g_array_free(s->tx, true);
    s->tx = NULL;
}

static uint8_t cc1101_status(Cc1101State *s)
{
    uint8_t st;

    switch (s->state) {
    case MARC_RX:     st = 1; break;
    case MARC_TX:     st = 2; break;
    case MARC_FSTXON: st = 3; break;
    default:          st = 0; break;
    }
    return st << 4 | (s->read ? 0 : 0x0F);  /* CHIP_RDYn = 0 */
}

static void cc1101_set_state(Cc1101State *s, uint8_t state)
{
    if (state == MARC_TX) {
        cc1101_tx_begin(s);
    } else if (s->state == MARC_TX) {
        cc1101_tx_end(s);
    }
    s->state = state;
    cc1101_link_check(s);
}

static void cc1101_strobe(Cc1101State *s, uint8_t cmd)
{
    switch (cmd) {
    case 0x30:                          /* SRES */
        cc1101_set_state(s, MARC_IDLE);
        memcpy(s->regs, cc1101_defaults, sizeof(s->regs));
        break;
    case 0x31: cc1101_set_state(s, MARC_FSTXON); break;
    case 0x32: case 0x33: case 0x36: cc1101_set_state(s, MARC_IDLE); break;
    case 0x34: case 0x38: cc1101_set_state(s, MARC_RX); break;
    case 0x35: cc1101_set_state(s, MARC_TX); break;
    case 0x39: cc1101_set_state(s, MARC_SLEEP); break;
    default: break;                     /* SFRX SFTX SWORRST SNOP */
    }
    cc1101_link_check(s);               /* SRES: the defaults' set-up */
    cc1101_update_gdo(s);
}

static uint8_t cc1101_status_reg(Cc1101State *s, uint8_t addr)
{
    switch (addr) {
    case 0x30: return 0x00;             /* PARTNUM */
    case 0x31: return 0x14;             /* VERSION */
    case 0x34: {                        /* RSSI: dBm = value/2 - 74 */
        long v = lround((cc1101_rssi_dbm(s) + 74) * 2);

        return (uint8_t)(int8_t)MIN(MAX(v, -128), 127);
    }
    case 0x35: return s->state;         /* MARCSTATE */
    case 0x38: return 0x00;             /* PKTSTATUS */
    case 0x39: return 0x94;             /* VCO_VC_DAC */
    default:   return 0x00;
    }
}

static uint32_t cc1101_transfer(SSIPeripheral *dev, uint32_t val)
{
    Cc1101State *s = CC1101(dev);
    uint8_t b = val;

    if (s->phase == 0) {
        s->read = b & 0x80;
        s->burst = b & 0x40;
        s->addr = b & 0x3F;
        uint8_t st = cc1101_status(s);
        if (s->addr >= 0x30 && s->addr <= 0x3D && !s->burst) {
            cc1101_strobe(s, s->addr);
            return st;                  /* stay in phase 0: next is header */
        }
        s->phase = 1;
        s->pa_idx = 0;
        return st;
    }
    /* data phase */
    uint8_t ret = cc1101_status(s);
    if (s->addr == 0x3E) {              /* PATABLE */
        if (s->read) {
            ret = s->patable[s->pa_idx & 7];
        } else {
            s->patable[s->pa_idx & 7] = b;
        }
        s->pa_idx++;
    } else if (s->addr == 0x3F) {       /* FIFO: nothing in, everything lost */
        ret = s->read ? 0 : ret;
    } else if (s->addr >= 0x2F) {       /* status register (burst read) */
        ret = cc1101_status_reg(s, s->addr);    /* 0x2F: no register */
    } else {
        if (s->read) {
            ret = s->regs[s->addr];
        } else {
            s->regs[s->addr] = b;
            if (s->addr == 0x00 || s->addr == 0x02) {
                cc1101_update_gdo(s);
            }
            if ((s->addr >= 0x0D && s->addr <= 0x12) || s->addr == 0x15 ||
                s->addr == 0x1B) {      /* FREQ, MDMCFG4-2, DEVIATN, AGCCTRL0 */
                cc1101_link_check(s);
            }
        }
        if (s->burst && s->addr < 0x2E) {
            s->addr++;
        }
    }
    if (!s->burst) {
        s->phase = 0;
    }
    return ret;
}

static int cc1101_set_cs(SSIPeripheral *dev, bool level)
{
    Cc1101State *s = CC1101(dev);

    s->phase = 0;
    if (s->state == MARC_SLEEP && !level) {
        s->state = MARC_IDLE;           /* CS low wakes the chip */
    }
    return 0;
}

static void cc1101_realize(SSIPeripheral *d, Error **errp)
{
    Cc1101State *s = CC1101(d);

    qdev_init_gpio_out_named(DEVICE(d), &s->gdo0, "gdo0", 1);
    qdev_init_gpio_out_named(DEVICE(d), &s->gdo2, "gdo2", 1);
    s->air_timer = timer_new_ns(QEMU_CLOCK_VIRTUAL, cc1101_air_edge, s);
    s->live_timer = timer_new_ns(QEMU_CLOCK_VIRTUAL, cc1101_live_edge, s);
    s->link_timer = timer_new_ns(QEMU_CLOCK_VIRTUAL, cc1101_link_announce, s);
    s->live = g_new0(int32_t, LIVE_MAX);
    flipper_link_init(&s->lk, &s->link, "Sub-GHz", cc1101_link_line,
                      cc1101_link_opened, s);
}

static char *cc1101_get_tx(Object *obj, Error **errp)
{
    return g_strdup(CC1101(obj)->tx_last ?: "");
}

static char *cc1101_get_rssi(Object *obj, Error **errp)
{
    return g_strdup_printf("%.1f", cc1101_rssi_dbm(CC1101(obj)));
}

/* what the radio is doing, as the link's cc1101 message has it */
static char *cc1101_get_radio(Object *obj, Error **errp)
{
    Cc1101State *s = CC1101(obj);

    if (!s->link_cfg) {
        cc1101_link_check(s);
    }
    return g_strdup(s->link_cfg);
}

static void cc1101_init(Object *obj)
{
    object_property_add_str(obj, "tx", cc1101_get_tx, NULL);
    object_property_set_description(obj, "tx",
        "The last transmission: @freq_hz,+high,-low,... in µs");
    object_property_add_str(obj, "rssi", cc1101_get_rssi, NULL);
    object_property_set_description(obj, "rssi",
        "The RSSI (dBm) the firmware reads now, at the chip's frequency and bandwidth");
    object_property_add_str(obj, "radio", cc1101_get_radio, NULL);
    object_property_set_description(obj, "radio",
        "What the chip is set to: state=sleep|idle|rx|fstxon|tx freq=Hz "
        "mod=... bw=Hz rate=baud dev=Hz boundary=dB");
    object_property_add_str(obj, "rx", cc1101_get_rx, cc1101_set_rx);
    object_property_set_description(obj, "rx",
        "Put a signal on the air: [@freq_hz,]+high,-low,... in µs, or file:PATH");
}

static void cc1101_reset_hold(Object *obj, ResetType type)
{
    Cc1101State *s = CC1101(obj);

    cc1101_tx_end(s);
    memcpy(s->regs, cc1101_defaults, sizeof(s->regs));
    memset(s->patable, 0, sizeof(s->patable));
    s->patable[0] = 0xC6;
    s->state = MARC_IDLE;
    s->phase = 0;
    if (s->live) {
        cc1101_link_check(s);           /* idle again: the bridge's air stops */
    }
}

static void cc1101_reset_exit(Object *obj, ResetType type)
{
    Cc1101State *s = CC1101(obj);

    /* GDO0 is only driven on changes: force the pins to the reset level */
    s->gdo0_level = !cc1101_gdo_level(s, s->regs[0x02]);
    cc1101_update_gdo(s);
}

/*
 * ---- a saved state
 *
 * What the chip is set to is saved.  What was on the air is not: a signal
 * being heard at the save (the one-shot air's, a bridge's) ends there, and
 * of a transmission under way the loaded machine captures the part after
 * the load.  GDO0's level is saved as its receivers save theirs.  A load
 * tells no line anything: cc1101_loaded() does, when the loaded machine
 * first runs.
 */
static int cc1101_post_load(void *opaque, int version_id)
{
    Cc1101State *s = opaque;

    /* what the loading machine itself had on the air, or was capturing */
    timer_del(s->air_timer);
    g_free(s->air);
    s->air = NULL;
    s->air_len = s->air_pos = 0;
    s->carrier = false;
    s->live_carrier = false;
    if (s->tx_active) {
        g_array_free(s->tx, true);
        s->tx = NULL;
        s->tx_active = false;
    }
    g_free(s->link_cfg);                /* the set-up is the loaded one: */
    s->link_cfg = NULL;                 /* a bridge is told, its air dropped */
    cc1101_link_check(s);
    return 0;
}

void cc1101_loaded(Cc1101State *s)
{
    cc1101_update_gdo(s);               /* no carrier now */
    if (s->state == MARC_TX) {
        cc1101_tx_begin(s);
    }
}

static const VMStateDescription vmstate_cc1101 = {
    .name = "cc1101",
    .version_id = 1,
    .minimum_version_id = 1,
    .post_load = cc1101_post_load,
    .fields = (const VMStateField[]) {
        VMSTATE_SSI_PERIPHERAL(parent_obj, Cc1101State),
        VMSTATE_UINT8_ARRAY(regs, Cc1101State, 0x2F),
        VMSTATE_UINT8_ARRAY(patable, Cc1101State, 8),
        VMSTATE_UINT8(state, Cc1101State),
        VMSTATE_INT32(phase, Cc1101State),
        VMSTATE_UINT8(addr, Cc1101State),
        VMSTATE_BOOL(read, Cc1101State),
        VMSTATE_BOOL(burst, Cc1101State),
        VMSTATE_INT32(pa_idx, Cc1101State),
        VMSTATE_BOOL(gdo0_level, Cc1101State),
        VMSTATE_BOOL(tx_level, Cc1101State),
        VMSTATE_END_OF_LIST()
    }
};

static const Property cc1101_props[] = {
    DEFINE_PROP_STRING("tx-dir", Cc1101State, tx_dir),
    DEFINE_PROP_CHR("link", Cc1101State, link),     /* a radio bridge */
};

static void cc1101_class_init(ObjectClass *klass, const void *data)
{
    SSIPeripheralClass *k = SSI_PERIPHERAL_CLASS(klass);
    ResettableClass *rc = RESETTABLE_CLASS(klass);

    device_class_set_props(DEVICE_CLASS(klass), cc1101_props);
    DEVICE_CLASS(klass)->vmsd = &vmstate_cc1101;
    k->realize = cc1101_realize;
    k->transfer = cc1101_transfer;
    k->set_cs = cc1101_set_cs;
    k->cs_polarity = SSI_CS_LOW;
    rc->phases.hold = cc1101_reset_hold;
    rc->phases.exit = cc1101_reset_exit;
}

static const TypeInfo flipper_rf_types[] = {
    {
        .name = TYPE_CC1101,
        .parent = TYPE_SSI_PERIPHERAL,
        .instance_size = sizeof(Cc1101State),
        .instance_init = cc1101_init,
        .class_init = cc1101_class_init,
    },
};
DEFINE_TYPES(flipper_rf_types)
