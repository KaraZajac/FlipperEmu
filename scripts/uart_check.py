#!/usr/bin/env python3
# Copyright (c) 2026, Kara Zajac
# SPDX-License-Identifier: BSD-3-Clause
"""Check the serial ports of the header, pins 15 and 16 (LPUART1) and pins
13 and 14 (USART1): the launcher's `uart`, with a script on the Flipper that
answers each line.

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
The script again, and after a reset.  Then pins 13 and 14, where the firmware
logs: `uart wait --port usart` reads the log.  The firmware waits there for
an expansion module, with the port's receiver off and an interrupt on pin 14's
fall, and says so; a byte sent then is a module's call: pin 14 is low for a
bit in the pins' record, the firmware answers with its heartbeat at 9600 baud,
and waits again when no module goes on; a byte to the port that is off is a
start bit on pin 16.  The same script on that port is answered the same way
(a line, 900 characters, `uart term`, the two ports not in each other's way),
with the firmware's log back on the pins when the script has closed the port.
The port's speed: for a script at 1200 baud 240 bytes take the two seconds
they take on a wire, and --for, which is how long nothing may move, does not
cut them off.  The firmware's own USB-UART bridge (the GPIO app), which reads
its port by DMA: what the host writes on the USB serial port comes out of pin
13, and what comes in on pin 14 goes to the host, a short line at once (the
idle line) and 600 bytes in their order; Back and Exit give the CLI its USB
port back.  Last a start with --lpuart none and --serial pty (an app that opens pins 15
and 16 still runs; pins 13 and 14 have a pty, and a program on it is answered
beside `uart send`) and one with --lpuart tcp:PORT.  Exit status 0 = pass.
"""
import argparse
import json
import os
import pathlib
import pty
import re
import select
import shutil
import socket
import string
import subprocess
import sys
import time
import tty

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import fe_header  # noqa: E402
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


def start(b, timeout, *more, serial=None):
    """Another run of the device, with more words for `run`: (its CLI
    answers, what was said)"""
    if b.uart.exists():
        b.uart.rename(b.dir / f"uart-run{b.runs}.log")
    b.runs += 1
    b.log = open(b.dir / "qemu.log", "ab")
    b.proc = subprocess.Popen(
        [sys.executable, str(ROOT / "flipperemu"), "-d", b.name, "run",
         "--headless", "--audio", "none", "--serial",
         serial or f"file:{b.uart}", *more],
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


def sent(b, port="lpuart"):
    """What the firmware sent on a port, this run"""
    try:
        return (b.dir / ("lpuart.log" if port == "lpuart"
                         else "uart.log")).read_bytes()
    except OSError:
        return b""


def on(port):
    """The words for `uart` that name a port"""
    return () if port == "lpuart" else ("--port", port)


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


def turn(b, word, label, port="lpuart", path="/ext/echo.js"):
    """A whole run of the script: ready, a line answered, bye"""
    p = script(b, path)
    c1, o1, e1, _ = fe(b, "uart", "wait", *on(port), "\\nready\\r\\n"
                       if port == "usart" else "ready", "--for", 30)
    c2, o2, e2, _ = fe(b, "uart", "send", *on(port), word + "\\n", "--expect",
                       f"you said: {word}\\r\\n")
    c3, o3, e3, _ = fe(b, "uart", "send", *on(port), "bye\\n", "--expect",
                       "closing")
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
          code == 0 and len(lines) == 4 and link.is_symlink()
          and f"pins 15 and 16 (LPUART1): {os.readlink(link)}" in lines[0]
          and "pins 13 and 14 (USART1): a file" in lines[2], out + err)
    check("... and how the firmware has them: the one off, the other at its "
          "log's speed, its receiver off", len(lines) == 4
          and lines[1].endswith("the firmware has it as: off (nothing has "
                                "opened it)")
          and re.search(r"has it as: \d+ baud, 8N1; its receiver is off, and "
                        r"the firmware waits for a call on pin 14 \(an "
                        r"interrupt on its fall, as for an expansion "
                        r"module\)$", lines[3]), out + err)
    code, out, err, took = fe(b, "uart", "send", "stale\\n")
    check("a line sent while no app has the port open: taken (and lost, as "
          "with nothing wired), exit 0, and told that nobody heard it",
          code == 0 and took < 5 and not out
          and "nothing in the firmware has the port open (LPUART1 is off)"
          in err and "nobody heard it" in err,
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
          "peripheral's, by its name", "alternate function" in tx
          and "alternate function" in rx and tx.endswith("LPUART1's TX")
          and rx.endswith("LPUART1's RX"), f"{tx}\n{rx}\n{err}")
    c2, o2, e2, _ = fe(b, "status", "--json")
    try:
        port = json.loads(o2)["serial"][1]
    except (ValueError, KeyError, IndexError, TypeError):
        port = None
    check("... and beneath the pins the port as the app has it: 115200 baud, "
          "8N1, read by interrupt; `status --json` has the same",
          re.search(r"pins 15 and 16 \(LPUART1\) +115200 baud, 8N1; what "
                    r"comes in is read \(by interrupt\)", out)
          and port and (port["name"], port["on"], port["baud"], port["frame"],
                        port["hears"]) == ("LPUART1", True, 115200, "8N1",
                                           "irq"),
          f"{out}{err}\n{port}")
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


def term_checks(b, port="lpuart"):
    """`uart term` on a terminal of its own, a line typed there, and another
    `uart send` meanwhile"""
    pins = "15 and 16" if port == "lpuart" else "13 and 14"
    master, slave = pty.openpty()
    p = subprocess.Popen([sys.executable, str(ROOT / "flipperemu"), "-d", b.name,
                          "uart", "term", *on(port)], stdin=slave, stdout=slave,
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

    other = free = (None, "", "", 0)
    try:
        pump(b"quits", 20)
        pump(None, 0.5)         # (what is typed before it has the terminal
        os.write(master, b"term 1\r")           # is thrown away with the
        pump(b"you said: term 1", 10)           # terminal's line mode)
        other = fe(b, "uart", "send", *on(port), "meanwhile\\n")
        if port != "lpuart":
            free = fe(b, "uart", "send", "the other port\\n")
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
    there = "" if port == "lpuart" else f" --port {port}"
    check(f"`uart term{there}`: what is typed goes out, the answer shows, "
          "Ctrl-] leaves it", p.returncode == 0 and "you said: term 1" in text,
          f"exit {p.returncode}\n{text}")
    check(f"a second `uart{there}` meanwhile: told who has the pins, exit 1",
          other[0] == 1 and f"pins {pins} are in use" in other[2]
          and "uart term" in other[2], f"{other[0]} {other[1]} {other[2]}")
    if port != "lpuart":
        check("... and one for pins 15 and 16 is not in its way",
              free[0] == 0, f"{free[0]} {free[1]} {free[2]}")


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


def port_state(b, name):
    """A serial port of the header, as `status --json` has it"""
    code, out, err, took = fe(b, "status", "--json")
    try:
        return next(p for p in json.loads(out)["serial"] if p["name"] == name)
    except (ValueError, KeyError, TypeError, StopIteration):
        return {}


def start_bit(path, pin):
    """A pin in a record of the header: (the ns it was low for, before it
    was again what it had been: None if it never was; its levels after
    that)"""
    try:
        record, changes = fe_header.read_record(path)
    except (OSError, ValueError):
        return None, []
    mine = [(ns, level, before) for ns, p, level, before in changes if p == pin]
    for i, (ns, level, before) in enumerate(mine[:-1]):
        if level == "low" and mine[i + 1][1] == before:
            return mine[i + 1][0] - ns, [c[1] for c in mine[i + 1:]]
    return None, [c[1] for c in mine]


def call_checks(b):
    """The RX pins while nothing reads them.  A byte that comes in is a
    start bit on the pin all the same, and the firmware's expansion service,
    which waits for pin 14 to fall, takes it as a module's call."""
    usart = port_state(b, "USART1")
    code, out, err, took = fe(b, "gpio")
    check("the firmware waits for an expansion module on pin 14 (USART1's "
          "receiver off, an interrupt on the pin's fall): `status --json` "
          "and `gpio` say so",
          (usart.get("hears"), usart.get("baud")) == ("edge", 230400)
          and "the firmware waits for a call on pin 14" in out,
          f"{usart}\n{out}{err}")
    vcd = b.dir / "call.vcd"
    fe(b, "gpio", "record", vcd)
    code, out, err, took = fe(b, "uart", "send", "--port", "usart", "--hex",
                              "00", "--expect", "\\x01\\x01")
    check("a byte sent there is such a call: the firmware answers with its "
          "heartbeat (01 01), and `uart send` says what the byte was taken as",
          code == 0 and "\x01\x01" in out and "it took that as one, as from "
          "an expansion module" in err, f"{code} {out!r} {err}")
    c2, o2, e2, _ = fe(b, "uart", "send", "x")
    time.sleep(1.0)                     # (a module has 250 ms to go on)
    fe(b, "gpio", "record", "stop")
    low, then = start_bit(vcd, "PB7")
    check("... in the pins' record: pin 14 low for about a bit at 9600 baud "
          "(104 µs), the port's while the firmware talks, and high again "
          "when no module went on",
          low and 50_000 <= low <= 1_000_000 and "unknown" in then
          and then[-1] == "high", f"{low} ns, then {then}")
    low, then = start_bit(vcd, "PC0")
    check("... and a byte to the port that is off is a start bit on pin 16 "
          "all the same: low, and let go; nobody heard it, as `uart send` "
          "says", c2 == 0 and "nobody heard it" in e2
          and low and 50_000 <= low <= 1_000_000,
          f"{c2} {e2}{low} ns, then {then}")
    usart = port_state(b, "USART1")
    code, out, err, took = fe(b, "uart", "send", "--port", "usart", "--hex",
                              "00", "--expect", "\\x01\\x01")
    check("the firmware waits again, at its log's speed, and answers a "
          "second call",
          (usart.get("hears"), usart.get("baud")) == ("edge", 230400)
          and code == 0 and "\x01\x01" in out, f"{usart}\n{code} {out!r} {err}")
    time.sleep(0.6)


def usart_checks(b):
    """Pins 13 and 14 (USART1): the firmware logs there, until an app opens
    the port"""
    echo = "/ext/echo13.js"
    code, out, err, took = fe(b, "uart", "wait", "--port", "usart",
                              "Startup complete")
    check("the log port (pins 13 and 14): `uart wait --port usart` reads it",
          code == 0 and "Startup complete" in out, f"{code} {out[-300:]!r} {err}")
    call_checks(b)
    code, out, err, took = fe(b, "uart", "send", "--port", "usart", "stale\\n")
    check("a line sent there while the firmware only logs: taken, exit 0, "
          "and no text that anything read (a call, as `uart send` says)",
          code == 0 and took < 5 and "read none of it as text" in err,
          f"{code} in {took:.1f} s\n{out}{err}")
    local = b.dir / "echo13.js"
    local.write_text(ECHO.read_text().replace('"lpuart"', '"usart"'))
    code, out, err, took = fe(b, "push", local, "/ext")
    if not check("the script, for pins 13 and 14, is on the card",
                 code == 0 and '"usart"' in local.read_text(), out + err):
        return
    p = script(b, echo)
    code, out, err, took = fe(b, "uart", "wait", "--port", "usart",
                              "\\nready\\r\\n", "--for", 30)
    if not check("an app opens that port: `uart wait --port usart` has its "
                 "first line, behind the firmware's log",
                 code == 0 and "ready" in out, f"{code} {out[-300:]!r} {err}"):
        print("        the CLI: " + ended(p, 5))
        return
    code, out, err, took = fe(b, "uart", "send", "--port", "usart",
                              "ping 13\\n", "--expect", "you said: ping 13\\r\\n")
    check("`uart send --port usart --expect`: the line is answered",
          code == 0 and "you said: ping 13" in out, f"{code} {out!r} {err}")
    check("... in well under a second", took < 1.0, f"{took:.2f} s")
    check("... and what was sent before the app had the port did not wait "
          "for it", b"stale" not in sent(b, "usart"), sent(b, "usart")[-300:])
    big = (string.ascii_letters + string.digits) * 15
    big = big[:900]
    code, out, err, took = fe(b, "uart", "send", "--port", "usart", big + "\\n",
                              "--expect", big[-40:] + "\\r\\n", "--for", 20)
    check("900 characters in a line there: back, in their order",
          code == 0 and "you said: " + big in out,
          f"{code} in {took:.1f} s\n{out!r}\n{err}")
    code, out, err, took = fe(b, "gpio")
    tx, rx = row(out, "PB6"), row(out, "PB7")
    check("`gpio` while the port is open: pins 13 and 14 are the peripheral's",
          "alternate function 7" in tx and "alternate function 7" in rx,
          f"{tx}\n{rx}\n{err}")
    term_checks(b, "usart")
    code, out, err, took = fe(b, "uart", "send", "--port", "usart", "bye\\n",
                              "--expect", "closing")
    said = ended(p)
    check("the script closes the port and ends",
          code == 0 and "echo done" in said and CRASH.search(said) is None,
          f"{code} {out!r} {err}\nthe CLI: {said}")
    before = len(sent(b, "usart"))
    turn(b, "again 13", "the script a second time on pins 13 and 14: ready, "
                        "answered, closed", "usart", echo)
    between = sent(b, "usart")[before:].partition(b"\nready\r\n")
    check("... and between the two the firmware's log was on the pins again",
          between[1] and b"[I][" in between[0], between[0][-400:])


def speed_checks(b):
    """The port's speed: what is sent comes in at the app's baud rate"""
    text = ECHO.read_text()
    slow = b.dir / "echo1200.js"
    slow.write_text(text.replace("115200", "1200"))
    code, out, err, took = fe(b, "push", slow, "/ext")
    if not check("the script, at 1200 baud, is on the card",
                 code == 0 and text.count("115200") == 1, out + err):
        return
    p = script(b, "/ext/echo1200.js")
    code, out, err, took = fe(b, "uart", "wait", "ready", "--for", 30)
    if not check("an app opens the port at 1200 baud: its first line comes",
                 code == 0 and "ready" in out, f"{code} {out!r} {err}"):
        print("        the CLI: " + ended(p, 5))
        return
    code, out, err, took = fe(b, "uart")
    check("... and `uart` has the speed that the app asked for",
          "the firmware has it as: 1200 baud, 8N1; what comes in is read"
          in out, out + err)
    line = (string.ascii_lowercase * 10)[:239]
    code, out, err, took = fe(b, "uart", "send", line + "\\n", "--for", 1)
    check("240 bytes for that port take the two seconds they take on a wire "
          "at 1200 baud, and --for 1 does not cut them off (it is how long "
          "nothing may move)", code == 0 and 1.9 <= took < 8,
          f"{code} in {took:.2f} s\n{out}{err}")
    code, out, err, took = fe(b, "uart", "wait", line[-40:] + "\\r\\n",
                              "--for", 10)
    check("... and are answered, in their order",
          code == 0 and "you said: " + line in out, f"{code} {out!r} {err}")
    code, out, err, took = fe(b, "uart", "send", "bye\\n", "--expect",
                              "closing")
    said = ended(p)
    check("... the script closes the port and ends",
          code == 0 and "echo done" in said and CRASH.search(said) is None,
          f"{code} {out!r} {err}\nthe CLI: {said}")


def bridge_checks(b):
    """The firmware's USB-UART bridge (in its GPIO app), which reads pins
    13 and 14 by DMA, and has the USB serial port instead of the CLI"""
    code, out, err, took = fe(b, "cli", "loader open GPIO")
    if not check("the GPIO app starts", code == 0
                 and CRASH.search(out) is None, f"{code} {out}{err}"):
        return
    fe(b, "press", "ok")                    # its first item: USB-UART Bridge
    fd = os.open(b.dir / "usb.tty", os.O_RDWR | os.O_NOCTTY)
    got = b""

    def hear(want, seconds):
        nonlocal got
        end = time.time() + seconds
        while time.time() < end and want not in got:
            if select.select([fd], [], [], 0.05)[0]:
                got += os.read(fd, 65536)
        return want in got

    try:
        tty.setraw(fd)
        time.sleep(1.5)         # (QEMU looks once a second who has a pty)
        for turn_ in range(1, 4):           # (the bridge takes a moment)
            os.write(fd, b"from usb %d\r\n" % turn_)
            code, out, err, took = fe(b, "uart", "wait", "--port", "usart",
                                      f"from usb {turn_}\\r\\n", "--for", 4)
            if code == 0:
                break
        check("the USB-UART bridge: what the host writes on the USB serial "
              "port comes out of pin 13", code == 0,
              f"{code} {out[-300:]!r} {err}")
        code, out, err, took = fe(b, "gpio")
        check("... `gpio` has the port as the bridge does: 115200 baud, read "
              "by DMA",
              re.search(r"pins 13 and 14 \(USART1\) +115200 baud, 8N1; what "
                        r"comes in is read \(by DMA\)", out), out + err)
        t0 = time.time()
        code, out, err, took = fe(b, "uart", "send", "--port", "usart",
                                  "to usb\\n")
        heard = hear(b"to usb\n", 5)
        check("... a short line into pin 14 is at the host at once (the "
              "bridge reads the port by DMA, and the idle line tells it)",
              code == 0 and heard and time.time() - t0 < 2,
              f"{code} in {time.time() - t0:.2f} s {out}{err}\n"
              f"the USB port: {got[-200:]!r}")
        big = ((string.ascii_letters + string.digits) * 10)[:599]
        code, out, err, took = fe(b, "uart", "send", "--port", "usart",
                                  big + "\\n")
        heard = hear(big.encode() + b"\n", 10)
        check("... 600 bytes: all of them, in their order",
              code == 0 and heard, f"{code} in {took:.1f} s {out}{err}\n"
              f"the USB port: {len(got)} bytes, {got[-700:]!r}")
    finally:
        os.close(fd)
    fe(b, "press", "back")
    fe(b, "press", "left")                  # "Exit USB-UART?": Exit
    code, out, err, took = fe(b, "wait", "--timeout", 30)
    fe(b, "press", "back")                  # and out of the app
    end = time.time() + 15
    ok, said = b.answers()
    while not ok and time.time() < end:
        time.sleep(0.5)
        ok, said = b.answers()
    check("Back and Exit: the bridge gives the USB port back, and the CLI "
          "answers", code == 0 and ok, f"{code} {out}{err}\n{said}")


def other_checks(b):
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
    ok, said = start(b, timeout, "--lpuart", "none", serial="pty")
    if not check("a start with --lpuart none and --serial pty", ok, said):
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
    link = b.dir / "usart.tty"
    code, out, err, took = fe(b, "uart")
    if not check("... pins 13 and 14 have a pty, with the device's usart.tty "
                 "as its name", code == 0 and link.is_symlink()
                 and f"pins 13 and 14 (USART1): {os.readlink(link)}" in out,
                 out + err):
        return
    fd = os.open(link, os.O_RDWR | os.O_NOCTTY)
    got = b""

    def hear(want, seconds):
        nonlocal got
        end = time.time() + seconds
        while time.time() < end and want not in got:
            if select.select([fd], [], [], 0.05)[0]:
                got += os.read(fd, 65536)
        return want in got

    try:
        tty.setraw(fd)
        time.sleep(1.5)         # (QEMU looks once a second who has a pty)
        p = script(b, "/ext/echo13.js")
        ready = hear(b"\nready\r\n", 30)
        os.write(fd, b"pty 13\n")
        answered = hear(b"you said: pty 13\r\n", 10)
        check("... a program on that pty has the app's first line, and its "
              "own line answered", ready and answered, repr(got[-400:]))
        code, out, err, took = fe(b, "uart", "send", "--port", "usart",
                                  "bye\\n", "--expect", "closing")
        said = ended(p)
        check("... and `uart send --port usart` works beside it",
              code == 0 and hear(b"closing\r\n", 5) and "echo done" in said,
              f"{code} {out!r} {err}\nthe pty: {got[-400:]!r}\nthe CLI: {said}")
    finally:
        os.close(fd)


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
                usart_checks(b)
                speed_checks(b)
                bridge_checks(b)
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
