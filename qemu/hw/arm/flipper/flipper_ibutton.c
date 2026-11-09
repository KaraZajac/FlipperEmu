/*
 * Flipper Zero iButton contact, with a virtual key on it
 *
 * The iButton pad is PB14, open drain with a pull-up.  Two kinds of key:
 *
 * Dallas 1-Wire keys (DS1990A, DS1992, DS1996, DS1971, and any other family
 * with its ROM only) are a 1-Wire slave on the line.  The firmware is the
 * bus master (lib/one_wire/one_wire_host.c): with interrupts off it drives
 * the line low, waits on DWT->CYCCNT, and samples it.  The slave follows the
 * master's own drive (the board reports GPIOB's writes), with virtual
 * times, so it sees the master's edges even while it holds the line low
 * itself.  A long low is a reset, which it answers with a presence pulse;
 * after that each slot starts at a falling edge, which also ends whatever
 * the slave still held from the last one.
 *
 * A bit the master writes is its low time: 9 us a 1, 64 us a 0 (1 and 8
 * at overdrive speed).  The master busy-waits at least that long, and an
 * emulated one only ever takes longer (overdrive's 1 us low takes 2-4), so
 * the threshold sits just under the 0's time: a 1 may stretch that far.
 *
 * To send a 0 the slave pulls the line low at once.  On silicon it lets go
 * after a fixed time, and the master samples well before that.  Here
 * virtual time runs on while the host preempts the vCPU, so the master's
 * sample can fall past any fixed time.  Instead the slave lets go once the
 * master has sampled: the board reports each read of GPIOB's IDR made
 * while the firmware's own drive is off (flipper_ibutton_sampled).  A
 * timer lets go anyway after 1 ms.
 *
 *   ROM commands: READ ROM, SEARCH ROM, SKIP ROM, MATCH ROM, and overdrive
 *   SKIP/MATCH ROM (a standard-length reset returns to standard speed).
 *   Function commands: READ MEMORY (a 2-byte address; 1 byte on a DS1971).
 *
 * A blank ("blank" line) is a 1-Wire key whose ROM can be written, as the
 * firmware writes the four kinds it knows (lib/ibutton/protocols/blanks):
 *   RW1990.1: D1 + a 0 bit enables writing, D5 + 64 bits (inverted) writes
 *     the ROM, D1 + a 1 bit locks it again;
 *   RW1990.2: the same with 1D, a 1 bit to enable, and plain bits;
 *   TM01x: C1 + a 1 bit, then C5 + 64 plain bits;
 *   TM2004: 3C + a 2-byte address, then per byte: the byte, its CRC back, a
 *     programming pulse, and the byte as programmed back.
 *
 * Cyfral and Metakom keys modulate their current on the contact, which the
 * Flipper senses through the RFID front end: COMP1, with the 125 kHz field
 * off (lib/ibutton/protocols/misc).  While COMP1 is enabled and no field is
 * on, such a key plays its waveform into COMP1: one frame of +high/-low
 * microseconds, repeated.  The firmware times these edges in software: its
 * COMP ISR reads COMP1's VALUE, then DWT->CYCCNT.  On silicon that ISR runs
 * within a fraction of a microsecond; an emulated one tens of them later,
 * and by a varying amount, which turns 41 us segments into 82 us ones.  So
 * the ISR's CYCCNT read is stamped with the edge's time on the key's own
 * timeline (wb55_dwt_stamp_next), and each edge waits until the ISR has
 * read the previous one, so two never merge into one interrupt.  The
 * timeline resyncs if delivery falls 1 ms behind it.
 *
 * "key" takes the key: file:PATH of a description from scripts/
 * fe_ibutton.py ("type onewire", "rom", "addr", "mem" lines, or "type wave"
 * and "wave"), or none.
 *
 * The other way round, the Flipper emulates a key:
 * - A Dallas key: its 1-Wire slave (lib/one_wire/one_wire_slave.c) catches
 *   a reset by EXTI, then busy-polls the pin with interrupts off.  The
 *   virtual reader ("reader", qom-set /machine/ibutton reader read) is a
 *   1-Wire master driven by that polling, not by time: it starts a slot on
 *   the slave's IDR read; writes a 1 by letting go at the read that saw the
 *   slot, a 0 by holding on 30 us past the slave's next read (its 20 us
 *   window starts between the two); and reads the slave's bit from its own
 *   drive, between the slot and the slave's next write of the pin.  It
 *   reads the ROM, and a memory key's memory, and writes a .ibtn file to
 *   "tx-dir" (the launcher: devices/NAME/ibutton-tx/); "tx" names it.
 *   The slave's waits read DWT before the pin, so a host stall of the vCPU
 *   longer than a window can make it miss a slot, or take a reset: then it
 *   reads the pin where it would write it, or goes quiet.  The reader then
 *   resets it, and resumes a memory read at the last whole byte, reading
 *   the 4 before it again to check that the address came through.
 * - A Cyfral or Metakom key: the firmware's TIM1 update ISR sets the next
 *   period (ARR), then the level PB14 holds for it.  The board reports each
 *   such pair (flipper_ibutton_emulated): the waveform as the firmware
 *   meant it, where an emulated ISR running late would make TIM1 repeat a
 *   level.  A capture is written once it is full (4096 levels), or when the
 *   emulation stops (PB14 no longer an output; or no level for a second).
 *
 * Copyright (c) 2026, Kara Zajac
 * SPDX-License-Identifier: BSD-3-Clause
 */
#include "qemu/osdep.h"
#include "qemu/timer.h"
#include "qemu/cutils.h"
#include "qemu/error-report.h"
#include "qapi/error.h"
#include "hw/core/irq.h"
#include "hw/core/qdev-properties.h"
#include "wb55.h"
#include "flipper.h"

enum { IBTN_NONE, IBTN_ONEWIRE, IBTN_WAVE };

/* what the slave does in the coming slots */
enum {
    OW_IDLE,                    /* not selected: until the next reset */
    OW_ROM_CMD,                 /* receive the ROM command */
    OW_TX,                      /* send txbuf, then tx_next */
    OW_SEARCH,                  /* per ROM bit: it, its complement, the choice */
    OW_MATCH,                   /* receive a ROM to compare */
    OW_FUNC,                    /* receive a function command */
    OW_ADDR,                    /* READ MEMORY: receive the address */
    OW_MEM,                     /* READ MEMORY: send memory, until a reset */
    OW_FLAG,                    /* a blank's write flag: one bit */
    OW_WROM,                    /* a blank's new ROM: 64 bits */
    OW_TM_ADDR,                 /* TM2004: the address to write */
    OW_TM_DATA,                 /* TM2004: a byte to write */
    OW_TM_PULSE,                /* TM2004: its programming pulse */
};

enum { BLANK_NONE, BLANK_RW1990_1, BLANK_RW1990_2, BLANK_TM01X, BLANK_TM2004 };
static const char *const blank_names[] = {
    [BLANK_RW1990_1] = "rw1990.1", [BLANK_RW1990_2] = "rw1990.2",
    [BLANK_TM01X] = "tm01x", [BLANK_TM2004] = "tm2004",
};

#define OW_RESET_NS       (300 * SCALE_US)  /* a longer low: a reset */
#define OW_OD_RESET_NS    (40 * SCALE_US)   /* ... at overdrive speed */
#define OW_BIT_NS         (56 * SCALE_US)   /* a shorter low writes a 1 */
#define OW_OD_BIT_NS      (7 * SCALE_US)
#define OW_HOLD_MAX_NS    (1 * SCALE_MS)    /* the master never sampled */
#define WAVE_SETTLE_NS    (1 * SCALE_MS)    /* the contact, then the key */
#define WAVE_READ_MAX_NS  (2 * SCALE_MS)    /* no ISR read by then: go on */
#define WAVE_RESYNC_NS    (1 * SCALE_MS)    /* delivery behind the timeline */
#define WAVE_SPACING_NS   (5 * SCALE_US)    /* an edge after the ISR's read */

/* ------------------------------------------------------------- 1-Wire */

/* the line as the slave drives it: low, or let go (the pull-up wins) */
static void ow_drive(FlipperIbuttonState *s, bool low)
{
    qemu_set_irq(s->pull, !low);
}

static uint8_t ow_mem(FlipperIbuttonState *s, uint32_t addr)
{
    return addr < s->mem_len ? s->mem[addr] : 0xFF;
}

/* the bit the slave sends in this slot; -1 when it receives one */
static int ow_tx_bit(FlipperIbuttonState *s)
{
    int b;

    switch (s->state) {
    case OW_TX:
        return (s->txbuf[s->bit / 8] >> (s->bit % 8)) & 1;
    case OW_MEM:
        return (ow_mem(s, s->addr + s->bit / 8) >> (s->bit % 8)) & 1;
    case OW_SEARCH:
        b = (s->rom[s->bit / 8] >> (s->bit % 8)) & 1;
        return s->phase == 0 ? b : s->phase == 1 ? !b : -1;
    default:
        return -1;
    }
}

static void ow_goto(FlipperIbuttonState *s, int state)
{
    s->state = state;
    s->bit = s->phase = s->count = 0;
    s->rx = 0;
}

/* send these bytes in the coming read slots, then go on to "next" */
static void ow_send(FlipperIbuttonState *s, const uint8_t *b, int n, int next)
{
    memcpy(s->txbuf, b, n);
    s->tx_bytes = n;
    s->tx_next = next;
    ow_goto(s, OW_TX);
}

static uint8_t ow_crc8(const uint8_t *b, int n)
{
    uint8_t crc = 0;

    for (int i = 0; i < n; i++) {
        uint8_t byte = b[i];

        for (int k = 0; k < 8; k++) {
            bool mix = (crc ^ byte) & 1;

            crc >>= 1;
            if (mix) {
                crc ^= 0x8C;
            }
            byte >>= 1;
        }
    }
    return crc;
}

/* a blank's own ROM commands: true when b was one */
static bool ow_blank_cmd(FlipperIbuttonState *s, uint8_t b)
{
    static const uint8_t flag[] = {
        [BLANK_RW1990_1] = 0xD1, [BLANK_RW1990_2] = 0x1D, [BLANK_TM01X] = 0xC1,
    };
    static const uint8_t wrom[] = {
        [BLANK_RW1990_1] = 0xD5, [BLANK_RW1990_2] = 0xD5, [BLANK_TM01X] = 0xC5,
    };

    if (s->blank == BLANK_NONE) {
        return false;
    }
    if (s->blank == BLANK_TM2004) {
        if (b != 0x3C) {
            return false;
        }
        ow_goto(s, OW_TM_ADDR);
        s->addr = 0;
        return true;
    }
    if (b == flag[s->blank]) {
        ow_goto(s, OW_FLAG);
        return true;
    }
    if (b == wrom[s->blank]) {
        ow_goto(s, OW_WROM);
        return true;
    }
    return false;
}

/* a whole byte came in */
static void ow_byte(FlipperIbuttonState *s, uint8_t b)
{
    switch (s->state) {
    case OW_ROM_CMD:
        if (ow_blank_cmd(s, b)) {
            return;
        }
        switch (b) {
        case 0x33:                      /* READ ROM */
            ow_send(s, s->rom, 8, OW_FUNC);
            return;
        case 0xF0:                      /* SEARCH ROM */
            ow_goto(s, OW_SEARCH);
            return;
        case 0xCC:                      /* SKIP ROM */
            ow_goto(s, OW_FUNC);
            return;
        case 0x3C:                      /* OVERDRIVE SKIP ROM */
            s->od = true;
            ow_goto(s, OW_FUNC);
            return;
        case 0x69:                      /* OVERDRIVE MATCH ROM */
            s->od = true;
            /* fall through */
        case 0x55:                      /* MATCH ROM */
            ow_goto(s, OW_MATCH);
            return;
        default:                        /* CONDITIONAL SEARCH (no alarm), ... */
            ow_goto(s, OW_IDLE);
            return;
        }
    case OW_MATCH:
        if (b != s->rom[s->count]) {
            ow_goto(s, OW_IDLE);        /* not us */
        } else if (++s->count == 8) {
            ow_goto(s, OW_FUNC);
        }
        return;
    case OW_FUNC:
        if (b == 0xF0 && s->addr_bytes) {   /* READ MEMORY */
            ow_goto(s, OW_ADDR);
            s->addr = 0;
        } else {
            ow_goto(s, OW_IDLE);        /* not modelled: stay quiet */
        }
        return;
    case OW_ADDR:
        s->addr |= (uint32_t)b << (8 * s->count);
        if (++s->count == s->addr_bytes) {
            uint32_t addr = s->addr;

            ow_goto(s, OW_MEM);
            s->addr = addr;
        }
        return;
    case OW_TM_ADDR:
        s->addr |= (uint32_t)b << (8 * s->count);
        if (++s->count == 2) {
            uint32_t addr = s->addr;

            ow_goto(s, OW_TM_DATA);
            s->addr = addr;
        }
        return;
    case OW_TM_DATA: {                  /* the byte: its CRC back, then a pulse */
        uint32_t addr = s->addr;

        s->tm_byte = b;
        ow_send(s, (uint8_t[]){ ow_crc8(&b, 1) }, 1, OW_TM_PULSE);
        s->addr = addr;
        return;
    }
    }
}

/* a blank's write is in: the new ROM */
static void ow_written(FlipperIbuttonState *s, const uint8_t *rom)
{
    memcpy(s->rom, rom, 8);
    info_report("flipper-zero: iButton %s blank: ROM <- %02X %02X %02X %02X "
                "%02X %02X %02X %02X", blank_names[s->blank], rom[0], rom[1],
                rom[2], rom[3], rom[4], rom[5], rom[6], rom[7]);
}

/* a slot is over: wbit is what the master wrote (1 in a read slot) */
static void ow_slot(FlipperIbuttonState *s, int wbit)
{
    switch (s->state) {
    case OW_ROM_CMD:
    case OW_MATCH:
    case OW_FUNC:
    case OW_ADDR:
    case OW_TM_ADDR:
    case OW_TM_DATA:
        s->rx |= wbit << s->bit;
        if (++s->bit == 8) {
            uint8_t b = s->rx;

            s->bit = 0;
            s->rx = 0;
            ow_byte(s, b);
        }
        return;
    case OW_TX:
        if (++s->bit == 8 * s->tx_bytes) {
            uint32_t addr = s->addr;

            ow_goto(s, s->tx_next);
            s->addr = addr;             /* TM2004 writes on from there */
        }
        return;
    case OW_FLAG:                       /* RW1990.1 enables on a 0, others on 1 */
        s->wr_enabled = wbit == (s->blank != BLANK_RW1990_1);
        ow_goto(s, OW_IDLE);
        return;
    case OW_WROM:
        if (wbit) {
            s->newrom[s->bit / 8] |= 1 << (s->bit % 8);
        } else {
            s->newrom[s->bit / 8] &= ~(1 << (s->bit % 8));
        }
        if (++s->bit == 64) {
            if (s->wr_enabled) {
                uint8_t rom[8];

                for (int i = 0; i < 8; i++) {   /* RW1990.1 takes it inverted */
                    rom[i] = s->blank == BLANK_RW1990_1 ? ~s->newrom[i] : s->newrom[i];
                }
                ow_written(s, rom);
            }
            ow_goto(s, OW_ROM_CMD);     /* the lock command needs no reset */
        }
        return;
    case OW_TM_PULSE: {                 /* programmed: the byte back */
        uint32_t addr = s->addr;

        if (addr < 8) {
            uint8_t rom[8];

            memcpy(rom, s->rom, 8);
            rom[addr] = s->tm_byte;
            if (addr == 7) {
                ow_written(s, rom);
            } else {
                memcpy(s->rom, rom, 8);
            }
        }
        ow_send(s, (uint8_t[]){ addr < 8 ? s->rom[addr] : 0xFF }, 1, OW_TM_DATA);
        s->addr = addr + 1;
        return;
    }
    case OW_MEM:
        s->bit++;                       /* on to the end of memory, then 1s */
        return;
    case OW_SEARCH:
        if (s->phase < 2) {
            s->phase++;
        } else if (wbit != ((s->rom[s->bit / 8] >> (s->bit % 8)) & 1)) {
            ow_goto(s, OW_IDLE);        /* the master took the other branch */
        } else {
            s->phase = 0;
            if (++s->bit == 64) {
                ow_goto(s, OW_FUNC);
            }
        }
        return;
    }
}

/* hold the line low until the master has sampled it */
static void ow_pull(FlipperIbuttonState *s, int64_t now, bool presence)
{
    s->pulling = true;
    s->presence = presence;
    ow_drive(s, true);
    timer_mod(s->timer, now + OW_HOLD_MAX_NS);
}

static void ow_release(FlipperIbuttonState *s)
{
    bool presence = s->presence;

    s->pulling = false;
    s->presence = false;
    timer_del(s->timer);
    ow_drive(s, false);
    if (!presence) {
        ow_slot(s, 0);                  /* the 0 it sent is out */
    }
}

static void ow_sampled(FlipperIbuttonState *s)
{
    if (s->pulling) {
        ow_release(s);
    }
}

static void ow_fall(FlipperIbuttonState *s, int64_t now)
{
    if (s->pulling) {
        ow_release(s);                  /* a new slot: the last one is over */
    }
    s->fall_ns = now;
    if (ow_tx_bit(s) == 0) {
        ow_pull(s, now, false);
    }
}

static void ow_rise(FlipperIbuttonState *s, int64_t now)
{
    int64_t low = now - s->fall_ns;

    if (low >= OW_RESET_NS || (s->od && low >= OW_OD_RESET_NS)) {
        if (s->pulling) {               /* that was no slot: drop the hold */
            s->pulling = s->presence = false;
            ow_drive(s, false);
        }
        if (low >= OW_RESET_NS) {
            s->od = false;              /* a standard reset: standard speed */
        }
        ow_goto(s, OW_ROM_CMD);
        ow_pull(s, now, true);
        return;
    }
    if (s->state != OW_IDLE && !s->pulling) {
        ow_slot(s, low < (s->od ? OW_OD_BIT_NS : OW_BIT_NS));
    }                                   /* holding a 0: over once it is sampled */
}

/* the firmware, as the master, changed its drive of the line */
static void ow_master(FlipperIbuttonState *s, bool low)
{
    if (low == s->m_low) {
        return;
    }
    s->m_low = low;
    if (s->kind != IBTN_ONEWIRE) {
        return;
    }
    if (low) {
        ow_fall(s, qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL));
    } else {
        ow_rise(s, qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL));
    }
}

static void ibtn_timer(void *opaque)
{
    ow_sampled(opaque);                 /* never sampled: let go anyway */
}

/* ------------------------------------------ reading an emulated key */

enum { RD_OFF, RD_RESET, RD_RESET_HOLD, RD_PRESENCE, RD_READY, RD_DETECT,
       RD_HOLD0, RD_READBIT };
enum { OP_RESET, OP_WRITE, OP_READ };

#define RD_RESET_NS      (600 * SCALE_US)   /* after the slave's ISR saw it */
#define RD_NO_ISR_NS     (5 * SCALE_MS)     /* no slave noticed the reset */
#define RD_HOLD0_NS      (30 * SCALE_US)    /* a 0 written: past its 20 us */
#define RD_OVERLAP       4                  /* bytes read again on a resume */
#define RD_TIMEOUT_NS    (20 * SCALE_MS)    /* no answer (the slave gives up at 15) */
#define RD_RETRIES       50                 /* in a row, with no byte read */

static void rd_line(FlipperIbuttonState *s, bool low)
{
    s->rd_pull = low;
    qemu_set_irq(s->pull, !low);
}

static void rd_add(FlipperIbuttonState *s, int op, int arg)
{
    s->rd_ops[s->rd_nops][0] = op;
    s->rd_ops[s->rd_nops][1] = arg;
    s->rd_nops++;
}

static void rd_finish(FlipperIbuttonState *s, const char *why);
static void rd_next_op(FlipperIbuttonState *s);

/* the slave dropped out: start over, or resume a memory read after a reset */
static void rd_retry(FlipperIbuttonState *s)
{
    int family = s->rd_buf[0], memlen = s->rd_len - 8;

    rd_line(s, false);
    if (s->rd_len > s->rd_retry_len) {  /* it got further: a fresh budget */
        s->rd_retry_len = s->rd_len;
        s->rd_stuck = 0;
    }
    if (++s->rd_stuck > RD_RETRIES) {
        rd_finish(s, s->rd_present ? "the key stopped answering" :
                     "no key answered (is the Flipper emulating one?)");
        return;
    }
    s->rd_retries++;
    s->rd_byte = s->rd_bit = s->rd_nops = s->rd_op = 0;   /* a partial byte: again */
    s->rd_skip = 0;
    rd_add(s, OP_RESET, 0);
    if (s->rd_len < 8 || s->rd_mem_total == 0) {
        s->rd_len = 0;                  /* the ROM again */
        rd_add(s, OP_WRITE, 0x33);
        rd_add(s, OP_READ, 8);
        s->rd_left = 8;
    } else {                            /* the rest of the memory, from a few */
        int addr = memlen - MIN(memlen, RD_OVERLAP);  /* bytes back: a check */

        s->rd_skip = memlen - addr;     /* that the address came through */
        rd_add(s, OP_WRITE, 0xCC);      /* SKIP ROM */
        rd_add(s, OP_WRITE, 0xF0);      /* READ MEMORY */
        rd_add(s, OP_WRITE, addr & 0xFF);
        if (family != 0x14) {
            rd_add(s, OP_WRITE, addr >> 8);
        }
        rd_add(s, OP_READ, s->rd_mem_total - addr);
        s->rd_left = s->rd_mem_total - addr;
    }
    rd_next_op(s);
}

/* the next operation of the program, from the ready state */
static void rd_next_op(FlipperIbuttonState *s)
{
    int64_t now = qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL);

    if (s->rd_op >= s->rd_nops) {
        rd_finish(s, NULL);
        return;
    }
    if (s->rd_ops[s->rd_op][0] == OP_RESET) {
        s->rd_op++;
        s->rd_state = RD_RESET;
        rd_line(s, true);
        timer_mod(s->rd_timer, now + RD_NO_ISR_NS);
        return;
    }
    s->rd_state = RD_READY;             /* a slot starts on a later IDR read */
    s->rd_reads = 0;
    timer_mod(s->rd_timer, now + RD_TIMEOUT_NS);
}

/* a slot is over: on to the next bit, byte or operation */
static void rd_bit_done(FlipperIbuttonState *s, int bit)
{
    int op = s->rd_ops[s->rd_op][0];

    if (op == OP_READ && bit) {
        s->rd_byte |= 1 << s->rd_bit;
    }
    if (++s->rd_bit == 8) {
        s->rd_bit = 0;
        if (op == OP_READ && s->rd_skip) {      /* read again after a resume */
            if (s->rd_byte != s->rd_buf[s->rd_len - s->rd_skip]) {
                /* the address was misheard, or the byte kept was wrong:
                 * resume before it, checking the bytes before that */
                s->rd_len -= s->rd_skip;
                rd_retry(s);
                return;
            }
            s->rd_skip--;
            s->rd_byte = 0;
            --s->rd_left;
            rd_next_op(s);
            return;
        }
        if (op == OP_READ) {
            s->rd_buf[s->rd_len++] = s->rd_byte;
            s->rd_byte = 0;
            if (s->rd_len == 8 && s->rd_op == 2) {      /* the ROM is in */
                static const uint16_t mem[256] = {
                    [0x08] = 128, [0x0C] = 8192, [0x14] = 32,
                };
                int n = mem[s->rd_buf[0]];

                if (n && s->rd_nops == 3) {             /* READ MEMORY from 0 */
                    s->rd_mem_total = n;
                    rd_add(s, OP_WRITE, 0xF0);
                    rd_add(s, OP_WRITE, 0x00);
                    if (s->rd_buf[0] != 0x14) {
                        rd_add(s, OP_WRITE, 0x00);      /* 2-byte address */
                    }
                    rd_add(s, OP_READ, n);
                }
            }
            if (--s->rd_left > 0) {
                rd_next_op(s);
                return;
            }
        }
        s->rd_op++;
        if (s->rd_op < s->rd_nops && s->rd_ops[s->rd_op][0] == OP_READ) {
            s->rd_left = s->rd_ops[s->rd_op][1];
        }
    }
    rd_next_op(s);
}

/* the slave read IDR: the reader's moves that wait on its polling */
static void rd_poll(FlipperIbuttonState *s, int64_t now)
{
    int op = s->rd_op < s->rd_nops ? s->rd_ops[s->rd_op][0] : -1;
    int arg = s->rd_op < s->rd_nops ? s->rd_ops[s->rd_op][1] : 0;

    switch (s->rd_state) {
    case RD_RESET:
        if (s->rd_pull) {               /* its EXTI ISR saw the reset begin */
            s->rd_state = RD_RESET_HOLD;
            timer_mod(s->rd_timer, now + RD_RESET_NS);
        }
        return;
    case RD_READY:
        if (++s->rd_reads >= 2) {       /* it is waiting for a slot */
            rd_line(s, true);
            s->rd_state = RD_DETECT;
        }
        return;
    case RD_DETECT:                     /* this read saw the slot begin */
        if (op == OP_WRITE && !((arg >> s->rd_bit) & 1)) {
            s->rd_t = -1;
            s->rd_state = RD_HOLD0;
            return;
        }
        rd_line(s, false);              /* a 1, or a read: let go now */
        if (op == OP_WRITE) {
            rd_bit_done(s, 1);
        } else {
            s->rd_fw_pulled = false;
            s->rd_state = RD_READBIT;
        }
        return;
    case RD_READBIT:
        /*
         * Sending a bit, the slave writes the pin and only waits on DWT
         * between: a read here is not that.  It missed the slot and waits
         * for one, or a host stall in a wait made it take a reset, whose
         * presence pulse would read as a 0 here.  It dropped out.
         */
        rd_retry(s);
        return;
    case RD_HOLD0:
        /*
         * The slave times its window from after the read that saw the slot;
         * a host stall there would shorten a hold timed from that read, and
         * a 0 would be heard as a 1.  From its first read in the window, a
         * hold is at least as long as the slave measures.
         */
        if (s->rd_t < 0) {
            s->rd_t = now;
        } else if (now - s->rd_t >= RD_HOLD0_NS) {
            rd_line(s, false);
            rd_bit_done(s, 0);
        }
        return;
    }
}

/* the slave wrote PB14 (low: it drives the line low) */
static void rd_fw(FlipperIbuttonState *s, bool low)
{
    switch (s->rd_state) {
    case RD_PRESENCE:
        if (low) {
            s->rd_fw_pulled = true;     /* its presence pulse */
        } else if (s->rd_fw_pulled) {
            s->rd_present = true;
            s->rd_op++;                 /* the reset is done */
            rd_next_op(s);
        }
        return;
    case RD_READBIT:
        if (low) {
            s->rd_fw_pulled = true;     /* it sends a 0 */
        } else {
            rd_bit_done(s, !s->rd_fw_pulled);
        }
        return;
    case RD_READY:
        s->rd_reads = 0;                /* it is busy: wait for its polling */
        return;
    }
}

static void rd_timer(void *opaque)
{
    FlipperIbuttonState *s = opaque;
    int64_t now = qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL);

    switch (s->rd_state) {
    case RD_RESET:                      /* no slave ISR: release anyway */
    case RD_RESET_HOLD:
        rd_line(s, false);
        s->rd_state = RD_PRESENCE;
        s->rd_fw_pulled = false;
        s->rd_op--;                     /* the reset op completes on presence */
        timer_mod(s->rd_timer, now + RD_TIMEOUT_NS);
        return;
    default:
        rd_retry(s);
        return;
    }
}

static const char *rd_family(uint8_t code)
{
    switch (code) {
    case 0x01: return "DS1990";
    case 0x08: return "DS1992";
    case 0x0C: return "DS1996";
    case 0x14: return "DS1971";
    default: return "DSGeneric";
    }
}

static void rd_finish(FlipperIbuttonState *s, const char *why)
{
    g_autoptr(GString) f = g_string_new(NULL);
    g_autoptr(GError) err = NULL;
    g_autofree char *path = NULL;
    const uint8_t *rom = s->rd_buf;
    int memlen = s->rd_len - 8;

    timer_del(s->rd_timer);
    rd_line(s, false);
    s->rd_state = RD_OFF;
    g_free(s->rd_prog);
    s->rd_prog = NULL;
    if (why || s->rd_len < 8) {
        warn_report("flipper-zero: iButton reader: %s",
                    why ?: "the ROM did not come through");
        if (s->rd_len < 8) {
            return;
        }
    }
    g_string_append_printf(f, "Filetype: Flipper iButton key\nVersion: 2\n"
                           "Protocol: %s\nRom Data:", rd_family(rom[0]));
    for (int i = 0; i < 8; i++) {
        g_string_append_printf(f, " %02X", rom[i]);
    }
    g_string_append_c(f, '\n');
    if (memlen > 0) {
        g_string_append_printf(f, "%s:", rom[0] == 0x14 ? "Eeprom Data" : "Sram Data");
        for (int i = 0; i < memlen; i++) {
            g_string_append_printf(f, " %02X", s->rd_buf[8 + i]);
        }
        g_string_append_c(f, '\n');
    }
    if (!s->tx_dir) {
        return;
    }
    g_mkdir_with_parents(s->tx_dir, 0755);
    for (int k = 1; k < 100000; k++) {
        g_free(path);
        path = g_strdup_printf("%s/read-%05d.ibtn", s->tx_dir, k);
        if (!g_file_test(path, G_FILE_TEST_EXISTS)) {
            break;
        }
    }
    if (g_file_set_contents(path, f->str, f->len, &err)) {
        g_autofree char *again = s->rd_retries ?
            g_strdup_printf(" (the key dropped out %d time%s; read on after a reset)",
                            s->rd_retries, s->rd_retries == 1 ? "" : "s") : g_strdup("");

        info_report("flipper-zero: iButton reader read the emulated key: %s "
                    "%02X%02X%02X%02X%02X%02X%02X%02X%s%s -> %s%s", rd_family(rom[0]),
                    rom[0], rom[1], rom[2], rom[3], rom[4], rom[5], rom[6], rom[7],
                    ow_crc8(rom, 7) == rom[7] ? "" : " (bad CRC)",
                    memlen > 0 ? " + memory" : "", path, again);
        g_free(s->tx_last);
        s->tx_last = g_steal_pointer(&path);
    } else {
        warn_report("flipper-zero: %s", err->message);
    }
}

/* --------------------------------- capturing an emulated Cyfral/Metakom */

#define CAP_IDLE_NS  (1000 * SCALE_MS)  /* no new level: the capture ends */
#define CAP_MAX      4096

static void cap_write(FlipperIbuttonState *s)
{
    g_autoptr(GString) f = g_string_new(NULL);
    g_autoptr(GError) err = NULL;
    g_autofree char *path = NULL;
    const int32_t *d = (const int32_t *)s->cap->data;
    guint n = s->cap->len;

    if (n >= 16 && s->tx_dir) {
        g_string_append(f, "Filetype: FlipperEmu iButton capture\nVersion: 1\n"
                        "# What an emulated Cyfral or Metakom key put on the iButton "
                        "pin: each level\n# the firmware set, +high/-low, for its "
                        "TIM1 period in us. ./flipperemu\n# ibutton FILE reads "
                        "it back.");
        for (guint i = 0; i < n; i++) {
            if (i % 32 == 0) {
                g_string_append(f, "\nWave:");
            }
            g_string_append_printf(f, " %+d", d[i]);
        }
        g_string_append_c(f, '\n');
        g_mkdir_with_parents(s->tx_dir, 0755);
        for (int k = 1; k < 100000; k++) {
            g_free(path);
            path = g_strdup_printf("%s/tx-%05d.txt", s->tx_dir, k);
            if (!g_file_test(path, G_FILE_TEST_EXISTS)) {
                break;
            }
        }
        if (g_file_set_contents(path, f->str, f->len, &err)) {
            info_report("flipper-zero: iButton emulation, %u levels -> %s", n, path);
            g_free(s->tx_last);
            s->tx_last = g_steal_pointer(&path);
        } else {
            warn_report("flipper-zero: %s", err->message);
        }
    }
}

/* the emulation stopped: write what was captured, unless it was written
 * full.  The idle timer is only a fallback: a host stall of the vCPU runs
 * the virtual clock on with no level set, and should not cut a capture. */
static void cap_timer(void *opaque)
{
    FlipperIbuttonState *s = opaque;

    timer_del(s->cap_timer);
    if (s->cap) {
        if (!s->cap_done) {
            cap_write(s);
        }
        g_array_free(s->cap, true);
        s->cap = NULL;
        s->cap_done = false;
    }
}

/* the firmware let go of PB14 (no longer an output): an emulation ended */
void flipper_ibutton_released(FlipperIbuttonState *s)
{
    if (s->cap) {
        cap_timer(s);
    }
}

/* the emulation set a level, held for ns (one TIM1 period) */
void flipper_ibutton_emulated(FlipperIbuttonState *s, bool high, int64_t ns)
{
    int32_t us = (ns + SCALE_US / 2) / SCALE_US;

    if (s->kind != IBTN_NONE || s->rd_state != RD_OFF) {
        return;                         /* a key on the contact, or reading one */
    }
    if (!s->cap) {
        s->cap = g_array_new(false, false, sizeof(int32_t));
    }
    if (!s->cap_done && us > 0) {
        g_array_append_val(s->cap, (int32_t){ high ? us : -us });
        if (s->cap->len == CAP_MAX) {   /* full: written now, though the key */
            cap_write(s);               /* emulates on (the rest repeats it) */
            s->cap_done = true;
        }
    }
    timer_mod(s->cap_timer, qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL) + CAP_IDLE_NS);
}

/* ------------------------------------------------- the board's hooks */

void flipper_ibutton_idr_read(FlipperIbuttonState *s, bool fw_released)
{
    if (s->rd_state != RD_OFF) {
        rd_poll(s, qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL));
    } else if (fw_released) {
        ow_sampled(s);                  /* a key's 0 or presence is read */
    }
}

void flipper_ibutton_fw_write(FlipperIbuttonState *s, bool low)
{
    if (s->rd_state != RD_OFF) {
        rd_fw(s, low);
    } else {
        ow_master(s, low);
    }
}

/* ------------------------------------------------- Cyfral and Metakom */

static void wave_step(void *opaque)
{
    FlipperIbuttonState *s = opaque;
    int32_t d;

    if (!s->playing || !s->wave_len) {
        return;
    }
    if (s->rfid && s->rfid->field_hz) {
        s->playing = false;             /* a 125 kHz read took COMP1 */
        return;
    }
    int64_t now = qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL);

    d = s->wave[s->wave_pos];
    s->wave_pos = (s->wave_pos + 1) % s->wave_len;
    if (now - s->next_t > WAVE_RESYNC_NS) {
        s->next_t = now;                /* fell behind: resync */
    }
    s->edge_t = s->next_t;              /* this edge, on the key's timeline */
    s->next_t += (int64_t)(d > 0 ? d : -d) * SCALE_US;
    s->awaiting = true;
    wb55_comp_input(s->comp, 0, d > 0, now);
    timer_mod(s->wave_timer, MAX(now, s->next_t) + WAVE_READ_MAX_NS);
}

/* the COMP ISR read COMP1: it reads CYCCNT next; the next edge may come */
void flipper_ibutton_comp_read(FlipperIbuttonState *s)
{
    int64_t now = qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL);

    if (!s->playing || !s->awaiting) {
        return;
    }
    s->awaiting = false;
    wb55_dwt_stamp_next(s->dwt, s->edge_t);
    timer_mod(s->wave_timer, MAX(now + WAVE_SPACING_NS, s->next_t));
}

/* COMP1 on with no 125 kHz field: the reader is the iButton one */
static void wave_update(FlipperIbuttonState *s)
{
    bool play = s->kind == IBTN_WAVE && s->comp_on &&
                !(s->rfid && s->rfid->field_hz);

    if (play == s->playing) {
        return;
    }
    s->playing = play;
    timer_del(s->wave_timer);
    if (play) {
        s->wave_pos = 0;
        s->awaiting = false;
        s->next_t = qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL) + WAVE_SETTLE_NS;
        timer_mod(s->wave_timer, s->next_t);
    }
}

void flipper_ibutton_comp(FlipperIbuttonState *s, bool on)
{
    s->comp_on = on;
    wave_update(s);
}

void flipper_ibutton_reset(FlipperIbuttonState *s)
{
    timer_del(s->timer);
    s->pulling = s->presence = s->od = s->m_low = false;
    ow_goto(s, OW_IDLE);
    ow_drive(s, false);                 /* the pull-up */
    flipper_ibutton_comp(s, false);     /* a reset cleared COMP1 */
}

/* ------------------------------------------------------------- the key */

static void ibtn_clear(FlipperIbuttonState *s)
{
    if (s->pulling) {
        s->pulling = false;
        ow_drive(s, false);
    }
    timer_del(s->timer);
    g_free(s->mem);
    s->mem = NULL;
    g_free(s->wave);
    s->wave = NULL;
    s->mem_len = s->wave_len = s->addr_bytes = 0;
    s->kind = IBTN_NONE;
    s->blank = BLANK_NONE;
    s->od = false;
    ow_goto(s, OW_IDLE);
}

static bool ibtn_hex(const char *text, GByteArray *out)
{
    g_auto(GStrv) parts = g_strsplit_set(text, " :,\t", -1);

    for (int i = 0; parts[i]; i++) {
        uint64_t v;

        if (!*parts[i]) {
            continue;
        }
        if (qemu_strtou64(parts[i], NULL, 16, &v) < 0 || v > 0xFF) {
            return false;
        }
        g_byte_array_append(out, (uint8_t[]){ v }, 1);
    }
    return true;
}

static void ibtn_set_key(Object *obj, const char *value, Error **errp)
{
    FlipperIbuttonState *s = FLIPPER_IBUTTON(obj);
    g_autofree char *contents = NULL;
    g_autoptr(GByteArray) rom = g_byte_array_new();
    g_autoptr(GByteArray) mem = g_byte_array_new();
    g_autoptr(GArray) wave = g_array_new(false, false, sizeof(int32_t));
    g_auto(GStrv) lines = NULL;
    int kind = IBTN_NONE, addr_bytes = 0, blank = BLANK_NONE;

    if (!strcmp(value, "none") || !*value) {
        ibtn_clear(s);
        g_free(s->key_str);
        s->key_str = NULL;
        wave_update(s);
        return;
    }
    if (!g_str_has_prefix(value, "file:")) {
        error_setg(errp, "ibutton key: file:PATH (from scripts/fe_ibutton.py) "
                   "or none");
        return;
    }
    if (!g_file_get_contents(value + 5, &contents, NULL, NULL)) {
        error_setg(errp, "ibutton key: cannot read %s", value + 5);
        return;
    }
    lines = g_strsplit(contents, "\n", -1);
    for (int i = 0; lines[i]; i++) {
        char *line = g_strstrip(lines[i]);
        char *arg = strchr(line, ' ');
        bool ok = true;

        if (!*line || *line == '#') {
            continue;
        }
        arg = arg ? g_strstrip(arg + 1) : (char *)"";
        if (g_str_has_prefix(line, "type ")) {
            kind = !strcmp(arg, "onewire") ? IBTN_ONEWIRE :
                   !strcmp(arg, "wave") ? IBTN_WAVE : -1;
            ok = kind >= 0;
        } else if (g_str_has_prefix(line, "rom ")) {
            ok = ibtn_hex(arg, rom);
        } else if (g_str_has_prefix(line, "mem ")) {
            ok = ibtn_hex(arg, mem);
        } else if (g_str_has_prefix(line, "blank ")) {
            blank = -1;
            for (int k = 1; k < ARRAY_SIZE(blank_names); k++) {
                if (!strcmp(arg, blank_names[k])) {
                    blank = k;
                }
            }
            ok = blank > 0;
        } else if (g_str_has_prefix(line, "addr ")) {
            addr_bytes = atoi(arg);
            ok = addr_bytes >= 0 && addr_bytes <= 2;
        } else if (g_str_has_prefix(line, "wave ")) {
            g_auto(GStrv) parts = g_strsplit_set(arg, " ,", -1);

            for (int k = 0; parts[k] && ok; k++) {
                int64_t v;

                if (!*parts[k]) {
                    continue;
                }
                ok = qemu_strtoi64(parts[k], NULL, 10, &v) == 0 && v &&
                     v >= -100000 && v <= 100000;
                if (ok) {
                    g_array_append_val(wave, (int32_t){ v });
                }
            }
        } else {
            ok = false;
        }
        if (!ok) {
            error_setg(errp, "ibutton key: line not understood: '%s'", line);
            return;
        }
    }
    if (kind == IBTN_ONEWIRE && rom->len != 8) {
        error_setg(errp, "ibutton key: a 1-Wire key needs an 8-byte rom");
        return;
    }
    if (kind == IBTN_WAVE && wave->len < 2) {
        error_setg(errp, "ibutton key: a wave key needs a wave");
        return;
    }
    if (kind == IBTN_NONE) {
        error_setg(errp, "ibutton key: no type");
        return;
    }
    ibtn_clear(s);
    s->kind = kind;
    if (kind == IBTN_ONEWIRE) {
        memcpy(s->rom, rom->data, 8);
        s->mem_len = mem->len;
        s->mem = g_byte_array_free(g_steal_pointer(&mem), false);
        s->addr_bytes = addr_bytes;
        s->blank = blank;
        s->wr_enabled = false;
    } else {
        s->wave_len = wave->len;
        s->wave = (int32_t *)g_array_free(g_steal_pointer(&wave), false);
    }
    g_free(s->key_str);
    s->key_str = g_strdup(value);
    s->playing = false;
    wave_update(s);
}

static char *ibtn_get_key(Object *obj, Error **errp)
{
    return g_strdup(FLIPPER_IBUTTON(obj)->key_str ?: "");
}

static char *ibtn_get_reader(Object *obj, Error **errp)
{
    return g_strdup(FLIPPER_IBUTTON(obj)->rd_prog ?: "");
}

static void ibtn_set_reader(Object *obj, const char *value, Error **errp)
{
    FlipperIbuttonState *s = FLIPPER_IBUTTON(obj);

    if (strcmp(value, "read")) {
        error_setg(errp, "ibutton reader: read (the ROM, and a memory key's "
                   "memory, of the key the Flipper emulates)");
        return;
    }
    if (s->kind != IBTN_NONE) {
        error_setg(errp, "ibutton reader: take the key off the contact first");
        return;
    }
    timer_del(s->rd_timer);
    g_free(s->rd_prog);
    s->rd_prog = g_strdup(value);
    s->rd_nops = s->rd_op = s->rd_len = s->rd_bit = 0;
    s->rd_retries = s->rd_stuck = s->rd_retry_len = s->rd_mem_total = 0;
    s->rd_skip = s->rd_byte = 0;
    s->rd_present = false;
    memset(s->rd_buf, 0, sizeof(s->rd_buf));
    rd_add(s, OP_RESET, 0);
    rd_add(s, OP_WRITE, 0x33);          /* READ ROM */
    rd_add(s, OP_READ, 8);
    s->rd_left = 8;
    rd_next_op(s);
}

static char *ibtn_get_tx(Object *obj, Error **errp)
{
    return g_strdup(FLIPPER_IBUTTON(obj)->tx_last ?: "");
}

static char *ibtn_get_rom(Object *obj, Error **errp)
{
    FlipperIbuttonState *s = FLIPPER_IBUTTON(obj);
    const uint8_t *r = s->rom;

    if (s->kind != IBTN_ONEWIRE) {
        return g_strdup("");
    }
    return g_strdup_printf("%02X %02X %02X %02X %02X %02X %02X %02X",
                           r[0], r[1], r[2], r[3], r[4], r[5], r[6], r[7]);
}

static void ibtn_init(Object *obj)
{
    FlipperIbuttonState *s = FLIPPER_IBUTTON(obj);

    s->timer = timer_new_ns(QEMU_CLOCK_VIRTUAL, ibtn_timer, s);
    s->wave_timer = timer_new_ns(QEMU_CLOCK_VIRTUAL, wave_step, s);
    s->rd_timer = timer_new_ns(QEMU_CLOCK_VIRTUAL, rd_timer, s);
    s->cap_timer = timer_new_ns(QEMU_CLOCK_VIRTUAL, cap_timer, s);
    qdev_init_gpio_out_named(DEVICE(obj), &s->pull, "pull", 1);
    object_property_add_str(obj, "key", ibtn_get_key, ibtn_set_key);
    object_property_set_description(obj, "key",
        "The key on the iButton contact: file:PATH (scripts/fe_ibutton.py) "
        "or none");
    object_property_add_str(obj, "reader", ibtn_get_reader, ibtn_set_reader);
    object_property_set_description(obj, "reader",
        "Read the key the Flipper emulates: read (a virtual 1-Wire master)");
    object_property_add_str(obj, "tx", ibtn_get_tx, NULL);
    object_property_set_description(obj, "tx",
        "The latest emulated key read, or Cyfral/Metakom capture");
    object_property_add_str(obj, "rom", ibtn_get_rom, NULL);
    object_property_set_description(obj, "rom",
        "A 1-Wire key's ROM now (a blank's, once written)");
}

static const Property ibtn_props[] = {
    DEFINE_PROP_STRING("tx-dir", FlipperIbuttonState, tx_dir),
};

static void ibtn_class_init(ObjectClass *klass, const void *data)
{
    DeviceClass *dc = DEVICE_CLASS(klass);

    dc->user_creatable = false;
    device_class_set_props(dc, ibtn_props);
}

static const TypeInfo ibtn_types[] = {
    {
        .name = TYPE_FLIPPER_IBUTTON,
        .parent = TYPE_DEVICE,
        .instance_size = sizeof(FlipperIbuttonState),
        .instance_init = ibtn_init,
        .class_init = ibtn_class_init,
    },
};
DEFINE_TYPES(ibtn_types)
