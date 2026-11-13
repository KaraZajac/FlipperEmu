/*
 * STM32WB55 ADC1: one conversion at a time, of one channel
 *
 * What the firmware's furi_hal_adc asks for, and no more.  The ADC comes out
 * of deep power-down, calibrates (ADCAL ends at once) and is enabled (ADEN,
 * then ADRDY).  ADSTART converts the channel in the first rank of the regular
 * sequence (SQR1.SQ1), and the result is in DR at once, with EOC and EOS:
 * a conversion takes no time here (260 us on hardware with the firmware's
 * usual 64 samples).
 *
 * The result is what an ideal converter gives: the input over VREF+, in the
 * resolution of CFGR.RES; with oversampling (CFGR2.ROVSE) that many equal
 * samples added and shifted right (OVSR, OVSS).  No noise, no offset.
 *
 * Channels 1 to 16 are pins, and the board says what voltage is on one.
 * Three more are inside the chip, each behind a bit of CCR:
 *   0   VREFINT, 1.212 V
 *   17  the temperature sensor: 0.76 V at 30 degrees C and 2.5 mV for each
 *       degree more; the chip is at 25 degrees here
 *   18  a third of the VBAT pin's voltage, which is VDD's 3.3 V here
 * The factory's values that turn such a result into volts and degrees
 * (VREFINT_CAL, TS_CAL1, TS_CAL2 in the system memory) are the same numbers:
 * stm32wb55_flash.c writes them from this model's (wb55.h).
 *
 * Not modelled, and said once in the log of unimplemented things (-d unimp):
 * a sequence of more than one conversion, continuous and discontinuous mode,
 * a hardware trigger, injected conversions, DMA, the interrupt, left
 * alignment, differential inputs, offsets and the analog watchdogs.
 *
 * Copyright (c) 2026, Kara Zajac
 * SPDX-License-Identifier: BSD-3-Clause
 */
#include "qemu/osdep.h"
#include "qemu/log.h"
#include "migration/vmstate.h"
#include "wb55.h"

enum {
    R_ISR = 0x00 / 4, R_IER = 0x04 / 4, R_CR = 0x08 / 4, R_CFGR = 0x0C / 4,
    R_CFGR2 = 0x10 / 4, R_SQR1 = 0x30 / 4, R_DR = 0x40 / 4,
    R_CSR = 0x300 / 4, R_CCR = 0x308 / 4,
};

#define ISR_ADRDY   (1u << 0)
#define ISR_EOSMP   (1u << 1)
#define ISR_EOC     (1u << 2)
#define ISR_EOS     (1u << 3)
#define ISR_ALL     0x7FFu

#define CR_ADEN     (1u << 0)
#define CR_ADDIS    (1u << 1)
#define CR_ADSTART  (1u << 2)
#define CR_JADSTART (1u << 3)
#define CR_ADVREGEN (1u << 28)
#define CR_DEEPPWD  (1u << 29)
#define CR_ADCALDIF (1u << 30)
#define CR_KEPT     (CR_ADVREGEN | CR_DEEPPWD | CR_ADCALDIF)

#define CFGR_DMAEN  (1u << 0)
#define CFGR_ALIGN  (1u << 5)
#define CFGR_EXTEN  (3u << 10)
#define CFGR_CONT   (1u << 13)
#define CFGR_DISCEN (1u << 16)
#define CFGR_JQDIS  (1u << 31)

#define CCR_VREFEN  (1u << 22)
#define CCR_TSEN    (1u << 23)
#define CCR_VBATEN  (1u << 24)

static void adc_unimp(WB55AdcState *s, const char *what)
{
    if (!s->warned) {
        s->warned = true;
        qemu_log_mask(LOG_UNIMP, "wb55-adc: %s is not modelled: a single "
                      "conversion of the first rank is all there is\n", what);
    }
}

bool wb55_adc_enabled(WB55AdcState *s)
{
    return s->regs[R_CR] & CR_ADEN;
}

/* the input of a channel in microvolts */
static uint64_t adc_input_uv(WB55AdcState *s, int channel)
{
    uint32_t ccr = s->regs[R_CCR];

    switch (channel) {
    case 0:
        return ccr & CCR_VREFEN ? WB55_VREFINT_UV : 0;
    case 17:
        return ccr & CCR_TSEN ? WB55_TS_UV(WB55_CHIP_DEGREES) : 0;
    case 18:
        return ccr & CCR_VBATEN ? WB55_VDD_MV * 1000 / 3 : 0;
    }
    if (channel > 18 || !s->input) {
        return 0;
    }
    return (uint64_t)s->input(s->input_opaque, channel) * 1000;
}

static void adc_convert(WB55AdcState *s)
{
    uint32_t cfgr = s->regs[R_CFGR], cfgr2 = s->regs[R_CFGR2];
    int channel = extract32(s->regs[R_SQR1], 6, 5);
    int bits = 12 - 2 * extract32(cfgr, 3, 2);
    uint32_t ref = s->vref ? s->vref(s->vref_opaque) : WB55_VDD_MV;
    uint64_t uv = adc_input_uv(s, channel);
    uint64_t data = MIN((uv << bits) / ((uint64_t)ref * 1000),
                        (1u << bits) - 1);

    if (extract32(s->regs[R_SQR1], 0, 4)) {
        adc_unimp(s, "a sequence of several conversions");
    }
    if (cfgr & (CFGR_CONT | CFGR_DISCEN)) {
        adc_unimp(s, "continuous or discontinuous mode");
    }
    if (cfgr & CFGR_DMAEN) {
        adc_unimp(s, "DMA");
    }
    if (cfgr & CFGR_ALIGN) {
        adc_unimp(s, "left alignment");
    }
    if (cfgr2 & 1) {                    /* ROVSE: 2^(OVSR+1) samples */
        data = (data << (extract32(cfgr2, 2, 3) + 1)) >>
               MIN(extract32(cfgr2, 5, 4), 8);
    }
    s->regs[R_DR] = data & 0xFFFF;
    s->regs[R_ISR] |= ISR_EOSMP | ISR_EOC | ISR_EOS;
    s->conversions++;
    s->last_channel = channel;
    s->last_data = s->regs[R_DR];
    s->last_uv = uv;
}

static uint64_t wb55_adc_read(void *opaque, hwaddr addr, unsigned size)
{
    WB55AdcState *s = opaque;
    uint32_t val = s->regs[addr / 4];

    switch (addr / 4) {
    case R_DR:
        s->regs[R_ISR] &= ~ISR_EOC;     /* reading the result takes it */
        break;
    case R_CSR:
        val = s->regs[R_ISR];           /* the common block's copy of ISR */
        break;
    }
    return val;
}

static void wb55_adc_write(void *opaque, hwaddr addr, uint64_t val64,
                           unsigned size)
{
    WB55AdcState *s = opaque;
    uint32_t val = val64, *cr = &s->regs[R_CR];

    switch (addr / 4) {
    case R_ISR:
        s->regs[R_ISR] &= ~(val & ISR_ALL);     /* write 1 to clear */
        return;
    case R_IER:
        if (val) {
            adc_unimp(s, "the interrupt");
        }
        break;
    case R_CR:
        /*
         * ADEN, ADDIS, ADSTART and the like are set by a 1 and not cleared
         * by a 0: the firmware's LL_ADC_REG_StartConversion() writes ADSTART
         * with ADEN as 0, and the ADC stays enabled.  ADCAL, ADSTART, ADSTP
         * and ADDIS read as 0 again at once: what they start is over.
         */
        *cr = (*cr & CR_ADEN) | (val & CR_KEPT);
        if (val & CR_ADDIS) {
            *cr &= ~CR_ADEN;
            s->regs[R_ISR] &= ~ISR_ADRDY;
        } else if (val & CR_ADEN) {
            if (*cr & CR_DEEPPWD || !(*cr & CR_ADVREGEN)) {
                qemu_log_mask(LOG_GUEST_ERROR, "wb55-adc: enabled in deep "
                              "power-down or with its regulator off\n");
            }
            *cr |= CR_ADEN;
            s->regs[R_ISR] |= ISR_ADRDY;
        }
        if (val & CR_JADSTART) {
            adc_unimp(s, "an injected conversion");
        }
        if (val & CR_ADSTART) {
            if (!(*cr & CR_ADEN)) {
                qemu_log_mask(LOG_GUEST_ERROR, "wb55-adc: a conversion "
                              "started while the ADC is disabled\n");
            } else if (s->regs[R_CFGR] & CFGR_EXTEN) {
                adc_unimp(s, "a hardware trigger");
            } else {
                adc_convert(s);
            }
        }
        return;
    case R_DR:
    case R_CSR:
        return;                         /* read-only */
    }
    s->regs[addr / 4] = val;
}

static const MemoryRegionOps wb55_adc_ops = {
    .read = wb55_adc_read,
    .write = wb55_adc_write,
    .endianness = DEVICE_LITTLE_ENDIAN,
    .impl.min_access_size = 4,
    .impl.max_access_size = 4,
    .valid.min_access_size = 1,
    .valid.max_access_size = 4,
};

/* (what the ADC has converted since the emulator started is not a register:
 * a reset leaves it, as the firmware resets the ADC after each use) */
static void wb55_adc_reset_hold(Object *obj, ResetType type)
{
    WB55AdcState *s = WB55_ADC(obj);

    memset(s->regs, 0, sizeof(s->regs));
    s->regs[R_CR] = CR_DEEPPWD;
    s->regs[R_CFGR] = CFGR_JQDIS;
}

static void wb55_adc_init(Object *obj)
{
    WB55AdcState *s = WB55_ADC(obj);

    memory_region_init_io(&s->mmio, obj, &wb55_adc_ops, s, "adc1",
                          sizeof(s->regs));
    sysbus_init_mmio(SYS_BUS_DEVICE(obj), &s->mmio);
}

/* A saved state: the registers, and what `status` tells of the last
 * conversion */
static const VMStateDescription vmstate_wb55_adc = {
    .name = "stm32wb55-adc",
    .version_id = 1,
    .minimum_version_id = 1,
    .fields = (const VMStateField[]) {
        VMSTATE_UINT32_ARRAY(regs, WB55AdcState, 0x400 / 4),
        VMSTATE_UINT64(conversions, WB55AdcState),
        VMSTATE_UINT8(last_channel, WB55AdcState),
        VMSTATE_UINT16(last_data, WB55AdcState),
        VMSTATE_UINT32(last_uv, WB55AdcState),
        VMSTATE_END_OF_LIST()
    }
};

static void wb55_adc_class_init(ObjectClass *klass, const void *data)
{
    ResettableClass *rc = RESETTABLE_CLASS(klass);

    rc->phases.hold = wb55_adc_reset_hold;
    DEVICE_CLASS(klass)->vmsd = &vmstate_wb55_adc;
}

static const TypeInfo wb55_adc_info = {
    .name = TYPE_WB55_ADC,
    .parent = TYPE_SYS_BUS_DEVICE,
    .instance_size = sizeof(WB55AdcState),
    .instance_init = wb55_adc_init,
    .class_init = wb55_adc_class_init,
};

static void wb55_adc_register(void)
{
    type_register_static(&wb55_adc_info);
}
type_init(wb55_adc_register)
