// Copyright (c) 2026, Kara Zajac
// SPDX-License-Identifier: BSD-3-Clause
//
// What scripts/gpio_check.py has a firmware measure with its ADC that a
// script's readAnalog() does not show: the firmware's own turning of a
// result into volts and degrees, the ADC's other reference voltage, and a
// pin's voltage that is the firmware's own doing.  It is the check's own and
// no example.
//
// It measures once and ends, and says in the log what it read:
//
//     ./flipperemu run
//     ./flipperemu gpio PA7 1.25V
//     cd scripts/adc_check_app && ../../flipperemu ufbt launch
//     ("at 2.048 V: pin 2 PA7 2500 and 2500 = 1250 mV")
//
// With each of the two reference voltages (2.048 V with 64 samples for a
// result, as furi_hal_adc_configure has it; 2.5 V with one sample):
//   - what is inside the chip: its reference voltage (which the firmware
//     turns into the ADC's own reference in mV), its temperature sensor,
//     and a third of the battery pin's voltage
//   - every pin of the header that the firmware's own table (gpio_pins) has
//     as an input of the ADC, twice in a row, as an analog pin
// And then pin 16 (PC0), with the 2.5 V reference, as an output that is
// high and low, and as an input with a pull-up and a pull-down: nothing from
// outside is needed for those.
#include <furi.h>
#include <furi_hal.h>
#include <furi_hal_resources.h>

#define TAG "FeAdc"

static void measure(FuriHalAdcHandle* adc, const char* at) {
    uint16_t ref = furi_hal_adc_read(adc, FuriHalAdcChannelVREFINT);
    uint16_t temp = furi_hal_adc_read(adc, FuriHalAdcChannelTEMPSENSOR);
    uint16_t bat = furi_hal_adc_read(adc, FuriHalAdcChannelVBAT);
    FURI_LOG_I(
        TAG,
        "%s: reference %u = %d mV, temperature %u = %d C, battery %u = %d mV",
        at,
        ref,
        (int)furi_hal_adc_convert_vref(adc, ref),
        temp,
        (int)furi_hal_adc_convert_temp(adc, temp),
        bat,
        (int)furi_hal_adc_convert_vbat(adc, bat));

    for(size_t i = 0; i < gpio_pins_count; i++) {
        const GpioPinRecord* p = &gpio_pins[i];
        if(p->channel == FuriHalAdcChannelNone) continue;
        furi_hal_gpio_init(p->pin, GpioModeAnalog, GpioPullNo, GpioSpeedLow);
        uint16_t first = furi_hal_adc_read(adc, p->channel);
        uint16_t second = furi_hal_adc_read(adc, p->channel);
        FURI_LOG_I(
            TAG,
            "%s: pin %u %s %u and %u = %d mV",
            at,
            p->number,
            p->name,
            first,
            second,
            (int)furi_hal_adc_convert_to_voltage(adc, second));
    }
}

int32_t fe_adc_check_app(void* p) {
    UNUSED(p);
    FuriHalAdcHandle* adc = furi_hal_adc_acquire();

    furi_hal_adc_configure(adc);
    measure(adc, "at 2.048 V");
    furi_hal_adc_configure_ex(
        adc,
        FuriHalAdcScale2500,
        FuriHalAdcClockSync64,
        FuriHalAdcOversampleNone,
        FuriHalAdcSamplingtime247_5);
    measure(adc, "at 2.5 V");

    const GpioPinRecord* own = furi_hal_resources_pin_by_number(16);
    furi_check(own && own->channel != FuriHalAdcChannelNone);
    furi_hal_gpio_write(own->pin, true);
    furi_hal_gpio_init(own->pin, GpioModeOutputPushPull, GpioPullNo, GpioSpeedLow);
    uint16_t high = furi_hal_adc_read(adc, own->channel);
    furi_hal_gpio_write(own->pin, false);
    uint16_t low = furi_hal_adc_read(adc, own->channel);
    furi_hal_gpio_init(own->pin, GpioModeInput, GpioPullUp, GpioSpeedLow);
    uint16_t up = furi_hal_adc_read(adc, own->channel);
    furi_hal_gpio_init(own->pin, GpioModeInput, GpioPullDown, GpioSpeedLow);
    uint16_t down = furi_hal_adc_read(adc, own->channel);
    furi_hal_gpio_init(own->pin, GpioModeAnalog, GpioPullNo, GpioSpeedLow);
    FURI_LOG_I(
        TAG,
        "pin %u %s as an output: %u high, %u low; as an input: %u with a pull-up, "
        "%u with a pull-down",
        own->number,
        own->name,
        high,
        low,
        up,
        down);

    furi_hal_adc_release(adc);
    FURI_LOG_I(TAG, "done");
    return 0;
}
