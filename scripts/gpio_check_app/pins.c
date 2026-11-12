// Copyright (c) 2026, Kara Zajac
// SPDX-License-Identifier: BSD-3-Clause
//
// What scripts/gpio_check.py has a firmware do on the header's pins that its
// `gpio` CLI command cannot: take an interrupt from a pin, and hold a line
// low that others hold too (an open-drain output).  It is the check's own
// and no example.
//
//     pin 4, PA4    an input with a pull-up, an interrupt on both edges
//     pin 16, PC0   an input with a pull-up, an interrupt when it falls
//     pin 15, PC1   an input with a pull-down, an interrupt when it rises
//     pin 6, PB2    an input with a pull-up; when it falls, this ends
//     pin 2, PA7    an open-drain output with a pull-up
//
// PA7 does what PA4 does: pulled low while PA4 is low, let go while it is
// high.  Every rise of PC1 turns PC0's interrupt off, or on again.  Each
// interrupt is logged with its number, and with what PA7 reads then:
//
//     ./flipperemu run
//     cd scripts/gpio_check_app && ../../flipperemu ufbt launch
//     ../../flipperemu gpio PA4 low      ("1 PA4 fell: PA7 pulled low, reads 0")
//
// A pin's interrupt is its number's: one line, and one handler, for PA4,
// PB4 and PC4.  The firmware's keys have lines 3, 6 and 10 to 13 (OK is PH3,
// Down is PC6), and an expansion module's call line 7 (PB7): so pins 3, 5
// and 7 (PA6, PB3, PC3) have no interrupt to give, and pin 2 (PA7) has none
// while the firmware waits for a module on pins 13 and 14;
// furi_hal_gpio_add_int_callback ends in a crash there.  A line also keeps
// the edges that it was given before: line 2 has its rising edge from the
// firmware's start (PA2, the NFC chip's interrupt), so PB2 asked for its
// fall alone gets its rise too.
#include <furi.h>
#include <furi_hal.h>
#include <furi_hal_resources.h>

#define TAG "FePins"
#define QUIET 120 /* s with no interrupt, after which it ends by itself */

enum { PinBoth, PinFall, PinRise, PinEnd, PinCount, PinLeave = PinCount };

static const struct {
    const GpioPin* pin;
    const char* name;
    GpioMode mode;
    GpioPull pull;
} pins[PinCount] = {
    [PinBoth] = {&gpio_ext_pa4, "PA4", GpioModeInterruptRiseFall, GpioPullUp},
    [PinFall] = {&gpio_ext_pc0, "PC0", GpioModeInterruptFall, GpioPullUp},
    [PinRise] = {&gpio_ext_pc1, "PC1", GpioModeInterruptRise, GpioPullDown},
    [PinEnd] = {&gpio_ext_pb2, "PB2", GpioModeInterruptFall, GpioPullUp},
};

typedef struct {
    uint8_t id;
    uint8_t level;
} Edge;

typedef struct {
    FuriMessageQueue* edges;
    uint8_t id;
} Watch;

// In the interrupt: the pin's level as it is then, for the app's thread
static void pin_edge(void* context) {
    Watch* watch = context;
    Edge edge = {.id = watch->id, .level = furi_hal_gpio_read(pins[watch->id].pin)};
    furi_message_queue_put(watch->edges, &edge, 0);
}

// Asked to end by the loader (`loader close`)
static bool pin_signal(uint32_t signal, void* arg, void* context) {
    UNUSED(arg);
    if(signal != FuriSignalExit) return false;
    Edge leave = {.id = PinLeave};
    furi_message_queue_put(context, &leave, 0);
    return true;
}

int32_t fe_pin_check_app(void* p) {
    UNUSED(p);
    FuriMessageQueue* edges = furi_message_queue_alloc(16, sizeof(Edge));
    const GpioPin* line = &gpio_ext_pa7;
    Watch watch[PinCount];
    uint32_t count = 0;
    bool low = false; /* does it pull PA7 low */
    bool listens = true; /* is PC0's interrupt on */
    Edge edge;

    furi_thread_set_signal_callback(furi_thread_get_current(), pin_signal, edges);

    furi_hal_gpio_write(line, true);
    furi_hal_gpio_init(line, GpioModeOutputOpenDrain, GpioPullUp, GpioSpeedLow);
    for(uint8_t i = 0; i < PinCount; i++) {
        watch[i].edges = edges;
        watch[i].id = i;
        furi_hal_gpio_init(pins[i].pin, pins[i].mode, pins[i].pull, GpioSpeedLow);
        furi_hal_gpio_add_int_callback(pins[i].pin, pin_edge, &watch[i]);
    }
    FURI_LOG_I(
        TAG,
        "ready: PA4 %d, PC0 %d, PC1 %d, PB2 %d; PA7 let go, reads %d",
        furi_hal_gpio_read(pins[PinBoth].pin),
        furi_hal_gpio_read(pins[PinFall].pin),
        furi_hal_gpio_read(pins[PinRise].pin),
        furi_hal_gpio_read(pins[PinEnd].pin),
        furi_hal_gpio_read(line));

    while(true) {
        if(furi_message_queue_get(edges, &edge, furi_ms_to_ticks(QUIET * 1000)) != FuriStatusOk) {
            FURI_LOG_W(TAG, "no interrupt for %d s", QUIET);
            break;
        }
        if(edge.id == PinLeave) {
            FURI_LOG_I(TAG, "asked to end");
            break;
        }
        count++;
        const char* more = "";
        if(edge.id == PinBoth) {
            low = !edge.level;
            furi_hal_gpio_write(line, !low);
        } else if(edge.id == PinRise) {
            listens = !listens;
            if(listens) {
                furi_hal_gpio_enable_int_callback(pins[PinFall].pin);
                more = "; PC0's interrupt on";
            } else {
                furi_hal_gpio_disable_int_callback(pins[PinFall].pin);
                more = "; PC0's interrupt off";
            }
        }
        FURI_LOG_I(
            TAG,
            "%lu %s %s: PA7 %s, reads %d%s",
            count,
            pins[edge.id].name,
            edge.level ? "rose" : "fell",
            low ? "pulled low" : "let go",
            furi_hal_gpio_read(line),
            more);
        if(edge.id == PinEnd) break;
    }

    // In this order: a pin that is made analog while its interrupt is on
    // takes its edges off the line with it
    for(uint8_t i = 0; i < PinCount; i++) {
        furi_hal_gpio_enable_int_callback(pins[i].pin);
        furi_hal_gpio_init(pins[i].pin, GpioModeAnalog, GpioPullNo, GpioSpeedLow);
        furi_hal_gpio_remove_int_callback(pins[i].pin);
    }
    furi_hal_gpio_init(line, GpioModeAnalog, GpioPullNo, GpioSpeedLow);
    furi_message_queue_free(edges);
    FURI_LOG_I(TAG, "done: %lu interrupts", count);
    return 0;
}
