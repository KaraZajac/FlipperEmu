#!/usr/bin/env python3
# Copyright (c) 2026, Kara Zajac
# SPDX-License-Identifier: BSD-3-Clause
"""Check the SPI bus of the header, pins 2 (MOSI), 3 (MISO) and 5 (SCK):
devices that `run --spi` puts there, each with a pin of the header as its
chip select, and the launcher's `spi`.

    scripts/spi_check.py firmware/official-1.5.1/flipper-z-f7-update-1.5.1.tgz

A `run --spi` that is said wrongly is refused in words before anything is
done: a pin of the bus as a chip select, two devices on one pin, one of the
Flipper's own chips, a type that QEMU has not; and `run --spi help` lists
those it has.  Then a throwaway device starts with two serial flashes, a
w25q32 with its chip select on pin 4 (where the firmware's own handle for the
header has it) and a w25q80bl on pin 7: `spi` names them, `spi --json`,
`gpio`, `status` and `status --json` have them, nobody is selected and
nothing was clocked, and each flash's contents are a file of the device, as
large as the part, every bit 1.

Where the firmware's JS has an spi module (Momentum, Unleashed, RogueMaster;
skipped otherwise), a script, examples/spi/flash.js, reads the flash's JEDEC
ID and Winbond's older one, erases a sector, writes seven bytes there and
reads them back.  The launcher has counted its bytes and has how the last one
was clocked, and the seven are in the flash's file while the device runs.

Where `ufbt` is on the PATH with an SDK for the firmware that is given (the
official release, as ufbt comes; skipped otherwise), an app in C of the
check's own (scripts/spi_check_app) does the same by the firmware's handle,
and then what that handle does not, a step whenever the check holds pin 6
the other way: a chip select on pin 7 with the other clock mode and speed,
the least significant bit first, frames of 16 bits; a pin of the three that
is not the bus's (no clock: nothing reaches the header; no MISO: nobody hears
the answer; MOSI as a plain output: the device gets the pin's level); bytes
with nobody selected, which read as MISO's pull.  While the app holds the bus
in the middle of a read, the table of `gpio` has the three pins as the bus's
and pin 4 as the chip select that selects now.  A chip select that is held
low from outside selects as one that is wired to GND does, and MISO held high
from outside is what the firmware reads when nobody answers.  The counts of
bytes that a device heard, and of those that none did, are right at every
step, and the flash's file has what was written and nothing else.  A reset of
the firmware in the middle of a read leaves nobody selected, and the app runs
again after it.

After a stop a flash of another size is refused while the file is there.  A
second start, with the flash on pin 4 alone, finds its contents kept: the
script and the app read what they wrote before, and the app's call to pin 7,
where nothing is now, is clocked with nobody selected.
Exit status 0 = pass.
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
from gpio_check import app_lines, hold, own_app, row, skip, skipped, status
from uart_check import fe, start
from ufbt_check import log_tail, tool

OWN = ROOT / "scripts" / "spi_check_app"
TAG = "FeSpi"
FAP = "/ext/apps/Debug/fe_spi_check.fap"        # where `ufbt launch` puts it
MIB = 1 << 20
SIZES = {4: 4 * MIB, 7: MIB}                    # a w25q32, a w25q80bl
ON = ("w25q32 with its chip select on pin 4, w25q80bl with its chip select "
      "on pin 7")
NOTHING = "the firmware has clocked nothing there yet"
BLANK = " ".join(["ff"] * 8)
# the flash on pin 4 by the firmware's JS: its IDs, then a sector erased and
# seven bytes written at 0x1000
EXAMPLE = ROOT / "examples" / "spi" / "flash.js"
JS_BYTES = 63           # what that script clocks, all of it to the flash
JS_PAGE = (0x1000, b"Flipper")
# what the app writes: a page, and three bytes with MOSI as a plain output
APP_PAGES = ((0x2000, b"Flipper!"), (0x3000, b"\x00\xff\x00"))
WRONG = (
    ("w25q32@", "a device is TYPE@PIN", "an @ with no pin after it"),
    ("@4", "a device is TYPE@PIN", "a pin with no type"),
    ("w25q32@PA7", "'PA7' is no pin for a chip select. Those are 4 (PA4), "
     "6 (PB2), 7 (PC3), 10 (PA14), 12 (PA13), 15 (PC1) and 16 (PC0)",
     "a pin of the bus itself as a chip select"),
    ("w25q32@9", "2, 3 and 5 are the bus itself",
     "a pin of the header that is no signal's"),
    ("w25q32@PA4,cs=1", "'cs' is not to be set",
     "a chip select as a property"),
    ("w25q32@PA4,bus=spi1", "'bus' is not to be set",
     "a bus of one's own choice"),
    ("w25q32@A4,id=x", "'id' is not to be set", "a name of one's own choice"),
    ("w25q32@4,drive", "PROPERTY=VALUE, not 'drive'",
     "a property with no value"),
    ("cc1101@4", "cc1101 is no device for the header here",
     "one of the Flipper's own chips"),
    ("w25q33@4", "QEMU has no SPI device 'w25q33' (`run --spi help` lists "
     "those it has", "a type that QEMU has not"),
    (("flash", "w25q80bl@PA4"), "w25q32 has its chip select on pin 4 "
     "already", "two with one chip select"),
)
# the app's waits after its first: how pin 6 is to be held, and the line it
# then logs before the next wait (or at its end)
STEPS = (("high", "5 done"), ("low", "6 done"), ("high", "11 the bus is held"),
         ("low", "12 done"), ("high", "done"))
RUN = ["1 JEDEC ID: ef 40 16", "2 90h: ef 15 ef 15", "2 90h from 1: 15 ef",
       "2 ABh: 15 15", f"3 erased: {BLANK}",
       "3 written: 46 6c 69 70 70 65 72 21"]


def bus(b):
    """What `status --json` says of the header's SPI bus"""
    code, out, err, _ = fe(b, "status", "--json")
    try:
        return json.loads(out).get("spi") or {}
    except ValueError:
        return {}


def count(st, since=(0, 0)):
    """The bytes that the firmware clocked there: (with a device selected,
    with none), since an earlier count"""
    return (st.get("heard", 0) - since[0], st.get("unheard", 0) - since[1])


def frame(hz, mode=0, lsb=False, bits=8):
    """How a frame was clocked, as `status --json` has it"""
    return {"hz": hz, "mode": mode, "lsb_first": lsb, "bits": bits}


def selected(st):
    """The pins of the chip selects that select now"""
    return [d["select"] for d in st.get("devices", ()) if d["selected"]]


def said_count(st):
    """What `spi` says of that count"""
    heard, unheard = count(st)
    return (f"the firmware clocked {heard + unheard} bytes there, "
            + (f"{heard} of them" if unheard else "all")
            + " with a device selected")


def flash(b):
    """The firmware runs the script: {a word of its: the bytes it printed
    after it}, and all that was said"""
    code, out, err, _ = fe(b, "cli", "--timeout", 60, "js /ext/flash.js")
    return {m[1]: m[2].strip() for m in
            re.finditer(r"^(\w+): *((?:[0-9a-f]{2} ?)*)", out, re.M)}, out + err


def lines(b, before, last, seconds=30.0):
    """The app's lines after its first `before`, once `last` is among them"""
    end = time.time() + seconds
    while True:
        new = app_lines(b, TAG)[before:]
        if last in new or time.time() > end:
            return new
        time.sleep(0.2)


def walk(b, last="done"):
    """From the app's first wait on: hold pin 6 as its steps want it, up to
    the one that logs `last`.  -> the app's lines since"""
    before = len(app_lines(b, TAG))
    for level, line in STEPS:
        hold(b, "PB2", level, line=line, tag=TAG)
        if line == last:
            break
    return app_lines(b, TAG)[before:]


def with_pages(data, pages):
    """A flash's contents with those bytes at those places"""
    data = bytearray(data)
    for at, page in pages:
        data[at:at + len(page)] = page
    return bytes(data)


def differ(a, b):
    """Where two flashes' contents differ first, for a person"""
    if len(a) != len(b):
        return f"{len(a)} bytes, not {len(b)}"
    at = next((i for i in range(len(a)) if a[i] != b[i]), None)
    return "the same" if at is None else (
        f"at {at:#x}: {a[at:at + 16].hex(' ')}, not {b[at:at + 16].hex(' ')}")


def wrong_checks(name):
    """What `run --spi` does not take: said before anything is done"""
    run = [sys.executable, str(ROOT / "flipperemu"), "-d", name, "run",
           "--headless", "--audio", "none"]
    for spec, words, what in WRONG:
        specs = [spec] if isinstance(spec, str) else list(spec)
        r = subprocess.run(run + [x for s in specs for x in ("--spi", s)],
                           capture_output=True, text=True, timeout=60,
                           stdin=subprocess.DEVNULL)
        check(f"`run --spi`, {what}: refused in words, and no device made",
              r.returncode == 1 and words in r.stderr
              and not (ROOT / "devices" / name).exists(),
              f"{r.returncode}\n{r.stdout}{r.stderr}")
    r = subprocess.run(run + ["--spi", "help"], capture_output=True, text=True,
                       timeout=60, stdin=subprocess.DEVNULL)
    said = " ".join(r.stdout.split())
    check("`run --spi help`: the pins that may be a chip select, the serial "
          "flashes there are, `flash` as a short name for one, none of the "
          "Flipper's own chips, and no device made",
          r.returncode == 0 and "4 (PA4), 6 (PB2), 7 (PC3), 10 (PA14), 12 "
          "(PA13), 15 (PC1) and 16 (PC0)" in said
          and "`flash` is w25q32 for short" in said
          and "Serial flashes, each as large as the part of that name" in said
          and re.search(r"\bw25q32,", said) and re.search(r"\bw25q80bl,", said)
          and not re.search(r"cc1101|st25r3916|st7567|sd-slot", said)
          and not (ROOT / "devices" / name).exists(),
          f"{r.returncode}\n{r.stdout}{r.stderr}")


def bus_checks(b):
    """What is on the bus as the launcher has it, before the firmware did
    anything there"""
    roms = {pin: b.dir / f"spi-{pin}.bin" for pin in SIZES}
    code, out, err, _ = fe(b, "spi")
    check("`spi`: the w25q32 with its chip select on pin 4 and the w25q80bl "
          "on pin 7, neither selected, each with the file of its contents; "
          "nothing was clocked yet",
          code == 0 and f"pins 2, 3 and 5 (SPI1): {ON}; {NOTHING}" in out
          and re.search(r"pin 4 +\(PA4\) +w25q32 +not selected; its contents "
                        r"are kept in " + re.escape(str(roms[4])), out)
          and re.search(r"pin 7 +\(PC3\) +w25q80bl +not selected; its "
                        r"contents are kept in " + re.escape(str(roms[7])),
                        out), out + err)
    new = {}
    for pin, path in roms.items():
        try:
            new[pin] = path.read_bytes()
        except OSError as e:
            new[pin] = str(e).encode()
    check("a new flash's file: as large as the part (4 MiB, 1 MiB), every "
          "bit 1", all(new[pin] == b"\xff" * SIZES[pin] for pin in SIZES),
          "\n".join(f"{roms[pin]}: {len(new[pin])} bytes, begins "
                    f"{new[pin][:16]!r}" for pin in SIZES))
    code, out, err, _ = fe(b, "spi", "--json")
    try:
        js = json.loads(out)
    except ValueError:
        js = {}
    on = [{"type": "w25q32", "select": 4, "pin": "PA4", "selected": False},
          {"type": "w25q80bl", "select": 7, "pin": "PC3", "selected": False}]
    check("`spi --json`: the bus's three pins, no byte and no frame yet, and "
          "each device with its type, its pin, is it selected, its drive and "
          "its file",
          code == 0 and {k: v for k, v in js.items() if k != "devices"}
          == {"name": "SPI1", "mosi": 2, "miso": 3, "sck": 5, "heard": 0,
              "unheard": 0, "last": None}
          and js.get("devices") == [
              dict(d, drive=f"spi-{d['select']}-rom",
                   file=str(roms[d["select"]])) for d in on], out + err)
    code, out, err, _ = fe(b, "gpio")
    check("`gpio` has the bus with what is on it, and says of pins 4 and 7 "
          "whose chip selects they are",
          code == 0 and "\nits SPI bus, MOSI on pin 2, MISO on pin 3 and SCK "
          "on pin 5 (`run --spi TYPE@PIN` puts a device there, PIN its chip "
          f"select)\n  pins 2, 3 and 5 (SPI1)  {ON}; {NOTHING}\n" in out
          and row(out, "PA4") == "4 PA4 analog floating w25q32's chip select"
          and row(out, "PC3") == "7 PC3 analog floating w25q80bl's chip "
                                 "select", out + err)
    code, out, err, _ = fe(b, "status")
    line = [x for x in out.splitlines() if x.startswith("spi")]
    st = bus(b)
    check("`status` has it too, and `status --json`",
          code == 0 and len(line) == 1
          and f"pins 2, 3 and 5 (SPI1): {ON}; {NOTHING}" in line[0]
          and st == {"name": "SPI1", "mosi": 2, "miso": 3, "sck": 5,
                     "heard": 0, "unheard": 0, "last": None, "devices": on},
          f"{out}{err}\n{st}")


def script_checks(b):
    """The firmware reads and writes the flash on pin 4 (its JS).  -> it
    did; False: this firmware's JS has no spi module"""
    rom = b.dir / "spi-4.bin"
    code, out, err, _ = fe(b, "push", EXAMPLE, "/ext")
    was, before = rom.read_bytes(), bus(b)
    got, said = flash(b)
    if code == 0 and "spi done" not in said and "spi" in said \
            and re.search(r"load fail|not found|no such", said, re.I):
        skip("a script (examples/spi/flash.js) reads a flash's IDs, erases "
             "a sector and writes there",
             "this firmware's JS has no spi module (" + " / ".join(
                 x.strip() for x in said.splitlines() if "spi" in x)[:160]
             + ")")
        return False
    check("a script on the Flipper (examples/spi/flash.js) reads the flash "
          "on pin 4: nothing kept in a new one, its JEDEC ID (ef 40 16), and "
          "after 90h and an address the maker and the device (ef 15)",
          code == 0 and got.get("kept") == BLANK
          and got.get("jedec") == "ef 40 16"
          and got.get("id90") == "00 00 00 00 ef 15" and "spi done" in said,
          out + err + said)
    check("... a sector erased, and seven bytes written there and read back",
          got.get("erased") == BLANK
          and got.get("written") == "46 6c 69 70 70 65 72 ff", said)
    after = bus(b)
    check(f"the launcher has counted the script's bytes: {JS_BYTES}, all with "
          "a device selected; the last at 2 MHz in mode 0, the most "
          "significant bit first, 8 bits; nobody is selected now",
          count(after, count(before)) == (JS_BYTES, 0)
          and after.get("last") == frame(2000000) and not selected(after),
          f"{before}\n{after}")
    code, out, err, _ = fe(b, "spi")
    check("`spi` says so in words",
          code == 0 and f"{said_count(after)}; the last at 2 MHz, mode 0, MSB "
          "first, 8 bits" in out, out + err)
    now = rom.read_bytes()
    check("the flash's file has those bytes while the device runs, and "
          "nothing else changed; the other flash's file is as it was",
          now == with_pages(was, [JS_PAGE])
          and (b.dir / "spi-7.bin").read_bytes() == b"\xff" * MIB,
          differ(now, with_pages(was, [JS_PAGE])))
    return True


def app_checks(b, folder):
    """The check's own app, a step at a time; then a reset in mid-use"""
    rom = b.dir / "spi-4.bin"
    was, first = rom.read_bytes(), bus(b)
    base = count(first)
    before = len(app_lines(b, TAG))
    code, text = tool(b, folder, "launch")
    got = lines(b, before, "4 done")
    check("`ufbt launch` starts the check's own app (scripts/spi_check_app). "
          "By the firmware's handle for the header it reads the flash on pin "
          "4: nothing kept in a sector, its JEDEC ID, Winbond's older IDs "
          "(90h: the maker and the device, and again; from address 1 the "
          "device first; ABh: the device alone)",
          code == 0 and got[:5] == [f"0 kept: {BLANK}"] + RUN[:4],
          text[-1500:] + log_tail(b))
    check("... the sector erased, and a page written there and read back",
          got[5:7] == RUN[4:], "\n".join(got))
    st = bus(b)
    check("a handle of the app's own, its chip select on pin 7, at 4 MHz "
          "with the clock resting high (mode 3): the other flash answers "
          "(ef 40 14).  86 bytes so far, all with a device selected, and "
          "the launcher has how the last was clocked",
          got[7:] == ["4 on pin 7: ef 40 14", "4 done"]
          and count(st, base) == (86, 0)
          and st.get("last") == frame(4000000, mode=3) and not selected(st),
          "\n".join(got) + f"\n{st}")
    code, out, err, _ = fe(b, "spi")
    check("`spi` says that in words: the count, and the last at 4 MHz, mode "
          "3", code == 0 and f"{said_count(st)}; the last at 4 MHz, mode 3, "
          "MSB first, 8 bits" in out, out + err)

    got = hold(b, "PB2", "high", line="5 done", tag=TAG)
    st = bus(b)
    check("the least significant bit first: the command goes out turned "
          "around (f9 for 9f), and so comes the answer (f7 02 68 for ef 40 "
          "16)", got == ["5 LSB first: f7 02 68", "5 done"]
          and count(st, base) == (90, 0)
          and st.get("last") == frame(2000000, lsb=True),
          "\n".join(got) + f"\n{st}")
    got = hold(b, "PB2", "low", line="6 done", tag=TAG)
    st = bus(b)
    code, out, err, _ = fe(b, "spi")
    check("frames of 16 bits: the command and the ID's first byte in one, "
          "the other two in the next; four bytes for the count, and `spi` "
          "has the frame's size",
          got == ["6 frames of 16 bits: 00ef 4016", "6 done"]
          and count(st, base) == (94, 0)
          and st.get("last") == frame(2000000, bits=16)
          and "the last at 2 MHz, mode 0, MSB first, 16 bits" in out,
          "\n".join(got) + f"\n{st}\n{out}{err}")

    got = hold(b, "PB2", "high", line="11 the bus is held", tag=TAG)
    st = bus(b)
    check("a pin of the three that is not the bus's.  The clock's: nothing "
          "reaches the header.  MISO's: the flash is asked, and nobody "
          "hears its answer", got[:2] == ["7 no clock on pin 5: 00 00 00",
                                          "8 pin 3 not MISO: 00 00 00"],
          "\n".join(got))
    check("MOSI's pin as a plain output for three bytes of a page: the "
          "flash gets the pin's level for every bit (low, high, low: 00 ff "
          "00), and not what the firmware sent (a5)",
          got[2:3] == ["9 pin 2 not MOSI: 00 ff 00 ff"], "\n".join(got))
    check("bytes that are clocked with nobody selected read as MISO's pull: "
          "00 with a pull-down, ff with a pull-up",
          got[3:4] == ["10 nobody selected: 00 00 00 ff ff ff"],
          "\n".join(got))
    check("the launcher counted those steps: 28 more bytes with a device "
          "selected (none for the step without a clock), and 8 with none",
          count(st, base) == (122, 8) and st.get("last") == frame(2000000),
          f"{st}")
    code, out, err, _ = fe(b, "spi")
    check("the app holds the bus in the middle of a read (11): `spi` has "
          "the flash on pin 4 as selected, the other not, and how many of "
          "the bytes a device heard",
          got[4:] == ["11 read: 46 6c", "11 the bus is held"]
          and selected(st) == [4] and code == 0 and said_count(st) in out
          and re.search(r"pin 4 +\(PA4\) +w25q32 +selected;", out)
          and re.search(r"pin 7 +\(PC3\) +w25q80bl +not selected;", out),
          "\n".join(got) + f"\n{st}\n{out}{err}")
    code, out, err, _ = fe(b, "gpio")
    pins = status(b)
    check("... and the table of `gpio` has pins 2, 3 and 5 as the bus's "
          "(alternate function 5), and pin 4 as an output that is low: the "
          "chip select, which selects now",
          code == 0
          and row(out, "PA7") == "2 PA7 alternate function 5, pull-down "
                                 "unknown SPI1's MOSI"
          and row(out, "PA6") == "3 PA6 alternate function 5, pull-down "
                                 "unknown SPI1's MISO"
          and row(out, "PB3") == "5 PB3 alternate function 5, pull-down "
                                 "unknown SPI1's SCK"
          and row(out, "PA4") == "4 PA4 output, pull-up low w25q32's chip "
                                 "select, which selects it now"
          and row(out, "PC3") == "7 PC3 analog floating w25q80bl's chip "
                                 "select"
          and all(pins.get(p, {}).get("function") == 5
                  for p in ("PA7", "PA6", "PB3")), out + err)

    code, out, err, _ = fe(b, "gpio", "PC3", "low")
    st = bus(b)
    c2, o2, e2, _ = fe(b, "gpio")
    check("pin 7 held low from outside, as a chip select that is wired to "
          "GND: the flash there is selected too, and the table says so",
          code == 0 and "held low from outside; it is low" in out
          and selected(st) == [4, 7]
          and row(o2, "PC3") == "7 PC3 analog low w25q80bl's chip select, "
                                "which selects it now; held low from outside",
          out + err + f"{st}\n" + o2 + e2)
    got = hold(b, "PB2", "low", line="12 done", tag=TAG)
    st = bus(b)
    check("the read goes on where it was after the wait (69 70), the handle "
          "lets go of pin 4, and a handle with no chip select of its own "
          "is answered by the flash on pin 7 (ef 40 14): 6 more bytes that "
          "a device heard",
          got == ["11 read on: 69 70", "12 selected from outside: ef 40 14",
                  "12 done"] and count(st, base) == (128, 8)
          and selected(st) == [7], "\n".join(got) + f"\n{st}")
    fe(b, "gpio", "PC3", "free")
    code, out, err, _ = fe(b, "gpio", "PA6", "high")
    got = hold(b, "PB2", "high", line="done", tag=TAG)
    st = bus(b)
    pins = status(b)
    check("pin 7 let go of, and MISO held high from outside: with nobody "
          "selected the firmware reads that (ff ff ff, with its own "
          "pull-down), 4 more bytes that no device heard; the app ends, and "
          "the bus's pins and pin 4 are analog again",
          code == 0 and "held high from outside; it is high" in out
          and got == ["13 MISO held from outside: ff ff ff", "done"]
          and count(st, base) == (128, 12) and not selected(st)
          and all(pins.get(p, {}).get("mode") == "analog"
                  for p in ("PA7", "PA6", "PB3", "PA4")),
          out + err + "\n".join(got) + f"\n{st}")
    fe(b, "gpio", "free")
    now, want = rom.read_bytes(), with_pages(was, APP_PAGES)
    check("the flash's file: the page (\"Flipper!\" at 0x2000) and the three "
          "bytes of levels (00 ff 00 at 0x3000), and nothing else changed; "
          "the other flash's file is as it was",
          now == want and (b.dir / "spi-7.bin").read_bytes() == b"\xff" * MIB,
          differ(now, want))

    # again, from the card, up to the read; and a reset in the middle of it
    before = len(app_lines(b, TAG))
    code, out, err, _ = fe(b, "app", FAP)
    got = lines(b, before, "4 done") + walk(b, "11 the bus is held")
    st = bus(b)
    check("started again from the card, the app finds the page that its "
          "first run wrote, and is in the middle of a read again",
          code == 0 and got[:1] == ["0 kept: 46 6c 69 70 70 65 72 21"]
          and got[-2:] == ["11 read: 46 6c", "11 the bus is held"]
          and selected(st) == [4], out + err + "\n".join(got) + f"\n{st}")
    code, out, err, _ = fe(b, "reset")
    c2, o2, e2, _ = fe(b, "wait", "--timeout", b.timeout)
    after, pins = bus(b), status(b)
    check("a reset of the firmware while its handle has the bus: nobody is "
          "selected after it, the bus's pins and pin 4 are analog, and the "
          "count is as it was",
          (code, c2) == (0, 0) and not selected(after)
          and len(after.get("devices", ())) == 2
          and count(after) == count(st)
          and all(pins.get(p, {}).get("mode") == "analog"
                  for p in ("PA7", "PA6", "PB3", "PA4")),
          out + err + o2 + e2 + f"{st}\n{after}")
    fe(b, "gpio", "free")
    before = len(app_lines(b, TAG))
    code, out, err, _ = fe(b, "app", FAP)
    got = lines(b, before, "4 done")
    rest = walk(b)
    fe(b, "gpio", "free")
    st = bus(b)
    check("the app after that reset: both flashes answer as before, the "
          "page is kept, erased and written anew; it runs to its end, and "
          "nobody is selected",
          code == 0 and got == ["0 kept: 46 6c 69 70 70 65 72 21"] + RUN
          + ["4 on pin 7: ef 40 14", "4 done"] and rest[-1:] == ["done"]
          and "11 read on: 69 70" in rest and not selected(st)
          and rom.read_bytes() == want,
          out + err + "\n".join(got + rest) + f"\n{st}")
    return True


def again_checks(b, timeout, script, app):
    """A stop, and a start with the flash on pin 4 alone.  script, app: what
    there is to read it with.  -> the device runs"""
    rom = b.dir / "spi-4.bin"
    b.stop()
    kept = rom.read_bytes() if rom.exists() else b""
    code, out, err, _ = fe(b, "run", "--headless", "--audio", "none", "--spi",
                           "w25q64@4", timeout=60)
    c2, o2, e2, _ = fe(b, "spi")
    check("a flash of another size on pin 4 while the file is there: refused "
          "in words (both sizes, the file, what to do), nothing started, "
          "the file as it was",
          code == 1 and "a flash of 8388608 bytes" in err
          and f"{rom}, are 4194304 bytes" in err and "remove the file" in err
          and c2 == 1 and "not running" in e2 and rom.read_bytes() == kept,
          f"{code}\n{out}{err}\n{o2}{e2}")
    ok, said = start(b, timeout, "--spi", "flash")
    if not check("a start with the flash on pin 4 alone (`--spi flash`)", ok,
                 said + b.uart_text()[-2000:]):
        return False
    code, out, err, _ = fe(b, "spi")
    check("`spi` has that one device and a count that began anew; the other "
          "flash's file is left as it was",
          code == 0 and "(SPI1): w25q32 with its chip select on pin 4; "
          + NOTHING in out and "w25q80bl" not in out
          and (b.dir / "spi-7.bin").read_bytes() == b"\xff" * MIB, out + err)
    if script:
        got, said = flash(b)
        check("the script finds the bytes that it wrote before the stop, and "
              "writes them anew",
              got.get("kept") == "46 6c 69 70 70 65 72 ff"
              and got.get("erased") == BLANK
              and got.get("written") == "46 6c 69 70 70 65 72 ff", said)
    if app:
        fe(b, "gpio", "free")
        before = len(app_lines(b, TAG))
        code, out, err, _ = fe(b, "app", FAP)
        got = lines(b, before, "4 done")
        st = bus(b)
        check("the app, started from the card, finds the page that it wrote "
              "before the stop; and its call to pin 7, where no device is "
              "now, is clocked with nobody selected: it reads MISO's pull "
              "(00 00 00), and those 4 bytes are counted as heard by none",
              code == 0 and got[:1] == ["0 kept: 46 6c 69 70 70 65 72 21"]
              and got[-2:] == ["4 on pin 7: 00 00 00", "4 done"]
              and (count(st) == (82 + JS_BYTES, 4) if script
                   else count(st) == (82, 4)),
              out + err + "\n".join(got) + f"\n{st}")
        rest = walk(b)
        fe(b, "gpio", "free")
        check("... and it runs to its end", rest[-1:] == ["done"],
              "\n".join(rest) + log_tail(b))
    if not script and not app:
        check("the flash's contents are as they were",
              rom.read_bytes() == kept and len(kept) == SIZES[4],
              differ(rom.read_bytes(), kept))
    return True


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("firmware")
    ap.add_argument("--timeout", type=float, default=120.0,
                    help="seconds to wait for the firmware to start")
    ap.add_argument("--keep", action="store_true",
                    help="keep the throwaway device")
    args = ap.parse_args()

    b = Bench(f"spicheck-{int(time.time())}-{os.getpid()}", args.firmware,
              args.timeout)
    try:
        code, out, err, took = fe(b, "spi")
        check("a device that is not running: says so, exit 1",
              code == 1 and "not running" in err, out + err)
        wrong_checks(b.name)
        b.dir.mkdir(parents=True, exist_ok=True)
        ok, said = start(b, args.timeout, "-f", args.firmware, "--spi",
                         "flash", "--spi", "w25q80bl@PC3")
        if ok:
            ok, said = b.answers()
        if check("the firmware starts with two serial flashes on the bus, "
                 "and its CLI answers", ok, said + b.uart_text()[-2000:]):
            bus_checks(b)
            script = script_checks(b)
            ran = []
            own_app(b, "an app in C on the bus: the firmware's handle, and "
                    "what it does not (another chip select, clock mode and "
                    "bit order, frames of 16 bits, a pin that is not the "
                    "bus's, nobody selected)", OWN, "fe_spi_check",
                    lambda b, folder: ran.append(app_checks(b, folder)))
            if again_checks(b, args.timeout, script, bool(ran)):
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
