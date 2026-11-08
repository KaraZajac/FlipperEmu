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
 * Copyright (c) 2026, Kara Zajac
 * SPDX-License-Identifier: BSD-3-Clause
 */
#include "qemu/osdep.h"
#include "qemu/timer.h"
#include "qemu/cutils.h"
#include "qapi/error.h"
#include "hw/core/irq.h"
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
 * interrupt is serviced long before the next one.  QEMU's main loop can run
 * this timer late and deliver two edges back to back, and the firmware's ISR
 * would then see the rising-edge capture before the falling one.  So an edge
 * waits (up to 10 ms) until the previous capture has been serviced.  Edges
 * keep their timestamps, so the measured durations stay exact.
 */
static void ir_edge(void *opaque)
{
    FlipperIrState *s = opaque;
    int64_t when = s->next_ns;
    int64_t now = qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL);

    if (s->tim && wb55_tim_capture_pending(s->tim) &&
        now < when + 10 * SCALE_MS) {
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

static void ir_init(Object *obj)
{
    FlipperIrState *s = FLIPPER_IR(obj);

    s->level = true;
    s->timer = timer_new_ns(QEMU_CLOCK_VIRTUAL, ir_edge, s);
    qdev_init_gpio_out_named(DEVICE(obj), &s->pin, "out", 1);
    object_property_add_str(obj, "rx", ir_get_rx, ir_set_rx);
    object_property_set_description(obj, "rx",
        "Receive a signal: mark/space durations in µs, starting with a mark");
}

static void ir_class_init(ObjectClass *klass, const void *data)
{
    DEVICE_CLASS(klass)->user_creatable = false;
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
