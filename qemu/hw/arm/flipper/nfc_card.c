/*
 * Virtual NFC cards for the ST25R3916 model: ISO14443-3A and what runs on it
 *
 * A card sees the reader's frames as bits on the air (least significant bit
 * first, an odd parity bit after each full byte, CRC_A where the protocol
 * has one) and answers the same way, so a protocol that bends the rules
 * (MIFARE Classic encrypts its parity bits) can be modelled exactly.
 *
 * ISO14443-3A: power-up in the field, REQA/WUPA (ATQA), anticollision and
 * select over cascade levels 1-3 (4, 7 or 10-byte UIDs), HLTA, and the
 * IDLE/READY/ACTIVE/HALT states.  Then Type 2 (NTAG21x, MIFARE Ultralight
 * and Ultralight EV1): READ, FAST_READ, WRITE, COMPATIBILITY_WRITE,
 * GET_VERSION, READ_SIG, READ_CNT, INCR_CNT, CHECK_TEARING, PWD_AUTH with
 * AUTH0/PROT from the configuration pages; anything else is NAKed, which
 * sends the card back to IDLE, as on silicon.  And MIFARE Classic Mini, 1K
 * and 4K: Crypto1 (from its published description), the nonce PRNG, plain
 * and nested AUTH, encrypted READ/WRITE/HALT, the sector trailers' access
 * bits.
 *
 * The description (scripts/fe_nfc.py writes it from a Flipper .nfc file) is
 * one "key value" per line:
 *   type t2                 uid 04 51 5C FA 6F 73 81     atqa 44 00
 *   sak 00                  version 00 04 04 02 01 00 11 03
 *   signature <32 bytes>    counter0 0 ... tearing0 BD ...
 *   pwd FF FF FF FF         pack 00 00
 *   mem <bytes>             (pages in order; may repeat)
 * or, for MIFARE Classic, "type mfc" and the blocks in mem (16 bytes each).
 *
 * Copyright (c) 2026, Kara Zajac
 * SPDX-License-Identifier: BSD-3-Clause
 */
#include "qemu/osdep.h"
#include "qemu/cutils.h"
#include "qemu/timer.h"
#include "qemu/bitops.h"
#include "qemu/host-utils.h"
#include "qapi/error.h"
#include "flipper.h"

#define MEM_MAX 4096                    /* NTAG216: 231 pages; 4K Classic */

enum { ST_OFF, ST_IDLE, ST_READY, ST_ACTIVE, ST_HALT };
enum { KIND_T2 = 1, KIND_MFC };
enum { MFC_NONE, MFC_AUTH, MFC_ON };    /* Crypto1 session */

struct NfcCard {
    int kind;
    uint8_t uid[10];
    int uid_len;
    uint8_t atqa[2];                    /* in transmission order */
    uint8_t sak;
    int state, level;                   /* ISO14443-3A, cascade level 0..2 */
    /* Type 2 */
    uint8_t mem[MEM_MAX];
    int mem_len;
    bool has_version, has_sig;
    uint8_t version[8], sig[32];
    uint32_t counter[3];
    uint8_t tearing[3];
    uint8_t pwd[4], pack[2];
    bool authed;
    int comp_write;                     /* page awaiting COMPATIBILITY_WRITE */
    /* MIFARE Classic: mem holds the blocks */
    int mfc;                            /* MFC_*: where the session is */
    uint64_t lfsr;                      /* Crypto1 state, bit i = x_i */
    uint8_t nt[4];                      /* the nonce in flight, as sent */
    int sector;                         /* authenticated sector, and key */
    bool key_b;
    int write_block;                    /* block awaiting WRITE data, -1 */
    int64_t power_ns;                   /* field on: the nonce PRNG runs */
};

/* ------------------------------------------------------------ answers */

/* bytes on the air (with parity), optionally followed by CRC_A */
static int ans(uint8_t *out, const uint8_t *data, int n, bool crc)
{
    uint8_t buf[300];

    memcpy(buf, data, n);
    if (crc) {
        uint16_t c = nfc_crc_a(buf, n);

        buf[n++] = c & 0xFF;
        buf[n++] = c >> 8;
    }
    return nfc_air_encode(buf, n * 8, out);
}

/* a 4-bit ACK (0xA) or NAK (0x0): no parity */
static int ans4(uint8_t *out, uint8_t v)
{
    return nfc_air_encode(&v, 4, out);
}

/* ------------------------------------------------------------ Type 2 */

static int t2_pages(NfcCard *c)
{
    return c->mem_len / 4;
}

/* the first configuration page (EV1, NTAG21x), or -1 */
static int t2_cfg(NfcCard *c)
{
    return c->has_version && t2_pages(c) >= 8 ? t2_pages(c) - 4 : -1;
}

/* may this page be read without PWD_AUTH? */
static bool t2_readable(NfcCard *c, int page)
{
    int cfg = t2_cfg(c);

    if (cfg < 0 || c->authed) {
        return true;
    }
    /* AUTH0 = CFG0[3], PROT = ACCESS (CFG1[0]) bit 7: reads protected too */
    return page < c->mem[cfg * 4 + 3] || !(c->mem[(cfg + 1) * 4] & 0x80);
}

static bool t2_writable(NfcCard *c, int page)
{
    int cfg = t2_cfg(c);

    return page >= 2 && page < t2_pages(c) &&
           (cfg < 0 || c->authed || page < c->mem[cfg * 4 + 3]);
}

static void t2_write(NfcCard *c, int page, const uint8_t *d)
{
    uint8_t *p = &c->mem[page * 4];

    if (page == 2) {                    /* lock bytes, then OR-only */
        p[2] |= d[2];
        p[3] |= d[3];
    } else if (page == 3) {             /* capability container: OTP */
        for (int i = 0; i < 4; i++) {
            p[i] |= d[i];
        }
    } else {
        memcpy(p, d, 4);
    }
}

static int t2_command(NfcCard *c, const uint8_t *f, int n, uint8_t *out)
{
    uint8_t buf[256];
    int pages = t2_pages(c);

    if (c->comp_write >= 0) {           /* COMPATIBILITY_WRITE, second part */
        int page = c->comp_write;

        c->comp_write = -1;
        if (n == 18) {
            t2_write(c, page, f);
            return ans4(out, 0xA);
        }
        return ans4(out, 0x0);
    }
    switch (f[0]) {
    case 0x30:                          /* READ: 4 pages, rolling over */
        if (n != 4 || f[1] >= pages || !t2_readable(c, f[1])) {
            break;
        }
        for (int i = 0; i < 16; i++) {
            int page = (f[1] + i / 4) % pages;

            buf[i] = t2_readable(c, page) ? c->mem[page * 4 + i % 4] : 0;
        }
        return ans(out, buf, 16, true);
    case 0x3A:                          /* FAST_READ start..end */
        if (n != 5 || f[1] > f[2] || f[2] >= pages || (f[2] - f[1] + 1) * 4 > 240) {
            break;
        }
        for (int p = f[1]; p <= f[2]; p++) {
            if (!t2_readable(c, p)) {
                return ans4(out, 0x0);
            }
            memcpy(&buf[(p - f[1]) * 4], &c->mem[p * 4], 4);
        }
        return ans(out, buf, (f[2] - f[1] + 1) * 4, true);
    case 0xA2:                          /* WRITE */
        if (n != 8 || !t2_writable(c, f[1])) {
            break;
        }
        t2_write(c, f[1], &f[2]);
        return ans4(out, 0xA);
    case 0xA0:                          /* COMPATIBILITY_WRITE */
        if (n != 4 || !t2_writable(c, f[1])) {
            break;
        }
        c->comp_write = f[1];
        return ans4(out, 0xA);
    case 0x60:                          /* GET_VERSION */
        if (n != 3 || !c->has_version) {
            break;
        }
        return ans(out, c->version, 8, true);
    case 0x3C:                          /* READ_SIG: EV1 and NTAG21x have one */
        if (n != 4 || !(c->has_sig || c->has_version)) {
            break;
        }
        return ans(out, c->sig, 32, true);
    case 0x39:                          /* READ_CNT */
        if (n != 4 || f[1] > 2 || !c->has_version) {
            break;
        }
        buf[0] = c->counter[f[1]];
        buf[1] = c->counter[f[1]] >> 8;
        buf[2] = c->counter[f[1]] >> 16;
        return ans(out, buf, 3, true);
    case 0xA5:                          /* INCR_CNT */
        if (n != 8 || f[1] > 2 || !c->has_version) {
            break;
        }
        c->counter[f[1]] = MIN(c->counter[f[1]] + (f[2] | f[3] << 8 | f[4] << 16),
                               0xFFFFFFu);
        return ans4(out, 0xA);
    case 0x3E:                          /* CHECK_TEARING */
        if (n != 4 || f[1] > 2 || !c->has_version) {
            break;
        }
        return ans(out, &c->tearing[f[1]], 1, true);
    case 0x1B:                          /* PWD_AUTH */
        if (n != 7 || t2_cfg(c) < 0 || memcmp(&f[1], c->pwd, 4)) {
            break;
        }
        c->authed = true;
        return ans(out, c->pack, 2, true);
    }
    c->state = ST_IDLE;                 /* NAK: back to IDLE */
    return ans4(out, 0x0);
}

static void card_reset(NfcCard *c, int state);

/* ------------------------------------------------------- MIFARE Classic */

/*
 * Crypto1 from its published description (F. Garcia et al., "Dismantling
 * MIFARE Classic", ESORICS 2008): a 48-bit LFSR x0..x47 (bit i of a word),
 * shifted towards x0 with the feedback L entering at x47, and a filter f
 * over the odd bits x9..x47 giving one keystream bit per clock.  Keys,
 * nonces and data go in in transmission order, least significant bit of
 * each byte first.
 */
#define C1_TAPS (BIT_ULL(0) | BIT_ULL(5) | BIT_ULL(9) | BIT_ULL(10) | BIT_ULL(12) | \
                 BIT_ULL(14) | BIT_ULL(15) | BIT_ULL(17) | BIT_ULL(19) |           \
                 BIT_ULL(24) | BIT_ULL(25) | BIT_ULL(27) | BIT_ULL(29) |           \
                 BIT_ULL(35) | BIT_ULL(39) | BIT_ULL(41) | BIT_ULL(42) | BIT_ULL(43))

static int c1_fa(int y0, int y1, int y2, int y3)
{
    return ((y0 | y1) ^ (y0 & y3)) ^ (y2 & ((y0 ^ y1) | y3));
}

static int c1_fb(int y0, int y1, int y2, int y3)
{
    return ((y0 & y1) | y2) ^ ((y0 ^ y1) & (y2 | y3));
}

static int c1_fc(int y0, int y1, int y2, int y3, int y4)
{
    return (y0 | ((y1 | y4) & (y3 ^ y4))) ^ ((y0 ^ (y1 & y3)) & ((y2 ^ y3) | (y1 & y4)));
}

/* the keystream bit the current state gives */
static int c1_ks(uint64_t x)
{
#define X(i) ((int)((x >> (i)) & 1))
    return c1_fc(c1_fa(X(9), X(11), X(13), X(15)), c1_fb(X(17), X(19), X(21), X(23)),
                 c1_fb(X(25), X(27), X(29), X(31)), c1_fa(X(33), X(35), X(37), X(39)),
                 c1_fb(X(41), X(43), X(45), X(47)));
#undef X
}

/* one clock with an input bit (XORed into the feedback) */
static void c1_clock(uint64_t *x, int in)
{
    int fb = (ctpop64(*x & C1_TAPS) & 1) ^ in;

    *x = (*x >> 1) | ((uint64_t)fb << 47);
}

static uint64_t c1_init(const uint8_t key[6])
{
    uint64_t x = 0;

    for (int i = 0; i < 48; i++) {
        x |= (uint64_t)((key[i >> 3] >> (i & 7)) & 1) << i;
    }
    return x;
}

/* the PRNG behind the nonces, on bits in transmission order (LE words):
 * x^16 + x^14 + x^13 + x^11 + 1, so nonces are "weak" as on silicon.  On a
 * 32-bit nonce it gives the successor that authentication uses. */
static uint32_t prng_next(uint32_t x, int n)
{
    while (n--) {
        x = x >> 1 | (((x >> 16) ^ (x >> 18) ^ (x >> 19) ^ (x >> 21)) & 1) << 31;
    }
    return x;
}

static uint16_t prng16(uint16_t x, int n)
{
    while (n--) {
        x = x >> 1 | ((x ^ x >> 2 ^ x >> 3 ^ x >> 5) & 1) << 15;
    }
    return x;
}

static uint32_t le32_of(const uint8_t *b)
{
    return b[0] | b[1] << 8 | b[2] << 16 | (uint32_t)b[3] << 24;
}

/* a fresh nonce: the 16-bit LFSR has run one step per bit time (128/fc)
 * since the field came on, from a fixed state */
static void mfc_nonce(NfcCard *c, uint8_t nt[4])
{
    int64_t steps = (qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL) - c->power_ns) / 9439;
    uint16_t s = prng16(0x0F01, (int)(steps % 65535));
    uint32_t x = s | (uint32_t)prng16(s, 16) << 16;

    for (int i = 0; i < 4; i++) {
        nt[i] = x >> (8 * i);
    }
}

static int mfc_blocks(NfcCard *c)
{
    return c->mem_len / 16;
}

static int mfc_sector(int block)
{
    return block < 128 ? block / 4 : 32 + (block - 128) / 16;
}

static int mfc_trailer(int sector)
{
    return sector < 32 ? sector * 4 + 3 : 128 + (sector - 32) * 16 + 15;
}

/* the access conditions C1C2C3 (as 4*C1 + 2*C2 + C3) of a block's group */
static int mfc_access(NfcCard *c, int block)
{
    int sector = mfc_sector(block);
    const uint8_t *t = &c->mem[mfc_trailer(sector) * 16];
    int group = sector < 32 ? block % 4 : MIN((block - 128) % 16 / 5, 3);

    if (block == mfc_trailer(sector)) {
        group = 3;
    }
    return ((t[7] >> (4 + group)) & 1) << 2 | ((t[8] >> group) & 1) << 1 |
           ((t[8] >> (4 + group)) & 1);
}

/* MF1S50 access tables: may the session's key read / write this block? */
static bool mfc_may_read(NfcCard *c, int block)
{
    int acc = mfc_access(c, block);

    if (block == mfc_trailer(mfc_sector(block))) {
        return true;                    /* access bits always; keys masked */
    }
    return acc == 7 ? false : (acc == 3 || acc == 5) ? c->key_b : true;
}

static bool mfc_may_write(NfcCard *c, int block)
{
    static const int8_t data_w[8] = { 3, 0, 0, 2, 0, 0, 2, 0 }; /* 1 A, 2 B, 3 A|B */
    int acc = mfc_access(c, block);

    if (block == mfc_trailer(mfc_sector(block))) {
        return true;                    /* field by field in mfc_write() */
    }
    return (data_w[acc] & (c->key_b ? 2 : 1)) != 0;
}

/* a block as the session reads it: a trailer's keys come back masked */
static void mfc_read(NfcCard *c, int block, uint8_t *d)
{
    memcpy(d, &c->mem[block * 16], 16);
    if (block == mfc_trailer(mfc_sector(block))) {
        int acc = mfc_access(c, block);

        memset(d, 0, 6);                /* key A is never readable */
        if (!(!c->key_b && (acc == 0 || acc == 2 || acc == 1))) {
            memset(d + 10, 0, 6);       /* key B, only in those modes, with A */
        }
    }
}

static void mfc_write(NfcCard *c, int block, const uint8_t *d)
{
    uint8_t *t = &c->mem[block * 16];

    if (block != mfc_trailer(mfc_sector(block))) {
        memcpy(t, d, 16);
        return;
    }
    /* trailer: which parts this key may write (MF1S50 table) */
    static const int8_t key_w[8] = { 1, 0, 2, 0, 1, 2, 0, 0 };  /* key A and B */
    static const int8_t acc_w[8] = { 0, 0, 0, 0, 1, 2, 2, 0 };  /* access bits */
    int acc = mfc_access(c, block), me = c->key_b ? 2 : 1;

    if (key_w[acc] & me) {
        memcpy(t, d, 6);
        memcpy(t + 10, d + 10, 6);
    }
    if (acc_w[acc] & me) {
        memcpy(t + 6, d + 6, 4);
    }
}

/* encrypt bytes for the air: data bits and their parity bits */
static int mfc_encrypt(NfcCard *c, const uint8_t *d, int n, uint8_t *out)
{
    int bits = 0;

    memset(out, 0, n * 9 / 8 + 2);
    for (int i = 0; i < n; i++) {
        for (int b = 0; b < 8; b++) {
            int ks = c1_ks(c->lfsr);

            c1_clock(&c->lfsr, 0);
            out[bits >> 3] |= (((d[i] >> b) & 1) ^ ks) << (bits & 7);
            bits++;
        }
        /* parity: of the plain byte, with the next keystream bit */
        out[bits >> 3] |= ((!(ctpop8(d[i]) & 1)) ^ c1_ks(c->lfsr)) << (bits & 7);
        bits++;
    }
    return bits;
}

static int mfc_ans(NfcCard *c, const uint8_t *d, int n, uint8_t *out)
{
    uint8_t buf[20];
    uint16_t crc = nfc_crc_a(d, n);

    memcpy(buf, d, n);
    buf[n] = crc & 0xFF;
    buf[n + 1] = crc >> 8;
    return mfc_encrypt(c, buf, n + 2, out);
}

static int mfc_ans4(NfcCard *c, uint8_t v, uint8_t *out)
{
    out[0] = 0;
    for (int b = 0; b < 4; b++) {
        int ks = c1_ks(c->lfsr);

        c1_clock(&c->lfsr, 0);
        out[0] |= (((v >> b) & 1) ^ ks) << b;
    }
    return 4;
}

/* a NAK ends the session: the card drops back to IDLE */
static int mfc_nak(NfcCard *c, uint8_t code, uint8_t *out)
{
    int n = mfc_ans4(c, code, out);

    card_reset(c, ST_IDLE);
    return n;
}

/* decrypt a reader frame (9 bits a byte) from air bit `start`; feed the
 * plaintext back into the LFSR while it is the reader nonce */
static int mfc_decrypt(NfcCard *c, const uint8_t *air, int start, int nbits,
                       uint8_t *d, bool feed)
{
    int n = nbits / 9;

    for (int i = 0; i < n; i++) {
        uint8_t v = 0;

        for (int b = 0; b < 8; b++) {
            int pos = start + i * 9 + b;
            int bit = ((air[pos >> 3] >> (pos & 7)) & 1) ^ c1_ks(c->lfsr);

            c1_clock(&c->lfsr, feed ? bit : 0);
            v |= bit << b;
        }
        d[i] = v;
    }
    return n;
}

/*
 * AUTH (0x60 key A / 0x61 key B) for a block: the card answers its nonce,
 * plain, or encrypted under the new key if a session is open (nested).
 */
static int mfc_auth(NfcCard *c, bool key_b, int block, bool nested, uint8_t *out)
{
    const uint8_t *cuid = &c->uid[c->uid_len - 4];
    const uint8_t *t;
    uint8_t nt[4];

    if (block >= mfc_blocks(c)) {
        card_reset(c, ST_IDLE);         /* no such block: silence */
        return 0;
    }
    t = &c->mem[mfc_trailer(mfc_sector(block)) * 16];
    mfc_nonce(c, nt);
    memcpy(c->nt, nt, 4);
    c->lfsr = c1_init(key_b ? t + 10 : t);
    c->sector = mfc_sector(block);
    c->key_b = key_b;
    c->mfc = MFC_AUTH;
    if (!nested) {
        for (int i = 0; i < 32; i++) {  /* uid ^ nt goes in, unencrypted */
            c1_clock(&c->lfsr, ((cuid[i >> 3] ^ nt[i >> 3]) >> (i & 7)) & 1);
        }
        return nfc_air_encode(nt, 32, out);
    }
    memset(out, 0, 5);
    for (int i = 0, pos = 0; i < 4; i++) {
        for (int b = 0; b < 8; b++, pos++) {
            int ks = c1_ks(c->lfsr);

            c1_clock(&c->lfsr, ((cuid[i] ^ nt[i]) >> b) & 1);
            out[pos >> 3] |= (((nt[i] >> b) & 1) ^ ks) << (pos & 7);
        }
        out[pos >> 3] |= ((!(ctpop8(nt[i]) & 1)) ^ c1_ks(c->lfsr)) << (pos & 7);
        pos++;
    }
    return 36;
}

static int mfc_crypto_frame(NfcCard *c, const uint8_t *air, int nbits, uint8_t *out)
{
    uint8_t d[32];
    int n;

    if (c->mfc == MFC_AUTH) {           /* {nR}{aR}: answer {aT} or nothing */
        uint32_t ar, nt = le32_of(c->nt);

        if (nbits != 72) {
            card_reset(c, ST_IDLE);
            return 0;
        }
        mfc_decrypt(c, air, 0, 36, d, true);        /* nR, fed back */
        mfc_decrypt(c, air, 36, 36, d + 4, false);  /* aR */
        ar = le32_of(d + 4);
        if (ar != prng_next(nt, 64)) {
            card_reset(c, ST_IDLE);     /* wrong key: the card goes quiet */
            return 0;
        }
        c->mfc = MFC_ON;
        nt = prng_next(nt, 96);
        for (int i = 0; i < 4; i++) {
            d[i] = nt >> (8 * i);
        }
        return mfc_encrypt(c, d, 4, out);
    }
    if (c->write_block >= 0) {          /* WRITE, second part: the data */
        int block = c->write_block;

        c->write_block = -1;
        n = mfc_decrypt(c, air, 0, nbits, d, false);
        if (n != 18 || nfc_crc_a(d, 18) != 0) {
            return mfc_nak(c, 0x5, out);
        }
        mfc_write(c, block, d);
        return mfc_ans4(c, 0xA, out);
    }
    n = mfc_decrypt(c, air, 0, nbits, d, false);
    if (n < 2 || nfc_crc_a(d, n) != 0) {
        return mfc_nak(c, 0x5, out);    /* NAK: parity/CRC error */
    }
    switch (d[0]) {
    case 0x30:                          /* READ */
        if (n != 4 || d[1] >= mfc_blocks(c) || mfc_sector(d[1]) != c->sector ||
            !mfc_may_read(c, d[1])) {
            return mfc_nak(c, 0x4, out);
        }
        {
            uint8_t blk[16];

            mfc_read(c, d[1], blk);
            return mfc_ans(c, blk, 16, out);
        }
    case 0xA0:                          /* WRITE */
        if (n != 4 || d[1] >= mfc_blocks(c) || mfc_sector(d[1]) != c->sector ||
            !mfc_may_write(c, d[1]) || d[1] == 0) {
            return mfc_nak(c, 0x4, out);
        }
        c->write_block = d[1];
        return mfc_ans4(c, 0xA, out);
    case 0x60: case 0x61:               /* nested AUTH */
        if (n != 4) {
            break;
        }
        return mfc_auth(c, d[0] == 0x61, d[1], true, out);
    case 0x50:                          /* HALT */
        if (n == 4 && d[1] == 0) {
            card_reset(c, ST_HALT);
            return 0;
        }
        break;
    }
    return mfc_nak(c, 0x4, out);
}

/* ------------------------------------------------------------ ISO14443-3A */

static void card_reset(NfcCard *c, int state)
{
    c->state = state;
    c->level = 0;
    c->authed = false;
    c->comp_write = -1;
    c->mfc = MFC_NONE;
    c->write_block = -1;
}

/* the UID bytes of cascade level `level`, with the cascade tag if needed */
static void cascade_uid(NfcCard *c, int level, uint8_t *u)
{
    int levels = c->uid_len == 4 ? 1 : c->uid_len == 7 ? 2 : 3;

    if (level < levels - 1) {           /* CT 0x88 + 3 bytes */
        u[0] = 0x88;
        memcpy(&u[1], &c->uid[level * 3], 3);
    } else {
        memcpy(u, &c->uid[level * 3], 4);
    }
}

void nfc_card_field(NfcCard *c, bool on)
{
    card_reset(c, on ? ST_IDLE : ST_OFF);
    c->power_ns = qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL);
}

int nfc_card_rx(NfcCard *c, const uint8_t *air, int nbits, uint8_t *out)
{
    uint8_t f[NFC_AIR_MAX];
    bool parity_ok;
    int n, levels = c->uid_len == 4 ? 1 : c->uid_len == 7 ? 2 : 3;

    if (c->state == ST_OFF) {
        return 0;
    }
    if (c->kind == KIND_MFC && c->state == ST_ACTIVE && c->mfc != MFC_NONE) {
        if (nbits != 7) {
            return mfc_crypto_frame(c, air, nbits, out);    /* encrypted */
        }
        card_reset(c, ST_IDLE);         /* a plain REQA/WUPA ends it */
    }
    if (nbits == 7) {                   /* short frame: REQA / WUPA */
        uint8_t cmd = 0;

        for (int i = 0; i < 7; i++) {
            cmd |= ((air[0] >> i) & 1) << i;
        }
        if ((cmd == 0x26 && c->state == ST_IDLE) ||
            (cmd == 0x52 && (c->state == ST_IDLE || c->state == ST_HALT ||
                             c->state == ST_READY))) {
            card_reset(c, ST_READY);
            return ans(out, c->atqa, 2, false);
        }
        return 0;
    }
    n = nfc_air_decode(air, nbits, f, &parity_ok) / 8;
    if (!parity_ok || n < 2) {
        return 0;                       /* garbled: no answer */
    }

    if (c->state == ST_READY) {
        uint8_t u[5];
        int lvl = (f[0] - 0x93) / 2;

        if ((f[0] != 0x93 && f[0] != 0x95 && f[0] != 0x97) || lvl != c->level) {
            card_reset(c, ST_IDLE);
            return 0;
        }
        cascade_uid(c, lvl, u);
        u[4] = u[0] ^ u[1] ^ u[2] ^ u[3];   /* BCC */
        if (f[1] == 0x20 && n == 2) {   /* anticollision: the whole UID part */
            return ans(out, u, 5, false);
        }
        if (f[1] == 0x70 && n == 9 && nfc_crc_a(f, 9) == 0 && !memcmp(&f[2], u, 5)) {
            uint8_t sak;

            if (lvl < levels - 1) {
                sak = 0x04;             /* UID not complete */
                c->level++;
            } else {
                sak = c->sak;
                c->state = ST_ACTIVE;
            }
            return ans(out, &sak, 1, true);
        }
        return 0;
    }
    if (c->state != ST_ACTIVE) {
        return 0;
    }
    if (nfc_crc_a(f, n) != 0) {
        return 0;                       /* commands carry CRC_A */
    }
    if (f[0] == 0x50 && f[1] == 0x00 && n == 4) {   /* HLTA */
        card_reset(c, ST_HALT);
        return 0;
    }
    switch (c->kind) {
    case KIND_T2:
        return t2_command(c, f, n, out);
    case KIND_MFC:
        if ((f[0] == 0x60 || f[0] == 0x61) && n == 4) {
            return mfc_auth(c, f[0] == 0x61, f[1], false, out);
        }
        return 0;                       /* anything else needs a session */
    }
    return 0;
}

/* ------------------------------------------------------------ loading */

/* the hex bytes of a value; returns how many, or -1 */
static int parse_hex(const char *s, uint8_t *out, int max)
{
    int n = 0;

    while (*s) {
        unsigned v;
        char *end;

        while (g_ascii_isspace(*s)) {
            s++;
        }
        if (!*s) {
            break;
        }
        if (s[0] == '?' && s[1] == '?') {   /* unread: zero */
            v = 0;
            end = (char *)s + 2;
        } else {
            v = strtoul(s, &end, 16);
            if (end == s || v > 0xFF) {
                return -1;
            }
        }
        if (n == max) {
            return -1;
        }
        out[n++] = v;
        s = end;
    }
    return n;
}

NfcCard *nfc_card_new(const char *desc, Error **errp)
{
    g_autofree NfcCard *c = g_new0(NfcCard, 1);
    g_auto(GStrv) lines = g_strsplit(desc, "\n", -1);

    c->sak = 0;
    memset(c->pwd, 0xFF, 4);
    memset(c->tearing, 0xBD, 3);
    for (int i = 0; lines[i]; i++) {
        g_autofree char *line = g_strstrip(g_strdup(lines[i]));
        char *val = line;
        int n = 0;

        if (!*line || *line == '#') {
            continue;
        }
        while (*val && !g_ascii_isspace(*val)) {
            val++;
        }
        if (*val) {
            *val++ = 0;
        }
        if (!strcmp(line, "type")) {
            if (!strcmp(g_strstrip(val), "t2")) {
                c->kind = KIND_T2;
            } else if (!strcmp(val, "mfc")) {
                c->kind = KIND_MFC;
            } else {
                error_setg(errp, "nfc card: unsupported type '%s'", val);
                return NULL;
            }
            continue;
        }
        if (!strncmp(line, "counter", 7) && line[7] >= '0' && line[7] <= '2') {
            c->counter[line[7] - '0'] = strtoul(val, NULL, 0);
            continue;
        }
        if (!strcmp(line, "uid")) {
            n = c->uid_len = parse_hex(val, c->uid, 10);
            if (n != 4 && n != 7 && n != 10) {
                error_setg(errp, "nfc card: a UID is 4, 7 or 10 bytes");
                return NULL;
            }
        } else if (!strcmp(line, "atqa")) {
            n = parse_hex(val, c->atqa, 2) == 2 ? 2 : -1;
        } else if (!strcmp(line, "sak")) {
            n = parse_hex(val, &c->sak, 1);
        } else if (!strcmp(line, "version")) {
            n = parse_hex(val, c->version, 8);
            c->has_version = n == 8;
        } else if (!strcmp(line, "signature")) {
            n = parse_hex(val, c->sig, 32);
            c->has_sig = n == 32;
        } else if (!strncmp(line, "tearing", 7) && line[7] >= '0' && line[7] <= '2') {
            n = parse_hex(val, &c->tearing[line[7] - '0'], 1);
        } else if (!strcmp(line, "pwd")) {
            n = parse_hex(val, c->pwd, 4) == 4 ? 4 : -1;
        } else if (!strcmp(line, "pack")) {
            n = parse_hex(val, c->pack, 2) == 2 ? 2 : -1;
        } else if (!strcmp(line, "mem")) {
            n = parse_hex(val, &c->mem[c->mem_len], MEM_MAX - c->mem_len);
            if (n > 0) {
                c->mem_len += n;
            }
        } else {
            continue;                   /* unknown keys: ignore */
        }
        if (n < 0) {
            error_setg(errp, "nfc card: bad value for '%s'", line);
            return NULL;
        }
    }
    if (!c->kind || !c->uid_len) {
        error_setg(errp, "nfc card: needs a type and a uid");
        return NULL;
    }
    if (c->kind == KIND_T2 && c->mem_len < 16) {
        error_setg(errp, "nfc card: a Type 2 tag needs its pages (mem)");
        return NULL;
    }
    if (c->kind == KIND_MFC && c->mem_len != 20 * 16 && c->mem_len != 64 * 16 &&
        c->mem_len != 256 * 16) {
        error_setg(errp, "nfc card: MIFARE Classic Mini, 1K or 4K: 20, 64 or "
                   "256 blocks of 16 bytes (got %d bytes)", c->mem_len);
        return NULL;
    }
    c->mem_len &= ~3;
    card_reset(c, ST_OFF);
    return g_steal_pointer(&c);
}

void nfc_card_free(NfcCard *c)
{
    g_free(c);
}
