#!/usr/bin/env python3
# Copyright (c) 2026, Kara Zajac
# SPDX-License-Identifier: BSD-3-Clause
"""Check the second USB serial port: the firmware's dual-CDC mode, which its
USB-UART bridge takes on "USB Channel 1" (the CLI stays on the first port).

    scripts/usb_dual_check.py firmware/official-1.1.2/flipper-z-f7-update-1.1.2.tgz

Boots a throwaway device, opens GPIO's USB-UART bridge and sets its USB
channel to 1.  A firmware that then has two CDC ACM functions (the official
ones up to 1.1.2): the host sees both, the second is bridged to
devices/NAME/usb2.tty, bytes written there come out of pin 13 and bytes on
pin 14 come out there, and the CLI answers on the first port meanwhile;
with the bridge left, the Flipper has one port again.

A firmware whose CLI service takes the USB back to one port as soon as the
bridge has asked for two (the official ones from 1.3 on, and those made
from them) has no second port, on a Flipper either: that is said, and the
one port has to be there and answer.  Exit status 0 = pass.
"""
import argparse
import json
import os
import pathlib
import select
import shutil
import sys
import termios
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from files_check import Bench, CRASH, check, results          # noqa: E402
import check_limit  # noqa: E402


def usb(b):
    code, text = b.fe("status", "--json")
    try:
        return json.loads(text[text.index("{"):]).get("usb") or {}
    except ValueError:
        return {}


def settled(b, seconds=20):
    """What the host sees, once it has seen the same for a while"""
    last, same, end = None, 0, time.time() + seconds
    while time.time() < end and same < 4:
        now = usb(b).get("device")
        same = same + 1 if now == last and now and "CDC" in now else 0
        last = now
        time.sleep(0.5)
    return last or ""


def raw(path):
    fd = os.open(os.path.realpath(path), os.O_RDWR | os.O_NOCTTY)
    attrs = termios.tcgetattr(fd)
    attrs[0] = attrs[1] = attrs[3] = 0
    termios.tcsetattr(fd, termios.TCSANOW, attrs)
    return fd


def second_port(b):
    seen = usb(b)
    port = seen.get("second_port")
    code, text = b.fe("status")
    check("the bridge on USB channel 1: the host sees two serial ports (CDC "
          "ACM x2), and `status` has the second one's pty (usb2.tty)",
          "CDC ACM x2" in (seen.get("device") or "") and port
          and os.path.exists(port) and "second serial port" in text,
          f"{seen}\n{text[:400]}")
    if not port:
        return
    fd = raw(port)
    try:
        time.sleep(1.0)                 # (the port's DTR, as a terminal's)
        waiter = b.bg("uart", "wait", "--port", "usart", "--for", 20,
                      "to the pins")
        time.sleep(1.0)
        os.write(fd, b"to the pins\r\n")
        out = waiter.communicate(timeout=40)[0]
        check("bytes written to the second port come out of pin 13",
              waiter.returncode == 0 and "to the pins" in out, out[-300:])
        code, text = b.fe("uart", "send", "--port", "usart", "from the pins\\r\\n")
        got, end = b"", time.time() + 10
        while time.time() < end and b"from the pins" not in got:
            if select.select([fd], [], [], 0.5)[0]:
                got += os.read(fd, 256)
        check("bytes that come in on pin 14 come out of the second port",
              b"from the pins\r\n" in got, f"{text} {got!r}")
        code, text = b.fe("cli", "loader info")
        check("the CLI answers on the first port meanwhile",
              code == 0 and "GPIO" in text, text[-300:])
    finally:
        os.close(fd)
    for key in ("back", "left"):        # "Exit USB-UART?": Exit
        b.fe("press", key)
        time.sleep(1.0)
    after = settled(b)
    code, text = b.fe("cli", "loader info")
    check("the bridge left: the Flipper has one serial port again, and the "
          "CLI answers there",
          "CDC ACM" in after and "x2" not in after and code == 0
          and usb(b).get("second_port") is None, f"{after} {text[-200:]}")


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("firmware")
    ap.add_argument("--timeout", type=float, default=120.0,
                    help="seconds to wait for the firmware to start")
    ap.add_argument("--keep", action="store_true",
                    help="keep the throwaway device")
    args = ap.parse_args()
    check_limit.arm(600)

    b = Bench(f"usb2check-{int(time.time())}-{os.getpid()}", args.firmware,
              args.timeout)
    try:
        if check("boot: 'Startup complete'", b.start(first=True),
                 b.uart_text()[-2000:]):
            b.fe("wait", "--timeout", args.timeout)
            before = settled(b)
            b.fe("cli", "loader open GPIO")
            time.sleep(1.5)
            # the bridge (the first item), its Config, USB Channel: 1, back
            for key, pause in (("ok", 1.5), ("left", 1.0), ("right", 0.7),
                               ("back", 1.0)):
                b.fe("press", key)
                time.sleep(pause)
            seen = settled(b)
            if "x2" in seen:
                second_port(b)
            else:
                print("  (this firmware has no second port on channel 1: its "
                      "CLI service sets the USB back to one port as soon as "
                      "the bridge has asked for two; on a Flipper as here)")
                code, text = b.fe("cli", "loader info")
                check("the bridge on USB channel 1: the firmware goes to two "
                      "ports and back to one; the host sees the one, and the "
                      "CLI answers there",
                      "CDC ACM" in before and "CDC ACM" in seen and code == 0
                      and "GPIO" in text, f"{before} | {seen} | {text[-200:]}")
            b.stop()
            log = b.uart_text(every=True)
            crash = CRASH.search(log)
            check("no crash in the firmware's logs", not crash,
                  crash and log[max(0, crash.start() - 300):crash.end() + 300])
    finally:
        b.stop()
        if not args.keep:
            shutil.rmtree(b.dir, ignore_errors=True)
    bad = [label for label, ok in results if not ok]
    print(f"{len(results) - len(bad)} of {len(results)} checks passed"
          + (f"; device kept: {b.dir}" if args.keep else ""))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
