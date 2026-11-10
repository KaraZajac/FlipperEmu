#!/usr/bin/env python3
# Copyright (c) 2026, Kara Zajac
# SPDX-License-Identifier: BSD-3-Clause
"""Check the launcher's `app` command: an app put on the Flipper and started.

    scripts/app_check.py firmware/official-1.5.1/flipper-z-f7-update-1.5.1.tgz

Boots a throwaway device.  The app it works with is one the firmware brought
along on its SD card, so it is built for that firmware: it is started from
the card, closed, copied back to here, put on the card again (unchanged, and
into another folder) and started from there, each time over the app that
runs; an app that takes no request to close is left with its Back button.
Then what must not work: the same app with another API version in its
manifest (which the loader refuses, or asks about on the screen: then the
question is cancelled once, and answered once), a file that is no app, a name
the firmware does not know.  Last, an app that does not live through being
asked to close (the firmware's NFC app in the middle of a read: a check of
its own fails, and the firmware starts again): the launcher says what
happened.  And what a stopped device takes.  Exit status 0 = pass.
"""
import argparse
import os
import pathlib
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import fe_apps                                              # noqa: E402
from files_check import Bench, CRASH, check, results        # noqa: E402

# apps that end when the loader asks them to, smallest first
WANTED = ("gpio.fap", "ibutton.fap", "lfrfid.fap", "infrared.fap")


def other_api(data):
    """The same app, as if it were built for the API after this one"""
    at = data.index(struct.pack("<II", fe_apps.FAP_MAGIC, 1))
    major, = struct.unpack_from("<H", data, at + 10)
    return data[:at + 10] + struct.pack("<H", major + 1) + data[at + 12:]


def app_checks(b, work):
    code, text = b.fe("app")
    check("no app at the start: it says so", code == 0 and "no app runs" in text,
          text)

    code, text = b.fe("app", "--list")
    on_card = re.findall(r"^  (/ext/apps/\S.*\.fap)$", text, re.M)
    own = re.findall(r"^  (?!/ext/)(\S.*)$", text, re.M)
    check("--list: the firmware's own apps, and the .fap files on its card",
          code == 0 and own and on_card and "(in the firmware):" in text, text)
    card = next((p for want in WANTED for p in on_card
                 if p.rsplit("/", 1)[1] == want), None)
    if not check("an app on the card to work with", card,
                 "none of " + ", ".join(WANTED)):
        return

    name = card.rsplit("/", 1)[1]
    code, text = b.fe("app", card)
    m = re.search(r'"(.+)" runs', text)
    check("an app started from the card: it runs",
          code == 0 and m and "Traceback" not in text, text)
    title = m.group(1) if m else "?"
    code, text = b.fe("app")
    check("asked alone: it says which app runs",
          code == 0 and f'"{title}" runs' in text, text)
    code, text = b.fe("app", "--close")
    code2, text2 = b.fe("app")
    check("--close: closed, and no app runs",
          code == 0 and f'"{title}" closed' in text and "no app runs" in text2,
          text + text2)
    code, text = b.fe("app", "--close")
    check("--close with no app: it says so",
          code == 0 and "no app runs" in text, text)

    local = work / name
    code, text = b.fe("pull", card, work)
    try:
        fap = fe_apps.manifest(local.read_bytes())
    except (OSError, fe_apps.AppError) as e:
        fap = None
        text += f"\n{e}"
    check("the app copied to here: its manifest reads", code == 0 and fap, text)
    if not fap:
        return

    code, text = b.fe("app", local)
    check("a .fap from here: said what it is built for, put over the app of "
          "its name, started",
          code == 0 and f'"{fap.name}", built for API {fap.api_major}.' in text
          and "which is this firmware's" in text
          and f"= {card} (unchanged)" in text and f'"{title}" runs' in text,
          text)

    code, text = b.fe("app", "--to", "/ext/apps/fe-check", local)
    code2, text2 = b.fe("ls", "/ext/apps/fe-check")
    check("again with --to, while it runs: the one that runs is closed, the "
          "file is copied there and started",
          code == 0 and f'"{title}" closed' in text
          and f"-> /ext/apps/fe-check/{name}" in text
          and f'"{title}" runs' in text and name in text2, text + text2)

    code, text = b.fe("app", "--no-start", "--to", "/ext/apps/fe-check2", local)
    code2, text2 = b.fe("app")
    check("--no-start: copied, and the app that runs is left alone",
          code == 0 and f"-> /ext/apps/fe-check2/{name}" in text
          and "closed" not in text and f'"{title}" runs' in text2, text + text2)
    b.fe("app", "--close")

    # an app with a loop of its own does not hear the loader's request
    deaf = next((p for p in on_card if p.endswith("/snake_game.fap")), None)
    if deaf:
        code, text = b.fe("app", deaf)
        code2, text2 = b.fe("app", "--close")
        code3, text3 = b.fe("app")
        check("an app that takes no request to close: its Back button is "
              "pressed, and it says so",
              code == 0 and '" runs' in text and code2 == 0
              and "with its Back button" in text2 and "no app runs" in text3,
              text + text2 + text3)

    by_name = next((n for n in own if n in ("Infrared", "GPIO", "iButton")),
                   own[0] if own else "?")
    code, text = b.fe("app", by_name)
    check(f"one of the firmware's own by its name ({by_name}): it runs",
          code == 0 and '" runs' in text, text)
    b.fe("app", "--close")

    # what must not work
    wrong = work / "other-api.fap"
    wrong.write_bytes(other_api(local.read_bytes()))
    code, text = b.fe("app", "--wait", "12", "--to", "/ext/apps/fe-check", wrong)
    code2, text2 = b.fe("app")
    asks = "cancelled with Back" in text
    check("built for another API: said before it is copied; the loader's "
          "refusal is shown"
          + (", after its question on the screen was cancelled" if asks else ""),
          code == 1 and f"this firmware's API is {fap.api_major}" in text
          and "he loader did not start it:" in text
          and "Traceback" not in text and "no app runs" in text2, text + text2)
    if asks:
        # this firmware asks on its screen: "Continue anyways?"
        p = b.bg("app", "/ext/apps/fe-check/other-api.fap")
        time.sleep(fe_apps.QUESTION + 4)
        b.fe("press", "right")
        try:
            text = p.communicate(timeout=90)[0]
        except subprocess.TimeoutExpired:
            p.kill()
            text = p.communicate()[0] + "\n(not done after 90 s)"
        check("its question answered with Continue: the app runs",
              p.returncode == 0 and "no answer yet" in text
              and f'"{title}" runs' in text, text)
        b.fe("app", "--close")

    junk = work / "junk.fap"
    junk.write_text("not an app\n")
    code, text = b.fe("app", junk)
    code2, text2 = b.fe("ls", "/ext/apps")
    check("a file that is no app: refused, and not copied",
          code == 1 and "not an ELF file" in text and "junk.fap" not in text2,
          text + text2)
    code, text = b.fe("app", work / "nothing.fap")
    check("a .fap that is not there: it says so",
          code == 1 and "no such file" in text, text)
    code, text = b.fe("app", "No Such App")
    check("a name the firmware does not know: the loader's refusal is shown",
          code == 1 and "the loader did not start it:" in text
          and "Traceback" not in text, text)
    code, text = b.fe("app", "/ext/apps/fe-check/nothing.fap")
    check("a path on the card with no app at it: the loader's refusal is "
          "shown", code == 1 and "the loader did not start it:" in text, text)
    code, text = b.fe("app", "--list", by_name)
    check("--list with an app named: usage, not a guess",
          code != 0 and "stand alone" in text, text)
    b.fe("rm", "-r", "/ext/apps/fe-check")
    b.fe("rm", "-r", "/ext/apps/fe-check2")
    return local


def crash_checks(b):
    """The loader's request ends an app's event loop where it is.  The NFC app
    of the official firmware frees its scanner then, which is still at work
    while it reads: a furi_check fails, and the firmware starts again."""
    code, text = b.fe("app", "NFC")
    if code:
        print("  SKIP  no NFC app in this firmware to close in the middle of "
              "a read", flush=True)
        return
    time.sleep(2)
    b.fe("press", "ok")                     # its first item: Read
    time.sleep(3)
    code, text = b.fe("app", "--close")
    if code == 0:
        print("  SKIP  this firmware's NFC app closes in the middle of a read",
              flush=True)
        return
    check("an app that crashes when it is asked to close (NFC, while it "
          "reads): the firmware's crash is reported, with what its log says",
          code == 1 and re.search(r"the firmware crashed \(.*furi_check "
                                  r"failed\)", text)
          and "has started again" in text and "Traceback" not in text, text)
    end = time.monotonic() + 40
    while True:
        code, text = b.fe("app")
        if (code == 0 and "no app runs" in text) or time.monotonic() > end:
            break
        time.sleep(1)
    check("after it the firmware is up again: no app runs",
          code == 0 and "no app runs" in text, text)


def stopped_checks(b, local):
    code, text = b.fe("app", local)
    check("a stopped device: it says that it is its firmware that starts apps",
          code == 1 and "is not running" in text and "--no-start" in text, text)
    code, text = b.fe("app", "--no-start", "--to", "/ext/apps/fe-check", local)
    code2, text2 = b.fe("ls", "/ext/apps/fe-check")
    check("a stopped device with --no-start: the file goes into its card's "
          "image",
          code == 0 and f"-> /ext/apps/fe-check/{local.name}" in text
          and local.name in text2, text + text2)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("firmware")
    ap.add_argument("--timeout", type=float, default=120.0,
                    help="seconds to wait for the firmware to start")
    ap.add_argument("--keep", action="store_true",
                    help="keep the throwaway device")
    args = ap.parse_args()

    b = Bench(f"appcheck-{int(time.time())}-{os.getpid()}", args.firmware, args.timeout)
    work = pathlib.Path(tempfile.mkdtemp(prefix="fe-app-"))
    try:
        if check("boot: 'Startup complete'", b.start(first=True),
                 b.uart_text()[-2000:]):
            time.sleep(2)
            local = app_checks(b, work)
            log = b.uart_text(every=True)
            crash = CRASH.search(log)
            check("no crash in the firmware's log", not crash,
                  crash and log[max(0, crash.start() - 300):crash.end() + 300])
            crash_checks(b)
            b.stop()
            if local:
                stopped_checks(b, local)
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
