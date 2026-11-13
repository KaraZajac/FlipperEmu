/*
 * Flipper Zero piezo speaker, driven by TIM16 channel 1 PWM on PB8
 *
 * furi_hal_speaker_start() sets the tone with TIM16's period and the volume
 * with the PWM duty cycle (at most 50 %).  This plays that pulse wave, with
 * its DC level removed, through a QEMU audio backend, so loudness follows
 * the duty cycle as it does on the real piezo.  -M flipper-zero,audiodev=ID
 * selects the backend; without one the speaker stays silent.
 *
 * QOM, on /machine/speaker:
 *   mute  (bool, RW)  keep it quiet; the firmware notices nothing
 *
 * Copyright (c) 2026, Kara Zajac
 * SPDX-License-Identifier: BSD-3-Clause
 */
#include "qemu/osdep.h"
#include "qemu/audio.h"
#include "qemu/error-report.h"
#include "qapi/error.h"
#include "hw/core/qdev-properties.h"
#include "system/system.h"
#include "migration/vmstate.h"
#include "flipper.h"

#define RATE        48000
#define AMPLITUDE   20000           /* peak-to-peak, of 65535 */

static void speaker_update_wave(FlipperSpeakerState *s)
{
    double duty = s->duty_ppm / 1e6;

    s->phase_inc = (uint32_t)(((uint64_t)s->freq_mhz << 32) / (RATE * 1000ull));
    s->threshold = (uint32_t)(duty * 4294967295.0);
    s->hi = (int16_t)(AMPLITUDE * (1.0 - duty));
    s->lo = (int16_t)(-AMPLITUDE * duty);
}

static void speaker_callback(void *opaque, int free)
{
    FlipperSpeakerState *s = opaque;
    int16_t buf[256];

    while (free > 0) {
        int n = MIN(free / (int)sizeof(buf[0]), (int)ARRAY_SIZE(buf));
        size_t done;

        if (n <= 0) {
            break;
        }
        for (int i = 0; i < n; i++) {
            if (s->on && !s->mute) {
                buf[i] = s->phase < s->threshold ? s->hi : s->lo;
                s->phase += s->phase_inc;
            } else {
                buf[i] = 0;
            }
        }
        done = audio_be_write(s->audio_be, s->voice, buf, n * sizeof(buf[0]));
        if (!done) {
            break;
        }
        free -= done;
    }
}

void flipper_speaker_set(FlipperSpeakerState *s, bool on, uint32_t freq_mhz,
                         uint32_t duty_ppm)
{
    /* inaudible or out of the sample rate's reach: treat as silence */
    on = on && duty_ppm && freq_mhz >= 20 * 1000 && freq_mhz < RATE / 2 * 1000u;
    if (on == s->on && freq_mhz == s->freq_mhz && duty_ppm == s->duty_ppm) {
        return;
    }
    s->on = on;
    s->freq_mhz = freq_mhz;
    s->duty_ppm = duty_ppm;
    speaker_update_wave(s);
    if (s->voice) {
        audio_be_set_active_out(s->audio_be, s->voice, on && !s->mute);
    }
}

static bool speaker_get_mute(Object *obj, Error **errp)
{
    return FLIPPER_SPEAKER(obj)->mute;
}

static void speaker_set_mute(Object *obj, bool v, Error **errp)
{
    FlipperSpeakerState *s = FLIPPER_SPEAKER(obj);

    s->mute = v;
    if (s->voice) {
        audio_be_set_active_out(s->audio_be, s->voice, s->on && !v);
    }
}

static void speaker_init(Object *obj)
{
    object_property_add_bool(obj, "mute", speaker_get_mute, speaker_set_mute);
    object_property_set_description(obj, "mute",
        "Keep the speaker quiet (the firmware notices nothing)");
}

/* QEMU never finalizes machine devices, so close the voice ourselves: that
 * releases the backend's hardware voice, and the wav driver then writes the
 * final RIFF sizes. */
static void speaker_exit(Notifier *n, void *data)
{
    FlipperSpeakerState *s = container_of(n, FlipperSpeakerState, exit);

    if (s->voice) {
        audio_be_close_out(s->audio_be, s->voice);
        s->voice = NULL;
    }
}

static void speaker_realize(DeviceState *dev, Error **errp)
{
    FlipperSpeakerState *s = FLIPPER_SPEAKER(dev);
    struct audsettings as = { RATE, 1, AUDIO_FORMAT_S16, 0 };

    if (!s->audio_be || !audio_be_check(&s->audio_be, errp)) {
        return;                     /* no audiodev: a silent speaker */
    }
    s->voice = audio_be_open_out(s->audio_be, s->voice, "flipper-speaker", s,
                                 speaker_callback, &as);
    if (!s->voice) {
        warn_report("flipper-speaker: could not open an audio voice");
        return;
    }
    s->exit.notify = speaker_exit;
    qemu_add_exit_notifier(&s->exit);
}

/*
 * A tone that sounded at the save sounds in the loaded machine: the timer
 * that makes it tells nothing in a load.  (A voice does not play while the
 * machine stands still, so none is heard before the machine runs.)
 */
static int speaker_post_load(void *opaque, int version_id)
{
    FlipperSpeakerState *s = opaque;

    s->phase = 0;
    speaker_update_wave(s);
    if (s->voice) {
        audio_be_set_active_out(s->audio_be, s->voice, s->on && !s->mute);
    }
    return 0;
}

static const VMStateDescription vmstate_flipper_speaker = {
    .name = "flipper-speaker",
    .version_id = 1,
    .minimum_version_id = 1,
    .post_load = speaker_post_load,
    .fields = (const VMStateField[]) {
        VMSTATE_BOOL(on, FlipperSpeakerState),
        VMSTATE_UINT32(freq_mhz, FlipperSpeakerState),
        VMSTATE_UINT32(duty_ppm, FlipperSpeakerState),
        VMSTATE_END_OF_LIST()
    }
};

static const Property speaker_props[] = {
    DEFINE_AUDIO_PROPERTIES(FlipperSpeakerState, audio_be),
};

static void speaker_class_init(ObjectClass *klass, const void *data)
{
    DeviceClass *dc = DEVICE_CLASS(klass);

    dc->realize = speaker_realize;
    dc->user_creatable = false;
    device_class_set_props(dc, speaker_props);
    dc->vmsd = &vmstate_flipper_speaker;
}

static const TypeInfo speaker_types[] = {
    {
        .name = TYPE_FLIPPER_SPEAKER,
        .parent = TYPE_DEVICE,
        .instance_size = sizeof(FlipperSpeakerState),
        .instance_init = speaker_init,
        .class_init = speaker_class_init,
    },
};
DEFINE_TYPES(speaker_types)
