#!/usr/bin/env python3
# Copyright (c) 2026, Kara Zajac
# SPDX-License-Identifier: BSD-3-Clause
"""Sub-GHz signals for FlipperEmu's CC1101 (/machine/subghz).

A signal is +high/-low durations in µs (the .sub RAW format) at a frequency.
The emulated CC1101 hears it while it is in RX, tuned within 500 kHz.

    fe_subghz.py capture.sub              -> "@433920000 2733 -2724 ..."
    fe_subghz.py princeton 0x95D5D4       -> a Princeton (PT2262) key
    fe_subghz.py came 0xABC               -> a CAME key (12 bits; `came 0xABCDEF 24`)
    fe_subghz.py nice-flo 0xABC | gatetx 0xABCDEF | holtek 0xABC | linear 0x2AB

A key of a protocol that has an encoder here is sent as the firmware's own
encoder for it sends it (lib/subghz/protocols), so the firmware's decoder
reads it back; a .sub file with such a key works too.
"""
import pathlib
import sys


def princeton(key, bits=24, te=390, guard=30, repeats=4):
    """PT2262 fixed code, as the firmware's Princeton encoder sends it:
    MSB first, 1 = high 3te / low te, 0 = high te / low 3te, stop bit, guard."""
    out = []
    for _ in range(repeats):
        for i in range(bits - 1, -1, -1):
            out += [3 * te, -te] if key >> i & 1 else [te, -3 * te]
        out += [te, -te * guard]
    return out


def low_first(key, bits, short, long, header, start, repeats):
    """A key as several of the firmware's encoders send it: a pause, a start
    pulse, and for each bit, the highest first, a low and a high: long low
    and short high for a 1, short low and long high for a 0"""
    out = []
    for _ in range(repeats):
        out += [-short * header, start]
        for i in range(bits - 1, -1, -1):
            out += [-long, short] if key >> i & 1 else [-short, long]
    return out


def came(key, bits=12, repeats=4):
    """CAME (12, 18, 24 or 25 bits): TE 320 µs, a pause whose length goes
    by the key's"""
    header = {24: 76, 42: 76, 12: 47, 18: 47, 25: 36}.get(bits, 16)
    return low_first(key, bits, 320, 640, header, 320, repeats)


def nice_flo(key, bits=12, repeats=4):
    """Nice FLO (12 or 24 bits): TE 700 µs"""
    return low_first(key, bits, 700, 1400, 36, 700, repeats)


def gate_tx(key, bits=24, repeats=4):
    """GateTX (24 bits): TE 350 µs, a start pulse of two"""
    return low_first(key, bits, 350, 700, 49, 700, repeats)


def holtek_ht12x(key, bits=12, te=320, repeats=4):
    """Holtek HT12X (12 bits): TE as the key file has it (320 µs)"""
    return low_first(key, bits, te, 2 * te, 36, te, repeats)


def linear(key, bits=10, repeats=4):
    """Linear (10 bits): TE 500 µs, a high and a low for each bit (long high
    for a 1), and after the last bit's high a pause"""
    out = []
    for _ in range(repeats):
        for i in range(bits - 1, 0, -1):
            out += [1500, -500] if key >> i & 1 else [500, -1500]
        out += [1500, -500 * 42] if key & 1 else [500, -500 * 44]
    return out


# a protocol as a .sub file names it -> (the word for it here, its encoder,
# the bits of a key when none are said)
ENCODERS = {"Princeton": ("princeton", princeton, 24),
            "CAME": ("came", came, 12),
            "Nice FLO": ("nice-flo", nice_flo, 12),
            "GateTX": ("gatetx", gate_tx, 24),
            "Holtek_HT12X": ("holtek", holtek_ht12x, 12),
            "Linear": ("linear", linear, 10)}
WORDS = {word: (name, make, bits) for name, (word, make, bits) in ENCODERS.items()}


def encode(word, key, bits=None, te=None, shown=True):
    """A key of a protocol by its word here (`came`): (the protocol's name,
    the bits, the durations).  shown: the key is what the Flipper shows for
    it, which with Linear is not what a key file has (the firmware keeps a
    Linear key with every bit turned over and turns it back to show it: its
    source says so); not shown: as a .sub file's Key."""
    name, make, default = WORDS[word.lower().replace("_", "-")]
    bits = bits or default
    if key >> bits:
        raise ValueError(f"{name}: the key {key:#x} has more than {bits} bits")
    if shown and name == "Linear":
        key = ~key & ((1 << bits) - 1)
    if te and name in ("Princeton", "Holtek_HT12X"):
        return name, bits, make(key, bits, te)
    return name, bits, make(key, bits)


def parse_sub(path):
    """(frequency Hz, durations) from a Flipper .sub file: RAW captures, or
    Princeton key files."""
    fields, raw = {}, []
    for line in pathlib.Path(path).read_text().splitlines():
        key, _, value = line.partition(":")
        key, value = key.strip(), value.strip()
        if key == "RAW_Data":
            raw += [int(x) for x in value.split()]
        elif key:
            fields[key] = value
    freq = int(fields.get("Frequency", "0") or 0)
    proto = fields.get("Protocol", "")
    if proto == "RAW":
        if not raw:
            raise ValueError(f"{path}: RAW file without RAW_Data")
        return freq, raw
    if proto == "Princeton":
        key = int.from_bytes(bytes(int(x, 16) for x in fields["Key"].split()), "big")
        bits = int(fields.get("Bit", 24))
        te = int(fields.get("TE", 390))
        guard = int(fields.get("Guard_time", 30))
        return freq, princeton(key, bits, te, guard)
    if proto in ENCODERS:
        word, make, default = ENCODERS[proto]
        key = int.from_bytes(bytes(int(x, 16) for x in fields["Key"].split()), "big")
        te = int(fields["TE"]) if "TE" in fields else None
        return freq, encode(word, key, int(fields.get("Bit", default)), te,
                            shown=False)[2]
    raise ValueError(f"{path}: protocol {proto!r} has no encoder here ("
                     + ", ".join(ENCODERS) + " have); record it as RAW "
                     "(Sub-GHz -> Read RAW) instead")


def main():
    if len(sys.argv) == 2 and sys.argv[1].endswith(".sub"):
        freq, durs = parse_sub(sys.argv[1])
    elif len(sys.argv) >= 3 and sys.argv[1].lower() in WORDS:
        freq, durs = 433920000, encode(
            sys.argv[1], int(sys.argv[2], 0),
            int(sys.argv[3]) if len(sys.argv) > 3 else None)[2]
    else:
        print(__doc__)
        return 2
    print(f"@{freq} " + " ".join(map(str, durs)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
