#!/usr/bin/env python3
# Copyright (c) 2026, Kara Zajac
# SPDX-License-Identifier: BSD-3-Clause
"""NFC cards for FlipperEmu's ST25R3916 (/machine/nfc).

Turns a Flipper .nfc file into the card description the emulated reader
holds in its field (see qemu/hw/arm/flipper/nfc_card.c):

    fe_nfc.py card.nfc                      -> the description, on stdout
    fe_nfc.py --ntag215 04112233445566      -> a blank NTAG215 with that UID
    fe_nfc.py --ntag215 04112233445566 https://flipperzero.one   (with a URL)
    fe_nfc.py --nfc-file --ntag215 04112233445566 [URL]  -> as a Flipper .nfc file

    fe_nfc.py --mfc1k 11223344              -> a factory MIFARE Classic 1K
    fe_nfc.py --desfire 04A1B2C3D4E5F6      -> a MIFARE DESFire EV1 with two
                                               applications and their files
    fe_nfc.py --nfc-file --desfire 04A1B2C3D4E5F6   -> ... as a Flipper .nfc file
    fe_nfc.py --ulc 04A1B2C3D4E5F6          -> a MIFARE Ultralight C, its pages
                                               from 16 on behind its key
    fe_nfc.py --iso14443b 1A2B3C4D          -> an ISO14443-4B card with that PUPI
    fe_nfc.py --st25tb D0021C0123456789     -> an ST25TB04K with that UID
    fe_nfc.py --felica 012E4CD21A8B3C5F     -> a FeliCa Lite-S with that IDm
    fe_nfc.py --slix E004010812345678       -> an NXP SLIX2 (ISO15693) with that UID
                                               (both with --nfc-file too)

    fe_nfc.py hf-mf-11223344-dump.json      -> a Proxmark3 dump's description
    fe_nfc.py --nfc-file hf-mfu-dump.json   -> the dump as a Flipper .nfc file

Supported: MIFARE Ultralight / Ultralight EV1 / Ultralight C / NTAG21x (Type
2; an Ultralight C's key is its pages 44 to 47), MIFARE
Classic Mini / 1K / 4K (unread blocks: zeros; unread keys: FF..FF), MIFARE
DESFire (what the dump has of it: its files that were read, and an
authentication error for those that were not), an ISO14443-4A card with no
application that is known (its ATS), FeliCa Lite-S (its 28 blocks),
ISO15693-3 and NXP's SLIX.  A
Proxmark3 dump is its client's JSON: `hf mf autopwn`, `hf mf dump`
(FileType "mfcard", "mfc v2", "mfc v3") or `hf mfu dump` ("mfu").
"""
import json
import pathlib
import sys

# NFC_LSB_ATQA_FORMAT_VERSION: files newer than this store ATQA MSB first
LSB_ATQA_FORMAT_VERSION = 2

T2_TYPES = ("NTAG/Ultralight", "NTAG203", "NTAG213", "NTAG215", "NTAG216",
            "Mifare Ultralight", "Mifare Ultralight 11", "Mifare Ultralight 21",
            "Mifare Ultralight C",
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


MFC_BLOCKS = {"MINI": 20, "1K": 64, "4K": 256}
MFC_TRAILER = bytes.fromhex("FFFFFFFFFFFF") + bytes.fromhex("FF078069") + \
    bytes.fromhex("FFFFFFFFFFFF")


def mfc_trailer(block):
    return block % 4 == 3 if block < 128 else (block - 128) % 16 == 15


def describe_mfc(fields, atqa):
    """A MIFARE Classic dump: its blocks; what was never read gets the
    factory values (the card's keys must be something)."""
    kind = fields.get("Mifare Classic type", "1K").upper()
    if kind not in MFC_BLOCKS:
        raise ValueError(f"MIFARE Classic type {kind!r} is not supported")
    uid = hexbytes(fields["UID"])
    out = [f"# MIFARE Classic {kind}", "type mfc", f"uid {uid.hex(' ')}",
           f"atqa {atqa.hex(' ')}", f"sak {fields.get('SAK', '08')}"]
    for b in range(MFC_BLOCKS[kind]):
        raw = fields.get(f"Block {b}", " ".join(["??"] * 16)).split()
        if mfc_trailer(b):
            raw = [MFC_TRAILER[i:i + 1].hex() if x == "??" else x
                   for i, x in enumerate(raw)]
        out.append(f"mem {' '.join('00' if x == '??' else x for x in raw)}")
    return "\n".join(out) + "\n"


REC_VERSION, REC_FREE, REC_APP, REC_FILE = 0x01, 0x02, 0x10, 0x20


def describe_iso4(fields, atqa, desfire):
    """An ISO14443-4A card; a MIFARE DESFire's tables as records for
    nfc_card.c: what its commands answer, taken from the dump"""
    uid = hexbytes(fields["UID"])
    out = ["# MIFARE DESFire" if desfire else "# ISO14443-4A", "type iso4",
           f"uid {uid.hex(' ')}", f"atqa {atqa.hex(' ')}",
           f"sak {fields.get('SAK', '20')}"]
    # the ATS as the file has it: its format byte, the interface bytes that
    # the format byte says are there, the historical bytes (a length before)
    if fields.get("T0"):
        t0 = int(fields["T0"], 16)
        ats = bytes([t0]) + b"".join(
            hexbytes(fields.get(key, "00"))[:1]
            for bit, key in ((0x10, "TA(1)"), (0x20, "TB(1)"), (0x40, "TC(1)"))
            if t0 & bit) + hexbytes(fields.get("T1...Tk", ""))
        out.append(f"ats {bytes([len(ats) + 1]).hex()} {ats.hex(' ')}".rstrip())
    elif fields.get("ATS"):
        out.append(f"ats {hexbytes(fields['ATS']).hex(' ')}")
    if not desfire:
        return "\n".join(out + ["app none"]) + "\n"
    out.append("app desfire")

    def rec(kind, data):
        out.append(f"rec {kind:02x} {bytes(data).hex(' ')}".rstrip())

    def flag(key):
        return fields.get(key, "false").strip().lower() == "true"

    def keys(prefix):
        """GetKeySettings' two bytes and each key's version (b"": the dump
        has none of it, the card asked for a key)"""
        if f"{prefix} Change Key ID" not in fields:
            return b""
        first = (flag(f"{prefix} Key Changeable")
                 | flag(f"{prefix} Free Directory List") << 1
                 | flag(f"{prefix} Free Create Delete") << 2
                 | flag(f"{prefix} Config Changeable") << 3
                 | (int(fields[f"{prefix} Change Key ID"], 16) & 0xF) << 4)
        second = (int(fields.get(f"{prefix} Max Keys", "01"), 16) & 0xF
                  | (int(fields.get(f"{prefix} Flags", "00"), 16) & 0xF) << 4)
        versions, i = [], 0
        while f"{prefix} Key {i} Version" in fields:
            versions.append(int(fields[f"{prefix} Key {i} Version"], 16))
            i += 1
        return bytes([first, second, *versions])

    version = hexbytes(fields.get("PICC Version", ""))
    if len(version) != 28:
        raise ValueError("a MIFARE DESFire dump has its PICC Version (28 bytes)")
    rec(REC_VERSION, version)
    if "PICC Free Memory" in fields:
        rec(REC_FREE, int(fields["PICC Free Memory"]).to_bytes(3, "little"))
    rec(REC_APP, bytes(3) + keys("PICC"))
    ids = hexbytes(fields.get("Application IDs", ""))
    for aid in (ids[i:i + 3] for i in range(0, len(ids) - 2, 3)):
        prefix = f"Application {aid.hex()}"
        rec(REC_APP, aid + keys(prefix))
        for fid in hexbytes(fields.get(f"{prefix} File IDs", "")):
            fp = f"{prefix} File {fid}"
            kind = int(fields.get(f"{fp} Type", "00"), 16)
            number = lambda key, size: int(fields.get(f"{fp} {key}", "0")
                                           ).to_bytes(size, "little")
            settings = bytes([kind, int(fields.get(f"{fp} Communication "
                                                   "Settings", "00"), 16)]) \
                + hexbytes(fields.get(f"{fp} Access Rights", "EE EE"))[:2]
            if kind in (0, 1):                  # standard, backup
                settings += number("Size", 3)
            elif kind == 2:                     # value
                settings += (number("Lo Limit", 4) + number("Hi Limit", 4)
                             + number("Limited Credit Value", 4)
                             + bytes([flag(f"{fp} Limited Credit Enabled")]))
            elif kind in (3, 4):                # linear, cyclic records
                settings += number("Size", 3) + number("Max", 3) + number("Cur", 3)
            else:                               # a transaction MAC's
                settings += (hexbytes(fields.get(f"{fp} Key Option", "00"))[:1]
                             + hexbytes(fields.get(f"{fp} Key Version", "00"))[:1])
            data = hexbytes(fields[fp]) if fields.get(fp) else None
            rec(REC_FILE, aid + bytes([fid, len(settings)]) + settings
                + bytes([data is not None]) + (data or b""))
    return "\n".join(out) + "\n"


def sample_desfire(uid):
    """A MIFARE DESFire EV1 4K as a Flipper .nfc file's text, made up: the
    card's own keys, and two applications with files of each kind that a
    reader gets without a key (one longer than a frame), and one that it
    does not get"""
    if len(uid) != 7:
        raise ValueError("a MIFARE DESFire's UID is 7 bytes")
    up = lambda b: bytes(b).hex(" ").upper()
    version = (bytes.fromhex("04010101001805") + bytes.fromhex("04010101041805")
               + uid + bytes.fromhex("BA3456789025" "20"))

    def keys(prefix, most, versions):
        return [f"{prefix} Change Key ID: 00", f"{prefix} Config Changeable: true",
                f"{prefix} Free Create Delete: true",
                f"{prefix} Free Directory List: true",
                f"{prefix} Key Changeable: true", f"{prefix} Flags: 00",
                f"{prefix} Max Keys: {most:02X}"] + \
            [f"{prefix} Key {i} Version: {v:02X}" for i, v in enumerate(versions)]
    out = ["Filetype: Flipper NFC device", "Version: 4",
           "Device type: Mifare DESFire", f"UID: {up(uid)}", "ATQA: 03 44",
           "SAK: 20", "T0: 75", "TA(1): 77", "TB(1): 81", "TC(1): 02",
           "T1...Tk: 80", f"PICC Version: {up(version)}",
           "PICC Free Memory: 2848", *keys("PICC", 1, [0]),
           "Application Count: 2", "Application IDs: 56 34 12 F4 00 01"]
    text = bytes(range(0x20, 0x70))             # 80 bytes: two frames
    a = "Application 563412"
    out += [*keys(a, 2, [0, 1]), f"{a} File IDs: 01 02 03 04",
            f"{a} File 1 Type: 00", f"{a} File 1 Communication Settings: 00",
            f"{a} File 1 Access Rights: EE EE", f"{a} File 1 Size: {len(text)}",
            f"{a} File 1: {up(text)}",
            f"{a} File 2 Type: 02", f"{a} File 2 Communication Settings: 00",
            f"{a} File 2 Access Rights: EE EE", f"{a} File 2 Hi Limit: 1000",
            f"{a} File 2 Lo Limit: 0", f"{a} File 2 Limited Credit Value: 0",
            f"{a} File 2 Limited Credit Enabled: false",
            f"{a} File 2: {up((250).to_bytes(4, 'little'))}",
            f"{a} File 3 Type: 04", f"{a} File 3 Communication Settings: 00",
            f"{a} File 3 Access Rights: EE EE", f"{a} File 3 Size: 16",
            f"{a} File 3 Max: 4", f"{a} File 3 Cur: 2",
            f"{a} File 3: {up(bytes(range(0xA0, 0xC0)))}",
            f"{a} File 4 Type: 00", f"{a} File 4 Communication Settings: 03",
            f"{a} File 4 Access Rights: 00 00", f"{a} File 4 Size: 32"]
    b = "Application f40001"
    out += [*keys(b, 1, [0]), f"{b} File IDs: 00",
            f"{b} File 0 Type: 01", f"{b} File 0 Communication Settings: 00",
            f"{b} File 0 Access Rights: EE EE", f"{b} File 0 Size: 8",
            f"{b} File 0: {up(b'FlipEmu!')}"]
    return "\n".join(out) + "\n"


def describe_iso14443b(fields, dev):
    """An ISO14443-3B or -4B card: its PUPI and what its ATQB says"""
    uid = hexbytes(fields["UID"])
    if len(uid) != 4:
        raise ValueError("an ISO14443B card's UID (its PUPI) is 4 bytes")
    app = hexbytes(fields.get("Application data", "00 00 00 00"))
    info = hexbytes(fields.get("Protocol info", "00 00 00"))
    if len(app) != 4 or len(info) != 3:
        raise ValueError("Application data is 4 bytes, Protocol info 3")
    return "\n".join([f"# {dev}", "type iso14443b", f"uid {uid.hex(' ')}",
                      f"appdata {app.hex(' ')}", f"protinfo {info.hex(' ')}",
                      "app none"]) + "\n"


ST25TB_BLOCKS = {"512AT": 16, "512AC": 16, "X512": 16, "2K": 64, "4K": 128,
                 "X4K": 128}


def describe_st25tb(fields):
    """An ST25TB (SRI, SRT, SRIX): its blocks and the system OTP block"""
    kind = fields.get("ST25TB Type", "")
    if kind not in ST25TB_BLOCKS:
        raise ValueError(f"ST25TB type {kind!r}: one of "
                         f"{', '.join(ST25TB_BLOCKS)}")
    uid = hexbytes(fields["UID"])
    if len(uid) != 8:
        raise ValueError("an ST25TB's UID is 8 bytes")
    out = [f"# ST25TB {kind}", "type st25tb", f"uid {uid.hex(' ')}",
           f"otp {fields.get('System OTP Block', 'FF FF FF FF')}"]
    for b in range(ST25TB_BLOCKS[kind]):
        out.append(f"mem {fields.get(f'Block {b}', 'FF FF FF FF')}")
    return "\n".join(out) + "\n"


def sample_st25tb(uid):
    """An ST25TB04K as a Flipper .nfc file's text, made up"""
    if len(uid) != 8 or uid[0] != 0xD0 or uid[1] != 0x02 or uid[2] >> 2 != 7:
        raise ValueError("an ST25TB04K's UID is 8 bytes, D0 02 and then 1C "
                         "to 1F")
    up = lambda b: bytes(b).hex(" ").upper()
    out = ["Filetype: Flipper NFC device", "Version: 4", "Device type: ST25TB",
           f"UID: {up(uid)}", "ST25TB Type: 4K"]
    for b in range(128):
        block = (bytes(4) if b < 5 else b"\xfe\xff\xff\xff" if b < 7 else
                 f"{b:04d}".encode())
        out.append(f"Block {b}: {up(block)}")
    out.append("System OTP Block: FF FF FF FE")         # blocks 7, 8 locked
    return "\n".join(out) + "\n"


def sample_iso14443b(uid):
    """An ISO14443-4B card as a Flipper .nfc file's text, made up"""
    if len(uid) != 4:
        raise ValueError("an ISO14443B card's UID (its PUPI) is 4 bytes")
    up = lambda b: bytes(b).hex(" ").upper()
    return "\n".join(["Filetype: Flipper NFC device", "Version: 4",
                      "Device type: ISO14443-4B", f"UID: {up(uid)}",
                      "Application data: 00 12 34 E1",
                      "Protocol info: 00 81 85"]) + "\n"


def describe_felica(fields):
    """A FeliCa Lite-S: its IDm and PMm, and its 28 blocks as a dump has
    them, each with the two status bytes the card gave for it (a block that
    the dump lacks: an illegal block number's)"""
    idm = hexbytes(fields.get("Manufacture id", fields["UID"]))
    pmm = hexbytes(fields.get("Manufacture parameter", "00 F1 00 00 00 01 43 00"))
    if "Block 0" not in fields:
        raise ValueError("a FeliCa dump with its blocks (a Lite-S's): a "
                         "FeliCa Standard card's systems are not supported yet")
    out = ["# FeliCa Lite-S", "type felica", f"uid {idm.hex(' ')}",
           f"pmm {pmm.hex(' ')}"]
    for i in range(28):
        block = hexbytes(fields.get(f"Block {i}", "01 A8 " + "00 " * 16))
        out.append(f"mem {block[:18].ljust(18, bytes(1)).hex(' ')}")
    return "\n".join(out) + "\n"


def describe_iso15693(fields, slix):
    """An ISO15693-3 card, and NXP's SLIX on it"""
    uid = hexbytes(fields["UID"])
    size = int(fields.get("Block Size", "04"), 16)
    count = int(fields.get("Block Count", "0"))
    data = hexbytes(fields.get("Data Content", ""))
    security = hexbytes(fields.get("Security Status", ""))
    if len(security) == count + 1:              # (an older file: a byte before)
        security = security[1:]
    if not count or len(data) < count * size:
        raise ValueError("an ISO15693 dump has its blocks (Block Count, "
                         "Block Size, Data Content)")
    flag = lambda key: fields.get(key, "false").strip().lower() == "true"
    lock = (flag("Lock DSFID") | flag("Lock AFI") << 1 | flag("Lock EAS") << 2
            | flag("Lock PPL") << 3)
    out = ["# SLIX" if slix else "# ISO15693-3", "type iso15693",
           f"uid {uid.hex(' ')}", f"dsfid {fields.get('DSFID', '00')}",
           f"afi {fields.get('AFI', '00')}",
           f"icref {fields.get('IC Reference', '00')}", f"blocksize {size:02x}",
           f"lock {lock:02x}"]
    out += [f"mem {data[i:i + 64].hex(' ')}" for i in range(0, count * size, 64)]
    out.append(f"security {security[:count].ljust(count, bytes(1)).hex(' ')}")
    if slix:
        out.append("slix 1")
        if fields.get("Signature"):
            out.append(f"signature {hexbytes(fields['Signature']).hex(' ')}")
        for name in ("Read", "Write", "Privacy", "Destroy", "EAS"):
            if fields.get(f"Password {name}"):
                out.append(f"pwd-{name.lower()} "
                           f"{hexbytes(fields[f'Password {name}']).hex(' ')}")
        out.append(f"privacy {int(flag('Privacy Mode'))}")
        out.append(f"protection {fields.get('Protection Pointer', '00')} "
                   f"{fields.get('Protection Condition', '00')}")
        out.append("accept-all "
                   f"{int(fields.get('Capabilities') == 'AcceptAllPasswords')}")
    return "\n".join(out) + "\n"


ULC_KEY = bytes.fromhex("49454D4B41455242214E4143554F5946")    # (made up)


def sample_ulc(uid):
    """A MIFARE Ultralight C as a Flipper .nfc file's text, made up: its
    pages from 16 on want its key (ULC_KEY), for reading too"""
    if len(uid) != 7:
        raise ValueError("a MIFARE Ultralight C's UID is 7 bytes")
    up = lambda b: bytes(b).hex(" ").upper()
    mem = bytearray(48 * 4)
    mem[0:3] = uid[0:3]
    mem[3] = 0x88 ^ uid[0] ^ uid[1] ^ uid[2]
    mem[4:8] = uid[3:7]
    mem[8] = uid[3] ^ uid[4] ^ uid[5] ^ uid[6]
    mem[9] = 0x48
    mem[16:32] = b"open to everyone"
    mem[64:80] = b"only with a key!"
    mem[42 * 4] = 16                            # AUTH0
    mem[44 * 4:48 * 4] = ULC_KEY
    out = ["Filetype: Flipper NFC device", "Version: 4",
           "Device type: NTAG/Ultralight", f"UID: {up(uid)}", "ATQA: 00 44",
           "SAK: 00", "Data format version: 2",
           "NTAG/Ultralight type: Mifare Ultralight C",
           f"Signature: {up(bytes(32))}", f"Mifare version: {up(bytes(8))}"]
    for i in range(3):
        out += [f"Counter {i}: 0", f"Tearing {i}: 00"]
    out += ["Pages total: 48", "Pages read: 48"]
    out += [f"Page {i}: {up(mem[i * 4:i * 4 + 4])}" for i in range(48)]
    out.append("Failed authentication attempts: 0")
    return "\n".join(out) + "\n"


def sample_felica(idm):
    """A FeliCa Lite-S as a Flipper .nfc file's text, made up: its user
    blocks with something in them, its ID blocks, its card key not given"""
    if len(idm) != 8:
        raise ValueError("a FeliCa card's IDm is 8 bytes")
    up = lambda b: bytes(b).hex(" ").upper()
    pmm = bytes.fromhex("00F1000000014300")
    blocks = [bytes(16)] * 28
    blocks[0] = b"FlipperEmu FeliCa"[:16]
    blocks[1] = bytes(range(0x10, 0x20))
    blocks[13] = bytes(range(0xD0, 0xE0))
    blocks[14] = bytes([0xFF] * 4 + [0] * 12)           # REG
    blocks[17] = idm + bytes(8)                         # ID
    blocks[18] = idm + pmm                              # D_ID
    blocks[20] = bytes.fromhex("88B4") + bytes(14)      # SYS_C
    blocks[23] = bytes.fromhex("FFFFFF00FF00100000000000000000 00")  # MC
    status = [bytes(2)] * 28
    status[22] = bytes.fromhex("01A8")                  # CK: not to be read
    out = ["Filetype: Flipper NFC device", "Version: 4", "Device type: FeliCa",
           f"UID: {up(idm)}", "Data format version: 2",
           f"Manufacture id: {up(idm)}", f"Manufacture parameter: {up(pmm)}",
           "IC Type: FeliCa Lite-S RC-S966", "Blocks total: 28",
           # (the firmware counts one fewer than it read, without a key)
           f"Blocks read: {sum(1 for s in status if not s[0]) - 1}"]
    out += [f"Block {i}: {up(status[i] + (blocks[i] if not status[i][0] else bytes(16)))}"
            for i in range(28)]
    return "\n".join(out) + "\n"


def sample_slix(uid):
    """An NXP SLIX2 (ICODE SLIX2, 80 blocks of 4 bytes: what a UID that
    begins E0 04 01 08 is) as a Flipper .nfc file's text, made up: an
    NDEF-like beginning, one block locked, a signature that is no NXP's"""
    if len(uid) != 8 or uid[0] != 0xE0:
        raise ValueError("an ISO15693 card's UID is 8 bytes, the first E0")
    up = lambda b: bytes(b).hex(" ").upper()
    data = bytearray(80 * 4)
    data[0:16] = bytes.fromhex("E1400D01" "030BD101" "07540265" "6E466C69")
    data[16:20] = b"pEmu"
    data[100:104] = bytes.fromhex("CAFEF00D")
    security = bytearray(80)
    security[25] = 1
    out = ["Filetype: Flipper NFC device", "Version: 4", "Device type: SLIX",
           f"UID: {up(uid)}", "DSFID: 00", "AFI: 00", "IC Reference: 01",
           "Lock DSFID: false", "Lock AFI: false", "Block Count: 80",
           "Block Size: 04", f"Data Content: {up(data)}",
           f"Security Status: {up(security)}", "Capabilities: Default",
           "Password Read: 00 00 00 00", "Password Write: 00 00 00 00",
           "Password Privacy: 0F 0F 0F 0F", "Password Destroy: 0F 0F 0F 0F",
           "Password EAS: 00 00 00 00", f"Signature: {up(range(0x40, 0x60))}",
           "Privacy Mode: false", "Protection Pointer: 00",
           "Protection Condition: 00", "Lock EAS: false", "Lock PPL: false"]
    return "\n".join(out) + "\n"


def parse_nfc_text(text):
    fields = {}
    for line in text.splitlines():
        if not line.startswith("#") and ":" in line:
            key, _, value = line.partition(":")
            fields[key.strip()] = value.strip()
    return fields


def describe(fields):
    """The card description for nfc_card.c."""
    version = int(fields.get("Version", "4"))
    dev = fields.get("Device type", "")
    if dev in ("Mifare Classic",):
        atqa = hexbytes(fields["ATQA"])
        return describe_mfc(fields, atqa[::-1] if version > LSB_ATQA_FORMAT_VERSION
                            else atqa)
    if dev == "FeliCa":
        return describe_felica(fields)
    if dev in ("ISO14443-3B", "ISO14443-4B"):
        return describe_iso14443b(fields, dev)
    if dev == "ST25TB":
        return describe_st25tb(fields)
    if dev in ("ISO15693-3", "SLIX"):
        return describe_iso15693(fields, dev == "SLIX")
    if dev in ("Mifare DESFire", "ISO14443-4A", "ISO14443-4A (Unknown)"):
        atqa = hexbytes(fields["ATQA"])
        return describe_iso4(fields, atqa[::-1] if version >
                             LSB_ATQA_FORMAT_VERSION else atqa,
                             dev == "Mifare DESFire")
    kind = fields.get("NTAG/Ultralight type", dev)
    if dev not in T2_TYPES and kind not in T2_TYPES:
        raise ValueError(f"device type {dev!r} is not supported yet "
                         "(NTAG/Ultralight, MIFARE Classic, MIFARE DESFire, "
                         "ISO14443-4A, ISO14443-3B and -4B, ST25TB, FeliCa "
                         "Lite-S, ISO15693-3 and SLIX are)")
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
    if kind == "Mifare Ultralight C":
        # (its key is pages 44 to 47, as the firmware has it when it knew
        # the key; zeros in a dump that was read without one)
        out.append("ulc 1")
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


def blank_mfc(kind, uid):
    """A factory MIFARE Classic: transport keys FF..FF, empty blocks."""
    blocks = MFC_BLOCKS[kind.upper()]
    if len(uid) != 4:
        raise ValueError("a MIFARE Classic UID here is 4 bytes")
    sak = {"MINI": 0x09, "1K": 0x08, "4K": 0x18}[kind.upper()]
    atqa = bytes([0x04, 0x00]) if blocks < 256 else bytes([0x02, 0x00])
    out = [f"# MIFARE Classic {kind.upper()} (blank)", "type mfc", f"uid {uid.hex(' ')}",
           f"atqa {atqa.hex(' ')}", f"sak {sak:02x}"]
    for b in range(blocks):
        if b == 0:                      # manufacturer block
            blk = uid + bytes([uid[0] ^ uid[1] ^ uid[2] ^ uid[3], sak]) + atqa + \
                bytes.fromhex("6263646566676869")
        elif mfc_trailer(b):
            blk = MFC_TRAILER
        else:
            blk = bytes(16)
        out.append(f"mem {blk.hex(' ')}")
    return "\n".join(out) + "\n"


# ------------------------------------------------------------ Proxmark3 dumps

def describe_pm3(path):
    """The card description for a Proxmark3 client's JSON dump."""
    d = json.loads(pathlib.Path(path).read_text())
    kind = d.get("FileType", "")
    card = d.get("Card", {})
    blocks = d.get("blocks", {})
    hexb = lambda v: bytes.fromhex(str(v).replace(" ", ""))
    mem = [hexb(blocks[k]) for k in sorted(blocks, key=int)]
    if not mem:
        raise ValueError(f"{path}: no blocks in the dump")
    uid = hexb(card.get("UID", ""))
    if kind in ("mfcard", "mfc v2", "mfc v3"):
        size = {20: "MINI", 64: "1K", 256: "4K"}.get(len(mem))
        if not size:
            raise ValueError(f"{path}: {len(mem)} blocks: not a MIFARE Classic "
                             "Mini, 1K or 4K")
        atqa = hexb(card.get("ATQA", "0400" if size != "4K" else "0200"))
        sak = card.get("SAK", {"MINI": "09", "1K": "08", "4K": "18"}[size])
        out = [f"# MIFARE Classic {size} (Proxmark3 dump)", "type mfc",
               f"uid {uid.hex(' ')}", f"atqa {atqa.hex(' ')}", f"sak {sak.lower()}"]
        out += [f"mem {b.hex(' ')}" for b in mem]
        return "\n".join(out) + "\n"
    if kind == "mfu":
        version = hexb(card.get("Version", "00" * 8))
        name = {0x0F: "NTAG213", 0x11: "NTAG215", 0x13: "NTAG216"}.get(
            version[6] if len(version) == 8 and version[2] == 0x04 else None,
            "Mifare Ultralight")
        out = [f"# {name} (Proxmark3 dump)", "type t2", f"uid {uid.hex(' ')}",
               "atqa 44 00", "sak 00"]
        if any(version):
            out.append(f"version {version.hex(' ')}")
        if card.get("Signature"):
            out.append(f"signature {hexb(card['Signature']).hex(' ')}")
        for i in range(3):
            if card.get(f"Counter{i}"):
                out.append(f"counter{i} {int.from_bytes(hexb(card[f'Counter{i}']), 'little')}")
            if card.get(f"Tearing{i}"):
                out.append(f"tearing{i} {card[f'Tearing{i}'].lower()}")
        out += [f"mem {b.hex(' ')}" for b in mem]
        return "\n".join(out) + "\n"
    raise ValueError(f"{path}: Proxmark3 FileType {kind!r} is not supported "
                     "(mfcard, mfc v2, mfc v3, mfu are)")


def describe_any(path):
    """A Flipper .nfc file's or a Proxmark3 JSON dump's description."""
    if str(path).lower().endswith(".json"):
        return describe_pm3(path)
    return describe(parse_nfc(path))


T2_NAMES = {45: "NTAG213", 135: "NTAG215", 231: "NTAG216"}


def nfc_file(desc):
    """A Flipper .nfc file (format 4) for a Type 2 or MIFARE Classic card
    description: what the firmware loads to emulate it (NFC -> Saved, or
    `emulate -f`)."""
    kv, mem = {}, bytearray()
    for line in desc.splitlines():
        key, _, val = line.partition(" ")
        if key == "mem":
            mem += hexbytes(val)
        elif key and not key.startswith("#"):
            kv[key] = val
    atqa = hexbytes(kv["atqa"])
    up = lambda b: b.hex(" ").upper()
    if kv.get("type") == "mfc":
        blocks = len(mem) // 16
        size = {20: "MINI", 64: "1K", 256: "4K"}[blocks]
        out = ["Filetype: Flipper NFC device", "Version: 4", "Device type: Mifare Classic",
               f"UID: {up(hexbytes(kv['uid']))}", f"ATQA: {up(atqa[::-1])}",
               f"SAK: {kv.get('sak', '08').upper()}", f"Mifare Classic type: {size}",
               "Data format version: 2"]
        out += [f"Block {i}: {up(mem[i * 16:i * 16 + 16])}" for i in range(blocks)]
        return "\n".join(out) + "\n"
    if kv.get("type") != "t2":
        raise ValueError("only Type 2 and MIFARE Classic cards can be written as .nfc")
    pages = len(mem) // 4
    out = ["Filetype: Flipper NFC device", "Version: 4",
           "Device type: NTAG/Ultralight", f"UID: {up(hexbytes(kv['uid']))}",
           f"ATQA: {up(atqa[::-1])}", f"SAK: {kv.get('sak', '00').upper()}",
           "Data format version: 2",
           f"NTAG/Ultralight type: {T2_NAMES.get(pages, 'Mifare Ultralight')}",
           f"Signature: {up(hexbytes(kv.get('signature', '00 ' * 32)))}",
           f"Mifare version: {up(hexbytes(kv.get('version', '00 ' * 8)))}"]
    for i in range(3):
        out += [f"Counter {i}: {kv.get(f'counter{i}', '0')}", f"Tearing {i}: 00"]
    out += [f"Pages total: {pages}", f"Pages read: {pages}"]
    out += [f"Page {i}: {up(mem[i * 4:i * 4 + 4])}" for i in range(pages)]
    out.append("Failed authentication attempts: 0")
    return "\n".join(out) + "\n"


# cards that are made up for a UID: (what makes the .nfc text, in words)
SAMPLES = {"desfire": (sample_desfire, "a MIFARE DESFire EV1 (two "
                       "applications, made up)"),
           "ulc": (sample_ulc, "a MIFARE Ultralight C (made up; its key "
                   f"{ULC_KEY.hex().upper()})"),
           "iso14443b": (sample_iso14443b, "an ISO14443-4B card (made up)"),
           "st25tb": (sample_st25tb, "an ST25TB04K (made up)"),
           "felica": (sample_felica, "a FeliCa Lite-S (made up)"),
           "slix": (sample_slix, "an NXP SLIX2, ISO15693 (made up)")}


def main():
    args = sys.argv[1:]
    as_file = "--nfc-file" in args
    args = [a for a in args if a != "--nfc-file"]
    if len(args) == 2 and args[0].lstrip("-") in SAMPLES:
        text = SAMPLES[args[0].lstrip("-")][0](bytes.fromhex(args[1]))
        print(text if as_file else describe(parse_nfc_text(text)), end="")
        return 0
    if as_file:
        if len(args) == 1 and args[0].lower().endswith(".json"):
            print(nfc_file(describe_pm3(args[0])), end="")
            return 0
        if len(args) in (2, 3) and args[0].lstrip("-") in BLANK:
            print(nfc_file(blank(args[0].lstrip("-"), bytes.fromhex(args[1]),
                                 args[2] if len(args) == 3 else None)), end="")
            return 0
        print(__doc__)
        return 2
    if len(args) == 1 and args[0].endswith((".nfc", ".json")):
        print(describe_any(args[0]), end="")
    elif len(args) == 2 and args[0].lstrip("-").lower() in ("mfcmini", "mfc1k", "mfc4k"):
        print(blank_mfc(args[0].lstrip("-")[3:], bytes.fromhex(args[1])), end="")
    elif len(args) in (2, 3) and args[0].lstrip("-") in BLANK:
        print(blank(args[0].lstrip("-"), bytes.fromhex(args[1]),
                    args[2] if len(args) == 3 else None), end="")
    else:
        print(__doc__)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
