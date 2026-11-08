/*
 * Flipper Zero infrared receiver (demodulating IR receiver on PA0)
 *
 * The receiver output idles high and is low while it sees a carrier burst
 * (a "mark").  It drives PA0, which the firmware reads, and TIM2's TI1
 * input, which captures the edges (furi_hal_infrared_async_rx_*).
 *
 * A signal is injected as mark/space durations in microseconds, starting
 * with a mark, written to the "rx" property of /machine/ir, e.g.
 *   qom-set /machine/ir rx 9000,4500,560,560,...,560
 * Every edge reaches TIM2 with its exact virtual time, so the captured
 * durations are exact even when QEMU's main loop fires a little late.
 *
 * Transmitting (furi_hal_infrared_async_tx_*): TIM1 makes the carrier with
 * PWM on CH3N (the IR LED on PB9; CH1N on PA7 for an external one).  A DMA
 * channel switches OC3M between PWM 2 (mark) and forced inactive (space) at
 * every update event, and another loads RCR, the number of carrier periods
 * until the next one.  The board reports each update; the marks and spaces
 * of a transmission are written, when the firmware stops TIM1, to "tx-dir"
 * (the launcher: devices/NAME/ir-tx/) as a raw signal in a Flipper .ir
 * file, which a real Flipper or `./flipperemu ir FILE` can play back.  "tx"
 * names the latest.
 *
 * Copyright (c) 2026, Kara Zajac
 * SPDX-License-Identifier: BSD-3-Clause
 */
#include "qemu/osdep.h"
#include "qemu/timer.h"
#include "qemu/cutils.h"
#include "qemu/error-report.h"
#include "qapi/error.h"
#include "hw/core/irq.h"
#include "hw/core/qdev-properties.h"
#include "wb55.h"
#include "flipper.h"

#define IR_MAX_EDGES 4096

static void ir_level(FlipperIrState *s, bool level, int64_t when)
{
    s->level = level;
    qemu_set_irq(s->pin, level);
    if (s->tim) {
        wb55_tim_input(s->tim, 0, level, when);
    }
}

/*
 * At next_ns the duration `pos` starts: even = mark (low), odd = space.
 *
 * Edges are spaced hundreds of microseconds apart, and on silicon the capture
 * interrupt is serviced long before the next one.  Here QEMU's main loop can
 * run this timer late, and the vCPU can lag (virtual time follows the host
 * clock even when the vCPU thread is not running).  Two captures in a row
 * would reach the firmware's ISR out of order or overwritten.  So an edge
 * waits until the firmware has read the previous capture (up to 250 ms).
 * Edges keep their timestamps, so the measured durations stay exact.
 */
static void ir_edge(void *opaque)
{
    FlipperIrState *s = opaque;
    int64_t when = s->next_ns;
    int64_t now = qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL);

    if (s->tim && wb55_tim_capture_pending(s->tim) &&
        now < when + 250 * SCALE_MS) {
        timer_mod(s->timer, now + 20 * SCALE_US);
        return;
    }

    if (s->pos >= s->len) {
        ir_level(s, true, when);        /* back to idle */
        g_free(s->dur);
        s->dur = NULL;
        s->len = s->pos = 0;
        return;
    }
    ir_level(s, s->pos & 1, when);
    s->next_ns = when + (int64_t)s->dur[s->pos++] * 1000;
    timer_mod(s->timer, s->next_ns);
}

static char *ir_get_rx(Object *obj, Error **errp)
{
    return g_strdup(FLIPPER_IR(obj)->dur ? "busy" : "");
}

static void ir_set_rx(Object *obj, const char *value, Error **errp)
{
    FlipperIrState *s = FLIPPER_IR(obj);
    g_auto(GStrv) parts = g_strsplit_set(value, " ,;\t\n", -1);
    g_autofree uint32_t *dur = g_new0(uint32_t, IR_MAX_EDGES);
    int n = 0;

    for (int i = 0; parts[i]; i++) {
        uint64_t v;

        if (!*parts[i]) {
            continue;
        }
        if (qemu_strtou64(parts[i][0] == '+' || parts[i][0] == '-' ?
                          parts[i] + 1 : parts[i], NULL, 10, &v) < 0 ||
            !v || v > 10 * 1000 * 1000 || n == IR_MAX_EDGES) {
            error_setg(errp, "ir rx: expected up to %d durations in µs (1..10 s), "
                       "got '%s'", IR_MAX_EDGES, parts[i]);
            return;
        }
        dur[n++] = v;
    }
    if (!n) {
        return;
    }
    timer_del(s->timer);
    g_free(s->dur);
    s->dur = g_steal_pointer(&dur);
    s->len = n;
    s->pos = 0;
    if (!s->level) {                    /* cut short mid-mark: release first */
        ir_level(s, true, qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL));
    }
    s->next_ns = qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL) + 2 * SCALE_MS;
    timer_mod(s->timer, s->next_ns);
}

void flipper_ir_idle(FlipperIrState *s)
{
    timer_del(s->timer);
    g_free(s->dur);
    s->dur = NULL;
    s->len = s->pos = 0;
    ir_level(s, true, qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL));
}

/* ------------------------------------------------------------ transmit */

void flipper_ir_tx(FlipperIrState *s, bool mark, int64_t when,
                   uint32_t freq_hz, uint32_t duty_ppm)
{
    uint32_t us;

    if (!s->tx) {
        if (!mark) {
            return;                     /* a transmission starts with a mark */
        }
        s->tx = g_array_new(false, false, sizeof(uint32_t));
        s->tx_edge_ns = when;
        s->tx_mark = true;
        s->tx_freq = freq_hz;
        s->tx_duty_ppm = duty_ppm;
        return;
    }
    if (mark == s->tx_mark) {
        return;
    }
    us = (MAX(when - s->tx_edge_ns, 0) + 500) / 1000;
    if (us) {
        g_array_append_val(s->tx, us);
    }
    s->tx_edge_ns = when;
    s->tx_mark = mark;
}

void flipper_ir_tx_end(FlipperIrState *s)
{
    g_autoptr(GString) out = NULL;
    g_autoptr(GError) err = NULL;
    g_autofree char *path = NULL;
    const uint32_t *d;
    guint n;

    if (!s->tx) {
        return;
    }
    if (s->tx_mark) {                   /* stopped mid-mark: keep it */
        flipper_ir_tx(s, false, qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL), 0, 0);
    }
    d = (const uint32_t *)s->tx->data;
    n = s->tx->len;                     /* mark, space, mark, ... */
    if (n % 2 == 0 && n) {
        n--;                            /* end on a mark */
    }
    if (n && s->tx_dir) {
        out = g_string_new(NULL);
        g_mkdir_with_parents(s->tx_dir, 0755);
        for (int k = 1; k < 100000; k++) {
            g_free(path);
            path = g_strdup_printf("%s/tx-%05d.ir", s->tx_dir, k);
            if (!g_file_test(path, G_FILE_TEST_EXISTS)) {
                break;
            }
        }
        g_string_append_printf(out, "Filetype: IR signals file\nVersion: 1\n#\n"
                               "name: tx_%05d\ntype: raw\nfrequency: %u\n"
                               "duty_cycle: %.6f\ndata:",
                               (int)strtol(strrchr(path, '-') + 1, NULL, 10),
                               s->tx_freq, s->tx_duty_ppm / 1e6);
        for (guint i = 0; i < n; i++) {
            g_string_append_printf(out, " %u", d[i]);
        }
        g_string_append_c(out, '\n');
        if (g_file_set_contents(path, out->str, out->len, &err)) {
            info_report("flipper-zero: infrared transmission at %u Hz, %u "
                        "marks/spaces -> %s", s->tx_freq, n, path);
            g_free(s->tx_last);
            s->tx_last = g_steal_pointer(&path);
        } else {
            warn_report("flipper-zero: %s", err->message);
        }
    }
    g_array_free(s->tx, true);
    s->tx = NULL;
}

static char *ir_get_tx(Object *obj, Error **errp)
{
    return g_strdup(FLIPPER_IR(obj)->tx_last ?: "");
}

static void ir_init(Object *obj)
{
    FlipperIrState *s = FLIPPER_IR(obj);

    s->level = true;
    s->timer = timer_new_ns(QEMU_CLOCK_VIRTUAL, ir_edge, s);
    qdev_init_gpio_out_named(DEVICE(obj), &s->pin, "out", 1);
    object_property_add_str(obj, "rx", ir_get_rx, ir_set_rx);
    object_property_set_description(obj, "rx",
        "Receive a signal: mark/space durations in µs, starting with a mark");
    object_property_add_str(obj, "tx", ir_get_tx, NULL);
    object_property_set_description(obj, "tx",
        "The latest transmission, saved as an .ir file");
}

static const Property ir_props[] = {
    DEFINE_PROP_STRING("tx-dir", FlipperIrState, tx_dir),
};

static void ir_class_init(ObjectClass *klass, const void *data)
{
    DEVICE_CLASS(klass)->user_creatable = false;
    device_class_set_props(DEVICE_CLASS(klass), ir_props);
}

static const TypeInfo ir_types[] = {
    {
        .name = TYPE_FLIPPER_IR,
        .parent = TYPE_DEVICE,
        .instance_size = sizeof(FlipperIrState),
        .instance_init = ir_init,
        .class_init = ir_class_init,
    },
};
DEFINE_TYPES(ir_types)
