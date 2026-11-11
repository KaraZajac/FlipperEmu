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
#include "chardev/char-fe.h"

/* ------------------------------------------- radio links (flipper_link.c) */
/* a line protocol on a chardev: a real radio, or a program, as an antenna */
typedef struct FlipperLink {
    CharFrontend *chr;
    const char *name;            /* for the log: "Sub-GHz", "NFC" */
    GString *in, *out;           /* a partial line in; bytes not yet sent */
    guint watch;
    bool open;                   /* a program is connected */
    void (*on_line)(void *opaque, char *line);
    void (*on_open)(void *opaque, bool open);
    void *opaque;
} FlipperLink;
void flipper_link_init(FlipperLink *l, CharFrontend *chr, const char *name,
                       void (*on_line)(void *opaque, char *line),
                       void (*on_open)(void *opaque, bool open), void *opaque);
void flipper_link_send(FlipperLink *l, const char *fmt, ...) G_GNUC_PRINTF(2, 3);

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
    uint32_t led_seen[2];        /* LED_FOR_...: the brightest since it looked */
    bool led_told[2];            /* ... and it has seen led_rgb as it is */
    uint32_t led_was;            /* the last colour that it had for long */
    int64_t led_since;           /* virtual ns: it has been led_rgb since */
    int64_t led_at;              /* ms (host) when led_seen was last set */
    bool vibro, speaker;
    uint64_t bytes;
};
bool st7567_pixel(St7567State *s, int x, int y);
/* the LED, for one who looks at it now and then (st7567.c) */
enum { LED_FOR_PICTURE, LED_FOR_PANEL };
void st7567_set_led(St7567State *s, uint32_t rgb);
uint32_t st7567_led(St7567State *s, int who);

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
    bool sysdwn;                 /* the battery is too low to go on */
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
    /* the battery was cut off with nothing on VBUS: the Flipper is off */
    void (*power_off)(void *opaque);
    void *opaque;
};

/* ------------------------------------------------- LP5562 LED driver */
#define TYPE_LP5562 "lp5562"
OBJECT_DECLARE_SIMPLE_TYPE(Lp5562State, LP5562)
/* one of its three program engines */
typedef struct Lp5562Engine {
    uint8_t pc, pwm;
    uint8_t peak;                /* the highest PWM since it was loaded */
    uint8_t loops[16];           /* how often the branch there was taken */
    uint8_t trig, need;          /* triggers it has got; those it waits for */
    uint8_t ramp;                /* steps left of the ramp it is in */
    bool down;
    bool busy;                   /* it is in a command */
    bool blocked;                /* ... that waits for a trigger */
    int64_t step;                /* ns: a step of the ramp */
    int64_t wait;                /* ns until what it is at is over */
} Lp5562Engine;
struct Lp5562State {
    I2CSlave parent_obj;
    uint8_t regs[0x71];
    uint8_t ptr;
    bool first;
    Lp5562Engine eng[3];
    QEMUTimer *timer;
    int64_t at;                  /* virtual ns the engines are at */
    uint32_t shown;              /* the outputs `changed` was last called for */
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
    /* a radio bridge on the "link" chardev (scripts/fe_sdr.py: an SDR) */
    CharFrontend link;
    FlipperLink lk;
    uint32_t link_epoch;         /* bumped whenever the receive set-up changes */
    char *link_cfg;              /* that set-up, as last announced */
    QEMUTimer *link_timer;       /* announces it once the writes settle */
    int32_t *live;               /* the bridge's air: a ring of +high/-low µs */
    unsigned live_rd, live_wr;
    int64_t live_queued_us, live_next_ns;
    QEMUTimer *live_timer;
    bool live_playing, live_carrier;
    float *spec;                 /* the bridge's spectrum: dBm per bin */
    int spec_n;
    double spec_f0, spec_step;   /* bin 0's frequency, the bin spacing (Hz) */
    int64_t spec_ns;             /* when it came */
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
    /* a card on the "link" chardev: a program, or real hardware's antenna */
    CharFrontend link;
    FlipperLink lk;
    QEMUTimer *link_timeout;     /* host time: no answer by then is silence */
    QEMUBH *link_stop;           /* stops the machine, outside the timers */
    bool link_paused;            /* the machine stopped while it answers */
    bool link_late;              /* warned about a slow answer */
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
    bool mute;                   /* property: no sound from it */
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

#define TYPE_FLIPPER_IBUTTON "flipper-ibutton"
OBJECT_DECLARE_SIMPLE_TYPE(FlipperIbuttonState, FLIPPER_IBUTTON)
struct FlipperIbuttonState {
    DeviceState parent_obj;
    struct WB55CompState *comp;  /* COMP1: what the contact senses */
    FlipperRfidState *rfid;      /* its field off: COMP1 is the iButton reader's */
    qemu_irq pull;               /* PB14: 0 pulls the line low, 1 lets go */
    QEMUTimer *timer;            /* lets go if the master never samples */
    QEMUTimer *wave_timer;
    char *key_str;
    int kind;
    /* a 1-Wire key */
    uint8_t rom[8];
    uint8_t *mem;
    uint32_t mem_len;
    int addr_bytes;              /* READ MEMORY's address: 2 bytes, 1, 0 none */
    bool m_low;                  /* the master drives the line low */
    bool pulling, presence;
    bool od;                     /* overdrive speed */
    int64_t fall_ns;             /* the master's last falling edge */
    int state, bit, phase, count;
    uint8_t rx;
    uint32_t addr;
    uint8_t txbuf[8];            /* what OW_TX sends, then tx_next */
    int tx_bytes, tx_next;
    /* a blank: its ROM can be written */
    int blank;                   /* RW1990.1, RW1990.2, TM01x, TM2004 */
    bool wr_enabled;             /* its write flag */
    uint8_t newrom[8];           /* the ROM being written */
    uint8_t tm_byte;             /* TM2004: the byte being programmed */
    /* a Cyfral or Metakom key */
    int32_t *wave;               /* +high/-low µs, repeated */
    int wave_len, wave_pos;
    bool comp_on, playing;
    bool awaiting;               /* the last edge waits for the ISR's read */
    int64_t edge_t, next_t;      /* that edge and the next on the key's timeline */
    struct WB55DwtState *dwt;    /* stamps the ISR's CYCCNT read */
    /* the Flipper emulates a key: a virtual reader, and captures */
    char *rd_prog;               /* "read" while a session runs */
    QEMUTimer *rd_timer;
    int rd_state, rd_op, rd_nops, rd_bit, rd_left, rd_reads;
    int rd_retries, rd_mem_total;   /* a memory read resumes after a dropout */
    int rd_stuck, rd_retry_len;     /* retries since the last new byte */
    int rd_skip;                    /* bytes read again, to check, on a resume */
    uint8_t rd_byte;                /* the byte being read */
    int rd_ops[8][2];            /* reset, write byte, read n bytes */
    bool rd_pull, rd_fw_pulled, rd_present;
    int64_t rd_t;
    uint8_t rd_buf[8 + 8192];    /* ROM, then memory */
    int rd_len;
    GArray *cap;                 /* Cyfral/Metakom: +high/-low µs */
    bool cap_done;               /* written full: the rest is ignored */
    QEMUTimer *cap_timer;
    char *tx_dir, *tx_last;
};
/* COMP1 was enabled or disabled */
void flipper_ibutton_comp(FlipperIbuttonState *s, bool on);
/* the CPU read COMP1's CSR */
void flipper_ibutton_comp_read(FlipperIbuttonState *s);
/* the CPU read GPIOB's IDR (fw_released: its own drive of PB14 is off) */
void flipper_ibutton_idr_read(FlipperIbuttonState *s, bool fw_released);
/* the CPU wrote PB14: it drives the line low, or released (or not an output) */
void flipper_ibutton_fw_write(FlipperIbuttonState *s, bool low);
/* emulating a Cyfral/Metakom key, the firmware set PB14 high/low for ns */
void flipper_ibutton_emulated(FlipperIbuttonState *s, bool high, int64_t ns);
/* PB14 is no longer an output: a Cyfral/Metakom emulation stopped */
void flipper_ibutton_released(FlipperIbuttonState *s);
/* a system reset: the line pulled up, the slave idle */
void flipper_ibutton_reset(FlipperIbuttonState *s);
/* the reader's antenna drive (TIM1 CH1N PWM) frequency, 0 = off */
void flipper_rfid_set_drive(FlipperRfidState *s, uint32_t drive_hz);
/* emulating: the load on the antenna (TIM2 CH3 on RFID_PULL) changed */
void flipper_rfid_load(FlipperRfidState *s, bool on, int64_t when);

/* ------------------------------------- front panel (flipper_panel.c) */
/* the buttons, in the order of the panel link's names for them */
enum { BTN_UP, BTN_DOWN, BTN_LEFT, BTN_RIGHT, BTN_OK, BTN_BACK, BTN_NUM };

/*
 * The Flipper's face on a link (docs/PANEL_LINK.md): the picture on the
 * LCD, the backlight, the LED, the vibration motor and the speaker go out;
 * button presses come in.  A window drawn around the emulator is its other
 * end.  It also reports QOM properties the window asks to watch.
 */
#define TYPE_FLIPPER_PANEL "flipper-panel"
OBJECT_DECLARE_SIMPLE_TYPE(FlipperPanelState, FLIPPER_PANEL)
struct FlipperPanelState {
    DeviceState parent_obj;
    CharFrontend link;
    FlipperLink lk;
    St7567State *lcd;            /* the picture; it has the lights' state too */
    FlipperSpeakerState *speaker;
    void (*key)(void *opaque, int button, bool down);
    void *key_opaque;
    QEMUTimer *tick;             /* host time, while a window is connected */
    QEMUTimer *release;          /* emulated time: a press's minimum length */
    uint8_t frame[1024];         /* as last sent */
    bool frame_sent;
    uint64_t lcd_bytes;          /* the LCD's data counter at the last tick */
    int settling;                /* ticks spent waiting for a frame's end */
    int64_t props_at;
    uint32_t backlight, led, freq, duty;
    bool vibro;
    uint32_t held, releasing;    /* bit per button */
    int64_t pressed_at[BTN_NUM];
    GPtrArray *watches;
    int stopped;                 /* as last sent: a RunState, or -1: it runs */
};
/* the speaker or the motor changed: tell the window now, not at its tick */
void flipper_panel_sync(FlipperPanelState *s);
/* the firmware turned the Flipper off; the emulator is about to exit */
void flipper_panel_power_off(FlipperPanelState *s);
/* the firmware has halted, with its PC there */
void flipper_panel_halted(FlipperPanelState *s, uint32_t at);
/* A link holds time still while the other side answers: its own stop, which
 * is not a pause (and which a pause outlasts).  -> it did stop the machine,
 * and flipper_vm_release() is to follow */
bool flipper_vm_hold(void);
void flipper_vm_release(void);

/* ------------------------------------ secure enclave (flipper_enclave.c) */
/* what the factory stores in the SoC's FUS; call before realizing the SoC */
struct WB55IpccState;
void flipper_enclave_setup(struct WB55IpccState *fus);

#endif
