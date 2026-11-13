/*
 * STM32WB55 CPU2: a behavioural BLE stack
 *
 * The Flipper's update packages carry ST's "BLE Light" wireless firmware,
 * 1.20.0 in all of them so far, which CPU2 runs beside the FUS.  CPU1 talks to
 * it in ST's ACI: HCI commands, most of them ST's own (OGF 0x3F), sent through
 * the mailbox's BLE channel (IPCC channel 1), each answered by a Command
 * Complete or a Command Status event that comes back in the BLE event queue.
 * ST's stack is a whole BLE host and controller.  This model is what CPU1 can
 * see of one: the commands that the Flipper firmwares send, answered as the
 * stack answers them, the GATT database that they build (services,
 * characteristics, descriptors and their values, with the handles that ST's
 * stack would give them), and the state of GAP (advertising, its name and
 * data, the security settings).  No radio: nothing is sent into the air.
 *
 * The events.  A command's answer goes back in a buffer of the command's
 * own: a Command Complete over the command in its buffer, a Command Status in
 * the BLE table's CS buffer; CPU1 never gives those back (hci_tl.c's
 * hci_send_req() keeps nothing).  For the other events CPU1 gives the stack a
 * pool of buffers (MB_MemManagerTable_t.blepool) and a spare one for each
 * channel, and gives each back through the free queue (IPCC channel 4) once
 * it is done with it.  An event waits here until its buffer is free and until
 * CPU1 has taken the last ones on its channel (the channel's flag is clear): a
 * channel's flag is set again only by its sender, so CPU1, which clears the
 * flag after it has emptied the queue, never misses one.
 *
 * Facts used (ST's code in the firmware's tree, lib/stm32wb_copro):
 *   - the ACI files in ble/core/auto, ble_hci_le.c: each command's opcode, its
 *     parameters and its answer (the *_cp0 and *_rp0 structures), and whether
 *     it is answered by Command Status (rq.event = 0x0F)
 *   - interface/patterns/ble_thread/tl/hci_tl.c: an answer that is shorter
 *     than the caller's structure fills what it has (MIN of the lengths), and
 *     the rest stays as the caller zeroed it
 *   - ST's GATT layout: a characteristic's value is the handle after its
 *     declaration, its CCCD the one after that (targets/f7/ble_glue/services
 *     look for writes there)
 *
 * Copyright (c) 2026, Kara Zajac
 * SPDX-License-Identifier: BSD-3-Clause
 */
#include "qemu/osdep.h"
#include "qemu/log.h"
#include "migration/vmstate.h"
#include "wb55.h"
#include "wb55_tl.h"

/* the answers' status codes: ble_defs.h, and HCI's own */
#define ST_OK                 0x00
#define ST_UNKNOWN_CONN       0x02
#define ST_DISALLOWED         0x0C
#define ST_INVALID_HANDLE     0x60
#define ST_OUT_OF_HANDLE      0x61
#define ST_NO_RESOURCES       0x64
#define ST_INVALID_PARAMS     0x92

#define EVT_CC                0x0E
#define EVT_CS                0x0F

#define CHAR_PROP_BROADCAST   0x01
#define CHAR_PROP_READ        0x02
#define CHAR_PROP_NOTIFY      0x10
#define CHAR_PROP_INDICATE    0x20

/* a buffer of the pool: the list node, the packet's type, code and length,
 * and 255 bytes of parameters (TL_BLE_EVENT_FRAME_SIZE), rounded to words */
#define SLOT                  ((TL_PKT_HEADER + 3 + WB55_BLE_EVT_LEN + 3) & ~3)
#define CH(n)                 (1u << ((n) - 1))

/* the commands (OGF << 10 | OCF) */
#define OP_HCI_RESET                  0x0C03
#define OP_HCI_READ_LOCAL_VERSION     0x1001
#define OP_LE_SET_SCAN_RSP_DATA       0x2009
#define OP_LE_TEST_END                0x201F
#define OP_LE_READ_PHY                0x2030
#define OP_LE_SET_DEFAULT_PHY         0x2031
#define OP_LE_RECEIVER_TEST_V2        0x2033
#define OP_LE_TRANSMITTER_TEST_V2     0x2034
#define OP_HAL_WRITE_CONFIG_DATA      0xFC0C
#define OP_HAL_SET_TX_POWER_LEVEL     0xFC0F
#define OP_HAL_LE_TX_TEST_PACKETS     0xFC14
#define OP_HAL_TONE_START             0xFC15
#define OP_HAL_TONE_STOP              0xFC16
#define OP_HAL_READ_RAW_RSSI          0xFC32
#define OP_HAL_RX_START               0xFC33
#define OP_HAL_RX_STOP                0xFC34
#define OP_GAP_SET_NON_DISCOVERABLE   0xFC81
#define OP_GAP_SET_DISCOVERABLE       0xFC83
#define OP_GAP_SET_IO_CAPABILITY      0xFC85
#define OP_GAP_SET_AUTH_REQUIREMENT   0xFC86
#define OP_GAP_PASS_KEY_RESP          0xFC88
#define OP_GAP_INIT                   0xFC8A
#define OP_GAP_PERIPHERAL_SEC_REQ     0xFC8D
#define OP_GAP_CONFIGURE_FILTER       0xFC92
#define OP_GAP_TERMINATE              0xFC93
#define OP_GAP_CLEAR_SECURITY_DB      0xFC94
#define OP_GAP_ALLOW_REBOND           0xFC95
#define OP_GAP_NUMERIC_CONFIRM        0xFCA5
#define OP_GAP_BEACON_START           0xFCB0
#define OP_GAP_BEACON_STOP            0xFCB1
#define OP_GAP_BEACON_SET_DATA        0xFCB2
#define OP_GATT_INIT                  0xFD01
#define OP_GATT_ADD_SERVICE           0xFD02
#define OP_GATT_ADD_CHAR              0xFD04
#define OP_GATT_ADD_CHAR_DESC         0xFD05
#define OP_GATT_UPDATE_CHAR_VALUE     0xFD06
#define OP_GATT_DEL_CHAR              0xFD07
#define OP_GATT_DEL_SERVICE           0xFD08
#define OP_GATT_UPDATE_CHAR_VALUE_EXT 0xFD2C
#define OP_L2CAP_CONN_PARAM_UPDATE    0xFD81

/* What the stack says of itself (hci_read_local_version_information): Core
 * 5.4, ST's company identifier.  The subversions are the emulator's. */
#define HCI_VERSION           0x0D
#define COMPANY_ST            0x0030

static uint16_t le16(const uint8_t *p)
{
    return p[0] | (p[1] << 8);
}

static void put16(uint8_t *p, uint16_t v)
{
    p[0] = v;
    p[1] = v >> 8;
}

/* ---------------------------------------------------------------- events */

static hwaddr ble_mm(WB55IpccState *s)
{
    return tl_rd32(wb55_ipcc_ref_table(s) + TL_REF_MEM_MANAGER);
}

/* A buffer of CPU1's for an event, or 0: none is free */
static hwaddr ble_take_buffer(WB55IpccState *s, int kind)
{
    WB55BleState *b = &s->ble;
    hwaddr ble = tl_rd32(wb55_ipcc_ref_table(s) + TL_REF_BLE);
    hwaddr mm = ble_mm(s);
    hwaddr buf;

    switch (kind) {
    case WB55_BLE_BUF_CMD:
        return ble ? tl_rd32(ble + TL_BLE_CMD_BUFFER) : 0;
    case WB55_BLE_BUF_CS:
        return ble ? tl_rd32(ble + TL_BLE_CS_BUFFER) : 0;
    }
    if (!mm) {
        return 0;
    }
    if (kind == WB55_BLE_BUF_SYS) {
        buf = b->spare_sys_busy ? 0 : tl_rd32(mm + TL_MM_SPARE_SYS);
        b->spare_sys_busy |= buf != 0;
        return buf;
    }
    hwaddr pool = tl_rd32(mm + TL_MM_BLE_POOL);
    uint32_t n = MIN(tl_rd32(mm + TL_MM_BLE_POOL_SIZE) / SLOT, WB55_BLE_SLOTS);
    for (uint32_t i = 0; pool && i < n; i++) {
        if (!(b->slots_busy & (1u << i))) {
            b->slots_busy |= 1u << i;
            return pool + i * SLOT;
        }
    }
    buf = b->spare_ble_busy ? 0 : tl_rd32(mm + TL_MM_SPARE_BLE);
    b->spare_ble_busy |= buf != 0;
    return buf;
}

/* CPU1 has given a buffer back */
void wb55_ble_released(WB55IpccState *s, hwaddr buf)
{
    WB55BleState *b = &s->ble;
    hwaddr mm = ble_mm(s);

    if (!mm) {
        return;
    }
    if (buf == tl_rd32(mm + TL_MM_SPARE_SYS)) {
        b->spare_sys_busy = false;
    } else if (buf == tl_rd32(mm + TL_MM_SPARE_BLE)) {
        b->spare_ble_busy = false;
    } else {
        hwaddr pool = tl_rd32(mm + TL_MM_BLE_POOL);
        if (pool && buf >= pool && (buf - pool) % SLOT == 0 &&
            (buf - pool) / SLOT < WB55_BLE_SLOTS) {
            b->slots_busy &= ~(1u << ((buf - pool) / SLOT));
        }
    }
}

/* The events that wait, into their queues, as far as there are buffers and
 * CPU1 has taken the last ones */
void wb55_ble_flush(WB55IpccState *s)
{
    WB55BleState *b = &s->ble;
    hwaddr ref = wb55_ipcc_ref_table(s);
    uint32_t posted = 0;

    while (b->npending) {
        WB55BleEvent *e = &b->pending[0];
        bool sys = e->buf == WB55_BLE_BUF_SYS;
        uint32_t ch = sys ? CH(2) : CH(1);
        hwaddr table = tl_rd32(ref + (sys ? TL_REF_SYS : TL_REF_BLE));
        hwaddr queue = table ? tl_rd32(table + (sys ? TL_SYS_QUEUE
                                                    : TL_BLE_EVT_QUEUE)) : 0;
        hwaddr buf;

        if (!queue || ((s->c2toc1sr & ch) && !(posted & ch))) {
            break;                      /* not set up yet, or not taken yet */
        }
        buf = ble_take_buffer(s, e->buf);
        if (!buf) {
            break;
        }
        tl_wr8(buf + TL_PKT_HEADER, sys ? TL_SYSEVT_PKT_TYPE
                                        : TL_BLEEVT_PKT_TYPE);
        tl_wr8(buf + TL_PKT_HEADER + 1, e->code);
        tl_wr8(buf + TL_PKT_HEADER + 2, e->len);
        tl_write(buf + TL_PKT_HEADER + 3, e->data, e->len);
        tl_lst_insert_tail(queue, buf);
        posted |= ch;
        b->npending--;
        memmove(&b->pending[0], &b->pending[1],
                b->npending * sizeof(b->pending[0]));
    }
    if (posted) {
        s->c2toc1sr |= posted;
        wb55_ipcc_update_irq(s);
    }
}

void wb55_ble_post(WB55IpccState *s, int buf, uint8_t code,
                   const uint8_t *data, int len)
{
    WB55BleState *b = &s->ble;
    WB55BleEvent *e;

    if (b->npending == WB55_BLE_PENDING) {
        qemu_log_mask(LOG_GUEST_ERROR, "flipper-cpu2: CPU1 takes no events: "
                      "event 0x%02x dropped\n", code);
        return;
    }
    e = &b->pending[b->npending++];
    e->buf = buf;
    e->code = code;
    e->len = MIN(len, WB55_BLE_EVT_LEN);
    memcpy(e->data, data, e->len);
    wb55_ble_flush(s);
}

/* Command Complete: Num_HCI_Command_Packets, the opcode, what it returns */
static void ble_complete(WB55IpccState *s, uint16_t op, const uint8_t *rp,
                         int rlen)
{
    uint8_t d[WB55_BLE_EVT_LEN];

    d[0] = 1;
    put16(d + 1, op);
    memcpy(d + 3, rp, rlen);
    wb55_ble_post(s, WB55_BLE_BUF_CMD, EVT_CC, d, 3 + rlen);
}

static void ble_status(WB55IpccState *s, uint16_t op, uint8_t status)
{
    uint8_t d[4] = { status, 1, op & 0xFF, op >> 8 };

    wb55_ble_post(s, WB55_BLE_BUF_CS, EVT_CS, d, sizeof(d));
}

/* ----------------------------------------------------------------- GATT */

static WB55BleAttr *attr_find(WB55BleState *b, uint16_t handle)
{
    for (int i = 0; i < b->nattrs; i++) {
        if (b->attr[i].kind != WB55_BLE_ATTR_FREE &&
            b->attr[i].handle == handle) {
            return &b->attr[i];
        }
    }
    return NULL;
}

static WB55BleService *svc_find(WB55BleState *b, uint16_t handle)
{
    for (int i = 0; i < WB55_BLE_SERVICES; i++) {
        if (b->svc[i].handle && b->svc[i].handle == handle) {
            return &b->svc[i];
        }
    }
    return NULL;
}

/* A record with room for a value of max_len bytes, or NULL */
static WB55BleAttr *attr_new(WB55BleState *b, uint16_t handle,
                             uint16_t service, int kind, int max_len)
{
    WB55BleAttr *a = NULL;

    if (b->values_used + max_len > WB55_BLE_VALUES) {
        return NULL;
    }
    for (int i = 0; i < b->nattrs; i++) {
        if (b->attr[i].kind == WB55_BLE_ATTR_FREE) {
            a = &b->attr[i];
            break;
        }
    }
    if (!a) {
        if (b->nattrs == WB55_BLE_ATTRS) {
            return NULL;
        }
        a = &b->attr[b->nattrs++];
    }
    memset(a, 0, sizeof(*a));
    a->handle = handle;
    a->service = service;
    a->kind = kind;
    a->max_len = max_len;
    a->at = b->values_used;
    b->values_used += max_len;
    memset(&b->values[a->at], 0, max_len);
    return a;
}

static void attr_uuid(WB55BleAttr *a, int type, const uint8_t *uuid)
{
    a->uuid_type = type;
    memcpy(a->uuid, uuid, type == 1 ? 2 : 16);
}

static void attr_uuid16(WB55BleAttr *a, uint16_t uuid)
{
    uint8_t u[2];

    put16(u, uuid);
    attr_uuid(a, 1, u);
}

/* a value set by CPU1 (or by the stack itself) */
static void attr_set(WB55BleState *b, WB55BleAttr *a, int at, const uint8_t *v,
                     int len)
{
    memcpy(&b->values[a->at + at], v, len);
    if (a->variable) {
        a->len = at + len;
    }
}

/* A service and its range of handles: its declaration, and `records` more */
static WB55BleService *svc_new(WB55BleState *b, int uuid_type,
                               const uint8_t *uuid, int primary, int records)
{
    WB55BleService *svc = NULL;
    WB55BleAttr *a;
    uint16_t h = b->next_handle;

    for (int i = 0; i < WB55_BLE_SERVICES; i++) {
        if (!b->svc[i].handle) {
            svc = &b->svc[i];
            break;
        }
    }
    if (!svc || h + records > 0xFFFF) {
        return NULL;
    }
    a = attr_new(b, h, h, WB55_BLE_ATTR_SERVICE, 0);
    if (!a) {
        return NULL;
    }
    attr_uuid(a, uuid_type, uuid);
    a->props = primary;                 /* (a service's type) */
    svc->handle = h;
    svc->next = h + 1;
    svc->last = h + records;
    b->next_handle = h + records + 1;
    return svc;
}

/* A characteristic in a service: its declaration, its value, a CCCD if it
 * notifies or indicates, an SCCD if it broadcasts.  -> its declaration's
 * handle, or 0 */
static uint16_t char_new(WB55BleState *b, WB55BleService *svc, int uuid_type,
                         const uint8_t *uuid, int max_len, int props,
                         int perms, int evt_mask, int variable)
{
    int need = 2 + !!(props & (CHAR_PROP_NOTIFY | CHAR_PROP_INDICATE)) +
               !!(props & CHAR_PROP_BROADCAST);
    uint16_t h = svc->next;
    WB55BleAttr *a;

    if (h + need - 1 > svc->last) {
        return 0;
    }
    a = attr_new(b, h, svc->handle, WB55_BLE_ATTR_CHAR, 0);
    if (!a) {
        return 0;
    }
    attr_uuid(a, uuid_type, uuid);
    a->props = props;
    a = attr_new(b, h + 1, svc->handle, WB55_BLE_ATTR_VALUE, max_len);
    if (!a) {
        return 0;
    }
    attr_uuid(a, uuid_type, uuid);
    a->props = props;
    a->perms = perms;
    a->evt_mask = evt_mask;
    a->variable = variable;
    a->len = variable ? 0 : max_len;
    svc->next = h + 2;
    if (props & (CHAR_PROP_NOTIFY | CHAR_PROP_INDICATE)) {
        a = attr_new(b, svc->next, svc->handle, WB55_BLE_ATTR_CCCD, 2);
        if (!a) {
            return 0;
        }
        attr_uuid16(a, 0x2902);
        a->len = 2;
        svc->next++;
    }
    if (props & CHAR_PROP_BROADCAST) {
        a = attr_new(b, svc->next, svc->handle, WB55_BLE_ATTR_DESC, 2);
        if (!a) {
            return 0;
        }
        attr_uuid16(a, 0x2903);
        a->len = 2;
        svc->next++;
    }
    return h;
}

/* aci_gatt_init: the database, with the GATT service in it (Service Changed,
 * which indicates) */
static void gatt_init(WB55BleState *b)
{
    static const uint8_t gatt[2] = { 0x01, 0x18 }, changed[2] = { 0x05, 0x2A };
    WB55BleService *svc;

    memset(b->svc, 0, sizeof(b->svc));
    memset(b->attr, 0, sizeof(b->attr));
    b->nattrs = b->values_used = 0;
    b->next_handle = 1;
    b->gap_service = b->name_char = b->appearance_char = 0;
    svc = svc_new(b, 1, gatt, 1, 3);
    if (svc) {
        char_new(b, svc, 1, changed, 4, CHAR_PROP_INDICATE, 0, 0, 0);
    }
}

/* aci_gap_init: the GAP service (Device Name, Appearance, and for a
 * peripheral its preferred connection parameters) */
static uint8_t gap_init(WB55BleState *b, int role, int name_len)
{
    static const uint8_t gap[2] = { 0x00, 0x18 }, name[2] = { 0x00, 0x2A },
        appearance[2] = { 0x01, 0x2A }, ppcp[2] = { 0x04, 0x2A };
    WB55BleService *svc = svc_new(b, 1, gap, 1, 6);

    if (!svc) {
        return ST_NO_RESOURCES;
    }
    b->role = role;
    b->gap_service = svc->handle;
    b->name_char = char_new(b, svc, 1, name, MAX(name_len, 1), CHAR_PROP_READ,
                            0, 0, 1);
    b->appearance_char = char_new(b, svc, 1, appearance, 2, CHAR_PROP_READ,
                                  0, 0, 0);
    char_new(b, svc, 1, ppcp, 8, CHAR_PROP_READ, 0, 0, 0);
    return b->name_char && b->appearance_char ? ST_OK : ST_NO_RESOURCES;
}

/* Remove a characteristic: its declaration up to the next one's */
static void char_del(WB55BleState *b, WB55BleService *svc, uint16_t handle)
{
    uint16_t end = svc->last + 1;

    for (int i = 0; i < b->nattrs; i++) {
        WB55BleAttr *a = &b->attr[i];
        if (a->kind == WB55_BLE_ATTR_CHAR && a->service == svc->handle &&
            a->handle > handle && a->handle < end) {
            end = a->handle;
        }
    }
    for (int i = 0; i < b->nattrs; i++) {
        WB55BleAttr *a = &b->attr[i];
        if (a->kind != WB55_BLE_ATTR_FREE && a->service == svc->handle &&
            a->handle >= handle && a->handle < end) {
            a->kind = WB55_BLE_ATTR_FREE;
        }
    }
}

/* the value of a characteristic of a service, by its declaration */
static WB55BleAttr *char_value(WB55BleState *b, uint16_t service,
                               uint16_t handle)
{
    WB55BleAttr *decl = attr_find(b, handle);
    WB55BleAttr *value = attr_find(b, handle + 1);

    if (!decl || decl->kind != WB55_BLE_ATTR_CHAR || decl->service != service ||
        !value || value->kind != WB55_BLE_ATTR_VALUE) {
        return NULL;
    }
    return value;
}

/* ------------------------------------------------------------- commands */

void wb55_ble_reset(WB55IpccState *s)
{
    WB55BleState *b = &s->ble;

    memset(b, 0, sizeof(*b));
    b->next_handle = 1;
}

/* SHCI_C2_BLE_Init: the stack starts.  Its parameters (SHCI_C2_Ble_Init_
 * Cmd_Param_t) say how large the database may grow: they are kept, and the
 * model's own sizes are what holds. */
uint8_t wb55_ble_init(WB55IpccState *s, const uint8_t *p, int plen)
{
    WB55BleState *b = &s->ble;

    if (!s->c2_stack) {
        return 0x01;
    }
    if (plen >= 14) {
        b->max_attrs = le16(p + 8);
        b->max_services = le16(p + 10);
        b->max_values = le16(p + 12);
    }
    b->up = true;
    return 0x00;
}

/* SHCI_C2_Config: where the stack's bonds go in CPU1's RAM, and which
 * system events CPU1 wants */
uint8_t wb55_ble_config(WB55IpccState *s, const uint8_t *p, int plen)
{
    if (plen >= 8) {
        s->ble.evt_mask1 = p[2];
        s->ble.nvm_ram = ldl_le_p(p + 4);
    }
    return 0x00;
}

static void ble_command(WB55IpccState *s, uint16_t op, const uint8_t *p,
                        int plen)
{
    WB55BleState *b = &s->ble;
    uint8_t rp[16] = { ST_OK };
    int rlen = 1;
    WB55BleService *svc;
    WB55BleAttr *a;

    if (!b->up) {
        rp[0] = ST_DISALLOWED;
        ble_complete(s, op, rp, rlen);
        return;
    }
    switch (op) {
    /* ---- HCI */
    case OP_HCI_RESET:
        b->adv = 0;
        b->beacon = 0;
        break;
    case OP_HCI_READ_LOCAL_VERSION:
        rp[1] = HCI_VERSION;
        put16(rp + 2, (s->stack_version >> 8) & 0xFFFF);
        rp[4] = HCI_VERSION;
        put16(rp + 5, COMPANY_ST);
        put16(rp + 7, s->stack_version >> 16);
        rlen = 9;
        break;
    case OP_LE_SET_SCAN_RSP_DATA:
        if (plen >= 1) {
            b->scan_rsp_len = MIN(p[0], MIN(plen - 1, 31));
            memcpy(b->scan_rsp, p + 1, b->scan_rsp_len);
        }
        break;
    case OP_LE_READ_PHY:
        rp[0] = ST_UNKNOWN_CONN;        /* no connection */
        put16(rp + 1, plen >= 2 ? le16(p) : 0);
        rlen = 5;
        break;
    case OP_LE_TEST_END:
        rlen = 3;                       /* no packets */
        break;
    case OP_HAL_LE_TX_TEST_PACKETS:
        rlen = 5;
        break;
    case OP_HAL_READ_RAW_RSSI:
        rlen = 4;                       /* nothing heard: -127 dBm */
        break;
    /* ---- HAL */
    case OP_HAL_WRITE_CONFIG_DATA:
        if (plen >= 2 && p[0] == 0x00 && p[1] >= 6 && plen >= 8) {
            memcpy(b->public_addr, p + 2, 6);   /* the public address */
        }
        break;
    case OP_HAL_SET_TX_POWER_LEVEL:
        if (plen >= 2) {
            b->tx_high = p[0];
            b->tx_level = p[1];
        }
        break;
    /* ---- GAP */
    case OP_GAP_INIT:
        rp[0] = plen >= 3 ? gap_init(b, p[0], p[2]) : ST_INVALID_PARAMS;
        put16(rp + 1, b->gap_service);
        put16(rp + 3, b->name_char);
        put16(rp + 5, b->appearance_char);
        rlen = 7;
        break;
    case OP_GAP_SET_DISCOVERABLE: {
        int at = 8, n;

        if (plen < 8) {
            rp[0] = ST_INVALID_PARAMS;
            break;
        }
        b->adv_type = p[0];
        b->adv_min = le16(p + 1);
        b->adv_max = le16(p + 3);
        b->own_addr_type = p[5];
        b->adv_filter = p[6];
        n = MIN(p[7], MIN(plen - at, (int)sizeof(b->name)));
        b->name_len = n;
        memcpy(b->name, p + at, n);
        at += p[7];
        if (at < plen) {
            n = MIN(p[at], MIN(plen - at - 1, (int)sizeof(b->uuids)));
            b->uuids_len = MAX(n, 0);
            memcpy(b->uuids, p + at + 1, b->uuids_len);
            at += 1 + p[at];
        }
        if (at + 4 <= plen) {
            b->conn_min = le16(p + at);
            b->conn_max = le16(p + at + 2);
        }
        b->adv = 1;
        break;
    }
    case OP_GAP_SET_NON_DISCOVERABLE:
        b->adv = 0;
        break;
    case OP_GAP_SET_IO_CAPABILITY:
        b->io_cap = plen >= 1 ? p[0] : 0;
        break;
    case OP_GAP_SET_AUTH_REQUIREMENT:
        if (plen >= 9) {
            b->bonding = p[0];
            b->mitm = p[1];
            b->sc = p[2];
            b->fixed_pin_used = p[6];
            b->fixed_pin = ldl_le_p(p + 7);
        }
        break;
    case OP_GAP_PASS_KEY_RESP:
    case OP_GAP_ALLOW_REBOND:
    case OP_GAP_NUMERIC_CONFIRM:
        rp[0] = ST_UNKNOWN_CONN;        /* no connection: nothing to answer */
        break;
    case OP_GAP_PERIPHERAL_SEC_REQ:
    case OP_GAP_TERMINATE:
    case OP_L2CAP_CONN_PARAM_UPDATE:
        ble_status(s, op, ST_UNKNOWN_CONN);
        return;
    case OP_GAP_BEACON_START:
        b->beacon = 1;
        break;
    case OP_GAP_BEACON_STOP:
        b->beacon = 0;
        break;
    case OP_GAP_BEACON_SET_DATA:        /* (an extra beacon's data: not kept) */
    case OP_GAP_CONFIGURE_FILTER:
    case OP_GAP_CLEAR_SECURITY_DB:      /* (no bonds are kept yet) */
    case OP_LE_SET_DEFAULT_PHY:
    /* the radio's tests: no radio, nothing sent or heard */
    case OP_HAL_TONE_START:
    case OP_HAL_TONE_STOP:
    case OP_HAL_RX_START:
    case OP_HAL_RX_STOP:
    case OP_LE_RECEIVER_TEST_V2:
    case OP_LE_TRANSMITTER_TEST_V2:
        break;
    /* ---- GATT */
    case OP_GATT_INIT:
        gatt_init(b);
        break;
    case OP_GATT_ADD_SERVICE: {
        int ulen = plen >= 1 && p[0] == 2 ? 16 : 2;

        svc = plen >= 3 + ulen ? svc_new(b, p[0], p + 1, p[1 + ulen] == 1,
                                         p[2 + ulen]) : NULL;
        rp[0] = svc ? ST_OK : ST_NO_RESOURCES;
        put16(rp + 1, svc ? svc->handle : 0);
        rlen = 3;
        break;
    }
    case OP_GATT_ADD_CHAR: {
        int ulen = plen >= 3 && p[2] == 2 ? 16 : 2;
        const uint8_t *q = p + 3 + ulen;
        uint16_t h = 0;

        if (plen < 3 + ulen + 7) {
            rp[0] = ST_INVALID_PARAMS;
        } else if (!(svc = svc_find(b, le16(p)))) {
            rp[0] = ST_INVALID_HANDLE;
        } else {
            h = char_new(b, svc, p[2], p + 3, le16(q), q[2], q[3], q[4], q[6]);
            rp[0] = h ? ST_OK : ST_OUT_OF_HANDLE;
        }
        put16(rp + 1, h);
        rlen = 3;
        break;
    }
    case OP_GATT_ADD_CHAR_DESC: {
        int ulen = plen >= 5 && p[4] == 2 ? 16 : 2;
        const uint8_t *q = p + 5 + ulen;
        uint16_t h = 0;

        if (plen < 5 + ulen + 2 || plen < 5 + ulen + 2 + q[1] + 5) {
            rp[0] = ST_INVALID_PARAMS;
        } else if (!(svc = svc_find(b, le16(p))) || svc->next > svc->last) {
            rp[0] = svc ? ST_OUT_OF_HANDLE : ST_INVALID_HANDLE;
        } else if (!(a = attr_new(b, svc->next, svc->handle,
                                  WB55_BLE_ATTR_DESC, MAX(q[0], q[1])))) {
            rp[0] = ST_NO_RESOURCES;
        } else {
            const uint8_t *r = q + 2 + q[1];

            attr_uuid(a, p[4], p + 5);
            a->perms = r[0];
            a->evt_mask = r[2];
            a->variable = r[4];
            a->len = a->variable ? 0 : a->max_len;
            attr_set(b, a, 0, q + 2, q[1]);
            h = svc->next++;
        }
        put16(rp + 1, h);
        rlen = 3;
        break;
    }
    case OP_GATT_UPDATE_CHAR_VALUE:
        if (plen < 6 || plen < 6 + p[5]) {
            rp[0] = ST_INVALID_PARAMS;
        } else if (!(a = char_value(b, le16(p), le16(p + 2)))) {
            rp[0] = ST_INVALID_HANDLE;
        } else if (p[4] + p[5] > a->max_len) {
            rp[0] = ST_INVALID_PARAMS;
        } else {
            attr_set(b, a, p[4], p + 6, p[5]);
        }
        break;
    case OP_GATT_UPDATE_CHAR_VALUE_EXT:
        if (plen < 12 || plen < 12 + p[11]) {
            rp[0] = ST_INVALID_PARAMS;
        } else if (!(a = char_value(b, le16(p + 2), le16(p + 4)))) {
            rp[0] = ST_INVALID_HANDLE;
        } else if (le16(p + 9) + p[11] > a->max_len ||
                   le16(p + 7) > a->max_len) {
            rp[0] = ST_INVALID_PARAMS;
        } else {
            attr_set(b, a, le16(p + 9), p + 12, p[11]);
            if (a->variable) {
                a->len = le16(p + 7);   /* Char_Length: the whole of it */
            }
        }
        break;
    case OP_GATT_DEL_CHAR:
        if (plen < 4 || !(svc = svc_find(b, le16(p))) ||
            !char_value(b, le16(p), le16(p + 2))) {
            rp[0] = ST_INVALID_HANDLE;
        } else {
            char_del(b, svc, le16(p + 2));
        }
        break;
    case OP_GATT_DEL_SERVICE:
        if (plen < 2 || !(svc = svc_find(b, le16(p)))) {
            rp[0] = ST_INVALID_HANDLE;
        } else {
            for (int i = 0; i < b->nattrs; i++) {
                if (b->attr[i].service == svc->handle) {
                    b->attr[i].kind = WB55_BLE_ATTR_FREE;
                }
            }
            memset(svc, 0, sizeof(*svc));
        }
        break;
    default:
        /*
         * Of the many commands of ST's stack, the firmwares here send the
         * ones above.  Another one is answered as done (a Command Complete
         * with the status alone: the caller's other fields stay zero), and
         * said once in QEMU's log of what is not done.
         */
        if (b->unknown_log < 32) {
            b->unknown_log++;
            qemu_log_mask(LOG_UNIMP, "flipper-cpu2: BLE command 0x%04x: "
                          "answered as done\n", op);
        }
        break;
    }
    ble_complete(s, op, rp, rlen);
}

/* IPCC channel 1 from CPU1: a command in the BLE table's command buffer */
void wb55_ble_command(WB55IpccState *s)
{
    hwaddr ble = tl_rd32(wb55_ipcc_ref_table(s) + TL_REF_BLE);
    hwaddr cmd = ble ? tl_rd32(ble + TL_BLE_CMD_BUFFER) : 0;
    uint8_t p[255];
    uint16_t op;
    int plen;

    if (!cmd) {
        return;
    }
    /* TL_CmdPacket_t: header | type | opcode (2) | length | parameters */
    op = tl_rd8(cmd + TL_PKT_HEADER + 1) |
         (tl_rd8(cmd + TL_PKT_HEADER + 2) << 8);
    plen = tl_rd8(cmd + TL_PKT_HEADER + 3);
    tl_read(cmd + TL_PKT_HEADER + 4, p, plen);
    ble_command(s, op, p, plen);
}

/* ---------------------------------------------------------- saved state */

static bool ble_state_ok(void *opaque, int version_id)
{
    WB55BleState *b = opaque;

    if (b->nattrs > WB55_BLE_ATTRS || b->values_used > WB55_BLE_VALUES ||
        b->npending > WB55_BLE_PENDING || b->name_len > sizeof(b->name) ||
        b->uuids_len > sizeof(b->uuids) ||
        b->scan_rsp_len > sizeof(b->scan_rsp)) {
        return false;
    }
    for (int i = 0; i < b->nattrs; i++) {
        if (b->attr[i].at + b->attr[i].max_len > WB55_BLE_VALUES ||
            b->attr[i].len > b->attr[i].max_len) {
            return false;
        }
    }
    return true;
}

static const VMStateDescription vmstate_wb55_ble_attr = {
    .name = "stm32wb55-ble/attr",
    .version_id = 1,
    .minimum_version_id = 1,
    .fields = (const VMStateField[]) {
        VMSTATE_UINT16(handle, WB55BleAttr),
        VMSTATE_UINT16(service, WB55BleAttr),
        VMSTATE_UINT8(kind, WB55BleAttr),
        VMSTATE_UINT8(uuid_type, WB55BleAttr),
        VMSTATE_UINT8_ARRAY(uuid, WB55BleAttr, 16),
        VMSTATE_UINT8(props, WB55BleAttr),
        VMSTATE_UINT8(perms, WB55BleAttr),
        VMSTATE_UINT8(evt_mask, WB55BleAttr),
        VMSTATE_UINT8(variable, WB55BleAttr),
        VMSTATE_UINT16(max_len, WB55BleAttr),
        VMSTATE_UINT16(len, WB55BleAttr),
        VMSTATE_UINT16(at, WB55BleAttr),
        VMSTATE_END_OF_LIST()
    }
};

static const VMStateDescription vmstate_wb55_ble_svc = {
    .name = "stm32wb55-ble/service",
    .version_id = 1,
    .minimum_version_id = 1,
    .fields = (const VMStateField[]) {
        VMSTATE_UINT16(handle, WB55BleService),
        VMSTATE_UINT16(last, WB55BleService),
        VMSTATE_UINT16(next, WB55BleService),
        VMSTATE_END_OF_LIST()
    }
};

static const VMStateDescription vmstate_wb55_ble_event = {
    .name = "stm32wb55-ble/event",
    .version_id = 1,
    .minimum_version_id = 1,
    .fields = (const VMStateField[]) {
        VMSTATE_UINT8(buf, WB55BleEvent),
        VMSTATE_UINT8(code, WB55BleEvent),
        VMSTATE_UINT8(len, WB55BleEvent),
        VMSTATE_UINT8_ARRAY(data, WB55BleEvent, WB55_BLE_EVT_LEN),
        VMSTATE_END_OF_LIST()
    }
};

const VMStateDescription vmstate_wb55_ble = {
    .name = "stm32wb55-ble",
    .version_id = 1,
    .minimum_version_id = 1,
    .fields = (const VMStateField[]) {
        VMSTATE_BOOL(up, WB55BleState),
        VMSTATE_UINT16(max_attrs, WB55BleState),
        VMSTATE_UINT16(max_services, WB55BleState),
        VMSTATE_UINT16(max_values, WB55BleState),
        VMSTATE_STRUCT_ARRAY(svc, WB55BleState, WB55_BLE_SERVICES, 1,
                             vmstate_wb55_ble_svc, WB55BleService),
        VMSTATE_STRUCT_ARRAY(attr, WB55BleState, WB55_BLE_ATTRS, 1,
                             vmstate_wb55_ble_attr, WB55BleAttr),
        VMSTATE_UINT8_ARRAY(values, WB55BleState, WB55_BLE_VALUES),
        VMSTATE_UINT16(nattrs, WB55BleState),
        VMSTATE_UINT16(values_used, WB55BleState),
        VMSTATE_UINT16(next_handle, WB55BleState),
        VMSTATE_UINT16(gap_service, WB55BleState),
        VMSTATE_UINT16(name_char, WB55BleState),
        VMSTATE_UINT16(appearance_char, WB55BleState),
        VMSTATE_UINT8(role, WB55BleState),
        VMSTATE_UINT8(adv, WB55BleState),
        VMSTATE_UINT8(adv_type, WB55BleState),
        VMSTATE_UINT8(own_addr_type, WB55BleState),
        VMSTATE_UINT8(adv_filter, WB55BleState),
        VMSTATE_UINT16(adv_min, WB55BleState),
        VMSTATE_UINT16(adv_max, WB55BleState),
        VMSTATE_UINT16(conn_min, WB55BleState),
        VMSTATE_UINT16(conn_max, WB55BleState),
        VMSTATE_UINT8(name_len, WB55BleState),
        VMSTATE_UINT8_ARRAY(name, WB55BleState, 32),
        VMSTATE_UINT8(uuids_len, WB55BleState),
        VMSTATE_UINT8_ARRAY(uuids, WB55BleState, 32),
        VMSTATE_UINT8(scan_rsp_len, WB55BleState),
        VMSTATE_UINT8_ARRAY(scan_rsp, WB55BleState, 31),
        VMSTATE_UINT8_ARRAY(public_addr, WB55BleState, 6),
        VMSTATE_UINT8(io_cap, WB55BleState),
        VMSTATE_UINT8(bonding, WB55BleState),
        VMSTATE_UINT8(mitm, WB55BleState),
        VMSTATE_UINT8(sc, WB55BleState),
        VMSTATE_UINT8(fixed_pin_used, WB55BleState),
        VMSTATE_UINT32(fixed_pin, WB55BleState),
        VMSTATE_UINT8(tx_high, WB55BleState),
        VMSTATE_UINT8(tx_level, WB55BleState),
        VMSTATE_UINT8(beacon, WB55BleState),
        VMSTATE_UINT32(nvm_ram, WB55BleState),
        VMSTATE_UINT32(evt_mask1, WB55BleState),
        VMSTATE_UINT32(slots_busy, WB55BleState),
        VMSTATE_BOOL(spare_ble_busy, WB55BleState),
        VMSTATE_BOOL(spare_sys_busy, WB55BleState),
        VMSTATE_STRUCT_ARRAY(pending, WB55BleState, WB55_BLE_PENDING, 1,
                             vmstate_wb55_ble_event, WB55BleEvent),
        VMSTATE_UINT8(npending, WB55BleState),
        VMSTATE_VALIDATE("its sizes are within their arrays", ble_state_ok),
        VMSTATE_END_OF_LIST()
    }
};
