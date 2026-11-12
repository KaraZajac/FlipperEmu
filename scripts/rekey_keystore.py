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
    rekey_keystore.py raw    IN OUT [--slot N] [--iv TEXT] [--enclave FILE]
    rekey_keystore.py verify FILE... [--slot N]           [--enclave FILE]
    rekey_keystore.py merge  BASE MORE... OUT [--all] [--new-only]

`keys` reads IN (a decrypted key list; blank lines and `#` comments are
skipped) and writes OUT. `raw` does the same for a RAW keystore (nice_flor_s,
alutech_at_4n, came_atomo): IN holds its decrypted bytes as hex, whole
16-byte blocks. `verify` decrypts an encrypted keystore with the
same key and prints the keys back, to check a file round-trips. --slot is the
enclave slot (default 1). --iv sets the stored IV (16 bytes; ASCII, or hex
with 0x); the default is fixed so re-keying is reproducible. --enclave reads
the key from a device's enclave.keys instead of FlipperEmu's built-in key.

`merge` makes one decrypted key list of several: OUT is BASE with the codes of
the MORE lists that BASE does not have. It encrypts nothing, and it prints
names and types only, never a key. The lines of BASE stay first and as they
are: a firmware gives a remote the name of the first line that fits it, and
some decoders take the first line of a type. Of the other lists, in their
order, these lines are left out:

  - a code that the list has (the same key and the same type), whatever its
    name;
  - a type above 19, which no firmware here has (19 is the last one of
    Unleashed, which has the most);
  - a line of type 1 to 4 whose key a type-0 line of BASE has, as it is or
    with its bytes in the other order: for a type-0 line the firmware tries
    simple, normal, secure and magic-XOR learning, each with both orders;
  - a type-0 line whose key a line that stays has, as it is or with its bytes
    in the other order: it would be eight more tries for every packet, each a
    chance to name a remote that is not this maker's.

The new type-0 lines go last. With --all only the first rule holds, and the
new lines keep their order. With --new-only OUT has the new lines alone,
without those of BASE: a list for a file of its own beside BASE's keystore,
which a firmware reads only where a program asks for it.

Keep the input key lists private: see the note in subghz_keystore.c.
"""
import argparse
import pathlib
import re
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
# The last learning type of a firmware here: Unleashed's
# KEELOQ_LEARNING_TELCOMA_TABLE_LO (lib/subghz/protocols/keeloq_common.h).
# The official firmware's last one is 7, Momentum's 8.
LAST_TYPE = 19
# The firmware reads a line with "%08lX%08lX:%hu:%s" into a name of 64 bytes.
KEY_FIELD = re.compile(r"[0-9A-Fa-f]{16}")
NAME_MAX = 63
LEFT_OUT = (
    ("same", "the list has the code (the same key and type)"),
    ("type", f"a type above {LAST_TYPE}, which no firmware here has"),
    ("typed", "a type-0 line of the base list has the key, and tries this "
              "type with it"),
    ("zero", "a line that stays has the key"),
)


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
            # The line is not shown: it may hold a key
            raise ValueError(f"{path}:{n}: expected KEY:TYPE:NAME")
        keys.append(line)
    return keys


def parse_key_line(line: str) -> tuple:
    """(key, type, name) of a `KEY:TYPE:NAME` line. ValueError for a key that
    is not 16 hex digits, and for a name that the firmware reads otherwise.
    The message has the name and the type, not the key."""
    key, kind, name = line.split(":")
    if not KEY_FIELD.fullmatch(key):
        raise ValueError(f"{name}:{kind}: the key is 16 hex digits")
    if not name or len(name) > NAME_MAX or name != "".join(name.split()) \
            or not name.isascii():
        raise ValueError(f"{name!r}:{kind}: a name is 1 to {NAME_MAX} ASCII "
                         "characters with no blank among them")
    return int(key, 16), int(kind), name


def merge(base: list, more: list, everything=False) -> tuple:
    """One key list of `base` and what `more` adds to it (both `KEY:TYPE:NAME`
    lines; the rules are in the text at the top). Returns (lines, left): the
    lines of the new list, and for each line of `more` that is left out
    (why, its NAME:TYPE, the NAME:TYPE of the line that stands for it)."""
    def other_order(key):
        return int.from_bytes(key.to_bytes(8, "big"), "little")

    def tag(entry):
        return f"{entry[2]}:{entry[1]}"

    def holder(table, key):
        return table.get(key) or table.get(other_order(key))

    first = {}              # (key, type) -> the first line with them
    typed, zero = {}, {}    # key -> the first line that stays, by type 0 or not
    for line in base:
        entry = parse_key_line(line)
        first.setdefault(entry[:2], entry)
        (typed if entry[1] else zero).setdefault(entry[0], entry)
    base_zero = dict(zero)
    left, new = [], []
    for line in more:
        entry = parse_key_line(line)
        if entry[:2] in first:
            left.append(("same", tag(entry), tag(first[entry[:2]])))
        else:
            first[entry[:2]] = entry
            new.append(entry)
    if everything:
        kept = new
    else:
        kept, last = [], []
        for entry in new:
            key, kind, _ = entry
            by = holder(base_zero, key) if 1 <= kind <= 4 else None
            if kind > LAST_TYPE:
                left.append(("type", tag(entry), None))
            elif kind == 0:
                last.append(entry)
            elif by:
                left.append(("typed", tag(entry), tag(by)))
            else:
                kept.append(entry)
                typed.setdefault(key, entry)
        for entry in last:
            by = holder(typed, entry[0]) or holder(zero, entry[0])
            if by:
                left.append(("zero", tag(entry), tag(by)))
            else:
                kept.append(entry)
                zero.setdefault(entry[0], entry)
    return (list(base) + ["%016X:%d:%s" % entry for entry in kept], left)


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
    ap = argparse.ArgumentParser(
        prog="rekey_keystore.py", description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("action", choices=("keys", "raw", "verify", "merge"))
    ap.add_argument("paths", nargs="+", metavar="PATH",
                    help="keys, raw: IN and OUT; verify: the keystores; "
                         "merge: the key lists, then OUT")
    ap.add_argument("--all", action="store_true",
                    help="merge: leave out only a code that the list has")
    ap.add_argument("--new-only", action="store_true",
                    help="merge: OUT has the new codes alone, without the "
                         "base list's lines")
    ap.add_argument("--slot", type=int, default=1, metavar="N",
                    help="the enclave's key slot (default 1)")
    ap.add_argument("--iv", default=None, metavar="TEXT",
                    help="the stored IV: 16 bytes, ASCII or 0x and hex")
    ap.add_argument("--enclave", default=None, metavar="FILE",
                    help="a device's enclave.keys, for its key and not the "
                         "built-in one")
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
        elif args.action == "merge":
            if len(args.paths) < 3:
                raise ValueError("merge needs a base list, one list more at "
                                 "least, and an output path")
            *srcs, dst = (pathlib.Path(p) for p in args.paths)
            if dst.exists() and any(dst.samefile(p) for p in srcs):
                raise ValueError(f"{dst} is one of the lists: the new list "
                                 "goes to another file")
            base = read_key_list(srcs[0])
            more = [k for p in srcs[1:] for k in read_key_list(p)]
            lines, left = merge(base, more, args.all)
            if args.new_only:
                lines = lines[len(base):]
            dst.write_text("".join(k + "\n" for k in lines))
            keys = len({k.split(":")[0].upper() for k in lines})
            if args.new_only:
                print(f"{dst}: {len(lines)} lines, the new ones alone (none "
                      f"of the {len(base)} of {srcs[0]}), {keys} keys")
            else:
                print(f"{dst}: {len(lines)} lines ({len(base)} of {srcs[0]}, "
                      f"{len(lines) - len(base)} new), {keys} keys")
            for why, text in LEFT_OUT:
                rows = [row for row in left if row[0] == why]
                if rows:
                    print(f"left out, {len(rows)}: {text}")
                for _, line, by in rows:
                    print(f"  {line}" + (f"  ({by})" if by else ""))
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
