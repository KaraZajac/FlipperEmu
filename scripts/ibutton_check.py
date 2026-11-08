#!/usr/bin/env python3
# Copyright (c) 2026, Kara Zajac
# SPDX-License-Identifier: BSD-3-Clause
"""Check the virtual iButton keys against a running emulated Flipper.

For every key kind (or the ones named), the key goes on the contact and
`ikey read` must print it: Dallas keys over 1-Wire (SEARCH ROM, READ ROM,
and READ MEMORY for the memory keys, at overdrive speed on a DS1996),
Cyfral and Metakom through the comparator.

    scripts/ibutton_check.py DEVICE [KIND ...]
"""
import pathlib
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


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    dev = ROOT / "devices" / sys.argv[1]
    kinds = sys.argv[2:] or list(SAMPLES)
    mon = Monitor(str(dev / "monitor.sock"))
    cli = FlipperCli(usb_endpoint(mon))
    cli.read_until_prompt(10)
    failed = 0
    try:
        for kind in kinds:
            data, label = SAMPLES[kind]
            ok, info = read_check(mon, cli, dev, kind, data, label)
            failed += not ok
            print(f"{'PASS' if ok else 'FAIL'}  {kind:8} read     {info}", flush=True)
    finally:
        cli.close()
        mon.close()
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
