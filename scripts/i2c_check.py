#!/usr/bin/env python3
# Copyright (c) 2026, Kara Zajac
# SPDX-License-Identifier: BSD-3-Clause
"""Check the I2C bus of the header, pins 15 (SDA) and 16 (SCL): devices that
`run --i2c` puts there, and the launcher's `i2c`.

    scripts/i2c_check.py firmware/momentum-mntm-012/flipper-z-f7-update-mntm-012.tgz

A `run --i2c` that is said wrongly is refused in words before anything is
done.  Then a throwaway device starts with a temperature sensor at 0x48 (at
23.5 degrees) and an EEPROM at 0x50: `i2c` names them with their values, the
EEPROM's contents are a file of the device, all bits 1.  The firmware's own
scan of the bus (its CLI's `i2c`) finds the two and nothing else, and the
launcher has counted its calls; `gpio`, `status` and `status --json` say what
is on the bus.  A value is read bare and set from outside, and what cannot be
is refused: a temperature out of the sensor's range, an address with nothing
at it, a value the device has not, one that is fixed while it runs.

Where the firmware's JS has an i2c module (Momentum, Unleashed, RogueMaster;
the official firmware's has none, and that part is skipped), the firmware runs
examples/i2c/sensor.js: it reads the temperature that was set, and again
after another was; the EEPROM counts its runs, in the device's file as soon
as written, and on after a reset.  After a stop an EEPROM of another size is
refused while the file is there, and a start with the EEPROM alone finds its
contents kept, and no sensor.  Exit status 0 = pass.
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time

from files_check import ROOT, Bench, CRASH, check, results
from uart_check import fe, start

SENSOR = ROOT / "examples" / "i2c" / "sensor.js"
WRONG = (
    ("tmp105", "TYPE@ADDRESS", "a device without an address"),
    ("tmp105@72", "as 0x48", "an address that is not in hex"),
    ("tmp105@0x03", "0x08 to 0x77", "an address that is not a device's"),
    ("tmp105@0x48,bus=i2c1", "not to be set", "a bus of one's own choice"),
    ("tmp105@0x48,temperature=warm", "in °C", "a temperature that is none"),
    ("eeprom@0x50,rom-size=0", "1 to 1048576", "an EEPROM of no bytes"),
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
    (t, n), said = sensor(b)
    check("another temperature from outside, below zero: the script reads "
          "it; its second run", code == 0 and (t, n) == (-7.5, 2),
          out + err + said)
    kept = rom.read_bytes()
    check("the EEPROM's file has the count while the device runs, and "
          "nothing else changed",
          kept == b"\x02" + b"\xff" * 255, kept[:16].hex(" "))
    code, out, err, _ = fe(b, "reset")
    c2, o2, e2, _ = fe(b, "wait", "--timeout", timeout)
    (t, n), said = sensor(b)
    check("after a reset: the third run, and the temperature the world has",
          (code, c2) == (0, 0) and (t, n) == (-7.5, 3),
          out + err + o2 + e2 + said)
    return 3


def again_checks(b, timeout, runs):
    """A stop, and a start with the EEPROM alone"""
    rom = b.dir / "i2c-50.bin"
    b.stop()
    kept = rom.read_bytes() if rom.exists() else b""
    code, out, err, _ = fe(b, "run", "--headless", "--audio", "none", "--i2c",
                           "eeprom@0x50,rom-size=512", timeout=60)
    c2, o2, e2, _ = fe(b, "i2c")
    check("an EEPROM of another size while the file is there: refused in "
          "words, nothing started, the file as it was",
          code == 1 and "256 bytes" in err and "rom-size=256" in err
          and c2 == 1 and "not running" in e2 and rom.read_bytes() == kept,
          f"{code}\n{out}{err}\n{o2}{e2}")
    ok, said = start(b, timeout, "--i2c", "eeprom@0x50")
    if not check("a start with the EEPROM alone", ok,
                 said + b.uart_text()[-2000:]):
        return
    found, said = scan(b)
    code, out, err, _ = fe(b, "i2c")
    check("the scan finds 0x50 alone, and `i2c` has no sensor",
          found == [0x50] and code == 0 and "tmp105" not in out
          and "at24c-eeprom at 0x50" in out, said + out + err)
    if runs is None:
        check("the EEPROM's contents are as they were",
              rom.read_bytes() == kept and len(kept) == 256, kept[:16].hex(" "))
        return
    (t, n), said = sensor(b)
    check("the script finds no sensor, and the EEPROM kept its count over "
          "the stop", t is None and "no sensor at 0x48" in said
          and n == runs + 1 and rom.read_bytes()[:2] == bytes([runs + 1, 255]),
          said + rom.read_bytes()[:16].hex(" "))


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
            runs = script_checks(b, args.timeout)
            again_checks(b, args.timeout, runs)
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
