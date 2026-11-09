#!/usr/bin/env python3
# Copyright (c) 2026, Kara Zajac
# SPDX-License-Identifier: BSD-3-Clause
"""Check the virtual iButton keys against a running emulated Flipper.

For every key kind (or the ones named), the key goes on the contact and
`ikey read` must print it: Dallas keys over 1-Wire (SEARCH ROM, READ ROM,
and READ MEMORY for the memory keys, at overdrive speed on a DS1996),
Cyfral and Metakom through the comparator.  For every kind of blank, `ikey
write` must write a DS1990A ROM onto it ("Write success": the firmware
reads it back and compares) and the blank must then hold that ROM.

Then the Flipper emulates each kind: a Dallas key saved with random
memory, emulated by the iButton app, must come back whole from the
virtual 1-Wire reader (READ ROM, READ MEMORY); a Cyfral or Metakom key
from `ikey emulate` must be captured and decode back to its data.

    scripts/ibutton_check.py DEVICE [KIND ...]      (KIND: a key, a blank,
                                                     or emulate-KIND)
"""
import pathlib
import random
import re
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import fe_ibutton  # noqa: E402
from fe_cli import FlipperCli, usb_endpoint  # noqa: E402
from fe_monitor import Monitor  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parent.parent

# kind: (data, what `ikey read` prints before the data)
SAMPLES = {
    "ds1990": ("01 A1 B2 C3 D4 E5 F6", "Dallas"),
    "ds1992": ("08 A1 B2 C3 D4 E5 F6", "Dallas"),
    "ds1996": ("0C A1 B2 C3 D4 E5 F6", "Dallas"),
    "ds1971": ("14 A1 B2 C3 D4 E5 F6", "Dallas"),
    "dallas": ("22 A1 B2 C3 D4 E5 F6", "(non-specific)"),
    "cyfral": ("A1 B2", "Cyfral"),
    "metakom": ("C3 55 33 0F", "Metakom"),
}
BLANKS = ("rw1990.1", "rw1990.2", "tm2004", "tm01x")
WRITE_ROM = "01 5A 6B 7C 8D 9E AF"     # the DS1990A ROM written (CRC added)
APP_KEY = "/ext/ibutton/ibutton_check.ibtn"
# Dallas kinds the iButton app emulates: (protocol, memory field, bytes)
EMULATED = {"ds1990": ("DS1990", None, 0), "ds1992": ("DS1992", "Sram Data", 128),
            "ds1996": ("DS1996", "Sram Data", 8192),
            "ds1971": ("DS1971", "Eeprom Data", 32), "dallas": ("DSGeneric", None, 0)}


def read_check(mon, cli, dev, kind, data, label):
    desc = fe_ibutton.describe_new(kind, fe_ibutton.hexbytes(data))
    rom = next((l[4:] for l in desc.splitlines() if l.startswith("rom ")), None)
    want = f"{label} {(rom or data).replace(' ', '').upper()}"
    path = dev / "ibutton-key.txt"
    path.write_text(desc)
    err = mon.cmd(f"qom-set /machine/ibutton key file:{path}")
    if err:
        return False, err
    t0 = time.time()
    found, out = cli.watch("ikey read", re.escape(want), timeout=30)
    mon.cmd("qom-set /machine/ibutton key none")
    if not found:
        got = re.findall(r"^(?:Dallas|Cyfral|Metakom|\(non-specific\)) [0-9A-F]+$", out, re.M)
        return False, f"read {got or 'nothing'}, wanted {want}"
    return True, f"{time.time() - t0:.1f} s, {want}"


def write_check(mon, cli, dev, blank):
    rom = fe_ibutton.rom_of(fe_ibutton.hexbytes(WRITE_ROM))
    path = dev / "ibutton-key.txt"
    path.write_text(fe_ibutton.describe_new(blank, b""))
    err = mon.cmd(f"qom-set /machine/ibutton key file:{path}")
    if err:
        return False, err
    t0 = time.time()
    found, out = cli.watch(f"ikey write Dallas {rom.hex().upper()}",
                           r"Write success|Write fail", timeout=60)
    now = mon.cmd("qom-get /machine/ibutton rom").strip().strip('"')
    mon.cmd("qom-set /machine/ibutton key none")
    if not found or b"success" not in found.group(0):
        return False, f"{found.group(0).decode() if found else 'no answer'}; ROM {now}"
    if now.replace(" ", "") != rom.hex().upper():
        return False, f"written, but the blank holds {now}"
    return True, f"{time.time() - t0:.1f} s, ROM {now}"


def reader_read(mon, timeout=120):
    """Run the virtual 1-Wire reader; the .ibtn file it wrote, or None."""
    before = mon.cmd("qom-get /machine/ibutton tx").strip().strip('"')
    err = mon.cmd("qom-set /machine/ibutton reader read")
    if err:
        raise ValueError(err)
    end = time.time() + timeout
    while mon.cmd("qom-get /machine/ibutton reader").strip().strip('"'):
        if time.time() > end:
            return None
        time.sleep(0.2)
    after = mon.cmd("qom-get /machine/ibutton tx").strip().strip('"')
    return after if after != before and after.endswith(".ibtn") else None


def emulate_dallas(mon, cli, kind):
    """A key saved with random memory, emulated by the iButton app (as
    Saved -> Emulate does), read back by the virtual reader."""
    proto, field, size = EMULATED[kind]
    rnd = random.Random(kind)
    rom = fe_ibutton.rom_of(fe_ibutton.hexbytes(SAMPLES[kind][0])[:1] +
                            bytes(rnd.randrange(256) for _ in range(6)))
    mem = bytes(rnd.randrange(256) for _ in range(size))
    key = (f"Filetype: Flipper iButton key\nVersion: 2\nProtocol: {proto}\n"
           f"Rom Data: {rom.hex(' ').upper()}\n")
    if field:
        key += f"{field}: {mem.hex(' ').upper()}\n"
    cli.run("storage mkdir /ext/ibutton", timeout=5)
    cli.run(f"storage remove {APP_KEY}", timeout=5)      # write_chunk appends
    ready, out = cli.watch(f"storage write_chunk {APP_KEY} {len(key)}", r"Ready",
                           timeout=5, stop=False)
    if not ready:
        return False, (out.strip().splitlines() or ["storage write_chunk failed"])[-1]
    cli.write(key.encode())
    cli.read_until_prompt(10)
    err = cli.run(f"loader open iButton {APP_KEY}", timeout=5)
    if err:
        return False, f"iButton app: {err}"
    time.sleep(1.5)
    t0 = time.time()
    try:
        path = reader_read(mon)
    finally:
        mon.press("back")                       # leaving the app stops it
        time.sleep(0.5)
    if not path:
        return False, "the reader read nothing"
    got = fe_ibutton.parse_ibtn(path)
    grom = fe_ibutton.hexbytes(got.get("Rom Data", ""))
    gmem = fe_ibutton.hexbytes(got.get(field, "")) if field else b""
    if grom != rom:
        return False, f"ROM {grom.hex().upper()}, wanted {rom.hex().upper()} ({path})"
    if gmem != mem:
        at = next((i for i in range(min(len(gmem), len(mem))) if gmem[i] != mem[i]),
                  min(len(gmem), len(mem)))
        return False, f"memory differs from byte {at} of {len(mem)} ({path})"
    return True, (f"{time.time() - t0:.1f} s, {proto} {rom.hex().upper()}"
                  + (f" + {size} bytes" if size else ""))


def emulate_wave(mon, cli, kind):
    """`ikey emulate` a Cyfral or Metakom key: the capture must decode back."""
    data = fe_ibutton.hexbytes(SAMPLES[kind][0])
    before = mon.cmd("qom-get /machine/ibutton tx")
    cli.watch(f"ikey emulate {kind.capitalize()} {data.hex().upper()}", r"NEVER",
              timeout=1.5)
    for _ in range(50):                         # written after 100 ms quiet
        after = mon.cmd("qom-get /machine/ibutton tx")
        if after != before:
            break
        time.sleep(0.1)
    else:
        return False, "no capture"
    path = after.strip().strip('"')
    try:
        proto, got = fe_ibutton.decode_capture(fe_ibutton.parse_capture(path))
    except ValueError as e:
        return False, f"{e} ({path})"
    if (proto.lower(), got) != (kind, data):
        return False, f"decoded {proto} {got.hex().upper()} ({path})"
    return True, f"{proto} {got.hex().upper()} from {pathlib.Path(path).name}"


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    dev = ROOT / "devices" / sys.argv[1]
    kinds = sys.argv[2:] or (list(SAMPLES) + list(BLANKS) +
                             [f"emulate-{k}" for k in SAMPLES])
    mon = Monitor(str(dev / "monitor.sock"))
    cli = FlipperCli(usb_endpoint(mon))
    cli.read_until_prompt(10)
    failed = 0
    try:
        for kind in kinds:
            if kind in BLANKS:
                what, (ok, info) = "write", write_check(mon, cli, dev, kind)
            elif kind.startswith("emulate-"):
                kind = kind[8:]
                check = emulate_dallas if kind in EMULATED else emulate_wave
                what, (ok, info) = "emulate", check(mon, cli, kind)
            else:
                data, label = SAMPLES[kind]
                what, (ok, info) = "read", read_check(mon, cli, dev, kind, data, label)
            failed += not ok
            print(f"{'PASS' if ok else 'FAIL'}  {kind:8} {what:8} {info}", flush=True)
    finally:
        cli.close()
        mon.close()
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
