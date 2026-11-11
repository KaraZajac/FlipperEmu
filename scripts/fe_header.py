"""The GPIO header, as the machine's `header` property has it: read by the
launcher's `gpio` and by the window.

Eighteen pins along the top of a Flipper.  Five are power (1 is +5V, 9 is 3V3;
8, 11 and 18 are GND), thirteen are pins of the MCU.  The property has a word
for each of those, "NUMBER:PIN:MODE:PULL:LEVEL:HELD":

    2:PA7:out:none:1:free 3:PA6:in:up:0:low ... 13:PB6:af7:up:?:- ...

and takes "PA6=1", "PA6=0" and "PA6=z" to hold a pin from outside, as a wire
to 3V3 or to GND would, and to let go of it.

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


def summary(pins):
    """The header in one line: the pins that something is on"""
    parts = [f"{p['pin']} is an output, {p['level']}" for p in pins
             if p["mode"] == "output" and p["holdable"]]
    parts += [f"{p['pin']} is held {p['held']}" for p in pins if p["held"]]
    return "; ".join(parts) or "none of its pins is an output, none is held"
