#!/usr/bin/env python3
# Copyright (c) 2026, Kara Zajac
# SPDX-License-Identifier: BSD-3-Clause
"""Check the launcher's `update` command: an update package for a Flipper
that runs, installed by its firmware.

    scripts/update_check.py firmware/official-1.5.1/flipper-z-f7-update-1.5.1.tgz
    scripts/update_check.py OLD.tgz --package NEW.tgz

Boots a throwaway device with the firmware, and gives it an update package
(the same one, unless another is named): written to the card's image with
the card out of its slot for that moment, then `update install` at the
firmware's CLI.  What is looked at: the card is in again and the firmware
has it; the launcher waits until the Flipper is back and says what runs on
it; a file that was on the card is still there; the emulator's own Sub-GHz
keystores (keystores/overlay) are on the card again, after the updater
unpacked the package's over them; the manifest on the card has its radio
stage, whose stack is the device's (CPU2 runs the update packages' own), and
the updater found that stack up to date; nothing crashed.

Then the radio stage proper, on the Flipper started with another stack
(`run --radio-stack 1.19.0`), which a plain `run` keeps (CPU2's FUS keeps
what it has).  A package whose radio image is damaged, through the
firmware's CLI as a real Flipper gets a package (`--cli`): the files that
are on the card are not sent again (what the launcher wrote to the image is
what the firmware reads there), the updater has the FUS delete the old
stack and stops at the image's CRC, its error is seen and said with the way
out of it, and that way works.  That is done with the firmware's log level
at none, where the updater would say nothing: the launcher lends it another
level, and gives the old one back.  Then the package as it is (`--no-wait`):
the FUS installs its stack from the image and starts it, and the updater
finds it up to date.  And what is refused: a folder and a file
that are no package, a Flipper with its cable pulled or its card out, one
that does not run.  At the end the Flipper is stopped and started with
`run --update`, which puts the package on the card of one that does not run:
the updater is started, and the keystores are on the card again after it.
Exit status 0 = pass.

The updater takes its time: three minutes or so with the official firmware,
more with a bigger package.
"""
import argparse
import hashlib
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from files_check import ROOT, Bench, CRASH, check, results  # noqa: E402

OVERLAY = pathlib.Path(__file__).resolve().parent.parent / "keystores" / "overlay"
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


def card(b):
    """(the card is in its slot, the firmware has it): the second by asking
    the firmware for a folder on it"""
    code, text = b.fe("status", "--json")
    try:
        slot = bool(json.loads(text)["sd"]["inserted"]) if code == 0 else None
    except (ValueError, KeyError):
        slot = None
    code, text = b.fe("cli", "storage info /ext")
    return slot, code == 0 and "Type:" in text


def card_within(b, seconds):
    end = time.time() + seconds
    while True:
        state = card(b)
        if state == (True, True) or time.time() > end:
            return state
        time.sleep(1)


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
    b.fe("sd", "eject")
    time.sleep(2)
    code, text = b.fe("update", pkg)
    check("with its card out of the slot: said so, and how to put it in",
          code == 1 and "not in the slot" in text and "sd insert" in text, text)
    b.fe("sd", "insert")
    state = card_within(b, 20)
    check("the card in again: the firmware has it", state == (True, True),
          state)


def keystores_wrong(b, ours):
    """Those of the emulator's keystores that the card does not have"""
    wrong = []
    for p in ours:
        at = "/ext/" + p.relative_to(OVERLAY).as_posix()
        code, text = b.fe("cli", f"storage md5 {at}")
        if code or hashlib.md5(p.read_bytes()).hexdigest() not in text:
            wrong.append(f"{at}: {text.strip()[-80:]}")
    return wrong


def run_update(b, pkg, ours, after, timeout=900):
    """`run --update`: the package on the card of a Flipper that does not
    run, and installed when it starts"""
    name, _ = manifest_of(pkg)
    b.stop()
    if b.uart.exists():
        b.uart.rename(b.dir / f"uart-run{b.runs}.log")
    b.runs += 1
    out = b.dir / "run-update.log"
    b.log = open(out, "wb")
    start = time.time()
    b.proc = subprocess.Popen(
        [sys.executable, str(ROOT / "flipperemu"), "-d", b.name, "run",
         "--headless", "--audio", "none", "--serial", f"file:{b.uart}",
         "--update", str(pkg)], stdout=b.log, stderr=subprocess.STDOUT)
    done = re.compile(r"the emulator's own are there again \((\d+) file|"
                      r"could not be written")
    text, said = "", None
    while time.time() - start < timeout and b.proc.poll() is None:
        text = out.read_text(errors="replace")
        said = done.search(text)
        if said or (not ours and "updater started" in text):
            break
        time.sleep(1)
    took = time.time() - start
    started = f"updater started: /ext/update/{name}/update.fuf" in text
    if ours:
        wrong = keystores_wrong(b, ours) if said else ["not looked at"]
        check("`run --update`, for a Flipper that does not run: the updater "
              f"started, and {took:.0f} s later the emulator's own keystores "
              f"are on the card again ({said.group(1) if said else 'none'} of "
              f"{len(ours)} files written), and the run says so",
              started and said and int(said.group(1) or 0) >= 1 and not wrong,
              "\n".join(wrong) + "\n" + text[-1500:])
    else:
        check("`run --update`, for a Flipper that does not run: the updater "
              "started", started, text[-1500:])
        time.sleep(20)                  # (it still answers for a moment)
    ok, text2 = answers_within(b, 20 if ours else timeout)
    now = version(b)
    check(f"... and it runs the firmware of the package ({now}), and its CLI "
          "answers", ok and now and now == after, f"{now}\n{text2}")
    crash = CRASH.search(b.uart_text())
    check("... with no crash in the firmware's log", not crash,
          crash and crash.group(0))


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
    check("the package written to the card, out of its slot for that, and "
          "installed: waited for, and said what runs "
          f"({took:.0f} s in all"
          + (f", {m.group(1)} s of them the updater)" if m else ")"),
          code == 0 and m and f"-> /ext/update/{name}/update.fuf" in text
          and "out of the slot for a moment" in text
          and "the card is in again" in text
          and "Traceback" not in text, text[-2500:])
    check("... in seconds, where the CLI takes minutes",
          m and took - int(m.group(1)) < 45, f"{took:.0f} s")
    state = card(b)
    check("the card is in its slot, and the firmware has it",
          state == (True, True), state)
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
    ours = sorted(p for p in OVERLAY.rglob("*") if p.is_file())
    if ours:
        said = re.search(r"the emulator's own are there again \((\d+) file", text)
        wrong = keystores_wrong(b, ours)
        check("the emulator's own Sub-GHz keystores are on the card again, "
              "after the updater unpacked the package's over them "
              f"({said.group(1) if said else 'none'} of {len(ours)} files "
              "written), and the launcher says so",
              said and int(said.group(1)) >= 1 and not wrong,
              "\n".join(wrong) + "\n" + text[-700:])
    else:
        print("  (no keystores/overlay here: not looked at)")
    code, text2 = b.fe("cli", f"storage read /ext/update/{name}/update.fuf")
    if radio:
        check("the manifest on the card has its radio stage, whose stack is "
              "the device's, and the updater found that stack up to date",
              code == 0 and re.search(r"(?m)^Radio: *\S", text2)
              and "Radio address:" in text2 and "Resources:" in text2
              and "is the device's: the updater finds it up to date" in text
              and "Stack version is up2date" in b.uart_text(),
              text2 + text[-600:])
    else:
        check("the manifest on the card: no radio stage, and its keys in "
              "place", code == 0 and re.search(r"(?m)^Radio: *\r?$", text2)
              and "Radio address:" in text2 and "Resources:" in text2, text2)
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
        print("  (the package has no radio stage: the FUS's install is not "
              "tried)")
        return ours, after

    # A Flipper whose CPU2 has another stack: its FUS keeps that one across
    # runs, as a chip's flash does
    # (waited for at its CLI: an updater may have left the log level at
    # none, and then "Startup complete" is not logged)
    b.stop()
    started = b.start(wait=False, more=("--radio-stack", "1.19.0"))
    code, text = b.fe("wait", "--timeout", 180, timeout=240)
    check("started again with another radio stack (`run --radio-stack "
          "1.19.0`)", started and code == 0
          and "radio stack 1.19.0 runs" in b.fe("status")[1],
          text + b.uart_text()[-800:])
    b.stop()
    started = b.start(wait=False)
    code, text = b.fe("wait", "--timeout", 180, timeout=240)
    check("a plain `run` after it: the stack that CPU2's FUS installed, "
          "1.19.0 (devices/NAME/radio.state)", started and code == 0
          and "radio stack 1.19.0 runs" in b.fe("status")[1],
          text + b.uart_text()[-800:])

    # A package whose radio image is damaged: the updater has the FUS
    # delete the old stack, then finds that the new one's CRC is not the
    # manifest's, and stops at its error.  At the log level none the updater
    # says nothing, and its error would not be seen: the launcher lends it
    # one for as long as it works.
    damaged = work / "damaged"
    if pkg.is_file():
        shutil.unpack_archive(str(pkg), str(damaged), "gztar")
    else:
        shutil.copytree(pkg, damaged)
    image = next(damaged.rglob("radio.bin"))
    data = bytearray(image.read_bytes())
    data[len(data) // 2] ^= 0xFF
    image.write_bytes(bytes(data))
    code, text = b.fe("log", "--set-level", "none")
    check("the firmware's log level set to none", code == 0
          and log_level(b) == "none", text)
    start = time.time()
    code, text = b.fe("update", "--cli", image.parent, timeout=1800)
    took = time.time() - start
    sent = re.findall(r"^  \S+ -> /ext/update/\S+/(\S+) \(", text, re.M)
    check("a package whose radio image is damaged, through the CLI: the "
          "files that the card has already are not sent again, the damaged "
          f"image is (sent: {', '.join(sent) or 'none'})",
          "through the Flipper's CLI" in text and "out of the slot" not in text
          and sent == ["radio.bin"], text)
    log = b.uart_text()
    check("... the updater has the FUS delete 1.19.0, finds the new image's "
          "CRC wrong, and stops: its error is seen, with a log level lent to "
          f"it, and said with the way out ({took:.0f} s)",
          code == 1 and "stopped at an error" in text
          and "log level is none: info while" in text
          and "press --hold 1500 back" in text and "shot" in text
          and "Removing stack" in log and "Writing stack" in log, text)
    ok, text = leave_error(b)
    check("a held Back leaves the updater: the firmware runs, its CLI "
          "answers, and CPU2 has no stack now (the FUS deleted it)",
          ok and version(b) == after
          and "no radio stack installed" in b.fe("status")[1], text)
    left = log_level(b)
    check(f"the log level that was lent is as it was: {left}", left == "none",
          left)

    # The package as it is: the FUS installs its stack from the image (at the
    # level info again, for the updater's lines: --no-wait lends it none)
    b.fe("log", "--set-level", "info")
    mark = len(b.uart_text())
    start = time.time()
    code, text = b.fe("update", "--no-wait", pkg, timeout=1800)
    took = time.time() - start
    check(f"--no-wait: back when the updater has started ({took:.0f} s)"
          + (", and how the emulator's keystores get to the card after it"
             if ours else ""),
          code == 0 and "updater started" in text and "updated in" not in text
          and "CPU2's FUS installs its stack 1.20.0" in text
          and (not ours or ("push " in text and "/ext/`, when it is back"
                            in text)), text)
    code, text = b.fe("wait", "--timeout", 600, timeout=660)
    log = b.uart_text()[mark:]
    status = b.fe("status")[1]
    check("the updater had CPU2's FUS install the package's stack from its "
          "image (Writing stack, Installing stack) and start it, and found "
          "it up to date: the firmware runs with 1.20.0, and CPU2's FUS "
          "keeps it", code == 0 and "radio stack 1.20.0 runs" in status
          and "Installing stack" in log and "Stack version is up2date" in log
          and "stack 1.20.0" in (b.dir / "radio.state").read_text(),
          text + status + log[-1500:])
    return ours, after


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

    b = Bench(f"updcheck-{int(time.time())}-{os.getpid()}", args.firmware, args.timeout)
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
            ours, after = update_checks(b, pkg, work, bool(args.package))
            run_update(b, pkg, ours, after)
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
