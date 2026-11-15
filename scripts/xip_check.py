#!/usr/bin/env python3
# Copyright (c) 2026, Kara Zajac
# SPDX-License-Identifier: BSD-3-Clause
"""Check code that is written to the flash while the firmware runs, and run
where it is (what a loader of apps that run in place does).

    scripts/xip_check.py firmware/official-1.5.1/flipper-z-f7-update-1.5.1.tgz

Needs `ufbt` on the PATH with this firmware's SDK (skipped without): the
check's own app (scripts/xip_check_app) is built and started.  It programs
a function into a blank page of the free flash and calls it there, and a
second one with a loop; erases the page and programs another function at
the first one's address, which has to be the one that runs then (code that
the emulator had translated is the flash's no more); erases the page
again.  With `run --coverage`, what ran there is in the coverage, each
piece with its bytes: the two functions that were at one address are two.
The firmware starts again afterwards: its flash is as it was.  Exit status
0 = pass.
"""
import argparse
import os
import pathlib
import re
import shutil
import sys
import tempfile
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from files_check import Bench, CRASH, ROOT, check, results    # noqa: E402
from ufbt_check import ANSI, logged, tool                     # noqa: E402
import check_limit  # noqa: E402
import fe_cov  # noqa: E402

PLUGIN = ROOT / "build" / "plugins" / "libfe_cov.so"
APP = ROOT / "scripts" / "xip_check_app"
SKIPPED = 77


def in_place(b, work, coverage):
    """-> None: no SDK for this firmware"""
    folder = work / "fe_xip_check"
    shutil.copytree(APP, folder)
    tool(b, folder, "-c")
    code, text = tool(b, folder)
    fap = folder / "dist" / "fe_xip_check.fap"
    if not check("`ufbt` alone: the check's own app is built "
                 "(scripts/xip_check_app)", code == 0 and fap.is_file(),
                 text[-1500:]):
        return False
    code, text = b.fe("app", "--no-start", "--to", "/ext/apps/fe-check", fap)
    b.fe("rm", "-r", "/ext/apps/fe-check")
    if "which is this firmware's" not in text:
        print("  ufbt's SDK is not for this firmware: code in the flash is "
              "not tried")
        return None
    code, text = tool(b, folder, "launch")
    done = code == 0 and logged(b, "[FeXipCheck] done", 60)
    said = dict(re.findall(r"\[FeXipCheck\] ([a-z ]+): (.*)",
                           ANSI.sub("", b.uart_text())))
    place = re.search(r"\[FeXipCheck\] page (\d+) at ([0-9A-F]{8}): (\w+)",
                      ANSI.sub("", b.uart_text()))
    if not check("the app is started (`ufbt launch`) and finds a blank page "
                 "below the radio stack's", done and place
                 and place.group(3) == "blank", text[-800:] + str(said)):
        return False
    at = int(place.group(2), 16)
    check(f"a function programmed into the flash (page {place.group(1)}, "
          f"{at:#010x}) while the firmware runs: the flash reads as it was "
          "written, and the function runs where it is (42); so does one "
          "with a loop (the sum of 1 to 10: 55)",
          said.get("flash") == "unlocked" and said.get("read back") == "same"
          and said.get("first") == "42" and said.get("sum") == "55", said)
    check("the page erased and another function programmed at the same "
          "address: that one runs (43), not the code that the emulator had "
          "of the first; the page erased again: blank",
          said.get("again") == "43" and said.get("restored") == "blank", said)
    if coverage:
        dump = work / "coverage.txt"
        code, text = b.fe("coverage", "dump", dump)
        blocks = fe_cov.read_dump(dump) if dump.exists() else []
        there = {(addr - at, size, count, data.hex() if data else None)
                 for addr, size, count, data in blocks
                 if at <= addr < at + 4096}
        check("`coverage dump`: what ran in that page, each piece with its "
              "bytes and how often: the two functions that were at one "
              "address as two, the loop's body nine times after the first",
              (0, 4, 1, "2a207047") in there and (0, 4, 1, "2b207047") in there
              and (0x102, 6, 9, "09180138fcd1") in there, f"{text}\n{there}")
    code, text = b.fe("app", "--close")
    b.fe("reset")
    code, text = b.fe("wait", "--timeout", 120)
    check("the app closed and the Flipper started again: the firmware "
          "starts from its flash as before", code == 0, text)
    return True


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("firmware")
    ap.add_argument("--timeout", type=float, default=120.0,
                    help="seconds to wait for the firmware to start")
    ap.add_argument("--keep", action="store_true",
                    help="keep the throwaway device")
    args = ap.parse_args()
    check_limit.arm(900)
    if not shutil.which("ufbt"):
        print("skipped: no `ufbt` on the PATH (the check's app is built by it)")
        return SKIPPED

    b = Bench(f"xipcheck-{int(time.time())}-{os.getpid()}", args.firmware,
              args.timeout)
    work = pathlib.Path(tempfile.mkdtemp(prefix="fe-xip-")).resolve()
    coverage = PLUGIN.exists()
    skipped = False
    try:
        if check("boot" + (" with --coverage" if coverage else "")
                 + ": 'Startup complete'",
                 b.start(first=True, more=("--coverage",) if coverage else ()),
                 b.uart_text()[-2000:]):
            b.fe("wait", "--timeout", args.timeout)
            skipped = in_place(b, work, coverage) is None
            log = b.uart_text(every=True)
            crash = CRASH.search(log)
            check("no crash in the firmware's logs", not crash,
                  crash and log[max(0, crash.start() - 300):crash.end() + 300])
            b.stop()
    finally:
        b.stop()
        shutil.rmtree(work, ignore_errors=True)
        if not args.keep:
            shutil.rmtree(b.dir, ignore_errors=True)
    if skipped:
        print("skipped: ufbt's SDK is not for this firmware")
        return SKIPPED
    bad = [label for label, ok in results if not ok]
    print(f"{len(results) - len(bad)} of {len(results)} checks passed"
          + (f"; device kept: {b.dir}" if args.keep else ""))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
