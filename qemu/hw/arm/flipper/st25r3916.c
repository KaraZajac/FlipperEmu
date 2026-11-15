/*
 * ST25R3916 NFC reader IC (the Flipper Zero's 13.56 MHz front end), modelled
 * frame by frame
 *
 * SPI: register space A and B (0xFB prefix), test registers (0xFC, ignored),
 * FIFO load (0x80) and read (0x9F), passive-target memory (0xA0-0xBF) and
 * direct commands (0xC0-0xFF).  The 512-byte FIFO keeps its byte count and
 * the bits of an incomplete last byte (FIFO_STATUS1/2), and the four
 * interrupt registers (main, timer/NFC, error/wake-up, target) clear on
 * read and drive the IRQ pin (PA2) while any unmasked bit is set.
 *
 * Polling (the firmware as the reader): TRANSMIT_WITH/WITHOUT_CRC,
 * TRANSMIT_REQA/WUPA turn the FIFO (NUM_TX_BYTES bits) into the frame that
 * goes on the air.  That is a bit stream, least significant bit first, with
 * an odd parity bit after every full byte unless ISO14443A_NFC.no_tx_par
 * says the FIFO already holds them (MIFARE Classic's encrypted parity).  The
 * card in the field (nfc_card.c) answers with a bit stream of its own, which
 * is checked and decoded back into the FIFO, raw when no_rx_par is set.
 * Times follow 106 kbit/s (128/fc a bit), except that the answer starts
 * 40 us after the end of transmission, well inside the firmware's frame
 * waiting time (1172+500 fc for activation), so that the RX start interrupt
 * never races the MCU timer that would declare a timeout.
 *
 * The card: qom-set /machine/nfc card file:PATH (scripts/fe_nfc.py writes
 * PATH from a Flipper .nfc file), "none" to take it away.
 *
 * Listening (the firmware emulates a card): a virtual reader can be put in
 * front of the Flipper (qom-set /machine/nfc reader scan|ntag).  Its field
 * raises I_eon; then, as the real chip does on its own from the passive-
 * target memory the firmware loaded (UID, ATQA, SAK per cascade level),
 * anticollision and select complete and I_wu_a fires.  After that every
 * reader command arrives in the FIFO with I_rxs/I_rxe, and what the
 * firmware TRANSMITs is the answer.  "ntag" reads a Type 2 tag (version,
 * signature, every page, the NFC counter).  When done the field goes off
 * (I_eof) and what the reader saw is written as a Flipper .nfc file to
 * "tx-dir" (the launcher: devices/NAME/nfc-tx/); "tx" names the latest.
 * An answer whose parity bits are not the chip's to make (MIFARE Classic's
 * encrypted ones) the firmware sends in transparent mode: the modulator
 * follows a pin of the MCU, MOSI, until the chip is selected again.  The
 * board reports that pin's edges (st25r3916_mod), and they are decoded
 * back into the answer's bits when the mode ends.
 * A reader can also be a program on the link: "reader on", "reader
 * activate", "reader send" (docs/RADIO_LINK.md).
 *
 * "record" (a file's whole path, or off) writes every frame on the air and
 * its answer as a line of text: the Flipper's as a reader, and a reader's
 * to the Flipper as a card.
 *
 * A card can also be on the "link" chardev (docs/RADIO_LINK.md): a program
 * (scripts/fe_nfc_link.py), or real hardware's antenna.  With no card set
 * here, each frame the firmware sends goes out on it ("poll", data bits
 * and the parity of each byte), and its answer ("answer", or "silence")
 * comes back as the card's.  The machine is stopped meanwhile, the virtual
 * clock with it, so however long the answer takes over a socket or USB,
 * the firmware sees it within its frame waiting time.
 *
 * Copyright (c) 2026, Kara Zajac
 * SPDX-License-Identifier: BSD-3-Clause
 */
#include "qemu/osdep.h"
#include "qemu/timer.h"
#include "qemu/main-loop.h"
#include "qemu/error-report.h"
#include "qapi/error.h"
#include "hw/core/irq.h"
#include "hw/core/qdev-properties.h"
#include "hw/core/qdev-properties-system.h"
#include "migration/vmstate.h"
#include "flipper.h"

#define REG_IO_CONF1      0x00
#define REG_OP_CONTROL    0x02
#define REG_MODE          0x03
#define REG_ISO14443A     0x05
#define REG_AUX           0x0A
#define REG_NRT1          0x10
#define REG_NRT2          0x11
#define REG_TIMER_EMV     0x12
#define REG_IRQ_MASK0     0x16
#define REG_IRQ_MAIN      0x1A
#define REG_FIFO_STATUS1  0x1E
#define REG_FIFO_STATUS2  0x1F
#define REG_COLLISION     0x20
#define REG_NUM_TX1       0x22
#define REG_NUM_TX2       0x23
#define REG_AD_RESULT     0x25
#define REG_AUX_DISPLAY   0x31
#define REG_IC_IDENTITY   0x3F

#define OP_EN     0x80
#define OP_RX_EN  0x40
#define OP_TX_EN  0x08
#define A_NO_TX_PAR 0x80
#define A_NO_RX_PAR 0x40
#define AUX_NO_CRC_RX 0x80

/* interrupt bits: register index, bit */
#define I_OSC  0, 0x80
#define I_RXS  0, 0x20
#define I_RXE  0, 0x10
#define I_TXE  0, 0x08
#define I_DCT  1, 0x80
#define I_NRE  1, 0x40
#define I_EON  1, 0x10
#define I_EOF  1, 0x08
#define I_WU_A 3, 0x01
#define I_WU_A_X 3, 0x02               /* ... from SLEEP, by a WUPA */
#define I_WU_F 3, 0x08                 /* a SENSF_REQ answered (FeliCa) */
#define MODE_TARG 0x80
/* MODE.om as an initiator: what is on the air */
#define MODE_OM(v)       (((v) >> 3) & 0xF)
#define OM_ISO14443A     0x1
#define OM_ISO14443B     0x2
#define OM_FELICA        0x3
#define OM_SUBCARRIER    0xE            /* stream mode: ISO15693 by software */
#define I_CRC  2, 0x80
#define I_PAR  2, 0x40

#define BIT_NS 9439                     /* 128 / 13.56 MHz */
#define FDT_NS (40 * SCALE_US)          /* end of TX to start of the answer */

enum { STAGE_IDLE, STAGE_TX, STAGE_RXS, STAGE_RXE, STAGE_NRT, STAGE_LINK };
#define LINK_ANSWER_MS 1000             /* host time for a card on the link */

static void st25_update_irq(St25r3916State *s)
{
    bool pending = false;

    for (int i = 0; i < 4; i++) {
        pending |= s->irq_status[i] & ~s->irq_mask[i];
    }
    if (s->tm) {
        /* transparent mode: the pin has the receiver's signal, not this */
        qemu_set_irq(s->irq, s->v_level);
        return;
    }
    qemu_set_irq(s->irq, pending);
}

static void st25_raise(St25r3916State *s, int reg, uint8_t bits)
{
    s->irq_status[reg] |= bits;
    st25_update_irq(s);
}

/* ------------------------------------------------------------ air helpers */

static inline int bit_get(const uint8_t *b, int i)
{
    return (b[i >> 3] >> (i & 7)) & 1;
}

static inline void bit_put(uint8_t *b, int i, int v)
{
    if (v) {
        b[i >> 3] |= 1 << (i & 7);
    } else {
        b[i >> 3] &= ~(1 << (i & 7));
    }
}

uint16_t nfc_crc_a(const uint8_t *d, int n)
{
    uint32_t crc = 0x6363;

    for (int i = 0; i < n; i++) {
        uint8_t b = d[i] ^ (crc & 0xFF);

        b ^= b << 4;
        crc = (crc >> 8) ^ ((uint32_t)b << 8) ^ ((uint32_t)b << 3) ^ (b >> 4);
    }
    return crc & 0xFFFF;
}

/*
 * Bytes to air bits: every full byte followed by its odd parity bit, then
 * the bits of an incomplete last byte (anticollision frames, 4-bit ACKs).
 */
int nfc_air_encode(const uint8_t *data, int nbits, uint8_t *air)
{
    int n = 0, full = nbits / 8;

    memset(air, 0, NFC_AIR_MAX);
    for (int i = 0; i < full && n + 9 <= NFC_AIR_MAX * 8; i++) {
        for (int b = 0; b < 8; b++) {
            bit_put(air, n++, (data[i] >> b) & 1);
        }
        bit_put(air, n++, !(ctpop8(data[i]) & 1));
    }
    for (int b = 0; b < nbits % 8; b++) {
        bit_put(air, n++, (data[full] >> b) & 1);
    }
    return n;
}

/*
 * Air bits back to bytes: full 9-bit groups are bytes and their parity; any
 * remainder is an incomplete last byte.  Returns bits, sets *parity_ok.
 */
int nfc_air_decode(const uint8_t *air, int nbits, uint8_t *data, bool *parity_ok)
{
    int bytes = nbits / 9, rest = nbits % 9;

    *parity_ok = true;
    memset(data, 0, (nbits + 7) / 8 + 1);
    for (int i = 0; i < bytes; i++) {
        uint8_t v = 0;

        for (int b = 0; b < 8; b++) {
            v |= bit_get(air, i * 9 + b) << b;
        }
        data[i] = v;
        if (bit_get(air, i * 9 + 8) != !(ctpop8(v) & 1)) {
            *parity_ok = false;
        }
    }
    for (int b = 0; b < rest && b < 8; b++) {
        data[bytes] |= bit_get(air, bytes * 9 + b) << b;
    }
    return bytes * 8 + MIN(rest, 8);
}

/* air bits -> data bits (returned) and the parity bit of each full byte */
static int nfc_air_split(const uint8_t *air, int nbits, uint8_t *data,
                         uint8_t *par)
{
    int bytes = nbits / 9, rest = nbits % 9;

    memset(data, 0, (nbits + 7) / 8 + 1);
    memset(par, 0, bytes / 8 + 1);
    for (int i = 0; i < bytes; i++) {
        for (int b = 0; b < 8; b++) {
            data[i] |= bit_get(air, i * 9 + b) << b;
        }
        bit_put(par, i, bit_get(air, i * 9 + 8));
    }
    for (int b = 0; b < rest && b < 8; b++) {
        data[bytes] |= bit_get(air, bytes * 9 + b) << b;
    }
    return bytes * 8 + MIN(rest, 8);
}

/* data bits and their parity (NULL: odd, as a card computes it) -> air */
static int nfc_air_join(const uint8_t *data, int nbits, const uint8_t *par,
                        uint8_t *air)
{
    int n = 0, full = nbits / 8;

    if (!par) {
        return nfc_air_encode(data, nbits, air);
    }
    memset(air, 0, NFC_AIR_MAX);
    for (int i = 0; i < full && n + 9 <= NFC_AIR_MAX * 8; i++) {
        for (int b = 0; b < 8; b++) {
            bit_put(air, n++, (data[i] >> b) & 1);
        }
        bit_put(air, n++, bit_get(par, i));
    }
    for (int b = 0; b < nbits % 8; b++) {
        bit_put(air, n++, (data[full] >> b) & 1);
    }
    return n;
}

/* ------------------------------------------------------------- a record */

#define REC_FLUSH_MS 50

static void st25_rec_flush(void *opaque)
{
    St25r3916State *s = opaque;

    if (s->rec) {
        fflush(s->rec);
        timer_mod(s->rec_flush,
                  qemu_clock_get_ms(QEMU_CLOCK_REALTIME) + REC_FLUSH_MS);
    }
}

static bool st25_rec_begin(St25r3916State *s)
{
    int64_t ns;

    if (!s->rec) {
        return false;
    }
    ns = qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL) - s->rec_t0;
    fprintf(s->rec, "%" PRId64 ".%09" PRId64 " ",
            (int64_t)(ns / NANOSECONDS_PER_SECOND),
            (int64_t)(ns % NANOSECONDS_PER_SECOND));
    s->rec_lines++;
    return true;
}

/* a frame in a line: its bits, its bytes, and after a "p" the parity bits
 * of its bytes where they are not the odd ones (MIFARE Classic's) */
static void st25_rec_frame(St25r3916State *s, const uint8_t *d, int bits,
                           const uint8_t *par)
{
    bool odd = true;

    if (bits <= 0) {
        fputc('-', s->rec);
        return;
    }
    fprintf(s->rec, "%d ", bits);
    for (int i = 0; i < (bits + 7) / 8; i++) {
        fprintf(s->rec, "%02X", d[i]);
        if (par && i < bits / 8) {
            odd &= ((par[i / 8] >> (i % 8)) & 1) == !(ctpop8(d[i]) & 1);
        }
    }
    if (!odd) {
        fputs(" p", s->rec);
        for (int i = 0; i < (bits / 8 + 7) / 8; i++) {
            fprintf(s->rec, "%02X", par[i]);
        }
    }
    s->rec_frames++;
}

/* an event that is no frame: a field, a card selected */
static void G_GNUC_PRINTF(2, 3) st25_rec_note(St25r3916State *s,
                                              const char *fmt, ...)
{
    va_list ap;

    if (!st25_rec_begin(s)) {
        return;
    }
    va_start(ap, fmt);
    vfprintf(s->rec, fmt, ap);
    va_end(ap);
    fputc('\n', s->rec);
}

/* who asked ("flipper": the firmware as a reader; "reader": one in front of
 * the Flipper as a card), in which technology, the frame and its answer */
static void st25_rec_exchange(St25r3916State *s, const char *who, char tech,
                              const uint8_t *out, int out_bits,
                              const uint8_t *out_par, const uint8_t *in,
                              int in_bits, const uint8_t *in_par)
{
    if (!st25_rec_begin(s)) {
        return;
    }
    fprintf(s->rec, "%s %c ", who, tech);
    st25_rec_frame(s, out, out_bits, out_par);
    fputs(" > ", s->rec);
    st25_rec_frame(s, in, in_bits, in_par);
    fputc('\n', s->rec);
}

/* MODE.om of the firmware as a reader -> the record's letter */
static char st25_rec_tech(St25r3916State *s)
{
    switch (MODE_OM(s->rega[REG_MODE])) {
    case OM_ISO14443A: return 'a';
    case OM_ISO14443B: return 'b';
    case OM_FELICA: return 'f';
    case OM_SUBCARRIER: return 'v';
    }
    return '?';
}

/* the firmware's frame (s->tx) and the card's answer (s->rx, s->nrx bits;
 * 0: none), both as they are on the air */
static void st25_rec_poll(St25r3916State *s)
{
    uint8_t out[NFC_AIR_MAX + 1], in[NFC_AIR_MAX + 1];
    uint8_t out_par[NFC_AIR_MAX / 8 + 2], in_par[NFC_AIR_MAX / 8 + 2];
    int out_bits, in_bits;

    if (!s->rec) {
        return;
    }
    if (MODE_OM(s->rega[REG_MODE]) == OM_SUBCARRIER && s->ntx >= 16 &&
        s->tx[0] == 0x21) {
        /* ISO15693 as the firmware codes it (see st25_card_bytes): the
         * bytes that the codes stand for */
        out_bits = in_bits = 0;
        memset(out, 0, sizeof(out));
        memset(in, 0, sizeof(in));
        for (int i = 1; i < s->ntx / 8 && s->tx[i] != 0x04; i++) {
            int pair = s->tx[i] == 0x02 ? 0 : s->tx[i] == 0x08 ? 1 :
                       s->tx[i] == 0x20 ? 2 : s->tx[i] == 0x80 ? 3 : -1;

            if (pair < 0) {
                break;
            }
            out[out_bits / 8] |= pair << (out_bits % 8);
            out_bits += 2;
        }
        for (int i = 5; i + 1 < s->nrx - 8; i += 2, in_bits++) {
            in[in_bits / 8] |= bit_get(s->rx, i + 1) << (in_bits % 8);
        }
        st25_rec_exchange(s, "flipper", 'v', out, out_bits, NULL, in, in_bits,
                          NULL);
        return;
    }
    if (MODE_OM(s->rega[REG_MODE]) != OM_ISO14443A) {
        st25_rec_exchange(s, "flipper", st25_rec_tech(s), s->tx, s->ntx, NULL,
                          s->rx, s->nrx, NULL);
        return;
    }
    out_bits = nfc_air_split(s->tx, s->ntx, out, out_par);
    in_bits = s->nrx > 0 ? nfc_air_split(s->rx, s->nrx, in, in_par) : 0;
    st25_rec_exchange(s, "flipper", 'a', out, out_bits, out_par, in, in_bits,
                      in_par);
}

/* listening as a FeliCa card (MODE: target, om2): its frames are bytes,
 * length first and the CRC last, with no parity */
static bool st25_listen_f(St25r3916State *s)
{
    return (s->rega[REG_MODE] & MODE_TARG) && (MODE_OM(s->rega[REG_MODE]) & 4);
}

/* ------------------------------------------------------------ the field */

static bool st25_field(St25r3916State *s)
{
    return (s->rega[REG_OP_CONTROL] & (OP_EN | OP_TX_EN)) == (OP_EN | OP_TX_EN);
}

static void st25_field_changed(St25r3916State *s)
{
    bool on = st25_field(s);

    if (on != s->field_on) {
        s->field_on = on;
        if (s->card) {
            nfc_card_field(s->card, on);
        }
        flipper_link_send(&s->lk, "field %s\n", on ? "on" : "off");
        st25_rec_note(s, "flipper field %s", on ? "on" : "off");
    }
}

static void st25_fifo_clear(St25r3916State *s)
{
    s->fifo_len = s->fifo_rd = 0;
    s->fifo_lb = 0;
}

static void st25_stop(St25r3916State *s)
{
    timer_del(s->timer);
    s->stage = STAGE_IDLE;
}

/*
 * The technologies that are bytes on the air (the firmware adds each one's
 * CRC itself, and sends with TRANSMIT_WITHOUT_CRC).  ISO14443B and FeliCa:
 * the FIFO's bytes are the frame's, the chip makes the SOF and EOF, the
 * preamble and the sync.  ISO15693 is the chip's sub-carrier stream mode,
 * coded by the firmware (furi_hal_nfc_iso15693.c): what it sends is a byte
 * for each two bits of data, "1 out of 4" (SOF 0x21; 0x02, 0x08, 0x20, 0x80
 * for 00, 01, 10, 11, low bits first; EOF 0x04), and what it receives is the
 * stream itself, low bit first: a SOF of five bits (0x17), two bits for
 * each bit of data (01 for a 0, 10 for a 1), an EOF (0x1D).
 * -> the answer's bits in s->rx, 0: none
 */
static int st25_card_bytes(St25r3916State *s, int om)
{
    uint8_t in[NFC_AIR_MAX / 4], out[128];
    int n = s->ntx / 8, got, bits = 0;

    if (om == OM_ISO14443B || om == OM_FELICA) {
        got = nfc_card_bytes(s->card, om == OM_FELICA ? NFC_TECH_F : NFC_TECH_B,
                             s->tx, n, s->rx, sizeof(s->rx));
        return MAX(got, 0) * 8;
    }
    if (om != OM_SUBCARRIER || n < 2 || s->tx[0] != 0x21) {
        return 0;
    }
    memset(in, 0, sizeof(in));
    for (int i = 1; i < n && s->tx[i] != 0x04; i++) {
        int pair = s->tx[i] == 0x02 ? 0 : s->tx[i] == 0x08 ? 1 :
                   s->tx[i] == 0x20 ? 2 : s->tx[i] == 0x80 ? 3 : -1;

        if (pair < 0 || bits / 8 >= sizeof(in)) {
            return 0;                   /* (no such symbol: no card hears it) */
        }
        in[bits / 8] |= pair << (bits % 8);
        bits += 2;
    }
    got = nfc_card_bytes(s->card, NFC_TECH_V, in, bits / 8, out, sizeof(out));
    if (got <= 0) {
        return 0;
    }
    memset(s->rx, 0, sizeof(s->rx));
    bits = 0;
    for (int i = 0; i < 5; i++, bits++) {
        bit_put(s->rx, bits, (0x17 >> i) & 1);
    }
    for (int i = 0; i < got * 8; i++, bits += 2) {
        bool one = (out[i / 8] >> (i % 8)) & 1;

        bit_put(s->rx, bits, !one);
        bit_put(s->rx, bits + 1, one);
    }
    for (int i = 0; i < 8; i++, bits++) {
        bit_put(s->rx, bits, (0x1D >> i) & 1);
    }
    return bits;
}

/* the answer is in: decode it into the FIFO the way the chip is set up */
static void st25_rx_end(St25r3916State *s)
{
    bool bytes = MODE_OM(s->rega[REG_MODE]) != OM_ISO14443A &&
                 !(s->rega[REG_MODE] & MODE_TARG);
    bool raw = bytes || (s->rega[REG_ISO14443A] & A_NO_RX_PAR);
    bool parity_ok = true;
    uint8_t data[NFC_AIR_MAX + 1];
    int bits;

    st25_fifo_clear(s);
    if (raw) {
        bits = s->nrx;
        memcpy(data, s->rx, (bits + 7) / 8);
    } else {
        bits = nfc_air_decode(s->rx, s->nrx, data, &parity_ok);
    }
    /* the FIFO's 512 bytes: a longer frame loses its end (a card sends
     * none, a program on the link can) */
    bits = MIN(bits, (int)sizeof(s->fifo) * 8);
    memcpy(s->fifo, data, (bits + 7) / 8);
    s->fifo_len = (bits + 7) / 8;
    s->fifo_lb = bits % 8;
    if (!parity_ok) {
        st25_raise(s, I_PAR);
    }
    if (!raw && !(s->rega[REG_AUX] & AUX_NO_CRC_RX) && bits % 8 == 0 &&
        s->fifo_len >= 3 && nfc_crc_a(s->fifo, s->fifo_len) != 0) {
        st25_raise(s, I_CRC);           /* CRC over data and CRC is 0 */
    }
    st25_raise(s, I_RXE);
}

static void st25_link_poll(St25r3916State *s);
static void st25_silence(St25r3916State *s, int64_t now);
static void st25_link_reader(St25r3916State *s, char *line);
static void st25_link_rd_end(St25r3916State *s);

/* the transmission is over: hand it to the card, schedule its answer */
static void st25_tx_end(St25r3916State *s, int64_t now)
{
    st25_raise(s, I_TXE);
    s->nrx = 0;
    if (s->rega[REG_MODE] & MODE_TARG) {
        s->stage = STAGE_IDLE;          /* the virtual reader has it already */
        return;
    }
    if (s->card && s->field_on && (s->rega[REG_OP_CONTROL] & OP_RX_EN)) {
        int om = MODE_OM(s->rega[REG_MODE]);

        s->nrx = om == OM_ISO14443A ?
                 nfc_card_rx(s->card, s->tx, s->ntx, s->rx) :
                 st25_card_bytes(s, om);
    } else if (MODE_OM(s->rega[REG_MODE]) != OM_ISO14443A) {
        /* (a card on the link is an ISO14443A card) */
    } else if (!s->card && s->lk.open && s->field_on &&
               (s->rega[REG_OP_CONTROL] & OP_RX_EN)) {
        st25_link_poll(s);              /* the card on the link answers */
        return;
    }
    st25_rec_poll(s);
    if (s->nrx > 0) {
        s->stage = STAGE_RXS;
        timer_mod(s->timer, now + FDT_NS);
        return;
    }
    st25_silence(s, now);
}

/* no answer: the no-response timer runs out, if it was set */
static void st25_silence(St25r3916State *s, int64_t now)
{
    uint32_t nrt = s->rega[REG_NRT1] << 8 | s->rega[REG_NRT2];

    if (nrt) {
        int64_t step = (s->rega[REG_TIMER_EMV] & 1) ? 4096 : 64;

        s->stage = STAGE_NRT;
        timer_mod(s->timer, now + muldiv64(nrt * step, NANOSECONDS_PER_SECOND,
                                           13560000));
    } else {
        s->stage = STAGE_IDLE;
    }
}

static void st25_timer(void *opaque)
{
    St25r3916State *s = opaque;
    int64_t now = qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL);

    switch (s->stage) {
    case STAGE_TX:
        st25_tx_end(s, now);
        break;
    case STAGE_RXS:
        st25_raise(s, I_RXS);
        s->stage = STAGE_RXE;
        timer_mod(s->timer, now + (int64_t)(s->nrx + 1) * BIT_NS);
        break;
    case STAGE_RXE:
        s->stage = STAGE_IDLE;
        st25_rx_end(s);
        break;
    case STAGE_NRT:
        s->stage = STAGE_IDLE;
        st25_raise(s, I_NRE);
        break;
    }
}

/* ---------------------------------------------------- a card on the link */

static void hex_put(GString *out, const uint8_t *b, int n)
{
    for (int i = 0; i < n; i++) {
        g_string_append_printf(out, "%02X", b[i]);
    }
}

static int hex_get(const char *p, uint8_t *b, int max)
{
    int n = 0;

    while (n < max && g_ascii_isxdigit(p[0]) && g_ascii_isxdigit(p[1])) {
        b[n++] = g_ascii_xdigit_value(p[0]) << 4 | g_ascii_xdigit_value(p[1]);
        p += 2;
    }
    return n;
}

/* the frame just sent goes out on the link; the machine waits for the card */
static void st25_link_poll(St25r3916State *s)
{
    uint8_t data[NFC_AIR_MAX + 1], par[NFC_AIR_MAX / 8 + 2];
    int nbits = nfc_air_split(s->tx, s->ntx, data, par);
    g_autoptr(GString) line = g_string_new(NULL);

    /* MODE.om: what the firmware polls for, so a relay can skip the rest */
    static const char *const om[16] = {
        [1] = "a", [2] = "b", [3] = "f", [4] = "t1", [14] = "v", [15] = "bpsk",
    };
    const char *mode = om[(s->rega[REG_MODE] >> 3) & 15];

    g_string_printf(line, "poll %d ", nbits);
    hex_put(line, data, (nbits + 7) / 8);
    g_string_append_c(line, ' ');
    hex_put(line, par, (nbits / 8 + 7) / 8);
    g_string_append_printf(line, " mode=%s\n", mode ? mode : "other");
    flipper_link_send(&s->lk, "%s", line->str);
    s->stage = STAGE_LINK;
    timer_mod(s->link_timeout,
              qemu_clock_get_ms(QEMU_CLOCK_REALTIME) + LINK_ANSWER_MS);
    /* not from here: this runs in a virtual-clock timer, and stopping the
     * machine waits for that clock's timers to finish, this one included */
    qemu_bh_schedule(s->link_stop);
}

/* time stands still till the card answers (if it has not already) */
static void st25_link_stop(void *opaque)
{
    St25r3916State *s = opaque;

    if (s->stage == STAGE_LINK && !s->link_paused) {
        s->link_paused = flipper_vm_hold();
    }
}

static void st25_link_resume(St25r3916State *s)
{
    timer_del(s->link_timeout);
    if (s->link_paused) {
        s->link_paused = false;
        flipper_vm_release();       /* (a pause that came meanwhile stays) */
    }
}

/* "answer NBITS DATA [PARITY]" or "silence": the card's turn is over */
static void st25_link_line(void *opaque, char *line)
{
    St25r3916State *s = opaque;
    uint8_t data[NFC_AIR_MAX], par[NFC_AIR_MAX / 8 + 1];
    int64_t now;
    char *p, *end;
    long nbits;
    bool own_par;

    if (g_str_has_prefix(line, "reader ")) {
        st25_link_reader(s, line + 7);
        return;
    }
    if (s->stage != STAGE_LINK) {
        return;                         /* nothing asked */
    }
    if (g_str_has_prefix(line, "silence")) {
        st25_link_resume(s);
        st25_rec_poll(s);
        st25_silence(s, qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL));
        return;
    }
    if (!g_str_has_prefix(line, "answer ")) {
        return;
    }
    nbits = strtol(line + 7, &end, 10);
    if (end == line + 7 || nbits <= 0 || nbits > (NFC_AIR_MAX - 2) * 8 * 8 / 9) {
        return;
    }
    p = end;
    while (*p == ' ') {
        p++;
    }
    memset(data, 0, sizeof(data));
    if (hex_get(p, data, sizeof(data)) < (nbits + 7) / 8) {
        return;
    }
    p += (nbits + 7) / 8 * 2;
    while (*p == ' ') {
        p++;
    }
    memset(par, 0, sizeof(par));
    own_par = hex_get(p, par, sizeof(par)) > 0;  /* encrypted parity, say */
    st25_link_resume(s);
    now = qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL);
    s->nrx = nfc_air_join(data, nbits, own_par ? par : NULL, s->rx);
    st25_rec_poll(s);
    s->stage = STAGE_RXS;
    timer_mod(s->timer, now + FDT_NS);
}

static void st25_link_timeout(void *opaque)
{
    St25r3916State *s = opaque;

    if (s->stage != STAGE_LINK) {
        return;
    }
    if (!s->link_late) {
        s->link_late = true;
        warn_report("flipper-zero: NFC link: no answer in %d ms, taken as "
                    "silence", LINK_ANSWER_MS);
    }
    st25_link_resume(s);
    st25_rec_poll(s);
    st25_silence(s, qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL));
}

static void st25_link_opened(void *opaque, bool open)
{
    St25r3916State *s = opaque;

    if (open) {
        s->link_late = false;
        flipper_link_send(&s->lk, "hello flipperemu nfc 1\nfield %s\n",
                          s->field_on ? "on" : "off");
    } else {
        st25_link_rd_end(s);            /* a reader on it: gone with it */
        if (s->stage == STAGE_LINK) {
            st25_link_resume(s);        /* gone mid-frame: silence */
            st25_rec_poll(s);
            st25_silence(s, qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL));
        }
    }
}

static void st25_rd_answer(St25r3916State *s);

/* TRANSMIT_*: build the frame on the air, end it after its own duration */
static void st25_transmit(St25r3916State *s, uint8_t cmd)
{
    uint8_t data[NFC_AIR_MAX];
    int nbits;

    st25_stop(s);
    memset(s->tx, 0, sizeof(s->tx));
    if (cmd == 0xC6 || cmd == 0xC7) {   /* REQA 0x26 / WUPA 0x52: 7 bits */
        uint8_t b = cmd == 0xC6 ? 0x26 : 0x52;

        for (int i = 0; i < 7; i++) {
            bit_put(s->tx, i, (b >> i) & 1);
        }
        s->ntx = 7;
    } else {
        nbits = (s->rega[REG_NUM_TX1] << 8 | s->rega[REG_NUM_TX2]) & 0x1FFF;
        nbits = MIN(nbits, (int)s->fifo_len * 8);
        memcpy(data, s->fifo, (nbits + 7) / 8);
        if (cmd == 0xC4 && nbits % 8 == 0 && nbits / 8 + 2 <= (int)sizeof(data)) {
            uint16_t crc = nfc_crc_a(data, nbits / 8);

            data[nbits / 8] = crc & 0xFF;
            data[nbits / 8 + 1] = crc >> 8;
            nbits += 16;
        }
        if ((s->rega[REG_ISO14443A] & A_NO_TX_PAR) || st25_listen_f(s) ||
            (MODE_OM(s->rega[REG_MODE]) != OM_ISO14443A &&
             !(s->rega[REG_MODE] & MODE_TARG))) {
            memcpy(s->tx, data, (nbits + 7) / 8);   /* as it is */
            s->ntx = nbits;
        } else {
            s->ntx = nfc_air_encode(data, nbits, s->tx);
        }
    }
    st25_fifo_clear(s);
    s->stage = STAGE_TX;
    timer_mod(s->timer, qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL) +
                        (int64_t)(s->ntx + 2) * BIT_NS);
    if (s->rega[REG_MODE] & MODE_TARG) {
        st25_rd_answer(s);              /* listening: this answers the reader */
    }
}

/* ------------------------------------------------------------ registers */

static void st25_defaults(St25r3916State *s)
{
    memset(s->rega, 0, sizeof(s->rega));
    memset(s->regb, 0, sizeof(s->regb));
    memset(s->irq_status, 0, sizeof(s->irq_status));
    memset(s->irq_mask, 0, sizeof(s->irq_mask));
    s->rega[REG_IO_CONF1] = 0x10;
    s->rega[REG_MODE] = 0x08;           /* ISO14443A */
    s->rega[REG_IC_IDENTITY] = 0x2A;    /* ic_type 5 (ST25R3916), rev 2 */
    st25_fifo_clear(s);
    st25_stop(s);
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
    switch (a) {
    case REG_FIFO_STATUS1:
        return (s->fifo_len - s->fifo_rd) & 0xFF;
    case REG_FIFO_STATUS2:
        return (((s->fifo_len - s->fifo_rd) >> 8) & 3) << 6 | (s->fifo_lb & 7) << 1;
    case REG_AUX_DISPLAY:
        return ((s->rega[REG_OP_CONTROL] & OP_EN) ? 0x10 : 0) |      /* osc_ok */
               (st25_field(s) ? 0x20 : 0) |                         /* tx_on */
               (s->rd_field || s->ext_field ? 0x40 : 0);            /* efd_o */
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
    switch (a) {
    case REG_IRQ_MAIN ... REG_IRQ_MAIN + 3:
    case REG_FIFO_STATUS1: case REG_FIFO_STATUS2: case REG_COLLISION:
    case REG_AD_RESULT: case REG_AUX_DISPLAY: case REG_IC_IDENTITY:
        return;                         /* read-only */
    case REG_OP_CONTROL: {
        bool osc_on = (v & OP_EN) && !(s->rega[a] & OP_EN);

        s->rega[a] = v;
        if (osc_on) {
            st25_raise(s, I_OSC);       /* oscillator stable */
        }
        st25_field_changed(s);
        return;
    }
    }
    s->rega[a & 0x3F] = v;
}

static void st25_direct(St25r3916State *s, uint8_t cmd)
{
    switch (cmd) {
    case 0xC1:                          /* SET_DEFAULT */
        st25_defaults(s);
        st25_field_changed(s);
        st25_update_irq(s);
        break;
    case 0xC2:                          /* STOP: all activity, and the FIFO */
        st25_stop(s);
        st25_fifo_clear(s);
        break;
    case 0xDB:                          /* CLEAR_FIFO (and collision) */
        st25_fifo_clear(s);
        s->rega[REG_COLLISION] = 0;
        break;
    case 0xC4: case 0xC5: case 0xC6: case 0xC7:     /* TRANSMIT_* */
        st25_transmit(s, cmd);
        break;
    case 0xCD:                          /* GOTO_SENSE */
        s->pt_sleep = false;
        break;
    case 0xDC:                          /* TRANSPARENT_MODE, till selected */
        s->tm = true;
        s->tm_n = 0;
        s->v_level = false;
        st25_update_irq(s);             /* the IRQ pin: the receiver's signal */
        break;
    case 0xCE:                          /* GOTO_SLEEP (after HLTA) */
        s->pt_sleep = true;
        break;
    case 0xE8:                          /* STOP_NRT */
        if (s->stage == STAGE_NRT) {
            st25_stop(s);
        }
        break;
    case 0xDF:                          /* MEASURE_VDD: ~3.3 V */
        s->rega[REG_AD_RESULT] = 141;
        st25_raise(s, I_DCT);
        break;
    case 0xD3: case 0xD9: case 0xDD: case 0xDE: case 0xD6: case 0xD8:
        s->rega[REG_AD_RESULT] = 0x80;  /* measurements / calibrations */
        st25_raise(s, I_DCT);
        break;
    default:
        break;
    }
}

/* ---------------------------------------------- listen: virtual reader */

enum { RD_FIELD_ON, RD_ACTIVATE, RD_VERSION, RD_SIG, RD_PAGES, RD_COUNTER,
       RD_HALT, RD_FIELD_OFF };

/* a reader's field around the Flipper: a session's, or a standing one
 * ("field"); the chip's external field detector reports either */
static void st25_ext_field(St25r3916State *s, bool session, bool standing)
{
    bool was = s->rd_field || s->ext_field;

    s->rd_field = session;
    s->ext_field = standing;
    if ((session || standing) != was) {
        if (was) {
            st25_raise(s, I_EOF);
        } else {
            st25_raise(s, I_EON);
        }
        st25_rec_note(s, "reader field %s", was ? "off" : "on");
    }
}
#define RD_ANSWER_NS (100 * SCALE_MS)   /* no answer by then: none */
#define RD_LINK_ANSWER_NS (250 * SCALE_MS)  /* ... to a program's frame: it
                                         * has no second and third send of
                                         * its own before it hears "silence" */
#define RD_GAP_NS    (1 * SCALE_MS)     /* an answer to the next command */
#define RD_LATE_NS   (5 * SCALE_MS)     /* an answer that has begun: wait on, */
#define RD_LATE_MAX  40                 /* ... so often at most */
#define RD_TRIES     3                  /* sends of one command, at most */

/* a reader's frame is out: kept until its answer, for the record's line */
static void st25_rd_asked(St25r3916State *s, const uint8_t *d, int bits,
                          const uint8_t *par)
{
    int n = MIN((bits + 7) / 8, (int)sizeof(s->rd_cmd));

    memcpy(s->rd_cmd, d, n);
    s->rd_cmd_bits = MIN(bits, n * 8);
    for (int i = 0; i < n; i++) {       /* (odd, where none was given) */
        bit_put(s->rd_cmd_par, i, par ? bit_get(par, i) : !(ctpop8(d[i]) & 1));
    }
}

/* ... and the line: with the firmware's answer, or with none */
static void st25_rd_recorded(St25r3916State *s, bool answered)
{
    bool f = st25_listen_f(s) || s->tm;     /* (bytes: no parity) */

    if (s->rec && s->rd_cmd_bits) {
        st25_rec_exchange(s, "reader", s->tm ? 'v' : f ? 'f' : 'a', s->rd_cmd,
                          s->rd_cmd_bits, f ? NULL : s->rd_cmd_par, s->rd_ans,
                          answered ? s->rd_ans_bits : 0, f ? NULL : s->rd_par);
    }
    s->rd_cmd_bits = 0;
}

/* a reader command reaches the firmware: into the FIFO, RXS then RXE */
static void st25_rd_send(St25r3916State *s, const uint8_t *cmd, int n)
{
    uint16_t crc = nfc_crc_a(cmd, n);

    st25_fifo_clear(s);
    memcpy(s->fifo, cmd, n);
    s->fifo[n] = crc & 0xFF;
    s->fifo[n + 1] = crc >> 8;
    s->fifo_len = n + 2;
    st25_rd_asked(s, s->fifo, (n + 2) * 8, NULL);
    s->rd_ans_bits = -1;
    s->rd_waiting = true;
    st25_raise(s, I_RXS);
    st25_raise(s, I_RXE);
    timer_mod(s->rd_timer, qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL) + RD_ANSWER_NS);
}

/* the firmware transmitted: that is the answer to the reader's command */
static void st25_rd_answer(St25r3916State *s)
{
    if (!s->rd_waiting) {
        return;
    }
    if (st25_listen_f(s) || s->tm) {    /* bytes, as they are */
        memcpy(s->rd_ans, s->tx, (s->ntx + 7) / 8);
        memset(s->rd_par, 0, sizeof(s->rd_par));
        s->rd_ans_bits = s->ntx;
    } else {
        s->rd_ans_bits = nfc_air_split(s->tx, s->ntx, s->rd_ans, s->rd_par);
    }
    st25_rd_recorded(s, true);
    s->rd_waiting = false;
    timer_mod(s->rd_timer, qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL) +
                           (int64_t)(s->ntx + 2) * BIT_NS + RD_GAP_NS);
}

/* an answer of n data bytes plus a good CRC_A? */
static bool rd_ok(St25r3916State *s, int n)
{
    return s->rd_ans_bits == (n + 2) * 8 && nfc_crc_a(s->rd_ans, n + 2) == 0;
}

/*
 * The reader's current command: sent, and sent again after no answer or a
 * garbled one, as readers retry (RD_TRIES sends in all).  True while its
 * answer is awaited; false once an answer is in (a NAK too) or the tries
 * are spent, and rd_ok() then tells whether it is the one wanted.  An
 * emulated CPU on a busy host can be late, or miss a command that came too
 * soon after its last answer; a real one is not.
 */
static bool rd_ask(St25r3916State *s, const uint8_t *cmd, int n, int want)
{
    if (rd_ok(s, want) || s->rd_ans_bits == 4 || s->rd_sent >= RD_TRIES) {
        s->rd_sent = 0;
        return false;
    }
    s->rd_sent++;
    st25_rd_send(s, cmd, n);
    return true;
}

/* on to the next step, a moment after the last answer */
static void rd_next(St25r3916State *s, int step, int64_t now)
{
    s->rd_ans_bits = -1;
    s->rd_step = step;
    timer_mod(s->rd_timer, now + RD_GAP_NS);
}

static void hex_line(GString *f, const uint8_t *b, int n)
{
    for (int i = 0; i < n; i++) {
        g_string_append_printf(f, "%s%02X", i ? " " : "", b[i]);
    }
    g_string_append_c(f, '\n');
}

static void st25_rd_write(St25r3916State *s)
{
    static const struct { uint8_t size; int pages; const char *name; } t2[] = {
        { 0x0B, 20, "Mifare Ultralight 11" }, { 0x0E, 41, "Mifare Ultralight 21" },
        { 0x0F, 45, "NTAG213" }, { 0x11, 135, "NTAG215" }, { 0x13, 231, "NTAG216" },
    };
    g_autoptr(GString) f = g_string_new("Filetype: Flipper NFC device\nVersion: 4\n");
    g_autoptr(GError) err = NULL;
    g_autofree char *path = NULL;
    const char *type = "Mifare Ultralight";

    if (!s->tx_dir || !s->rd_uid_len) {
        return;
    }
    for (int i = 0; i < ARRAY_SIZE(t2) && s->rd_has_version; i++) {
        if (s->rd_version[6] == t2[i].size) {
            type = t2[i].name;
        }
    }
    if (!strcmp(s->rd_prog, "ntag")) {
        g_string_append(f, "Device type: NTAG/Ultralight\nUID: ");
    } else {
        g_string_append(f, "Device type: ISO14443-3A\nUID: ");
    }
    hex_line(f, s->rd_uid, s->rd_uid_len);
    g_string_append_printf(f, "ATQA: %02X %02X\nSAK: %02X\n", s->rd_atqa[1],
                           s->rd_atqa[0], s->rd_sak);   /* v4: ATQA MSB first */
    if (!strcmp(s->rd_prog, "ntag")) {
        g_string_append_printf(f, "Data format version: 2\nNTAG/Ultralight type: %s\n",
                               type);
        g_string_append(f, "Signature: ");
        hex_line(f, s->rd_sig, 32);
        g_string_append(f, "Mifare version: ");
        hex_line(f, s->rd_version, 8);
        for (int i = 0; i < 3; i++) {
            g_string_append_printf(f, "Counter %d: %u\nTearing %d: 00\n", i,
                                   i == 2 && s->rd_has_cnt ?
                                   s->rd_cnt[0] | s->rd_cnt[1] << 8 |
                                   s->rd_cnt[2] << 16 : 0, i);
        }
        g_string_append_printf(f, "Pages total: %d\nPages read: %d\n",
                               MAX(s->rd_pages, s->rd_pages_read), s->rd_pages_read);
        for (int p = 0; p < s->rd_pages_read; p++) {
            g_string_append_printf(f, "Page %d: ", p);
            hex_line(f, &s->rd_mem[p * 4], 4);
        }
        g_string_append(f, "Failed authentication attempts: 0\n");
    }
    g_mkdir_with_parents(s->tx_dir, 0755);
    for (int k = 1; k < 100000; k++) {
        g_free(path);
        path = g_strdup_printf("%s/read-%05d.nfc", s->tx_dir, k);
        if (!g_file_test(path, G_FILE_TEST_EXISTS)) {
            break;
        }
    }
    if (g_file_set_contents(path, f->str, f->len, &err)) {
        info_report("flipper-zero: NFC reader read the emulated card -> %s", path);
        g_free(s->tx_last);
        s->tx_last = g_steal_pointer(&path);
    } else {
        warn_report("flipper-zero: %s", err->message);
    }
}

/* ------------------------------------------ listen: transparent mode */

#define TM_IDLE_NS (500 * SCALE_US)

/*
 * The pin's edges while the mode is on, from its first rise.  What the
 * firmware puts there (lib/digital_signal's ISO14443A preset) is the card's
 * side of 106 kbit/s: Manchester with the 847.5 kHz subcarrier, a half wave
 * of it 8/fc long.  A 1 has the subcarrier in the first half of its 128/fc
 * and a 0 in the second; the frame starts with a 1, and ends where a bit
 * has none.  The bits after the start are the frame as it is on the air:
 * eight of data and the parity that the firmware chose, or fewer than
 * eight alone (MIFARE Classic's encrypted ACK and NAK).
 */
void st25r3916_mod(St25r3916State *s, bool level, int64_t when)
{
    if (!s->tm || s->tm_n >= ARRAY_SIZE(s->tm_edge) ||
        level == (s->tm_n ? (bool)(s->tm_edge[s->tm_n - 1] & 1) : false)) {
        return;
    }
    s->tm_edge[s->tm_n++] = when << 1 | level;
    /* (a firmware that stays in the mode, as an ISO15693 card: its frame
     * is over when no edge has come for a while) */
    timer_mod(s->tm_idle, qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL) + TM_IDLE_NS);
}

/* how long the pin was high in [a, b); *from: the first edge to look at */
static int64_t tm_high(St25r3916State *s, int64_t a, int64_t b, int *from)
{
    int64_t sum = 0;

    for (int i = *from; i + 1 < s->tm_n; i++) {
        int64_t t = s->tm_edge[i] >> 1, end = s->tm_edge[i + 1] >> 1;

        if (end <= a) {
            *from = i + 1;
            continue;
        }
        if (t >= b) {
            break;
        }
        if (s->tm_edge[i] & 1) {
            sum += MIN(end, b) - MAX(t, a);
        }
    }
    return sum;
}

/* The edges that are kept: an ISO15693 card's (its subcarrier is fc/32, a
 * half wave of 16/fc), not an ISO14443A card's (fc/16, 8/fc)? */
static bool st25_tm_wave_v(St25r3916State *s)
{
    return s->tm_n >= 2 &&
           (s->tm_edge[1] >> 1) - (s->tm_edge[0] >> 1) >=
           muldiv64(12, NANOSECONDS_PER_SECOND, 13560000);
}

static void st25_tm_end(St25r3916State *s)
{
    int64_t t0 = s->tm_n ? s->tm_edge[0] >> 1 : 0;
    int n = 0, from = 0;

    s->tm = false;
    timer_del(s->tm_idle);
    timer_del(s->v_timer);
    if (s->v_n && s->rd_waiting) {
        /* a reader's frame that the firmware did not stay to hear */
        timer_mod(s->rd_timer, qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL));
    }
    s->v_n = s->v_at = 0;
    s->v_level = false;
    st25_update_irq(s);                 /* the pin is the interrupts' again */
    if (s->tm_n < 2 || !(s->rega[REG_MODE] & MODE_TARG) || st25_tm_wave_v(s)) {
        s->tm_n = 0;
        return;
    }
    memset(s->tx, 0, sizeof(s->tx));
    for (int k = 1; n < (NFC_AIR_MAX - 1) * 8; k++) {
        int64_t a = t0 + muldiv64((uint64_t)k * 128, NANOSECONDS_PER_SECOND,
                                  13560000);
        int64_t mid = a + BIT_NS / 2;
        int64_t first = tm_high(s, a, mid, &from);
        int64_t second = tm_high(s, mid, a + BIT_NS, &from);

        if (MAX(first, second) < BIT_NS / 16) {
            break;                      /* no subcarrier: the frame's end */
        }
        bit_put(s->tx, n++, first > second);
    }
    s->ntx = n;
    s->tm_n = 0;
    if (n) {
        st25_rd_answer(s);              /* this answers the reader */
    }
}

/*
 * A firmware that emulates an ISO15693 card does all of it in this mode,
 * and never leaves it: the chip only carries the field.
 *
 * What it sends (lib/digital_signal's ISO15693 preset, the high data rate
 * with one subcarrier) is pulses of fc/32, eight of them in 256/fc.  A
 * frame starts with 24 pulses, then a 1; a 0 is pulses and then as long a
 * silence, a 1 the silence first; it ends with a 0 and 24 pulses.  So from
 * the first rise: three units of pulses, a silence, a unit of pulses, and
 * then two units a bit, until both halves of one have pulses.
 *
 * No chip select ends this frame.  It is over when no edge has come for
 * TM_IDLE_NS and the firmware's DMA does not drive the pin any more.
 */
static void st25_tm_idle(void *opaque)
{
    St25r3916State *s = opaque;
    const int64_t unit = muldiv64(256, NANOSECONDS_PER_SECOND, 13560000);
    int64_t t0 = s->tm_n ? s->tm_edge[0] >> 1 : 0;
    int n = 0, from = 0;
    bool whole = false;

    if (!s->tm || s->tm_n < 2) {
        s->tm_n = 0;
        return;
    }
    if (s->v_sending && s->v_sending(s->v_opaque)) {
        /* no end: the firmware's sequence is still on, and the next of its
         * edges waits for the firmware (stm32wb55_tim.c: the ring's marker) */
        timer_mod(s->tm_idle, qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL) +
                              TM_IDLE_NS);
        return;
    }
    if (!st25_tm_wave_v(s)) {
        /* an ISO14443A answer, and the firmware late to select the chip
         * again (a busy host): that is its end, and its edges wait for it */
        return;
    }
    memset(s->tx, 0, sizeof(s->tx));
    for (int k = 0; n < (NFC_AIR_MAX - 1) * 8; k++) {
        int64_t a = t0 + (5 + 2 * k) * unit;
        bool first = tm_high(s, a, a + unit, &from) > unit / 4;
        bool second = tm_high(s, a + unit, a + 2 * unit, &from) > unit / 4;

        if (first == second) {
            whole = first && n > 0;     /* the end's 24 pulses, after its 0 */
            break;
        }
        bit_put(s->tx, n++, second);
    }
    s->tm_n = 0;
    if (!whole || (n - 1) % 8) {
        return;                         /* nothing that is a frame */
    }
    s->ntx = n - 1;                     /* (without the end's 0) */
    st25_rd_answer(s);
}

/*
 * What it receives.  The firmware has the chip's receive signal on the IRQ
 * pin and samples it by itself (lib/signal_reader): the first rise starts
 * a timer by a DMAMUX request generator, each compare of the timer has the
 * DMA read the port's input register, and an interrupt takes every eight
 * samples for a byte.  A reader's frame is slots of 128/fc with a pause in
 * some: a start (0x21 as such a byte: slots 0 and 5), a byte for every two
 * bits (0x02, 0x08, 0x20, 0x80: "1 out of 4"), an end (0x04).
 *
 * The samples are given eight at a time, with no timer of 9.4 us: the pin
 * is set, a rise is the generator's request, and the timer's request
 * follows for each slot.  The next eight wait
 * - for the firmware's interrupt handler to have taken these (the DMA's
 *   interrupt neither pending nor active);
 * - for the 75.5 us that eight slots last;
 * - for the firmware's thread, which takes the bytes apart between two of
 *   those interrupts on a Flipper and loses bytes when the next one
 *   overtakes it.  It is done when the CPU sleeps again.  A CPU that never
 *   sleeps is no rare thing (while the NFC shell's `emulate` runs, the
 *   shell looks every two ticks whether it has ended, and the idle task
 *   never gets to its WFI): then V_BUSY_NS of the CPU's own running, which
 *   is the host's time for its thread and not the machine's clock.  The
 *   clock goes on while a busy host leaves the CPU waiting, and the thread
 *   is slow the first time its code runs (it is translated then).
 */
#define V_REQ_EDGE   1                  /* DMAMUX: request generator 0 */
#define V_REQ_SAMPLE 33                 /* ... TIM16's channel 1 */
#define V_POLL_NS    (20 * SCALE_US)
#define V_BYTE_NS    75516              /* eight slots: 1024 / 13.56 MHz */
#define V_BUSY_NS    (400 * SCALE_US)   /* a CPU that does not sleep: its
                                         * running, then on */
#define V_STUCK_NS   (500 * SCALE_MS)   /* a firmware that takes nothing: on */
#define V_ARM_NS     (50 * SCALE_MS)    /* the firmware may not listen yet */

static void st25_v_level(St25r3916State *s, bool level)
{
    if (level != s->v_level) {
        s->v_level = level;
        qemu_set_irq(s->irq, level);
        if (level && s->v_request) {
            s->v_request(s->v_opaque, V_REQ_EDGE);
        }
    }
}

static void st25_v_play(void *opaque)
{
    St25r3916State *s = opaque;
    int64_t now = qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL);

    if (!s->tm || !s->v_n || !s->v_request) {
        s->v_n = s->v_at = 0;
        return;
    }
    if (s->v_at == 0 && !s->v_armed(s->v_opaque)) {
        /* nobody samples yet: the firmware is still at its last answer */
        if (now < s->v_until) {
            timer_mod(s->v_timer, now + V_POLL_NS);
            return;
        }
        s->v_n = 0;                     /* a frame that nobody heard */
        timer_mod(s->rd_timer, now);
        return;
    }
    while (s->v_at < s->v_n * 8) {
        if (s->v_at && s->v_at % 8 == 0 && now < s->v_given + V_STUCK_NS) {
            if (!s->v_handled(s->v_opaque)) {
                timer_mod(s->v_timer, now + V_POLL_NS);
                return;
            }
            if (now < s->v_next) {
                timer_mod(s->v_timer, s->v_next);
                return;
            }
            if (!s->cpu_asleep(s->v_opaque)) {
                int64_t ran = s->cpu_ns(s->v_opaque);

                if (ran < 0 || s->v_ran < 0 ? now < s->v_given + V_BUSY_NS :
                                              ran < s->v_ran + V_BUSY_NS) {
                    timer_mod(s->v_timer, now + V_POLL_NS);
                    return;
                }
            }
        }
        st25_v_level(s, (s->v_code[s->v_at / 8] >> (s->v_at % 8)) & 1);
        s->v_request(s->v_opaque, V_REQ_SAMPLE);
        s->v_at++;
        if (s->v_at % 8 == 0) {
            s->v_next = MAX(now, s->v_next) + V_BYTE_NS;
            s->v_given = now;
            s->v_ran = s->cpu_ns(s->v_opaque);
        }
    }
    st25_v_level(s, false);
    s->v_n = s->v_at = 0;
    /* the frame is out: the firmware's answer, or none in its time */
    timer_mod(s->rd_timer, now + RD_LINK_ANSWER_NS);
}

/* a reader's frame (with its CRC) for a firmware that listens so */
static void st25_v_frame(St25r3916State *s, const uint8_t *d, int n)
{
    static const uint8_t pair[4] = { 0x02, 0x08, 0x20, 0x80 };
    int k = 0;

    n = MIN(n, (int)(sizeof(s->v_code) - 2) / 4);
    s->v_code[k++] = 0x21;
    for (int i = 0; i < n; i++) {
        for (int b = 0; b < 8; b += 2) {
            s->v_code[k++] = pair[(d[i] >> b) & 3];
        }
    }
    s->v_code[k++] = 0x04;
    s->v_n = k;
    s->v_at = 0;
    s->v_next = 0;
    s->tm_n = 0;                        /* (what it sent before is over) */
    s->v_until = qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL) + V_ARM_NS;
    st25_v_play(s);
}

/* ----------------------------------------------- a reader on the link */

static bool st25_rd_on_link(St25r3916State *s)
{
    return s->rd_prog && !strcmp(s->rd_prog, "link");
}

/* the session of a reader on the link ends: its field goes */
static void st25_link_rd_end(St25r3916State *s)
{
    if (!st25_rd_on_link(s)) {
        return;
    }
    timer_del(s->rd_timer);
    s->rd_waiting = false;
    s->rd_ans_bits = -1;
    g_free(s->rd_prog);
    s->rd_prog = NULL;
    st25_ext_field(s, false, s->ext_field);
}

/* its timer: the Flipper's answer is over, or none has come */
static void st25_link_rd_timer(St25r3916State *s)
{
    g_autoptr(GString) line = g_string_new(NULL);

    if (s->rd_waiting || s->rd_ans_bits <= 0) {
        st25_rd_recorded(s, false);
        s->rd_waiting = false;
        s->rd_ans_bits = -1;
        flipper_link_send(&s->lk, "reader silence\n");
        return;
    }
    g_string_printf(line, "reader answer %d ", s->rd_ans_bits);
    hex_put(line, s->rd_ans, (s->rd_ans_bits + 7) / 8);
    g_string_append_c(line, ' ');
    hex_put(line, s->rd_par, (s->rd_ans_bits / 8 + 7) / 8);
    s->rd_ans_bits = -1;
    flipper_link_send(&s->lk, "%s\n", line->str);
}

/*
 * "reader on|off|activate|send BITS DATA [PARITY]|saved PATH": the program
 * on the link is a reader in front of a Flipper that emulates a card.
 */
static void st25_link_reader(St25r3916State *s, char *line)
{
    uint8_t data[sizeof(s->fifo)], par[sizeof(s->fifo) / 8 + 1];
    bool listening = (s->rega[REG_MODE] & MODE_TARG) &&
                     (s->rega[REG_OP_CONTROL] & OP_EN);
    g_autoptr(GString) out = g_string_new(NULL);
    bool own_par, odd = true;
    long nbits;
    char *end, *p;

    if (g_str_has_prefix(line, "on")) {
        if (s->rd_prog && !st25_rd_on_link(s)) {
            flipper_link_send(&s->lk, "reader busy\n");
            return;                     /* a reader of the machine's own */
        }
        g_free(s->rd_prog);
        s->rd_prog = g_strdup("link");
        s->rd_waiting = false;
        s->rd_ans_bits = -1;
        st25_ext_field(s, true, s->ext_field);
        flipper_link_send(&s->lk, "reader field on\n");
        return;
    }
    if (g_str_has_prefix(line, "off")) {
        st25_link_rd_end(s);
        flipper_link_send(&s->lk, "reader field off\n");
        return;
    }
    if (g_str_has_prefix(line, "saved ")) {     /* what it read, as a file */
        g_free(s->tx_last);
        s->tx_last = g_strndup(g_strstrip((char *)line + 6), 1024);
        flipper_link_send(&s->lk, "reader saved\n");
        return;
    }
    if (!st25_rd_on_link(s)) {
        flipper_link_send(&s->lk, "reader error no field: reader on\n");
        return;
    }
    if (s->rd_waiting || s->rd_ans_bits >= 0) {
        flipper_link_send(&s->lk, "reader error an answer is due\n");
        return;
    }
    if (g_str_has_prefix(line, "activate")) {
        /* WUPA, anticollision and SELECT, which the chip answers by itself
         * from the passive-target memory: from SENSE, and from SLEEP too */
        int n = (s->rega[REG_AUX] & 0x30) ? 7 : 4;

        if (!listening) {
            flipper_link_send(&s->lk, "reader target none\n");
            return;
        }
        if (s->tm) {
            /* a firmware that listens by itself, as an ISO15693 card: the
             * chip knows nothing of the card, and an inventory is a frame */
            flipper_link_send(&s->lk, "reader target v\n");
            return;
        }
        if (st25_listen_f(s)) {
            /* a SENSF_REQ, which the chip answers from the memory that the
             * firmware loaded for it (at 15: the system code, the response
             * code, IDm, PMm, two bytes more) */
            g_string_append(out, "reader target f ");
            hex_put(out, &s->pt_mem[18], 8);
            g_string_append_c(out, ' ');
            hex_put(out, &s->pt_mem[26], 8);
            g_string_append_printf(out, " %02X%02X\n", s->pt_mem[15],
                                   s->pt_mem[16]);
            st25_raise(s, I_WU_F);
            flipper_link_send(&s->lk, "%s", out->str);
            st25_rec_note(s, "reader f polling > %s",
                          g_strchomp(out->str) + strlen("reader target f "));
            return;
        }
        g_string_append(out, "reader target a ");
        hex_put(out, s->pt_mem, n);
        g_string_append_printf(out, " %02X%02X %02X\n", s->pt_mem[10],
                               s->pt_mem[11], s->pt_mem[n == 4 ? 12 : 13]);
        if (s->pt_sleep) {
            s->pt_sleep = false;
            st25_raise(s, I_WU_A_X);
        } else {
            st25_raise(s, I_WU_A);
        }
        flipper_link_send(&s->lk, "%s", out->str);
        st25_rec_note(s, "reader a select > %s",
                      g_strchomp(out->str) + strlen("reader target a "));
        return;
    }
    if (!g_str_has_prefix(line, "send ")) {
        flipper_link_send(&s->lk, "reader error on, off, activate or send\n");
        return;
    }
    nbits = strtol(line + 5, &end, 10);
    if (end == line + 5 || nbits <= 0 || nbits > (long)sizeof(data) * 8) {
        flipper_link_send(&s->lk, "reader error a frame's bits\n");
        return;
    }
    p = end;
    while (*p == ' ') {
        p++;
    }
    memset(data, 0, sizeof(data));
    if (hex_get(p, data, sizeof(data)) < (nbits + 7) / 8) {
        flipper_link_send(&s->lk, "reader error a frame's bytes\n");
        return;
    }
    p += (nbits + 7) / 8 * 2;
    while (*p == ' ') {
        p++;
    }
    memset(par, 0, sizeof(par));
    own_par = hex_get(p, par, sizeof(par)) > 0;
    for (int i = 0; own_par && i < nbits / 8; i++) {
        odd &= bit_get(par, i) == !(ctpop8(data[i]) & 1);
    }
    st25_rd_asked(s, data, nbits, own_par ? par : NULL);
    if (listening && s->tm) {           /* the firmware samples it itself */
        s->rd_ans_bits = -1;
        s->rd_waiting = true;
        st25_v_frame(s, data, nbits / 8);
        return;
    }
    if (!listening) {                   /* nobody hears it */
        s->rd_waiting = true;
        timer_mod(s->rd_timer, qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL) +
                               RD_LINK_ANSWER_NS);
        return;
    }
    /* into the FIFO as the chip receives a frame: the data, its parity
     * bits checked and dropped */
    st25_fifo_clear(s);
    memcpy(s->fifo, data, (nbits + 7) / 8);
    s->fifo_len = (nbits + 7) / 8;
    s->fifo_lb = nbits % 8;
    s->rd_ans_bits = -1;
    s->rd_waiting = true;
    st25_raise(s, I_RXS);
    if (!odd) {
        st25_raise(s, I_PAR);
    }
    st25_raise(s, I_RXE);
    timer_mod(s->rd_timer, qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL) + RD_LINK_ANSWER_NS);
}

/* the reader's program, one step per timer tick */
static void st25_rd_timer(void *opaque)
{
    St25r3916State *s = opaque;
    int64_t now = qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL);
    bool ntag = s->rd_prog && !strcmp(s->rd_prog, "ntag");
    uint8_t cmd[4];

    if (!s->rd_prog) {
        return;
    }
    if (s->rd_waiting && s->tm && s->tm_n && s->rd_late < RD_LATE_MAX) {
        /* The answer is on its way, edge by edge: the reader's time was
         * for its start, and the reader hears it out. */
        s->rd_late++;
        timer_mod(s->rd_timer, now + RD_LATE_NS);
        return;
    }
    s->rd_late = 0;
    if (st25_rd_on_link(s)) {
        st25_link_rd_timer(s);
        return;
    }
    if (s->rd_waiting) {                /* timed out: no answer */
        st25_rd_recorded(s, false);
        s->rd_waiting = false;
        s->rd_ans_bits = -1;
    }
    switch (s->rd_step) {
    case RD_FIELD_ON:
        st25_ext_field(s, true, s->ext_field);
        s->rd_step = RD_ACTIVATE;
        s->rd_tries = 0;
        timer_mod(s->rd_timer, now + 5 * SCALE_MS);
        return;
    case RD_ACTIVATE: {
        /* what the chip answers by itself from the passive-target memory */
        bool listening = (s->rega[REG_MODE] & MODE_TARG) &&
                         (s->rega[REG_OP_CONTROL] & OP_EN) && !s->pt_sleep;

        if (!listening) {
            if (++s->rd_tries < 40) {   /* the firmware may start late: 4 s */
                timer_mod(s->rd_timer, now + 100 * SCALE_MS);
                return;
            }
            s->rd_step = RD_FIELD_OFF;
            break;
        }
        s->rd_uid_len = (s->rega[REG_AUX] & 0x30) ? 7 : 4;
        memcpy(s->rd_uid, s->pt_mem, s->rd_uid_len);
        s->rd_atqa[0] = s->pt_mem[10];
        s->rd_atqa[1] = s->pt_mem[11];
        s->rd_sak = s->pt_mem[s->rd_uid_len == 4 ? 12 : 13];
        st25_raise(s, I_WU_A);
        if (s->rec) {
            g_autoptr(GString) uid = g_string_new(NULL);

            hex_put(uid, s->rd_uid, s->rd_uid_len);
            st25_rec_note(s, "reader a select > %s %02X%02X %02X", uid->str,
                          s->rd_atqa[0], s->rd_atqa[1], s->rd_sak);
        }
        s->rd_step = ntag ? RD_VERSION : RD_HALT;
        timer_mod(s->rd_timer, now + 1 * SCALE_MS);
        return;
    }
    case RD_VERSION:
        cmd[0] = 0x60;
        if (rd_ask(s, cmd, 1, 8)) {
            return;
        }
        if (rd_ok(s, 8)) {
            memcpy(s->rd_version, s->rd_ans, 8);
            s->rd_has_version = true;
            switch (s->rd_version[6]) {     /* storage size: pages */
            case 0x0B: s->rd_pages = 20; break;
            case 0x0E: s->rd_pages = 41; break;
            case 0x0F: s->rd_pages = 45; break;
            case 0x11: s->rd_pages = 135; break;
            case 0x13: s->rd_pages = 231; break;
            }
        }
        rd_next(s, s->rd_has_version ? RD_SIG : RD_PAGES, now);
        return;
    case RD_SIG:
        cmd[0] = 0x3C;
        cmd[1] = 0x00;
        if (rd_ask(s, cmd, 2, 32)) {
            return;
        }
        if (rd_ok(s, 32)) {
            memcpy(s->rd_sig, s->rd_ans, 32);
            s->rd_has_sig = true;
        }
        rd_next(s, RD_PAGES, now);
        return;
    case RD_PAGES: {
        int limit = s->rd_pages ? s->rd_pages : 256;

        if (s->rd_pages_read < limit) {
            cmd[0] = 0x30;
            cmd[1] = s->rd_pages_read;
            if (rd_ask(s, cmd, 2, 16)) {
                return;
            }
            if (rd_ok(s, 16)) {             /* four pages; a NAK past the end */
                memcpy(&s->rd_mem[s->rd_pages_read * 4], s->rd_ans,
                       MIN(16, (limit - s->rd_pages_read) * 4));
                s->rd_pages_read = MIN(s->rd_pages_read + 4, limit);
                if (s->rd_pages_read < limit) {
                    rd_next(s, RD_PAGES, now);
                    return;
                }
            }
        }
        rd_next(s, s->rd_has_version ? RD_COUNTER : RD_HALT, now);
        return;
    }
    case RD_COUNTER:
        cmd[0] = 0x39;
        cmd[1] = 0x02;
        if (rd_ask(s, cmd, 2, 3)) {
            return;
        }
        if (rd_ok(s, 3)) {
            memcpy(s->rd_cnt, s->rd_ans, 3);
            s->rd_has_cnt = true;
        }
        rd_next(s, RD_HALT, now);
        return;
    case RD_HALT:
        cmd[0] = 0x50;
        cmd[1] = 0x00;
        st25_rd_send(s, cmd, 2);
        st25_rd_recorded(s, false);
        s->rd_waiting = false;          /* HLTA has no answer */
        s->rd_step = RD_FIELD_OFF;
        timer_mod(s->rd_timer, now + 2 * SCALE_MS);
        return;
    case RD_FIELD_OFF:
        break;
    }
    st25_ext_field(s, false, s->ext_field);   /* RD_FIELD_OFF: done */
    st25_rd_write(s);
    g_free(s->rd_prog);
    s->rd_prog = NULL;
}

static bool st25_get_field(Object *obj, Error **errp)
{
    return ST25R3916(obj)->ext_field;
}

static void st25_set_field(Object *obj, bool value, Error **errp)
{
    St25r3916State *s = ST25R3916(obj);

    st25_ext_field(s, s->rd_field, value);
}

static char *st25_get_reader(Object *obj, Error **errp)
{
    return g_strdup(ST25R3916(obj)->rd_prog ?: "");
}

static void st25_set_reader(Object *obj, const char *value, Error **errp)
{
    St25r3916State *s = ST25R3916(obj);

    if (strcmp(value, "scan") && strcmp(value, "ntag")) {
        error_setg(errp, "nfc reader: scan (activate and identify) or ntag "
                   "(read a Type 2 tag)");
        return;
    }
    timer_del(s->rd_timer);
    g_free(s->rd_prog);
    s->rd_prog = g_strdup(value);
    s->rd_step = RD_FIELD_ON;
    s->rd_waiting = false;
    s->rd_ans_bits = -1;
    s->rd_uid_len = 0;
    s->rd_has_version = s->rd_has_sig = s->rd_has_cnt = false;
    s->rd_pages = s->rd_pages_read = 0;
    s->rd_tries = s->rd_sent = 0;
    timer_mod(s->rd_timer, qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL) + 1 * SCALE_MS);
}

static char *st25_get_tx(Object *obj, Error **errp)
{
    return g_strdup(ST25R3916(obj)->tx_last ?: "");
}

/* "LINES FRAMES NS PATH" while the air is recorded (the lines written so
 * far, the frames in them, ns since it began, the file), else nothing */
static char *st25_get_record(Object *obj, Error **errp)
{
    St25r3916State *s = ST25R3916(obj);

    if (!s->rec) {
        return g_strdup("");
    }
    return g_strdup_printf("%" PRIu64 " %" PRIu64 " %" PRId64 " %s",
                           s->rec_lines, s->rec_frames,
                           qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL) - s->rec_t0,
                           s->rec_path);
}

/* a file to record to, with its whole path; off: the record ends */
static void st25_set_record(Object *obj, const char *value, Error **errp)
{
    St25r3916State *s = ST25R3916(obj);
    FILE *f;

    if (!strcmp(value, "off")) {
        if (s->rec) {
            fclose(s->rec);
            s->rec = NULL;
        }
        return;
    }
    if (s->rec) {
        error_setg(errp, "record: it records to %s already (set it to off to "
                   "end that)", s->rec_path);
        return;
    }
    if (value[0] != '/') {
        error_setg(errp, "record: a file with its whole path, or off");
        return;
    }
    if (!s->rec_flush) {
        error_setg(errp, "record: set it on a machine that runs");
        return;
    }
    f = fopen(value, "w");
    if (!f) {
        error_setg_errno(errp, errno, "record: cannot write %s", value);
        return;
    }
    g_free(s->rec_path);
    s->rec_path = g_strdup(value);
    s->rec_t0 = qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL);
    s->rec_lines = s->rec_frames = 0;
    fprintf(f, "# FlipperEmu: the frames on the air at a Flipper Zero's NFC "
               "antenna\n"
               "# a line: the time in s; who asked (flipper: the firmware as a "
               "reader, of the card held to it;\n"
               "#   reader: a reader in front of the Flipper, of the card that "
               "the firmware emulates);\n"
               "#   the technology (a: ISO14443A, b: ISO14443B, f: FeliCa, v: "
               "ISO15693); the frame's bits and bytes\n"
               "#   (pXX: its bytes' parity bits where they are not the odd "
               "ones, low bit first);\n"
               "#   > the answer in the same way, or - for none\n"
               "# other lines: a field going on or off (the flipper's own, a "
               "reader's), and a reader's\n"
               "#   activation of the emulated card: \"reader a select > UID "
               "ATQA SAK\", \"reader f polling > IDm PMm SC\"\n");
    s->rec = f;
    st25_rec_flush(s);
}

/* ------------------------------------------------------------------ SPI */

enum { PH_CMD, PH_REG, PH_FIFO_LOAD, PH_SKIP, PH_FIFO_READ, PH_PT };

static uint32_t st25_transfer(SSIPeripheral *dev, uint32_t val)
{
    St25r3916State *s = ST25R3916(dev);
    uint8_t b = val;
    uint8_t ret = 0;

    switch (s->phase) {
    case PH_CMD:
        if (b == 0xFB) {                /* space B prefix */
            s->prefix_b = true;
        } else if (b == 0xFC) {         /* test register access: ignore */
            s->phase = PH_SKIP;
        } else if (b < 0x80) {
            s->read = b & 0x40;
            s->addr = b & 0x3F;
            s->space_b = s->prefix_b;
            s->prefix_b = false;
            s->phase = PH_REG;
        } else if (b == 0x80) {
            s->phase = PH_FIFO_LOAD;
        } else if (b == 0x9F) {
            s->phase = PH_FIFO_READ;
        } else if (b >= 0xA0 && b < 0xC0) {
            s->pt_read = b == 0xBF;
            s->pt_pos = b == 0xA8 ? 15 : b == 0xAC ? 36 : 0;
            s->phase = PH_PT;
        } else if (b >= 0xC0) {
            st25_direct(s, b);
        }
        return 0;
    case PH_REG:
        if (s->read) {
            ret = st25_read_reg(s, s->addr, s->space_b);
        } else {
            st25_write_reg(s, s->addr, s->space_b, b);
        }
        s->addr = (s->addr + 1) & 0x3F;
        return ret;
    case PH_FIFO_LOAD:
        if (s->fifo_len < sizeof(s->fifo)) {
            s->fifo[s->fifo_len++] = b;
        }
        return 0;
    case PH_FIFO_READ:
        return s->fifo_rd < s->fifo_len ? s->fifo[s->fifo_rd++] : 0;
    case PH_PT:
        if (s->pt_pos < sizeof(s->pt_mem)) {
            if (s->pt_read) {
                ret = s->pt_mem[s->pt_pos];
            } else {
                s->pt_mem[s->pt_pos] = b;
            }
            s->pt_pos++;
        }
        return ret;
    default:                            /* test access: swallow the frame */
        return 0;
    }
}

static int st25_set_cs(SSIPeripheral *dev, bool level)
{
    St25r3916State *s = ST25R3916(dev);

    if (s->tm && !level) {
        st25_tm_end(s);                 /* selected again: the mode's end */
    }
    s->phase = PH_CMD;
    s->prefix_b = false;
    return 0;
}

/* ------------------------------------------------------------ the card */

static char *st25_get_card(Object *obj, Error **errp)
{
    return g_strdup(ST25R3916(obj)->card_str ?: "");
}

static void st25_set_card(Object *obj, const char *value, Error **errp)
{
    St25r3916State *s = ST25R3916(obj);
    g_autofree char *contents = NULL;
    NfcCard *card = NULL;

    if (strcmp(value, "none") && strcmp(value, "") && strcmp(value, "''")) {
        g_autoptr(GError) err = NULL;

        if (!g_str_has_prefix(value, "file:")) {
            error_setg(errp, "nfc card: expected file:PATH (scripts/fe_nfc.py "
                       "makes one from a .nfc file) or none");
            return;
        }
        if (!g_file_get_contents(value + 5, &contents, NULL, &err)) {
            error_setg(errp, "nfc card: %s", err->message);
            return;
        }
        card = nfc_card_new(contents, errp);
        if (!card) {
            return;
        }
    }
    if (s->card) {
        nfc_card_free(s->card);
    }
    s->card = card;
    g_free(s->card_str);
    s->card_str = card ? g_strdup(value) : NULL;
    if (card && s->field_on) {
        nfc_card_field(card, true);     /* held into a field already on */
    }
}

static void st25_realize(SSIPeripheral *d, Error **errp)
{
    St25r3916State *s = ST25R3916(d);

    qdev_init_gpio_out_named(DEVICE(d), &s->irq, "irq", 1);
    s->timer = timer_new_ns(QEMU_CLOCK_VIRTUAL, st25_timer, s);
    s->rd_timer = timer_new_ns(QEMU_CLOCK_VIRTUAL, st25_rd_timer, s);
    s->tm_idle = timer_new_ns(QEMU_CLOCK_VIRTUAL, st25_tm_idle, s);
    s->v_timer = timer_new_ns(QEMU_CLOCK_VIRTUAL, st25_v_play, s);
    s->rec_flush = timer_new_ms(QEMU_CLOCK_REALTIME, st25_rec_flush, s);
    s->link_timeout = timer_new_ms(QEMU_CLOCK_REALTIME, st25_link_timeout, s);
    s->link_stop = qemu_bh_new(st25_link_stop, s);
    flipper_link_init(&s->lk, &s->link, "NFC", st25_link_line,
                      st25_link_opened, s);
}

/* what the chip is doing: reading with its own field on, or listening for
 * a reader's as a card (the firmware emulating one) */
static char *st25_get_radio(Object *obj, Error **errp)
{
    St25r3916State *s = ST25R3916(obj);

    if (s->field_on) {
        return g_strdup("field");
    }
    if ((s->rega[REG_MODE] & MODE_TARG) &&
        (s->rega[REG_OP_CONTROL] & OP_EN)) {
        return g_strdup("listen");
    }
    return g_strdup("idle");
}

static void st25_init(Object *obj)
{
    object_property_add_str(obj, "card", st25_get_card, st25_set_card);
    object_property_set_description(obj, "card",
        "The card in the field: file:PATH (from scripts/fe_nfc.py) or none");
    object_property_add_str(obj, "reader", st25_get_reader, st25_set_reader);
    object_property_set_description(obj, "reader",
        "Put a virtual reader in front of an emulating Flipper: scan | ntag");
    object_property_add_bool(obj, "field", st25_get_field, st25_set_field);
    object_property_set_description(obj, "field",
        "A reader's field around the Flipper with no reader talking (what "
        "field detection sees; \"reader\" brings its own)");
    object_property_add_str(obj, "radio", st25_get_radio, NULL);
    object_property_set_description(obj, "radio",
        "What the chip is doing: field (its own 13.56 MHz field is on: it "
        "is reading), listen (it waits for a reader, as a card) or idle");
    object_property_add_str(obj, "tx", st25_get_tx, NULL);
    object_property_set_description(obj, "tx",
        "What the virtual reader last read, as a .nfc file");
    object_property_add_str(obj, "record", st25_get_record, st25_set_record);
    object_property_set_description(obj, "record",
        "Record the frames on the air and their answers as text: a file with "
        "its whole path, or off (read: LINES FRAMES NS PATH)");
}

/*
 * ---- a saved state
 *
 * The chip as the firmware set it up and where it is in a frame, the card
 * held to the Flipper (nfc_card.c: all of it, so a talk with it goes on)
 * and a standing reader's field.  Not saved, because it was in the air: a
 * virtual reader's session and a question to a card on the link.  For the
 * loaded machine the reader is gone (its field goes off at the first run)
 * and the card on the link stays silent.  A load drives no line and writes
 * nothing to a link: st25r3916_loaded() does, when the machine runs.
 */
#define ST25_STR_MAX (1u << 20)         /* a card's name in a saved state */

static bool st25_card_needed(void *opaque)
{
    St25r3916State *s = opaque;

    return s->card != NULL;
}

static int st25_card_pre_load(void *opaque)
{
    St25r3916State *s = opaque;

    if (!s->card) {
        s->card = nfc_card_blank();
    }
    return 0;
}

static const VMStateDescription vmstate_st25_card = {
    .name = "st25r3916/card",
    .version_id = 5,                    /* (the card's: FeliCa's authentication) */
    .minimum_version_id = 5,
    .needed = st25_card_needed,
    .pre_load = st25_card_pre_load,
    .fields = (const VMStateField[]) {
        VMSTATE_STRUCT_POINTER(card, St25r3916State, vmstate_nfc_card,
                               NfcCard),
        VMSTATE_END_OF_LIST()
    }
};

static int st25_pre_save(void *opaque)
{
    St25r3916State *s = opaque;

    s->card_str_len = s->card_str ?
                      MIN(strlen(s->card_str) + 1, ST25_STR_MAX) : 0;
    return 0;
}

/* before the size is used: a damaged file asks for no memory */
static bool st25_size_ok(void *opaque, int version_id)
{
    St25r3916State *s = opaque;

    return s->card_str_len <= ST25_STR_MAX;
}

/*
 * Transparent mode, for a firmware that stays in it: one that emulates an
 * ISO15693 card is in it from the emulation's start to its end.  (A frame
 * that is on its way either way is not kept: a reader asks again.)
 */
static bool st25_tm_needed(void *opaque)
{
    St25r3916State *s = opaque;

    return s->tm;
}

static const VMStateDescription vmstate_st25_tm = {
    .name = "st25r3916/transparent",
    .version_id = 1,
    .minimum_version_id = 1,
    .needed = st25_tm_needed,
    .fields = (const VMStateField[]) {
        VMSTATE_BOOL(tm, St25r3916State),
        VMSTATE_END_OF_LIST()
    }
};

/* no card is held and no reader talks: the state says what is */
static int st25_pre_load(void *opaque)
{
    St25r3916State *s = opaque;

    nfc_card_free(s->card);
    s->card = NULL;
    g_free(s->card_str);
    s->card_str = NULL;
    timer_del(s->rd_timer);
    g_free(s->rd_prog);
    s->rd_prog = NULL;
    s->rd_waiting = false;
    s->rd_ans_bits = -1;
    s->tm = false;
    s->tm_n = 0;
    s->v_n = s->v_at = 0;
    s->v_level = false;
    timer_del(s->tm_idle);
    timer_del(s->v_timer);
    timer_del(s->link_timeout);
    return 0;
}

/* the lengths the frames are copied by: within their buffers */
static int st25_post_load(void *opaque, int version_id)
{
    St25r3916State *s = opaque;

    if (s->card_str_len) {
        s->card_str[s->card_str_len - 1] = '\0';
    }
    if (!s->card) {                     /* a name names a card */
        g_free(s->card_str);
        s->card_str = NULL;
    }
    if (s->fifo_len > sizeof(s->fifo) || s->fifo_rd > s->fifo_len ||
        s->ntx < 0 || s->ntx > (NFC_AIR_MAX - 1) * 8 ||
        s->nrx < 0 || s->nrx > NFC_AIR_MAX * 8) {
        return -EINVAL;
    }
    return 0;
}

void st25r3916_loaded(St25r3916State *s)
{
    if (s->stage == STAGE_LINK) {       /* asked in the saved machine's time */
        st25_silence(s, qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL));
    }
    if (s->rd_field) {                  /* the session's reader is gone */
        st25_ext_field(s, false, s->ext_field);
    }
    flipper_link_send(&s->lk, "field %s\n", s->field_on ? "on" : "off");
}

static const VMStateDescription vmstate_st25r3916 = {
    .name = "st25r3916",
    .version_id = 1,
    .minimum_version_id = 1,
    .pre_save = st25_pre_save,
    .pre_load = st25_pre_load,
    .post_load = st25_post_load,
    .fields = (const VMStateField[]) {
        VMSTATE_SSI_PERIPHERAL(parent_obj, St25r3916State),
        VMSTATE_UINT32(card_str_len, St25r3916State),
        VMSTATE_VALIDATE("a card's name of a size", st25_size_ok),
        VMSTATE_VBUFFER_ALLOC_UINT32(card_str, St25r3916State, 1, NULL,
                                     card_str_len),
        VMSTATE_UINT8_ARRAY(rega, St25r3916State, 64),
        VMSTATE_UINT8_ARRAY(regb, St25r3916State, 64),
        VMSTATE_UINT8_ARRAY(irq_mask, St25r3916State, 4),
        VMSTATE_UINT8_ARRAY(irq_status, St25r3916State, 4),
        VMSTATE_INT32(phase, St25r3916State),
        VMSTATE_UINT8(addr, St25r3916State),
        VMSTATE_BOOL(read, St25r3916State),
        VMSTATE_BOOL(space_b, St25r3916State),
        VMSTATE_BOOL(prefix_b, St25r3916State),
        VMSTATE_UINT8_ARRAY(fifo, St25r3916State, 512),
        VMSTATE_UINT16(fifo_len, St25r3916State),
        VMSTATE_UINT16(fifo_rd, St25r3916State),
        VMSTATE_UINT8(fifo_lb, St25r3916State),
        VMSTATE_UINT8_ARRAY(pt_mem, St25r3916State, 48),
        VMSTATE_UINT32(pt_pos, St25r3916State),
        VMSTATE_BOOL(pt_read, St25r3916State),
        VMSTATE_TIMER_PTR(timer, St25r3916State),
        VMSTATE_INT32(stage, St25r3916State),
        VMSTATE_UINT8_ARRAY(tx, St25r3916State, NFC_AIR_MAX),
        VMSTATE_UINT8_ARRAY(rx, St25r3916State, NFC_AIR_MAX),
        VMSTATE_INT32(ntx, St25r3916State),
        VMSTATE_INT32(nrx, St25r3916State),
        VMSTATE_BOOL(field_on, St25r3916State),
        VMSTATE_BOOL(rd_field, St25r3916State),
        VMSTATE_BOOL(ext_field, St25r3916State),
        VMSTATE_BOOL(pt_sleep, St25r3916State),
        VMSTATE_END_OF_LIST()
    },
    .subsections = (const VMStateDescription * const []) {
        &vmstate_st25_card,
        &vmstate_st25_tm,
        NULL
    }
};

static const Property st25_props[] = {
    DEFINE_PROP_STRING("tx-dir", St25r3916State, tx_dir),
    DEFINE_PROP_CHR("link", St25r3916State, link),  /* a card elsewhere */
};

static void st25_reset_hold(Object *obj, ResetType type)
{
    St25r3916State *s = ST25R3916(obj);

    if (s->timer && !flipper_board_keeps()) {
        st25_defaults(s);   /* (the MCU's reset: until a SET_DEFAULT) */
    }
    s->phase = PH_CMD;
    s->prefix_b = false;
    s->tm = false;
    s->tm_n = 0;
    s->v_n = s->v_at = 0;
    s->v_level = false;
    if (s->tm_idle) {
        timer_del(s->tm_idle);
        timer_del(s->v_timer);
    }
}

static void st25_reset_exit(Object *obj, ResetType type)
{
    St25r3916State *s = ST25R3916(obj);

    st25_field_changed(s);
    st25_update_irq(s);
}

static void st25_class_init(ObjectClass *klass, const void *data)
{
    SSIPeripheralClass *k = SSI_PERIPHERAL_CLASS(klass);
    ResettableClass *rc = RESETTABLE_CLASS(klass);

    k->realize = st25_realize;
    device_class_set_props(DEVICE_CLASS(klass), st25_props);
    DEVICE_CLASS(klass)->vmsd = &vmstate_st25r3916;
    k->transfer = st25_transfer;
    k->set_cs = st25_set_cs;
    k->cs_polarity = SSI_CS_LOW;
    rc->phases.hold = st25_reset_hold;
    rc->phases.exit = st25_reset_exit;
}

static const TypeInfo st25_types[] = {
    {
        .name = TYPE_ST25R3916,
        .parent = TYPE_SSI_PERIPHERAL,
        .instance_size = sizeof(St25r3916State),
        .instance_init = st25_init,
        .class_init = st25_class_init,
    },
};
DEFINE_TYPES(st25_types)
