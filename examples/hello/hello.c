// Copyright (c) 2026, Kara Zajac
// SPDX-License-Identifier: BSD-3-Clause
//
// An app to try FlipperEmu with (docs/DEVELOPING.md): it draws, it takes the
// buttons, it logs.  Back leaves it, and so does the loader's request to
// (which `ufbt launch` sends to the app that runs, before it starts the new
// build).  A long press of OK crashes it on purpose, to see what a crash
// looks like from outside.
#include <furi.h>
#include <gui/gui.h>
#include <input/input.h>

#define TAG "FeHello"

typedef struct {
    FuriMessageQueue* keys;
    FuriMutex* mutex;
    uint32_t presses;
    const char* last;
} Hello;

static void hello_draw(Canvas* canvas, void* context) {
    Hello* hello = context;
    char line[40];

    furi_mutex_acquire(hello->mutex, FuriWaitForever);
    snprintf(line, sizeof(line), "%lu pressed, the last: %s", hello->presses, hello->last);
    furi_mutex_release(hello->mutex);

    canvas_clear(canvas);
    canvas_set_font(canvas, FontPrimary);
    canvas_draw_str_aligned(canvas, 64, 10, AlignCenter, AlignCenter, "Hello, FlipperEmu");
    canvas_set_font(canvas, FontSecondary);
    canvas_draw_str_aligned(canvas, 64, 30, AlignCenter, AlignCenter, line);
    canvas_draw_str_aligned(canvas, 64, 52, AlignCenter, AlignCenter, "Back leaves, long OK crashes");
}

static void hello_input(InputEvent* event, void* context) {
    Hello* hello = context;
    furi_message_queue_put(hello->keys, event, FuriWaitForever);
}

// Asked to end by the loader (`loader close`), on the thread that asks: it is
// queued like a key, with a type that no key has
static bool hello_signal(uint32_t signal, void* arg, void* context) {
    UNUSED(arg);
    Hello* hello = context;
    if(signal != FuriSignalExit) return false;
    InputEvent leave = {.type = InputTypeMAX};
    furi_message_queue_put(hello->keys, &leave, 0);
    return true;
}

int32_t fe_hello_app(void* p) {
    UNUSED(p);
    Hello hello = {
        .keys = furi_message_queue_alloc(8, sizeof(InputEvent)),
        .mutex = furi_mutex_alloc(FuriMutexTypeNormal),
        .last = "none",
    };

    ViewPort* view_port = view_port_alloc();
    view_port_draw_callback_set(view_port, hello_draw, &hello);
    view_port_input_callback_set(view_port, hello_input, &hello);
    Gui* gui = furi_record_open(RECORD_GUI);
    gui_add_view_port(gui, view_port, GuiLayerFullscreen);
    furi_thread_set_signal_callback(furi_thread_get_current(), hello_signal, &hello);
    FURI_LOG_I(TAG, "started");

    InputEvent event;
    while(furi_message_queue_get(hello.keys, &event, FuriWaitForever) == FuriStatusOk) {
        if(event.type == InputTypeLong && event.key == InputKeyOk) {
            FURI_LOG_E(TAG, "a long OK: crashing on purpose");
            furi_crash("fe_hello: on purpose");
        }
        if(event.type == InputTypeMAX) break;
        if(event.type != InputTypeShort) continue;
        if(event.key == InputKeyBack) break;

        furi_mutex_acquire(hello.mutex, FuriWaitForever);
        hello.presses++;
        hello.last = input_get_key_name(event.key);
        furi_mutex_release(hello.mutex);
        FURI_LOG_I(TAG, "%s, which makes %lu", hello.last, hello.presses);
        view_port_update(view_port);
    }

    FURI_LOG_I(TAG, "left after %lu", hello.presses);
    furi_thread_set_signal_callback(furi_thread_get_current(), NULL, NULL);
    gui_remove_view_port(gui, view_port);
    furi_record_close(RECORD_GUI);
    view_port_free(view_port);
    furi_mutex_free(hello.mutex);
    furi_message_queue_free(hello.keys);
    return 0;
}
