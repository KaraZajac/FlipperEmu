/*
 * STM32WB55 SPI (RM0434 chapter 35, the "FIFO" SPI of STM32L4/WB)
 *
 * Master-mode model: a frame written to DR is shifted out immediately over the
 * SSI bus (chip selects are GPIOs, driven by the firmware), so TXE is always
 * set, FTLVL is 0 and BSY is 0. The received frame enters a 32-bit RX FIFO
 * (four 8-bit frames); FRLVL reports its level and overflow sets OVR.
 *
 * The firmware drains the RX FIFO by FRLVL after every transfer
 * (furi_hal_spi_bus_end_txrx), reads byte-wide (LL_SPI_ReceiveData8) and runs
 * SD-card blocks through DMA2 ch6/ch7 (furi_hal_spi_bus_trx_dma); the DMA
 * request lines below are levels: TX = TXDMAEN, RX = RXDMAEN && RXNE.
 *
 * Copyright (c) 2026, Kara Zajac
 * SPDX-License-Identifier: BSD-3-Clause
 */
#include "qemu/osdep.h"
#include "qemu/log.h"
#include "hw/core/irq.h"
#include "wb55.h"

#define CR1_SPE    (1u << 6)
#define CR2_RXDMAEN (1u << 0)
#define CR2_TXDMAEN (1u << 1)
#define CR2_ERRIE  (1u << 5)
#define CR2_RXNEIE (1u << 6)
#define CR2_TXEIE  (1u << 7)
#define CR2_FRXTH  (1u << 12)

#define SR_RXNE    (1u << 0)
#define SR_TXE     (1u << 1)
#define SR_OVR     (1u << 6)

static unsigned spi_ds_bits(WB55SpiState *s)
{
    unsigned ds = (s->cr2 >> 8) & 0xF;
    return ds < 3 ? 8 : ds + 1;
}

static bool spi_rxne(WB55SpiState *s)
{
    if (spi_ds_bits(s) > 8) {
        return s->rxcount >= 2;
    }
    return (s->cr2 & CR2_FRXTH) ? s->rxcount >= 1 : s->rxcount >= 2;
}

static uint32_t spi_sr(WB55SpiState *s)
{
    uint32_t sr = SR_TXE | s->sr_err;
    unsigned lvl = s->rxcount >= 3 ? 3 : s->rxcount;

    if (spi_rxne(s)) {
        sr |= SR_RXNE;
    }
    sr |= lvl << 9;                     /* FRLVL */
    return sr;
}

static void spi_update(WB55SpiState *s)
{
    uint32_t sr = spi_sr(s);
    bool irq = ((s->cr2 & CR2_RXNEIE) && (sr & SR_RXNE)) ||
               ((s->cr2 & CR2_TXEIE) && (sr & SR_TXE)) ||
               ((s->cr2 & CR2_ERRIE) && (sr & SR_OVR));

    qemu_set_irq(s->irq, irq);
    qemu_set_irq(s->dma_tx, (s->cr1 & CR1_SPE) && (s->cr2 & CR2_TXDMAEN));
    qemu_set_irq(s->dma_rx, (s->cr2 & CR2_RXDMAEN) && (sr & SR_RXNE));
}

static void spi_push_rx(WB55SpiState *s, uint8_t b)
{
    if (s->rxcount >= 4) {
        s->sr_err |= SR_OVR;            /* overrun: new data is lost */
        return;
    }
    s->rxfifo[s->rxcount++] = b;
}

static uint8_t spi_pop_rx(WB55SpiState *s)
{
    uint8_t b = s->rxfifo[0];

    if (s->rxcount == 0) {
        return 0;
    }
    memmove(s->rxfifo, s->rxfifo + 1, 3);
    s->rxcount--;
    return b;
}

static void spi_xfer_frame(WB55SpiState *s, uint32_t tx, unsigned bits)
{
    uint32_t rx = ssi_transfer(s->bus, tx & ((1u << bits) - 1));

    spi_push_rx(s, rx & 0xFF);
    if (bits > 8) {
        spi_push_rx(s, (rx >> 8) & 0xFF);
    }
}

static uint64_t wb55_spi_read(void *opaque, hwaddr addr, unsigned size)
{
    WB55SpiState *s = opaque;
    uint64_t v = 0;

    switch (addr) {
    case 0x00: v = s->cr1; break;
    case 0x04: v = s->cr2; break;
    case 0x08: v = spi_sr(s); break;
    case 0x0C:
        if (size == 1) {
            v = spi_pop_rx(s);
        } else {
            v = spi_pop_rx(s);
            v |= (uint32_t)spi_pop_rx(s) << 8;
        }
        break;
    case 0x10: v = s->crcpr; break;
    }
    spi_update(s);
    return v;
}

static void wb55_spi_write(void *opaque, hwaddr addr, uint64_t val64,
                           unsigned size)
{
    WB55SpiState *s = opaque;
    uint32_t val = val64;

    switch (addr) {
    case 0x00:
        s->cr1 = val & 0xFFFF;
        break;
    case 0x04:
        s->cr2 = val & 0x7FFF;
        break;
    case 0x08:
        /* CRCERR is rc_w0; OVR/MODF/FRE clear by read sequences - accept
         * an explicit write as a clear to keep LL_SPI_ClearFlag_* happy */
        s->sr_err &= val;
        break;
    case 0x0C:
        if (!(s->cr1 & CR1_SPE)) {
            qemu_log_mask(LOG_GUEST_ERROR, "wb55-spi: DR write while SPE=0\n");
        }
        if (spi_ds_bits(s) > 8) {
            spi_xfer_frame(s, val, spi_ds_bits(s));
        } else if (size == 1) {
            spi_xfer_frame(s, val, 8);
        } else {
            /* data packing: 16-bit write in 8-bit mode sends two frames */
            spi_xfer_frame(s, val, 8);
            spi_xfer_frame(s, val >> 8, 8);
        }
        break;
    case 0x10:
        s->crcpr = val & 0xFFFF;
        break;
    default:
        break;
    }
    spi_update(s);
}

static const MemoryRegionOps wb55_spi_ops = {
    .read = wb55_spi_read,
    .write = wb55_spi_write,
    .endianness = DEVICE_LITTLE_ENDIAN,
    .valid.min_access_size = 1,
    .valid.max_access_size = 4,
    .impl.min_access_size = 1,
    .impl.max_access_size = 4,
};

static void wb55_spi_reset_hold(Object *obj, ResetType type)
{
    WB55SpiState *s = WB55_SPI(obj);

    s->cr1 = 0;
    s->cr2 = 0x0700;                    /* DS = 8 bit */
    s->sr_err = 0;
    s->crcpr = 7;
    s->rxcount = 0;
}

static void wb55_spi_reset_exit(Object *obj, ResetType type)
{
    spi_update(WB55_SPI(obj));
}

static void wb55_spi_init(Object *obj)
{
    WB55SpiState *s = WB55_SPI(obj);

    memory_region_init_io(&s->mmio, obj, &wb55_spi_ops, s, "spi", 0x400);
    sysbus_init_mmio(SYS_BUS_DEVICE(obj), &s->mmio);
    sysbus_init_irq(SYS_BUS_DEVICE(obj), &s->irq);
    qdev_init_gpio_out_named(DEVICE(obj), &s->dma_rx, "dma-rx", 1);
    qdev_init_gpio_out_named(DEVICE(obj), &s->dma_tx, "dma-tx", 1);
    s->bus = ssi_create_bus(DEVICE(obj), "ssi");
}

static void wb55_spi_class_init(ObjectClass *klass, const void *data)
{
    ResettableClass *rc = RESETTABLE_CLASS(klass);

    rc->phases.hold = wb55_spi_reset_hold;
    rc->phases.exit = wb55_spi_reset_exit;
}

static const TypeInfo wb55_spi_info = {
    .name = TYPE_WB55_SPI,
    .parent = TYPE_SYS_BUS_DEVICE,
    .instance_size = sizeof(WB55SpiState),
    .instance_init = wb55_spi_init,
    .class_init = wb55_spi_class_init,
};

static void wb55_spi_register(void)
{
    type_register_static(&wb55_spi_info);
}
type_init(wb55_spi_register)
