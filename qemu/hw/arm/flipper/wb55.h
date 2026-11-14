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
#include "qemu/notify.h"
#include "chardev/char-fe.h"

struct FlipperLink;                     /* flipper.h */

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
    WB55_DREQ_TIM1_UP = 25, WB55_DREQ_TIM2_UP = 32,
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
    VMChangeStateEntry *loaded;  /* a saved state came: the image follows */
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

/* ----------------------------------------------------------------- AES */
#define TYPE_WB55_AES "stm32wb55-aes"
OBJECT_DECLARE_SIMPLE_TYPE(WB55AesState, WB55_AES)

/* One CBC encryption, in AES's own byte order: wb55_aes_enclave_load() */
typedef struct WB55AesAnswer {
    uint8_t iv[16], in[16], out[16];
} WB55AesAnswer;

struct WB55AesState {
    SysBusDevice parent_obj;
    MemoryRegion mmio;
    qemu_irq irq;
    char *name;
    uint32_t cr, sr;
    uint32_t key[8];             /* KEYR0..7 */
    uint32_t iv[4];              /* IVR0..3 */
    uint32_t susp[8];
    uint32_t din[4], dout[4];
    uint8_t nin, nout;           /* words written / words left to read */
    uint8_t gcm_h[16], gcm_y[16];
    bool secure;                 /* the key registers are CPU2's */
    const WB55AesAnswer *answer;
    bool has_answer;             /* in a saved state: `answer` was there */
    bool answer_due;             /* loaded with one, not put back yet */
    bool warned_dma;
};

/* CPU2's side of AES1: a key CPU1 can use but not read */
void wb55_aes_enclave_load(WB55AesState *s, const uint8_t *key, int len,
                           const WB55AesAnswer *answer);
void wb55_aes_enclave_unload(WB55AesState *s);
void wb55_aes_enclave_answer(WB55AesState *s, const WB55AesAnswer *answer);

/* ---------------------------------------------------------- IPCC + CPU2 */
#define TYPE_WB55_IPCC "stm32wb55-ipcc"
OBJECT_DECLARE_SIMPLE_TYPE(WB55IpccState, WB55_IPCC)

/* The FUS's user keys (AN5185): index 0 the master key, 1..100 the others */
#define WB55_FUS_KEYS        101
enum { WB55_FUS_KEY_NONE = 0, WB55_FUS_KEY_SIMPLE, WB55_FUS_KEY_MASTER,
       WB55_FUS_KEY_ENCRYPTED };
typedef struct WB55FusKey {
    uint8_t type;                /* WB55_FUS_KEY_*: how it was stored */
    uint8_t len;                 /* 16 or 32 */
    uint8_t data[32];            /* the key itself, decrypted if it came so */
} WB55FusKey;

/*
 * CPU2's BLE stack, behavioural (stm32wb55_ble.c): what ST's wireless
 * firmware keeps for CPU1, its GATT database and its GAP state, and the
 * events on their way to CPU1.  Sized above what the Flipper firmwares ask
 * of the stack in SHCI_C2_BLE_Init (8 services, 68 attribute records, 1344
 * bytes of values in 1.5.1).
 */
#define WB55_BLE_SERVICES    24
#define WB55_BLE_ATTRS       192
#define WB55_BLE_VALUES      4096
#define WB55_BLE_SLOTS       16      /* event buffers in CPU1's pool, at most */
#define WB55_BLE_PENDING     8       /* events waiting for a buffer */
#define WB55_BLE_EVT_LEN     255     /* an event's parameters, at most */

enum { WB55_BLE_ATTR_FREE, WB55_BLE_ATTR_SERVICE, WB55_BLE_ATTR_CHAR,
       WB55_BLE_ATTR_VALUE, WB55_BLE_ATTR_CCCD, WB55_BLE_ATTR_DESC };

typedef struct WB55BleAttr {
    uint16_t handle;
    uint16_t service;            /* its service's declaration */
    uint8_t kind;                /* WB55_BLE_ATTR_*: FREE is an unused record */
    uint8_t uuid_type;           /* 1: 16 bits, 2: 128 bits */
    uint8_t uuid[16];            /* as the command has it, little-endian */
    uint8_t props;               /* a characteristic's properties */
    uint8_t perms;               /* security permissions */
    uint8_t access;              /* a descriptor's: read 1, write 2, w/o rsp 4 */
    uint8_t evt_mask;            /* GATT_Evt_Mask: what CPU1 hears of */
    uint8_t variable;            /* its value's length may change */
    uint16_t max_len;            /* room for its value */
    uint16_t len;                /* its value's length */
    uint16_t at;                 /* where its value is in WB55BleState.values */
} WB55BleAttr;

typedef struct WB55BleService {
    uint16_t handle;             /* its declaration; 0: a free entry */
    uint16_t last;               /* the last handle of its range */
    uint16_t next;               /* the next handle that is free in it */
} WB55BleService;

/* where an event goes: which buffer of CPU1's, and so which channel */
enum { WB55_BLE_BUF_POOL, WB55_BLE_BUF_CMD, WB55_BLE_BUF_CS, WB55_BLE_BUF_SYS };

typedef struct WB55BleEvent {
    uint8_t buf;                 /* WB55_BLE_BUF_* */
    uint8_t code;                /* the event code */
    uint8_t len;                 /* the length of its parameters */
    uint8_t data[WB55_BLE_EVT_LEN];
} WB55BleEvent;

typedef struct WB55BleState {
    bool up;                     /* SHCI_C2_BLE_Init has been done */
    uint16_t max_attrs, max_services, max_values;  /* what BLE_Init asked */
    WB55BleService svc[WB55_BLE_SERVICES];
    WB55BleAttr attr[WB55_BLE_ATTRS];
    uint8_t values[WB55_BLE_VALUES];
    uint16_t nattrs, values_used, next_handle;
    /* GAP */
    uint16_t gap_service, name_char, appearance_char;
    uint8_t role;
    uint8_t adv;                 /* advertising: discoverable */
    uint8_t adv_type, own_addr_type, adv_filter;
    uint16_t adv_min, adv_max, conn_min, conn_max;
    uint8_t name_len;            /* the local name's AD structure, type first */
    uint8_t name[32];
    uint8_t uuids_len;           /* the service UUIDs' AD structure */
    uint8_t uuids[32];
    uint8_t scan_rsp_len;
    uint8_t scan_rsp[31];
    uint8_t public_addr[6];
    uint8_t random_addr[6];      /* the static random one (config data 0x2E) */
    uint8_t io_cap, bonding, mitm, sc, fixed_pin_used;
    uint32_t fixed_pin;
    /* The connection: a central on the link (docs/RADIO_LINK.md), one at a
     * time.  What a stack restart or a reset ends. */
    uint16_t att_mtu;            /* the server's ATT MTU (BLE_Init's AttMtu) */
    uint16_t conn;               /* its handle; 0: no connection */
    uint8_t peer[6], peer_type;  /* the central's address, LSB first */
    uint16_t interval, latency, timeout;   /* 1.25 ms, events, 10 ms */
    uint16_t mtu;                /* the ATT MTU agreed: 23 until an exchange */
    uint8_t sec;                 /* WB55_BLE_SEC_*: what the link has */
    uint8_t pair;                /* WB55_BLE_PAIR_*: a pairing under way */
    uint8_t pair_flags;          /* WB55_BLE_PF_*: who has answered, how */
    uint32_t pair_key;           /* CPU1's passkey, or the value compared */
    uint32_t pair_entered;       /* the central's passkey */
    uint8_t pair_io;             /* the central's IO capability */
    uint8_t pair_opts;           /* WB55_BLE_PO_*: what it asked for */
    uint8_t indications;         /* sent, and not yet confirmed */
    uint8_t config1;             /* SHCI_C2_Config's Config1 */
    uint8_t tx_high, tx_level;
    uint8_t beacon;              /* the additional beacon is on */
    /* SHCI_C2_Config: where the stack keeps its bonds in CPU1's RAM */
    uint32_t nvm_ram, evt_mask1;
    /* the event buffers (the pool and the two spares are in CPU1's RAM) */
    uint32_t slots_busy;         /* a bit for each buffer of the pool */
    bool spare_ble_busy, spare_sys_busy;
    WB55BleEvent pending[WB55_BLE_PENDING];
    uint8_t npending;
    uint32_t unknown_log;
} WB55BleState;

struct WB55IpccState {
    SysBusDevice parent_obj;
    MemoryRegion mmio;
    qemu_irq irq_rx, irq_tx;
    uint32_t c1cr, c1mr, c1toc2sr, c2cr, c2mr, c2toc1sr;
    /* behavioural CPU2: ST's FUS, and a BLE stack if one is installed */
    QEMUTimer *boot_timer;
    bool c2_booted;
    bool c2_stack;               /* it runs the stack (else the FUS) */
    bool c2_reinit;              /* told to restart: waits for CPU1's enable */
    char *radio_stack;           /* property: "1.20.0" (the version), "none" */
    uint32_t stack_version;      /* its InfoTable Version word, 0: none */
    uint8_t stack_type;          /* INFO_STACK_TYPE_BLE_*: 3 = Light */
    WB55BleState ble;
    CharFrontend ble_chr;        /* property "ble-link": a central's line link */
    struct FlipperLink *ble_link;
    char *ble_adv;               /* the advertising line it was sent last */
    uint32_t fus_version;
    uint64_t uid64;
    WB55FlashState *flash;       /* IPCCBR option byte -> table address */
    WB55RccState *rcc;           /* SHCI SET_SYSTEM_CLOCK switches SYSCLK */
    uint32_t unknown_cmd_log;
    /* the FUS's user key store: stm32wb55_fus.c */
    WB55AesState *aes;           /* AES1: where a loaded key goes */
    char *key_file;              /* property "keys": the store on the host */
    WB55FusKey key[WB55_FUS_KEYS];
    bool key_locked[WB55_FUS_KEYS];     /* FUS_LOCK_USR_KEY: until a reset */
    uint8_t key_loaded;          /* the index in AES1's key registers, or 0 */
    /* board hooks. The keys a device leaves the factory with: called once
     * for a store that doesn't exist yet, to fill it with
     * wb55_fus_put_key(). */
    void (*provision)(WB55IpccState *s);
    /* A CBC encryption the key at `index` is known to produce, or NULL */
    const WB55AesAnswer *(*key_answer)(int index, const uint8_t *key, int len);
    VMChangeStateEntry *loaded;  /* a saved state came: the file follows */
};

bool wb55_fus_realize(WB55IpccState *s, Error **errp);
void wb55_fus_reset(WB55IpccState *s);
void wb55_fus_put_key(WB55IpccState *s, int index, int type,
                      const uint8_t *key, int len);
/* The SHCI commands: these return the status byte (0x00 done, 0x01 fail) */
uint8_t wb55_fus_store_key(WB55IpccState *s, const uint8_t *p, int plen,
                           uint8_t *index);
uint8_t wb55_fus_load_key(WB55IpccState *s, uint8_t index);
uint8_t wb55_fus_unload_key(WB55IpccState *s, uint8_t index);
uint8_t wb55_fus_lock_key(WB55IpccState *s, uint8_t index);
/* A saved state was loaded and the machine runs: the store on the host is
 * the loaded one, and AES1 has its answer again if it had one */
void wb55_fus_loaded(WB55IpccState *s);

/* stm32wb55_ipcc.c, for the stack's model */
hwaddr wb55_ipcc_ref_table(WB55IpccState *s);
void wb55_ipcc_update_irq(WB55IpccState *s);
/* stm32wb55_ble.c */
void wb55_ble_reset(WB55IpccState *s);
uint8_t wb55_ble_init(WB55IpccState *s, const uint8_t *p, int plen);
uint8_t wb55_ble_config(WB55IpccState *s, const uint8_t *p, int plen);
void wb55_ble_command(WB55IpccState *s);
void wb55_ble_post(WB55IpccState *s, int buf, uint8_t code,
                   const uint8_t *data, int len);
void wb55_ble_released(WB55IpccState *s, hwaddr buf);
void wb55_ble_flush(WB55IpccState *s);
/* The link to a central on the host: set up at realize; a loaded machine's
 * connection ends at its first run (it was in the air) */
void wb55_ble_realize(WB55IpccState *s);
void wb55_ble_loaded(WB55IpccState *s);
int wb55_ble_bonds(WB55IpccState *s);   /* the centrals it is bonded with */
extern const VMStateDescription vmstate_wb55_ble;

/* WB55BleState.sec */
#define WB55_BLE_SEC_ENCRYPTED       0x01
#define WB55_BLE_SEC_AUTHENTICATED   0x02    /* MITM: a passkey or a compare */
#define WB55_BLE_SEC_BONDED          0x04
/* WB55BleState.pair: how a pairing goes */
enum { WB55_BLE_PAIR_NONE, WB55_BLE_PAIR_PASSKEY, WB55_BLE_PAIR_COMPARE };
/* WB55BleState.pair_flags */
#define WB55_BLE_PF_FLIPPER          0x01    /* CPU1 has answered */
#define WB55_BLE_PF_CENTRAL          0x02    /* the central has answered */
#define WB55_BLE_PF_FLIPPER_YES      0x04    /* (a compare: what they said) */
#define WB55_BLE_PF_CENTRAL_YES      0x08
#define WB55_BLE_PF_ASKED            0x10    /* the central was asked */
#define WB55_BLE_PF_AUTH             0x20    /* it will be authenticated */
#define WB55_BLE_PF_BOND             0x40    /* both sides bond */
#define WB55_BLE_PF_SC               0x80    /* LE Secure Connections */
/* WB55BleState.pair_opts: the central's pairing request */
#define WB55_BLE_PO_MITM             0x01
#define WB55_BLE_PO_SC               0x02
#define WB55_BLE_PO_BOND             0x04
#define WB55_BLE_PO_REBOND           0x08    /* bond lost: CPU1 to allow it */

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
    int64_t saved_offset;        /* in a saved state: calendar - host clock */
    VMChangeStateEntry *loaded;  /* a saved state came: the calendar follows */
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
    /* the SoC's: the timer's clock in Hz, as RCC_CCIPR picks it (none: LSE) */
    uint32_t (*clock_hz)(void *opaque);
    void *clock_opaque;
    /* the board's: after every register write and after reset */
    void (*changed)(void *opaque);
    void *changed_opaque;
};
/* the output as a PWM, if it is one: the timer's clock in Hz, what the
 * prescaler divides it by, and a period and its high part in ticks */
bool wb55_lptim_pwm(WB55LptimState *s, uint32_t *clock, uint32_t *divider,
                    uint32_t *period, uint32_t *high);

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
unsigned wb55_spi_bits(WB55SpiState *s);    /* a frame's, as CR2.DS has it */

/* ----------------------------------------------------------------- I2C */
#define TYPE_WB55_I2C "stm32wb55-i2c"
OBJECT_DECLARE_SIMPLE_TYPE(WB55I2cState, WB55_I2C)
/* What happens on a bus, as a board is told it (WB55I2cState.tap).  A part
 * of a transfer is what lies between a START and the next START or STOP. */
typedef enum WB55I2cTap {
    WB55_I2C_TAP_CALL,           /* START and the address byte as it is on
                                  * the wire (bit 0: to read); ok: taken */
    WB55_I2C_TAP_BYTE,           /* a byte written (ok: the device took it)
                                  * or read */
    WB55_I2C_TAP_END,            /* the part is over; ok: with a STOP (not
                                  * ok: another START follows) */
} WB55I2cTap;
struct WB55I2cState {
    SysBusDevice parent_obj;
    MemoryRegion mmio;
    qemu_irq irq_ev, irq_er;
    I2CBus *bus;
    char *bus_name;              /* the bus by name, for -device ...,bus= */
    uint32_t cr1, cr2, oar1, oar2, timingr, timeoutr, isr, pecr, rxdr, txdr;
    bool active;                 /* START sent, STOP not yet */
    bool reading;
    uint32_t count;              /* bytes moved in this NBYTES window */
    /* What the firmware did on the bus since the emulator started: for
     * whoever looks from outside, and no state of the chip's */
    uint64_t calls, unanswered;  /* addresses called; those nobody took */
    uint8_t last;                /* the last address called, */
    bool last_read, last_taken;  /* to read or to write; and whether taken */
    /* ... and every part of every transfer, for a board that records them
     * (NULL: none does) */
    void (*tap)(void *opaque, WB55I2cTap what, uint8_t value, bool ok);
    void *tap_opaque;
    bool tap_open;               /* a part was told of, and not yet its end */
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
    uint16_t latched;            /* channels owed a beat by a request edge */
    uint64_t pulse;              /* requests that are events (timers), not levels */
    uint16_t served[64];         /* per request: channels of its last edge */
    bool in_service;
    bool dirty;
};

/* have the channels that served request `req` last time been given more
 * to do (enabled, count left)?  A timer waits for that before its next
 * update when the firmware re-arms normal-mode DMA from an ISR */
bool wb55_dma_ready_for(WB55DmaState *s, int req);
/* a request that signals events (a timer's update): one beat per channel
 * each time it rises, however long it stays up */
void wb55_dma_set_pulse(WB55DmaState *s, int req);

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
    bool stamped;                /* the next CYCCNT read gives stamp_ns's count */
    int64_t stamp_ns;
    int64_t read_ns;             /* when CYCCNT was last read */
};
/* The next read of CYCCNT returns its value at virtual time "when", as if
 * the CPU read it then: an ISR that times edges in software reads the
 * edge's own time, as on silicon where it runs within a fraction of a
 * microsecond, not the emulated interrupt's latency. */
void wb55_dwt_stamp_next(WB55DwtState *s, int64_t when);
/* The virtual time of the last read of CYCCNT: where a busy-wait of the
 * firmware's ended, when the register write after it came late. */
int64_t wb55_dwt_last_read(WB55DwtState *s);

#define TYPE_WB55_DBGMCU "stm32wb55-dbgmcu"
OBJECT_DECLARE_SIMPLE_TYPE(WB55DbgmcuState, WB55_DBGMCU)
struct WB55DbgmcuState {
    SysBusDevice parent_obj;
    MemoryRegion mmio;
    uint32_t regs[0x54 / 4];
};

/* ------------------------------------------------- TIM (general purpose) */
#define TYPE_WB55_TIM "stm32wb55-tim"
OBJECT_DECLARE_SIMPLE_TYPE(WB55TimState, WB55_TIM)
struct WB55TimState {
    SysBusDevice parent_obj;
    MemoryRegion mmio;
    qemu_irq irq;                /* any interrupt (TIM2's vector) */
    qemu_irq irq_up, irq_cc, irq_trg, irq_brk;  /* TIM1's four vectors */
    Clock *clk;
    QEMUTimer *timer;
    uint32_t width;              /* 16 or 32 bit counter */
    uint32_t rcr_bits;           /* repetition counter width, 0: none */
    bool batch_dma;              /* DMA-only updates may be replayed late */
    uint32_t rep;                /* repetition count in effect (from RCR) */
    uint32_t regs[0x70 / 4];
    bool running;
    uint32_t cnt_base;           /* CNT at base_ns */
    int64_t base_ns;
    int64_t checked_ns;          /* event flags are current up to here */
    int64_t now_override;        /* processing an update: its exact time */
    bool ti_level[4];
    uint8_t cap_unread;          /* captured, CCRx not read yet (bitmask) */
    int64_t cap_ns[4];           /* when each channel last captured */
    NotifierList cap_read;       /* the firmware read the last pending CCRx */
    bool oc_level[4];            /* output compare pins (OCxREF) */
    qemu_irq dma_up;             /* update DMA request (DMAMUX TIMx_UP) */
    bool dma_up_pending;         /* raised, not yet acknowledged */
    bool in_updates;             /* tim_run_updates() is replaying updates */
    uint64_t ext_hz;             /* clock on the ETR pin (0: none) */
    /* board hook: an output compare pin changed at an exact time (times can
     * be ahead of virtual time: a PWM period is reported as it starts) */
    void (*oc_changed)(void *opaque, int ch, bool level, int64_t when);
    void *oc_opaque;
    /* board hook: an update event at its exact time, after its DMA (only
     * while updates are replayed one by one: UDE or toggle mode, or UG) */
    void (*update_hook)(void *opaque, int64_t when);
    void *update_opaque;
    /* board hook: after every register write and after reset */
    void (*changed)(void *opaque);
    void *changed_opaque;
    /* SoC hook: is the update DMA re-armed? (see wb55_dma_ready_for) */
    bool (*dma_ready)(void *opaque);
    void *dma_ready_opaque;
    bool dma_wait;               /* an update waits for the DMA */
};
/* an edge on input TI1..TI4 (ti 0..3) at virtual time `when` */
void wb55_tim_input(WB55TimState *s, int ti, bool level, int64_t when);
/* the clock on the ETR pin, for external clock modes (0: none) */
void wb55_tim_set_ext_clock(WB55TimState *s, uint64_t hz);
/* has the firmware still to service a capture interrupt? */
bool wb55_tim_capture_pending(WB55TimState *s);

/* ------------------------------------------------------- COMP1 / COMP2 */
#define TYPE_WB55_COMP "stm32wb55-comp"
OBJECT_DECLARE_SIMPLE_TYPE(WB55CompState, WB55_COMP)
struct WB55CompState {
    SysBusDevice parent_obj;
    MemoryRegion mmio;
    uint32_t csr[2];
    bool inp[2];                 /* analog: input+ above input- */
    bool out[2];
    qemu_irq exti[2];            /* EXTI lines 20 (COMP1) and 21 (COMP2) */
    /* SoC hook: an output changed at an exact virtual time */
    void (*out_changed)(void *opaque, int n, bool level, int64_t when);
    void *out_opaque;
    /* board hook: comparator n was enabled or disabled */
    void (*en_changed)(void *opaque, int n, bool en);
    void *en_opaque;
    /* board hook: the CPU read comparator n's CSR (its VALUE): an ISR
     * timing edges in software has taken this one */
    void (*val_read)(void *opaque, int n);
    void *val_opaque;
};
/* the board's analog side: is comparator n's input+ above input-? */
void wb55_comp_input(WB55CompState *s, int n, bool above, int64_t when);

/* ---------------------------------------------------------------- ADC1 */
/*
 * The chip's analog side: its supply, its internal reference, its
 * temperature and its temperature sensor's output at t degrees C.  The ADC
 * model converts these, and the factory's values in the system memory, which
 * the firmware turns such a result into volts and degrees with, are made of
 * the same numbers (WB55_ADC_CAL: a 12-bit result, rounded).
 */
#define WB55_VDD_MV         3300u
#define WB55_VREFINT_UV     1212000u
#define WB55_CHIP_DEGREES   25
#define WB55_TS_UV(t)       ((uint32_t)(760000 + 2500 * ((t) - 30)))
#define WB55_ADC_CAL(uv, ref_mv) \
    ((uint16_t)(((uint64_t)(uv) * 4096 + (ref_mv) * 500u) / ((ref_mv) * 1000u)))
#define WB55_TS_CAL1_ADDR     0x1FFF75A8u   /* the sensor at 30 C, VREF+ 3.0 V */
#define WB55_VREFINT_CAL_ADDR 0x1FFF75AAu   /* VREFINT with VREF+ at 3.6 V */
#define WB55_TS_CAL2_ADDR     0x1FFF75CAu   /* the sensor at 130 C, 3.0 V */

#define TYPE_WB55_ADC "stm32wb55-adc"
OBJECT_DECLARE_SIMPLE_TYPE(WB55AdcState, WB55_ADC)
struct WB55AdcState {
    SysBusDevice parent_obj;
    MemoryRegion mmio;
    uint32_t regs[0x400 / 4];    /* ADC1 and, from 0x300, the common block */
    bool warned;
    /* SoC hook: the voltage on VREF+ in mV */
    uint32_t (*vref)(void *opaque);
    void *vref_opaque;
    /* board hook: the voltage in mV on the pin of a channel (1 to 16) */
    uint32_t (*input)(void *opaque, int channel);
    void *input_opaque;
    uint64_t conversions;        /* since the emulator started */
    uint8_t last_channel;        /* the last one: its channel, */
    uint16_t last_data;          /* its result, */
    uint32_t last_uv;            /* and the voltage it was of */
};
bool wb55_adc_enabled(WB55AdcState *s);

/* Read/write register file used for blocks that only need readback. */
#define TYPE_WB55_REGFILE "stm32wb55-regfile"
OBJECT_DECLARE_SIMPLE_TYPE(WB55RegfileState, WB55_REGFILE)
struct WB55RegfileState {
    SysBusDevice parent_obj;
    MemoryRegion mmio;
    char *name;
    uint32_t size;
    uint32_t *regs;
    uint32_t nregs;              /* size / 4 */
    uint32_t kind;               /* WB55_RF_* special behaviour */
    bool warned;
    /* board hook, called after every write and after reset (e.g. TIM16 -> speaker) */
    void (*changed)(void *opaque);
    void *changed_opaque;
};
enum { WB55_RF_PLAIN = 0, WB55_RF_TIM, WB55_RF_CRC };

/* ----------------------------------------------------------------- USB */
#include "chardev/char-fe.h"
#include "qemu/fifo8.h"
#include "qemu/queue.h"

#define TYPE_WB55_USB "stm32wb55-usb"
OBJECT_DECLARE_SIMPLE_TYPE(WB55UsbState, WB55_USB)

/* a URB from a USB/IP client (the host kernel), being executed on the bus */
typedef struct WB55Urb {
    uint32_t seqnum, flags, len, start_frame, npackets;
    uint8_t ep, dir;             /* dir: 1 = IN */
    uint8_t setup[8];
    uint8_t *data;               /* OUT payload, or IN buffer */
    uint32_t done;
    bool started, zlp_done, unlinked;
    QTAILQ_ENTRY(WB55Urb) next;
} WB55Urb;

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
    int64_t ep_armed[8];         /* the CPU last (re)armed EPn, or it last moved data */
    int64_t ep_done[8];          /* EPn's last transaction, until the CPU re-arms it */
    int64_t ep_look[8];          /* when the host next looks for an idle CPU */
    int64_t ep_idle[8];          /* a re-arm to the CPU's idling, lately */
    bool ep_timed[8];            /* that look is the planned one */
    int64_t bus_last;            /* the last bulk/interrupt transaction */
    int64_t turn_ns;             /* the firmware's turnaround, averaged */

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
    bool hup;                   /* the port was closed: DTR is to drop */
    bool stale;                 /* the device may hold a packet for who left */
    int64_t hup_by, reopen_at;
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

    /* USB/IP export: the PC's kernel becomes the host (qFlipper & co.) */
    CharFrontend usbip_chr;
    bool usbip_open, usbip_imported;
    GByteArray *usbip_rx, *usbip_tx;
    guint usbip_watch;
    QTAILQ_HEAD(, WB55Urb) urbs;
    WB55Urb *ctl_urb;            /* the URB the control engine is serving */
    uint16_t ep_mps[2][16];      /* [dir][ep] from the configuration */
    uint8_t nif, if_class[16][3];

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
    WB55AesState aes1, aes2;         /* AES1: CPU2 can lend it a key */
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
    WB55TimState tim1;               /* RFID carrier, IR transmit */
    WB55TimState tim2;               /* IR receive, Sub-GHz async RX capture */
    WB55TimState tim17;              /* NFC block-TX timer */
    WB55CompState comp;              /* COMP1: 125 kHz RFID demodulator */
    WB55AdcState adc;                /* ADC1: six pins of the GPIO header */
    Stm32l4x5GpioState gpio[WB55_NUM_PORTS];
    Stm32l4x5UsartBaseState usart1;
    Stm32l4x5UsartBaseState lpuart1;
    OrIRQState exti_or[3];           /* EXTI9_5, EXTI15_10, COMP */
    OrIRQState tim17_or;             /* TIM1_TRG_COM_TIM17 vector */

    MemoryRegion sram1, sram2a, sram2b;
    MemoryRegion sram2a_alias, sram2b_alias;
    MemoryRegion boot_flash, boot_sysmem, boot_sram1;

    WB55RegfileState *tim16;         /* speaker PWM (TIM16 CH1 on PB8) */

    qemu_irq *gpio_tap;
    qemu_irq gpio_out[WB55_PINS];    /* pin levels for board devices */
    /* board hooks for bit-banged buses, from an overlay on each GPIO port
     * that passes every access through: the CPU read IDR (its value
     * already taken: it has sampled the pins), or wrote MODER, OTYPER,
     * PUPDR, ODR, BSRR or BRR (a pin's level may have changed) */
    void (*idr_read)(void *opaque, int port);
    void (*out_written)(void *opaque, int port, hwaddr reg, uint32_t data);
    void *gpio_hook_opaque;
    struct WB55GpioTap {
        struct WB55SocState *soc;
        int port;
        MemoryRegion mr;
    } gpio_tap_mr[WB55_NUM_PORTS];
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
