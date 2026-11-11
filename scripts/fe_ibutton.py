#!/usr/bin/env python3
# Copyright (c) 2026, Kara Zajac
# SPDX-License-Identifier: BSD-3-Clause
"""iButton keys for FlipperEmu's iButton contact (/machine/ibutton).

Turns a Flipper .ibtn file, or a new key, into the description the emulated
contact takes (see qemu/hw/arm/flipper/flipper_ibutton.c):

    fe_ibutton.py key.ibtn                      -> the description, on stdout
    fe_ibutton.py ds1990 01 23 45 67 89 AB CD   -> a DS1990A (CRC added)
    fe_ibutton.py ds1992 08 11 22 33 44 55 66   -> a DS1992, memory zeroed
    fe_ibutton.py cyfral A1 B2                  -> a Cyfral key (2 bytes)
    fe_ibutton.py metakom C3 55 33 0F           -> a Metakom key (4 bytes, each
                                                   with even parity)
    fe_ibutton.py rw1990 [ROM]                  -> a blank the Flipper can write:
                                                   rw1990 (.1), rw1990.2, tm2004,
                                                   tm01x (ROM: what it reads now)
    fe_ibutton.py ibutton-tx/tx-00001.txt       -> the Cyfral/Metakom key an
                                                   emulation capture holds

Dallas keys are 1-Wire slaves: their ROM (family code, serial, CRC) and,
for DS1992/DS1996/DS1971, their memory.  Cyfral and Metakom keys are a
waveform, from ports of the firmware's own encoders (lib/ibutton/
protocols/misc): what the Flipper's comparator sees while it reads one.
"""
import pathlib
import sys

# family code: (name, READ MEMORY address bytes, memory size)
FAMILIES = {
    0x01: ("DS1990", 0, 0),
    0x08: ("DS1992", 2, 128),
    0x0C: ("DS1996", 2, 8192),
    0x14: ("DS1971", 1, 32),
}
MEMORY_KEYS = {"DS1992": "Sram Data", "DS1996": "Sram Data", "DS1971": "Eeprom Data"}


def maxim_crc8(data, crc=0):
    """The 1-Wire CRC (maxim_crc8: x^8 + x^5 + x^4 + 1, reflected)."""
    for byte in data:
        for _ in range(8):
            mix = (crc ^ byte) & 1
            crc >>= 1
            if mix:
                crc ^= 0x8C
            byte >>= 1
    return crc


def hexbytes(text):
    """Bytes from hex digits: apart (`01 A1 B2`, `01:A1:B2`), or run together
    as the Flipper prints a key (`01A1B2`)."""
    out = bytearray()
    for word in text.replace(":", " ").split():
        if word[:2] in ("0x", "0X") and len(word) > 2:
            word = word[2:]
        if not all(c in "0123456789abcdefABCDEF" for c in word):
            raise ValueError(f"{word}: not hex digits")
        if len(word) > 2 and len(word) % 2:
            raise ValueError(f"{word}: {len(word)} hex digits, half a byte too "
                             "many or too few")
        out += bytes.fromhex(word.zfill(2))
    return bytes(out)


def rom_of(data):
    """8 ROM bytes: given, or 7 and the CRC."""
    if len(data) == 7:
        return bytes(data) + bytes([maxim_crc8(data)])
    if len(data) != 8:
        raise ValueError("a 1-Wire ROM is 7 bytes (CRC added) or 8")
    return bytes(data)


BLANKS = {"rw1990": "rw1990.1", "rw1990.1": "rw1990.1", "rw1990.2": "rw1990.2",
          "tm2004": "tm2004", "tm01x": "tm01x"}
BLANK_ROM = bytes.fromhex("01000000000000")     # a fresh blank reads as this


def onewire(rom, mem=b"", blank=None):
    """The description of a Dallas key: ROM, and memory for a memory key."""
    name, addr_bytes, size = FAMILIES.get(rom[0], ("DSGeneric", 0, 0))
    if blank:
        name, addr_bytes = f"{blank.upper()} blank", 0
    out = [f"# {name}", "type onewire", f"rom {rom.hex(' ')}"]
    if blank:
        out.append(f"blank {blank}")
    if addr_bytes:
        mem = bytes(mem[:size]).ljust(size, b"\0")
        out.append(f"addr {addr_bytes}")
        out += [f"mem {mem[i:i + 32].hex(' ')}" for i in range(0, len(mem), 32)]
    return "\n".join(out) + "\n"


# ------------------------------------------------------ Cyfral, Metakom

PERIOD_US = 125                         # both: one bit, 125 us (8000 cycles)
SHORT, LONG = 41, 82                    # 0.33 and 0.66 of it


def cyfral_wave(data):
    """protocol_cyfral_encoder_yield: the start nibble 0001, then 8 nibbles,
    one per 2 data bits (11: 0111, 10: 1011, 01: 1101, 00: 1110), sent
    LSB first; a 0 is low LONG then high SHORT, a 1 the other way."""
    if len(data) != 2:
        raise ValueError("a Cyfral key is 2 bytes")
    value16 = data[0] | data[1] << 8    # the firmware's uint16, little-endian
    enc = 0
    for i in range(8):
        enc = enc << 4 | {3: 0b0111, 2: 0b1011, 1: 0b1101, 0: 0b1110}[(value16 >> (2 * i)) & 3]
    bits = [0, 0, 0, 1] + [(enc >> i) & 1 for i in range(32)]
    out = []
    for b in bits:
        out += [-(SHORT if b else LONG), +(LONG if b else SHORT)]
    return out


def metakom_wave(data):
    """protocol_metakom_encoder_yield: a sync low of one period, the start
    word 010, then 32 data bits MSB first; a 0 is high SHORT then low LONG,
    a 1 the other way.  Every byte needs even parity."""
    if len(data) != 4:
        raise ValueError("a Metakom key is 4 bytes")
    value32 = int.from_bytes(bytes(data), "little")     # the firmware's uint32
    for k in range(4):
        if bin((value32 >> (8 * k)) & 0xFF).count("1") % 2:
            raise ValueError("every Metakom byte needs even parity")
    bits = [0, 1, 0] + [(value32 >> (31 - i)) & 1 for i in range(32)]
    out = [-PERIOD_US]
    for b in bits:
        out += [+(LONG if b else SHORT), -(SHORT if b else LONG)]
    return out


def wave(name, values):
    """A wave key's description: equal levels merged, the loop's ends too."""
    merged = []
    for v in values:
        if merged and (merged[-1] > 0) == (v > 0):
            merged[-1] += v
        else:
            merged.append(v)
    if len(merged) > 1 and (merged[0] > 0) == (merged[-1] > 0):
        merged[0] += merged.pop()
    return f"# {name}\ntype wave\nwave {','.join(f'{v:+d}' for v in merged)}\n"


# ------------------------------------------- emulation captures (ibutton-tx)

def parse_capture(path):
    """Signed microseconds (+ high) from a FlipperEmu iButton capture."""
    wave, kind = [], None
    for line in pathlib.Path(path).read_text().splitlines():
        key, _, value = line.partition(":")
        if key == "Filetype":
            kind = value.strip()
        elif key == "Wave":
            wave += [int(x) for x in value.split()]
    if kind != "FlipperEmu iButton capture":
        raise ValueError(f"{path}: not a FlipperEmu iButton capture")
    return wave


def _runs(wave):
    """Equal levels merged: [(high, us)]."""
    out = []
    for v in wave:
        if out and out[-1][0] == (v > 0):
            out[-1] = (out[-1][0], out[-1][1] + abs(v))
        else:
            out.append((v > 0, abs(v)))
    return out


def _frames(runs):
    """Every Cyfral and Metakom frame in the levels, as the firmware's
    decoders would read them: [(protocol, data bytes)]."""
    found = []
    # Metakom: a sync low longer than a period, then high/low pairs: the
    # start word 010 and 32 bits, MSB first, every byte with even parity.
    # A bit is its high (long: 1); its low may run into the next sync.
    for i, (high, us) in enumerate(runs):
        if high or us <= 1.2 * PERIOD_US or i + 70 > len(runs):
            continue
        bits = [int(runs[i + 1 + 2 * k][1] > PERIOD_US / 2) for k in range(35)]
        if bits[:3] == [0, 1, 0]:
            data = int("".join(map(str, bits[3:])), 2).to_bytes(4, "little")
            if all(bin(b).count("1") % 2 == 0 for b in data):
                found.append(("Metakom", data))
    # Cyfral: low/high pairs, the start nibble 0001, then 8 nibbles each
    # with one 0, two data bits apiece
    nib = {0b1110: 3, 0b1101: 2, 0b1011: 1, 0b0111: 0}
    for i in range(len(runs) - 71):
        if runs[i][0]:
            continue
        bits = [int(runs[i + 2 * k + 1][1] > runs[i + 2 * k][1]) for k in range(36)]
        if bits[:4] != [0, 0, 0, 1]:
            continue
        nibbles = [int("".join(map(str, bits[4 + 4 * k:8 + 4 * k])), 2) for k in range(8)]
        if all(n in nib for n in nibbles):
            value = 0
            for n in nibbles:
                value = value << 2 | nib[n]
            found.append(("Cyfral", value.to_bytes(2, "little")))
    return found


def decode_capture(wave):
    """(protocol, data bytes) of a captured Cyfral or Metakom emulation: the
    frame found most often (an emulated key repeats it), and at least twice."""
    frames = _frames(_runs(wave)[1:-1])         # the ends may be cut short
    if frames:
        best = max(set(frames), key=frames.count)
        if frames.count(best) >= 2:
            return best
    raise ValueError("no Cyfral or Metakom frame repeats in the capture")


# ------------------------------------------------------------- .ibtn

def parse_ibtn(path):
    fields = {}
    for line in pathlib.Path(path).read_text().splitlines():
        if line.startswith("#") or ":" not in line:
            continue
        key, _, value = line.partition(":")
        fields[key.strip()] = value.strip()
    if fields.get("Filetype") != "Flipper iButton key":
        raise ValueError(f"{path}: not a Flipper iButton key file")
    return fields


def describe(fields):
    """The description for a .ibtn file (format version 1 or 2)."""
    proto = fields.get("Protocol") or fields.get("Key type", "")
    if proto in ("Cyfral", "Metakom"):
        data = hexbytes(fields["Data"])
        return wave(proto, cyfral_wave(data) if proto == "Cyfral" else metakom_wave(data))
    rom = hexbytes(fields.get("Rom Data") or fields.get("Data", ""))
    if len(rom) != 8:
        raise ValueError(f"protocol {proto!r}: no 8-byte ROM")
    mem = hexbytes(fields.get(MEMORY_KEYS.get(proto, "-"), ""))
    return onewire(rom, mem)


def describe_new(kind, data):
    """A key made up from its kind and data."""
    kind = kind.lower()
    if kind in BLANKS:
        return onewire(rom_of(data or BLANK_ROM), blank=BLANKS[kind])
    if kind == "cyfral":
        return wave("Cyfral", cyfral_wave(data))
    if kind == "metakom":
        return wave("Metakom", metakom_wave(data))
    families = {name.lower(): code for code, (name, _, _) in FAMILIES.items()}
    if kind in families:
        if len(data) == 6 or (len(data) == 7 and data[0] != families[kind]):
            data = bytes([families[kind]]) + bytes(data)[:6]   # serial only
        rom = rom_of(data)
        if rom[0] != families[kind]:
            raise ValueError(f"a {kind.upper()} ROM starts with family code "
                             f"{families[kind]:02X}")
        return onewire(rom)
    if kind in ("dallas", "dsgeneric"):
        return onewire(rom_of(data))
    raise ValueError(f"unknown key kind {kind!r}")


def main():
    args = sys.argv[1:]
    try:
        if len(args) == 1 and args[0].endswith(".ibtn"):
            print(describe(parse_ibtn(args[0])), end="")
        elif len(args) == 1 and pathlib.Path(args[0]).is_file():
            proto, data = decode_capture(parse_capture(args[0]))
            print(describe_new(proto, data), end="")
        elif len(args) >= 2 or (args and args[0].lower() in BLANKS):
            print(describe_new(args[0], hexbytes(" ".join(args[1:]))), end="")
        else:
            print(__doc__)
            return 2
    except (ValueError, KeyError) as e:
        sys.exit(f"fe_ibutton: {e}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
