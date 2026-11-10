#!/usr/bin/env python3
# Copyright (c) 2026, Kara Zajac
# SPDX-License-Identifier: BSD-3-Clause
"""A Proxmark3 as the emulated Flipper's NFC antenna.

    ./flipperemu nfc --link pm3[:PORT]        the Flipper reads real cards
                                              through a Proxmark3
    fe_pm3.py sim [CARD]                      a simulated Proxmark3 on a pty,
                                              with a Python card in its field

With the link card `pm3`, every frame the emulated Flipper's reader sends
is relayed raw by a Proxmark3 (Iceman/RRG firmware) to the real card in its
field, anticollision included, and the card's answer comes back as is.
QEMU stops the machine meanwhile, so the round trip over USB costs the
firmware nothing.

Firmware v4.23346 (September 2026) and later relay exactly: the frame goes
out as air bits with its own parity, and the answer comes back with the
card's (ISO14A_NO_PARITY), so MIFARE Classic's encrypted exchanges get
through.  Older firmware takes the frame's bytes and adds odd parity
itself: everything relays but Classic after authentication.

The port is the first Proxmark3 found (/dev/serial/by-id/*proxmark*, or USB
9ac4:4b8f / 2d2d:504d), or the one given.  It talks the Proxmark3's own USB
protocol (NG frames, MIX for older firmware): no client needed.  Only
ISO14443A frames go out; the Flipper's ISO14443B, FeliCa and ISO15693
polls get silence.
"""
import os
import pathlib
import select
import struct
import sys
import termios
import time
import tty

CMD_ACK = 0x00FF
CMD_DEBUG_PRINT_STRING = 0x0100
CMD_VERSION = 0x0107
CMD_PING = 0x0109
CMD_WTX = 0x0116
CMD_HF_ISO14443A_READER = 0x0385
CMD_HF_DROPFIELD = 0x0430

ISO14A_CONNECT = 1 << 0
ISO14A_NO_DISCONNECT = 1 << 1
ISO14A_RAW = 1 << 3
ISO14A_NO_SELECT = 1 << 7
ISO14A_NO_PARITY = 1 << 18

CMD_MAGIC, CMD_POST = 0x61334D50, 0x3361      # "PM3a", "a3"
RESP_MAGIC = 0x62334D50                       # "PM3b"
USB_IDS = {("9ac4", "4b8f"), ("2d2d", "504d")}


def find_port():
    """The first Proxmark3's serial port."""
    byid = pathlib.Path("/dev/serial/by-id")
    if byid.is_dir():
        for p in sorted(byid.iterdir()):
            if "proxmark" in p.name.lower():
                return str(p.resolve())
    for tty_dev in sorted(pathlib.Path("/sys/class/tty").glob("ttyACM*")):
        usb = tty_dev / "device" / ".."
        try:
            ids = ((usb / "idVendor").read_text().strip(), (usb / "idProduct").read_text().strip())
        except OSError:
            continue
        if ids in USB_IDS:
            return f"/dev/{tty_dev.name}"
    raise OSError("no Proxmark3 found (plugged in? say pm3:/dev/ttyACMn)")


def air_join(nbits, data, par=None):
    """Data bits and the parity of each full byte (odd if not given) -> air
    bits (LSB first, a parity bit after every full byte), packed: (air bit
    count, bytes)."""
    if par is None or len(par) * 8 < nbits // 8:
        par = odd_parity(data[:nbits // 8])
    bits = []
    for i in range(nbits // 8):
        bits += [(data[i] >> b) & 1 for b in range(8)] + [(par[i // 8] >> (i % 8)) & 1]
    full = nbits // 8
    bits += [(data[full] >> b) & 1 for b in range(nbits % 8)]
    out = bytearray((len(bits) + 7) // 8)
    for i, v in enumerate(bits):
        out[i // 8] |= v << (i % 8)
    return len(bits), bytes(out)


def air_split(raw, bits=None):
    """Air bits, packed -> (data bits, data, parity of each full byte).
    Without a bit count, as a Proxmark3 receives them (ISO14A_NO_PARITY:
    every bit, eight to a byte, the count lost): k bytes take 9k bits, so
    ceil(9k/8) raw bytes, and one raw byte can only be a 4-bit ACK/NAK."""
    if bits is None:
        if len(raw) == 1:
            return 4, bytes([raw[0] & 0x0F]), b""
        bits = len(raw) * 8 // 9 * 9
    k, rest = bits // 9, min(bits % 9, 8)
    bit = lambda i: (raw[i // 8] >> (i % 8)) & 1
    data = bytearray(sum(bit(9 * j + b) << b for b in range(8)) for j in range(k))
    if rest:
        data.append(sum(bit(9 * k + b) << b for b in range(rest)))
    par = bytearray((k + 7) // 8)
    for j in range(k):
        par[j // 8] |= bit(9 * j + 8) << (j % 8)
    return 8 * k + rest, bytes(data), bytes(par)


def odd_parity(data):
    par = bytearray((len(data) + 7) // 8)
    for i, b in enumerate(data):
        par[i // 8] |= (1 - bin(b).count("1") % 2) << (i % 8)
    return bytes(par)


class Proxmark3:
    """A Proxmark3 on a serial port, spoken to in its own frames."""

    def __init__(self, port=None):
        self.port = port or find_port()
        self.fd = os.open(self.port, os.O_RDWR | os.O_NOCTTY)
        try:
            tty.setraw(self.fd)
            termios.tcflush(self.fd, termios.TCIOFLUSH)
        except termios.error:
            pass                                # a pty in a test is fine
        self.buf = b""
        self.ng = self._probe_ng()
        self.version = self._version()

    def close(self):
        try:
            self.send_ng(CMD_HF_DROPFIELD)
        finally:
            os.close(self.fd)

    # ---- frames
    def send_ng(self, cmd, data=b""):
        os.write(self.fd, struct.pack("<IHH", CMD_MAGIC, len(data) | 0x8000, cmd) + data +
                 struct.pack("<H", CMD_POST))

    def send_mix(self, cmd, a0=0, a1=0, a2=0, data=b""):
        body = struct.pack("<QQQ", a0, a1, a2) + data
        os.write(self.fd, struct.pack("<IHH", CMD_MAGIC, len(body), cmd) + body +
                 struct.pack("<H", CMD_POST))

    def _read(self, n, deadline):
        while len(self.buf) < n:
            left = deadline - time.monotonic()
            if left <= 0 or not select.select([self.fd], [], [], left)[0]:
                raise TimeoutError("the Proxmark3 did not answer")
            chunk = os.read(self.fd, 4096)
            if not chunk:
                raise OSError("the Proxmark3 went away")
            self.buf += chunk
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def recv(self, timeout=2.0):
        """The next reply: (cmd, status, args, data).  Debug text is printed
        and a wait-time extension waits on."""
        deadline = time.monotonic() + timeout
        while True:
            pre = self._read(10, deadline)
            magic, lng, status, reason, cmd = struct.unpack("<IHbbH", pre)
            if magic != RESP_MAGIC:             # an OLD frame: 544 bytes in all
                rest = self._read(544 - 10, deadline)
                frame = pre + rest
                cmd, a0, a1, a2 = struct.unpack("<QQQQ", frame[:32])
                return cmd, 0, (a0, a1, a2), frame[32:]
            length, ng = lng & 0x7FFF, bool(lng & 0x8000)
            data = self._read(length, deadline) + b""
            self._read(2, deadline)             # postamble (CRC or "b3")
            args = (0, 0, 0)
            if not ng and length >= 24:
                args = struct.unpack("<QQQ", data[:24])
                data = data[24:]
            if cmd == CMD_DEBUG_PRINT_STRING:
                text = data[2:].split(b"\0")[0].decode(errors="replace")
                print(f"[pm3] {text}", file=sys.stderr, flush=True)
                continue
            if cmd == CMD_WTX:
                deadline = time.monotonic() + timeout + (struct.unpack("<H", data[:2])[0] / 1000
                                                          if len(data) >= 2 else 0)
                continue
            return cmd, status, args, data

    def _drain(self, quiet=0.3):
        """Drop whatever the device still has to say."""
        while select.select([self.fd], [], [], quiet)[0]:
            if not os.read(self.fd, 4096):
                break
        self.buf = b""

    def _probe_ng(self):
        """NG firmware answers a too-short reader command with an NG error on
        that command.  Older firmware takes the frame's MIX arguments from
        whatever its stack held (they are not filled in for NG frames): it
        may do a harmless reader operation with them, and answers, if at
        all, with a MIX CMD_ACK.  Either way, any leftovers are dropped."""
        self.send_ng(CMD_HF_ISO14443A_READER, b"\0")
        try:
            cmd, _, _, _ = self.recv(timeout=0.6)
            ng = cmd == CMD_HF_ISO14443A_READER
        except TimeoutError:
            ng = False
        self.send_ng(CMD_HF_DROPFIELD)
        self._drain()
        return ng

    def _version(self):
        self.send_ng(CMD_VERSION)
        try:
            _, _, _, data = self.recv(timeout=1.0)
        except TimeoutError:
            return "?"
        words = [w for w in data.decode(errors="replace").replace("\0", "\n").split()
                 if "/" in w and "v" in w]
        return words[-1] if words else "?"

    # ---- ISO14443A
    def _reader(self, flags, data=b"", nbits=0):
        if self.ng:
            self.send_ng(CMD_HF_ISO14443A_READER,
                         struct.pack("<IIIHH", flags, 0, 0, len(data), nbits) + data)
        else:
            self.send_mix(CMD_HF_ISO14443A_READER, flags, len(data) | (nbits << 16), 0, data)

    def field(self, on):
        if on:
            self._reader(ISO14A_CONNECT | ISO14A_NO_SELECT | ISO14A_NO_DISCONNECT)
        else:
            self.send_ng(CMD_HF_DROPFIELD)

    def transceive(self, nbits, data, par):
        """One frame to the card in the field: (bits, data, parity) of its
        answer, or None."""
        if self.ng:                             # exact: our parity, the card's
            airbits, air = air_join(nbits, data, par)
            self._reader(ISO14A_RAW | ISO14A_NO_DISCONNECT | ISO14A_NO_PARITY, air, airbits)
            cmd, status, _, resp = self.recv()
            if cmd != CMD_HF_ISO14443A_READER or status != 0 or len(resp) < 4:
                return None
            n = struct.unpack("<H", resp[:2])[0]
            return air_split(resp[4:4 + n]) if n else None
        self._reader(ISO14A_RAW | ISO14A_NO_DISCONNECT, data[:(nbits + 7) // 8],
                     nbits if nbits % 8 else 0)
        cmd, _, args, resp = self.recv()
        n = args[0] if cmd == CMD_ACK else 0
        if not n:
            return None
        ans = resp[:n]
        if n == 1 and ans[0] < 0x10:            # an ACK/NAK: 4 bits
            return 4, ans, b""
        return 8 * n, ans, odd_parity(ans)


class Pm3Relay:
    """The link card that is a real one, through a Proxmark3."""

    def __init__(self, port=None):
        self.pm3 = Proxmark3(port)
        exact = "exact parity" if self.pm3.ng else "parity added by the Proxmark"
        self.name = (f"real cards through a Proxmark3 on {self.pm3.port} "
                     f"({self.pm3.version}; {exact})")

    def field(self, on):
        self.pm3.field(on)

    def frame(self, nbits, data, parity, mode="a"):
        if mode != "a":
            return None                         # ISO14443A only
        return self.pm3.transceive(nbits, data, parity)


# ------------------------------------------------------------ a simulated one

def simulate(card, master_fd, ng=True, quiet=False):
    """Be a Proxmark3 on `master_fd` (a pty) with `card` (an fe_nfc_link
    Card) in its field: enough of the protocol for Proxmark3 above."""
    buf, field = b"", False

    def reply_ng(cmd, data=b"", status=0):
        os.write(master_fd, struct.pack("<IHbbH", RESP_MAGIC, len(data) | 0x8000, status, 0,
                                        cmd) + data + struct.pack("<H", 0x3362))

    def reply_mix(cmd, a0, data=b""):
        body = struct.pack("<QQQ", a0, 0, 0) + data
        os.write(master_fd, struct.pack("<IHbbH", RESP_MAGIC, len(body), 0, 0, cmd) + body +
                 struct.pack("<H", 0x3362))

    while True:
        try:
            chunk = os.read(master_fd, 4096)
        except OSError:
            return
        if not chunk:
            return
        buf += chunk
        while len(buf) >= 10:
            magic, lng, cmd = struct.unpack("<IHH", buf[:8])
            length, is_ng = lng & 0x7FFF, bool(lng & 0x8000)
            if len(buf) < 8 + length + 2:
                break
            data, buf = buf[8:8 + length], buf[8 + length + 2:]
            args = (0, 0, 0)
            if not is_ng:
                args, data = struct.unpack("<QQQ", data[:24]), data[24:]
            if cmd == CMD_VERSION:
                reply_ng(CMD_VERSION, b"\0" * 12 + b"os: Iceman/master/v4.99999-sim\0")
                continue
            if cmd == CMD_HF_DROPFIELD:
                if field:
                    card.field(False)
                field = False
                continue
            if cmd != CMD_HF_ISO14443A_READER:
                continue
            if is_ng:
                if not ng or len(data) < 16:
                    if ng:
                        reply_ng(CMD_HF_ISO14443A_READER, status=-1)
                    continue
                flags, _, _, n, nbits = struct.unpack("<IIIHH", data[:16])
                payload = data[16:16 + n]
            else:
                if ng:
                    continue
                flags, n, nbits, payload = args[0], args[1] & 0xFFFF, args[1] >> 16, data[:args[1] & 0xFFFF]
            if flags & ISO14A_CONNECT and not field:
                field = True
                card.field(True)
            if not flags & ISO14A_RAW:
                continue
            if flags & ISO14A_NO_PARITY:        # air bits in, air bits out
                ans = card.frame(*air_split(payload, nbits or 8 * n))
            else:
                ans = card.frame(nbits or 8 * n, payload, odd_parity(payload))
            if isinstance(ans, (bytes, bytearray)):
                ans = (8 * len(ans), bytes(ans))
            if not quiet:
                print(f"[pm3-sim] {payload.hex()} -> {ans[1].hex() if ans else '-'}",
                      file=sys.stderr, flush=True)
            if flags & ISO14A_NO_PARITY:
                if not ans:
                    raw = b""
                elif ans[0] == 4:
                    raw = bytes([ans[1][0] & 0x0F])
                else:
                    raw = air_join(ans[0], ans[1], ans[2] if len(ans) > 2 else None)[1]
                reply_ng(CMD_HF_ISO14443A_READER, struct.pack("<HBB", len(raw), 0, 0) + raw)
            elif ng:
                resp = ans[1] if ans else b""
                reply_ng(CMD_HF_ISO14443A_READER, struct.pack("<HBB", len(resp), 0, 0) + resp)
            else:
                resp = ans[1] if ans else b""
                reply_mix(CMD_ACK, len(resp), resp.ljust(512, b"\0"))


def main():
    args = sys.argv[1:]
    if args and args[0] == "sim":
        import pty
        sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
        import fe_nfc_link
        old = "--old" in args
        master, slave = pty.openpty()
        tty.setraw(slave)
        uid = bytes.fromhex("04A1B2C3D4E5F6")
        card = fe_nfc_link.DemoIsoDep(uid)
        print(f"[pm3-sim] a Proxmark3 ({'MIX, old' if old else 'NG'} firmware) on "
              f"{os.ttyname(slave)}, an ISO14443-4A card {uid.hex().upper()} in its field",
              flush=True)
        simulate(card, master, ng=not old)
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main())
