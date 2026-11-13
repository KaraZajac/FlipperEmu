// Copyright (c) 2026, Kara Zajac
// SPDX-License-Identifier: BSD-3-Clause
//
// What scripts/snapshot_check.py has a firmware do with the enclave around
// a save.  A key of the enclave is loaded into AES1 (the slot is the app's
// argument: 1 to 10 are the factory keys, 11 is the device's own, which is
// made here if the device has none yet), and the app waits for OK with the
// key loaded: the check saves the machine there, stops it and loads the
// snapshot.  After OK a block is encrypted with the key that is loaded, then
// once more (AES1 goes on from the first: CBC), the key is unloaded, loaded
// again and the block encrypted a third time.  The log has what came out
// each time, and the check holds that beside a run of the app with no save
// in it.  It is the check's own and no example.
//
//     ./flipperemu app /ext/apps/Debug/fe_keys_check.fap 11
//
// With slot 1 the block is the one of the firmware's own self-test for that
// key (furi_hal_crypto.c: enclave_signature_iv[0], enclave_signature_input[0]
// and enclave_signature_expected[0]).  AES1 answers that one from a table for
// FlipperEmu's key 1 (flipper_enclave.c), and a state has no copy of where
// the table is: the FUS puts that back after a load.
#include <furi.h>
#include <furi_hal.h>
#include <gui/gui.h>
#include <input/input.h>
#include <stdio.h>
#include <stdlib.h>

#define TAG "FeKeys"
#define BLOCK 16

static const uint8_t own_iv[BLOCK] = {
    0x46, 0x6c, 0x69, 0x70, 0x70, 0x65, 0x72, 0x45,
    0x6d, 0x75, 0x20, 0x49, 0x56, 0x20, 0x30, 0x31};
static const uint8_t own_block[BLOCK] = {
    0x61, 0x20, 0x62, 0x6c, 0x6f, 0x63, 0x6b, 0x20,
    0x6f, 0x66, 0x20, 0x31, 0x36, 0x20, 0x21, 0x21};
static const uint8_t test_iv[BLOCK] = {
    0xac, 0x5d, 0x68, 0xb8, 0x79, 0x74, 0xfc, 0x7f,
    0x45, 0x02, 0x82, 0xf1, 0x48, 0x7e, 0x75, 0x8a};
static const uint8_t test_block[BLOCK] = {
    0x9f, 0x5c, 0xb1, 0x43, 0x17, 0x53, 0x18, 0x8c,
    0x66, 0x3d, 0x39, 0x45, 0x90, 0x13, 0xa9, 0xde};
static const uint8_t test_answer[BLOCK] = {
    0xe9, 0x9a, 0xce, 0xe9, 0x4d, 0xe1, 0x7f, 0x55,
    0xcb, 0x8a, 0xbf, 0xf2, 0x4d, 0x98, 0x27, 0x67};

static void draw(Canvas* canvas, void* context) {
    UNUSED(context);
    canvas_set_font(canvas, FontPrimary);
    canvas_draw_str(canvas, 2, 12, "A key is loaded");
    canvas_set_font(canvas, FontSecondary);
    canvas_draw_str(canvas, 2, 28, "OK: on with it");
}

static void input(InputEvent* event, void* context) {
    if(event->type == InputTypeShort && event->key == InputKeyOk) {
        furi_message_queue_put(context, event, 0);
    }
}

static const char* hex(const uint8_t* data, char* text) {
    for(size_t i = 0; i < BLOCK; i++) {
        snprintf(text + 2 * i, 3, "%02x", data[i]);
    }
    return text;
}

static void wait_for_ok(void) {
    FuriMessageQueue* queue = furi_message_queue_alloc(4, sizeof(InputEvent));
    ViewPort* view_port = view_port_alloc();
    view_port_draw_callback_set(view_port, draw, NULL);
    view_port_input_callback_set(view_port, input, queue);
    Gui* gui = furi_record_open(RECORD_GUI);
    gui_add_view_port(gui, view_port, GuiLayerFullscreen);
    InputEvent event;
    furi_message_queue_get(queue, &event, FuriWaitForever);
    gui_remove_view_port(gui, view_port);
    furi_record_close(RECORD_GUI);
    view_port_free(view_port);
    furi_message_queue_free(queue);
}

int32_t fe_keys_check_app(void* p) {
    int asked = p ? atoi(p) : (int)FURI_HAL_CRYPTO_ENCLAVE_UNIQUE_KEY_SLOT;
    if(asked < 1 || asked > (int)FURI_HAL_CRYPTO_ENCLAVE_UNIQUE_KEY_SLOT) {
        FURI_LOG_E(TAG, "no slot of the enclave's keys: %d", asked);
        return 1;
    }
    uint8_t slot = asked;
    const uint8_t* iv = slot == 1 ? test_iv : own_iv;
    const uint8_t* block = slot == 1 ? test_block : own_block;
    uint8_t out[BLOCK];
    char text[2 * BLOCK + 1];

    // (the device's own key is made when it is first asked for: U2F does that)
    bool there = furi_hal_crypto_enclave_ensure_key(slot);
    bool loaded = there && furi_hal_crypto_enclave_load_key(slot, iv);
    if(!loaded) {
        FURI_LOG_E(TAG, "slot %d there %d, not loaded", slot, there);
        return 1;
    }
    FURI_LOG_I(TAG, "slot %d loaded 1, waiting for OK", slot);
    wait_for_ok();

    bool used = furi_hal_crypto_encrypt(block, out, BLOCK);
    if(slot == 1) {
        FURI_LOG_I(
            TAG, "1 used %d, the self-test's answer %d: %s", used,
            used && !memcmp(out, test_answer, BLOCK), used ? hex(out, text) : "-");
    } else {
        FURI_LOG_I(TAG, "1 used %d: %s", used, used ? hex(out, text) : "-");
    }
    used = furi_hal_crypto_encrypt(block, out, BLOCK);
    FURI_LOG_I(TAG, "2 once more, used %d: %s", used, used ? hex(out, text) : "-");
    bool unloaded = furi_hal_crypto_enclave_unload_key(slot);
    loaded = unloaded && furi_hal_crypto_enclave_load_key(slot, iv);
    used = loaded && furi_hal_crypto_encrypt(block, out, BLOCK);
    FURI_LOG_I(
        TAG, "3 unloaded %d, loaded again %d, used %d: %s", unloaded, loaded, used,
        used ? hex(out, text) : "-");
    if(loaded) {
        furi_hal_crypto_enclave_unload_key(slot);
    }
    FURI_LOG_I(TAG, "left");
    return 0;
}
