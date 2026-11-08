/*
 * Flipper Zero radio front-ends on SPI1: TI CC1101 (sub-GHz, CS=PD0,
 * GDO0=PA1) and ST25R3916 (NFC, CS=PE4, IRQ=PA2).
 *
 * Register-level models: enough for the firmware's own bring-up
 * (furi_hal_subghz_init() GDO0 self-test, furi_hal_nfc_init() chip id,
 * oscillator and VDD measurement) and for apps to configure the chips.
 *
 * Sub-GHz receive: a signal can be put "on the air" through the CC1101's
 * "rx" property (+high/-low µs, optionally at a frequency).  While the chip
 * is in RX, tuned within 500 kHz and set to asynchronous serial output on
 * GDO0 (IOCFG0 = 0x0D), GDO0 follows the carrier, and RSSI reads strong.
 * GDO0 is PA1 and TIM2's TI2, which is how furi_hal_subghz captures it.
 *
 * Copyright (c) 2026, Kara Zajac
 * SPDX-License-Identifier: BSD-3-Clause
 */
#include "qemu/osdep.h"
#include "qemu/log.h"
#include "qemu/timer.h"
#include "qemu/cutils.h"
#include "qapi/error.h"
#include "hw/core/irq.h"
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
 * reach the capture timer waits (up to 10 ms) for the firmware to service
 * the previous capture; the timestamp keeps the measured duration exact.
 */
static void cc1101_air_edge(void *opaque)
{
    Cc1101State *s = opaque;
    int64_t when = s->air_next_ns;
    int64_t now = qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL);
    bool level;

    if (s->tim && cc1101_hears(s) && wb55_tim_capture_pending(s->tim) &&
        now < when + 10 * SCALE_MS) {
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

static void cc1101_strobe(Cc1101State *s, uint8_t cmd)
{
    switch (cmd) {
    case 0x30:                          /* SRES */
        memcpy(s->regs, cc1101_defaults, sizeof(s->regs));
        s->state = MARC_IDLE;
        break;
    case 0x31: s->state = MARC_FSTXON; break;
    case 0x32: case 0x33: case 0x36: s->state = MARC_IDLE; break;
    case 0x34: case 0x38: s->state = MARC_RX; break;
    case 0x35: s->state = MARC_TX; break;
    case 0x39: s->state = MARC_SLEEP; break;
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

static void cc1101_init(Object *obj)
{
    object_property_add_str(obj, "rx", cc1101_get_rx, cc1101_set_rx);
    object_property_set_description(obj, "rx",
        "Put a signal on the air: [@freq_hz,]+high,-low,... in µs, or file:PATH");
}

static void cc1101_reset_hold(Object *obj, ResetType type)
{
    Cc1101State *s = CC1101(obj);

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

static void cc1101_class_init(ObjectClass *klass, const void *data)
{
    SSIPeripheralClass *k = SSI_PERIPHERAL_CLASS(klass);
    ResettableClass *rc = RESETTABLE_CLASS(klass);

    k->realize = cc1101_realize;
    k->transfer = cc1101_transfer;
    k->set_cs = cc1101_set_cs;
    k->cs_polarity = SSI_CS_LOW;
    rc->phases.hold = cc1101_reset_hold;
    rc->phases.exit = cc1101_reset_exit;
}

/* ------------------------------------------------------------ ST25R3916 */

#define REG_OP_CONTROL   0x02
#define REG_IRQ_MASK0    0x16
#define REG_IRQ_MAIN     0x1A
#define REG_AD_RESULT    0x25
#define REG_AUX_DISPLAY  0x31
#define REG_IC_IDENTITY  0x3F

static void st25_update_irq(St25r3916State *s)
{
    bool pending = false;

    for (int i = 0; i < 4; i++) {
        pending |= s->irq_status[i] & ~s->irq_mask[i];
    }
    qemu_set_irq(s->irq, pending);
}

static void st25_raise(St25r3916State *s, int reg, uint8_t bits)
{
    s->irq_status[reg] |= bits;
    st25_update_irq(s);
}

static void st25_defaults(St25r3916State *s)
{
    memset(s->rega, 0, sizeof(s->rega));
    memset(s->regb, 0, sizeof(s->regb));
    memset(s->irq_status, 0, sizeof(s->irq_status));
    memset(s->irq_mask, 0, sizeof(s->irq_mask));
    s->rega[0x00] = 0x10;               /* IO_CONF1 */
    s->rega[0x03] = 0x08;               /* MODE: ISO14443A */
    s->rega[REG_IC_IDENTITY] = 0x2A;    /* ic_type 5 (ST25R3916), rev 2 */
}

static uint8_t st25_read_reg(St25r3916State *s, uint8_t a, bool b)
{
    if (b) {
        return s->regb[a & 0x3F];
    }
    if (a >= REG_IRQ_MASK0 && a < REG_IRQ_MASK0 + 4) {
        return s->irq_mask[a - REG_IRQ_MASK0];
    }
    if (a >= REG_IRQ_MAIN && a < REG_IRQ_MAIN + 4) {
        uint8_t v = s->irq_status[a - REG_IRQ_MAIN];
        s->irq_status[a - REG_IRQ_MAIN] = 0;    /* read clears */
        st25_update_irq(s);
        return v;
    }
    if (a == REG_AUX_DISPLAY) {
        return (s->rega[REG_OP_CONTROL] & 0x80) ? 0x10 : 0x00;  /* osc_ok */
    }
    return s->rega[a & 0x3F];
}

static void st25_write_reg(St25r3916State *s, uint8_t a, bool b, uint8_t v)
{
    if (b) {
        s->regb[a & 0x3F] = v;
        return;
    }
    if (a >= REG_IRQ_MASK0 && a < REG_IRQ_MASK0 + 4) {
        s->irq_mask[a - REG_IRQ_MASK0] = v;
        st25_update_irq(s);
        return;
    }
    if (a >= REG_IRQ_MAIN && a < REG_IRQ_MAIN + 4) {
        return;                         /* read-only */
    }
    if (a == REG_OP_CONTROL) {
        bool osc_on = (v & 0x80) && !(s->rega[a] & 0x80);
        s->rega[a] = v;
        if (osc_on) {
            st25_raise(s, 0, 0x80);     /* I_osc: oscillator stable */
        }
        return;
    }
    if (a == REG_IC_IDENTITY || a == REG_AUX_DISPLAY || a == REG_AD_RESULT) {
        return;
    }
    s->rega[a & 0x3F] = v;
}

static void st25_direct(St25r3916State *s, uint8_t cmd)
{
    switch (cmd) {
    case 0xC1:                          /* SET_DEFAULT */
        st25_defaults(s);
        st25_update_irq(s);
        break;
    case 0xC2: case 0xDB:               /* STOP, CLEAR_FIFO */
        break;
    case 0xDF:                          /* MEASURE_VDD: ~3.3 V */
        s->rega[REG_AD_RESULT] = 141;
        st25_raise(s, 1, 0x80);         /* I_dct */
        break;
    case 0xD3: case 0xD9: case 0xDD: case 0xDE: case 0xD6: case 0xD8:
        s->rega[REG_AD_RESULT] = 0x80;  /* measurements / calibrations */
        st25_raise(s, 1, 0x80);
        break;
    default:
        break;
    }
}

static uint32_t st25_transfer(SSIPeripheral *dev, uint32_t val)
{
    St25r3916State *s = ST25R3916(dev);
    uint8_t b = val;
    uint8_t ret = 0;

    switch (s->phase) {
    case 0:                             /* command byte */
        if (b == 0xFB) {                /* space B prefix */
            s->prefix_b = true;
            return 0;
        }
        if (b == 0xFC) {                /* test register access: ignore */
            s->phase = 3;
            return 0;
        }
        if ((b & 0xC0) == 0x00 || (b & 0xC0) == 0x40) {
            s->read = b & 0x40;
            s->addr = b & 0x3F;
            s->space_b = s->prefix_b;
            s->prefix_b = false;
            s->phase = 1;
        } else if ((b & 0xC0) == 0x80) {
            s->phase = 2;               /* FIFO / PT memory */
        } else {
            st25_direct(s, b);
        }
        return 0;
    case 1:
        if (s->read) {
            ret = st25_read_reg(s, s->addr, s->space_b);
        } else {
            st25_write_reg(s, s->addr, s->space_b, b);
        }
        s->addr = (s->addr + 1) & 0x3F;
        return ret;
    case 3:                             /* test access: swallow the frame */
        return 0;
    default:                            /* FIFO: empty */
        return 0;
    }
}

static int st25_set_cs(SSIPeripheral *dev, bool level)
{
    St25r3916State *s = ST25R3916(dev);

    s->phase = 0;
    s->prefix_b = false;
    return 0;
}

static void st25_realize(SSIPeripheral *d, Error **errp)
{
    St25r3916State *s = ST25R3916(d);

    qdev_init_gpio_out_named(DEVICE(d), &s->irq, "irq", 1);
}

static void st25_reset_hold(Object *obj, ResetType type)
{
    St25r3916State *s = ST25R3916(obj);

    st25_defaults(s);
    s->phase = 0;
    s->prefix_b = false;
}

static void st25_reset_exit(Object *obj, ResetType type)
{
    st25_update_irq(ST25R3916(obj));
}

static void st25_class_init(ObjectClass *klass, const void *data)
{
    SSIPeripheralClass *k = SSI_PERIPHERAL_CLASS(klass);
    ResettableClass *rc = RESETTABLE_CLASS(klass);

    k->realize = st25_realize;
    k->transfer = st25_transfer;
    k->set_cs = st25_set_cs;
    k->cs_polarity = SSI_CS_LOW;
    rc->phases.hold = st25_reset_hold;
    rc->phases.exit = st25_reset_exit;
}

static const TypeInfo flipper_rf_types[] = {
    {
        .name = TYPE_CC1101,
        .parent = TYPE_SSI_PERIPHERAL,
        .instance_size = sizeof(Cc1101State),
        .instance_init = cc1101_init,
        .class_init = cc1101_class_init,
    }, {
        .name = TYPE_ST25R3916,
        .parent = TYPE_SSI_PERIPHERAL,
        .instance_size = sizeof(St25r3916State),
        .class_init = st25_class_init,
    },
};
DEFINE_TYPES(flipper_rf_types)
