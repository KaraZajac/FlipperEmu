#!/usr/bin/env python3
# Copyright (c) 2026, Kara Zajac
# SPDX-License-Identifier: BSD-3-Clause
"""Check the Sub-GHz keys that the launcher makes (`subghz came KEY` and the
others, scripts/fe_subghz.py): each as the firmware's own encoder sends it,
so the firmware's decoder has to read the key back.

    scripts/subghz_check.py firmware/official-1.5.1/flipper-z-f7-update-1.5.1.tgz

Boots a throwaway device, listens with the firmware's `subghz rx` at 433.92
MHz and puts a key of each protocol on the air: Princeton, CAME (12 and 24
bits), Nice FLO (12 and 24), GateTX, Holtek HT12X, Linear.  The same keys
from .sub files, as the Sub-GHz app saves them.  A firmware whose `subghz
rx` does not start the internal radio from the CLI (Unleashed, RogueMaster)
is skipped, said so.  Exit status 0 = pass.
"""
import argparse
import os
import pathlib
import re
import select
import shutil
import sys
import tempfile
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from files_check import Bench, CRASH, check, results          # noqa: E402
from fe_cli import FlipperCli, usb_endpoint                  # noqa: E402
from fe_monitor import Monitor                               # noqa: E402
import check_limit  # noqa: E402

SKIPPED = 77
# the launcher's words -> what the firmware's `subghz rx` has to say
KEYS = (
    (("princeton", "0x95D5D4"), "Princeton 24bit", "Key:0x0095D5D4"),
    (("came", "0xABC"), "CAME 12bit", "Key:0x00000ABC"),
    (("came", "0xABCDEF", "--bits", "24"), "CAME 24bit", "Key:0x00ABCDEF"),
    (("nice-flo", "0x5A3"), "Nice FLO 12bit", "Key:0x000005A3"),
    (("nice-flo", "0x5A3C0F", "--bits", "24"), "Nice FLO 24bit", "Key:0x005A3C0F"),
    (("gatetx", "0xC0FFEE"), "GateTX 24bit", "Key:C0FFEE"),
    # (at the firmware's own TE, 320 µs, a Holtek key is a CAME key's
    # signal, and CAME's decoder has it first)
    (("holtek", "0x9A5", "--te", "205"), "Holtek_HT12X 12b", "Key:0x9A5"),
    (("linear", "0x2AB"), "Linear 10bit", "Key:0x2AB"),
)
# .sub files of two of them, as the Sub-GHz app writes a key.  (Linear's
# is kept with every bit turned over: the Flipper shows 0x2AB for 0x154.)
FILES = (
    ("CAME", 12, "00 00 00 00 00 00 0A BC", "CAME 12bit", "Key:0x00000ABC"),
    ("Linear", 10, "00 00 00 00 00 00 01 54", "Linear 10bit", "Key:0x2AB"),
)


def heard(b, cli, *words):
    """`subghz rx`, then a signal by the launcher: what the firmware said"""
    cli.write(b"subghz rx 433920000 0\r")
    time.sleep(1.5)
    code, text = b.fe("subghz", *words)
    out, end = b"", time.time() + 5
    while time.time() < end and b"Key" not in out:
        if select.select([cli.fd], [], [], 0.1)[0]:
            out += os.read(cli.fd, 4096)
    end = time.time() + 0.6                     # (the lines after the key's)
    while time.time() < end:
        if select.select([cli.fd], [], [], 0.1)[0]:
            out += os.read(cli.fd, 4096)
    cli.write(b"\x03")
    time.sleep(0.4)
    try:
        cli.read_until_prompt(5)
    except (OSError, TimeoutError):
        pass
    return code, text, out.decode(errors="replace")


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("firmware")
    ap.add_argument("--timeout", type=float, default=120.0,
                    help="seconds to wait for the firmware to start")
    ap.add_argument("--keep", action="store_true",
                    help="keep the throwaway device")
    args = ap.parse_args()
    check_limit.arm(600)

    b = Bench(f"subghzcheck-{int(time.time())}-{os.getpid()}", args.firmware,
              args.timeout)
    work = pathlib.Path(tempfile.mkdtemp(prefix="fe-subghz-")).resolve()
    skipped = False
    try:
        if check("boot: 'Startup complete'", b.start(first=True),
                 b.uart_text()[-2000:]):
            b.fe("wait", "--timeout", args.timeout)
            m = Monitor(str(b.dir / "monitor.sock"))
            try:
                cli = FlipperCli(usb_endpoint(m))
            finally:
                m.close()
            try:
                cli.read_until_prompt(15)
                for words, name, key in KEYS:
                    code, text, out = heard(b, cli, *words)
                    if "failed to start" in out:
                        skipped = True
                        break
                    check(f"`subghz {' '.join(words)}`: the firmware's "
                          f"`subghz rx` reads it: {name}, {key}",
                          code == 0 and name in out and key in out,
                          text + out[-600:])
                for name, bits, key, said, shown in () if skipped else FILES:
                    path = work / f"{name}.sub"
                    path.write_text(
                        "Filetype: Flipper SubGhz Key File\nVersion: 1\n"
                        "Frequency: 433920000\n"
                        "Preset: FuriHalSubGhzPresetOok650Async\n"
                        f"Protocol: {name}\nBit: {bits}\nKey: {key}\n")
                    code, text, out = heard(b, cli, path)
                    check(f"a .sub file with a {name} key ({key}): read as "
                          f"{said}, {shown}",
                          code == 0 and said in out and shown in out,
                          text + out[-600:])
            finally:
                cli.close()
            b.stop()
            log = b.uart_text(every=True)
            crash = CRASH.search(log)
            check("no crash in the firmware's logs", not crash,
                  crash and log[max(0, crash.start() - 300):crash.end() + 300])
    finally:
        b.stop()
        shutil.rmtree(work, ignore_errors=True)
        if not args.keep:
            shutil.rmtree(b.dir, ignore_errors=True)
    if skipped:
        print("skipped: this firmware's `subghz rx` does not start the "
              "internal radio from the CLI")
        return SKIPPED
    bad = [label for label, ok in results if not ok]
    print(f"{len(results) - len(bad)} of {len(results)} checks passed"
          + (f"; device kept: {b.dir}" if args.keep else ""))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
