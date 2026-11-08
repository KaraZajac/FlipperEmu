/*
 * Flipper Zero board-level devices (display, power ICs, radios)
 *
 * Copyright (c) 2026, Kara Zajac
 * SPDX-License-Identifier: BSD-3-Clause
 */
#ifndef HW_ARM_FLIPPER_FLIPPER_H
#define HW_ARM_FLIPPER_FLIPPER_H

#include "hw/ssi/ssi.h"
#include "hw/i2c/i2c.h"
#include "qom/object.h"
#include "qemu/notify.h"

/* ---------------------------------------------------------- ST7567 LCD */
#define TYPE_ST7567 "st7567"
OBJECT_DECLARE_SIMPLE_TYPE(St7567State, ST7567)
struct St7567State {
    SSIPeripheral parent_obj;
    struct QemuConsole *con;
    uint8_t ram[9][132];
    uint8_t page, col, start_line, contrast, pending_arg, rmw_col;
    bool adc_reverse, com_reverse, display_on, inverse, all_on, rmw;
    bool a0, rst_n;
    bool dirty, status_dirty;
    uint32_t scale;
    int border, status_h;
    /* board status shown next to the panel */
    uint8_t backlight;
    uint32_t led_rgb;
    bool vibro, speaker;
    uint64_t bytes;
};
bool st7567_pixel(St7567State *s, int x, int y);

/* ----------------------------------------------------- BQ27220 gauge */
#define TYPE_BQ27220 "bq27220"
OBJECT_DECLARE_SIMPLE_TYPE(Bq27220State, BQ27220)
struct Bq27220State {
    I2CSlave parent_obj;
    uint8_t ptr;                 /* command pointer */
    uint8_t wbuf[40];
    int wlen;
    bool reading;
    uint8_t snap[0x80];          /* register image for the current read */
    /* MAC / data memory */
    uint16_t mac_cmd;
    uint8_t mac_data[32];
    uint8_t dm[0x1000];          /* data memory 0x9000..0x9FFF */
    uint16_t pend_addr;
    uint8_t pend_data[32];
    int pend_len;
    uint8_t sec;                 /* 3 sealed, 2 unsealed, 1 full access */
    uint16_t last_key;
    bool initcomp, cfgupdate;
    /* battery */
    uint32_t soc, voltage_mv, temperature_dk, capacity_mah;
    int32_t current_ma;
};

/* ---------------------------------------------------- BQ25896 charger */
#define TYPE_BQ25896 "bq25896"
OBJECT_DECLARE_SIMPLE_TYPE(Bq25896State, BQ25896)
struct Bq25896State {
    I2CSlave parent_obj;
    uint8_t regs[0x15];
    uint8_t ptr;
    bool first;
    bool vbus;                   /* USB cable "plugged" */
    Bq27220State *gauge;
};

/* ------------------------------------------------- LP5562 LED driver */
#define TYPE_LP5562 "lp5562"
OBJECT_DECLARE_SIMPLE_TYPE(Lp5562State, LP5562)
struct Lp5562State {
    I2CSlave parent_obj;
    uint8_t regs[0x71];
    uint8_t ptr;
    bool first;
    void (*changed)(void *opaque);
    void *opaque;
};
void lp5562_rgbw(Lp5562State *s, uint8_t *r, uint8_t *g, uint8_t *b,
                 uint8_t *w);

/* ---------------------------------------------------- CC1101 sub-GHz */
#define TYPE_CC1101 "cc1101"
OBJECT_DECLARE_SIMPLE_TYPE(Cc1101State, CC1101)
struct Cc1101State {
    SSIPeripheral parent_obj;
    uint8_t regs[0x2F];
    uint8_t patable[8];
    uint8_t state;               /* MARCSTATE */
    int phase;                   /* 0: header expected */
    uint8_t addr;
    bool read, burst;
    int pa_idx;
    qemu_irq gdo0, gdo2;
    /* what is on the air: +high / -low µs at air_freq (0: any frequency) */
    struct WB55TimState *tim;    /* TIM2: GDO0 is its TI2 input */
    QEMUTimer *air_timer;
    int32_t *air;
    int air_len, air_pos;
    int64_t air_next_ns;
    uint32_t air_freq;
    bool carrier;                /* the air level right now */
    bool gdo0_level;             /* what GDO0 drives */
    /* transmit capture: GDO0 is the data input while in TX */
    char *tx_dir;                /* write each transmission here as .sub */
    GArray *tx;                  /* int32 +high/-low µs */
    int64_t tx_edge_ns;
    bool tx_level, tx_active;
    char *tx_last;               /* "@freq,+d,-d,..." of the last one */
};
void cc1101_tx_data(Cc1101State *s, bool level, int64_t when);

/* ---------------------------------------------------- ST25R3916 NFC */
#define TYPE_ST25R3916 "st25r3916"
OBJECT_DECLARE_SIMPLE_TYPE(St25r3916State, ST25R3916)
#define NFC_AIR_MAX 1024         /* bytes of a frame on the air (8192 bits) */
typedef struct NfcCard NfcCard;  /* a card in the field (nfc_card.c) */
struct St25r3916State {
    SSIPeripheral parent_obj;
    uint8_t rega[64], regb[64];
    uint8_t irq_mask[4], irq_status[4];
    int phase;                   /* SPI frame phase (PH_*) */
    uint8_t addr;
    bool read, space_b, prefix_b;
    qemu_irq irq;
    uint8_t fifo[512];
    uint16_t fifo_len, fifo_rd;  /* bytes in, bytes read out */
    uint8_t fifo_lb;             /* bits in an incomplete last byte */
    uint8_t pt_mem[48];          /* passive-target memory (listen mode) */
    unsigned pt_pos;
    bool pt_read;
    QEMUTimer *timer;
    int stage;                   /* what the timer ends: TX, RX start/end */
    uint8_t tx[NFC_AIR_MAX], rx[NFC_AIR_MAX];   /* frames on the air */
    int ntx, nrx;                /* their lengths in bits */
    bool field_on;
    NfcCard *card;
    char *card_str;
    /* listen mode: a virtual reader in front of the emulating Flipper */
    QEMUTimer *rd_timer;
    char *rd_prog;               /* "scan", "ntag" while a session runs */
    int rd_step, rd_tries, rd_sent;
    bool rd_field;               /* its field is on */
    bool ext_field;              /* a reader's field with no session ("field") */
    bool rd_waiting;             /* a command is out, the answer due */
    bool pt_sleep;               /* passive-target logic in SLEEP (HLTA) */
    uint8_t rd_ans[NFC_AIR_MAX]; /* the Flipper's answer, as bytes */
    int rd_ans_bits;             /* -1: none */
    uint8_t rd_uid[10], rd_atqa[2], rd_sak;
    int rd_uid_len;
    bool rd_has_version, rd_has_sig, rd_has_cnt;
    uint8_t rd_version[8], rd_sig[32], rd_cnt[3];
    uint8_t rd_mem[256 * 4];
    int rd_pages, rd_pages_read;
    char *tx_dir;                /* the reader's results go here as .nfc */
    char *tx_last;
};
/* ISO14443A helpers (st25r3916.c): CRC_A, bytes <-> air bits with parity */
uint16_t nfc_crc_a(const uint8_t *d, int n);
int nfc_air_encode(const uint8_t *data, int nbits, uint8_t *air);
int nfc_air_decode(const uint8_t *air, int nbits, uint8_t *data, bool *parity_ok);
/* the card (nfc_card.c) */
NfcCard *nfc_card_new(const char *desc, Error **errp);
void nfc_card_free(NfcCard *c);
void nfc_card_field(NfcCard *c, bool on);
/* a frame from the reader, as air bits; returns the answer's bits (0: none) */
int nfc_card_rx(NfcCard *c, const uint8_t *air, int nbits, uint8_t *out);

/* --------------------------------------------- piezo speaker (TIM16) */
#define TYPE_FLIPPER_SPEAKER "flipper-speaker"
OBJECT_DECLARE_SIMPLE_TYPE(FlipperSpeakerState, FLIPPER_SPEAKER)
struct FlipperSpeakerState {
    DeviceState parent_obj;
    struct AudioBackend *audio_be;
    struct SWVoiceOut *voice;
    bool on;
    uint32_t freq_mhz;           /* tone, milli-Hz */
    uint32_t duty_ppm;           /* PWM duty: how the firmware sets volume */
    uint32_t phase, phase_inc, threshold;
    int16_t hi, lo;
    Notifier exit;               /* close the voice: finishes WAV headers */
};
void flipper_speaker_set(FlipperSpeakerState *s, bool on, uint32_t freq_mhz,
                         uint32_t duty_ppm);

/* ------------------------------------------------ infrared receiver */
#define TYPE_FLIPPER_IR "flipper-ir"
OBJECT_DECLARE_SIMPLE_TYPE(FlipperIrState, FLIPPER_IR)
struct FlipperIrState {
    DeviceState parent_obj;
    qemu_irq pin;                /* PA0 */
    struct WB55TimState *tim;    /* TIM2: TI1 captures the edges */
    QEMUTimer *timer;
    uint32_t *dur;               /* mark/space durations being played, µs */
    int len, pos;
    int64_t next_ns;
    bool level;
    /* transmit: TIM1 puts the carrier on the IR LED during marks */
    GArray *tx;                  /* mark/space µs recorded so far */
    int64_t tx_edge_ns;          /* when the current mark or space began */
    bool tx_mark;
    uint32_t tx_freq, tx_duty_ppm;
    char *tx_dir;                /* write each transmission here as .ir */
    char *tx_last;
};
void flipper_ir_idle(FlipperIrState *s);
/* transmitting: from `when` on, the LED carries the carrier (a mark) or not */
void flipper_ir_tx(FlipperIrState *s, bool mark, int64_t when,
                   uint32_t freq_hz, uint32_t duty_ppm);
/* the transmitter stopped: save what was sent */
void flipper_ir_tx_end(FlipperIrState *s);

/* ------------------------------------------- 125 kHz RFID (virtual tag) */
#define TYPE_FLIPPER_RFID "flipper-rfid"
/* a T5577 in the 125 kHz field (t5577.c) */
typedef struct T5577 {
    uint32_t blk[8];             /* page 0; block 0 is the configuration */
    uint32_t p1[4];              /* page 1 */
    uint8_t lock;                /* page 0 blocks locked */
    bool on;                     /* the field, as the tag last saw it */
    int64_t since;               /* ... since then */
    int64_t on_start;            /* when the field came on before this gap */
    int64_t prev_on;             /* Tc of field before this gap */
    bool cmd;                    /* receiving a command */
    bool pushed;                 /* this gap's opening pushed a bit */
    int nbits;
    uint8_t bits[72];
} T5577;
#define T5577_DEFAULT_CONFIG 0x000880E8  /* Manchester RF/32, blocks 1-7, ST */
void t5577_blank(T5577 *t);
/* the field went on or off: true when page 0 changed (a new frame) */
bool t5577_field(T5577 *t, bool on, int64_t now, int64_t tc_ns);
/* when to poll: a command can close with the field still on (-1: none) */
int64_t t5577_deadline(const T5577 *t, int64_t tc_ns);
bool t5577_poll(T5577 *t, int64_t now, int64_t tc_ns);
/* what the comparator sees of the tag: +high/-low µs at 125 kHz, at most
 * max of them (-1: more; 0: silent); *psk for a PSK modulation */
int t5577_frame(const T5577 *t, int32_t *dur, int max, bool *psk);

OBJECT_DECLARE_SIMPLE_TYPE(FlipperRfidState, FLIPPER_RFID)
struct FlipperRfidState {
    DeviceState parent_obj;
    struct WB55CompState *comp;  /* COMP1: the demodulated antenna signal */
    struct WB55TimState *tim;    /* TIM2: captures COMP1's edges (pacing) */
    QEMUTimer *timer;
    int32_t *tag;                /* one frame: +high / -low µs at 125 kHz */
    char *tag_str;
    int len, pos;
    bool psk;                    /* a PSK tag: answers a PSK-mode read only */
    uint32_t field_hz;           /* the reader's field, 0 = off */
    bool psk_mode;               /* the reader drives the PSK way */
    int64_t next_ns;             /* the next edge on the tag's own timeline */
    int64_t shift_ns;            /* how far that timeline trails virtual time */
    bool waiting;                /* the next edge waits for a capture read */
    uint64_t n_edges, n_waits, n_forced;    /* "stats" */
    int64_t delivered_ns;        /* when the last edge reached COMP1 */
    int64_t read_ns;             /* when the firmware read its capture */
    int64_t isr_ns;              /* how long that took (moving average) */
    int64_t gap_ns;              /* the tag's own gap after the last edge */
    bool noise;                  /* comparator noise while no tag answers */
    bool noise_high;
    uint32_t rng;
    Notifier cap_read;           /* TIM2: the firmware read its capture */
    /* emulating: a virtual reader's field clocks TIM2 (ETR); CH3 loads it */
    bool reader;                 /* a reader's field around the Flipper */
    GArray *tx;                  /* the load so far: +on/-off carrier cycles */
    int64_t tx_t0, tx_last_ns;   /* first and latest edge */
    uint64_t tx_cycles;          /* cycles recorded so far */
    bool tx_level;
    QEMUTimer *tx_timer;         /* ends a capture after a silence */
    char *tx_dir;                /* write each capture here */
    char *tx_last;               /* the latest capture file */
    /* a T5577 instead of a fixed tag: written through the field's gaps */
    T5577 *t5;
    int64_t t5_tc_ns;            /* the field clock, as last seen */
    QEMUTimer *t5_timer;         /* a command can end with the field on */
};
#define FLIPPER_RFID_READER_HZ 125000
/* the reader's antenna drive (TIM1 CH1N PWM) frequency, 0 = off */
void flipper_rfid_set_drive(FlipperRfidState *s, uint32_t drive_hz);
/* emulating: the load on the antenna (TIM2 CH3 on RFID_PULL) changed */
void flipper_rfid_load(FlipperRfidState *s, bool on, int64_t when);

#endif
