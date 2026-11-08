/*
 * Sitronix ST7567 / ST7565 128x64 monochrome LCD controller (Flipper panel)
 *
 * SPI write-only interface; the A0 pin (Flipper "DI", PB1) selects command (0)
 * or display data (1); RST (PB0) is active low; CS (PC11) active low.
 *
 * Geometry, derived from the firmware's u8x8 driver (lib/u8g2/u8g2_glue.c):
 * normal mode programs SEG normal (0xA0), COM reverse (0xC8) and writes
 * columns 0..127; "flip" (left-handed) mode programs 0xA1/0xC0 and an x
 * offset of 4. Both are upright/rotated-180 only if the panel's top row is
 * COM63 and its left column SEG0, which is what is rendered here.
 *
 * Copyright (c) 2026, Kara Zajac
 * SPDX-License-Identifier: BSD-3-Clause
 */
#include "qemu/osdep.h"
#include "qemu/log.h"
#include "hw/ssi/ssi.h"
#include "hw/core/irq.h"
#include "hw/core/qdev-properties.h"
#include "ui/console.h"
#include "ui/pixel_ops.h"
#include "flipper.h"

#define COLS 132
#define PAGES 9

static void st7567_reset_state(St7567State *s)
{
    s->page = 0;
    s->col = 0;
    s->start_line = 0;
    s->adc_reverse = false;
    s->com_reverse = false;
    s->display_on = false;
    s->inverse = false;
    s->all_on = false;
    s->contrast = 32;
    s->pending_arg = 0;
    s->rmw = false;
    s->dirty = true;
}

static void st7567_command(St7567State *s, uint8_t c)
{
    if (s->pending_arg) {
        if (s->pending_arg == 0x81) {
            s->contrast = c & 0x3F;
        }
        s->pending_arg = 0;
        return;
    }
    switch (c) {
    case 0xAE: case 0xAF:
        s->display_on = c & 1;
        break;
    case 0xA0: case 0xA1:
        s->adc_reverse = c & 1;
        break;
    case 0xA2: case 0xA3:               /* bias */
        break;
    case 0xA4: case 0xA5:
        s->all_on = c & 1;
        break;
    case 0xA6: case 0xA7:
        s->inverse = c & 1;
        break;
    case 0xE0:                          /* read-modify-write */
        s->rmw = true;
        s->rmw_col = s->col;
        return;
    case 0xEE:
        s->rmw = false;
        s->col = s->rmw_col;
        return;
    case 0xE2:                          /* software reset */
        s->page = 0;
        s->col = 0;
        s->start_line = 0;
        s->contrast = 32;
        break;
    case 0xE3:                          /* NOP */
        return;
    case 0x81: case 0xF8:               /* two-byte commands */
        s->pending_arg = c;
        return;
    default:
        if ((c & 0xC0) == 0x40) {
            s->start_line = c & 0x3F;
        } else if ((c & 0xF0) == 0xB0) {
            s->page = c & 0x0F;
        } else if ((c & 0xF0) == 0x10) {
            s->col = (s->col & 0x0F) | ((c & 0x0F) << 4);
        } else if ((c & 0xF0) == 0x00) {
            s->col = (s->col & 0xF0) | (c & 0x0F);
        } else if ((c & 0xF0) == 0xC0) {
            s->com_reverse = (c >> 3) & 1;
        } else if ((c & 0xF8) == 0x28 || (c & 0xF8) == 0x20) {
            /* power control / regulation ratio */
        } else {
            qemu_log_mask(LOG_UNIMP, "st7567: command 0x%02x\n", c);
        }
        break;
    }
    s->dirty = true;
}

static uint32_t st7567_transfer(SSIPeripheral *dev, uint32_t val)
{
    St7567State *s = ST7567(dev);

    if (!s->rst_n) {
        return 0;
    }
    if (s->a0) {
        if (s->page < PAGES && s->col < COLS) {
            s->ram[s->page][s->col] = val;
        }
        if (s->col < COLS - 1) {
            s->col++;
        }
        s->dirty = true;
        s->bytes++;
    } else {
        st7567_command(s, val);
    }
    return 0;
}

static void st7567_a0(void *opaque, int n, int level)
{
    ST7567(opaque)->a0 = level;
}

static void st7567_rst(void *opaque, int n, int level)
{
    St7567State *s = opaque;

    if (!level && s->rst_n) {
        st7567_reset_state(s);
    }
    s->rst_n = level;
}

bool st7567_pixel(St7567State *s, int x, int y)
{
    int col = s->adc_reverse ? (COLS - 1 - x) : x;
    int line = s->com_reverse ? y : (63 - y);
    int row = (line + s->start_line) & 63;
    bool on = (s->ram[row / 8][col] >> (row % 8)) & 1;

    if (s->inverse) {
        on = !on;
    }
    if (s->all_on) {
        on = true;
    }
    return s->display_on && on;
}

static uint32_t mix(uint32_t a, uint32_t b, unsigned t)    /* t: 0..255 */
{
    uint32_t r = (((a >> 16) & 0xFF) * (255 - t) + ((b >> 16) & 0xFF) * t) / 255;
    uint32_t g = (((a >> 8) & 0xFF) * (255 - t) + ((b >> 8) & 0xFF) * t) / 255;
    uint32_t bl = ((a & 0xFF) * (255 - t) + (b & 0xFF) * t) / 255;
    return (r << 16) | (g << 8) | bl;
}

static void fill(uint32_t *fb, int stride, int x0, int y0, int w, int h,
                 uint32_t c)
{
    for (int y = y0; y < y0 + h; y++) {
        for (int x = x0; x < x0 + w; x++) {
            fb[y * stride + x] = c;
        }
    }
}

static bool st7567_update(void *opaque)
{
    St7567State *s = opaque;
    DisplaySurface *surface = qemu_console_surface(s->con);
    int sc = s->scale, b = s->border;
    int w = 128 * sc + 2 * b, h = 64 * sc + 2 * b + s->status_h;

    if (!s->dirty && !s->status_dirty) {
        return true;
    }
    if (surface_width(surface) != w || surface_height(surface) != h) {
        qemu_console_resize(s->con, w, h);
        surface = qemu_console_surface(s->con);
    }
    if (surface_bits_per_pixel(surface) != 32) {
        return true;
    }
    uint32_t *fb = (uint32_t *)surface_data(surface);
    int stride = surface_stride(surface) / 4;

    /* Flipper LCD: orange backlight, dark pixels; unlit panel is dim amber */
    unsigned bl = s->backlight;
    uint32_t bg = mix(0x3A2A18, 0xFF8C1A, bl);
    uint32_t fg = mix(0x241A10, 0x1A0E04, bl);

    fill(fb, stride, 0, 0, w, h, 0x181818);                 /* bezel */
    fill(fb, stride, b - 2, b - 2, 128 * sc + 4, 64 * sc + 4, 0x000000);
    for (int y = 0; y < 64; y++) {
        for (int x = 0; x < 128; x++) {
            uint32_t c = st7567_pixel(s, x, y) ? fg : bg;
            fill(fb, stride, b + x * sc, b + y * sc, sc, sc, c);
            if (sc >= 4) {                                   /* pixel grid */
                fill(fb, stride, b + x * sc + sc - 1, b + y * sc, 1, sc,
                     mix(c, bg, 96));
                fill(fb, stride, b + x * sc, b + y * sc + sc - 1, sc, 1,
                     mix(c, bg, 96));
            }
        }
    }
    /* status strip: RGB LED and vibro indicators */
    int sy = 64 * sc + 2 * b, r = s->status_h / 3;
    if (s->status_h) {
        uint32_t led = s->led_rgb;
        fill(fb, stride, b, sy + (s->status_h - 2 * r) / 2, 2 * r, 2 * r,
             led ? led : 0x303030);
        if (s->vibro) {
            fill(fb, stride, b + 4 * r, sy + (s->status_h - 2 * r) / 2,
                 3 * r, 2 * r, 0x8080FF);
        }
        if (s->speaker) {
            fill(fb, stride, b + 8 * r, sy + (s->status_h - 2 * r) / 2,
                 2 * r, 2 * r, 0x60FF60);
        }
    }
    qemu_console_update(s->con, 0, 0, w, h);
    s->dirty = false;
    s->status_dirty = false;
    return true;
}

static void st7567_invalidate(void *opaque)
{
    ST7567(opaque)->dirty = true;
}

static const GraphicHwOps st7567_ops = {
    .invalidate = st7567_invalidate,
    .gfx_update = st7567_update,
};

static void st7567_realize(SSIPeripheral *d, Error **errp)
{
    St7567State *s = ST7567(d);
    DeviceState *dev = DEVICE(d);

    if (s->scale < 1) {
        s->scale = 1;
    }
    s->status_h = s->scale >= 3 ? 6 * s->scale : 0;
    s->border = 4 * s->scale;
    s->con = qemu_graphic_console_create(dev, 0, &st7567_ops, s);
    qemu_console_resize(s->con, 128 * s->scale + 2 * s->border,
                        64 * s->scale + 2 * s->border + s->status_h);
    qdev_init_gpio_in_named(dev, st7567_a0, "a0", 1);
    qdev_init_gpio_in_named(dev, st7567_rst, "rst", 1);
    s->rst_n = true;
    st7567_reset_state(s);
}

static const Property st7567_props[] = {
    DEFINE_PROP_UINT32("scale", St7567State, scale, 4),
};

static void st7567_class_init(ObjectClass *klass, const void *data)
{
    DeviceClass *dc = DEVICE_CLASS(klass);
    SSIPeripheralClass *k = SSI_PERIPHERAL_CLASS(klass);

    k->realize = st7567_realize;
    k->transfer = st7567_transfer;
    k->cs_polarity = SSI_CS_LOW;
    device_class_set_props(dc, st7567_props);
    set_bit(DEVICE_CATEGORY_DISPLAY, dc->categories);
}

static const TypeInfo st7567_info = {
    .name = TYPE_ST7567,
    .parent = TYPE_SSI_PERIPHERAL,
    .instance_size = sizeof(St7567State),
    .class_init = st7567_class_init,
};

static void st7567_register(void)
{
    type_register_static(&st7567_info);
}
type_init(st7567_register)
