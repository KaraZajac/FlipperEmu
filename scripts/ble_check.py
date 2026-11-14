#!/usr/bin/env python3
# Copyright (c) 2026, Kara Zajac
# SPDX-License-Identifier: BSD-3-Clause
"""Check the emulated radio stack: CPU2's BLE stack, as the firmware sees it.

    scripts/ble_check.py firmware/official-1.5.1/flipper-z-f7-update-1.5.1.tgz

A throwaway device starts with the stack of a Flipper from the factory
(1.20.0, BLE Light): the firmware starts it ("Radio stack started"), and
`status` has it running with the firmware's GATT services in it and nothing
advertised (Bluetooth is off on a new Flipper).  The firmware's own view
(`info device`: the stack's mode, version and type) and `bt hci_info` (the
stack answers a command of its own) have to agree.  Settings > Bluetooth on:
it advertises, under its name, from the Flipper Devices address block.

Then a central on the link (ble.sock, scripts/fe_ble.py), as a phone is: it
sees the advertising; it connects, and the firmware asks it to pair; the MTU
is the stack's 414; before pairing the device information and the serial
service refuse a read.  A wrong PIN fails, and the Flipper ends the
connection.  The PIN it shows (taken from its log) pairs: authenticated,
bonded, LE Secure Connections.  Then GATT has the firmware's five services
and its commit; `battery 42` comes as a notification; an RPC session goes
through the serial service (a ping, the device's info); the central's
disconnect closes it, and a central that goes away times out.

A reboot keeps Bluetooth on (the firmware has the stack start again).  A
snapshot saved with a central connected loads with the connection gone (the
firmware hears so) and the stack answering.  Settings > Bluetooth off: it
does not advertise, and the link says so.  `run --radio-stack none` is a
Flipper with no stack: the firmware says so and runs on, and a plain `run`
after it has the stack again.  No crash in any of the runs' logs.  Exit
status 0 = pass.
"""
import argparse
import json
import os
import re
import shutil
import sys
import time

from files_check import Bench, CRASH, check, results
from uart_check import fe
import fe_ble

QUIET = ("--headless", "--audio", "none")
PHONE = "C6:89:67:45:23:01"     # the link's central, when it names none
logs = []


def state(b):
    """`status --json`"""
    code, out, err, took = fe(b, "status", "--json")
    try:
        return json.loads(out)
    except ValueError:
        return {}


def bt(b):
    return state(b).get("bluetooth") or {}


def until(what, timeout, step=0.5):
    end = time.time() + timeout
    while time.time() < end:
        got = what()
        if got:
            return got
        time.sleep(step)
    return what()


def cli(b, *commands):
    code, out, err, took = fe(b, "cli", "--timeout", 30, *commands)
    return out


def up(b, timeout, *words):
    """A start that goes on by itself, until the CLI answers"""
    code, out, err, took = fe(b, *words, "--detach", *QUIET)
    said = out + err
    if code == 0:
        code, out, err, took = fe(b, "wait", "--timeout", timeout)
        said += out + err
    return code == 0, said


def stop(b):
    try:
        logs.append(b.uart.read_text(errors="replace"))
    except OSError:
        pass
    fe(b, "stop")


def pairs(c, pin):
    """c.pair() -> (its words, the failure's reason)"""
    try:
        return c.pair(pin), None
    except fe_ble.PairingFailed as e:
        return None, e.reason


def central(b, name, version, commit):
    """A central on the link (ble.sock), as a phone is: what it sees, a
    pairing that fails and one that works, GATT, RPC, and the ends of a
    connection"""
    sock = b.dir / "ble.sock"
    with fe_ble.Central(sock, timeout=15) as c:
        adv = c.advertising(15)
        check("the link (ble.sock): a scanner sees the advertising: "
              "connectable, LE General Discoverable, its name, Flipper "
              "Devices' address, the serial profile's service",
              adv["connectable"] and adv.get("flags") == 0x06
              and name in (adv.get("name") or "")
              and (adv.get("addr") or "").startswith("80:E1:26")
              and len(adv.get("uuids") or []) == 1, adv)

        c.connect()
        asked = until(lambda: c.pump(0.2) or c.security_request, 10)
        s = bt(b)
        conn = s.get("connection") or {}
        check("a central connects: the firmware asks it to pair (a Security "
              "Request: bonding, MITM), and stops advertising; `status` has "
              "the connection",
              asked and asked.get("bond") == "1" and asked.get("mitm") == "1"
              and c.adv is None and s.get("advertising") is False
              and conn.get("central") == PHONE, f"{asked} {s}")

        mtu = c.mtu(517)
        check("the MTU: the most that the firmware's stack takes (414)",
              mtu == 414, mtu)

        c.gatt()
        refused = []
        for uuid in ("2a29", fe_ble.SERIAL_FLOW):
            try:
                c.read(c.char(uuid))
                refused.append(None)
            except fe_ble.LinkError as e:
                refused.append(e.code)
        check("before pairing, the device information and the serial "
              "service refuse a read: insufficient authentication (0x05)",
              refused == [5, 5], refused)

        mark = fe_ble.log_size(b.uart)

        def wrong():
            real = fe_ble.pin_from_log(b.uart, mark)
            return None if real is None else f"{(int(real) + 1) % 1000000:06d}"
        paired, reason = pairs(c, wrong)
        ended = c.wait_disconnected(10)
        again = c.advertising(15)
        check("a wrong PIN: the pairing fails (confirm value failed, 0x04), "
              "the Flipper ends the connection itself (authentication "
              "failure, 0x05), and advertises again",
              paired is None and reason == 0x04 and ended == ("flipper", 0x05)
              and again is not None, f"{paired} {reason} {ended} {again}")

        c.connect()
        c.mtu(517)
        mark = fe_ble.log_size(b.uart)
        paired, reason = pairs(c, lambda: fe_ble.pin_from_log(b.uart, mark))
        s = bt(b)
        conn = s.get("connection") or {}
        check("the PIN that the Flipper shows (its log has it too): paired, "
              "authenticated and bonded, by LE Secure Connections; `status` "
              "says so",
              paired == {"authenticated": "1", "bonded": "1", "sc": "1"}
              and conn.get("authenticated") is True
              and conn.get("bonded") is True, f"{paired} {reason} {s}")

        c.gatt()
        uuids = {svc.uuid for svc in c.services}
        maker = c.read(c.char("2a29")).decode(errors="replace")
        soft = c.read(c.char("2a28")).decode(errors="replace")
        # (the software revision is the firmware's commit, branch, branch
        # number and build date: "9e810dc3 1.5.1 0 15-10-2026")
        check("GATT: the firmware's five services (GAP, GATT, device "
              "information, battery, serial); the device information names "
              "its maker, and the firmware by its commit",
              uuids >= {"1800", "1801", "180a", "180f", fe_ble.SERIAL}
              and maker and commit and soft.startswith(commit + " "),
              f"{sorted(uuids)} {maker!r} {soft!r} {commit!r}")

        level = c.char("2a19")
        c.subscribe(level)
        fe(b, "battery", "42")
        try:
            got = c.take(level.value, timeout=20)[2]
        except TimeoutError:
            got = None
        now = c.read(level)
        check("the battery level: `battery 42` comes to the central as a "
              "notification, and a read has it", got == b"\x2a"
              and now == b"\x2a", f"{got} {now}")

        try:
            rpc = fe_ble.Rpc(c, timeout=20)
            back = rpc.ping(b"ble_check over the air")
            info = rpc.device_info()
        except (fe_ble.LinkError, TimeoutError, KeyError) as e:
            back, info = None, {"error": str(e)}
        check("RPC through the serial service, as the phone app has it: a "
              "ping comes back whole, and the device's info names its "
              "firmware and the stack 1.20",
              back == b"ble_check over the air"
              and info.get("firmware_version") == version
              and info.get("radio_stack_minor") == "20",
              f"{back} {dict(list(info.items())[:8])}")

        c.disconnect()
        s = until(lambda: (bt(b).get("advertising") or None) and bt(b), 15)
        check("the central ends the connection: the firmware closes its RPC "
              "session and advertises again",
              s and s.get("connection") is None
              and until(lambda: "Close RPC connection" in b.uart_text(), 5),
              s)

    mark = len(b.uart_text())
    gone = fe_ble.Central(sock, timeout=15)
    gone.connect()
    gone.close()
    s = until(lambda: (bt(b).get("advertising") or None) and bt(b), 15)
    check("a central that goes away while connected: the connection times "
          "out at the firmware (0x08), which advertises again",
          s and s.get("connection") is None
          and until(lambda: re.search(r"Reason: 0?8\b",
                                      b.uart_text()[mark:]), 5),
          f"{s} {b.uart_text()[mark:][-300:]}")


def bluetooth(b, on):
    """Settings > Bluetooth, on or off: the app, the value, and Back (the
    app hands its settings to the BT service as it closes)"""
    code, out, err, took = fe(b, "app", "Bluetooth")
    if code:
        return out + err
    time.sleep(1.5)
    fe(b, "press", "right" if on else "left")
    time.sleep(0.5)
    fe(b, "press", "back")
    return ""


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("firmware")
    ap.add_argument("--timeout", type=float, default=120.0,
                    help="seconds to wait for the firmware to start")
    ap.add_argument("--keep", action="store_true",
                    help="keep the throwaway device")
    args = ap.parse_args()

    b = Bench(f"blecheck-{int(time.time())}-{os.getpid()}", args.firmware,
              args.timeout)
    name = b.name[:8]
    try:
        ok, said = up(b, args.timeout, "run", "-f", args.firmware)
        s = bt(b) if ok else {}
        if not check("a new Flipper: the firmware starts the stack, 1.20.0, "
                     "with its GATT services in it, and advertises nothing",
                     ok and "Radio stack started" in b.uart_text()
                     and s.get("stack") == "1.20.0"
                     and s.get("runs") == "stack"
                     and s.get("up") is True and s.get("services", 0) >= 4
                     and s.get("advertising") is False,
                     f"{said[-600:]} {s}"):
            return 1
        info = cli(b, "info device")
        field = lambda key: (re.search(rf"(?m)^{re.escape(key)}\s*:\s*(\S+)",
                                       info) or [None, None])[1]
        check("the firmware's own view (`info device`): the stack runs, "
              "1.20.0, release type 2, BLE Light",
              field("radio.mode") == "Stack"
              and field("radio.stack.major") == "1"
              and field("radio.stack.minor") == "20"
              and field("radio.stack.release") == "2"
              and field("radio.stack.type") == "3",
              info[-800:])
        hci = cli(b, "bt hci_info")
        check("`bt hci_info`: the stack answers, HCI 5.4 (13), ST's company "
              "identifier (48)",
              re.search(r"Ret: 0, HCI_Version: 13, .*Manufacturer_Name: 48",
                        hci), hci[-400:])

        said = bluetooth(b, True)
        s = until(lambda: (bt(b).get("advertising") or None) and bt(b), 15)
        check("Settings > Bluetooth on: it advertises, under its name, from "
              "Flipper Devices' address block (80:E1:26)",
              not said and s and name in (s.get("name") or "")
              and (s.get("address") or "").startswith("80:E1:26"),
              f"{said} {s}")
        if s:
            central(b, name, field("firmware.version"),
                    field("firmware.commit.hash"))

        code, out, err, took = fe(b, "wait", "--timeout", args.timeout, "--",
                                  "reset")
        s = until(lambda: (bt(b).get("advertising") or None) and bt(b), 20)
        check("a reboot: the firmware has the stack start again, and it "
              "advertises again", code == 0 and s and s.get("up") is True,
              f"{out + err} {s}")

        # saved with a central connected, which stays until the run ends:
        # the loaded firmware has to hear that the connection is gone.  (A
        # load puts the saved log back, and the run goes on from it: the
        # timeouts are counted.)
        timeouts = lambda: len(re.findall(r"Reason: 0?8\b", b.uart_text()))
        before = timeouts()
        held = fe_ble.Central(b.dir / "ble.sock", timeout=15)
        try:
            held.connect()
        except (TimeoutError, fe_ble.LinkError) as e:
            print(f"  (no connection for the snapshot: {e})")
        code, out, err, took = fe(b, "snapshot", "save", "ble")
        stop(b)
        held.close()
        ok, said = up(b, args.timeout, "snapshot", "load", "ble")
        s = until(lambda: (bt(b).get("advertising") or None) and bt(b),
                  15) if ok else {}
        hci = cli(b, "bt hci_info") if ok else ""
        check("a snapshot saved with a central connected: after the load "
              "the firmware hears that the connection is gone (a timeout), "
              "advertises as before, and the loaded stack answers",
              code == 0 and ok and s and s.get("connection") is None
              and s.get("services", 0) >= 4 and "Ret: 0" in hci
              and until(lambda: timeouts() > before, 10),
              f"{said[-400:]} {s} {hci[-200:]} {before} {timeouts()}")

        with fe_ble.Central(b.dir / "ble.sock", timeout=15) as watch:
            watch.advertising(15)
            said = bluetooth(b, False)
            s = until(lambda: bt(b).get("advertising") is False and bt(b), 15)
            watch.pump(1.0)
            check("Settings > Bluetooth off: it does not advertise, and the "
                  "link says so (adv off)",
                  not said and s and s.get("up") is True
                  and watch.adv is None, f"{said} {s}")
        stop(b)

        ok, said = up(b, args.timeout, "run", "--radio-stack", "none")
        s = bt(b) if ok else {}
        check("`run --radio-stack none`: a Flipper with no stack; the "
              "firmware says so, and runs on",
              ok and s.get("stack") is None and s.get("runs") == "fus"
              and "Stack isn't installed!" in b.uart_text()
              and "no radio stack installed" in fe(b, "status")[1],
              f"{said[-400:]} {s}")
        stop(b)
        ok, said = up(b, args.timeout, "run")
        s = bt(b) if ok else {}
        check("a plain `run` after it: the stack again", ok
              and s.get("runs") == "stack" and s.get("up") is True,
              f"{said[-400:]} {s}")
        stop(b)

        log = "\n".join(logs)
        crash = CRASH.search(log)
        check("no crash in the firmware's logs", not crash,
              crash and log[max(0, crash.start() - 300):crash.end() + 300])
    finally:
        fe(b, "stop")
        if not args.keep:
            shutil.rmtree(b.dir, ignore_errors=True)
    bad = [label for label, ok in results if not ok]
    print(f"{len(results) - len(bad)} of {len(results)} checks passed"
          + (f"; device kept: {b.dir}" if args.keep else ""))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
