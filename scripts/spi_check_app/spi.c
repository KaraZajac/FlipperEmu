// Copyright (c) 2026, Kara Zajac
// SPDX-License-Identifier: BSD-3-Clause
//
// What scripts/spi_check.py has a firmware do on the header's SPI bus (SPI1
// on pins 2, 3 and 5: MOSI on PA7, MISO on PA6, SCK on PB3), with two serial
// flashes there: one with its chip select on pin 4 (PA4), where the
// firmware's handle for the header has it, and one on pin 7 (PC3).  First
// what an app does with a flash: its IDs, a sector erased, a page written
// and read.  Then what the firmware's own handle does not: a chip select on
// another pin, the other clock mode and another speed, the least significant
// bit first, frames of 16 bits, a pin of the three that is not the bus's,
// and bytes that are clocked with nobody selected.  It is the check's own
// and no example.
//
// It logs what every step read.  After some it waits until what is held to
// PB2 (pin 6) changes: high for the next step, low for the one after it,
// and so on.
//
//     ./flipperemu run --spi w25q32@PA4 --spi w25q80bl@PC3
//     cd scripts/spi_check_app && ../../flipperemu ufbt launch
//     ./flipperemu spi             ("... the last at 4 MHz, mode 3, ...")
//     ./flipperemu gpio PB2 high   (the next step)
#include <furi.h>
#include <furi_hal.h>
#include <furi_hal_resources.h>
#include <furi_hal_spi.h>
#include <furi_hal_spi_config.h>

#include <stm32wbxx_ll_spi.h>

#define TAG "FeSpi"
#define WAIT 100 /* ms that a transfer may take */
#define SECTOR 0x002000 /* erased, and a page written there */
#define LEVELS 0x003000 /* written with MOSI as a plain output */

// How a handle of the app's own has the bus
typedef struct {
    uint32_t speed; /* LL_SPI_BAUDRATEPRESCALER_... */
    bool mode3; /* the clock rests high, and the second edge counts */
    bool lsb_first;
    bool wide; /* frames of 16 bits */
    bool no_sck, no_miso; /* a pin that is left as it is: analog */
    GpioPull miso_pull;
} How;

static How how;
static bool held = false;

static void own_event(const FuriHalSpiBusHandle* handle, FuriHalSpiBusHandleEvent event) {
    if(event == FuriHalSpiBusHandleEventInit) {
        if(handle->cs) {
            furi_hal_gpio_write(handle->cs, true);
            furi_hal_gpio_init(handle->cs, GpioModeOutputPushPull, GpioPullUp, GpioSpeedVeryHigh);
        }
    } else if(event == FuriHalSpiBusHandleEventDeinit) {
        if(handle->cs) {
            furi_hal_gpio_write(handle->cs, true);
            furi_hal_gpio_init(handle->cs, GpioModeAnalog, GpioPullNo, GpioSpeedLow);
        }
    } else if(event == FuriHalSpiBusHandleEventActivate) {
        LL_SPI_InitTypeDef frame = furi_hal_spi_preset_1edge_low_2m;
        frame.BaudRate = how.speed;
        if(how.mode3) {
            frame.ClockPolarity = LL_SPI_POLARITY_HIGH;
            frame.ClockPhase = LL_SPI_PHASE_2EDGE;
        }
        if(how.lsb_first) frame.BitOrder = LL_SPI_LSB_FIRST;
        if(how.wide) frame.DataWidth = LL_SPI_DATAWIDTH_16BIT;
        LL_SPI_Init(handle->bus->spi, &frame);
        LL_SPI_SetRxFIFOThreshold(
            handle->bus->spi, how.wide ? LL_SPI_RX_FIFO_TH_HALF : LL_SPI_RX_FIFO_TH_QUARTER);
        LL_SPI_Enable(handle->bus->spi);

        if(!how.no_miso)
            furi_hal_gpio_init_ex(
                handle->miso,
                GpioModeAltFunctionPushPull,
                how.miso_pull,
                GpioSpeedVeryHigh,
                GpioAltFn5SPI1);
        furi_hal_gpio_init_ex(
            handle->mosi,
            GpioModeAltFunctionPushPull,
            GpioPullDown,
            GpioSpeedVeryHigh,
            GpioAltFn5SPI1);
        if(!how.no_sck)
            furi_hal_gpio_init_ex(
                handle->sck,
                GpioModeAltFunctionPushPull,
                GpioPullDown,
                GpioSpeedVeryHigh,
                GpioAltFn5SPI1);
        if(handle->cs) furi_hal_gpio_write(handle->cs, false);
    } else if(event == FuriHalSpiBusHandleEventDeactivate) {
        if(handle->cs) furi_hal_gpio_write(handle->cs, true);
        furi_hal_gpio_init(handle->miso, GpioModeAnalog, GpioPullNo, GpioSpeedLow);
        furi_hal_gpio_init(handle->mosi, GpioModeAnalog, GpioPullNo, GpioSpeedLow);
        furi_hal_gpio_init(handle->sck, GpioModeAnalog, GpioPullNo, GpioSpeedLow);
        LL_SPI_Disable(handle->bus->spi);
    }
}

// The bus with the chip select of the app's choice: pin 4, pin 7, or none
static FuriHalSpiBusHandle own = {
    .bus = &furi_hal_spi_bus_r,
    .callback = own_event,
    .miso = &gpio_ext_pa6,
    .mosi = &gpio_ext_pa7,
    .sck = &gpio_ext_pb3,
    .cs = NULL,
};

static const How plain = {
    .speed = LL_SPI_BAUDRATEPRESCALER_DIV32,
    .miso_pull = GpioPullDown,
};

// The bus as `way` has it, and with that chip select, until given().  (Pin 4
// is an output all the while, as the firmware's handle has it: a chip select
// that was made analog again would select nothing after that.)
static void take(const GpioPin* cs, How way) {
    how = way;
    own.cs = cs;
    if(cs != &gpio_ext_pa4) furi_hal_spi_bus_handle_init(&own);
    furi_hal_spi_acquire(&own);
}

static void given(void) {
    furi_hal_spi_release(&own);
    if(own.cs != &gpio_ext_pa4) furi_hal_spi_bus_handle_deinit(&own);
}

// Bytes out and then bytes in, while the handle has the bus
static void bytes(
    const FuriHalSpiBusHandle* handle,
    const uint8_t* out,
    size_t n_out,
    uint8_t* in,
    size_t n_in) {
    furi_hal_spi_bus_tx(handle, out, n_out, WAIT);
    if(n_in) {
        memset(in, 0, n_in);
        furi_hal_spi_bus_rx(handle, in, n_in, WAIT);
    }
}

// The same as a frame of its own: the handle takes the bus, and lets go
static void ask(
    const FuriHalSpiBusHandle* handle,
    const uint8_t* out,
    size_t n_out,
    uint8_t* in,
    size_t n_in) {
    furi_hal_spi_acquire(handle);
    bytes(handle, out, n_out, in, n_in);
    furi_hal_spi_release(handle);
}

// A flash's bytes from an address on
static void flash_read(const FuriHalSpiBusHandle* handle, uint32_t at, uint8_t* in, size_t n) {
    const uint8_t out[] = {0x03, at >> 16, at >> 8, at};
    ask(handle, out, sizeof(out), in, n);
}

// Wait until a flash is done with an erase or a write.  -> its status then
static uint8_t flash_done(const FuriHalSpiBusHandle* handle) {
    const uint8_t out = 0x05;
    uint8_t status;
    do {
        ask(handle, &out, 1, &status, 1);
    } while(status & 1);
    return status;
}

// A frame of 16 bits, as the chip's own calls send it
static uint16_t frame16(uint16_t out) {
    while(!LL_SPI_IsActiveFlag_TXE(SPI1)) {
    }
    LL_SPI_TransmitData16(SPI1, out);
    while(!LL_SPI_IsActiveFlag_RXNE(SPI1)) {
    }
    return LL_SPI_ReceiveData16(SPI1);
}

// Bytes as text: "ef 40 16"
static const char* hex(const uint8_t* b, size_t n) {
    static char text[3 * 16 + 1];
    char* to = text;
    for(size_t i = 0; i < n && i < 16; i++) to += snprintf(to, 4, "%02x ", b[i]);
    if(to > text) to[-1] = 0;
    return text;
}

// Say what was done, and wait until what is held to PB2 is the other level
static void step(const char* said) {
    FURI_LOG_I(TAG, "%s", said);
    held = !held;
    while(furi_hal_gpio_read(&gpio_ext_pb2) != held) furi_delay_ms(20);
}

int32_t fe_spi_check_app(void* p) {
    UNUSED(p);
    const FuriHalSpiBusHandle* ext = &furi_hal_spi_bus_handle_external;
    const uint8_t jedec = 0x9f, enable = 0x06;
    uint8_t in[8];
    How way;

    furi_hal_gpio_init(&gpio_ext_pb2, GpioModeInput, GpioPullDown, GpioSpeedLow);
    furi_hal_spi_bus_handle_init(ext);

    // 0: what the flash on pin 4 has from a run before this one
    flash_read(ext, SECTOR, in, 8);
    FURI_LOG_I(TAG, "0 kept: %s", hex(in, 8));

    // 1: its JEDEC ID, by the firmware's handle for the header
    ask(ext, &jedec, 1, in, 3);
    FURI_LOG_I(TAG, "1 JEDEC ID: %s", hex(in, 3));

    // 2: Winbond's two older IDs.  90h: the maker and the device, or the
    // device first when the address is 1; ABh: the device alone
    const uint8_t id90[] = {0x90, 0, 0, 0}, id90_1[] = {0x90, 0, 0, 1}, id_ab[] = {0xab, 0, 0, 0};
    ask(ext, id90, sizeof(id90), in, 4);
    ask(ext, id90_1, sizeof(id90_1), in + 4, 2);
    ask(ext, id_ab, sizeof(id_ab), in + 6, 2);
    FURI_LOG_I(TAG, "2 90h: %s", hex(in, 4));
    FURI_LOG_I(TAG, "2 90h from 1: %s", hex(in + 4, 2));
    FURI_LOG_I(TAG, "2 ABh: %s", hex(in + 6, 2));

    // 3: a sector erased, and eight bytes written to its start
    const uint8_t erase[] = {0x20, (uint8_t)(SECTOR >> 16), (uint8_t)(SECTOR >> 8), (uint8_t)SECTOR};
    const uint8_t page[] = {0x02, (uint8_t)(SECTOR >> 16), (uint8_t)(SECTOR >> 8),
                            (uint8_t)SECTOR, 'F', 'l', 'i', 'p', 'p', 'e', 'r', '!'};
    ask(ext, &enable, 1, NULL, 0);
    ask(ext, erase, sizeof(erase), NULL, 0);
    flash_done(ext);
    flash_read(ext, SECTOR, in, 8);
    FURI_LOG_I(TAG, "3 erased: %s", hex(in, 8));
    ask(ext, &enable, 1, NULL, 0);
    ask(ext, page, sizeof(page), NULL, 0);
    flash_done(ext);
    flash_read(ext, SECTOR, in, 8);
    FURI_LOG_I(TAG, "3 written: %s", hex(in, 8));

    // 4: the flash on pin 7, by a handle of the app's own: 4 MHz, and the
    // other clock mode that a flash takes
    way = plain;
    way.speed = LL_SPI_BAUDRATEPRESCALER_DIV16;
    way.mode3 = true;
    take(&gpio_ext_pc3, way);
    bytes(&own, &jedec, 1, in, 3);
    given();
    FURI_LOG_I(TAG, "4 on pin 7: %s", hex(in, 3));
    step("4 done");

    // 5: the least significant bit first.  The flash has the other order,
    // so the command goes out turned around, and its answer comes in so
    const uint8_t turned = 0xf9;
    way = plain;
    way.lsb_first = true;
    take(&gpio_ext_pa4, way);
    bytes(&own, &turned, 1, in, 3);
    given();
    FURI_LOG_I(TAG, "5 LSB first: %s", hex(in, 3));
    step("5 done");

    // 6: frames of 16 bits: the command and a byte of the answer in the
    // first, two more bytes in the second
    way = plain;
    way.wide = true;
    take(&gpio_ext_pa4, way);
    uint16_t first = frame16(0x9f00);
    uint16_t second = frame16(0x0000);
    given();
    FURI_LOG_I(TAG, "6 frames of 16 bits: %04x %04x", first, second);
    step("6 done");

    // 7: the clock's pin is not the bus's: nothing reaches the header
    way = plain;
    way.no_sck = true;
    take(&gpio_ext_pa4, way);
    bytes(&own, &jedec, 1, in, 3);
    given();
    FURI_LOG_I(TAG, "7 no clock on pin 5: %s", hex(in, 3));

    // 8: MISO's pin is not the bus's: the flash answers, and nobody hears
    way = plain;
    way.no_miso = true;
    take(&gpio_ext_pa4, way);
    bytes(&own, &jedec, 1, in, 3);
    given();
    FURI_LOG_I(TAG, "8 pin 3 not MISO: %s", hex(in, 3));

    // 9: MOSI's pin is a plain output for three bytes of a write: the flash
    // gets the pin's level for every bit, low, high and low, and not a5
    const uint8_t levels[] = {0x02, (uint8_t)(LEVELS >> 16), (uint8_t)(LEVELS >> 8), (uint8_t)LEVELS};
    const uint8_t a5 = 0xa5;
    ask(ext, &enable, 1, NULL, 0);
    furi_hal_spi_acquire(ext);
    bytes(ext, levels, sizeof(levels), NULL, 0);
    furi_hal_gpio_write(&gpio_ext_pa7, false);
    furi_hal_gpio_init(&gpio_ext_pa7, GpioModeOutputPushPull, GpioPullNo, GpioSpeedLow);
    bytes(ext, &a5, 1, NULL, 0);
    furi_hal_gpio_write(&gpio_ext_pa7, true);
    bytes(ext, &a5, 1, NULL, 0);
    furi_hal_gpio_write(&gpio_ext_pa7, false);
    bytes(ext, &a5, 1, NULL, 0);
    furi_hal_spi_release(ext);
    flash_done(ext);
    flash_read(ext, LEVELS, in, 4);
    FURI_LOG_I(TAG, "9 pin 2 not MOSI: %s", hex(in, 4));

    // 10: nobody is selected.  MISO has its pull then: down, and up
    way = plain;
    take(NULL, way);
    bytes(&own, &jedec, 1, in, 3);
    given();
    way.miso_pull = GpioPullUp;
    take(NULL, way);
    bytes(&own, &jedec, 1, in + 3, 3);
    given();
    FURI_LOG_I(TAG, "10 nobody selected: %s", hex(in, 6));

    // 11: the firmware's handle has the bus, and the flash on pin 4 is in
    // the middle of a read, which goes on where it was after the wait
    const uint8_t begin[] = {0x03, (uint8_t)(SECTOR >> 16), (uint8_t)(SECTOR >> 8), (uint8_t)SECTOR};
    furi_hal_spi_acquire(ext);
    bytes(ext, begin, sizeof(begin), in, 2);
    FURI_LOG_I(TAG, "11 read: %s", hex(in, 2));
    step("11 the bus is held");
    memset(in, 0, 2);
    furi_hal_spi_bus_rx(ext, in, 2, WAIT);
    furi_hal_spi_release(ext);
    FURI_LOG_I(TAG, "11 read on: %s", hex(in, 2));

    // 12: the app selects nobody, and the outside holds pin 7 low by now:
    // the flash there answers
    take(NULL, plain);
    bytes(&own, &jedec, 1, in, 3);
    given();
    FURI_LOG_I(TAG, "12 selected from outside: %s", hex(in, 3));
    step("12 done");

    // 13: nobody is selected again, and the outside holds MISO high
    take(NULL, plain);
    bytes(&own, &jedec, 1, in, 3);
    given();
    FURI_LOG_I(TAG, "13 MISO held from outside: %s", hex(in, 3));

    furi_hal_spi_bus_handle_deinit(ext);
    furi_hal_gpio_init(&gpio_ext_pb2, GpioModeAnalog, GpioPullNo, GpioSpeedLow);
    FURI_LOG_I(TAG, "done");
    return 0;
}
