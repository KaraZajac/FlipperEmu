#!/usr/bin/env python3
# Copyright (c) 2026, Kara Zajac
# SPDX-License-Identifier: BSD-3-Clause
"""The secure enclave of an emulated Flipper, from the host's side.

A Flipper's "secure enclave" is the key store of ST's FUS on the radio core
plus the AES peripheral: the firmware names a key slot, and encrypts or
decrypts with a key it can never read (docs/ARCHITECTURE.md). An emulated
Flipper keeps those keys in a text file, devices/NAME/enclave.keys. This
reads and writes that file and does with a key what the firmware's enclave
does, so that files can be made for an emulated Flipper, or read back,
without booting it.

    slots   0 the master key (it only decrypts keys being stored), 1-10 the
            factory keys, 11 the device's own key (the firmware makes it on
            first use), 12-100 whatever apps store
    keys    FlipperEmu's factory keys are public: key N is
            SHA-256("FlipperEmu enclave key N"). What an emulated Flipper
            encrypts is not secret, and what a real Flipper encrypted cannot
            be read with them.

    fe_enclave.py show FILE
    fe_enclave.py encrypt FILE SLOT IV [TEXT]   what `crypto encrypt SLOT IV` prints (the
                                                firmware pads the last block with what
                                                its heap holds, this with zeros: a TEXT
                                                of 15, 31, 47... characters has no padding)
    fe_enclave.py decrypt FILE SLOT IV HEX      what `crypto decrypt SLOT IV` prints
    fe_enclave.py wrap FILE KEY [IV]            KEY (hex), encrypted under the master
                                                key for `crypto store_key N encrypted`
    fe_enclave.py reset FILE                    the factory keys again, nothing else
    fe_enclave.py blank FILE                    no keys at all: "Secure Enclave is
                                                damaged", until keys are stored

The emulator reads the file when it starts and writes all of it when the
firmware stores a key: change it only while the device is stopped.
"""
import hashlib
import os
import pathlib
import sys
import tempfile

SLOTS = 101                 # 0..100
FACTORY_KEYS = 10           # slots 1..10
UNIQUE_SLOT = 11            # the device's own key
TYPES = ("master", "simple", "encrypted")

HEADER = ("# STM32WB55 FUS customer key storage (AN5185): INDEX TYPE BITS KEY\n"
          "# Index 0 is the master key; the others follow in the order they were\n"
          "# stored, without gaps. An encrypted key is kept decrypted.\n")


def default_key(index: int) -> bytes:
    """FlipperEmu's key for slot 0..10 (qemu/hw/arm/flipper/flipper_enclave.c)."""
    return hashlib.sha256(f"FlipperEmu enclave key {index}".encode()).digest()


def factory_store() -> dict:
    """The enclave of a new emulated Flipper: {slot: (type, key)}."""
    return {i: ("simple" if i else "master", default_key(i))
            for i in range(FACTORY_KEYS + 1)}


def read_store(path) -> dict:
    """{slot: (type, key)} from a key store file; ValueError if it is not one."""
    keys = {}
    for n, line in enumerate(pathlib.Path(path).read_text().splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        where = f"{path}:{n}"
        fields = line.split()
        if len(fields) != 4 or not fields[0].isdigit() or not fields[2].isdigit():
            raise ValueError(f"{where}: expected INDEX TYPE BITS KEY")
        index, kind, bits, hexkey = int(fields[0]), fields[1], int(fields[2]), fields[3]
        if kind not in TYPES:
            raise ValueError(f"{where}: key type '{kind}': expected master, simple "
                             "or encrypted")
        if index >= SLOTS:
            raise ValueError(f"{where}: index {index}: the FUS holds keys 0 to "
                             f"{SLOTS - 1}")
        if (index == 0) != (kind == "master"):
            raise ValueError(f"{where}: index 0 is the master key, and only that one")
        if bits not in (128, 256):
            raise ValueError(f"{where}: a key has 128 or 256 bits, not {bits}")
        try:
            key = bytes.fromhex(hexkey)
        except ValueError:
            key = b""
        if len(hexkey) != bits // 4 or len(key) != bits // 8:
            raise ValueError(f"{where}: a {bits}-bit key is {bits // 4} hex digits")
        if index in keys:
            raise ValueError(f"{where}: index {index} is there twice")
        keys[index] = (kind, key)
    for index in sorted(keys):
        if index > 1 and index - 1 not in keys:
            raise ValueError(f"{path}: key {index} without a key {index - 1}: the "
                             "FUS stores them in order")
    return keys


def write_store(path, keys: dict):
    """Write a key store file the way the emulator does: whole, owner only."""
    path = pathlib.Path(path)
    text = HEADER + "".join(f"{i} {keys[i][0]} {len(keys[i][1]) * 8} {keys[i][1].hex()}\n"
                            for i in sorted(keys))
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        os.unlink(tmp)
        raise


def describe(index: int, kind: str, key: bytes) -> str:
    """What a slot is for, in a few words."""
    ours = index <= FACTORY_KEYS and key == default_key(index)
    if index == 0:
        return "master key" + (" (FlipperEmu's)" if ours else "")
    if index <= FACTORY_KEYS:
        return "factory key" + (" (FlipperEmu's)" if ours else
                                ", not FlipperEmu's: fails the firmware's check")
    if index == UNIQUE_SLOT:
        return "the device's own key"
    return "stored by an app or the CLI"


def valid_factory_keys(keys: dict) -> int:
    """How many of slots 1..10 pass the firmware's self-test in the emulator."""
    return sum(1 for i in range(1, FACTORY_KEYS + 1)
               if keys.get(i, (None, b""))[1] == default_key(i))


# ------------------------------------------------------------------- AES
# FIPS 197, as plainly as it is written there: this is for a handful of
# blocks, and saves the scripts a dependency.

def _xtime(a):
    a <<= 1
    return a ^ 0x11B if a & 0x100 else a


def _make_sbox():
    sbox, p, q = [0] * 256, 1, 1
    while True:
        p = (p ^ _xtime(p)) & 0xFF                      # p * 3
        q ^= q << 1                                     # q / 3
        q ^= q << 2
        q ^= q << 4
        q &= 0xFF
        if q & 0x80:
            q ^= 0x09
        x = q
        for shift in (1, 2, 3, 4):
            x ^= ((q << shift) | (q >> (8 - shift))) & 0xFF
        sbox[p] = x ^ 0x63
        if p == 1:
            break
    sbox[0] = 0x63
    return sbox


_SBOX = _make_sbox()
_INV = [0] * 256
for _i, _v in enumerate(_SBOX):
    _INV[_v] = _i


def _mix(s):
    out = []
    for c in range(0, 16, 4):
        a = s[c:c + 4]
        t = a[0] ^ a[1] ^ a[2] ^ a[3]
        out += [a[i] ^ t ^ _xtime(a[i] ^ a[(i + 1) % 4]) for i in range(4)]
    return out


def _unmix(s):
    out = []
    for c in range(0, 16, 4):
        a = s[c:c + 4]
        u = _xtime(_xtime(a[0] ^ a[2]))
        v = _xtime(_xtime(a[1] ^ a[3]))
        out += [a[0] ^ u, a[1] ^ v, a[2] ^ u, a[3] ^ v]
    return _mix(out)


class AES:
    """AES-128/192/256 on single 16-byte blocks."""

    def __init__(self, key: bytes):
        if len(key) not in (16, 24, 32):
            raise ValueError("an AES key is 16, 24 or 32 bytes")
        nk = len(key) // 4
        w = [list(key[4 * i:4 * i + 4]) for i in range(nk)]
        rcon = 1
        for i in range(nk, 4 * (nk + 7)):
            t = w[i - 1]
            if i % nk == 0:
                t = [_SBOX[b] for b in t[1:] + t[:1]]
                t[0] ^= rcon
                rcon = _xtime(rcon)
            elif nk > 6 and i % nk == 4:
                t = [_SBOX[b] for b in t]
            w.append([a ^ b for a, b in zip(w[i - nk], t)])
        self.rk = [sum(w[r:r + 4], []) for r in range(0, len(w), 4)]

    def encrypt(self, block: bytes) -> bytes:
        s = [a ^ b for a, b in zip(block, self.rk[0])]
        last = len(self.rk) - 1
        for r in range(1, last + 1):
            s = [_SBOX[b] for b in s]
            s = [s[(i + 4 * (i % 4)) % 16] for i in range(16)]      # ShiftRows
            if r != last:
                s = _mix(s)
            s = [a ^ b for a, b in zip(s, self.rk[r])]
        return bytes(s)

    def decrypt(self, block: bytes) -> bytes:
        s = [a ^ b for a, b in zip(block, self.rk[-1])]
        for r in range(len(self.rk) - 2, -1, -1):
            s = [s[(i - 4 * (i % 4)) % 16] for i in range(16)]      # InvShiftRows
            s = [_INV[b] for b in s]
            s = [a ^ b for a, b in zip(s, self.rk[r])]
            if r:
                s = _unmix(s)
        return bytes(s)


def _xor(a, b):
    return bytes(x ^ y for x, y in zip(a, b))


def cbc_encrypt(key: bytes, iv: bytes, data: bytes) -> bytes:
    aes, out = AES(key), b""
    for i in range(0, len(data), 16):
        iv = aes.encrypt(_xor(data[i:i + 16], iv))
        out += iv
    return out


def cbc_decrypt(key: bytes, iv: bytes, data: bytes) -> bytes:
    aes, out = AES(key), b""
    for i in range(0, len(data), 16):
        out += _xor(aes.decrypt(data[i:i + 16]), iv)
        iv = data[i:i + 16]
    return out


def ctr(key: bytes, nonce: bytes, counter: int, data: bytes) -> bytes:
    """AES-CTR with a 12-byte nonce and a 32-bit big-endian block counter."""
    aes, out = AES(key), b""
    for i in range(0, len(data), 16):
        pad = aes.encrypt(nonce + ((counter + i // 16) & 0xFFFFFFFF).to_bytes(4, "big"))
        out += _xor(data[i:i + 16], pad)
    return out


# ------------------------------------------------- what the firmware does

def _words(data: bytes) -> bytes:
    """The firmware hands AES1 its buffers as little-endian words, AES1 takes
    a word as four bytes of the block, first byte on top: every group of
    four bytes is reversed, on the way in and on the way out."""
    return bytes(data[i ^ 3] for i in range(len(data)))


def _aes256(key: bytes) -> bytes:
    # The firmware sets KEYSIZE to 256 bits whatever the slot holds, and a
    # 128-bit key is in KEYR3..0: the lower half of the 256-bit key AES1
    # then uses (stm32wb55_fus.c: what a real FUS does there is not known).
    return key if len(key) == 32 else bytes(16) + key


def encrypt(key: bytes, iv: bytes, data: bytes) -> bytes:
    """furi_hal_crypto_enclave_load_key(slot, iv) + furi_hal_crypto_encrypt()
    with the slot's key: AES-256-CBC over byte-reversed words. `data` is
    whole 16-byte blocks."""
    if len(iv) != 16 or len(data) % 16:
        raise ValueError("the IV is 16 bytes, the data whole 16-byte blocks")
    return _words(cbc_encrypt(_aes256(key), _words(iv), _words(data)))


def decrypt(key: bytes, iv: bytes, data: bytes) -> bytes:
    """... + furi_hal_crypto_decrypt()."""
    if len(iv) != 16 or len(data) % 16:
        raise ValueError("the IV is 16 bytes, the data whole 16-byte blocks")
    return _words(cbc_decrypt(_aes256(key), _words(iv), _words(data)))


def cli_encrypt(key: bytes, iv: bytes, text: str) -> bytes:
    """What `crypto encrypt SLOT IV` makes of typed text: a C string, padded
    with zeros to whole blocks. The firmware pads with whatever follows the
    string in its heap (crypto_cli.c reserves the room and does not clear
    it): that is zeros often, not always. A text of 15, 31, 47... characters
    fills its last block and leaves nothing to differ."""
    data = text.encode() + b"\0"
    return encrypt(key, iv, data + bytes(-len(data) % 16))


def wrap(master: bytes, key: bytes, iv: bytes) -> bytes:
    """`key` as FUS_STORE_USR_KEY wants an encrypted one: AES-GCM under the
    master key, without the tag (so CTR from counter 2), then the 12-byte IV.
    The hex of this is the last argument of `crypto store_key N encrypted`."""
    if len(key) not in (16, 32) or len(iv) != 12:
        raise ValueError("a key is 16 or 32 bytes, its IV 12")
    return ctr(master, iv, 2, key) + iv


# ------------------------------------------------------------------- CLI

def show(path) -> str:
    keys = read_store(path)
    lines = [f"{'slot':>4}  {'type':9} {'bits':>4}  key"]
    for i in sorted(keys):
        kind, key = keys[i]
        lines.append(f"{i:>4}  {kind:9} {len(key) * 8:>4}  {key.hex()}  "
                     f"{describe(i, kind, key)}")
    if not keys:
        lines.append("      (no keys)")
    valid = valid_factory_keys(keys)
    lines.append(f"factory keys the firmware's check accepts: {valid} of {FACTORY_KEYS}"
                 + ("" if valid == FACTORY_KEYS else
                    "  (\"Secure Enclave is damaged\" at start)"))
    return "\n".join(lines)


def slot_key(path, slot: str) -> bytes:
    keys = read_store(path)
    if not slot.isdigit() or not 1 <= int(slot) <= SLOTS - 1:
        raise ValueError(f"slot {slot}: expected 1 to {SLOTS - 1}")
    if int(slot) not in keys:
        raise ValueError(f"slot {slot} is empty")
    return keys[int(slot)][1]


def parse_hex(text: str, what: str, sizes) -> bytes:
    """Hex from the command line; `sizes` are the lengths it may have in
    bytes, or None for whole 16-byte blocks."""
    try:
        data = bytes.fromhex(text)
    except ValueError:
        raise ValueError(f"{what}: not hex: {text!r}") from None
    if sizes is None and (not data or len(data) % 16):
        raise ValueError(f"{what}: whole 16-byte blocks in hex, not {len(data)} bytes")
    if sizes is not None and len(data) not in sizes:
        raise ValueError(f"{what}: {' or '.join(map(str, sizes))} bytes in hex, "
                         f"not {len(data)}")
    return data


def main(argv, usage=__doc__) -> int:
    if len(argv) < 3:
        print(usage)
        return 2
    action, path, rest = argv[1], argv[2], argv[3:]
    try:
        if action == "show" and not rest:
            print(show(path))
        elif action == "encrypt" and len(rest) in (2, 3):
            text = rest[2] if len(rest) == 3 else sys.stdin.read()
            print(cli_encrypt(slot_key(path, rest[0]), parse_hex(rest[1], "IV", (16,)),
                              text).hex())
        elif action == "decrypt" and len(rest) == 3:
            key, iv = slot_key(path, rest[0]), parse_hex(rest[1], "IV", (16,))
            plain = decrypt(key, iv, parse_hex(rest[2], "data", None))
            print(plain.split(b"\0")[0].decode(errors="replace"))
        elif action == "wrap" and len(rest) in (1, 2):
            keys = read_store(path)
            if 0 not in keys:
                raise ValueError("no master key in this enclave")
            iv = parse_hex(rest[1], "IV", (12,)) if len(rest) == 2 else os.urandom(12)
            key = parse_hex(rest[0], "key", (16, 32))
            print(wrap(keys[0][1], key, iv).hex())
        elif action in ("reset", "blank") and not rest:
            write_store(path, factory_store() if action == "reset" else {})
            print(show(path))
        else:
            print(usage)
            return 2
    except (ValueError, OSError) as e:
        print(f"enclave: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
