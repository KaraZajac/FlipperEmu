"""The GPIO header, as the machine's `header` property has it: read by the
launcher's `gpio` and by the window.

Eighteen pins along the top of a Flipper.  Five are power (1 is +5V, 9 is 3V3;
8, 11 and 18 are GND), thirteen are pins of the MCU.  The property has a word
for each of those, "NUMBER:PIN:MODE:PULL:LEVEL:HELD":

    2:PA7:out:none:1:free 3:PA6:in:up:0:low ... 13:PB6:af7:up:?:- ...

(LEVEL is 0, 1, z: nothing holds the pin, p: a timer's PWM is on it, or ?:
another peripheral's signal, which the emulator does not keep as a level)

(an input that the firmware has an interrupt for is "in+r", "in+f" or
"in+rf": when it rises, when it falls, both) and takes "PA6=1", "PA6=0" and
"PA6=z" to hold a pin from outside, as a wire to 3V3 or to GND would, and to
let go of it.  "PA6=1250mV" holds a voltage between those to it, which is
"1250mV" as what is held: six of the pins are inputs of the MCU's ADC, and
the machine's `header-adc` has what the firmware converted there (parse_adc).

Two of the pins are the PWM outputs that the firmware's furi_hal_pwm has,
each a timer's.  The machine's `header-pwm` has them,
"NAME:PIN:STATE:CLOCK:DIVIDER:PERIOD:HIGH" (parse_pwm):

    TIM1:2:on:64000000:1:64000:32000 LPTIM2:4:off:0:0:0:0

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

Three more are an SPI bus, the firmware's external one, and each of seven
others may be the chip select of a device there.  The machine's `header-spi`
has what is on it and what the firmware clocked there (parse_spi),
"NAME:MOSI:MISO:SCK:HEARD:UNHEARD:LAST", then "TYPE@PIN:selected|idle" for
each device:

    SPI1:2:3:5:50:0:2000000/0/msb/8 w25q32@4:idle

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
# What the MCU runs on, in mV: a pin held high is at that
VDD_MV = 3300
# A held voltage is a level too, to a pin's digital side: high from here up
# (the machine's choice: half of VDD)
HIGH_MV = VDD_MV // 2
# The inputs of the ADC that are on the header: the pin's number there, by
# the ADC's number for the input
ADC_PINS = {1: 16, 2: 15, 4: 7, 9: 4, 11: 3, 12: 2}
# ... and those inside the chip
ADC_INSIDE = {0: "the chip's own reference voltage",
              17: "the chip's temperature sensor",
              18: "a third of the battery pin's voltage"}
# The alternate function that puts a serial port on its pins
PORT_FUNCTION = {"USART1": 7, "LPUART1": 8}
# The baud rates with a name: a divider seldom gives one to the last digit
RATES = (110, 300, 600, 1200, 2400, 4800, 9600, 14400, 19200, 28800, 31250,
         38400, 57600, 74880, 76800, 115200, 230400, 250000, 460800, 500000,
         921600, 1000000, 1500000, 2000000, 3000000)


def parse_pwm(text):
    """The header's PWM outputs (the machine's `header-pwm`), each a dict:
    the "timer" whose output it is (TIM1), its pin's "number" on the header,
    is it "on" (the timer makes a PWM, and the pin is in the timer's
    alternate function), and then, 0 while it is off: the timer's "clock" in
    Hz, what its prescaler divides that by ("divider"), and a "period" and
    its "high" part in ticks of the divided clock.  From those, None while it
    is off: the frequency in Hz ("hz") and the percent of the time for which
    the pin is high ("duty")."""
    outputs = []
    for word in text.split():
        timer, number, state, clock, divider, period, high = word.split(":")
        clock, divider, period, high = (int(n) for n in (clock, divider,
                                                         period, high))
        on = state == "on"
        if state not in ("on", "off") or (on and not (
                clock > 0 and divider > 0 and period >= high >= 0 < period)):
            raise ValueError(word)
        outputs.append({
            "timer": timer, "number": int(number), "on": on, "clock": clock,
            "divider": divider, "period": period, "high": high,
            "hz": clock / (divider * period) if on else None,
            "duty": 100 * high / period if on else None})
    return outputs


def hertz(hz):
    """A frequency in words, to four digits: 440 Hz, 9.998 kHz, 1 kHz"""
    hz = float(f"{hz:.4g}")     # (999.98 Hz is 1 kHz, and not 1000 Hz)
    for unit, size in (("MHz", 1e6), ("kHz", 1e3)):
        if hz >= size:
            return f"{hz / size:.4g} {unit}"
    return f"{hz:.4g} Hz"


def pwm_text(pwm):
    """A PWM on a pin, in words"""
    return (f"PWM from {pwm['timer']}: {hertz(pwm['hz'])}, high "
            f"{pwm['duty']:.4g} % of the time")


def level_text(p):
    """A pin's level, as a sentence about the pin goes on: "is high", or
    "has a PWM from TIM1: 1 kHz, high 50 % of the time" for a PWM"""
    if p["level"] == "pwm":
        return "has a " + pwm_text(p["pwm"])
    return f"is {p['level']}"


def parse(text, pwm=None):
    """The header's signal pins in their order on it, each a dict: its
    "number" there, the MCU's "pin" (PA7), the "label" on the case, what the
    firmware has it as ("mode": input, output, alternate or analog;
    "function": which alternate one; "open_drain"; "pull": up, down or None;
    "interrupt": the edges of an input that it has an interrupt for, rising,
    falling, both or None), its "level" (high, low, floating: nothing holds
    it, pwm: a timer's PWM is on it, or unknown: another peripheral's signal,
    which the emulator does not keep as a level), and what the outside holds
    on it ("held": high, low or None; "held_mv": at how many mV, 3300 for
    high and 0 for low, and a voltage between them is high or low as a level
    too; "holdable": may it), which input of the ADC it is ("adc": its number
    there, or None), and the PWM that a timer has on it ("pwm": None, or what
    parse_pwm has for it; one that is high all of the time or none of it is
    a level, high or low).

    pwm: the machine's `header-pwm`, as text (not given: no pin's PWM is
    known, and such a pin's level is unknown)."""
    pins = []
    inputs = {number: channel for channel, number in ADC_PINS.items()}
    outputs = {o["number"]: {k: v for k, v in o.items()
                             if k not in ("number", "on")}
               for o in parse_pwm(pwm or "") if o["on"]}
    for word in text.split():
        number, pin, mode, pull, level, held = word.split(":")
        mode, _, edges = mode.partition("+")
        function = re.fullmatch(r"af(\d+)(od)?", mode)
        volts = re.fullmatch(r"(\d+)mV", held)
        mv = int(volts[1]) if volts else {"high": VDD_MV, "low": 0}.get(held)
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
            "level": {"0": "low", "1": "high", "z": "floating",
                      "p": "pwm" if int(number) in outputs else "unknown"
                      }.get(level, "unknown"),
            "held": None if mv is None else "high" if mv >= HIGH_MV else "low",
            "held_mv": mv,
            "holdable": held != "-",
            "adc": inputs.get(int(number)),
            "pwm": outputs.get(int(number))})
    return pins


def volts(mv):
    """A voltage in words: 1.25 V, 3.3 V, 0 V"""
    return f"{mv / 1000:g} V"


def parse_volts(word):
    """A voltage as a user writes it (1.25V, 1250mV, 0.4v): in mV, to the
    nearest one.  None: that is no voltage."""
    m = re.fullmatch(r"(\d+(?:\.\d*)?|\.\d+)(m?)v", word.strip().lower())
    if not m:
        return None
    return int(float(m[1]) * (1 if m[2] else 1000) + 0.5)


def held_word(p):
    """What the outside holds a pin at: "high", "low", "at 1.25 V"; None:
    nothing"""
    if not p["held"]:
        return None
    mv = p.get("held_mv")
    return p["held"] if mv in (None, 0, VDD_MV) else f"at {volts(mv)}"


def held_text(p):
    """... as a pin is said to be held: "held high from outside", and with a
    voltage what that is as a level"""
    word = held_word(p)
    return f"held {word} from outside" + (
        f" ({p['held']} as a level)" if word != p["held"] else "")


def parse_adc(text):
    """The ADC as the firmware has it (the machine's `header-adc`), a dict:
    its "name" (ADC1), is it "on" (the firmware has it on while something
    uses it), its "reference" voltage now in mV, how many "conversions" it
    made since the emulator started, and the "last" one: None, or the
    "channel" (the ADC's number for the input), the header's "pin" that it is
    (its number there; None: an input that is not on the header), the "data"
    that the firmware read, and the "uv" that were on the input, in
    microvolts."""
    name, state, reference, conversions, last = text.split(":", 4)
    if state not in ("on", "off"):
        raise ValueError(text)
    if last != "-":
        channel, data, uv = (int(n) for n in last.split(":"))
        last = {"channel": channel, "pin": ADC_PINS.get(channel),
                "data": data, "uv": uv}
    return {"name": name, "on": state == "on", "reference": int(reference),
            "conversions": int(conversions),
            "last": None if last == "-" else last}


def adc_input(last, pins=()):
    """An input of the ADC in words, from a conversion as parse_adc has it:
    "pin 4 (PA4)", or what it is inside the chip"""
    if last["pin"]:
        names = {p["number"]: p["pin"] for p in pins}
        return f"pin {last['pin']}" + (f" ({names[last['pin']]})"
                                       if last["pin"] in names else "")
    return ADC_INSIDE.get(last["channel"],
                          f"its input {last['channel']}, which is not on "
                          "the header")


def adc_text(adc, pins=()):
    """The ADC and what the firmware converted with it, in words"""
    last, n = adc["last"], adc["conversions"]
    return ((f"on, its reference at {volts(adc['reference'])}" if adc["on"]
             else "off (nothing uses it now)") + "; "
            + ("the firmware has converted nothing yet" if not n or not last
               else f"{n} conversion{'' if n == 1 else 's'}, the last: "
                    f"{adc_input(last, pins)}, {volts(last['uv'] / 1000)}, "
                    f"read as {last['data']}"))


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


# The alternate function that puts the header's SPI bus on its three pins
SPI_FUNCTION = 5

# The pins that may be the chip select of a device there, by their numbers on
# the header, with the MCU's names: 4 is the one of the firmware's own handle
# for the bus (2, 3 and 5 are the bus)
SPI_SELECTS = {4: "PA4", 6: "PB2", 7: "PC3", 10: "PA14", 12: "PA13",
               15: "PC1", 16: "PC0"}


def spi_select(word):
    """The pin a user means as a device's chip select, by the MCU's name
    (PA4), by what the case says (A4), or by its number on the header (4):
    that number, or None for none that may be one"""
    want = word.upper()
    for number, pin in SPI_SELECTS.items():
        if want in (pin, LABELS.get(number), str(number)):
            return number
    return None


def parse_spi(text):
    """The header's SPI bus (the machine's `header-spi`), a dict: its "name"
    (SPI1), its pins by their numbers on the header ("mosi", "miso", "sck"),
    how many bytes the firmware clocked there since the emulator started
    with a device selected ("heard") and with none ("unheard"), how it
    clocked the "last" frame (None, or its clock in "hz", the SPI "mode" 0
    to 3, "lsb_first", and the "bits" of a frame), and the "devices" on the
    bus, each its "type", the pin of its chip select by its number on the
    header ("select") and by the MCU's name ("pin"), and is it "selected"
    just now; the lowest pin first."""
    words = text.split()
    name, mosi, miso, sck, heard, unheard, last = words[0].split(":")
    how = None
    if last != "-":
        hz, mode, order, bits = last.split("/")
        if order not in ("msb", "lsb"):
            raise ValueError(words[0])
        how = {"hz": int(hz), "mode": int(mode), "lsb_first": order == "lsb",
               "bits": int(bits)}
    devices = []
    for word in words[1:]:
        kind, _, rest = word.rpartition("@")
        number, _, state = rest.partition(":")
        if state not in ("selected", "idle"):
            raise ValueError(word)
        devices.append({"type": kind, "select": int(number),
                        "pin": SPI_SELECTS.get(int(number), "?"),
                        "selected": state == "selected"})
    return {"name": name, "mosi": int(mosi), "miso": int(miso),
            "sck": int(sck), "heard": int(heard), "unheard": int(unheard),
            "last": how,
            "devices": sorted(devices, key=lambda d: d["select"])}


def spi_name(bus, first="p"):
    return (f"{first}ins {bus['mosi']}, {bus['miso']} and {bus['sck']} "
            f"({bus['name']})")


def spi_devices(bus):
    """What is on the bus, in words"""
    return ", ".join(f"{d['type']} with its chip select on pin {d['select']}"
                     for d in bus["devices"]) or "no device"


def spi_frame(last):
    """How the firmware clocked a frame, in words"""
    return (f"{hertz(last['hz'])}, mode {last['mode']}, "
            f"{'LSB' if last['lsb_first'] else 'MSB'} first, "
            f"{last['bits']} bits")


def spi_did(bus):
    """What the firmware did on the header's SPI bus, in words: how many
    bytes it clocked there, how many of them while a device was selected
    (the others nobody heard), and how it clocked the last frame"""
    last, heard = bus["last"], bus["heard"]
    total = heard + bus["unheard"]
    if not total or not last:
        return "the firmware has clocked nothing there yet"
    if total == 1:
        to = "with a device selected" if heard else "with no device selected"
    else:
        to = ("all" if heard == total else "none" if not heard
              else f"{heard} of them") + " with a device selected"
    return (f"the firmware clocked {total} byte{'' if total == 1 else 's'} "
            f"there, {to}; the last at {spi_frame(last)}")


def spi_text(bus):
    """What is on the header's SPI bus and what the firmware did there, in
    words"""
    return f"{spi_devices(bus)}; {spi_did(bus)}"


def spi_pin(p, bus):
    """Is a pin of the header one of the SPI bus's just now?  "MOSI", "MISO",
    "SCK" or None.  They are the bus's while the firmware has them in its
    alternate function: from the moment it takes the bus until it lets go."""
    if bus and p["function"] == SPI_FUNCTION and p["mode"] == "alternate":
        return {bus["mosi"]: "MOSI", bus["miso"]: "MISO",
                bus["sck"]: "SCK"}.get(p["number"])
    return None


def spi_chip(p, bus):
    """The device on the SPI bus whose chip select a pin of the header is
    (as parse_spi has it), or None"""
    for d in (bus or {}).get("devices", ()):
        if d["select"] == p["number"]:
            return d
    return None


def spi_chip_text(d):
    """A pin that is a device's chip select, in words"""
    return f"{d['type']}'s chip select" + (
        ", which selects it now" if d["selected"] else "")


def overruled(p):
    """Is something held to a pin that the firmware drives both ways, itself
    or with a timer's PWM?  On hardware the two would fight; here the
    firmware's level is the pin's."""
    return bool(p["held"]) and not p["open_drain"] and (
        p["mode"] == "output" or bool(p.get("pwm")))


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
    parts += [f"{p['pin']} has a PWM ({hertz(p['pwm']['hz'])}, high "
              f"{p['pwm']['duty']:.4g} %)" for p in pins if p.get("pwm")]
    parts += [f"{p['pin']} is held {held_word(p)}" for p in pins if p["held"]]
    return "; ".join(parts) or "none of its pins is an output, none is held"
