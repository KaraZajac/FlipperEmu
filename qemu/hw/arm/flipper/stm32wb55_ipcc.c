/*
 * STM32WB55 IPCC (RM0434 chapter 38) + a behavioural CPU2
 *
 * CPU2 runs ST's closed radio firmware. It is not emulated; instead this model
 * behaves like a real device's CPU2: the Firmware Upgrade Service (FUS) and,
 * as on a Flipper from the factory, ST's BLE stack, which it starts with
 * (stm32wb55_ble.c).  With the machine's radio-stack=none it is a FUS with no
 * wireless stack installed, a state the Flipper firmware handles ("Stack isn't
 * installed!").  The FUS keeps the application's AES keys, the Flipper's
 * "secure enclave": the four key commands are decoded here and carried out in
 * stm32wb55_fus.c; the stack takes them as the FUS does.
 * A dead CPU2 is NOT an option: furi_hal_crypto_enclave_load_key() leaks its
 * mutex when furi_hal_bt_is_alive() is false and the desktop then deadlocks
 * (targets/f7/furi_hal/furi_hal_crypto.c, firmware 1.5.1).
 *
 * Transport layer facts (lib/stm32wb_copro .../tl/tl_mbox.c, shci_tl.c,
 * mbox_def.h, tl.h, targets/f7/ble_glue/hw_ipcc.c, ble_glue.c):
 *   - MB_RefTable_t lives at SRAM2A + 4*FLASH->IPCCBR.IPCCDBA
 *   - ref[0] -> device info table; FUS writes MB_FUS_DeviceInfoTable_t there
 *   - ref[3] -> MB_SysTable_t {pcmd_buffer, sys_queue}
 *   - ref[4] -> MB_MemManagerTable_t {spare_ble, spare_sys, ..., free_queue}
 *   - lists are circular doubly linked tListNode {next, prev}
 *   - system command: CPU1 fills pcmd_buffer (TL_CmdPacket_t), sets C1->C2
 *     channel 2 and spins until CPU2 clears it; the response is a
 *     TL_EvtSerial_t written at the start of the same buffer (shci_send()
 *     copies it from offset 0)
 *   - async event: CPU2 links a TL_EvtPacket_t into sys_queue and sets C2->C1
 *     channel 2; CPU1 returns buffers on FreeBufQueue + C1->C2 channel 4
 *   - the stack: its device info table is MB_DeviceInfoTable_t (SafeBoot,
 *     FUS and wireless firmware's versions; SHCI_GetWirelessFwInfo() tells
 *     the two kinds apart by the FUS's keyword), its ready event says
 *     WIRELESS_FW_RUNNING, and BLE commands come on channel 1
 *   - SHCI_C2_Reinit: CPU2 restarts its firmware when CPU1 next enables the
 *     IPCC (HW_IPCC_Enable(): SEV and C2BOOT set again); the PWR model
 *     reports each such write
 *
 * Copyright (c) 2026, Kara Zajac
 * SPDX-License-Identifier: BSD-3-Clause
 */
#include "qemu/osdep.h"
#include "qemu/log.h"
#include "qemu/error-report.h"
#include "hw/core/irq.h"
#include "hw/core/qdev-properties.h"
#include "hw/core/qdev-properties-system.h"
#include "qapi/error.h"
#include "migration/vmstate.h"
#include "system/runstate.h"
#include "qobject/qdict.h"
#include "qobject/qjson.h"
#include "wb55.h"
#include "wb55_tl.h"
#include "flipper.h"

#define C1CR      0x00
#define C1MR      0x04
#define C1SCR     0x08
#define C1TOC2SR  0x0C
#define C2CR      0x10
#define C2MR      0x14
#define C2SCR     0x18
#define C2TOC1SR  0x1C

#define CH(n)     (1u << ((n) - 1))
#define CH_MASK   0x3Fu

/* SHCI */
#define SHCI_OPCODE_FUS_GET_STATE      0xFC52
#define SHCI_OPCODE_C2_BLE_INIT        0xFC66
#define SHCI_OPCODE_C2_FLASH_ERASE     0xFC69
#define SHCI_OPCODE_C2_REINIT          0xFC6F
#define SHCI_OPCODE_C2_FLASH_CONTROL   0xFC73
#define SHCI_OPCODE_C2_CONFIG          0xFC75
#define SHCI_OPCODE_FUS_STORE_USR_KEY  0xFC58
#define SHCI_OPCODE_FUS_LOAD_USR_KEY   0xFC59
#define SHCI_OPCODE_FUS_LOCK_USR_KEY   0xFC5D
#define SHCI_OPCODE_FUS_UNLOAD_USR_KEY 0xFC5E
#define SHCI_OPCODE_C2_SET_SYSTEM_CLOCK 0xFC79
#define SHCI_EVTCODE                   0xFF
#define SHCI_SUB_EVT_CODE_READY        0x9200
#define WIRELESS_FW_RUNNING            0x00
#define FUS_FW_RUNNING                 0x01
#define SHCI_FUS_CMD_NOT_SUPPORTED     0xFF
#define TL_BLEEVT_CC_OPCODE            0x0E
#define FUS_DEVICE_INFO_VALID          0xA94656B9u
#define SHCI_UNKNOWN_CMD               0x01
#define SAFE_BOOT_VERSION              0x01000300
/* the stack of the Flipper's update packages: 1.20.0, branch 0, release
 * type 2, BLE Light (their manifests' "Radio version: 01 14 00 00 02 03"),
 * 116956 bytes (29 sectors of 4 KB) */
#define STACK_DEFAULT                  "1.20.0"
#define STACK_RELEASE_TYPE             2
#define STACK_TYPE_BLE_FULL            0x01
#define STACK_TYPE_BLE_LIGHT           0x03
#define STACK_TYPE_BLE_FULL_EXT_ADV    0x06
#define STACK_FLASH_SECTORS            29

/*
 * The kinds of ST's BLE stack that the model is, and their 1.20.0 images as
 * their FUS footers have them (stm32wb_copro v1.20.0): the flash that the
 * image takes, in 4 KB sectors, and what it leaves CPU1 of SRAM2a and
 * SRAM2b, in KB, which the FUS puts into the option bytes (SBRSA, SNBRSA).
 * The footer's info words are ST's placeholders: its sizes are what tells
 * the three apart (the running stack reports its type).  The model is the
 * same BLE stack for all three; what it takes from the kind is the type
 * that it reports, the flash, and SRAM2.
 */
typedef struct WB55StackKind {
    const char *name;            /* in radio-stack= and in `status` */
    const char *words;
    uint8_t type;                /* INFO_STACK_TYPE_BLE_* */
    uint8_t sectors, sram2a, sram2b;
} WB55StackKind;

static const WB55StackKind stack_kinds[] = {
    { "light", "BLE Light", STACK_TYPE_BLE_LIGHT, 29, 18, 11 },
    { "full", "BLE Full", STACK_TYPE_BLE_FULL, 38, 10, 15 },
    { "full-ext", "BLE Full extended", STACK_TYPE_BLE_FULL_EXT_ADV, 47, 2, 10 },
};

/* what CPU1 keeps of SRAM2 on a Flipper from the factory (Light's) */
#define C2_SRAM2A_FACTORY              18
#define C2_SRAM2B_FACTORY              11

static const WB55StackKind *stack_kind_named(const char *name)
{
    for (int i = 0; i < ARRAY_SIZE(stack_kinds); i++) {
        if (!strcmp(stack_kinds[i].name, name)) {
            return &stack_kinds[i];
        }
    }
    return NULL;
}

static const WB55StackKind *stack_kind_of_type(uint8_t type)
{
    for (int i = 0; i < ARRAY_SIZE(stack_kinds); i++) {
        if (stack_kinds[i].type == type) {
            return &stack_kinds[i];
        }
    }
    return NULL;
}

static const WB55StackKind *stack_kind_of_image(uint8_t sectors,
                                                uint8_t sram2a, uint8_t sram2b)
{
    for (int i = 0; i < ARRAY_SIZE(stack_kinds); i++) {
        const WB55StackKind *k = &stack_kinds[i];

        if (k->sectors == sectors && k->sram2a == sram2a &&
            k->sram2b == sram2b) {
            return k;
        }
    }
    return NULL;
}

/* The FUS's own operations on the wireless stack (AN5185; shci.h) */
#define SHCI_OPCODE_FUS_FW_UPGRADE     0xFC54
#define SHCI_OPCODE_FUS_FW_DELETE      0xFC55
#define SHCI_OPCODE_FUS_START_WS       0xFC5A
#define FUS_STATE_IDLE                 0x00
#define FUS_STATE_FW_UPGRD_ONGOING     0x10
#define FUS_STATE_SERVICE_ONGOING      0x30
#define FUS_STATE_ERROR                0xFF
#define FUS_ERROR_IMG_NOT_FOUND        0x01
#define FUS_ERROR_IMG_CORRUPT          0x02
/* A wireless stack's image, as ST builds them (fbt's coprobin.py): at its
 * end ST's signature footer, and before the signature the FUS footer with
 * the stack's version.  Each footer is 20 bytes, its last 8 the version
 * (build and branch, sub, minor, major) and a magic word. */
#define IMG_FOOTER                     20
#define IMG_MAGIC_SIG_ST               0xD3A12C5Eu
#define IMG_MAGIC_SIG_CUSTOMER         0xE2B51D4Au
#define IMG_MAGIC_STACK                0x23372991u
/* where the FUS begins in the flash: a stack image is written below it */
#define FUS_BASE                       0x080F4000u
#define FLASH_BASE_ADDR                0x08000000u
/* how long an operation takes, in the machine's time (seconds on a chip) */
#define FUS_SWITCH_MS                  20
#define FUS_DELETE_MS                  400
#define FUS_UPGRADE_MS                 1500
/* ... and from the end of a delete or an install to the FUS's restart of
 * the MCU, in which FUS_GET_STATE says it is done: the updater shows "Core
 * 2 busy" there and waits for the restart (update_task_wait_for_restart);
 * old MoonOS's devices died with that on the screen */
#define FUS_RESTART_MS                 300

hwaddr wb55_ipcc_ref_table(WB55IpccState *s)
{
    uint32_t ipccdba = 0;
    if (s->flash) {
        ipccdba = s->flash->ob[13] & 0x3FFF;    /* FLASH->IPCCBR */
    }
    return WB55_SRAM2A_BASE + (ipccdba << 2);
}

void wb55_ipcc_update_irq(WB55IpccState *s)
{
    uint32_t rx = s->c2toc1sr & ~s->c1mr & CH_MASK;
    uint32_t tx = ~s->c1toc2sr & ~(s->c1mr >> 16) & CH_MASK;

    qemu_set_irq(s->irq_rx, (s->c1cr & 1) && rx);
    qemu_set_irq(s->irq_tx, (s->c1cr & (1u << 16)) && tx);
}

/* ------------------------------------------------------------- CPU2 (FUS) */

/*
 * The stack checks that each buffer CPU1 gives it lies wholly in the SRAM
 * that is not CPU2's.  1.20.0 hard-faults on one that does not; 1.21.0
 * reports a "security attack" instead (ST's release notes, BLE 190951:
 * "Manage correctly the case where the buffers provided to the CPU2 are not
 * in the unsecure SRAM"; ST's community, on the HCI stacks' ACL buffer: the
 * check is there since 1.16.0).  CPU2's hard fault leaves AN5289's record at
 * SRAM2A_BASE (a magic word, its PC, LR and SP), over the start of CPU1's
 * reference table, and CPU2 then loops: it answers nothing more.  The
 * firmware looks for that record every 5 s and before each command it sends
 * (ble_glue.c: "ST(R) Copro(R) HardFault").  Which buffers the stack checks,
 * and when, ST does not say: the model checks the transport layer's at
 * SHCI_C2_BLE_Init (where ST's customer saw it) and the NVM buffer at
 * SHCI_C2_Config.  The PC, LR and SP are the model's (the stack's base, 0,
 * the top of SRAM2b).
 */
#define C2_HARDFAULT_MAGIC             0x1170FD0Fu

static void cpu2_hardfault(WB55IpccState *s)
{
    s->c2_fault = true;
    tl_wr32(WB55_SRAM2A_BASE + 0, C2_HARDFAULT_MAGIC);
    tl_wr32(WB55_SRAM2A_BASE + 4, FUS_BASE - s->stack_sectors * 4096);
    tl_wr32(WB55_SRAM2A_BASE + 8, 0);
    tl_wr32(WB55_SRAM2A_BASE + 12, WB55_SRAM2B_BASE + 0x8000 - 0x20);
    warn_report("flipper-cpu2: the stack hard-faults: %s (stack 1.20.0 "
                "checks the buffers that CPU1 gives it); CPU2 does nothing "
                "more until a reset", (char *)s->c2_fault_why);
}

bool wb55_c2_buffer_ok(WB55IpccState *s, const char *what, hwaddr addr,
                       unsigned len)
{
    if (s->c2_fault || !addr || !len || !s->flash ||
        !wb55_flash_is_c2_ram(s->flash, addr, len)) {
        return !s->c2_fault;
    }
    snprintf((char *)s->c2_fault_why, sizeof(s->c2_fault_why), "%s at 0x%08"
             HWADDR_PRIx " to 0x%08" HWADDR_PRIx " reaches into the SRAM2 "
             "that it keeps for CPU2", what, addr, addr + len - 1);
    cpu2_hardfault(s);
    return false;
}

/* MemorySize, as the stack's InfoTable has it (shci.h INFO_SIZE_*) */
static uint32_t stack_memory_size(WB55IpccState *s)
{
    return s->stack_sectors | (uint32_t)s->c2_sram2a << 16 |
           (uint32_t)s->c2_sram2b << 24;
}

static void cpu2_write_device_info(WB55IpccState *s, hwaddr ref)
{
    hwaddr info = tl_rd32(ref + 0);        /* ref->p_device_info_table */
    if (!info) {
        return;
    }
    /* MB_FUS_DeviceInfoTable_t */
    tl_wr32(info + 0, FUS_DEVICE_INFO_VALID);
    tl_wr32(info + 4, 0);                  /* Reserved1, LastFusActiveState,
                                              LastWirelessStackState,
                                              CurrentWirelessStackType */
    tl_wr32(info + 8, SAFE_BOOT_VERSION);  /* SafeBootVersion */
    tl_wr32(info + 12, s->fus_version);    /* FusVersion */
    tl_wr32(info + 16, 0x00000000);        /* FusMemorySize */
    /* the stack that is installed, which the FUS does not run: the
     * updater compares its version with the package's (0.0: none) */
    tl_wr32(info + 20, s->stack_version);  /* WirelessStackVersion */
    tl_wr32(info + 24, s->stack_version ? stack_memory_size(s) : 0);
    tl_wr32(info + 28, s->stack_version ? s->stack_type : 0);
    tl_wr32(info + 32, 0);                 /* WirelessFirmwareThreadInfo */
    tl_wr32(info + 36, 0);                 /* Reserved2 */
    tl_wr32(info + 40, (uint32_t)s->uid64);
    tl_wr32(info + 44, (uint32_t)(s->uid64 >> 32));
    tl_wr8(info + 48, 0x95);               /* DeviceId 0x6495 */
    tl_wr8(info + 49, 0x64);
}

/* MB_DeviceInfoTable_t: what a CPU2 that runs the stack writes */
static void cpu2_write_stack_info(WB55IpccState *s, hwaddr ref)
{
    hwaddr info = tl_rd32(ref + TL_REF_DEVICE_INFO);
    if (!info) {
        return;
    }
    /* SafeBootInfoTable: Version */
    tl_wr32(info + 0, SAFE_BOOT_VERSION);
    /* FusInfoTable: Version, MemorySize, FusInfo */
    tl_wr32(info + 4, s->fus_version);
    tl_wr32(info + 8, 0);
    tl_wr32(info + 12, 0);
    /* WirelessFwInfoTable: Version, MemorySize (shci.h: the flash that
     * the stack takes in 4 KB sectors, SRAM1, and what it leaves CPU1 of
     * SRAM2a and SRAM2b: the image footer's word), InfoStack, Reserved */
    tl_wr32(info + 16, s->stack_version);
    tl_wr32(info + 20, stack_memory_size(s));
    tl_wr32(info + 24, s->stack_type);
    tl_wr32(info + 28, 0);
}

static void cpu2_boot(void *opaque)
{
    WB55IpccState *s = opaque;
    hwaddr ref = wb55_ipcc_ref_table(s);
    hwaddr sys = tl_rd32(ref + TL_REF_SYS);
    hwaddr mm = tl_rd32(ref + TL_REF_MEM_MANAGER);
    /* SHCI_C2_Ready_Evt_t: the subevent code, and what runs */
    uint8_t ready[3] = { SHCI_SUB_EVT_CODE_READY & 0xFF,
                         SHCI_SUB_EVT_CODE_READY >> 8, FUS_FW_RUNNING };

    if (!sys || !mm) {
        warn_report("flipper-cpu2: transport tables not set up at C2BOOT");
        return;
    }
    s->c2_booted = true;
    /* The channels as a reset IPCC has them: CPU1 switched its clock off and
     * on around a restart (furi_hal_bus_disable()/enable(), IPCCRST on the
     * chip), and what it sent while CPU2 was down is gone */
    s->c1toc2sr = s->c2toc1sr = 0;
    wb55_ipcc_update_irq(s);
    wb55_ble_reset(s);
    /* the FUS starts the stack, if there is one and it was not told to stay
     * (a second FUS_GET_STATE under the stack, until FUS_START_WS) */
    s->c2_stack = s->stack_version != 0 && !s->c2_fus_boot;
    s->fus_asked = 0;
    if (s->c2_stack) {
        cpu2_write_stack_info(s, ref);
        ready[2] = WIRELESS_FW_RUNNING;
    } else {
        cpu2_write_device_info(s, ref);
    }
    wb55_ble_post(s, WB55_BLE_BUF_SYS, SHCI_EVTCODE, ready, sizeof(ready));
}

static void cpu2_c2boot(void *opaque, int n, int level)
{
    WB55IpccState *s = opaque;

    if (!level || s->c2_booted) {
        return;
    }
    if (s->c2_reinit) {
        /*
         * A restart after C2_REINIT: at once, while CPU1 still holds the
         * core-2 lock of furi_hal_bt_reinit() (furi_hal_bt.c).  With any
         * delay, a key load that waits for that lock gets it before the
         * ready event and finds BT not alive, and
         * furi_hal_crypto_enclave_load_key() then keeps its mutex for good:
         * the next key load waits forever (an `info device` at the wrong
         * moment did, and the CLI with it).
         */
        s->c2_reinit = false;
        timer_del(s->boot_timer);
        cpu2_boot(s);
        return;
    }
    /* FUS start-up takes a few ms on silicon (and CPU1's event thread does
     * not run yet: a ready event that came at once would not be heard) */
    timer_mod(s->boot_timer,
              qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL) + 5 * SCALE_MS);
}

/* ------------------------------------------- the FUS and the wireless stack */
/*
 * The FUS installs and deletes the wireless stack, and starts it or stays.
 * What it keeps of that is in CPU2's flash on a chip, and in a file on the
 * host here ("radio-state"; the launcher's devices/NAME/radio.state), one
 * line each:
 *
 *   stack 1.20.0 branch 0 release 2 type 3 sectors 29     (or: stack none)
 *   boot stack                                            (or: boot fus)
 *
 * The updater's radio stage drives it (update_task_worker_flasher.c): a
 * stack of another version has CPU2 restart into the FUS (a second
 * FUS_GET_STATE under the stack), FUS_FW_DELETE deletes it, CPU1 writes the
 * package's image into its flash and FUS_FW_UPGRADE installs it from there,
 * FUS_START_WS starts it.  Each of these ends in a restart of the whole MCU,
 * for which the updater waits.
 */

static void radio_save(WB55IpccState *s)
{
    g_autoptr(GString) g = g_string_new(
        "# CPU2 of this Flipper: its wireless stack, and whether it stays in\n"
        "# its FUS at its next start (the emulator writes this)\n");
    g_autoptr(GError) err = NULL;

    if (!s->radio_file) {
        return;
    }
    if (s->stack_version) {
        g_string_append_printf(g, "stack %u.%u.%u branch %u release %u type %u "
                               "sectors %u\n", s->stack_version >> 24,
                               (s->stack_version >> 16) & 0xFF,
                               (s->stack_version >> 8) & 0xFF,
                               (s->stack_version >> 4) & 0x0F,
                               s->stack_version & 0x0F, s->stack_type,
                               s->stack_sectors);
    } else {
        g_string_append(g, "stack none\n");
    }
    /* what the FUS put into the option bytes (it stays when a stack is
     * deleted, until the next is installed) */
    g_string_append_printf(g, "sram2 %u %u\n", s->c2_sram2a, s->c2_sram2b);
    g_string_append_printf(g, "boot %s\n", s->c2_fus_boot ? "fus" : "stack");
    if (!g_file_set_contents(s->radio_file, g->str, g->len, &err)) {
        warn_report("flipper-cpu2: %s: %s", s->radio_file, err->message);
    }
}

/* The file of before, if there is one: true when it said something */
static bool radio_load(WB55IpccState *s)
{
    g_autofree char *text = NULL;
    g_auto(GStrv) lines = NULL;
    bool said = false;

    if (!s->radio_file || !g_file_get_contents(s->radio_file, &text, NULL,
                                               NULL)) {
        return false;
    }
    lines = g_strsplit(text, "\n", -1);
    for (int i = 0; lines[i]; i++) {
        g_auto(GStrv) w = g_strsplit_set(g_strstrip(lines[i]), " \t", -1);
        unsigned major, minor, sub;
        char end;

        if (!w[0] || w[0][0] == '#' || !w[1]) {
            continue;
        }
        if (!strcmp(w[0], "stack") && !strcmp(w[1], "none")) {
            s->stack_version = 0;
            s->stack_type = 0;
            s->stack_sectors = 0;
            said = true;
        } else if (!strcmp(w[0], "stack") &&
                   sscanf(w[1], "%u.%u.%u%c", &major, &minor, &sub, &end) == 3 &&
                   major && major < 256 && minor < 256 && sub < 256) {
            unsigned branch = 0, release = STACK_RELEASE_TYPE;
            unsigned type = STACK_TYPE_BLE_LIGHT, sectors = STACK_FLASH_SECTORS;

            for (int j = 2; w[j] && w[j + 1]; j += 2) {
                unsigned v = strtoul(w[j + 1], NULL, 10);

                if (!strcmp(w[j], "branch")) {
                    branch = v & 0x0F;
                } else if (!strcmp(w[j], "release")) {
                    release = v & 0x0F;
                } else if (!strcmp(w[j], "type")) {
                    type = v & 0xFF;
                } else if (!strcmp(w[j], "sectors")) {
                    sectors = v & 0xFF;
                }
            }
            s->stack_version = major << 24 | minor << 16 | sub << 8 |
                               branch << 4 | release;
            s->stack_type = type;
            s->stack_sectors = sectors;
            said = true;
        } else if (!strcmp(w[0], "sram2") && w[2]) {
            unsigned a = strtoul(w[1], NULL, 10), b = strtoul(w[2], NULL, 10);

            if (a < 32 && b < 32) {
                s->c2_sram2a = a;
                s->c2_sram2b = b;
            }
        } else if (!strcmp(w[0], "boot")) {
            s->c2_fus_boot = !strcmp(w[1], "fus");
        }
    }
    return said;
}

/* The secure area starts at the stack, or at the FUS when there is none:
 * CPU1 may write below it (the updater checks that its image fits).  CPU2
 * boots there (SBRV, a word address), and SRAM2 is CPU2's from where the
 * last stack's footer said */
static void fus_sfsa(WB55IpccState *s)
{
    if (s->flash) {
        uint8_t page = (FUS_BASE - FLASH_BASE_ADDR) / 4096 -
                       (s->stack_version ? s->stack_sectors : 0);

        wb55_flash_set_sfsa(s->flash, page);
        wb55_flash_set_c2_sram(s->flash, s->c2_sram2a, s->c2_sram2b,
                               page * 4096 / 4);
    }
}

/* An operation of the FUS's, done in `ms` of the machine's time */
static void fus_after(WB55IpccState *s, uint8_t op, int ms)
{
    s->fus_op = op;
    timer_mod(s->fus_timer,
              qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL) + ms * SCALE_MS);
}

/*
 * FUS_FW_UPGRADE: the image that CPU1 wrote into its flash at `src`, up to
 * where the FUS begins (the updater puts it so that it ends in the last
 * page below it; the rest of that page is erased).  The FUS checks it and
 * installs it: here its footers are read for the stack's version.  The
 * encrypted body is ST's business; the model runs a stack of that version.
 */
static bool fus_install(WB55IpccState *s)
{
    uint32_t src = s->fus_src, n, last, sig;
    g_autofree uint8_t *img = NULL;
    const uint8_t *foot;

    if (src < FLASH_BASE_ADDR || src >= FUS_BASE - 2 * IMG_FOOTER) {
        s->fus_error = FUS_ERROR_IMG_NOT_FOUND;
        return false;
    }
    n = FUS_BASE - src;
    img = g_malloc(n);
    address_space_read(&address_space_memory, src, WB55_C2_ATTRS,
                       img, n);
    while (n && img[n - 1] == 0xFF) {
        n--;                            /* the erased end of the last page */
    }
    /* The flash is programmed in doublewords: the last one may hold up to
     * seven bytes past the image (what the updater's buffer had there).
     * The image ends with the signature footer's magic word. */
    last = n;
    for (sig = 0; n >= 2 * IMG_FOOTER && n + 8 > last; n--) {
        sig = ldl_le_p(img + n - 4);
        if (sig == IMG_MAGIC_SIG_ST || sig == IMG_MAGIC_SIG_CUSTOMER) {
            break;
        }
    }
    if (sig != IMG_MAGIC_SIG_ST && sig != IMG_MAGIC_SIG_CUSTOMER) {
        s->fus_error = FUS_ERROR_IMG_NOT_FOUND;
        return false;
    }
    foot = img + n - IMG_FOOTER;
    /* the signature: its size in the footer's byte 8, then the footer */
    if (n < 2u * IMG_FOOTER + foot[8]) {
        s->fus_error = FUS_ERROR_IMG_CORRUPT;
        return false;
    }
    foot = img + n - IMG_FOOTER - foot[8] - IMG_FOOTER;
    if (ldl_le_p(foot + 16) != IMG_MAGIC_STACK || !foot[15]) {
        s->fus_error = FUS_ERROR_IMG_CORRUPT;   /* no stack's (a FUS image) */
        return false;
    }
    /* the footer's memory word: the flash that the image takes (sectors),
     * SRAM1 (unused), what it leaves CPU1 of SRAM2a and SRAM2b (KB) */
    uint8_t sectors = foot[8] ? foot[8] : DIV_ROUND_UP(n, 4096);
    const WB55StackKind *k = stack_kind_of_image(sectors, foot[10], foot[11]);

    if (!k) {
        warn_report("flipper-cpu2: FUS_FW_UPGRADE: a stack %u.%u.%u of %u "
                    "sectors that leaves CPU1 %u KB of SRAM2a and %u of "
                    "SRAM2b: no kind that the emulated CPU2 is (BLE Light, "
                    "Full and Full extended 1.20.0)", foot[15], foot[14],
                    foot[13], sectors, foot[10], foot[11]);
        s->fus_error = FUS_ERROR_IMG_CORRUPT;
        return false;
    }
    /* the version: major, minor, sub, then branch and build in one byte,
     * which is the InfoTable word's low byte as it is */
    s->stack_version = foot[15] << 24 | foot[14] << 16 | foot[13] << 8 |
                       foot[12];
    s->stack_type = k->type;
    s->stack_sectors = sectors;
    s->c2_sram2a = foot[10];
    s->c2_sram2b = foot[11];
    info_report("flipper-cpu2: the FUS installed %s %u.%u.%u: the flash "
                "from 0x%08x and SRAM2 from 0x%08x and 0x%08x are CPU2's "
                "after the restart", k->words, foot[15], foot[14], foot[13],
                FUS_BASE - sectors * 4096,
                WB55_SRAM2A_BASE + s->c2_sram2a * 1024,
                WB55_SRAM2B_BASE + s->c2_sram2b * 1024);
    return true;
}

static void fus_operation_done(void *opaque)
{
    WB55IpccState *s = opaque;
    uint8_t op = s->fus_op;

    s->fus_op = WB55_FUS_OP_NONE;
    if (op == WB55_FUS_OP_DELETE) {
        s->stack_version = 0;
        s->stack_type = 0;
        s->stack_sectors = 0;
        fus_sfsa(s);
        radio_save(s);
    } else if (op == WB55_FUS_OP_UPGRADE) {
        if (!fus_install(s)) {
            /* no restart: FUS_GET_STATE has the error for CPU1 */
            warn_report("flipper-cpu2: FUS_FW_UPGRADE: no stack image at "
                        "0x%08x (error 0x%02x)", s->fus_src, s->fus_error);
            return;
        }
        fus_sfsa(s);
        radio_save(s);
    }
    if (op == WB55_FUS_OP_DELETE || op == WB55_FUS_OP_UPGRADE) {
        fus_after(s, WB55_FUS_OP_SWITCH, FUS_RESTART_MS);   /* done, then */
        return;
    }
    /* the FUS restarts the whole MCU, and boots itself or the stack */
    qemu_system_reset_request(SHUTDOWN_CAUSE_GUEST_RESET);
}

static void cpu2_system_command(WB55IpccState *s)
{
    hwaddr sys = tl_rd32(wb55_ipcc_ref_table(s) + TL_REF_SYS);
    hwaddr buf = sys ? tl_rd32(sys + TL_SYS_CMD_BUFFER) : 0;
    uint8_t status = SHCI_UNKNOWN_CMD;
    uint8_t extra[2];
    int nextra = 0;
    uint8_t payload[255];

    if (!buf) {
        return;
    }
    /* TL_CmdPacket_t: header(8) | type(1) | cmdcode(2) | plen(1) | payload */
    uint16_t opcode = tl_rd8(buf + 9) | (tl_rd8(buf + 10) << 8);
    int plen = tl_rd8(buf + 11);

    /* the answer goes over the command: take the command out first */
    address_space_read(&address_space_memory, buf + 12, WB55_C2_ATTRS,
                       payload, plen);

    switch (opcode) {
    case SHCI_OPCODE_FUS_GET_STATE:
        if (s->c2_stack) {
            /* The stack does not know it.  The FUS hears of a second one,
             * and restarts the whole MCU into itself, where it stays
             * (ble_glue_force_c2_mode() counts on that) */
            status = SHCI_FUS_CMD_NOT_SUPPORTED;
            if (++s->fus_asked >= 2 && !s->fus_op) {
                s->c2_fus_boot = true;
                radio_save(s);
                fus_after(s, WB55_FUS_OP_SWITCH, FUS_SWITCH_MS);
            }
            break;
        }
        status = s->fus_error ? FUS_STATE_ERROR
               : s->fus_op == WB55_FUS_OP_UPGRADE ? FUS_STATE_FW_UPGRD_ONGOING
               : s->fus_op == WB55_FUS_OP_DELETE ? FUS_STATE_SERVICE_ONGOING
               : FUS_STATE_IDLE;
        extra[0] = s->fus_error;
        nextra = 1;
        break;
    /* the FUS's work on the stack: each ends in a restart of the MCU */
    case SHCI_OPCODE_FUS_FW_DELETE:
        if (s->c2_stack || s->fus_op) {
            status = s->c2_stack ? SHCI_FUS_CMD_NOT_SUPPORTED : 0x01;
            break;
        }
        status = 0x00;
        s->fus_error = 0;
        fus_after(s, WB55_FUS_OP_DELETE, FUS_DELETE_MS);
        break;
    case SHCI_OPCODE_FUS_FW_UPGRADE:
        if (s->c2_stack || s->fus_op) {
            status = s->c2_stack ? SHCI_FUS_CMD_NOT_SUPPORTED : 0x01;
            break;
        }
        /* the image's address (and where to put it, which is the FUS's own
         * business when it is 0, as the Flipper sends it) */
        s->fus_src = plen >= 4 ? ldl_le_p(payload) : 0;
        status = 0x00;
        s->fus_error = 0;
        fus_after(s, WB55_FUS_OP_UPGRADE, FUS_UPGRADE_MS);
        break;
    case SHCI_OPCODE_FUS_START_WS:
        if (s->c2_stack || s->fus_op || !s->stack_version) {
            status = s->c2_stack ? SHCI_FUS_CMD_NOT_SUPPORTED : 0x01;
            break;
        }
        status = 0x00;
        s->c2_fus_boot = false;
        radio_save(s);
        fus_after(s, WB55_FUS_OP_SWITCH, FUS_SWITCH_MS);
        break;
    /* the stack's own */
    case SHCI_OPCODE_C2_BLE_INIT:
        status = wb55_ble_init(s, payload, plen);
        break;
    case SHCI_OPCODE_C2_CONFIG:
        status = s->c2_stack ? wb55_ble_config(s, payload, plen) : 0x01;
        break;
    case SHCI_OPCODE_C2_FLASH_CONTROL:
    case SHCI_OPCODE_C2_FLASH_ERASE:
        /* (CPU2's radio does not touch the flash here: nothing to hold) */
        status = s->c2_stack ? 0x00 : 0x01;
        break;
    case SHCI_OPCODE_C2_REINIT:
        if (s->c2_stack) {
            /* CPU2 restarts its firmware when CPU1 enables the IPCC again
             * (cpu2_c2boot); its stack starts from nothing then */
            status = 0x00;
            s->c2_booted = false;
            s->c2_reinit = true;
        }
        break;
    case SHCI_OPCODE_C2_SET_SYSTEM_CLOCK:
        /*
         * Strictly a stack command, but it is the one the firmware's STOP2
         * entry (furi_hal_power_deep_sleep) depends on: CPU1 asks CPU2 to
         * move SYSCLK PLL -> HSE before stopping and HSE -> PLL after.
         * Refusing it makes the idle task spin instead of sleeping, which
         * is what every device does after an update clears the
         * legacy-sleep flag. Payload: 0 HSE->PLL, 1 PLL(on)->HSE,
         * 2 PLL(off)->HSE.
         */
        if (s->rcc && plen >= 1) {
            uint8_t sel = payload[0];
            wb55_rcc_c2_set_sysclk(s->rcc, sel ? 2 : 3, sel == 2);
            status = 0x00;
        }
        break;
    /* The customer key storage. The answers are a status byte, 0x00 or
     * 0x01, and for a key stored the index the FUS gave it. */
    case SHCI_OPCODE_FUS_STORE_USR_KEY:
        extra[0] = 0;
        status = wb55_fus_store_key(s, payload, plen, &extra[0]);
        nextra = 1;
        break;
    case SHCI_OPCODE_FUS_LOAD_USR_KEY:
        status = plen >= 1 ? wb55_fus_load_key(s, payload[0]) : 0x01;
        break;
    case SHCI_OPCODE_FUS_UNLOAD_USR_KEY:
        status = plen >= 1 ? wb55_fus_unload_key(s, payload[0]) : 0x01;
        break;
    case SHCI_OPCODE_FUS_LOCK_USR_KEY:
        status = plen >= 1 ? wb55_fus_lock_key(s, payload[0]) : 0x01;
        break;
    default:
        /* No stack to start or to talk to, no upgrade: the FUS refuses. */
        if (s->unknown_cmd_log < 16) {
            s->unknown_cmd_log++;
            qemu_log_mask(LOG_UNIMP, "flipper-cpu2: SHCI 0x%04x -> error\n",
                          opcode);
        }
        break;
    }
    if (s->c2_fault) {
        return;                         /* (it faulted on it: no answer) */
    }
    /* TL_EvtSerial_t at offset 0: type | evtcode | plen | TL_CcEvt_t */
    tl_wr8(buf + 0, TL_SYSRSP_PKT_TYPE);
    tl_wr8(buf + 1, TL_BLEEVT_CC_OPCODE);
    tl_wr8(buf + 2, 3 + 1 + nextra);
    tl_wr8(buf + 3, 0xFF);              /* NumHCI (AN5185 6.3.4) */
    tl_wr8(buf + 4, opcode & 0xFF);
    tl_wr8(buf + 5, opcode >> 8);
    tl_wr8(buf + 6, status);
    for (int i = 0; i < nextra; i++) {
        tl_wr8(buf + 7 + i, extra[i]);
    }
}

static void cpu2_release_buffers(WB55IpccState *s)
{
    hwaddr mm = tl_rd32(wb55_ipcc_ref_table(s) + TL_REF_MEM_MANAGER);
    hwaddr q = mm ? tl_rd32(mm + TL_MM_FREE_QUEUE) : 0;
    int guard = 64;

    while (q && !tl_lst_is_empty(q) && guard--) {
        wb55_ble_released(s, tl_lst_remove_head(q));
    }
    wb55_ble_flush(s);
}

/* CPU1 raised a C1->C2 flag: CPU2 reacts immediately. */
static void cpu2_channel_set(WB55IpccState *s, uint32_t chans)
{
    if (!s->c2_booted || s->c2_fault) {
        return;
    }
    if (chans & CH(2)) {                /* system command/response */
        cpu2_system_command(s);
        if (s->c2_fault) {
            return;                     /* the flag stays: no answer */
        }
        s->c1toc2sr &= ~CH(2);
    }
    if ((chans & CH(1)) && s->c2_stack) {   /* a BLE command */
        s->c1toc2sr &= ~CH(1);
        wb55_ble_command(s);
    }
    if (chans & CH(4)) {                /* memory manager: buffers released */
        cpu2_release_buffers(s);
        s->c1toc2sr &= ~CH(4);
    }
    /* channel 6 (ACL data) is never taken: the firmwares send none */
}

/* ------------------------------------------------------------------ IPCC */

static uint64_t wb55_ipcc_read(void *opaque, hwaddr addr, unsigned size)
{
    WB55IpccState *s = opaque;

    switch (addr) {
    case C1CR:      return s->c1cr;
    case C1MR:      return s->c1mr;
    case C1TOC2SR:  return s->c1toc2sr;
    case C2CR:      return s->c2cr;
    case C2MR:      return s->c2mr;
    case C2TOC1SR:  return s->c2toc1sr;
    case 0x3F0:     return 0x00000006;  /* HWCFGR: 6 channels */
    case 0x3F4:     return 0x00000010;
    case 0x3F8:     return 0x00100071;
    case 0x3FC:     return 0xA3C5DD01;
    }
    return 0;
}

static void wb55_ipcc_write(void *opaque, hwaddr addr, uint64_t val64,
                            unsigned size)
{
    WB55IpccState *s = opaque;
    uint32_t val = val64;

    switch (addr) {
    case C1CR:
        s->c1cr = val & 0x00010001;
        break;
    case C1MR:
        s->c1mr = val & 0x003F003F;
        break;
    case C1SCR: {
        uint32_t set = (val >> 16) & CH_MASK;
        s->c2toc1sr &= ~(val & CH_MASK);        /* CHnC: ack received msgs */
        s->c1toc2sr |= set;                     /* CHnS: post to CPU2      */
        if (set) {
            cpu2_channel_set(s, set);
        }
        if (val & (CH(1) | CH(2))) {
            wb55_ble_flush(s);          /* events that waited for that */
        }
        break;
    }
    case C2CR:
        s->c2cr = val & 0x00010001;
        break;
    case C2MR:
        s->c2mr = val & 0x003F003F;
        break;
    case C2SCR:
        s->c1toc2sr &= ~(val & CH_MASK);
        s->c2toc1sr |= (val >> 16) & CH_MASK;
        break;
    default:
        qemu_log_mask(LOG_GUEST_ERROR, "wb55-ipcc: write 0x%" HWADDR_PRIx "\n",
                      addr);
    }
    wb55_ipcc_update_irq(s);
}

static const MemoryRegionOps wb55_ipcc_ops = {
    .read = wb55_ipcc_read,
    .write = wb55_ipcc_write,
    .endianness = DEVICE_LITTLE_ENDIAN,
    .impl.min_access_size = 4,
    .impl.max_access_size = 4,
    .valid.min_access_size = 4,
    .valid.max_access_size = 4,
};

static void wb55_ipcc_reset_hold(Object *obj, ResetType type)
{
    WB55IpccState *s = WB55_IPCC(obj);

    s->c1cr = s->c2cr = 0;
    s->c1mr = s->c2mr = 0x003F003F;
    s->c1toc2sr = s->c2toc1sr = 0;
    s->c2_booted = false;
    s->c2_stack = false;
    s->c2_reinit = false;
    s->c2_fault = false;
    timer_del(s->boot_timer);
    /* an operation of the FUS's that was under way is cut short; what is
     * installed, and whether CPU2 stays in its FUS, stay as they were */
    timer_del(s->fus_timer);
    s->fus_op = WB55_FUS_OP_NONE;
    s->fus_asked = 0;
    s->fus_error = 0;
    wb55_ble_reset(s);
    wb55_fus_reset(s);
    qemu_set_irq(s->irq_rx, 0);
    qemu_set_irq(s->irq_tx, 0);
}

static void wb55_ipcc_realize(DeviceState *dev, Error **errp)
{
    WB55IpccState *s = WB55_IPCC(dev);
    const char *v = s->radio_stack ? s->radio_stack : STACK_DEFAULT;
    g_autofree char *copy = g_strdup(v);
    const char *version = copy;
    char *kind_name = strchr(copy, ':');
    const WB55StackKind *kind = stack_kind_named("light");
    unsigned major, minor, sub;
    char end;

    /* VERSION, KIND, VERSION:KIND (1.20.0 and Light when left out), none */
    if (kind_name) {
        *kind_name++ = '\0';
    } else if (stack_kind_named(copy)) {
        kind_name = copy;
        version = STACK_DEFAULT;
    }
    if (kind_name && !(kind = stack_kind_named(kind_name))) {
        error_setg(errp, "radio-stack: '%s' is no kind of stack that the "
                   "model is (light, full, full-ext)", kind_name);
        return;
    }
    s->c2_sram2a = C2_SRAM2A_FACTORY;
    s->c2_sram2b = C2_SRAM2B_FACTORY;
    if (!strcmp(version, "none")) {
        s->stack_version = 0;
    } else if (sscanf(version, "%u.%u.%u%c", &major, &minor, &sub,
                      &end) == 3 &&
               major && major < 256 && minor < 256 && sub < 256) {
        s->stack_version = major << 24 | minor << 16 | sub << 8 |
                           STACK_RELEASE_TYPE;
        s->stack_type = kind->type;
        s->c2_sram2a = kind->sram2a;
        s->c2_sram2b = kind->sram2b;
    } else {
        error_setg(errp, "radio-stack: '%s' is no version (MAJOR.MINOR.SUB), "
                   "no kind (light, full, full-ext) and not 'none'", v);
        return;
    }
    s->stack_sectors = s->stack_version ? kind->sectors : 0;
    /* What the FUS installed before: the file has it, when there is one
     * (a run that names a stack has the launcher remove the file first) */
    radio_load(s);
    radio_save(s);
    fus_sfsa(s);                        /* (the flash is realized already) */
    if (wb55_fus_realize(s, errp)) {
        wb55_ble_realize(s);
    }
}

static void wb55_ipcc_init_props(Object *obj);

static void wb55_ipcc_init(Object *obj)
{
    WB55IpccState *s = WB55_IPCC(obj);

    memory_region_init_io(&s->mmio, obj, &wb55_ipcc_ops, s, "ipcc", 0x400);
    sysbus_init_mmio(SYS_BUS_DEVICE(obj), &s->mmio);
    sysbus_init_irq(SYS_BUS_DEVICE(obj), &s->irq_rx);
    sysbus_init_irq(SYS_BUS_DEVICE(obj), &s->irq_tx);
    qdev_init_gpio_in_named(DEVICE(obj), cpu2_c2boot, "c2boot", 1);
    s->boot_timer = timer_new_ns(QEMU_CLOCK_VIRTUAL, cpu2_boot, s);
    s->fus_timer = timer_new_ns(QEMU_CLOCK_VIRTUAL, fus_operation_done, s);
    wb55_ipcc_init_props(obj);
}

static const Property wb55_ipcc_props[] = {
    DEFINE_PROP_UINT32("fus-version", WB55IpccState, fus_version, 0x01020000),
    DEFINE_PROP_UINT64("uid64", WB55IpccState, uid64, 0),
    /* the FUS's keys on the host: stm32wb55_fus.c */
    DEFINE_PROP_STRING("keys", WB55IpccState, key_file),
    /* the wireless stack that is installed: its version, or "none" */
    DEFINE_PROP_STRING("radio-stack", WB55IpccState, radio_stack),
    /* a central on the host, as lines (docs/RADIO_LINK.md) */
    DEFINE_PROP_CHR("ble-link", WB55IpccState, ble_chr),
    /* what the FUS keeps of the stack: a file on the host */
    DEFINE_PROP_STRING("radio-state", WB55IpccState, radio_file),
};

/*
 * A saved state: the registers, whether CPU2 has started, and CPU2's key
 * store with its locks.  (So a state file holds keys, as the store's own
 * file does.)  The file on the host follows when the machine next runs: see
 * wb55_fus_loaded().
 */
static void wb55_ipcc_loaded(void *opaque, bool running, RunState state)
{
    WB55IpccState *s = opaque;

    if (!running) {
        return;
    }
    qemu_del_vm_change_state_handler(s->loaded);
    s->loaded = NULL;
    wb55_fus_loaded(s);
    wb55_ble_loaded(s);
    radio_save(s);                      /* the loaded CPU2's stack, on disk */
}

static int wb55_ipcc_post_load(void *opaque, int version_id)
{
    WB55IpccState *s = opaque;

    if (!s->loaded) {
        s->loaded = qemu_add_vm_change_state_handler(wb55_ipcc_loaded, s);
    }
    return 0;
}

static const VMStateDescription vmstate_wb55_fus_key = {
    .name = "stm32wb55-ipcc/key",
    .version_id = 1,
    .minimum_version_id = 1,
    .fields = (const VMStateField[]) {
        VMSTATE_UINT8(type, WB55FusKey),
        VMSTATE_UINT8(len, WB55FusKey),
        VMSTATE_UINT8_ARRAY(data, WB55FusKey, 32),
        VMSTATE_END_OF_LIST()
    }
};

static const VMStateDescription vmstate_wb55_ipcc = {
    .name = "stm32wb55-ipcc",
    .version_id = 7,
    .minimum_version_id = 5,
    .post_load = wb55_ipcc_post_load,
    .fields = (const VMStateField[]) {
        VMSTATE_UINT32(c1cr, WB55IpccState),
        VMSTATE_UINT32(c1mr, WB55IpccState),
        VMSTATE_UINT32(c1toc2sr, WB55IpccState),
        VMSTATE_UINT32(c2cr, WB55IpccState),
        VMSTATE_UINT32(c2mr, WB55IpccState),
        VMSTATE_UINT32(c2toc1sr, WB55IpccState),
        VMSTATE_TIMER_PTR(boot_timer, WB55IpccState),
        VMSTATE_BOOL(c2_booted, WB55IpccState),
        VMSTATE_STRUCT_ARRAY(key, WB55IpccState, WB55_FUS_KEYS, 0,
                             vmstate_wb55_fus_key, WB55FusKey),
        VMSTATE_BOOL_ARRAY(key_locked, WB55IpccState, WB55_FUS_KEYS),
        VMSTATE_UINT8(key_loaded, WB55IpccState),
        VMSTATE_BOOL(c2_stack, WB55IpccState),
        VMSTATE_BOOL(c2_reinit, WB55IpccState),
        VMSTATE_UINT32(stack_version, WB55IpccState),
        VMSTATE_UINT8(stack_type, WB55IpccState),
        VMSTATE_UINT8(stack_sectors, WB55IpccState),
        VMSTATE_UINT8_V(c2_sram2a, WB55IpccState, 6),
        VMSTATE_UINT8_V(c2_sram2b, WB55IpccState, 6),
        VMSTATE_BOOL_V(c2_fault, WB55IpccState, 7),
        VMSTATE_BUFFER_V(c2_fault_why, WB55IpccState, 7),
        VMSTATE_BOOL(c2_fus_boot, WB55IpccState),
        VMSTATE_UINT8(fus_asked, WB55IpccState),
        VMSTATE_UINT8(fus_op, WB55IpccState),
        VMSTATE_UINT8(fus_error, WB55IpccState),
        VMSTATE_UINT32(fus_src, WB55IpccState),
        VMSTATE_TIMER_PTR(fus_timer, WB55IpccState),
        VMSTATE_STRUCT(ble, WB55IpccState, 2, vmstate_wb55_ble, WB55BleState),
        VMSTATE_END_OF_LIST()
    }
};

/* "ble": what CPU2 runs and what its stack holds, as JSON (`status`) */
static char *wb55_ipcc_get_ble(Object *obj, Error **errp)
{
    WB55IpccState *s = WB55_IPCC(obj);
    WB55BleState *b = &s->ble;
    QDict *d = qdict_new();
    GString *js;
    char *out;
    int n;

    if (s->stack_version) {
        char *v = g_strdup_printf("%u.%u.%u", s->stack_version >> 24,
                                  (s->stack_version >> 16) & 0xFF,
                                  (s->stack_version >> 8) & 0xFF);
        const WB55StackKind *k = stack_kind_of_type(s->stack_type);

        qdict_put_str(d, "stack", v);
        qdict_put_str(d, "kind", k ? k->words : "unknown");
        qdict_put_int(d, "type", s->stack_type);
        g_free(v);
    } else {
        qdict_put_null(d, "stack");
    }
    qdict_put_str(d, "runs", s->c2_fault ? "hardfault" :
                  !s->c2_booted ? "nothing" : s->c2_stack ? "stack" : "fus");
    if (s->c2_fault) {
        qdict_put_str(d, "fault", (char *)s->c2_fault_why);
    }
    qdict_put_bool(d, "up", b->up);
    /* the FUS: whether CPU2 stays in it, what it is doing */
    qdict_put_bool(d, "stays_in_fus", s->c2_fus_boot);
    qdict_put_str(d, "fus_operation",
                  s->fus_op == WB55_FUS_OP_DELETE ? "deleting"
                  : s->fus_op == WB55_FUS_OP_UPGRADE ? "installing"
                  : s->fus_op == WB55_FUS_OP_SWITCH ? "restarting" : "none");
    qdict_put_int(d, "fus_error", s->fus_error);
    qdict_put_bool(d, "advertising", b->adv);
    if (b->name_len > 1) {
        /* the local name's AD structure: its type, then the name */
        char *name = g_strndup((const char *)b->name + 1, b->name_len - 1);
        qdict_put_str(d, "name", g_utf8_validate(name, -1, NULL) ? name
                                                                 : "?");
        g_free(name);
    }
    if (b->public_addr[0] | b->public_addr[1] | b->public_addr[5]) {
        char *a = g_strdup_printf("%02X:%02X:%02X:%02X:%02X:%02X",
                                  b->public_addr[5], b->public_addr[4],
                                  b->public_addr[3], b->public_addr[2],
                                  b->public_addr[1], b->public_addr[0]);
        qdict_put_str(d, "address", a);
        g_free(a);
    }
    n = 0;
    for (int i = 0; i < WB55_BLE_SERVICES; i++) {
        n += b->svc[i].handle != 0;
    }
    qdict_put_int(d, "services", n);
    n = 0;
    for (int i = 0; i < b->nattrs; i++) {
        n += b->attr[i].kind != WB55_BLE_ATTR_FREE;
    }
    qdict_put_int(d, "attributes", n);
    qdict_put_bool(d, "beacon", b->beacon);
    qdict_put_int(d, "events_waiting", b->npending);
    /* a program on the link (ble.sock), and its connection */
    qdict_put_bool(d, "link", s->ble_link && s->ble_link->open);
    /* the centrals it has a bond with (in the RAM that CPU1 saves) */
    qdict_put_int(d, "bonds", wb55_ble_bonds(s));
    if (b->conn) {
        QDict *c = qdict_new();
        char *a = g_strdup_printf("%02X:%02X:%02X:%02X:%02X:%02X",
                                  b->peer[5], b->peer[4], b->peer[3],
                                  b->peer[2], b->peer[1], b->peer[0]);

        qdict_put_int(c, "handle", b->conn);
        qdict_put_str(c, "central", a);
        qdict_put_int(c, "interval", b->interval);
        qdict_put_int(c, "mtu", b->mtu);
        qdict_put_bool(c, "encrypted", b->sec & WB55_BLE_SEC_ENCRYPTED);
        qdict_put_bool(c, "authenticated",
                       b->sec & WB55_BLE_SEC_AUTHENTICATED);
        qdict_put_bool(c, "bonded", b->sec & WB55_BLE_SEC_BONDED);
        qdict_put_str(c, "pairing", b->pair == WB55_BLE_PAIR_PASSKEY
                      ? "passkey" : b->pair == WB55_BLE_PAIR_COMPARE
                      ? "compare" : "none");
        qdict_put(d, "connection", c);
        g_free(a);
    } else {
        qdict_put_null(d, "connection");
    }
    js = qobject_to_json(QOBJECT(d));
    out = g_string_free(js, false);
    qobject_unref(d);
    return out;
}

static void wb55_ipcc_init_props(Object *obj)
{
    object_property_add_str(obj, "ble", wb55_ipcc_get_ble, NULL);
}

static void wb55_ipcc_class_init(ObjectClass *klass, const void *data)
{
    DeviceClass *dc = DEVICE_CLASS(klass);

    dc->realize = wb55_ipcc_realize;
    device_class_set_props(dc, wb55_ipcc_props);
    RESETTABLE_CLASS(klass)->phases.hold = wb55_ipcc_reset_hold;
    dc->vmsd = &vmstate_wb55_ipcc;
}

static const TypeInfo wb55_ipcc_info = {
    .name = TYPE_WB55_IPCC,
    .parent = TYPE_SYS_BUS_DEVICE,
    .instance_size = sizeof(WB55IpccState),
    .instance_init = wb55_ipcc_init,
    .class_init = wb55_ipcc_class_init,
};

static void wb55_ipcc_register(void)
{
    type_register_static(&wb55_ipcc_info);
}
type_init(wb55_ipcc_register)
