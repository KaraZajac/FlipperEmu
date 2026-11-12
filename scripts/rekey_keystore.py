#!/usr/bin/env python3
# Copyright (c) 2026, Kara Zajac
# SPDX-License-Identifier: BSD-3-Clause
"""Re-key a Flipper Sub-GHz keystore to the emulated Flipper's enclave key.

A real Flipper's Sub-GHz keystores (subghz/assets/keeloq_mfcodes and the
rest) are AES-256-CBC encrypted under factory key 1, which never leaves the
device. FlipperEmu's enclave has its own public factory keys instead
(scripts/fe_enclave.py, qemu/hw/arm/flipper/flipper_enclave.c), so the stock
files decrypt to noise there and Sub-GHz loses its manufacturer codes.

This closes that gap without the real key. It takes the DECRYPTED keys - one
`KEY:TYPE:NAME` line each, exactly the firmware's `%08lX%08lX:%hu:%s` - and
writes an equivalent keystore encrypted under FlipperEmu's key 1, byte for
byte in the firmware's on-disk format (targets/f7/furi_hal/furi_hal_crypto.c
and lib/subghz/subghz_keystore.c). The unmodified firmware then loads it in
the emulator exactly as it loads the stock file on hardware; drop the output
into a firmware fork's resources (applications/.../resources/subghz/assets/).

    rekey_keystore.py keys   IN OUT [--slot N] [--iv TEXT] [--enclave FILE]
    rekey_keystore.py verify FILE   [--slot N]            [--enclave FILE]

`keys` reads IN (a decrypted key list; blank lines and `#` comments are
skipped) and writes OUT. `verify` decrypts an encrypted keystore with the
same key and prints the keys back, to check a file round-trips. --slot is the
enclave slot (default 1). --iv sets the stored IV (16 bytes; ASCII, or hex
with 0x); the default is fixed so re-keying is reproducible. --enclave reads
the key from a device's enclave.keys instead of FlipperEmu's built-in key.

Keep the input key lists private: see the note in subghz_keystore.c.
"""
import argparse
import pathlib
import struct
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import fe_enclave  # noqa: E402

# The firmware's cute default was "...Meow Meow OwO"; ours says where it came
# from. Any 16 bytes work: the field is stored in the clear and scrambled the
# same way on both sides. Fixed, so a re-key is reproducible (stable in git).
DEFAULT_IV = b"FlipperEmu rekey"
FILETYPE = "Flipper SubGhz Keystore File"
RAW_FILETYPE = "Flipper SubGhz Keystore RAW File"
VERSION = 0


def mess_with_iv(iv: bytes) -> bytes:
    """subghz_keystore_mess_with_iv(): the firmware scrambles the stored IV
    into the one it actually chains with, on both read and write. Ported from
    the ARM asm (lib/subghz/subghz_keystore.c): per 32-bit little-endian word,
    add the word byte-wise (UADD8) to itself shifted up one byte, carrying the
    top byte of the word below in."""
    def uadd8(a, b):
        return sum(((((a >> s) & 0xFF) + ((b >> s) & 0xFF)) & 0xFF) << s
                   for s in (0, 8, 16, 24))

    w = list(struct.unpack("<4I", iv))
    carry = 0
    out = []
    for i in range(4):
        shifted = ((w[i] << 8) | carry) & 0xFFFFFFFF
        out.append(uadd8(shifted, w[i]))
        carry = w[i] >> 24
    return struct.pack("<4I", *out)


def parse_iv(text: str) -> bytes:
    """--iv as 16 ASCII bytes, or hex if it starts 0x / is 32 hex digits."""
    t = text.strip()
    if t.lower().startswith("0x"):
        t = t[2:]
    if len(t) == 32 and all(c in "0123456789abcdefABCDEF" for c in t):
        return bytes.fromhex(t)
    data = text.encode()
    if len(data) != 16:
        raise ValueError(f"the IV is 16 bytes (got {len(data)}); "
                         "pass 16 characters or 32 hex digits")
    return data


def read_key_list(path) -> list:
    """`KEY:TYPE:NAME` lines from a decrypted key list, in file order."""
    keys = []
    for n, line in enumerate(pathlib.Path(path).read_text().splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        fields = line.split(":")
        if len(fields) != 3 or not fields[0] or not fields[1].isdigit():
            raise ValueError(f"{path}:{n}: expected KEY:TYPE:NAME, got {line!r}")
        keys.append(line)
    return keys


def plaintext_line(key_line: str) -> bytes:
    """One key as the firmware packs it: the text, zero-padded to a whole
    number of 16-byte blocks (subghz_keystore_save memsets then snprintf's)."""
    data = key_line.encode("ascii")
    return data + bytes(-len(data) % 16)


def enclave_key(slot: int, enclave_file) -> bytes:
    if enclave_file:
        key = fe_enclave.slot_key(enclave_file, str(slot))
    else:
        key = fe_enclave.default_key(slot)
    if len(key) != 32:
        raise ValueError(f"slot {slot}: a Sub-GHz keystore needs a 256-bit key")
    return key


def rekey(key_lines: list, key: bytes, stored_iv: bytes) -> str:
    """The encrypted keystore file text for these keys under `key`."""
    lines = [plaintext_line(k) for k in key_lines]
    plain = b"".join(lines)
    # One continuous CBC stream (the firmware loads the key once and the IV
    # register chains across every line), then split back per line.
    cipher = fe_enclave.encrypt(key, mess_with_iv(bytearray(stored_iv)), plain)
    out = [f"Filetype: {FILETYPE}", f"Version: {VERSION}", "Encryption: 1",
           "IV: " + " ".join(f"{b:02X}" for b in stored_iv)]
    pos = 0
    for raw in lines:
        out.append(cipher[pos:pos + len(raw)].hex().upper())
        pos += len(raw)
    return "\n".join(out) + "\n"


def read_raw_hex(path) -> bytes:
    """The decrypted bytes of a RAW keystore, as a hex blob (whitespace and
    `#` comment lines ignored). Whole 16-byte blocks."""
    hx = "".join(ln.split("#", 1)[0] for ln in pathlib.Path(path).read_text()
                 .splitlines())
    hx = "".join(hx.split())
    try:
        data = bytes.fromhex(hx)
    except ValueError:
        raise ValueError(f"{path}: expected hex bytes") from None
    if not data or len(data) % 16:
        raise ValueError(f"{path}: RAW data must be whole 16-byte blocks "
                         f"(got {len(data)} bytes)")
    return data


def rekey_raw(plain: bytes, key: bytes, stored_iv: bytes) -> str:
    """A RAW keystore (nice_flor_s, alutech_at_4n, came_atomo): one CBC chain
    over the whole blob, read back at any 16-byte offset with the block before
    it as the IV (subghz_keystore_raw_get_data)."""
    cipher = fe_enclave.encrypt(key, mess_with_iv(bytearray(stored_iv)), plain)
    return "\n".join([f"Filetype: {RAW_FILETYPE}", f"Version: {VERSION}",
                      "Encryption: 1",
                      "IV: " + " ".join(f"{b:02X}" for b in stored_iv),
                      "Encrypt_data: RAW", cipher.hex().upper()]) + "\n"


def _parse_keystore(text: str):
    """(is_raw, stored_iv, cipher, line_lengths) from a keystore file."""
    is_raw = False
    stored_iv = None
    cipher = b""
    lengths = []
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("Filetype:"):
            is_raw = "RAW File" in line
        elif line.startswith("IV:"):
            stored_iv = bytes.fromhex(line[3:].replace(" ", ""))
        elif line and ":" not in line and \
                all(c in "0123456789ABCDEFabcdef" for c in line):
            if len(line) % 32:
                raise ValueError(f"encrypted line not a whole block: {line!r}")
            cipher += bytes.fromhex(line)
            lengths.append(len(line) // 2)
    if stored_iv is None:
        raise ValueError("no IV in the file")
    return is_raw, stored_iv, cipher, lengths


def verify(text: str, key: bytes) -> list:
    """Decrypt a keystore the way the firmware does. For a normal keystore
    returns the KEY:TYPE:NAME lines; for a RAW one, the decrypted hex blob."""
    is_raw, stored_iv, cipher, lengths = _parse_keystore(text)
    plain = fe_enclave.decrypt(key, mess_with_iv(bytearray(stored_iv)), cipher)
    if is_raw:
        return [plain.hex().upper()]
    keys, pos = [], 0
    for ln in lengths:
        keys.append(plain[pos:pos + ln].split(b"\0")[0].decode("ascii", "replace"))
        pos += ln
    return keys


def main(argv) -> int:
    ap = argparse.ArgumentParser(add_help=False)
    ap.add_argument("action", choices=("keys", "raw", "verify"))
    ap.add_argument("paths", nargs="+")
    ap.add_argument("--slot", type=int, default=1)
    ap.add_argument("--iv", default=None)
    ap.add_argument("--enclave", default=None)
    try:
        args = ap.parse_args(argv[1:])
        key = enclave_key(args.slot, args.enclave)
        if args.action == "keys":
            if len(args.paths) != 2:
                raise ValueError("keys needs an input and an output path")
            src, dst = args.paths
            stored_iv = parse_iv(args.iv) if args.iv else DEFAULT_IV
            key_lines = read_key_list(src)
            text = rekey(key_lines, key, stored_iv)
            back = verify(text, key)
            if back != key_lines:
                raise ValueError("self-check failed: the file does not "
                                 "round-trip under the same key")
            pathlib.Path(dst).write_text(text)
            print(f"{dst}: {len(key_lines)} keys, encrypted under slot "
                  f"{args.slot}, round-trips OK")
        elif args.action == "raw":
            if len(args.paths) != 2:
                raise ValueError("raw needs an input and an output path")
            src, dst = args.paths
            stored_iv = parse_iv(args.iv) if args.iv else DEFAULT_IV
            plain = read_raw_hex(src)
            text = rekey_raw(plain, key, stored_iv)
            if bytes.fromhex(verify(text, key)[0]) != plain:
                raise ValueError("self-check failed: RAW file does not "
                                 "round-trip under the same key")
            pathlib.Path(dst).write_text(text)
            print(f"{dst}: {len(plain)} RAW bytes, encrypted under slot "
                  f"{args.slot}, round-trips OK")
        else:
            for p in args.paths:
                for k in verify(pathlib.Path(p).read_text(), key):
                    print(k)
    except (ValueError, OSError) as e:
        print(f"rekey: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
