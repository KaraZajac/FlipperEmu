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
 *
 * Usage:
 *   qemu-system-arm -M flipper-zero -kernel firmware.elf \
 *       -drive if=sd,format=raw,file=sd.img -serial stdio
 *   -drive if=pflash,format=raw,file=flash.img  persistent 1 MiB flash
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
#include "system/system.h"
#include "target/arm/cpu-qom.h"
#include "wb55.h"
#include "flipper.h"

#define TYPE_FLIPPER_ZERO MACHINE_TYPE_NAME("flipper-zero")
OBJECT_DECLARE_SIMPLE_TYPE(FlipperZeroState, FLIPPER_ZERO)

struct FlipperZeroState {
    MachineState parent_obj;
    WB55SocState soc;
    St7567State *lcd;
    Lp5562State *led;
    FlipperSpeakerState *speaker;
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
    QemuInputHandlerState *input;
    uint32_t keys_down;          /* bit per button */
    bool sd_present;
    char *nvram;                 /* battery-backed state file */
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

enum { BTN_UP, BTN_DOWN, BTN_LEFT, BTN_RIGHT, BTN_OK, BTN_BACK, BTN_NUM };
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
    s->lcd->led_rgb = (r << 16) | (g << 8) | b;
    s->lcd->dirty = true;
}

static void flipper_vibro(void *opaque, int n, int level)
{
    FlipperZeroState *s = opaque;
    s->lcd->vibro = level;
    s->lcd->status_dirty = true;
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

/* External levels the board holds on MCU pins. A GPIO port reset releases
 * external drivers, so these are re-applied after every system reset. */
static void flipper_drive_pins(FlipperZeroState *s)
{
    qemu_set_irq(soc_in(s, 'C', 10), s->sd_present ? 0 : 1); /* SD card detect */
    qemu_set_irq(soc_in(s, 'C', 2), 0);     /* SPI2 MISO idle low */
    qemu_set_irq(soc_in(s, 'B', 4), 0);     /* SPI1 MISO: CC1101 CHIP_RDYn */
    flipper_ir_idle(s->ir);                 /* IR receiver output idles high */
    flipper_ibutton_reset(s->ibutton);      /* iButton line pulled up */
    for (int b = 0; b < BTN_NUM; b++) {     /* buttons released */
        if (!(s->keys_down & (1u << b))) {
            qemu_set_irq(soc_in(s, buttons[b].port, buttons[b].pin),
                         buttons[b].active_high ? 0 : 1);
        }
    }
}

/* a read of GPIOB's IDR while the firmware's drive of PB14 is off: if a
 * 1-Wire key holds the line low for it, that read has sampled it */
static void flipper_idr_read(void *opaque, int port)
{
    FlipperZeroState *s = opaque;

    if (port == 1 && (s->soc.gpio[1].odr & (1u << 14))) {
        flipper_ibutton_sampled(s->ibutton);
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

static void flipper_zero_reset(MachineState *machine, ResetType type)
{
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
    qdev_prop_set_uint32(soc, "rtc-bkp1", s->legacy_sleep ? 0x00002000 : 0);
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
        object_property_set_bool(OBJECT(&s->soc.usb), "attached", true,
                                 &error_fatal);
    }
    if (s->usbip) {
        Chardev *chr = qemu_chr_find(s->usbip);

        if (!chr) {
            error_report("flipper-zero: usbip: no chardev '%s'", s->usbip);
            exit(1);
        }
        qdev_prop_set_chr(DEVICE(&s->soc.usb), "usbip", chr);
        object_property_set_bool(OBJECT(&s->soc.usb), "attached", true,
                                 &error_fatal);
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
    soc_out(s, 'C', 12, qdev_get_gpio_in_named(dev, SSI_GPIO_CS, 0));
    DeviceState *card = qdev_new(TYPE_SD_CARD_SPI);
    qdev_prop_set_drive_err(card, "drive",
                            dinfo ? blk_by_legacy_dinfo(dinfo) : NULL,
                            &error_fatal);
    qdev_realize_and_unref(card, qdev_get_child_bus(dev, "sd-bus"),
                           &error_fatal);
    s->sd_present = dinfo != NULL;

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
    soc_out(s, 'B', 14, qdev_get_gpio_in_named(dev, "line", 0));
    qdev_connect_gpio_out_named(dev, "pull", 0, soc_in(s, 'B', 14));
    s->soc.idr_read = flipper_idr_read;
    s->soc.idr_opaque = s;
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

static void flipper_zero_instance_init(Object *obj)
{
    FlipperZeroState *s = FLIPPER_ZERO(obj);

    s->name = g_strdup("Qemu");
    s->scale = 4;
    s->region = 2;                      /* US/CA/AU */
    s->legacy_sleep = false;            /* the firmware's default: STOP2 */
    object_property_add_uint32_ptr(obj, "lcd-scale", &s->scale,
                                   OBJ_PROP_FLAG_READWRITE);
    object_property_add_uint32_ptr(obj, "region", &s->region,
                                   OBJ_PROP_FLAG_READWRITE);
    object_property_add_bool(obj, "legacy-sleep", flipper_get_legacy,
                             flipper_set_legacy);
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
    },
};
DEFINE_TYPES(flipper_zero_type)
