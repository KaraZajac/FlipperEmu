/*
 * Flipper Zero 125 kHz RFID front end, with a virtual tag in its field
 *
 * Reading (furi_hal_rfid_tim_read_*): TIM1 drives the antenna on CH1N (PB13,
 * RFID_OUT), at 125 kHz and 50% duty for ASK and FSK tags, or at 62.5 kHz and
 * 25% duty, which still rings the 125 kHz antenna tank and turns a PSK tag's
 * phase into levels.  The analog front end demodulates the tag's load
 * modulation onto PC5 (RFID_RF_IN), COMP1 squares it, and TIM2 (TI4 remapped
 * to COMP1) captures the edges.
 *
 * The virtual tag is what COMP1 sees while the field powers it: one frame of
 * +high/-low durations in µs (for a 125 kHz field; other field frequencies
 * scale them, as a real tag's clock would), repeated while the field is on:
 *   qom-set /machine/rfid tag "+256,-256,+512,..."
 * A "psk:" prefix marks a PSK tag, which only shows up when the reader drives
 * the PSK way; ASK and FSK tags show up either way.  "file:PATH" reads the
 * tag from a file (long FSK frames do not fit on a monitor command line).
 * "none" (or "") takes the tag away: the field then carries noise only, as
 * a real one does (rfid_segment; the "noise" property turns it off).
 * "field" reads back the reader's state: "125000 ask", "125000 psk", "off".
 *
 * Emulating (furi_hal_rfid_tim_emulate_dma_start): the Flipper is the tag.
 * TIM2 counts a reader's carrier on ETR (PA15, RFID_CARRIER), DMA loads ARR
 * and CCR3 every period, and CH3 in PWM mode switches the antenna load
 * (PA2, RFID_PULL).  A virtual 125 kHz reader ("reader", on by default)
 * provides that carrier and records the load, in carrier cycles, until the
 * emulation goes quiet; each capture is written to "tx-dir" (the launcher:
 * devices/NAME/rfid-tx/), and "tx" names the latest.
 *
 * Copyright (c) 2026, Kara Zajac
 * SPDX-License-Identifier: BSD-3-Clause
 */
#include "qemu/osdep.h"
#include "qemu/timer.h"
#include "qemu/cutils.h"
#include "qemu/error-report.h"
#include "qapi/error.h"
#include "hw/core/qdev-properties.h"
#include "wb55.h"
#include "flipper.h"

#define RFID_MAX_EDGES 8192
#define RFID_POWER_UP_NS (1 * SCALE_MS)     /* tag wakes up, then modulates */
#define RFID_PACE_MAX_NS (250 * SCALE_MS)

/* the tag answers this field (PSK tags only a PSK-mode read) */
static bool rfid_answers(FlipperRfidState *s)
{
    return s->field_hz && s->len && (!s->psk || s->psk_mode);
}

/* something reaches the comparator: the tag, or the field's noise */
static bool rfid_active(FlipperRfidState *s)
{
    return s->field_hz && (rfid_answers(s) || s->noise);
}

/*
 * The next segment, +high/-low µs.  Without a tag that answers, a real
 * reader's comparator still sees noise, and furi_hal_rfid's worker depends
 * on it: it only checks its 2 s ASK/PSK switch timeout after a buffer of
 * edges arrives, so a silent field would keep `rfid read` in ASK mode for
 * ever (and never find a PSK tag).  The noise is spikes of 17-40 µs, past
 * the worker's 16 µs filter, between gaps of 150-1500 µs: too irregular to
 * decode, and with pulse/period well under the 0.2 that the worker takes
 * for a card in the field.
 */
static int32_t rfid_segment(FlipperRfidState *s)
{
    uint32_t r;

    if (rfid_answers(s)) {
        if (s->pos >= s->len) {
            s->pos = 0;
        }
        return s->tag[s->pos++];
    }
    s->rng ^= s->rng << 13;             /* xorshift32 */
    s->rng ^= s->rng >> 17;
    s->rng ^= s->rng << 5;
    r = s->rng;
    s->noise_high = !s->noise_high;
    return s->noise_high ? (int32_t)(17 + r % 24) : -(int32_t)(150 + r % 1351);
}

static int64_t rfid_ns(FlipperRfidState *s, uint32_t us)
{
    return muldiv64(us, 1000 * 125000ull, s->field_hz);
}

/*
 * Deliver the next edge, if it is due.
 *
 * Each edge reaches COMP1 (and TIM2) stamped with its exact time on the
 * tag's own timeline, next_ns, so every captured duration is exact.  It is
 * delivered once virtual time has reached next_ns + shift_ns, and only after
 * the firmware has read the previous capture: the vCPU can lag behind
 * virtual time, and a second edge would overwrite the capture its ISR is
 * about to read.  Any such delay is added to shift_ns rather than caught up
 * in a burst: the reader then always has at least the real gap between two
 * edges to digest them (furi_hal_rfid's worker decodes in a thread, with
 * buffers for ~200 ms of edges), as it would on silicon.
 */
static void rfid_deliver(FlipperRfidState *s)
{
    int64_t now = qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL);
    int64_t due = s->next_ns + s->shift_ns;
    int32_t d;

    s->waiting = false;
    if (!rfid_active(s)) {
        return;
    }
    if (now < due) {
        timer_mod(s->timer, due);
        return;
    }
    if (s->tim && wb55_tim_capture_pending(s->tim) &&
        now < due + RFID_PACE_MAX_NS) {
        s->waiting = true;              /* rfid_cap_read() resumes */
        timer_mod(s->timer, due + RFID_PACE_MAX_NS);
        return;
    }
    s->shift_ns = now - s->next_ns;     /* late by now: trail a little more */
    d = rfid_segment(s);
    wb55_comp_input(s->comp, 0, d > 0, s->next_ns);
    s->next_ns += rfid_ns(s, d > 0 ? d : -d);
    timer_mod(s->timer, s->next_ns + s->shift_ns);
}

static void rfid_timer(void *opaque)
{
    rfid_deliver(opaque);
}

/* TIM2: the firmware has read its capture (inside the ISR's MMIO read) */
static void rfid_cap_read(Notifier *n, void *data)
{
    FlipperRfidState *s = container_of(n, FlipperRfidState, cap_read);

    if (s->waiting) {
        rfid_deliver(s);
    }
}

/* (re)start or stop the tag after a change of field or tag */
static void rfid_restart(FlipperRfidState *s)
{
    int64_t now = qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL);

    timer_del(s->timer);
    s->pos = 0;
    s->waiting = false;
    s->noise_high = false;
    wb55_comp_input(s->comp, 0, false, now);    /* unpowered: no signal */
    if (rfid_active(s)) {
        s->next_ns = now + RFID_POWER_UP_NS;
        s->shift_ns = 0;
        timer_mod(s->timer, s->next_ns);
    }
}

void flipper_rfid_set_drive(FlipperRfidState *s, uint32_t drive_hz)
{
    uint32_t field = 0;
    bool psk = false;

    if (drive_hz >= 100000 && drive_hz <= 150000) {
        field = drive_hz;
    } else if (drive_hz >= 50000 && drive_hz <= 75000) {
        field = 2 * drive_hz;           /* the tank rings at twice the drive */
        psk = true;
    }
    if (field == s->field_hz && psk == s->psk_mode) {
        return;
    }
    s->field_hz = field;
    s->psk_mode = psk;
    rfid_restart(s);
}

static char *rfid_get_tag(Object *obj, Error **errp)
{
    return g_strdup(FLIPPER_RFID(obj)->tag_str ?: "");
}

static void rfid_set_tag(Object *obj, const char *value, Error **errp)
{
    FlipperRfidState *s = FLIPPER_RFID(obj);
    g_autofree char *contents = NULL;
    const char *list = value;
    bool psk = false;
    g_auto(GStrv) parts = NULL;
    g_autofree int32_t *dur = g_new0(int32_t, RFID_MAX_EDGES);
    int64_t frame = 0;
    int n = 0;

    if (g_str_has_prefix(value, "file:")) {    /* past the monitor's line limit */
        g_autoptr(GError) err = NULL;

        if (!g_file_get_contents(value + 5, &contents, NULL, &err)) {
            error_setg(errp, "rfid tag: %s", err->message);
            return;
        }
        list = contents;
    }
    while (g_ascii_isspace(*list)) {
        list++;
    }
    if (!strcmp(list, "none") || !strcmp(list, "''") || !strcmp(list, "\"\"")) {
        list = "";                      /* HMP cannot pass an empty string */
    } else if (g_str_has_prefix(list, "psk:")) {
        psk = true;
        list += 4;
    } else if (g_str_has_prefix(list, "ask:")) {
        list += 4;
    }
    parts = g_strsplit_set(list, " ,;\t\n", -1);
    for (int i = 0; parts[i]; i++) {
        const char *p = parts[i];
        bool high = *p != '-';
        uint64_t v;

        if (!*p) {
            continue;
        }
        if (*p == '+' || *p == '-') {
            p++;
        }
        if (qemu_strtou64(p, NULL, 10, &v) < 0 || !v || v > 1000000 ||
            n == RFID_MAX_EDGES) {
            error_setg(errp, "rfid tag: expected up to %d durations in µs "
                       "(+high/-low, 1..1000000), got '%s'", RFID_MAX_EDGES,
                       parts[i]);
            return;
        }
        dur[n++] = high ? (int32_t)v : -(int32_t)v;
        frame += v;
    }
    if (n && frame < 1000) {
        error_setg(errp, "rfid tag: a frame shorter than 1 ms (%" PRId64
                   " µs) cannot be a tag", frame);
        return;
    }
    g_free(s->tag);
    g_free(s->tag_str);
    s->tag = n ? g_steal_pointer(&dur) : NULL;
    s->tag_str = n ? g_strdup(value) : NULL;
    s->len = n;
    s->psk = psk;
    rfid_restart(s);
}

/* ---------------------------------------- emulation: the virtual reader */

#define RFID_TX_IDLE_NS (100 * SCALE_MS)    /* a quiet field ends a capture */
#define RFID_TX_MAX_CYCLES (2 * FLIPPER_RFID_READER_HZ)     /* 2 s */

static void rfid_tx_finish(FlipperRfidState *s)
{
    g_autoptr(GString) out = g_string_new(NULL);
    g_autoptr(GError) err = NULL;
    g_autofree char *path = NULL;
    const int32_t *d = (const int32_t *)s->tx->data;
    guint n = s->tx->len;
    uint64_t cycles = 0;

    timer_del(s->tx_timer);
    for (guint i = 0; i < n; i++) {
        cycles += d[i] > 0 ? d[i] : -d[i];
    }
    if (n >= 8) {
        g_string_append_printf(out,
            "Filetype: FlipperEmu RFID capture\nVersion: 1\n"
            "# The load an emulated tag put on a 125 kHz reader's field (TIM2 "
            "CH3 on\n# RFID_PULL), +on/-off in carrier cycles. "
            "scripts/fe_rfid.py turns it\n# back into a tag: "
            "./flipperemu rfid FILE\nFrequency: %d", FLIPPER_RFID_READER_HZ);
        for (guint i = 0; i < n; i++) {
            if (i % 512 == 0) {
                g_string_append(out, "\nCycles:");
            }
            g_string_append_printf(out, " %+d", d[i]);
        }
        g_string_append_c(out, '\n');
        if (s->tx_dir) {
            g_mkdir_with_parents(s->tx_dir, 0755);
            for (int k = 1; k < 100000; k++) {
                g_free(path);
                path = g_strdup_printf("%s/tx-%05d.txt", s->tx_dir, k);
                if (!g_file_test(path, G_FILE_TEST_EXISTS)) {
                    break;
                }
            }
            if (g_file_set_contents(path, out->str, out->len, &err)) {
                info_report("flipper-zero: 125 kHz emulation, %u levels "
                            "(%.0f ms) -> %s", n,
                            cycles * 1000.0 / FLIPPER_RFID_READER_HZ, path);
                g_free(s->tx_last);
                s->tx_last = g_steal_pointer(&path);
            } else {
                warn_report("flipper-zero: %s", err->message);
            }
        }
    }
    g_array_free(s->tx, true);
    s->tx = NULL;
}

static void rfid_tx_timer(void *opaque)
{
    FlipperRfidState *s = opaque;
    int64_t now = qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL);

    if (!s->tx) {
        return;
    }
    if (now - s->tx_last_ns >= RFID_TX_IDLE_NS) {
        rfid_tx_finish(s);              /* the level held last is not kept */
    } else {
        timer_mod(s->tx_timer, s->tx_last_ns + RFID_TX_IDLE_NS);
    }
}

void flipper_rfid_load(FlipperRfidState *s, bool on, int64_t when)
{
    uint64_t c;

    if (!s->reader) {
        return;
    }
    if (!s->tx) {
        if (!on) {
            return;                     /* a capture starts with a load */
        }
        s->tx = g_array_new(false, false, sizeof(int32_t));
        s->tx_t0 = s->tx_last_ns = when;
        s->tx_cycles = 0;
        s->tx_level = true;
        timer_mod(s->tx_timer, when + RFID_TX_IDLE_NS);
        return;
    }
    if (on == s->tx_level) {
        return;
    }
    /* edges fall on carrier cycles: round to the nearest one */
    c = muldiv64(MAX(when - s->tx_t0, 0) + NANOSECONDS_PER_SECOND /
                 (2 * FLIPPER_RFID_READER_HZ), FLIPPER_RFID_READER_HZ,
                 NANOSECONDS_PER_SECOND);
    if (c > s->tx_cycles && s->tx_cycles < RFID_TX_MAX_CYCLES) {
        int32_t seg = c - s->tx_cycles;

        seg = s->tx_level ? seg : -seg;
        g_array_append_val(s->tx, seg);
    }
    s->tx_cycles = MAX(c, s->tx_cycles);
    s->tx_level = on;
    s->tx_last_ns = when;
}

static bool rfid_get_reader(Object *obj, Error **errp)
{
    return FLIPPER_RFID(obj)->reader;
}

static void rfid_set_reader(Object *obj, bool value, Error **errp)
{
    FlipperRfidState *s = FLIPPER_RFID(obj);

    s->reader = value;
    if (s->tim) {
        wb55_tim_set_ext_clock(s->tim, value ? FLIPPER_RFID_READER_HZ : 0);
    }
}

static char *rfid_get_tx(Object *obj, Error **errp)
{
    return g_strdup(FLIPPER_RFID(obj)->tx_last ?: "");
}

/* ------------------------------------------------------------ properties */

static bool rfid_get_noise(Object *obj, Error **errp)
{
    return FLIPPER_RFID(obj)->noise;
}

static void rfid_set_noise(Object *obj, bool value, Error **errp)
{
    FlipperRfidState *s = FLIPPER_RFID(obj);

    s->noise = value;
    rfid_restart(s);
}

static char *rfid_get_field(Object *obj, Error **errp)
{
    FlipperRfidState *s = FLIPPER_RFID(obj);

    if (!s->field_hz) {
        return g_strdup("off");
    }
    return g_strdup_printf("%u %s", s->field_hz, s->psk_mode ? "psk" : "ask");
}

static void rfid_init(Object *obj)
{
    FlipperRfidState *s = FLIPPER_RFID(obj);

    s->timer = timer_new_ns(QEMU_CLOCK_VIRTUAL, rfid_timer, s);
    s->tx_timer = timer_new_ns(QEMU_CLOCK_VIRTUAL, rfid_tx_timer, s);
    s->cap_read.notify = rfid_cap_read;
    s->reader = true;
    object_property_add_bool(obj, "reader", rfid_get_reader, rfid_set_reader);
    object_property_set_description(obj, "reader",
        "A virtual 125 kHz reader's field (clocks TIM2 when the Flipper "
        "emulates a tag, and records what it sends)");
    object_property_add_str(obj, "tx", rfid_get_tx, NULL);
    object_property_set_description(obj, "tx",
        "The latest emulation capture file");
    object_property_add_str(obj, "tag", rfid_get_tag, rfid_set_tag);
    object_property_set_description(obj, "tag",
        "The tag in the field: what COMP1 sees, one frame of +high/-low µs "
        "(\"psk:\" prefix: a PSK tag), or file:PATH; \"none\": no tag");
    s->noise = true;
    s->rng = 0x2545F491;
    object_property_add_bool(obj, "noise", rfid_get_noise, rfid_set_noise);
    object_property_set_description(obj, "noise",
        "Comparator noise while no tag answers the field (as on a real "
        "reader; the firmware's ASK/PSK switching needs it)");
    object_property_add_str(obj, "field", rfid_get_field, NULL);
    object_property_set_description(obj, "field",
        "The reader's field: \"<Hz> ask|psk\" or \"off\"");
}

static void rfid_realize(DeviceState *dev, Error **errp)
{
    FlipperRfidState *s = FLIPPER_RFID(dev);

    if (s->tim) {                       /* the reader's carrier on ETR */
        wb55_tim_set_ext_clock(s->tim, s->reader ? FLIPPER_RFID_READER_HZ : 0);
    }
}

static const Property rfid_props[] = {
    DEFINE_PROP_STRING("tx-dir", FlipperRfidState, tx_dir),
};

static void rfid_class_init(ObjectClass *klass, const void *data)
{
    DeviceClass *dc = DEVICE_CLASS(klass);

    dc->user_creatable = false;
    dc->realize = rfid_realize;
    device_class_set_props(dc, rfid_props);
}

static const TypeInfo rfid_types[] = {
    {
        .name = TYPE_FLIPPER_RFID,
        .parent = TYPE_DEVICE,
        .instance_size = sizeof(FlipperRfidState),
        .instance_init = rfid_init,
        .class_init = rfid_class_init,
    },
};
DEFINE_TYPES(rfid_types)
