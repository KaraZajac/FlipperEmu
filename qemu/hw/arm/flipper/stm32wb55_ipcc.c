/*
 * STM32WB55 IPCC (RM0434 chapter 38) + a behavioural CPU2
 *
 * CPU2 runs ST's closed radio firmware. It is not emulated; instead this model
 * behaves like a real device whose CPU2 runs the Firmware Upgrade Service
 * (FUS) with no wireless stack installed, a state the Flipper firmware handles
 * ("Stack isn't installed!") and that keeps the crypto enclave path alive.
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
 *
 * Copyright (c) 2026, Kara Zajac
 * SPDX-License-Identifier: BSD-3-Clause
 */
#include "qemu/osdep.h"
#include "qemu/log.h"
#include "qemu/error-report.h"
#include "hw/core/irq.h"
#include "hw/core/qdev-properties.h"
#include "system/address-spaces.h"
#include "system/memory.h"
#include "wb55.h"

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
#define SHCI_EVTCODE                   0xFF
#define SHCI_SUB_EVT_CODE_READY        0x9200
#define FUS_FW_RUNNING                 0x01
#define TL_SYSEVT_PKT_TYPE             0x12
#define TL_SYSRSP_PKT_TYPE             0x11
#define TL_BLEEVT_CC_OPCODE            0x0E
#define FUS_DEVICE_INFO_VALID          0xA94656B9u
#define SHCI_UNKNOWN_CMD               0x01

static uint32_t rd32(hwaddr a)
{
    uint32_t v = 0;
    address_space_read(&address_space_memory, a, MEMTXATTRS_UNSPECIFIED, &v, 4);
    return le32_to_cpu(v);
}

static void wr32(hwaddr a, uint32_t v)
{
    v = cpu_to_le32(v);
    address_space_write(&address_space_memory, a, MEMTXATTRS_UNSPECIFIED, &v, 4);
}

static void wr8(hwaddr a, uint8_t v)
{
    address_space_write(&address_space_memory, a, MEMTXATTRS_UNSPECIFIED, &v, 1);
}

static uint8_t rd8(hwaddr a)
{
    uint8_t v = 0;
    address_space_read(&address_space_memory, a, MEMTXATTRS_UNSPECIFIED, &v, 1);
    return v;
}

static hwaddr ref_table(WB55IpccState *s)
{
    uint32_t ipccdba = 0;
    if (s->flash) {
        ipccdba = s->flash->ob[13] & 0x3FFF;    /* FLASH->IPCCBR */
    }
    return WB55_SRAM2A_BASE + (ipccdba << 2);
}

/* tListNode helpers on guest memory */
static bool lst_is_empty(hwaddr head)
{
    return rd32(head) == head;
}

static void lst_insert_tail(hwaddr head, hwaddr node)
{
    hwaddr prev = rd32(head + 4);
    wr32(node, head);           /* node->next = head */
    wr32(node + 4, prev);       /* node->prev = head->prev */
    wr32(prev, node);           /* prev->next = node */
    wr32(head + 4, node);       /* head->prev = node */
}

static hwaddr lst_remove_head(hwaddr head)
{
    hwaddr node = rd32(head);
    hwaddr next = rd32(node);
    wr32(head, next);
    wr32(next + 4, head);
    return node;
}

static void ipcc_update_irq(WB55IpccState *s)
{
    uint32_t rx = s->c2toc1sr & ~s->c1mr & CH_MASK;
    uint32_t tx = ~s->c1toc2sr & ~(s->c1mr >> 16) & CH_MASK;

    qemu_set_irq(s->irq_rx, (s->c1cr & 1) && rx);
    qemu_set_irq(s->irq_tx, (s->c1cr & (1u << 16)) && tx);
}

/* ------------------------------------------------------------- CPU2 (FUS) */

static void cpu2_write_device_info(WB55IpccState *s, hwaddr ref)
{
    hwaddr info = rd32(ref + 0);        /* ref->p_device_info_table */
    if (!info) {
        return;
    }
    /* MB_FUS_DeviceInfoTable_t */
    wr32(info + 0, FUS_DEVICE_INFO_VALID);
    wr32(info + 4, 0);                  /* Reserved1, LastFusActiveState,
                                           LastWirelessStackState,
                                           CurrentWirelessStackType */
    wr32(info + 8, 0x01000300);         /* SafeBootVersion */
    wr32(info + 12, s->fus_version);    /* FusVersion */
    wr32(info + 16, 0x00000000);        /* FusMemorySize */
    wr32(info + 20, 0);                 /* WirelessStackVersion: none */
    wr32(info + 24, 0);                 /* WirelessStackMemorySize */
    wr32(info + 28, 0);                 /* WirelessFirmwareBleInfo */
    wr32(info + 32, 0);                 /* WirelessFirmwareThreadInfo */
    wr32(info + 36, 0);                 /* Reserved2 */
    wr32(info + 40, (uint32_t)s->uid64);
    wr32(info + 44, (uint32_t)(s->uid64 >> 32));
    wr8(info + 48, 0x95);               /* DeviceId 0x6495 */
    wr8(info + 49, 0x64);
}

static void cpu2_post_ready(WB55IpccState *s, hwaddr ref)
{
    hwaddr sys = rd32(ref + 12);        /* ref->p_sys_table */
    hwaddr mm = rd32(ref + 16);         /* ref->p_mem_manager_table */
    if (!sys || !mm) {
        warn_report("flipper-cpu2: transport tables not set up at C2BOOT");
        return;
    }
    hwaddr queue = rd32(sys + 4);       /* sys_queue */
    hwaddr evt = rd32(mm + 4);          /* spare_sys_buffer */
    if (!queue || !evt) {
        warn_report("flipper-cpu2: no system queue/spare buffer");
        return;
    }
    /* TL_EvtPacket_t: header(8) | type | evtcode | plen | subevt(2) | rsp */
    wr8(evt + 8, TL_SYSEVT_PKT_TYPE);
    wr8(evt + 9, SHCI_EVTCODE);
    wr8(evt + 10, 3);
    wr8(evt + 11, SHCI_SUB_EVT_CODE_READY & 0xFF);
    wr8(evt + 12, SHCI_SUB_EVT_CODE_READY >> 8);
    wr8(evt + 13, FUS_FW_RUNNING);
    lst_insert_tail(queue, evt);
    s->c2toc1sr |= CH(2);
    ipcc_update_irq(s);
}

static void cpu2_boot(void *opaque)
{
    WB55IpccState *s = opaque;
    hwaddr ref = ref_table(s);

    s->c2_booted = true;
    cpu2_write_device_info(s, ref);
    cpu2_post_ready(s, ref);
}

static void cpu2_c2boot(void *opaque, int n, int level)
{
    WB55IpccState *s = opaque;

    if (level && !s->c2_booted) {
        /* FUS start-up takes a few ms on silicon */
        timer_mod(s->boot_timer,
                  qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL) + 5 * SCALE_MS);
    }
}

static void cpu2_system_command(WB55IpccState *s)
{
    hwaddr sys = rd32(ref_table(s) + 12);
    hwaddr buf = sys ? rd32(sys + 0) : 0;   /* pcmd_buffer */
    uint8_t status = SHCI_UNKNOWN_CMD;
    uint8_t extra[2];
    int nextra = 0;

    if (!buf) {
        return;
    }
    /* TL_CmdPacket_t: header(8) | type(1) | cmdcode(2) | plen(1) | payload */
    uint16_t opcode = rd8(buf + 9) | (rd8(buf + 10) << 8);

    switch (opcode) {
    case SHCI_OPCODE_FUS_GET_STATE:
        status = 0x00;                  /* FUS_STATE_IDLE */
        extra[0] = 0x00;                /* FUS_STATE_NO_ERROR */
        nextra = 1;
        break;
    default:
        /* No keys, no stack, no clock switch: the FUS refuses. */
        if (s->unknown_cmd_log < 16) {
            s->unknown_cmd_log++;
            qemu_log_mask(LOG_UNIMP, "flipper-cpu2: SHCI 0x%04x -> error\n",
                          opcode);
        }
        break;
    }
    /* TL_EvtSerial_t at offset 0: type | evtcode | plen | TL_CcEvt_t */
    wr8(buf + 0, TL_SYSRSP_PKT_TYPE);
    wr8(buf + 1, TL_BLEEVT_CC_OPCODE);
    wr8(buf + 2, 3 + 1 + nextra);
    wr8(buf + 3, 1);                    /* numcmd */
    wr8(buf + 4, opcode & 0xFF);
    wr8(buf + 5, opcode >> 8);
    wr8(buf + 6, status);
    for (int i = 0; i < nextra; i++) {
        wr8(buf + 7 + i, extra[i]);
    }
}

static void cpu2_release_buffers(WB55IpccState *s)
{
    hwaddr mm = rd32(ref_table(s) + 16);
    hwaddr q = mm ? rd32(mm + 16) : 0;  /* pevt_free_buffer_queue */
    int guard = 64;

    while (q && !lst_is_empty(q) && guard--) {
        lst_remove_head(q);             /* the FUS keeps no buffer pool */
    }
}

/* CPU1 raised a C1->C2 flag: CPU2 reacts immediately. */
static void cpu2_channel_set(WB55IpccState *s, uint32_t chans)
{
    if (!s->c2_booted) {
        return;
    }
    if (chans & CH(2)) {                /* system command/response */
        cpu2_system_command(s);
        s->c1toc2sr &= ~CH(2);
    }
    if (chans & CH(4)) {                /* memory manager: buffers released */
        cpu2_release_buffers(s);
        s->c1toc2sr &= ~CH(4);
    }
    /* channel 1 (BLE commands) and 6 (ACL data) are never taken: no stack */
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
    ipcc_update_irq(s);
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
    timer_del(s->boot_timer);
    qemu_set_irq(s->irq_rx, 0);
    qemu_set_irq(s->irq_tx, 0);
}

static void wb55_ipcc_init(Object *obj)
{
    WB55IpccState *s = WB55_IPCC(obj);

    memory_region_init_io(&s->mmio, obj, &wb55_ipcc_ops, s, "ipcc", 0x400);
    sysbus_init_mmio(SYS_BUS_DEVICE(obj), &s->mmio);
    sysbus_init_irq(SYS_BUS_DEVICE(obj), &s->irq_rx);
    sysbus_init_irq(SYS_BUS_DEVICE(obj), &s->irq_tx);
    qdev_init_gpio_in_named(DEVICE(obj), cpu2_c2boot, "c2boot", 1);
    s->boot_timer = timer_new_ns(QEMU_CLOCK_VIRTUAL, cpu2_boot, s);
}

static const Property wb55_ipcc_props[] = {
    DEFINE_PROP_UINT32("fus-version", WB55IpccState, fus_version, 0x01020000),
    DEFINE_PROP_UINT64("uid64", WB55IpccState, uid64, 0),
};

static void wb55_ipcc_class_init(ObjectClass *klass, const void *data)
{
    device_class_set_props(DEVICE_CLASS(klass), wb55_ipcc_props);
    RESETTABLE_CLASS(klass)->phases.hold = wb55_ipcc_reset_hold;
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
