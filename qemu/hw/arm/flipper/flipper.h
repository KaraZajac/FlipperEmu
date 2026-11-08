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
};

/* ---------------------------------------------------- ST25R3916 NFC */
#define TYPE_ST25R3916 "st25r3916"
OBJECT_DECLARE_SIMPLE_TYPE(St25r3916State, ST25R3916)
struct St25r3916State {
    SSIPeripheral parent_obj;
    uint8_t rega[64], regb[64];
    uint8_t irq_mask[4], irq_status[4];
    int phase;                   /* 0 command, 1 register data, 2 fifo, 3 skip */
    uint8_t addr;
    bool read, space_b, prefix_b;
    qemu_irq irq;
};

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
};
void flipper_ir_idle(FlipperIrState *s);

#endif
