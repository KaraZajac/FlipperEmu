#!/usr/bin/env python3
# Copyright (c) 2026, Kara Zajac
# SPDX-License-Identifier: BSD-3-Clause
"""Drive a running FlipperEmu instance through QEMU's HMP monitor socket.

    fe_monitor.py SOCK screenshot out.png      # LCD -> PNG (needs Pillow for PNG)
    fe_monitor.py SOCK press ok|back|up|down|left|right [ms]
    fe_monitor.py SOCK hmp "info registers"    # any monitor command

Start the emulator with:  -monitor unix:SOCK,server=on,wait=off
"""
import os
import re
import socket
import sys
import tempfile
import time

KEYS = {"ok": "ret", "back": "backspace", "up": "up", "down": "down",
        "left": "left", "right": "right"}


class Monitor:
    def __init__(self, path, timeout=5.0):
        self.s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.s.settimeout(timeout)
        self.s.connect(path)
        self._read()

    def _read(self):
        buf = b""
        while not buf.rstrip().endswith(b"(qemu)"):
            try:
                chunk = self.s.recv(65536)
            except socket.timeout:
                break
            if not chunk:
                break
            buf += chunk
        return buf.decode(errors="replace")

    def cmd(self, line):
        self.s.sendall(line.encode() + b"\n")
        out = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", self._read())
        # strip the echoed command and the trailing prompt
        lines = [l for l in out.replace("\r", "").split("\n")
                 if not l.startswith("(qemu)") and not l.rstrip().endswith(line.strip())]
        return "\n".join(lines).strip()

    def screenshot(self, png_path):
        fd, ppm = tempfile.mkstemp(suffix=".ppm")
        os.close(fd)
        self.cmd(f"screendump {ppm}")
        for _ in range(50):              # the dump is written asynchronously
            if os.path.getsize(ppm) > 0:
                break
            time.sleep(0.05)
        if png_path.endswith(".ppm"):
            os.replace(ppm, png_path)
            return png_path
        from PIL import Image
        Image.open(ppm).save(png_path)
        os.unlink(ppm)
        return png_path

    def press(self, key, hold_ms=120):
        self.cmd(f"sendkey {KEYS.get(key, key)} {hold_ms}")
        time.sleep(hold_ms / 1000 + 0.05)


def main():
    if len(sys.argv) < 3:
        print(__doc__)
        return 2
    m = Monitor(sys.argv[1])
    op = sys.argv[2]
    if op == "screenshot":
        print(m.screenshot(sys.argv[3]))
    elif op == "press":
        m.press(sys.argv[3], int(sys.argv[4]) if len(sys.argv) > 4 else 120)
    elif op == "hmp":
        print(m.cmd(" ".join(sys.argv[3:])))
    else:
        print(__doc__)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
