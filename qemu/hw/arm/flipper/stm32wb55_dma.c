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
 * Several channels may select the same request: each rising edge then owes
 * every one of them a beat, even after the first beat has made the
 * peripheral drop the request.  A timer's update request works that way on
 * silicon, and the firmware relies on it: furi_hal_rfid's emulation feeds
 * TIM2 ARR and CCR3 from two channels on TIM2_UP, and furi_hal_infrared's
 * transmitter TIM1 RCR and CCMR2 on TIM1_UP.
 *
 * Copyright (c) 2026, Kara Zajac
 * SPDX-License-Identifier: BSD-3-Clause
 */
#include "qemu/osdep.h"
#include "qemu/log.h"
#include "hw/core/irq.h"
#include "system/address-spaces.h"
#include "system/memory.h"
#include "migration/vmstate.h"
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

/*
 * A channel's two sides, each looked up once for a transfer: the address
 * space's translation of every beat was the most of what a block of the
 * card cost the host (BRINGUP_LOG 192).  Memory that is RAM is a pointer
 * then, and what is written there is marked as written (code in it is
 * translated again); a register goes to its device as before.  A side that
 * one region does not hold whole goes the old way, as does a channel whose
 * CCR is written with other sizes or directions while it is enabled.
 */
#define CCR_CACHED (CCR_EN | CCR_DIR | CCR_PINC | CCR_MINC | (0xF << 8) | \
                    CCR_MEM2MEM)

static void dma_uncache(WB55DmaChannel *ch)
{
    if (ch->cached) {
        address_space_cache_destroy(&ch->mcache);
        address_space_cache_destroy(&ch->pcache);
        address_space_cache_init_empty(&ch->mcache);
        address_space_cache_init_empty(&ch->pcache);
        ch->cached = 0;
    }
}

static void dma_cache(WB55DmaChannel *ch)
{
    unsigned psize = dma_size(ch->ccr >> 8), msize = dma_size(ch->ccr >> 10);
    hwaddr mlen = (ch->ccr & CCR_MINC) ? (hwaddr)ch->reload * msize : msize;
    hwaddr plen = (ch->ccr & CCR_PINC) ? (hwaddr)ch->reload * psize : psize;
    bool to_periph = ch->ccr & CCR_DIR;

    dma_uncache(ch);
    if (!(ch->ccr & CCR_EN) || !ch->reload) {
        return;
    }
    if (address_space_cache_init(&ch->mcache, &address_space_memory, ch->cmar,
                                 mlen, !to_periph) < (int64_t)mlen ||
        address_space_cache_init(&ch->pcache, &address_space_memory, ch->cpar,
                                 plen, to_periph) < (int64_t)plen) {
        address_space_cache_destroy(&ch->mcache);
        address_space_cache_destroy(&ch->pcache);
        address_space_cache_init_empty(&ch->mcache);
        address_space_cache_init_empty(&ch->pcache);
        return;
    }
    ch->cached = ch->ccr & CCR_CACHED;
}

/* a beat through the channel's caches */
static MemTxResult dma_beat_cached(WB55DmaChannel *ch, unsigned psize,
                                   unsigned msize)
{
    hwaddr poff = (ch->ccr & CCR_PINC) ? (hwaddr)ch->pidx * psize : 0;
    hwaddr moff = (ch->ccr & CCR_MINC) ? (hwaddr)ch->midx * msize : 0;
    MemoryRegionCache *from = &ch->pcache, *to = &ch->mcache;
    hwaddr foff = poff, toff = moff;
    unsigned fsize = psize, tsize = msize;
    uint32_t data = 0;
    MemTxResult r;

    if (ch->ccr & CCR_DIR) {            /* memory -> peripheral */
        from = &ch->mcache;
        to = &ch->pcache;
        foff = moff;
        toff = poff;
        fsize = msize;
        tsize = psize;
    }
    r = address_space_read_cached(from, foff, &data, fsize);
    if (r == MEMTX_OK) {
        r = address_space_write_cached(to, toff, &data, tsize);
        if (to->ptr) {
            address_space_cache_invalidate(to, toff, tsize);
        }
    }
    return r;
}

static bool dma_beat(WB55DmaState *s, int c)
{
    WB55DmaChannel *ch = &s->ch[c];
    unsigned psize = dma_size(ch->ccr >> 8), msize = dma_size(ch->ccr >> 10);
    hwaddr pa = ch->cpar + ((ch->ccr & CCR_PINC) ? ch->pidx * psize : 0);
    hwaddr ma = ch->cmar + ((ch->ccr & CCR_MINC) ? ch->midx * msize : 0);
    uint32_t data = 0;
    MemTxResult r;

    if (ch->cached && ch->cached != (ch->ccr & CCR_CACHED)) {
        dma_uncache(ch);                /* (rewritten while enabled) */
    }
    if (ch->cached) {
        r = dma_beat_cached(ch, psize, msize);
    } else if (ch->ccr & CCR_DIR) {     /* memory -> peripheral */
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
    s->latched &= ~(1u << c);
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
    return req != 0 && req < 64 &&
           ((((s->req & ~s->pulse) >> req) & 1) || (s->latched & (1u << c)));
}

/*
 * A timer's update request is an event.  On silicon the DMA acknowledges
 * it with its first transfer, whatever that transfer touches; here a
 * channel's latch is that one transfer.  Taken as a level, a circular
 * channel that never touches the timer itself (RFID field detection: TIM1's
 * update copies TIM2's counter to RAM) would run without end.
 */
void wb55_dma_set_pulse(WB55DmaState *s, int req)
{
    s->pulse |= 1ull << req;
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
        uint16_t owed = 0;

        for (int c = 0; c < 14; c++) {
            if ((s->ch[c].ccr & CCR_EN) && (s->muxccr[c] & 0x3F) == n) {
                owed |= 1u << c;
            }
        }
        s->latched |= owed;
        dma_run(s);
        s->served[n] = owed & ~s->latched;
    }
}

bool wb55_dma_ready_for(WB55DmaState *s, int req)
{
    for (int c = 0; c < 14; c++) {
        WB55DmaChannel *ch = &s->ch[c];

        if ((s->served[req] & (1u << c)) &&
            (!(ch->ccr & CCR_EN) || ch->cndtr == 0)) {
            return false;
        }
    }
    return true;
}

/*
 * The firmware feeds a timer from a ring that its DMA interrupt refills by
 * halves: the half-transfer interrupt writes the first half anew while the
 * second is played, transfer-complete the second while the first is
 * (furi_hal_subghz's transmitter: 256 periods for TIM2's ARR; furi_hal_rfid's
 * emulation).  Such a channel that is about to enter a half: the SoC asks
 * the NVIC whether its handler is done (soc_tim2_dma_ready).
 */
int wb55_dma_at_half(WB55DmaState *s, int req)
{
    for (int c = 0; c < 14; c++) {
        WB55DmaChannel *ch = &s->ch[c];
        const uint32_t want = CCR_EN | CCR_CIRC;        /* (either way) */

        if ((ch->ccr & want) == want && (ch->ccr & (CCR_HTIE | CCR_TCIE)) &&
            (s->muxccr[c] & 0x3F) == req && ch->reload >= 2 &&
            (ch->cndtr == ch->reload || ch->cndtr == ch->reload / 2)) {
            return c;
        }
    }
    return -1;
}

/* is a channel on this request enabled? */
bool wb55_dma_on(WB55DmaState *s, int req)
{
    for (int c = 0; c < 14; c++) {
        if ((s->ch[c].ccr & CCR_EN) && (s->muxccr[c] & 0x3F) == req) {
            return true;
        }
    }
    return false;
}

/* is a channel on this request enabled, its two interrupts with it, and
 * none of its transfers done?  (The interrupts: the firmware's
 * signal_reader enables them last of all that it sets up.) */
bool wb55_dma_fresh(WB55DmaState *s, int req)
{
    const uint32_t want = CCR_EN | CCR_HTIE | CCR_TCIE;

    for (int c = 0; c < 14; c++) {
        WB55DmaChannel *ch = &s->ch[c];

        if ((ch->ccr & want) == want && (s->muxccr[c] & 0x3F) == req &&
            ch->cndtr && ch->cndtr == ch->reload) {
            return true;
        }
    }
    return false;
}

int wb55_dma_ring_mark(WB55DmaState *s, int req, hwaddr periph, uint32_t value)
{
    for (int c = 0; c < 14; c++) {
        WB55DmaChannel *ch = &s->ch[c];
        const uint32_t want = CCR_EN | CCR_DIR | CCR_CIRC;
        unsigned msize = dma_size(ch->ccr >> 10);
        uint32_t ring[256];

        if ((ch->ccr & (want | CCR_PINC | CCR_MINC)) != (want | CCR_MINC) ||
            ch->cpar != periph || (s->muxccr[c] & 0x3F) != req ||
            ch->reload < 2 || ch->reload > ARRAY_SIZE(ring) || msize != 4) {
            continue;
        }
        if (address_space_read(&address_space_memory, ch->cmar,
                               MEMTXATTRS_UNSPECIFIED, ring,
                               ch->reload * 4) != MEMTX_OK) {
            continue;
        }
        if (le32_to_cpu(ring[(ch->midx + 1) % ch->reload]) == value) {
            return 2;
        }
        for (unsigned i = 0; i < ch->reload; i++) {
            if (le32_to_cpu(ring[i]) == value) {
                return 1;
            }
        }
        return 0;
    }
    return -1;
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
    case 0x4:
        if (s->count_read && (s->ch[c].ccr & CCR_EN) && !s->in_service) {
            s->count_read(s->count_read_opaque, s->muxccr[c] & 0x3F);
        }
        return s->ch[c].cndtr;
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
        if (!(val & CCR_EN)) {
            s->latched &= ~(1u << c);
            dma_uncache(ch);
        }
        if (enabling) {
            ch->pidx = ch->midx = 0;
            ch->reload = ch->cndtr;
            dma_cache(ch);
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

    for (int c = 0; c < 14; c++) {
        dma_uncache(&s->ch[c]);
        s->ch[c].ccr = s->ch[c].cndtr = s->ch[c].cpar = s->ch[c].cmar = 0;
        s->ch[c].reload = s->ch[c].pidx = s->ch[c].midx = 0;
    }
    memset(s->isr, 0, sizeof(s->isr));
    memset(s->muxccr, 0, sizeof(s->muxccr));
    s->in_service = s->dirty = false;
    s->latched = 0;
    memset(s->served, 0, sizeof(s->served));
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
    for (int c = 0; c < 14; c++) {
        address_space_cache_init_empty(&s->ch[c].mcache);
        address_space_cache_init_empty(&s->ch[c].pcache);
    }
}

/* (a state loaded: its enabled channels looked up again) */
static int dma_post_load(void *opaque, int version_id)
{
    WB55DmaState *s = opaque;

    for (int c = 0; c < 14; c++) {
        dma_uncache(&s->ch[c]);
        dma_cache(&s->ch[c]);
    }
    return 0;
}

static const VMStateDescription vmstate_wb55_dma_channel = {
    .name = "stm32wb55-dma/channel",
    .version_id = 1,
    .minimum_version_id = 1,
    .fields = (const VMStateField[]) {
        VMSTATE_UINT32(ccr, WB55DmaChannel),
        VMSTATE_UINT32(cndtr, WB55DmaChannel),
        VMSTATE_UINT32(cpar, WB55DmaChannel),
        VMSTATE_UINT32(cmar, WB55DmaChannel),
        VMSTATE_UINT32(reload, WB55DmaChannel),
        VMSTATE_UINT32(pidx, WB55DmaChannel),
        VMSTATE_UINT32(midx, WB55DmaChannel),
        VMSTATE_END_OF_LIST()
    }
};

/* A saved state: with the request lines' levels, which nobody sends again */
static const VMStateDescription vmstate_wb55_dma = {
    .name = "stm32wb55-dma",
    .version_id = 1,
    .minimum_version_id = 1,
    .post_load = dma_post_load,
    .fields = (const VMStateField[]) {
        VMSTATE_STRUCT_ARRAY(ch, WB55DmaState, 14, 0, vmstate_wb55_dma_channel,
                             WB55DmaChannel),
        VMSTATE_UINT32_ARRAY(isr, WB55DmaState, 2),
        VMSTATE_UINT32_ARRAY(muxccr, WB55DmaState, 14),
        VMSTATE_UINT64(req, WB55DmaState),
        VMSTATE_UINT16(latched, WB55DmaState),
        VMSTATE_UINT16_ARRAY(served, WB55DmaState, 64),
        VMSTATE_BOOL(in_service, WB55DmaState),
        VMSTATE_BOOL(dirty, WB55DmaState),
        VMSTATE_END_OF_LIST()
    }
};

static void wb55_dma_class_init(ObjectClass *klass, const void *data)
{
    RESETTABLE_CLASS(klass)->phases.hold = wb55_dma_reset_hold;
    DEVICE_CLASS(klass)->vmsd = &vmstate_wb55_dma;
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
