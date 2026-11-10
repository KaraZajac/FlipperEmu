#!/usr/bin/env python3
# Copyright (c) 2026, Kara Zajac
# SPDX-License-Identifier: BSD-3-Clause
"""Talk to the Flipper CLI of a running FlipperEmu device over its USB CDC port.

The emulator bridges the firmware's USB serial port (the one qFlipper and
`screen /dev/ttyACM0` use on a real Flipper) to a QEMU chardev with the id
"usb". This finds that chardev through the monitor and connects to it; that
raises DTR, the firmware starts a CLI session, and closing the connection
ends it. The port is one line for one client at a time: a lock file (lock=)
makes this project's own commands take turns.

    fe_cli.py MONITOR_SOCK                         interactive, Ctrl-] quits
    fe_cli.py MONITOR_SOCK "info device" "storage list /ext"
"""
import fcntl
import os
import re
import select
import socket
import sys
import time

PROMPT = b">: "
ANSI = re.compile(rb"\x1b\[[0-9;]*[A-Za-z]")


def usb_endpoint(monitor, label="usb"):
    """('pty', '/dev/pts/N') | ('unix', path) | ('tcp', (host, port)) | None

    monitor: the monitor socket path, or an open fe_monitor.Monitor (QEMU
    serves one monitor client at a time)."""
    from fe_monitor import Monitor
    if isinstance(monitor, Monitor):
        out = monitor.cmd("info chardev")
    else:
        m = Monitor(str(monitor))
        try:
            out = m.cmd("info chardev")
        finally:
            m.close()
    for line in out.splitlines():
        name, _, rest = line.partition(": filename=")
        if name.strip() != label:
            continue
        rest = rest.removeprefix("disconnected:").split(",")[0]
        kind, _, where = rest.partition(":")
        if kind == "pty":
            return "pty", where
        if kind == "unix":
            return "unix", where
        if kind == "tcp":
            host, _, port = where.rpartition(":")
            return "tcp", (host or "127.0.0.1", int(port))
        return kind, where
    return None


class PortBusy(ConnectionError):
    """Another command of ours has the USB serial port"""


class FlipperCli:
    def __init__(self, endpoint, timeout=10.0, lock=None, wait=10.0):
        """lock: a file that whoever talks on this port holds locked; it is
        waited for for `wait` seconds (a command of a few lines is over
        sooner), then it is PortBusy"""
        kind, where = endpoint
        self.sock = self.lock = None
        if lock:
            self.take(lock, wait)
        if kind == "pty":
            import tty
            self.fd = os.open(where, os.O_RDWR | os.O_NOCTTY)
            tty.setraw(self.fd)
        elif kind in ("unix", "tcp"):
            fam = socket.AF_UNIX if kind == "unix" else socket.AF_INET
            self.sock = socket.socket(fam, socket.SOCK_STREAM)
            self.sock.settimeout(timeout)
            self.sock.connect(where)
            self.fd = self.sock.fileno()
        else:
            raise ValueError(f"cannot connect to a '{kind}' chardev")
        self.buf = b""

    def take(self, lock, wait):
        f = open(lock, "a+")
        end = time.monotonic() + wait
        while True:
            try:
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() > end:
                    f.seek(0)
                    who = f.read().strip()
                    f.close()
                    raise PortBusy("its USB serial port is in use"
                                   + (f" by {who}" if who else "")
                                   + ": one at a time") from None
                time.sleep(0.1)
        f.seek(0)
        f.truncate()
        f.write(f"`{' '.join([os.path.basename(sys.argv[0]), *sys.argv[1:]])}` "
                f"(pid {os.getpid()})\n")
        f.flush()
        self.lock = f

    def close(self):
        if self.sock:
            self.sock.close()
        else:
            os.close(self.fd)
        if self.lock:
            self.lock.seek(0)
            self.lock.truncate()
            self.lock.close()               # (which lets it go)
            self.lock = None

    def _fill(self, timeout):
        r, _, _ = select.select([self.fd], [], [], timeout)
        if not r:
            return False
        data = self.sock.recv(65536) if self.sock else os.read(self.fd, 65536)
        if not data:
            raise ConnectionError("USB CDC connection closed")
        self.buf += data
        return True

    def read_until_prompt(self, timeout):
        """Everything up to and including the next CLI prompt."""
        end = time.time() + timeout
        while not self.buf.endswith(PROMPT):
            if time.time() > end:
                raise TimeoutError("no CLI prompt (is the firmware's CLI over "
                                   "USB enabled?)")
            self._fill(min(0.2, max(0.0, end - time.time())))
        out, self.buf = self.buf, b""
        return out

    def read_exactly(self, n, timeout):
        """The next n bytes, whatever they are"""
        end = time.time() + timeout
        while len(self.buf) < n:
            if time.time() > end:
                raise TimeoutError(f"the Flipper sent {len(self.buf)} of "
                                   f"{n} bytes, and no more")
            self._fill(min(0.2, max(0.0, end - time.time())))
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def read_line(self, timeout):
        """The next line, without its end"""
        end = time.time() + timeout
        while b"\r\n" not in self.buf:
            if time.time() > end:
                raise TimeoutError("the Flipper's CLI does not answer")
            self._fill(min(0.2, max(0.0, end - time.time())))
        line, _, self.buf = self.buf.partition(b"\r\n")
        return ANSI.sub(b"", line).decode(errors="replace")

    def write(self, data: bytes):
        if self.sock:
            self.sock.sendall(data)
            return
        view = memoryview(data)
        while view:                         # a pty takes what it has room for
            view = view[os.write(self.fd, view):]

    def run(self, command, timeout=30.0, color=False):
        """Run one CLI command, return its output (without echo and prompt)."""
        self.write(command.encode() + b"\r")
        out = self.read_until_prompt(timeout)
        out = out[:-len(PROMPT)]
        first, _, rest = out.partition(b"\r\n")       # the echoed command
        if not color:
            rest = ANSI.sub(b"", rest)
        return rest.decode(errors="replace").replace("\r\n", "\n").rstrip("\n")

    def watch(self, command, until, timeout=10.0, stop=True):
        """Start a command that runs until Ctrl+C (`rfid read`, `ir rx`, ...),
        collect its output until the regex `until` matches or `timeout`
        passes, then stop it with Ctrl+C. Returns (match or None, output).
        An empty command watches one that is already running."""
        pattern = re.compile(until.encode() if isinstance(until, str) else until)
        if command:
            self.write(command.encode() + b"\r")
        end, found = time.time() + timeout, None
        while time.time() < end:
            found = pattern.search(ANSI.sub(b"", self.buf))
            if found:
                break
            self._fill(min(0.1, max(0.0, end - time.time())))
        out = self.buf
        self.buf = b""
        if stop:
            self.write(b"\x03")
            try:
                out += self.read_until_prompt(5)
            except TimeoutError:
                out += self.buf
                self.buf = b""
        out = ANSI.sub(b"", out).decode(errors="replace")
        return found, out.replace("\r\n", "\n")

    def interactive(self):
        import termios
        import tty
        stdin = sys.stdin.fileno()
        old = termios.tcgetattr(stdin) if os.isatty(stdin) else None
        if old:
            tty.setraw(stdin)
        try:
            if self.buf:
                os.write(1, self.buf)
                self.buf = b""
            while True:
                r, _, _ = select.select([stdin, self.fd], [], [])
                if self.fd in r:
                    data = self.sock.recv(65536) if self.sock else os.read(self.fd, 65536)
                    if not data:
                        break
                    os.write(1, data)
                if stdin in r:
                    data = os.read(stdin, 1024)
                    if not data or b"\x1d" in data:        # Ctrl-]
                        break
                    self.write(data.replace(b"\n", b"\r"))
        finally:
            if old:
                termios.tcsetattr(stdin, termios.TCSADRAIN, old)
            print()


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    ep = usb_endpoint(sys.argv[1])
    if not ep:
        print("no 'usb' chardev on that QEMU (run with -chardev ...,id=usb "
              "-M flipper-zero,usb-serial=usb)", file=sys.stderr)
        return 1
    cli = FlipperCli(ep)
    try:
        if len(sys.argv) == 2:
            print(f"[USB CDC {ep[1]}; Ctrl-] quits]")
            cli.interactive()
            return 0
        cli.read_until_prompt(15)
        for command in sys.argv[2:]:
            print(cli.run(command, color=sys.stdout.isatty()))
    finally:
        cli.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
