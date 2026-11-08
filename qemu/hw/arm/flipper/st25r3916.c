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
 * (Custom-parity answers, MIFARE Classic's, go out by transparent mode on
 * a bit-banged pin, which this does not decode.)
 *
 * Copyright (c) 2026, Kara Zajac
 * SPDX-License-Identifier: BSD-3-Clause
 */
#include "qemu/osdep.h"
#include "qemu/timer.h"
#include "qemu/error-report.h"
#include "qapi/error.h"
#include "hw/core/irq.h"
#include "hw/core/qdev-properties.h"
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
#define MODE_TARG 0x80
#define I_CRC  2, 0x80
#define I_PAR  2, 0x40

#define BIT_NS 9439                     /* 128 / 13.56 MHz */
#define FDT_NS (40 * SCALE_US)          /* end of TX to start of the answer */

enum { STAGE_IDLE, STAGE_TX, STAGE_RXS, STAGE_RXE, STAGE_NRT };

static void st25_update_irq(St25r3916State *s)
{
    bool pending = false;

    for (int i = 0; i < 4; i++) {
        pending |= s->irq_status[i] & ~s->irq_mask[i];
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

/* the answer is in: decode it into the FIFO the way the chip is set up */
static void st25_rx_end(St25r3916State *s)
{
    bool raw = s->rega[REG_ISO14443A] & A_NO_RX_PAR;
    bool parity_ok = true;
    int bits;

    st25_fifo_clear(s);
    if (raw) {
        bits = s->nrx;
        memcpy(s->fifo, s->rx, (bits + 7) / 8);
    } else {
        bits = nfc_air_decode(s->rx, s->nrx, s->fifo, &parity_ok);
    }
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
        s->nrx = nfc_card_rx(s->card, s->tx, s->ntx, s->rx);
    }
    if (s->nrx > 0) {
        s->stage = STAGE_RXS;
        timer_mod(s->timer, now + FDT_NS);
        return;
    }
    /* silence: the no-response timer runs out, if it was set */
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
        if (s->rega[REG_ISO14443A] & A_NO_TX_PAR) {
            memcpy(s->tx, data, (nbits + 7) / 8);
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
    }
}
#define RD_ANSWER_NS (100 * SCALE_MS)   /* no answer by then: none */
#define RD_GAP_NS    (1 * SCALE_MS)     /* an answer to the next command */
#define RD_TRIES     3                  /* sends of one command, at most */

/* a reader command reaches the firmware: into the FIFO, RXS then RXE */
static void st25_rd_send(St25r3916State *s, const uint8_t *cmd, int n)
{
    uint16_t crc = nfc_crc_a(cmd, n);

    st25_fifo_clear(s);
    memcpy(s->fifo, cmd, n);
    s->fifo[n] = crc & 0xFF;
    s->fifo[n + 1] = crc >> 8;
    s->fifo_len = n + 2;
    s->rd_ans_bits = -1;
    s->rd_waiting = true;
    st25_raise(s, I_RXS);
    st25_raise(s, I_RXE);
    timer_mod(s->rd_timer, qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL) + RD_ANSWER_NS);
}

/* the firmware transmitted: that is the answer to the reader's command */
static void st25_rd_answer(St25r3916State *s)
{
    bool ok;

    if (!s->rd_waiting) {
        return;
    }
    s->rd_ans_bits = nfc_air_decode(s->tx, s->ntx, s->rd_ans, &ok);
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
    if (s->rd_waiting) {                /* timed out: no answer */
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
    object_property_add_str(obj, "tx", st25_get_tx, NULL);
    object_property_set_description(obj, "tx",
        "What the virtual reader last read, as a .nfc file");
}

static const Property st25_props[] = {
    DEFINE_PROP_STRING("tx-dir", St25r3916State, tx_dir),
};

static void st25_reset_hold(Object *obj, ResetType type)
{
    St25r3916State *s = ST25R3916(obj);

    if (s->timer) {
        st25_defaults(s);
    }
    s->phase = PH_CMD;
    s->prefix_b = false;
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
