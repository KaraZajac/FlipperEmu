#!/usr/bin/env python3
# Copyright (c) 2026, Kara Zajac
# SPDX-License-Identifier: BSD-3-Clause
"""Check `./flipperemu gdb`: gdb on a running device, with the firmware's and
an app's symbols.

    scripts/gdb_check.py firmware/official-1.5.1/flipper-z-f7-update-1.5.1.tgz

Needs `ufbt` on the PATH, with the SDK of the firmware that is given: its
toolchain has the gdb, its SDK the firmware's ELF and the gdb script that
knows the firmware's list of loaded apps.  Without them nothing is checked
and the exit status is 77.

Boots a throwaway device and builds examples/hello in a folder of its own.
Then, with gdb in batch mode each time: a backtrace of the firmware that
runs, with functions and lines; a breakpoint in the app's entry, set before
the app is on the Flipper, which `ufbt launch` runs into; a breakpoint on a
line of it, and a variable of it printed there; its crash, where gdb stops
and says what the firmware says of it; and after each of these the firmware
runs on, and the debug flag the firmware has for a probe is off.  Then the
stub on a TCP port, a second gdb turned away, what `status` and `continue`
say of a firmware a gdb has stopped, a gdb that dies with the
firmware stopped, and a device that does not run.  Exit status 0 = pass.
"""
import argparse
import os
import pathlib
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from fe_monitor import Monitor                                # noqa: E402
from files_check import Bench, ROOT, check, results          # noqa: E402
from ufbt_check import APP, SKIPPED, log_tail, logged, tool   # noqa: E402

# (the flag the firmware has for a debug probe: see scripts/fe_gdb.py)
FLAG = 'printf "debug flag %d\\n", furi_hal_debug_gdb_session_active'
OFF = "debug flag 0"


def batch(commands):
    out = ["--", "-batch"]
    for c in commands:
        out += ["-ex", c]
    return out


def gdb(b, *commands, options=(), timeout=180):
    """`./flipperemu gdb -- -batch -ex ...`: (exit status, what was said)"""
    return b.fe("gdb", *options, *batch(commands), timeout=timeout)


class Gdb:
    """One that is not waited for: it is at a `continue`, and something else
    makes the firmware get to where it stops"""

    def __init__(self, b, work, *commands):
        self.b = b
        self.path = work / f"gdb-{time.time():.3f}.txt"
        self.out = open(self.path, "wb")
        self.p = subprocess.Popen(
            [sys.executable, str(ROOT / "flipperemu"), "-d", b.name, "gdb",
             *batch(commands)], stdin=subprocess.DEVNULL, stdout=self.out,
            stderr=subprocess.STDOUT, start_new_session=True)

    def said(self):
        return self.path.read_text(errors="replace")

    def wait(self, running, seconds=60.0):
        """Until it is on the device, with the firmware stopped under it or
        (running) let go again.  What it writes says so too late: to a file,
        it writes when it ends."""
        end = time.time() + seconds
        while time.time() < end and self.p.poll() is None:
            try:
                m = Monitor(str(self.b.dir / "monitor.sock"))
                try:
                    on = any(line.startswith("gdb: filename=")
                             and "=disconnected:" not in line
                             for line in m.cmd("info chardev").splitlines())
                    runs = "running" in m.cmd("info status")
                finally:
                    m.close()
                if on and runs == running:
                    return True
            except OSError:
                pass
            time.sleep(0.2)
        return False

    def end(self, seconds=90.0):
        """-> (exit status: None when it had to be ended, what it said)"""
        try:
            code = self.p.wait(seconds)
        except subprocess.TimeoutExpired:
            code = None
            os.killpg(self.p.pid, signal.SIGKILL)
            self.p.wait()
        self.out.close()
        return code, self.said()


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def line_of(source, text):
    for n, line in enumerate(source.read_text().splitlines(), 1):
        if text in line:
            return n
    raise SystemExit(f"{source}: no line with {text!r}")


def gdb_checks(b, work, folder):
    code, text = tool(b, folder)
    fap = folder / "dist" / "fe_hello.fap"
    if not check("`ufbt` alone: the app is built", code == 0 and fap.is_file(),
                 text[-1500:]):
        return False
    code, text = b.fe("app", "--no-start", "--to", "/ext/apps/fe-check", fap)
    b.fe("rm", "-r", "/ext/apps/fe-check")
    if "which is this firmware's" not in text:
        print("  ufbt's SDK is not for this firmware:\n        "
              + text.strip().replace("\n", "\n        "))
        return None

    code, text = gdb(b, "bt 3", FLAG)
    if "no ELF of the firmware" in text or "has no Python" in text:
        print("  " + text.strip().replace("\n", "\n  "))
        return None
    check("gdb on the firmware that runs: a backtrace with functions and lines",
          code == 0 and "the firmware's symbols:" in text
          and "#0 " in text and ".c:" in text and " in ?? ()" not in text, text)
    check("the firmware's debug flag is off under it", OFF in text, text)
    ok, text = b.answers()
    check("gdb gone: the firmware runs on, and its CLI answers", ok, text)

    # ---- a breakpoint in an app that is not on the Flipper yet
    g = Gdb(b, work, "break fe_hello_app", "continue", "bt 2", "next", "next",
            "info locals", FLAG)
    waiting = g.wait(running=True)
    code, text = tool(b, folder, "launch")
    code2, said = g.end()
    check("a breakpoint in the app's entry, set before the app is there: "
          "`ufbt launch` runs into it",
          waiting and code2 == 0 and "Breakpoint 1, fe_hello_app (" in said
          and "hello.c:" in said, said + text[-600:])
    check("the app's source lines and its variables there",
          "view_port = " in said and "hello = {" in said and OFF in said, said)
    code2, text2 = b.fe("app")
    check("gdb gone: the app runs on",
          code == 0 and f'"{APP}" runs' in text2
          and logged(b, "[FeHello] started"), text[-800:] + text2 + log_tail(b))

    # ---- a line of the app that runs, and what a variable holds there
    source = folder / "hello.c"
    g = Gdb(b, work, f"break hello.c:{line_of(source, 'which makes')}",
            "continue", "print hello.presses", "print hello.last")
    waiting = g.wait(running=True)
    b.fe("press", "up")
    code, said = g.end()
    check("gdb on an app that runs already: a breakpoint on a line of it, and "
          "its variables there",
          waiting and code == 0 and "$1 = 1" in said and '"Up"' in said, said)
    check("gdb gone: the app goes on from there",
          logged(b, "[FeHello] Up, which makes 1"), log_tail(b))

    # ---- its crash
    g = Gdb(b, work, "continue", "bt 6", FLAG)
    waiting = g.wait(running=True)
    b.fe("press", "ok", "--hold", "1200")
    code, said = g.end()
    check("the app crashes: gdb stops there, and says what the firmware says "
          "of it",
          waiting and code == 0
          and "the firmware crashed: fe_hello: on purpose" in said
          and "`continue` lets it restart" in said, said)
    check("... with the app's line in the backtrace, and the flag still off",
          "fe_hello_app (" in said and f"hello.c:{line_of(source, 'furi_crash(')}"
          in said and OFF in said, said)
    end = time.time() + b.timeout
    while time.time() < end and b.uart_text().count("Startup complete") < 2:
        time.sleep(0.3)
    time.sleep(2)
    ok, text = b.answers()
    check("gdb gone: the firmware restarts as it does without one",
          "[CRASH]" in b.uart_text()
          and b.uart_text().count("Startup complete") == 2 and ok,
          text + log_tail(b))

    # ---- the stub on a TCP port, for an IDE
    port = free_port()
    code, text = b.fe("gdb", "--serve", port)
    init = (b.dir / "gdb.init").read_text()
    check("`gdb --serve PORT`: the stub is on that port of this computer, "
          "and says how to get on it",
          code == 0 and f"target remote 127.0.0.1:{port}" in init
          and f"localhost:{port}" in text, text + init)
    own = shlex.split(text.strip().splitlines()[-1]) if code == 0 else []
    r = None
    if own and "gdb" in pathlib.Path(own[0]).name:
        r = subprocess.run([*own, "-batch", "-ex", "bt 1", "-ex", FLAG],
                           capture_output=True, text=True, timeout=180,
                           stdin=subprocess.DEVNULL)
    check("a gdb of one's own, as it was told: on it, with the symbols",
          r is not None and r.returncode == 0 and "#0 " in r.stdout
          and OFF in r.stdout, r and r.stdout + r.stderr)
    code, text = gdb(b, "bt 1")
    check("`gdb` after it: takes the stub where it is",
          code == 0 and "#0 " in text, text)

    # ---- two at once, and one that dies
    g = Gdb(b, work, "python import time; time.sleep(12)")
    waiting = g.wait(running=False)
    code, text = gdb(b, "bt 1")
    check("a second gdb while one is on it: turned away",
          waiting and code == 1 and "a gdb is on it already" in text, text)
    code, text = b.fe("status", "--json")
    code2, text2 = b.fe("continue")
    check("stopped under a gdb: `status` says whose stop it is, and `continue` "
          "leaves it to the gdb",
          code == 0 and '"stopped": "debug"' in text
          and code2 == 1 and "a gdb is on it" in text2, text + text2)
    g.end()
    code, text = gdb(b, "python import os, signal; "
                        "os.kill(os.getpid(), signal.SIGKILL)")
    ok, text2 = b.answers()
    check("a gdb that dies with the firmware stopped: the firmware is let go",
          code != 0 and "it runs again" in text and ok, text + text2)
    return True


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

    b = Bench(f"gdbcheck-{int(time.time())}-{os.getpid()}", args.firmware, args.timeout)
    work = pathlib.Path(tempfile.mkdtemp(prefix="fe-gdb-")).resolve()
    folder = work / "fe_hello"
    folder.mkdir()
    for name in ("application.fam", "hello.c"):
        shutil.copy(ROOT / "examples" / "hello" / name, folder)
    done = False
    try:
        if check("boot: 'Startup complete'", b.start(first=True),
                 b.uart_text()[-2000:]):
            time.sleep(2)
            done = gdb_checks(b, work, folder)
            b.stop()
            if done:
                code, text = gdb(b, "bt 1")
                check("a stopped device: `gdb` says that it is not running",
                      code == 1 and "is not running" in text, text)
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
