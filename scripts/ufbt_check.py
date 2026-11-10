#!/usr/bin/env python3
# Copyright (c) 2026, Kara Zajac
# SPDX-License-Identifier: BSD-3-Clause
"""Check the firmware's own app tool, ufbt, against an emulated Flipper.

    scripts/ufbt_check.py firmware/official-1.5.1/flipper-z-f7-update-1.5.1.tgz

Needs `ufbt` on the PATH, with an SDK for the firmware that is given (ufbt
gets the one of the official release by itself, on its first run).  Without
it, or with an SDK for another API, nothing is checked and the exit status
is 77.

Boots a throwaway device, copies examples/hello to a folder of its own and
goes the way its developer goes: `./flipperemu ufbt launch` builds it, puts
it on the Flipper and starts it; its log lines are in the firmware's log;
buttons pressed are counted there; launched again while it runs, it is
closed and the new one started; its crash is in the log, and the Flipper
restarts.  Then `ufbt cli` in a terminal of its own, with a command typed
and a second command kept waiting; the targets that are refused, because
they go to a debug probe or bring a radio stack; and what a stopped device
takes.  Exit status 0 = pass.

`./flipperemu fbt` is checked with a stand-in for a firmware tree's `fbt`,
which says what it was called with: which fbt is found, where it is run, the
port it is given, what is refused.  No firmware is built here.
"""
import argparse
import os
import pathlib
import pty
import re
import select
import shutil
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from files_check import Bench, CRASH, ROOT, check, results   # noqa: E402

SKIPPED = 77
ANSI = re.compile(r"\x1b\[[0-9;]*m")
APP = "Hello FlipperEmu"


def tool(b, folder, *args, timeout=1800):
    """`./flipperemu ufbt ...` in the app's folder: (exit status, what it said)"""
    r = subprocess.run([sys.executable, str(ROOT / "flipperemu"), "-d", b.name,
                        "ufbt", *args], cwd=folder, capture_output=True,
                       text=True, timeout=timeout, stdin=subprocess.DEVNULL)
    text = (r.stdout + r.stderr).replace("\r", "\n")
    # (without the progress bars of a transfer)
    return r.returncode, "\n".join(line for line in text.splitlines()
                                   if line.strip() and "%" not in line)


def logged(b, what, seconds=15.0, count=1):
    """The firmware's log has `what` (count times) within some seconds"""
    end = time.time() + seconds
    while True:
        text = ANSI.sub("", b.uart_text())
        if text.count(what) >= count:
            return True
        if time.time() > end:
            return False
        time.sleep(0.3)


def log_tail(b, lines=25):
    return "\n".join(ANSI.sub("", b.uart_text()).splitlines()[-lines:])


def cli_session(b, folder):
    """`ufbt cli` on a terminal of its own.  A command is typed, and another
    launcher command is started meanwhile.  -> (its exit status, what its
    terminal showed, the other command's status and text)"""
    master, slave = pty.openpty()
    p = subprocess.Popen([sys.executable, str(ROOT / "flipperemu"), "-d", b.name,
                          "ufbt", "cli"], cwd=folder, stdin=slave, stdout=slave,
                         stderr=slave, start_new_session=True)
    os.close(slave)
    out = b""

    def pump(until, seconds):
        nonlocal out
        end = time.time() + seconds
        while time.time() < end and (until is None or until not in out):
            ready, _, _ = select.select([master], [], [], 0.2)
            if ready:
                try:
                    out += os.read(master, 65536)
                except OSError:
                    break

    other = (None, "")
    try:
        pump(b">: ", 60)
        os.write(master, b"uptime\r")
        pump(b"Uptime:", 15)
        other = b.fe("cli", "uptime", timeout=60)
        pump(None, 0.5)
        os.write(master, b"\x1d")               # Ctrl-]: the way out of it
        try:
            p.wait(15)
        except subprocess.TimeoutExpired:
            pass
    finally:
        if p.poll() is None:
            p.kill()
            p.wait()
        os.close(master)
    return p.returncode, out.decode(errors="replace"), other


def ufbt_checks(b, folder):
    code, text = tool(b, folder)
    fap = folder / "dist" / "fe_hello.fap"
    if not check("`ufbt` alone: the app is built", code == 0 and fap.is_file(),
                 text[-1500:]):
        return False

    code, text = b.fe("app", "--no-start", "--to", "/ext/apps/fe-check", fap)
    b.fe("rm", "-r", "/ext/apps/fe-check")
    if "which is this firmware's" not in text:
        print("  ufbt's SDK is not for this firmware:\n        "
              + text.strip().replace("\n", "\n        ")
              + "\n        (`ufbt update` gets another: docs/DEVELOPING.md)")
        return None

    code, text = tool(b, folder, "launch")
    code2, text2 = b.fe("app")
    check("`ufbt launch`: built, put on the Flipper and started",
          code == 0 and f'"{APP}" runs' in text2, text[-1500:] + text2)
    check("its log lines are in the firmware's log",
          logged(b, "[FeHello] started"), log_tail(b))
    b.fe("press", "up", "down", "left")
    check("buttons pressed there: it counts them",
          logged(b, "[FeHello] Left, which makes 3"), log_tail(b))

    code, text = tool(b, folder, "launch")
    code2, text2 = b.fe("app")
    check("launched again while it runs: closed, and the new one started",
          code == 0 and "App closed" in text and f'"{APP}" runs' in text2
          and logged(b, "[FeHello] left after 3")
          and logged(b, "[FeHello] started", count=2), text[-1500:] + text2)

    before = b.uart_text()
    crash = CRASH.search(before)
    check("no crash in the firmware's log so far", not crash,
          crash and before[max(0, crash.start() - 300):crash.end() + 300])
    b.fe("press", "ok", "--hold", "1200")
    check("its crash (a long OK) is in the log, with its message",
          logged(b, "fe_hello: on purpose") and "[CRASH]" in b.uart_text(),
          log_tail(b))
    end = time.time() + b.timeout
    while time.time() < end and b.uart_text().count("Startup complete") < 2:
        time.sleep(0.3)
    time.sleep(2)
    code, text = b.fe("app")
    ok, text2 = b.answers()
    check("the Flipper restarts after it, and its CLI answers again",
          b.uart_text().count("Startup complete") == 2 and "no app runs" in text
          and ok, text + text2 + log_tail(b))

    code, shown, (code2, text2) = cli_session(b, folder)
    check("`ufbt cli`: the firmware's prompt, a command typed and answered, "
          "left with Ctrl-]",
          code == 0 and ">: " in shown and "Uptime:" in shown, shown[-800:])
    check("another command meanwhile: told who has the port",
          code2 == 1 and "in use by" in text2 and "ufbt cli" in text2, text2)
    ok, text = b.answers()
    check("and the port is free after it", ok, text)

    code, text = tool(b, folder, "flash")
    check("`ufbt flash` (a debug probe's): refused, with what does it here",
          code == 1 and "debug probe" in text and "flash FILE" in text, text)
    code, text = tool(b, folder, "flash_usb")
    check("`ufbt flash_usb` (a package with a radio stack): refused, with what "
          "does it here",
          code == 1 and "radio stack" in text and "run --update" in text, text)
    code, text = tool(b, folder, "status")
    check("`ufbt status` (ufbt's own): passed on as it is",
          code == 0 and "SDK" in text.upper(), text)
    return True


def stand_in_tree(work):
    """A folder that looks like a firmware's tree to the launcher, with an
    `fbt` that says where it was run and with what: (the tree, a folder of an
    app in it)"""
    tree = (work / "tree").resolve()
    app = tree / "applications_user" / "my_app"
    app.mkdir(parents=True)
    (tree / "SConstruct").write_text("")
    fbt = tree / "fbt"
    fbt.write_text('#!/bin/sh\necho "stand-in fbt in $PWD: $*"\n')
    fbt.chmod(0o755)
    return tree, app


def fbt(b, folder, *args):
    r = subprocess.run([sys.executable, str(ROOT / "flipperemu"), "-d", b.name,
                        "fbt", *args], cwd=folder, capture_output=True,
                       text=True, timeout=120, stdin=subprocess.DEVNULL)
    return r.returncode, r.stdout + r.stderr


def fbt_checks(b, work):
    tree, app = stand_in_tree(work)
    port = f"FLIP_PORT={b.dir / 'usb.tty'}"
    code, text = fbt(b, app, "launch", "APPSRC=my_app")
    check("`fbt launch` in a folder of a tree: the tree's fbt, run in its "
          "root, with the emulated port named",
          code == 0
          and f"stand-in fbt in {tree}: launch APPSRC=my_app {port}" in text,
          text)
    code, text = fbt(b, tree, "flash_usb")
    check("`fbt flash_usb` (the package without a radio stack): passed on, "
          "with the port", code == 0 and f": flash_usb {port}" in text, text)
    code, text = fbt(b, tree, "cli", "FLIP_PORT=/dev/null")
    check("a port that is named already: passed on as it is",
          code == 0 and text.strip().endswith(": cli FLIP_PORT=/dev/null"),
          text)
    code, text = fbt(b, tree, "flash_usb_full")
    check("`fbt flash_usb_full` (with a radio stack): refused, with "
          "`fbt flash_usb` and `run --update`",
          code == 1 and "stand-in" not in text and "radio stack" in text
          and "`fbt flash_usb` sends" in text and "run --update" in text, text)
    code, text = fbt(b, tree, "flash")
    check("`fbt flash` (a debug probe's): refused",
          code == 1 and "stand-in" not in text and "debug probe" in text, text)
    code, text = fbt(b, work, "launch")
    check("`fbt` outside a firmware's tree: said so",
          code == 1 and "no `fbt` in this folder or above it" in text, text)
    return tree


def stopped_checks(b, folder, tree):
    code, text = tool(b, folder, "launch")
    check("a stopped device: `ufbt launch` says that it is not running",
          code == 1 and "is not running" in text, text)
    code, text = tool(b, folder)
    check("a stopped device: `ufbt` alone still builds", code == 0, text[-1500:])
    code, text = fbt(b, tree, "launch", "APPSRC=my_app")
    check("a stopped device: `fbt launch` says that it is not running",
          code == 1 and "stand-in" not in text and "is not running" in text,
          text)
    code, text = fbt(b, tree, "updater_package")
    check("a stopped device: `fbt` still builds",
          code == 0 and ": updater_package FLIP_PORT=" in text, text)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("firmware")
    ap.add_argument("--timeout", type=float, default=120.0,
                    help="seconds to wait for the firmware to start")
    ap.add_argument("--keep", action="store_true",
                    help="keep the throwaway device")
    args = ap.parse_args()

    if not shutil.which("ufbt"):
        print("skipped: no `ufbt` on the PATH (`pipx install ufbt` gets it)")
        return SKIPPED

    b = Bench(f"ufbtcheck-{int(time.time())}", args.firmware, args.timeout)
    work = pathlib.Path(tempfile.mkdtemp(prefix="fe-ufbt-"))
    folder = work / "fe_hello"
    folder.mkdir()
    for name in ("application.fam", "hello.c"):
        shutil.copy(ROOT / "examples" / "hello" / name, folder)
    done = False
    try:
        if check("boot: 'Startup complete'", b.start(first=True),
                 b.uart_text()[-2000:]):
            time.sleep(2)
            done = ufbt_checks(b, folder)
            tree = fbt_checks(b, work) if done else None
            b.stop()
            if done:
                stopped_checks(b, folder, tree)
    finally:
        b.stop()
        shutil.rmtree(work, ignore_errors=True)
        if not args.keep:
            shutil.rmtree(b.dir, ignore_errors=True)
    if done is None:
        print("skipped")
        return SKIPPED
    bad = [label for label, ok in results if not ok]
    print(f"{len(results) - len(bad)} of {len(results)} checks passed"
          + (f"; device kept: {b.dir}" if args.keep else ""))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
