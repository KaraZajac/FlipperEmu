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
it advertises, under its name, from the Flipper Devices address block.  A
reboot keeps it so (the firmware has the stack start again).  A snapshot keeps
it so too, and the loaded stack answers.  Settings > Bluetooth off: it does
not advertise.  `run --radio-stack none` is a Flipper with no stack: the
firmware says so and runs on, and a plain `run` after it has the stack again.
No crash in any of the runs' logs.  Exit status 0 = pass.
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

QUIET = ("--headless", "--audio", "none")
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

        code, out, err, took = fe(b, "wait", "--timeout", args.timeout, "--",
                                  "reset")
        s = until(lambda: (bt(b).get("advertising") or None) and bt(b), 20)
        check("a reboot: the firmware has the stack start again, and it "
              "advertises again", code == 0 and s and s.get("up") is True,
              f"{out + err} {s}")

        code, out, err, took = fe(b, "snapshot", "save", "ble")
        stop(b)
        ok, said = up(b, args.timeout, "snapshot", "load", "ble")
        s = bt(b) if ok else {}
        hci = cli(b, "bt hci_info") if ok else ""
        check("a snapshot: after a load it advertises as before, and the "
              "loaded stack answers",
              code == 0 and ok and s.get("advertising") is True
              and s.get("services", 0) >= 4 and "Ret: 0" in hci,
              f"{said[-400:]} {s} {hci[-200:]}")

        said = bluetooth(b, False)
        s = until(lambda: bt(b).get("advertising") is False and bt(b), 15)
        check("Settings > Bluetooth off: it does not advertise",
              not said and s and s.get("up") is True, f"{said} {s}")
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
