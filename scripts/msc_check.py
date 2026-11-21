#!/usr/bin/env python3
# Copyright (c) 2026, Kara Zajac
# SPDX-License-Identifier: BSD-3-Clause
"""Check the Flipper as a USB drive: the Mass Storage app, and a host.

    scripts/msc_check.py firmware/momentum-mntm-012/flipper-z-f7-update-mntm-012.tgz

Boots a throwaway device with `run --usbip` on a free port and is the host
itself (fe_msc.py: USB/IP in user space, so no root, and nothing is plugged
into this PC).  At the desktop there is no drive on the port.  An image of
random blocks goes onto the card, and the forks' Mass Storage app (on their
cards; the official firmware has none, and the check skips it) is opened
with it.  Then: one LUN, SCSI in Bulk-Only Transport on two bulk endpoints,
the image's name as the serial string; INQUIRY, TEST UNIT READY, READ
CAPACITY (the image's blocks); every block read back is the image's; blocks
written are read back so; a command that the app does not know is refused
with its sense (illegal request), and a read past the last block is refused
with the status in the data's place; the drive answers after both, and
after an eject (the app, opened with the image's path, keeps it).  The
same from the command line (fe_msc.py info, read).  Back leaves the app,
and the image on the card is what was written.  Exit status 0 = pass.
"""
import argparse
import os
import pathlib
import random
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time

import fe_msc
from files_check import ROOT, Bench, CRASH, check, results
from gpio_check import skip, skipped
from uart_check import fe, start
import check_limit

APP = "mass_storage.fap"                # found on the card, by this name
FOLDER = "/ext/apps_data/mass_storage"  # the app's own (/data)
NAME = "check"
BLOCKS = 1024                           # 512 KiB


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def tool(port, *words):
    """fe_msc.py as a command: (exit status, out, err)"""
    r = subprocess.run([sys.executable, str(ROOT / "scripts" / "fe_msc.py"),
                        "--port", str(port), *words], capture_output=True,
                       timeout=300, stdin=subprocess.DEVNULL)
    return (r.returncode, r.stdout,
            r.stderr.decode("utf-8", "replace"))


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("firmware")
    ap.add_argument("--timeout", type=float, default=120.0,
                    help="seconds to wait for the firmware to start")
    ap.add_argument("--keep", action="store_true",
                    help="keep the throwaway device")
    args = ap.parse_args()
    check_limit.arm(600)

    b = Bench(f"msccheck-{int(time.time())}-{os.getpid()}", args.firmware,
              args.timeout)
    work = pathlib.Path(tempfile.mkdtemp(prefix="fe-msc-"))
    port = free_port()
    try:
        b.dir.mkdir(parents=True, exist_ok=True)
        ok, said = start(b, args.timeout, "-f", args.firmware, "--usbip",
                         str(port))
        if ok:
            ok, said = b.answers()
        if check("the firmware starts with its USB port on USB/IP too, and "
                 "its CLI answers", ok, said + b.uart_text()[-2000:]):
            run(b, port, work)
            log = b.uart_text(every=True)
            crash = CRASH.search(log)
            check("no crash in the firmware's log", not crash,
                  crash and log[max(0, crash.start() - 300):crash.end() + 300])
    finally:
        b.stop()
        fe(b, "stop")
        shutil.rmtree(work, ignore_errors=True)
        if not args.keep:
            shutil.rmtree(b.dir, ignore_errors=True)
    bad = [label for label, ok in results if not ok]
    print(f"{len(results) - len(bad)} of {len(results)} checks passed"
          + (f", {len(skipped)} skipped" if skipped else "")
          + (f"; device kept: {b.dir}" if args.keep else ""))
    return 1 if bad else 0


def run(b, port, work):
    try:
        with fe_msc.Drive(port, appear=2):
            said = "a drive"
    except fe_msc.NoDrive as e:
        said = str(e)
    ok, more = b.answers()
    check("at the desktop: no drive on the port (a serial port, no mass "
          "storage interface), and the CLI answers after the host has looked",
          "no mass storage interface" in said and ok, said + "\n" + more)
    code, out, err, took = fe(b, "app", "--list")
    fap = next((p for p in re.findall(r"^  (/ext/apps/\S.*\.fap)$", out, re.M)
                if p.rsplit("/", 1)[1] == APP), None)
    if not fap:
        skip("Mass Storage", f"this firmware's card has no {APP}")
        return
    image = random.Random(1).randbytes(BLOCKS * 512)
    local = work / f"{NAME}.img"
    local.write_bytes(image)
    path = f"{FOLDER}/{NAME}.img"
    fe(b, "mkdir", FOLDER)
    code, out, err, took = fe(b, "push", "-f", "--card", local, path)
    if not check(f"an image of {BLOCKS} random blocks onto the card", code == 0,
                 out + err):
        return
    code, out, err, took = fe(b, "app", fap, path)
    said = out + err
    if not check("Mass Storage opened with `app` and the image: it has the "
                 "USB port, a mass storage device, said at once",
                 code == 0 and "has the Flipper's USB port for itself" in said
                 and "mass storage" in said and "Traceback" not in said,
                 f"{took:.1f} s: {said}"):
        return
    try:
        d = fe_msc.Drive(port)
    except (fe_msc.NoDrive, OSError, TimeoutError) as e:
        check("a host finds the drive", False, e)
        return
    written = None
    try:
        written = drive_checks(b, d, image)
    finally:
        d.close()
    if written is not None:
        tool_checks(port, work, written)
    fe(b, "press", "back")
    ok, said = b.answers()
    check("Back: Mass Storage is left, the serial port is back and the CLI "
          "answers", ok, said)
    if written is None or not ok:
        return
    back = work / "back.img"
    code, out, err, took = fe(b, "pull", "-f", path, back)
    got = back.read_bytes() if code == 0 and back.exists() else b""
    check("the image on the card afterwards: what the host wrote, block for "
          "block", got == written,
          out + err + f" ({len(got)} bytes; blocks that differ: "
          f"{differ(got, written)[:20]})")


def differ(a, b):
    n = max(len(a), len(b)) // 512
    return [i for i in range(n) if a[i * 512:(i + 1) * 512]
            != b[i * 512:(i + 1) * 512]]


def drive_checks(b, d, image):
    """-> the image as the drive has it in the end, or None"""
    check("the drive: one mass storage interface, SCSI in Bulk-Only "
          "Transport, a bulk endpoint each way of 64 bytes, one LUN; the "
          "image's name as its serial string",
          d.kind == (6, 0x50) and d.ep_in[1] == 64 and d.ep_out[1] == 64
          and d.max_lun == 0 and d.serial == NAME,
          f"{d.vid:04x}:{d.pid:04x} {d.manufacturer!r} {d.product!r} serial "
          f"{d.serial!r}; kind {d.kind}, in {d.ep_in}, out {d.ep_out}, max "
          f"LUN {d.max_lun}")
    code, out, err, took = fe(b, "status")
    check("`status` meanwhile: a PC has the port over USB/IP",
          code == 0 and "USB/IP" in out, out + err)
    try:
        q = d.inquiry()
        not_ready = d.ready()
        blocks, size = d.capacity()
    except (fe_msc.Refused, OSError, TimeoutError) as e:
        check("INQUIRY, TEST UNIT READY, READ CAPACITY", False, e)
        return None
    check("INQUIRY: a removable disk, \"Flipper\" \"Mass Storage\"",
          q["type"] == 0 and q["removable"] and q["vendor"] == "Flipper"
          and q["product"] == "Mass Storage", q)
    check(f"TEST UNIT READY: ready; READ CAPACITY: the image's {BLOCKS} "
          "blocks of 512 bytes",
          not_ready is None and (blocks, size) == (BLOCKS, 512),
          f"{not_ready}; {blocks} x {size}")
    t0 = time.monotonic()
    try:
        got = b"".join(d.read(lba, min(128, BLOCKS - lba))
                       for lba in range(0, BLOCKS, 128))
        said = ""
    except (fe_msc.Refused, OSError, TimeoutError) as e:
        got, said = b"", str(e)
    took = time.monotonic() - t0
    if not check("every block read (READ(10), 128 a command) is the image's",
                 got == image, f"{said} {len(got)} bytes in {took:.1f} s "
                 f"({len(got) / 1024 / max(took, 1e-6):.0f} KiB/s); blocks "
                 f"that differ: {differ(got, image)[:20]}"):
        return None
    print(f"        ({len(got) // 1024} KiB in {took:.1f} s, "
          f"{len(got) / 1024 / max(took, 1e-6):.0f} KiB/s)", flush=True)
    new = random.Random(2).randbytes(64 * 512)
    try:
        t0 = time.monotonic()
        d.write(200, new)
        took = time.monotonic() - t0
        again = d.read(200, 64) + d.read(199, 1) + d.read(264, 1)
        said = ""
    except (fe_msc.Refused, OSError, TimeoutError) as e:
        again, said, took = b"", str(e), 0
    written = image[:200 * 512] + new + image[264 * 512:]
    if not check("64 blocks written (WRITE(10)) at block 200 read back so, "
                 "and the blocks beside them as they were",
                 again == new + image[199 * 512:200 * 512]
                 + image[264 * 512:265 * 512],
                 f"{said} write {took:.1f} s; {len(again)} bytes back"):
        return None
    try:
        result, _, _ = d.command(bytes([0x35] + [0] * 9))    # SYNCHRONIZE CACHE(10)
        sense = d.sense()
        said = ""
    except (OSError, TimeoutError) as e:
        result, sense, said = None, None, str(e)
    check("a command that the app does not know (SYNCHRONIZE CACHE): "
          "refused, and REQUEST SENSE says why (illegal request, invalid "
          "command operation code)",
          result == fe_msc.FAILED and sense == (5, 0x20, 0),
          f"{said} status {result}, sense {sense}")
    try:
        result, data, _ = d.command(bytes([0x28, 0, 0, 0, BLOCKS >> 8,
                                           BLOCKS & 0xFF, 0, 0, 1, 0]), 512)
        after = d.read(BLOCKS - 1, 1)
        said = ""
    except (fe_msc.Refused, OSError, TimeoutError) as e:
        result, data, after, said = None, None, b"", str(e)
    check("a read past the last block: refused, with the status in the "
          "data's place (as usb-storage takes it), and the drive reads "
          "after it",
          result == fe_msc.FAILED and data == b""
          and after == written[-512:], f"{said} status {result}, data "
          f"{len(data) if data is not None else None} bytes")
    try:
        d.eject()
        not_ready = d.ready()
        said = ""
    except (fe_msc.Refused, OSError, TimeoutError) as e:
        not_ready, said = "", str(e)
    check("eject (START STOP UNIT): done, and the drive stays (the app was "
          "opened with the image's path: it has no list to go back to)",
          not_ready is None, f"{said} {not_ready}")
    return written


def tool_checks(port, work, written):
    code, out, err = tool(port, "info")
    text = out.decode("utf-8", "replace")
    check("`fe_msc.py info`: the USB device, a removable disk, its blocks",
          code == 0 and "a mass storage device, SCSI in Bulk-Only Transport"
          in text and 'a removable disk: "Flipper" "Mass Storage"' in text
          and f"{BLOCKS} blocks of 512 bytes (512 KiB), ready" in text,
          text + err)
    print("        " + text.strip().replace("\n", "\n        "), flush=True)
    out_file = work / "read.img"
    code, out, err = tool(port, "read", out_file)
    got = out_file.read_bytes() if out_file.exists() else b""
    check("`fe_msc.py read FILE`: the whole drive, as written",
          code == 0 and got == written, err)


if __name__ == "__main__":
    sys.exit(main())
