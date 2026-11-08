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
#include "qemu/log.h"
#include "qemu/timer.h"
#include "qemu/cutils.h"
#include "qemu/error-report.h"
#include "qapi/error.h"
#include "hw/core/irq.h"
#include "hw/core/qdev-properties.h"
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
    case 0x0D: lvl = cc1101_hears(s) && s->carrier; break; /* async data */
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
    cc1101_update_gdo(s);
}

static uint8_t cc1101_status_reg(Cc1101State *s, uint8_t addr)
{
    switch (addr) {
    case 0x30: return 0x00;             /* PARTNUM */
    case 0x31: return 0x14;             /* VERSION */
    case 0x34:                          /* RSSI: dBm = value/2 - 74 */
        return cc1101_hears(s) && s->air ? 0x44 /* -40 dBm */
                                         : 0xCC /* -100 dBm */;
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
    } else if (s->addr >= 0x30) {       /* status register (burst read) */
        ret = cc1101_status_reg(s, s->addr);
    } else {
        if (s->read) {
            ret = s->regs[s->addr];
        } else {
            s->regs[s->addr] = b;
            if (s->addr == 0x00 || s->addr == 0x02) {
                cc1101_update_gdo(s);
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
}

static char *cc1101_get_tx(Object *obj, Error **errp)
{
    return g_strdup(CC1101(obj)->tx_last ?: "");
}

static void cc1101_init(Object *obj)
{
    object_property_add_str(obj, "tx", cc1101_get_tx, NULL);
    object_property_set_description(obj, "tx",
        "The last transmission: @freq_hz,+high,-low,... in µs");
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
}

static void cc1101_reset_exit(Object *obj, ResetType type)
{
    Cc1101State *s = CC1101(obj);

    /* GDO0 is only driven on changes: force the pins to the reset level */
    s->gdo0_level = !cc1101_gdo_level(s, s->regs[0x02]);
    cc1101_update_gdo(s);
}

static const Property cc1101_props[] = {
    DEFINE_PROP_STRING("tx-dir", Cc1101State, tx_dir),
};

static void cc1101_class_init(ObjectClass *klass, const void *data)
{
    SSIPeripheralClass *k = SSI_PERIPHERAL_CLASS(klass);
    ResettableClass *rc = RESETTABLE_CLASS(klass);

    device_class_set_props(DEVICE_CLASS(klass), cc1101_props);
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
