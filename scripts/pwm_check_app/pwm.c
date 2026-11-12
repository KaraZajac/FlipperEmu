// Copyright (c) 2026, Kara Zajac
// SPDX-License-Identifier: BSD-3-Clause
//
// What scripts/gpio_check.py has a firmware do with the header's two PWM
// outputs (TIM1 on pin 2, PA7; LPTIM2 on pin 4, PA4) that the firmware's
// own calls for them, which a script's pwmWrite() uses, do not: the other
// polarity of each output, the timer's other PWM mode, a prescaler with a
// period of the app's own, a pin that is open drain, a timer that runs with
// no pin, and a pin with no timer.  It is the check's own and no example.
//
// It does one step, says so in the log, and waits until what is held to PA6
// (pin 3) changes: high for the second step, low for the third, and so on.
//
//     ./flipperemu run
//     cd scripts/pwm_check_app && ../../flipperemu ufbt launch
//     ./flipperemu gpio            ("PWM from TIM1: 1 kHz, high 20 % ...")
//     ./flipperemu gpio PA6 high   (the next step)
#include <furi.h>
#include <furi_hal.h>
#include <furi_hal_bus.h>
#include <furi_hal_pwm.h>
#include <furi_hal_resources.h>

#include <stm32wbxx_ll_lptim.h>
#include <stm32wbxx_ll_rcc.h>
#include <stm32wbxx_ll_tim.h>

#define TAG "FePwm"

static bool held = false;

// Say what was done, and wait until what is held to PA6 is the other level
static void step(const char* done) {
    FURI_LOG_I(TAG, "%s", done);
    held = !held;
    while(furi_hal_gpio_read(&gpio_ext_pa6) != held) furi_delay_ms(20);
}

int32_t fe_pwm_check_app(void* p) {
    UNUSED(p);
    furi_hal_gpio_init(&gpio_ext_pa6, GpioModeInput, GpioPullDown, GpioSpeedLow);

    furi_hal_pwm_start(FuriHalPwmOutputIdTim1PA7, 1000, 20);
    furi_hal_pwm_start(FuriHalPwmOutputIdLptim2PA4, 10000, 30);
    FURI_LOG_I(
        TAG,
        "running: %d %d",
        furi_hal_pwm_is_running(FuriHalPwmOutputIdTim1PA7),
        furi_hal_pwm_is_running(FuriHalPwmOutputIdLptim2PA4));
    step("1: both as the firmware starts them");

    // TIM1's output with the other polarity: its low part is the high one.
    // LPTIM2 anew, not as the firmware has it: its clock over 4, 1000 ticks
    // for a period, and the polarity that the chip calls regular (high from
    // the compare value to the period's end)
    LL_TIM_OC_SetPolarity(TIM1, LL_TIM_CHANNEL_CH1N, LL_TIM_OCPOLARITY_LOW);
    furi_hal_pwm_stop(FuriHalPwmOutputIdLptim2PA4);
    furi_hal_gpio_init_ex(
        &gpio_ext_pa4,
        GpioModeAltFunctionPushPull,
        GpioPullNo,
        GpioSpeedVeryHigh,
        GpioAltFn14LPTIM2);
    furi_hal_bus_enable(FuriHalBusLPTIM2);
    LL_LPTIM_SetUpdateMode(LPTIM2, LL_LPTIM_UPDATE_MODE_ENDOFPERIOD);
    LL_RCC_SetLPTIMClockSource(LL_RCC_LPTIM2_CLKSOURCE_PCLK1);
    LL_LPTIM_SetClockSource(LPTIM2, LL_LPTIM_CLK_SOURCE_INTERNAL);
    LL_LPTIM_ConfigOutput(LPTIM2, LL_LPTIM_OUTPUT_WAVEFORM_PWM, LL_LPTIM_OUTPUT_POLARITY_REGULAR);
    LL_LPTIM_SetCounterMode(LPTIM2, LL_LPTIM_COUNTER_MODE_INTERNAL);
    LL_LPTIM_SetPrescaler(LPTIM2, LL_LPTIM_PRESCALER_DIV4);
    LL_LPTIM_Enable(LPTIM2);
    LL_LPTIM_SetAutoReload(LPTIM2, 999);
    LL_LPTIM_SetCompare(LPTIM2, 250);
    LL_LPTIM_StartCounter(LPTIM2, LL_LPTIM_OPERATING_MODE_CONTINUOUS);
    step("2: the other polarity for both, LPTIM2 with a prescaler");

    // TIM1's other PWM mode, which turns its output around once more.  PA4
    // is taken from LPTIM2, which runs on
    LL_TIM_OC_SetMode(TIM1, LL_TIM_CHANNEL_CH1, LL_TIM_OCMODE_PWM2);
    furi_hal_gpio_init_simple(&gpio_ext_pa4, GpioModeAnalog);
    step("3: TIM1 in PWM mode 2, PA4 analog");

    // PA7 as an open-drain output of the timer, with a pull-up: what holds
    // it low from outside wins.  PA4 is LPTIM2's again
    furi_hal_gpio_init_ex(
        &gpio_ext_pa7,
        GpioModeAltFunctionOpenDrain,
        GpioPullUp,
        GpioSpeedVeryHigh,
        GpioAltFn1TIM1);
    furi_hal_gpio_init_ex(
        &gpio_ext_pa4,
        GpioModeAltFunctionPushPull,
        GpioPullNo,
        GpioSpeedVeryHigh,
        GpioAltFn14LPTIM2);
    step("4: PA7 open drain, PA4 is LPTIM2's again");

    // both stopped; PA7 is still the pin of a timer that makes nothing now
    furi_hal_pwm_stop(FuriHalPwmOutputIdTim1PA7);
    furi_hal_pwm_stop(FuriHalPwmOutputIdLptim2PA4);
    furi_hal_gpio_init_ex(
        &gpio_ext_pa7,
        GpioModeAltFunctionPushPull,
        GpioPullNo,
        GpioSpeedLow,
        GpioAltFn1TIM1);
    step("5: both stopped, PA7 still TIM1's pin");

    furi_hal_gpio_init_simple(&gpio_ext_pa7, GpioModeAnalog);
    furi_hal_gpio_init_simple(&gpio_ext_pa6, GpioModeAnalog);
    FURI_LOG_I(
        TAG,
        "done: %d %d",
        furi_hal_pwm_is_running(FuriHalPwmOutputIdTim1PA7),
        furi_hal_pwm_is_running(FuriHalPwmOutputIdLptim2PA4));
    return 0;
}
