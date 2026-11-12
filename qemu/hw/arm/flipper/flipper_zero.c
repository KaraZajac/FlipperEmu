/*
 * Flipper Zero ("f7" target) machine
 *
 * Board wiring (targets/f7/furi_hal/furi_hal_resources.[ch]):
 *   SPI2 (bus "d"): ST7567 LCD  CS=PC11 DI(A0)=PB1 RST=PB0
 *                   microSD     CS=PC12 CD=PC10 (low = card present)
 *   SPI1 (bus "r"): CC1101      CS=PD0  GDO0=PA1
 *                   ST25R3916   CS=PE4  IRQ=PA2
 *   I2C1 (power):   BQ27220 0x55, BQ25896 0x6B, LP5562 0x30
 *   Buttons: Up PB10, Down PC6, Left PB11, Right PB12, Back PC13 (active low,
 *            pull-up) and OK PH3 (active high, pull-down)
 *   Vibro PA8, speaker PB8 (TIM16 CH1), USART1 log console PB6/PB7
 *   GPIO header: 2 PA7, 3 PA6, 4 PA4, 5 PB3, 6 PB2, 7 PC3, 10 PA14 (SWCLK),
 *            12 PA13 (SWDIO), 13 PB6 (TX), 14 PB7 (RX), 15 PC1, 16 PC0,
 *            17 PB14 (iButton); /machine "header" shows them and holds them,
 *            "header-record" writes every change of their levels to a file
 *   I2C3 (header):  SDA PC1 (15), SCL PC0 (16), devices by -device
 *            TYPE,bus=i2c3,address=N; /machine "header-i2c" says what is
 *            there and what was called, "header-i2c-record" writes every
 *            transfer to a file
 *
 * Usage:
 *   qemu-system-arm -M flipper-zero -kernel firmware.elf \
 *       -drive if=sd,format=raw,file=sd.img -serial stdio
 *   -drive if=pflash,format=raw,file=flash.img  persistent 1 MiB flash
 *   -M flipper-zero,nvram=FILE                  battery-backed state
 *   -M flipper-zero,enclave=FILE                the secure enclave's keys
 *   -M flipper-zero,usb-cable=off               start with USB unplugged
 *   -M flipper-zero,sd-card=off                 ... and the microSD pulled out
 *   -global flipper-panel.link=CHARDEV          the front panel, for a window
 *                                               around it (docs/PANEL_LINK.md)
 *
 * Keys: arrows = D-pad, Enter/Space = OK, Backspace/Escape = Back.
 *
 * Copyright (c) 2026, Kara Zajac
 * SPDX-License-Identifier: BSD-3-Clause
 */
#include "qemu/osdep.h"
#include "qapi/error.h"
#include "qemu/error-report.h"
#include "qemu/cutils.h"
#include "hw/core/boards.h"
#include "hw/core/qdev-properties.h"
#include "hw/core/qdev-properties-system.h"
#include "chardev/char.h"
#include "hw/core/irq.h"
#include "hw/arm/boot.h"
#include "hw/arm/machines-qom.h"
#include "hw/sd/sd.h"
#include "system/block-backend.h"
#include "system/blockdev.h"
#include "ui/input.h"
#include "system/reset.h"
#include "system/runstate.h"
#include "system/system.h"
#include "system/address-spaces.h"
#include "system/cpu-throttle.h"
#include "exec/cpu-common.h"
#include "target/arm/cpu.h"
#include "wb55.h"
#include "flipper.h"

#define TYPE_FLIPPER_ZERO MACHINE_TYPE_NAME("flipper-zero")
OBJECT_DECLARE_SIMPLE_TYPE(FlipperZeroState, FLIPPER_ZERO)

#define HDR_PINS 13                 /* the GPIO header's signal pins */

/*
 * The microSD slot when no card is in it.  Nothing drives MISO then, and the
 * board's pull-up holds it high: every byte the firmware clocks in with the
 * slot selected is 0xFF.  That is how it learns that nothing answers (it
 * waits for a byte that is not 0xFF, eight times); zeroes would be an R1
 * that says "done", and the wait for a card that is no longer busy would
 * never end.
 */
#define TYPE_FLIPPER_SD_SLOT "flipper-sd-slot"
OBJECT_DECLARE_SIMPLE_TYPE(FlipperSdSlot, FLIPPER_SD_SLOT)

struct FlipperSdSlot {
    SSIPeripheral parent_obj;
    bool empty;
};

static uint32_t flipper_sd_slot_transfer(SSIPeripheral *dev, uint32_t tx)
{
    return FLIPPER_SD_SLOT(dev)->empty ? 0xff : 0;
}

static void flipper_sd_slot_realize(SSIPeripheral *dev, Error **errp)
{
}

static void flipper_sd_slot_class_init(ObjectClass *oc, const void *data)
{
    SSIPeripheralClass *k = SSI_PERIPHERAL_CLASS(oc);

    k->realize = flipper_sd_slot_realize;
    k->transfer = flipper_sd_slot_transfer;
    k->cs_polarity = SSI_CS_LOW;
}

struct FlipperZeroState {
    MachineState parent_obj;
    WB55SocState soc;
    St7567State *lcd;
    Lp5562State *led;
    FlipperSpeakerState *speaker;
    FlipperPanelState *panel;
    FlipperIrState *ir;
    FlipperRfidState *rfid;
    FlipperIbuttonState *ibutton;
    Cc1101State *cc1101;
    bool tim1_running;
    Bq27220State *gauge;
    Bq25896State *charger;
    char *name;
    char *usb_serial;            /* chardev id bridged to USB CDC */
    char *usbip;                 /* chardev id serving USB/IP (TCP 3240) */
    uint32_t scale;
    uint32_t region;
    bool legacy_sleep;
    int log_level;               /* the firmware's, to start with; -1: as is */
    bool usb_cable;              /* plugged in from the start */
    QemuInputHandlerState *input;
    uint32_t keys_down;          /* bit per button */
    bool sd_drive;               /* there is a card: -drive if=sd */
    bool sd_in;                  /* ... and it is in the slot */
    bool sd_cs;                  /* PC12: the slot's chip select */
    DeviceState *sd_spi;         /* the card's SPI side */
    FlipperSdSlot *sd_slot;      /* what the slot answers without a card */
    char *nvram;                 /* battery-backed state file */
    char *enclave;               /* the FUS's key store (flipper_enclave.c) */
    QEMUTimer *halt_watch;       /* has the firmware halted? */
    uint32_t halt_at;            /* where, when it last looked (0: it has not) */
    bool halt_rest;              /* ... and the CPU rests because of it */
    bool halt_said;
    uint8_t hdr_hold[HDR_PINS];  /* what the outside holds on a header pin */
    QEMUTimer *hdr_rx_end[2];    /* a start bit on a serial port's RX pin */
    bool hdr_rx_low[2];
    FILE *hdr_rec;               /* the record of the header's levels (VCD) */
    char *hdr_rec_path;
    char hdr_rec_last[HDR_PINS]; /* ... each pin's level as it has it */
    int64_t hdr_rec_t0;          /* ... when it began (virtual clock, ns) */
    int64_t hdr_rec_at;          /* ... and the last time written to it */
    uint64_t hdr_rec_changes;
    QEMUTimer *hdr_rec_flush;    /* what was recorded reaches the file */
    FILE *i2c_rec;               /* the record of the header's I2C bus (text) */
    char *i2c_rec_path;
    int64_t i2c_rec_t0;          /* ... when it began (virtual clock, ns) */
    bool i2c_rec_part;           /* ... a line is begun; of bytes read */
    bool i2c_rec_reads;
    uint64_t i2c_rec_calls, i2c_rec_unanswered;
    uint64_t i2c_rec_written, i2c_rec_read;     /* bytes */
    Notifier exit_notifier;
};

/*
 * Battery-backed state that survives "power off" on a real Flipper: the fuel
 * gauge's data memory (its own flash) and the RTC backup domain (calendar,
 * BKP0R..19R with boot mode, settings flags, crash record).
 */
#define NVRAM_MAGIC   0x564E4546u   /* "FENV" */
#define NVRAM_VERSION 2
typedef struct FlipperNvram {
    uint32_t magic, version;
    uint8_t gauge_sec, gauge_initcomp, gauge_cfgupdate, pad;
    uint8_t gauge_dm[0x1000];
    WB55RtcBackup rtc;
} FlipperNvram;

static const struct {
    char port;
    int pin;
    bool active_high;
} buttons[BTN_NUM] = {
    [BTN_UP] = { 'B', 10, false }, [BTN_DOWN] = { 'C', 6, false },
    [BTN_LEFT] = { 'B', 11, false }, [BTN_RIGHT] = { 'B', 12, false },
    [BTN_OK] = { 'H', 3, true }, [BTN_BACK] = { 'C', 13, false },
};

static DeviceState *spi_dev(SSIBus *bus, const char *type, uint8_t cs)
{
    DeviceState *dev = qdev_new(type);

    qdev_prop_set_uint8(dev, "cs", cs);
    ssi_realize_and_unref(dev, bus, &error_fatal);
    return dev;
}

/* an SPI chip as a named machine child: /machine/subghz, /machine/nfc */
static DeviceState *spi_named(MachineState *m, SSIBus *bus, const char *type,
                              uint8_t cs, const char *name)
{
    DeviceState *dev = qdev_new(type);

    qdev_prop_set_uint8(dev, "cs", cs);
    object_property_add_child(OBJECT(m), name, OBJECT(dev));
    ssi_realize_and_unref(dev, bus, &error_fatal);
    return dev;
}

/* I2C chips as named machine children: /machine/gauge, /machine/charger ... */
static I2CSlave *flipper_i2c_dev(MachineState *m, I2CBus *bus, const char *type,
                                 uint8_t addr, const char *name)
{
    I2CSlave *dev = i2c_slave_new(type, addr);

    object_property_add_child(OBJECT(m), name, OBJECT(dev));
    i2c_slave_realize_and_unref(dev, bus, &error_fatal);
    return dev;
}

static qemu_irq soc_in(FlipperZeroState *s, char port, int pin)
{
    return qdev_get_gpio_in_named(DEVICE(&s->soc), "gpio-in",
                                  wb55_pin(port, pin));
}

static void soc_out(FlipperZeroState *s, char port, int pin, qemu_irq to)
{
    qdev_connect_gpio_out_named(DEVICE(&s->soc), "gpio-out",
                                wb55_pin(port, pin), to);
}

/* ----------------------------------------------------------------- input */

static int key_to_button(int qcode)
{
    switch (qcode) {
    case Q_KEY_CODE_UP:        return BTN_UP;
    case Q_KEY_CODE_DOWN:      return BTN_DOWN;
    case Q_KEY_CODE_LEFT:      return BTN_LEFT;
    case Q_KEY_CODE_RIGHT:     return BTN_RIGHT;
    case Q_KEY_CODE_RET:
    case Q_KEY_CODE_KP_ENTER:
    case Q_KEY_CODE_SPC:       return BTN_OK;
    case Q_KEY_CODE_BACKSPACE:
    case Q_KEY_CODE_ESC:       return BTN_BACK;
    default:                   return -1;
    }
}

static void flipper_button(FlipperZeroState *s, int b, bool down)
{
    bool level = buttons[b].active_high ? down : !down;

    if (down) {
        s->keys_down |= 1u << b;
    } else {
        s->keys_down &= ~(1u << b);
    }
    qemu_set_irq(soc_in(s, buttons[b].port, buttons[b].pin), level);
}

/* a button on the front panel's link */
static void flipper_panel_key(void *opaque, int b, bool down)
{
    flipper_button(opaque, b, down);
}

static void flipper_input_event(DeviceState *dev, QemuConsole *src,
                                QemuInputEvent *evt)
{
    FlipperZeroState *s = FLIPPER_ZERO(qdev_get_machine());
    int b = key_to_button(qemu_input_linux_to_qcode(evt->key.key));

    if (b >= 0 && evt->key.down != !!(s->keys_down & (1u << b))) {
        flipper_button(s, b, evt->key.down);
    }
}

static const QemuInputHandler flipper_input_handler = {
    .name = "Flipper Zero buttons",
    .mask = INPUT_EVENT_MASK_KEY,
    .event = flipper_input_event,
};

/* --------------------------------------------- indicators on the window */

static void flipper_led_changed(void *opaque)
{
    FlipperZeroState *s = opaque;
    uint8_t r, g, b, w;

    lp5562_rgbw(s->led, &r, &g, &b, &w);
    s->lcd->backlight = w;
    st7567_set_led(s->lcd, (r << 16) | (g << 8) | b);
    s->lcd->dirty = true;
}

static void flipper_vibro(void *opaque, int n, int level)
{
    FlipperZeroState *s = opaque;
    s->lcd->vibro = level;
    s->lcd->status_dirty = true;
    flipper_panel_sync(s->panel);
}

/*
 * The piezo on PB8 is driven by TIM16 CH1 PWM: furi_hal_speaker sets the tone
 * with the timer period, HCLK / ((PSC+1)(ARR+1)), and the volume with the
 * duty cycle, CCR1 / (ARR+1). Outputs need CEN, CC1E and MOE (BDTR).
 */
static void flipper_tim16_changed(void *opaque)
{
    FlipperZeroState *s = opaque;
    const uint32_t *r = s->soc.tim16->regs;
    uint32_t psc = r[0x28 / 4] & 0xFFFF, arr = r[0x2C / 4] & 0xFFFF;
    uint32_t ccr1 = r[0x34 / 4] & 0xFFFF, ccmr1 = r[0x18 / 4];
    int oc1m = ((ccmr1 >> 4) & 7) | ((ccmr1 >> 13) & 8);
    bool on = (r[0x00 / 4] & 1) && (r[0x20 / 4] & 1) &&
              (r[0x44 / 4] & (1u << 15)) && (oc1m == 6 || oc1m == 7) && arr;
    uint32_t freq_mhz = 0, duty = 0;

    if (on) {
        uint64_t clk = clock_get_hz(s->soc.rcc.hclk);

        freq_mhz = clk * 1000 / ((uint64_t)(psc + 1) * (arr + 1));
        duty = MIN((uint64_t)ccr1 * 1000000 / (arr + 1), 1000000);
        if (oc1m == 7) {
            duty = 1000000 - duty;      /* PWM mode 2 */
        }
    }
    flipper_speaker_set(s->speaker, on, freq_mhz, duty);
    if (s->lcd->speaker != (on && duty)) {
        s->lcd->speaker = on && duty;
        s->lcd->status_dirty = true;
    }
    flipper_panel_sync(s->panel);
}

/*
 * The infrared transmitter: TIM1 PWM on CH3N (the IR LED, PB9) or CH1N (an
 * external LED on PA7) while OCxM is PWM, i.e. during marks; the update
 * events (every RCR+1 carrier periods) are the mark/space boundaries.
 * Returns whether the carrier is on, with its frequency and duty.
 */
static bool flipper_ir_carrier(FlipperZeroState *s, uint32_t *hz, uint32_t *duty)
{
    const uint32_t *r = s->soc.tim1.regs;
    uint32_t ccer = r[0x20 / 4], arr = r[0x2C / 4] & 0xFFFF;
    uint32_t psc = r[0x28 / 4] & 0xFFFF;
    int ch = (ccer & (1u << 10)) ? 2 : (ccer & (1u << 2)) ? 0 : -1;
    uint32_t ccmr = ch == 2 ? r[0x1C / 4] : r[0x18 / 4];
    int m = ((ccmr >> 4) & 7) | ((ccmr >> 13) & 8);
    uint64_t ccr, f;

    if (ch < 0 || !(r[0x00 / 4] & 1) || !(r[0x44 / 4] & (1u << 15)) ||
        (m != 6 && m != 7)) {
        return false;
    }
    f = clock_get_hz(s->soc.rcc.hclk) / ((uint64_t)(psc + 1) * (arr + 1));
    if (f < 10000 || f > 60000) {
        return false;                   /* not an IR carrier (RFID: 125 kHz) */
    }
    ccr = MIN(r[(0x34 + 4 * ch) / 4] & 0xFFFF, arr + 1);
    *hz = f;
    *duty = (m == 6 ? ccr : arr + 1 - ccr) * 1000000 / (arr + 1);
    return true;
}

static void flipper_tim1_update(void *opaque, int64_t when)
{
    FlipperZeroState *s = opaque;
    uint32_t hz = 0, duty = 0;
    bool mark = flipper_ir_carrier(s, &hz, &duty);

    flipper_ir_tx(s->ir, mark, when, hz, duty);
}

/*
 * The 125 kHz RFID reader drives its antenna with TIM1 CH1N PWM (PB13):
 * frequency HCLK / ((PSC+1)(ARR+1)); it needs CEN, CC1NE and MOE.  For the
 * IR transmitter, TIM1 starting (CEN) begins the first mark, and stopping
 * ends the transmission; marks and spaces between come from updates.
 */
static void flipper_tim1_changed(void *opaque)
{
    FlipperZeroState *s = opaque;
    const uint32_t *r = s->soc.tim1.regs;
    uint32_t psc = r[0x28 / 4] & 0xFFFF, arr = r[0x2C / 4] & 0xFFFF;
    uint32_t ccmr1 = r[0x18 / 4];
    int oc1m = ((ccmr1 >> 4) & 7) | ((ccmr1 >> 13) & 8);
    bool running = r[0x00 / 4] & 1;
    bool field = running && (r[0x20 / 4] & (1u << 2)) &&
                 (r[0x44 / 4] & (1u << 15)) && (oc1m == 6 || oc1m == 7) && arr &&
                 (r[0x34 / 4] & 0xFFFF);
    uint32_t hz = 0, duty = 0;

    flipper_rfid_set_drive(s->rfid, field ? clock_get_hz(s->soc.rcc.hclk) /
                                            ((uint64_t)(psc + 1) * (arr + 1)) : 0);
    if (running != s->tim1_running) {
        s->tim1_running = running;
        if (running && flipper_ir_carrier(s, &hz, &duty)) {
            flipper_ir_tx(s->ir, true, qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL),
                          hz, duty);
        } else if (!running) {
            flipper_ir_tx_end(s->ir);
        }
    }
}

/* ------------------------------------------------- OTP / unique device ID */

static void flipper_provision(FlipperZeroState *s, uint64_t uid64)
{
    uint8_t *info = wb55_flash_info_ptr(&s->soc.flash);
    uint8_t *otp = info + (WB55_OTP_BASE - WB55_INFO_BASE);
    char name[9] = { 0 };

    /* FuriHalVersionOTPv2 (targets/f7/furi_hal/furi_hal_version.c) */
    stw_le_p(otp + 0, 0xBABE);          /* header_magic */
    otp[2] = 2;                         /* header_version */
    otp[3] = 0;
    stl_le_p(otp + 4, 1704067200);      /* header_timestamp: 2024-01-01 */
    otp[8] = 12;                        /* board_version */
    otp[9] = 7;                         /* board_target: f7 */
    otp[10] = 9;                        /* board_body */
    otp[11] = 6;                        /* board_connect */
    otp[12] = 1;                        /* board_display: ERC */
    otp[13] = 0;
    stw_le_p(otp + 14, 0);
    otp[16] = 1;                        /* board_color: black */
    otp[17] = s->region;                /* board_region */
    stw_le_p(otp + 18, 0);
    stl_le_p(otp + 20, 0);
    pstrcpy(name, sizeof(name), s->name ? s->name : "Qemu");
    memcpy(otp + 24, name, 8);

    /* UID64: UDN, device id, ST company id (LL_FLASH_GetUDN & co.) */
    stl_le_p(info + (WB55_UID64_BASE - WB55_INFO_BASE), (uint32_t)uid64);
    stl_le_p(info + (WB55_UID64_BASE - WB55_INFO_BASE) + 4,
             0x0080E100u | 0x26u);
    for (int i = 0; i < 12; i++) {
        info[WB55_UID96_BASE - WB55_INFO_BASE + i] = uid64 >> ((i % 8) * 8);
    }
    stw_le_p(info + (WB55_FLASHSIZE_BASE - WB55_INFO_BASE), 1024);
}

static void flipper_nvram_load(FlipperZeroState *s)
{
    g_autofree FlipperNvram *nv = NULL;
    gsize len = 0;

    if (!s->nvram || !g_file_get_contents(s->nvram, (gchar **)&nv, &len, NULL)) {
        return;
    }
    if (len != sizeof(*nv) || nv->magic != NVRAM_MAGIC ||
        nv->version != NVRAM_VERSION) {
        warn_report("flipper-zero: ignoring incompatible nvram %s", s->nvram);
        return;
    }
    memcpy(s->gauge->dm, nv->gauge_dm, sizeof(s->gauge->dm));
    s->gauge->sec = nv->gauge_sec;
    s->gauge->initcomp = nv->gauge_initcomp;
    s->gauge->cfgupdate = nv->gauge_cfgupdate;
    wb55_rtc_import(&s->soc.rtc, &nv->rtc);
}

static void flipper_nvram_save(Notifier *n, void *data)
{
    FlipperZeroState *s = container_of(n, FlipperZeroState, exit_notifier);
    g_autofree FlipperNvram *nv = g_new0(FlipperNvram, 1);
    g_autoptr(GError) err = NULL;

    nv->magic = NVRAM_MAGIC;
    nv->version = NVRAM_VERSION;
    memcpy(nv->gauge_dm, s->gauge->dm, sizeof(nv->gauge_dm));
    nv->gauge_sec = s->gauge->sec;
    nv->gauge_initcomp = s->gauge->initcomp;
    nv->gauge_cfgupdate = s->gauge->cfgupdate;
    wb55_rtc_export(&s->soc.rtc, &nv->rtc);
    if (!g_file_set_contents(s->nvram, (const gchar *)nv, sizeof(*nv), &err)) {
        warn_report("flipper-zero: cannot save nvram: %s", err->message);
    }
}

/* The slot's switch: low with a card in it.  The firmware looks at it
 * when it has nothing else to do with the card, and mounts or unmounts it
 * when it changes. */
static void flipper_sd_detect(FlipperZeroState *s)
{
    qemu_set_irq(soc_in(s, 'C', 10), s->sd_drive && s->sd_in ? 0 : 1);
}

/* The slot's chip select reaches the card that is in it */
static void flipper_sd_select(FlipperZeroState *s)
{
    bool in = s->sd_drive && s->sd_in;

    s->sd_slot->empty = !in;
    qemu_set_irq(qdev_get_gpio_in_named(DEVICE(s->sd_slot), SSI_GPIO_CS, 0),
                 s->sd_cs);
    qemu_set_irq(qdev_get_gpio_in_named(s->sd_spi, SSI_GPIO_CS, 0),
                 in ? s->sd_cs : 1);
}

static void flipper_sd_cs(void *opaque, int n, int level)
{
    FlipperZeroState *s = opaque;

    s->sd_cs = level;
    flipper_sd_select(s);
}

/* ------------------------------------------------------- the GPIO header */

/*
 * The header's signal pins by their number on it (1 is +5V, 9 is 3V3; 8, 11
 * and 18 are GND).  The outside may hold those that nothing of the emulator
 * is on: not the log's UART (13, 14), and not the iButton pin (17), which
 * has its own keys and readers (flipper_ibutton.c).
 */
static const struct {
    uint8_t number;
    char port;
    uint8_t pin;
    bool holds;
} header[HDR_PINS] = {
    { 2, 'A', 7, true },   { 3, 'A', 6, true },   { 4, 'A', 4, true },
    { 5, 'B', 3, true },   { 6, 'B', 2, true },   { 7, 'C', 3, true },
    { 10, 'A', 14, true }, { 12, 'A', 13, true }, { 13, 'B', 6, false },
    { 14, 'B', 7, false }, { 15, 'C', 1, true },  { 16, 'C', 0, true },
    { 17, 'B', 14, false },
};

enum { HDR_FREE, HDR_LOW, HDR_HIGH };

static Stm32l4x5GpioState *header_port(FlipperZeroState *s, int i)
{
    return &s->soc.gpio[header[i].port - 'A'];
}

/* Does the firmware drive the pin both ways (a push-pull output)?  Then
 * what the outside holds does not show; on hardware the two would fight. */
static bool header_driven(FlipperZeroState *s, int i)
{
    Stm32l4x5GpioState *g = header_port(s, i);

    return extract32(g->moder, 2 * header[i].pin, 2) == 1 &&
           !extract32(g->otyper, header[i].pin, 1);
}

static int header_index(int number)
{
    for (int i = 0; i < HDR_PINS; i++) {
        if (header[i].number == number) {
            return i;
        }
    }
    g_assert_not_reached();
}

enum { HDR_RISE = 1, HDR_FALL = 2 };

/*
 * The edges of a header pin that the firmware has an interrupt for: an
 * input, whose EXTI line is the pin's port's, with the interrupt on.  A
 * line is its number's (line 2 for PA2, PB2 and PC2) and keeps an edge that
 * was asked for another pin of it before: this is what the line has.
 */
static int header_edges(FlipperZeroState *s, int i)
{
    int pin = header[i].pin;

    if (extract32(header_port(s, i)->moder, 2 * pin, 2) != 0 ||
        extract32(s->soc.syscfg.exticr[pin / 4], 4 * (pin % 4), 3) !=
            header[i].port - 'A' ||
        !extract32(s->soc.exti.imr[0], pin, 1)) {
        return 0;
    }
    return (extract32(s->soc.exti.rtsr[0], pin, 1) ? HDR_RISE : 0) |
           (extract32(s->soc.exti.ftsr[0], pin, 1) ? HDR_FALL : 0);
}

/* Does the firmware wait for a header pin to fall? */
static bool header_waits_fall(FlipperZeroState *s, int i)
{
    return header_edges(s, i) & HDR_FALL;
}

/*
 * What the outside holds, put on the pins of a port (-1: of all).  QEMU's
 * GPIO model lets go of a pin when the firmware makes it a push-pull
 * output, and a port's reset lets go of all: so this is done again after a
 * write to MODER, and after a reset.
 */
static void flipper_header_held(FlipperZeroState *s, int port)
{
    for (int i = 0; i < HDR_PINS; i++) {
        if (s->hdr_hold[i] != HDR_FREE && !header_driven(s, i) &&
            (port < 0 || port == header[i].port - 'A')) {
            qemu_set_irq(soc_in(s, header[i].port, header[i].pin),
                         s->hdr_hold[i] == HDR_HIGH);
        }
    }
}

/* a pin's level: '0', '1', 'z' (nothing holds it: it floats) or '?' (a
 * peripheral's pin, whose signal is not kept as a level) */
static char header_level(FlipperZeroState *s, int i)
{
    Stm32l4x5GpioState *g = header_port(s, i);
    int pin = header[i].pin, mode = extract32(g->moder, 2 * pin, 2);
    int pull = extract32(g->pupdr, 2 * pin, 2);
    bool odr = extract32(g->odr, pin, 1);

    if (mode == 1 && (!extract32(g->otyper, pin, 1) || !odr)) {
        return '0' + odr;               /* the firmware's own drive */
    }
    if (!extract32(g->disconnected_pins, pin, 1)) {
        return '0' + extract32(g->pins_connected_high, pin, 1);
    }
    if (mode == 2) {
        return '?';
    }
    return pull == 1 ? '1' : pull == 2 ? '0' : 'z';
}

/*
 * The record: every change of a header pin's level with its time, as a VCD
 * file, which GTKWave, PulseView and sigrok read.  Times are the virtual
 * clock's, in ns from where the record began.  A level is 0, 1, z (nothing
 * drives the pin) or x (a peripheral's pin).  Looked at where a level can
 * change: after the firmware's write to a GPIO register, after a reset,
 * when the outside holds a pin or lets go of it, and when an iButton key or
 * reader of the emulator's pulls pin 17 or lets go of it.
 */
#define HDR_REC_FLUSH_MS 50

static void flipper_header_look(FlipperZeroState *s, int port)
{
    int64_t now;
    bool stamped = false;

    if (!s->hdr_rec) {
        return;
    }
    now = qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL) - s->hdr_rec_t0;
    if (now <= s->hdr_rec_at) {         /* two looks in one tick of the */
        now = s->hdr_rec_at + 1;        /* clock: both are in the record */
    }
    for (int i = 0; i < HDR_PINS; i++) {
        char level;

        if (port >= 0 && port != header[i].port - 'A') {
            continue;
        }
        level = header_level(s, i);
        if (level == s->hdr_rec_last[i]) {
            continue;
        }
        if (!stamped) {
            fprintf(s->hdr_rec, "#%" PRId64 "\n", now);
            s->hdr_rec_at = now;
            stamped = true;
        }
        fprintf(s->hdr_rec, "%c%c\n", level == '?' ? 'x' : level, 'a' + i);
        s->hdr_rec_last[i] = level;
        s->hdr_rec_changes++;
    }
}

/* (for both records: the pins' and the I2C bus's) */
static void flipper_header_rec_flush(void *opaque)
{
    FlipperZeroState *s = opaque;

    if (s->hdr_rec) {
        fflush(s->hdr_rec);
    }
    if (s->i2c_rec) {
        fflush(s->i2c_rec);
    }
    if (s->hdr_rec || s->i2c_rec) {
        timer_mod(s->hdr_rec_flush,
                  qemu_clock_get_ms(QEMU_CLOCK_REALTIME) + HDR_REC_FLUSH_MS);
    }
}

/* a record began: what it gets reaches its file within 50 ms */
static void flipper_header_rec_flushes(FlipperZeroState *s)
{
    if (!s->hdr_rec_flush) {
        s->hdr_rec_flush = timer_new_ms(QEMU_CLOCK_REALTIME,
                                        flipper_header_rec_flush, s);
    }
    timer_mod(s->hdr_rec_flush,
              qemu_clock_get_ms(QEMU_CLOCK_REALTIME) + HDR_REC_FLUSH_MS);
}

/* a file for a record, by a property's value: NULL with the reason */
static FILE *flipper_rec_open(const char *prop, const char *value,
                              const char *running, Error **errp)
{
    FILE *f;

    if (running) {
        error_setg(errp, "%s: it records to %s already (set it to off to "
                   "end that)", prop, running);
        return NULL;
    }
    if (value[0] != '/') {
        error_setg(errp, "%s: a file with its whole path, or off", prop);
        return NULL;
    }
    if (!phase_check(PHASE_MACHINE_INITIALIZED)) {
        error_setg(errp, "%s: set it on a machine that runs", prop);
        return NULL;
    }
    f = fopen(value, "w");
    if (!f) {
        error_setg_errno(errp, errno, "%s: cannot write %s", prop, value);
    }
    return f;
}

static void flipper_header_rec_stop(FlipperZeroState *s)
{
    int64_t now;

    if (!s->hdr_rec) {
        return;
    }
    /* its end, so that the last level has a length */
    now = qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL) - s->hdr_rec_t0;
    if (now > s->hdr_rec_at) {
        fprintf(s->hdr_rec, "#%" PRId64 "\n", now);
    }
    fclose(s->hdr_rec);
    s->hdr_rec = NULL;
}

/* "CHANGES NS PATH" while it records (changes written, ns since it began,
 * the file), and nothing while it does not */
static char *flipper_get_header_record(Object *obj, Error **errp)
{
    FlipperZeroState *s = FLIPPER_ZERO(obj);

    if (!s->hdr_rec) {
        return g_strdup("");
    }
    return g_strdup_printf("%" PRIu64 " %" PRId64 " %s", s->hdr_rec_changes,
                           qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL) -
                           s->hdr_rec_t0, s->hdr_rec_path);
}

/* a file to record to, with its whole path; off: the record ends */
static void flipper_set_header_record(Object *obj, const char *value,
                                      Error **errp)
{
    FlipperZeroState *s = FLIPPER_ZERO(obj);
    FILE *f;

    if (!strcmp(value, "off")) {
        flipper_header_rec_stop(s);
        return;
    }
    f = flipper_rec_open("header-record", value,
                         s->hdr_rec ? s->hdr_rec_path : NULL, errp);
    if (!f) {
        return;
    }
    g_free(s->hdr_rec_path);
    s->hdr_rec_path = g_strdup(value);
    s->hdr_rec = f;
    s->hdr_rec_t0 = qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL);
    s->hdr_rec_at = 0;
    s->hdr_rec_changes = 0;
    fprintf(f, "$version FlipperEmu, the GPIO header of a Flipper Zero $end\n"
               "$comment x: a peripheral has the pin; z: nothing drives it "
               "$end\n$timescale 1 ns $end\n$scope module header $end\n");
    for (int i = 0; i < HDR_PINS; i++) {
        fprintf(f, "$var wire 1 %c P%c%d $end\n", 'a' + i, header[i].port,
                header[i].pin);
    }
    fprintf(f, "$upscope $end\n$enddefinitions $end\n#0\n$dumpvars\n");
    for (int i = 0; i < HDR_PINS; i++) {
        s->hdr_rec_last[i] = header_level(s, i);
        fprintf(f, "%c%c\n", s->hdr_rec_last[i] == '?' ? 'x' :
                s->hdr_rec_last[i], 'a' + i);
    }
    fprintf(f, "$end\n");
    fflush(f);
    flipper_header_rec_flushes(s);
}

/* "NUMBER:PIN:MODE:PULL:LEVEL:HELD" for each pin: MODE in, out, od (an
 * open-drain output), afN, afNod or analog, and after in, out or od the edges
 * that the firmware has an interrupt for (+r, +f, +rf: an input alone has
 * any); PULL up, down or none; LEVEL as above; HELD high, low, free, or - for
 * a pin that is not for holding */
static char *flipper_get_header(Object *obj, Error **errp)
{
    static const char *const pulls[] = { "none", "up", "down", "none" };
    static const char *const holds[] = { "free", "low", "high" };
    static const char *const edges[] = { "", "+r", "+f", "+rf" };
    FlipperZeroState *s = FLIPPER_ZERO(obj);
    GString *out = g_string_new(NULL);

    for (int i = 0; i < HDR_PINS; i++) {
        Stm32l4x5GpioState *g = header_port(s, i);
        int pin = header[i].pin, mode = extract32(g->moder, 2 * pin, 2);
        bool od = extract32(g->otyper, pin, 1);

        g_string_append_printf(out, "%s%d:P%c%d:", i ? " " : "",
                               header[i].number, header[i].port, pin);
        if (mode == 2) {
            g_string_append_printf(out, "af%d%s",
                                   extract32(pin < 8 ? g->afrl : g->afrh,
                                             4 * (pin & 7), 4),
                                   od ? "od" : "");
        } else {
            g_string_append(out, mode == 0 ? "in" : mode == 3 ? "analog" :
                                 od ? "od" : "out");
            g_string_append(out, edges[header_edges(s, i)]);
        }
        g_string_append_printf(out, ":%s:%c:%s",
                               pulls[extract32(g->pupdr, 2 * pin, 2)],
                               header_level(s, i),
                               header[i].holds ? holds[s->hdr_hold[i]] : "-");
    }
    return g_string_free(out, false);
}

/*
 * A serial port of the header as the firmware has it,
 * "NAME:TX:RX:BAUD:FRAME:SENDS:HEARS": TX and RX its pins by their numbers on
 * the header; BAUD what its divider gives, 0 while the port is off; FRAME as
 * in 8N1; SENDS on or off; HEARS off, irq or dma (who is told of a byte), or
 * unread: the receiver is on and nobody is told, so a byte waits; or edge:
 * the receiver is off, and an interrupt waits for the RX pin to fall, which
 * is how the firmware waits for an expansion module's call.
 */
static void serial_port(FlipperZeroState *s, GString *out, const char *name,
                        hwaddr base, Stm32l4x5UsartBaseState *u, bool lp,
                        int tx, int rx)
{
    static const uint16_t prescaler[16] = {
        1, 2, 4, 6, 8, 10, 12, 16, 32, 64, 128, 256, 256, 256, 256, 256
    };
    static const char *const stop[] = { "1", "0.5", "2", "1.5" };
    bool on = extract32(u->cr1, 0, 1), parity = extract32(u->cr1, 10, 1);
    int bits = extract32(u->cr1, 28, 1) ? 7 : extract32(u->cr1, 12, 1) ? 9 : 8;
    uint32_t presc = 0, div, baud;
    uint64_t hz;

    /* (PRESC is not in every QEMU tree's model: read as the firmware does) */
    address_space_read(&address_space_memory, base + 0x2C,
                       MEMTXATTRS_UNSPECIFIED, &presc, sizeof(presc));
    hz = clock_get_hz(u->clk) / prescaler[le32_to_cpu(presc) & 0xF];
    if (lp) {
        div = u->brr & 0xFFFFF;         /* BRR = 256 * clock / baud rate */
        baud = div < 0x300 ? 0 : hz * 256 / div;
    } else {
        div = u->brr & 0xFFFF;
        if (div < 16) {
            baud = 0;
        } else {
            if (extract32(u->cr1, 15, 1)) {     /* oversampling by 8 */
                div = ((div & 0xFFF0) | ((div & 7) << 1)) / 2;
            }
            baud = hz / div;
        }
    }
    g_string_append_printf(out, "%s%s:%d:%d:%u:%d%c%s:%s:%s",
                           out->len ? " " : "", name, tx, rx, on ? baud : 0,
                           bits - parity, !parity ? 'N' :
                           extract32(u->cr1, 9, 1) ? 'O' : 'E',
                           stop[extract32(u->cr2, 12, 2)],
                           on && extract32(u->cr1, 3, 1) ? "on" : "off",
                           !on || !extract32(u->cr1, 2, 1) ?
                           (header_waits_fall(s, header_index(rx)) ?
                            "edge" : "off") :
                           extract32(u->cr3, 6, 1) ? "dma" :
                           extract32(u->cr1, 5, 1) ? "irq" : "unread");
}

static char *flipper_get_header_serial(Object *obj, Error **errp)
{
    FlipperZeroState *s = FLIPPER_ZERO(obj);
    GString *out = g_string_new(NULL);

    serial_port(s, out, "USART1", 0x40013800, &s->soc.usart1, false, 13, 14);
    serial_port(s, out, "LPUART1", 0x40008000, &s->soc.lpuart1, true, 15, 16);
    return g_string_free(out, false);
}

/*
 * The header's I2C bus, "I2C3:SDA:SCL:CALLS:UNANSWERED:LAST" and then
 * "TYPE@ADDRESS" for each device on it: SDA and SCL its pins by their
 * numbers on the header; CALLS how many times the firmware called an address
 * since the emulator started, UNANSWERED how many of those nobody took; LAST
 * the last address called (two hex digits), r or w for what it was called
 * for, + or - for taken or not; "-" before the first call.  The firmware
 * has the bus on only while it uses it, so there is no on and off to tell.
 */
static char *flipper_get_header_i2c(Object *obj, Error **errp)
{
    FlipperZeroState *s = FLIPPER_ZERO(obj);
    WB55I2cState *i = &s->soc.i2c[1];
    GString *out = g_string_new(NULL);
    BusChild *kid;

    g_string_printf(out, "I2C3:15:16:%" PRIu64 ":%" PRIu64 ":", i->calls,
                    i->unanswered);
    if (i->calls) {
        g_string_append_printf(out, "%02x%c%c", i->last,
                               i->last_read ? 'r' : 'w',
                               i->last_taken ? '+' : '-');
    } else {
        g_string_append_c(out, '-');
    }
    QTAILQ_FOREACH(kid, &BUS(i->bus)->children, sibling) {
        g_string_append_printf(out, " %s@%02x",
                               object_get_typename(OBJECT(kid->child)),
                               I2C_SLAVE(kid->child)->address);
    }
    return g_string_free(out, false);
}

/*
 * The record of the header's I2C bus: every part of every transfer (what
 * lies between a START and the next START or STOP), a line of text each:
 *
 *   SECONDS ADDRESS w|r [BYTE...] [no answer] [+|...]
 *
 * SECONDS: the virtual clock at the START, from where the record began.
 * ADDRESS: the seven bits that were called, as 0x48; w or r: to write or to
 * read.  Then the bytes written or read, two hex digits each, with "!"
 * after one that the device refused (the transfer ends there).  "no
 * answer": nobody took the address.  "+": no STOP, the next line's START
 * follows at once.  "...": the record ended before the part did.  Lines
 * with "#" first say what the file is and what was on the bus as it began.
 * A transfer takes no time here, so a line is whole when it is written; a
 * part that the firmware leaves open (no STOP yet) is in the file when it
 * ends.
 */
static void flipper_i2c_tap(void *opaque, WB55I2cTap what, uint8_t value,
                            bool ok)
{
    FlipperZeroState *s = opaque;
    FILE *f = s->i2c_rec;
    int64_t ns;

    if (!f) {
        return;
    }
    switch (what) {
    case WB55_I2C_TAP_CALL:
        ns = qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL) - s->i2c_rec_t0;
        fprintf(f, "%" PRId64 ".%09" PRId64 " 0x%02x %c%s",
                (int64_t)(ns / NANOSECONDS_PER_SECOND),
                (int64_t)(ns % NANOSECONDS_PER_SECOND),
                value >> 1, value & 1 ? 'r' : 'w', ok ? "" : " no answer");
        s->i2c_rec_part = true;
        s->i2c_rec_reads = value & 1;
        s->i2c_rec_calls++;
        s->i2c_rec_unanswered += !ok;
        break;
    case WB55_I2C_TAP_BYTE:
        if (s->i2c_rec_part) {
            fprintf(f, " %02x%s", value, ok ? "" : "!");
            if (s->i2c_rec_reads) {
                s->i2c_rec_read++;
            } else {
                s->i2c_rec_written += ok;
            }
        }
        break;
    case WB55_I2C_TAP_END:
        if (s->i2c_rec_part) {
            fputs(ok ? "\n" : " +\n", f);
            s->i2c_rec_part = false;
        }
        break;
    }
}

static void flipper_i2c_rec_stop(FlipperZeroState *s)
{
    if (!s->i2c_rec) {
        return;
    }
    if (s->i2c_rec_part) {
        fputs(" ...\n", s->i2c_rec);
        s->i2c_rec_part = false;
    }
    fclose(s->i2c_rec);
    s->i2c_rec = NULL;
}

/* "CALLS UNANSWERED WRITTEN READ NS PATH" while it records (addresses
 * called and how many of them nobody took, bytes written and read, ns since
 * it began, the file), and nothing while it does not */
static char *flipper_get_header_i2c_record(Object *obj, Error **errp)
{
    FlipperZeroState *s = FLIPPER_ZERO(obj);

    if (!s->i2c_rec) {
        return g_strdup("");
    }
    return g_strdup_printf("%" PRIu64 " %" PRIu64 " %" PRIu64 " %" PRIu64
                           " %" PRId64 " %s", s->i2c_rec_calls,
                           s->i2c_rec_unanswered, s->i2c_rec_written,
                           s->i2c_rec_read,
                           qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL) -
                           s->i2c_rec_t0, s->i2c_rec_path);
}

/* a file to record to, with its whole path; off: the record ends */
static void flipper_set_header_i2c_record(Object *obj, const char *value,
                                          Error **errp)
{
    FlipperZeroState *s = FLIPPER_ZERO(obj);
    WB55I2cState *i = &s->soc.i2c[1];
    const char *sep = " ";
    BusChild *kid;
    unsigned address;
    FILE *f;

    if (!strcmp(value, "off")) {
        flipper_i2c_rec_stop(s);
        return;
    }
    f = flipper_rec_open("header-i2c-record", value,
                         s->i2c_rec ? s->i2c_rec_path : NULL, errp);
    if (!f) {
        return;
    }
    g_free(s->i2c_rec_path);
    s->i2c_rec_path = g_strdup(value);
    s->i2c_rec_t0 = qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL);
    s->i2c_rec_part = false;
    s->i2c_rec_calls = s->i2c_rec_unanswered = 0;
    s->i2c_rec_written = s->i2c_rec_read = 0;
    fprintf(f, "# FlipperEmu: the I2C bus of a Flipper Zero's GPIO header "
               "(I2C3: SDA pin 15, SCL pin 16)\n# on the bus:");
    for (address = 0; address < 0x80; address++) {  /* in their order */
        QTAILQ_FOREACH(kid, &BUS(i->bus)->children, sibling) {
            if (I2C_SLAVE(kid->child)->address == address) {
                fprintf(f, "%s%s at 0x%02x", sep,
                        object_get_typename(OBJECT(kid->child)), address);
                sep = ", ";
            }
        }
    }
    fprintf(f, "%s\n"
               "# a line: the START's time in s, the address called, w or r, "
               "the bytes written or read\n"
               "#   no answer: nobody took the address    "
               "!: the device refused that byte\n"
               "#   +: no STOP, the next line's START follows    "
               "...: the record ended there\n",
            QTAILQ_EMPTY(&BUS(i->bus)->children) ? " nothing" : "");
    fflush(f);
    s->i2c_rec = f;
    flipper_header_rec_flushes(s);
}

/* "PA7=1", "PA7=0" or "PA7=z" (let go of it); several, with blanks or
 * commas between them */
static void flipper_set_header(Object *obj, const char *value, Error **errp)
{
    FlipperZeroState *s = FLIPPER_ZERO(obj);
    g_auto(GStrv) words = g_strsplit_set(value, " ,", -1);
    uint8_t hold[HDR_PINS];

    memcpy(hold, s->hdr_hold, sizeof(hold));
    for (char **w = words; *w; w++) {
        char port, level;
        int pin, used = 0, i;

        if (!**w) {
            continue;
        }
        if (sscanf(*w, "P%c%d=%c%n", &port, &pin, &level, &used) != 3 ||
            (*w)[used] || !strchr("01z", level)) {
            error_setg(errp, "header: expected PIN=0, PIN=1 or PIN=z (as in "
                       "PA7=1), not '%s'", *w);
            return;
        }
        for (i = 0; i < HDR_PINS; i++) {
            if (header[i].port == port && header[i].pin == pin) {
                break;
            }
        }
        if (i == HDR_PINS) {
            error_setg(errp, "header: no pin P%c%d on the GPIO header (it "
                       "has PA7 PA6 PA4 PB3 PB2 PC3 PA14 PA13 PC1 PC0 to "
                       "hold)", port, pin);
            return;
        }
        if (!header[i].holds) {
            error_setg(errp, "header: P%c%d is not a pin to hold: %s", port,
                       pin, header[i].pin == 14 ?
                       "the iButton pin has its own keys and readers" :
                       "the firmware's log is on that UART");
            return;
        }
        hold[i] = level == 'z' ? HDR_FREE : level == '1' ? HDR_HIGH : HDR_LOW;
    }
    for (int i = 0; i < HDR_PINS; i++) {
        bool was = s->hdr_hold[i] != HDR_FREE;

        s->hdr_hold[i] = hold[i];
        if (was && hold[i] == HDR_FREE &&
            phase_check(PHASE_MACHINE_INITIALIZED)) {
            object_property_set_uint(OBJECT(header_port(s, i)),
                                     "disconnected-pins",
                                     1u << header[i].pin, &error_abort);
        }
    }
    if (phase_check(PHASE_MACHINE_INITIALIZED)) {
        flipper_header_held(s, -1);
        flipper_header_look(s, -1);
    }
}

/*
 * A character came to a serial port of the header while its receiver was
 * off (the UART's "rx-unheard", qemu/patches/0008).  The UART drops it, but
 * on a wire its start bit is on the RX pin all the same, and that is where
 * the firmware waits for an expansion module: with the receiver off, and an
 * interrupt on the pin's falling edge.  So the pin goes low from outside
 * for a bit time at 9600 baud (the speed such a module begins at), and is
 * then let go, or given back to what the outside holds on it.
 */
#define HDR_RX_PULSE_NS (NANOSECONDS_PER_SECOND / 9600)

static const uint8_t hdr_rx_pin[2] = { 14, 16 };    /* USART1, LPUART1 */

static void flipper_rx_pulse_end(FlipperZeroState *s, int n)
{
    int i = header_index(hdr_rx_pin[n]);

    if (!s->hdr_rx_low[n]) {
        return;
    }
    s->hdr_rx_low[n] = false;
    if (s->hdr_hold[i] == HDR_FREE) {
        object_property_set_uint(OBJECT(header_port(s, i)),
                                 "disconnected-pins", 1u << header[i].pin,
                                 &error_abort);
    } else {
        flipper_header_held(s, header[i].port - 'A');
    }
    flipper_header_look(s, header[i].port - 'A');
}

static void flipper_usart_pulse_end(void *opaque)
{
    flipper_rx_pulse_end(opaque, 0);
}

static void flipper_lpuart_pulse_end(void *opaque)
{
    flipper_rx_pulse_end(opaque, 1);
}

static void flipper_rx_unheard(void *opaque, int n, int level)
{
    FlipperZeroState *s = opaque;
    int i = header_index(hdr_rx_pin[n]);

    if (!level || header_driven(s, i)) {    /* (an output: it would fight) */
        return;
    }
    s->hdr_rx_low[n] = true;
    qemu_set_irq(soc_in(s, header[i].port, header[i].pin), 0);
    flipper_header_look(s, header[i].port - 'A');
    timer_mod(s->hdr_rx_end[n],
              qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL) + HDR_RX_PULSE_NS);
}

/* External levels the board holds on MCU pins. A GPIO port reset releases
 * external drivers, so these are re-applied after every system reset. */
static void flipper_drive_pins(FlipperZeroState *s)
{
    for (int n = 0; n < 2; n++) {       /* (a start bit: the reset ended it) */
        timer_del(s->hdr_rx_end[n]);
        s->hdr_rx_low[n] = false;
    }
    flipper_header_held(s, -1);         /* the header: as the outside has it */
    flipper_sd_detect(s);
    qemu_set_irq(soc_in(s, 'C', 2), 0);     /* SPI2 MISO idle low */
    qemu_set_irq(soc_in(s, 'B', 4), 0);     /* SPI1 MISO: CC1101 CHIP_RDYn */
    flipper_ir_idle(s->ir);                 /* IR receiver output idles high */
    flipper_ibutton_reset(s->ibutton);      /* iButton line pulled up */
    for (int b = 0; b < BTN_NUM; b++) {     /* buttons: as they are held */
        bool down = s->keys_down & (1u << b);

        qemu_set_irq(soc_in(s, buttons[b].port, buttons[b].pin),
                     buttons[b].active_high ? down : !down);
    }
    flipper_header_look(s, -1);             /* the header's record */
}

/* a read of GPIOB's IDR: the firmware sampled PB14, the iButton line */
/* The iButton pin as a key or a reader of the emulator's has it: low, or
 * let go.  It is a pin of the header (17), so its record has that too. */
static void flipper_ibutton_pull(void *opaque, int n, int level)
{
    FlipperZeroState *s = opaque;

    qemu_set_irq(soc_in(s, 'B', 14), level);
    flipper_header_look(s, 1);
}

static void flipper_idr_read(void *opaque, int port)
{
    FlipperZeroState *s = opaque;

    if (port == 1) {
        flipper_ibutton_idr_read(s->ibutton,
                                 s->soc.gpio[1].odr & (1u << 14));
    }
}

static bool flipper_pb14_output(FlipperZeroState *s)
{
    return ((s->soc.gpio[1].moder >> 28) & 3) == 1;
}

/* a write to a GPIO register that a pin's level may change with.  For the
 * header: what is held shows again, and the record has the change.  For
 * GPIOB's MODER/ODR/BSRR/BRR that may concern PB14: what the firmware
 * drives on the iButton line now (low, or released) */
static void flipper_out_written(void *opaque, int port, hwaddr reg,
                                uint32_t data)
{
    FlipperZeroState *s = opaque;
    const uint32_t bit = 1u << 14, *tim = s->soc.tim1.regs;

    if (reg == 0x00) {                  /* MODER: a header pin may be an */
        flipper_header_held(s, port);   /* input again, for what holds it */
    }
    flipper_header_look(s, port);
    if (reg == 0x04 || reg == 0x0c) {
        return;                         /* OTYPER, PUPDR: the header's only */
    }
    if (port != 1 || (reg == 0x18 && !(data & (bit | bit << 16))) ||
        (reg == 0x28 && !(data & bit))) {
        return;                         /* another pin's BSRR/BRR write */
    }
    flipper_ibutton_fw_write(s->ibutton, flipper_pb14_output(s) &&
                                         !(s->soc.gpio[1].odr & bit));
    if (reg == 0x00 && !flipper_pb14_output(s)) {
        flipper_ibutton_released(s->ibutton);
    }
    /*
     * Emulating a Cyfral or Metakom key, the firmware's TIM1 update ISR sets
     * each next period (ARR) and then the level it holds for (BSRR/BRR);
     * that pair is the key's waveform.  Taken here, not at the updates, it
     * keeps to the firmware's timeline: a vCPU late to the ISR (a host
     * thread waking from WFI) would stretch or repeat a level there.
     */
    if (reg != 0x00 && flipper_pb14_output(s) && (tim[0x0C / 4] & 1)) {
        uint64_t ticks = (uint64_t)((tim[0x28 / 4] & 0xFFFF) + 1) *
                         ((tim[0x2C / 4] & 0xFFFF) + 1);

        flipper_ibutton_emulated(s->ibutton, s->soc.gpio[1].odr & bit,
                                 muldiv64(ticks, NANOSECONDS_PER_SECOND,
                                          clock_get_hz(s->soc.rcc.hclk)));
    }
}

/* COMP1 on with the 125 kHz field off: the iButton reader (Cyfral, Metakom) */
static void flipper_comp_en(void *opaque, int n, bool en)
{
    FlipperZeroState *s = opaque;

    if (n == 0) {
        flipper_ibutton_comp(s->ibutton, en);
    }
}

static void flipper_comp_val_read(void *opaque, int n)
{
    FlipperZeroState *s = opaque;

    if (n == 0) {
        flipper_ibutton_comp_read(s->ibutton);
    }
}

/*
 * A firmware that has halted: a debug build after a crash (it waits for a
 * debugger there), any build after furi_halt().  Both end in the same two
 * instructions with the interrupts off, `wfi` and a branch back to it, and
 * that `wfi` returns at once, on a real Flipper as well: an interrupt is
 * pending, and masked.  A real one only draws its current then; here a core
 * of the host would run at full speed for nothing, so the CPU is left to run
 * for a hundredth of the time.  A reset ends that, a debugger or a pause that
 * stops it, and a PC that has left the loop.
 */
#define HALT_WATCH_NS   (250 * SCALE_MS)
#define HALT_REST_PCT   99

static uint32_t flipper_halted_at(FlipperZeroState *s)
{
    ARMCPU *cpu = s->soc.armv7m.cpu;
    uint32_t pc = cpu->env.regs[15] & ~1u;
    uint16_t code[3];

    if (pc < 2 ||
        cpu_memory_rw_debug(CPU(cpu), pc - 2, code, sizeof(code), false)) {
        return 0;
    }
    for (int i = 0; i < 3; i++) {
        code[i] = le16_to_cpu(code[i]);
    }
    if (code[1] == 0xbf30 && code[2] == 0xe7fd) {       /* wfi; b .-2 */
        return pc;
    }
    return code[0] == 0xbf30 && code[1] == 0xe7fd ? pc - 2 : 0;
}

static void flipper_halt_rest(FlipperZeroState *s, bool rest)
{
    if (rest == s->halt_rest) {
        return;
    }
    s->halt_rest = rest;
    if (rest) {
        cpu_throttle_set(HALT_REST_PCT);
    } else {
        cpu_throttle_stop();
    }
}

static void flipper_halt_watch(void *opaque)
{
    FlipperZeroState *s = opaque;
    uint32_t at = flipper_halted_at(s);

    if (!at) {
        s->halt_said = false;
        flipper_halt_rest(s, false);
    } else if (at == s->halt_at) {      /* there twice: not passing through */
        if (!s->halt_said) {
            s->halt_said = true;
            info_report("flipper-zero: the firmware has halted at 0x%08x (a "
                        "debug build after a crash, or furi_halt()); its CPU "
                        "rests until a reset or a debugger", at);
            flipper_panel_halted(s->panel, at);
        }
        flipper_halt_rest(s, true);
    }
    s->halt_at = at;
    timer_mod(s->halt_watch,
              qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL) + HALT_WATCH_NS);
}

/* Stopped (a debugger, a pause): what goes on from there is not slowed,
 * a single step least of all.  Still in the loop later: it rests again. */
static void flipper_halt_vm_state(void *opaque, bool running, RunState state)
{
    FlipperZeroState *s = opaque;

    if (!running) {
        flipper_halt_rest(s, false);
        s->halt_at = 0;
    }
}

static void flipper_zero_reset(MachineState *machine, ResetType type)
{
    FlipperZeroState *s = FLIPPER_ZERO(machine);

    flipper_halt_rest(s, false);
    s->halt_at = 0;
    s->halt_said = false;
    wb55_soc_latch_boot(&FLIPPER_ZERO(machine)->soc);
    qemu_devices_reset(type);
    flipper_drive_pins(FLIPPER_ZERO(machine));
    wb55_soc_sync_pins(&FLIPPER_ZERO(machine)->soc);
}

/*
 * TIM2 outputs: CH2 drives PA1, the CC1101's GDO0 data input (Sub-GHz
 * transmit); CH3 drives PA2, RFID_PULL, the 125 kHz antenna load (emulation).
 */
static void flipper_tim2_oc(void *opaque, int ch, bool level, int64_t when)
{
    FlipperZeroState *s = opaque;

    if (ch == 1) {
        cc1101_tx_data(s->cc1101, level, when);
    } else if (ch == 2) {
        flipper_rfid_load(s->rfid, level, when);
    }
}

/* The virtual USB cable also carries VBUS to the charger. */
static void flipper_usb_vbus(void *opaque, bool on)
{
    FlipperZeroState *s = opaque;

    object_property_set_bool(OBJECT(s->charger), "usb", on, &error_abort);
}

/*
 * The charger cut the battery off with nothing on VBUS: the Flipper is off
 * (Settings > Power > Power OFF, or a flat battery).  A real one stays off
 * until Back is held; an emulator that is off has nothing to run, so it
 * exits, and what started it starts it again to switch it on.
 */
static void flipper_power_off(void *opaque)
{
    FlipperZeroState *s = opaque;

    info_report("flipper-zero: powered off");
    flipper_panel_power_off(s->panel);
    qemu_system_shutdown_request(SHUTDOWN_CAUSE_GUEST_SHUTDOWN);
}

/* --------------------------------------------------------------- machine */

static void flipper_zero_init(MachineState *machine)
{
    FlipperZeroState *s = FLIPPER_ZERO(machine);
    DeviceState *dev, *soc;
    DriveInfo *dinfo;
    uint64_t uid64;

    object_initialize_child(OBJECT(machine), "soc", &s->soc, TYPE_WB55_SOC);
    soc = DEVICE(&s->soc);
    uid64 = g_str_hash(s->name ? s->name : "Qemu") | 0x0011223300000000ull;
    qdev_prop_set_uint64(soc, "uid64", uid64);
    qdev_prop_set_uint32(soc, "rtc-bkp1", (s->legacy_sleep ? 0x00002000 : 0) |
                         (s->log_level > 0 ? s->log_level : 0));
    dinfo = drive_get(IF_PFLASH, 0, 0);
    if (dinfo) {
        qdev_prop_set_drive_err(soc, "flash-drive", blk_by_legacy_dinfo(dinfo),
                                &error_fatal);
    }
    if (s->usb_serial) {
        Chardev *chr = qemu_chr_find(s->usb_serial);

        if (!chr) {
            error_report("flipper-zero: usb-serial: no chardev '%s'",
                         s->usb_serial);
            exit(1);
        }
        qdev_prop_set_chr(DEVICE(&s->soc.usb), "chardev", chr);
        object_property_set_bool(OBJECT(&s->soc.usb), "attached",
                                 s->usb_cable, &error_fatal);
    }
    if (s->usbip) {
        Chardev *chr = qemu_chr_find(s->usbip);

        if (!chr) {
            error_report("flipper-zero: usbip: no chardev '%s'", s->usbip);
            exit(1);
        }
        qdev_prop_set_chr(DEVICE(&s->soc.usb), "usbip", chr);
        object_property_set_bool(OBJECT(&s->soc.usb), "attached",
                                 s->usb_cable, &error_fatal);
    }
    flipper_enclave_setup(&s->soc.ipcc);
    if (s->enclave) {
        qdev_prop_set_string(soc, "fus-keys", s->enclave);
    }
    sysbus_realize(SYS_BUS_DEVICE(&s->soc), &error_fatal);
    flipper_provision(s, uid64);

    /* ---- SPI2: display + microSD */
    SSIBus *spi2 = s->soc.spi[1].bus;
    dev = qdev_new(TYPE_ST7567);
    qdev_prop_set_uint32(dev, "scale", s->scale);
    qdev_prop_set_uint8(dev, "cs", 0);
    ssi_realize_and_unref(dev, spi2, &error_fatal);
    s->lcd = ST7567(dev);
    soc_out(s, 'C', 11, qdev_get_gpio_in_named(dev, SSI_GPIO_CS, 0));
    soc_out(s, 'B', 1, qdev_get_gpio_in_named(dev, "a0", 0));
    soc_out(s, 'B', 0, qdev_get_gpio_in_named(dev, "rst", 0));

    dinfo = drive_get(IF_SD, 0, 0);
    dev = spi_dev(spi2, "ssi-sd", 1);
    DeviceState *card = qdev_new(TYPE_SD_CARD_SPI);
    qdev_prop_set_drive_err(card, "drive",
                            dinfo ? blk_by_legacy_dinfo(dinfo) : NULL,
                            &error_fatal);
    qdev_realize_and_unref(card, qdev_get_child_bus(dev, "sd-bus"),
                           &error_fatal);
    s->sd_drive = dinfo != NULL;
    s->sd_spi = dev;
    s->sd_slot = FLIPPER_SD_SLOT(spi_dev(spi2, TYPE_FLIPPER_SD_SLOT, 2));
    s->sd_cs = true;
    flipper_sd_select(s);
    soc_out(s, 'C', 12, qemu_allocate_irq(flipper_sd_cs, s, 0));

    /* ---- SPI1: CC1101 + ST25R3916 */
    SSIBus *spi1 = s->soc.spi[0].bus;
    dev = spi_named(machine, spi1, TYPE_CC1101, 0, "subghz");
    soc_out(s, 'D', 0, qdev_get_gpio_in_named(dev, SSI_GPIO_CS, 0));
    qdev_connect_gpio_out_named(dev, "gdo0", 0, soc_in(s, 'A', 1));
    CC1101(dev)->tim = &s->soc.tim2;    /* GDO0 is also TIM2 TI2 */
    s->cc1101 = CC1101(dev);
    s->soc.tim2.oc_changed = flipper_tim2_oc;   /* TX: TIM2 CH2 -> GDO0 */
    s->soc.tim2.oc_opaque = s;
    dev = spi_named(machine, spi1, TYPE_ST25R3916, 1, "nfc");
    soc_out(s, 'E', 4, qdev_get_gpio_in_named(dev, SSI_GPIO_CS, 0));
    qdev_connect_gpio_out_named(dev, "irq", 0, soc_in(s, 'A', 2));

    /* ---- I2C1: power management */
    I2CBus *i2c1 = s->soc.i2c[0].bus;
    s->gauge = BQ27220(flipper_i2c_dev(machine, i2c1, TYPE_BQ27220, 0x55,
                                       "gauge"));
    s->charger = BQ25896(flipper_i2c_dev(machine, i2c1, TYPE_BQ25896, 0x6B,
                                         "charger"));
    s->charger->gauge = s->gauge;
    s->charger->power_off = flipper_power_off;
    s->charger->opaque = s;
    s->led = LP5562(flipper_i2c_dev(machine, i2c1, TYPE_LP5562, 0x30, "led"));
    s->soc.usb.vbus_changed = flipper_usb_vbus;
    s->soc.usb.vbus_opaque = s;
    if (s->soc.usb.attached) {
        flipper_usb_vbus(s, true);
    }
    if (s->nvram) {
        flipper_nvram_load(s);
        s->exit_notifier.notify = flipper_nvram_save;
        qemu_add_exit_notifier(&s->exit_notifier);
    }
    if (s->log_level >= 0 && s->soc.rtc.backup_valid) {
        /* over what the saved backup domain had */
        s->soc.rtc.bkp[1] = (s->soc.rtc.bkp[1] & ~0xFu) | s->log_level;
    }
    s->led->changed = flipper_led_changed;
    s->led->opaque = s;

    /* ---- buttons, vibro, speaker */
    s->input = qemu_input_handler_register(DEVICE(s->lcd),
                                           &flipper_input_handler);
    qemu_input_handler_activate(s->input);
    soc_out(s, 'A', 8, qemu_allocate_irq(flipper_vibro, s, 0));
    dev = qdev_new(TYPE_FLIPPER_SPEAKER);
    if (machine->audiodev) {
        qdev_prop_set_string(dev, "audiodev", machine->audiodev);
    }
    object_property_add_child(OBJECT(machine), "speaker", OBJECT(dev));
    qdev_realize_and_unref(dev, NULL, &error_fatal);
    s->speaker = FLIPPER_SPEAKER(dev);
    s->soc.tim16->changed = flipper_tim16_changed;
    s->soc.tim16->changed_opaque = s;

    /* ---- the front panel on a link: a window's view of all of the above */
    dev = qdev_new(TYPE_FLIPPER_PANEL);
    s->panel = FLIPPER_PANEL(dev);
    s->panel->lcd = s->lcd;
    s->panel->speaker = s->speaker;
    s->panel->key = flipper_panel_key;
    s->panel->key_opaque = s;
    object_property_add_child(OBJECT(machine), "panel", OBJECT(dev));
    qdev_realize_and_unref(dev, NULL, &error_fatal);

    /* ---- infrared receiver: PA0 and TIM2 TI1 */
    dev = qdev_new(TYPE_FLIPPER_IR);
    object_property_add_child(OBJECT(machine), "ir", OBJECT(dev));
    qdev_realize_and_unref(dev, NULL, &error_fatal);
    s->ir = FLIPPER_IR(dev);
    s->ir->tim = &s->soc.tim2;
    qdev_connect_gpio_out_named(dev, "out", 0, soc_in(s, 'A', 0));

    /* ---- 125 kHz RFID: TIM1 carrier, COMP1 -> TIM2 TI4 (reading);
     *      reader carrier -> TIM2 ETR, TIM2 CH3 -> antenna load (emulating) */
    dev = qdev_new(TYPE_FLIPPER_RFID);
    s->rfid = FLIPPER_RFID(dev);
    s->rfid->comp = &s->soc.comp;
    s->rfid->tim = &s->soc.tim2;
    object_property_add_child(OBJECT(machine), "rfid", OBJECT(dev));
    qdev_realize_and_unref(dev, NULL, &error_fatal);
    notifier_list_add(&s->soc.tim2.cap_read, &s->rfid->cap_read);
    s->soc.tim1.changed = flipper_tim1_changed;
    s->soc.tim1.changed_opaque = s;
    s->soc.tim1.update_hook = flipper_tim1_update;  /* IR transmit */
    s->soc.tim1.update_opaque = s;

    /* ---- iButton: PB14, open drain with a pull-up (1-Wire keys);
     *      COMP1 with the 125 kHz field off (Cyfral, Metakom) */
    dev = qdev_new(TYPE_FLIPPER_IBUTTON);
    s->ibutton = FLIPPER_IBUTTON(dev);
    s->ibutton->comp = &s->soc.comp;
    s->ibutton->rfid = s->rfid;
    s->ibutton->dwt = &s->soc.dwt;
    object_property_add_child(OBJECT(machine), "ibutton", OBJECT(dev));
    qdev_realize_and_unref(dev, NULL, &error_fatal);
    qdev_connect_gpio_out_named(dev, "pull", 0,
                                qemu_allocate_irq(flipper_ibutton_pull, s, 0));
    s->soc.idr_read = flipper_idr_read;
    s->soc.out_written = flipper_out_written;
    s->soc.gpio_hook_opaque = s;
    s->soc.i2c[1].tap = flipper_i2c_tap;
    s->soc.i2c[1].tap_opaque = s;
    s->soc.comp.en_changed = flipper_comp_en;
    s->soc.comp.en_opaque = s;
    s->soc.comp.val_read = flipper_comp_val_read;
    s->soc.comp.val_opaque = s;

    if (!machine->kernel_filename && !drive_get(IF_PFLASH, 0, 0)) {
        warn_report("flipper-zero: no firmware (-kernel) and no flash image "
                    "(-drive if=pflash): the CPU will lock up on erased flash");
    }
    armv7m_load_kernel(s->soc.armv7m.cpu, machine->kernel_filename,
                       WB55_FLASH_BASE, WB55_FLASH_SIZE);

    s->hdr_rx_end[0] = timer_new_ns(QEMU_CLOCK_VIRTUAL,
                                    flipper_usart_pulse_end, s);
    s->hdr_rx_end[1] = timer_new_ns(QEMU_CLOCK_VIRTUAL,
                                    flipper_lpuart_pulse_end, s);
    for (int n = 0; n < 2; n++) {
        dev = DEVICE(n ? &s->soc.lpuart1 : &s->soc.usart1);
        /* (a line of qemu/patches/0008: a tree without it still runs) */
        if (object_property_find(OBJECT(dev), "rx-unheard[0]")) {
            qdev_connect_gpio_out_named(dev, "rx-unheard", 0,
                qemu_allocate_irq(flipper_rx_unheard, s, n));
        }
    }

    s->halt_watch = timer_new_ns(QEMU_CLOCK_VIRTUAL, flipper_halt_watch, s);
    timer_mod(s->halt_watch,
              qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL) + HALT_WATCH_NS);
    qemu_add_vm_change_state_handler(flipper_halt_vm_state, s);
}

static char *flipper_get_nvram(Object *obj, Error **errp)
{
    return g_strdup(FLIPPER_ZERO(obj)->nvram);
}

static void flipper_set_nvram(Object *obj, const char *value, Error **errp)
{
    FlipperZeroState *s = FLIPPER_ZERO(obj);

    g_free(s->nvram);
    s->nvram = *value ? g_strdup(value) : NULL;
}

static char *flipper_get_enclave(Object *obj, Error **errp)
{
    return g_strdup(FLIPPER_ZERO(obj)->enclave);
}

static void flipper_set_enclave(Object *obj, const char *value, Error **errp)
{
    FlipperZeroState *s = FLIPPER_ZERO(obj);

    g_free(s->enclave);
    s->enclave = *value ? g_strdup(value) : NULL;
}

static char *flipper_get_usb_serial(Object *obj, Error **errp)
{
    return g_strdup(FLIPPER_ZERO(obj)->usb_serial);
}

static void flipper_set_usb_serial(Object *obj, const char *value,
                                   Error **errp)
{
    FlipperZeroState *s = FLIPPER_ZERO(obj);

    g_free(s->usb_serial);
    s->usb_serial = *value ? g_strdup(value) : NULL;
}

static char *flipper_get_usbip(Object *obj, Error **errp)
{
    return g_strdup(FLIPPER_ZERO(obj)->usbip);
}

static void flipper_set_usbip(Object *obj, const char *value, Error **errp)
{
    FlipperZeroState *s = FLIPPER_ZERO(obj);

    g_free(s->usbip);
    s->usbip = *value ? g_strdup(value) : NULL;
}

static char *flipper_get_name(Object *obj, Error **errp)
{
    return g_strdup(FLIPPER_ZERO(obj)->name);
}

static void flipper_set_name(Object *obj, const char *value, Error **errp)
{
    FlipperZeroState *s = FLIPPER_ZERO(obj);

    if (strlen(value) > 8) {
        error_setg(errp, "device name is at most 8 characters");
        return;
    }
    g_free(s->name);
    s->name = g_strdup(value);
}

static bool flipper_get_legacy(Object *obj, Error **errp)
{
    return FLIPPER_ZERO(obj)->legacy_sleep;
}

static void flipper_set_legacy(Object *obj, bool v, Error **errp)
{
    FLIPPER_ZERO(obj)->legacy_sleep = v;
}

/*
 * The firmware keeps its log level in the low four bits of RTC backup
 * register 1 (Settings > System > Log Level writes it there) and reads it
 * when it starts: set while it runs, it is the level of its next start.
 */
static const char *const flipper_log_levels[] = {
    "default", "none", "error", "warn", "info", "debug", "trace",
};

static char *flipper_get_log_level(Object *obj, Error **errp)
{
    FlipperZeroState *s = FLIPPER_ZERO(obj);
    int level = s->log_level;

    if (phase_check(PHASE_MACHINE_INITIALIZED)) {
        level = s->soc.rtc.bkp[1] & 0xF;
    }
    if (level < 0) {
        return g_strdup("");
    }
    if (level >= ARRAY_SIZE(flipper_log_levels)) {
        return g_strdup_printf("%d", level);
    }
    return g_strdup(flipper_log_levels[level]);
}

static void flipper_set_log_level(Object *obj, const char *value, Error **errp)
{
    FlipperZeroState *s = FLIPPER_ZERO(obj);
    int level = g_ascii_strcasecmp(value, "warning") ? -1 : 3;

    for (int i = 0; i < ARRAY_SIZE(flipper_log_levels); i++) {
        if (!g_ascii_strcasecmp(value, flipper_log_levels[i])) {
            level = i;
        }
    }
    if (level < 0) {
        error_setg(errp, "log-level: default, none, error, warn, info, "
                   "debug or trace, not '%s'", value);
        return;
    }
    s->log_level = level;
    if (phase_check(PHASE_MACHINE_INITIALIZED)) {
        s->soc.rtc.bkp[1] = (s->soc.rtc.bkp[1] & ~0xFu) | level;
    }
}

static bool flipper_get_usb_cable(Object *obj, Error **errp)
{
    return FLIPPER_ZERO(obj)->usb_cable;
}

static void flipper_set_usb_cable(Object *obj, bool v, Error **errp)
{
    FLIPPER_ZERO(obj)->usb_cable = v;
}

static bool flipper_get_sd_card(Object *obj, Error **errp)
{
    FlipperZeroState *s = FLIPPER_ZERO(obj);

    return s->sd_in && (s->sd_drive ||
                        !phase_check(PHASE_MACHINE_INITIALIZED));
}

/* Pulled out and pushed back in, it is the same card: the image of -drive.
 * Out, it is gone in the middle of whatever it was doing: a block that was
 * on its way is not written, and the firmware gets no more answers.  In
 * again, it has been without power, and starts as it does at power-on. */
static void flipper_set_sd_card(Object *obj, bool v, Error **errp)
{
    FlipperZeroState *s = FLIPPER_ZERO(obj);

    if (!phase_check(PHASE_MACHINE_INITIALIZED)) {
        s->sd_in = v;
    } else if (v && !s->sd_drive) {
        error_setg(errp, "no microSD card: started without -drive if=sd");
    } else if (v != s->sd_in) {
        s->sd_in = v;
        if (v) {
            device_cold_reset(s->sd_spi);
        }
        flipper_sd_select(s);
        flipper_sd_detect(s);
    }
}

static void flipper_zero_instance_init(Object *obj)
{
    FlipperZeroState *s = FLIPPER_ZERO(obj);

    s->name = g_strdup("Qemu");
    s->scale = 4;
    s->region = 2;                      /* US/CA/AU */
    s->legacy_sleep = false;            /* the firmware's default: STOP2 */
    s->log_level = -1;
    s->usb_cable = true;
    s->sd_in = true;
    object_property_add_uint32_ptr(obj, "lcd-scale", &s->scale,
                                   OBJ_PROP_FLAG_READWRITE);
    object_property_add_uint32_ptr(obj, "region", &s->region,
                                   OBJ_PROP_FLAG_READWRITE);
    object_property_add_bool(obj, "legacy-sleep", flipper_get_legacy,
                             flipper_set_legacy);
    object_property_add_str(obj, "log-level", flipper_get_log_level,
                            flipper_set_log_level);
    object_property_set_description(obj, "log-level",
        "The firmware's log level: default, none, error, warn, info, debug "
        "or trace (not given: as it was left).  It reads it when it starts");
    object_property_add_bool(obj, "usb-cable", flipper_get_usb_cable,
                             flipper_set_usb_cable);
    object_property_set_description(obj, "usb-cable",
        "The USB cable is plugged in from the start (default on); off: plug "
        "it later with /machine/soc/usb attached");
    object_property_add_str(obj, "header", flipper_get_header,
                            flipper_set_header);
    object_property_set_description(obj, "header",
        "The GPIO header's pins as the firmware has them (in+r, in+f, "
        "in+rf: an input with an interrupt on its rise, fall, both), and what "
        "the outside holds on them: set PA7=1, PA7=0 or PA7=z (let go)");
    object_property_add_str(obj, "header-serial", flipper_get_header_serial,
                            NULL);
    object_property_set_description(obj, "header-serial",
        "The header's serial ports as the firmware has them: for each its "
        "name, pins, baud rate (0: off), frame, and who takes what comes in");
    object_property_add_str(obj, "header-i2c", flipper_get_header_i2c, NULL);
    object_property_set_description(obj, "header-i2c",
        "The header's I2C bus: its name and pins, how many addresses the "
        "firmware called and how many nobody took, the last one, and the "
        "devices on it (-device TYPE,bus=i2c3,address=N)");
    object_property_add_str(obj, "header-i2c-record",
                            flipper_get_header_i2c_record,
                            flipper_set_header_i2c_record);
    object_property_set_description(obj, "header-i2c-record",
        "A text file that gets every transfer on the header's I2C bus: the "
        "address called, the bytes written and read (off: the record ends)."
        "  Read: calls, those unanswered, bytes written, bytes read, ns, "
        "the file");
    object_property_add_str(obj, "header-record", flipper_get_header_record,
                            flipper_set_header_record);
    object_property_set_description(obj, "header-record",
        "A VCD file that gets every change of a header pin's level with its "
        "time (off: the record ends).  Read: changes, ns, the file");
    object_property_add_bool(obj, "sd-card", flipper_get_sd_card,
                             flipper_set_sd_card);
    object_property_set_description(obj, "sd-card",
        "The microSD card is in its slot (default on, with -drive if=sd); "
        "it can be pulled out and pushed in while running");
}

static void flipper_zero_class_init(ObjectClass *oc, const void *data)
{
    MachineClass *mc = MACHINE_CLASS(oc);
    static const char *valid_cpu_types[] = {
        ARM_CPU_TYPE_NAME("cortex-m4"),
        NULL
    };

    mc->desc = "Flipper Zero (STM32WB55RG, Cortex-M4F, firmware target f7)";
    mc->init = flipper_zero_init;
    mc->reset = flipper_zero_reset;
    mc->valid_cpu_types = valid_cpu_types;
    mc->default_ram_size = 0;
    mc->default_cpus = 1;
    mc->min_cpus = 1;
    mc->max_cpus = 1;
    machine_add_audiodev_property(mc);  /* -M flipper-zero,audiodev=ID: speaker */
    object_class_property_add_str(oc, "name", flipper_get_name,
                                  flipper_set_name);
    object_class_property_add_str(oc, "nvram", flipper_get_nvram,
                                  flipper_set_nvram);
    object_class_property_add_str(oc, "usbip", flipper_get_usbip,
                                  flipper_set_usbip);
    object_class_property_set_description(oc, "usbip",
        "Chardev id (a TCP server socket) exporting the USB device over USB/IP");
    object_class_property_add_str(oc, "usb-serial", flipper_get_usb_serial,
                                  flipper_set_usb_serial);
    object_class_property_set_description(oc, "usb-serial",
        "Chardev id for the USB CDC port (CLI/RPC); plugs in the USB cable");
    object_class_property_set_description(oc, "nvram",
        "File keeping battery-backed state (gauge, RTC) between runs");
    object_class_property_add_str(oc, "enclave", flipper_get_enclave,
                                  flipper_set_enclave);
    object_class_property_set_description(oc, "enclave",
        "File keeping the secure enclave's keys; made with FlipperEmu's own "
        "factory keys if it doesn't exist");
    object_class_property_set_description(oc, "name",
        "Device name programmed into OTP (max 8 chars)");
}

static const TypeInfo flipper_zero_type[] = {
    {
        .name = TYPE_FLIPPER_ZERO,
        .parent = TYPE_MACHINE,
        .instance_size = sizeof(FlipperZeroState),
        .instance_init = flipper_zero_instance_init,
        .class_init = flipper_zero_class_init,
        .interfaces = arm_machine_interfaces,
    }, {
        .name = TYPE_FLIPPER_SD_SLOT,
        .parent = TYPE_SSI_PERIPHERAL,
        .instance_size = sizeof(FlipperSdSlot),
        .class_init = flipper_sd_slot_class_init,
    },
};
DEFINE_TYPES(flipper_zero_type)
