/*
 * Flipper Zero power bus (I2C1): BQ27220 fuel gauge (0x55), BQ25896 charger
 * (0x6B), LP5562 LED/backlight driver (0x30).
 *
 * BQ27220: the firmware's driver (lib/drivers/bq27220.c) checks the device
 * number through the MAC, unseals (0x0414/0x3672), reads OperationStatus /
 * ControlStatus and compares its data-memory profile; on mismatch it resets,
 * takes full access (0xFFFF x2), enters CFG_UPDATE, rewrites every parameter
 * (address + big-endian data to 0x3E, checksum/length to 0x60), exits and
 * seals. A fresh virtual gauge goes through that once, like a new device.
 * A missing gauge would cost several 4 s retry delays and report 0 %.
 *
 * Copyright (c) 2026, Kara Zajac
 * SPDX-License-Identifier: BSD-3-Clause
 */
#include "qemu/osdep.h"
#include "qemu/log.h"
#include "hw/core/qdev-properties.h"
#include "qapi/visitor.h"
#include "flipper.h"

/* ------------------------------------------------------------- BQ27220 */

#define SEC_SEALED   3
#define SEC_UNSEALED 2
#define SEC_FULL     1

/* BatteryStatus SYSDWN, "the system should shut down": the gauge raises it
 * under one voltage and drops it over another.  Both are in its data
 * memory, where the firmware leaves them as they came; here they are these.
 * Off USB, the firmware then counts down and switches the Flipper off. */
#define SYSDWN_SET_MV    3150
#define SYSDWN_CLEAR_MV  3400

static uint16_t bq_opstatus(Bq27220State *s)
{
    return (s->sec << 1) | (s->initcomp ? 1u << 5 : 0) |
           (s->cfgupdate ? 1u << 10 : 0);
}

static void bq_mac_word(Bq27220State *s, uint16_t v)
{
    memset(s->mac_data, 0, sizeof(s->mac_data));
    s->mac_data[0] = v & 0xFF;
    s->mac_data[1] = v >> 8;
}

static void bq_control(Bq27220State *s, uint16_t sub)
{
    s->mac_cmd = sub;
    switch (sub) {
    case 0x0000: bq_mac_word(s, 0x0000); break;         /* CONTROL_STATUS */
    case 0x0001: bq_mac_word(s, 0x0220); break;         /* DEVICE_NUMBER  */
    case 0x0002: bq_mac_word(s, 0x0102); break;         /* FW_VERSION     */
    case 0x0003: bq_mac_word(s, 0x00A0); break;         /* HW_VERSION     */
    case 0x0030: s->sec = SEC_SEALED; break;            /* SEALED         */
    case 0x0041:                                        /* RESET          */
        s->initcomp = true;
        s->cfgupdate = false;
        break;
    case 0x0054: bq_mac_word(s, bq_opstatus(s)); break; /* OPERATION_STATUS */
    case 0x0056: bq_mac_word(s, 0x0000); break;         /* GAUGING_STATUS */
    case 0x0090:                                        /* ENTER_CFG_UPDATE */
        if (s->sec != SEC_SEALED) {
            s->cfgupdate = true;
        }
        break;
    case 0x0091: case 0x0092:                           /* EXIT_CFG_UPDATE */
        s->cfgupdate = false;
        break;
    case 0x0414:
        break;
    case 0x3672:
        if (s->last_key == 0x0414 && s->sec == SEC_SEALED) {
            s->sec = SEC_UNSEALED;
        }
        break;
    case 0xFFFF:
        if (s->last_key == 0xFFFF && s->sec == SEC_UNSEALED) {
            s->sec = SEC_FULL;
        }
        break;
    default:
        qemu_log_mask(LOG_UNIMP, "bq27220: subcommand 0x%04x\n", sub);
        break;
    }
    s->last_key = sub;
}

static void bq_dm_select(Bq27220State *s, uint16_t addr)
{
    s->mac_cmd = addr;
    for (int i = 0; i < 32; i++) {
        uint32_t a = addr + i - 0x9000;
        s->mac_data[i] = a < sizeof(s->dm) ? s->dm[a] : 0;
    }
}

static void bq_write_done(Bq27220State *s)
{
    int n = s->wlen;
    uint8_t *d = s->wbuf;
    uint8_t start = s->ptr;

    if (n == 0) {
        return;
    }
    if (start == 0x00 && n >= 2) {                      /* Control() */
        bq_control(s, d[0] | (d[1] << 8));
    } else if (start == 0x3E && n >= 2) {               /* MAC */
        uint16_t v = d[0] | (d[1] << 8);
        if (n == 2) {
            if (v >= 0x4000 && v != 0xFFFF) {
                bq_dm_select(s, v);
            } else {
                bq_control(s, v);
            }
        } else {
            s->pend_addr = v;
            s->pend_len = MIN(n - 2, 32);
            memcpy(s->pend_data, d + 2, s->pend_len);
        }
    } else if (start == 0x60 && n >= 2 && s->pend_len) { /* checksum, len */
        uint8_t sum = (s->pend_addr & 0xFF) + (s->pend_addr >> 8);
        for (int i = 0; i < s->pend_len; i++) {
            sum += s->pend_data[i];
        }
        if ((uint8_t)(0xFF - sum) == d[0] && d[1] == s->pend_len + 4) {
            for (int i = 0; i < s->pend_len; i++) {
                uint32_t a = s->pend_addr + i - 0x9000;
                if (a < sizeof(s->dm)) {
                    s->dm[a] = s->pend_data[i];
                }
            }
        } else {
            qemu_log_mask(LOG_GUEST_ERROR, "bq27220: bad DM checksum\n");
        }
        s->pend_len = 0;
    }
}

static void bq_put16(uint8_t *p, uint16_t v)
{
    p[0] = v & 0xFF;
    p[1] = v >> 8;
}

static void bq_snapshot(Bq27220State *s)
{
    uint8_t *r = s->snap;
    bool charging = s->current_ma > 0;
    uint32_t rem = s->capacity_mah * MIN(s->soc, 100u) / 100;

    if (s->voltage_mv <= SYSDWN_SET_MV) {
        s->sysdwn = true;
    } else if (s->voltage_mv >= SYSDWN_CLEAR_MV) {
        s->sysdwn = false;
    }
    memset(r, 0, sizeof(s->snap));
    bq_put16(r + 0x00, 0x0000);                     /* ControlStatus */
    bq_put16(r + 0x04, 0xFFFF);
    bq_put16(r + 0x06, s->temperature_dk);
    bq_put16(r + 0x08, s->voltage_mv);
    bq_put16(r + 0x0A, (charging ? 0 : 1) | (s->sysdwn ? 1u << 1 : 0) |
                       (1u << 3) | (s->soc >= 100 ? 1u << 9 : 0));
                                        /* DSG, SYSDWN, BATTPRES, FC */
    bq_put16(r + 0x0C, (uint16_t)(int16_t)s->current_ma);
    bq_put16(r + 0x10, rem);
    bq_put16(r + 0x12, s->capacity_mah);
    bq_put16(r + 0x14, (uint16_t)(int16_t)s->current_ma);
    bq_put16(r + 0x16, (!charging && s->current_ma) ?
                       rem * 60 / (uint32_t)-s->current_ma : 0xFFFF);
    bq_put16(r + 0x18, charging ?
                       (s->capacity_mah - rem) * 60 / s->current_ma : 0xFFFF);
    bq_put16(r + 0x1A, (uint16_t)-10);
    bq_put16(r + 0x1C, 0xFFFF);
    bq_put16(r + 0x24, (uint16_t)(int16_t)(s->current_ma * (int)s->voltage_mv
                                           / 1000));
    bq_put16(r + 0x28, s->temperature_dk);
    bq_put16(r + 0x2A, 7);                          /* CycleCount */
    bq_put16(r + 0x2C, MIN(s->soc, 100u));
    bq_put16(r + 0x2E, 100);                        /* StateOfHealth */
    bq_put16(r + 0x30, 4208);
    bq_put16(r + 0x32, charging ? s->current_ma : 0);
    bq_put16(r + 0x3A, bq_opstatus(s));
    bq_put16(r + 0x3C, s->capacity_mah);
    bq_put16(r + 0x3E, s->mac_cmd);
    memcpy(r + 0x40, s->mac_data, 32);
    uint8_t sum = (s->mac_cmd & 0xFF) + (s->mac_cmd >> 8);
    for (int i = 0; i < 32; i++) {
        sum += s->mac_data[i];
    }
    r[0x60] = 0xFF - sum;
    r[0x61] = 36;
}

static int bq27220_event(I2CSlave *i2c, enum i2c_event ev)
{
    Bq27220State *s = BQ27220(i2c);

    switch (ev) {
    case I2C_START_SEND:
        s->wlen = -1;                   /* first byte is the pointer */
        s->reading = false;
        break;
    case I2C_START_RECV:
        if (!s->reading) {
            bq_write_done(s);           /* repeated start ends the write */
        }
        s->reading = true;
        s->wlen = 0;
        bq_snapshot(s);
        break;
    case I2C_FINISH:
        if (!s->reading) {
            bq_write_done(s);
        }
        s->wlen = 0;
        s->reading = false;
        break;
    default:
        break;
    }
    return 0;
}

static int bq27220_send(I2CSlave *i2c, uint8_t data)
{
    Bq27220State *s = BQ27220(i2c);

    if (s->wlen < 0) {
        s->ptr = data;
        s->wlen = 0;
    } else if (s->wlen < (int)sizeof(s->wbuf)) {
        s->wbuf[s->wlen++] = data;
    }
    return 0;
}

static uint8_t bq27220_recv(I2CSlave *i2c)
{
    Bq27220State *s = BQ27220(i2c);
    uint8_t v = s->snap[s->ptr & 0x7F];

    s->ptr++;
    return v;
}

static void bq27220_reset_hold(Object *obj, ResetType type)
{
    Bq27220State *s = BQ27220(obj);

    /* the gauge is battery powered: keeps state across MCU resets */
    s->wlen = 0;
    s->reading = false;
    if (!s->initcomp) {
        s->sec = SEC_SEALED;
        s->initcomp = true;
        s->cfgupdate = false;
    }
}

static void bq_get_current(Object *obj, Visitor *v, const char *name,
                           void *opaque, Error **errp)
{
    visit_type_int32(v, name, &BQ27220(obj)->current_ma, errp);
}

static void bq_set_current(Object *obj, Visitor *v, const char *name,
                           void *opaque, Error **errp)
{
    visit_type_int32(v, name, &BQ27220(obj)->current_ma, errp);
}

/* Battery knobs stay writable at runtime: qom-set /machine/gauge soc 5 */
static void bq27220_init(Object *obj)
{
    Bq27220State *s = BQ27220(obj);

    s->soc = 87;
    s->voltage_mv = 4061;
    s->current_ma = -42;
    s->temperature_dk = 2981;
    s->capacity_mah = 2100;
    object_property_add_uint32_ptr(obj, "soc", &s->soc, OBJ_PROP_FLAG_READWRITE);
    object_property_add_uint32_ptr(obj, "voltage-mv", &s->voltage_mv,
                                   OBJ_PROP_FLAG_READWRITE);
    object_property_add_uint32_ptr(obj, "temperature-dk", &s->temperature_dk,
                                   OBJ_PROP_FLAG_READWRITE);
    object_property_add_uint32_ptr(obj, "capacity-mah", &s->capacity_mah,
                                   OBJ_PROP_FLAG_READWRITE);
    object_property_add(obj, "current-ma", "int32", bq_get_current,
                        bq_set_current, NULL, NULL);
}

static void bq27220_class_init(ObjectClass *klass, const void *data)
{
    I2CSlaveClass *k = I2C_SLAVE_CLASS(klass);

    k->event = bq27220_event;
    k->send = bq27220_send;
    k->recv = bq27220_recv;
    RESETTABLE_CLASS(klass)->phases.hold = bq27220_reset_hold;
}

/* ------------------------------------------------------------- BQ25896 */

static const uint8_t bq25896_defaults[0x15] = {
    0x3A, 0x06, 0x3D, 0x1A, 0x20, 0x13, 0x5E, 0x9D, 0x03, 0x44, 0x93,
    0x00, 0x00, 0x12, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x02,
};

static void bq25896_adc(Bq25896State *s)
{
    uint32_t vbat = s->gauge ? s->gauge->voltage_mv : 4000;
    bool charging = s->vbus && s->gauge && s->gauge->soc < 100;

    /* REG0B: VBUS_STAT, CHRG_STAT, PG_STAT, VSYS_STAT */
    s->regs[0x0B] = s->vbus ? ((0x2 << 5) | ((charging ? 2 : 3) << 3) | (1 << 2))
                            : 0;
    s->regs[0x0E] = vbat > 2304 ? MIN((vbat - 2304) / 20, 127u) : 0;
    s->regs[0x0F] = vbat > 2304 ? MIN((vbat + 50 - 2304) / 20, 127u) : 0;
    s->regs[0x10] = 0x4C;                           /* TSPCT ~ room temp */
    s->regs[0x11] = s->vbus ? 0x80 | ((5000 - 2600) / 100) : 0;
    s->regs[0x12] = charging ? 500 / 50 : 0;
}

static int bq25896_event(I2CSlave *i2c, enum i2c_event ev)
{
    Bq25896State *s = BQ25896(i2c);

    if (ev == I2C_START_SEND) {
        s->first = true;
    } else if (ev == I2C_START_RECV) {
        bq25896_adc(s);
    }
    return 0;
}

/*
 * REG09 BATFET_DIS turns the battery's switch off, at once with BATFET_DLY
 * clear (as the firmware leaves it).  That is how the Flipper switches
 * itself off: with nothing on VBUS either, the system rail is gone.  While
 * USB power is there it keeps running, and goes off when the cable does.
 */
static void bq25896_batfet(Bq25896State *s)
{
    if ((s->regs[0x09] & 0x20) && !s->vbus && s->power_off) {
        s->power_off(s->opaque);
    }
}

static int bq25896_send(I2CSlave *i2c, uint8_t data)
{
    Bq25896State *s = BQ25896(i2c);

    if (s->first) {
        s->ptr = data;
        s->first = false;
        return 0;
    }
    if (s->ptr < sizeof(s->regs)) {
        if (s->ptr == 0x14 && (data & 0x80)) {      /* REG_RST */
            memcpy(s->regs, bq25896_defaults, sizeof(s->regs));
        } else if (s->ptr < 0x0B || s->ptr == 0x0D) {
            s->regs[s->ptr] = data;                 /* status regs are RO */
            if (s->ptr == 0x09) {
                bq25896_batfet(s);
            }
        }
    }
    s->ptr++;
    return 0;
}

static uint8_t bq25896_recv(I2CSlave *i2c)
{
    Bq25896State *s = BQ25896(i2c);
    uint8_t v = s->ptr < sizeof(s->regs) ? s->regs[s->ptr] : 0;

    s->ptr++;
    return v;
}

static void bq25896_reset_hold(Object *obj, ResetType type)
{
    Bq25896State *s = BQ25896(obj);

    memcpy(s->regs, bq25896_defaults, sizeof(s->regs));
    bq25896_adc(s);
}

static bool bq25896_get_usb(Object *obj, Error **errp)
{
    return BQ25896(obj)->vbus;
}

static void bq25896_set_usb(Object *obj, bool value, Error **errp)
{
    Bq25896State *s = BQ25896(obj);

    s->vbus = value;
    if (s->gauge) {                     /* plugged in: charging current */
        s->gauge->current_ma = value ? 480 : -42;
    }
    bq25896_adc(s);
    bq25896_batfet(s);
}

/* qom-set /machine/charger usb true  - plug a USB charger in */
static void bq25896_init(Object *obj)
{
    object_property_add_bool(obj, "usb", bq25896_get_usb, bq25896_set_usb);
}

static void bq25896_class_init(ObjectClass *klass, const void *data)
{
    I2CSlaveClass *k = I2C_SLAVE_CLASS(klass);

    k->event = bq25896_event;
    k->send = bq25896_send;
    k->recv = bq25896_recv;
    RESETTABLE_CLASS(klass)->phases.hold = bq25896_reset_hold;
}

/* -------------------------------------------------------------- LP5562 */

void lp5562_rgbw(Lp5562State *s, uint8_t *r, uint8_t *g, uint8_t *b,
                 uint8_t *w)
{
    bool en = s->regs[0x00] & 0x40;                 /* CHIP_EN */

    *b = en ? s->regs[0x02] : 0;
    *g = en ? s->regs[0x03] : 0;
    *r = en ? s->regs[0x04] : 0;
    *w = en ? s->regs[0x0E] : 0;
}

static int lp5562_event(I2CSlave *i2c, enum i2c_event ev)
{
    Lp5562State *s = LP5562(i2c);

    if (ev == I2C_START_SEND) {
        s->first = true;
    } else if (ev == I2C_FINISH && s->changed) {
        s->changed(s->opaque);
    }
    return 0;
}

static int lp5562_send(I2CSlave *i2c, uint8_t data)
{
    Lp5562State *s = LP5562(i2c);

    if (s->first) {
        s->ptr = data;
        s->first = false;
        return 0;
    }
    if (s->ptr == 0x0D) {
        if (data == 0xFF) {                         /* reset */
            memset(s->regs, 0, sizeof(s->regs));
            s->regs[0x05] = s->regs[0x06] = s->regs[0x07] = 0xAF;
            s->regs[0x0F] = 0xAF;
        }
    } else if (s->ptr < sizeof(s->regs)) {
        s->regs[s->ptr] = data;
    }
    s->ptr++;
    return 0;
}

static uint8_t lp5562_recv(I2CSlave *i2c)
{
    Lp5562State *s = LP5562(i2c);
    uint8_t v = s->ptr < sizeof(s->regs) ? s->regs[s->ptr] : 0;

    s->ptr++;
    return v;
}

static void lp5562_reset_hold(Object *obj, ResetType type)
{
    Lp5562State *s = LP5562(obj);

    memset(s->regs, 0, sizeof(s->regs));
    s->regs[0x05] = s->regs[0x06] = s->regs[0x07] = s->regs[0x0F] = 0xAF;
}

static void lp5562_class_init(ObjectClass *klass, const void *data)
{
    I2CSlaveClass *k = I2C_SLAVE_CLASS(klass);

    k->event = lp5562_event;
    k->send = lp5562_send;
    k->recv = lp5562_recv;
    RESETTABLE_CLASS(klass)->phases.hold = lp5562_reset_hold;
}

static const TypeInfo flipper_power_types[] = {
    {
        .name = TYPE_BQ27220,
        .parent = TYPE_I2C_SLAVE,
        .instance_size = sizeof(Bq27220State),
        .instance_init = bq27220_init,
        .class_init = bq27220_class_init,
    }, {
        .name = TYPE_BQ25896,
        .parent = TYPE_I2C_SLAVE,
        .instance_size = sizeof(Bq25896State),
        .instance_init = bq25896_init,
        .class_init = bq25896_class_init,
    }, {
        .name = TYPE_LP5562,
        .parent = TYPE_I2C_SLAVE,
        .instance_size = sizeof(Lp5562State),
        .class_init = lp5562_class_init,
    },
};
DEFINE_TYPES(flipper_power_types)
