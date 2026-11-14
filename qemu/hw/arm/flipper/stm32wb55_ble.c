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
#include "flipper.h"

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
#define CHAR_PROP_WRITE_NO_RSP 0x04
#define CHAR_PROP_WRITE       0x08
#define CHAR_PROP_NOTIFY      0x10
#define CHAR_PROP_INDICATE    0x20

/* security permissions; a descriptor's access; ble_defs.h */
#define PERM_AUTHEN_READ      0x01
#define PERM_ENCRY_READ       0x04
#define PERM_AUTHEN_WRITE     0x08
#define PERM_ENCRY_WRITE      0x20
#define ACCESS_READ           0x01
#define ACCESS_WRITE          0x02
#define ACCESS_WRITE_NO_RSP   0x04
#define GATT_NOTIFY_ATTRIBUTE_WRITE 0x01

/* the events of a connection: HCI's, and ST's own (vendor, 0xFF) */
#define EVT_DISCONNECTION     0x05
#define EVT_ENCRYPTION_CHANGE 0x08
#define EVT_LE_META           0x3E
#define EVT_VENDOR            0xFF
#define LE_CONNECTION_COMPLETE        0x01
#define LE_CONNECTION_UPDATE_COMPLETE 0x03
#define VS_GAP_PAIRING_COMPLETE       0x0401
#define VS_GAP_PASS_KEY_REQ           0x0402
#define VS_GAP_PERIPHERAL_SEC_INIT    0x0404
#define VS_GAP_NUMERIC_COMPARISON     0x0409
#define VS_L2CAP_CONN_UPDATE_RESP     0x0800
#define VS_GATT_ATTRIBUTE_MODIFIED    0x0C01
#define VS_ATT_EXCHANGE_MTU_RESP      0x0C03
#define VS_GATT_SERVER_CONFIRMATION   0x0C17

/* why a connection ended (HCI's error codes) */
#define HCI_CONN_TIMEOUT      0x08
#define HCI_REMOTE_USER_TERM  0x13
#define HCI_LOCAL_HOST_TERM   0x16

/* ATT's errors, as a central gets them */
#define ATT_INVALID_HANDLE    0x01
#define ATT_READ_NOT_PERMITTED 0x02
#define ATT_WRITE_NOT_PERMITTED 0x03
#define ATT_INSUFFICIENT_AUTHEN 0x05
#define ATT_INVALID_LENGTH    0x0D

/* SMP: a pairing's end (ACI_GAP_PAIRING_COMPLETE: status, reason) */
#define SMP_FAILED            0x02
#define SMP_PASSKEY_ENTRY_FAILED 0x01
#define SMP_AUTH_REQUIREMENTS 0x03
#define SMP_CONFIRM_VALUE_FAILED 0x04
#define SMP_NUM_COMPARISON_FAILED 0x0C

/* IO capabilities, the Flipper's (aci_gap_set_io_capability) or a central's */
#define IO_DISPLAY_ONLY       0x00
#define IO_DISPLAY_YES_NO     0x01
#define IO_KEYBOARD_ONLY      0x02
#define IO_NONE               0x03
#define IO_KEYBOARD_DISPLAY   0x04

#define ADV_SCAN_IND          0x02
#define ADV_NONCONN_IND       0x03

#define CONN_HANDLE           0x0801   /* the one connection's handle */
#define ATT_MTU_MIN           23

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
    if (s->ble_link && s->ble_link->held &&
        b->npending + 4 <= WB55_BLE_PENDING) {
        flipper_link_resume(s->ble_link);   /* the central's next lines */
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

/* ------------------------------------------------------ the connection */
/*
 * A central on the link (devices/NAME/ble.sock; docs/RADIO_LINK.md): a
 * program on the host plays the phone.  It sees the advertising, connects,
 * pairs, and reads, writes and subscribes to what the firmware's services
 * hold.  The link carries what goes through the air at the level of GAP, SMP
 * and ATT: no packets, no keys.  To CPU1 it is ST's stack that says what a
 * real central did: the connection's events, the pairing's, a client's
 * writes; and what CPU1 updates goes to the central as a notification or an
 * indication, if the central has asked for them in the CCCD.
 */

static void G_GNUC_PRINTF(2, 3) ble_say(WB55IpccState *s, const char *fmt, ...)
{
    va_list ap;
    char *line;

    if (!s->ble_link || !s->ble_link->open) {
        return;
    }
    va_start(ap, fmt);
    line = g_strdup_vprintf(fmt, ap);
    va_end(ap);
    flipper_link_send(s->ble_link, "%s\n", line);
    g_free(line);
}

/* An event of ST's own: its code, then its parameters */
static void ble_vendor(WB55IpccState *s, uint16_t ecode, const uint8_t *p,
                       int len)
{
    uint8_t d[WB55_BLE_EVT_LEN];

    len = MIN(len, WB55_BLE_EVT_LEN - 2);
    put16(d, ecode);
    if (len > 0) {
        memcpy(d + 2, p, len);
    }
    wb55_ble_post(s, WB55_BLE_BUF_POOL, EVT_VENDOR, d, 2 + len);
}

static char *hex(const uint8_t *p, int n)
{
    GString *g = g_string_sized_new(2 * n + 1);

    for (int i = 0; i < n; i++) {
        g_string_append_printf(g, "%02x", p[i]);
    }
    return g_string_free(g, false);
}

/* -> the number of bytes, or -1: not hex, or more than max */
static int unhex(const char *t, uint8_t *out, int max)
{
    int n = 0;

    for (; t && t[0]; t += 2) {
        int hi = g_ascii_xdigit_value(t[0]);
        int lo = t[1] ? g_ascii_xdigit_value(t[1]) : -1;

        if (hi < 0 || lo < 0 || n == max) {
            return -1;
        }
        out[n++] = hi << 4 | lo;
    }
    return n;
}

static void addr_str(char *out, const uint8_t *a)
{
    snprintf(out, 18, "%02X:%02X:%02X:%02X:%02X:%02X",
             a[5], a[4], a[3], a[2], a[1], a[0]);
}

static bool addr_parse(const char *t, uint8_t *a)
{
    unsigned v[6];
    char end;

    if (sscanf(t, "%x:%x:%x:%x:%x:%x%c", &v[0], &v[1], &v[2], &v[3], &v[4],
               &v[5], &end) != 6) {
        return false;
    }
    for (int i = 0; i < 6; i++) {
        if (v[i] > 0xFF) {
            return false;
        }
        a[5 - i] = v[i];
    }
    return true;
}

/* A number in [lo, hi]: decimal, or 0x... */
static bool num(const char *t, int base, uint64_t lo, uint64_t hi,
                uint64_t *v)
{
    char *end;

    if (!t || !*t) {
        return false;
    }
    *v = g_ascii_strtoull(t, &end, base);
    return !*end && *v >= lo && *v <= hi;
}

/* A line's words, empty ones left out */
static char **words(char *line)
{
    char **w = g_strsplit_set(line, " \t", -1);
    int j = 0;

    for (int i = 0; w[i]; i++) {
        if (*w[i]) {
            w[j++] = w[i];
        } else {
            g_free(w[i]);
        }
    }
    w[j] = NULL;
    return w;
}

/* The value of KEY=VALUE among the words after the first, or NULL */
static const char *arg(char **w, const char *key)
{
    size_t n = strlen(key);

    for (int i = 1; w[i]; i++) {
        if (!strncmp(w[i], key, n) && w[i][n] == '=') {
            return w[i] + n + 1;
        }
    }
    return NULL;
}

static void uuid_str(char *out, const WB55BleAttr *a)
{
    if (a->uuid_type == 1) {
        snprintf(out, 37, "%04x", le16(a->uuid));
        return;
    }
    /* 128 bits: as written, the most significant byte first */
    for (int i = 15, at = 0; i >= 0; i--) {
        at += sprintf(out + at, "%02x", a->uuid[i]);
        if (i == 12 || i == 10 || i == 8 || i == 6) {
            out[at++] = '-';
        }
    }
}

/* The advertising, as a scanner sees it: sent when it changes, and to a
 * program that has just connected */
static void ble_adv_announce(WB55IpccState *s)
{
    WB55BleState *b = &s->ble;
    char *line;

    if (b->up && b->adv && !b->conn) {
        const uint8_t *own = b->own_addr_type ? b->random_addr
                                              : b->public_addr;
        uint8_t ad[31];
        int n = 0;
        char a[18];
        g_autofree char *data = NULL, *scan = NULL;

        /* what the stack puts in it: the flags (LE General Discoverable,
         * no BR/EDR), then the local name and the services' UUIDs that
         * aci_gap_set_discoverable() gave, each as far as there is room */
        ad[n++] = 2;
        ad[n++] = 0x01;
        ad[n++] = 0x06;
        if (b->name_len && n + 1 + b->name_len <= (int)sizeof(ad)) {
            ad[n++] = b->name_len;
            memcpy(ad + n, b->name, b->name_len);
            n += b->name_len;
        }
        if (b->uuids_len > 1 && n + 1 + b->uuids_len <= (int)sizeof(ad)) {
            ad[n++] = b->uuids_len;
            memcpy(ad + n, b->uuids, b->uuids_len);
            n += b->uuids_len;
        }
        addr_str(a, own);
        data = hex(ad, n);
        scan = hex(b->scan_rsp, b->scan_rsp_len);
        line = g_strdup_printf("adv on addr=%s addr_type=%s connectable=%d "
                               "interval=%u-%u data=%s scan=%s", a,
                               b->own_addr_type ? "random" : "public",
                               b->adv_type < ADV_SCAN_IND, b->adv_min,
                               b->adv_max, data, scan);
    } else {
        line = g_strdup("adv off");
    }
    if (g_strcmp0(line, s->ble_adv)) {
        g_free(s->ble_adv);
        s->ble_adv = line;
        ble_say(s, "%s", line);
    } else {
        g_free(line);
    }
}

/* The connection is over, and what it had: no bonds are kept, so the
 * client's CCCDs go back to nothing too */
static void ble_conn_drop(WB55BleState *b)
{
    b->conn = 0;
    b->sec = 0;
    b->pair = WB55_BLE_PAIR_NONE;
    b->pair_flags = 0;
    b->indications = 0;
    b->mtu = ATT_MTU_MIN;
    for (int i = 0; i < b->nattrs; i++) {
        if (b->attr[i].kind == WB55_BLE_ATTR_CCCD) {
            memset(&b->values[b->attr[i].at], 0, 2);
        }
    }
}

/* It ends, and CPU1 hears so (HCI_DISCONNECTION_COMPLETE, the reason as
 * CPU1's side has it) */
static void ble_disconnected(WB55IpccState *s, uint8_t reason)
{
    WB55BleState *b = &s->ble;
    uint8_t d[4] = { 0x00, b->conn & 0xFF, b->conn >> 8, reason };

    wb55_ble_post(s, WB55_BLE_BUF_POOL, EVT_DISCONNECTION, d, sizeof(d));
    ble_conn_drop(b);
}

static void ble_connect(WB55IpccState *s, char **w)
{
    /* a central's address when the line gives none: a static random one */
    static const uint8_t phone[6] = { 0x01, 0x23, 0x45, 0x67, 0x89, 0xC6 };
    WB55BleState *b = &s->ble;
    const char *t;
    uint64_t v;
    uint8_t d[19];

    if (b->conn) {
        ble_say(s, "error op=connect reason=connected");
        return;
    }
    if (!b->up || !b->adv) {
        ble_say(s, "error op=connect reason=not-advertising");
        return;
    }
    if (b->adv_type >= ADV_SCAN_IND) {
        ble_say(s, "error op=connect reason=not-connectable");
        return;
    }
    memcpy(b->peer, phone, 6);
    b->peer_type = 1;
    if ((t = arg(w, "addr")) && !addr_parse(t, b->peer)) {
        ble_say(s, "error op=connect reason=addr");
        return;
    }
    if ((t = arg(w, "addr_type"))) {
        b->peer_type = strcmp(t, "public") != 0;
    }
    /* 30 ms, as phones like to start; any the firmware asks for later */
    b->interval = 24;
    b->latency = 0;
    b->timeout = 400;
    if ((t = arg(w, "interval"))) {
        if (!num(t, 0, 6, 3200, &v)) {
            ble_say(s, "error op=connect reason=interval");
            return;
        }
        b->interval = v;
    }
    ble_conn_drop(b);
    b->conn = CONN_HANDLE;
    b->adv = 0;                         /* a connection ends the advertising */
    d[0] = LE_CONNECTION_COMPLETE;
    d[1] = 0x00;
    put16(d + 2, b->conn);
    d[4] = 0x01;                        /* the Flipper's role: peripheral */
    d[5] = b->peer_type;
    memcpy(d + 6, b->peer, 6);
    put16(d + 12, b->interval);
    put16(d + 14, b->latency);
    put16(d + 16, b->timeout);
    d[18] = 0x00;                       /* the central's clock: 500 ppm */
    wb55_ble_post(s, WB55_BLE_BUF_POOL, EVT_LE_META, d, sizeof(d));
    ble_say(s, "connected handle=0x%04x interval=%u latency=%u timeout=%u "
            "mtu=%u", b->conn, b->interval, b->latency, b->timeout, b->mtu);
    ble_adv_announce(s);
}

/* The parameters that CPU1 asked for (an L2CAP request): the central takes
 * the shortest interval of them */
static void ble_conn_update(WB55IpccState *s, uint16_t min, uint16_t latency,
                            uint16_t timeout)
{
    WB55BleState *b = &s->ble;
    uint8_t r[4], d[10];

    put16(r, b->conn);
    put16(r + 2, 0x0000);               /* accepted */
    ble_vendor(s, VS_L2CAP_CONN_UPDATE_RESP, r, sizeof(r));
    b->interval = MAX(min, 6);
    b->latency = latency;
    if (timeout) {
        b->timeout = timeout;
    }
    d[0] = LE_CONNECTION_UPDATE_COMPLETE;
    d[1] = 0x00;
    put16(d + 2, b->conn);
    put16(d + 4, b->interval);
    put16(d + 6, b->latency);
    put16(d + 8, b->timeout);
    wb55_ble_post(s, WB55_BLE_BUF_POOL, EVT_LE_META, d, sizeof(d));
    ble_say(s, "params interval=%u latency=%u timeout=%u", b->interval,
            b->latency, b->timeout);
}

/* ---- pairing (SMP), the Flipper the responder */

enum { METHOD_JUST_WORKS, METHOD_PASSKEY, METHOD_PASSKEY_SHOW,
       METHOD_COMPARE };

/* How two sides pair, by their IO capabilities (Core 5.4, Vol 3, Part H,
 * 2.3.5.1): the initiator is the central, the responder the Flipper.
 * PASSKEY: the Flipper shows the key (CPU1's), the central enters it;
 * PASSKEY_SHOW: the central shows one, and the Flipper is to enter it. */
static int pair_method(int ini, int rsp, bool sc)
{
    bool ini_kbd = ini == IO_KEYBOARD_ONLY || ini == IO_KEYBOARD_DISPLAY;
    bool rsp_kbd = rsp == IO_KEYBOARD_ONLY || rsp == IO_KEYBOARD_DISPLAY;
    bool ini_disp = ini == IO_DISPLAY_ONLY || ini == IO_DISPLAY_YES_NO ||
                    ini == IO_KEYBOARD_DISPLAY;
    bool rsp_disp = rsp == IO_DISPLAY_ONLY || rsp == IO_DISPLAY_YES_NO ||
                    rsp == IO_KEYBOARD_DISPLAY;
    bool ini_yn = ini == IO_DISPLAY_YES_NO || ini == IO_KEYBOARD_DISPLAY;
    bool rsp_yn = rsp == IO_DISPLAY_YES_NO || rsp == IO_KEYBOARD_DISPLAY;

    if (ini == IO_NONE || rsp == IO_NONE) {
        return METHOD_JUST_WORKS;
    }
    if (sc && ini_yn && rsp_yn) {
        return METHOD_COMPARE;
    }
    if (ini_kbd && rsp_disp) {
        return METHOD_PASSKEY;
    }
    if (rsp_kbd && (ini_disp || ini_kbd)) {
        return METHOD_PASSKEY_SHOW;
    }
    return METHOD_JUST_WORKS;
}

/* The pairing's end: reason 0 = paired (the link encrypted first) */
static void ble_pair_done(WB55IpccState *s, uint8_t reason)
{
    WB55BleState *b = &s->ble;
    uint8_t d[4];

    if (!reason) {
        uint8_t e[4] = { 0x00, b->conn & 0xFF, b->conn >> 8, 0x01 };

        wb55_ble_post(s, WB55_BLE_BUF_POOL, EVT_ENCRYPTION_CHANGE, e,
                      sizeof(e));
        b->sec = WB55_BLE_SEC_ENCRYPTED |
            (b->pair_flags & WB55_BLE_PF_AUTH ? WB55_BLE_SEC_AUTHENTICATED : 0) |
            (b->pair_flags & WB55_BLE_PF_BOND ? WB55_BLE_SEC_BONDED : 0);
    }
    put16(d, b->conn);
    d[2] = reason ? SMP_FAILED : 0x00;
    d[3] = reason;
    ble_vendor(s, VS_GAP_PAIRING_COMPLETE, d, sizeof(d));
    if (reason) {
        ble_say(s, "pairing failed reason=0x%02x", reason);
    } else {
        ble_say(s, "paired authenticated=%d bonded=%d sc=%d",
                !!(b->sec & WB55_BLE_SEC_AUTHENTICATED),
                !!(b->sec & WB55_BLE_SEC_BONDED),
                !!(b->pair_flags & WB55_BLE_PF_SC));
    }
    b->pair = WB55_BLE_PAIR_NONE;
    b->pair_flags = 0;
}

/* After an answer, CPU1's or the central's: ask the central, or end it */
static void ble_pair_progress(WB55IpccState *s)
{
    WB55BleState *b = &s->ble;
    uint8_t f = b->pair_flags;
    bool both = (f & WB55_BLE_PF_FLIPPER) && (f & WB55_BLE_PF_CENTRAL);

    if (b->pair == WB55_BLE_PAIR_PASSKEY) {
        if ((f & WB55_BLE_PF_FLIPPER) && !(f & WB55_BLE_PF_ASKED)) {
            /* the key is on the Flipper's screen now: the central's user
             * reads it there */
            b->pair_flags |= WB55_BLE_PF_ASKED;
            ble_say(s, "pairing passkey enter");
        }
        if (both) {
            ble_pair_done(s, b->pair_key == b->pair_entered
                             ? 0 : SMP_CONFIRM_VALUE_FAILED);
        }
    } else if (b->pair == WB55_BLE_PAIR_COMPARE && both) {
        ble_pair_done(s, (f & WB55_BLE_PF_FLIPPER_YES) &&
                         (f & WB55_BLE_PF_CENTRAL_YES)
                         ? 0 : SMP_NUM_COMPARISON_FAILED);
    }
}

/* pair [io=keyboard-display|keyboard|display|yesno|none] [bond=0|1]
 *      [sc=0|1] [mitm=0|1]: the central's Pairing Request */
static void ble_pair(WB55IpccState *s, char **w)
{
    static const char *const ios[] = { "display", "yesno", "keyboard", "none",
                                       "keyboard-display" };
    WB55BleState *b = &s->ble;
    const char *t;
    int io = IO_KEYBOARD_DISPLAY, method;
    bool sc, mitm;
    uint8_t d[6];

    if (!b->conn) {
        ble_say(s, "error op=pair reason=not-connected");
        return;
    }
    if (b->pair) {
        ble_say(s, "error op=pair reason=pairing");
        return;
    }
    if ((t = arg(w, "io"))) {
        for (io = 0; io < (int)ARRAY_SIZE(ios) && strcmp(t, ios[io]); io++) {
        }
        if (io == (int)ARRAY_SIZE(ios)) {
            ble_say(s, "error op=pair reason=io");
            return;
        }
    }
    sc = g_strcmp0(arg(w, "sc"), "0") && b->sc;
    mitm = g_strcmp0(arg(w, "mitm"), "0");
    method = pair_method(io, b->io_cap, sc);
    b->pair_flags = (sc ? WB55_BLE_PF_SC : 0) |
        (g_strcmp0(arg(w, "bond"), "0") && b->bonding ? WB55_BLE_PF_BOND : 0);
    if (method == METHOD_JUST_WORKS) {
        /* no protection against a man in the middle: whoever wants one
         * makes it fail */
        ble_pair_done(s, b->mitm || mitm ? SMP_AUTH_REQUIREMENTS : 0);
        return;
    }
    b->pair_flags |= WB55_BLE_PF_AUTH;
    put16(d, b->conn);
    if (method == METHOD_COMPARE) {
        b->pair = WB55_BLE_PAIR_COMPARE;
        b->pair_key = g_random_int_range(0, 1000000);
        stl_le_p(d + 2, b->pair_key);
        b->pair_flags |= WB55_BLE_PF_ASKED;
        ble_vendor(s, VS_GAP_NUMERIC_COMPARISON, d, 6);
        ble_say(s, "pairing compare %06u", b->pair_key);
        return;
    }
    b->pair = WB55_BLE_PAIR_PASSKEY;
    if (method == METHOD_PASSKEY_SHOW) {
        /* the central shows a key of its own (its `passkey` line), which
         * the Flipper's user would have to enter */
        b->pair_flags |= WB55_BLE_PF_ASKED;
        ble_say(s, "pairing passkey show");
    }
    ble_vendor(s, VS_GAP_PASS_KEY_REQ, d, 2);
}

static void ble_passkey(WB55IpccState *s, char **w)
{
    WB55BleState *b = &s->ble;
    uint64_t v;

    if (b->pair != WB55_BLE_PAIR_PASSKEY) {
        ble_say(s, "error op=passkey reason=not-asked");
    } else if (!g_strcmp0(w[1], "cancel")) {
        ble_pair_done(s, SMP_PASSKEY_ENTRY_FAILED);
    } else if (!num(w[1], 10, 0, 999999, &v)) {
        ble_say(s, "error op=passkey reason=number");
    } else {
        b->pair_entered = v;
        b->pair_flags |= WB55_BLE_PF_CENTRAL;
        ble_pair_progress(s);
    }
}

static void ble_confirm(WB55IpccState *s, char **w)
{
    WB55BleState *b = &s->ble;

    if (b->pair != WB55_BLE_PAIR_COMPARE) {
        ble_say(s, "error op=confirm reason=not-asked");
        return;
    }
    b->pair_flags |= WB55_BLE_PF_CENTRAL |
                     (!g_strcmp0(w[1], "yes") ? WB55_BLE_PF_CENTRAL_YES : 0);
    ble_pair_progress(s);
}

/* ---- ATT, the Flipper the server */

/* The bytes a client reads of an attribute -> their number */
static int attr_bytes(WB55BleState *b, WB55BleAttr *a, uint8_t *out)
{
    int ulen = a->uuid_type == 1 ? 2 : 16;

    switch (a->kind) {
    case WB55_BLE_ATTR_SERVICE:
        memcpy(out, a->uuid, ulen);
        return ulen;
    case WB55_BLE_ATTR_CHAR:
        out[0] = a->props;
        put16(out + 1, a->handle + 1);
        memcpy(out + 3, a->uuid, ulen);
        return 3 + ulen;
    default:
        memcpy(out, &b->values[a->at], a->len);
        return a->len;
    }
}

/* What the link's security lacks for the permissions: 0, or ATT's error.
 * A link that is not encrypted lacks authentication (it has no key). */
static uint8_t att_security(WB55BleState *b, uint8_t perms, bool write)
{
    uint8_t authen = write ? PERM_AUTHEN_WRITE : PERM_AUTHEN_READ;
    uint8_t encry = write ? PERM_ENCRY_WRITE : PERM_ENCRY_READ;

    if ((perms & authen) && !(b->sec & WB55_BLE_SEC_AUTHENTICATED)) {
        return ATT_INSUFFICIENT_AUTHEN;
    }
    if ((perms & encry) && !(b->sec & WB55_BLE_SEC_ENCRYPTED)) {
        return ATT_INSUFFICIENT_AUTHEN;
    }
    return 0;
}

static bool ble_handle(WB55IpccState *s, char **w, const char *op,
                       uint16_t *handle)
{
    uint64_t v;

    if (!num(w[1], 0, 1, 0xFFFF, &v)) {
        ble_say(s, "error op=%s reason=handle", op);
        return false;
    }
    *handle = v;
    if (!s->ble.conn) {
        ble_say(s, "error op=%s handle=0x%04x reason=not-connected", op,
                *handle);
        return false;
    }
    return true;
}

static void ble_read(WB55IpccState *s, char **w)
{
    WB55BleState *b = &s->ble;
    WB55BleAttr *a;
    uint8_t v[WB55_BLE_VALUES], err = 0;
    uint16_t h;
    g_autofree char *x = NULL;

    if (!ble_handle(s, w, "read", &h)) {
        return;
    }
    a = attr_find(b, h);
    if (!a) {
        err = ATT_INVALID_HANDLE;
    } else if (a->kind == WB55_BLE_ATTR_VALUE) {
        err = !(a->props & CHAR_PROP_READ) ? ATT_READ_NOT_PERMITTED
                                            : att_security(b, a->perms, false);
    } else if (a->kind == WB55_BLE_ATTR_DESC) {
        err = !(a->access & ACCESS_READ) ? ATT_READ_NOT_PERMITTED
                                          : att_security(b, a->perms, false);
    }
    if (err) {
        ble_say(s, "error op=read handle=0x%04x code=0x%02x", h, err);
        return;
    }
    x = hex(v, attr_bytes(b, a, v));
    ble_say(s, "value 0x%04x %s", h, x);
}

/* A client's write that CPU1 hears of: in events of up to 245 bytes, the
 * offset's bit 15 set while more is to come */
static void ble_attr_modified(WB55IpccState *s, uint16_t handle,
                              const uint8_t *v, int len)
{
    WB55BleState *b = &s->ble;
    const int room = WB55_BLE_EVT_LEN - 2 - 8;
    uint8_t d[8 + WB55_BLE_EVT_LEN];
    int at = 0;

    do {
        int n = MIN(len - at, room);

        put16(d, b->conn);
        put16(d + 2, handle);
        put16(d + 4, at | (at + n < len ? 0x8000 : 0));
        put16(d + 6, n);
        memcpy(d + 8, v + at, n);
        ble_vendor(s, VS_GATT_ATTRIBUTE_MODIFIED, d, 8 + n);
        at += n;
    } while (at < len);
}

/* write HANDLE HEX: a Write Request (answered); write-cmd: a Write Command */
static void ble_write(WB55IpccState *s, char **w, bool request)
{
    WB55BleState *b = &s->ble;
    const char *op = request ? "write" : "write-cmd";
    WB55BleAttr *a;
    uint8_t v[WB55_BLE_VALUES], err = 0;
    uint16_t h;
    int n;

    if (!ble_handle(s, w, op, &h)) {
        return;
    }
    n = unhex(w[1] ? w[2] : NULL, v, sizeof(v));
    a = attr_find(b, h);
    if (n < 0) {
        ble_say(s, "error op=%s handle=0x%04x reason=hex", op, h);
        return;
    }
    if (!a) {
        err = ATT_INVALID_HANDLE;
    } else if (n > b->mtu - 3) {
        err = ATT_INVALID_LENGTH;       /* more than one request carries */
    } else if (a->kind == WB55_BLE_ATTR_VALUE) {
        err = !(a->props & (request ? CHAR_PROP_WRITE : CHAR_PROP_WRITE_NO_RSP))
              ? ATT_WRITE_NOT_PERMITTED
              : n > a->max_len ? ATT_INVALID_LENGTH
              : att_security(b, a->perms, true);
    } else if (a->kind == WB55_BLE_ATTR_CCCD) {
        err = n != 2 ? ATT_INVALID_LENGTH : 0;
    } else if (a->kind == WB55_BLE_ATTR_DESC) {
        err = !(a->access & (request ? ACCESS_WRITE : ACCESS_WRITE_NO_RSP))
              ? ATT_WRITE_NOT_PERMITTED
              : n > a->max_len ? ATT_INVALID_LENGTH
              : att_security(b, a->perms, true);
    } else {
        err = ATT_WRITE_NOT_PERMITTED;  /* a declaration */
    }
    if (err) {
        ble_say(s, "error op=%s handle=0x%04x code=0x%02x", op, h, err);
        return;
    }
    memcpy(&b->values[a->at], v, n);
    if (a->variable) {
        a->len = n;
    }
    /* a CCCD's write CPU1 always hears of (ST's services look for it) */
    if (a->kind == WB55_BLE_ATTR_CCCD ||
        (a->evt_mask & GATT_NOTIFY_ATTRIBUTE_WRITE)) {
        ble_attr_modified(s, h, v, n);
    }
    if (request) {
        ble_say(s, "written 0x%04x", h);
    }
}

#define OUT_NOTIFY   1
#define OUT_INDICATE 2

/* A value that CPU1 has updated, to the client as far as it asked for it in
 * the CCCD (and no more than its MTU carries) */
static void ble_value_out(WB55IpccState *s, WB55BleAttr *a, int how)
{
    WB55BleState *b = &s->ble;
    WB55BleAttr *c = attr_find(b, a->handle + 1);
    g_autofree char *x = NULL;
    uint16_t cfg;

    if (!b->conn || !how || !c || c->kind != WB55_BLE_ATTR_CCCD) {
        return;
    }
    cfg = le16(&b->values[c->at]);
    x = hex(&b->values[a->at], MIN(a->len, b->mtu - 3));
    if ((how & OUT_NOTIFY) && (cfg & 1) && (a->props & CHAR_PROP_NOTIFY)) {
        ble_say(s, "notify 0x%04x %s", a->handle, x);
    }
    if ((how & OUT_INDICATE) && (cfg & 2) && (a->props & CHAR_PROP_INDICATE)) {
        b->indications += b->indications < 255;
        ble_say(s, "indicate 0x%04x %s", a->handle, x);
    }
}

/* ack: the client confirms an indication */
static void ble_ack(WB55IpccState *s)
{
    WB55BleState *b = &s->ble;
    uint8_t d[2];

    if (b->conn && b->indications) {
        b->indications--;
        put16(d, b->conn);
        ble_vendor(s, VS_GATT_SERVER_CONFIRMATION, d, sizeof(d));
    }
}

/* mtu N: the client's Exchange MTU Request */
static void ble_mtu(WB55IpccState *s, char **w)
{
    WB55BleState *b = &s->ble;
    uint64_t v;
    uint8_t d[4];

    if (!b->conn) {
        ble_say(s, "error op=mtu reason=not-connected");
        return;
    }
    if (!num(w[1], 0, ATT_MTU_MIN, 0xFFFF, &v)) {
        ble_say(s, "error op=mtu reason=number");
        return;
    }
    b->mtu = MAX(MIN(v, b->att_mtu ? b->att_mtu : ATT_MTU_MIN), ATT_MTU_MIN);
    put16(d, b->conn);
    put16(d + 2, b->mtu);
    ble_vendor(s, VS_ATT_EXCHANGE_MTU_RESP, d, sizeof(d));
    ble_say(s, "mtu %u", b->mtu);
}

static int attr_cmp(const void *x, const void *y)
{
    const WB55BleAttr *a = *(WB55BleAttr * const *)x;
    const WB55BleAttr *c = *(WB55BleAttr * const *)y;

    return a->handle - c->handle;
}

/* gatt: the database, as a client's discovery finds it */
static void ble_gatt_dump(WB55IpccState *s)
{
    WB55BleState *b = &s->ble;
    WB55BleAttr *list[WB55_BLE_ATTRS];
    int n = 0;
    char u[40];

    for (int i = 0; i < b->nattrs; i++) {
        if (b->attr[i].kind != WB55_BLE_ATTR_FREE) {
            list[n++] = &b->attr[i];
        }
    }
    qsort(list, n, sizeof(list[0]), attr_cmp);
    for (int i = 0; i < n; i++) {
        WB55BleAttr *a = list[i], *v;
        WB55BleService *svc;

        uuid_str(u, a);
        switch (a->kind) {
        case WB55_BLE_ATTR_SERVICE:
            svc = svc_find(b, a->handle);
            ble_say(s, "service 0x%04x end=0x%04x uuid=%s primary=%d",
                    a->handle, svc ? svc->last : a->handle, u, a->props == 1);
            break;
        case WB55_BLE_ATTR_CHAR:
            v = attr_find(b, a->handle + 1);
            ble_say(s, "char 0x%04x value=0x%04x props=0x%02x perms=0x%02x "
                    "uuid=%s", a->handle, a->handle + 1, a->props,
                    v ? v->perms : 0, u);
            break;
        case WB55_BLE_ATTR_CCCD:
            ble_say(s, "cccd 0x%04x", a->handle);
            break;
        case WB55_BLE_ATTR_DESC:
            ble_say(s, "desc 0x%04x access=0x%02x perms=0x%02x uuid=%s",
                    a->handle, a->access, a->perms, u);
            break;
        }
    }
    ble_say(s, "gatt end");
}

/* A line from the central */
static void ble_link_line(void *opaque, char *line)
{
    WB55IpccState *s = opaque;
    WB55BleState *b = &s->ble;
    char **w = words(line);
    const char *t;
    uint64_t v;

    if (!w[0]) {
        /* nothing */
    } else if (!strcmp(w[0], "connect")) {
        ble_connect(s, w);
    } else if (!strcmp(w[0], "disconnect")) {
        uint8_t reason = HCI_REMOTE_USER_TERM;

        if ((t = arg(w, "reason")) && num(t, 0, 0, 0xFF, &v)) {
            reason = v;
        }
        if (!b->conn) {
            ble_say(s, "error op=disconnect reason=not-connected");
        } else {
            ble_say(s, "disconnected by=central reason=0x%02x", reason);
            ble_disconnected(s, reason);
        }
    } else if (!strcmp(w[0], "pair")) {
        ble_pair(s, w);
    } else if (!strcmp(w[0], "passkey")) {
        ble_passkey(s, w);
    } else if (!strcmp(w[0], "confirm")) {
        ble_confirm(s, w);
    } else if (!strcmp(w[0], "mtu")) {
        ble_mtu(s, w);
    } else if (!strcmp(w[0], "read")) {
        ble_read(s, w);
    } else if (!strcmp(w[0], "write")) {
        ble_write(s, w, true);
    } else if (!strcmp(w[0], "write-cmd")) {
        ble_write(s, w, false);
    } else if (!strcmp(w[0], "ack")) {
        ble_ack(s);
    } else if (!strcmp(w[0], "gatt")) {
        ble_gatt_dump(s);
    } else if (!strcmp(w[0], "adv")) {
        g_free(s->ble_adv);
        s->ble_adv = NULL;
        ble_adv_announce(s);
    }                                   /* (others: ignored, as the links do) */
    g_strfreev(w);
}

static void ble_link_opened(void *opaque, bool open)
{
    WB55IpccState *s = opaque;

    g_free(s->ble_adv);
    s->ble_adv = NULL;
    if (open) {
        flipper_link_send(s->ble_link, "hello flipperemu ble 1\n");
        ble_adv_announce(s);
    } else if (s->ble.conn) {
        /* the central went away: the connection times out */
        ble_disconnected(s, HCI_CONN_TIMEOUT);
    }
}

/* A line takes room for up to three events (a long write is two, a
 * passkey two): the link waits for CPU1 rather than lose one */
static bool ble_link_ready(void *opaque)
{
    WB55IpccState *s = opaque;

    return s->ble.npending + 4 <= WB55_BLE_PENDING;
}

void wb55_ble_realize(WB55IpccState *s)
{
    s->ble_link = g_new0(FlipperLink, 1);
    flipper_link_init(s->ble_link, &s->ble_chr, "Bluetooth", ble_link_line,
                      ble_link_opened, s);
    s->ble_link->ready = ble_link_ready;
}

/* A loaded machine runs: a connection that was in the air at the save is
 * gone (and no program was on the link of this new run anyway) */
void wb55_ble_loaded(WB55IpccState *s)
{
    if (s->ble.conn) {
        ble_disconnected(s, HCI_CONN_TIMEOUT);
    }
    ble_adv_announce(s);
}

/* ------------------------------------------------------------- commands */

void wb55_ble_reset(WB55IpccState *s)
{
    WB55BleState *b = &s->ble;

    if (b->conn) {
        /* the stack stopped: the central's side times out */
        ble_say(s, "disconnected by=flipper reason=0x%02x", HCI_CONN_TIMEOUT);
    }
    memset(b, 0, sizeof(*b));
    b->next_handle = 1;
    b->mtu = ATT_MTU_MIN;
    ble_adv_announce(s);
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
    if (plen >= 20) {
        b->att_mtu = le16(p + 18);      /* AttMtu: what a client may get */
    }
    b->up = true;
    ble_adv_announce(s);
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
    bool ours = b->conn && plen >= 2 && le16(p) == b->conn;
    bool pairing = false;               /* an answer that a pairing waited for */
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
        if (b->conn) {
            ble_say(s, "disconnected by=flipper reason=0x%02x",
                    HCI_CONN_TIMEOUT);
            ble_conn_drop(b);
        }
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
        rp[0] = ours ? ST_OK : ST_UNKNOWN_CONN;
        put16(rp + 1, plen >= 2 ? le16(p) : 0);
        rp[3] = rp[4] = ours ? 0x01 : 0x00;     /* LE 1M, both ways */
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
        } else if (plen >= 8 && p[0] == 0x2E && p[1] >= 6) {
            memcpy(b->random_addr, p + 2, 6);   /* the static random one */
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
    case OP_GAP_PASS_KEY_RESP:          /* the key it shows, for the pairing */
        if (!ours || plen < 6) {
            rp[0] = ST_UNKNOWN_CONN;
        } else if (b->pair != WB55_BLE_PAIR_PASSKEY ||
                   (b->pair_flags & WB55_BLE_PF_FLIPPER)) {
            rp[0] = ST_DISALLOWED;
        } else {
            b->pair_key = ldl_le_p(p + 2);
            b->pair_flags |= WB55_BLE_PF_FLIPPER;
            pairing = true;
        }
        break;
    case OP_GAP_NUMERIC_CONFIRM:        /* its user's yes or no */
        if (!ours || plen < 3) {
            rp[0] = ST_UNKNOWN_CONN;
        } else if (b->pair != WB55_BLE_PAIR_COMPARE ||
                   (b->pair_flags & WB55_BLE_PF_FLIPPER)) {
            rp[0] = ST_DISALLOWED;
        } else {
            b->pair_flags |= WB55_BLE_PF_FLIPPER |
                             (p[2] == 0x01 ? WB55_BLE_PF_FLIPPER_YES : 0);
            pairing = true;
        }
        break;
    case OP_GAP_ALLOW_REBOND:
        rp[0] = ours ? ST_OK : ST_UNKNOWN_CONN;
        break;
    case OP_GAP_PERIPHERAL_SEC_REQ:     /* a Security Request to the central */
        ble_status(s, op, ours ? ST_OK : ST_UNKNOWN_CONN);
        if (ours) {
            ble_vendor(s, VS_GAP_PERIPHERAL_SEC_INIT, NULL, 0);
            ble_say(s, "security-request bond=%d mitm=%d sc=%d", !!b->bonding,
                    !!b->mitm, !!b->sc);
        }
        return;
    case OP_GAP_TERMINATE:
        ble_status(s, op, ours && plen >= 3 ? ST_OK : ST_UNKNOWN_CONN);
        if (ours && plen >= 3) {
            ble_say(s, "disconnected by=flipper reason=0x%02x", p[2]);
            ble_disconnected(s, HCI_LOCAL_HOST_TERM);
        }
        return;
    case OP_L2CAP_CONN_PARAM_UPDATE:
        ble_status(s, op, ours && plen >= 10 ? ST_OK : ST_UNKNOWN_CONN);
        if (ours && plen >= 10) {
            ble_conn_update(s, le16(p + 2), le16(p + 6), le16(p + 8));
        }
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
            a->access = r[1];
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
            /* to the client: whatever its CCCD asks for */
            ble_value_out(s, a, OUT_NOTIFY | OUT_INDICATE);
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
        } else if (le16(p) && le16(p) != b->conn) {
            rp[0] = ST_UNKNOWN_CONN;    /* Conn_Handle_To_Notify: not ours */
        } else {
            attr_set(b, a, le16(p + 9), p + 12, p[11]);
            if (a->variable) {
                a->len = le16(p + 7);   /* Char_Length: the whole of it */
            }
            /* Update_Type: 0 the value alone (a part of it, the serial
             * service's chunks), 1 a notification, 2 an indication */
            ble_value_out(s, a, p[6] & (OUT_NOTIFY | OUT_INDICATE));
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
    /* what follows the answer: the pairing goes on, and a scanner sees the
     * advertising as the command left it */
    if (pairing) {
        ble_pair_progress(s);
    }
    ble_adv_announce(s);
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
        b->scan_rsp_len > sizeof(b->scan_rsp) ||
        b->mtu < ATT_MTU_MIN || b->pair > WB55_BLE_PAIR_COMPARE) {
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
    .version_id = 2,
    .minimum_version_id = 2,
    .fields = (const VMStateField[]) {
        VMSTATE_UINT16(handle, WB55BleAttr),
        VMSTATE_UINT16(service, WB55BleAttr),
        VMSTATE_UINT8(kind, WB55BleAttr),
        VMSTATE_UINT8(uuid_type, WB55BleAttr),
        VMSTATE_UINT8_ARRAY(uuid, WB55BleAttr, 16),
        VMSTATE_UINT8(props, WB55BleAttr),
        VMSTATE_UINT8(perms, WB55BleAttr),
        VMSTATE_UINT8(access, WB55BleAttr),
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

/*
 * The connection is in it, though a load ends it (wb55_ble_loaded()): CPU1
 * has to hear that the one it knew of is gone.
 */
const VMStateDescription vmstate_wb55_ble = {
    .name = "stm32wb55-ble",
    .version_id = 2,
    .minimum_version_id = 2,
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
        VMSTATE_UINT8_ARRAY(random_addr, WB55BleState, 6),
        VMSTATE_UINT16(att_mtu, WB55BleState),
        VMSTATE_UINT16(conn, WB55BleState),
        VMSTATE_UINT8_ARRAY(peer, WB55BleState, 6),
        VMSTATE_UINT8(peer_type, WB55BleState),
        VMSTATE_UINT16(interval, WB55BleState),
        VMSTATE_UINT16(latency, WB55BleState),
        VMSTATE_UINT16(timeout, WB55BleState),
        VMSTATE_UINT16(mtu, WB55BleState),
        VMSTATE_UINT8(sec, WB55BleState),
        VMSTATE_UINT8(pair, WB55BleState),
        VMSTATE_UINT8(pair_flags, WB55BleState),
        VMSTATE_UINT32(pair_key, WB55BleState),
        VMSTATE_UINT32(pair_entered, WB55BleState),
        VMSTATE_UINT8(indications, WB55BleState),
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
