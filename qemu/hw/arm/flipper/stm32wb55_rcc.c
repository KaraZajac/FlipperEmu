/*
 * STM32WB55 RCC: clock tree, ready flags, bus resets, reset flags
 *
 * The firmware busy-waits on every *RDY flag (targets/f7/furi_hal/
 * furi_hal_clock.c), asserts that a bus clock is disabled before enabling it
 * (furi_hal_bus.c), and stops its tickless-idle timer by pulsing
 * APB1RSTR1.LPTIM1RST (furi_hal_idle_timer.h), so the reset lines are wired to
 * real device resets. It also writes to peripherals it holds in reset (see
 * the bus reset registers below).
 *
 * Copyright (c) 2026, Kara Zajac
 * SPDX-License-Identifier: BSD-3-Clause
 */
#include "qemu/osdep.h"
#include "qemu/log.h"
#include "qapi/error.h"
#include "hw/core/irq.h"
#include "hw/core/qdev-clock.h"
#include "hw/core/qdev.h"
#include "system/runstate.h"
#include "migration/vmstate.h"
#include "wb55.h"

#define R_CR        (0x00 / 4)
#define R_ICSCR     (0x04 / 4)
#define R_CFGR      (0x08 / 4)
#define R_PLLCFGR   (0x0C / 4)
#define R_PLLSAI1   (0x10 / 4)
#define R_CIER      (0x18 / 4)
#define R_CIFR      (0x1C / 4)
#define R_CICR      (0x20 / 4)
#define R_SMPSCR    (0x24 / 4)
#define R_AHB1ENR   (0x48 / 4)
#define R_AHB3ENR   (0x50 / 4)
#define R_APB1ENR1  (0x58 / 4)
#define R_BDCR      (0x90 / 4)
#define R_CSR       (0x94 / 4)
#define R_CRRCR     (0x98 / 4)
#define R_HSECR     (0x9C / 4)
#define R_EXTCFGR   (0x108 / 4)

#define CSR_RMVF    (1u << 23)
#define CSR_OBLRSTF (1u << 25)
#define CSR_PINRSTF (1u << 26)
#define CSR_BORRSTF (1u << 27)
#define CSR_SFTRSTF (1u << 28)
#define CSR_FLAGS   0xFE000000u

static const uint32_t msi_hz[16] = {
    100000, 200000, 400000, 800000, 1000000, 2000000, 4000000, 8000000,
    16000000, 24000000, 32000000, 48000000, 48000000, 48000000, 48000000,
    48000000,
};

static uint32_t pll_input(WB55RccState *s, uint32_t src)
{
    switch (src & 3) {
    case 1: return msi_hz[(s->regs[R_CR] >> 4) & 0xF];
    case 2: return 16000000;
    case 3: return 32000000;
    default: return 0;
    }
}

static void wb55_rcc_update_clocks(WB55RccState *s)
{
    uint32_t cfgr = s->regs[R_CFGR];
    uint64_t sysclk;

    switch ((cfgr >> 2) & 3) {           /* SWS */
    case 0:
        sysclk = msi_hz[(s->regs[R_CR] >> 4) & 0xF];
        break;
    case 1:
        sysclk = 16000000;
        break;
    case 2:
        sysclk = (s->regs[R_CR] & (1u << 20)) ? 16000000 : 32000000; /* HSEPRE */
        break;
    default: {
        uint32_t pll = s->regs[R_PLLCFGR];
        uint32_t m = ((pll >> 4) & 7) + 1;
        uint32_t n = (pll >> 8) & 0x7F;
        uint32_t r = ((pll >> 29) & 7) + 1;
        sysclk = (uint64_t)pll_input(s, pll) / m * n / (r < 2 ? 2 : r);
        if (!sysclk) {
            sysclk = 64000000;
        }
        break;
    }
    }

    static const uint16_t hpre_div[16] = {
        1, 3, 5, 1, 1, 6, 10, 32, 2, 4, 8, 16, 64, 128, 256, 512,
    };
    uint64_t hclk = sysclk / hpre_div[(cfgr >> 4) & 0xF];
    if (!hclk) {
        hclk = 4000000;
    }
    if (clock_get_hz(s->hclk) != hclk) {
        clock_update_hz(s->hclk, hclk);
        clock_update_hz(s->refclk, hclk / 8);
    }
}

/* CPU2 owns the system clock switch while the radio stack runs. */
void wb55_rcc_c2_set_sysclk(WB55RccState *s, int sw, bool pll_off)
{
    uint32_t cfgr = s->regs[R_CFGR] & ~0xFu;

    s->regs[R_CFGR] = cfgr | (sw & 3) | ((sw & 3) << 2);
    if (pll_off) {
        s->regs[R_CR] &= ~((1u << 24) | (1u << 25));    /* PLLON, PLLRDY */
    }
    wb55_rcc_update_clocks(s);
}

void wb55_rcc_add_reset_hook(WB55RccState *s, uint16_t off, int bit,
                             DeviceState *dev)
{
    assert(s->nhooks < ARRAY_SIZE(s->hooks));
    s->hooks[s->nhooks++] = (WB55RccHook){ .off = off, .bit = bit, .dev = dev };
}

void wb55_rcc_request_reset(WB55RccState *s, uint32_t csr_flag)
{
    s->pending_reset_flags |= csr_flag | CSR_PINRSTF;
    qemu_system_reset_request(SHUTDOWN_CAUSE_GUEST_RESET);
}

static void wb55_rcc_sysresetreq(void *opaque, int n, int level)
{
    if (level) {
        wb55_rcc_request_reset(WB55_RCC(opaque), CSR_SFTRSTF);
    }
}

/*
 * Reset one peripheral, not what hangs off its buses: device_cold_reset()
 * would also reset the external chips on an SPI/I2C controller's bus (the
 * firmware pulses I2C1RST/SPI2RST around transactions via furi_hal_bus).
 */
static void wb55_rcc_local_reset(DeviceState *dev)
{
    ResettableClass *rc = RESETTABLE_GET_CLASS(dev);
    Object *obj = OBJECT(dev);

    if (rc->phases.enter) {
        rc->phases.enter(obj, RESET_TYPE_COLD);
    }
    if (rc->phases.hold) {
        rc->phases.hold(obj, RESET_TYPE_COLD);
    }
    if (rc->phases.exit) {
        rc->phases.exit(obj, RESET_TYPE_COLD);
    }
}

static void wb55_rcc_backup_reset(WB55RccState *s)
{
    s->regs[R_BDCR] = 0;
    if (s->rtc) {
        wb55_rtc_backup_domain_reset(WB55_RTC(s->rtc));
    }
}

static uint64_t wb55_rcc_read(void *opaque, hwaddr addr, unsigned size)
{
    WB55RccState *s = opaque;
    unsigned idx = addr / 4;

    if (idx >= WB55_RCC_NREGS) {
        return 0;
    }
    return s->regs[idx];
}

static void wb55_rcc_write(void *opaque, hwaddr addr, uint64_t val64,
                           unsigned size)
{
    WB55RccState *s = opaque;
    unsigned idx = addr / 4;
    uint32_t val = val64, old;

    if (idx >= WB55_RCC_NREGS) {
        return;
    }
    old = s->regs[idx];

    switch (idx) {
    case R_CR:
        /* MSIRDY<-MSION, HSIRDY<-HSION, HSIKERDY<-HSIKERON, HSERDY<-HSEON,
         * PLLRDY<-PLLON, PLLSAI1RDY<-PLLSAI1ON. CSS-detected bits stay 0. */
        val &= ~((1u << 1) | (1u << 10) | (1u << 12) | (1u << 17) |
                 (1u << 25) | (1u << 27));
        val |= (val & 1u) << 1;
        val |= (val & (1u << 8)) << 2;
        val |= (val & (1u << 9)) << 3;
        val |= (val & (1u << 16)) << 1;
        val |= (val & (1u << 24)) << 1;
        val |= (val & (1u << 26)) << 1;
        s->regs[idx] = val;
        wb55_rcc_update_clocks(s);
        break;
    case R_CFGR:
        val &= ~(0xCu | (7u << 16));
        val |= (val & 3u) << 2;             /* SWS follows SW            */
        val |= 7u << 16;                    /* HPREF/PPRE1F/PPRE2F ready  */
        s->regs[idx] = val;
        wb55_rcc_update_clocks(s);
        break;
    case R_PLLCFGR:
        s->regs[idx] = val;
        wb55_rcc_update_clocks(s);
        break;
    case R_CIFR:
        break;                              /* read-only */
    case R_CICR:
        s->regs[R_CIFR] &= ~val;
        break;
    case R_SMPSCR:
        val = (val & ~0x300u) | ((val & 3u) << 8);  /* SMPSSWS<-SMPSSEL */
        s->regs[idx] = val;
        break;
    case WB55_RCC_AHB1RSTR / 4:
    case WB55_RCC_AHB2RSTR / 4:
    case WB55_RCC_AHB3RSTR / 4:
    case WB55_RCC_APB1RSTR1 / 4:
    case WB55_RCC_APB1RSTR2 / 4:
    case WB55_RCC_APB2RSTR / 4: {
        /*
         * Reset on both edges of the bit. A peripheral that is held in reset
         * takes no writes; here its registers stay writable, so the release
         * throws away what was written meanwhile. The firmware does write:
         * furi_hal_serial_resume() sets UE in LPUART1 after every deep
         * sleep, in use or not, and LL_LPUART_Init() configures nothing
         * when it finds UE set.
         */
        uint32_t changed = val ^ old;
        s->regs[idx] = val;
        for (int i = 0; i < s->nhooks; i++) {
            if (s->hooks[i].off == addr &&
                (changed & (1u << s->hooks[i].bit))) {
                wb55_rcc_local_reset(s->hooks[i].dev);
            }
        }
        break;
    }
    case R_BDCR:
        if ((val & (1u << 16)) && !(old & (1u << 16))) {
            wb55_rcc_backup_reset(s);
            s->regs[idx] = 1u << 16;
            break;
        }
        if (old & (1u << 16)) {             /* held in reset */
            s->regs[idx] = val & (1u << 16);
            break;
        }
        val &= ~((1u << 1) | (1u << 6));
        val |= (val & 1u) << 1;             /* LSERDY <- LSEON */
        s->regs[idx] = val;
        break;
    case R_CSR:
        if (val & CSR_RMVF) {
            old &= ~CSR_FLAGS;
        }
        val &= ~(CSR_FLAGS | CSR_RMVF | (1u << 1) | (1u << 3));
        val |= (val & 1u) << 1;             /* LSI1RDY <- LSI1ON */
        val |= (val & 4u) << 1;             /* LSI2RDY <- LSI2ON */
        s->regs[idx] = val | (old & CSR_FLAGS);
        break;
    case R_CRRCR:
        val = (val & ~2u) | ((val & 1u) << 1);  /* HSI48RDY <- HSI48ON */
        s->regs[idx] = val;
        break;
    case R_EXTCFGR:
        s->regs[idx] = val | (3u << 16);    /* SHDHPREF/C2HPREF */
        break;
    default:
        s->regs[idx] = val;
        break;
    }
}

static const MemoryRegionOps wb55_rcc_ops = {
    .read = wb55_rcc_read,
    .write = wb55_rcc_write,
    .endianness = DEVICE_LITTLE_ENDIAN,
    .impl.min_access_size = 4,
    .impl.max_access_size = 4,
    .valid.min_access_size = 1,
    .valid.max_access_size = 4,
};

static void wb55_rcc_reset_hold(Object *obj, ResetType type)
{
    WB55RccState *s = WB55_RCC(obj);
    uint32_t bdcr = s->regs[R_BDCR];
    uint32_t flags = s->regs[R_CSR] & CSR_FLAGS;

    memset(s->regs, 0, sizeof(s->regs));
    s->regs[R_CR] = 0x00000063;          /* MSION|MSIRDY, MSIRANGE=4 MHz */
    s->regs[R_ICSCR] = 0x40000000;
    s->regs[R_CFGR] = 0x00070000;
    s->regs[R_PLLCFGR] = 0x22041000;
    s->regs[R_PLLSAI1] = 0x22041000;
    s->regs[R_SMPSCR] = 0x00000301;
    s->regs[R_AHB3ENR] = 0x02080000;
    s->regs[R_APB1ENR1] = 0x00000400;
    s->regs[R_HSECR] = 0x00000030;
    s->regs[R_EXTCFGR] = 0x00030000;

    if (!s->powered) {
        /* first power-on: backup domain starts empty, POR flags */
        s->powered = true;
        s->regs[R_BDCR] = 0;
        s->regs[R_CSR] = CSR_BORRSTF | CSR_PINRSTF;
    } else {
        s->regs[R_BDCR] = bdcr;             /* backup domain survives */
        s->regs[R_CSR] = flags |
            (s->pending_reset_flags ? s->pending_reset_flags : CSR_PINRSTF);
    }
    s->pending_reset_flags = 0;
}

static void wb55_rcc_reset_exit(Object *obj, ResetType type)
{
    wb55_rcc_update_clocks(WB55_RCC(obj));
}

static void wb55_rcc_init(Object *obj)
{
    WB55RccState *s = WB55_RCC(obj);

    memory_region_init_io(&s->mmio, obj, &wb55_rcc_ops, s, "rcc", 0x400);
    sysbus_init_mmio(SYS_BUS_DEVICE(obj), &s->mmio);
    sysbus_init_irq(SYS_BUS_DEVICE(obj), &s->irq);
    s->hclk = qdev_init_clock_out(DEVICE(obj), "hclk");
    s->refclk = qdev_init_clock_out(DEVICE(obj), "refclk");
    qdev_init_gpio_in_named(DEVICE(obj), wb55_rcc_sysresetreq, "sysresetreq", 1);
    clock_set_hz(s->hclk, 4000000);
    clock_set_hz(s->refclk, 500000);
}

/*
 * A saved state.  The clocks' periods come with it, as the rules for clocks
 * want it (docs/devel/clocks.rst): nothing is propagated in a load, and who
 * takes a clock from here saves that clock too.
 */
static const VMStateDescription vmstate_wb55_rcc = {
    .name = "stm32wb55-rcc",
    .version_id = 1,
    .minimum_version_id = 1,
    .fields = (const VMStateField[]) {
        VMSTATE_UINT32_ARRAY(regs, WB55RccState, WB55_RCC_NREGS),
        VMSTATE_BOOL(powered, WB55RccState),
        VMSTATE_UINT32(pending_reset_flags, WB55RccState),
        VMSTATE_CLOCK(hclk, WB55RccState),
        VMSTATE_CLOCK(refclk, WB55RccState),
        VMSTATE_END_OF_LIST()
    }
};

static void wb55_rcc_class_init(ObjectClass *klass, const void *data)
{
    ResettableClass *rc = RESETTABLE_CLASS(klass);

    rc->phases.hold = wb55_rcc_reset_hold;
    rc->phases.exit = wb55_rcc_reset_exit;
    DEVICE_CLASS(klass)->vmsd = &vmstate_wb55_rcc;
}

static const TypeInfo wb55_rcc_info = {
    .name = TYPE_WB55_RCC,
    .parent = TYPE_SYS_BUS_DEVICE,
    .instance_size = sizeof(WB55RccState),
    .instance_init = wb55_rcc_init,
    .class_init = wb55_rcc_class_init,
};

static void wb55_rcc_register(void)
{
    type_register_static(&wb55_rcc_info);
}
type_init(wb55_rcc_register)
