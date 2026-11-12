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

Then what the firmware's `gpio` command cannot do, with an app of the check's
own, scripts/gpio_check_app (it needs `ufbt` on the PATH and its SDK to be
this firmware's, which is the official release's as ufbt comes; skipped
otherwise): interrupts and an open-drain output.  The table says which edges
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
          out.endswith("its I2C bus, SDA on pin 15 and SCL on pin 16 (`run "
                       "--i2c TYPE@ADDRESS` puts a device there)\n"
                       "  pins 15 and 16 (I2C3)  no device; the firmware has "
                       "called no address yet\n"), out)
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
          and st["PA7"].get("interrupt") is None, st)
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
          code == 1 and "PA7 is high and not low after 2 s" in err
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


def app_lines(b):
    """What the check's app has logged: its lines, without time and tag"""
    return [line.split("[FePins] ", 1)[1].strip()
            for line in ANSI.sub("", b.uart_text()).splitlines()
            if "[FePins] " in line]


def hold(b, pin, level, line=None, quiet=1.0):
    """Hold a header pin from outside, or let go of it.  -> the app's lines
    since: with `line`, as soon as that one is there (10 s at most); without,
    after `quiet` seconds, in which the app is to log nothing"""
    before = len(app_lines(b))
    fe(b, "gpio", pin, level)
    end = time.time() + (10.0 if line else quiet)
    while time.time() < end and not (line and line in app_lines(b)[before:]):
        time.sleep(0.1)
    return app_lines(b)[before:]


def interrupt_checks(b):
    """Nothing is held, and the header's pins are as after a start"""
    label = "interrupts from held pins, and an open-drain output"
    if not shutil.which("ufbt"):
        return skip(label, "no `ufbt` on the PATH (`pipx install ufbt` gets "
                    "it)")
    work = pathlib.Path(tempfile.mkdtemp(prefix="fe-pins-"))
    try:
        folder = work / "fe_pin_check"
        shutil.copytree(APP, folder)
        code, text = tool(b, folder)
        fap = folder / "dist" / "fe_pin_check.fap"
        if code or not fap.is_file():
            return check("`ufbt` builds the check's app", False, text[-1500:])
        code, out, err, took = fe(b, "app", "--no-start", "--to",
                                  "/ext/apps/fe-check", fap)
        fe(b, "rm", "-r", "/ext/apps/fe-check")
        if "which is this firmware's" not in out:
            return skip(label, "ufbt's SDK is not for this firmware: "
                        + " ".join((out + err).split())[:200])
        app_checks(b, folder)
    finally:
        fe(b, "gpio", "record", "stop")
        fe(b, "gpio", "free")
        fe(b, "app", "--close")
        shutil.rmtree(work, ignore_errors=True)


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
            interrupt_checks(b)
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
