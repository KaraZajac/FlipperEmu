#!/usr/bin/env python3
# Copyright (c) 2026, Kara Zajac
# SPDX-License-Identifier: BSD-3-Clause
"""NFC cards for FlipperEmu's ST25R3916 (/machine/nfc).

Turns a Flipper .nfc file into the card description the emulated reader
holds in its field (see qemu/hw/arm/flipper/nfc_card.c):

    fe_nfc.py card.nfc                      -> the description, on stdout
    fe_nfc.py --ntag215 04112233445566      -> a blank NTAG215 with that UID
    fe_nfc.py --ntag215 04112233445566 https://flipperzero.one   (with a URL)

Supported: MIFARE Ultralight / Ultralight EV1 / NTAG21x (Type 2).
"""
import pathlib
import sys

# NFC_LSB_ATQA_FORMAT_VERSION: files newer than this store ATQA MSB first
LSB_ATQA_FORMAT_VERSION = 2

T2_TYPES = ("NTAG/Ultralight", "NTAG203", "NTAG213", "NTAG215", "NTAG216",
            "Mifare Ultralight", "Mifare Ultralight 11", "Mifare Ultralight 21",
            "Mifare Ultralight EV1", "NTAG I2C 1K", "NTAG I2C 2K", "NTAG I2C Plus 1K",
            "NTAG I2C Plus 2K")

# GET_VERSION answers and page counts of blank tags
BLANK = {
    "ntag213": (bytes.fromhex("0004040201000F03"), 45),
    "ntag215": (bytes.fromhex("0004040201001103"), 135),
    "ntag216": (bytes.fromhex("0004040201001303"), 231),
}


def hexbytes(text):
    return bytes(0 if x == "??" else int(x, 16) for x in text.split())


def parse_nfc(path):
    """The key/value lines of a Flipper .nfc file (pages and blocks too)."""
    fields = {}
    for line in pathlib.Path(path).read_text().splitlines():
        if line.startswith("#") or ":" not in line:
            continue
        key, _, value = line.partition(":")
        fields[key.strip()] = value.strip()
    if fields.get("Filetype") != "Flipper NFC device":
        raise ValueError(f"{path}: not a Flipper NFC device file")
    return fields


def describe(fields):
    """The card description for nfc_card.c."""
    version = int(fields.get("Version", "4"))
    dev = fields.get("Device type", "")
    kind = fields.get("NTAG/Ultralight type", dev)
    if dev not in T2_TYPES and kind not in T2_TYPES:
        raise ValueError(f"device type {dev!r} is not supported yet "
                         "(NTAG/Ultralight are)")
    uid = hexbytes(fields["UID"])
    atqa = hexbytes(fields["ATQA"])
    if version > LSB_ATQA_FORMAT_VERSION:
        atqa = atqa[::-1]                       # stored MSB first: swap back
    out = [f"# {kind}", "type t2", f"uid {uid.hex(' ')}", f"atqa {atqa.hex(' ')}",
           f"sak {fields.get('SAK', '00')}"]
    if fields.get("Mifare version"):
        v = hexbytes(fields["Mifare version"])
        if any(v):
            out.append(f"version {v.hex(' ')}")
    if fields.get("Signature"):                 # zeros too: clones have them
        out.append(f"signature {hexbytes(fields['Signature']).hex(' ')}")
    for i in range(3):
        if f"Counter {i}" in fields:
            out.append(f"counter{i} {int(fields[f'Counter {i}'], 0)}")
        if f"Tearing {i}" in fields:
            out.append(f"tearing{i} {fields[f'Tearing {i}']}")
    pages = int(fields.get("Pages total", "0"))
    if not pages:
        raise ValueError("no pages in the file")
    for p in range(pages):
        out.append(f"mem {fields.get(f'Page {p}', '00 00 00 00')}")
    return "\n".join(out) + "\n"


URI_PREFIXES = ["", "http://www.", "https://www.", "http://", "https://", "tel:", "mailto:"]


def ndef_uri(url):
    """An NDEF message TLV holding one URI record."""
    code = max((i for i, p in enumerate(URI_PREFIXES) if url.startswith(p)),
               key=lambda i: len(URI_PREFIXES[i]))
    payload = bytes([code]) + url[len(URI_PREFIXES[code]):].encode()
    if len(payload) > 255:
        raise ValueError("URL too long for a short record")
    record = bytes([0xD1, 0x01, len(payload), 0x55]) + payload
    if len(record) >= 255:
        raise ValueError("URL too long")
    return bytes([0x03, len(record)]) + record + b"\xFE"


def blank(kind, uid, url=None):
    """A factory-fresh NTAG21x with this 7-byte UID (and a URL, as NDEF)."""
    vers, pages = BLANK[kind]
    if len(uid) != 7:
        raise ValueError("an NTAG UID is 7 bytes")
    mem = bytearray(pages * 4)
    mem[0:3] = uid[0:3]
    mem[3] = 0x88 ^ uid[0] ^ uid[1] ^ uid[2]    # BCC0
    mem[4:8] = uid[3:7]
    mem[8] = uid[3] ^ uid[4] ^ uid[5] ^ uid[6]  # BCC1
    mem[9] = 0x48                               # internal
    mem[12:16] = bytes([0xE1, 0x10, {45: 0x12, 135: 0x3E, 231: 0x6D}[pages], 0x00])
    tlv = ndef_uri(url) if url else bytes([0x03, 0x00, 0xFE])   # empty NDEF
    if 16 + len(tlv) > (pages - 5) * 4:
        raise ValueError("NDEF message does not fit")
    mem[16:16 + len(tlv)] = tlv
    cfg = (pages - 4) * 4
    mem[cfg:cfg + 4] = bytes([0x04, 0x00, 0x00, 0xFF])   # MIRROR, AUTH0 = FF
    out = [f"# {kind.upper()} (blank)", "type t2", f"uid {uid.hex(' ')}", "atqa 44 00",
           "sak 00", f"version {vers.hex(' ')}"]
    out += [f"mem {mem[i:i + 4].hex(' ')}" for i in range(0, len(mem), 4)]
    return "\n".join(out) + "\n"


def main():
    args = sys.argv[1:]
    if len(args) == 1 and args[0].endswith(".nfc"):
        print(describe(parse_nfc(args[0])), end="")
    elif len(args) in (2, 3) and args[0].lstrip("-") in BLANK:
        print(blank(args[0].lstrip("-"), bytes.fromhex(args[1]),
                    args[2] if len(args) == 3 else None), end="")
    else:
        print(__doc__)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
