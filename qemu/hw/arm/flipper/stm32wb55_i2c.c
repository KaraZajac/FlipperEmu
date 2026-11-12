/*
 * STM32WB55 I2C (RM0434 chapter 32, "I2C v2" with NBYTES/AUTOEND/RELOAD)
 *
 * Master mode only, transactions complete instantly. The firmware drives it
 * by polling (targets/f7/furi_hal/furi_hal_i2c.c):
 *   - CR2 START with SADD/RD_WRN/NBYTES and AUTOEND, SOFTEND or RELOAD
 *   - TXIS -> write TXDR; RXNE -> read RXDR
 *   - wait STOPF (AUTOEND) or TC/TCR (SOFTEND/RELOAD), then restart
 *   - a NACKed address gives NACKF + STOPF (automatic STOP)
 *   - NBYTES=0 + AUTOEND is a presence probe (furi_hal_i2c_is_device_ready)
 *
 * Address 0, the general call, is taken by nobody: none of the devices here
 * listens to it, and QEMU's bus would have every one of them answer.
 *
 * Copyright (c) 2026, Kara Zajac
 * SPDX-License-Identifier: BSD-3-Clause
 */
#include "qemu/osdep.h"
#include "qemu/log.h"
#include "hw/core/irq.h"
#include "hw/core/qdev-properties.h"
#include "wb55.h"

#define CR1_PE      (1u << 0)

#define CR2_RD_WRN  (1u << 10)
#define CR2_START   (1u << 13)
#define CR2_STOP    (1u << 14)
#define CR2_NACK    (1u << 15)
#define CR2_RELOAD  (1u << 24)
#define CR2_AUTOEND (1u << 25)

#define ISR_TXE     (1u << 0)
#define ISR_TXIS    (1u << 1)
#define ISR_RXNE    (1u << 2)
#define ISR_NACKF   (1u << 4)
#define ISR_STOPF   (1u << 5)
#define ISR_TC      (1u << 6)
#define ISR_TCR     (1u << 7)
#define ISR_BUSY    (1u << 15)
#define ISR_DIR     (1u << 16)

static unsigned i2c_nbytes(WB55I2cState *s)
{
    return (s->cr2 >> 16) & 0xFF;
}

static void i2c_update_irq(WB55I2cState *s)
{
    uint32_t cr1 = s->cr1, isr = s->isr;
    bool ev = ((cr1 & (1u << 1)) && (isr & ISR_TXIS)) ||   /* TXIE  */
              ((cr1 & (1u << 2)) && (isr & ISR_RXNE)) ||   /* RXIE  */
              ((cr1 & (1u << 4)) && (isr & ISR_NACKF)) ||  /* NACKIE */
              ((cr1 & (1u << 5)) && (isr & ISR_STOPF)) ||  /* STOPIE */
              ((cr1 & (1u << 6)) && (isr & (ISR_TC | ISR_TCR)));
    qemu_set_irq(s->irq_ev, ev);
    qemu_set_irq(s->irq_er, 0);
}

static void i2c_tap(WB55I2cState *s, WB55I2cTap what, uint8_t value, bool ok)
{
    if (s->tap) {
        s->tap(s->tap_opaque, what, value, ok);
    }
}

/* the part that was told of is over: with a STOP, or with another START */
static void i2c_tap_end(WB55I2cState *s, bool stop)
{
    if (s->tap_open) {
        s->tap_open = false;
        i2c_tap(s, WB55_I2C_TAP_END, 0, stop);
    }
}

static void i2c_stop(WB55I2cState *s)
{
    if (s->active) {
        i2c_end_transfer(s->bus);
    }
    i2c_tap_end(s, true);
    s->active = false;
    s->isr &= ~(ISR_BUSY | ISR_TXIS | ISR_DIR);
    s->isr |= ISR_STOPF | ISR_TXE;
}

/* called when the current NBYTES window is complete */
static void i2c_window_done(WB55I2cState *s)
{
    if (s->cr2 & CR2_RELOAD) {
        s->isr |= ISR_TCR;
    } else if (s->cr2 & CR2_AUTOEND) {
        i2c_stop(s);
    } else {
        s->isr |= ISR_TC;
    }
}

static void i2c_fetch(WB55I2cState *s)
{
    s->rxdr = i2c_recv(s->bus);
    s->isr |= ISR_RXNE;
    i2c_tap(s, WB55_I2C_TAP_BYTE, s->rxdr, true);
}

static void i2c_start(WB55I2cState *s)
{
    uint8_t addr = (s->cr2 >> 1) & 0x7F;   /* 7-bit addressing */
    bool read = s->cr2 & CR2_RD_WRN;

    s->isr &= ~(ISR_TC | ISR_TCR | ISR_NACKF | ISR_STOPF | ISR_RXNE | ISR_TXIS);
    s->isr |= ISR_BUSY;
    s->count = 0;
    s->reading = read;
    if (read) {
        s->isr |= ISR_DIR;
    } else {
        s->isr &= ~ISR_DIR;
    }

    i2c_tap_end(s, false);              /* a START with no STOP before it */
    if (s->active && addr != s->last) {
        /*
         * ... and for another address.  QEMU's bus looks for a device only
         * when it is idle: a START in a transfer goes to the device that
         * has it, whatever the address is.  So that one is let go of here,
         * as it lets go by itself at an address that is not its own, and
         * the bus looks again.  (It is told of a STOP, which QEMU has no
         * other word for: a device that acts on one, as an EEPROM that then
         * keeps what was written, does so here.)
         */
        i2c_end_transfer(s->bus);
        s->active = false;
    }
    s->calls++;
    s->last = addr;
    s->last_read = read;
    s->last_taken = addr && !i2c_start_transfer(s->bus, addr, read);
    s->tap_open = true;
    i2c_tap(s, WB55_I2C_TAP_CALL, addr << 1 | read, s->last_taken);
    if (!s->last_taken) {
        /* address NACK: STOP is generated automatically */
        s->unanswered++;
        if (addr) {
            s->active = true;
        }
        s->isr |= ISR_NACKF;
        i2c_stop(s);
        return;
    }
    s->active = true;
    if (i2c_nbytes(s) == 0) {
        i2c_window_done(s);
        return;
    }
    if (read) {
        i2c_fetch(s);
    } else {
        s->isr |= ISR_TXIS;
    }
}

static uint64_t wb55_i2c_read(void *opaque, hwaddr addr, unsigned size)
{
    WB55I2cState *s = opaque;
    uint64_t v = 0;

    switch (addr) {
    case 0x00: v = s->cr1; break;
    case 0x04: v = s->cr2; break;
    case 0x08: v = s->oar1; break;
    case 0x0C: v = s->oar2; break;
    case 0x10: v = s->timingr; break;
    case 0x14: v = s->timeoutr; break;
    case 0x18: v = s->isr; break;
    case 0x20: v = s->pecr; break;
    case 0x24:
        v = s->rxdr;
        if (s->isr & ISR_RXNE) {
            s->isr &= ~ISR_RXNE;
            s->count++;
            if (s->count < i2c_nbytes(s)) {
                i2c_fetch(s);
            } else {
                if (s->cr2 & CR2_AUTOEND) {
                    i2c_nack(s->bus);   /* NACK the last byte */
                }
                i2c_window_done(s);
            }
        }
        break;
    case 0x28: v = s->txdr; break;
    }
    i2c_update_irq(s);
    return v;
}

static void wb55_i2c_write(void *opaque, hwaddr addr, uint64_t val64,
                           unsigned size)
{
    WB55I2cState *s = opaque;
    uint32_t val = val64;

    switch (addr) {
    case 0x00:
        if ((s->cr1 & CR1_PE) && !(val & CR1_PE)) {
            /* PE=0: software reset of the state machine */
            if (s->active) {
                i2c_end_transfer(s->bus);
            }
            i2c_tap_end(s, true);       /* the lines are let go of */
            s->active = false;
            s->isr = ISR_TXE;
        }
        s->cr1 = val;
        break;
    case 0x04: {
        bool was_reload = s->cr2 & CR2_RELOAD;
        s->cr2 = val & ~(CR2_START | CR2_STOP);
        if (val & CR2_START) {
            i2c_start(s);
        } else if (was_reload && (s->isr & ISR_TCR)) {
            /* continuation after RELOAD: new NBYTES window */
            s->isr &= ~ISR_TCR;
            s->count = 0;
            if (i2c_nbytes(s) == 0) {
                i2c_window_done(s);
            } else if (s->reading) {
                i2c_fetch(s);
            } else {
                s->isr |= ISR_TXIS;
            }
        }
        if (val & CR2_STOP) {
            s->isr &= ~ISR_TC;
            i2c_stop(s);
        }
        break;
    }
    case 0x08: s->oar1 = val; break;
    case 0x0C: s->oar2 = val; break;
    case 0x10: s->timingr = val; break;
    case 0x14: s->timeoutr = val; break;
    case 0x18:
        if (val & ISR_TXE) {
            s->isr |= ISR_TXE;          /* flush TXDR */
        }
        break;
    case 0x1C:                          /* ICR */
        s->isr &= ~(val & 0x3F38);
        break;
    case 0x28:
        s->txdr = val & 0xFF;
        if (s->active && !s->reading && (s->isr & ISR_TXIS)) {
            s->isr &= ~ISR_TXIS;
            if (i2c_send(s->bus, s->txdr)) {
                i2c_tap(s, WB55_I2C_TAP_BYTE, s->txdr, false);
                s->isr |= ISR_NACKF;
                i2c_stop(s);
                break;
            }
            i2c_tap(s, WB55_I2C_TAP_BYTE, s->txdr, true);
            s->count++;
            if (s->count < i2c_nbytes(s)) {
                s->isr |= ISR_TXIS;
            } else {
                i2c_window_done(s);
            }
        }
        break;
    default:
        break;
    }
    i2c_update_irq(s);
}

static const MemoryRegionOps wb55_i2c_ops = {
    .read = wb55_i2c_read,
    .write = wb55_i2c_write,
    .endianness = DEVICE_LITTLE_ENDIAN,
    .impl.min_access_size = 4,
    .impl.max_access_size = 4,
    .valid.min_access_size = 1,
    .valid.max_access_size = 4,
};

static void wb55_i2c_reset_hold(Object *obj, ResetType type)
{
    WB55I2cState *s = WB55_I2C(obj);

    if (s->active) {
        i2c_end_transfer(s->bus);
    }
    i2c_tap_end(s, true);
    s->active = false;
    s->cr1 = s->cr2 = s->oar1 = s->oar2 = s->timingr = s->timeoutr = 0;
    s->pecr = s->rxdr = s->txdr = 0;
    s->isr = ISR_TXE;
    s->count = 0;
}

static void wb55_i2c_init(Object *obj)
{
    WB55I2cState *s = WB55_I2C(obj);

    memory_region_init_io(&s->mmio, obj, &wb55_i2c_ops, s, "i2c", 0x400);
    sysbus_init_mmio(SYS_BUS_DEVICE(obj), &s->mmio);
    sysbus_init_irq(SYS_BUS_DEVICE(obj), &s->irq_ev);
    sysbus_init_irq(SYS_BUS_DEVICE(obj), &s->irq_er);
}

static void wb55_i2c_realize(DeviceState *dev, Error **errp)
{
    WB55I2cState *s = WB55_I2C(dev);

    s->bus = i2c_init_bus(dev, s->bus_name ?: "i2c");
}

static const Property wb55_i2c_props[] = {
    DEFINE_PROP_STRING("bus-name", WB55I2cState, bus_name),
};

static void wb55_i2c_class_init(ObjectClass *klass, const void *data)
{
    RESETTABLE_CLASS(klass)->phases.hold = wb55_i2c_reset_hold;
    DEVICE_CLASS(klass)->realize = wb55_i2c_realize;
    device_class_set_props(DEVICE_CLASS(klass), wb55_i2c_props);
}

static const TypeInfo wb55_i2c_info = {
    .name = TYPE_WB55_I2C,
    .parent = TYPE_SYS_BUS_DEVICE,
    .instance_size = sizeof(WB55I2cState),
    .instance_init = wb55_i2c_init,
    .class_init = wb55_i2c_class_init,
};

static void wb55_i2c_register(void)
{
    type_register_static(&wb55_i2c_info);
}
type_init(wb55_i2c_register)
