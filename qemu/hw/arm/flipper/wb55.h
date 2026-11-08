/*
 * STM32WB55 (Flipper Zero "f7") SoC device models for QEMU - shared declarations
 *
 * Register layouts follow ST RM0434 and the CMSIS header stm32wb55xx.h.
 * Behaviour that the Flipper firmware depends on is called out next to the
 * code that implements it, with the firmware file it was taken from.
 *
 * Copyright (c) 2026, Kara Zajac
 * SPDX-License-Identifier: BSD-3-Clause
 */
#ifndef HW_ARM_FLIPPER_WB55_H
#define HW_ARM_FLIPPER_WB55_H

#include "hw/core/sysbus.h"
#include "hw/core/clock.h"
#include "hw/ssi/ssi.h"
#include "hw/i2c/i2c.h"
#include "qom/object.h"
#include "qemu/timer.h"

/* ---------------------------------------------------------------- memory map */
#define WB55_FLASH_BASE      0x08000000u
#define WB55_FLASH_SIZE      (1024 * 1024)
#define WB55_FLASH_PAGE      4096u
#define WB55_SRAM1_BASE      0x20000000u
#define WB55_SRAM1_SIZE      (192 * 1024)
#define WB55_SRAM2A_BASE     0x20030000u
#define WB55_SRAM2A_SIZE     (32 * 1024)
#define WB55_SRAM2B_BASE     0x20038000u
#define WB55_SRAM2B_SIZE     (32 * 1024)
#define WB55_SYSMEM_BASE     0x1FFF0000u   /* system bootloader ROM            */
#define WB55_OTP_BASE        0x1FFF7000u   /* 1 KiB OTP (board provisioning)   */
#define WB55_UID64_BASE      0x1FFF7580u
#define WB55_UID96_BASE      0x1FFF7590u
#define WB55_FLASHSIZE_BASE  0x1FFF75E0u
#define WB55_OB_BASE         0x1FFF8000u   /* option bytes, value/complement   */
#define WB55_INFO_BASE       WB55_SYSMEM_BASE
#define WB55_INFO_SIZE       0x9000u       /* 0x1FFF0000 .. 0x1FFF8FFF         */

#define WB55_NUM_IRQ         63
#define WB55_NUM_PORTS       6             /* A B C D E H                       */
#define WB55_PINS            (WB55_NUM_PORTS * 16)

/* NVIC lines (stm32wb55xx.h IRQn_Type) */
enum {
    WB55_IRQ_PVD_PVM = 1, WB55_IRQ_TAMP_STAMP_LSECSS = 2, WB55_IRQ_RTC_WKUP = 3,
    WB55_IRQ_FLASH = 4, WB55_IRQ_RCC = 5, WB55_IRQ_EXTI0 = 6,
    WB55_IRQ_DMA1_CH1 = 11, WB55_IRQ_ADC1 = 18, WB55_IRQ_USB_HP = 19,
    WB55_IRQ_USB_LP = 20, WB55_IRQ_C2SEV = 21, WB55_IRQ_COMP = 22,
    WB55_IRQ_EXTI9_5 = 23, WB55_IRQ_TIM1_BRK = 24, WB55_IRQ_TIM1_UP_TIM16 = 25,
    WB55_IRQ_TIM1_TRG_COM_TIM17 = 26, WB55_IRQ_TIM1_CC = 27, WB55_IRQ_TIM2 = 28,
    WB55_IRQ_PKA = 29, WB55_IRQ_I2C1_EV = 30, WB55_IRQ_I2C1_ER = 31,
    WB55_IRQ_I2C3_EV = 32, WB55_IRQ_I2C3_ER = 33, WB55_IRQ_SPI1 = 34,
    WB55_IRQ_SPI2 = 35, WB55_IRQ_USART1 = 36, WB55_IRQ_LPUART1 = 37,
    WB55_IRQ_EXTI15_10 = 40, WB55_IRQ_RTC_ALARM = 41, WB55_IRQ_IPCC_C1_RX = 44,
    WB55_IRQ_IPCC_C1_TX = 45, WB55_IRQ_HSEM = 46, WB55_IRQ_LPTIM1 = 47,
    WB55_IRQ_LPTIM2 = 48, WB55_IRQ_AES1 = 51, WB55_IRQ_AES2 = 52,
    WB55_IRQ_RNG = 53, WB55_IRQ_DMA2_CH1 = 55, WB55_IRQ_DMAMUX_OVR = 62,
};

/* DMAMUX request inputs (stm32wbxx_ll_dmamux.h) */
enum {
    WB55_DREQ_SPI1_RX = 6, WB55_DREQ_SPI1_TX = 7,
    WB55_DREQ_SPI2_RX = 8, WB55_DREQ_SPI2_TX = 9,
    WB55_DREQ_I2C1_RX = 10, WB55_DREQ_I2C1_TX = 11,
    WB55_DREQ_I2C3_RX = 12, WB55_DREQ_I2C3_TX = 13,
    WB55_DREQ_USART1_RX = 14, WB55_DREQ_USART1_TX = 15,
    WB55_DREQ_LPUART1_RX = 16, WB55_DREQ_LPUART1_TX = 17,
    WB55_DREQ_NUM = 41,
};

/* RCC register offsets that other models care about */
#define WB55_RCC_AHB1RSTR   0x28
#define WB55_RCC_AHB2RSTR   0x2C
#define WB55_RCC_AHB3RSTR   0x30
#define WB55_RCC_APB1RSTR1  0x38
#define WB55_RCC_APB1RSTR2  0x3C
#define WB55_RCC_APB2RSTR   0x40

/* ----------------------------------------------------------------- RCC */
#define TYPE_WB55_RCC "stm32wb55-rcc"
OBJECT_DECLARE_SIMPLE_TYPE(WB55RccState, WB55_RCC)
#define WB55_RCC_NREGS (0x190 / 4)

typedef struct WB55RccHook {
    uint16_t off;
    uint8_t bit;
    DeviceState *dev;
} WB55RccHook;

struct WB55RccState {
    SysBusDevice parent_obj;
    MemoryRegion mmio;
    qemu_irq irq;
    Clock *hclk;                 /* CPU1 HCLK, follows the selected SYSCLK */
    Clock *refclk;               /* HCLK/8, SysTick external reference     */
    uint32_t regs[WB55_RCC_NREGS];
    WB55RccHook hooks[64];
    int nhooks;
    bool powered;                /* backup domain has seen power           */
    uint32_t pending_reset_flags;/* CSR flags for the next reset           */
    DeviceState *rtc;            /* backup domain reset target             */
};
void wb55_rcc_add_reset_hook(WB55RccState *s, uint16_t off, int bit,
                             DeviceState *dev);
void wb55_rcc_request_reset(WB55RccState *s, uint32_t csr_flag);
void wb55_rcc_c2_set_sysclk(WB55RccState *s, int sw, bool pll_off);

/* ----------------------------------------------------------------- PWR */
#define TYPE_WB55_PWR "stm32wb55-pwr"
OBJECT_DECLARE_SIMPLE_TYPE(WB55PwrState, WB55_PWR)
struct WB55PwrState {
    SysBusDevice parent_obj;
    MemoryRegion mmio;
    uint32_t regs[0x90 / 4];
    qemu_irq c2boot;             /* raised when CPU1 sets PWR_CR4.C2BOOT */
};

/* --------------------------------------------------------------- FLASH */
#define TYPE_WB55_FLASH "stm32wb55-flash"
OBJECT_DECLARE_SIMPLE_TYPE(WB55FlashState, WB55_FLASH)
struct WB55FlashState {
    SysBusDevice parent_obj;
    MemoryRegion mmio;           /* controller registers @0x58004000 */
    MemoryRegion flash;          /* 1 MiB main flash (ROM device)     */
    MemoryRegion info;           /* sysmem + OTP + UID + option bytes */
    struct BlockBackend *blk;    /* optional persistent flash image   */
    qemu_irq irq;
    uint32_t acr, sr, cr, eccr, c2acr, c2sr, c2cr;
    uint32_t ob[16];             /* option byte "registers" (values)  */
    uint32_t key_state, optkey_state;
    bool pg_pending;
    hwaddr pg_addr;
    uint32_t pg_lo;
    WB55RccState *rcc;
};
uint8_t *wb55_flash_info_ptr(WB55FlashState *s);

/* ---------------------------------------------------------------- HSEM */
#define TYPE_WB55_HSEM "stm32wb55-hsem"
OBJECT_DECLARE_SIMPLE_TYPE(WB55HsemState, WB55_HSEM)
struct WB55HsemState {
    SysBusDevice parent_obj;
    MemoryRegion mmio;
    qemu_irq irq;
    uint32_t r[32];              /* LOCK | COREID<<8 | PROCID */
    uint32_t c1ier, c1isr, c2ier, c2isr, keyr;
};

/* ---------------------------------------------------------- IPCC + CPU2 */
#define TYPE_WB55_IPCC "stm32wb55-ipcc"
OBJECT_DECLARE_SIMPLE_TYPE(WB55IpccState, WB55_IPCC)
struct WB55IpccState {
    SysBusDevice parent_obj;
    MemoryRegion mmio;
    qemu_irq irq_rx, irq_tx;
    uint32_t c1cr, c1mr, c1toc2sr, c2cr, c2mr, c2toc1sr;
    /* behavioural CPU2: ST FUS without a wireless stack */
    QEMUTimer *boot_timer;
    bool c2_booted;
    uint32_t fus_version;
    uint64_t uid64;
    WB55FlashState *flash;       /* IPCCBR option byte -> table address */
    WB55RccState *rcc;           /* SHCI SET_SYSTEM_CLOCK switches SYSCLK */
    uint32_t unknown_cmd_log;
};

/* ---------------------------------------------------------------- EXTI */
#define TYPE_WB55_EXTI "stm32wb55-exti"
OBJECT_DECLARE_SIMPLE_TYPE(WB55ExtiState, WB55_EXTI)
#define WB55_EXTI_LINES 64
struct WB55ExtiState {
    SysBusDevice parent_obj;
    MemoryRegion mmio;
    uint32_t rtsr[2], ftsr[2], swier[2], pr[2], imr[2], emr[2];
    uint32_t c2imr[2], c2emr[2];
    uint64_t level;              /* current input level per line */
    qemu_irq out[WB55_EXTI_LINES];
};

/* -------------------------------------------------------------- SYSCFG */
#define TYPE_WB55_SYSCFG "stm32wb55-syscfg"
OBJECT_DECLARE_SIMPLE_TYPE(WB55SyscfgState, WB55_SYSCFG)
struct WB55SyscfgState {
    SysBusDevice parent_obj;
    MemoryRegion mmio;
    uint32_t memrmp, cfgr1, exticr[4], scsr, cfgr2, swpr1, swpr2;
    uint32_t imr1, imr2, c2imr1, c2imr2, sipcr;
    uint32_t vrefbuf_csr, vrefbuf_ccr;
    uint8_t level[8 * 16];       /* pin level per EXTICR port code */
    qemu_irq exti[16];
    void (*remap)(void *opaque, int mode);
    void *remap_opaque;
};

/* ----------------------------------------------------------------- RTC */
#define TYPE_WB55_RTC "stm32wb55-rtc"
OBJECT_DECLARE_SIMPLE_TYPE(WB55RtcState, WB55_RTC)
struct WB55RtcState {
    SysBusDevice parent_obj;
    MemoryRegion mmio;
    qemu_irq alarm_irq, wakeup_irq, tamp_irq;  /* -> EXTI 17 / 19 / 18 */
    QEMUTimer *tick;
    bool backup_valid;           /* backup domain powered */
    int64_t base_s;              /* calendar seconds at base_ns */
    int64_t base_ns;
    uint32_t cr, isr_flags, prer, wutr, alrmar, alrmbr, alrmassr, alrmbssr;
    uint32_t calr, tampcr, or_, shiftr, wpr;
    uint32_t bkp[20];
    uint32_t init_bkp[20];       /* "factory" backup register contents */
    int64_t wut_next_ns;
    int64_t last_alarm_s;
};
void wb55_rtc_backup_domain_reset(WB55RtcState *s);

/* backup domain image, persisted by the board between runs */
typedef struct WB55RtcBackup {
    int64_t calendar_offset;     /* guest calendar - host clock, seconds */
    uint32_t cr, prer, wutr, alrmar, alrmbr, alrmassr, alrmbssr;
    uint32_t calr, tampcr, or_;
    uint32_t bkp[20];
} WB55RtcBackup;
void wb55_rtc_export(WB55RtcState *s, WB55RtcBackup *b);
void wb55_rtc_import(WB55RtcState *s, const WB55RtcBackup *b);

/* --------------------------------------------------------------- LPTIM */
#define TYPE_WB55_LPTIM "stm32wb55-lptim"
OBJECT_DECLARE_SIMPLE_TYPE(WB55LptimState, WB55_LPTIM)
struct WB55LptimState {
    SysBusDevice parent_obj;
    MemoryRegion mmio;
    qemu_irq irq;
    QEMUTimer *timer;
    uint32_t isr, ier, cfgr, cr, cmp, arr, or_;
    bool running, oneshot;
    int64_t start_ns;            /* counter == 0 at this time */
    uint32_t stopped_cnt;
};

/* ----------------------------------------------------------------- SPI */
#define TYPE_WB55_SPI "stm32wb55-spi"
OBJECT_DECLARE_SIMPLE_TYPE(WB55SpiState, WB55_SPI)
struct WB55SpiState {
    SysBusDevice parent_obj;
    MemoryRegion mmio;
    qemu_irq irq;
    qemu_irq dma_rx, dma_tx;
    SSIBus *bus;
    uint32_t cr1, cr2, sr_err, crcpr;
    uint8_t rxfifo[4];
    int rxcount;
};

/* ----------------------------------------------------------------- I2C */
#define TYPE_WB55_I2C "stm32wb55-i2c"
OBJECT_DECLARE_SIMPLE_TYPE(WB55I2cState, WB55_I2C)
struct WB55I2cState {
    SysBusDevice parent_obj;
    MemoryRegion mmio;
    qemu_irq irq_ev, irq_er;
    I2CBus *bus;
    uint32_t cr1, cr2, oar1, oar2, timingr, timeoutr, isr, pecr, rxdr, txdr;
    bool active;                 /* START sent, STOP not yet */
    bool reading;
    uint32_t count;              /* bytes moved in this NBYTES window */
};

/* ----------------------------------------------------------- DMA/DMAMUX */
#define TYPE_WB55_DMA "stm32wb55-dma"
OBJECT_DECLARE_SIMPLE_TYPE(WB55DmaState, WB55_DMA)
typedef struct WB55DmaChannel {
    uint32_t ccr, cndtr, cpar, cmar;
    uint32_t reload;             /* CNDTR value programmed (circular) */
    uint32_t pidx, midx;         /* transfer progress */
} WB55DmaChannel;
struct WB55DmaState {
    SysBusDevice parent_obj;
    MemoryRegion mmio_dma1, mmio_dma2, mmio_mux;
    qemu_irq irq[14];
    WB55DmaChannel ch[14];       /* DMA1 ch1..7 = 0..6, DMA2 ch1..7 = 7..13 */
    uint32_t isr[2];
    uint32_t muxccr[14];
    uint64_t req;                /* request line levels (bit = DMAMUX input) */
    bool in_service;
    bool dirty;
};

/* ------------------------------------------------- small misc devices */
#define TYPE_WB55_RNG "stm32wb55-rng"
OBJECT_DECLARE_SIMPLE_TYPE(WB55RngState, WB55_RNG)
struct WB55RngState {
    SysBusDevice parent_obj;
    MemoryRegion mmio;
    qemu_irq irq;
    uint32_t cr, sr;
};

#define TYPE_WB55_DWT "stm32wb55-dwt"
OBJECT_DECLARE_SIMPLE_TYPE(WB55DwtState, WB55_DWT)
struct WB55DwtState {
    SysBusDevice parent_obj;
    MemoryRegion mmio;
    Clock *clk;
    uint32_t ctrl;
    uint64_t base_cycles;        /* CYCCNT at base_ns */
    int64_t base_ns;
    uint32_t comp[16];
};

#define TYPE_WB55_DBGMCU "stm32wb55-dbgmcu"
OBJECT_DECLARE_SIMPLE_TYPE(WB55DbgmcuState, WB55_DBGMCU)
struct WB55DbgmcuState {
    SysBusDevice parent_obj;
    MemoryRegion mmio;
    uint32_t regs[0x54 / 4];
};

/* Read/write register file used for blocks that only need readback. */
#define TYPE_WB55_REGFILE "stm32wb55-regfile"
OBJECT_DECLARE_SIMPLE_TYPE(WB55RegfileState, WB55_REGFILE)
struct WB55RegfileState {
    SysBusDevice parent_obj;
    MemoryRegion mmio;
    char *name;
    uint32_t size;
    uint32_t *regs;
    uint32_t kind;               /* WB55_RF_* special behaviour */
    bool warned;
    /* board hook, called after every write and after reset (e.g. TIM16 -> speaker) */
    void (*changed)(void *opaque);
    void *changed_opaque;
};
enum { WB55_RF_PLAIN = 0, WB55_RF_ADC, WB55_RF_TIM, WB55_RF_AES, WB55_RF_CRC };

/* ----------------------------------------------------------------- USB */
#include "chardev/char-fe.h"
#include "qemu/fifo8.h"

#define TYPE_WB55_USB "stm32wb55-usb"
OBJECT_DECLARE_SIMPLE_TYPE(WB55UsbState, WB55_USB)

/* one control transfer issued by the built-in host */
typedef struct WB55UsbCtl {
    bool active;
    uint8_t setup[8];
    uint8_t buf[512];
    uint16_t len, done;
    uint8_t stage;
    int64_t deadline;
    int64_t not_before;
} WB55UsbCtl;

struct WB55UsbState {
    SysBusDevice parent_obj;
    MemoryRegion mmio;           /* registers  @0x40006800 */
    MemoryRegion pma_mmio;       /* packet RAM @0x40006C00 */
    qemu_irq irq_lp, irq_hp;

    /* device controller */
    uint16_t epr[8];
    uint16_t epr_seen[8];        /* CTR bits the CPU saw at its last read */
    uint16_t cntr, istr, istr_seen, daddr, btable, lpmcsr, bcdr;
    uint8_t pma[1024];
    int64_t frame_base_ns;

    /* built-in host */
    CharFrontend chr;            /* CDC ACM data <-> this chardev */
    bool attached;               /* virtual cable plugged in */
    bool debug;
    QEMUTimer *timer;
    int hstate;
    int64_t wait_until;
    int failures;
    uint8_t addr;                /* device address the host talks to */
    uint8_t ep0_mps;
    int enum_step;
    WB55UsbCtl ctl;
    uint8_t dev_desc[18];
    uint8_t cfg_desc[512];
    uint16_t cfg_len;
    uint16_t langid;
    char strings[3][64];         /* manufacturer, product, serial */
    char functions[64];          /* "CDC ACM", "HID", ... */

    /* CDC ACM function (first one in the configuration) */
    bool cdc;
    uint8_t cdc_if, cdc_in, cdc_out;
    uint16_t cdc_out_mps;
    bool chr_open, dtr_sent, dtr_pending;
    Fifo8 to_dev, to_chr;
    guint watch;

    /* HID function: keystrokes become text on the same chardev */
    bool hid;
    uint8_t hid_in, hid_interval;
    int64_t hid_next_poll;
    uint8_t kb_mods, kb_keys[6], kb_lone_mods, mouse_btn;
    uint16_t alt_code;           /* Windows Alt+Numpad character entry */
    uint8_t alt_digits;
    uint16_t consumer[2];

    /* board hook: VBUS follows the virtual cable */
    void (*vbus_changed)(void *opaque, bool on);
    void *vbus_opaque;
};

/* ------------------------------------------------------------ the SoC */
#include "hw/arm/armv7m.h"
#include "hw/gpio/stm32l4x5_gpio.h"
#include "hw/char/stm32l4x5_usart.h"
#include "hw/core/or-irq.h"

#define TYPE_WB55_SOC "stm32wb55-soc"
OBJECT_DECLARE_SIMPLE_TYPE(WB55SocState, WB55_SOC)

enum { WB55_PA = 0, WB55_PB, WB55_PC, WB55_PD, WB55_PE, WB55_PH };
#define WB55_PIN(port, pin) ((port) * 16 + (pin))

struct WB55SocState {
    SysBusDevice parent_obj;

    ARMv7MState armv7m;
    WB55RccState rcc;
    WB55PwrState pwr;
    WB55FlashState flash;
    WB55HsemState hsem;
    WB55IpccState ipcc;
    WB55ExtiState exti;
    WB55SyscfgState syscfg;
    WB55RtcState rtc;
    WB55LptimState lptim[2];
    WB55SpiState spi[2];             /* SPI1 (radio bus), SPI2 (display/SD) */
    WB55I2cState i2c[2];             /* I2C1 (power bus), I2C3 (external)   */
    WB55DmaState dma;
    WB55RngState rng;
    WB55DwtState dwt;
    WB55DbgmcuState dbgmcu;
    WB55UsbState usb;
    Stm32l4x5GpioState gpio[WB55_NUM_PORTS];
    Stm32l4x5UsartBaseState usart1;
    Stm32l4x5UsartBaseState lpuart1;
    OrIRQState exti_or[3];           /* EXTI9_5, EXTI15_10, COMP */

    MemoryRegion sram1, sram2a, sram2b;
    MemoryRegion sram2a_alias, sram2b_alias;
    MemoryRegion boot_flash, boot_sysmem, boot_sram1;

    WB55RegfileState *tim16;         /* speaker PWM (TIM16 CH1 on PB8) */

    qemu_irq *gpio_tap;
    qemu_irq gpio_out[WB55_PINS];    /* pin levels for board devices */
    uint8_t pin_level[WB55_PINS];
};

void wb55_soc_latch_boot(WB55SocState *s);
void wb55_soc_sync_pins(WB55SocState *s);

/* GPIO pin index helpers for the board */
static inline int wb55_pin(char port, int pin)
{
    static const char ports[] = "ABCDEH";
    const char *p = strchr(ports, port);
    return p ? (int)(p - ports) * 16 + pin : -1;
}

#endif
