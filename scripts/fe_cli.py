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
QUIET = 2.0                 # seconds without a byte before gone() is asked
# the most that a command that was cut off can still wait for: one chunk of a
# `storage write_chunk`, as the firmware's own tools send it (ours: 1024)
CUT_MOST = 8192 + 64
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


def take_port(lock, wait=10.0, what="its USB serial port is"):
    """Lock the file that whoever talks on a device's USB serial port holds
    locked.  It is waited for for `wait` seconds (a command of a few lines is
    over sooner), then it is PortBusy.  -> the open file, for leave_port().
    Its `left` is who had the port before and did not leave it (it was killed
    in the middle of what it did there), or ''."""
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
                raise PortBusy(f"{what} in use"
                               + (f" by {who}" if who else "")
                               + ": one at a time") from None
            time.sleep(0.1)
    f.seek(0)
    left = f.read().strip()                 # (leave_port() takes it away)
    f.seek(0)
    f.truncate()
    f.write(f"`{' '.join([os.path.basename(sys.argv[0]), *sys.argv[1:]])}` "
            f"(pid {os.getpid()})\n")
    f.flush()
    f.left = left
    return f


def leave_port(f):
    f.seek(0)
    f.truncate()
    f.close()                               # (which lets it go)


class FlipperCli:
    def __init__(self, endpoint, timeout=10.0, lock=None, wait=10.0):
        """lock: a file that whoever talks on this port holds locked
        (take_port)"""
        kind, where = endpoint
        self.sock = self.lock = None
        # who had the port before and was killed there (take_port); "" = it
        # left as it should
        self.cut = ""
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
        # gone(): asked while a prompt is long in coming; true = none will
        # come (the firmware has halted), and the wait ends there
        self.gone = None

    def take(self, lock, wait):
        self.lock = take_port(lock, wait)
        self.cut = self.lock.left

    def close(self):
        if self.sock:
            self.sock.close()
        else:
            os.close(self.fd)
        if self.lock:
            leave_port(self.lock)
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

    def read_until_prompt(self, timeout, what=None):
        """Everything up to and including the next CLI prompt.
        what: what to say when it does not come"""
        end = time.time() + timeout
        heard = time.time()
        while not self.buf.endswith(PROMPT):
            if time.time() > end:
                raise TimeoutError(what or "the Flipper's CLI does not answer")
            if self._fill(min(0.2, max(0.0, end - time.time()))):
                heard = time.time()
            elif self.gone and time.time() - heard > QUIET:
                heard = time.time()
                if self.gone():
                    raise TimeoutError("the Flipper's CLI answers no more")
        out, self.buf = self.buf, b""
        return out

    def end_cut_off(self, wait=1.5, most=CUT_MOST, step=64, quiet=0.3,
                    limit=20.0):
        """The command before this one was cut off: where did that leave the
        firmware's CLI?  A firmware that ends a command when the port closes
        prompts at once.  An old one (official 1.0.1) does not: its `storage
        write_chunk` waits for every byte of its chunk, however long, and
        takes for one of them the mark that the port's closing puts into what
        it reads.  So it is either still waiting, and says nothing, or it had
        its chunk one byte early, and the last byte that was sent is typed at
        its prompt, where the next command would begin with it.  Either way it
        gets Ctrl-C, as it would at a terminal: as many as it still waits for,
        or the one that clears its line.
        -> (how many it got, what was typed at its prompt), with the CLI at an
        empty prompt and what it said in buf; None if it came to no prompt."""
        end = time.time() + limit

        def hear(first):
            got = self._fill(first)
            while got and time.time() < end and self._fill(quiet):
                pass
            return got

        hear(wait)
        sent, typed = 0, b""
        while not self.buf.endswith(PROMPT):
            if sent >= most or time.time() > end:
                return None
            _, free, _ = select.select([], [self.fd], [], 0.5)
            if not free:
                return None         # it takes no more: it is busy, not waiting
            at = self.buf.rfind(PROMPT)
            if at >= 0:
                typed = self.buf[at + len(PROMPT):]
            # one clears a line (or ends a command that still talks); a chunk
            # that is waited for takes many
            piece = b"\x03" * (1 if self.buf else min(step, most - sent))
            sent += self.sock.send(piece) if self.sock \
                else os.write(self.fd, piece)
            hear(1.0 if self.buf else 0.05)
        return sent, typed

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
        return self.answer(self.read_until_prompt(
            timeout, f"the Flipper's CLI was not done with `{command}` "
                     f"after {timeout:g} s"), color)

    @staticmethod
    def answer(out, color=False):
        """What a command printed, from all that came up to its prompt"""
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

    def interactive(self, newline=b"\r"):
        """A terminal on it, until Ctrl-].  newline: what a typed line feed
        becomes (the CLI wants a carriage return); None: nothing else"""
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
                    self.write(data.replace(b"\n", newline) if newline
                               else data)
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
