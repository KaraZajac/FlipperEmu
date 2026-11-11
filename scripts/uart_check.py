#!/usr/bin/env python3
# Copyright (c) 2026, Kara Zajac
# SPDX-License-Identifier: BSD-3-Clause
"""Check the serial port on header pins 15 and 16 (LPUART1): the launcher's
`uart`, with a script on the Flipper that answers each line.

    scripts/uart_check.py firmware/official-1.5.1/flipper-z-f7-update-1.5.1.tgz

Starts a throwaway device and puts examples/uart/echo.js on its card.  What
is sent while no app has the port open is lost, as with nothing wired.  Then
the firmware runs the script (its CLI's `js`): the port opens although the
firmware has slept since the start (which once hung it), "ready" comes, and
`uart send --expect` gets each line answered: text, the \\ codes, bytes in
hex, 900 characters in their order.  `uart wait` finds what came after a send
that did not wait, and does not find it twice; what does not come is exit
status 1 after the time given.  The pins say what they are in `gpio`.  A
program on the port's pty is answered too, and hears what `uart send` gets;
`uart term` on a terminal of its own, with a second `uart` refused meanwhile.
The script again, and after a reset.  The log port (pins 13 and 14): `uart
wait` reads it, `uart send` says it cannot yet.  Last a start with --lpuart
none (an app that opens the port still runs) and one with --lpuart tcp:PORT.
Exit status 0 = pass.
"""
import argparse
import os
import pathlib
import pty
import select
import shutil
import socket
import string
import subprocess
import sys
import time
import tty

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from files_check import ROOT, Bench, CRASH, check, results  # noqa: E402

ECHO = ROOT / "examples" / "uart" / "echo.js"
ONCE = """let serial = require("serial");
serial.setup("lpuart", 115200);
serial.write("hello\\r\\n");
serial.end();
print("once done");
"""


def fe(b, *args, timeout=600):
    """A launcher command: (exit status, what it printed, what it said on
    stderr, the seconds it took)"""
    t0 = time.time()
    r = subprocess.run([sys.executable, str(ROOT / "flipperemu"), "-d", b.name,
                        *map(str, args)], capture_output=True, timeout=timeout,
                       encoding="utf-8", errors="replace",
                       stdin=subprocess.DEVNULL)
    return r.returncode, r.stdout, r.stderr, time.time() - t0


def start(b, timeout, *more):
    """Another run of the device, with more words for `run`: (its CLI
    answers, what was said)"""
    if b.uart.exists():
        b.uart.rename(b.dir / f"uart-run{b.runs}.log")
    b.runs += 1
    b.log = open(b.dir / "qemu.log", "ab")
    b.proc = subprocess.Popen(
        [sys.executable, str(ROOT / "flipperemu"), "-d", b.name, "run",
         "--headless", "--audio", "none", "--serial", f"file:{b.uart}", *more],
        stdout=b.log, stderr=subprocess.STDOUT)
    end = time.time() + timeout
    while time.time() < end and "Startup complete" not in b.uart_text():
        time.sleep(0.2)
    code, out, err, took = fe(b, "wait", "--timeout", timeout)
    return code == 0, out + err


def row(table, pin):
    """A pin's line of `gpio`, its blanks made one"""
    for line in table.splitlines():
        words = line.split()
        if len(words) > 1 and words[1] == pin:
            return " ".join(words)
    return ""


def sent(b):
    """What the firmware sent on the port, this run"""
    try:
        return (b.dir / "lpuart.log").read_bytes()
    except OSError:
        return b""


def script(b, path="/ext/echo.js"):
    """The firmware runs a script, in its CLI; not waited for"""
    return b.bg("cli", "--timeout", 300, f"js {path}")


def ended(p, seconds=60):
    """What a script's CLI showed, when it is over"""
    try:
        return p.communicate(timeout=seconds)[0]
    except subprocess.TimeoutExpired:
        p.kill()
        return "(it did not end) " + p.communicate()[0]


def turn(b, word, label):
    """A whole run of the script: ready, a line answered, bye"""
    p = script(b)
    c1, o1, e1, _ = fe(b, "uart", "wait", "ready", "--for", 30)
    c2, o2, e2, _ = fe(b, "uart", "send", word + "\\n", "--expect",
                       f"you said: {word}\\r\\n")
    c3, o3, e3, _ = fe(b, "uart", "send", "bye\\n", "--expect", "closing")
    said = ended(p)
    return check(label, (c1, c2, c3) == (0, 0, 0) and "ready" in o1
                 and "echo done" in said,
                 f"wait: {c1} {o1!r} {e1}\nsend: {c2} {o2!r} {e2}\n"
                 f"bye: {c3} {o3!r} {e3}\nthe CLI: {said}")


def open_checks(b):
    """-> the script's CLI, while the script runs (None: it did not start)"""
    code, out, err, took = fe(b, "uart")
    link = b.dir / "lpuart.tty"
    lines = out.splitlines()
    check("`uart` alone: where the two ports go (pins 15 and 16 to a pty, "
          "with the device's lpuart.tty as its name)",
          code == 0 and len(lines) == 2 and link.is_symlink()
          and f"pins 15 and 16 (LPUART1): {os.readlink(link)}" in lines[0]
          and "pins 13 and 14 (USART1): a file" in lines[1], out + err)
    code, out, err, took = fe(b, "uart", "send", "stale\\n")
    check("a line sent while no app has the port open: taken (and lost, as "
          "with nothing wired), exit 0", code == 0 and took < 5,
          f"{code} in {took:.1f} s\n{out}{err}")
    once = b.dir / "uart_once.js"
    once.write_text(ONCE)
    c1, o1, e1, _ = fe(b, "push", ECHO, "/ext")
    c2, o2, e2, _ = fe(b, "push", once, "/ext")
    if not check("the two scripts are on the card", (c1, c2) == (0, 0),
                 o1 + e1 + o2 + e2):
        return None
    p = script(b)
    code, out, err, took = fe(b, "uart", "wait", "ready", "--for", 30)
    if not check("an app opens the port (after the firmware has slept): it "
                 "does not hang, and `uart wait` has its first line",
                 code == 0 and "ready" in out, f"{code} {out!r} {err}"):
        print("        the CLI: " + ended(p, 5))
        return None
    code, out, err, took = fe(b, "uart", "send", "ping 1\\r\\n", "--expect",
                              "ping 1")
    check("`uart send --expect`: the line is answered, and the answer printed",
          code == 0 and "you said: ping 1" in out, f"{code} {out!r} {err}")
    check("... in well under a second", took < 1.0, f"{took:.2f} s")
    check("... and what was sent before the port was open did not wait for it",
          b"stale" not in sent(b), sent(b))
    code, out, err, took = fe(b, "uart", "send", "--hex", "41 42 43 0a",
                              "--expect", "ABC")
    check("bytes in hex (--hex)", code == 0 and "you said: ABC" in out,
          f"{code} {out!r} {err}")
    code, out, err, took = fe(b, "uart", "send", r"tab\there\x21\\\n",
                              "--expect", r"here!\\")
    check(r"the codes for what cannot be typed (\t \xNN \\ \n), both ways",
          code == 0 and "you said: tab\there!\\" in out, f"{code} {out!r} {err}")
    big = (string.ascii_letters + string.digits) * 15
    big = big[:900]
    code, out, err, took = fe(b, "uart", "send", big + "\\n", "--expect",
                              big[-40:] + "\\r\\n", "--for", 20)
    check("900 characters in a line: back, in their order",
          code == 0 and "you said: " + big in out,
          f"{code} in {took:.1f} s\n{out!r}\n{err}")
    code, out, err, took = fe(b, "uart", "send", "no expect\\n")
    c2, o2, e2, _ = fe(b, "uart", "wait", "no expect\\r\\n")
    check("a send that does not wait prints nothing; `uart wait` then has "
          "the answer", code == 0 and not out and c2 == 0
          and "you said: no expect" in o2, f"{code} {out!r} {err}\n{c2} {o2!r} {e2}")
    code, out, err, took = fe(b, "uart", "wait", "no expect", "--for", 1)
    check("what was found is not found again: exit 1 after the time given",
          code == 1 and "did not come in 1 s" in err and 1 <= took < 4,
          f"{code} in {took:.1f} s\n{out!r}\n{err}")
    code, out, err, took = fe(b, "uart", "send", "quiet\\n", "--expect", "pong",
                              "--for", 1)
    check("an answer that does not come: exit 1, with what did come",
          code == 1 and "'pong' did not come in 1 s" in err
          and "you said: quiet" in out, f"{code} {out!r} {err}")
    code, out, err, took = fe(b, "gpio")
    tx, rx = row(out, "PC1"), row(out, "PC0")
    check("`gpio` while the port is open: pins 15 and 16 are the "
          "peripheral's", "alternate function" in tx
          and "alternate function" in rx, f"{tx}\n{rx}\n{err}")
    return p


def pty_checks(b):
    fd = os.open(b.dir / "lpuart.tty", os.O_RDWR | os.O_NOCTTY)
    got = b""

    def hear(want, seconds):
        nonlocal got
        end = time.time() + seconds
        while time.time() < end and want not in got:
            if select.select([fd], [], [], 0.05)[0]:
                got += os.read(fd, 65536)
        return want in got

    def ask(text):
        t0 = time.time()
        os.write(fd, text.encode() + b"\n")
        return hear(b"you said: " + text.encode() + b"\r\n", 10), time.time() - t0

    try:
        tty.setraw(fd)
        ok, took = ask("pty 1")
        check("a program on the port's pty: its line is answered", ok,
              f"in {took:.1f} s: {got!r}")
        ok, took = ask("pty 2")
        check("... and its next one at once", ok and took < 0.5,
              f"in {took:.2f} s: {got!r}")
        code, out, err, took = fe(b, "uart", "send", "both\\n", "--expect",
                                  "you said: both")
        check("`uart send` beside that program: answered, and the program "
              "hears the answer too",
              code == 0 and hear(b"you said: both\r\n", 5),
              f"{code} {out!r} {err}\nthe pty: {got!r}")
    finally:
        os.close(fd)


def term_checks(b):
    """`uart term` on a terminal of its own, a line typed there, and another
    `uart send` meanwhile"""
    master, slave = pty.openpty()
    p = subprocess.Popen([sys.executable, str(ROOT / "flipperemu"), "-d", b.name,
                          "uart", "term"], stdin=slave, stdout=slave,
                         stderr=slave, start_new_session=True)
    os.close(slave)
    out = b""

    def pump(until, seconds):
        nonlocal out
        end = time.time() + seconds
        while time.time() < end and (until is None or until not in out):
            if select.select([master], [], [], 0.1)[0]:
                try:
                    out += os.read(master, 65536)
                except OSError:
                    break

    other = (None, "", "", 0)
    try:
        pump(b"quits", 20)
        pump(None, 0.5)         # (what is typed before it has the terminal
        os.write(master, b"term 1\r")           # is thrown away with the
        pump(b"you said: term 1", 10)           # terminal's line mode)
        other = fe(b, "uart", "send", "meanwhile\\n")
        pump(None, 0.3)
        os.write(master, b"\x1d")               # Ctrl-]
        try:
            p.wait(15)
        except subprocess.TimeoutExpired:
            pass
    finally:
        if p.poll() is None:
            p.kill()
            p.wait()
        os.close(master)
    text = out.decode(errors="replace")
    check("`uart term`: what is typed goes out, the answer shows, Ctrl-] "
          "leaves it", p.returncode == 0 and "you said: term 1" in text,
          f"exit {p.returncode}\n{text}")
    check("a second `uart` meanwhile: told who has the pins, exit 1",
          other[0] == 1 and "pins 15 and 16 are in use" in other[2]
          and "uart term" in other[2], f"{other[0]} {other[1]} {other[2]}")


def close_checks(b, p):
    code, out, err, took = fe(b, "uart", "send", "bye\\n", "--expect", "closing")
    said = ended(p)
    check("the script closes the port and ends",
          code == 0 and "echo done" in said and CRASH.search(said) is None,
          f"{code} {out!r} {err}\nthe CLI: {said}")
    code, out, err, took = fe(b, "gpio")
    tx, rx = row(out, "PC1"), row(out, "PC0")
    check("`gpio` after: the two pins are nobody's again",
          "alternate" not in tx and "alternate" not in rx and tx and rx,
          f"{tx}\n{rx}\n{err}")


def other_checks(b):
    code, out, err, took = fe(b, "uart", "wait", "--port", "usart",
                              "Startup complete")
    check("the log port (pins 13 and 14): `uart wait --port usart` reads it",
          code == 0 and "Startup complete" in out, f"{code} {out[-300:]!r} {err}")
    code, out, err, took = fe(b, "uart", "send", "--port", "usart", "x")
    check("... `uart send` there: says it is for pins 15 and 16 so far, exit 1",
          code == 1 and "pins 15 and 16 so far" in err, f"{code} {out} {err}")
    c1, o1, e1, _ = fe(b, "uart", "send", "--hex", "zz")
    c2, o2, e2, _ = fe(b, "uart", "send")
    c3, o3, e3, _ = fe(b, "uart", "wait")
    c4, o4, e4, _ = fe(b, "uart", "send", "x", "--for", 0)
    check("refused, each with its reason: hex that is none, nothing to send, "
          "nothing to wait for, no time",
          (c1, c2, c3, c4) == (1, 1, 1, 1) and "not bytes in hex" in e1
          and "what?" in e2 and "for what?" in e3 and "--for" in e4,
          f"{c1} {e1}{c2} {e2}{c3} {e3}{c4} {e4}")


def none_checks(b, timeout):
    b.stop()
    ok, said = start(b, timeout, "--lpuart", "none")
    if not check("a start with --lpuart none", ok, said):
        return
    code, out, err, took = fe(b, "uart")
    c2, o2, e2, _ = fe(b, "uart", "send", "x")
    check("... `uart` says the pins go nowhere, and `uart send` that it "
          "cannot (exit 1)",
          code == 0 and "pins 15 and 16 (LPUART1): nowhere" in out
          and not (b.dir / "lpuart.tty").is_symlink()
          and c2 == 1 and "nowhere" in e2, f"{code} {out}{err}\n{c2} {o2}{e2}")
    code, out, err, took = fe(b, "cli", "--timeout", 60, "js /ext/uart_once.js")
    check("... an app that opens the port and writes to it runs to its end",
          "once done" in out, f"{code} in {took:.1f} s\n{out}{err}")


def tcp_checks(b, timeout):
    b.stop()
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    ok, said = start(b, timeout, "--lpuart", f"tcp:{port}")
    if not check("a start with --lpuart tcp:PORT", ok, said):
        return
    code, out, err, took = fe(b, "uart")
    check("... `uart` names the port",
          code == 0 and f"TCP port {port} of 127.0.0.1" in out, out + err)
    got = b""
    said = ""
    code = None
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=5) as c:
            def hear(want, seconds):
                nonlocal got
                end = time.time() + seconds
                while time.time() < end and want not in got:
                    if select.select([c], [], [], 0.05)[0]:
                        data = c.recv(65536)
                        if not data:
                            break
                        got += data
                return want in got

            p = script(b)
            ready = hear(b"ready\r\n", 30)
            c.sendall(b"tcp 1\n")
            answered = hear(b"you said: tcp 1\r\n", 10)
            code, out, err, took = fe(b, "uart", "send", "bye\\n", "--expect",
                                      "closing")
            said = ended(p)
            heard = hear(b"closing\r\n", 5)
    except OSError as e:
        ready = answered = heard = False
        said += f"\n{e}"
    check("... a program that connects there has the app's first line, and "
          "its own line answered", ready and answered, f"{got!r}\n{said}")
    check("... and `uart send` works beside it",
          code == 0 and heard and "echo done" in said,
          f"{code}\n{got!r}\nthe CLI: {said}")


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("firmware")
    ap.add_argument("--timeout", type=float, default=120.0,
                    help="seconds to wait for the firmware to start")
    ap.add_argument("--keep", action="store_true",
                    help="keep the throwaway device")
    args = ap.parse_args()

    b = Bench(f"uartcheck-{int(time.time())}-{os.getpid()}", args.firmware,
              args.timeout)
    try:
        code, out, err, took = fe(b, "uart")
        check("a device that is not running: says so, exit 1",
              code == 1 and "not running" in err, out + err)
        ok = b.start(first=True)
        said = ""
        if ok:
            code, out, err, took = fe(b, "wait", "--timeout", args.timeout)
            ok, said = b.answers() if code == 0 else (False, out + err)
        if check("the firmware starts, and its CLI answers", ok,
                 said + b.uart_text()[-2000:]):
            p = open_checks(b)
            if p:
                pty_checks(b)
                term_checks(b)
                close_checks(b, p)
                turn(b, "again", "the script a second time: ready, answered, "
                                 "closed")
                code, out, err, took = fe(b, "reset")
                c2, o2, e2, _ = fe(b, "wait", "--timeout", args.timeout)
                if check("the firmware reset, and up again",
                         (code, c2) == (0, 0), out + err + o2 + e2):
                    turn(b, "after reset", "the script after a reset: ready, "
                                           "answered, closed")
                other_checks(b)
                none_checks(b, args.timeout)
                tcp_checks(b, args.timeout)
            ok, said = b.answers()
            check("the CLI still answers", ok, said)
            log = b.uart_text(every=True)
            crash = CRASH.search(log)
            check("no crash in the firmware's log", not crash,
                  crash and log[max(0, crash.start() - 300):crash.end() + 300])
    finally:
        b.stop()
        fe(b, "stop")
        if not args.keep:
            shutil.rmtree(b.dir, ignore_errors=True)
    bad = [label for label, ok in results if not ok]
    print(f"{len(results) - len(bad)} of {len(results)} checks passed"
          + (f"; device kept: {b.dir}" if args.keep else ""))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
