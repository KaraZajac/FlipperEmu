/*
 * STM32WB55RG SoC (CPU1 side) for the Flipper Zero "f7" target
 *
 * Memory map and IRQ numbers: RM0434 chapter 2/14 and stm32wb55xx.h.
 * GPIO, USART and LPUART reuse QEMU's STM32L4x5 models (same IP); everything
 * that differs on the WB55 (RCC, PWR, EXTI, SYSCFG, FLASH, HSEM, IPCC, RTC,
 * LPTIM, SPI, I2C, DMA/DMAMUX, RNG, DWT, USB) is modelled in this directory.
 *
 * Copyright (c) 2026, Kara Zajac
 * SPDX-License-Identifier: BSD-3-Clause
 */
#include "qemu/osdep.h"
#include "qemu/units.h"
#include "qapi/error.h"
#include "hw/core/irq.h"
#include "hw/core/qdev-clock.h"
#include "hw/core/qdev-properties.h"
#include "hw/misc/unimp.h"
#include "system/address-spaces.h"
#include "system/system.h"
#include "target/arm/cpu-qom.h"
#include "wb55.h"

static const hwaddr gpio_addr[WB55_NUM_PORTS] = {
    0x48000000, 0x48000400, 0x48000800, 0x48000C00, 0x48001000, 0x48001C00,
};
static const uint8_t gpio_code[WB55_NUM_PORTS] = { 0, 1, 2, 3, 4, 7 };
/* RM0434 GPIO reset values (MODER, OSPEEDR, PUPDR): SWD on PA13/PA14,
 * JTDI/JTDO/NJTRST on PA15/PB3/PB4 */
static const uint32_t gpio_reset[WB55_NUM_PORTS][3] = {
    { 0xABFFFFFF, 0x0C000000, 0x64000000 },
    { 0xFFFFFEBF, 0x00000000, 0x00000100 },
    { 0xFFFFFFFF, 0x00000000, 0x00000000 },
    { 0xFFFFFFFF, 0x00000000, 0x00000000 },
    { 0x000003FF, 0x00000000, 0x00000000 },
    { 0x000000CF, 0x00000000, 0x00000000 },
};

static void soc_gpio_tap(void *opaque, int n, int level)
{
    WB55SocState *s = opaque;
    int port = n / 16, pin = n % 16;

    s->pin_level[n] = !!level;
    qemu_set_irq(qdev_get_gpio_in(DEVICE(&s->syscfg),
                                  gpio_code[port] * 16 + pin), level);
    qemu_set_irq(s->gpio_out[n], level);
}

/*
 * Each GPIO port's registers are overlaid by a region that passes every
 * access through to QEMU's GPIO model and then tells the board: a read of
 * IDR (the CPU has sampled the pins; the value is taken before the hook
 * runs), or a write that can change what the CPU drives.  A bit-banged bus
 * (a 1-Wire key on the iButton pin) follows the CPU's own drive this way,
 * edge for edge, even while the key itself holds the line low.
 */
static MemTxResult soc_gpio_tap_read(void *opaque, hwaddr addr, uint64_t *data,
                                     unsigned size, MemTxAttrs attrs)
{
    struct WB55GpioTap *tap = opaque;
    WB55SocState *s = tap->soc;
    MemoryRegion *mr = sysbus_mmio_get_region(SYS_BUS_DEVICE(&s->gpio[tap->port]), 0);
    MemTxResult r = memory_region_dispatch_read(mr, addr, data, size_memop(size),
                                                attrs);

    if (addr == 0x10 && s->idr_read) {
        s->idr_read(s->gpio_hook_opaque, tap->port);
    }
    return r;
}

static MemTxResult soc_gpio_tap_write(void *opaque, hwaddr addr, uint64_t data,
                                      unsigned size, MemTxAttrs attrs)
{
    struct WB55GpioTap *tap = opaque;
    WB55SocState *s = tap->soc;
    MemoryRegion *mr = sysbus_mmio_get_region(SYS_BUS_DEVICE(&s->gpio[tap->port]), 0);
    MemTxResult r = memory_region_dispatch_write(mr, addr, data, size_memop(size),
                                                 attrs);

    if ((addr == 0x00 || addr == 0x04 || addr == 0x0c || addr == 0x14 ||
         addr == 0x18 || addr == 0x28) && s->out_written) {
        s->out_written(s->gpio_hook_opaque, tap->port, addr, data);
    }
    return r;
}

static const MemoryRegionOps soc_gpio_tap_ops = {
    .read_with_attrs = soc_gpio_tap_read,
    .write_with_attrs = soc_gpio_tap_write,
    .endianness = DEVICE_LITTLE_ENDIAN,
    .impl.min_access_size = 1, .impl.max_access_size = 4,
    .valid.min_access_size = 1, .valid.max_access_size = 4,
};

static void soc_gpio_in(void *opaque, int n, int level)
{
    WB55SocState *s = opaque;

    qemu_set_irq(qdev_get_gpio_in(DEVICE(&s->gpio[n / 16]), n % 16), level);
}

static void soc_boot_remap(void *opaque, int mode)
{
    WB55SocState *s = opaque;

    /* MEM_MODE: 000 main flash, 001 system flash, 011 SRAM1 */
    memory_region_set_enabled(&s->boot_flash, mode != 1 && mode != 3);
    memory_region_set_enabled(&s->boot_sysmem, mode == 1);
    memory_region_set_enabled(&s->boot_sram1, mode == 3);
}

/*
 * System reset latches the boot configuration (BOOT0 low, nBOOT1/nSWBOOT0
 * from the option bytes: main flash) before the core fetches SP and PC.
 * The RAM updater runs with SRAM1 remapped at 0 and ends with a reset that
 * must boot the freshly written flash, so this cannot wait for SYSCFG's own
 * reset, which runs after the CPU has read its vector table.
 */
void wb55_soc_latch_boot(WB55SocState *s)
{
    soc_boot_remap(s, 0);
}

/*
 * QEMU's STM32L4x5 GPIO reset zeroes IDR before recomputing it, so a pin
 * that was high and is now low or floating never tells the devices on it.
 * The ST7567's A0 (PB1) is one: after a warm reset the firmware's first
 * commands (soft reset, display on) would land in display RAM as pixels.
 * After a reset, re-send every pin whose level the devices have not seen.
 */
void wb55_soc_sync_pins(WB55SocState *s)
{
    for (int n = 0; n < WB55_PINS; n++) {
        int level = (s->gpio[n / 16].idr >> (n % 16)) & 1;

        if (level != s->pin_level[n]) {
            soc_gpio_tap(s, n, level);
        }
    }
}

/* TIM1's update DMA: is it re-armed for the next update? */
static bool soc_tim1_dma_ready(void *opaque)
{
    WB55SocState *s = opaque;

    return wb55_dma_ready_for(&s->dma, WB55_DREQ_TIM1_UP);
}

/*
 * A comparator output changed.  TIM2_OR.TI4_RMP (bits 3:2) picks TI4's
 * source: 00 the TIM2_CH4 pin, 01 COMP1, 10 COMP2, 11 COMP1 OR COMP2.
 */
static void soc_comp_out(void *opaque, int n, bool level, int64_t when)
{
    WB55SocState *s = opaque;
    int rmp = (s->tim2.regs[0x50 / 4] >> 2) & 3;

    if (rmp && (rmp & (1 << n))) {
        wb55_tim_input(&s->tim2, 3, ((rmp & 1) && s->comp.out[0]) ||
                                    ((rmp & 2) && s->comp.out[1]), when);
    }
}

static DeviceState *soc_regfile(const char *name, hwaddr addr, uint32_t size,
                                uint32_t kind)
{
    DeviceState *dev = qdev_new(TYPE_WB55_REGFILE);

    qdev_prop_set_string(dev, "name", name);
    qdev_prop_set_uint32(dev, "size", size);
    qdev_prop_set_uint32(dev, "kind", kind);
    sysbus_realize_and_unref(SYS_BUS_DEVICE(dev), &error_fatal);
    sysbus_mmio_map(SYS_BUS_DEVICE(dev), 0, addr);
    return dev;
}

static void soc_map(SysBusDevice *sbd, hwaddr addr, Error **errp)
{
    if (sysbus_realize(sbd, errp)) {
        sysbus_mmio_map(sbd, 0, addr);
    }
}

static void wb55_soc_init(Object *obj)
{
    WB55SocState *s = WB55_SOC(obj);

    object_initialize_child(obj, "armv7m", &s->armv7m, TYPE_ARMV7M);
    object_initialize_child(obj, "rcc", &s->rcc, TYPE_WB55_RCC);
    object_initialize_child(obj, "pwr", &s->pwr, TYPE_WB55_PWR);
    object_initialize_child(obj, "flash", &s->flash, TYPE_WB55_FLASH);
    object_initialize_child(obj, "hsem", &s->hsem, TYPE_WB55_HSEM);
    object_initialize_child(obj, "ipcc", &s->ipcc, TYPE_WB55_IPCC);
    object_initialize_child(obj, "aes1", &s->aes1, TYPE_WB55_AES);
    object_initialize_child(obj, "aes2", &s->aes2, TYPE_WB55_AES);
    object_initialize_child(obj, "exti", &s->exti, TYPE_WB55_EXTI);
    object_initialize_child(obj, "syscfg", &s->syscfg, TYPE_WB55_SYSCFG);
    object_initialize_child(obj, "rtc", &s->rtc, TYPE_WB55_RTC);
    object_initialize_child(obj, "lptim1", &s->lptim[0], TYPE_WB55_LPTIM);
    object_initialize_child(obj, "lptim2", &s->lptim[1], TYPE_WB55_LPTIM);
    object_initialize_child(obj, "spi1", &s->spi[0], TYPE_WB55_SPI);
    object_initialize_child(obj, "spi2", &s->spi[1], TYPE_WB55_SPI);
    object_initialize_child(obj, "i2c1", &s->i2c[0], TYPE_WB55_I2C);
    object_initialize_child(obj, "i2c3", &s->i2c[1], TYPE_WB55_I2C);
    object_initialize_child(obj, "dma", &s->dma, TYPE_WB55_DMA);
    object_initialize_child(obj, "rng", &s->rng, TYPE_WB55_RNG);
    object_initialize_child(obj, "dwt", &s->dwt, TYPE_WB55_DWT);
    object_initialize_child(obj, "dbgmcu", &s->dbgmcu, TYPE_WB55_DBGMCU);
    object_initialize_child(obj, "usb", &s->usb, TYPE_WB55_USB);
    object_initialize_child(obj, "tim1", &s->tim1, TYPE_WB55_TIM);
    object_initialize_child(obj, "tim2", &s->tim2, TYPE_WB55_TIM);
    object_initialize_child(obj, "tim17", &s->tim17, TYPE_WB55_TIM);
    object_initialize_child(obj, "tim17-or", &s->tim17_or, TYPE_OR_IRQ);
    object_initialize_child(obj, "comp", &s->comp, TYPE_WB55_COMP);
    for (int i = 0; i < WB55_NUM_PORTS; i++) {
        g_autofree char *name = g_strdup_printf("gpio%c", "abcdeh"[i]);
        object_initialize_child(obj, name, &s->gpio[i], TYPE_STM32L4X5_GPIO);
    }
    object_initialize_child(obj, "usart1", &s->usart1, TYPE_STM32L4X5_USART);
    object_initialize_child(obj, "lpuart1", &s->lpuart1, TYPE_STM32L4X5_LPUART);
    for (int i = 0; i < 3; i++) {
        object_initialize_child(obj, "exti-or[*]", &s->exti_or[i], TYPE_OR_IRQ);
    }

    /* board-facing knobs */
    object_property_add_alias(obj, "flash-drive", OBJECT(&s->flash), "drive");
    object_property_add_alias(obj, "uid64", OBJECT(&s->ipcc), "uid64");
    object_property_add_alias(obj, "fus-keys", OBJECT(&s->ipcc), "keys");
    object_property_add_alias(obj, "rtc-bkp1", OBJECT(&s->rtc), "bkp1");

    qdev_init_gpio_out_named(DEVICE(obj), s->gpio_out, "gpio-out", WB55_PINS);
    qdev_init_gpio_in_named(DEVICE(obj), soc_gpio_in, "gpio-in", WB55_PINS);
}

static void wb55_soc_realize(DeviceState *dev_soc, Error **errp)
{
    WB55SocState *s = WB55_SOC(dev_soc);
    MemoryRegion *sysmem = get_system_memory();
    DeviceState *armv7m = DEVICE(&s->armv7m);
    DeviceState *dev;
    SysBusDevice *sbd;

#define NVIC(n) qdev_get_gpio_in(armv7m, (n))

    /* ---- clocks and reset controller first: everything hangs off them */
    if (!sysbus_realize(SYS_BUS_DEVICE(&s->rcc), errp)) {
        return;
    }
    sysbus_mmio_map(SYS_BUS_DEVICE(&s->rcc), 0, 0x58000000);
    Clock *hclk = qdev_get_clock_out(DEVICE(&s->rcc), "hclk");
    Clock *refclk = qdev_get_clock_out(DEVICE(&s->rcc), "refclk");

    /* ---- flash: controller, main array, info block */
    s->flash.rcc = &s->rcc;
    if (!sysbus_realize(SYS_BUS_DEVICE(&s->flash), errp)) {
        return;
    }
    sysbus_mmio_map(SYS_BUS_DEVICE(&s->flash), 0, 0x58004000);
    sysbus_connect_irq(SYS_BUS_DEVICE(&s->flash), 0, NULL);
    memory_region_add_subregion(sysmem, WB55_FLASH_BASE, &s->flash.flash);
    memory_region_add_subregion(sysmem, WB55_INFO_BASE, &s->flash.info);

    /* ---- SRAM */
    if (!memory_region_init_ram(&s->sram1, OBJECT(dev_soc), "wb55.sram1",
                                WB55_SRAM1_SIZE, errp) ||
        !memory_region_init_ram(&s->sram2a, OBJECT(dev_soc), "wb55.sram2a",
                                WB55_SRAM2A_SIZE, errp) ||
        !memory_region_init_ram(&s->sram2b, OBJECT(dev_soc), "wb55.sram2b",
                                WB55_SRAM2B_SIZE, errp)) {
        return;
    }
    memory_region_add_subregion(sysmem, WB55_SRAM1_BASE, &s->sram1);
    memory_region_add_subregion(sysmem, WB55_SRAM2A_BASE, &s->sram2a);
    memory_region_add_subregion(sysmem, WB55_SRAM2B_BASE, &s->sram2b);
    memory_region_init_alias(&s->sram2a_alias, OBJECT(dev_soc), "sram2a-alias",
                             &s->sram2a, 0, WB55_SRAM2A_SIZE);
    memory_region_init_alias(&s->sram2b_alias, OBJECT(dev_soc), "sram2b-alias",
                             &s->sram2b, 0, WB55_SRAM2B_SIZE);
    memory_region_add_subregion(sysmem, 0x10000000, &s->sram2a_alias);
    memory_region_add_subregion(sysmem, 0x10008000, &s->sram2b_alias);

    /* ---- boot aliases at 0x00000000, selected by SYSCFG_MEMRMP */
    memory_region_init_alias(&s->boot_flash, OBJECT(dev_soc), "boot-flash",
                             &s->flash.flash, 0, WB55_FLASH_SIZE);
    memory_region_init_alias(&s->boot_sysmem, OBJECT(dev_soc), "boot-sysmem",
                             &s->flash.info, 0, WB55_INFO_SIZE);
    memory_region_init_alias(&s->boot_sram1, OBJECT(dev_soc), "boot-sram1",
                             &s->sram1, 0, WB55_SRAM1_SIZE);
    memory_region_add_subregion_overlap(sysmem, 0, &s->boot_flash, 1);
    memory_region_add_subregion_overlap(sysmem, 0, &s->boot_sysmem, 1);
    memory_region_add_subregion_overlap(sysmem, 0, &s->boot_sram1, 1);
    soc_boot_remap(s, 0);

    /* ---- CPU1: Cortex-M4F, 63 external interrupts, 4 priority bits */
    qdev_prop_set_uint32(armv7m, "num-irq", WB55_NUM_IRQ);
    qdev_prop_set_uint32(armv7m, "num-prio-bits", 4);
    qdev_prop_set_string(armv7m, "cpu-type", ARM_CPU_TYPE_NAME("cortex-m4"));
    qdev_connect_clock_in(armv7m, "cpuclk", hclk);
    qdev_connect_clock_in(armv7m, "refclk", refclk);
    object_property_set_link(OBJECT(&s->armv7m), "memory", OBJECT(sysmem),
                             &error_abort);
    if (!sysbus_realize(SYS_BUS_DEVICE(&s->armv7m), errp)) {
        return;
    }
    qdev_connect_gpio_out_named(armv7m, "SYSRESETREQ", 0,
                                qdev_get_gpio_in_named(DEVICE(&s->rcc),
                                                       "sysresetreq", 0));
    sysbus_connect_irq(SYS_BUS_DEVICE(&s->rcc), 0, NVIC(WB55_IRQ_RCC));
    sysbus_connect_irq(SYS_BUS_DEVICE(&s->flash), 0, NVIC(WB55_IRQ_FLASH));

    /* DWT and DBGMCU live in the PPB; overlay QEMU's RAZ/WI default there */
    qdev_connect_clock_in(DEVICE(&s->dwt), "clk", hclk);
    if (!sysbus_realize(SYS_BUS_DEVICE(&s->dwt), errp) ||
        !sysbus_realize(SYS_BUS_DEVICE(&s->dbgmcu), errp)) {
        return;
    }
    memory_region_add_subregion_overlap(&s->armv7m.container, 0xE0001000,
        sysbus_mmio_get_region(SYS_BUS_DEVICE(&s->dwt), 0), 1);
    memory_region_add_subregion_overlap(&s->armv7m.container, 0xE0042000,
        sysbus_mmio_get_region(SYS_BUS_DEVICE(&s->dbgmcu), 0), 1);

    /* ---- EXTI with OR gates for the shared vectors */
    soc_map(SYS_BUS_DEVICE(&s->exti), 0x58000800, errp);
    static const int or_lines[3] = { 5, 6, 2 };
    static const int or_irq[3] = { WB55_IRQ_EXTI9_5, WB55_IRQ_EXTI15_10,
                                   WB55_IRQ_COMP };
    for (int i = 0; i < 3; i++) {
        object_property_set_int(OBJECT(&s->exti_or[i]), "num-lines",
                                or_lines[i], &error_abort);
        if (!qdev_realize(DEVICE(&s->exti_or[i]), NULL, errp)) {
            return;
        }
        qdev_connect_gpio_out(DEVICE(&s->exti_or[i]), 0, NVIC(or_irq[i]));
    }
    dev = DEVICE(&s->exti);
    for (int l = 0; l <= 4; l++) {
        qdev_connect_gpio_out(dev, l, NVIC(WB55_IRQ_EXTI0 + l));
    }
    for (int l = 5; l <= 9; l++) {
        qdev_connect_gpio_out(dev, l, qdev_get_gpio_in(DEVICE(&s->exti_or[0]),
                                                       l - 5));
    }
    for (int l = 10; l <= 15; l++) {
        qdev_connect_gpio_out(dev, l, qdev_get_gpio_in(DEVICE(&s->exti_or[1]),
                                                       l - 10));
    }
    qdev_connect_gpio_out(dev, 16, NVIC(WB55_IRQ_PVD_PVM));
    qdev_connect_gpio_out(dev, 17, NVIC(WB55_IRQ_RTC_ALARM));
    qdev_connect_gpio_out(dev, 18, NVIC(WB55_IRQ_TAMP_STAMP_LSECSS));
    qdev_connect_gpio_out(dev, 19, NVIC(WB55_IRQ_RTC_WKUP));
    qdev_connect_gpio_out(dev, 20, qdev_get_gpio_in(DEVICE(&s->exti_or[2]), 0));
    qdev_connect_gpio_out(dev, 21, qdev_get_gpio_in(DEVICE(&s->exti_or[2]), 1));

    /* ---- SYSCFG (+VREFBUF) and GPIO ports */
    s->syscfg.remap = soc_boot_remap;
    s->syscfg.remap_opaque = s;
    soc_map(SYS_BUS_DEVICE(&s->syscfg), 0x40010000, errp);
    for (int l = 0; l < 16; l++) {
        qdev_connect_gpio_out(DEVICE(&s->syscfg), l,
                              qdev_get_gpio_in(DEVICE(&s->exti), l));
    }
    s->gpio_tap = qemu_allocate_irqs(soc_gpio_tap, s, WB55_PINS);
    for (int i = 0; i < WB55_NUM_PORTS; i++) {
        dev = DEVICE(&s->gpio[i]);
        g_autofree char *name = g_strdup_printf("%c", "ABCDEH"[i]);
        qdev_prop_set_string(dev, "name", name);
        qdev_prop_set_uint32(dev, "mode-reset", gpio_reset[i][0]);
        qdev_prop_set_uint32(dev, "ospeed-reset", gpio_reset[i][1]);
        qdev_prop_set_uint32(dev, "pupd-reset", gpio_reset[i][2]);
        qdev_connect_clock_in(dev, "clk", hclk);
        soc_map(SYS_BUS_DEVICE(dev), gpio_addr[i], errp);
        for (int p = 0; p < 16; p++) {
            qdev_connect_gpio_out(dev, p, s->gpio_tap[i * 16 + p]);
        }
        s->gpio_tap_mr[i].soc = s;
        s->gpio_tap_mr[i].port = i;
        memory_region_init_io(&s->gpio_tap_mr[i].mr, OBJECT(s), &soc_gpio_tap_ops,
                              &s->gpio_tap_mr[i], "gpio-tap", 0x400);
        memory_region_add_subregion_overlap(get_system_memory(), gpio_addr[i],
                                            &s->gpio_tap_mr[i].mr, 1);
    }

    /* ---- power, semaphores, IPCC + behavioural CPU2 */
    soc_map(SYS_BUS_DEVICE(&s->pwr), 0x58000400, errp);
    soc_map(SYS_BUS_DEVICE(&s->hsem), 0x58001400, errp);
    sysbus_connect_irq(SYS_BUS_DEVICE(&s->hsem), 0, NVIC(WB55_IRQ_HSEM));
    s->ipcc.flash = &s->flash;
    s->ipcc.rcc = &s->rcc;
    s->ipcc.aes = &s->aes1;             /* where the FUS loads a key */
    soc_map(SYS_BUS_DEVICE(&s->ipcc), 0x58000C00, errp);
    sysbus_connect_irq(SYS_BUS_DEVICE(&s->ipcc), 0, NVIC(WB55_IRQ_IPCC_C1_RX));
    sysbus_connect_irq(SYS_BUS_DEVICE(&s->ipcc), 1, NVIC(WB55_IRQ_IPCC_C1_TX));
    qdev_connect_gpio_out_named(DEVICE(&s->pwr), "c2boot", 0,
        qdev_get_gpio_in_named(DEVICE(&s->ipcc), "c2boot", 0));

    /* ---- RTC (backup domain) -> EXTI 17/19/18 */
    soc_map(SYS_BUS_DEVICE(&s->rtc), 0x40002800, errp);
    sbd = SYS_BUS_DEVICE(&s->rtc);
    sysbus_connect_irq(sbd, 0, qdev_get_gpio_in(DEVICE(&s->exti), 17));
    sysbus_connect_irq(sbd, 1, qdev_get_gpio_in(DEVICE(&s->exti), 19));
    sysbus_connect_irq(sbd, 2, qdev_get_gpio_in(DEVICE(&s->exti), 18));
    s->rcc.rtc = DEVICE(&s->rtc);

    /* ---- timers */
    soc_map(SYS_BUS_DEVICE(&s->lptim[0]), 0x40007C00, errp);
    soc_map(SYS_BUS_DEVICE(&s->lptim[1]), 0x40009400, errp);
    sysbus_connect_irq(SYS_BUS_DEVICE(&s->lptim[0]), 0, NVIC(WB55_IRQ_LPTIM1));
    sysbus_connect_irq(SYS_BUS_DEVICE(&s->lptim[1]), 0, NVIC(WB55_IRQ_LPTIM2));
    wb55_rcc_add_reset_hook(&s->rcc, WB55_RCC_APB1RSTR1, 31,
                            DEVICE(&s->lptim[0]));
    wb55_rcc_add_reset_hook(&s->rcc, WB55_RCC_APB1RSTR2, 5,
                            DEVICE(&s->lptim[1]));

    /* ---- DMA1/DMA2/DMAMUX1 */
    if (!sysbus_realize(SYS_BUS_DEVICE(&s->dma), errp)) {
        return;
    }
    sbd = SYS_BUS_DEVICE(&s->dma);
    sysbus_mmio_map(sbd, 0, 0x40020000);
    sysbus_mmio_map(sbd, 1, 0x40020400);
    sysbus_mmio_map(sbd, 2, 0x40020800);
    for (int c = 0; c < 7; c++) {
        sysbus_connect_irq(sbd, c, NVIC(WB55_IRQ_DMA1_CH1 + c));
        sysbus_connect_irq(sbd, 7 + c, NVIC(WB55_IRQ_DMA2_CH1 + c));
    }
#define DREQ(n) qdev_get_gpio_in_named(DEVICE(&s->dma), "request", (n))

    /* ---- SPI1 (CC1101, ST25R3916), SPI2 (display, microSD) */
    static const hwaddr spi_addr[2] = { 0x40013000, 0x40003800 };
    static const int spi_irq[2] = { WB55_IRQ_SPI1, WB55_IRQ_SPI2 };
    static const int spi_rx[2] = { WB55_DREQ_SPI1_RX, WB55_DREQ_SPI2_RX };
    for (int i = 0; i < 2; i++) {
        dev = DEVICE(&s->spi[i]);
        soc_map(SYS_BUS_DEVICE(dev), spi_addr[i], errp);
        sysbus_connect_irq(SYS_BUS_DEVICE(dev), 0, NVIC(spi_irq[i]));
        qdev_connect_gpio_out_named(dev, "dma-rx", 0, DREQ(spi_rx[i]));
        qdev_connect_gpio_out_named(dev, "dma-tx", 0, DREQ(spi_rx[i] + 1));
    }
    wb55_rcc_add_reset_hook(&s->rcc, WB55_RCC_APB2RSTR, 12, DEVICE(&s->spi[0]));
    wb55_rcc_add_reset_hook(&s->rcc, WB55_RCC_APB1RSTR1, 14, DEVICE(&s->spi[1]));

    /* ---- I2C1 (gauge, charger, LED driver), I2C3 (external header) */
    static const hwaddr i2c_addr[2] = { 0x40005400, 0x40005C00 };
    static const int i2c_irq[2] = { WB55_IRQ_I2C1_EV, WB55_IRQ_I2C3_EV };
    for (int i = 0; i < 2; i++) {
        sbd = SYS_BUS_DEVICE(&s->i2c[i]);
        soc_map(sbd, i2c_addr[i], errp);
        sysbus_connect_irq(sbd, 0, NVIC(i2c_irq[i]));
        sysbus_connect_irq(sbd, 1, NVIC(i2c_irq[i] + 1));
    }
    wb55_rcc_add_reset_hook(&s->rcc, WB55_RCC_APB1RSTR1, 21, DEVICE(&s->i2c[0]));
    wb55_rcc_add_reset_hook(&s->rcc, WB55_RCC_APB1RSTR1, 23, DEVICE(&s->i2c[1]));

    /* ---- USART1 (log console, header pins 13/14) and LPUART1 */
    dev = DEVICE(&s->usart1);
    qdev_prop_set_chr(dev, "chardev", serial_hd(0));
    qdev_connect_clock_in(dev, "clk", hclk);
    soc_map(SYS_BUS_DEVICE(dev), 0x40013800, errp);
    sysbus_connect_irq(SYS_BUS_DEVICE(dev), 0, NVIC(WB55_IRQ_USART1));
    wb55_rcc_add_reset_hook(&s->rcc, WB55_RCC_APB2RSTR, 14, dev);
    dev = DEVICE(&s->lpuart1);
    qdev_prop_set_chr(dev, "chardev", serial_hd(1));
    qdev_connect_clock_in(dev, "clk", hclk);
    soc_map(SYS_BUS_DEVICE(dev), 0x40008000, errp);
    sysbus_connect_irq(SYS_BUS_DEVICE(dev), 0, NVIC(WB55_IRQ_LPUART1));
    wb55_rcc_add_reset_hook(&s->rcc, WB55_RCC_APB1RSTR2, 0, dev);

    /* ---- USB FS device (+ built-in host): registers, packet memory */
    sbd = SYS_BUS_DEVICE(&s->usb);
    if (!sysbus_realize(sbd, errp)) {
        return;
    }
    sysbus_mmio_map(sbd, 0, 0x40006800);
    sysbus_mmio_map(sbd, 1, 0x40006C00);
    sysbus_connect_irq(sbd, 0, NVIC(WB55_IRQ_USB_LP));
    sysbus_connect_irq(sbd, 1, NVIC(WB55_IRQ_USB_HP));
    wb55_rcc_add_reset_hook(&s->rcc, WB55_RCC_APB1RSTR1, 26, DEVICE(&s->usb));

    /* ---- RNG */
    soc_map(SYS_BUS_DEVICE(&s->rng), 0x58001000, errp);
    sysbus_connect_irq(SYS_BUS_DEVICE(&s->rng), 0, NVIC(WB55_IRQ_RNG));

    /* ---- read-back register files (no behaviour the firmware needs yet) */
    /* TIM1: advanced timer (RFID carrier, IR transmit, RFID field timeout) */
    dev = DEVICE(&s->tim1);
    qdev_prop_set_uint32(dev, "rcr-bits", 16);
    qdev_connect_clock_in(dev, "clk", hclk);
    soc_map(SYS_BUS_DEVICE(dev), 0x40012C00, errp);
    sysbus_connect_irq(SYS_BUS_DEVICE(dev), 1, NVIC(WB55_IRQ_TIM1_UP_TIM16));
    sysbus_connect_irq(SYS_BUS_DEVICE(dev), 2, NVIC(WB55_IRQ_TIM1_CC));
    /* TIM1 trigger/commutation and all of TIM17 share one vector */
    object_property_set_int(OBJECT(&s->tim17_or), "num-lines", 2, &error_abort);
    if (!qdev_realize(DEVICE(&s->tim17_or), NULL, errp)) {
        return;
    }
    qdev_connect_gpio_out(DEVICE(&s->tim17_or), 0, NVIC(WB55_IRQ_TIM1_TRG_COM_TIM17));
    sysbus_connect_irq(SYS_BUS_DEVICE(dev), 3,
                       qdev_get_gpio_in(DEVICE(&s->tim17_or), 0));
    sysbus_connect_irq(SYS_BUS_DEVICE(dev), 4, NVIC(WB55_IRQ_TIM1_BRK));
    qdev_connect_gpio_out_named(dev, "dma-up", 0, DREQ(WB55_DREQ_TIM1_UP));
    wb55_dma_set_pulse(&s->dma, WB55_DREQ_TIM1_UP);
    s->tim1.dma_ready = soc_tim1_dma_ready;
    s->tim1.dma_ready_opaque = s;
    wb55_rcc_add_reset_hook(&s->rcc, WB55_RCC_APB2RSTR, 11, dev);
    /* TIM2: 32-bit general-purpose timer (IR receive, Sub-GHz capture, RFID) */
    dev = DEVICE(&s->tim2);
    qdev_prop_set_uint32(dev, "width", 32);
    qdev_prop_set_bit(dev, "batch-dma", true);
    qdev_connect_clock_in(dev, "clk", hclk);
    soc_map(SYS_BUS_DEVICE(dev), 0x40000000, errp);
    sysbus_connect_irq(SYS_BUS_DEVICE(dev), 0, NVIC(WB55_IRQ_TIM2));
    qdev_connect_gpio_out_named(dev, "dma-up", 0, DREQ(WB55_DREQ_TIM2_UP));
    wb55_dma_set_pulse(&s->dma, WB55_DREQ_TIM2_UP);
    wb55_rcc_add_reset_hook(&s->rcc, WB55_RCC_APB1RSTR1, 0, dev);
    dev = soc_regfile("tim16", 0x40014400, 0x400, WB55_RF_TIM);
    wb55_rcc_add_reset_hook(&s->rcc, WB55_RCC_APB2RSTR, 17, dev);
    s->tim16 = WB55_REGFILE(dev);
    /* TIM17: NFC block-TX timer (one-pulse, update interrupt) */
    dev = DEVICE(&s->tim17);
    qdev_prop_set_uint32(dev, "rcr-bits", 8);
    qdev_connect_clock_in(dev, "clk", hclk);
    soc_map(SYS_BUS_DEVICE(dev), 0x40014800, errp);
    sysbus_connect_irq(SYS_BUS_DEVICE(dev), 0,
                       qdev_get_gpio_in(DEVICE(&s->tim17_or), 1));
    wb55_rcc_add_reset_hook(&s->rcc, WB55_RCC_APB2RSTR, 18, dev);
    dev = soc_regfile("adc1", 0x50040000, 0x400, WB55_RF_ADC);
    wb55_rcc_add_reset_hook(&s->rcc, WB55_RCC_AHB2RSTR, 13, dev);
    /* AES1 is the application's, and the one the FUS lends keys to; AES2
     * belongs to the radio stack, when there is one */
    dev = DEVICE(&s->aes1);
    qdev_prop_set_string(dev, "name", "aes1");
    soc_map(SYS_BUS_DEVICE(dev), 0x50060000, errp);
    sysbus_connect_irq(SYS_BUS_DEVICE(dev), 0, NVIC(WB55_IRQ_AES1));
    wb55_rcc_add_reset_hook(&s->rcc, WB55_RCC_AHB2RSTR, 16, dev);
    dev = DEVICE(&s->aes2);
    qdev_prop_set_string(dev, "name", "aes2");
    soc_map(SYS_BUS_DEVICE(dev), 0x58001800, errp);
    sysbus_connect_irq(SYS_BUS_DEVICE(dev), 0, NVIC(WB55_IRQ_AES2));
    wb55_rcc_add_reset_hook(&s->rcc, WB55_RCC_AHB3RSTR, 17, dev);
    soc_regfile("pka", 0x58002000, 0x2000, WB55_RF_PLAIN);
    soc_regfile("crc", 0x40023000, 0x400, WB55_RF_PLAIN);
    /* COMP1/2: EXTI lines 20/21, and TIM2's TI4 when TIM2_OR remaps it */
    s->comp.out_changed = soc_comp_out;
    s->comp.out_opaque = s;
    soc_map(SYS_BUS_DEVICE(&s->comp), 0x40010200, errp);
    for (int n = 0; n < 2; n++) {
        qdev_connect_gpio_out_named(DEVICE(&s->comp), "exti", n,
                                    qdev_get_gpio_in(DEVICE(&s->exti), 20 + n));
    }
    soc_regfile("iwdg", 0x40003000, 0x400, WB55_RF_PLAIN);
    soc_regfile("wwdg", 0x40002C00, 0x400, WB55_RF_PLAIN);
    soc_regfile("crs", 0x40006000, 0x400, WB55_RF_PLAIN);
    soc_regfile("lcd", 0x40002400, 0x400, WB55_RF_PLAIN);
    soc_regfile("sai1", 0x40015400, 0x400, WB55_RF_PLAIN);
    soc_regfile("tsc", 0x40024000, 0x400, WB55_RF_PLAIN);
    soc_regfile("quadspi", 0xA0001000, 0x400, WB55_RF_PLAIN);
#undef NVIC
#undef DREQ
}

static void wb55_soc_class_init(ObjectClass *klass, const void *data)
{
    DeviceClass *dc = DEVICE_CLASS(klass);

    dc->realize = wb55_soc_realize;
    dc->user_creatable = false;
}

static const TypeInfo wb55_soc_info = {
    .name = TYPE_WB55_SOC,
    .parent = TYPE_SYS_BUS_DEVICE,
    .instance_size = sizeof(WB55SocState),
    .instance_init = wb55_soc_init,
    .class_init = wb55_soc_class_init,
};

static void wb55_soc_register(void)
{
    type_register_static(&wb55_soc_info);
}
type_init(wb55_soc_register)
