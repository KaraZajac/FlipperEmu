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
#include "qemu/timer.h"
#include "hw/core/qdev-properties.h"
#include "qapi/visitor.h"
#include "migration/vmstate.h"
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

/*
 * A saved state has the gauge with its data memory, and the battery as it
 * was set from outside: the battery of a loaded machine is the saved one.
 */
static const VMStateDescription vmstate_bq27220 = {
    .name = "bq27220",
    .version_id = 1,
    .minimum_version_id = 1,
    .fields = (const VMStateField[]) {
        VMSTATE_I2C_SLAVE(parent_obj, Bq27220State),
        VMSTATE_UINT8(ptr, Bq27220State),
        VMSTATE_UINT8_ARRAY(wbuf, Bq27220State, 40),
        VMSTATE_INT32(wlen, Bq27220State),
        VMSTATE_BOOL(reading, Bq27220State),
        VMSTATE_UINT8_ARRAY(snap, Bq27220State, 0x80),
        VMSTATE_UINT16(mac_cmd, Bq27220State),
        VMSTATE_UINT8_ARRAY(mac_data, Bq27220State, 32),
        VMSTATE_UINT8_ARRAY(dm, Bq27220State, 0x1000),
        VMSTATE_UINT16(pend_addr, Bq27220State),
        VMSTATE_UINT8_ARRAY(pend_data, Bq27220State, 32),
        VMSTATE_INT32(pend_len, Bq27220State),
        VMSTATE_UINT8(sec, Bq27220State),
        VMSTATE_UINT16(last_key, Bq27220State),
        VMSTATE_BOOL(initcomp, Bq27220State),
        VMSTATE_BOOL(cfgupdate, Bq27220State),
        VMSTATE_UINT32(soc, Bq27220State),
        VMSTATE_UINT32(voltage_mv, Bq27220State),
        VMSTATE_UINT32(temperature_dk, Bq27220State),
        VMSTATE_UINT32(capacity_mah, Bq27220State),
        VMSTATE_INT32(current_ma, Bq27220State),
        VMSTATE_BOOL(sysdwn, Bq27220State),
        VMSTATE_END_OF_LIST()
    }
};

static void bq27220_class_init(ObjectClass *klass, const void *data)
{
    I2CSlaveClass *k = I2C_SLAVE_CLASS(klass);

    DEVICE_CLASS(klass)->vmsd = &vmstate_bq27220;
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

/* (with whether there was power on VBUS: the cable is as it was saved) */
static const VMStateDescription vmstate_bq25896 = {
    .name = "bq25896",
    .version_id = 1,
    .minimum_version_id = 1,
    .fields = (const VMStateField[]) {
        VMSTATE_I2C_SLAVE(parent_obj, Bq25896State),
        VMSTATE_UINT8_ARRAY(regs, Bq25896State, 0x15),
        VMSTATE_UINT8(ptr, Bq25896State),
        VMSTATE_BOOL(first, Bq25896State),
        VMSTATE_BOOL(vbus, Bq25896State),
        VMSTATE_END_OF_LIST()
    }
};

static void bq25896_class_init(ObjectClass *klass, const void *data)
{
    I2CSlaveClass *k = I2C_SLAVE_CLASS(klass);

    DEVICE_CLASS(klass)->vmsd = &vmstate_bq25896;
    k->event = bq25896_event;
    k->send = bq25896_send;
    k->recv = bq25896_recv;
    RESETTABLE_CLASS(klass)->phases.hold = bq25896_reset_hold;
}

/* -------------------------------------------------------------- LP5562 */

/*
 * Four PWM outputs: B, G and R are the status LED, W is the backlight.  Each
 * is set from its I2C register or by one of three program engines (register
 * 70h says which).  An engine runs up to 16 commands on the chip's 32768 Hz
 * clock: ramp or wait, set PWM, go to start, branch, end, trigger.  The
 * firmware fades the backlight with a ramp on engine 1 and blinks the LED
 * with a loop on engine 2.
 */

enum { EXEC_HOLD, EXEC_STEP, EXEC_RUN, EXEC_ONCE };         /* ENGx_EXEC */
enum { MODE_DISABLED, MODE_LOAD, MODE_RUN, MODE_DIRECT };   /* ENGx_MODE */

#define LP_CHIP_EN      0x40
#define LP_CYCLES(n)    muldiv64(n, NANOSECONDS_PER_SECOND, 32768)
#define LP_COMMAND      16          /* clock cycles one command takes */
/* the engines are looked at no more often than this */
#define LP_SLICE_NS     (2 * SCALE_MS)

static const uint8_t lp5562_pwm_reg[] = { 0x02, 0x03, 0x04, 0x0E }; /* BGRW */

static int lp5562_exec(Lp5562State *s, int n)
{
    return (s->regs[0x00] >> (4 - 2 * n)) & 3;
}

static int lp5562_mode(Lp5562State *s, int n)
{
    return (s->regs[0x01] >> (4 - 2 * n)) & 3;
}

/* the chip is off, or an engine is being loaded: none of them goes on */
static bool lp5562_frozen(Lp5562State *s)
{
    return !(s->regs[0x00] & LP_CHIP_EN) ||
           lp5562_mode(s, 0) == MODE_LOAD || lp5562_mode(s, 1) == MODE_LOAD ||
           lp5562_mode(s, 2) == MODE_LOAD;
}

/* the engine that output ch (B, G, R, W) is mapped to, or -1: its register */
static int lp5562_engine_of(Lp5562State *s, int ch)
{
    return ((s->regs[0x70] >> (2 * ch)) & 3) - 1;
}

static uint8_t lp5562_out(Lp5562State *s, int ch)
{
    int n = lp5562_engine_of(s, ch);

    if (!(s->regs[0x00] & LP_CHIP_EN)) {
        return 0;
    }
    if (n < 0) {
        return s->regs[lp5562_pwm_reg[ch]];
    }
    switch (lp5562_mode(s, n)) {
    case MODE_DISABLED:
        return 0;
    case MODE_DIRECT:           /* engine 1 is the B register, 2 G, 3 R */
        return s->regs[0x02 + n];
    default:
        return s->eng[n].pwm;
    }
}

void lp5562_rgbw(Lp5562State *s, uint8_t *r, uint8_t *g, uint8_t *b,
                 uint8_t *w)
{
    *b = lp5562_out(s, 0);
    *g = lp5562_out(s, 1);
    *r = lp5562_out(s, 2);
    *w = lp5562_out(s, 3);
}

static void lp5562_tell(Lp5562State *s, bool always)
{
    uint32_t out = (uint32_t)lp5562_out(s, 3) << 24 | lp5562_out(s, 2) << 16 |
                   lp5562_out(s, 1) << 8 | lp5562_out(s, 0);

    if ((always || out != s->shown) && s->changed) {
        s->shown = out;
        s->changed(s->opaque);
    }
}

static void lp5562_set_pwm(Lp5562Engine *e, int pwm)
{
    e->pwm = MIN(MAX(pwm, 0), 255);
    e->peak = MAX(e->peak, e->pwm);
}

static void lp5562_trigger(Lp5562State *s, int to, int from)
{
    Lp5562Engine *e = &s->eng[to];

    e->trig |= 1 << from;
    if (e->blocked && (e->trig & e->need) == e->need) {
        e->blocked = false;         /* it has them all: it goes on */
        e->wait = 0;
    }
}

/* engine n takes the command at its PC */
static void lp5562_begin(Lp5562State *s, int n)
{
    Lp5562Engine *e = &s->eng[n];
    int exec = lp5562_exec(s, n), at = e->pc;
    const uint8_t *mem = &s->regs[0x10 + 0x20 * n + 2 * at];
    uint16_t cmd = mem[0] << 8 | mem[1];
    unsigned time = (cmd >> 8) & 0x3F, count = (cmd >> 7) & 0x3F;
    int64_t cycles = LP_COMMAND;

    e->busy = true;
    e->pc = (at + 1) & 15;
    if (exec != EXEC_RUN) {                         /* this one, then hold */
        s->regs[0x00] &= ~(3 << (4 - 2 * n));
    }
    if (!(cmd & 0x8000)) {
        if (time) {                                 /* ramp, or wait */
            cycles = time * (cmd & 0x4000 ? 512 : 16);
            e->step = LP_CYCLES(cycles);
            e->ramp = cmd & 0x7F ? (cmd & 0x7F) + 1 : 0;
            e->down = cmd & 0x80;
        } else if (cmd & 0x4000) {                  /* set PWM */
            lp5562_set_pwm(e, cmd & 0xFF);
        } else {                                    /* go to start */
            e->pc = 0;
        }
    } else if ((cmd & 0xE000) == 0xA000) {          /* branch */
        if (!count || e->loops[at] < count) {
            e->loops[at] += count != 0;
            e->pc = cmd & 15;
        } else {
            e->loops[at] = 0;
        }
    } else if ((cmd & 0xE000) == 0xC000) {          /* end */
        if (cmd & 0x1000) {                         /* int */
            s->regs[0x0C] |= 4 >> n;
        }
        if (cmd & 0x0800) {                         /* reset */
            e->pwm = 0;
        }
        e->pc = 0;
        e->busy = false;
        s->regs[0x00] &= ~(3 << (4 - 2 * n));
        return;
    } else if ((cmd & 0xE000) == 0xE000) {          /* trigger */
        e->need = (cmd >> 7) & 7;
        for (int to = 0; to < 3; to++) {
            if (cmd & (2 << to)) {
                lp5562_trigger(s, to, n);
            }
        }
    }
    if (exec == EXEC_ONCE && e->pc == ((at + 1) & 15)) {
        e->pc = at;                                 /* and it stays there */
    }
    e->wait = LP_CYCLES(cycles);
}

/* an engine with nothing in hand takes its next command, if it may */
static void lp5562_kick(Lp5562State *s, int n)
{
    if (!s->eng[n].busy && lp5562_mode(s, n) == MODE_RUN &&
        lp5562_exec(s, n) != EXEC_HOLD && !lp5562_frozen(s)) {
        lp5562_begin(s, n);
    }
}

/* the time of what engine n is at is over: a step of a ramp, or a command */
static void lp5562_over(Lp5562State *s, int n)
{
    Lp5562Engine *e = &s->eng[n];

    if (e->ramp) {
        lp5562_set_pwm(e, e->pwm + (e->down ? -1 : 1));
        if (--e->ramp) {
            e->wait = e->step;
            return;
        }
    }
    if (e->need) {                                  /* triggers to wait for */
        if ((e->trig & e->need) != e->need) {
            e->blocked = true;
            return;
        }
        e->trig &= ~e->need;
        e->need = 0;
    }
    e->busy = false;
    lp5562_kick(s, n);
}

/* bring the engines up to now */
static void lp5562_sync(Lp5562State *s)
{
    int64_t now = qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL), left = now - s->at;

    s->at = now;
    while (!lp5562_frozen(s)) {
        Lp5562Engine *first = NULL;
        int64_t t;

        for (int n = 0; n < 3; n++) {
            Lp5562Engine *e = &s->eng[n];

            if (e->busy && !e->blocked && (!first || e->wait < first->wait)) {
                first = e;
            }
        }
        if (!first) {
            break;
        }
        t = MIN(first->wait, left);
        left -= t;
        for (int n = 0; n < 3; n++) {
            if (s->eng[n].busy && !s->eng[n].blocked) {
                s->eng[n].wait -= t;
            }
        }
        if (first->wait) {
            break;
        }
        lp5562_over(s, first - s->eng);
        lp5562_tell(s, false);      /* each change: a flash can be short */
    }
}

static void lp5562_arm(Lp5562State *s)
{
    int64_t wait = INT64_MAX;

    for (int n = 0; n < 3 && !lp5562_frozen(s); n++) {
        if (s->eng[n].busy && !s->eng[n].blocked) {
            wait = MIN(wait, s->eng[n].wait);
        }
    }
    if (wait == INT64_MAX) {
        timer_del(s->timer);
    } else {
        timer_mod(s->timer, s->at + MAX(wait, LP_SLICE_NS));
    }
}

static void lp5562_timer(void *opaque)
{
    Lp5562State *s = opaque;

    lp5562_sync(s);
    lp5562_arm(s);
}

static void lp5562_reset(Lp5562State *s)
{
    memset(s->regs, 0, sizeof(s->regs));
    s->regs[0x05] = s->regs[0x06] = s->regs[0x07] = s->regs[0x0F] = 0xAF;
    s->regs[0x70] = 0x39;       /* B: engine 1, G: engine 2, R: engine 3 */
    memset(s->eng, 0, sizeof(s->eng));
}

static void lp5562_write(Lp5562State *s, uint8_t reg, uint8_t data)
{
    uint8_t old = s->regs[0x01];
    int n;

    switch (reg) {
    case 0x00:                                      /* ENABLE */
        s->regs[reg] = data;
        break;
    case 0x01:                                      /* OP_MODE */
        s->regs[reg] = data & 0x3F;
        for (n = 0; n < 3; n++) {
            int mode = lp5562_mode(s, n);

            /* disabled, load and direct make an engine start afresh */
            if (mode != MODE_RUN && mode != ((old >> (4 - 2 * n)) & 3)) {
                uint8_t pwm = mode == MODE_DISABLED ? 0 : s->eng[n].pwm;

                memset(&s->eng[n], 0, sizeof(s->eng[n]));
                s->eng[n].pwm = pwm;
            }
        }
        break;
    case 0x09 ... 0x0B:                             /* an engine's PC */
        if (lp5562_exec(s, reg - 0x09) == EXEC_HOLD) {
            s->eng[reg - 0x09].pc = data & 15;
        }
        return;
    case 0x0C:                                      /* STATUS: read only */
        return;
    case 0x0D:                                      /* RESET */
        if (data == 0xFF) {
            lp5562_reset(s);
        }
        return;
    case 0x10 ... 0x6F:                             /* an engine's program */
        if (lp5562_mode(s, (reg - 0x10) / 0x20) == MODE_LOAD) {
            s->regs[reg] = data;
        }
        return;
    default:
        if (reg < sizeof(s->regs)) {
            s->regs[reg] = data;
        }
        return;
    }
    for (n = 0; n < 3; n++) {
        lp5562_kick(s, n);
    }
}

static int lp5562_event(I2CSlave *i2c, enum i2c_event ev)
{
    Lp5562State *s = LP5562(i2c);

    if (ev == I2C_START_SEND) {
        s->first = true;
    } else if (ev == I2C_FINISH) {
        lp5562_tell(s, true);
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
    lp5562_sync(s);
    lp5562_write(s, s->ptr++, data);
    lp5562_arm(s);
    return 0;
}

static uint8_t lp5562_recv(I2CSlave *i2c)
{
    Lp5562State *s = LP5562(i2c);
    uint8_t reg = s->ptr++, v;

    lp5562_sync(s);
    switch (reg) {
    case 0x09 ... 0x0B:
        return s->eng[reg - 0x09].pc;
    case 0x0C:                                      /* reading clears it */
        v = s->regs[reg];
        s->regs[reg] = 0;
        return v;
    default:
        return reg < sizeof(s->regs) ? s->regs[reg] : 0;
    }
}

/* the engine that is at work on output ch, or -1 */
static int lp5562_at_work(Lp5562State *s, int ch)
{
    int n = lp5562_engine_of(s, ch);

    return n >= 0 && (s->regs[0x00] & LP_CHIP_EN) &&
           lp5562_mode(s, n) == MODE_RUN && s->eng[n].busy ? n : -1;
}

/* the LED as rrggbb.  A colour that an engine is at work on (a blink) is the
 * brightest the engine has made it, so that it is the same whenever asked */
static char *lp5562_get_led(Object *obj, Error **errp)
{
    Lp5562State *s = LP5562(obj);
    uint32_t rgb = 0;

    lp5562_sync(s);
    for (int ch = 2; ch >= 0; ch--) {
        int n = lp5562_at_work(s, ch);

        rgb = rgb << 8 | (n < 0 ? lp5562_out(s, ch) : s->eng[n].peak);
    }
    return g_strdup_printf("%06x", rgb);
}

static bool lp5562_get_blinking(Object *obj, Error **errp)
{
    Lp5562State *s = LP5562(obj);

    lp5562_sync(s);
    return lp5562_at_work(s, 0) >= 0 || lp5562_at_work(s, 1) >= 0 ||
           lp5562_at_work(s, 2) >= 0;
}

static void lp5562_get_backlight(Object *obj, Visitor *v, const char *name,
                                 void *opaque, Error **errp)
{
    Lp5562State *s = LP5562(obj);
    uint8_t w;

    lp5562_sync(s);
    w = lp5562_out(s, 3);
    visit_type_uint8(v, name, &w, errp);
}

/* the three engines: "off", "load", "direct", or what it does, where its PC
 * is and its PWM, as "run@3=255" ("wait": for a trigger) */
static char *lp5562_get_engines(Object *obj, Error **errp)
{
    static const char *const mode[] = { "off", "load", NULL, "direct" };
    Lp5562State *s = LP5562(obj);
    GString *text = g_string_new(NULL);

    lp5562_sync(s);
    for (int n = 0; n < 3; n++) {
        Lp5562Engine *e = &s->eng[n];

        g_string_append(text, n ? " " : "");
        if (mode[lp5562_mode(s, n)]) {
            g_string_append(text, mode[lp5562_mode(s, n)]);
        } else {
            g_string_append_printf(text, "%s@%u=%u", e->blocked ? "wait" :
                                   e->busy ? "run" : "hold", e->pc, e->pwm);
        }
    }
    return g_string_free(text, false);
}

static void lp5562_reset_hold(Object *obj, ResetType type)
{
    Lp5562State *s = LP5562(obj);

    lp5562_reset(s);
    timer_del(s->timer);
}

static void lp5562_init(Object *obj)
{
    Lp5562State *s = LP5562(obj);

    s->timer = timer_new_ns(QEMU_CLOCK_VIRTUAL, lp5562_timer, s);
}

/*
 * A saved state has the engines where they are in their programs: a blink
 * or a fade that was under way goes on from there.  The board is not told
 * of the outputs in a load (the display has what it was last told).
 */
static const VMStateDescription vmstate_lp5562_engine = {
    .name = "lp5562/engine",
    .version_id = 1,
    .minimum_version_id = 1,
    .fields = (const VMStateField[]) {
        VMSTATE_UINT8(pc, Lp5562Engine),
        VMSTATE_UINT8(pwm, Lp5562Engine),
        VMSTATE_UINT8(peak, Lp5562Engine),
        VMSTATE_UINT8_ARRAY(loops, Lp5562Engine, 16),
        VMSTATE_UINT8(trig, Lp5562Engine),
        VMSTATE_UINT8(need, Lp5562Engine),
        VMSTATE_UINT8(ramp, Lp5562Engine),
        VMSTATE_BOOL(down, Lp5562Engine),
        VMSTATE_BOOL(busy, Lp5562Engine),
        VMSTATE_BOOL(blocked, Lp5562Engine),
        VMSTATE_INT64(step, Lp5562Engine),
        VMSTATE_INT64(wait, Lp5562Engine),
        VMSTATE_END_OF_LIST()
    }
};

static const VMStateDescription vmstate_lp5562 = {
    .name = "lp5562",
    .version_id = 1,
    .minimum_version_id = 1,
    .fields = (const VMStateField[]) {
        VMSTATE_I2C_SLAVE(parent_obj, Lp5562State),
        VMSTATE_UINT8_ARRAY(regs, Lp5562State, 0x71),
        VMSTATE_UINT8(ptr, Lp5562State),
        VMSTATE_BOOL(first, Lp5562State),
        VMSTATE_STRUCT_ARRAY(eng, Lp5562State, 3, 1, vmstate_lp5562_engine,
                             Lp5562Engine),
        VMSTATE_TIMER_PTR(timer, Lp5562State),
        VMSTATE_INT64(at, Lp5562State),
        VMSTATE_UINT32(shown, Lp5562State),
        VMSTATE_END_OF_LIST()
    }
};

static void lp5562_class_init(ObjectClass *klass, const void *data)
{
    I2CSlaveClass *k = I2C_SLAVE_CLASS(klass);

    DEVICE_CLASS(klass)->vmsd = &vmstate_lp5562;
    k->event = lp5562_event;
    k->send = lp5562_send;
    k->recv = lp5562_recv;
    RESETTABLE_CLASS(klass)->phases.hold = lp5562_reset_hold;
    object_class_property_add_str(klass, "led", lp5562_get_led, NULL);
    object_class_property_add_bool(klass, "blinking", lp5562_get_blinking,
                                   NULL);
    object_class_property_add(klass, "backlight", "uint8",
                              lp5562_get_backlight, NULL, NULL, NULL);
    object_class_property_add_str(klass, "engines", lp5562_get_engines, NULL);
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
        .instance_init = lp5562_init,
        .class_init = lp5562_class_init,
    },
};
DEFINE_TYPES(flipper_power_types)
