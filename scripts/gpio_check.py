#!/usr/bin/env python3
# Copyright (c) 2026, Kara Zajac
# SPDX-License-Identifier: BSD-3-Clause
"""Check the GPIO header: the launcher's `gpio`, with the firmware's own
`gpio` CLI command on the other side of the pins.

    scripts/gpio_check.py firmware/official-1.5.1/flipper-z-f7-update-1.5.1.tgz

Starts a throwaway device.  The header after a start: its thirteen signal
pins, none an output, none held, with its serial ports and its I2C bus
beneath them.  Then the firmware makes PA7 an output and
sets it (`gpio mode`, `gpio set` in its CLI): the launcher sees that, as a
table, as one word, by the pin's three names, in `status`, and a `--wait` for
a level returns when the firmware sets it (and with exit status 1 when it
does not).  Then the other way: PA6 an input, held high and low from outside
and let go of, and the firmware's `gpio read` says so.  A pin held while the
firmware drives it keeps the firmware's level, and has what is held when it
is an input again.  What is held stays so over a reset of the firmware.  Then
the record (`gpio record FILE.vcd`): what the firmware does to PA7 and what
is held to PA6 are in the file in their order, each with its time, also over
a reset; `gpio watch` prints changes as they happen, and one that a script
ends with `kill` leaves no record behind; a record of pin 17 while the
firmware reads a 1-Wire key has both sides of the line.  Then what is
refused: the pins with their own use, power pins, pins that are not there,
levels that are none.

Then a voltage (`gpio PA7 1.25V`), for the firmware's ADC, which has six of
the pins as inputs.  A script in the firmware's JavaScript measures each of
them twice in a row (readAnalog(), in mV up to its reference's 2.048 V) and
reads what is held; the launcher says what the ADC converted, and that it is
on while the script has it.  A voltage is a level too, high from 1.65 V up.
(Skipped on a firmware whose JavaScript cannot do that.)

Then PWM.  The firmware has two outputs for one, PA7 (TIM1) and PA4 (LPTIM2),
and a script starts both (pwmWrite()) and gives them other numbers when the
check holds PA6.  Such a pin is `pwm` in the table, with its timer, its
frequency and the part of the time for which it is high; `--wait pwm` waits
for one; `status --json` has the timer's numbers, which the check works out
from the firmware's arithmetic beforehand, with and without a prescaler and
with LPTIM2 on its 32768 Hz clock.  A PWM that is high for none or for all of
the time is a low or a high pin, and a record has no edges of a PWM.  (Skipped
on a firmware whose JavaScript makes none.)

Then what the firmware's `gpio` command cannot do, with three apps of the
check's own, scripts/gpio_check_app, scripts/adc_check_app and
scripts/pwm_check_app (they need
`ufbt` on the PATH and its SDK to be this firmware's, which is the official
release's as ufbt comes; skipped otherwise).  The ADC with the firmware's own
turning of a result into volts and degrees: the chip's reference, its
temperature sensor and the battery pin, and the pins, with both of the ADC's
reference voltages; a pin that the firmware itself drives or pulls measures
as that.  The PWMs that the firmware's two calls never make: the other
polarity of both outputs, TIM1's second PWM mode, a prescaler in LPTIM2, PA7
as the timer's open-drain output (held low from outside it is low), a timer
that runs while its pin is something else, and a pin that is a timer's with
no PWM from it.  And interrupts and an open-drain output.  The table says which edges
of a pin the firmware has an interrupt for.  A pin held low and high from
outside gives the app its interrupt on the edge it asked for, each once, and
none on the other; an interrupt that the app turns off gives none, and gives
them again when it is on.  PA7, the open-drain output, is low while the app
pulls it low, also with 3V3 held to it, and high by its pull-up when the app
lets go; held low from outside then, the firmware reads it as low.  A record
has all of that.

At the end the emulator is stopped while it records, and the file has what
was recorded.  Exit status 0 = pass.
"""
import argparse
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import fe_header  # noqa: E402
from files_check import ROOT, Bench, CRASH, check, results  # noqa: E402
from ufbt_check import ANSI, log_tail, logged, tool  # noqa: E402

SIGNALS = "PA7 PA6 PA4 PB3 PB2 PC3 PA14 PA13 PB6 PB7 PC1 PC0 PB14".split()
APP = ROOT / "scripts" / "gpio_check_app"
ADC_APP = ROOT / "scripts" / "adc_check_app"
PWM_APP = ROOT / "scripts" / "pwm_check_app"
ADC_PINS = {"PA7": 12, "PA6": 11, "PA4": 9, "PC3": 4, "PC1": 2, "PC0": 1}
ADC_BLOCK = ("its ADC, with pins 2, 3, 4, 7, 15 and 16 as inputs (`gpio PIN "
             "1.25V` holds a voltage to one)\n  ADC1  ")
# every pin that the ADC has as an input, measured twice in a row; then the
# ADC is kept for a while, for the launcher to say that it is on
ADC_JS = """\
let eventLoop = require("event_loop");
let gpio = require("gpio");
let names = ["pa7", "pa6", "pa4", "pc3", "pc1", "pc0"];
for (let i = 0; i < names.length; i++) {
    let pin = gpio.get(names[i]);
    pin.init({ direction: "in", inMode: "analog" });
    print(names[i], pin.readAnalog(), pin.readAnalog());
}
delay(6000);
print("end");
"""
# a PWM on each of the two pins that the firmware has one for, then other
# numbers for them three times, then none.  Each step when what is held to
# PA6 changes
PWM_JS = """\
let eventLoop = require("event_loop");
let gpio = require("gpio");
let a7 = gpio.get("pa7");
let a4 = gpio.get("pa4");
let a6 = gpio.get("pa6");
a6.init({ direction: "in", inMode: "plain_digital", pull: "down" });
function until(level) {
    while (a6.read() !== level) {
        delay(50);
    }
}
print("supported", a7.isPwmSupported(), a4.isPwmSupported(), a6.isPwmSupported());
print("running", a7.isPwmRunning(), a4.isPwmRunning());
a7.pwmWrite(1000, 50);
a4.pwmWrite(10000, 30);
print("running", a7.isPwmRunning(), a4.isPwmRunning());
until(true);
a7.pwmWrite(440, 25);
a4.pwmWrite(5, 50);
until(false);
a7.pwmWrite(100, 10);
a4.pwmWrite(100, 10);
until(true);
a7.pwmWrite(1000, 0);
a4.pwmWrite(1000, 100);
until(false);
a7.pwmStop();
a4.pwmStop();
print("running", a7.isPwmRunning(), a4.isPwmRunning());
print("end");
"""
skipped = []


def skip(label, why):
    skipped.append(label)
    print(f"  SKIP  {label}\n        {why}", flush=True)


def fe(b, *args, timeout=600, cwd=None):
    """A launcher command: (exit status, what it printed, what it said on
    stderr, the seconds it took)"""
    t0 = time.time()
    r = subprocess.run([sys.executable, str(ROOT / "flipperemu"), "-d", b.name,
                        *map(str, args)], capture_output=True, timeout=timeout,
                       encoding="utf-8", errors="replace", cwd=cwd,
                       stdin=subprocess.DEVNULL)
    return r.returncode, r.stdout, r.stderr, time.time() - t0


def row(table, pin):
    """A pin's line of the table, its blanks made one"""
    for line in table.splitlines():
        words = line.split()
        if len(words) > 1 and words[1] == pin:
            return " ".join(words)
    return ""


def firmware(b, *commands):
    """The firmware's own CLI: what it answered"""
    code, out, err, took = fe(b, "cli", *commands)
    return out + err


def status(b):
    code, out, err, took = fe(b, "status", "--json")
    try:
        return {p["pin"]: p for p in json.loads(out)["gpio"]}
    except (ValueError, KeyError, TypeError):
        return {}


def adc(b):
    """The ADC as `status --json` has it"""
    code, out, err, took = fe(b, "status", "--json")
    try:
        return json.loads(out).get("adc") or {}
    except (ValueError, AttributeError):
        return {}


def start_checks(b):
    code, out, err, took = fe(b, "gpio")
    rows = [row(out, p) for p in SIGNALS]
    check("the header after a start: its thirteen signal pins, in their "
          "order on it", code == 0 and all(rows)
          and [r.split()[0] for r in rows]
          == "2 3 4 5 6 7 10 12 13 14 15 16 17".split()
          and "1 is +5V" in out, out + err)
    check("... none an output, none held; the free ones float",
          not re.search(r"\boutput\b|\bheld\b", out)
          and all(row(out, p).endswith("floating")
                  for p in "PA7 PA6 PA4 PB3 PB2 PC3 PC1 PC0".split()), out)
    check("... and the pins with their own use say so: the log's UART (its RX "
          "an input pulled up, high, with the interrupt on its fall that "
          "waits for a module's call), the iButton pin",
          "USART1's TX" in row(out, "PB6")
          and re.match(r"14 PB7 input, pull-up, interrupt when it falls high "
                       r"USART1's RX", row(out, "PB7"))
          and "iButton" in row(out, "PB14")
          and len(re.findall("interrupt", out.split("its serial ports")[0]))
          == 1, out)
    check("... beneath them the two serial ports as the firmware has them: "
          "the log's, which sends and waits for a call, and one that is off",
          re.search(r"\n  pins 13 and 14 \(USART1\) +\d+ baud, 8N1; its "
                    r"receiver is off, and the firmware waits for a call on "
                    r"pin 14 \(an interrupt on its fall, as for an expansion "
                    r"module\)\n"
                    r"  pins 15 and 16 \(LPUART1\) +off \(nothing has opened "
                    r"it\)\n", out), out)
    check("... and the I2C bus of pins 15 and 16, with no device on it and "
          "no address called",
          "\nits I2C bus, SDA on pin 15 and SCL on pin 16 (`run "
          "--i2c TYPE@ADDRESS` puts a device there)\n"
          "  pins 15 and 16 (I2C3)  no device; the firmware has "
          "called no address yet\n" in out, out)
    first = adc(b)
    check("... and at the end the ADC, with the six pins that are its "
          "inputs: off, as nothing uses it, and it has converted nothing; "
          "status --json has the same (off, its reference is the chip's "
          "3.3 V then)",
          out.endswith(ADC_BLOCK + "off (nothing uses it now); the firmware "
                       "has converted nothing yet\n")
          and first == {"name": "ADC1", "on": False, "reference": 3300,
                        "conversions": 0, "last": None}, out + repr(first))
    code, out, err, took = fe(b, "gpio", "PA7")
    check("a pin alone: its level as one word, and nothing else",
          code == 0 and out == "floating\n" and not err, out + err)
    st = status(b)
    check("status --json: the thirteen pins, with what they are",
          list(st) == SIGNALS and st["PA7"].get("label") == "A7"
          and st["PA7"].get("number") == 2 and st["PA7"].get("held") is None
          and st["PA7"].get("holdable") is True
          and st["PB14"].get("holdable") is False
          and st["PB6"].get("mode") == "alternate"
          and st["PB6"].get("function") == 7 and st["PB7"].get("pull") == "up"
          and st["PB7"].get("interrupt") == "falling"
          and st["PA7"].get("interrupt") is None
          and {p: st[p].get("adc", 0) for p in SIGNALS}
          == {p: ADC_PINS.get(p) for p in SIGNALS}
          and st["PA7"].get("held_mv", 0) is None, st)
    code, out, err, took = fe(b, "status")
    check("status: one line for the header, with nothing on it",
          re.search(r"^gpio\s*: none of its pins is an output, none is held$",
                    out, re.M), out + err)


def output_checks(b):
    said = firmware(b, "gpio mode PA7 1")
    code, out, err, took = fe(b, "gpio")
    check("the firmware makes PA7 an output (`gpio mode PA7 1` in its CLI): "
          "the table has it as one, low",
          "output" in said and row(out, "PA7") == "2 PA7 output low",
          said + out + err)
    said = firmware(b, "gpio set PA7 1")
    words = [fe(b, "gpio", name)[1].strip() for name in ("PA7", "a7", "2")]
    check("... and sets it (`gpio set PA7 1`): high, by the pin's name, by "
          "what the case says (A7) and by its number on the header (2)",
          "=> 1" in said and words == ["high"] * 3, said + repr(words))
    code, out, err, took = fe(b, "status")
    check("status: PA7 is an output, high",
          re.search(r"^gpio\s*: PA7 is an output, high$", out, re.M), out + err)
    code, out, err, took = fe(b, "gpio", "PA7", "--wait", "high",
                              "--timeout", 5)
    check("--wait for the level it has: back at once, exit 0",
          code == 0 and "PA7 is high" in out and took < 3, out + err)
    code, out, err, took = fe(b, "gpio", "PA7", "--wait", "low",
                              "--timeout", 2)
    check("--wait for a level that does not come: exit 1 after the timeout, "
          "with what the pin is",
          code == 1 and "PA7 is high, not low after 2 s" in err
          and "output" in err and 1.5 < took < 10, out + err)
    waiting = b.bg("gpio", "PA7", "--wait", "low", "--timeout", 60)
    time.sleep(1.0)
    still = waiting.poll() is None
    said = firmware(b, "gpio set PA7 0")
    try:
        out = waiting.communicate(timeout=30)[0]
    except subprocess.TimeoutExpired:
        waiting.kill()
        out = "(still waiting)" + waiting.communicate()[0]
    check("--wait for a level the firmware sets a second later: it waits, "
          "the CLI is free meanwhile, and it is back with exit 0",
          still and "=> 0" in said and waiting.returncode == 0
          and "PA7 is low" in out, said + out)


def input_checks(b):
    said = firmware(b, "gpio mode PA6 0")
    code, out, err, took = fe(b, "gpio")
    check("the firmware makes PA6 an input: the table has it as one, "
          "floating", "input" in said
          and row(out, "PA6") == "3 PA6 input floating", said + out + err)
    code, out, err, took = fe(b, "gpio", "PA6", "high")
    said = firmware(b, "gpio read PA6")
    table = fe(b, "gpio")[1]
    check("PA6 held high from outside: the firmware's `gpio read PA6` reads "
          "1, and the table says who holds it",
          code == 0 and "held high from outside" in out and "<= 1" in said
          and row(table, "PA6") == "3 PA6 input high held high from outside",
          out + err + said + table)
    code, out, err, took = fe(b, "gpio", "A6", "low")
    said = firmware(b, "gpio read PA6")
    check("... held low (as A6): it reads 0",
          code == 0 and "held low" in out and "<= 0" in said
          and fe(b, "gpio", "PA6")[1] == "low\n", out + err + said)
    code, out, err, took = fe(b, "status")
    check("status: PA6 is held low",
          re.search(r"^gpio\s*: .*PA6 is held low$", out, re.M), out + err)
    code, out, err, took = fe(b, "gpio", "3", "free")
    check("... let go of (as 3): it floats again, and nothing holds it",
          code == 0 and "let go of" in out
          and fe(b, "gpio", "PA6")[1] == "floating\n"
          and status(b).get("PA6", {}).get("held", 0) is None, out + err)
    code, out, err, took = fe(b, "gpio", "PA6", "1")
    check("1 and 0 for high and low",
          code == 0 and fe(b, "gpio", "PA6")[1] == "high\n"
          and fe(b, "gpio", "PA6", "0")[0] == 0
          and fe(b, "gpio", "PA6")[1] == "low\n", out + err)


def fight_checks(b):
    """PA7 is an output and low, PA6 an input held low"""
    code, out, err, took = fe(b, "gpio", "PA7", "high")
    table = fe(b, "gpio")[1]
    check("a pin held high while the firmware drives it low: it keeps the "
          "firmware's level, and both the answer and the table say why",
          code == 0 and "The firmware drives it" in out
          and fe(b, "gpio", "PA7")[1] == "low\n"
          and "does not show while the firmware drives" in row(table, "PA7"),
          out + err + table)
    said = firmware(b, "gpio mode PA7 0", "gpio read PA7")
    check("... and when the firmware makes it an input, it has what is "
          "held: `gpio read PA7` reads 1",
          "<= 1" in said and fe(b, "gpio", "PA7")[1] == "high\n", said)

    code, out, err, took = fe(b, "reset")
    code2, out2, err2, took = fe(b, "wait", "--timeout", b.timeout)
    st = status(b)
    check("a reset of the firmware: its pins are as after a start again, "
          "and what was held is still held",
          code == 0 and code2 == 0 and st.get("PA7", {}).get("mode") == "analog"
          and st["PA7"].get("held") == "high" and st["PA6"].get("held") == "low",
          out + err + out2 + err2 + repr(st))
    said = firmware(b, "gpio mode PA7 0", "gpio mode PA6 0", "gpio read PA7",
                    "gpio read PA6")
    check("... the firmware, started again, reads them: PA7 1, PA6 0",
          "PA7 <= 1" in said and "PA6 <= 0" in said, said)
    code, out, err, took = fe(b, "gpio", "free")
    st = status(b)
    check("`gpio free`: lets go of all, and says which they were",
          code == 0 and "PA7 high" in out and "PA6 low" in out
          and not any(p.get("held") for p in st.values()) and st
          and st["PA7"]["level"] == "floating", out + err + repr(st))
    code, out, err, took = fe(b, "gpio", "free")
    check("... again, with nothing held: says so, exit 0",
          code == 0 and "nothing is held" in out and "it was" not in out,
          out + err)


def recorded(path):
    """A record: (its changes, what was wrong with the file)"""
    try:
        record, changes = fe_header.read_record(path)
    except (OSError, ValueError) as e:
        return [], f"{type(e).__name__}: {e}"
    return changes, ""


def of(changes, pin):
    return [level for ns, p, level, before in changes if p == pin]


def record_checks(b):
    """Nothing is held; the firmware has PA7 and PA6 as inputs"""
    vcd, other = b.dir / "pins.vcd", b.dir / "other.vcd"
    code, out, err, took = fe(b, "gpio", "record")
    code2, out2, err2, took = fe(b, "gpio", "record", "stop")
    check("`gpio record` with no record running: says so, exit 0; `gpio "
          "record stop`: exit 1 and one line",
          code == 0 and "not being recorded" in out and code2 == 1
          and not out2 and len(err2.strip().splitlines()) == 1
          and "nothing is being recorded" in err2, out + err + out2 + err2)

    code, out, err, took = fe(b, "gpio", "record", "pins.vcd", cwd=b.dir)
    code2, out2, err2, took = fe(b, "gpio", "record")
    head = vcd.read_text() if vcd.exists() else ""
    check("`gpio record pins.vcd`: a VCD file where the command was given, "
          "with the thirteen pins and the levels they begin with; `gpio "
          "record` says that it runs",
          code == 0 and code2 == 0 and str(vcd) in out2
          and "no change" in out2 and "$timescale 1 ns $end" in head
          and all(f" {p} $end\n" in head for p in SIGNALS)
          and head.count("$var wire 1 ") == 13 and "#0\n$dumpvars\n" in head
          and head.rstrip().endswith("$end") and not recorded(vcd)[1],
          out + err + out2 + err2 + head + recorded(vcd)[1])

    said = firmware(b, "gpio mode PA7 1", "gpio set PA7 1", "gpio set PA7 0",
                    "gpio set PA7 1")
    fe(b, "gpio", "PA6", "high")
    fe(b, "gpio", "PA6", "free")
    code, out, err, took = fe(b, "gpio", "record", other)
    check("a second record while one runs: exit 1, one line with the file "
          "of the first, and no second file",
          code == 1 and not out and len(err.strip().splitlines()) == 1
          and str(vcd) in err and "already" in err and not other.exists(),
          out + err)
    time.sleep(0.3)
    growing, wrong = recorded(vcd)      # what reached the file as it runs
    code, out, err, took = fe(b, "gpio", "record", "stop")
    changes, wrong2 = recorded(vcd)
    times = [c[0] for c in changes]
    check("what the firmware did to PA7 is in it in its order (an output: "
          "low; then set 1, 0, 1), and was there while the record ran",
          not wrong and not wrong2 and of(changes, "PA7")
          == ["low", "high", "low", "high"] and of(growing, "PA7")
          == of(changes, "PA7"), said + wrong + wrong2 + repr(changes))
    check("... what was held to PA6 too (high, let go of: floating); every "
          "change has its own time, later than the one before",
          of(changes, "PA6") == ["high", "floating"]
          and all(a < z for a, z in zip(times, times[1:]))
          and 0 < times[-1] < 120e9, repr(changes))
    text = vcd.read_text() if vcd.exists() else ""
    last = text.rstrip().splitlines()[-1] if text.strip() else ""
    check("`gpio record stop`: says the file and how many changes are in "
          f"it ({len(changes)}); the file ends with the time it ended at",
          code == 0 and str(vcd) in out and changes
          and f"{len(changes)} changes of a pin's level" in out
          and re.fullmatch(r"#\d+", last) and int(last[1:]) > times[-1],
          out + err + last)
    size = vcd.stat().st_size if vcd.exists() else 0
    firmware(b, "gpio set PA7 0")
    time.sleep(0.3)
    code, out, err, took = fe(b, "gpio", "record")
    check("... and nothing is written to it after that",
          size and vcd.stat().st_size == size and "not being recorded" in out,
          out + err)

    # as it happens: PA7 is an output and low
    watch = b.bg("gpio", "watch", "A7", "--for", 6)
    time.sleep(2.0)
    said = firmware(b, "gpio set PA7 1", "gpio set PA7 0")
    try:
        out = watch.communicate(timeout=60)[0]
    except subprocess.TimeoutExpired:
        watch.kill()
        out = "(still watching)" + watch.communicate()[0]
    lines = [" ".join(line.split()[1:]) for line in out.splitlines()
             if re.match(r"\s*\d+\.\d{6}\s", line)]
    code, out2, err2, took = fe(b, "gpio", "record")
    check("`gpio watch A7 --for 6`: the two changes the firmware makes "
          "meanwhile, a line each, the second with how long PA7 was high; "
          "it ends by itself and leaves no record running, and no file",
          watch.returncode == 0 and len(lines) == 2
          and lines[0] == "PA7 high" and lines[1].startswith("PA7 low (high "
                                                             "for ")
          and "2 changes of a pin's level" in out
          and "not being recorded" in out2
          and not (b.dir / "gpio-watch.vcd").exists(), out + said + out2)
    # a script that has a watch in its background ends it with `kill`
    watch = b.bg("gpio", "watch", "A7")
    first = watch.stdout.readline()             # it watches now
    said = firmware(b, "gpio set PA7 1", "gpio set PA7 0")
    time.sleep(1.0)
    watch.terminate()
    try:
        out = watch.communicate(timeout=60)[0]
    except subprocess.TimeoutExpired:
        watch.kill()
        out = "(still watching)" + watch.communicate()[0]
    lines = [" ".join(line.split()[1:]) for line in out.splitlines()
             if re.match(r"\s*\d+\.\d{6}\s", line)]
    code, out2, err2, took = fe(b, "gpio", "record")
    check("`gpio watch A7` that is ended with `kill`, as a script ends what "
          "it began: the two changes and their count, exit 0, no record "
          "left running and no file",
          watch.returncode == 0 and "Ctrl+C ends it" in first
          and [x.split(" (")[0] for x in lines] == ["PA7 high", "PA7 low"]
          and "2 changes of a pin's level" in out
          and "not being recorded" in out2
          and not (b.dir / "gpio-watch.vcd").exists(),
          f"{watch.returncode}\n{first}{out}{said}{out2}")

    # pin 17, the iButton pin, has two sides: the firmware's, and a key's
    ow = b.dir / "ow.vcd"
    fe(b, "ibutton", "ds1990", "01", "A1", "B2", "C3", "D4", "E5", "F6")
    fe(b, "gpio", "record", ow)
    said = firmware(b, "ikey read")
    fe(b, "gpio", "record", "stop")
    fe(b, "ibutton", "none")
    changes, wrong = recorded(ow)
    line = [(ns, level) for ns, pin, level, before in changes if pin == "PB14"]
    lows = [(a, z - a) for (a, level), (z, after) in zip(line, line[1:])
            if level == "low"]
    us = [round(length / 1000) for at, length in lows]
    check("a record while the firmware reads a 1-Wire key (`ikey read`): "
          "pin 17 has the firmware's reset pulse (480 us; here "
          f"{us[0] if us else '-'}), right after it the key's own answer, "
          f"its presence pulse ({us[1] if us[1:] else '-'} us), then the "
          "slots of the command and of the ROM's 64 bits; the line is high "
          "at the end",
          "Dallas 01A1B2C3D4E5F68F" in said and not wrong and len(lows) >= 74
          and 400 <= us[0] <= 2000 and 30 <= us[1] <= 1100
          and lows[1][0] - (lows[0][0] + lows[0][1]) < 200000
          and line[-1][1] == "high", said + wrong + repr(us[:90]))

    # over a reset: the record goes on, and has what the reset did to a pin
    fe(b, "gpio", "record", vcd)
    code, out, err, took = fe(b, "reset")
    code2, out2, err2, took = fe(b, "wait", "--timeout", b.timeout)
    code3, out3, err3, took = fe(b, "gpio", "record", "stop")
    changes, wrong = recorded(vcd)
    check("a record over a reset of the firmware: it goes on, and PA7, an "
          "output before, is in it as let go of (floating)",
          code == 0 and code2 == 0 and code3 == 0 and not wrong
          and of(changes, "PA7")[-1:] == ["floating"],
          out + err + out2 + err2 + out3 + err3 + wrong + repr(changes))


def refused_checks(b):
    def refused(label, words, *args):
        code, out, err, took = fe(b, "gpio", *args)
        check(label + ": exit 1, one line that says why",
              code == 1 and not out and len(err.strip().splitlines()) == 1
              and all(w in err for w in words), out + err)

    refused("the iButton pin (1W) is not held from here", ["PB14", "iButton"],
            "1W", "high")
    refused("nor are USART1's pins, the log's (TX, RX)",
            ["PB6", "USART1's TX", "uart --port usart"], "TX", "low")
    refused("... RX", ["PB7", "USART1's RX", "uart send --port usart"],
            "PB7", "low")
    refused("a pin that is not on the header (PD3), with those that are",
            ["'PD3'", "PA7", "PC0"], "PD3")
    refused("a power pin (9, 3V3)", ["3V3"], "9", "high")
    refused("a level that is none (up)", ["'up'", "high or low"], "PA6", "up")
    refused("--wait and a level to hold together", ["--wait"], "PA6", "high",
            "--wait", "high")
    refused("--wait without a pin", ["--wait"], "--wait", "high")
    refused("a record to a file that cannot be written",
            ["cannot write", "/nowhere/pins.vcd"], "record", "/nowhere/pins.vcd")
    refused("--for without watch", ["--for", "watch"], "PA6", "--for", "3")
    refused("watch of a pin that is not there", ["'PD3'", "PA7"],
            "watch", "PD3")
    code, out, err, took = fe(b, "gpio", "record")
    check("... and none of those left a record running",
          code == 0 and "not being recorded" in out, out + err)
    st = status(b)
    check("... and none of that held anything",
          st and not any(p.get("held") for p in st.values()), st)
    code, out, err, took = fe(b, "gpio", "PB7")
    check("a pin that is not for holding can still be read (RX: high)",
          code == 0 and out == "high\n", out + err)


def reads(mv, reference):
    """What an ideal 12-bit ADC makes of a voltage, both in mV"""
    return min(4095, mv * 4096 // reference)


def adc_checks(b):
    """Nothing is held, and the header's pins are as after a start"""
    held = (("PA7", "1.25V", 1250, "low"), ("PA6", "high", 3300, "high"),
            ("PA4", "low", 0, "low"), ("PC3", "2047mV", 2047, "high"),
            ("PC1", "3.3V", 3300, "high"), ("PC0", ".1v", 100, "low"))
    said = [fe(b, "gpio", pin, word) for pin, word, mv, level in held]
    table = fe(b, "gpio")[1]
    check("a voltage held to a pin (`gpio PA7 1.25V`, 2047mV, .1v): the "
          "answer and the table have it, and what it is as a level: high "
          "from 1.65 V up, half of what the chip runs on.  3.3 V is a wire "
          "to 3V3, as `high` is",
          all(code == 0 for code, out, err, took in said)
          and "PA7 (pin 2 of the header): held at 1.25 V from outside (low "
              "as a level); it is low" in said[0][1]
          and row(table, "PA7") == "2 PA7 analog low held at 1.25 V from "
                                   "outside (low as a level)"
          and row(table, "PA6") == "3 PA6 analog high held high from outside"
          and row(table, "PC3") == "7 PC3 analog high held at 2.047 V from "
                                   "outside (high as a level)"
          and row(table, "PC1") == "15 PC1 analog high held high from outside"
          and row(table, "PC0") == "16 PC0 analog low held at 0.1 V from "
                                   "outside (low as a level)",
          "".join(out + err for code, out, err, took in said) + table)
    st = status(b)
    check("status --json: \"held_mv\" beside \"held\"",
          [(st.get(pin, {}).get("held_mv"), st.get(pin, {}).get("held"))
           for pin, word, mv, level in held]
          == [(mv, level) for pin, word, mv, level in held], st)

    js = b.dir / "fe_adc.js"
    js.write_text(ADC_JS)
    code, out, err, took = fe(b, "push", js, "/ext/fe_adc.js")
    before = adc(b)
    script = b.bg("cli", "--timeout", 60, "js /ext/fe_adc.js")
    end, now = time.time() + 30, {}
    while time.time() < end and script.poll() is None:
        now = adc(b)
        if now.get("on") and now.get("conversions", 0) \
                >= before.get("conversions", 0) + 12:
            break
        time.sleep(0.3)
    block = fe(b, "gpio")[1].partition(ADC_BLOCK)[2]
    try:
        text = script.communicate(timeout=90)[0]
    except subprocess.TimeoutExpired:
        script.kill()
        text = script.communicate()[0]
    lines = [line.strip() for line in text.splitlines()]
    if "Script done!" not in text or "pa7" not in text:
        skip("a script in the firmware's JavaScript measures the held "
             "voltages (readAnalog)",
             "this firmware's JavaScript did not run it: "
             + " ".join(text.split())[:300])
    else:
        # the script has mV, to the firmware's reference of 2.048 V: half of
        # a result of 12 bits
        want = [f"{pin.lower()} {reads(mv, 2048) // 2} {reads(mv, 2048) // 2}"
                for pin, word, mv, level in held]
        check("a script in the firmware's JavaScript measures each of the "
              "six twice in a row (readAnalog(), in mV): 1250 for 1.25 V, "
              "100 for 0.1 V, 0 for a wire to GND, and 2047, the most there "
              "is, for 2.047 V and for 3V3",
              code == 0 and [x for x in lines if x[:2] in ("pa", "pc")]
              == want and want[0] == "pa7 1250 1250"
              and want[1] == "pa6 2047 2047" and "end" in lines,
              text + repr(want) + log_tail(b))
        check("... while the script has the ADC, the launcher has it as on, "
              "with its reference at 2.048 V, and the last of the twelve "
              "conversions: pin 16 (PC0) with 0.1 V on it, read as 200",
              now.get("on") is True and now.get("reference") == 2048
              and now.get("conversions") == before.get("conversions", 0) + 12
              and now.get("last") == {"channel": 1, "pin": 16, "data": 200,
                                      "uv": 100000}
              and block == "on, its reference at 2.048 V; 12 conversions, "
                           "the last: pin 16 (PC0), 0.1 V, read as 200\n",
              repr(before) + repr(now) + block)
        after = adc(b)
        code, out, err, took = fe(b, "status")
        check("... and as off again when the script has ended, with what "
              "it converted; `status` has that in a line",
              after.get("on") is False and after.get("reference") == 3300
              and after.get("conversions") == now.get("conversions")
              and re.search(r"^adc\s*: off \(nothing uses it now\); 12 "
                            r"conversions, the last: pin 16 \(PC0\), 0\.1 V, "
                            r"read as 200$", out, re.M),
              repr(after) + out + err)
    fe(b, "rm", "/ext/fe_adc.js")

    code, out, err, took = fe(b, "gpio", "free")
    check("`gpio free` lets go of them, and says what each was held at",
          code == 0 and "(it was: PA7 at 1.25 V, PA6 high, PA4 low, PC3 at "
                        "2.047 V, PC1 high, PC0 at 0.1 V)" in out, out + err)
    firmware(b, "gpio mode PA6 0")
    levels = []
    for word in ("1649mV", "1.65V", "0.4V", "2.4V"):
        fe(b, "gpio", "PA6", word)
        levels.append(("<= 1" in firmware(b, "gpio read PA6"),
                       fe(b, "gpio", "PA6")[1].strip()))
    check("a voltage is a level too, to a pin that the firmware reads as an "
          "input (its `gpio read PA6`): 1.649 V is low and 1.65 V high, "
          "0.4 V low, 2.4 V high",
          levels == [(False, "low"), (True, "high"), (False, "low"),
                     (True, "high")], levels)
    code, out, err, took = fe(b, "gpio", "PB3", "0.4V")
    table = fe(b, "gpio")[1]
    check("a voltage to a pin that is no input of the ADC's (PB3): held, "
          "and the answer says that it is a level there and no more",
          code == 0 and "held at 0.4 V from outside (low as a level)" in out
          and "PB3 is no input of the ADC's" in out
          and row(table, "PB3") == "5 PB3 analog low held at 0.4 V from "
                                   "outside (low as a level)", out + err + table)
    fe(b, "gpio", "free")       # (PA6 stays an input: the firmware's CLI has
                                # no way back.  The check's app has one)

    def refused(label, words, *args):
        code, out, err, took = fe(b, "gpio", *args)
        check(label + ": exit 1, one line that says why",
              code == 1 and not out and len(err.strip().splitlines()) == 1
              and all(w in err for w in words), out + err)

    refused("more than the 3.3 V that the chip runs on (3.4V)",
            ["3.4V", "0 to 3.3 V"], "PA7", "3.4V")
    refused("a number with no unit (1.25)", ["'1.25'", "1250mV"],
            "PA7", "1.25")
    refused("a voltage below 0 (-1V)", ["'-1V'", "1250mV"], "PA7", "-1V")
    refused("a voltage to the log's UART (TX)", ["PB6", "USART1's TX"],
            "TX", "1V")
    st = status(b)
    check("... and none of that held anything",
          st and not any(p.get("held") for p in st.values()), st)


def pwms(st):
    """The header's PWMs in what status() gave: a pin -> (its timer's clock
    in Hz, what divides that, a period and its high part in ticks)"""
    return {pin: tuple(p["pwm"].get(k) for k in ("clock", "divider", "period",
                                                 "high"))
            for pin, p in st.items() if p.get("pwm")}


def pwm_wait(b, want, seconds=20.0):
    """Wait until the header's PWMs are these: (are they, the status)"""
    end = time.time() + seconds
    while True:
        st = status(b)
        if pwms(st) == want or time.time() > end:
            return pwms(st) == want, st
        time.sleep(0.2)


def pwm_checks(b):
    """Nothing is held, and nothing is recorded"""
    vcd, js = b.dir / "pwm.vcd", b.dir / "fe_pwm.js"
    js.write_text(PWM_JS)
    fe(b, "push", js, "/ext/fe_pwm.js")
    code, out, err, took = fe(b, "gpio", "PA7", "--wait", "pwm",
                              "--timeout", 2)
    check("--wait pwm for a pin that has no PWM: exit 1 after the timeout",
          code == 1 and re.search(r"PA7 is \w+, not pwm after 2 s", err)
          and 1.5 < took < 10, out + err)
    fe(b, "gpio", "record", vcd)
    script = b.bg("cli", "--timeout", 120, "js /ext/fe_pwm.js")

    def step(level, want):
        fe(b, "gpio", "PA6", level)
        ok, st = pwm_wait(b, want)
        return ok, st, fe(b, "gpio")[1]

    def ended():
        try:
            return script.communicate(timeout=60)[0]
        except subprocess.TimeoutExpired:
            script.kill()
            return "(still running) " + script.communicate()[0]

    def tidy():
        fe(b, "gpio", "record", "stop")
        fe(b, "gpio", "free")
        fe(b, "rm", "/ext/fe_pwm.js")

    code, out, err, took = fe(b, "gpio", "PA7", "--wait", "pwm",
                              "--timeout", 30)
    first = {"PA7": (64000000, 1, 64000, 32000),
             "PA4": (64000000, 1, 6401, 1921)}
    there, st = pwm_wait(b, first) if code == 0 else (False, {})
    if not there:
        for level in ("high", "low", "high", "low"):    # (to the script's end)
            fe(b, "gpio", "PA6", level)
            time.sleep(1.0)
        text = ended()
        tidy()
        if "running true true" in text:
            return check("a script's PWMs (pwmWrite) are on the header",
                         False, text + out + err + repr(st) + log_tail(b))
        return skip("a script in the firmware's JavaScript makes PWMs "
                    "(pwmWrite)", "this firmware's JavaScript did not run "
                    "it: " + " ".join(text.split())[:300])

    check("a script in the firmware's JavaScript starts a PWM on each of "
          "the two pins that the firmware has one for (pwmWrite(1000, 50) "
          "on PA7, pwmWrite(10000, 30) on PA4): `gpio PA7 --wait pwm` is "
          "back with exit 0 when it is there, and says what it is",
          "PA7 has a PWM from TIM1: 1 kHz, high 50 % of the time" in out,
          out + err)
    table = fe(b, "gpio")[1]
    words = [fe(b, "gpio", name)[1] for name in ("PA7", "4")]
    check("the table has both: pin 2 as TIM1's (its alternate function 1) "
          "and pin 4 as LPTIM2's (14), each with its frequency and the part "
          "of the time for which it is high.  Such a pin's level is the "
          "word `pwm`",
          row(table, "PA7") == "2 PA7 alternate function 1 pwm PWM from "
                               "TIM1: 1 kHz, high 50 % of the time"
          and row(table, "PA4") == "4 PA4 alternate function 14 pwm PWM "
                                   "from LPTIM2: 9.998 kHz, high 30.01 % of "
                                   "the time"
          and row(table, "PA6") == "3 PA6 input, pull-down low"
          and words == ["pwm\n"] * 2, table + repr(words))
    code, out, err, took = fe(b, "status")
    a7, a4 = st["PA7"]["pwm"], st["PA4"]["pwm"]
    check("status --json has the numbers that it comes from: a timer's "
          "clock, what divides it, and a period with its high part, in "
          "ticks.  TIM1: 64 MHz, 64000 ticks with 32000 high.  LPTIM2, "
          "which counts one tick more than the firmware asks for: 6401 "
          "ticks with 1921 high, so 9.998 kHz and 30.01 %.  `status` has "
          "both in its line",
          a7["timer"] == "TIM1" and a7["hz"] == 1000 and a7["duty"] == 50
          and a4["timer"] == "LPTIM2" and abs(a4["hz"] - 64e6 / 6401) < 1e-6
          and abs(a4["duty"] - 192100 / 6401) < 1e-6
          and st["PA7"]["level"] == "pwm" and st["PA4"]["level"] == "pwm"
          and st["PA6"]["pwm"] is None and st["PA6"]["level"] == "low"
          and re.search(r"^gpio\s*: PA7 has a PWM \(1 kHz, high 50 %\); PA4 "
                        r"has a PWM \(9\.998 kHz, high 30\.01 %\)$", out,
                        re.M), repr(st) + out + err)
    code, out, err, took = fe(b, "gpio", "PA7", "high")
    table = fe(b, "gpio")[1]
    fe(b, "gpio", "PA7", "free")
    check("3V3 held to a pin whose PWM the firmware drives (push-pull): "
          "the PWM stays, and the answer and the table say that what is "
          "held does not show",
          code == 0 and "PA7 (pin 2 of the header): held high from outside; "
                        "it has a PWM from TIM1: 1 kHz, high 50 % of the "
                        "time" in out and "The firmware drives it" in out
          and row(table, "PA7") == "2 PA7 alternate function 1 pwm PWM from "
                                   "TIM1: 1 kHz, high 50 % of the time; held "
                                   "high from outside, which does not show "
                                   "while the firmware drives the pin",
          out + err + table)

    ok, st, table = step("high", {"PA7": (64000000, 3, 48484, 12121),
                                  "PA4": (32768, 1, 6554, 3277)})
    check("other numbers for both (pwmWrite(440, 25), pwmWrite(5, 50)).  "
          "TIM1 with a prescaler: 64 MHz over 3, and 48484 ticks, 440 Hz.  "
          "LPTIM2, for which 5 Hz is too slow with the 64 MHz, on the "
          "32768 Hz crystal, as the firmware has it then: 6554 ticks, 5 Hz",
          ok and row(table, "PA7").endswith("pwm PWM from TIM1: 440 Hz, "
                                            "high 25 % of the time")
          and row(table, "PA4").endswith("pwm PWM from LPTIM2: 5 Hz, high "
                                         "50 % of the time"),
          repr(pwms(st)) + table)
    ok, st, table = step("low", {"PA7": (64000000, 10, 64000, 6400),
                                 "PA4": (64000000, 16, 40001, 4001)})
    check("... and 100 Hz, 10 % for both: each timer with its prescaler "
          "(TIM1 over 10, LPTIM2 over 16)",
          ok and row(table, "PA7").endswith("pwm PWM from TIM1: 100 Hz, "
                                            "high 10 % of the time")
          and row(table, "PA4").endswith("pwm PWM from LPTIM2: 100 Hz, high "
                                         "10 % of the time"),
          repr(pwms(st)) + table)
    ok, st, table = step("high", {"PA7": (64000000, 1, 64000, 0),
                                  "PA4": (64000000, 1, 64001, 64001)})
    code, out, err, took = fe(b, "gpio", "PA7", "--wait", "pwm",
                              "--timeout", 1)
    check("a PWM that is high for none of the time (pwmWrite(1000, 0)) is "
          "a pin that is low, and one for all of it (100) a pin that is "
          "high: no `pwm` then, for --wait either",
          ok and row(table, "PA7") == "2 PA7 alternate function 1 low PWM "
                                      "from TIM1: 1 kHz, high 0 % of the time"
          and row(table, "PA4") == "4 PA4 alternate function 14 high PWM "
                                   "from LPTIM2: 1 kHz, high 100 % of the "
                                   "time"
          and code == 1 and "PA7 is low, not pwm after 1 s" in err,
          repr(pwms(st)) + table + out + err)
    ok, st, table = step("low", {})
    text = ended()
    lines = [" ".join(line.split()) for line in text.splitlines()]
    code, out, err, took = fe(b, "status")
    check("pwmStop() for both: no PWM any more, the two pins are analog "
          "and float, and `status` has none",
          ok and row(table, "PA7") == "2 PA7 analog floating"
          and row(table, "PA4") == "4 PA4 analog floating"
          and "PWM" not in table and "PWM" not in out, repr(pwms(st)) + table
          + out + err)
    check("what the script itself was told: PA7 and PA4 can have a PWM and "
          "PA6 cannot (isPwmSupported); none runs before and none after, "
          "both in between (isPwmRunning)",
          [x for x in lines if x.startswith(("supported", "running"))]
          == ["supported true true false", "running false false",
              "running true true", "running false false"]
          and "end" in lines and "Script done!" in text,
          text + log_tail(b))
    code, out, err, took = fe(b, "gpio", "record", "stop")
    changes, wrong = recorded(vcd)
    check("a record of all that has no edges of a PWM: such a pin is "
          "unknown (x) in it for as long as the PWM runs.  PA7: unknown, "
          "low (0 %), floating; PA4: unknown, high (100 %), floating",
          code == 0 and not wrong
          and of(changes, "PA7") == ["unknown", "low", "floating"]
          and of(changes, "PA4") == ["unknown", "high", "floating"],
          out + err + wrong + repr(changes))
    tidy()


def pwm_app_checks(b, folder):
    def rows(table):
        return row(table, "PA7"), row(table, "PA4")

    def step(level, line, want):
        said = hold(b, "PA6", level, line, tag="FePwm")
        ok, st = pwm_wait(b, want)
        return ok and line in said, st, fe(b, "gpio")[1]

    code, text = tool(b, folder, "launch")
    done = logged(b, "[FePwm] 1: both as the firmware starts them", 30)
    ok, st = pwm_wait(b, {"PA7": (64000000, 1, 64000, 12800),
                          "PA4": (64000000, 1, 6401, 1921)})
    check("`ufbt launch` starts the check's third app "
          "(scripts/pwm_check_app), which starts both PWMs with the "
          "firmware's own calls (furi_hal_pwm_start: 1 kHz and 20 %, 10 kHz "
          "and 30 %); the firmware says that both run",
          code == 0 and done and ok
          and "running: 1 1" in app_lines(b, "FePwm"),
          text[-1500:] + repr(pwms(st)) + log_tail(b))
    ok, st, table = step("high", "2: the other polarity for both, LPTIM2 "
                         "with a prescaler",
                         {"PA7": (64000000, 1, 64000, 51200),
                          "PA4": (64000000, 4, 1000, 749)})
    check("the other polarity of each output, which the firmware's calls "
          "do not set.  TIM1's is high for what was its low part: 80 %.  "
          "LPTIM2, which the app sets up with its clock over 4 and 1000 "
          "ticks for a period, is high from its compare value (250) to the "
          "period's end: 749 ticks, at 16 kHz",
          ok and rows(table)
          == ("2 PA7 alternate function 1 pwm PWM from TIM1: 1 kHz, high "
              "80 % of the time",
              "4 PA4 alternate function 14 pwm PWM from LPTIM2: 16 kHz, "
              "high 74.9 % of the time"), repr(pwms(st)) + table)
    ok, st, table = step("low", "3: TIM1 in PWM mode 2, PA4 analog",
                         {"PA7": (64000000, 1, 64000, 12800)})
    check("TIM1's other PWM mode turns its output around once more: 20 %.  "
          "And a timer whose pin is not its own is no PWM on the header: "
          "PA4, analog now, floats while LPTIM2 runs on",
          ok and rows(table)
          == ("2 PA7 alternate function 1 pwm PWM from TIM1: 1 kHz, high "
              "20 % of the time", "4 PA4 analog floating"),
          repr(pwms(st)) + table)
    ok, st, table = step("high", "4: PA7 open drain, PA4 is LPTIM2's again",
                         {"PA7": (64000000, 1, 64000, 12800),
                          "PA4": (64000000, 4, 1000, 749)})
    low = fe(b, "gpio", "PA7", "low")[1]
    table_low = fe(b, "gpio")[1]
    high = fe(b, "gpio", "PA7", "high")[1]
    table_high = fe(b, "gpio")[1]
    fe(b, "gpio", "PA7", "free")
    od = "2 PA7 alternate function 1, open drain, pull-up "
    said = "PWM from TIM1: 1 kHz, high 20 % of the time"
    check("PA7 as an open-drain output of the timer, with a pull-up: its "
          "PWM shows.  A wire to GND wins there: held low from outside, the "
          "pin is low.  With 3V3 held to it, the PWM shows again.  PA4, "
          "LPTIM2's once more, has the PWM that ran on",
          ok and rows(table) == (od + "pwm " + said, "4 PA4 alternate "
                                 "function 14 pwm PWM from LPTIM2: 16 kHz, "
                                 "high 74.9 % of the time")
          and "held low from outside; it is low" in low
          and row(table_low, "PA7") == od + "low " + said + "; held low "
                                                           "from outside"
          and "held high from outside; it has a " + said in high
          and row(table_high, "PA7") == od + "pwm " + said + "; held high "
                                                            "from outside",
          repr(pwms(st)) + table + low + table_low + high + table_high)
    ok, st, table = step("low", "5: both stopped, PA7 still TIM1's pin", {})
    check("both stopped (furi_hal_pwm_stop): no PWM.  PA7, which the app "
          "makes TIM1's pin once more, is `unknown` then: a peripheral's "
          "pin with a signal that the emulator has no word for",
          ok and rows(table) == ("2 PA7 alternate function 1 unknown",
                                 "4 PA4 analog floating"),
          repr(pwms(st)) + table)
    said = hold(b, "PA6", "high", "done: 0 0", tag="FePwm")
    st = status(b)
    check("the app ends: the firmware says that neither runs, and the "
          "three pins are analog",
          "done: 0 0" in said
          and all(st.get(p, {}).get("mode") == "analog"
                  for p in ("PA7", "PA6", "PA4")),
          repr(said) + repr(st) + log_tail(b))


def app_lines(b, tag="FePins"):
    """What one of the check's apps has logged: its lines, without time and
    tag"""
    return [line.split(f"[{tag}] ", 1)[1].strip()
            for line in ANSI.sub("", b.uart_text()).splitlines()
            if f"[{tag}] " in line]


def hold(b, pin, level, line=None, quiet=1.0, tag="FePins"):
    """Hold a header pin from outside, or let go of it.  -> the app's lines
    since: with `line`, as soon as that one is there (10 s at most); without,
    after `quiet` seconds, in which the app is to log nothing"""
    before = len(app_lines(b, tag))
    fe(b, "gpio", pin, level)
    end = time.time() + (10.0 if line else quiet)
    while time.time() < end and not (line
                                     and line in app_lines(b, tag)[before:]):
        time.sleep(0.1)
    return app_lines(b, tag)[before:]


def own_app(b, label, source, appid, checks):
    """Build one of the check's own apps, and run its checks if ufbt's SDK
    is this firmware's.  Nothing is held then, and the header's pins are as
    after a start"""
    if not shutil.which("ufbt"):
        return skip(label, "no `ufbt` on the PATH (`pipx install ufbt` gets "
                    "it)")
    work = pathlib.Path(tempfile.mkdtemp(prefix="fe-pins-"))
    try:
        folder = work / appid
        shutil.copytree(source, folder)
        code, text = tool(b, folder)
        fap = folder / "dist" / f"{appid}.fap"
        if code or not fap.is_file():
            return check(f"`ufbt` builds the check's app ({source.name})",
                         False, text[-1500:])
        code, out, err, took = fe(b, "app", "--no-start", "--to",
                                  "/ext/apps/fe-check", fap)
        fe(b, "rm", "-r", "/ext/apps/fe-check")
        if "which is this firmware's" not in out:
            return skip(label, "ufbt's SDK is not for this firmware: "
                        + " ".join((out + err).split())[:200])
        checks(b, folder)
    finally:
        fe(b, "gpio", "record", "stop")
        fe(b, "gpio", "free")
        fe(b, "app", "--close")
        shutil.rmtree(work, ignore_errors=True)


def adc_app_checks(b, folder):
    held = (("PA7", "1.25V", 1250), ("PA6", "high", 3300), ("PA4", "2V", 2000),
            ("PC3", "100mV", 100), ("PC1", "low", 0), ("PC0", None, 0))
    for pin, word, mv in held:
        if word:
            fe(b, "gpio", pin, word)
    before = adc(b)
    code, text = tool(b, folder, "launch")
    done = logged(b, "[FeAdc] done", 30)
    lines = app_lines(b, "FeAdc")
    check("`ufbt launch` starts the check's other app "
          "(scripts/adc_check_app), which measures with the firmware's ADC, "
          "says what it read, and ends", code == 0 and done,
          text[-1500:] + log_tail(b))
    check("what is inside the chip, with the firmware's own turning of a "
          "result into volts and degrees.  With the reference at 2.048 V: "
          "the chip's 1.212 V reads as 2424, from which the firmware has the "
          "ADC's reference as 2048 mV; the temperature sensor as 1495, "
          "25 degrees; a third of the battery pin's 3.3 V as 2200, 3300 mV",
          "at 2.048 V: reference 2424 = 2048 mV, temperature 1495 = 25 C, "
          "battery 2200 = 3300 mV" in lines, "\n".join(lines))
    check("... and with the ADC's other reference, 2.5 V, and one sample "
          "for a result: other numbers for the same (1985, 1224, 1802), and "
          "2500 mV, 25 degrees, 3300 mV",
          "at 2.5 V: reference 1985 = 2500 mV, temperature 1224 = 25 C, "
          "battery 1802 = 3300 mV" in lines, "\n".join(lines))
    numbers = dict(zip("PA7 PA6 PA4 PC3 PC1 PC0".split(), (2, 3, 4, 7, 15, 16)))
    for at, reference in (("at 2.048 V", 2048), ("at 2.5 V", 2500)):
        # (the firmware's mV: a result times the reference, over 4095)
        want = [f"{at}: pin {numbers[pin]} {pin} {reads(mv, reference)} and "
                f"{reads(mv, reference)} = "
                f"{reads(mv, reference) * reference // 4095} mV"
                for pin, word, mv in held]
        got = [x for x in lines if x.startswith(f"{at}: pin ")]
        check(f"the six pins of the firmware's own table {at}, each twice "
              "in a row: " + ", ".join(
                  f"{pin} {x.split(' = ')[1]}" for (pin, word, mv), x
                  in zip(held, want))
              + " (held: 1.25 V, 3V3, 2 V, 0.1 V, GND, nothing)",
              got == want, "\n".join(got) + "\n" + "\n".join(want))
    check("a pin's voltage that is the firmware's own doing (PC0, with "
          "nothing held to it): 3.3 V as an output that is high and 0 V as "
          "one that is low, 3.3 V as an input with a pull-up and 0 V with a "
          "pull-down",
          "pin 16 PC0 as an output: 4095 high, 0 low; as an input: 4095 "
          "with a pull-up, 0 with a pull-down" in lines, "\n".join(lines))
    after = adc(b)
    st = status(b)
    check("the launcher afterwards: the ADC is off, with the app's 34 "
          "conversions more and the last of them (pin 16, 0 V, read as 0), "
          "and the six pins are analog",
          after.get("on") is False
          and after.get("conversions") == before.get("conversions", 0) + 34
          and after.get("last") == {"channel": 1, "pin": 16, "data": 0,
                                    "uv": 0}
          and all(st.get(pin, {}).get("mode") == "analog" for pin in numbers),
          repr(before) + repr(after) + repr(st))


def app_checks(b, folder):
    vcd = b.dir / "app.vcd"
    fe(b, "gpio", "record", vcd)
    code, text = tool(b, folder, "launch")
    check("`ufbt launch` starts the check's own app (scripts/gpio_check_app):"
          " four inputs with an interrupt, and PA7 an open-drain output "
          "that it lets go of; it reads them by their pulls, PA7 high",
          code == 0 and logged(b, "[FePins] ready: PA4 1, PC0 1, PC1 0, "
                                  "PB2 1; PA7 let go, reads 1"),
          text[-1500:] + log_tail(b))
    code, out, err, took = fe(b, "gpio")
    check("the table has the edges that the firmware has an interrupt for: "
          "both for PA4, the fall for PC0, the rise for PC1 (pulled down, so "
          "low); and PA7 as an open-drain output with a pull-up, high",
          code == 0
          and row(out, "PA4") == "4 PA4 input, pull-up, interrupt on both "
                                 "edges high"
          and row(out, "PC0") == "16 PC0 input, pull-up, interrupt when it "
                                 "falls high"
          and row(out, "PC1") == "15 PC1 input, pull-down, interrupt when it "
                                 "rises low"
          and row(out, "PA7") == "2 PA7 output, open drain, pull-up high",
          out + err)
    check("... PB2, which the app asked for its fall alone, has both: an "
          "interrupt line is its number's, and line 2 has had its rise "
          "since the firmware's start (PA2, the NFC chip's interrupt)",
          row(out, "PB2") == "6 PB2 input, pull-up, interrupt on both edges "
                             "high", out)
    st = status(b)
    check("status --json: the same (\"interrupt\": both, falling, rising; "
          "\"open_drain\")",
          [st.get(p, {}).get("interrupt") for p in "PA4 PC0 PC1 PB2 PA7 PA6"
           .split()] == ["both", "falling", "rising", "both", None, None]
          and st["PA7"].get("open_drain") is True
          and st["PA7"].get("mode") == "output"
          and st["PA7"].get("pull") == "up", st)

    said = hold(b, "PA4", "low", "1 PA4 fell: PA7 pulled low, reads 0")
    level = fe(b, "gpio", "PA7")[1]
    check("PA4 held low: one interrupt, on its fall; the app pulls PA7 low "
          "for it, and from outside PA7 is low",
          said == ["1 PA4 fell: PA7 pulled low, reads 0"]
          and level == "low\n", repr(said) + level + log_tail(b))
    code, out, err, took = fe(b, "gpio", "PA7", "high")
    table = fe(b, "gpio")[1]
    check("3V3 held to PA7 while the app pulls it low: it stays low, as "
          "an open-drain line does",
          code == 0 and "held high from outside; it is low" in out
          and row(table, "PA7") == "2 PA7 output, open drain, pull-up low "
                                   "held high from outside", out + err + table)
    said = hold(b, "PA4", "high", "2 PA4 rose: PA7 let go, reads 1")
    level = fe(b, "gpio", "PA7")[1]
    check("PA4 held high: one interrupt, on its rise; the app lets go of "
          "PA7, which is high", said == ["2 PA4 rose: PA7 let go, reads 1"]
          and level == "high\n", repr(said) + level + log_tail(b))
    again = hold(b, "PA4", "high")
    check("... held high once more: no edge, no interrupt", again == [],
          again)

    code, out, err, took = fe(b, "gpio", "PA7", "low")
    said = hold(b, "PC0", "low", "3 PC0 fell: PA7 let go, reads 0")
    check("PA7 held low from outside while the app lets go of it: it is "
          "low, and the firmware reads it so in PC0's interrupt, which "
          "comes on PC0's fall",
          code == 0 and "held low from outside; it is low" in out
          and said == ["3 PC0 fell: PA7 let go, reads 0"],
          out + err + repr(said) + log_tail(b))
    fe(b, "gpio", "PA7", "free")
    level = fe(b, "gpio", "PA7")[1]
    said = hold(b, "PC0", "high")
    check("PA7 let go of: high by its pull-up.  PC0 held high: no "
          "interrupt, its rise was not asked for",
          level == "high\n" and said == [], level + repr(said))

    said = hold(b, "PC1", "high",
                "4 PC1 rose: PA7 let go, reads 1; PC0's interrupt off")
    table = fe(b, "gpio")[1]
    quiet = hold(b, "PC0", "low") + hold(b, "PC0", "high")
    check("PC1 held high: one interrupt, on its rise, in which the app "
          "turns PC0's interrupt off.  The table has PC0 without one, and "
          "PC0 held low and high gives none",
          said == ["4 PC1 rose: PA7 let go, reads 1; PC0's interrupt off"]
          and row(table, "PC0") == "16 PC0 input, pull-up high held high "
                                   "from outside"
          and quiet == [], repr(said) + repr(quiet) + table)
    quiet = hold(b, "PC1", "low")
    said = hold(b, "PC1", "high",
                "5 PC1 rose: PA7 let go, reads 1; PC0's interrupt on")
    table = fe(b, "gpio")[1]
    more = hold(b, "PC0", "low", "6 PC0 fell: PA7 let go, reads 1")
    check("PC1 held low (no interrupt on its fall) and high again: PC0's "
          "interrupt is on, the table has it, and PC0's next fall is one "
          "interrupt: none was kept from the time it was off",
          quiet == []
          and said == ["5 PC1 rose: PA7 let go, reads 1; PC0's interrupt on"]
          and "interrupt when it falls" in row(table, "PC0")
          and more == ["6 PC0 fell: PA7 let go, reads 1"],
          repr(quiet) + repr(said) + repr(more) + table)

    said = hold(b, "PB2", "low", "done: 7 interrupts")
    st = status(b)
    check("PB2 held low: its interrupt, with which the app ends.  It has "
          "counted seven, and its five pins are analog again, with no "
          "interrupt",
          said == ["7 PB2 fell: PA7 let go, reads 1", "done: 7 interrupts"]
          and all(st.get(p, {}).get("mode") == "analog"
                  and st[p].get("interrupt") is None
                  for p in "PA7 PA4 PC0 PC1 PB2".split()),
          repr(said) + repr(st) + log_tail(b))
    code, out, err, took = fe(b, "gpio", "free")
    table = fe(b, "gpio")[1]
    check("`gpio free` lets go of the four that were held; the header is "
          "as after a start, PB7's interrupt alone on it",
          code == 0 and all(f"{p} {level}" in out for p, level in
                            (("PA4", "high"), ("PC0", "low"), ("PC1", "high"),
                             ("PB2", "low")))
          and len(re.findall("interrupt", table.split("its serial ports")[0]))
          == 1 and "interrupt when it falls" in row(table, "PB7")
          and all(row(table, p).endswith("floating")
                  for p in "PA7 PA4 PC0 PC1 PB2".split()), out + err + table)
    code, out, err, took = fe(b, "gpio", "record", "stop")
    changes, wrong = recorded(vcd)
    check("the record of all that: PA7 high when the app made it an output, "
          "low while the app pulled it low (also with 3V3 held to it), "
          "high, low while it was held low, high, and floating at the end",
          code == 0 and not wrong
          and of(changes, "PA7") == ["high", "low", "high", "low", "high",
                                     "floating"]
          and of(changes, "PA4")[:3] == ["high", "low", "high"]
          and of(changes, "PC1")[:4] == ["low", "high", "low", "high"],
          out + err + wrong + repr(changes))
    fe(b, "nfc", "ntag215", "04A1B2C3D4E5F6", "https://flipperzero.one")
    code, out, err, took = fe(b, "cli", "--timeout", 30, "nfc", "mfu info",
                              "exit")
    fe(b, "nfc", "none")
    check("... and the firmware's NFC, whose chip's interrupt is on the "
          "line that PB2 had, reads a tag afterwards (`mfu info` in its CLI)",
          code == 0 and "NTAG215" in out, out + err + log_tail(b))


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("firmware")
    ap.add_argument("--timeout", type=float, default=120.0,
                    help="seconds to wait for the firmware to start")
    ap.add_argument("--keep", action="store_true",
                    help="keep the throwaway device")
    args = ap.parse_args()

    b = Bench(f"gpiocheck-{int(time.time())}-{os.getpid()}", args.firmware,
              args.timeout)
    try:
        code, out, err, took = fe(b, "gpio")
        check("a device that is not running: says so, exit 1",
              code == 1 and "not running" in err, out + err)
        ok = b.start(first=True)
        said = ""
        if ok:
            code, out, err, took = fe(b, "wait", "--timeout", args.timeout)
            ok, said = b.answers() if code == 0 else (False, out + err)
        if check("the firmware starts, and its CLI answers", ok,
                 said + b.uart_text()[-2000:]):
            start_checks(b)
            output_checks(b)
            input_checks(b)
            fight_checks(b)
            record_checks(b)
            refused_checks(b)
            adc_checks(b)
            pwm_checks(b)
            own_app(b, "the ADC with the firmware's own turning of a result "
                    "into volts and degrees", ADC_APP, "fe_adc_check",
                    adc_app_checks)
            own_app(b, "PWMs that the firmware's own calls do not make: the "
                    "other polarity and PWM mode, an open-drain pin, a "
                    "timer with no pin", PWM_APP, "fe_pwm_check",
                    pwm_app_checks)
            own_app(b, "interrupts from held pins, and an open-drain output",
                    APP, "fe_pin_check", app_checks)
            ok, said = b.answers()
            check("the CLI still answers", ok, said)
            log = b.uart_text(every=True)
            crash = CRASH.search(log)
            check("no crash in the firmware's log", not crash,
                  crash and log[max(0, crash.start() - 300):crash.end() + 300])
            vcd = b.dir / "last.vcd"
            fe(b, "gpio", "record", vcd)
            fe(b, "gpio", "PA6", "high")
            b.stop()
            code, out, err, took = fe(b, "stop")
            changes, wrong = recorded(vcd)
            check("the emulator stopped while it records: the file has what "
                  "was recorded (PA6 held high)",
                  not wrong and of(changes, "PA6") == ["high"],
                  out + err + wrong + repr(changes))
    finally:
        b.stop()
        fe(b, "stop")
        if not args.keep:
            shutil.rmtree(b.dir, ignore_errors=True)
    bad = [label for label, ok in results if not ok]
    print(f"{len(results) - len(bad)} of {len(results)} checks passed"
          + (f", {len(skipped)} skipped" if skipped else "")
          + (f"; device kept: {b.dir}" if args.keep else ""))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
