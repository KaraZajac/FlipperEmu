// Copyright (c) 2026, Kara Zajac
// SPDX-License-Identifier: BSD-3-Clause
//
// What scripts/xip_check.py runs: what a loader of apps that run in place
// does, at its smallest.  A page of the flash that nothing uses (the last
// below the radio stack's, and only when it is blank) is programmed with a
// function while the firmware runs, and the function is called where it
// is; then with another function at the same address, which has to be the
// one that runs then; then erased again.  It is the check's own and no
// example: it writes the flash by its registers, with no word to the radio
// stack, which a loader on a Flipper must not do.
//
// What it finds goes to the log, a line each ("FeXipCheck"), "done" last.
// It then waits to be closed.
#include <furi.h>
#include <furi_hal.h>
#include <stm32wbxx.h>

#define TAG       "FeXipCheck"
#define PAGE_SIZE 4096U

// movs r0, #42; bx lr; nop; nop
static const uint8_t code_first[8] = {0x2A, 0x20, 0x70, 0x47, 0x00, 0xBF, 0x00, 0xBF};
// movs r0, #43; bx lr; nop; nop
static const uint8_t code_again[8] = {0x2B, 0x20, 0x70, 0x47, 0x00, 0xBF, 0x00, 0xBF};
// movs r1, #0; loop: adds r1, r1, r0; subs r0, #1; bne loop; mov r0, r1;
// bx lr; nop; nop  (the sum of 1 to r0: a loop, so more than one block)
static const uint8_t code_sum[16] = {0x00, 0x21, 0x09, 0x18, 0x01, 0x38, 0xFC, 0xD1,
                                     0x08, 0x46, 0x70, 0x47, 0x00, 0xBF, 0x00, 0xBF};

typedef uint32_t (*Function)(uint32_t);

static void flash_wait(void) {
    while(FLASH->SR & (FLASH_SR_BSY | FLASH_SR_PESD)) {
    }
}

static void flash_erase(uint32_t page) {
    flash_wait();
    FLASH->SR = FLASH_SR_EOP | FLASH_SR_OPERR | FLASH_SR_PROGERR | FLASH_SR_WRPERR |
                FLASH_SR_PGAERR | FLASH_SR_SIZERR | FLASH_SR_PGSERR;
    FLASH->CR = (FLASH->CR & ~FLASH_CR_PNB) | FLASH_CR_PER | (page << FLASH_CR_PNB_Pos);
    FLASH->CR |= FLASH_CR_STRT;
    flash_wait();
    FLASH->CR &= ~(FLASH_CR_PER | FLASH_CR_PNB);
}

// (eight bytes at a time, as the flash takes them)
static void flash_program(uint32_t at, const uint8_t* bytes, size_t size) {
    flash_wait();
    FLASH->CR |= FLASH_CR_PG;
    for(size_t i = 0; i < size; i += 8) {
        uint32_t low, high;
        memcpy(&low, bytes + i, 4);
        memcpy(&high, bytes + i + 4, 4);
        *(volatile uint32_t*)(at + i) = low;
        *(volatile uint32_t*)(at + i + 4) = high;
        flash_wait();
    }
    FLASH->CR &= ~FLASH_CR_PG;
}

static bool page_blank(uint32_t at) {
    for(uint32_t i = 0; i < PAGE_SIZE; i += 4) {
        if(*(const uint32_t*)(at + i) != 0xFFFFFFFFU) return false;
    }
    return true;
}

static bool on_signal(uint32_t signal, void* arg, void* context) {
    UNUSED(arg);
    if(signal != FuriSignalExit) return false;
    furi_semaphore_release(context);
    return true;
}

int32_t fe_xip_check_app(void* p) {
    UNUSED(p);
    FuriSemaphore* leave = furi_semaphore_alloc(1, 0);
    furi_thread_set_signal_callback(furi_thread_get_current(), on_signal, leave);

    // the page below the first that is the radio stack's
    uint32_t page = (FLASH->SFR & FLASH_SFR_SFSA) - 1;
    uint32_t at = FLASH_BASE + page * PAGE_SIZE;
    bool blank = page_blank(at);
    FURI_LOG_I(TAG, "page %lu at %08lX: %s", page, at, blank ? "blank" : "in use");
    if(blank) {
        FURI_CRITICAL_ENTER();
        bool locked = FLASH->CR & FLASH_CR_LOCK;
        if(locked) {
            FLASH->KEYR = 0x45670123U;
            FLASH->KEYR = 0xCDEF89ABU;
        }
        bool open = !(FLASH->CR & FLASH_CR_LOCK);
        uint32_t first = 0, sum = 0, again = 0;
        bool same = false, restored = false;
        if(open) {
            flash_program(at, code_first, sizeof(code_first));
            flash_program(at + 0x100, code_sum, sizeof(code_sum));
            same = !memcmp((const void*)at, code_first, sizeof(code_first)) &&
                   !memcmp((const void*)(at + 0x100), code_sum, sizeof(code_sum));
            first = ((Function)(at | 1))(0);
            sum = ((Function)((at + 0x100) | 1))(10);
            // another function where the first was: it is the one that runs
            flash_erase(page);
            flash_program(at, code_again, sizeof(code_again));
            again = ((Function)(at | 1))(0);
            flash_erase(page);
            restored = page_blank(at);
            if(locked) FLASH->CR |= FLASH_CR_LOCK;
        }
        FURI_CRITICAL_EXIT();
        FURI_LOG_I(TAG, "flash: %s", open ? "unlocked" : "stays locked");
        FURI_LOG_I(TAG, "read back: %s", same ? "same" : "other");
        FURI_LOG_I(TAG, "first: %lu", first);
        FURI_LOG_I(TAG, "sum: %lu", sum);
        FURI_LOG_I(TAG, "again: %lu", again);
        FURI_LOG_I(TAG, "restored: %s", restored ? "blank" : "not blank");
    }
    FURI_LOG_I(TAG, "done");

    furi_semaphore_acquire(leave, FuriWaitForever);
    furi_thread_set_signal_callback(furi_thread_get_current(), NULL, NULL);
    furi_semaphore_free(leave);
    return 0;
}
