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
 * sends the card back to IDLE, as on silicon.  MIFARE Ultralight C ("ulc
 * 1"): its 48 pages, AUTHENTICATE with two-key triple DES (the key is pages
 * 44 to 47, never read), AUTH0 and AUTH1 for what needs it, and none of
 * EV1's commands.  And MIFARE Classic Mini, 1K
 * and 4K: Crypto1 (from its published description), the nonce PRNG, plain
 * and nested AUTH, encrypted READ/WRITE/HALT, the sector trailers' access
 * bits.
 *
 * ISO14443-4 (a card whose SAK says so, "type iso4"): RATS and the ATS, PPS,
 * and then blocks: I-blocks with the reader's chaining taken (R(ACK) to each
 * part), R(NAK) answered as a presence check, DESELECT.  The card's answer
 * to a command is one I-block with the command's block number.  What runs
 * on it: MIFARE DESFire's native commands, plain or wrapped in ISO 7816
 * (GetVersion, GetFreeMemory, GetKeySettings, GetKeyVersion,
 * GetApplicationIDs, SelectApplication, GetFileIDs, GetFileSettings,
 * ReadData, GetValue, ReadRecords, with ADDITIONAL FRAME for what is longer
 * than 59 bytes; an Authenticate is refused, and a file that the dump could
 * not read asks for one).  With no application ("app none") a command gets
 * ISO 7816's "instruction not supported".
 *
 * The technologies whose frames are bytes (the chip model takes them from
 * and to the firmware's own coding, st25r3916.c): FeliCa Lite-S ("type
 * felica": polling, Read and Write Without Encryption on its 28 blocks, each
 * with the two status bytes that the dump has of it; its MAC is the dump's,
 * so an authentication with a key fails, as with a card whose key is
 * another), and ISO15693-3 with NXP's SLIX on it ("type iso15693":
 * inventory, the addressed, selected and quiet states, system information,
 * blocks read and written singly and in runs with their security status,
 * AFI and DSFID and their locks; SLIX's system information, signature,
 * random number, passwords, privacy mode, EAS).  FeliCa Standard (records
 * of its systems, areas, services and public blocks: polling by a system's
 * code, Request System Code, Search Service Code, Request Service, reading
 * and writing without encryption).  ISO14443B ("type
 * iso14443b": REQB/WUPB and the ATQB, ATTRIB, HLTB, and then ISO14443-4's
 * blocks when the protocol info says so) and ST's ST25TB on its air ("type
 * st25tb": Initiate, Select, Get_UID, Read_Block and Write_Block, the
 * system OTP block at 255).
 *
 * The description (scripts/fe_nfc.py writes it from a Flipper .nfc file) is
 * one "key value" per line:
 *   type t2                 uid 04 51 5C FA 6F 73 81     atqa 44 00
 *   sak 00                  version 00 04 04 02 01 00 11 03
 *   signature <32 bytes>    counter0 0 ... tearing0 BD ...
 *   pwd FF FF FF FF         pack 00 00
 *   mem <bytes>             (pages in order; may repeat)
 * or, for MIFARE Classic, "type mfc" and the blocks in mem (16 bytes each);
 * or "type iso4" with
 *   ats 06 75 77 81 02 80   app desfire   (or mfplus: MIFARE Plus, with
 *                                          its version in a record 01; or
 *                                          iso7816: REC_7816_* below)
 *   rec 01 <28 bytes>       (a record of the application's tables: a type
 *                            and its bytes, see REC_* below; may repeat)
 * or "type felica" with uid (the IDm), pmm, and in mem 18 bytes a block
 * (two status bytes and its 16) for the 28 of a Lite-S in a dump's order,
 * or for a FeliCa Standard card no mem and records (rec 30 ...: REC_FS_*);
 * or "type iso14443b" with uid (the PUPI, 4 bytes), appdata (4), protinfo
 * (3) and app as for iso4; or "type st25tb" with uid (8, D0 first), mem
 * (its blocks) and otp (4 bytes);
 * or "type iso15693" with uid (as it is printed, E0 first), dsfid, afi,
 * icref, blocksize, lock (bit 0 DSFID, 1 AFI, 2 EAS, 3 PPL), mem (the
 * blocks), security (a byte a block), and for a SLIX: slix 1, signature,
 * pwd-read/-write/-privacy/-destroy/-eas (4 bytes each), privacy 0|1,
 * protection <pointer> <condition>, accept-all 0|1.
 *
 * Copyright (c) 2026, Kara Zajac
 * SPDX-License-Identifier: BSD-3-Clause
 */
#include "qemu/osdep.h"
#include "qemu/cutils.h"
#include "qemu/timer.h"
#include "qemu/bitops.h"
#include "qemu/host-utils.h"
#include "qemu/guest-random.h"
#include "qapi/error.h"
#include "migration/vmstate.h"
#include "flipper.h"

#define MEM_MAX 4096                    /* NTAG216: 231 pages; 4K Classic */
#define APP_MAX 16384                   /* an ISO14443-4 application's tables */

enum { ST_OFF, ST_IDLE, ST_READY, ST_ACTIVE, ST_HALT };
enum { KIND_T2 = 1, KIND_MFC, KIND_ISO4, KIND_FELICA, KIND_V, KIND_B,
       KIND_ST25TB };
enum { MFC_NONE, MFC_AUTH, MFC_ON };    /* Crypto1 session */
enum { APP_NONE, APP_DESFIRE, APP_MFPLUS, APP_7816 };   /* on ISO14443-4 */
/* what a DESFire ADDITIONAL FRAME goes on with */
enum { DF_NONE, DF_VERSION_2, DF_VERSION_3, DF_DATA };

/*
 * A DESFire card's tables, as records in `app`: a type, the payload's
 * length (2 bytes, low first), the payload.
 *   REC_VERSION  the 28 bytes of GetVersion's three frames
 *   REC_FREE     GetFreeMemory's 3 bytes (no record: the card has no such
 *                command, as a DESFire before EV1)
 *   REC_APP      an application (000000: the card itself): its AID (3),
 *                GetKeySettings' 2 bytes, a key's version for each key
 *   REC_FILE     a file: its application's AID (3), its number, the length
 *                of GetFileSettings' answer and that answer, whether its
 *                contents were read (1: yes), its contents
 */
#define REC_VERSION 0x01
#define REC_FREE    0x02
#define REC_APP     0x10
#define REC_FILE    0x20
#define DF_FRAME    59                  /* bytes of data to a frame */

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
    bool ulc;                           /* MIFARE Ultralight C */
    bool ulc_auth;                      /* AUTHENTICATE's second part awaited */
    uint8_t ulc_rndb[8], ulc_iv[8];     /* its number, the last ciphertext */
    /* MIFARE Classic: mem holds the blocks */
    int mfc;                            /* MFC_*: where the session is */
    uint64_t lfsr;                      /* Crypto1 state, bit i = x_i */
    uint8_t nt[4];                      /* the nonce in flight, as sent */
    int sector;                         /* authenticated sector, and key */
    bool key_b;
    int write_block;                    /* block awaiting WRITE data, -1 */
    int64_t power_ns;                   /* field on: the nonce PRNG runs */
    /* ISO14443-4 */
    uint8_t ats[20];
    int ats_len;
    bool dep;                           /* RATS answered: blocks from now on */
    uint8_t dep_in[600];                /* a command that comes in parts */
    int dep_in_len;
    int app_kind;                       /* APP_* */
    uint8_t app[APP_MAX];               /* its tables */
    int app_len;
    /* MIFARE DESFire */
    uint8_t df_aid[3];                  /* the application that is selected */
    int df_more;                        /* DF_*: what ADDITIONAL FRAME continues */
    int df_at, df_left;                 /* ... and where in `app`, how much */
    /* ISO 7816-4: the application and the file that are selected (where
     * their records' payloads are in `app`; -1: none) */
    int iso_df, iso_ef;
    /* FeliCa: uid is the IDm, mem the blocks (18 bytes each) */
    uint8_t pmm[8];
    /* ISO14443B: uid is the PUPI; the application data and protocol info
     * of its ATQB.  ST25TB: uid as it is printed (D0 first), mem its
     * blocks of 4 bytes, the system OTP block, the chip ID of this session */
    uint8_t b_app[4], b_info[3];
    uint8_t tb_otp[4], tb_id;
    /* ISO15693: uid as it is printed (E0 first), mem the blocks */
    uint8_t v_dsfid, v_afi, v_icref, v_bsize;
    uint8_t v_lock;                     /* V_LOCK_* */
    uint8_t v_sec[256];                 /* a block's security status */
    bool v_quiet, v_selected;
    bool slix;                          /* NXP's commands too */
    uint8_t slix_pwd[5][4];             /* read, write, privacy, destroy, EAS */
    uint8_t slix_pp[2];                 /* protection pointer and condition */
    uint8_t slix_rnd[2];                /* the random number last given */
    bool slix_privacy, slix_accept_all, slix_eas;
};
#define V_LOCK_DSFID 1
#define V_LOCK_AFI   2
#define V_LOCK_EAS   4
#define V_LOCK_PPL   8

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

/* ------------------------------------------------------------------- DES
 *
 * FIPS 46-3, for the cards that use it (MIFARE Ultralight C's two-key
 * triple DES).  Bits are numbered as the standard has them, 1 the highest
 * of the first byte. */
static const uint8_t des_ip[64] = {
    58, 50, 42, 34, 26, 18, 10, 2, 60, 52, 44, 36, 28, 20, 12, 4,
    62, 54, 46, 38, 30, 22, 14, 6, 64, 56, 48, 40, 32, 24, 16, 8,
    57, 49, 41, 33, 25, 17, 9, 1, 59, 51, 43, 35, 27, 19, 11, 3,
    61, 53, 45, 37, 29, 21, 13, 5, 63, 55, 47, 39, 31, 23, 15, 7 };
static const uint8_t des_fp[64] = {
    40, 8, 48, 16, 56, 24, 64, 32, 39, 7, 47, 15, 55, 23, 63, 31,
    38, 6, 46, 14, 54, 22, 62, 30, 37, 5, 45, 13, 53, 21, 61, 29,
    36, 4, 44, 12, 52, 20, 60, 28, 35, 3, 43, 11, 51, 19, 59, 27,
    34, 2, 42, 10, 50, 18, 58, 26, 33, 1, 41, 9, 49, 17, 57, 25 };
static const uint8_t des_e[48] = {
    32, 1, 2, 3, 4, 5, 4, 5, 6, 7, 8, 9, 8, 9, 10, 11, 12, 13,
    12, 13, 14, 15, 16, 17, 16, 17, 18, 19, 20, 21, 20, 21, 22, 23, 24, 25,
    24, 25, 26, 27, 28, 29, 28, 29, 30, 31, 32, 1 };
static const uint8_t des_p[32] = {
    16, 7, 20, 21, 29, 12, 28, 17, 1, 15, 23, 26, 5, 18, 31, 10,
    2, 8, 24, 14, 32, 27, 3, 9, 19, 13, 30, 6, 22, 11, 4, 25 };
static const uint8_t des_pc1[56] = {
    57, 49, 41, 33, 25, 17, 9, 1, 58, 50, 42, 34, 26, 18,
    10, 2, 59, 51, 43, 35, 27, 19, 11, 3, 60, 52, 44, 36,
    63, 55, 47, 39, 31, 23, 15, 7, 62, 54, 46, 38, 30, 22,
    14, 6, 61, 53, 45, 37, 29, 21, 13, 5, 28, 20, 12, 4 };
static const uint8_t des_pc2[48] = {
    14, 17, 11, 24, 1, 5, 3, 28, 15, 6, 21, 10, 23, 19, 12, 4,
    26, 8, 16, 7, 27, 20, 13, 2, 41, 52, 31, 37, 47, 55, 30, 40,
    51, 45, 33, 48, 44, 49, 39, 56, 34, 53, 46, 42, 50, 36, 29, 32 };
static const uint8_t des_shift[16] = {
    1, 1, 2, 2, 2, 2, 2, 2, 1, 2, 2, 2, 2, 2, 2, 1 };
static const uint8_t des_s[8][64] = {
    { 14, 4, 13, 1, 2, 15, 11, 8, 3, 10, 6, 12, 5, 9, 0, 7,
      0, 15, 7, 4, 14, 2, 13, 1, 10, 6, 12, 11, 9, 5, 3, 8,
      4, 1, 14, 8, 13, 6, 2, 11, 15, 12, 9, 7, 3, 10, 5, 0,
      15, 12, 8, 2, 4, 9, 1, 7, 5, 11, 3, 14, 10, 0, 6, 13 },
    { 15, 1, 8, 14, 6, 11, 3, 4, 9, 7, 2, 13, 12, 0, 5, 10,
      3, 13, 4, 7, 15, 2, 8, 14, 12, 0, 1, 10, 6, 9, 11, 5,
      0, 14, 7, 11, 10, 4, 13, 1, 5, 8, 12, 6, 9, 3, 2, 15,
      13, 8, 10, 1, 3, 15, 4, 2, 11, 6, 7, 12, 0, 5, 14, 9 },
    { 10, 0, 9, 14, 6, 3, 15, 5, 1, 13, 12, 7, 11, 4, 2, 8,
      13, 7, 0, 9, 3, 4, 6, 10, 2, 8, 5, 14, 12, 11, 15, 1,
      13, 6, 4, 9, 8, 15, 3, 0, 11, 1, 2, 12, 5, 10, 14, 7,
      1, 10, 13, 0, 6, 9, 8, 7, 4, 15, 14, 3, 11, 5, 2, 12 },
    { 7, 13, 14, 3, 0, 6, 9, 10, 1, 2, 8, 5, 11, 12, 4, 15,
      13, 8, 11, 5, 6, 15, 0, 3, 4, 7, 2, 12, 1, 10, 14, 9,
      10, 6, 9, 0, 12, 11, 7, 13, 15, 1, 3, 14, 5, 2, 8, 4,
      3, 15, 0, 6, 10, 1, 13, 8, 9, 4, 5, 11, 12, 7, 2, 14 },
    { 2, 12, 4, 1, 7, 10, 11, 6, 8, 5, 3, 15, 13, 0, 14, 9,
      14, 11, 2, 12, 4, 7, 13, 1, 5, 0, 15, 10, 3, 9, 8, 6,
      4, 2, 1, 11, 10, 13, 7, 8, 15, 9, 12, 5, 6, 3, 0, 14,
      11, 8, 12, 7, 1, 14, 2, 13, 6, 15, 0, 9, 10, 4, 5, 3 },
    { 12, 1, 10, 15, 9, 2, 6, 8, 0, 13, 3, 4, 14, 7, 5, 11,
      10, 15, 4, 2, 7, 12, 9, 5, 6, 1, 13, 14, 0, 11, 3, 8,
      9, 14, 15, 5, 2, 8, 12, 3, 7, 0, 4, 10, 1, 13, 11, 6,
      4, 3, 2, 12, 9, 5, 15, 10, 11, 14, 1, 7, 6, 0, 8, 13 },
    { 4, 11, 2, 14, 15, 0, 8, 13, 3, 12, 9, 7, 5, 10, 6, 1,
      13, 0, 11, 7, 4, 9, 1, 10, 14, 3, 5, 12, 2, 15, 8, 6,
      1, 4, 11, 13, 12, 3, 7, 14, 10, 15, 6, 8, 0, 5, 9, 2,
      6, 11, 13, 8, 1, 4, 10, 7, 9, 5, 0, 15, 14, 2, 3, 12 },
    { 13, 2, 8, 4, 6, 15, 11, 1, 10, 9, 3, 14, 5, 0, 12, 7,
      1, 15, 13, 8, 10, 3, 7, 4, 12, 5, 6, 11, 0, 14, 9, 2,
      7, 11, 4, 1, 9, 12, 14, 2, 0, 6, 10, 13, 15, 3, 5, 8,
      2, 1, 14, 7, 4, 10, 8, 13, 15, 12, 9, 0, 3, 5, 6, 11 } };

/* the bits of `in` (its highest is bit 1 of `width`) that a table names */
static uint64_t des_permute(uint64_t in, const uint8_t *table, int n,
                            int width)
{
    uint64_t out = 0;

    for (int i = 0; i < n; i++) {
        out = out << 1 | ((in >> (width - table[i])) & 1);
    }
    return out;
}

/* one block through DES with a key of 8 bytes */
static void des_block(const uint8_t key[8], const uint8_t in[8],
                      uint8_t out[8], bool decrypt)
{
    uint64_t k = 0, b = 0, sub[16];
    uint32_t c, d, l, r;

    for (int i = 0; i < 8; i++) {
        k = k << 8 | key[i];
        b = b << 8 | in[i];
    }
    k = des_permute(k, des_pc1, 56, 64);
    c = k >> 28;
    d = k & 0x0FFFFFFF;
    for (int i = 0; i < 16; i++) {
        c = ((c << des_shift[i]) | (c >> (28 - des_shift[i]))) & 0x0FFFFFFF;
        d = ((d << des_shift[i]) | (d >> (28 - des_shift[i]))) & 0x0FFFFFFF;
        sub[i] = des_permute((uint64_t)c << 28 | d, des_pc2, 48, 56);
    }
    b = des_permute(b, des_ip, 64, 64);
    l = b >> 32;
    r = b;
    for (int i = 0; i < 16; i++) {
        uint64_t x = des_permute(r, des_e, 48, 32) ^ sub[decrypt ? 15 - i : i];
        uint32_t f = 0, t;

        for (int s = 0; s < 8; s++) {
            int six = (x >> (42 - 6 * s)) & 0x3F;
            int row = (six >> 4 & 2) | (six & 1), col = six >> 1 & 0xF;

            f = f << 4 | des_s[s][row * 16 + col];
        }
        t = l ^ des_permute(f, des_p, 32, 32);
        l = r;
        r = t;
    }
    b = des_permute((uint64_t)r << 32 | l, des_fp, 64, 64);
    for (int i = 7; i >= 0; i--) {
        out[i] = b;
        b >>= 8;
    }
}

/* Triple DES with two keys (16 bytes: K1, K2, K1 again), in CBC: `iv` is
 * the last block of ciphertext when it returns */
static void tdes2_cbc(const uint8_t key[16], uint8_t iv[8], const uint8_t *in,
                      uint8_t *out, int blocks, bool decrypt)
{
    for (int n = 0; n < blocks; n++, in += 8, out += 8) {
        uint8_t x[8], y[8];

        if (decrypt) {
            des_block(key, in, x, true);
            des_block(key + 8, x, y, false);
            des_block(key, y, x, true);
            for (int i = 0; i < 8; i++) {
                y[i] = x[i] ^ iv[i];
            }
            memcpy(iv, in, 8);
            memcpy(out, y, 8);
        } else {
            for (int i = 0; i < 8; i++) {
                x[i] = in[i] ^ iv[i];
            }
            des_block(key, x, y, false);
            des_block(key + 8, y, x, true);
            des_block(key, x, out, false);
            memcpy(iv, out, 8);
        }
    }
}

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

    if (c->ulc) {
        /* the key's pages never; from AUTH0 (page 42) on with the key,
         * unless AUTH1 (page 43) says that only writes need it */
        return page < 44 && (c->authed || page < c->mem[42 * 4] ||
                             (c->mem[43 * 4] & 1));
    }
    if (cfg < 0 || c->authed) {
        return true;
    }
    /* AUTH0 = CFG0[3], PROT = ACCESS (CFG1[0]) bit 7: reads protected too */
    return page < c->mem[cfg * 4 + 3] || !(c->mem[(cfg + 1) * 4] & 0x80);
}

static bool t2_writable(NfcCard *c, int page)
{
    int cfg = t2_cfg(c);

    if (c->ulc) {
        return page >= 2 && page < 48 && (c->authed || page < c->mem[42 * 4]);
    }
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
    if (c->ulc) {
        bool second = c->ulc_auth;

        c->ulc_auth = false;
        if (f[0] == 0x1A && n == 4 && f[1] == 0x00) {
            /* AUTHENTICATE: the card's number, enciphered (IV 0) */
            qemu_guest_getrandom_nofail(c->ulc_rndb, 8);
            memset(c->ulc_iv, 0, 8);
            buf[0] = 0xAF;
            tdes2_cbc(&c->mem[44 * 4], c->ulc_iv, c->ulc_rndb, &buf[1], 1,
                      false);
            c->ulc_auth = true;
            c->authed = false;
            return ans(out, buf, 9, true);
        }
        if (f[0] == 0xAF && n == 19 && second) {
            /* the reader's number and ours turned by a byte, enciphered
             * on from our ciphertext: ours has to be right; then the
             * reader's, turned, enciphered on from the reader's */
            uint8_t plain[16], turned[8];

            tdes2_cbc(&c->mem[44 * 4], c->ulc_iv, &f[1], plain, 2, true);
            memcpy(turned, &c->ulc_rndb[1], 7);
            turned[7] = c->ulc_rndb[0];
            if (!memcmp(&plain[8], turned, 8)) {
                memcpy(turned, &plain[1], 7);
                turned[7] = plain[0];
                buf[0] = 0x00;
                tdes2_cbc(&c->mem[44 * 4], c->ulc_iv, turned, &buf[1], 1,
                          false);
                c->authed = true;
                return ans(out, buf, 9, true);
            }
            c->state = ST_IDLE;         /* a wrong key: a NAK */
            return ans4(out, 0x0);
        }
        if (f[0] != 0x30 && f[0] != 0xA2 && f[0] != 0xA0) {
            c->state = ST_IDLE;         /* none of EV1's commands */
            return ans4(out, 0x0);
        }
        pages = 44;                     /* (a READ rolls over before the key) */
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

/* ------------------------------------------------------- MIFARE DESFire */

/* The next record of a type from `at` on: where its payload is and how
 * long (-1: none).  *at is then behind it. */
static int rec_next(NfcCard *c, int *at, int type, int *len)
{
    while (*at + 3 <= c->app_len) {
        int n = c->app[*at + 1] | c->app[*at + 2] << 8;
        int here = *at + 3;
        bool hit = c->app[*at] == type;

        if (here + n > c->app_len) {
            break;
        }
        *at = here + n;
        if (hit) {
            *len = n;
            return here;
        }
    }
    return -1;
}

/* the record at *at: where its payload is (-1: none); *at is then behind it */
static int rec_any(NfcCard *c, int *at, int *type, int *len)
{
    int n, here = *at + 3;

    if (here > c->app_len) {
        return -1;
    }
    n = c->app[*at + 1] | c->app[*at + 2] << 8;
    if (here + n > c->app_len) {
        return -1;
    }
    *type = c->app[*at];
    *len = n;
    *at = here + n;
    return here;
}

static int df_app(NfcCard *c, const uint8_t aid[3], int *len)
{
    int at = 0, here;

    while ((here = rec_next(c, &at, REC_APP, len)) >= 0) {
        if (*len >= 3 && !memcmp(&c->app[here], aid, 3)) {
            return here;
        }
    }
    return -1;
}

/* a file of the selected application: where its settings' length is */
static int df_file(NfcCard *c, uint8_t id, int *len)
{
    int at = 0, here;

    while ((here = rec_next(c, &at, REC_FILE, len)) >= 0) {
        if (*len >= 6 && !memcmp(&c->app[here], c->df_aid, 3) &&
            c->app[here + 3] == id) {
            return here;
        }
    }
    return -1;
}

static uint32_t le24(const uint8_t *b)
{
    return b[0] | b[1] << 8 | (uint32_t)b[2] << 16;
}

/* the next frame's worth of what is being read; its status says if more */
static int df_frame(NfcCard *c, uint8_t *d, uint8_t *st)
{
    int n = MIN(c->df_left, DF_FRAME);

    memcpy(d, &c->app[c->df_at], n);
    c->df_at += n;
    c->df_left -= n;
    c->df_more = c->df_left ? DF_DATA : DF_NONE;
    *st = c->df_left ? 0xAF : 0x00;
    return n;
}

/* A file's contents from `off`, `want` bytes of them (0: to its end).
 * records: `off` and `want` count records, the newest first, as ReadRecords
 * has it (the file's settings say how long one is and how many there are) */
static int df_read(NfcCard *c, const uint8_t *p, int pn, uint8_t *d,
                   uint8_t *st, bool records)
{
    int len, here = pn >= 7 ? df_file(c, p[0], &len) : -1;
    int nset, data, size;
    uint32_t off, want;

    if (pn < 7) {
        *st = 0x7E;                     /* LENGTH_ERROR */
        return 0;
    }
    if (here < 0) {
        *st = 0xF0;                     /* FILE_NOT_FOUND */
        return 0;
    }
    nset = c->app[here + 4];
    data = here + 5 + nset + 1;
    size = here + len - data;
    if (5 + nset + 1 > len || !c->app[here + 5 + nset]) {
        *st = 0xAE;                     /* AUTHENTICATION_ERROR: not ours */
        return 0;
    }
    off = le24(p + 1);
    want = le24(p + 4);
    if (records) {
        uint32_t one = nset >= 13 ? le24(&c->app[here + 9]) : 0;
        uint32_t have = one ? MIN(le24(&c->app[here + 15]), size / one) : 0;

        if (!one || off >= have || want > have - off) {
            *st = 0xBE;
            return 0;
        }
        want = (want ? want : have - off) * one;
        off = (have - off) * one - want;
    }
    if (off > size || want > size - off) {
        *st = 0xBE;                     /* BOUNDARY_ERROR */
        return 0;
    }
    c->df_at = data + off;
    c->df_left = want ? want : size - off;
    return df_frame(c, d, st);
}

/* A DESFire command (native, or wrapped: 90 INS 00 00 Lc data 00) -> its
 * answer (a status and data; wrapped: data and 91 status) */
static int df_command(NfcCard *c, const uint8_t *in, int n, uint8_t *out)
{
    bool wrapped = n >= 5 && in[0] == 0x90;
    uint8_t cmd = wrapped ? in[1] : in[0];
    const uint8_t *p = wrapped ? in + 5 : in + 1;
    int pn = wrapped ? MIN(in[4], n - 5) : n - 1;
    uint8_t d[DF_FRAME + 4], st = 0x00;
    int dn = 0, len, here, at = 0, more = c->df_more;

    c->df_more = DF_NONE;
    switch (cmd) {
    case 0x60:                          /* GetVersion: three frames */
        here = rec_next(c, &at, REC_VERSION, &len);
        if (here < 0 || len != 28) {
            st = 0x1C;
            break;
        }
        memcpy(d, &c->app[here], dn = 7);
        c->df_at = here + 7;
        c->df_more = DF_VERSION_2;
        st = 0xAF;
        break;
    case 0xAF:                          /* ADDITIONAL FRAME */
        if (more == DF_VERSION_2) {
            memcpy(d, &c->app[c->df_at], dn = 7);
            c->df_at += 7;
            c->df_more = DF_VERSION_3;
            st = 0xAF;
        } else if (more == DF_VERSION_3) {
            memcpy(d, &c->app[c->df_at], dn = 14);
        } else if (more == DF_DATA) {
            dn = df_frame(c, d, &st);
        } else {
            st = 0xCA;                  /* COMMAND_ABORTED */
        }
        break;
    case 0x6E:                          /* GetFreeMemory */
        here = rec_next(c, &at, REC_FREE, &len);
        if (here < 0 || len != 3) {
            st = 0x1C;                  /* ILLEGAL_COMMAND_CODE */
            break;
        }
        memcpy(d, &c->app[here], dn = 3);
        break;
    case 0x45:                          /* GetKeySettings */
        here = df_app(c, c->df_aid, &len);
        if (here < 0 || len < 5) {
            st = 0xAE;
            break;
        }
        memcpy(d, &c->app[here + 3], dn = 2);
        break;
    case 0x64:                          /* GetKeyVersion */
        here = df_app(c, c->df_aid, &len);
        if (pn < 1 || here < 0 || len < 5) {
            st = pn < 1 ? 0x7E : 0xAE;
        } else if (p[0] >= len - 5) {
            st = 0x40;                  /* NO_SUCH_KEY */
        } else {
            d[dn++] = c->app[here + 5 + p[0]];
        }
        break;
    case 0x6A:                          /* GetApplicationIDs */
        if (c->df_aid[0] | c->df_aid[1] | c->df_aid[2]) {
            st = 0x9D;                  /* PERMISSION_DENIED: at the card's */
            break;
        }
        while ((here = rec_next(c, &at, REC_APP, &len)) >= 0) {
            const uint8_t *aid = &c->app[here];

            if (len >= 3 && (aid[0] | aid[1] | aid[2]) && dn + 3 <= 57) {
                memcpy(&d[dn], aid, 3);
                dn += 3;
            }
        }
        break;
    case 0x5A:                          /* SelectApplication */
        if (pn < 3) {
            st = 0x7E;
        } else if ((p[0] | p[1] | p[2]) && df_app(c, p, &len) < 0) {
            st = 0xA0;                  /* APPLICATION_NOT_FOUND */
        } else {
            memcpy(c->df_aid, p, 3);
        }
        break;
    case 0x6F:                          /* GetFileIDs */
        if (!(c->df_aid[0] | c->df_aid[1] | c->df_aid[2])) {
            st = 0x9D;
            break;
        }
        while ((here = rec_next(c, &at, REC_FILE, &len)) >= 0) {
            if (len >= 6 && !memcmp(&c->app[here], c->df_aid, 3) && dn < 32) {
                d[dn++] = c->app[here + 3];
            }
        }
        break;
    case 0xF5:                          /* GetFileSettings */
        here = pn >= 1 ? df_file(c, p[0], &len) : -1;
        if (here < 0) {
            st = pn < 1 ? 0x7E : 0xF0;
            break;
        }
        dn = MIN(c->app[here + 4], len - 5);
        memcpy(d, &c->app[here + 5], dn);
        break;
    case 0xBD:                          /* ReadData */
    case 0xBB:                          /* ReadRecords */
        dn = df_read(c, p, pn, d, &st, cmd == 0xBB);
        break;
    case 0x6C: {                        /* GetValue */
        uint8_t all[7] = { pn >= 1 ? p[0] : 0, 0, 0, 0, 4, 0, 0 };

        dn = df_read(c, all, pn >= 1 ? 7 : 0, d, &st, false);
        break;
    }
    case 0x0A: case 0x1A: case 0xAA:    /* Authenticate: no key is ours */
        st = 0xAE;
        break;
    default:
        st = 0x1C;
        break;
    }
    if (wrapped) {
        memcpy(out, d, dn);
        out[dn] = 0x91;
        out[dn + 1] = st;
        return dn + 2;
    }
    out[0] = st;
    memcpy(out + 1, d, dn);
    return dn + 1;
}

/* ------------------------------------------------------------ ISO14443-4 */

/* A whole command to the application that runs on the card -> its answer */
/* ---------------------------------------------------------- ISO 7816-4 */

/*
 * A card of ISO 7816-4's commands ("app iso7816"): applications that are
 * selected by name, files of bytes, records, data objects.  A Type 4 tag
 * (NDEF) is one: an application with two files.  An EMV card is one:
 * applications with records, and an answer to GET PROCESSING OPTIONS.  The
 * records (in app[]), each of an application after its own, and those
 * before the first application the card's own:
 */
#define REC_7816_DF     0x40    /* a name's length, the name, what SELECT
                                 * answers with (the FCI) */
#define REC_7816_EF     0x41    /* a file's ID (2 bytes), its bytes */
#define REC_7816_RECORD 0x42    /* a record's SFI, its number, its bytes */
#define REC_7816_DATA   0x43    /* a tag (2 bytes), what GET DATA answers */
#define REC_7816_ANSWER 0x44    /* how a command begins (a length before),
                                 * the whole answer to it with its status */
#define ISO_LE_MAX      251     /* (a frame of 256 with PCB, status, CRC) */

/* the records of the application at `df` (where its record's payload is;
 * < 0: the card's own, before the first application) */
static void iso_span(NfcCard *c, int df, int *from, int *to)
{
    int at = 0, type, len, here;
    bool inside = df < 0;

    *from = 0;
    while ((here = rec_any(c, &at, &type, &len)) >= 0) {
        if (type != REC_7816_DF) {
            continue;
        }
        if (inside) {
            *to = here - 3;
            return;
        }
        if (here == df) {
            inside = true;
            *from = at;
        }
    }
    *to = inside ? at : 0;
}

/* a record of a type whose first bytes are `key`, of the application that
 * is selected or else of the card: where its payload is (-1: none) */
static int iso_find(NfcCard *c, int type, const uint8_t *key, int klen,
                    int *len)
{
    for (int pass = c->iso_df >= 0 ? 0 : 1; pass < 2; pass++) {
        int from, to, here, kind;

        iso_span(c, pass ? -1 : c->iso_df, &from, &to);
        while (from < to && (here = rec_any(c, &from, &kind, len)) >= 0) {
            if (kind == type && *len >= klen && !memcmp(&c->app[here], key,
                                                         klen)) {
                return here;
            }
        }
    }
    return -1;
}

static int iso_sw(uint8_t *out, int n, uint16_t sw)
{
    out[n] = sw >> 8;
    out[n + 1] = sw & 0xFF;
    return n + 2;
}

static int iso_command(NfcCard *c, const uint8_t *in, int n, uint8_t *out)
{
    int lc = 0, le = -1, len, here, at = 0, type;
    const uint8_t *data = in + 5;

    if (n < 4) {
        return iso_sw(out, 0, 0x6700);
    }
    if (n == 5) {
        le = in[4];
    } else if (n > 5) {
        lc = in[4];
        if (n == 5 + lc + 1) {
            le = in[n - 1];
        } else if (n != 5 + lc) {
            return iso_sw(out, 0, 0x6700);      /* a wrong length */
        }
    }
    /* an answer that is the card's to give as it is (EMV's GET PROCESSING
     * OPTIONS, whose answer does not depend on what the terminal says) */
    for (int pass = c->iso_df >= 0 ? 0 : 1; pass < 2; pass++) {
        int from, to;

        iso_span(c, pass ? -1 : c->iso_df, &from, &to);
        while (from < to && (here = rec_any(c, &from, &type, &len)) >= 0) {
            int plen = len >= 1 ? c->app[here] : 0;

            if (type == REC_7816_ANSWER && plen >= 1 && plen < len &&
                plen <= n && !memcmp(&c->app[here + 1], in, plen) &&
                len - 1 - plen <= ISO_LE_MAX + 2) {
                memcpy(out, &c->app[here + 1 + plen], len - 1 - plen);
                return len - 1 - plen;
            }
        }
    }
    switch (in[1]) {
    case 0xA4:                          /* SELECT */
        if (in[2] == 0x04) {            /* an application, by its name or
                                         * by how the name begins */
            while ((here = rec_any(c, &at, &type, &len)) >= 0) {
                int nlen = len >= 1 ? c->app[here] : 0;

                if (type == REC_7816_DF && lc >= 1 && nlen >= lc &&
                    nlen < len && !memcmp(&c->app[here + 1], data, lc)) {
                    c->iso_df = here;
                    c->iso_ef = -1;
                    len -= 1 + nlen;
                    if ((in[3] & 0x0C) == 0x0C || len > ISO_LE_MAX) {
                        len = 0;        /* no answer's data is asked for */
                    }
                    memcpy(out, &c->app[here + 1 + nlen], len);
                    return iso_sw(out, len, 0x9000);
                }
            }
            return iso_sw(out, 0, 0x6A82);      /* no such file */
        }
        if ((in[2] == 0x00 || in[2] == 0x02) && lc == 2) {  /* a file */
            if (data[0] == 0x3F && data[1] == 0x00) {       /* the card */
                c->iso_df = c->iso_ef = -1;
                return iso_sw(out, 0, 0x9000);
            }
            here = iso_find(c, REC_7816_EF, data, 2, &len);
            if (here < 0) {
                return iso_sw(out, 0, 0x6A82);
            }
            c->iso_ef = here;
            return iso_sw(out, 0, 0x9000);
        }
        return iso_sw(out, 0, 0x6A86);          /* P1, P2: not so */
    case 0xB0:                          /* READ BINARY */
    case 0xD6:                          /* UPDATE BINARY */
        at = 0;
        if (c->iso_ef < 0 || (in[2] & 0x80) ||
            rec_any(c, &(int){ c->iso_ef - 3 }, &type, &len) != c->iso_ef ||
            type != REC_7816_EF || len < 2) {
            return iso_sw(out, 0, 0x6986);      /* no file is selected */
        }
        here = c->iso_ef + 2;
        len -= 2;
        at = in[2] << 8 | in[3];
        if (in[1] == 0xD6) {
            if (lc < 1 || at + lc > len) {
                return iso_sw(out, 0, 0x6B00);  /* beyond the file */
            }
            memcpy(&c->app[here + at], data, lc);
            return iso_sw(out, 0, 0x9000);
        }
        if (n != 5 || at >= len) {
            return iso_sw(out, 0, n != 5 ? 0x6700 : 0x6B00);
        }
        le = MIN(MIN(le ? le : 256, ISO_LE_MAX), len - at);
        memcpy(out, &c->app[here + at], le);
        return iso_sw(out, le, 0x9000);
    case 0xB2: {                        /* READ RECORD: P1 its number, the
                                         * SFI in P2's high five bits */
        uint8_t key[2] = { in[3] >> 3, in[2] };

        if ((in[3] & 0x07) != 0x04) {
            return iso_sw(out, 0, 0x6A86);
        }
        here = iso_find(c, REC_7816_RECORD, key, 2, &len);
        if (here < 0 || len - 2 > ISO_LE_MAX) {
            /* no such record; no such file, when it has none at all */
            return iso_sw(out, 0, iso_find(c, REC_7816_RECORD, key, 1, &len)
                          < 0 ? 0x6A82 : 0x6A83);
        }
        memcpy(out, &c->app[here + 2], len - 2);
        return iso_sw(out, len - 2, 0x9000);
    }
    case 0xCA:                          /* GET DATA: a tag in P1 and P2 */
        here = iso_find(c, REC_7816_DATA, &in[2], 2, &len);
        if (here < 0 || len - 2 > ISO_LE_MAX) {
            return iso_sw(out, 0, 0x6A88);      /* no such data */
        }
        memcpy(out, &c->app[here + 2], len - 2);
        return iso_sw(out, len - 2, 0x9000);
    }
    return iso_sw(out, 0, 0x6D00);              /* no such instruction */
}

static int app_command(NfcCard *c, const uint8_t *in, int n, uint8_t *out)
{
    if (c->app_kind == APP_DESFIRE && n >= 1) {
        return df_command(c, in, n, out);
    }
    if (c->app_kind == APP_7816) {
        return iso_command(c, in, n, out);
    }
    if (c->app_kind == APP_MFPLUS && n >= 1) {
        /* MIFARE Plus: of its commands GetVersion (EV1 and EV2 have it:
         * three frames, as a DESFire's, from the same record; a Plus S or X
         * has no such record and says so); the others are "command
         * invalid", by which a DESFire's reader knows that it is none */
        if (n == 1 && (in[0] == 0x60 || in[0] == 0xAF)) {
            return df_command(c, in, n, out);
        }
        c->df_more = DF_NONE;
        out[0] = 0x0B;
        return 1;
    }
    out[0] = 0x6D;                      /* ISO 7816: INS not supported */
    out[1] = 0x00;
    return 2;
}

/* A block to an ISO14443-4 card (without its CRC) -> the card's block in r
 * (0: none).  *halt: the card was deselected and is halted after its answer */
static int iso4_block(NfcCard *c, const uint8_t *f, int len, uint8_t *r,
                      bool *halt)
{
    uint8_t pcb = f[0];
    int at = 1, rn;

    *halt = false;
    if ((pcb & 0xF0) == 0xD0) {         /* PPS: as asked */
        r[0] = pcb;
        return 1;
    }
    if (pcb & 0x08) {                   /* a CID follows (ours is 0) */
        if (len < 2) {
            return 0;
        }
        r[1] = f[at++];
    }
    if ((pcb & 0xC0) == 0xC0) {         /* S-block */
        if ((pcb & 0x30) == 0x00) {     /* DESELECT: said back, then halted */
            r[0] = pcb;
            *halt = true;
            return at;
        }
        return 0;
    }
    if ((pcb & 0xC0) == 0x80) {         /* R-block */
        if (pcb & 0x10) {               /* R(NAK): we are here */
            r[0] = 0xA2 | (pcb & 0x09);
            return at;
        }
        return 0;                       /* R(ACK): we send nothing in parts */
    }
    if (pcb & 0xC0) {
        return 0;
    }
    if (pcb & 0x04) {                   /* a NAD */
        at++;
    }
    if (at > len || c->dep_in_len + len - at > sizeof(c->dep_in)) {
        c->dep_in_len = 0;
        return 0;
    }
    memcpy(&c->dep_in[c->dep_in_len], &f[at], len - at);
    c->dep_in_len += len - at;
    if (pcb & 0x10) {                   /* more to come: R(ACK) */
        r[0] = 0xA2 | (pcb & 0x09);
        return pcb & 0x08 ? 2 : 1;
    }
    r[0] = 0x02 | (pcb & 0x09);         /* an I-block with its number */
    rn = pcb & 0x08 ? 2 : 1;
    rn += app_command(c, c->dep_in, c->dep_in_len, &r[rn]);
    c->dep_in_len = 0;
    return rn;
}

/* a frame to an ISO14443-4A card that is selected (f: with its CRC_A) */
static int iso4_frame(NfcCard *c, const uint8_t *f, int n, uint8_t *out)
{
    uint8_t r[300];
    bool halt;
    int rn;

    if (!c->dep) {
        if (f[0] == 0xE0 && n == 4) {   /* RATS */
            c->dep = true;
            c->dep_in_len = 0;
            return ans(out, c->ats, c->ats_len, true);
        }
        card_reset(c, ST_IDLE);         /* not ours: silence, and from anew */
        return 0;
    }
    rn = iso4_block(c, f, n - 2, r, &halt);
    if (rn <= 0) {
        return 0;
    }
    rn = ans(out, r, rn, true);
    if (halt) {
        card_reset(c, ST_HALT);
    }
    return rn;
}

/* ------------------------------------------------------------ ISO14443-3A */

static void card_reset(NfcCard *c, int state)
{
    c->state = state;
    c->level = 0;
    c->authed = false;
    c->ulc_auth = false;
    c->comp_write = -1;
    c->mfc = MFC_NONE;
    c->write_block = -1;
    c->dep = false;
    c->dep_in_len = 0;
    c->df_more = DF_NONE;
    memset(c->df_aid, 0, 3);
    c->iso_df = c->iso_ef = -1;
    c->v_quiet = c->v_selected = false; /* (a field that went and came) */
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

    if (c->state == ST_OFF || c->kind >= KIND_FELICA) {
        return 0;                       /* (no ISO14443A card) */
    }
    /* (KIND_B's blocks are ISO14443-4's too, but its air is not this) */
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
    case KIND_ISO4:
        return iso4_frame(c, f, n, out);
    }
    return 0;
}

/* ---------------------------------------------------------------- FeliCa */

/* CRC-16 as FeliCa has it (x^16 + x^12 + x^5 + 1 from 0, the high byte
 * first) */
static uint16_t felica_crc(const uint8_t *d, int n)
{
    uint16_t crc = 0;

    for (int i = 0; i < n; i++) {
        crc ^= d[i] << 8;
        for (int b = 0; b < 8; b++) {
            crc = crc & 0x8000 ? crc << 1 ^ 0x1021 : crc << 1;
        }
    }
    return crc;
}

/* where a Lite-S's block is in a dump (-1: it has no such block): 0x00 to
 * 0x0E, 0x80 to 0x88, 0x90 to 0x92, 0xA0 */
static int felica_block(NfcCard *c, int number)
{
    int at = number <= 0x0E ? number :
             number >= 0x80 && number <= 0x88 ? 15 + number - 0x80 :
             number >= 0x90 && number <= 0x92 ? 24 + number - 0x90 :
             number == 0xA0 ? 27 : -1;

    return at >= 0 && (at + 1) * 18 <= c->mem_len ? at : -1;
}

/* A frame: its length (itself and what follows, less the CRC), the
 * command, what the command has, the CRC */
/*
 * FeliCa Standard.  A card has systems, each with a code; a system has areas
 * and services in an order of its own, and a service has blocks.  A reader
 * speaks to one system at a time: its number is the high four bits of the
 * IDm's first byte.  The records (in app[]) are a system's code, then its
 * areas and services in the card's order, and those blocks of its services
 * that are read without a key (a dump has no others).  A system, an area
 * and a service each have a key's version, which Request Service tells.
 */
#define REC_FS_SYSTEM   0x30    /* its code (the high byte first), its key's
                                 * version (the low byte first, as the rest) */
#define REC_FS_AREA     0x31    /* its code, its end, its key's version */
#define REC_FS_BLOCK    0x32    /* its service's code, its number, 16 bytes */
#define REC_FS_SERVICE  0x33    /* its code, its key's version */

/* System `sys`: where its code is (-1: no such system), and its records,
 * from *from up to *to.  sys < 0: none is looked for; -> how many there are */
static int fs_system(NfcCard *c, int sys, int *from, int *to)
{
    int at = 0, type, len, here, code = -1, n = 0;

    while ((here = rec_any(c, &at, &type, &len)) >= 0) {
        if (type != REC_FS_SYSTEM || len != 4) {
            continue;
        }
        if (code >= 0) {
            *to = here - 3;
            return code;
        }
        if (n++ == sys) {
            code = here;
            *from = at;
        }
    }
    if (sys < 0) {
        return n;
    }
    *to = at;
    return code;
}

/* is this record an area or a service, as they are to be? */
static bool fs_is_node(int type, int len)
{
    return (type == REC_FS_AREA && len == 6) ||
           (type == REC_FS_SERVICE && len == 4);
}

/* a system's area or service of a code: where it is (-1: none), and which */
static int fs_node(NfcCard *c, int from, int to, int code, int *type)
{
    int len, here;

    while (from < to && (here = rec_any(c, &from, type, &len)) >= 0) {
        if (fs_is_node(*type, len) &&
            (c->app[here] | c->app[here + 1] << 8) == code) {
            return here;
        }
    }
    return -1;
}

/* a service's block: where its 16 bytes are (-1: none) */
static int fs_block(NfcCard *c, int from, int to, int code, int number)
{
    int type, len, here;

    while (from < to && (here = rec_any(c, &from, &type, &len)) >= 0) {
        if (type == REC_FS_BLOCK && len == 19 && c->app[here + 2] == number &&
            (c->app[here] | c->app[here + 1] << 8) == code) {
            return here + 3;
        }
    }
    return -1;
}

/* a command to a FeliCa Standard card (its length is right, and its CRC)
 * -> the answer in r from r[1] on, and its length with r[0] (0: none) */
static int fs_command(NfcCard *c, const uint8_t *in, int len, uint8_t *r)
{
    int systems = fs_system(c, -1, NULL, NULL), from = 0, to = 0, rn;
    int code = -1;
    int type, plen, here, n, nsvc, nblk, at;
    int where[16];
    bool write = in[1] == 0x08;

    if (in[1] == 0x00) {
        /* Polling: the system of a code (0xFF: any byte), or the first */
        if (len != 6) {
            return 0;
        }
        for (n = 0; n < systems; n++) {
            code = fs_system(c, n, &from, &to);
            if ((in[2] == 0xFF || in[2] == c->app[code]) &&
                (in[3] == 0xFF || in[3] == c->app[code + 1])) {
                break;
            }
        }
        if (n == systems) {
            return 0;
        }
        r[1] = 0x01;
        memcpy(&r[2], c->uid, 8);
        r[2] = (r[2] & 0x0F) | n << 4;
        memcpy(&r[10], c->pmm, 8);
        rn = 18;
        if (in[4] == 0x01) {            /* the system's code */
            r[rn++] = c->app[code];
            r[rn++] = c->app[code + 1];
        } else if (in[4] == 0x02) {     /* how fast it can talk: 212, 424 */
            r[rn++] = 0x00;
            r[rn++] = 0x83;
        }
        return rn;
    }
    /* the others are to an IDm, whose high bits are a system's number */
    if (len < 10 || (in[2] & 0x0F) != (c->uid[0] & 0x0F) ||
        memcmp(&in[3], &c->uid[1], 7) ||
        fs_system(c, in[2] >> 4, &from, &to) < 0) {
        return 0;
    }
    r[1] = in[1] + 1;
    memcpy(&r[2], &in[2], 8);
    rn = 10;
    switch (in[1]) {
    case 0x04:                          /* Request Response: mode 0 */
        if (len != 10) {
            return 0;
        }
        r[rn++] = 0x00;
        return rn;
    case 0x0C:                          /* Request System Code: all of them */
        if (len != 10) {
            return 0;
        }
        r[rn++] = systems;
        for (n = 0; n < systems; n++) {
            code = fs_system(c, n, &from, &to);
            r[rn++] = c->app[code];
            r[rn++] = c->app[code + 1];
        }
        return rn;
    case 0x0A:                          /* Search Service Code: the n-th */
        if (len != 12) {
            return 0;
        }
        n = in[10] | in[11] << 8;
        while (from < to && (here = rec_any(c, &from, &type, &plen)) >= 0) {
            if (fs_is_node(type, plen) && !n--) {
                plen -= 2;              /* (an area with its end) */
                memcpy(&r[rn], &c->app[here], plen);
                return rn + plen;
            }
        }
        r[rn++] = 0xFF;                 /* no more */
        r[rn++] = 0xFF;
        return rn;
    case 0x02:                          /* Request Service: key versions */
        n = len > 10 ? in[10] : 0;
        if (n < 1 || n > 32 || len != 11 + 2 * n) {
            return 0;
        }
        r[rn++] = n;
        code = fs_system(c, in[2] >> 4, &from, &to);
        for (int i = 0; i < n; i++) {
            int node = in[11 + 2 * i] | in[12 + 2 * i] << 8;

            /* (0xFFFF asks for the system's; 0xFFFF is said for none) */
            here = node == 0xFFFF ? code + 2 :
                   fs_node(c, from, to, node, &type);
            if (here >= 0 && node != 0xFFFF) {
                here += type == REC_FS_AREA ? 4 : 2;
            }
            r[rn++] = here < 0 ? 0xFF : c->app[here];
            r[rn++] = here < 0 ? 0xFF : c->app[here + 1];
        }
        return rn;
    case 0x06:                          /* Read Without Encryption */
    case 0x08:                          /* Write Without Encryption */
        r[10] = r[11] = 0;
        rn = 12;
        nsvc = len > 10 ? in[10] : 0;
        if (nsvc < 1 || nsvc > 16 || len < 12 + 2 * nsvc) {
            r[10] = 0xFF;
            r[11] = 0xA1;               /* the number of services */
            return rn;
        }
        nblk = in[11 + 2 * nsvc];
        at = 12 + 2 * nsvc;
        if (nblk < 1 || nblk > (write ? 13 : 15)) {
            r[10] = 0xFF;
            r[11] = 0xA2;               /* the number of blocks */
            return rn;
        }
        for (int i = 0; i < nblk; i++) {
            /* a block of the list: two bytes or three; the service by its
             * place in the list of services, the block's number */
            int wide = !(at < len && (in[at] & 0x80));
            int order, number, status = 0;

            if (at + 2 + wide > len) {
                r[10] = 0xFF;
                r[11] = 0xA2;
                return rn;
            }
            order = in[at] & 0x0F;
            number = in[at + 1] | (wide ? in[at + 2] << 8 : 0);
            at += 2 + wide;
            code = order < nsvc ? in[11 + 2 * order] | in[12 + 2 * order] << 8
                                : -1;
            where[i] = -1;
            if (code < 0) {
                status = 0xA3;          /* no such place in the list */
            } else if (fs_node(c, from, to, code, &type) < 0 ||
                       type != REC_FS_SERVICE) {
                status = 0xA6;          /* no such service */
            } else if (!(code & 0x01) || (write && (code & 0x3F) != 0x09)) {
                status = 0xA5;          /* not without a key; not written */
            } else {
                where[i] = fs_block(c, from, to, code, number);
                if (where[i] < 0) {
                    status = 0xA8;      /* an illegal block number */
                }
            }
            if (status) {
                r[10] = 1 << (i % 8);   /* (which block of the list) */
                r[11] = status;
                return rn;
            }
        }
        if (at + (write ? 16 * nblk : 0) != len) {
            r[10] = 0xFF;
            r[11] = 0xA2;
            return rn;
        }
        if (write) {
            for (int i = 0; i < nblk; i++) {
                memcpy(&c->app[where[i]], &in[at + 16 * i], 16);
            }
            return rn;
        }
        r[rn++] = nblk;
        for (int i = 0; i < nblk; i++) {
            memcpy(&r[rn], &c->app[where[i]], 16);
            rn += 16;
        }
        return rn;
    }
    return 0;
}

static int felica_frame(NfcCard *c, const uint8_t *in, int n, uint8_t *out,
                        int max)
{
    uint8_t r[280];
    int len = n >= 4 ? in[0] : 0, rn = 0, count, at;
    const uint8_t *list;
    uint16_t crc;

    if (len < 2 || len + 2 != n || felica_crc(in, n) != 0) {
        return 0;
    }
    if (c->app_len) {                   /* a FeliCa Standard card */
        rn = fs_command(c, in, len, r);
        if (rn <= 0) {
            return 0;
        }
        goto answer;
    }
    switch (in[1]) {
    case 0x00:                          /* Polling: a system code, or any */
        if (len != 6 || ((in[2] != 0xFF || in[3] != 0xFF) &&
                         (in[2] != 0x88 || in[3] != 0xB4))) {
            return 0;
        }
        r[1] = 0x01;
        memcpy(&r[2], c->uid, 8);
        memcpy(&r[10], c->pmm, 8);
        rn = 18;
        if (in[4] == 0x01) {            /* the system code asked for */
            r[rn++] = 0x88;
            r[rn++] = 0xB4;
        }
        break;
    case 0x06:                          /* Read Without Encryption */
    case 0x08:                          /* Write Without Encryption */
        /* IDm, one service and its code, the blocks: two bytes each */
        if (len < 16 || memcmp(&in[2], c->uid, 8) || in[10] != 1) {
            return 0;
        }
        count = in[13];
        list = &in[14];
        r[1] = in[1] + 1;
        memcpy(&r[2], c->uid, 8);
        r[10] = r[11] = 0;
        rn = 12;
        if (count < 1 || count > 4 || 14 + 2 * count +
            (in[1] == 0x08 ? 16 * count : 0) != len) {
            r[10] = 0xFF;               /* the block list as it is: no such */
            r[11] = 0xA2;
            break;
        }
        for (int i = 0; i < count && !r[10]; i++) {
            at = (list[2 * i] & 0x80) ? felica_block(c, list[2 * i + 1]) : -1;
            if (at < 0) {
                r[10] = 1 << i;         /* (which block of the list) */
                r[11] = 0xA8;           /* an illegal block number */
            } else if (in[1] == 0x06 && c->mem[at * 18]) {
                r[10] = c->mem[at * 18];        /* as the dump had it */
                r[11] = c->mem[at * 18 + 1];
            } else if (in[1] == 0x08 && list[2 * i + 1] > 0x0E &&
                       list[2 * i + 1] != 0x80 && list[2 * i + 1] != 0x92 &&
                       list[2 * i + 1] != 0x91) {
                r[10] = 1 << i;         /* not one that is written so */
                r[11] = 0xA8;
            }
        }
        if (r[10]) {
            break;
        }
        if (in[1] == 0x08) {
            for (int i = 0; i < count; i++) {
                at = felica_block(c, list[2 * i + 1]);
                memcpy(&c->mem[at * 18 + 2], &list[2 * count + 16 * i], 16);
            }
            break;
        }
        r[rn++] = count;
        for (int i = 0; i < count; i++) {
            at = felica_block(c, list[2 * i + 1]);
            memcpy(&r[rn], &c->mem[at * 18 + 2], 16);
            rn += 16;
        }
        break;
    default:
        return 0;                       /* a Lite-S has no other */
    }
answer:
    r[0] = rn;
    crc = felica_crc(r, rn);
    r[rn++] = crc >> 8;
    r[rn++] = crc & 0xFF;
    if (rn > max) {
        return 0;
    }
    memcpy(out, r, rn);
    return rn;
}

/* -------------------------------------------------------------- ISO15693 */

/* CRC-16 as ISO15693 has it (ISO/IEC 13239: reflected, from 0xFFFF,
 * inverted, the low byte first) */
static uint16_t v_crc(const uint8_t *d, int n)
{
    uint16_t crc = 0xFFFF;

    for (int i = 0; i < n; i++) {
        crc ^= d[i];
        for (int b = 0; b < 8; b++) {
            crc = crc & 1 ? crc >> 1 ^ 0x8408 : crc >> 1;
        }
    }
    return ~crc;
}

static int v_blocks(NfcCard *c)
{
    return c->v_bsize ? c->mem_len / c->v_bsize : 0;
}

/* a request's answer: its flags (0, or 1 and an error's code), its data */
static int v_command(NfcCard *c, const uint8_t *in, int n, uint8_t *r)
{
    uint8_t flags = in[0], cmd = in[1];
    bool inventory = flags & 0x04, option = flags & 0x40;
    int at = 2, rn = 1, blocks = v_blocks(c), first, count;
    uint8_t uid[8];

    for (int i = 0; i < 8; i++) {
        uid[i] = c->uid[7 - i];         /* on the air: the low byte first */
    }
    r[0] = 0;
#define V_ERROR(code) do { r[0] = 1; r[1] = (code); return 2; } while (0)
    if (cmd >= 0xA0) {                  /* a manufacturer's: its code (NXP) */
        if (!c->slix || at >= n || in[at++] != 0x04) {
            return 0;
        }
    }
    if (c->slix_privacy && cmd != 0xB2 && cmd != 0xB3) {
        return 0;                       /* in privacy mode: silence */
    }
    if (inventory) {
        int mask;

        if (cmd != 0x01 || c->v_quiet) {
            return 0;
        }
        if (flags & 0x10) {             /* an AFI to match */
            if (at >= n || (in[at] && in[at] != c->v_afi)) {
                return 0;
            }
            at++;
        }
        mask = at < n ? in[at++] : 0;
        if (mask > 64 || at + (mask + 7) / 8 > n) {
            return 0;
        }
        for (int i = 0; i < mask; i++) {
            if (((in[at + i / 8] ^ uid[i / 8]) >> (i % 8)) & 1) {
                return 0;
            }
        }
        r[rn++] = c->v_dsfid;
        memcpy(&r[rn], uid, 8);
        return rn + 8;
    }
    if (flags & 0x20) {                 /* addressed: to this UID alone */
        if (at + 8 > n || memcmp(&in[at], uid, 8)) {
            return 0;
        }
        at += 8;
    } else if (flags & 0x10) {          /* to the selected one */
        if (!c->v_selected) {
            return 0;
        }
    } else if (c->v_quiet) {
        return 0;
    }
    switch (cmd) {
    case 0x02:                          /* Stay quiet */
        c->v_quiet = true;
        return 0;
    case 0x25:                          /* Select */
        c->v_selected = true;
        c->v_quiet = false;
        break;
    case 0x26:                          /* Reset to ready */
        c->v_selected = c->v_quiet = false;
        break;
    case 0x20:                          /* Read single block */
    case 0x23:                          /* Read multiple blocks */
        if (at >= n || (cmd == 0x23 && at + 1 >= n)) {
            V_ERROR(0x02);
        }
        first = in[at];
        count = cmd == 0x23 ? in[at + 1] + 1 : 1;
        if (first >= blocks || count > blocks - first ||
            count * (c->v_bsize + option) > 120) {
            V_ERROR(0x10);              /* no such block */
        }
        for (int b = first; b < first + count; b++) {
            if (option) {
                r[rn++] = c->v_sec[b];
            }
            memcpy(&r[rn], &c->mem[b * c->v_bsize], c->v_bsize);
            rn += c->v_bsize;
        }
        break;
    case 0x21:                          /* Write single block */
        if (at + 1 + c->v_bsize > n) {
            V_ERROR(0x02);
        }
        first = in[at];
        if (first >= blocks) {
            V_ERROR(0x10);
        }
        if (c->v_sec[first] & 1) {
            V_ERROR(0x12);              /* the block is locked */
        }
        memcpy(&c->mem[first * c->v_bsize], &in[at + 1], c->v_bsize);
        break;
    case 0x22:                          /* Lock block */
        if (at >= n || in[at] >= blocks) {
            V_ERROR(0x10);
        }
        if (c->v_sec[in[at]] & 1) {
            V_ERROR(0x11);              /* locked already */
        }
        c->v_sec[in[at]] |= 1;
        break;
    case 0x27:                          /* Write AFI */
    case 0x29:                          /* Write DSFID */
        if (at >= n) {
            V_ERROR(0x02);
        }
        if (c->v_lock & (cmd == 0x27 ? V_LOCK_AFI : V_LOCK_DSFID)) {
            V_ERROR(0x12);
        }
        *(cmd == 0x27 ? &c->v_afi : &c->v_dsfid) = in[at];
        break;
    case 0x28:                          /* Lock AFI */
    case 0x2A:                          /* Lock DSFID */
        if (c->v_lock & (cmd == 0x28 ? V_LOCK_AFI : V_LOCK_DSFID)) {
            V_ERROR(0x11);
        }
        c->v_lock |= cmd == 0x28 ? V_LOCK_AFI : V_LOCK_DSFID;
        break;
    case 0x2B:                          /* Get system information */
        r[rn++] = 0x0F;                 /* DSFID, AFI, memory, IC reference */
        memcpy(&r[rn], uid, 8);
        rn += 8;
        r[rn++] = c->v_dsfid;
        r[rn++] = c->v_afi;
        r[rn++] = blocks - 1;
        r[rn++] = c->v_bsize - 1;
        r[rn++] = c->v_icref;
        break;
    case 0x2C:                          /* Get multiple block security status */
        if (at + 1 >= n) {
            V_ERROR(0x02);
        }
        first = in[at];
        count = in[at + 1] + 1;
        if (first >= blocks || count > blocks - first || count > 120) {
            V_ERROR(0x10);
        }
        memcpy(&r[rn], &c->v_sec[first], count);
        rn += count;
        break;
    /* ---- NXP's (SLIX, SLIX2) */
    case 0xA2:                          /* Set EAS */
    case 0xA3:                          /* Reset EAS */
        if (c->v_lock & V_LOCK_EAS) {
            V_ERROR(0x12);
        }
        c->slix_eas = cmd == 0xA2;
        break;
    case 0xA4:                          /* Lock EAS */
        c->v_lock |= V_LOCK_EAS;
        break;
    case 0xAB:                          /* Get NXP system information */
        r[rn++] = c->slix_pp[0];
        r[rn++] = c->slix_pp[1];
        r[rn++] = (c->v_lock & V_LOCK_AFI ? 1 : 0) |
                  (c->v_lock & V_LOCK_EAS ? 2 : 0) |
                  (c->v_lock & V_LOCK_DSFID ? 4 : 0) |
                  (c->v_lock & V_LOCK_PPL ? 8 : 0);
        memset(&r[rn], 0, 4);           /* feature flags */
        rn += 4;
        break;
    case 0xB2:                          /* Get random number */
        qemu_guest_getrandom_nofail(c->slix_rnd, 2);
        memcpy(&r[rn], c->slix_rnd, 2);
        rn += 2;
        break;
    case 0xB3: {                        /* Set password: it XOR the number */
        static const uint8_t ids[5] = { 0x01, 0x02, 0x04, 0x08, 0x10 };
        int which = -1;

        if (at + 5 > n) {
            V_ERROR(0x02);
        }
        for (int i = 0; i < 5; i++) {
            which = in[at] == ids[i] ? i : which;
        }
        if (which < 0) {
            V_ERROR(0x0F);
        }
        for (int i = 0; i < 4 && !c->slix_accept_all; i++) {
            if ((in[at + 1 + i] ^ c->slix_rnd[i % 2]) != c->slix_pwd[which][i]) {
                return 0;               /* a wrong one: silence */
            }
        }
        if (which == 2) {
            c->slix_privacy = false;    /* the privacy password ends it */
        }
        break;
    }
    case 0xBA:                          /* Enable privacy */
        c->slix_privacy = true;
        break;
    case 0xBD:                          /* Read signature */
        memcpy(&r[rn], c->sig, 32);
        rn += 32;
        break;
    default:
        V_ERROR(0x01);                  /* not supported */
    }
#undef V_ERROR
    return rn;
}

static int v_frame(NfcCard *c, const uint8_t *in, int n, uint8_t *out, int max)
{
    uint8_t r[140];
    uint16_t crc;
    int rn;

    if (n < 4 || v_crc(in, n - 2) != (in[n - 2] | in[n - 1] << 8)) {
        return 0;
    }
    rn = v_command(c, in, n - 2, r);
    if (rn <= 0 || rn + 2 > max) {
        return 0;
    }
    crc = v_crc(r, rn);
    memcpy(out, r, rn);
    out[rn] = crc & 0xFF;
    out[rn + 1] = crc >> 8;
    return rn + 2;
}

/* ------------------------------------------------ ISO14443B and ST25TB */

/* an answer's bytes and their CRC_B (ISO/IEC 13239, as ISO15693's) */
static int b_answer(const uint8_t *r, int rn, uint8_t *out, int max)
{
    uint16_t crc = v_crc(r, rn);

    if (rn + 2 > max) {
        return 0;
    }
    memcpy(out, r, rn);
    out[rn] = crc & 0xFF;
    out[rn + 1] = crc >> 8;
    return rn + 2;
}

static int b_frame(NfcCard *c, const uint8_t *in, int n, uint8_t *out, int max)
{
    uint8_t r[300];
    bool halt;
    int rn;

    if (n < 3 || v_crc(in, n - 2) != (in[n - 2] | in[n - 1] << 8)) {
        return 0;
    }
    n -= 2;
    if (in[0] == 0x05 && n == 3) {      /* REQB / WUPB (bit 3 of PARAM) */
        if (c->state == ST_ACTIVE || (c->state == ST_HALT && !(in[2] & 0x08)) ||
            (in[1] && in[1] != c->b_app[0])) {      /* (an AFI to match) */
            return 0;
        }
        c->state = ST_READY;
        c->dep = false;
        r[0] = 0x50;                    /* ATQB */
        memcpy(&r[1], c->uid, 4);
        memcpy(&r[5], c->b_app, 4);
        memcpy(&r[9], c->b_info, 3);
        return b_answer(r, 12, out, max);
    }
    if (in[0] == 0x1D && n >= 9 && c->state == ST_READY &&
        !memcmp(&in[1], c->uid, 4)) {   /* ATTRIB: selected, with a CID */
        c->state = ST_ACTIVE;
        c->dep = c->b_info[1] & 0x01;   /* ISO14443-4, if it says so */
        c->dep_in_len = 0;
        r[0] = in[8] & 0x0F;            /* MBLI 0, the CID */
        return b_answer(r, 1, out, max);
    }
    if (in[0] == 0x50 && n == 5 && !memcmp(&in[1], c->uid, 4) &&
        c->state != ST_IDLE) {          /* HLTB */
        card_reset(c, ST_HALT);
        r[0] = 0x00;
        return b_answer(r, 1, out, max);
    }
    if (c->state != ST_ACTIVE || !c->dep) {
        return 0;
    }
    rn = iso4_block(c, in, n, r, &halt);
    if (rn <= 0) {
        return 0;
    }
    rn = b_answer(r, rn, out, max);
    if (halt) {
        card_reset(c, ST_HALT);
    }
    return rn;
}

static uint32_t tb_le32(const uint8_t *p)
{
    return p[0] | p[1] << 8 | p[2] << 16 | (uint32_t)p[3] << 24;
}

/* ST's ST25TB / SRI / SRT: ISO14443B's air and CRC, commands of its own */
static int tb_frame(NfcCard *c, const uint8_t *in, int n, uint8_t *out,
                    int max)
{
    uint8_t r[8];
    int blocks = c->mem_len / 4;

    if (n < 3 || v_crc(in, n - 2) != (in[n - 2] | in[n - 1] << 8)) {
        return 0;
    }
    n -= 2;
    if (in[0] == 0x06 && n == 2 && (in[1] == 0x00 || in[1] == 0x04)) {
        /* Initiate (and Pcall16): the chip takes an ID for the session */
        if (c->state == ST_ACTIVE || c->state == ST_HALT) {
            return 0;
        }
        qemu_guest_getrandom_nofail(&c->tb_id, 1);
        c->state = ST_READY;
        return b_answer(&c->tb_id, 1, out, max);
    }
    if (in[0] == 0x0E && n == 2) {      /* Select: by the ID */
        if (c->state == ST_READY && in[1] == c->tb_id) {
            c->state = ST_ACTIVE;
            return b_answer(&c->tb_id, 1, out, max);
        }
        if (c->state == ST_ACTIVE && in[1] != c->tb_id) {
            c->state = ST_READY;        /* another is meant */
        }
        return 0;
    }
    if (c->state != ST_ACTIVE) {
        return 0;
    }
    switch (in[0]) {
    case 0x0B:                          /* Get_UID: the low byte first */
        if (n != 1) {
            return 0;
        }
        for (int i = 0; i < 8; i++) {
            r[i] = c->uid[7 - i];
        }
        return b_answer(r, 8, out, max);
    case 0x08:                          /* Read_Block */
        if (n != 2 || (in[1] != 0xFF && in[1] >= blocks)) {
            return 0;
        }
        return b_answer(in[1] == 0xFF ? c->tb_otp : &c->mem[in[1] * 4], 4,
                        out, max);
    case 0x09:                          /* Write_Block: no answer */
        if (n != 6) {
            return 0;
        }
        if (in[1] == 0xFF) {            /* the system OTP block: 1 to 0 */
            for (int i = 0; i < 4; i++) {
                c->tb_otp[i] &= in[2 + i];
            }
        } else if (in[1] < 5) {         /* the resettable OTP bits: 1 to 0 */
            for (int i = 0; i < 4; i++) {
                c->mem[in[1] * 4 + i] &= in[2 + i];
            }
        } else if (in[1] < 7) {         /* the counters: only down */
            if (tb_le32(&in[2]) < tb_le32(&c->mem[in[1] * 4])) {
                memcpy(&c->mem[in[1] * 4], &in[2], 4);
            }
        } else if (in[1] < blocks) {
            /* blocks 7 to 15 behind the lock bits of the system block
             * (b24: 7 and 8, b25: 9 ... b31: 15; 0 is locked) */
            int bit = in[1] < 9 ? 0 : in[1] - 8;

            if (in[1] > 15 || (c->tb_otp[3] >> bit) & 1) {
                memcpy(&c->mem[in[1] * 4], &in[2], 4);
            }
        }
        return 0;
    case 0x0C:                          /* Reset_to_Inventory */
        c->state = ST_IDLE;
        return 0;
    case 0x0F:                          /* Completion: done with */
        c->state = ST_HALT;
        return 0;
    }
    return 0;
}

/* A frame of a technology whose frames are bytes (ISO14443B, FeliCa,
 * ISO15693), to a card of that technology -> its answer's bytes (0: none) */
int nfc_card_bytes(NfcCard *c, int tech, const uint8_t *in, int n,
                   uint8_t *out, int max)
{
    if (c->state == ST_OFF) {
        return 0;
    }
    if (tech == NFC_TECH_F && c->kind == KIND_FELICA) {
        return felica_frame(c, in, n, out, max);
    }
    if (tech == NFC_TECH_V && c->kind == KIND_V) {
        return v_frame(c, in, n, out, max);
    }
    if (tech == NFC_TECH_B && c->kind == KIND_B) {
        return b_frame(c, in, n, out, max);
    }
    if (tech == NFC_TECH_B && c->kind == KIND_ST25TB) {
        return tb_frame(c, in, n, out, max);
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
            } else if (!strcmp(val, "iso4")) {
                c->kind = KIND_ISO4;
            } else if (!strcmp(val, "felica")) {
                c->kind = KIND_FELICA;
            } else if (!strcmp(val, "iso15693")) {
                c->kind = KIND_V;
            } else if (!strcmp(val, "iso14443b")) {
                c->kind = KIND_B;
            } else if (!strcmp(val, "st25tb")) {
                c->kind = KIND_ST25TB;
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
        if (!strcmp(line, "app")) {
            if (!strcmp(g_strstrip(val), "desfire")) {
                c->app_kind = APP_DESFIRE;
            } else if (!strcmp(val, "mfplus")) {
                c->app_kind = APP_MFPLUS;
            } else if (!strcmp(val, "iso7816")) {
                c->app_kind = APP_7816;
            } else if (!strcmp(val, "none")) {
                c->app_kind = APP_NONE;
            } else {
                error_setg(errp, "nfc card: no application '%s'", val);
                return NULL;
            }
            continue;
        }
        if (!strcmp(line, "rec")) {     /* a record: its type, its payload */
            g_autofree uint8_t *b = g_malloc(APP_MAX);
            int got = parse_hex(val, b, APP_MAX);

            if (got < 1 || c->app_len + 2 + got > APP_MAX) {
                error_setg(errp, "nfc card: a record that is none, or more "
                           "of them than %d bytes", APP_MAX);
                return NULL;
            }
            c->app[c->app_len] = b[0];
            c->app[c->app_len + 1] = (got - 1) & 0xFF;
            c->app[c->app_len + 2] = (got - 1) >> 8;
            memcpy(&c->app[c->app_len + 3], b + 1, got - 1);
            c->app_len += 2 + got;
            continue;
        }
        if (!strcmp(line, "uid")) {
            n = c->uid_len = parse_hex(val, c->uid, 10);
            if (n != 4 && n != 7 && n != 8 && n != 10) {
                error_setg(errp, "nfc card: a UID is 4, 7 or 10 bytes (8: a "
                           "FeliCa card's IDm, an ISO15693 card's UID)");
                return NULL;
            }
        } else if (!strcmp(line, "pmm")) {
            n = parse_hex(val, c->pmm, 8) == 8 ? 8 : -1;
        } else if (!strcmp(line, "appdata")) {
            n = parse_hex(val, c->b_app, 4) == 4 ? 4 : -1;
        } else if (!strcmp(line, "protinfo")) {
            n = parse_hex(val, c->b_info, 3) == 3 ? 3 : -1;
        } else if (!strcmp(line, "otp")) {
            n = parse_hex(val, c->tb_otp, 4) == 4 ? 4 : -1;
        } else if (!strcmp(line, "dsfid")) {
            n = parse_hex(val, &c->v_dsfid, 1);
        } else if (!strcmp(line, "afi")) {
            n = parse_hex(val, &c->v_afi, 1);
        } else if (!strcmp(line, "icref")) {
            n = parse_hex(val, &c->v_icref, 1);
        } else if (!strcmp(line, "blocksize")) {
            n = parse_hex(val, &c->v_bsize, 1);
        } else if (!strcmp(line, "lock")) {
            n = parse_hex(val, &c->v_lock, 1);
        } else if (!strcmp(line, "security")) {
            n = parse_hex(val, c->v_sec, sizeof(c->v_sec));
        } else if (!strcmp(line, "ulc")) {
            c->ulc = strtoul(val, NULL, 0) != 0;
        } else if (!strcmp(line, "slix")) {
            c->slix = strtoul(val, NULL, 0) != 0;
        } else if (!strcmp(line, "privacy")) {
            c->slix_privacy = strtoul(val, NULL, 0) != 0;
        } else if (!strcmp(line, "accept-all")) {
            c->slix_accept_all = strtoul(val, NULL, 0) != 0;
        } else if (!strcmp(line, "protection")) {
            n = parse_hex(val, c->slix_pp, 2) == 2 ? 2 : -1;
        } else if (!strncmp(line, "pwd-", 4)) {
            static const char *const names[5] = { "read", "write", "privacy",
                                                  "destroy", "eas" };
            n = -1;
            for (int k = 0; k < 5; k++) {
                if (!strcmp(line + 4, names[k])) {
                    n = parse_hex(val, c->slix_pwd[k], 4) == 4 ? 4 : -1;
                }
            }
        } else if (!strcmp(line, "atqa")) {
            n = parse_hex(val, c->atqa, 2) == 2 ? 2 : -1;
        } else if (!strcmp(line, "sak")) {
            n = parse_hex(val, &c->sak, 1);
        } else if (!strcmp(line, "ats")) {
            n = c->ats_len = parse_hex(val, c->ats, sizeof(c->ats));
            if (n < 1 || c->ats[0] != n) {
                error_setg(errp, "nfc card: an ATS begins with its length");
                return NULL;
            }
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
    if (c->ulc && (c->kind != KIND_T2 || c->mem_len != 48 * 4)) {
        error_setg(errp, "nfc card: a MIFARE Ultralight C is a Type 2 tag of "
                   "48 pages");
        return NULL;
    }
    if (c->kind == KIND_MFC && c->mem_len != 20 * 16 && c->mem_len != 64 * 16 &&
        c->mem_len != 256 * 16) {
        error_setg(errp, "nfc card: MIFARE Classic Mini, 1K or 4K: 20, 64 or "
                   "256 blocks of 16 bytes (got %d bytes)", c->mem_len);
        return NULL;
    }
    if ((c->kind == KIND_FELICA || c->kind == KIND_V ||
         c->kind == KIND_ST25TB) != (c->uid_len == 8)) {
        error_setg(errp, "nfc card: a FeliCa card's IDm and an ISO15693 or "
                   "ST25TB card's UID are 8 bytes, and no other card's is");
        return NULL;
    }
    if (c->kind == KIND_B && c->uid_len != 4) {
        error_setg(errp, "nfc card: an ISO14443B card's PUPI is 4 bytes");
        return NULL;
    }
    if (c->kind == KIND_ST25TB && (c->mem_len < 16 * 4 || c->mem_len > 256 * 4
                                   || (c->mem_len & 3))) {
        error_setg(errp, "nfc card: an ST25TB has 16 to 255 blocks of 4 "
                   "bytes");
        return NULL;
    }
    if (c->kind == KIND_FELICA && c->app_len) {
        if (c->mem_len || fs_system(c, -1, NULL, NULL) < 1 ||
            fs_system(c, -1, NULL, NULL) > 16 || c->app[0] != REC_FS_SYSTEM) {
            error_setg(errp, "nfc card: a FeliCa Standard card has 1 to 16 "
                       "systems (rec 30 ...), each with its areas, services "
                       "and blocks after it, and no blocks of a Lite-S (mem)");
            return NULL;
        }
    } else if (c->kind == KIND_FELICA && c->mem_len != 28 * 18) {
        error_setg(errp, "nfc card: a FeliCa Lite-S has 28 blocks, 18 bytes "
                   "each here (two of status, 16 of data): got %d bytes",
                   c->mem_len);
        return NULL;
    }
    if (c->kind == KIND_V && (c->v_bsize < 1 || c->v_bsize > 32 ||
                              c->mem_len < c->v_bsize ||
                              c->mem_len / c->v_bsize > 256)) {
        error_setg(errp, "nfc card: an ISO15693 card has 1 to 256 blocks of "
                   "1 to 32 bytes (blocksize, mem)");
        return NULL;
    }
    if (c->kind == KIND_ISO4) {
        static const uint8_t ats[] = { 0x06, 0x75, 0x77, 0x81, 0x02, 0x80 };

        if (!(c->sak & 0x20)) {
            error_setg(errp, "nfc card: an ISO14443-4 card's SAK says so "
                       "(bit 0x20); this one's is %02X", c->sak);
            return NULL;
        }
        if (!c->ats_len) {              /* (a DESFire EV1's) */
            memcpy(c->ats, ats, c->ats_len = sizeof(ats));
        }
    }
    if (c->kind == KIND_T2 || c->kind == KIND_MFC) {
        c->mem_len &= ~3;
    }
    card_reset(c, ST_OFF);
    return g_steal_pointer(&c);
}

void nfc_card_free(NfcCard *c)
{
    g_free(c);
}

/*
 * ---- a saved state
 *
 * The card as it is: what was written to it, its counters, where it is in
 * its activation and, for MIFARE Classic, in its Crypto1 session.  A talk
 * between the firmware and the card goes on after a load.
 */
NfcCard *nfc_card_blank(void)
{
    return g_new0(NfcCard, 1);
}

/* what the card indexes with: within what it has */
static int card_post_load(void *opaque, int version_id)
{
    NfcCard *c = opaque;
    bool mfc = c->kind == KIND_MFC, iso4 = c->kind == KIND_ISO4;
    bool bytes = c->kind == KIND_FELICA || c->kind == KIND_V ||
                 c->kind == KIND_ST25TB;
    bool fs = c->kind == KIND_FELICA && c->app_len > 0;

    iso4 = iso4 || c->kind == KIND_B || fs;     /* (no pages either) */
    if (c->kind < KIND_T2 || c->kind > KIND_ST25TB ||
        (c->uid_len != 4 && c->uid_len != 7 && c->uid_len != 10 &&
         !(bytes && c->uid_len == 8)) ||
        c->state < ST_OFF || c->state > ST_HALT ||
        c->level < 0 || c->level > 2 ||
        c->mem_len < (iso4 ? 0 : 16) || c->mem_len > MEM_MAX ||
        (!bytes && (c->mem_len & 3)) ||
        (c->kind == KIND_FELICA && c->mem_len != (fs ? 0 : 28 * 18)) ||
        (c->kind == KIND_V && (c->v_bsize < 1 || c->v_bsize > 32 ||
                               c->mem_len / c->v_bsize > 256)) ||
        (mfc && c->mem_len != 20 * 16 && c->mem_len != 64 * 16 &&
         c->mem_len != 256 * 16) ||
        c->comp_write < -1 || c->comp_write >= MAX(t2_pages(c), 0) ||
        c->write_block < -1 || c->write_block >= MAX(mfc_blocks(c), 0) ||
        c->mfc < MFC_NONE || c->mfc > MFC_ON ||
        c->ats_len < 0 || c->ats_len > sizeof(c->ats) ||
        c->dep_in_len < 0 || c->dep_in_len > sizeof(c->dep_in) ||
        c->app_kind < APP_NONE || c->app_kind > APP_7816 ||
        c->iso_df < -1 || c->iso_df > c->app_len ||
        c->iso_ef < -1 || c->iso_ef > c->app_len ||
        c->app_len < 0 || c->app_len > APP_MAX ||
        c->df_more < DF_NONE || c->df_more > DF_DATA ||
        c->df_at < 0 || c->df_left < 0 || c->df_at > c->app_len ||
        c->df_left > c->app_len - c->df_at ||
        (c->df_more == DF_VERSION_2 && c->df_at + 21 > c->app_len) ||
        (c->df_more == DF_VERSION_3 && c->df_at + 14 > c->app_len) ||
        (c->ulc && (c->kind != KIND_T2 || c->mem_len != 48 * 4))) {
        return -EINVAL;
    }
    return 0;
}

const VMStateDescription vmstate_nfc_card = {
    .name = "nfc-card",
    .version_id = 4,                    /* (4: ISO 7816-4's selection) */
    .minimum_version_id = 4,
    .post_load = card_post_load,
    .fields = (const VMStateField[]) {
        VMSTATE_INT32(kind, NfcCard),
        VMSTATE_UINT8_ARRAY(uid, NfcCard, 10),
        VMSTATE_INT32(uid_len, NfcCard),
        VMSTATE_UINT8_ARRAY(atqa, NfcCard, 2),
        VMSTATE_UINT8(sak, NfcCard),
        VMSTATE_INT32(state, NfcCard),
        VMSTATE_INT32(level, NfcCard),
        VMSTATE_UINT8_ARRAY(mem, NfcCard, MEM_MAX),
        VMSTATE_INT32(mem_len, NfcCard),
        VMSTATE_BOOL(has_version, NfcCard),
        VMSTATE_BOOL(has_sig, NfcCard),
        VMSTATE_UINT8_ARRAY(version, NfcCard, 8),
        VMSTATE_UINT8_ARRAY(sig, NfcCard, 32),
        VMSTATE_UINT32_ARRAY(counter, NfcCard, 3),
        VMSTATE_UINT8_ARRAY(tearing, NfcCard, 3),
        VMSTATE_UINT8_ARRAY(pwd, NfcCard, 4),
        VMSTATE_UINT8_ARRAY(pack, NfcCard, 2),
        VMSTATE_BOOL(authed, NfcCard),
        VMSTATE_INT32(comp_write, NfcCard),
        VMSTATE_INT32(mfc, NfcCard),
        VMSTATE_UINT64(lfsr, NfcCard),
        VMSTATE_UINT8_ARRAY(nt, NfcCard, 4),
        VMSTATE_INT32(sector, NfcCard),
        VMSTATE_BOOL(key_b, NfcCard),
        VMSTATE_INT32(write_block, NfcCard),
        VMSTATE_INT64(power_ns, NfcCard),
        VMSTATE_UINT8_ARRAY(ats, NfcCard, 20),
        VMSTATE_INT32(ats_len, NfcCard),
        VMSTATE_BOOL(dep, NfcCard),
        VMSTATE_UINT8_ARRAY(dep_in, NfcCard, 600),
        VMSTATE_INT32(dep_in_len, NfcCard),
        VMSTATE_INT32(app_kind, NfcCard),
        VMSTATE_UINT8_ARRAY(app, NfcCard, APP_MAX),
        VMSTATE_INT32(app_len, NfcCard),
        VMSTATE_UINT8_ARRAY(df_aid, NfcCard, 3),
        VMSTATE_INT32(df_more, NfcCard),
        VMSTATE_INT32(df_at, NfcCard),
        VMSTATE_INT32(df_left, NfcCard),
        VMSTATE_INT32(iso_df, NfcCard),
        VMSTATE_INT32(iso_ef, NfcCard),
        VMSTATE_UINT8_ARRAY(pmm, NfcCard, 8),
        VMSTATE_UINT8(v_dsfid, NfcCard),
        VMSTATE_UINT8(v_afi, NfcCard),
        VMSTATE_UINT8(v_icref, NfcCard),
        VMSTATE_UINT8(v_bsize, NfcCard),
        VMSTATE_UINT8(v_lock, NfcCard),
        VMSTATE_UINT8_ARRAY(v_sec, NfcCard, 256),
        VMSTATE_BOOL(v_quiet, NfcCard),
        VMSTATE_BOOL(v_selected, NfcCard),
        VMSTATE_BOOL(slix, NfcCard),
        VMSTATE_UINT8_2DARRAY(slix_pwd, NfcCard, 5, 4),
        VMSTATE_UINT8_ARRAY(slix_pp, NfcCard, 2),
        VMSTATE_UINT8_ARRAY(slix_rnd, NfcCard, 2),
        VMSTATE_BOOL(slix_privacy, NfcCard),
        VMSTATE_BOOL(slix_accept_all, NfcCard),
        VMSTATE_BOOL(slix_eas, NfcCard),
        VMSTATE_BOOL(ulc, NfcCard),
        VMSTATE_BOOL(ulc_auth, NfcCard),
        VMSTATE_UINT8_ARRAY(ulc_rndb, NfcCard, 8),
        VMSTATE_UINT8_ARRAY(ulc_iv, NfcCard, 8),
        VMSTATE_UINT8_ARRAY(b_app, NfcCard, 4),
        VMSTATE_UINT8_ARRAY(b_info, NfcCard, 3),
        VMSTATE_UINT8_ARRAY(tb_otp, NfcCard, 4),
        VMSTATE_UINT8(tb_id, NfcCard),
        VMSTATE_END_OF_LIST()
    }
};
