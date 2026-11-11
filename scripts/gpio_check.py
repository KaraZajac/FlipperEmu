#!/usr/bin/env python3
# Copyright (c) 2026, Kara Zajac
# SPDX-License-Identifier: BSD-3-Clause
"""Check the GPIO header: the launcher's `gpio`, with the firmware's own
`gpio` CLI command on the other side of the pins.

    scripts/gpio_check.py firmware/official-1.5.1/flipper-z-f7-update-1.5.1.tgz

Starts a throwaway device.  The header after a start: its thirteen signal
pins, none an output, none held.  Then the firmware makes PA7 an output and
sets it (`gpio mode`, `gpio set` in its CLI): the launcher sees that, as a
table, as one word, by the pin's three names, in `status`, and a `--wait` for
a level returns when the firmware sets it (and with exit status 1 when it
does not).  Then the other way: PA6 an input, held high and low from outside
and let go of, and the firmware's `gpio read` says so.  A pin held while the
firmware drives it keeps the firmware's level, and has what is held when it
is an input again.  What is held stays so over a reset of the firmware.  Then
the record (`gpio record FILE.vcd`): what the firmware does to PA7 and what
is held to PA6 are in the file in their order, each with its time, also over
a reset; `gpio watch` prints changes as they happen; a record of pin 17 while
the firmware reads a 1-Wire key has both sides of the line.  Last what is
refused: the pins with their own use, power pins, pins that are not there,
levels that are none.  At the end the emulator is stopped while it records,
and the file has what was recorded.  Exit status 0 = pass.
"""
import argparse
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import fe_header  # noqa: E402
from files_check import ROOT, Bench, CRASH, check, results  # noqa: E402

SIGNALS = "PA7 PA6 PA4 PB3 PB2 PC3 PA14 PA13 PB6 PB7 PC1 PC0 PB14".split()


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
          "an input pulled up, and high), the iButton pin",
          "log is on this UART" in row(out, "PB6")
          and re.match(r"14 PB7 input, pull-up high .*UART", row(out, "PB7"))
          and "iButton" in row(out, "PB14"), out)
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
          and st["PB6"].get("function") == 7 and st["PB7"].get("pull") == "up",
          st)
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
    refused("nor is the log's UART (TX, RX)", ["PB6", "log"], "TX", "low")
    refused("... RX", ["PB7", "log"], "PB7", "low")
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
          + (f"; device kept: {b.dir}" if args.keep else ""))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
