/*
 * A radio link: a line protocol on a chardev (docs/RADIO_LINK.md), for the
 * radios that can have a real one, or any program, as their antenna: the
 * CC1101 (Sub-GHz) and the ST25R3916 (NFC).
 *
 * Lines are ASCII, one message each, "\n"-terminated.  What the emulator
 * sends is queued and written as the other end takes it, never blocking
 * the machine; what comes in is handed over a whole line at a time.  A
 * "hello ..." line from the other end, describing itself, is logged.
 *
 * Copyright (c) 2026, Kara Zajac
 * SPDX-License-Identifier: BSD-3-Clause
 */
#include "qemu/osdep.h"
#include "qemu/error-report.h"
#include "flipper.h"

#define LINK_LINE_MAX (1 << 20)         /* no line ends by then: not a link */
#define LINK_OUT_MAX  (1 << 20)         /* queued this much: it stopped reading */

static void flipper_link_flush(FlipperLink *l);

static gboolean flipper_link_writable(void *do_not_use, GIOCondition cond,
                                      void *opaque)
{
    FlipperLink *l = opaque;

    l->watch = 0;
    flipper_link_flush(l);
    return G_SOURCE_REMOVE;
}

static void flipper_link_flush(FlipperLink *l)
{
    while (l->out->len) {
        int w = qemu_chr_fe_write(l->chr, (const uint8_t *)l->out->str,
                                  l->out->len);

        if (w <= 0) {
            if (!qemu_chr_fe_backend_connected(l->chr) || !l->open) {
                g_string_truncate(l->out, 0);
            } else if (!l->watch) {
                l->watch = qemu_chr_fe_add_watch(l->chr, G_IO_OUT | G_IO_HUP,
                                                 flipper_link_writable, l);
            }
            return;
        }
        g_string_erase(l->out, 0, w);
    }
}

void flipper_link_send(FlipperLink *l, const char *fmt, ...)
{
    va_list ap;

    if (!l->open || l->out->len > LINK_OUT_MAX) {
        return;
    }
    va_start(ap, fmt);
    g_string_append_vprintf(l->out, fmt, ap);
    va_end(ap);
    flipper_link_flush(l);
}

static int flipper_link_can_read(void *opaque)
{
    FlipperLink *l = opaque;

    return l->held ? 0 : 65536;
}

/* The whole lines that have come, each to its device: as far as it can take
 * them (its `ready`), and the rest when it calls flipper_link_resume() */
static void flipper_link_lines(FlipperLink *l)
{
    char *nl;

    if (l->busy) {
        return;                         /* (a line's handling called back) */
    }
    l->busy = true;
    l->held = false;
    while ((nl = memchr(l->in->str, '\n', l->in->len))) {
        if (l->ready && !l->ready(l->opaque)) {
            l->held = true;             /* no more read until it says so */
            break;
        }
        *nl = 0;
        if (nl > l->in->str && nl[-1] == '\r') {
            nl[-1] = 0;
        }
        if (g_str_has_prefix(l->in->str, "hello ")) {
            info_report("flipper-zero: %s link: %s", l->name,
                        g_strstrip(l->in->str + 6));
        } else {
            l->on_line(l->opaque, l->in->str);
        }
        g_string_erase(l->in, 0, nl + 1 - l->in->str);
    }
    if (l->in->len > LINK_LINE_MAX) {
        g_string_truncate(l->in, 0);
    }
    l->busy = false;
}

static void flipper_link_read(void *opaque, const uint8_t *buf, int size)
{
    FlipperLink *l = opaque;

    g_string_append_len(l->in, (const char *)buf, size);
    flipper_link_lines(l);
}

void flipper_link_resume(FlipperLink *l)
{
    if (l->held && !l->busy) {
        flipper_link_lines(l);
        if (!l->held) {
            qemu_chr_fe_accept_input(l->chr);
        }
    }
}

static void flipper_link_event(void *opaque, QEMUChrEvent event)
{
    FlipperLink *l = opaque;

    switch (event) {
    case CHR_EVENT_OPENED:
        l->open = true;
        l->held = false;
        g_string_truncate(l->in, 0);
        g_string_truncate(l->out, 0);
        l->on_open(l->opaque, true);
        break;
    case CHR_EVENT_CLOSED:
        l->open = false;
        l->held = false;
        g_string_truncate(l->in, 0);
        g_string_truncate(l->out, 0);
        l->on_open(l->opaque, false);
        break;
    default:
        break;
    }
}

void flipper_link_init(FlipperLink *l, CharFrontend *chr, const char *name,
                       void (*on_line)(void *opaque, char *line),
                       void (*on_open)(void *opaque, bool open), void *opaque)
{
    l->chr = chr;
    l->name = name;
    l->on_line = on_line;
    l->on_open = on_open;
    l->opaque = opaque;
    l->in = g_string_new(NULL);
    l->out = g_string_new(NULL);
    if (qemu_chr_fe_backend_connected(chr)) {
        qemu_chr_fe_set_handlers(chr, flipper_link_can_read, flipper_link_read,
                                 flipper_link_event, NULL, l, NULL, true);
    }
}
