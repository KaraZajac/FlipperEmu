#!/usr/bin/env python3
# Copyright (c) 2026, Kara Zajac
# SPDX-License-Identifier: BSD-3-Clause
"""Check the launcher's `update` command: an update package sent to a Flipper
that runs, and installed by its firmware.

    scripts/update_check.py firmware/official-1.5.1/flipper-z-f7-update-1.5.1.tgz
    scripts/update_check.py OLD.tgz --package NEW.tgz

Boots a throwaway device with the firmware, and sends it an update package
(the same one, unless another is named) the way a real Flipper gets one:
file by file through the firmware's CLI, then `update install`.  What is
looked at: the launcher waits until the Flipper is back and says what runs
on it; a file that was on the card is still there; the manifest on the card
has no radio stage; nothing crashed.  Then with the radio stage left in,
which the emulated second core cannot install: the files that are on the
card are not sent again, the updater's error is seen and said with the way
out of it, and that way works.  That is done with the firmware's log level
at none, where the updater would say nothing: the launcher lends it another
level, and gives the old one back.  And what is refused: a folder and a file
that are no package, a Flipper with its cable pulled, one that does not run.
Exit status 0 = pass.

The transfer is as slow as a Flipper's CLI (some 20 KB/s) and the updater
takes its time: five minutes or so with the official firmware, more with a
bigger package.
"""
import argparse
import json
import pathlib
import re
import shutil
import sys
import tarfile
import tempfile
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from files_check import Bench, CRASH, check, results        # noqa: E402

VERSION = re.compile(r"(?m)^firmware\.version *: (.*)$")


def manifest_of(pkg):
    """(the folder the package is in, whether it brings a radio stack)"""
    with tarfile.open(pkg, "r:*") as tar:
        for member in tar.getmembers():
            path = pathlib.PurePosixPath(member.name)
            if path.name == "update.fuf" and member.isfile():
                text = tar.extractfile(member).read().decode(errors="replace")
                return path.parent.name, bool(re.search(r"(?m)^Radio: *\S", text))
    sys.exit(f"{pkg}: no update.fuf in it")


def version(b):
    code, text = b.fe("cli", "info device")
    m = VERSION.search(text)
    return m.group(1).strip() if code == 0 and m else None


def log_level(b):
    """The firmware's log level, as `status` has it"""
    code, text = b.fe("status", "--json")
    try:
        return json.loads(text)["log"]["level"] if code == 0 else None
    except (ValueError, KeyError):
        return None


def answers_within(b, seconds):
    """The CLI answers within so many seconds: (it does, what it said last)"""
    end = time.time() + seconds
    while True:
        ok, text = b.answers()
        if ok or time.time() > end:
            return ok, text
        time.sleep(2)


def leave_error(b):
    """Out of the updater's error screen, as the launcher says: a held Back"""
    for _ in range(2):
        b.fe("press", "--hold", "1500", "back")
        ok, text = answers_within(b, 45)
        if ok:
            break
    return ok, text


def refusals(b, pkg, work):
    (work / "nothing").mkdir()
    code, text = b.fe("update", work / "nothing")
    check("a folder with no update.fuf in it: refused",
          code == 1 and "expected one update.fuf, found 0" in text, text)
    (work / "no.tgz").write_bytes(b"not a package\n" * 100)
    code, text = b.fe("update", work / "no.tgz")
    check("a file that is no package: refused",
          code == 1 and "not an update package" in text
          and "Traceback" not in text, text)
    code, text = b.fe("update", work / "none.tgz")
    check("a package that is not there: said so",
          code == 1 and "no such update package" in text, text)
    b.fe("usb", "unplug")
    time.sleep(1)
    code, text = b.fe("update", pkg)
    b.fe("usb", "plug")
    check("with its USB cable pulled: no CLI to send it through, and how to "
          "get one", code == 1 and "no CLI" in text and "usb plug" in text,
          text)
    ok, text = answers_within(b, 30)
    check("plugged in again: its CLI answers", ok, text)


def update_checks(b, pkg, work, other):
    name, radio = manifest_of(pkg)
    before = version(b)
    check(f"the firmware says which it is: {before}", before)
    marker = work / "fe-marker.txt"
    marker.write_text("kept\n")
    code, text = b.fe("push", marker, "/ext/fe-check/")
    check("a file of ours on its card", code == 0, text)
    level = log_level(b)

    start = time.time()
    code, text = b.fe("update", pkg, timeout=3600)
    took = time.time() - start
    m = re.search(r"updated in (\d+) s: it runs (\S+)", text)
    check("the package sent through the CLI and installed: waited for, and "
          f"said what runs ({took:.0f} s in all"
          + (f", {m.group(1)} s of them the updater)" if m else ")"),
          code == 0 and m and f"-> /ext/update/{name}/update.fuf" in text
          and "Traceback" not in text, text[-2500:])
    left = log_level(b)
    said = re.search(r"left the firmware's log level at (\w+)", text)
    check(f"the firmware's log level after it: {left}, "
          + ("as before" if left == level else
             f"which its updater set (it was {level}), and the launcher says "
             "so, with the command that sets it again"),
          left and level and (not said if left == level else
                              said and said.group(1) == left
                              and f"log --set-level {level}" in text),
          text[-800:])
    ok, text2 = answers_within(b, 20)
    check("its CLI answers after it", ok, text2)
    after = version(b)
    if other:
        check(f"it runs the firmware of the package: {after}, and no longer "
              f"{before}", after and after != before
              and m and m.group(2) == after, f"{after}\n{text[-600:]}")
    else:
        check(f"it runs the same firmware as before: {after}",
              after and after == before and m and m.group(2) == after,
              f"{after}\n{text[-600:]}")
    code, text2 = b.fe("cli", "storage read /ext/fe-check/fe-marker.txt")
    check("the file that was on the card is still there",
          code == 0 and "kept" in text2, text2)
    code, text2 = b.fe("cli", f"storage read /ext/update/{name}/update.fuf")
    check("the manifest on the card: no radio stage, and its keys in place",
          code == 0 and re.search(r"(?m)^Radio: *\r?$", text2)
          and "Radio version: 00 00 00 00 00 00" in text2
          and "Radio address:" in text2 and "Resources:" in text2
          and "without its radio stage" in text, text2)
    code, text2 = b.fe("status")
    check("status: the firmware, as installed by update",
          code == 0 and "(installed by update)" in text2, text2)
    log = b.uart_text()
    stages = re.findall(r"\[UpdWorker\].*?Stage (\d+), progress", log)
    check(f"the updater's stages are in the firmware's log ({len(stages)})",
          len(stages) >= 6, log[-1500:])
    crash = CRASH.search(log)
    check("no crash in the firmware's log", not crash,
          crash and log[max(0, crash.start() - 300):crash.end() + 300])
    if not radio:
        print("  (the package has no radio stage: the updater's error is not "
              "tried)")
        return

    # at this level the updater says nothing, and its error would not be
    # seen: the launcher lends it one for as long as it works
    code, text = b.fe("log", "--set-level", "none")
    check("the firmware's log level set to none", code == 0
          and log_level(b) == "none", text)
    start = time.time()
    code, text = b.fe("update", "--keep-radio", pkg, timeout=1800)
    took = time.time() - start
    sent = re.findall(r"^  \S+ -> /ext/update/\S+/(\S+) \(", text, re.M)
    check("with the radio stage kept: the files that are on the card are not "
          f"sent again (sent: {', '.join(sent)})",
          "(unchanged)" in text and "radio.bin" in sent
          and "firmware.dfu" not in sent, text)
    check("... and the updater's error is seen, with a log level lent to it, "
          f"and said with the way out ({took:.0f} s)",
          code == 1 and "stopped at an error" in text
          and "log level is none: info while" in text
          and "press --hold 1500 back" in text and "shot" in text, text)
    ok, text = leave_error(b)
    check("a held Back leaves the updater: the firmware runs, and its CLI "
          "answers", ok and version(b) == after, text)
    left = log_level(b)
    check(f"the log level that was lent is as it was: {left}", left == "none",
          left)

    start = time.time()
    code, text = b.fe("update", "--keep-radio", "--no-wait", pkg, timeout=1800)
    took = time.time() - start
    check(f"--no-wait: back when the updater has started ({took:.0f} s)",
          code == 0 and "updater started" in text and "updated in" not in text,
          text)
    time.sleep(12)
    ok, text = leave_error(b)
    check("and the firmware runs again after that one too", ok, text)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("firmware")
    ap.add_argument("--package", metavar="PKG",
                    help="the update package to send (a .tgz; default: the "
                         "firmware's own)")
    ap.add_argument("--timeout", type=float, default=120.0,
                    help="seconds to wait for the firmware to start")
    ap.add_argument("--keep", action="store_true",
                    help="keep the throwaway device")
    args = ap.parse_args()
    pkg = pathlib.Path(args.package or args.firmware).resolve()

    b = Bench(f"updcheck-{int(time.time())}", args.firmware, args.timeout)
    work = pathlib.Path(tempfile.mkdtemp(prefix="fe-upd-"))
    try:
        code, text = b.fe("update", pkg)
        check("a device that does not run: said so, with `run --update`",
              code == 1 and "is not running" in text and "run --update" in text,
              text)
        if check("boot: 'Startup complete'", b.start(first=True),
                 b.uart_text()[-2000:]):
            time.sleep(2)
            refusals(b, pkg, work)
            update_checks(b, pkg, work, bool(args.package))
            b.stop()
    finally:
        b.stop()
        shutil.rmtree(work, ignore_errors=True)
        if not args.keep:
            shutil.rmtree(b.dir, ignore_errors=True)
    bad = [label for label, ok in results if not ok]
    print(f"{len(results) - len(bad)} of {len(results)} checks passed"
          + (f"; device kept: {b.dir}" if args.keep else ""))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
