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
 * Copyright (c) 2026, Kara Zajac
 * SPDX-License-Identifier: BSD-3-Clause
 */
#include "qemu/osdep.h"
#include "qemu/timer.h"
#include "qemu/error-report.h"
#include "qapi/error.h"
#include "hw/core/irq.h"
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
               (st25_field(s) ? 0x20 : 0);                          /* tx_on */
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
}

static void st25_init(Object *obj)
{
    object_property_add_str(obj, "card", st25_get_card, st25_set_card);
    object_property_set_description(obj, "card",
        "The card in the field: file:PATH (from scripts/fe_nfc.py) or none");
}

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
