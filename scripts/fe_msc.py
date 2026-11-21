#!/usr/bin/env python3
# Copyright (c) 2026, Kara Zajac
# SPDX-License-Identifier: BSD-3-Clause
"""A USB drive's host for an emulated Flipper, in user space.

A PC reaches a USB drive through the kernel: usb-storage, the SCSI disk
driver, a block device.  This does the same over USB/IP with no kernel in
between (fe_usbip.UsbipClient plays vhci-hcd's part), so it needs no root
and plugs nothing into this PC: the mass storage interface, GET MAX LUN, the
SCSI commands that a PC gives a drive (INQUIRY, TEST UNIT READY, READ
CAPACITY, READ(10), WRITE(10), REQUEST SENSE, START STOP UNIT) in the
wrappers of Bulk-Only Transport on the two bulk endpoints, and a stalled
endpoint cleared, or the drive reset, as usb-storage does.

    ./flipperemu -d mine run --usbip      # then Mass Storage on the Flipper
    scripts/fe_msc.py                     # what the drive says it is
    scripts/fe_msc.py read disk.img       # its blocks, into a file
    scripts/fe_msc.py write disk.img      # a file's blocks, onto it
    scripts/fe_msc.py eject               # as a PC's "eject" does
"""
import argparse
import select
import struct
import sys
import time

import fe_usbip

CBW_SIG, CSW_SIG = 0x43425355, 0x53425355       # "USBC", "USBS"
CSW_LEN = 13
EPIPE = -32                                     # a stall (Linux's errno)
GOOD, FAILED, PHASE_ERROR = 0, 1, 2
SENSE_KEYS = {0: "no sense", 1: "recovered error", 2: "not ready",
              3: "medium error", 4: "hardware error", 5: "illegal request",
              6: "unit attention", 7: "data protect", 0xB: "aborted command"}
TYPES = {0: "disk", 5: "CD/DVD drive", 7: "optical disk", 0xE: "simple disk"}


class NoDrive(Exception):
    """What is on the port is no USB drive (or nothing is)."""


class Refused(Exception):
    """A command that the drive answered with "failed", and its sense."""

    def __init__(self, what, sense):
        self.sense = sense
        if sense:
            key, asc, ascq = sense
            said = (f"{SENSE_KEYS.get(key, f'sense key {key:#x}')}, "
                    f"ASC/ASCQ {asc:02X}/{ascq:02X}")
        else:
            said = "it gave no reason"
        super().__init__(f"the drive refused {what}: {said}")


class Drive:
    """The USB device behind a USB/IP port, taken as a PC's usb-storage takes
    a mass storage device: descriptors, SET_CONFIGURATION, GET MAX LUN, and
    then commands, each a CBW, data either way and a CSW."""

    def __init__(self, port=3240, host="127.0.0.1", timeout=30, appear=10):
        # appear: seconds to wait for the device to be on the bus (an app
        # that has just taken the port, a host that has just let go of it)
        end = time.monotonic() + appear
        while True:
            try:
                if any(d["busid"] == "1-1" for d in
                       fe_usbip.devlist(host, port, timeout=10)):
                    self.c = fe_usbip.UsbipClient(host=host, port=port,
                                                  timeout=10)
                    break
                why = "the Flipper's USB port is not on the bus now"
            except (ConnectionRefusedError, TimeoutError) as e:
                raise NoDrive(f"nothing answers USB/IP on {host}:{port} "
                              f"({e.strerror or e}): the device runs, and was "
                              "started with `run --usbip`?") from e
            except ConnectionError as e:        # gone between the two
                why = str(e)
            except OSError as e:
                raise NoDrive(f"no USB/IP on {host}:{port}: {e}") from e
            if time.monotonic() >= end:
                raise NoDrive(why)
            time.sleep(0.2)
        self.timeout = timeout
        self.late = []
        self.tag = 0
        self.lun = 0
        self.blocks = self.block = None
        try:
            self.enumerate()
        except Exception:
            self.close()
            raise

    def close(self):
        self.c.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def control(self, *a, **kw):
        return self.c.control(*a, pending=self.late, **kw)

    def string(self, index):
        if not index:
            return ""
        try:
            d = self.control(0x80, 6, 0x0300 | index, 0x0409, 255)
        except IOError:
            return ""
        return d[2:d[0]].decode("utf-16-le", "replace") if d else ""

    def enumerate(self):
        dd = self.control(0x80, 6, 0x0100, 0, 18)
        self.vid, self.pid = struct.unpack_from("<HH", dd, 8)
        head = self.control(0x80, 6, 0x0200, 0, 9)
        cfg = self.control(0x80, 6, 0x0200, 0,
                           struct.unpack_from("<H", head, 2)[0])
        self.interface = self.ep_in = self.ep_out = None
        self.kind, at, cls = None, 0, None
        while at + 2 <= len(cfg) and cfg[at]:
            n, what = cfg[at], cfg[at + 1]
            d = cfg[at:at + n]
            if what == 4:                               # interface
                cls = d[5]
                if cls == 8 and self.interface is None:
                    self.interface, self.kind = d[2], (d[6], d[7])
            elif what == 5 and cls == 8 and d[3] & 3 == 2:  # bulk endpoint
                ep = (d[2] & 0xF, struct.unpack_from("<H", d, 4)[0])
                if d[2] & 0x80:
                    self.ep_in = self.ep_in or ep
                else:
                    self.ep_out = self.ep_out or ep
            at += n
        if self.interface is None:
            raise NoDrive(f"the USB device there ({self.vid:04x}:{self.pid:04x}) "
                          "has no mass storage interface: Mass Storage is open "
                          "on the Flipper, with an image?")
        if self.kind != (6, 0x50) or not (self.ep_in and self.ep_out):
            raise NoDrive("the mass storage interface there is not SCSI in "
                          "Bulk-Only Transport (subclass "
                          f"{self.kind[0]:#04x}, protocol {self.kind[1]:#04x})")
        self.manufacturer, self.product, self.serial = (
            self.string(dd[i]) for i in (14, 15, 16))
        self.control(0x00, 9, cfg[5], 0)                # SET_CONFIGURATION
        try:                                            # GET MAX LUN
            self.max_lun = self.control(0xA1, 0xFE, 0, self.interface, 1)[0]
        except (IOError, IndexError):                   # a stall: one LUN
            self.max_lun = 0

    # ---- Bulk-Only Transport
    def _bulk(self, ep, data_in=0, data=b""):
        """One URB on a bulk endpoint -> (status, data)"""
        seq = (self.c.submit_in(ep, data_in) if data_in
               else self.c.submit(ep, False, data=data))
        end = time.monotonic() + self.timeout
        while True:
            for i, reply in enumerate(self.late):
                if reply[1] == seq:
                    del self.late[i]
                    return reply[2], reply[3]
            left = end - time.monotonic()
            if left <= 0 or not select.select([self.c.sock], [], [], left)[0]:
                raise TimeoutError(f"the drive did not answer in {self.timeout:g} s")
            reply = self.c.receive()
            if reply[1] == seq:
                return reply[2], reply[3]
            self.late.append(reply)

    def clear_halt(self, ep, into_host):
        """CLEAR_FEATURE(ENDPOINT_HALT): a stalled endpoint goes on"""
        self.control(0x02, 1, 0, ep | (0x80 if into_host else 0))

    def reset(self):
        """Reset recovery: Bulk-Only Mass Storage Reset, both halts cleared"""
        try:
            self.control(0x21, 0xFF, 0, self.interface)
        finally:
            self.clear_halt(self.ep_in[0], True)
            self.clear_halt(self.ep_out[0], False)

    def command(self, cdb, data_in=0, data_out=b""):
        """A command block, its data, its status -> (status, data, residue)"""
        self.tag = self.tag % 0xFFFFFFFF + 1
        cbw = struct.pack("<IIIBBB", CBW_SIG, self.tag,
                          data_in or len(data_out), 0x80 if data_in else 0,
                          self.lun, len(cdb)) + bytes(cdb).ljust(16, b"\0")
        status, _ = self._bulk(self.ep_out[0], data=cbw)
        if status:
            self.reset()
            raise IOError(f"the drive did not take a command (status {status})")
        got = csw = None
        if data_in:
            status, got = self._bulk(self.ep_in[0], data_in=data_in)
            if status == EPIPE:                         # no (more) data
                self.clear_halt(self.ep_in[0], True)
                got = b""
            elif status:
                raise IOError(f"a read from the drive failed (status {status})")
            elif (len(got) == CSW_LEN < data_in
                  and struct.unpack_from("<I", got)[0] == CSW_SIG):
                csw, got = got, b""     # its status in the data's place:
                # the data phase skipped, as usb-storage takes it
        elif data_out:
            status, _ = self._bulk(self.ep_out[0], data=data_out)
            if status == EPIPE:                         # took no (more) data
                self.clear_halt(self.ep_out[0], False)
            elif status:
                raise IOError(f"a write to the drive failed (status {status})")
        if csw is None:
            status, csw = self._bulk(self.ep_in[0], data_in=CSW_LEN)
            if status == EPIPE:                         # once more, then
                self.clear_halt(self.ep_in[0], True)
                status, csw = self._bulk(self.ep_in[0], data_in=CSW_LEN)
            if status:
                self.reset()
                raise IOError(f"no status from the drive (status {status})")
        if len(csw) != CSW_LEN:
            self.reset()
            raise IOError(f"the drive's status has {len(csw)} bytes, not 13")
        sig, tag, residue, result = struct.unpack("<IIIB", csw)
        if sig != CSW_SIG or tag != self.tag or result == PHASE_ERROR:
            self.reset()
            raise IOError("the drive's status is not this command's "
                          f"(signature {sig:#010x}, tag {tag} for {self.tag}, "
                          f"status {result}): it was reset")
        return result, got or b"", residue

    # ---- SCSI
    def good(self, what, cdb, data_in=0, data_out=b""):
        """A command that has to succeed -> its data; Refused with the
        drive's sense otherwise"""
        result, got, _ = self.command(cdb, data_in, data_out)
        if result != GOOD:
            raise Refused(what, self.sense())
        return got

    def sense(self):
        """REQUEST SENSE -> (key, ASC, ASCQ) of the last failure, or None"""
        try:
            result, got, _ = self.command(bytes([0x03, 0, 0, 0, 18, 0]), 18)
        except IOError:
            return None
        if result != GOOD or len(got) < 14:
            return None
        return got[2] & 0xF, got[12], got[13]

    def inquiry(self):
        d = self.good("INQUIRY", bytes([0x12, 0, 0, 0, 36, 0]), 36)
        if len(d) < 36:
            raise IOError(f"the drive's INQUIRY data has {len(d)} bytes")
        text = (lambda b: b.decode("ascii", "replace").strip())
        return {"type": d[0] & 0x1F, "removable": bool(d[1] & 0x80),
                "vendor": text(d[8:16]), "product": text(d[16:32]),
                "revision": text(d[32:36])}

    def unit_serial(self):
        """INQUIRY's page 0x80, or None"""
        try:
            d = self.good("INQUIRY (its serial number)",
                          bytes([0x12, 1, 0x80, 0, 252, 0]), 252)
        except Refused:
            return None
        return d[4:4 + d[3]].decode("ascii", "replace").strip() \
            if len(d) >= 4 else None

    def ready(self):
        """TEST UNIT READY -> None, or what the drive's sense says"""
        result, _, _ = self.command(bytes(6))
        if result == GOOD:
            return None
        return str(Refused("TEST UNIT READY", self.sense())).split(": ", 1)[-1]

    def capacity(self):
        """READ CAPACITY(10) -> (blocks, bytes in a block)"""
        d = self.good("READ CAPACITY", bytes([0x25] + [0] * 9), 8)
        last, size = struct.unpack(">II", d[:8])
        self.blocks, self.block = last + 1, size
        return self.blocks, self.block

    def read(self, lba, count):
        if self.block is None:
            self.capacity()
        return self.good(f"a READ(10) of block {lba}",
                         struct.pack(">BBIBHB", 0x28, 0, lba, 0, count, 0),
                         count * self.block)

    def write(self, lba, data):
        if self.block is None:
            self.capacity()
        if len(data) % self.block:
            raise ValueError(f"{len(data)} bytes are no whole blocks of {self.block}")
        self.good(f"a WRITE(10) at block {lba}",
                  struct.pack(">BBIBHB", 0x2A, 0, lba, 0,
                              len(data) // self.block, 0), data_out=data)

    def eject(self):
        """START STOP UNIT with LoEj: what a PC's eject sends"""
        self.good("START STOP UNIT (eject)", bytes([0x1B, 0, 0, 0, 0x02, 0]))


def size_text(n):
    for unit in ("bytes", "KiB", "MiB", "GiB"):
        if n < 1024 or unit == "GiB":
            return f"{n:g} {unit}" if unit == "bytes" else f"{n:.4g} {unit}"
        n /= 1024


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("what", nargs="?", default="info",
                    choices=("info", "read", "write", "eject"))
    ap.add_argument("file", nargs="?",
                    help="read: where its blocks go (none or -: standard "
                         "output); write: the file whose bytes go onto it "
                         "(none or -: standard input), padded to whole blocks")
    ap.add_argument("--lba", type=int, default=0, help="the first block")
    ap.add_argument("--count", type=int,
                    help="read: how many blocks (all from --lba on)")
    ap.add_argument("--chunk", type=int, default=128,
                    help="blocks in one command at most (128)")
    ap.add_argument("--port", type=int, default=3240)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--timeout", type=float, default=30,
                    help="seconds for one command (30)")
    args = ap.parse_args(argv)
    try:
        with Drive(args.port, args.host, args.timeout) as d:
            if args.what == "info":
                q = d.inquiry()
                serial = d.unit_serial()
                not_ready = d.ready()
                blocks, size = d.capacity()
                names = " ".join(f'"{s}"' for s in
                                 (d.manufacturer, d.product) if s)
                print(f"USB {d.vid:04x}:{d.pid:04x} {names}"
                      + (f' serial "{d.serial}"' if d.serial else "")
                      + ": a mass storage device, SCSI in Bulk-Only Transport "
                      f"on endpoints {d.ep_in[0]} in and {d.ep_out[0]} out "
                      f"({d.ep_in[1]} bytes), {d.max_lun + 1} LUN"
                      + ("s" if d.max_lun else ""))
                kind = (TYPES.get(q["type"])
                        or f"device of type {q['type']:#x}")
                print(f"a{' removable' if q['removable'] else ''} {kind}: "
                      f'"{q["vendor"]}" "{q["product"]}" revision '
                      f'"{q["revision"]}"'
                      + (f', serial number "{serial}"' if serial else ""))
                print(f"{blocks} blocks of {size} bytes "
                      f"({size_text(blocks * size)}), "
                      + ("ready" if not_ready is None
                         else f"not ready: {not_ready}"))
                return 0
            if args.what == "eject":
                d.eject()
                print("ejected", file=sys.stderr)
                return 0
            blocks, size = d.capacity()
            start = time.monotonic()
            if args.what == "read":
                count = (args.count if args.count is not None
                         else blocks - args.lba)
                if args.lba < 0 or count < 0 or args.lba + count > blocks:
                    raise ValueError(f"blocks {args.lba} to {args.lba + count - 1}"
                                     f" are not all on the drive ({blocks})")
                out = (sys.stdout.buffer if args.file in (None, "-")
                       else open(args.file, "wb"))
                try:
                    lba = args.lba
                    while lba < args.lba + count:
                        n = min(args.chunk, args.lba + count - lba)
                        out.write(d.read(lba, n))
                        lba += n
                finally:
                    if out is not sys.stdout.buffer:
                        out.close()
                did = "read"
            else:
                data = (sys.stdin.buffer.read() if args.file in (None, "-")
                        else open(args.file, "rb").read())
                if len(data) % size:
                    data += bytes(size - len(data) % size)
                count = len(data) // size
                if args.lba < 0 or args.lba + count > blocks:
                    raise ValueError(f"{count} blocks from block {args.lba} do "
                                     f"not fit on the drive ({blocks})")
                for at in range(0, count, args.chunk):
                    n = min(args.chunk, count - at)
                    d.write(args.lba + at, data[at * size:(at + n) * size])
                did = "wrote"
            took = time.monotonic() - start
            print(f"{did} {count} blocks of {size} bytes "
                  f"({size_text(count * size)}) from block {args.lba} in "
                  f"{took:.1f} s ({size_text(count * size / max(took, 1e-6))}"
                  "/s)", file=sys.stderr)
    except (NoDrive, Refused, TimeoutError, OSError, ValueError) as e:
        print(f"fe_msc: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
