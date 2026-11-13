/*
 * STM32WB55 SYSCFG: EXTI port multiplexer and boot memory remap
 *
 * Inputs: 8 ports x 16 pins, indexed by EXTICR port code * 16 + pin
 * (A=0 B=1 C=2 D=3 E=4 H=7). Only the port selected in EXTICRx drives an
 * EXTI line; the level of every pin is remembered so selecting a port picks
 * up its current level without a spurious edge.
 *
 * MEMRMP.MEM_MODE selects what is aliased at 0x00000000 (main flash, system
 * flash or SRAM1). The OTA updater copies itself to SRAM1, selects SRAM1 and
 * jumps through the vector table at 0 (applications/system/updater,
 * furi_hal_switch()).
 *
 * VREFBUF (0x40010030) shares this block and is handled here too.
 *
 * Copyright (c) 2026, Kara Zajac
 * SPDX-License-Identifier: BSD-3-Clause
 */
#include "qemu/osdep.h"
#include "qemu/log.h"
#include "hw/core/irq.h"
#include "migration/vmstate.h"
#include "wb55.h"

static void syscfg_route(WB55SyscfgState *s, int line)
{
    int port = (s->exticr[line / 4] >> ((line % 4) * 4)) & 0x7;
    qemu_set_irq(s->exti[line], s->level[port * 16 + line]);
}

static void syscfg_set_pin(void *opaque, int n, int level)
{
    WB55SyscfgState *s = opaque;
    int line = n % 16;
    int port = n / 16;
    int sel = (s->exticr[line / 4] >> ((line % 4) * 4)) & 0x7;

    s->level[n] = !!level;
    if (sel == port) {
        qemu_set_irq(s->exti[line], !!level);
    }
}

static uint64_t wb55_syscfg_read(void *opaque, hwaddr addr, unsigned size)
{
    WB55SyscfgState *s = opaque;

    switch (addr) {
    case 0x00: return s->memrmp;
    case 0x04: return s->cfgr1;
    case 0x08: case 0x0C: case 0x10: case 0x14:
        return s->exticr[(addr - 0x08) / 4];
    case 0x18: return s->scsr;
    case 0x1C: return s->cfgr2;
    case 0x20: return s->swpr1;
    case 0x28: return s->swpr2;
    case 0x30:                          /* VREFBUF_CSR: VRR follows ENVR */
        return s->vrefbuf_csr | ((s->vrefbuf_csr & 1) << 3);
    case 0x34: return s->vrefbuf_ccr;
    case 0x100: return s->imr1;
    case 0x104: return s->imr2;
    case 0x108: return s->c2imr1;
    case 0x10C: return s->c2imr2;
    case 0x110: return s->sipcr;
    }
    return 0;
}

static void wb55_syscfg_write(void *opaque, hwaddr addr, uint64_t val64,
                              unsigned size)
{
    WB55SyscfgState *s = opaque;
    uint32_t val = val64;

    switch (addr) {
    case 0x00:
        if ((val & 7) != (s->memrmp & 7) && s->remap) {
            s->memrmp = val & 7;
            s->remap(s->remap_opaque, val & 7);
        }
        s->memrmp = val & 7;
        break;
    case 0x04:
        s->cfgr1 = val;
        break;
    case 0x08: case 0x0C: case 0x10: case 0x14: {
        int r = (addr - 0x08) / 4;
        s->exticr[r] = val & 0x7777;
        for (int i = 0; i < 4; i++) {
            syscfg_route(s, r * 4 + i);
        }
        break;
    }
    case 0x18:
        /* SRAM2 erase completes instantly (SRAM2BSY stays 0) */
        s->scsr = 0;
        break;
    case 0x1C:
        s->cfgr2 = (s->cfgr2 & ~0xF00u) | (val & 0xF);
        break;
    case 0x20: s->swpr1 |= val; break;
    case 0x24: break;                   /* SKR: key register */
    case 0x28: s->swpr2 |= val; break;
    case 0x30: s->vrefbuf_csr = val & 7; break;
    case 0x34: s->vrefbuf_ccr = val & 0x3F; break;
    case 0x100: s->imr1 = val; break;
    case 0x104: s->imr2 = val; break;
    case 0x108: s->c2imr1 = val; break;
    case 0x10C: s->c2imr2 = val; break;
    case 0x110: s->sipcr = val; break;
    default:
        qemu_log_mask(LOG_GUEST_ERROR, "wb55-syscfg: write 0x%" HWADDR_PRIx "\n",
                      addr);
    }
}

static const MemoryRegionOps wb55_syscfg_ops = {
    .read = wb55_syscfg_read,
    .write = wb55_syscfg_write,
    .endianness = DEVICE_LITTLE_ENDIAN,
    .impl.min_access_size = 4,
    .impl.max_access_size = 4,
    .valid.min_access_size = 1,
    .valid.max_access_size = 4,
};

static void wb55_syscfg_reset_hold(Object *obj, ResetType type)
{
    WB55SyscfgState *s = WB55_SYSCFG(obj);

    s->memrmp = 0;
    s->cfgr1 = 0x7C000001;
    memset(s->exticr, 0, sizeof(s->exticr));
    s->scsr = s->cfgr2 = s->swpr1 = s->swpr2 = 0;
    s->imr1 = s->imr2 = s->c2imr1 = s->c2imr2 = s->sipcr = 0;
    s->vrefbuf_csr = 2;                 /* HIZ */
    s->vrefbuf_ccr = 0;
}

static void wb55_syscfg_reset_exit(Object *obj, ResetType type)
{
    WB55SyscfgState *s = WB55_SYSCFG(obj);

    if (s->remap) {
        s->remap(s->remap_opaque, 0);
    }
    for (int i = 0; i < 16; i++) {
        syscfg_route(s, i);
    }
}

static void wb55_syscfg_init(Object *obj)
{
    WB55SyscfgState *s = WB55_SYSCFG(obj);

    memory_region_init_io(&s->mmio, obj, &wb55_syscfg_ops, s, "syscfg", 0x200);
    sysbus_init_mmio(SYS_BUS_DEVICE(obj), &s->mmio);
    qdev_init_gpio_in(DEVICE(obj), syscfg_set_pin, 8 * 16);
    qdev_init_gpio_out(DEVICE(obj), s->exti, 16);
}

/* A saved state: what is mapped at address 0 follows MEMRMP again */
static int wb55_syscfg_post_load(void *opaque, int version_id)
{
    WB55SyscfgState *s = opaque;

    if (s->remap) {
        s->remap(s->remap_opaque, s->memrmp & 7);
    }
    return 0;
}

static const VMStateDescription vmstate_wb55_syscfg = {
    .name = "stm32wb55-syscfg",
    .version_id = 1,
    .minimum_version_id = 1,
    .post_load = wb55_syscfg_post_load,
    .fields = (const VMStateField[]) {
        VMSTATE_UINT32(memrmp, WB55SyscfgState),
        VMSTATE_UINT32(cfgr1, WB55SyscfgState),
        VMSTATE_UINT32_ARRAY(exticr, WB55SyscfgState, 4),
        VMSTATE_UINT32(scsr, WB55SyscfgState),
        VMSTATE_UINT32(cfgr2, WB55SyscfgState),
        VMSTATE_UINT32(swpr1, WB55SyscfgState),
        VMSTATE_UINT32(swpr2, WB55SyscfgState),
        VMSTATE_UINT32(imr1, WB55SyscfgState),
        VMSTATE_UINT32(imr2, WB55SyscfgState),
        VMSTATE_UINT32(c2imr1, WB55SyscfgState),
        VMSTATE_UINT32(c2imr2, WB55SyscfgState),
        VMSTATE_UINT32(sipcr, WB55SyscfgState),
        VMSTATE_UINT32(vrefbuf_csr, WB55SyscfgState),
        VMSTATE_UINT32(vrefbuf_ccr, WB55SyscfgState),
        VMSTATE_UINT8_ARRAY(level, WB55SyscfgState, 8 * 16),
        VMSTATE_END_OF_LIST()
    }
};

static void wb55_syscfg_class_init(ObjectClass *klass, const void *data)
{
    ResettableClass *rc = RESETTABLE_CLASS(klass);

    rc->phases.hold = wb55_syscfg_reset_hold;
    rc->phases.exit = wb55_syscfg_reset_exit;
    DEVICE_CLASS(klass)->vmsd = &vmstate_wb55_syscfg;
}

static const TypeInfo wb55_syscfg_info = {
    .name = TYPE_WB55_SYSCFG,
    .parent = TYPE_SYS_BUS_DEVICE,
    .instance_size = sizeof(WB55SyscfgState),
    .instance_init = wb55_syscfg_init,
    .class_init = wb55_syscfg_class_init,
};

static void wb55_syscfg_register(void)
{
    type_register_static(&wb55_syscfg_info);
}
type_init(wb55_syscfg_register)
