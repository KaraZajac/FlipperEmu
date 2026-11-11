// Copyright (c) 2026, Kara Zajac
// SPDX-License-Identifier: BSD-3-Clause
//
// The I2C bus of the header, pins 15 (SDA) and 16 (SCL), from an app: a
// TMP105 temperature sensor at 0x48, read once a second, and an EEPROM at
// 0x50 that counts the times this app ran.  It is what sensor.js does, for
// every firmware: the official one's JS has no i2c module.
//
//     ./flipperemu run --i2c tmp105@0x48,temperature=23.5 --i2c eeprom@0x50
//     cd examples/i2c && ../../flipperemu ufbt launch
//     ../../flipperemu i2c 0x48 temperature -7.5       (and the screen follows)
//
// furi_hal_i2c takes an address as it is on the wire, shifted left by one:
// 0x48 is 0x90.
#include <furi.h>
#include <furi_hal.h>
#include <gui/gui.h>
#include <input/input.h>

#define TAG "FeSensor"
#define SENSOR (0x48 << 1)
#define EEPROM (0x50 << 1)
#define WAIT 50 /* ms that a transfer may take */
#define NO_SENSOR INT32_MAX

typedef struct {
    FuriMessageQueue* keys;
    FuriMutex* mutex;
    int32_t hundredths; /* of a degree, or NO_SENSOR */
} Sensor;

// The temperature in hundredths of a degree, NO_SENSOR when nothing answers
static int32_t sensor_read(void) {
    const FuriHalI2cBusHandle* bus = &furi_hal_i2c_handle_external;
    // register 1, its configuration: 12 bits (it starts with 9)
    const uint8_t setup[] = {1, 0x60};
    // register 0: the temperature in 1/256 of a degree, the high byte first
    const uint8_t temperature = 0;
    uint8_t raw[2];
    int32_t hundredths = NO_SENSOR;

    furi_hal_i2c_acquire(bus);
    if(furi_hal_i2c_tx(bus, SENSOR, setup, sizeof(setup), WAIT) &&
       furi_hal_i2c_trx(bus, SENSOR, &temperature, 1, raw, sizeof(raw), WAIT)) {
        hundredths = (int16_t)(raw[0] << 8 | raw[1]) * 100 / 256;
    }
    furi_hal_i2c_release(bus);
    return hundredths;
}

// The EEPROM's byte 0, one more than it was: how often this ran, 0 when
// nothing answers (a new EEPROM has 0xFF everywhere)
static uint8_t eeprom_count(void) {
    const FuriHalI2cBusHandle* bus = &furi_hal_i2c_handle_external;
    const uint8_t at = 0;
    uint8_t count = 0;
    uint8_t runs = 0;

    furi_hal_i2c_acquire(bus);
    if(furi_hal_i2c_trx(bus, EEPROM, &at, 1, &count, 1, WAIT)) {
        const uint8_t set[] = {at, count == 0xFF ? 1 : count + 1};
        if(furi_hal_i2c_tx(bus, EEPROM, set, sizeof(set), WAIT)) runs = set[1];
    }
    furi_hal_i2c_release(bus);
    return runs;
}

static void sensor_text(char* text, size_t size, int32_t hundredths) {
    int32_t amount = hundredths < 0 ? -hundredths : hundredths;
    snprintf(
        text,
        size,
        "%s%ld.%02ld",
        hundredths < 0 ? "-" : "",
        amount / 100,
        amount % 100);
}

static void sensor_draw(Canvas* canvas, void* context) {
    Sensor* sensor = context;
    char text[16];

    furi_mutex_acquire(sensor->mutex, FuriWaitForever);
    int32_t hundredths = sensor->hundredths;
    furi_mutex_release(sensor->mutex);

    canvas_clear(canvas);
    canvas_set_font(canvas, FontPrimary);
    canvas_draw_str_aligned(canvas, 64, 8, AlignCenter, AlignCenter, "Sensor at 0x48, in C");
    if(hundredths == NO_SENSOR) {
        canvas_draw_str_aligned(canvas, 64, 32, AlignCenter, AlignCenter, "does not answer");
    } else {
        sensor_text(text, sizeof(text), hundredths);
        canvas_set_font(canvas, FontBigNumbers);
        canvas_draw_str_aligned(canvas, 64, 32, AlignCenter, AlignCenter, text);
    }
    canvas_set_font(canvas, FontSecondary);
    canvas_draw_str_aligned(canvas, 64, 56, AlignCenter, AlignCenter, "Back leaves");
}

static void sensor_input(InputEvent* event, void* context) {
    Sensor* sensor = context;
    furi_message_queue_put(sensor->keys, event, FuriWaitForever);
}

// Asked to end by the loader (`loader close`), on the thread that asks: it is
// queued like a key, with a type that no key has
static bool sensor_signal(uint32_t signal, void* arg, void* context) {
    UNUSED(arg);
    Sensor* sensor = context;
    if(signal != FuriSignalExit) return false;
    InputEvent leave = {.type = InputTypeMAX};
    furi_message_queue_put(sensor->keys, &leave, 0);
    return true;
}

int32_t fe_sensor_app(void* p) {
    UNUSED(p);
    Sensor sensor = {
        .keys = furi_message_queue_alloc(8, sizeof(InputEvent)),
        .mutex = furi_mutex_alloc(FuriMutexTypeNormal),
        .hundredths = NO_SENSOR,
    };
    char text[16];

    ViewPort* view_port = view_port_alloc();
    view_port_draw_callback_set(view_port, sensor_draw, &sensor);
    view_port_input_callback_set(view_port, sensor_input, &sensor);
    Gui* gui = furi_record_open(RECORD_GUI);
    gui_add_view_port(gui, view_port, GuiLayerFullscreen);
    furi_thread_set_signal_callback(furi_thread_get_current(), sensor_signal, &sensor);

    uint8_t runs = eeprom_count();
    if(runs) {
        FURI_LOG_I(TAG, "started, run number %u by the EEPROM at 0x50", runs);
    } else {
        FURI_LOG_W(TAG, "started, no EEPROM at 0x50 to count the runs");
    }

    // Once a second, and at any key: the sensor is read, and a temperature
    // that is not the one before is logged and drawn
    InputEvent event;
    bool first = true;
    for(;;) {
        int32_t hundredths = sensor_read();
        if(first || hundredths != sensor.hundredths) {
            furi_mutex_acquire(sensor.mutex, FuriWaitForever);
            sensor.hundredths = hundredths;
            furi_mutex_release(sensor.mutex);
            if(hundredths == NO_SENSOR) {
                FURI_LOG_W(TAG, "no sensor at 0x48");
            } else {
                sensor_text(text, sizeof(text), hundredths);
                FURI_LOG_I(TAG, "%s C at 0x48", text);
            }
            view_port_update(view_port);
            first = false;
        }
        if(furi_message_queue_get(sensor.keys, &event, 1000) != FuriStatusOk) continue;
        if(event.type == InputTypeMAX) break;
        if(event.type == InputTypeShort && event.key == InputKeyBack) break;
    }

    FURI_LOG_I(TAG, "left");
    furi_thread_set_signal_callback(furi_thread_get_current(), NULL, NULL);
    gui_remove_view_port(gui, view_port);
    furi_record_close(RECORD_GUI);
    view_port_free(view_port);
    furi_mutex_free(sensor.mutex);
    furi_message_queue_free(sensor.keys);
    return 0;
}
