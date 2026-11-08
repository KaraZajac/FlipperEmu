#!/usr/bin/env python3
# Copyright (c) 2026, Kara Zajac
# SPDX-License-Identifier: BSD-3-Clause
"""Plug an emulated Flipper into this PC over USB/IP (run as root).

    sudo scripts/usbip_attach.py              # attach once (port 3240)
    sudo scripts/usbip_attach.py --watch      # keep it attached across reboots,
                                              # mode changes and updates
    sudo scripts/usbip_attach.py --detach     # unplug

Start the emulator with `./flipperemu run --usbip`. Once attached the kernel
sees a real USB device (0483:5740), cdc_acm binds /dev/ttyACM*, and qFlipper,
lab.flipper.net (Chrome WebSerial) or any serial tool finds the Flipper.

This does what `usbip attach` does (the usbip package is not needed): it
imports the device over TCP and hands the socket to the vhci-hcd driver.
"""
import argparse
import os
import pathlib
import subprocess
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import fe_usbip  # noqa: E402


def attached_ports():
    """vhci ports in use by us (busid 1-1 from 127.0.0.1 is all we know)."""
    ports = []
    for base in fe_usbip._vhci_paths():
        with open(os.path.join(base, "status")) as f:
            for line in f.read().splitlines()[1:]:
                cols = line.split()
                if len(cols) >= 3 and cols[2] == "006":      # VDEV_ST_USED
                    ports.append((os.path.join(base, "detach"), int(cols[1])))
    return ports


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=3240)
    ap.add_argument("--busid", default="1-1")
    ap.add_argument("--watch", action="store_true",
                    help="re-attach whenever the device comes back")
    ap.add_argument("--detach", action="store_true", help="unplug all vhci devices")
    args = ap.parse_args()

    if os.geteuid() != 0:
        sys.exit("needs root: sudo " + " ".join(sys.argv))
    if not fe_usbip._vhci_paths():
        subprocess.run(["modprobe", "vhci-hcd"], check=True)
        time.sleep(0.5)
    if args.detach:
        for path, port in attached_ports():
            with open(path, "w") as f:
                f.write(str(port))
            print(f"detached vhci port {port}")
        return 0

    while True:
        try:
            if any(d["busid"] == args.busid
                   for d in fe_usbip.devlist(args.host, args.port)):
                vport, dev = fe_usbip.attach_vhci(args.host, args.port, args.busid)
                print(f"attached {dev['vid']:04x}:{dev['pid']:04x} on vhci port {vport}"
                      " (watch `dmesg`, then /dev/ttyACM*)", flush=True)
                if not args.watch:
                    return 0
                # attached: wait until the server ends the session (reboot, ...)
                while any(p == vport for _, p in attached_ports()):
                    time.sleep(1)
                print("device left the bus; waiting for it to come back", flush=True)
            elif not args.watch:
                sys.exit("the emulator exports no device yet (is it running with "
                         "--usbip, and has the firmware connected USB?)")
        except (OSError, ConnectionError) as e:
            if not args.watch:
                sys.exit(f"attach failed: {e}")
        time.sleep(1)


if __name__ == "__main__":
    sys.exit(main())
