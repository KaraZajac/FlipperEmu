#!/usr/bin/env python3
# Copyright (c) 2026, Kara Zajac
# SPDX-License-Identifier: BSD-3-Clause
"""USB/IP client side for FlipperEmu (Linux usbip protocol v1.1.1).

Two uses:
  * attach_vhci(): hand an imported device to the kernel's vhci-hcd, which is
    what `usbip attach` does; the emulated Flipper then becomes a real USB
    device on this PC (root required), and qFlipper finds it.
  * UsbipClient: speak the protocol from userspace, playing the role of the
    kernel; the self-test uses it to enumerate the device and run the CLI.

Standard library only.
"""
import glob
import os
import socket
import struct

VERSION = 0x0111
CMD_SUBMIT, CMD_UNLINK, RET_SUBMIT, RET_UNLINK = 1, 2, 3, 4
USB_SPEED_FULL = 2


def _recv_exact(sock, n):
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("USB/IP server closed the connection")
        buf += chunk
    return buf


def _parse_device(blk):
    path, busid = blk[0:256], blk[256:288]
    busnum, devnum, speed, vid, pid, bcd = struct.unpack(">IIIHHH", blk[288:306])
    cls, sub, proto, cfg, ncfg, nif = blk[306:312]
    return {
        "path": path.split(b"\0", 1)[0].decode(), "busid": busid.split(b"\0", 1)[0].decode(),
        "busnum": busnum, "devnum": devnum, "speed": speed, "vid": vid, "pid": pid,
        "bcd": bcd, "class": (cls, sub, proto), "config": cfg, "nconfigs": ncfg,
        "ninterfaces": nif,
    }


def devlist(host="127.0.0.1", port=3240, timeout=5):
    """OP_REQ_DEVLIST: the devices the server exports."""
    with socket.create_connection((host, port), timeout=timeout) as s:
        s.sendall(struct.pack(">HHI", VERSION, 0x8005, 0))
        ver, code, status, n = struct.unpack(">HHII", _recv_exact(s, 12))
        if code != 0x0005 or status:
            raise ConnectionError(f"bad OP_REP_DEVLIST: code {code:#x} status {status}")
        devs = []
        for _ in range(n):
            d = _parse_device(_recv_exact(s, 0x138))
            d["interfaces"] = [tuple(_recv_exact(s, 4)[:3]) for _ in range(d["ninterfaces"])]
            devs.append(d)
        return devs


def import_device(sock, busid="1-1"):
    """OP_REQ_IMPORT on a connected socket; returns the device block."""
    sock.sendall(struct.pack(">HHI32s", VERSION, 0x8003, 0, busid.encode()))
    ver, code, status = struct.unpack(">HHI", _recv_exact(sock, 8))
    if code != 0x0003 or status:
        raise ConnectionError(f"import of {busid} refused (status {status}): is the "
                              "emulated Flipper running and connected over USB?")
    return _parse_device(_recv_exact(sock, 0x138))


class UsbipClient:
    """A URB-level client: what vhci-hcd does in the kernel, in Python."""

    def __init__(self, host="127.0.0.1", port=3240, busid="1-1", timeout=10):
        self.sock = socket.create_connection((host, port), timeout=timeout)
        self.dev = import_device(self.sock, busid)
        self.devid = self.dev["busnum"] << 16 | self.dev["devnum"]
        self.seq = 0

    def close(self):
        self.sock.close()

    def submit(self, ep, direction_in, length=0, data=b"", setup=b"\0" * 8, flags=0):
        self.seq += 1
        hdr = struct.pack(">IIIII", CMD_SUBMIT, self.seq, self.devid,
                          1 if direction_in else 0, ep)
        hdr += struct.pack(">IIIII", flags | (0x200 if direction_in else 0),
                           length if direction_in else len(data), 0, 0xFFFFFFFF, 0)
        hdr += setup
        self.sock.sendall(hdr + (b"" if direction_in else data))
        return self.seq

    def unlink(self, seqnum):
        self.seq += 1
        self.sock.sendall(struct.pack(">IIIIII", CMD_UNLINK, self.seq, self.devid, 0, 0,
                                      seqnum) + b"\0" * 24)
        return self.seq

    def receive(self):
        """(command, seqnum, status, data) of the next reply."""
        hdr = _recv_exact(self.sock, 48)
        cmd, seq, _, _, _, status, actual = struct.unpack(">IIIIIiI", hdr[:28])
        if cmd == RET_SUBMIT:
            data = _recv_exact(self.sock, actual) if seq in self._in_seqs else b""
            self._in_seqs.discard(seq)
            return cmd, seq, status, data, actual
        return cmd, seq, status, b"", 0

    _in_seqs = set()

    def submit_in(self, ep, length, **kw):
        seq = self.submit(ep, True, length=length, **kw)
        self._in_seqs.add(seq)
        return seq

    def control(self, bm, req, value, index, length=0, data=b"", pending=None):
        """A synchronous control transfer; other replies go to `pending`."""
        setup = struct.pack("<BBHHH", bm, req, value, index, length if bm & 0x80 else len(data))
        if bm & 0x80:
            seq = self.submit_in(0, length, setup=setup)
        else:
            seq = self.submit(0, False, data=data, setup=setup)
        while True:
            cmd, s, status, got, actual = self.receive()
            if s == seq:
                if status:
                    raise IOError(f"control {bm:#04x} {req:#04x} failed: status {status}")
                return got
            if pending is not None:
                pending.append((cmd, s, status, got, actual))


def _vhci_paths():
    return sorted(glob.glob("/sys/devices/platform/vhci_hcd.*"))


def free_vhci_port():
    """(attach path, port) of a free high-speed (USB 2) vhci-hcd port."""
    for base in _vhci_paths():
        with open(os.path.join(base, "status")) as f:
            for line in f.read().splitlines()[1:]:
                cols = line.split()
                # hub port sta spd dev sockfd local_busid; sta 004 = VDEV_ST_NULL
                if len(cols) >= 3 and cols[0] == "hs" and cols[2] == "004":
                    return os.path.join(base, "attach"), int(cols[1])
    return None, None


def attach_vhci(host="127.0.0.1", port=3240, busid="1-1"):
    """Import the device and give the connection to vhci-hcd (root)."""
    attach, vport = free_vhci_port()
    if attach is None:
        raise OSError("no free vhci-hcd port: is the module loaded "
                      "(modprobe vhci-hcd)?")
    sock = socket.create_connection((host, port), timeout=10)
    try:
        dev = import_device(sock, busid)
        devid = dev["busnum"] << 16 | dev["devnum"]
        with open(attach, "w") as f:
            f.write(f"{vport} {sock.fileno()} {devid} {dev['speed'] or USB_SPEED_FULL}")
    finally:
        sock.close()        # the kernel holds its own reference now
    return vport, dev
