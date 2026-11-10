#!/usr/bin/env python3
# Copyright (c) 2026, Kara Zajac
# SPDX-License-Identifier: BSD-3-Clause
"""The firmware's log: what a Flipper writes to its UART.

On a real Flipper the log comes out of pins 13 and 14 of the GPIO header
(USART1), for whoever holds a serial adapter to them.  The emulator keeps it
in devices/NAME/uart.log, whatever else `run --serial` does with it, and this
reads that file: `flipperemu log` in a terminal, and the firmware log of the
window.

A line of the log is `<ms since it started> [<level>][<tag>] <text>`, with
colour codes around the level and the tag; what is not (a crash report, an
app's printf) is passed as it is.

    fe_log.py devices/NAME/uart.log [-f] [-n LINES] [--level LEVEL] [--grep RE]
"""
import argparse
import collections
import os
import re
import sys
import time

ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
CONTROL = re.compile(r"[\x00-\x08\x0a-\x1f\x7f]")       # (a tab stays)
LINE = re.compile(r"^(\d+) \[([EWIDT])\]\[([^\]]*)\] ?(.*)$", re.S)
ORDER = "TDIWE"                         # from the least to the most serious
NAMES = {"E": "error", "W": "warning", "I": "info", "D": "debug", "T": "trace"}
# what `--level` and the window's filter take
LEVELS = {"trace": "T", "debug": "D", "info": "I", "warn": "W", "warning": "W",
          "error": "E"}
# the firmware's own levels (FuriLogLevel), as `run --log-level` and
# `log --set-level` name them; default is its info
FIRMWARE_LEVELS = ("default", "none", "error", "warn", "info", "debug", "trace")
CRASH = re.compile(r"\[(CRASH|HALT)\](.*)")
BASIC = ("black", "red", "green", "yellow", "blue", "magenta", "cyan", "white")
LONGEST = 8192                          # a line without an end is cut there
CHUNK = 1 << 20                         # read by one poll()

Line = collections.namedtuple("Line", "raw plain ms level tag text")


class Mark(str):
    """Between the lines of what Log.poll() returns"""


RESTART = Mark("a new run")             # the log file was replaced
REBOOT = Mark("it started again")       # the firmware did, within a run


def parse(raw):
    """raw: one line as the firmware wrote it, without its line end"""
    plain = CONTROL.sub("", ANSI.sub("", raw))
    m = LINE.match(plain)
    if not m:
        return Line(raw, plain, None, "", "", plain)
    return Line(raw, plain, int(m.group(1)), m.group(2), m.group(3), m.group(4))


def passes(line, level="", pattern=None):
    """level: the least serious one to let through ('' for all of them; a line
    that is not a log line has none and always passes); pattern: a compiled
    regex its text must hold"""
    if level and line.level and ORDER.index(line.level) < ORDER.index(level):
        return False
    return not pattern or bool(pattern.search(line.plain))


def smart(pattern):
    """A regex typed as a filter: capitals in it matter only if it has any"""
    return re.compile(pattern, 0 if any(c.isupper() for c in pattern) else re.I)


def crash_of(line):
    """'[CRASH][thread] message' -> 'CRASH', '[thread] message'; else None"""
    m = CRASH.search(line.plain)
    if not m or line.level:
        return None
    return m.group(1), m.group(2).strip()


def sgr(colour, params):
    """The text colour after the colour code `ESC [ params m`"""
    codes = [int(p) if p.isdigit() else 0 for p in params.split(";")]
    i = 0
    while i < len(codes):
        n = codes[i]
        if n in (0, 39):
            colour = None
        elif 30 <= n <= 37 or 90 <= n <= 97:
            colour = BASIC[n % 10]
        elif n in (38, 48):             # one of 256, or RGB: not told apart
            i += 2 if codes[i + 1:i + 2] == [5] else 4
        i += 1
    return colour


def segments(raw, colour=None):
    """A line in pieces of one colour, [(text, colour)], and the colour it
    leaves on for the next line (which is how a crash report is red).
    colour: the one the line before left on"""
    out, pos = [], 0
    for m in ANSI.finditer(raw):
        if m.start() > pos:
            out.append((raw[pos:m.start()], colour))
        if m.group().endswith("m"):
            colour = sgr(colour, m.group()[2:-1])
        pos = m.end()
    out.append((raw[pos:], colour))
    out = [(CONTROL.sub("", text), c) for text, c in out]
    return [(text, c) for text, c in out if text], colour


def code(colour):
    """The colour code that switches a terminal to it"""
    return f"\x1b[0;3{BASIC.index(colour)}m" if colour else "\x1b[0m"


class Follower:
    """A log file that grows, line by line.  When the file is replaced or cut
    short (the device was started again) it is read from its start, and
    poll() says so with RESTART."""

    def __init__(self, path, tail=None):
        """tail: of a log that is there already, only so many bytes of its
        end (None: all of it)"""
        self.path = str(path)
        self.tail = tail
        self.f = None
        self.ino = None
        self.pos = 0
        self.skip = False           # in the middle of a line: to its end
        self.more = False           # the last poll() did not reach the end
        self.rest = b""             # the start of a line that is not complete
        self.rest_since = 0.0

    def close(self):
        if self.f:
            self.f.close()
            self.f = None

    def _open(self):
        try:
            f = open(self.path, "rb")
        except OSError:
            return False
        self.close()
        st = os.fstat(f.fileno())
        self.f, self.ino, self.pos, self.rest = f, st.st_ino, 0, b""
        self.skip = self.tail is not None and st.st_size > self.tail
        if self.skip:
            self.pos = st.st_size - self.tail
        self.tail = None            # (of the first one only)
        return True

    def poll(self, flush_after=1.0):
        """The lines that are new (str, without line ends) since the last
        call.  A line the firmware has not ended is kept back for
        `flush_after` seconds (a crash can end in the middle of one)."""
        out = []
        self.more = False
        try:
            st = os.stat(self.path)
        except OSError:
            st = None
        if self.f is None:
            if st is None or not self._open():
                return out
        elif st is None:
            return out              # gone: what was read from it stays
        elif st.st_ino != self.ino or st.st_size < self.pos:
            if not self._open():
                return out
            out.append(RESTART)
        self.f.seek(self.pos)
        data = self.f.read(CHUNK)
        self.pos += len(data)
        self.more = len(data) == CHUNK
        if self.skip and data:
            cut = data.find(b"\n")
            self.skip = cut < 0
            data = b"" if cut < 0 else data[cut + 1:]
            if not data:
                return out
        if data:
            if not self.rest:
                self.rest_since = time.monotonic()
            *lines, self.rest = (self.rest + data).split(b"\n")
            if len(self.rest) > LONGEST:
                lines.append(self.rest)
                self.rest = b""
            if lines:
                self.rest_since = time.monotonic()
            out += [ln.rstrip(b"\r").decode(errors="replace") for ln in lines]
        elif self.rest and time.monotonic() - self.rest_since >= flush_after:
            out += self.flush()
        return out

    def flush(self):
        """What is left of a line without an end"""
        if not self.rest:
            return []
        rest, self.rest = self.rest, b""
        return [rest.rstrip(b"\r").decode(errors="replace")]


class Log:
    """The log at `path`, read as it grows.  poll() returns what is new: its
    Lines, with RESTART where a new run began (the file was replaced) and
    REBOOT where the firmware started again within one.  The firmware counts
    its timestamps from its start: one that goes back is a new start."""

    def __init__(self, path, tail=None):
        self.follower = Follower(path, tail)
        self.last_ms = None

    @property
    def more(self):
        return self.follower.more

    def close(self):
        self.follower.close()

    def _parsed(self, items):
        out = []
        for item in items:
            if item is RESTART:
                self.last_ms = None
                out.append(item)
                continue
            line = parse(item)
            if line.ms is not None:
                if self.last_ms is not None and line.ms < self.last_ms:
                    out.append(REBOOT)
                self.last_ms = line.ms
            out.append(line)
        return out

    def poll(self, flush_after=1.0):
        return self._parsed(self.follower.poll(flush_after))

    def flush(self):
        return self._parsed(self.follower.flush())


def show(path, follow=False, count=None, level="", pattern=None, color=None,
         out=None, wait=0.1, stop=None):
    """Print the log at `path`; follow: and what is added to it, until ^C (or
    until stop() is true).  count: of what is there already, only the last so
    many lines.  color: None is "if `out` is a terminal".  Returns the number
    of lines printed, or None if there is no log."""
    out = out or sys.stdout
    if color is None:
        color = out.isatty()
    if not follow and not os.path.exists(path):
        return None
    log = Log(path)
    printed = 0
    colour = None                   # the one the line before left on
    marks = []                      # those since the last line that passed

    def passing(items):
        """Of each line that passes: the marks before it, the line, and the
        colour that was on before it"""
        nonlocal colour, marks
        kept = []
        for item in items:
            if isinstance(item, Mark):
                marks.append(item)
                colour = None
                continue
            before = colour
            colour = segments(item.raw, colour)[1]
            if item.plain.strip() and passes(item, level, pattern):
                kept.append((marks, item, before))
                marks = []
        return kept

    def emit(entries):
        nonlocal printed
        for before_it, line, before in entries:
            for mark in before_it:
                out.write(f"--- {mark} ---\n")
            if color:
                text = (code(before) if before else "") + line.raw
                # a colour the firmware left on must not stain the terminal
                text += "\x1b[0m" if "\x1b[" in text else ""
            else:
                text = line.plain
            out.write(text + "\n")
            printed += 1
        out.flush()

    try:
        first = None if count is None else collections.deque(maxlen=count)
        while True:                 # what is there already
            entries = passing(log.poll())
            if not log.more and not follow:
                entries += passing(log.flush())
            if first is None:
                emit(entries)
            else:
                first.extend(entries)
            if not log.more:
                break
        emit(first or [])
        while follow and not (stop and stop()):
            emit(passing(log.poll()))
            if not log.more:
                time.sleep(wait)
    except KeyboardInterrupt:
        pass
    except BrokenPipeError:
        # whoever read it went away (`| head`): not an error; and nothing
        # more is to be written when Python closes stdout
        try:
            os.dup2(os.open(os.devnull, os.O_WRONLY), out.fileno())
        except (OSError, ValueError, AttributeError):
            pass
    finally:
        log.close()
    return printed


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("log", help="devices/NAME/uart.log")
    ap.add_argument("-f", "--follow", action="store_true",
                    help="and what is added to it, until ^C")
    ap.add_argument("-n", "--lines", type=int, metavar="N",
                    help="only the last N lines of what is there")
    ap.add_argument("-l", "--level", choices=sorted(LEVELS),
                    help="only lines at least this serious")
    ap.add_argument("-g", "--grep", metavar="RE",
                    help="only lines that hold this (capitals in it matter "
                         "only if it has any)")
    args = ap.parse_args()
    n = show(args.log, args.follow, args.lines, LEVELS.get(args.level, ""),
             smart(args.grep) if args.grep else None)
    if n is None:
        print(f"{args.log}: no such log", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
