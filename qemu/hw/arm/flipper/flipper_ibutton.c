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

void flipper_ibutton_sampled(FlipperIbuttonState *s)
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

void flipper_ibutton_master(FlipperIbuttonState *s, bool low)
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
    flipper_ibutton_sampled(opaque);    /* never sampled: let go anyway */
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
    qdev_init_gpio_out_named(DEVICE(obj), &s->pull, "pull", 1);
    object_property_add_str(obj, "key", ibtn_get_key, ibtn_set_key);
    object_property_set_description(obj, "key",
        "The key on the iButton contact: file:PATH (scripts/fe_ibutton.py) "
        "or none");
    object_property_add_str(obj, "rom", ibtn_get_rom, NULL);
    object_property_set_description(obj, "rom",
        "A 1-Wire key's ROM now (a blank's, once written)");
}

static void ibtn_class_init(ObjectClass *klass, const void *data)
{
    DeviceClass *dc = DEVICE_CLASS(klass);

    dc->user_creatable = false;
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
