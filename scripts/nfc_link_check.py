#!/usr/bin/env python3
# Copyright (c) 2026, Kara Zajac
# SPDX-License-Identifier: BSD-3-Clause
"""Check the NFC link and the Proxmark3 support with no Proxmark3 at all.

    scripts/nfc_link_check.py DEVICE [CHECK ...]    dumps, direct, pm3, pm3-old

The checks:
- dumps: Proxmark3 JSON dumps (an NTAG215 from `hf mfu dump`, a MIFARE
  Classic 1K from `hf mf autopwn`) become card descriptions and Flipper
  .nfc files with their UID, version, signature, counters and every block
  (no device needed).
- direct: an ISO14443-4A card played by fe_nfc_link.py: `apdu` in the NFC
  shell gets its answers through the firmware's ISO-DEP stack.
- pm3: the same card behind a simulated Proxmark3 (fe_pm3.py sim, firmware
  of September 2026 on: exact parity both ways), relayed by the `pm3` link
  card: every frame, anticollision included, goes through the Proxmark3's
  own USB protocol.
- pm3-old: the same with older Proxmark3 firmware (MIX frames, parity added
  by the Proxmark3).
"""
import json
import pathlib
import re
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import fe_nfc  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parent.parent
UID = "04A1B2C3D4E5F6"


def check_dumps(_):
    with tempfile.TemporaryDirectory() as tmp:
        desc = fe_nfc.blank("ntag215", bytes.fromhex(UID), "https://example.org")
        mem = b"".join(bytes.fromhex(l[4:].replace(" ", "")) for l in desc.splitlines()
                       if l.startswith("mem "))
        mfu = {"Created": "proxmark3", "FileType": "mfu",
               "Card": {"UID": UID, "Version": "0004040201001103", "TBO_0": "0000",
                        "TBO_1": "00", "Signature": "AB" * 32, "Counter2": "050000",
                        "Tearing2": "BD"},
               "blocks": {str(i): mem[4 * i:4 * i + 4].hex().upper()
                          for i in range(len(mem) // 4)}}
        blocks = {str(b): ("A1B2C3D404080400" + "6263646566676869" if b == 0 else
                           "FFFFFFFFFFFFFF078069FFFFFFFFFFFF" if b % 4 == 3 else
                           b"PROXMARK3 DUMP!!".hex() if b == 4 else "00" * 16)
                  for b in range(64)}
        mfc = {"Created": "proxmark3", "FileType": "mfc v2",
               "Card": {"UID": "A1B2C3D4", "ATQA": "0400", "SAK": "08"}, "blocks": blocks}
        pu, pc = pathlib.Path(tmp, "mfu.json"), pathlib.Path(tmp, "mfc.json")
        pu.write_text(json.dumps(mfu))
        pc.write_text(json.dumps(mfc))
        du, dc = fe_nfc.describe_pm3(pu), fe_nfc.describe_pm3(pc)
        fu, fc = fe_nfc.nfc_file(du), fe_nfc.nfc_file(dc)
    want = ["# NTAG215" in du, "version 00 04 04 02 01 00 11 03" in du,
            "counter2 5" in du, du.count("mem ") == 135,
            "# MIFARE Classic 1K" in dc, "atqa 04 00" in dc, dc.count("mem ") == 64,
            "Pages total: 135" in fu, "ATQA: 00 04" in fc,
            "Block 4: 50 52 4F 58 4D 41 52 4B 33 20 44 55 4D 50 21 21" in fc]
    if all(want):
        return True, "an NTAG215 (135 pages) and a MIFARE Classic 1K, as cards and .nfc"
    return False, f"conversion checks {want}"


def apdu_through(dev, link_args, label):
    """Start the link card, send an APDU from the NFC shell."""
    from fe_cli import FlipperCli, usb_endpoint
    from fe_monitor import Monitor
    procs = []
    try:
        if link_args[0] == "sim":
            sim = subprocess.Popen([sys.executable, str(ROOT / "scripts" / "fe_pm3.py"),
                                    "sim", *link_args[1:]], stdout=subprocess.PIPE,
                                   stderr=subprocess.DEVNULL, text=True)
            procs.append(sim)
            pty = re.search(r"on (/dev/pts/\d+)", sim.stdout.readline()).group(1)
            link_args = ["pm3:" + pty]
        card = subprocess.Popen([sys.executable, str(ROOT / "scripts" / "fe_nfc_link.py"),
                                 str(ROOT / "devices" / dev / "nfc.sock"), *link_args],
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        procs.append(card)
        hello = card.stdout.readline().strip()
        time.sleep(1.0)
        mon = Monitor(str(ROOT / "devices" / dev / "monitor.sock"))
        cli = FlipperCli(usb_endpoint(mon))
        try:
            cli.read_until_prompt(15)
            cli.watch("nfc", r"\[nfc\]>: ", timeout=8, stop=False)
            _, out = cli.watch("apdu -p 4a -d 00CA000000", r"Rx:[^\n]*\n|\[nfc\]>: ",
                               timeout=40, stop=False)
            cli.read_until_prompt(10)
            cli.run("exit", timeout=5)
        finally:
            cli.close()
            mon.close()
    finally:
        for p in reversed(procs):
            p.terminate()
            p.wait(5)
    if f"Rx: {' '.join(UID[i:i + 2] for i in range(0, 14, 2))} 90 00" in out:
        how = hello.split(" -> ")[0].replace("[fe_nfc_link] ", "")
        return True, f"GET DATA answered by {how}"
    return False, f"{label}: {out.strip()[-200:]!r}"


CHECKS = {
    "dumps": check_dumps,
    "direct": lambda dev: apdu_through(dev, ["iso-dep"], "direct"),
    "pm3": lambda dev: apdu_through(dev, ["sim"], "pm3"),
    "pm3-old": lambda dev: apdu_through(dev, ["sim", "--old"], "pm3-old"),
}


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    dev, names = sys.argv[1], sys.argv[2:] or list(CHECKS)
    failed = 0
    for name in names:
        t0 = time.time()
        try:
            ok, info = CHECKS[name](dev)
        except (OSError, ConnectionError, TimeoutError, ValueError, AttributeError) as e:
            ok, info = False, str(e)
        failed += not ok
        print(f"{'PASS' if ok else 'FAIL'}  {name:8} {info} ({time.time() - t0:.1f} s)",
              flush=True)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
