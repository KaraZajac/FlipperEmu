#!/usr/bin/env python3
# Copyright (c) 2026, Kara Zajac
# SPDX-License-Identifier: BSD-3-Clause
"""Sub-GHz signals for FlipperEmu's CC1101 (/machine/subghz).

A signal is +high/-low durations in µs (the .sub RAW format) at a frequency.
The emulated CC1101 hears it while it is in RX, tuned within 500 kHz.

    fe_subghz.py capture.sub              -> "@433920000 2733 -2724 ..."
    fe_subghz.py princeton 0x95D5D4       -> a Princeton (PT2262) key
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
    raise ValueError(f"{path}: protocol {proto!r} has no encoder here; "
                     "record it as RAW (Sub-GHz -> Read RAW) instead")


def main():
    if len(sys.argv) == 2 and sys.argv[1].endswith(".sub"):
        freq, durs = parse_sub(sys.argv[1])
    elif len(sys.argv) >= 3 and sys.argv[1].lower() == "princeton":
        freq, durs = 433920000, princeton(int(sys.argv[2], 0))
    else:
        print(__doc__)
        return 2
    print(f"@{freq} " + " ".join(map(str, durs)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
