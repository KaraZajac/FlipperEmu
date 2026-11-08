/*
 * T5577 (ATA5577C): a writable 125 kHz tag for the virtual RFID field
 *
 * Downlink: a reader writes by switching its field off for short gaps.  A
 * start gap opens a command after the tag has been powered for a while;
 * each stretch of field between two gaps is one bit (16-31 field clocks a
 * 0, 48-63 a 1; a field clock, Tc, is 8 us at 125 kHz); a long stretch
 * closes the command.  A command is an opcode (10 page 0, 11 page 1, 00
 * reset), then for a write [a 32-bit password,] the lock bit, 32 data bits
 * and a 3-bit block address, all MSB first: what lib/lfrfid/tools/t5577.c
 * sends.  The windows here are wide, because an emulated reader's gaps run
 * long when the host is busy.  An EM4305 command (the firmware sends one
 * after the T5577 write) starts with a 0 bit, so here it ends up an opcode
 * 0x and is ignored, as on a real T5577.
 *
 * Uplink: in regular read mode the tag sends blocks 1..MAXBLK of page 0 in
 * a loop (block 0 alone if MAXBLK is 0), modulated as block 0 says:
 * Manchester, bi-phase, differential bi-phase, direct, FSK1/FSK2(a) or
 * PSK1/2/3, at RF/8..RF/128, or RF/(2n+2) in extended mode.  The waveforms
 * follow the firmware's own encoders, whose conventions its T5577 write
 * requests rely on: Manchester sends a bit's value then its inverse; FSK
 * sends whole subcarrier cycles with the phase carried over (fsk_osc);
 * PSK1 is an RF/2 subcarrier whose phase flips where the data changes.  Not
 * modelled: the sequence terminator (the firmware's decoders ignore it),
 * AOR, the POR delay, direct access reads, test mode and page 1's
 * analogue settings.
 *
 * Copyright (c) 2026, Kara Zajac
 * SPDX-License-Identifier: BSD-3-Clause
 */
#include "qemu/osdep.h"
#include "qemu/error-report.h"
#include "flipper.h"

#define T5_GAP_MIN      4       /* Tc: a shorter dip is no gap */
#define T5_GAP_MAX      128     /* Tc: a longer one powers the tag down */
#define T5_POWERED      100     /* Tc of field before a start gap */
#define T5_BIT_1        40      /* Tc: shorter, a 0 (nominal 24); longer, a 1 (56) */
#define T5_CLOSE        200     /* Tc: longer, the command is over */
#define T5_MAX_CYCLES   (1 << 20)

#define CFG_PWD         0x00000010
#define CFG_X_MODE      0x00020000

void t5577_blank(T5577 *t)
{
    memset(t, 0, sizeof(*t));
    t->blk[0] = T5577_DEFAULT_CONFIG;
}

static uint32_t t5_word(const uint8_t *b)
{
    uint32_t v = 0;

    for (int i = 0; i < 32; i++) {
        v = v << 1 | b[i];
    }
    return v;
}

/* a whole command is in: act on it; true when page 0 changed */
static bool t5_command(T5577 *t)
{
    const uint8_t *b = t->bits;
    int n = t->nbits, i = 2, page, addr;
    bool pwd_mode = t->blk[0] & CFG_PWD, lock;
    uint32_t data;

    if (n < 2 || !b[0]) {
        return false;                   /* 00 reset, 01 test mode */
    }
    page = b[1];
    if (n == 70) {                      /* with the password */
        if (pwd_mode && t5_word(b + 2) != t->blk[7]) {
            info_report("flipper-zero: T5577 write refused: wrong password");
            return false;
        }
        i = 34;
    } else if (n != 38) {
        return false;                   /* a read of a page, or not modelled */
    } else if (pwd_mode) {
        info_report("flipper-zero: T5577 write refused: the tag wants a password");
        return false;
    }
    lock = b[i];
    data = t5_word(b + i + 1);
    addr = b[i + 33] << 2 | b[i + 34] << 1 | b[i + 35];
    if (page) {
        if (addr == 3) {                /* 1 and 2 are traceability data */
            t->p1[3] = data;
            info_report("flipper-zero: T5577 page 1 block 3 <- %08X", data);
        }
        return false;
    }
    if (t->lock & (1 << addr)) {
        info_report("flipper-zero: T5577 write refused: block %d is locked", addr);
        return false;
    }
    t->blk[addr] = data;
    if (lock) {
        t->lock |= 1 << addr;
    }
    info_report("flipper-zero: T5577 block %d <- %08X%s", addr, data,
                lock ? " (and locked)" : "");
    return true;
}

bool t5577_field(T5577 *t, bool on, int64_t now, int64_t tc_ns)
{
    int64_t d;
    bool changed = false;

    if (on == t->on) {
        return false;
    }
    d = (now - t->since) / tc_ns;       /* Tc the field held its last state */
    t->on = on;
    if (!on) {                          /* a gap opens: the field was on for d */
        t->on_start = t->since;
        t->pushed = false;
        t->since = now;
        if (t->cmd) {
            if (d > T5_CLOSE) {
                changed = t5_command(t);
                t->cmd = false;
            } else if (t->nbits < (int)sizeof(t->bits)) {
                t->bits[t->nbits++] = d >= T5_BIT_1;
                t->pushed = true;
            } else {
                t->cmd = false;         /* too long for any command */
            }
        }
        t->prev_on = d;
        return changed;
    }
    if (d < T5_GAP_MIN) {               /* a dip the tag does not notice */
        if (t->pushed) {
            t->nbits--;
        }
        t->since = t->on_start;         /* the field was on all along */
        return false;
    }
    t->since = now;
    if (d > T5_GAP_MAX) {               /* unpowered: start over */
        t->cmd = false;
        return false;
    }
    if (!t->cmd && t->prev_on >= T5_POWERED) {
        t->cmd = true;                  /* a start gap */
        t->nbits = 0;
    }
    return false;
}

int64_t t5577_deadline(const T5577 *t, int64_t tc_ns)
{
    return t->cmd && t->on ? t->since + (T5_CLOSE + 1) * tc_ns : -1;
}

bool t5577_poll(T5577 *t, int64_t now, int64_t tc_ns)
{
    bool changed;

    if (!t->cmd || !t->on || (now - t->since) / tc_ns <= T5_CLOSE) {
        return false;
    }
    changed = t5_command(t);
    t->cmd = false;
    t->prev_on = (now - t->since) / tc_ns;
    return changed;
}

/* ------------------------------------------------------------- uplink */

typedef struct {
    uint8_t *lv;                        /* the level at each field clock */
    size_t n, cap;
    bool full;
} T5Cycles;

static void t5_add(T5Cycles *c, int level, int n)
{
    if (c->n + n > T5_MAX_CYCLES) {
        c->full = true;
        return;
    }
    if (c->n + n > c->cap) {
        c->cap = MAX(c->cap * 2, c->n + n);
        c->lv = g_realloc(c->lv, c->cap);
    }
    memset(c->lv + c->n, level, n);
    c->n += n;
}

static void t5_manchester(T5Cycles *c, const uint8_t *b, int nb, int rate)
{
    for (int i = 0; i < nb; i++) {
        t5_add(c, b[i], rate / 2);
        t5_add(c, !b[i], rate - rate / 2);
    }
}

static void t5_direct(T5Cycles *c, const uint8_t *b, int nb, int rate)
{
    for (int i = 0; i < nb; i++) {
        t5_add(c, b[i], rate);
    }
}

/* every pulse flips the level; a bit equal to dbl is two half pulses */
static void t5_biphase(T5Cycles *c, const uint8_t *b, int nb, int rate, int dbl)
{
    int level = 0;

    for (int pass = 0; pass < 2; pass++) {
        for (int i = 0; i < nb; i++) {
            if (b[i] == dbl) {
                level ^= 1;
                t5_add(c, level, rate / 2);
                level ^= 1;
                t5_add(c, level, rate - rate / 2);
            } else {
                level ^= 1;
                t5_add(c, level, rate);
            }
        }
        if (!level) {
            break;                      /* back where it started: it loops */
        }
    }
}

/* kind 1: the phase flips where the data changes; 2: at each 1; 3: at 0->1 */
static void t5_psk(T5Cycles *c, const uint8_t *b, int nb, int rate, int cf,
                   int kind)
{
    bool pol = true;
    int prev = b[nb - 1];

    for (int pass = 0; pass < 2; pass++) {
        for (int i = 0; i < nb; i++) {
            bool flip = kind == 1 ? b[i] != prev : kind == 2 ? b[i] : !prev && b[i];

            pol ^= flip;
            prev = b[i];
            for (int k = 0; k < rate / cf; k++) {
                t5_add(c, pol, cf / 2);
                t5_add(c, !pol, cf - cf / 2);
            }
        }
        if (pol) {
            break;
        }
    }
}

/* fsk_osc: whole cycles of f0 (a 0) or f1 (a 1) field clocks; a bit lasts
 * until the cycles add up past the bit rate, and the excess carries over.
 * The loop is complete once a pass over the data starts at a phase seen
 * before. */
static void t5_fsk(T5Cycles *c, const uint8_t *b, int nb, int rate, int f0, int f1)
{
    size_t start[256];
    int phase = 0;

    for (int i = 0; i < ARRAY_SIZE(start); i++) {
        start[i] = SIZE_MAX;
    }
    while (!c->full) {
        if (start[phase] != SIZE_MAX) {     /* the loop: from there to here */
            memmove(c->lv, c->lv + start[phase], c->n - start[phase]);
            c->n -= start[phase];
            return;
        }
        start[phase] = c->n;
        for (int i = 0; i < nb; i++) {
            for (;;) {
                int p = b[i] ? f1 : f0;

                phase += p;
                t5_add(c, 1, p / 2);
                t5_add(c, 0, p - p / 2);
                if (phase > rate) {
                    phase -= rate;
                    break;
                }
            }
        }
    }
}

static int t5577_rate(uint32_t cfg)
{
    static const int basic[8] = { 8, 16, 32, 40, 50, 64, 100, 128 };
    int mk = cfg >> 28;

    if ((cfg & CFG_X_MODE) && (mk == 6 || mk == 9)) {
        return 2 * ((cfg >> 18) & 0x3F) + 2;
    }
    return basic[(cfg >> 18) & 7];
}

int t5577_frame(const T5577 *t, int32_t *dur, int max, bool *psk)
{
    uint32_t cfg = t->blk[0];
    int mod = (cfg >> 12) & 0x1F, rate = t5577_rate(cfg);
    int maxblk = (cfg >> 5) & 7, cf = (int[]){ 2, 4, 8, 2 }[(cfg >> 10) & 3];
    int first = maxblk ? 1 : 0, last = maxblk, nb = 0, n = 0;
    uint8_t bits[8 * 32];
    T5Cycles c = { 0 };
    size_t step;

    for (int blk = first; blk <= last; blk++) {
        for (int i = 31; i >= 0; i--) {
            bits[nb++] = (t->blk[blk] >> i) & 1;
        }
    }
    *psk = mod >= 1 && mod <= 3;
    switch (mod) {
    case 0x00:
        t5_direct(&c, bits, nb, rate);
        break;
    case 0x01: case 0x02: case 0x03:
        t5_psk(&c, bits, nb, rate, cf, mod);
        break;
    case 0x04:                          /* FSK1:  1 = RF/8,  0 = RF/5 */
        t5_fsk(&c, bits, nb, rate, 5, 8);
        break;
    case 0x05:                          /* FSK2:  1 = RF/8,  0 = RF/10 */
        t5_fsk(&c, bits, nb, rate, 10, 8);
        break;
    case 0x06:                          /* FSK1a: 1 = RF/5,  0 = RF/8 */
        t5_fsk(&c, bits, nb, rate, 8, 5);
        break;
    case 0x07:                          /* FSK2a: 1 = RF/10, 0 = RF/8 (HID) */
        t5_fsk(&c, bits, nb, rate, 8, 10);
        break;
    case 0x08:
        t5_manchester(&c, bits, nb, rate);
        break;
    case 0x10:                          /* bi-phase: a 1 has the mid-bit change */
        t5_biphase(&c, bits, nb, rate, 1);
        break;
    case 0x18:                          /* differential bi-phase: a 0 has it */
        t5_biphase(&c, bits, nb, rate, 0);
        break;
    default:
        g_free(c.lv);
        return 0;                       /* reserved: silent */
    }
    if (c.full) {
        g_free(c.lv);
        return -1;
    }
    /* as the comparator sees it: a PSK subcarrier only as its phase */
    step = *psk ? 2 : 1;
    for (size_t i = 0; i < c.n; i += step) {
        bool high = c.lv[i];
        int32_t us = 8 * step;

        if (n && (dur[n - 1] > 0) == high) {
            dur[n - 1] += high ? us : -us;
        } else if (n == max) {
            g_free(c.lv);
            return -1;
        } else {
            dur[n++] = high ? us : -us;
        }
    }
    if (n > 1 && (dur[0] > 0) == (dur[n - 1] > 0)) {
        dur[0] += dur[--n];             /* the frame loops: join its ends */
    }
    g_free(c.lv);
    return n;
}
