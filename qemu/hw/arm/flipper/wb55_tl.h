/*
 * STM32WB55 CPU2's side of ST's mailbox (the transport layer, "TL"): guest
 * memory as CPU2 sees it, and the circular doubly linked lists (tListNode
 * {next, prev}) that the queues of the mailbox are made of.  Shared by the
 * IPCC model (the system channel, the FUS) and the BLE stack's model.
 *
 * Copyright (c) 2026, Kara Zajac
 * SPDX-License-Identifier: BSD-3-Clause
 */
#ifndef HW_ARM_FLIPPER_WB55_TL_H
#define HW_ARM_FLIPPER_WB55_TL_H

#include "system/address-spaces.h"
#include "system/memory.h"

static inline uint32_t tl_rd32(hwaddr a)
{
    uint32_t v = 0;
    address_space_read(&address_space_memory, a, MEMTXATTRS_UNSPECIFIED, &v, 4);
    return le32_to_cpu(v);
}

static inline void tl_wr32(hwaddr a, uint32_t v)
{
    v = cpu_to_le32(v);
    address_space_write(&address_space_memory, a, MEMTXATTRS_UNSPECIFIED,
                        &v, 4);
}

static inline uint8_t tl_rd8(hwaddr a)
{
    uint8_t v = 0;
    address_space_read(&address_space_memory, a, MEMTXATTRS_UNSPECIFIED, &v, 1);
    return v;
}

static inline void tl_wr8(hwaddr a, uint8_t v)
{
    address_space_write(&address_space_memory, a, MEMTXATTRS_UNSPECIFIED,
                        &v, 1);
}

static inline void tl_read(hwaddr a, void *buf, int len)
{
    address_space_read(&address_space_memory, a, MEMTXATTRS_UNSPECIFIED, buf,
                       len);
}

static inline void tl_write(hwaddr a, const void *buf, int len)
{
    address_space_write(&address_space_memory, a, MEMTXATTRS_UNSPECIFIED, buf,
                        len);
}

static inline bool tl_lst_is_empty(hwaddr head)
{
    return tl_rd32(head) == head;
}

static inline void tl_lst_insert_tail(hwaddr head, hwaddr node)
{
    hwaddr prev = tl_rd32(head + 4);
    tl_wr32(node, head);            /* node->next = head */
    tl_wr32(node + 4, prev);        /* node->prev = head->prev */
    tl_wr32(prev, node);            /* prev->next = node */
    tl_wr32(head + 4, node);        /* head->prev = node */
}

static inline hwaddr tl_lst_remove_head(hwaddr head)
{
    hwaddr node = tl_rd32(head);
    hwaddr next = tl_rd32(node);
    tl_wr32(head, next);
    tl_wr32(next + 4, head);
    return node;
}

/* MB_RefTable_t: the tables' pointers, by their offsets */
#define TL_REF_DEVICE_INFO   0
#define TL_REF_BLE           4
#define TL_REF_SYS           12
#define TL_REF_MEM_MANAGER   16
/* MB_BleTable_t */
#define TL_BLE_CMD_BUFFER    0
#define TL_BLE_CS_BUFFER     4
#define TL_BLE_EVT_QUEUE     8
#define TL_BLE_ACL_BUFFER    12
/* MB_SysTable_t */
#define TL_SYS_CMD_BUFFER    0
#define TL_SYS_QUEUE         4
/* MB_MemManagerTable_t */
#define TL_MM_SPARE_BLE      0
#define TL_MM_SPARE_SYS      4
#define TL_MM_BLE_POOL       8
#define TL_MM_BLE_POOL_SIZE  12
#define TL_MM_FREE_QUEUE     16

/* packets: TL_PacketHeader_t (the list node, 8 bytes) and then the serial */
#define TL_PKT_HEADER        8
#define TL_BLECMD_PKT_TYPE   0x01
#define TL_BLEEVT_PKT_TYPE   0x04
#define TL_SYSRSP_PKT_TYPE   0x11
#define TL_SYSEVT_PKT_TYPE   0x12

#endif
