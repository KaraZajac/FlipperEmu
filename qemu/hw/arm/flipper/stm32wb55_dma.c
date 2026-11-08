/*
 * STM32WB55 DMA1/DMA2 (7 channels each) + DMAMUX1 (RM0434 chapters 11, 12)
 *
 * Request-driven model: peripherals drive DMAMUX request inputs as levels
 * (e.g. SPI TX = TXDMAEN, SPI RX = RXDMAEN && RXNE). An enabled channel whose
 * selected request is high performs one beat (one data item) at a time; after
 * every beat arbitration restarts, highest priority first and the lower
 * channel number winning ties - so an SPI RX channel (DMA2 ch6) drains each
 * byte the TX channel (DMA2 ch7) clocked in before the 4-byte RX FIFO can
 * overflow, which is what the firmware's SD-card path relies on
 * (targets/f7/furi_hal/furi_hal_spi.c, furi_hal_spi_bus_trx_dma).
 *
 * Channels without a request (DMAMUX id 0) in MEM2MEM mode run to completion.
 *
 * Copyright (c) 2026, Kara Zajac
 * SPDX-License-Identifier: BSD-3-Clause
 */
#include "qemu/osdep.h"
#include "qemu/log.h"
#include "hw/core/irq.h"
#include "system/address-spaces.h"
#include "system/memory.h"
#include "wb55.h"

#define CCR_EN      (1u << 0)
#define CCR_TCIE    (1u << 1)
#define CCR_HTIE    (1u << 2)
#define CCR_TEIE    (1u << 3)
#define CCR_DIR     (1u << 4)       /* 1: read from memory */
#define CCR_CIRC    (1u << 5)
#define CCR_PINC    (1u << 6)
#define CCR_MINC    (1u << 7)
#define CCR_MEM2MEM (1u << 14)

#define IF_G  1u
#define IF_TC 2u
#define IF_HT 4u
#define IF_TE 8u

static void dma_run(WB55DmaState *s);

static uint32_t *dma_isr(WB55DmaState *s, int c)
{
    return &s->isr[c / 7];
}

static unsigned dma_shift(int c)
{
    return (c % 7) * 4;
}

static void dma_update_irq(WB55DmaState *s, int c)
{
    WB55DmaChannel *ch = &s->ch[c];
    uint32_t f = (*dma_isr(s, c) >> dma_shift(c)) & 0xF;
    bool lvl = ((f & IF_TC) && (ch->ccr & CCR_TCIE)) ||
               ((f & IF_HT) && (ch->ccr & CCR_HTIE)) ||
               ((f & IF_TE) && (ch->ccr & CCR_TEIE));
    qemu_set_irq(s->irq[c], lvl);
}

static void dma_flag(WB55DmaState *s, int c, uint32_t f)
{
    *dma_isr(s, c) |= (f | IF_G) << dma_shift(c);
    dma_update_irq(s, c);
}

static unsigned dma_size(uint32_t field)
{
    return 1u << (field & 3);           /* 0: byte, 1: half, 2: word */
}

static bool dma_beat(WB55DmaState *s, int c)
{
    WB55DmaChannel *ch = &s->ch[c];
    unsigned psize = dma_size(ch->ccr >> 8), msize = dma_size(ch->ccr >> 10);
    hwaddr pa = ch->cpar + ((ch->ccr & CCR_PINC) ? ch->pidx * psize : 0);
    hwaddr ma = ch->cmar + ((ch->ccr & CCR_MINC) ? ch->midx * msize : 0);
    uint32_t data = 0;
    MemTxResult r;

    if (ch->ccr & CCR_DIR) {            /* memory -> peripheral */
        r = address_space_read(&address_space_memory, ma,
                               MEMTXATTRS_UNSPECIFIED, &data, msize);
        if (r == MEMTX_OK) {
            r = address_space_write(&address_space_memory, pa,
                                    MEMTXATTRS_UNSPECIFIED, &data, psize);
        }
    } else {                            /* peripheral -> memory */
        r = address_space_read(&address_space_memory, pa,
                               MEMTXATTRS_UNSPECIFIED, &data, psize);
        if (r == MEMTX_OK) {
            r = address_space_write(&address_space_memory, ma,
                                    MEMTXATTRS_UNSPECIFIED, &data, msize);
        }
    }
    if (r != MEMTX_OK) {
        qemu_log_mask(LOG_GUEST_ERROR, "wb55-dma: bus error on channel %d\n", c);
        ch->ccr &= ~CCR_EN;
        dma_flag(s, c, IF_TE);
        return false;
    }
    ch->pidx++;
    ch->midx++;
    ch->cndtr--;
    if (ch->cndtr == ch->reload / 2 && ch->reload > 1) {
        dma_flag(s, c, IF_HT);
    }
    if (ch->cndtr == 0) {
        if (ch->ccr & CCR_CIRC) {
            ch->cndtr = ch->reload;
            ch->pidx = ch->midx = 0;
        }
        dma_flag(s, c, IF_TC);
    }
    return true;
}

static bool dma_ready(WB55DmaState *s, int c)
{
    WB55DmaChannel *ch = &s->ch[c];
    uint32_t req;

    if (!(ch->ccr & CCR_EN) || ch->cndtr == 0) {
        return false;
    }
    if (ch->ccr & CCR_MEM2MEM) {
        return true;
    }
    req = s->muxccr[c] & 0x3F;
    return req != 0 && req < 64 && (s->req >> req) & 1;
}

static void dma_run(WB55DmaState *s)
{
    if (s->in_service) {
        s->dirty = true;                /* re-arbitrate when the beat ends */
        return;
    }
    s->in_service = true;
    for (int guard = 0; guard < 1 << 20; guard++) {
        int best = -1, best_pl = -1;
        s->dirty = false;
        for (int c = 0; c < 14; c++) {
            int pl = (s->ch[c].ccr >> 12) & 3;
            if (dma_ready(s, c) && pl > best_pl) {
                best = c;
                best_pl = pl;
            }
        }
        if (best < 0) {
            break;
        }
        if (!dma_beat(s, best)) {
            continue;
        }
    }
    s->in_service = false;
}

static void dma_request(void *opaque, int n, int level)
{
    WB55DmaState *s = opaque;
    uint64_t bit = 1ull << n;
    bool rose = level && !(s->req & bit);

    if (level) {
        s->req |= bit;
    } else {
        s->req &= ~bit;
    }
    if (rose) {
        dma_run(s);
    }
}

/* ---- DMA1/DMA2 register blocks: ISR, IFCR, then 7 x {CCR,CNDTR,CPAR,CMAR} */

static uint64_t dma_read(WB55DmaState *s, int ctrl, hwaddr addr)
{
    if (addr == 0x00) {
        return s->isr[ctrl];
    }
    if (addr == 0x04 || addr < 0x08 || addr >= 0x08 + 7 * 0x14) {
        return 0;
    }
    int c = ctrl * 7 + (addr - 0x08) / 0x14;
    switch ((addr - 0x08) % 0x14) {
    case 0x0: return s->ch[c].ccr;
    case 0x4: return s->ch[c].cndtr;
    case 0x8: return s->ch[c].cpar;
    case 0xC: return s->ch[c].cmar;
    }
    return 0;
}

static void dma_write(WB55DmaState *s, int ctrl, hwaddr addr, uint32_t val)
{
    if (addr == 0x04) {                 /* IFCR */
        s->isr[ctrl] &= ~val;
        /* clearing G clears all flags of that channel */
        for (int i = 0; i < 7; i++) {
            if (val & (1u << (i * 4))) {
                s->isr[ctrl] &= ~(0xFu << (i * 4));
            }
            dma_update_irq(s, ctrl * 7 + i);
        }
        return;
    }
    if (addr < 0x08 || addr >= 0x08 + 7 * 0x14) {
        return;
    }
    int c = ctrl * 7 + (addr - 0x08) / 0x14;
    WB55DmaChannel *ch = &s->ch[c];
    switch ((addr - 0x08) % 0x14) {
    case 0x0: {
        bool enabling = (val & CCR_EN) && !(ch->ccr & CCR_EN);
        ch->ccr = val & 0x7FFF;
        if (enabling) {
            ch->pidx = ch->midx = 0;
            ch->reload = ch->cndtr;
        }
        dma_update_irq(s, c);
        if (enabling) {
            dma_run(s);
        }
        break;
    }
    case 0x4:
        if (!(ch->ccr & CCR_EN)) {
            ch->cndtr = val & 0xFFFF;
        }
        break;
    case 0x8:
        if (!(ch->ccr & CCR_EN)) {
            ch->cpar = val;
        }
        break;
    case 0xC:
        if (!(ch->ccr & CCR_EN)) {
            ch->cmar = val;
        }
        break;
    }
}

static uint64_t dma1_read(void *o, hwaddr a, unsigned sz)
{
    return dma_read(o, 0, a);
}

static void dma1_write(void *o, hwaddr a, uint64_t v, unsigned sz)
{
    dma_write(o, 0, a, v);
}

static uint64_t dma2_read(void *o, hwaddr a, unsigned sz)
{
    return dma_read(o, 1, a);
}

static void dma2_write(void *o, hwaddr a, uint64_t v, unsigned sz)
{
    dma_write(o, 1, a, v);
}

static const MemoryRegionOps dma1_ops = {
    .read = dma1_read, .write = dma1_write,
    .endianness = DEVICE_LITTLE_ENDIAN,
    .impl.min_access_size = 4, .impl.max_access_size = 4,
    .valid.min_access_size = 1, .valid.max_access_size = 4,
};

static const MemoryRegionOps dma2_ops = {
    .read = dma2_read, .write = dma2_write,
    .endianness = DEVICE_LITTLE_ENDIAN,
    .impl.min_access_size = 4, .impl.max_access_size = 4,
    .valid.min_access_size = 1, .valid.max_access_size = 4,
};

/* ---- DMAMUX1: C0CR..C13CR, request generators unused by the firmware */

static uint64_t mux_read(void *opaque, hwaddr addr, unsigned size)
{
    WB55DmaState *s = opaque;

    if (addr < 14 * 4) {
        return s->muxccr[addr / 4];
    }
    return 0;
}

static void mux_write(void *opaque, hwaddr addr, uint64_t val, unsigned size)
{
    WB55DmaState *s = opaque;

    if (addr < 14 * 4) {
        s->muxccr[addr / 4] = val & 0x1FFF3FF;
        dma_run(s);
    }
}

static const MemoryRegionOps mux_ops = {
    .read = mux_read, .write = mux_write,
    .endianness = DEVICE_LITTLE_ENDIAN,
    .impl.min_access_size = 4, .impl.max_access_size = 4,
    .valid.min_access_size = 1, .valid.max_access_size = 4,
};

static void wb55_dma_reset_hold(Object *obj, ResetType type)
{
    WB55DmaState *s = WB55_DMA(obj);

    memset(s->ch, 0, sizeof(s->ch));
    memset(s->isr, 0, sizeof(s->isr));
    memset(s->muxccr, 0, sizeof(s->muxccr));
    s->in_service = s->dirty = false;
    for (int c = 0; c < 14; c++) {
        qemu_set_irq(s->irq[c], 0);
    }
}

static void wb55_dma_init(Object *obj)
{
    WB55DmaState *s = WB55_DMA(obj);
    SysBusDevice *sbd = SYS_BUS_DEVICE(obj);

    memory_region_init_io(&s->mmio_dma1, obj, &dma1_ops, s, "dma1", 0x400);
    memory_region_init_io(&s->mmio_dma2, obj, &dma2_ops, s, "dma2", 0x400);
    memory_region_init_io(&s->mmio_mux, obj, &mux_ops, s, "dmamux1", 0x400);
    sysbus_init_mmio(sbd, &s->mmio_dma1);
    sysbus_init_mmio(sbd, &s->mmio_dma2);
    sysbus_init_mmio(sbd, &s->mmio_mux);
    for (int c = 0; c < 14; c++) {
        sysbus_init_irq(sbd, &s->irq[c]);
    }
    qdev_init_gpio_in_named(DEVICE(obj), dma_request, "request", 64);
}

static void wb55_dma_class_init(ObjectClass *klass, const void *data)
{
    RESETTABLE_CLASS(klass)->phases.hold = wb55_dma_reset_hold;
}

static const TypeInfo wb55_dma_info = {
    .name = TYPE_WB55_DMA,
    .parent = TYPE_SYS_BUS_DEVICE,
    .instance_size = sizeof(WB55DmaState),
    .instance_init = wb55_dma_init,
    .class_init = wb55_dma_class_init,
};

static void wb55_dma_register(void)
{
    type_register_static(&wb55_dma_info);
}
type_init(wb55_dma_register)
