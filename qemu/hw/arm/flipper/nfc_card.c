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
 * sends the card back to IDLE, as on silicon.
 *
 * The description (scripts/fe_nfc.py writes it from a Flipper .nfc file) is
 * one "key value" per line:
 *   type t2                 uid 04 51 5C FA 6F 73 81     atqa 44 00
 *   sak 00                  version 00 04 04 02 01 00 11 03
 *   signature <32 bytes>    counter0 0 ... tearing0 BD ...
 *   pwd FF FF FF FF         pack 00 00
 *   mem <bytes>             (pages in order; may repeat)
 *
 * Copyright (c) 2026, Kara Zajac
 * SPDX-License-Identifier: BSD-3-Clause
 */
#include "qemu/osdep.h"
#include "qemu/cutils.h"
#include "qapi/error.h"
#include "flipper.h"

#define MEM_MAX 4096                    /* NTAG216: 231 pages; 4K Classic */

enum { ST_OFF, ST_IDLE, ST_READY, ST_ACTIVE, ST_HALT };
enum { KIND_T2 = 1 };

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

/* ------------------------------------------------------------ ISO14443-3A */

static void card_reset(NfcCard *c, int state)
{
    c->state = state;
    c->level = 0;
    c->authed = false;
    c->comp_write = -1;
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
}

int nfc_card_rx(NfcCard *c, const uint8_t *air, int nbits, uint8_t *out)
{
    uint8_t f[NFC_AIR_MAX];
    bool parity_ok;
    int n, levels = c->uid_len == 4 ? 1 : c->uid_len == 7 ? 2 : 3;

    if (c->state == ST_OFF) {
        return 0;
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
    c->mem_len &= ~3;
    card_reset(c, ST_OFF);
    return g_steal_pointer(&c);
}

void nfc_card_free(NfcCard *c)
{
    g_free(c);
}
