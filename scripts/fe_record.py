#!/usr/bin/env python3
# Copyright (c) 2026, Kara Zajac
# SPDX-License-Identifier: BSD-3-Clause
"""A recording of the Flipper's screen, written as an animated GIF.

    fe_record.py devices/NAME/panel.sock out.gif [--seconds N] [--scale N]

The pictures come from the front-panel link (docs/PANEL_LINK.md), as the
window gets them: each picture the LCD shows, at the time it showed it.
`./flipperemu record` and the window's "Record the screen" both use this.

The GIF is written here, with no library: two colours, the screen lit as
the window draws it, each LCD pixel `scale` pixels wide.  A picture that
follows another holds only the rectangle that changed, so a clock that
ticks or a cursor that moves costs a few bytes.
"""
import math
import signal
import socket
import struct
import sys
import time

W, H = 128, 64
BG, FG = "#ff8c1a", "#1a0e04"       # the LCD lit: the window's colours
SLOT = 0.02     # a GIF counts 1/100 s, and viewers take less than 2 for 10
LONGEST = 60000                     # ... in a 16-bit number
LIMIT = 30000   # pictures (30 MB of them): ten minutes of a screen that
                # never stands still


def rgb(colour):
    return bytes.fromhex(colour.lstrip("#"))


def changed(a, b):
    """The rectangle (x, y, w, h) of LCD pixels in which two pictures differ,
    or None"""
    rows = [y for y in range(H) if a[16 * y:16 * y + 16] != b[16 * y:16 * y + 16]]
    if not rows:
        return None
    mask = 0
    for y in rows:
        mask |= (int.from_bytes(a[16 * y:16 * y + 16], "big")
                 ^ int.from_bytes(b[16 * y:16 * y + 16], "big"))
    x0 = W - mask.bit_length()                      # (its top bit is x = 0)
    x1 = W - (mask & -mask).bit_length()
    return x0, rows[0], x1 - x0 + 1, rows[-1] - rows[0] + 1


def wide(scale):
    """byte of a picture -> its 8 pixels as colour numbers, each `scale` wide"""
    return [b"".join(bytes([(byte >> bit) & 1]) * scale
                     for bit in range(7, -1, -1)) for byte in range(256)]


def lzw(pixels):
    """GIF's compression of colour numbers 0 and 1 (2-bit codes to start
    with, as the format wants at least), in its blocks of up to 255 bytes"""
    CLEAR, END, FIRST, FULL = 4, 5, 6, 4096
    data = bytearray()
    acc = bits = 0
    width, free, table = 3, FIRST, {}

    def put(code):
        nonlocal acc, bits
        acc |= code << bits
        bits += width
        while bits >= 8:
            data.append(acc & 0xFF)
            acc >>= 8
            bits -= 8

    put(CLEAR)
    have = pixels[0]
    for pixel in memoryview(pixels)[1:]:
        key = (have << 1) | pixel
        code = table.get(key)
        if code is not None:
            have = code
            continue
        put(have)
        have = pixel
        if free < FULL:
            table[key] = free
            free += 1
            if free > (1 << width) and width < 12:
                width += 1              # (the reader is one entry behind)
        else:
            put(CLEAR)
            width, free = 3, FIRST
            table.clear()
    put(have)
    put(END)
    if bits:
        data.append(acc & 0xFF)
    out = bytearray()
    for at in range(0, len(data), 255):
        block = data[at:at + 255]
        out += bytes([len(block)]) + block
    return bytes(out) + b"\0"


class Recording:
    """Pictures of the LCD (1024 bytes each: 64 rows of 16, the leftmost
    pixel in a byte's top bit), and when each came"""

    def __init__(self):
        self.shots = []                 # (seconds, picture)
        self.end = None

    def add(self, frame, now=None):
        """The LCD shows this from now on"""
        now = time.monotonic() if now is None else now
        if len(frame) != W * H // 8 or self.full():
            return
        frame = bytes(frame)
        if not self.shots or self.shots[-1][1] != frame:
            self.shots.append((now, frame))
        self.end = now

    def stop(self, now=None):
        if self.shots:
            self.end = max(time.monotonic() if now is None else now,
                           self.shots[-1][0])

    def full(self):
        return len(self.shots) >= LIMIT

    def seconds(self):
        return self.end - self.shots[0][0] if self.shots else 0.0

    def frames(self):
        """[(picture, how long it stays, in 1/100 s)], on the GIF's clock:
        of two pictures in one of its slots, the later is the one seen"""
        if not self.shots:
            return []
        t0 = self.shots[0][0]
        kept = []                       # [slot, picture]
        for t, frame in self.shots:
            slot = int((t - t0) / SLOT)
            if kept and kept[-1][0] == slot:
                kept[-1][1] = frame
                if len(kept) > 1 and kept[-2][1] == frame:
                    kept.pop()          # ... and it is the one before it
            elif not kept or kept[-1][1] != frame:
                kept.append([slot, frame])
        last = max(kept[-1][0] + 1, math.ceil((self.end - t0) / SLOT))
        slots = [slot for slot, _ in kept[1:]] + [last]
        return [(frame, (until - slot) * 2)
                for (slot, frame), until in zip(kept, slots)]

    def gif(self, scale=4, bg=BG, fg=FG):
        """The file's bytes; plays for ever"""
        frames = self.frames()
        if not frames:
            raise ValueError("no picture to write")
        table = wide(scale)
        out = bytearray(b"GIF89a")
        out += struct.pack("<HHBBB", W * scale, H * scale, 0xF0, 0, 0)
        out += rgb(bg) + rgb(fg)                    # colours 0 and 1
        out += b"\x21\xff\x0bNETSCAPE2.0\x03\x01\x00\x00\x00"   # repeat

        def image(frame, x, y, w, h, hundredths):
            pixels = bytearray()
            for row in range(y, y + h):
                line = b"".join(table[byte]
                                for byte in frame[16 * row:16 * row + 16])
                pixels += line[x * scale:(x + w) * scale] * scale
            # (it stays where it is when the next one comes)
            return (b"\x21\xf9\x04\x04" + struct.pack("<H", hundredths)
                    + b"\0\0\x2c" + struct.pack("<HHHHB", x * scale, y * scale,
                                                 w * scale, h * scale, 0)
                    + b"\x02" + lzw(pixels))

        before = None
        for frame, hundredths in frames:
            box = changed(before, frame) if before else (0, 0, W, H)
            x, y, w, h = box or (0, 0, 1, 1)
            while True:                 # one that stays longer than a GIF
                stay = min(hundredths, LONGEST)     # can count: again, as
                out += image(frame, x, y, w, h, stay)   # one pixel of it
                hundredths -= stay
                if not hundredths:
                    break
                w = h = 1
            before = frame
        return bytes(out) + b"\x3b"

    def save(self, path, **how):
        """-> (pictures, seconds, bytes)"""
        data = self.gif(**how)
        with open(path, "wb") as f:
            f.write(data)
        return len(self.frames()), self.seconds(), len(data)


def record(path, seconds=None, stop=lambda: False, patience=2.0):
    """What a device's LCD shows, from its front-panel link, until `seconds`
    have passed, stop() says so or the emulator has gone.  -> a Recording;
    None when another program has the link (the emulator talks to one)."""
    s = socket.socket(socket.AF_UNIX)
    s.connect(str(path))
    s.sendall(b"hello flipperemu record\n")
    s.settimeout(0.05)
    rec = Recording()
    buf = b""
    since = time.monotonic()
    hello = None
    try:
        while not stop() and not rec.full():
            now = time.monotonic()
            if hello is None and now - since > patience:
                return None
            if seconds is not None and hello is not None and now - hello >= seconds:
                break
            try:
                data = s.recv(65536)
            except socket.timeout:
                continue
            except InterruptedError:
                continue
            if not data:
                break
            now = time.monotonic()
            *lines, buf = (buf + data).split(b"\n")
            for line in lines:
                if line.startswith(b"hello ") and hello is None:
                    hello = now
                elif line.startswith(b"lcd "):
                    try:
                        rec.add(bytes.fromhex(line[4:].decode("ascii")), now)
                    except ValueError:
                        pass
    finally:
        s.close()
    if hello is None:
        return None
    rec.stop()
    return rec


def main():
    import argparse
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("panel", help="devices/NAME/panel.sock")
    ap.add_argument("out", help="the GIF to write")
    ap.add_argument("--seconds", type=float, help="how long (default: until ^C)")
    ap.add_argument("--scale", type=int, default=4,
                    help="pixels to an LCD pixel (default 4: 512x256)")
    args = ap.parse_args()
    done = []
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: done.append(1))
    rec = record(args.panel, args.seconds, lambda: bool(done))
    if rec is None:
        sys.exit("another program has the front-panel link")
    if not rec.shots:
        sys.exit("no picture came")
    frames, seconds, size = rec.save(args.out, scale=args.scale)
    print(f"{frames} pictures, {seconds:.1f} s: {args.out} ({size} bytes)")


if __name__ == "__main__":
    main()
