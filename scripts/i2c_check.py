#!/usr/bin/env python3
# Copyright (c) 2026, Kara Zajac
# SPDX-License-Identifier: BSD-3-Clause
"""Check the I2C bus of the header, pins 15 (SDA) and 16 (SCL): devices that
`run --i2c` puts there, and the launcher's `i2c`.

    scripts/i2c_check.py firmware/momentum-mntm-012/flipper-z-f7-update-mntm-012.tgz

A `run --i2c` that is said wrongly is refused in words before anything is
done, a type that QEMU has not among them, and `run --i2c help` lists those
it has.  Then a throwaway device starts with a temperature sensor at 0x48 (at
23.5 degrees) and an EEPROM at 0x50: `i2c` names them with their values, the
EEPROM's contents are a file of the device, all bits 1.  The firmware's own
scan of the bus (its CLI's `i2c`) finds the two and nothing else, and the
launcher has counted its calls; `gpio`, `status` and `status --json` say what
is on the bus.  A value is read bare and set from outside, and what cannot be
is refused: a temperature out of the sensor's range, an address with nothing
at it, a value the device has not, one that is fixed while it runs.  `i2c
--json` has the bus and every device's values for a program.

`i2c record FILE` writes every transfer to a text file, a line for each
START: the scan again, and the file has its 128 calls in their order, the two
that were answered, and what was on the bus; the record's own count is the
file's.  `i2c watch 0x50` shows the one call of a scan that goes there, as it
happens, and leaves no record behind; nor does a watch that a script ends
with `kill`.  What is said wrongly is refused.  And
the word on an address that was not shifted (a call to 0x24 that nobody
answers while a sensor is at 0x48) is said for that call and for no other.
A line with a mark (no STOP before the next START, a byte the device
refused, a record that ended inside a transfer) is read as it is written,
and said in words.

Where the firmware's JS has an i2c module (Momentum, Unleashed, RogueMaster;
the official firmware's has none, and that part is skipped), the firmware runs
examples/i2c/sensor.js: it reads the temperature that was set, and again
after another was; the EEPROM counts its runs, in the device's file as soon
as written, and on after a reset.  A record of one run has every byte the
script wrote and read; `i2c watch` shows another run as it happens, and a
script that gives the firmware 0x48 as it is: the call to 0x24, and the word
on it, which `i2c` has too.

Where `ufbt` is on the PATH with an SDK for the firmware that is given (the
official release, as ufbt comes; skipped otherwise), the same from an app in
C, examples/i2c/sensor.c: built, put on the Flipper and started by
`./flipperemu ufbt launch`, it logs the run that the EEPROM counts and the
temperature, and logs another when the world outside has one (a record has
both readings, byte for byte, and the count it wrote); and the example's own
test.sh passes on a Flipper of its own.

A record that nobody ends goes on over a reset of the firmware, and its file
is whole when the device is stopped.  After the stop an EEPROM of another
size is refused while the file is there, and a start without the sensor
finds the EEPROM's contents kept.

That second start has QEMU's device for tests on the bus as well (i2c-echo at
0x42: it keeps three bytes, gives them back to a read, and refuses a fourth),
for what neither example does there.  A script (where the JS has an i2c
module) writes four bytes and five: the record marks the fourth as refused,
and the firmware's call says so only when a byte was still to follow.  And
where ufbt has the firmware's SDK, an app in C of the check's own
(scripts/i2c_check_app) does that and more: a START with no STOP before it,
to the same device, to another, and to an address where nobody is; 300 bytes
in one read; a write that is held and gone on with; and a transfer that is
open while the record is ended.  The record has each as the app's log says
it went.  (A firmware with neither: skipped.)
Exit status 0 = pass.
"""
import argparse
import json
import os
import pathlib
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time

from files_check import ROOT, Bench, CRASH, check, results
from uart_check import fe, start
from ufbt_check import ANSI, log_tail, logged, tool

sys.path.insert(0, str(ROOT / "scripts"))
import fe_header                                            # noqa: E402

EXAMPLE = ROOT / "examples" / "i2c"
SENSOR = EXAMPLE / "sensor.js"
APP = "/ext/apps/Examples/fe_sensor.fap"
OWN = ROOT / "scripts" / "i2c_check_app"
# QEMU's i2c-echo keeps three bytes, and refuses a fourth
ECHO_JS = """let i2c = require("i2c");
let ECHO = 0x42 << 1;
print("four:", i2c.write(ECHO, [1, 2, 3, 4], 50));
print("five:", i2c.write(ECHO, [0x11, 0x12, 0x13, 0x14, 0x15], 50));
let back = Uint8Array(i2c.read(ECHO, 3, 50));
print("back:", back[0], back[1], back[2]);
print("echo done");
"""
WRONG = (
    ("tmp105", "TYPE@ADDRESS", "a device without an address"),
    ("tmp105@72", "as 0x48", "an address that is not in hex"),
    ("tmp105@0x03", "0x08 to 0x77", "an address that is not a device's"),
    ("tmp105@0x48,bus=i2c1", "not to be set", "a bus of one's own choice"),
    ("tmp105@0x48,temperature=warm", "in °C", "a temperature that is none"),
    ("eeprom@0x50,rom-size=0", "1 to 1048576", "an EEPROM of no bytes"),
    ("lm75@0x48", "QEMU has no I2C device 'lm75'.  It has: ",
     "a type that QEMU has not"),
)
skipped = []


def skip(label, why):
    skipped.append(label)
    print(f"  SKIP  {label}\n        {why}", flush=True)


def bus(b):
    """What `status --json` says of the header's I2C bus"""
    code, out, err, _ = fe(b, "status", "--json")
    try:
        return json.loads(out).get("i2c") or {}
    except ValueError:
        return {}


def scan(b):
    """The firmware's own scan of the bus: the addresses that answered (None:
    it printed no table), and what it printed"""
    code, out, err, _ = fe(b, "cli", "--timeout", 60, "i2c")
    found, rows = [], 0
    for line in out.splitlines():
        row = re.match(r"\s*([0-7]) \|((?: [-#]){16})", line)
        if row:
            rows += 1
            found += [int(row[1], 16) * 16 + at
                      for at, mark in enumerate(row[2].split()) if mark == "#"]
    return (found if rows == 8 else None), out + err


def sensor(b):
    """The firmware runs examples/i2c/sensor.js: (the temperature it read,
    the number of the run; None each: it said none), and what it printed"""
    code, out, err, _ = fe(b, "cli", "--timeout", 60, "js /ext/sensor.js")
    t = re.search(r"temperature: (-?[0-9.]+)", out)
    n = re.search(r"run number: (\d+)", out)
    return (float(t[1]) if t else None, int(n[1]) if n else None), out + err


def recorded(path):
    """A record of the bus, read: (what was on the bus as the file says it,
    the parts of its transfers as (address, "r" or "w", was it taken, the
    bytes), the same as fe_header has them, what is wrong with the file)"""
    try:
        devices, parts = fe_header.read_i2c_record(path)
    except (OSError, ValueError) as e:
        return [], [], [], f"{path}: {e!r}"
    return devices, [(p["address"], "r" if p["read"] else "w", p["taken"],
                      p["data"]) for p in parts], parts, ""


def count(parts):
    """What the record itself should have counted for those parts, in the
    launcher's words"""
    return fe_header.i2c_recorded({
        "calls": len(parts), "unanswered": sum(not p["taken"] for p in parts),
        "written": sum(len(p["data"]) - p["refused"] for p in parts
                       if not p["read"]),
        "read": sum(len(p["data"]) for p in parts if p["read"])})


def watched(watch, wait=60):
    """What an `i2c watch` printed, once it ended: (all of it, its lines of
    the bus without their times, each with one blank between its words)"""
    try:
        out = watch.communicate(timeout=wait)[0]
    except subprocess.TimeoutExpired:
        watch.kill()
        out = "(still watching)" + watch.communicate()[0]
    return out, [" ".join(line.split()[1:]) for line in out.splitlines()
                 if re.match(r"\s*\d+\.\d{6}\s", line)]


def record_checks(b):
    """Every transfer into a file, and as it happens: the firmware's scan"""
    rec, other = b.dir / "bus.txt", b.dir / "bus2.txt"
    code, out, err, _ = fe(b, "i2c", "record")
    c2, o2, e2, _ = fe(b, "i2c", "record", "stop")
    check("`i2c record` while none runs: says so; `i2c record stop`: exit "
          "1, there is none to end",
          code == 0 and "not being recorded" in out and c2 == 1 and not o2
          and "nothing is being recorded" in e2, out + err + o2 + e2)
    code, out, err, _ = fe(b, "i2c", "record", rec)
    c2, o2, e2, _ = fe(b, "i2c", "record", other)
    check("`i2c record FILE` begins one; another is refused while it runs, "
          "with the file it records to, and makes none",
          code == 0 and str(rec) in out and c2 == 1 and not o2
          and "already" in e2 and str(rec) in e2 and not other.exists(),
          out + err + o2 + e2)
    found, said = scan(b)
    time.sleep(0.3)
    _, growing, _, wrong = recorded(rec)        # the file while it records
    code, out, err, _ = fe(b, "i2c", "record")
    check("the firmware's scan: its 128 calls are in the file while the "
          "record runs, and `i2c record` has counted them, 2 answered",
          found == [0x48, 0x50] and not wrong and len(growing) == 128
          and code == 0 and "128 addresses called, 2 answered; 0 bytes "
          "written, 0 read" in out and str(rec) in out,
          said + wrong + out + err + f"\n{len(growing)} parts")
    code, out, err, _ = fe(b, "i2c", "record", "stop")
    devices, short, parts, wrong = recorded(rec)
    check("`i2c record stop`: says the file and what is in it, which is "
          "what the file has; the file says what was on the bus",
          code == 0 and str(rec) in out and not wrong and count(parts) in out
          and devices == [{"type": "tmp105", "address": 0x48},
                          {"type": "at24c-eeprom", "address": 0x50}],
          out + err + wrong + f"\n{count(parts)}\n{devices}")
    check("the record: every address from 0x00 to 0x7f, once and in that "
          "order, each called to write, with no byte and a STOP; 0x48 and "
          "0x50 answered, no other",
          short == [(at, "w", at in (0x48, 0x50), b"") for at in range(128)]
          and all(p["stop"] and p["whole"] for p in parts),
          wrong + "\n".join(map(str, short[:12])))
    times = [p["ns"] for p in parts]
    check("... each with its time, none before the one that it follows",
          times and all(a <= z for a, z in zip(times, times[1:]))
          and 0 <= times[0] and times[-1] < 120e9, repr(times[:12]))
    size = rec.stat().st_size if rec.exists() else 0

    # as it happens: of a scan, the one call to the EEPROM
    watch = b.bg("i2c", "watch", "0x50", "--for", 8)
    first = watch.stdout.readline()             # it watches now
    scan(b)
    out, lines = watched(watch)
    code, out2, err2, _ = fe(b, "i2c", "record")
    check("`i2c watch 0x50 --for 8`: of another scan the one call to that "
          "address, with the device's type; it ends by itself, and leaves "
          "no record running and no file",
          watch.returncode == 0 and "the calls to 0x50" in first
          and "tmp105 at 0x48, at24c-eeprom at 0x50" in first
          and lines == ["0x50 at24c-eeprom to write: answered, and no byte"]
          and "1 START in" in out and "not being recorded" in out2
          and not (b.dir / "i2c-watch.txt").exists(), first + out + out2 + err2)
    check("... and nothing was written to the record that had ended",
          size and rec.stat().st_size == size, f"{size}")
    # a script that has a watch in its background ends it with `kill`
    watch = b.bg("i2c", "watch")
    first = watch.stdout.readline()
    scan(b)
    time.sleep(1.0)
    watch.terminate()
    out, lines = watched(watch)
    code, out2, err2, _ = fe(b, "i2c", "record")
    check("`i2c watch` that is ended with `kill`, as a script ends what it "
          "began: a third scan's 128 calls and their count, exit 0, no "
          "record left running and no file",
          watch.returncode == 0 and "Ctrl+C ends it" in first
          and len(lines) == 128 and "128 STARTs in" in out
          and "not being recorded" in out2
          and not (b.dir / "i2c-watch.txt").exists(),
          f"{watch.returncode}\n{first}{out[-600:]}{out2}{err2}")

    for words, said, what in (
            (("record", rec, "more"), "a file, or `stop`",
             "two files to record to"),
            (("--json", "record"), "not with --json", "a record as JSON"),
            (("record", "/nowhere-fe/bus.txt"), "cannot write "
             "/nowhere-fe/bus.txt", "a file that cannot be written"),
            (("watch", "48"), "as 0x48", "an address that is not in hex"),
            (("watch", "0x48", "0x50"), "one address, or none",
             "two addresses to watch"),
            (("--for", "5"), "--for goes with watch", "--for with no watch"),
            (("watch", "--for", "0"), "more than 0 seconds",
             "a watch for no time")):
        code, out, err, _ = fe(b, "i2c", *words)
        check(f"`i2c`, {what}: exit 1, said in words",
              code == 1 and said in err and not out, f"{code}\n{out}{err}")
    code, out, err, _ = fe(b, "i2c", "record")
    check("... and none of that began a record",
          code == 0 and "not being recorded" in out, out + err)


def shifted_checks():
    """The word on an address that was not shifted: when it is said.  (The
    launcher's own doing, the same on every firmware; a firmware whose JS
    can call the bus is given such a script further on.)"""
    on = [{"type": "tmp105", "address": 0x48},
          {"type": "at24c-eeprom", "address": 0x51}]
    said = {at: fe_header.i2c_shifted(at, on) for at in range(128)}
    last = {"address": 0x24, "read": False, "taken": False}
    bus = {"devices": on, "last": last}
    check("a call to 0x24 that nobody takes while a sensor is at 0x48: a "
          "word on the shift, with the address to give the firmware (0x90); "
          "the same for 0x28 and a device at 0x51; for no other address, "
          "and not when the call was answered",
          sorted(at for at in said if said[at]) == [0x24, 0x28]
          and "nobody is at 0x24. If the tmp105 at 0x48 was meant" in said[0x24]
          and "0x48 << 1 (0x90)" in said[0x24]
          and "the at24c-eeprom at 0x51" in said[0x28]
          and "0x51 << 1 (0xa2)" in said[0x28]
          and fe_header.i2c_hint(bus) == said[0x24]
          and fe_header.i2c_hint(dict(bus, last=dict(last, taken=True)))
          is None and fe_header.i2c_hint(dict(bus, last=None)) is None,
          repr({at: said[at] for at in said if said[at]}))


def marks_checks():
    """The marks at a record's line: how the launcher reads such a line, and
    what a watch says of it.  (The launcher's own doing, on lines that are
    written here; the machine's writing of them is in rare_checks.)"""
    def read(line):
        try:
            part = fe_header.i2c_part(line)
        except ValueError:
            return "no line"
        return part and (part["address"], "wr"[part["read"]], part["taken"],
                         part["data"].hex(), part["refused"], part["stop"],
                         part["whole"], part["ns"],
                         fe_header.i2c_part_text(part))
    many = " ".join(f"{x:02x}" for x in range(20))
    got = [read(x) for x in (
        "0.000000100 0x48 w 00 +",
        "2.500000000 0x50 w 00 ff!",
        "3.000000007 0x50 r 01 02 ...",
        "4.000000000 0x24 r no answer",
        "5.000000000 0x50 w 00 ff! +",
        f"6.000000000 0x50 r {many}")]
    want = [
        (0x48, "w", True, "00", False, False, True, 100,
         "wrote 00 (no STOP: the next one follows at once)"),
        (0x50, "w", True, "00ff", True, True, True, 2500000000,
         "wrote 00 ff (the last one refused)"),
        (0x50, "r", True, "0102", False, True, False, 3000000007,
         "read 01 02 (the record ended there)"),
        (0x24, "r", False, "", False, True, True, 4000000000,
         "to read: no answer"),
        (0x50, "w", True, "00ff", True, False, True, 5000000000,
         "wrote 00 ff (the last one refused) (no STOP: the next one follows "
         "at once)"),
        (0x50, "r", True, many.replace(" ", ""), False, True, True,
         6000000000, "read " + many[:47] + " ... (20 bytes)")]
    none = [read(x) for x in ("", "# on the bus: nothing", "   ")]
    wrong = [read(x) for x in (
        "hello", "1.0 0x48 w", "1.000000000 48 w", "1.000000000 0x48 x",
        "1.000000000 0x48 r 17!", "1.000000000 0x24 w no answer 00",
        "1.000000000 0x48 w 0", "1.000000000 0x48")]
    check("a record's line with a mark (`+`: no STOP, `!`: a byte refused, "
          "`...`: the record ended there), written here: read "
          "as it is written and said in words, 20 bytes as 16 and their "
          "number; a comment is no part, and what is no line of a record "
          "is not taken for one",
          got == want and none == [None] * 3 and wrong == ["no line"] * 8,
          "\n".join(map(repr, got + none + wrong)))


def wrong_checks(name):
    """What `run --i2c` does not take: said before anything is done"""
    run = [sys.executable, str(ROOT / "flipperemu"), "-d", name, "run",
           "--headless", "--audio", "none"]
    for spec, words, what in WRONG + (
            (("tmp105@0x48", "tmp421@0x48"), "already", "two at one address"),):
        specs = [spec] if isinstance(spec, str) else list(spec)
        r = subprocess.run(run + [x for s in specs for x in ("--i2c", s)],
                           capture_output=True, text=True, timeout=60,
                           stdin=subprocess.DEVNULL)
        check(f"`run --i2c`, {what}: refused in words, and no device made",
              r.returncode == 1 and words in r.stderr
              and not (ROOT / "devices" / name).exists(),
              f"{r.returncode}\n{r.stdout}{r.stderr}")
    r = subprocess.run(run + ["--i2c", "help"], capture_output=True, text=True,
                       timeout=60, stdin=subprocess.DEVNULL)
    check("`run --i2c help`: the types there are, each with a word on what "
          "it is, and no device made",
          r.returncode == 0 and re.search(r"^  tmp105 +a temperature sensor",
                                          r.stdout, re.M)
          and re.search(r"^  at24c-eeprom +an EEPROM.*`eeprom`", r.stdout,
                        re.M)
          and not (ROOT / "devices" / name).exists(),
          f"{r.returncode}\n{r.stdout}{r.stderr}")


def bus_checks(b):
    """What is on the bus, as the launcher and as the firmware see it"""
    rom = b.dir / "i2c-50.bin"
    code, out, err, _ = fe(b, "i2c")
    check("`i2c`: the sensor at 0x48 with the temperature it started with, "
          "the EEPROM at 0x50 and the file of its contents",
          code == 0 and "pins 15 and 16 (I2C3)" in out
          and re.search(r"0x48 +tmp105 +temperature 23\.5 °C", out)
          and re.search(r"0x50 +at24c-eeprom .*kept in " + re.escape(str(rom)),
                        out) and "rom-size 256" in out, out + err)
    try:
        new = rom.read_bytes()
    except OSError as e:
        new = str(e)
    check("a new EEPROM's file: 256 bytes, every bit 1", new == b"\xff" * 256,
          repr(new)[:200])
    before = bus(b)
    found, said = scan(b)
    check("the firmware's scan of the bus finds 0x48 and 0x50, and nothing "
          "else", found == [0x48, 0x50], said)
    after = bus(b)
    calls = after.get("calls", 0) - before.get("calls", 0)
    taken = calls - (after.get("unanswered", 0) - before.get("unanswered", 0))
    check("the scan's calls are counted: 128 addresses, 2 answered, the last "
          "one not", (calls, taken) == (128, 2) and after.get("last")
          and not after["last"]["taken"], f"{before}\n{after}")
    code, out, err, _ = fe(b, "i2c")
    check("`i2c` says so in words", code == 0 and re.search(
        r"the firmware called \d+ addresses, \d+ answered; the last: "
        r"0x[0-9a-f]{2} to (read|write), no answer", out), out + err)
    code, out, err, _ = fe(b, "gpio")
    check("`gpio` has the bus, and what is on it",
          code == 0 and "its I2C bus, SDA on pin 15 and SCL on pin 16" in out
          and "tmp105 at 0x48, at24c-eeprom at 0x50" in out, out + err)
    code, out, err, _ = fe(b, "status")
    line = [x for x in out.splitlines() if x.startswith("i2c")]
    check("`status` has it too, and `status --json`",
          code == 0 and len(line) == 1
          and "tmp105 at 0x48, at24c-eeprom at 0x50" in line[0]
          and after.get("devices") == [{"type": "tmp105", "address": 0x48},
                                       {"type": "at24c-eeprom",
                                        "address": 0x50}]
          and (after.get("sda"), after.get("scl")) == (15, 16),
          f"{out}{err}\n{after}")


def value_checks(b):
    """A device's values from outside: read, set, refused"""
    code, out, err, _ = fe(b, "i2c", "0x48", "temperature")
    check("`i2c 0x48 temperature`: the value, bare",
          code == 0 and out.strip() == "23.5", out + err)
    code, out, err, _ = fe(b, "i2c", "0x48", "temperature", "30.25")
    c2, o2, e2, _ = fe(b, "i2c", "0x48")
    check("`i2c 0x48 temperature 30.25` sets it",
          (code, c2) == (0, 0) and "temperature 30.25 °C" in out
          and "temperature 30.25 °C" in o2, out + err + o2 + e2)
    for words, said, what in (
            (("0x48", "temperature", "200"), "out of range",
             "a temperature out of the sensor's range"),
            (("0x48", "temperature", "warm"), "in °C",
             "a temperature that is none"),
            (("0x49",), "nothing is at 0x49", "an address with nothing at it"),
            (("0x48", "humidity", "40"), "has no 'humidity' (it has: "
             "temperature)", "a value the device has not"),
            (("0x50", "rom-size", "512"), "stays as it is while it runs",
             "a value that is fixed while it runs"),
            (("48",), "as 0x48", "an address that is not in hex")):
        code, out, err, _ = fe(b, "i2c", *words)
        check(f"`i2c`, {what}: exit 1, said in words",
              code == 1 and said in err and not out, f"{code}\n{out}{err}")
    code, out, err, _ = fe(b, "i2c", "0x48", "temperature")
    check("... and the temperature is as it was set",
          code == 0 and out.strip() == "30.25", out + err)
    code, out, err, _ = fe(b, "i2c", "--json")
    c2, o2, e2, _ = fe(b, "i2c", "--json", "0x50")
    c3, o3, e3, _ = fe(b, "i2c", "--json", "0x48", "temperature")
    try:
        whole, one = json.loads(out), json.loads(o2)
    except ValueError:
        whole, one = {}, {}
    devices = whole.get("devices") or []
    check("`i2c --json`: the bus for a program, each device with its values "
          "(a temperature in °C, an EEPROM's file); with an address, that "
          "device alone; not with a value's name, which comes bare",
          (code, c2) == (0, 0)
          and (whole.get("name"), whole.get("sda"), whole.get("scl"))
          == ("I2C3", 15, 16) and whole.get("calls", 0) >= 128
          and devices == [{"type": "tmp105", "address": 0x48,
                           "values": {"temperature": 30.25}}, one]
          and one.get("type") == "at24c-eeprom"
          and one.get("file") == str(b.dir / "i2c-50.bin")
          and one.get("values", {}).get("rom-size") == 256
          and c3 == 1 and "comes bare" in e3 and not o3,
          out + err + o2 + e2 + o3 + e3)


def script_checks(b, timeout):
    """The firmware reads the sensor and writes the EEPROM (its JS).  ->
    the runs the EEPROM has counted; None: this firmware's JS has no i2c"""
    rom = b.dir / "i2c-50.bin"
    code, out, err, _ = fe(b, "push", SENSOR, "/ext")
    (t, n), said = sensor(b)
    if code == 0 and "sensor done" not in said and "i2c" in said \
            and re.search(r"load fail|not found|no such", said, re.I):
        skip("a script reads the sensor and counts in the EEPROM",
             "this firmware's JS has no i2c module (" + " / ".join(
                 x.strip() for x in said.splitlines() if "i2c" in x)[:160]
             + "): only its scan was tried on the bus")
        return None
    check("a script on the Flipper reads the temperature that was set, and "
          "the EEPROM counts its first run",
          code == 0 and (t, n) == (30.25, 1) and "sensor done" in said,
          out + err + said)
    code, out, err, _ = fe(b, "i2c", "0x48", "temperature", "-7.5")
    rec = b.dir / "script.txt"
    fe(b, "i2c", "record", rec)
    (t, n), said = sensor(b)
    c2, o2, e2, _ = fe(b, "i2c", "record", "stop")
    check("another temperature from outside, below zero: the script reads "
          "it; its second run", code == 0 and (t, n) == (-7.5, 2),
          out + err + said)
    _, short, parts, wrong = recorded(rec)
    check("the record of that run: the sensor asked for (no byte), set to "
          "12 bits (01 60), its register 0 named and read (f8 80, which is "
          "-7.5); the EEPROM asked for, its byte 0 read (01) and written "
          "(00 02); and the record's own count is the file's",
          c2 == 0 and not wrong and short == [
              (0x48, "w", True, b""), (0x48, "w", True, b"\x01\x60"),
              (0x48, "w", True, b"\x00"), (0x48, "r", True, b"\xf8\x80"),
              (0x50, "w", True, b""), (0x50, "w", True, b"\x00"),
              (0x50, "r", True, b"\x01"), (0x50, "w", True, b"\x00\x02")]
          and count(parts) in o2 and "8 addresses called, all answered; 6 "
          "bytes written, 3 read" in o2,
          o2 + e2 + wrong + "\n".join(map(str, short)))
    kept = rom.read_bytes()
    check("the EEPROM's file has the count while the device runs, and "
          "nothing else changed",
          kept == b"\x02" + b"\xff" * 255, kept[:16].hex(" "))
    code, out, err, _ = fe(b, "reset")
    c2, o2, e2, _ = fe(b, "wait", "--timeout", timeout)
    watch = b.bg("i2c", "watch")
    first = watch.stdout.readline()             # it watches now
    (t, n), said = sensor(b)
    check("after a reset: the third run, and the temperature the world has",
          (code, c2) == (0, 0) and (t, n) == (-7.5, 3),
          out + err + o2 + e2 + said)
    # a script that gives the firmware the sensor's address as it is
    plain = b.dir / "plain.js"
    plain.write_text('let i2c = require("i2c");\n'
                     'print("there:", i2c.isDeviceReady(0x48, 50));\n')
    fe(b, "push", plain, "/ext")
    code, said, err, _ = fe(b, "cli", "--timeout", 60, "js /ext/plain.js")
    time.sleep(0.3)
    watch.send_signal(signal.SIGINT)            # as Ctrl+C does
    out, lines = watched(watch)
    word = ("nobody is at 0x24. If the tmp105 at 0x48 was meant: the "
            "firmware takes an address shifted left by one bit, 0x48 << 1 "
            "(0x90)")
    check("`i2c watch` meanwhile: that run as it happened, a line for each "
          "START with the device's type and the bytes; Ctrl+C ends it, and "
          "it leaves no record running",
          watch.returncode == 0 and "the header's I2C bus as the firmware "
          "talks there" in first and "Ctrl+C ends it" in first
          and lines[:8] == [
              "0x48 tmp105 to write: answered, and no byte",
              "0x48 tmp105 wrote 01 60", "0x48 tmp105 wrote 00",
              "0x48 tmp105 read f8 80",
              "0x50 at24c-eeprom to write: answered, and no byte",
              "0x50 at24c-eeprom wrote 00", "0x50 at24c-eeprom read 02",
              "0x50 at24c-eeprom wrote 00 03"]
          and "not being recorded" in fe(b, "i2c", "record")[1]
          and not (b.dir / "i2c-watch.txt").exists(), first + out)
    after = [x.strip() for x in out.splitlines()]
    check("a script that gives the firmware 0x48 as it is: nobody answers "
          "(it called 0x24), and the watch has that call with a word on "
          "the shift under it",
          code == 0 and "there: false" in said
          and lines[8:] == ["0x24 to write: no answer"]
          and after.count(word) == 1 and "9 STARTs in" in out,
          said + err + out)
    code, out, err, _ = fe(b, "i2c")
    check("... and `i2c` has it for the last call",
          code == 0 and "the last: 0x24 to write, no answer" in out
          and f"({word})" in out, out + err)
    return 3


def app_checks(b, firmware, runs):
    """The firmware reads the sensor and writes the EEPROM from an app in C,
    which ufbt builds.  -> the runs the EEPROM has counted then; None: no
    ufbt, or its SDK is for another firmware"""
    label = "an app in C reads the sensor and counts in the EEPROM"
    if not shutil.which("ufbt"):
        skip(label, "no `ufbt` on the PATH (`pipx install ufbt` gets it)")
        return None
    rom = b.dir / "i2c-50.bin"
    work = pathlib.Path(tempfile.mkdtemp(prefix="fe-i2c-"))
    own = b.name + "-t"
    try:
        folder = work / "fe_sensor"
        folder.mkdir()
        for name in ("application.fam", "sensor.c"):
            shutil.copy(EXAMPLE / name, folder)
        code, text = tool(b, folder)
        fap = folder / "dist" / "fe_sensor.fap"
        if code or not fap.is_file():
            check("`ufbt` builds the example's app", False, text[-1500:])
            return None
        code, out, err, _ = fe(b, "app", "--no-start", "--to",
                               "/ext/apps/fe-check", fap)
        fe(b, "rm", "-r", "/ext/apps/fe-check")
        if "which is this firmware's" not in out:
            skip(label, "ufbt's SDK is not for this firmware: "
                 + " ".join((out + err).split())[:200])
            return None

        now = (runs or 0) + 1
        rec = b.dir / "app.txt"
        fe(b, "i2c", "0x48", "temperature", "21.25")
        fe(b, "i2c", "record", rec)
        code, text = tool(b, folder, "launch")
        check("`ufbt launch`: the app is built, put on the Flipper and "
              "started; it logs the run that the EEPROM counts, and the "
              "temperature that was set",
              code == 0 and logged(b, f"[FeSensor] started, run number {now} "
                                      "by the EEPROM at 0x50")
              and logged(b, "[FeSensor] 21.25 C at 0x48"),
              text[-1500:] + log_tail(b))
        code, out, err, _ = fe(b, "wait", "--timeout", 10, "--log",
                               r"FeSensor.* -0\.50 C at 0x48", "--", "i2c",
                               "0x48", "temperature", "-0.5")
        check("another temperature from outside while it runs, just below "
              "zero: its next reading has it, within the second",
              code == 0 and "-0.50 C" in out, out + err + log_tail(b))
        code, out, err, _ = fe(b, "app", "--close")
        check("closed: it says so in the log, and the EEPROM's file has the "
              "count", code == 0 and logged(b, "[FeSensor] left")
              and rom.read_bytes()[:2] == bytes([now, 255]),
              out + err + log_tail(b) + rom.read_bytes()[:16].hex(" "))
        code, out, err, _ = fe(b, "i2c", "record", "stop")
        _, short, parts, wrong = recorded(rec)
        reads = [p[3] for p in short if p[:2] == (0x48, "r")]
        check("the record of the app's run: the EEPROM's byte 0 read and "
              "the run's number written there (00 %02x), the sensor set to "
              "12 bits (01 60), and both temperatures read byte for byte "
              "(15 40, then ff 80); every call answered" % now,
              code == 0 and not wrong and count(parts) in out
              and (0x50, "r", True, bytes([now - 1 or 255])) in short
              and (0x50, "w", True, bytes([0, now])) in short
              and (0x48, "w", True, b"\x01\x60") in short
              and reads and reads[0] == b"\x15\x40"
              and reads[-1] == b"\xff\x80" and set(reads) ==
              {b"\x15\x40", b"\xff\x80"} and all(p[2] for p in short),
              out + err + wrong + "\n".join(map(str, short[:40])))

        began = time.time()
        try:
            r = subprocess.run(["./test.sh", os.path.abspath(firmware), own],
                               cwd=EXAMPLE, capture_output=True, text=True,
                               timeout=600, stdin=subprocess.DEVNULL)
            code, text = r.returncode, (r.stdout + r.stderr).replace("\r", "\n")
        except subprocess.TimeoutExpired as e:
            code, text = None, str(e)
        check("the example's own test (examples/i2c/test.sh) on a Flipper of "
              f"its own: it passes ({time.time() - began:.0f} s)",
              code == 0 and "the app reads what the sensor has" in text
              and text.count("its screen is as in screens/") == 2,
              text[-3000:])
        return now
    finally:
        subprocess.run([sys.executable, str(ROOT / "flipperemu"), "-d", own,
                        "stop"], capture_output=True)
        shutil.rmtree(str(b.dir) + "-t", ignore_errors=True)
        shutil.rmtree(work, ignore_errors=True)


def again_checks(b, timeout, runs, script, app):
    """A stop, and a start without the sensor: the EEPROM, and QEMU's echo
    device for rare_checks.  `runs`: what the EEPROM has counted (None:
    nothing wrote to it); script, app: what there is to read it with.
    -> the device runs"""
    rom = b.dir / "i2c-50.bin"
    # a record that nobody ends: over a reset, and until the device stops
    left = b.dir / "left.txt"
    fe(b, "i2c", "record", left)
    code, out, err, _ = fe(b, "reset")
    c2, o2, e2, _ = fe(b, "wait", "--timeout", timeout)
    found, said = scan(b)
    c3, o3, e3, _ = fe(b, "i2c", "record")
    check("a record goes on over a reset of the firmware: the scan after it "
          "is counted",
          (code, c2, c3) == (0, 0, 0) and found == [0x48, 0x50]
          and f"recording the header's I2C bus to {left}: 128 addresses "
          "called, 2 answered" in o3, out + err + o2 + e2 + said + o3 + e3)
    b.stop()
    _, short, parts, wrong = recorded(left)
    check("a record that runs as the device is stopped: its file is whole, "
          "the scan's 128 calls and no line cut",
          not wrong and [p[0] for p in short] == list(range(128))
          and all(p["whole"] and p["stop"] for p in parts)
          and left.read_bytes().endswith(b"0x7f w no answer\n"),
          wrong + left.read_text(errors="replace")[-400:]
          if left.exists() else "no file")
    kept = rom.read_bytes() if rom.exists() else b""
    code, out, err, _ = fe(b, "run", "--headless", "--audio", "none", "--i2c",
                           "eeprom@0x50,rom-size=512", timeout=60)
    c2, o2, e2, _ = fe(b, "i2c")
    check("an EEPROM of another size while the file is there: refused in "
          "words, nothing started, the file as it was",
          code == 1 and "256 bytes" in err and "rom-size=256" in err
          and c2 == 1 and "not running" in e2 and rom.read_bytes() == kept,
          f"{code}\n{out}{err}\n{o2}{e2}")
    ok, said = start(b, timeout, "--i2c", "eeprom@0x50", "--i2c",
                     "i2c-echo@0x42")
    if not check("a start without the sensor: the EEPROM, and QEMU's device "
                 "for tests (i2c-echo) at 0x42", ok,
                 said + b.uart_text()[-2000:]):
        return False
    found, said = scan(b)
    code, out, err, _ = fe(b, "i2c")
    check("the scan finds those two, and `i2c` has no sensor",
          found == [0x42, 0x50] and code == 0 and "tmp105" not in out
          and "i2c-echo at 0x42, at24c-eeprom at 0x50" in out,
          said + out + err)
    if runs is None:
        check("the EEPROM's contents are as they were",
              rom.read_bytes() == kept and len(kept) == 256, kept[:16].hex(" "))
        return True
    if script:
        runs += 1
        (t, n), said = sensor(b)
        check("the script finds no sensor, and the EEPROM kept its count "
              "over the stop", t is None and "no sensor at 0x48" in said
              and n == runs and rom.read_bytes()[:2] == bytes([runs, 255]),
              said + rom.read_bytes()[:16].hex(" "))
    if app:
        runs += 1
        code, out, err, _ = fe(b, "app", APP)
        check("the app, started from the card, finds no sensor, and the "
              "EEPROM kept its count over the stop",
              code == 0 and logged(b, "[FeSensor] no sensor at 0x48")
              and logged(b, f"[FeSensor] started, run number {runs} by")
              and rom.read_bytes()[:2] == bytes([runs, 255]),
              out + err + log_tail(b) + rom.read_bytes()[:16].hex(" "))
        fe(b, "app", "--close")
    return True


def texts(parts):
    """What a watch says of each part (None: a part that the record has not)"""
    return [fe_header.i2c_part_text(p) if "data" in p else None
            for p in parts]


def rare_checks(b, script, app):
    """What neither example does on the bus, with QEMU's echo device at 0x42:
    it keeps three bytes, gives them back to a read, and refuses a fourth.
    script: the firmware's JS has an i2c module; app: ufbt's SDK is for this
    firmware.  The machine's writing of a record's marks, which marks_checks
    has the launcher's reading of"""
    if not script and not app:
        skip("a byte that a device refuses, and a START with no STOP before "
             "it", "this firmware's JS has no i2c module, and ufbt's SDK is "
             "not for it: nothing here makes it do either")
        return
    if script:
        js = b.dir / "echo.js"
        js.write_text(ECHO_JS)
        rec = b.dir / "echo.txt"
        fe(b, "push", js, "/ext")
        fe(b, "i2c", "record", rec)
        code, said, err, _ = fe(b, "cli", "--timeout", 60, "js /ext/echo.js")
        c2, o2, e2, _ = fe(b, "i2c", "record", "stop")
        _, short, parts, wrong = recorded(rec)
        check("a script writes four bytes to a device that keeps three: the "
              "record marks the fourth as refused, and the firmware's call "
              "says that it went well all the same; of five, the fourth is "
              "refused and the fifth not sent, and that call says so; the "
              "three that the device kept are read back",
              code == 0 and c2 == 0 and not wrong and "four: true" in said
              and "five: false" in said and "back: 17 18 19" in said
              and short == [(0x42, "w", True, b"\x01\x02\x03\x04"),
                            (0x42, "w", True, b"\x11\x12\x13\x14"),
                            (0x42, "r", True, b"\x11\x12\x13")]
              and [p["refused"] for p in parts] == [True, True, False]
              and all(p["stop"] and p["whole"] for p in parts)
              and texts(parts)[:2] == [
                  "wrote 01 02 03 04 (the last one refused)",
                  "wrote 11 12 13 14 (the last one refused)"]
              and count(parts) in o2 and "3 addresses called, all answered; "
              "6 bytes written, 3 read" in o2,
              said + err + o2 + e2 + wrong + "\n".join(map(str, short)))
    if not app:
        return

    rom = b.dir / "i2c-50.bin"
    rec = b.dir / "bus.txt"
    work = pathlib.Path(tempfile.mkdtemp(prefix="fe-i2c-"))
    try:
        folder = work / "fe_bus_check"
        folder.mkdir()
        for name in ("application.fam", "bus.c"):
            shutil.copy(OWN / name, folder)
        fe(b, "i2c", "record", rec)
        code, text = tool(b, folder, "launch")
        # its last transfer is open for seconds: the record ends inside it
        held = logged(b, "[FeBus] 8 written 1, and the bus is held", 30)
        c2, o2, e2, _ = fe(b, "i2c", "record", "stop")
        ended = logged(b, "[FeBus] done", 30)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    said = [line.split("[FeBus] ", 1)[1].strip()
            for line in ANSI.sub("", b.uart_text()).splitlines()
            if "[FeBus] " in line]
    _, short, parts, wrong = recorded(rec)
    lines, counted = len(short), count(parts)
    short += [None] * (15 - len(short))
    parts += [{"stop": None, "whole": None, "refused": None}] \
        * (15 - len(parts))
    said += [""] * (10 - len(said))
    told = "\n".join(said) + "\n" + o2 + e2 + wrong + "\n" + "\n".join(
        f"{p} {q.get('stop')} {q.get('whole')}"[:90]
        for p, q in zip(short, parts))
    kept = rom.read_bytes()
    long = (kept * 2)[:300]
    echo = [None, (0x42, "w", True), (0x42, "r", True)]

    def line(n, how, data, stop=True, refused=False, whole=True):
        return short[n] == echo[how][:3] + (data,) \
            and (parts[n]["stop"], parts[n]["refused"], parts[n]["whole"]) \
            == (stop, refused, whole)

    check("an app in C of the check's own (scripts/i2c_check_app), by `ufbt "
          "launch`: it runs to its end and logs what each of its calls said",
          code == 0 and held and ended and said[9] == "done"
          and len(said) == 10 and not wrong, text[-1500:] + told)
    check("three bytes written and read after a START with no STOP before "
          "it (furi_hal_i2c_tx_ext, furi_hal_i2c_rx_ext): the record has "
          "the write with a `+` and the read on the line after it, and the "
          "app got its bytes",
          said[0] == "1 written 1, read after a START again 1: a1 b2 c3"
          and line(0, 1, b"\xa1\xb2\xc3", stop=False)
          and line(1, 2, b"\xa1\xb2\xc3")
          and texts(parts[:1]) == ["wrote a1 b2 c3 (no STOP: the next one "
                                   "follows at once)"], told)
    check("a fourth byte, which the device refuses: marked in the record, "
          "and the firmware's call says that it went well; of five the "
          "fifth is not sent, and that call says that it did not",
          said[1:3] == ["2 four bytes written 1", "3 five bytes written 0"]
          and line(2, 1, b"\x01\x02\x03\x04", refused=True)
          and line(3, 1, b"\x11\x12\x13\x14", refused=True), told)
    check("a START with no STOP before it for another address: the EEPROM "
          "answers there with its own bytes, and not the device that had "
          "the bus; and where nobody is the call fails, with `no answer` "
          "in the record",
          said[3] == "4 EEPROM set 1, echo written 1, EEPROM read after a "
          "START again 1: e1 e2"
          and said[4] == "5 echo written 1, nobody read after a START again 0"
          and short[4:6] == [(0x50, "w", True, b"\x10\xe1\xe2"),
                             (0x50, "w", True, b"\x10")]
          and line(6, 1, b"\x21", stop=False)
          and short[7] == (0x50, "r", True, b"\xe1\xe2") and parts[7]["stop"]
          and line(8, 1, b"\x31", stop=False)
          and short[9] == (0x33, "r", False, b"") and parts[9]["stop"]
          and kept[16:18] == b"\xe1\xe2", told + kept[:32].hex(" "))
    check("300 bytes in one read, more than the peripheral counts at once: "
          "one line of the record, the EEPROM's 256 and its first 44 "
          "again; a write that is held and gone on with (no STOP, and no "
          "START): one line of its three bytes",
          said[5] == f"6 300 bytes read 1: their sum {sum(long)}, byte 16 "
          "e1, byte 273 e2"
          and short[10:12] == [(0x50, "w", True, b"\x00"),
                               (0x50, "r", True, long)]
          and said[6] == "7 two bytes written and held 1, a third 1, read "
          "1: 41 42 43"
          and line(12, 1, b"\x41\x42\x43") and line(13, 2, b"\x41\x42\x43"),
          told)
    found, scanned = scan(b)
    check("the record ended inside a transfer (a byte written, and the "
          "START that reads it seconds away): its last line says so with "
          "`...`, the count is the file's, and the app's read after it "
          "went well; the firmware's scan then finds the two devices",
          said[7:9] == ["8 written 1, and the bus is held for 4 s",
                        "8 read after a START again 1: 51"]
          and line(14, 1, b"\x51", whole=False) and lines == 15
          and rec.read_text().endswith(" 0x42 w 51 ...\n")
          and texts(parts[14:]) == ["wrote 51 (the record ended there)"]
          and c2 == 0 and counted in o2 and "15 addresses called, 14 "
          "answered; 20 bytes written, 308 read" in o2
          and found == [0x42, 0x50], told + scanned)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("firmware")
    ap.add_argument("--timeout", type=float, default=120.0,
                    help="seconds to wait for the firmware to start")
    ap.add_argument("--keep", action="store_true",
                    help="keep the throwaway device")
    args = ap.parse_args()

    b = Bench(f"i2ccheck-{int(time.time())}-{os.getpid()}", args.firmware,
              args.timeout)
    try:
        code, out, err, took = fe(b, "i2c")
        check("a device that is not running: says so, exit 1",
              code == 1 and "not running" in err, out + err)
        wrong_checks(b.name)
        b.dir.mkdir(parents=True, exist_ok=True)
        ok, said = start(b, args.timeout, "-f", args.firmware, "--i2c",
                         "tmp105@0x48,temperature=23.5", "--i2c", "eeprom@0x50")
        if ok:
            ok, said = b.answers()
        if check("the firmware starts with a sensor and an EEPROM on the "
                 "bus, and its CLI answers", ok,
                 said + b.uart_text()[-2000:]):
            bus_checks(b)
            value_checks(b)
            record_checks(b)
            shifted_checks()
            marks_checks()
            runs = script_checks(b, args.timeout)
            ran = app_checks(b, args.firmware, runs)
            if again_checks(b, args.timeout, ran or runs, runs is not None,
                            ran is not None):
                rare_checks(b, runs is not None, ran is not None)
            ok, said = b.answers()
            check("the CLI still answers", ok, said)
            log = b.uart_text(every=True)
            crash = CRASH.search(log)
            check("no crash in the firmware's log", not crash,
                  crash and log[max(0, crash.start() - 300):crash.end() + 300])
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
