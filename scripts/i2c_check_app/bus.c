// Copyright (c) 2026, Kara Zajac
// SPDX-License-Identifier: BSD-3-Clause
//
// What scripts/i2c_check.py has a firmware do on the header's I2C bus that
// neither example in examples/i2c does: a START with no STOP before it, a
// byte that a device refuses, a transfer of more than 255 bytes, one that
// is held and gone on with, and one that is open for seconds.  It runs
// once, logs what every call said, and ends; the check has a record of the
// bus (`i2c record`) to hold beside that.  It is the check's own and no
// example.
//
//     ./flipperemu run --i2c eeprom@0x50 --i2c i2c-echo@0x42
//     cd scripts/i2c_check_app && ../../flipperemu ufbt launch
//
// QEMU's i2c-echo keeps three bytes, gives them back to a read, and refuses
// a fourth.  furi_hal_i2c takes an address as it is on the wire, shifted
// left by one.
#include <furi.h>
#include <furi_hal.h>

#define TAG "FeBus"
#define ECHO (0x42 << 1)
#define EEPROM (0x50 << 1)
#define NOBODY (0x33 << 1)
#define WAIT 100 /* ms that a transfer may take */
#define LONG 300 /* bytes: more than one count of the peripheral holds */
#define HOLD 4000 /* ms between a write and the START after it, at the end */

int32_t fe_bus_check_app(void* p) {
    UNUSED(p);
    const FuriHalI2cBusHandle* bus = &furi_hal_i2c_handle_external;
    uint8_t* big = malloc(LONG);
    uint8_t got[3] = {0};
    bool a, b, c;

    furi_hal_i2c_acquire(bus);

    // 1: three bytes to the echo, and with no STOP another START to read them
    const uint8_t three[] = {0xa1, 0xb2, 0xc3};
    a = furi_hal_i2c_tx_ext(
        bus, ECHO, false, three, sizeof(three), FuriHalI2cBeginStart, FuriHalI2cEndAwaitRestart,
        WAIT);
    b = furi_hal_i2c_rx_ext(
        bus, ECHO, false, got, sizeof(got), FuriHalI2cBeginRestart, FuriHalI2cEndStop, WAIT);
    FURI_LOG_I(TAG, "1 written %d, read after a START again %d: %02x %02x %02x", a, b, got[0],
               got[1], got[2]);

    // 2: four bytes, of which the echo refuses the last
    const uint8_t four[] = {0x01, 0x02, 0x03, 0x04};
    a = furi_hal_i2c_tx(bus, ECHO, four, sizeof(four), WAIT);
    FURI_LOG_I(TAG, "2 four bytes written %d", a);

    // 3: five: the fourth is refused, and the fifth is not sent
    const uint8_t five[] = {0x11, 0x12, 0x13, 0x14, 0x15};
    a = furi_hal_i2c_tx(bus, ECHO, five, sizeof(five), WAIT);
    FURI_LOG_I(TAG, "3 five bytes written %d", a);

    // 4: a byte to the echo, and with no STOP a START for another device:
    // the EEPROM, which has two bytes at 0x10 by then
    const uint8_t fill[] = {0x10, 0xe1, 0xe2};
    const uint8_t at = 0x10;
    const uint8_t one = 0x21;
    memset(got, 0, sizeof(got));
    a = furi_hal_i2c_tx(bus, EEPROM, fill, sizeof(fill), WAIT) &&
        furi_hal_i2c_tx(bus, EEPROM, &at, 1, WAIT);
    b = furi_hal_i2c_tx_ext(
        bus, ECHO, false, &one, 1, FuriHalI2cBeginStart, FuriHalI2cEndAwaitRestart, WAIT);
    c = furi_hal_i2c_rx_ext(
        bus, EEPROM, false, got, 2, FuriHalI2cBeginRestart, FuriHalI2cEndStop, WAIT);
    FURI_LOG_I(TAG, "4 EEPROM set %d, echo written %d, EEPROM read after a START again %d: "
               "%02x %02x", a, b, c, got[0], got[1]);

    // 5: the same for an address where nobody is
    const uint8_t gone = 0x31;
    a = furi_hal_i2c_tx_ext(
        bus, ECHO, false, &gone, 1, FuriHalI2cBeginStart, FuriHalI2cEndAwaitRestart, WAIT);
    b = furi_hal_i2c_rx_ext(
        bus, NOBODY, false, got, 1, FuriHalI2cBeginRestart, FuriHalI2cEndStop, WAIT);
    FURI_LOG_I(TAG, "5 echo written %d, nobody read after a START again %d", a, b);

    // 6: 300 bytes of the EEPROM from its byte 0 in one read (it has 256,
    // and goes on at its start)
    const uint8_t zero = 0;
    uint32_t sum = 0;
    memset(big, 0, LONG);
    a = furi_hal_i2c_trx(bus, EEPROM, &zero, 1, big, LONG, WAIT);
    for(size_t i = 0; i < LONG; i++) sum += big[i];
    FURI_LOG_I(TAG, "6 %d bytes read %d: their sum %lu, byte 16 %02x, byte 273 %02x", LONG, a,
               sum, big[16], big[273]);

    // 7: two bytes to the echo, the transfer held (no STOP, and no START
    // after it), and a third; then all three read
    const uint8_t two[] = {0x41, 0x42};
    const uint8_t third = 0x43;
    memset(got, 0, sizeof(got));
    a = furi_hal_i2c_tx_ext(
        bus, ECHO, false, two, sizeof(two), FuriHalI2cBeginStart, FuriHalI2cEndPause, WAIT);
    b = furi_hal_i2c_tx_ext(
        bus, ECHO, false, &third, 1, FuriHalI2cBeginResume, FuriHalI2cEndStop, WAIT);
    c = furi_hal_i2c_rx(bus, ECHO, got, sizeof(got), WAIT);
    FURI_LOG_I(TAG, "7 two bytes written and held %d, a third %d, read %d: %02x %02x %02x", a, b,
               c, got[0], got[1], got[2]);

    // 8: a byte to the echo, and four seconds before the START that reads
    // it: time for the check to end its record inside a transfer
    const uint8_t late = 0x51;
    memset(got, 0, sizeof(got));
    a = furi_hal_i2c_tx_ext(
        bus, ECHO, false, &late, 1, FuriHalI2cBeginStart, FuriHalI2cEndAwaitRestart, WAIT);
    FURI_LOG_I(TAG, "8 written %d, and the bus is held for %d s", a, HOLD / 1000);
    furi_delay_ms(HOLD);
    b = furi_hal_i2c_rx_ext(
        bus, ECHO, false, got, 1, FuriHalI2cBeginRestart, FuriHalI2cEndStop, WAIT);
    FURI_LOG_I(TAG, "8 read after a START again %d: %02x", b, got[0]);

    furi_hal_i2c_release(bus);
    free(big);
    FURI_LOG_I(TAG, "done");
    return 0;
}
