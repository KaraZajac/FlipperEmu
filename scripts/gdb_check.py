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
runs on, and the debug flag the firmware has for a probe is off.  Then a
breakpoint in a stock app, by its name: where the update server's symbols of
the stock apps are beside the firmware (scripts/fetch_firmware.py puts them
there; skipped without them).  Then the
stub on a TCP port, and `gdb --vscode`: the debug configuration it writes
into the app's folder, into the launch.json that `ufbt vscode_dist` made
there, and not into a file that is none; and that configuration used as VS
Code's debugger would use it, by a gdb that is driven over its machine
interface with the firmware running meanwhile: a breakpoint by the source
file's full path, set before the app starts and run into, a pause, and a
disconnect that leaves the firmware running.  (VS Code itself is not
started.)  Then a second gdb turned away, what `status` and `continue`
say of a firmware a gdb has stopped, a gdb that dies with the
firmware stopped, and a device that does not run.  Exit status 0 = pass.
"""
import argparse
import difflib
import json
import os
import pathlib
import re
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from fe_monitor import Monitor                                # noqa: E402
from files_check import Bench, ROOT, check, results          # noqa: E402
from ufbt_check import APP, SKIPPED, log_tail, logged, tool   # noqa: E402

# (the flag the firmware has for a debug probe: see scripts/fe_gdb.py)
FLAG = 'printf "debug flag %d\\n", furi_hal_debug_gdb_session_active'
OFF = "debug flag 0"
STOCK = "/ext/apps/Games/snake_game.fap"    # a stock app that needs nothing
OURS = "/ext/apps/Examples/fe_hello.fap"    # where `ufbt launch` puts the example


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


class Front:
    """A gdb as an IDE's debugger has it: told what to do over its machine
    interface (GDB/MI), and with the firmware running while it is told more
    (mi-async), which is how VS Code's Cortex-Debug drives one"""

    def __init__(self, program, folder):
        self.p = subprocess.Popen(
            [program, "-q", "--interpreter=mi2"], cwd=folder, text=True,
            errors="replace", stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, start_new_session=True)
        self.lines = []
        threading.Thread(target=self.read, daemon=True).start()

    def read(self):
        for line in self.p.stdout:
            self.lines.append(line.rstrip("\n"))

    def said(self):
        return "\n".join(line[:300] for line in self.lines[-60:])

    def until(self, what, start, seconds=30.0):
        """The first line from `start` on that begins with (one of) `what`,
        or "" when none came in time"""
        end = time.time() + seconds
        while True:
            for line in self.lines[start:]:
                if line.startswith(what):
                    return line
            if time.time() >= end or self.p.poll() is not None:
                return ""
            time.sleep(0.05)

    def do(self, command, answer="^done", seconds=30.0):
        """A command: the line that answers it, or "" for an error or for
        none in time"""
        start = len(self.lines)
        try:
            self.p.stdin.write(command + "\n")
            self.p.stdin.flush()
        except OSError:
            return ""
        line = self.until((answer, "^error"), start, seconds)
        return line if line.startswith(answer) else ""

    def end(self, seconds=20.0):
        """It leaves: its exit status, None when it had to be ended"""
        try:
            self.p.stdin.write("-gdb-exit\n")
            self.p.stdin.flush()
            return self.p.wait(seconds)
        except (OSError, subprocess.TimeoutExpired):
            if self.p.poll() is None:
                os.killpg(self.p.pid, signal.SIGKILL)
            self.p.wait()
            return None


def at(b, folder, *args, env=None, timeout=600):
    """A launcher command in a folder: (exit status, what it printed)"""
    r = subprocess.run([sys.executable, str(ROOT / "flipperemu"), "-d", b.name,
                        *map(str, args)], cwd=folder, capture_output=True,
                       text=True, timeout=timeout, stdin=subprocess.DEVNULL,
                       env=env)
    return r.returncode, r.stdout + r.stderr


def relaxed(text):
    """A launch.json as VS Code reads it: with comments on lines of their
    own, and a comma before a closing bracket (ufbt's has both)"""
    text = "\n".join(line for line in text.splitlines()
                     if not line.lstrip().startswith("//"))
    return json.loads(re.sub(r",(\s*[}\]])", r"\1", text))


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


def vscode_checks(b, work, folder, port, program):
    """`gdb --vscode`, with the stub on that port already"""
    launch = folder / ".vscode" / "launch.json"
    name = f"FlipperEmu ({b.name})"
    serve = ("gdb", "--vscode", "--serve", port)

    # (first: no SDK where it is looked for.  The next `gdb` finds it again.)
    empty = work / "no-sdk"
    empty.mkdir()
    code, text = at(b, work, "gdb", "--serve", port, "--with", program,
                    env={**os.environ, "UFBT_HOME": str(empty)})
    check("`gdb` where ufbt's SDK is not: says that apps will have no symbols",
          code == 0 and ("no symbols of apps: " in text and str(empty) in text)
          == ("the firmware's symbols: " in text), text)

    code, text = at(b, folder, *serve)
    try:
        configs = json.loads(launch.read_text())["configurations"]
    except (OSError, ValueError, KeyError) as e:
        configs = [{"error": str(e)}]
    cfg = configs[0]
    try:
        init = "".join(pathlib.Path(c.split(None, 1)[1]).read_text()
                       for c in cfg["postAttachCommands"])
        sound = (cfg["type"] == "cortex-debug" and cfg["request"] == "attach"
                 and cfg["servertype"] == "external" and cfg["name"] == name
                 and cfg["gdbTarget"] == f"localhost:{port}"
                 and pathlib.Path(cfg["executable"]).is_file()
                 and pathlib.Path(cfg["gdbPath"]).is_file())
    except (OSError, KeyError, IndexError) as e:
        init, sound = str(e), False
    if not check("`gdb --vscode`: a debug configuration for VS Code in the "
                 "folder, with this stub, the firmware's ELF and a gdb",
                 code == 0 and len(configs) == 1 and sound and "is there" in text
                 and "fe_gdb.py" in init and "\nfile " not in init
                 and "target remote" not in init, text + repr(configs) + init):
        return

    # ---- the launch.json ufbt makes: comments, and commas JSON does not take
    shutil.rmtree(launch.parent)
    code, text = tool(b, folder, "vscode_dist")
    before = launch.read_text() if launch.is_file() else ""
    if code or "//" not in before:
        print("  SKIP  ufbt's own launch.json: `ufbt vscode_dist` made none "
              "with comments here", flush=True)
    else:
        code, text = at(b, folder, *serve)
        once = launch.read_text()
        code2, text2 = at(b, folder, *serve)
        after = launch.read_text()
        try:
            theirs = [c.get("name") for c in relaxed(before)["configurations"]]
            names = [c.get("name") for c in relaxed(after)["configurations"]]
        except (ValueError, KeyError) as e:
            theirs, names = [], [str(e)]
        gone = [line for line in difflib.ndiff(before.splitlines(),
                                               after.splitlines())
                if line.startswith("- ")]
        check("a folder `ufbt vscode_dist` has set up: added to ufbt's "
              "configurations, whose file is else as it was, comments too; "
              "and once more leaves one of it",
              code == 0 and code2 == 0 and '" added' in text
              and "is as of now" in text2 and theirs and not gone
              and names == theirs + [name] and once == after,
              text + text2 + repr(names) + repr(gone))
    none = "{ this is no launch.json\n"
    launch.write_text(none)
    code, text = at(b, folder, *serve)
    check("a launch.json that is none: left as it is, and the configuration "
          "is printed instead",
          code == 0 and launch.read_text() == none and "left as it is" in text
          and '"type": "cortex-debug"' in text, text)
    shutil.rmtree(launch.parent)

    # ---- what VS Code's debugger does with it
    source = folder / "hello.c"
    line = line_of(source, "which makes")
    front = Front(cfg["gdbPath"], folder)
    try:
        on = (front.do("-gdb-set mi-async on")
              and front.do(f'-file-exec-and-symbols "{cfg["executable"]}"')
              and front.do(f'-target-select extended-remote {cfg["gdbTarget"]}',
                           "^connected")
              and all(front.do(f'-interpreter-exec console "{c}"')
                      for c in cfg["postAttachCommands"]))
        pending = front.do(f'-break-insert -f "{source}:{line}"')
        running = front.do("-exec-continue", "^running")
        check("a debugger as VS Code's, on that configuration: connected, and "
              "a breakpoint by the file's full path waits for the app",
              bool(on) and "<PENDING>" in pending and bool(running),
              front.said())
        code, text = b.fe("app", OURS)
        mark = len(front.lines)
        b.fe("press", "down")
        hit = front.until("*stopped", mark)
        value = front.do("-data-evaluate-expression hello.presses")
        flag = front.do(f"-data-evaluate-expression {FLAG.split()[-1]}")
        check("the app starts and a button is pressed: stopped at that line, "
              "with the app's variables, and the debug flag off",
              code == 0 and 'reason="breakpoint-hit"' in hit
              and f'fullname="{source}"' in hit and f'line="{line}"' in hit
              and 'value="1"' in value
              and ('value="false"' in flag or 'value="0"' in flag),
              text + front.said())
        front.do("-exec-continue", "^running")
        paused = front.do("-exec-interrupt", "*stopped")
        check("its Pause: the firmware stops where it is",
              'reason="signal-received"' in paused, front.said())
        detached = front.do("-target-detach")
    finally:
        code = front.end()
    ok, text = b.answers()
    check("its Disconnect, a detach and then gdb leaves: the firmware runs "
          "on, and the app with it",
          bool(detached) and code == 0 and ok
          and logged(b, "[FeHello] Down, which makes 1"),
          text + front.said() + log_tail(b))
    b.fe("app", "--close")


def gdb_checks(b, work, folder):
    # (ufbt has one build folder for all, and an app's objects are kept there
    # with the path they were built from: a breakpoint by a file's full path,
    # an IDE's kind, needs them built from this folder)
    tool(b, folder, "-c")
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

    # ---- a stock app: its symbols are the update server's, beside the firmware
    symbols = pathlib.Path(b.firmware).resolve().parent / "appsymbols"
    if not (symbols / "snake_game_d.elf").is_file():
        print("  SKIP  a stock app under gdb: no appsymbols/ beside the "
              "firmware (scripts/fetch_firmware.py puts it there)", flush=True)
    else:
        g = Gdb(b, work, "break snake_game_app", "continue", "bt 2", FLAG)
        waiting = g.wait(running=True)
        code, text = b.fe("app", STOCK)
        code2, said = g.end()
        check("a stock app, its symbols from the update server beside the "
              "firmware: a breakpoint in it by name, and its lines",
              waiting and code2 == 0
              and "Breakpoint 1, snake_game_app (" in said
              and "snake_game.c:" in said and OFF in said, said + text)
        code2, text2 = b.fe("app", "--close")
        check("gdb gone: the stock app runs on, and closes",
              code == 0 and "runs" in text and code2 == 0 and "closed" in text2,
              text + text2)

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
    if own:
        vscode_checks(b, work, folder, port, own[0])

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
