#!/usr/bin/env python3
# Copyright (c) 2026, Kara Zajac
# SPDX-License-Identifier: BSD-3-Clause
"""Check what a test script is made of: the launcher's `wait`, and the screen
as text (`shot --text`).

    scripts/wait_check.py firmware/official-1.5.1/flipper-z-f7-update-1.5.1.tgz

Starts a throwaway device and does not wait for it: `wait` does, until its
CLI answers.  Then `wait --log`: a line from the start, one that never comes,
one that a command brings (an app started from the card) and one from before
the command, which does not count.  Then the screen: as text and as a picture
(the same pixels), `wait --screen` with it, with one character changed (how
many pixels differ, and the screen, on stderr) and with a '?' there; an app
closed and started again through `wait --screen ... -- app`, which is how a
test waits for its app's first screen.  Then what must be refused, and a
command that fails.  Last what a wait says when what it waits for cannot come:
after a crash (the firmware's NFC app, closed while it reads: a release build
starts again, a debug build halts), and when the device is stopped under it.
At the end `run --detach`, which a test starts its Flipper with: back when the
emulator is up, back at once with one line for a firmware that is not there,
and no launcher left behind after a `stop`.  Exit status 0 = pass.
"""
import argparse
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
import time
import types

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import fe_screen                                            # noqa: E402
from files_check import ROOT, Bench, CRASH, check, results  # noqa: E402

# apps whose first screen stands still, smallest first
WANTED = ("gpio.fap", "ibutton.fap", "lfrfid.fap", "infrared.fap")


def fe(b, *args, timeout=600):
    """A launcher command: (exit status, what it printed, what it said on
    stderr, the seconds it took).  What it prints is UTF-8 (the screen)."""
    t0 = time.time()
    r = subprocess.run([sys.executable, str(ROOT / "flipperemu"), "-d", b.name,
                        *map(str, args)], capture_output=True, timeout=timeout,
                       encoding="utf-8", errors="replace",
                       stdin=subprocess.DEVNULL)
    return r.returncode, r.stdout, r.stderr, time.time() - t0


def log_checks(b):
    code, out, err, took = fe(b, "wait", "--log", "startup complete",
                              "--timeout", 60)
    check("--log, a line it logged while it started: found, and printed",
          code == 0 and "Startup complete" in out, out + err)
    code, out, err, took = fe(b, "wait", "--log", "Startup Complete",
                              "--timeout", 2)
    check("--log with capitals the line does not have: exit 1 after the "
          "timeout, with the log's last line",
          code == 1 and "no line with" in err and "since it started" in err
          and "its last line: " in err and 1.5 < took < 15, out + err)
    code, out, err, took = fe(b, "wait", "--log", "startup complete",
                              "--timeout", 2, "--", "press", "back")
    check("--log with a command: a line from before the command does not "
          "count", code == 1 and "after that" in err, out + err)

    code, out, err, took = fe(b, "app", "--list")
    on_card = re.findall(r"^\s+(/ext/\S+\.fap)\s*$", out, re.M)
    app = next((a for w in WANTED for a in on_card if a.endswith("/" + w)), None)
    if not check("an app on the card to work with", app, out + err):
        return None
    code, out, err, took = fe(b, "wait", "--log", "loader", "--timeout", 30,
                              "--", "app", app)
    check("--log with a command that starts an app: the app runs, and the "
          "loader's line is printed",
          code == 0 and "runs" in out and "loader" in out.lower(), out + err)
    return app


def screen_checks(b, app, work):
    time.sleep(2)                       # the app's first screen is drawn
    code, out, err, took = fe(b, "shot", "--text")
    lines = out.split("\n")
    check("shot --text: 32 lines of ' ', '▀', '▄' and '█', and not an empty "
          "screen", code == 0 and len(lines) == 33 and not lines[-1]
          and set(out) <= set(fe_screen.CELLS + "\n")
          and max(map(len, lines)) <= fe_screen.W
          and sum(c != " " for c in out.replace("\n", "")) > 50, out + err)

    # Paused, nothing on it moves: two shots are of the same screen.
    fe(b, "pause")
    as_text, as_png = work / "app.txt", work / "app.png"
    code1, out1, err1, _ = fe(b, "shot", "--text", as_text)
    code2, out2, err2, _ = fe(b, "shot", as_png)
    try:
        bits = fe_screen.load(as_text)[0]
        same = bits == fe_screen.load(as_png)[0] and any(bits)
        why = ""
    except (OSError, ValueError) as e:
        same, why = False, str(e)
    check("paused: the screen written as text and as a picture, the same "
          "pixels in both", code1 == 0 and code2 == 0 and same,
          out1 + err1 + out2 + err2 + why)
    if not same:
        fe(b, "continue")
        return
    for ref in (as_text, as_png):
        code, out, err, took = fe(b, "wait", "--screen", ref, "--timeout", 5)
        check(f"wait --screen with the {'text' if ref == as_text else 'picture'}"
              ": the screen is as in it", code == 0 and "is as in" in out,
              out + err)

    shown = as_text.read_text(encoding="utf-8")
    rows = [line.ljust(fe_screen.W) for line in shown.split("\n")[:32]]
    row, col = 20, 60
    was = rows[row][col]
    n = 2 if was == " " else fe_screen.CELLS.index(was).bit_count()

    def changed(to):
        return "\n".join(rows[:row] + [rows[row][:col] + to + rows[row][col + 1:]]
                         + rows[row + 1:]) + "\n"

    other, masked = work / "other.txt", work / "masked.txt"
    other.write_text(changed("█" if was == " " else " "), encoding="utf-8")
    masked.write_text(changed("?"), encoding="utf-8")
    code, out, err, took = fe(b, "wait", "--screen", other, "--timeout", 1)
    check("one character of it changed: exit 1, how many pixels differ, that "
          "the device is paused, and the screen as it is",
          code == 1 and f": {n} pixel{'s' if n != 1 else ''} differ" in err
          and "paused" in err and shown in err, out + err)
    code, out, err, took = fe(b, "wait", "--screen", masked, "--timeout", 5)
    check("a '?' in that place: any two pixels will do there", code == 0,
          out + err)
    r = subprocess.run([sys.executable, str(ROOT / "scripts/fe_screen.py"),
                        str(as_png), str(other)], capture_output=True, text=True)
    check("fe_screen.py with the picture and the changed text: the same count, "
          "exit 1", r.returncode == 1 and r.stdout.startswith(f"{n} pixel"),
          r.stdout + r.stderr)
    fe(b, "continue")

    code, out, err, took = fe(b, "wait", "--screen", as_text, "--timeout", 10)
    check("going on: the app's screen is still as in the text",
          code == 0, out + err)
    code, out, err, took = fe(b, "app", "--close")
    code, out, err, took = fe(b, "wait", "--screen", as_text, "--timeout", 3)
    check("the app closed: its screen is not, exit 1",
          code == 1 and "differ" in err and "it shows:" in err, out + err)
    code, out, err, took = fe(b, "wait", "--screen", as_text, "--timeout", 30,
                              "--", "app", app)
    check("wait --screen with the command that starts the app: its screen "
          "comes", code == 0 and "runs" in out and "is as in" in out, out + err)
    fe(b, "app", "--close")


def refused_checks(b, work):
    code, out, err, took = fe(b, "wait", "--screen", work / "none.txt")
    check("a reference that is not there: it says so, and does not wait",
          code == 1 and "No such file" in err and took < 10, out + err)
    (work / "words.txt").write_text("not a screen\n")
    code, out, err, took = fe(b, "wait", "--screen", work / "words.txt")
    check("a file that is no screen: where it is not one",
          code == 1 and "line 1, column 1" in err and took < 10, out + err)
    code, out, err, took = fe(b, "wait", "--log", "(")
    check("--log with what is no regular expression: it says so",
          code == 1 and "--log (" in err and took < 10, out + err)
    code, out, err, took = fe(b, "wait", "--log", "x", "--screen",
                              work / "app.txt")
    check("--log and --screen together: one at a time",
          code == 1 and "one at a time" in err, out + err)
    code, out, err, took = fe(b, "wait", "--timeout", 30, "--", "app",
                              "/ext/apps/none/none.fap")
    check("a command that fails: its exit status, and nothing is waited for",
          code != 0 and "its CLI answers" not in out and took < 25, out + err)
    code, out, err, took = fe(b, "wait", "--log", "x", "--timeout", 30, "--",
                              "press", "up", "sideways")
    check("a button that a Flipper has not: `press` says which there are, and "
          "nothing is waited for",
          code == 1 and "'sideways' is no button" in err and "back" in err
          and took < 10, out + err)
    code, out, err, took = fe(b, "wait", "--timeout", 30)
    check("after all that: its CLI answers", code == 0, out + err)


def crash_checks(b):
    """What a wait says when the firmware has crashed.  The crash is the one of
    app_check.py: the firmware's NFC app, asked to close while it reads."""
    code, out, err, took = fe(b, "app", "NFC")
    if code:
        print("  SKIP  no NFC app in this firmware to close in the middle of "
              "a read", flush=True)
        return
    time.sleep(2)
    fe(b, "press", "ok")                    # its first item: Read
    time.sleep(3)
    code, out, err, took = fe(b, "app", "--close")
    if code == 0:
        print("  SKIP  this firmware's NFC app closes in the middle of a read",
              flush=True)
        return
    if "has halted" in out + err:
        code, out, err, took = fe(b, "wait", "--timeout", 60)
        check("a debug build that has halted after a crash: `wait` says so "
              "at once", code == 1 and "has halted" in err and took < 15,
              out + err)
        code, out, err, took = fe(b, "wait", "--log", "no such line",
                                  "--timeout", 60)
        check("... `wait --log` within seconds",
              code == 1 and "has halted" in err and took < 20, out + err)
        (b.dir / "blank.txt").write_text("\n")
        code, out, err, took = fe(b, "wait", "--screen", b.dir / "blank.txt",
                                  "--timeout", 60)
        check("... and `wait --screen`",
              code == 1 and "has halted" in err and took < 20, out[:300] + err[:600])
        code, out, err, took = fe(b, "wait", "--timeout", 90, "--", "reset")
        check("`wait -- reset`: it starts again, and its CLI answers",
              code == 0 and "its CLI answers" in out, out + err)
        return
    code, out, err, took = fe(b, "wait", "--timeout", 90)
    check("a release build after a crash: it has started again, and `wait` "
          "goes on until its CLI answers", code == 0, out + err)
    code, out, err, took = fe(b, "wait", "--log", "no such line",
                              "--timeout", 3)
    check("a wait in vain after that: told that the firmware started again "
          "after a crash", code == 1 and "started again after a crash" in err,
          out + err)


def stop_checks(b):
    waiting = b.bg("wait", "--log", "no such line", "--timeout", 120)
    time.sleep(4)
    t0 = time.time()
    b.stop()
    try:
        said = waiting.communicate(timeout=30)[0]
    except subprocess.TimeoutExpired:
        waiting.kill()
        said = "(still waiting 30 s after the stop)"
    check("the device stopped while a `wait --log` waits: that ends within "
          "seconds, and says that it is not running any more",
          waiting.returncode == 1 and "not running any more" in said
          and time.time() - t0 < 30, said)
    code, out, err, took = fe(b, "wait", "--timeout", 2)
    check("stopped: `wait` says that it is not running",
          code == 1 and "not running" in err, out + err)
    code, out, err, took = fe(b, "shot", "--text")
    check("... and so does `shot --text`",
          code == 1 and "not running" in err and not out, out + err)


def launchers(name):
    """The launchers that run this device (`run`, detached or not)"""
    found = []
    for d in pathlib.Path("/proc").iterdir():
        if d.name.isdigit():
            try:
                argv = (d / "cmdline").read_bytes().split(b"\0")
            except OSError:
                continue
            if name.encode() in argv and b"run" in argv \
                    and any(a.endswith(b"flipperemu") for a in argv):
                found.append(int(d.name))
    return found


def detach_checks(b, work):
    """`run --detach` on the device, which is stopped by now"""
    none = types.SimpleNamespace(name=b.name + "-none")
    for how in ((), ("--detach",)):
        code, out, err, took = fe(none, "run", *how, "--headless", "--audio",
                                  "none", "-f", work / "none.tgz")
        left = [p.name for p in b.dir.parent.iterdir() if none.name in p.name]
        check(f"`run {' '.join(how)} -f` with a firmware that is not there: "
              "one line that says so, at once, and no device left behind",
              code == 1 and "No such file" in out + err
              and "Traceback" not in out + err and took < 10 and not left,
              f"{out + err}\nleft: {left}")
    (work / "text.tgz").write_text("no firmware at all\n")
    code, out, err, took = fe(none, "run", "--detach", "--headless", "--audio",
                              "none", "-f", work / "text.tgz")
    left = [p.name for p in b.dir.parent.iterdir() if none.name in p.name]
    check("... and with a file that is no firmware: the same",
          code == 1 and "not an update package" in out + err
          and "Traceback" not in out + err and took < 10 and not left,
          f"{out + err}\nleft: {left}")
    code, out, err, took = fe(b, "run", "--detach", "--headless", "--audio",
                              "none")
    log = b.dir / "run.log"
    check("`run --detach`: back when the emulator is up, with its lines and "
          "where it prints from then on",
          code == 0 and "runs on by itself" in out and "USB serial" in out
          and log.is_file() and took < 60, out + err)
    code, out, err, took = fe(b, "wait", "--timeout", 90)
    check("... `wait` after it: its CLI answers",
          code == 0 and "its CLI answers" in out, out + err)
    code, out, err, took = fe(b, "wait", "--log", "loader|desktop|usb",
                              "--timeout", 30)
    check("... and its log is where `wait --log` reads it", code == 0,
          out + err)
    code, out, err, took = fe(b, "run", "--detach", "--headless", "--audio",
                              "none")
    check("`run --detach` on a device that runs: refused at once",
          code == 1 and "is running" in err and took < 10, out + err)
    had = launchers(b.name)
    fe(b, "stop")
    end = time.time() + 15
    while time.time() < end and launchers(b.name):
        time.sleep(0.3)
    code, out, err, took = fe(b, "wait", "--timeout", 1)
    check("`stop`: it is not running, and the launcher that ran it by itself "
          "has gone",
          len(had) == 1 and not launchers(b.name) and code == 1
          and "not running" in err, f"before: {had}, now: {launchers(b.name)}")


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("firmware")
    ap.add_argument("--timeout", type=float, default=120.0,
                    help="seconds to wait for the firmware to start")
    ap.add_argument("--keep", action="store_true",
                    help="keep the throwaway device")
    args = ap.parse_args()

    b = Bench(f"waitcheck-{int(time.time())}-{os.getpid()}", args.firmware,
              args.timeout)
    work = pathlib.Path(tempfile.mkdtemp(prefix="fe-wait-"))
    try:
        code, out, err, took = fe(b, "wait", "--timeout", 2)
        check("a device that is not there: not running, exit 1",
              code == 1 and "not running after 2 s" in err, out + err)
        b.start(first=True, wait=False)
        code, out, err, took = fe(b, "wait", "--timeout", args.timeout)
        ok, said = b.answers() if code == 0 else (False, "")
        if check("`wait` right after the device was started: until its CLI "
                 "answers, which it then does",
                 code == 0 and "its CLI answers" in out and ok,
                 out + err + said + b.uart_text()[-2000:]):
            app = log_checks(b)
            if app:
                screen_checks(b, app, work)
            refused_checks(b, work)
            log = b.uart_text(every=True)
            crash = CRASH.search(log)
            check("no crash in the firmware's log", not crash,
                  crash and log[max(0, crash.start() - 300):crash.end() + 300])
            crash_checks(b)
            stop_checks(b)
            detach_checks(b, work)
    finally:
        b.stop()
        fe(b, "stop")
        shutil.rmtree(work, ignore_errors=True)
        if not args.keep:
            shutil.rmtree(b.dir, ignore_errors=True)
    bad = [label for label, ok in results if not ok]
    print(f"{len(results) - len(bad)} of {len(results)} checks passed"
          + (f"; device kept: {b.dir}" if args.keep else ""))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
