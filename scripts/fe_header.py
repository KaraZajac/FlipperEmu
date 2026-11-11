"""The GPIO header, as the machine's `header` property has it: read by the
launcher's `gpio` and by the window.

Eighteen pins along the top of a Flipper.  Five are power (1 is +5V, 9 is 3V3;
8, 11 and 18 are GND), thirteen are pins of the MCU.  The property has a word
for each of those, "NUMBER:PIN:MODE:PULL:LEVEL:HELD":

    2:PA7:out:none:1:free 3:PA6:in:up:0:low ... 13:PB6:af7:up:?:- ...

and takes "PA6=1", "PA6=0" and "PA6=z" to hold a pin from outside, as a wire
to 3V3 or to GND would, and to let go of it.

The machine's `header-record` is a file that gets every change of a pin's
level with its time, a VCD file as logic analyzers' programs read (Record).

Copyright (c) 2026, Kara Zajac
SPDX-License-Identifier: BSD-3-Clause
"""
import re

# What is printed beside the pins on a Flipper's case, by the pin's number
LABELS = {2: "A7", 3: "A6", 4: "A4", 5: "B3", 6: "B2", 7: "C3", 10: "SWC",
          12: "SIO", 13: "TX", 14: "RX", 15: "C1", 16: "C0", 17: "1W"}
POWER = {1: "+5V", 8: "GND", 9: "3V3", 11: "GND", 18: "GND"}
# The pins that are not held from here, and why
NOT_HELD = {13: "the firmware's log is on this UART (its TX)",
            14: "the firmware's log is on this UART (its RX)",
            17: "the iButton pin (`ibutton` holds a key to it)"}


def parse(text):
    """The header's signal pins in their order on it, each a dict: its
    "number" there, the MCU's "pin" (PA7), the "label" on the case, what the
    firmware has it as ("mode": input, output, alternate or analog;
    "function": which alternate one; "open_drain"; "pull": up, down or None),
    its "level" (high, low, floating: nothing holds it, or unknown: a
    peripheral's signal, which the emulator does not keep as a level), and
    what the outside holds on it ("held": high, low or None; "holdable": may
    it)."""
    pins = []
    for word in text.split():
        number, pin, mode, pull, level, held = word.split(":")
        function = re.fullmatch(r"af(\d+)(od)?", mode)
        pins.append({
            "number": int(number), "pin": pin,
            "label": LABELS.get(int(number), pin[1:]),
            "mode": "alternate" if function else
                    {"in": "input", "out": "output", "od": "output"}.get(mode, mode),
            "function": int(function[1]) if function else None,
            "open_drain": mode.endswith("od"),
            "pull": None if pull == "none" else pull,
            "level": {"0": "low", "1": "high", "z": "floating"}.get(level, "unknown"),
            "held": held if held in ("high", "low") else None,
            "holdable": held != "-"})
    return pins


def mode_text(p):
    """What the firmware has a pin as, in words"""
    return ((f"alternate function {p['function']}" if p["mode"] == "alternate"
             else p["mode"])
            + (", open drain" if p["open_drain"] else "")
            + (f", pull-{p['pull']}" if p["pull"] else ""))


def overruled(p):
    """Is something held to a pin that the firmware drives both ways?  On
    hardware the two would fight; here the firmware's level is the pin's."""
    return bool(p["held"]) and p["mode"] == "output" and not p["open_drain"]


LEVELS = {"0": "low", "1": "high", "z": "floating", "x": "unknown"}


class Record:
    """A record of the header's levels (a VCD file the machine writes), read
    a line at a time, so also while it grows.  Its times are ns of the
    Flipper's own time, from where the record began."""

    def __init__(self):
        self.pins = {}          # the file's sign for a pin: the pin (PA7)
        self.levels = {}        # a pin's level, as far as was read
        self.ns = 0             # the time of what was read last
        self.changes = 0        # changes of a level since it began
        self.begins = False     # reading the levels it began with

    def feed(self, line):
        """A line of the file.  A change of a level: (ns, pin, level, the
        level before); any other line: None.  ValueError: no line of such a
        file."""
        line = line.strip()
        if line.startswith("$var "):
            words = line.split()        # $var wire 1 a PA7 $end
            if len(words) != 6:
                raise ValueError(line)
            self.pins[words[3]] = words[4]
        elif line.startswith("$dumpvars"):
            self.begins = True
        elif line == "$end":
            self.begins = False
        elif line.startswith("#"):
            ns = int(line[1:])
            if ns < self.ns:
                raise ValueError(f"{line}: back in time, from {self.ns}")
            self.ns = ns
        elif line[:1] in LEVELS and line[1:] in self.pins:
            pin, level = self.pins[line[1:]], LEVELS[line[0]]
            before, self.levels[pin] = self.levels.get(pin), level
            if not self.begins:
                self.changes += 1
                return self.ns, pin, level, before
        elif line and not line.startswith("$"):
            raise ValueError(line)
        return None


def read_record(path):
    """A whole record: (the Record as it ended, its changes)"""
    record, changes = Record(), []
    with open(path, encoding="ascii") as f:
        for line in f:
            change = record.feed(line)
            if change:
                changes.append(change)
    return record, changes


def parse_record(text):
    """What the machine says of the record (its `header-record`, as read):
    (changes so far, ns of the Flipper's time, the file); None: none runs"""
    if not isinstance(text, str) or not text:
        return None
    changes, ns, path = text.split(" ", 2)
    return int(changes), int(ns), path


def span(ns):
    """A length of time in words: 850 ns, 33.0 µs, 1.20 ms, 2.500 s"""
    if ns < 1000:
        return f"{ns} ns"
    if ns < 1000000:
        return f"{ns / 1e3:.1f} µs"
    if ns < 1000000000:
        return f"{ns / 1e6:.2f} ms"
    return f"{ns / 1e9:.3f} s"


def summary(pins):
    """The header in one line: the pins that something is on"""
    parts = [f"{p['pin']} is an output, {p['level']}" for p in pins
             if p["mode"] == "output" and p["holdable"]]
    parts += [f"{p['pin']} is held {p['held']}" for p in pins if p["held"]]
    return "; ".join(parts) or "none of its pins is an output, none is held"
