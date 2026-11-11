/*
 * The Flipper's front panel on a link (docs/PANEL_LINK.md)
 *
 * A window that draws a Flipper around the emulator connects here.  It gets
 * the picture on the LCD, the backlight, the LED, the vibration motor and
 * the speaker's tone, and sends button presses: with a press and a release
 * of their own, so a button can be held for as long as a finger holds it.
 * It can also name QOM properties to be told about when they change (the
 * USB cable, the battery, the card in the field ...), so it needs no
 * monitor of its own.  And it is told when the machine stands still: paused
 * by someone, or stopped by a debugger.
 *
 * The link is a line protocol on a chardev, like the radios'.  Nothing here
 * runs while no program is connected.
 *
 * Copyright (c) 2026, Kara Zajac
 * SPDX-License-Identifier: BSD-3-Clause
 */
#include "qemu/osdep.h"
#include "qemu/timer.h"
#include "qapi/error.h"
#include "system/runstate.h"
#include "chardev/char.h"
#include "qobject/qobject.h"
#include "qobject/qjson.h"
#include "qom/qom-qobject.h"
#include "hw/core/qdev-properties.h"
#include "hw/core/qdev-properties-system.h"
#include "flipper.h"

#define TICK_MS       16            /* a frame and the lights: this often */
#define SETTLE_MS     3             /* a frame is arriving: look again then */
#define SETTLE_MAX    5             /* ... but no more often than this */
#define PROPS_MS      100           /* watched properties: this often */
#define WATCH_MAX     64
#define OUT_BUSY      (64 * 1024)   /* the window is behind: no frames */
/* The firmware debounces its buttons over a few milliseconds, and a touchpad
 * tap is a press and a release at once: a press lasts at least this long. */
#define PRESS_MIN_NS  (40 * SCALE_MS)

typedef struct PanelWatch {
    char *path, *name, *last;       /* last: the JSON reported, or NULL */
} PanelWatch;

static const char *const panel_keys[BTN_NUM] = {
    [BTN_UP] = "up", [BTN_DOWN] = "down", [BTN_LEFT] = "left",
    [BTN_RIGHT] = "right", [BTN_OK] = "ok", [BTN_BACK] = "back",
};

/* ---- the machine, stopped and going
 *
 * It stands still for two kinds of reason.  Somebody asked for it: a pause
 * (the launcher's, the window's), which lasts until somebody ends it; or a
 * debugger has it.  Or a link holds time still while the other side answers
 * (the NFC link, for the card on it): that is over in milliseconds, it is
 * the link's own business, and to everyone else the machine runs through
 * it.  A pause that comes during such a stop outlasts it.
 */
static bool vm_paused;          /* somebody asked for it */
static int vm_holds;            /* stops of a link's own */

bool flipper_vm_hold(void)
{
    if (!runstate_is_running()) {
        return false;
    }
    vm_holds++;
    vm_stop(RUN_STATE_PAUSED);
    return true;
}

void flipper_vm_release(void)
{
    assert(vm_holds > 0);
    if (!--vm_holds && !vm_paused) {
        vm_start();
    }
}

/* a gdb is on QEMU's stub */
static bool panel_gdb(void)
{
    Chardev *chr = qemu_chr_find("gdb");

    return chr && object_property_get_bool(OBJECT(chr), "connected", NULL);
}

/* QEMU's run state if the machine stands still, or -1 */
static int panel_stopped(void)
{
    RunState state = runstate_get();

    if (runstate_is_running() || (vm_holds && !vm_paused)) {
        return -1;
    }
    /* (one that was reset while it stood still is "prelaunch" to QEMU) */
    if (vm_paused || state == RUN_STATE_PRELAUNCH) {
        return RUN_STATE_PAUSED;
    }
    /*
     * QEMU has "debug" at a breakpoint only: a gdb that has just got on, or
     * interrupted, is "paused" to it, and is the debugger's stop all the same
     */
    return state == RUN_STATE_PAUSED && panel_gdb() ? RUN_STATE_DEBUG : state;
}

static void panel_vm_change(void *opaque, bool running, RunState state)
{
    if (running) {
        vm_paused = false;      /* whoever said go: the monitor, a debugger */
    }
}

static bool panel_get_paused(Object *obj, Error **errp)
{
    return vm_paused;
}

static void panel_set_paused(Object *obj, bool value, Error **errp)
{
    vm_paused = value;
    if (value) {
        vm_stop(RUN_STATE_PAUSED);      /* (nothing, if it stands still) */
    } else if (!vm_holds) {
        vm_start();
    }
}

static char *panel_get_stopped(Object *obj, Error **errp)
{
    int state = panel_stopped();

    return g_strdup(state < 0 ? "" : RunState_str(state));
}

static void panel_running(FlipperPanelState *s, bool all)
{
    int state = panel_stopped();

    if (all || state != s->stopped) {
        s->stopped = state;
        if (state < 0) {
            flipper_link_send(&s->lk, "running 1\n");
        } else {
            flipper_link_send(&s->lk, "running 0 %s\n", RunState_str(state));
        }
    }
}

/* ---- out: the picture and the lights */

/* The frame as the link carries it: 64 rows of 16 bytes, top row first, the
 * leftmost pixel in a byte's top bit, 1 for a pixel that is on. */
static void panel_lcd(FlipperPanelState *s, bool all)
{
    static const char hex[] = "0123456789abcdef";
    uint8_t f[sizeof(s->frame)] = { 0 };
    char line[2 * sizeof(f) + 1];

    if (all) {
        s->frame_sent = false;
    }
    if (s->lk.out->len > OUT_BUSY) {
        return;                     /* it gets the picture once it caught up */
    }
    for (int y = 0; y < 64; y++) {
        for (int x = 0; x < 128; x++) {
            if (st7567_pixel(s->lcd, x, y)) {
                f[y * 16 + x / 8] |= 0x80 >> (x & 7);
            }
        }
    }
    if (s->frame_sent && !memcmp(f, s->frame, sizeof(f))) {
        return;
    }
    memcpy(s->frame, f, sizeof(f));
    s->frame_sent = true;
    for (int i = 0; i < sizeof(f); i++) {
        line[2 * i] = hex[f[i] >> 4];
        line[2 * i + 1] = hex[f[i] & 15];
    }
    line[2 * sizeof(f)] = 0;
    flipper_link_send(&s->lk, "lcd %s\n", line);
}

static void panel_lights(FlipperPanelState *s, bool all)
{
    St7567State *lcd = s->lcd;
    uint32_t freq = s->speaker->on ? s->speaker->freq_mhz : 0;
    uint32_t duty = s->speaker->on ? s->speaker->duty_ppm : 0;
    uint32_t led = st7567_led(lcd, LED_FOR_PANEL);

    if (all || lcd->backlight != s->backlight) {
        s->backlight = lcd->backlight;
        flipper_link_send(&s->lk, "backlight %u\n", s->backlight);
    }
    if (all || led != s->led) {
        s->led = led;
        flipper_link_send(&s->lk, "led %06x\n", s->led);
    }
    if (all || lcd->vibro != s->vibro) {
        s->vibro = lcd->vibro;
        flipper_link_send(&s->lk, "vibro %d\n", s->vibro);
    }
    if (all || freq != s->freq || duty != s->duty) {
        s->freq = freq;
        s->duty = duty;
        flipper_link_send(&s->lk, "speaker freq=%u duty=%u\n", freq, duty);
    }
}

void flipper_panel_sync(FlipperPanelState *s)
{
    if (s && s->lk.open) {
        panel_lights(s, false);
    }
}

void flipper_panel_power_off(FlipperPanelState *s)
{
    if (s) {
        flipper_link_send(&s->lk, "power 0\n");
    }
}

void flipper_panel_halted(FlipperPanelState *s, uint32_t at)
{
    if (s && s->lk.open) {
        flipper_link_send(&s->lk, "halted 0x%08x\n", at);
    }
}

/* ---- out: properties the window watches */

static void panel_watch_free(void *p)
{
    PanelWatch *w = p;

    g_free(w->path);
    g_free(w->name);
    g_free(w->last);
    g_free(w);
}

/* a property's value as JSON, as the monitor's qom-get has it; null: none */
static char *panel_prop_json(const char *path, const char *name)
{
    Object *obj = object_resolve_path(path, NULL);
    QObject *v = obj ? object_property_get_qobject(obj, name, NULL) : NULL;
    GString *js;

    if (!v) {
        return g_strdup("null");
    }
    js = qobject_to_json(v);
    qobject_unref(v);
    return g_string_free(js, FALSE);
}

static void panel_props(FlipperPanelState *s, bool all)
{
    for (guint i = 0; i < s->watches->len; i++) {
        PanelWatch *w = g_ptr_array_index(s->watches, i);
        char *now = panel_prop_json(w->path, w->name);

        if (all || !w->last || strcmp(now, w->last)) {
            flipper_link_send(&s->lk, "prop %s %s %s\n", w->path, w->name,
                              now);
            g_free(w->last);
            w->last = now;
        } else {
            g_free(now);
        }
    }
}

static void panel_watch(FlipperPanelState *s, const char *path,
                        const char *name)
{
    PanelWatch *w = NULL;

    for (guint i = 0; i < s->watches->len && !w; i++) {
        PanelWatch *have = g_ptr_array_index(s->watches, i);

        if (!strcmp(have->path, path) && !strcmp(have->name, name)) {
            w = have;
        }
    }
    if (w) {
        g_clear_pointer(&w->last, g_free);      /* say it again */
    } else if (s->watches->len < WATCH_MAX) {
        w = g_new0(PanelWatch, 1);
        w->path = g_strdup(path);
        w->name = g_strdup(name);
        g_ptr_array_add(s->watches, w);
    }
    panel_props(s, false);
}

static void panel_everything(FlipperPanelState *s)
{
    panel_running(s, true);
    panel_lights(s, true);
    panel_lcd(s, true);
    panel_props(s, true);
    s->lcd_bytes = s->lcd->bytes;
    s->settling = 0;
}

static void panel_tick(void *opaque)
{
    FlipperPanelState *s = opaque;
    int64_t now = qemu_clock_get_ms(QEMU_CLOCK_REALTIME);
    int64_t next = TICK_MS;

    if (!s->lk.open) {
        return;
    }
    panel_running(s, false);
    panel_lights(s, false);
    if (s->lcd->bytes != s->lcd_bytes && s->settling < SETTLE_MAX) {
        /* the firmware is sending a frame: wait for all of it, briefly */
        s->settling++;
        next = SETTLE_MS;
    } else {
        s->settling = 0;
        panel_lcd(s, false);
    }
    s->lcd_bytes = s->lcd->bytes;
    if (now >= s->props_at) {
        s->props_at = now + PROPS_MS;
        panel_props(s, false);
    }
    timer_mod(s->tick, now + next);
}

/* ---- in: the buttons */

static void panel_release_arm(FlipperPanelState *s)
{
    int64_t at = INT64_MAX;

    for (int b = 0; b < BTN_NUM; b++) {
        if (s->releasing & (1u << b)) {
            at = MIN(at, s->pressed_at[b] + PRESS_MIN_NS);
        }
    }
    if (at == INT64_MAX) {
        timer_del(s->release);
    } else {
        timer_mod(s->release, at);
    }
}

static void panel_key(FlipperPanelState *s, int b, bool down)
{
    int64_t now = qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL);
    uint32_t bit = 1u << b;

    if (down) {
        s->releasing &= ~bit;       /* pressed again: still the same press */
        if (!(s->held & bit)) {
            s->held |= bit;
            s->pressed_at[b] = now;
            s->key(s->key_opaque, b, true);
        }
    } else if (s->held & bit) {
        if (now - s->pressed_at[b] >= PRESS_MIN_NS) {
            s->held &= ~bit;
            s->releasing &= ~bit;
            s->key(s->key_opaque, b, false);
        } else {
            s->releasing |= bit;    /* too soon for the firmware to see it */
        }
    }
    panel_release_arm(s);
}

static void panel_release(void *opaque)
{
    FlipperPanelState *s = opaque;

    for (int b = 0; b < BTN_NUM; b++) {
        if (s->releasing & (1u << b)) {
            panel_key(s, b, false);
        }
    }
}

/* ---- the link */

static void panel_line(void *opaque, char *line)
{
    FlipperPanelState *s = opaque;
    char *save = NULL;
    const char *cmd = strtok_r(line, " \t", &save);
    const char *a = cmd ? strtok_r(NULL, " \t", &save) : NULL;
    const char *b = a ? strtok_r(NULL, " \t", &save) : NULL;

    if (!cmd) {
        return;
    }
    if (!strcmp(cmd, "press") || !strcmp(cmd, "release")) {
        for (int i = 0; a && i < BTN_NUM; i++) {
            if (!strcmp(a, panel_keys[i])) {
                panel_key(s, i, cmd[0] == 'p');
            }
        }
    } else if (!strcmp(cmd, "refresh")) {
        panel_everything(s);
    } else if (!strcmp(cmd, "watch") && b) {
        panel_watch(s, a, b);
    }
}

static void panel_opened(void *opaque, bool open)
{
    FlipperPanelState *s = opaque;

    g_ptr_array_set_size(s->watches, 0);
    if (open) {
        flipper_link_send(&s->lk, "hello flipperemu panel 1\n");
        panel_everything(s);
        s->props_at = 0;
        timer_mod(s->tick, qemu_clock_get_ms(QEMU_CLOCK_REALTIME) + TICK_MS);
    } else {
        timer_del(s->tick);
        for (int b = 0; b < BTN_NUM; b++) {     /* no finger left on them */
            panel_key(s, b, false);
        }
    }
}

static void panel_realize(DeviceState *dev, Error **errp)
{
    FlipperPanelState *s = FLIPPER_PANEL(dev);

    s->tick = timer_new_ms(QEMU_CLOCK_REALTIME, panel_tick, s);
    s->release = timer_new_ns(QEMU_CLOCK_VIRTUAL, panel_release, s);
    s->watches = g_ptr_array_new_with_free_func(panel_watch_free);
    qemu_add_vm_change_state_handler(panel_vm_change, s);
    flipper_link_init(&s->lk, &s->link, "panel", panel_line, panel_opened, s);
}

static const Property panel_props_list[] = {
    DEFINE_PROP_CHR("link", FlipperPanelState, link),
};

static void panel_class_init(ObjectClass *klass, const void *data)
{
    DeviceClass *dc = DEVICE_CLASS(klass);

    dc->realize = panel_realize;
    dc->user_creatable = false;
    device_class_set_props(dc, panel_props_list);
    object_class_property_add_bool(klass, "paused", panel_get_paused,
                                   panel_set_paused);
    object_class_property_set_description(klass, "paused",
        "The machine stands still because somebody asked for it; set it to "
        "pause and to go on");
    object_class_property_add_str(klass, "stopped", panel_get_stopped, NULL);
    object_class_property_set_description(klass, "stopped",
        "Why the machine stands still (QEMU's run state: paused, debug ...), "
        "or empty: it runs");
}

static const TypeInfo panel_types[] = {
    {
        .name = TYPE_FLIPPER_PANEL,
        .parent = TYPE_DEVICE,
        .instance_size = sizeof(FlipperPanelState),
        .class_init = panel_class_init,
    },
};
DEFINE_TYPES(panel_types)
