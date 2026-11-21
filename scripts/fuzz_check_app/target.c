// Copyright (c) 2026, Kara Zajac
// SPDX-License-Identifier: BSD-3-Clause
//
// What scripts/fuzz_check.py has `flipperemu fuzz` find: a crash that an
// input reaches only through three checks of its bytes, one after another.
// It is the check's own and no example.
//
// The input is a signal's argument, which the CLI gives to the app that
// runs (`loader signal 100 A1B2C3D4`: signal 100, four bytes as hex):
//
//     ./flipperemu run --coverage
//     cd scripts/fuzz_check_app && ../../flipperemu ufbt launch
//     ../../flipperemu fuzz cli --command 'loader signal 100 {hex}' --max 4
//
// Each check that an input passes runs code that never ran before, which is
// what the fuzzer keeps an input for: the first byte is found, then the
// second from it, then the third.  An input made blindly would have to have
// all three at once (one in sixteen million).
//
// Signal 101 writes a word to the address that is its argument: with 0, a
// fault of the CPU's (the firmware keeps the first megabyte from everyone),
// for the place that the emulator notes of one.  Signal 102 calls itself
// until the stack of the thread that runs it is used up: the firmware keeps
// 32 bytes at the end of each thread's stack read-only (its MPU's region,
// set at every change of thread), and the push that reaches them faults.
#include <furi.h>

#define TAG "FeFuzzCheck"
#define SIGNAL_INPUT 100
#define SIGNAL_WRITE 101
#define SIGNAL_DEEPER 102

typedef struct {
    FuriSemaphore* leave;
    // (volatile: each check stays code of its own, with its count kept)
    volatile uint32_t inputs, first, second;
} Target;

// (a frame of 16 bytes: a push of each reaches the 32 bytes at the end)
static volatile uint32_t target_depth;

static uint32_t target_deeper(uint32_t n) {
    volatile uint32_t here = n;
    target_depth = n;
    if(n > 1000000) return n;
    return target_deeper(n + 1) + here; // (no tail call)
}

static bool target_signal(uint32_t signal, void* arg, void* context) {
    Target* target = context;
    if(signal == FuriSignalExit) {
        furi_semaphore_release(target->leave);
        return true;
    }
    if(signal == SIGNAL_WRITE) {
        volatile uint32_t* where = arg;
        *where = 1;
        return true;
    }
    if(signal == SIGNAL_DEEPER) {
        target_deeper(0);
        return true;
    }
    if(signal != SIGNAL_INPUT) return false;

    uint32_t input = (uint32_t)arg;
    target->inputs++;
    if((input >> 24) == 0x80) {
        target->first++;
        if(((input >> 16) & 0xFF) == 0x7F) {
            target->second++;
            if(((input >> 8) & 0xFF) == 0x10) {
                furi_crash("fe_fuzz_check: found");
            }
        }
    }
    return true;
}

int32_t fe_fuzz_check_app(void* p) {
    UNUSED(p);
    Target target = {.leave = furi_semaphore_alloc(1, 0)};
    furi_thread_set_signal_callback(furi_thread_get_current(), target_signal, &target);
    FURI_LOG_I(TAG, "waiting for inputs");

    furi_semaphore_acquire(target.leave, FuriWaitForever);

    FURI_LOG_I(
        TAG,
        "left after %lu inputs: %lu passed the first check, %lu the second",
        target.inputs,
        target.first,
        target.second);
    furi_thread_set_signal_callback(furi_thread_get_current(), NULL, NULL);
    furi_semaphore_free(target.leave);
    return 0;
}
