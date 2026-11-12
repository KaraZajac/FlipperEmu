"""The GPIO header, as the machine's `header` property has it: read by the
launcher's `gpio` and by the window.

Eighteen pins along the top of a Flipper.  Five are power (1 is +5V, 9 is 3V3;
8, 11 and 18 are GND), thirteen are pins of the MCU.  The property has a word
for each of those, "NUMBER:PIN:MODE:PULL:LEVEL:HELD":

    2:PA7:out:none:1:free 3:PA6:in:up:0:low ... 13:PB6:af7:up:?:- ...

(an input that the firmware has an interrupt for is "in+r", "in+f" or
"in+rf": when it rises, when it falls, both) and takes "PA6=1", "PA6=0" and
"PA6=z" to hold a pin from outside, as a wire to 3V3 or to GND would, and to
let go of it.

The machine's `header-record` is a file that gets every change of a pin's
level with its time, a VCD file as logic analyzers' programs read (Record).

Four of the pins are two serial ports.  The machine's `header-serial` has them
as the firmware has set them up, "NAME:TX:RX:BAUD:FRAME:SENDS:HEARS":

    USART1:13:14:230215:8N1:on:edge LPUART1:15:16:0:8N1:off:off

Two more are an I2C bus, the firmware's external one.  The machine's
`header-i2c` has what is on it and what the firmware called there
(parse_i2c), and its `header-i2c-record` is a file that gets every transfer:
a line of text for each part of one, with the address and the bytes
(i2c_part).

Copyright (c) 2026, Kara Zajac
SPDX-License-Identifier: BSD-3-Clause
"""
import json
import re

# What is printed beside the pins on a Flipper's case, by the pin's number
LABELS = {2: "A7", 3: "A6", 4: "A4", 5: "B3", 6: "B2", 7: "C3", 10: "SWC",
          12: "SIO", 13: "TX", 14: "RX", 15: "C1", 16: "C0", 17: "1W"}
POWER = {1: "+5V", 8: "GND", 9: "3V3", 11: "GND", 18: "GND"}
# The pins that are not held from here, and why
NOT_HELD = {13: "USART1's TX (`uart --port usart` has what it sends)",
            14: "USART1's RX (`uart send --port usart` puts bytes on it)",
            17: "the iButton pin (`ibutton` holds a key to it)"}
# The alternate function that puts a serial port on its pins
PORT_FUNCTION = {"USART1": 7, "LPUART1": 8}
# The baud rates with a name: a divider seldom gives one to the last digit
RATES = (110, 300, 600, 1200, 2400, 4800, 9600, 14400, 19200, 28800, 31250,
         38400, 57600, 74880, 76800, 115200, 230400, 250000, 460800, 500000,
         921600, 1000000, 1500000, 2000000, 3000000)


def parse(text):
    """The header's signal pins in their order on it, each a dict: its
    "number" there, the MCU's "pin" (PA7), the "label" on the case, what the
    firmware has it as ("mode": input, output, alternate or analog;
    "function": which alternate one; "open_drain"; "pull": up, down or None;
    "interrupt": the edges of an input that it has an interrupt for, rising,
    falling, both or None), its "level" (high, low, floating: nothing holds
    it, or unknown: a peripheral's signal, which the emulator does not keep
    as a level), and what the outside holds on it ("held": high, low or None;
    "holdable": may it)."""
    pins = []
    for word in text.split():
        number, pin, mode, pull, level, held = word.split(":")
        mode, _, edges = mode.partition("+")
        function = re.fullmatch(r"af(\d+)(od)?", mode)
        pins.append({
            "number": int(number), "pin": pin,
            "label": LABELS.get(int(number), pin[1:]),
            "mode": "alternate" if function else
                    {"in": "input", "out": "output", "od": "output"}.get(mode, mode),
            "function": int(function[1]) if function else None,
            "open_drain": mode.endswith("od"),
            "pull": None if pull == "none" else pull,
            "interrupt": {"r": "rising", "f": "falling",
                          "rf": "both"}.get(edges),
            "level": {"0": "low", "1": "high", "z": "floating"}.get(level, "unknown"),
            "held": held if held in ("high", "low") else None,
            "holdable": held != "-"})
    return pins


def mode_text(p):
    """What the firmware has a pin as, in words"""
    return ((f"alternate function {p['function']}" if p["mode"] == "alternate"
             else p["mode"])
            + (", open drain" if p["open_drain"] else "")
            + (f", pull-{p['pull']}" if p["pull"] else "")
            + {"rising": ", interrupt when it rises",
               "falling": ", interrupt when it falls",
               "both": ", interrupt on both edges"}.get(p.get("interrupt"), ""))


def parse_serial(text):
    """The header's serial ports as the firmware has them, each a dict: its
    "name" (USART1), its pins by their numbers on the header ("tx", "rx"),
    is it "on", its "baud" rate (the usual one that its divider gives within
    half a percent, or else what the divider gives, which is "divider"), its
    "frame" (8N1), does it send ("sends"), and who takes what comes in
    ("hears": irq, dma; off: nobody, the receiver is off and a byte is
    dropped; edge: the same, but an interrupt waits for the RX pin to fall,
    and a byte's start bit is that: so the firmware waits for an expansion
    module; unread: nobody, the receiver is on and a byte waits)."""
    ports = []
    for word in text.split():
        name, tx, rx, baud, frame, sends, hears = word.split(":")
        if hears not in ("off", "edge", "irq", "dma", "unread"):
            raise ValueError(word)
        divider = int(baud)
        near = min(RATES, key=lambda r: abs(r - divider))
        ports.append({
            "name": name, "tx": int(tx), "rx": int(rx), "on": divider > 0,
            "baud": near if abs(near - divider) <= near / 200 else divider,
            "divider": divider, "frame": frame, "sends": sends == "on",
            "hears": hears})
    return ports


def port_name(port, first="p"):
    return f"{first}ins {port['tx']} and {port['rx']} ({port['name']})"


def port_calls(port):
    """What a port that waits for a call is said to do"""
    return (f"the firmware waits for a call on pin {port['rx']} (an "
            "interrupt on its fall, as for an expansion module)")


def port_text(port):
    """How the firmware has a serial port, in words"""
    if not port["on"]:
        return ("off (nothing has opened it)"
                + ("; " + port_calls(port) if port["hears"] == "edge" else ""))
    return (f"{port['baud']} baud, {port['frame']}; "
            + ("" if port["sends"] else "its transmitter is off; ")
            + {"off": "what comes in is dropped (its receiver is off)",
               "edge": "its receiver is off, and " + port_calls(port),
               "irq": "what comes in is read (by interrupt)",
               "dma": "what comes in is read (by DMA)",
               "unread": "what comes in waits to be read (its receiver is "
                         "on, with no interrupt and no DMA for it)"}
               [port["hears"]])


def port_pin(p, ports):
    """Is a pin of the header one of a serial port's just now?  (the port,
    "TX" or "RX"), or None.  USART1's pins are its own at all times; LPUART1's
    are a port's while the firmware has them in its alternate function."""
    for port in ports or ():
        side = {port["tx"]: "TX", port["rx"]: "RX"}.get(p["number"])
        if side and (not p["holdable"]
                     or p["function"] == PORT_FUNCTION.get(port["name"])):
            return port, side
    return None


# The alternate function that puts the header's I2C bus on its two pins
I2C_FUNCTION = 4


def parse_i2c(text):
    """The header's I2C bus (the machine's `header-i2c`), a dict: its "name"
    (I2C3), its pins by their numbers on the header ("sda", "scl"), how many
    addresses the firmware called since the emulator started ("calls") and
    how many of those nobody took ("unanswered"), the "last" one called
    (None, or its "address", was it to "read", was it "taken"), and the
    "devices" on the bus, each its "type" and "address", the lowest first."""
    words = text.split()
    name, sda, scl, calls, unanswered, last = words[0].split(":")
    if last != "-" and (len(last) != 4 or last[2] not in "rw"
                        or last[3] not in "+-"):
        raise ValueError(words[0])
    devices = []
    for word in words[1:]:
        kind, _, address = word.rpartition("@")
        devices.append({"type": kind, "address": int(address, 16)})
    return {"name": name, "sda": int(sda), "scl": int(scl),
            "calls": int(calls), "unanswered": int(unanswered),
            "last": None if last == "-" else {
                "address": int(last[:2], 16), "read": last[2] == "r",
                "taken": last[3] == "+"},
            "devices": sorted(devices, key=lambda d: d["address"])}


def i2c_name(bus, first="p"):
    return f"{first}ins {bus['sda']} and {bus['scl']} ({bus['name']})"


def i2c_devices(bus):
    """What is on the bus, in words"""
    return ", ".join(f"{d['type']} at 0x{d['address']:02x}"
                     for d in bus["devices"]) or "no device"


def i2c_did(bus):
    """What the firmware did on the header's I2C bus, in words: how many
    addresses it called ("calls"), how many of those nobody took
    ("unanswered"), and the "last" one"""
    last, calls = bus["last"], bus["calls"]
    if not calls or not last:
        return "the firmware has called no address yet"
    taken = calls - bus["unanswered"]
    return (f"the firmware called {calls} "
            f"address{'' if calls == 1 else 'es'}, "
            + ("none" if not taken else "it" if calls == 1 else
               "all" if taken == calls else str(taken))
            + f" answered; the last: 0x{last['address']:02x} to "
            f"{'read' if last['read'] else 'write'}, "
            + ("answered" if last["taken"] else "no answer"))


def i2c_text(bus):
    """What is on the header's I2C bus and what the firmware did there, in
    words"""
    return f"{i2c_devices(bus)}; {i2c_did(bus)}"


def i2c_degrees(name):
    """Is a value of that name, of a device on the bus, a temperature?
    QEMU's sensors have theirs in thousandths of a degree; the launcher reads
    and writes them in °C."""
    return name.startswith("temperature")


def i2c_said(name, value, unit=True):
    """A value of a device on the bus in words, from what `i2c --json` has
    for it (a temperature: in °C).  unit: with its unit, and a text in
    quotes; without: bare, for a script."""
    if isinstance(value, bool):
        return "on" if value else "off"
    if i2c_degrees(name) and isinstance(value, (int, float)):
        return f"{value:g}" + (" °C" if unit else "")
    if isinstance(value, str):
        return json.dumps(value) if unit else value
    return "?" if value is None else str(value)


def i2c_value(device, name):
    """A value of a device, as `i2c --json` has the device, with its name:
    "temperature 23.5 °C".  An EEPROM's drive is said as where its contents
    are kept."""
    value = device["values"].get(name)
    if name != "drive" or device["type"] != "at24c-eeprom":
        return f"{name} {i2c_said(name, value)}"
    if device.get("file"):
        return f"kept in {device['file']}"
    return "kept nowhere" if not value else f"kept in drive {json.dumps(value)}"


def i2c_pin(p, bus):
    """Is a pin of the header one of the I2C bus's just now?  "SDA", "SCL"
    or None.  They are the bus's while the firmware has them in its alternate
    function: while it talks there, and no longer."""
    if bus and p["function"] == I2C_FUNCTION and p["mode"] == "alternate":
        return {bus["sda"]: "SDA", bus["scl"]: "SCL"}.get(p["number"])
    return None


def parse_i2c_record(text):
    """What the machine says of the record of the I2C bus (its
    `header-i2c-record`, as read), a dict: the addresses called since it
    began ("calls") and how many of them nobody took ("unanswered"), the
    bytes "written" and "read", "ns" of the Flipper's time, the "path" of
    the file; None: none runs"""
    if not isinstance(text, str) or not text:
        return None
    calls, unanswered, written, read, ns, path = text.split(" ", 5)
    return {"calls": int(calls), "unanswered": int(unanswered),
            "written": int(written), "read": int(read), "ns": int(ns),
            "path": path}


def i2c_recorded(record, brief=False):
    """What a record of the I2C bus has so far, in words (brief: no more
    than the addresses called)"""
    calls, taken = record["calls"], record["calls"] - record["unanswered"]
    if not calls:
        return "no address called"
    if brief:
        return f"{calls} address{'' if calls == 1 else 'es'} called"
    return (f"{calls} address{'' if calls == 1 else 'es'} called, "
            + ("none" if not taken else "it" if calls == 1 else
               "all" if taken == calls else str(taken)) + " answered; "
            f"{record['written']} byte{'' if record['written'] == 1 else 's'}"
            f" written, {record['read']} read")


def i2c_part(line):
    """A line of a record of the I2C bus (a text file the machine writes): a
    part of a transfer, which is what lies between a START and the next
    START or STOP.  A dict: "ns" of the Flipper's time at its START, from
    where the record began; the "address" called (its seven bits), to "read"
    or not, was it "taken"; the bytes written or read ("data"; "refused":
    the device did not take the last one written); "stop": it ended with a
    STOP (not: the next part's START follows with none); "whole": the record
    did not end inside it.  None: a line that is no part (a comment, an
    empty one).  ValueError: no line of such a file."""
    words = line.split()
    if not words or words[0].startswith("#"):
        return None
    try:
        s, ns = words[0].split(".")
        if len(ns) != 9 or len(words[1]) != 4 or words[1][:2] != "0x":
            raise ValueError
        part = {"ns": int(s) * 1000000000 + int(ns),
                "address": int(words[1], 16), "read": "wr".index(words[2]) == 1,
                "taken": True, "data": b"", "refused": False, "stop": True,
                "whole": True}
        rest = words[3:]
        if rest[:2] == ["no", "answer"]:
            part["taken"], rest = False, rest[2:]
        if rest[-1:] == ["+"]:
            part["stop"], rest = False, rest[:-1]
        elif rest[-1:] == ["..."]:
            part["whole"], rest = False, rest[:-1]
        if rest and rest[-1].endswith("!"):
            part["refused"], rest[-1] = True, rest[-1][:-1]
        if any(len(w) != 2 for w in rest) or (rest and not part["taken"]) \
                or (part["refused"] and part["read"]) or len(words[2]) != 1:
            raise ValueError
        part["data"] = bytes(int(w, 16) for w in rest)
    except (ValueError, IndexError):
        raise ValueError(line.strip()) from None
    return part


def read_i2c_record(path):
    """A whole record of the I2C bus: (what was on the bus as it began, as
    the file says it: each device's "type" and "address"; its parts)"""
    devices, parts = [], []
    with open(path, encoding="ascii") as f:
        for line in f:
            if line.startswith("# on the bus:"):
                devices = [{"type": kind, "address": int(at, 16)}
                           for kind, at in re.findall(r"(\S+) at (0x[0-9a-f]{2})",
                                                      line)]
            part = i2c_part(line)
            if part:
                parts.append(part)
    return devices, parts


def i2c_part_text(part, most=16):
    """A part of a transfer in words: what was written or read (of many
    bytes the first `most`, and how many there were)"""
    to = "read" if part["read"] else "write"
    data = part["data"]
    if not part["taken"]:
        said = f"to {to}: no answer"
    elif not data:
        said = f"to {to}: answered, and no byte"
    else:
        shown = data if len(data) <= most else data[:most]
        said = (("read " if part["read"] else "wrote ") + shown.hex(" ")
                + (f" ... ({len(data)} bytes)" if len(shown) < len(data)
                   else ""))
        if part["refused"]:
            said += " (the last one refused)"
    if not part["whole"]:
        said += " (the record ended there)"
    elif not part["stop"]:
        said += " (no STOP: the next one follows at once)"
    return said


def i2c_shifted(address, devices):
    """A call that nobody took, while a device is at twice that address: the
    firmware's calls (furi_hal_i2c_*, the JS module's) take the address
    shifted left by one bit, the bit above the one that says read or write,
    and an app that gives them 0x48 calls 0x24.  A word on that, or None."""
    for d in devices:
        if d["address"] >> 1 == address and d["address"] != address:
            at = d["address"]
            return (f"nobody is at 0x{address:02x}. If the {d['type']} at "
                    f"0x{at:02x} was meant: the firmware takes an address "
                    f"shifted left by one bit, 0x{at:02x} << 1 "
                    f"(0x{at << 1:02x})")
    return None


def i2c_hint(bus):
    """That word for the last call on the bus, as `header-i2c` has it"""
    last = bus["last"]
    if not last or last["taken"]:
        return None
    return i2c_shifted(last["address"], bus["devices"])


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
