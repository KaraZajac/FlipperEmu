#!/usr/bin/env python3
# Copyright (c) 2026, Kara Zajac
# SPDX-License-Identifier: BSD-3-Clause
"""Check fe_rfid.py's tag encoders against a running emulated Flipper.

For every protocol (or the ones named), two checks:
  emulate: `rfid emulate` on the Flipper, captured by the virtual reader,
           must be the same loop as fe_rfid.py's encoder produces;
  read:    fe_rfid.py's tag in the field must make `rfid read` print the
           protocol and data.

    scripts/rfid_check.py DEVICE [PROTOCOL ...]
"""
import pathlib
import re
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import fe_rfid  # noqa: E402
from fe_cli import FlipperCli, usb_endpoint  # noqa: E402
from fe_monitor import Monitor  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parent.parent

# data that each protocol's decoder gives back unchanged
SAMPLES = {
    "EM4100": "DE AD BE EF 01",
    "EM4100/32": "12 34 56 78 9A",
    "EM4100/16": "01 02 03 04 05",
    "Electra": "01 23 45 67 89 7E 71 AA",     # EM ID, epilogue 7E71 AA..
    "H10301": "01 02 03",
    "Idteck": "49 44 54 4B 35 1F BE 4B",      # bytes 0-3 are the preamble
    "Indala26": "11 22 33 40",
    "IoProxXSF": "65 01 12 34",               # FC 101, v1, code 4660
    "AWID": "1A BD 88 EB 80 00 00 00 00",     # 26-bit, FC 123, card 4567
    "FDX-A": "92 34 D6 F8 10",                # every byte odd parity
    "FDX-B": "0F C0 00 00 03 9F 00 01 00 00 00",  # 999 000000001008, animal
    "HIDProx": "01 04 D2 1B BA A0",           # 37-bit H10304, FC 1234, card 56789
    "HIDExt": "00 00 00 01 23 45 67 89 AB CD EF 00",  # 92 bits used
    "Pyramid": "1A 7B 30 39",                 # 26-bit, FC 123, card 12345
    "Viking": "12 34 56 78",
    "Jablotron": "12 34 56 78 90",
    "Paradox": "00 1E CC 0E 5A 40",           # FC 123, card 12345, CRC 0x69
    "PAC/Stanley": "12 34 56 78",
    "Keri": "80 00 30 39",                    # internal ID 12345
    "Gallagher": "15 00 12 34 00 56 78 9A",   # region 1, IL 5, FC 4660, card 5666970
    "Nexwatch": "00 21 3C 9F 8F 15 0C 00",    # byte 0 zero, parity in byte 5
    "Radio Key": "00 1B 1F 31 0F 60",         # RKKT 26-bit, FC 27, card 7985
    "GProxII": "F8 12 D7 A1 20 4A C8 14 35 C0 40 12",  # 26-bit, FC 123, card 4567
    "Noralsy": "BB 02 14 FF 01 11 80 22 33 97 00 00",  # card 0112233, 2018
}


KEY_FILE = "/ext/lfrfid/rfid_check.rfid"


def emulate_in_app(mon, cli, name, data):
    """Emulate the way the 125 kHz RFID app emulates a saved key: `rfid
    emulate` reads the protocol name only up to a space ("Radio Key")."""
    key = (f"Filetype: Flipper RFID key\nVersion: 1\nKey type: {name}\n"
           f"Data: {data.hex(' ').upper()}\n").encode()
    cli.run(f"storage remove {KEY_FILE}")       # write_chunk appends
    ready, out = cli.watch(f"storage write_chunk {KEY_FILE} {len(key)}", r"Ready",
                           timeout=5, stop=False)
    if not ready:
        return (out.strip().splitlines() or ["storage write_chunk failed"])[-1]
    cli.write(key)
    cli.read_until_prompt(5)
    err = cli.run(f'loader open "125 kHz RFID" {KEY_FILE}')
    if err:
        return err
    time.sleep(1.2)
    mon.press("back")                           # leaving the app stops it
    return None


def emulate_check(mon, cli, name, data):
    before = mon.cmd("qom-get /machine/rfid tx")
    via = ""
    if " " in name:
        err = emulate_in_app(mon, cli, name, data)
        if err:
            return False, f"125 kHz RFID app: {err}"
        via = ", via the 125 kHz RFID app"
    else:
        cli.watch(f"rfid emulate {name} {data.hex().upper()}", r"NEVER", timeout=1.2)
    for _ in range(50):                         # written after 100 ms quiet
        after = mon.cmd("qom-get /machine/rfid tx")
        if after != before:
            break
        time.sleep(0.1)
    else:
        return False, "no capture"
    path = after.strip().strip('"')
    try:
        frame, mod = fe_rfid.capture_frame(fe_rfid.parse_capture(path))
    except ValueError as e:
        return False, f"{e} ({path})"
    _, mine, my_mod = fe_rfid.encode(name, data)
    if not fe_rfid.same_frame(frame, mine):
        return False, (f"capture differs: {sum(n for _, n in frame)} cycles "
                       f"vs {sum(n for _, n in mine)} ({path})")
    return True, f"{sum(n for _, n in frame)} cycles, {mod}{via}"


def read_check(mon, cli, dev, name, data):
    tagfile = dev / "rfid-tag.txt"
    tagfile.write_text(fe_rfid.tag(name, data))
    err = mon.cmd(f"qom-set /machine/rfid tag file:{tagfile}")
    if err:
        return False, err
    want = f"{name} {data.hex().upper()}"
    t0 = time.time()
    found, out = cli.watch("rfid read", re.escape(want), timeout=12)
    mon.cmd("qom-set /machine/rfid tag none")
    if not found:
        names = "|".join(re.escape(n) for n in fe_rfid.PROTOCOLS)
        got = re.findall(rf"^(?:{names}) [0-9A-F]+$", out, re.M)
        return False, f"read {got or 'nothing'}"
    return True, f"{time.time() - t0:.1f} s"


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    dev = ROOT / "devices" / sys.argv[1]
    names = sys.argv[2:] or list(SAMPLES)
    mon = Monitor(str(dev / "monitor.sock"))
    cli = FlipperCli(usb_endpoint(mon))
    cli.read_until_prompt(10)
    cli.buf = b""
    failed = 0
    try:
        for name in names:
            data = fe_rfid.parse_hex(SAMPLES[name])
            for what, fn in (("emulate", lambda: emulate_check(mon, cli, name, data)),
                             ("read", lambda: read_check(mon, cli, dev, name, data))):
                ok, info = fn()
                failed += not ok
                print(f"{'PASS' if ok else 'FAIL'}  {name:11} {what:8} {info}", flush=True)
    finally:
        cli.close()
        mon.close()
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
