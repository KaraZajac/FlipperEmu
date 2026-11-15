#!/usr/bin/env python3
# Copyright (c) 2026, Kara Zajac
# SPDX-License-Identifier: BSD-3-Clause
"""A reader in front of a Flipper that emulates a card: a program on the
NFC link (docs/RADIO_LINK.md, "A reader on the link").

    ./flipperemu nfc --reader classic CARD.nfc   # read it with CARD's keys
    ./flipperemu nfc --reader iso-dep            # an ISO14443-4A card: its ATS
    ./flipperemu nfc --reader t4t                # a Type 4 tag: its NDEF message
    ./flipperemu nfc --reader felica             # a FeliCa Lite-S: its blocks
    ./flipperemu nfc --reader felica CARD.nfc    # ... with CARD's card key
    ./flipperemu nfc --reader ulc CARD.nfc       # a MIFARE Ultralight C, with
                                                 # the key in CARD's pages
    scripts/fe_nfc_reader.py devices/NAME/nfc.sock classic CARD.nfc -o OUT.nfc

The emulator's chip model is the air: `reader on` is this reader's field
around the Flipper, `reader activate` the WUPA, anticollision and SELECT that
the chip answers by itself, and `reader send` a frame whose answer comes back
as the firmware sent it, bits and parity (MIFARE Classic's encrypted ones
too, which the firmware modulates itself in the chip's transparent mode).

`classic` is a MIFARE Classic reader: it authenticates to each sector with
the keys of the card file it is given (key A, then key B, as a reader that
knows the card would), reads every block it may, and writes what it read as
a Flipper .nfc file.  Crypto1 is written from its published description
(Garcia et al., "Dismantling MIFARE Classic", ESORICS 2008), as the card
model's is.

`iso-dep` activates an ISO14443-4A card (RATS) and writes what it answered,
and `t4t` reads an NFC Forum Type 4 tag through ISO14443-4's blocks: the NDEF
application, the capability container, the NDEF file.  `felica` polls for
a FeliCa card and reads a FeliCa Lite-S block by block, without its card key.
"""
import argparse
import os
import pathlib
import socket
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import fe_nfc  # noqa: E402


def crc_a(data):
    crc = 0x6363
    for b in data:
        b ^= crc & 0xFF
        b = (b ^ (b << 4)) & 0xFF
        crc = (crc >> 8) ^ (b << 8) ^ (b << 3) ^ (b >> 4)
    return bytes((crc & 0xFF, (crc >> 8) & 0xFF))


def odd(byte):
    """The odd parity bit of a byte"""
    return 1 - bin(byte).count("1") % 2


class Silence(Exception):
    """No answer to a frame"""


class ReaderLink:
    """The reader's side of the NFC link"""

    def __init__(self, path, timeout=20.0):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(timeout)
        self.sock.connect(str(path))
        self.buf = b""
        self.log = None                 # a function of (sent, answer) lines
        # the emulator greets the one program it talks to; a second one is
        # connected and hears nothing until the first has gone
        self.sock.settimeout(3.0)
        try:
            while b"\n" not in self.buf:
                data = self.sock.recv(65536)
                if not data:
                    raise ConnectionError("the emulator has gone")
                self.buf += data
        except socket.timeout:
            self.sock.close()
            raise ConnectionError("the device's NFC link is taken by another "
                                  "program (a card of `nfc --link`?)") from None
        self.sock.settimeout(timeout)
        self.sock.sendall(b"hello fe_nfc_reader\n")

    def close(self):
        try:
            self.sock.sendall(b"reader off\n")
        except OSError:
            pass
        self.sock.close()

    def line(self):
        """The next line that is the reader's (the card's side of the link
        goes on meanwhile: `field`, `hello`)"""
        while True:
            while b"\n" not in self.buf:
                data = self.sock.recv(65536)
                if not data:
                    raise ConnectionError("the emulator has gone")
                self.buf += data
            raw, self.buf = self.buf.split(b"\n", 1)
            words = raw.decode(errors="replace").split()
            if words and words[0] == "reader":
                return words[1:]

    def ask(self, text):
        self.sock.sendall(f"reader {text}\n".encode())
        words = self.line()
        if words and words[0] == "error":
            raise RuntimeError("the NFC link: " + " ".join(words[1:]))
        if words and words[0] == "busy":
            raise RuntimeError("another reader is in front of the Flipper")
        return words

    def on(self):
        self.ask("on")

    def saved(self, path):
        """Tell the emulator where what was read is kept (`status` shows
        it)"""
        self.ask(f"saved {path}")

    def off(self):
        self.ask("off")

    def activate(self):
        """WUPA, anticollision, SELECT: (uid, atqa, sak), or None when the
        Flipper is not an ISO14443A card now"""
        words = self.ask("activate")
        if len(words) < 5 or words[1] != "a":
            return None
        return bytes.fromhex(words[2]), bytes.fromhex(words[3]), int(words[4], 16)

    def poll_f(self):
        """FeliCa's polling: (IDm, PMm, system code), or None when the
        Flipper is not a FeliCa card now"""
        words = self.ask("activate")
        if len(words) < 5 or words[1] != "f":
            return None
        return tuple(bytes.fromhex(w) for w in words[2:5])

    def send(self, data, nbits=None, parity=None):
        """A frame to the Flipper: (bits, data, parity bits) of its answer.
        parity: a list of bits, one for each full byte; None: odd"""
        nbits = len(data) * 8 if nbits is None else nbits
        text = f"send {nbits} {bytes(data).hex()}"
        if parity is not None:
            packed = bytearray((len(parity) + 7) // 8)
            for i, bit in enumerate(parity):
                packed[i // 8] |= bit << (i % 8)
            text += " " + packed.hex()
        words = self.ask(text)
        if self.log:
            self.log(text, " ".join(words))
        if not words or words[0] != "answer":
            raise Silence()
        bits = int(words[1])
        got = bytes.fromhex(words[2])
        packed = bytes.fromhex(words[3]) if len(words) > 3 else b""
        par = [(packed[i // 8] >> (i % 8)) & 1 for i in range(bits // 8)
               if i // 8 < len(packed)]
        return bits, got, par


# ----------------------------------------------------------------- Crypto1

TAPS = sum(1 << i for i in (0, 5, 9, 10, 12, 14, 15, 17, 19, 24, 25, 27, 29,
                            35, 39, 41, 42, 43))


def _fa(y0, y1, y2, y3):
    return ((y0 | y1) ^ (y0 & y3)) ^ (y2 & ((y0 ^ y1) | y3))


def _fb(y0, y1, y2, y3):
    return ((y0 & y1) | y2) ^ ((y0 ^ y1) & (y2 | y3))


def _fc(y0, y1, y2, y3, y4):
    return (y0 | ((y1 | y4) & (y3 ^ y4))) ^ ((y0 ^ (y1 & y3)) & ((y2 ^ y3) | (y1 & y4)))


class Crypto1:
    """The cipher's 48-bit register, bits in transmission order"""

    def __init__(self, key):
        self.x = int.from_bytes(key, "little")

    def ks(self):
        x = self.x
        b = lambda i: (x >> i) & 1
        return _fc(_fa(b(9), b(11), b(13), b(15)), _fb(b(17), b(19), b(21), b(23)),
                   _fb(b(25), b(27), b(29), b(31)), _fa(b(33), b(35), b(37), b(39)),
                   _fb(b(41), b(43), b(45), b(47)))

    def clock(self, bit=0):
        fb = (bin(self.x & TAPS).count("1") & 1) ^ bit
        self.x = (self.x >> 1) | (fb << 47)

    def crypt(self, data, feed=False):
        """Bytes through the keystream: (the other text, each byte's
        parity bit as it is on the air).  feed: the plain bits go into the
        register (a nonce that is sent)"""
        out, par = bytearray(), []
        for byte in data:
            v = 0
            for b in range(8):
                bit = (byte >> b) & 1
                v |= (bit ^ self.ks()) << b
                self.clock(bit if feed else 0)
            out.append(v)
            par.append(odd(byte) ^ self.ks())
        return bytes(out), par

    def decrypt(self, data, feed_with=None):
        """Bytes from the card.  feed_with: the bytes that go into the
        register XORed with the plain ones (a nested nonce: the UID)"""
        out = bytearray()
        for i, byte in enumerate(data):
            v = 0
            for b in range(8):
                bit = ((byte >> b) & 1) ^ self.ks()
                v |= bit << b
                self.clock(bit ^ ((feed_with[i] >> b) & 1) if feed_with else 0)
            out.append(v)
        return bytes(out)

    def nibble(self, value):
        """Four bits from the card (its ACK or NAK)"""
        v = 0
        for b in range(4):
            v |= (((value >> b) & 1) ^ self.ks()) << b
            self.clock()
        return v


def prng(x, n):
    """The card's nonce generator, n steps on (a word in transmission
    order)"""
    for _ in range(n):
        x = x >> 1 | (((x >> 16) ^ (x >> 18) ^ (x >> 19) ^ (x >> 21)) & 1) << 31
    return x


# --------------------------------------------------------------------- DES

# (FIPS 46-3's tables, as the card model has them: a reader of MIFARE
# Ultralight C and of FeliCa Lite-S needs two-key triple DES in CBC, and
# Python's own library has no DES)
DES_IP = (
    58, 50, 42, 34, 26, 18, 10, 2, 60, 52, 44, 36, 28, 20, 12, 4, 62, 54,
    46, 38, 30, 22, 14, 6, 64, 56, 48, 40, 32, 24, 16, 8, 57, 49, 41, 33,
    25, 17, 9, 1, 59, 51, 43, 35, 27, 19, 11, 3, 61, 53, 45, 37, 29, 21,
    13, 5, 63, 55, 47, 39, 31, 23, 15, 7)
DES_FP = (
    40, 8, 48, 16, 56, 24, 64, 32, 39, 7, 47, 15, 55, 23, 63, 31, 38, 6,
    46, 14, 54, 22, 62, 30, 37, 5, 45, 13, 53, 21, 61, 29, 36, 4, 44, 12,
    52, 20, 60, 28, 35, 3, 43, 11, 51, 19, 59, 27, 34, 2, 42, 10, 50, 18,
    58, 26, 33, 1, 41, 9, 49, 17, 57, 25)
DES_E = (
    32, 1, 2, 3, 4, 5, 4, 5, 6, 7, 8, 9, 8, 9, 10, 11, 12, 13, 12, 13, 14,
    15, 16, 17, 16, 17, 18, 19, 20, 21, 20, 21, 22, 23, 24, 25, 24, 25,
    26, 27, 28, 29, 28, 29, 30, 31, 32, 1)
DES_P = (
    16, 7, 20, 21, 29, 12, 28, 17, 1, 15, 23, 26, 5, 18, 31, 10, 2, 8, 24,
    14, 32, 27, 3, 9, 19, 13, 30, 6, 22, 11, 4, 25)
DES_PC1 = (
    57, 49, 41, 33, 25, 17, 9, 1, 58, 50, 42, 34, 26, 18, 10, 2, 59, 51,
    43, 35, 27, 19, 11, 3, 60, 52, 44, 36, 63, 55, 47, 39, 31, 23, 15, 7,
    62, 54, 46, 38, 30, 22, 14, 6, 61, 53, 45, 37, 29, 21, 13, 5, 28, 20,
    12, 4)
DES_PC2 = (
    14, 17, 11, 24, 1, 5, 3, 28, 15, 6, 21, 10, 23, 19, 12, 4, 26, 8, 16,
    7, 27, 20, 13, 2, 41, 52, 31, 37, 47, 55, 30, 40, 51, 45, 33, 48, 44,
    49, 39, 56, 34, 53, 46, 42, 50, 36, 29, 32)
DES_SHIFT = (
    1, 1, 2, 2, 2, 2, 2, 2, 1, 2, 2, 2, 2, 2, 2, 1)
DES_S = (
    14, 4, 13, 1, 2, 15, 11, 8, 3, 10, 6, 12, 5, 9, 0, 7, 0, 15, 7, 4, 14,
    2, 13, 1, 10, 6, 12, 11, 9, 5, 3, 8, 4, 1, 14, 8, 13, 6, 2, 11, 15,
    12, 9, 7, 3, 10, 5, 0, 15, 12, 8, 2, 4, 9, 1, 7, 5, 11, 3, 14, 10, 0,
    6, 13, 15, 1, 8, 14, 6, 11, 3, 4, 9, 7, 2, 13, 12, 0, 5, 10, 3, 13, 4,
    7, 15, 2, 8, 14, 12, 0, 1, 10, 6, 9, 11, 5, 0, 14, 7, 11, 10, 4, 13,
    1, 5, 8, 12, 6, 9, 3, 2, 15, 13, 8, 10, 1, 3, 15, 4, 2, 11, 6, 7, 12,
    0, 5, 14, 9, 10, 0, 9, 14, 6, 3, 15, 5, 1, 13, 12, 7, 11, 4, 2, 8, 13,
    7, 0, 9, 3, 4, 6, 10, 2, 8, 5, 14, 12, 11, 15, 1, 13, 6, 4, 9, 8, 15,
    3, 0, 11, 1, 2, 12, 5, 10, 14, 7, 1, 10, 13, 0, 6, 9, 8, 7, 4, 15, 14,
    3, 11, 5, 2, 12, 7, 13, 14, 3, 0, 6, 9, 10, 1, 2, 8, 5, 11, 12, 4, 15,
    13, 8, 11, 5, 6, 15, 0, 3, 4, 7, 2, 12, 1, 10, 14, 9, 10, 6, 9, 0, 12,
    11, 7, 13, 15, 1, 3, 14, 5, 2, 8, 4, 3, 15, 0, 6, 10, 1, 13, 8, 9, 4,
    5, 11, 12, 7, 2, 14, 2, 12, 4, 1, 7, 10, 11, 6, 8, 5, 3, 15, 13, 0,
    14, 9, 14, 11, 2, 12, 4, 7, 13, 1, 5, 0, 15, 10, 3, 9, 8, 6, 4, 2, 1,
    11, 10, 13, 7, 8, 15, 9, 12, 5, 6, 3, 0, 14, 11, 8, 12, 7, 1, 14, 2,
    13, 6, 15, 0, 9, 10, 4, 5, 3, 12, 1, 10, 15, 9, 2, 6, 8, 0, 13, 3, 4,
    14, 7, 5, 11, 10, 15, 4, 2, 7, 12, 9, 5, 6, 1, 13, 14, 0, 11, 3, 8, 9,
    14, 15, 5, 2, 8, 12, 3, 7, 0, 4, 10, 1, 13, 11, 6, 4, 3, 2, 12, 9, 5,
    15, 10, 11, 14, 1, 7, 6, 0, 8, 13, 4, 11, 2, 14, 15, 0, 8, 13, 3, 12,
    9, 7, 5, 10, 6, 1, 13, 0, 11, 7, 4, 9, 1, 10, 14, 3, 5, 12, 2, 15, 8,
    6, 1, 4, 11, 13, 12, 3, 7, 14, 10, 15, 6, 8, 0, 5, 9, 2, 6, 11, 13, 8,
    1, 4, 10, 7, 9, 5, 0, 15, 14, 2, 3, 12, 13, 2, 8, 4, 6, 15, 11, 1, 10,
    9, 3, 14, 5, 0, 12, 7, 1, 15, 13, 8, 10, 3, 7, 4, 12, 5, 6, 11, 0, 14,
    9, 2, 7, 11, 4, 1, 9, 12, 14, 2, 0, 6, 10, 13, 15, 3, 5, 8, 2, 1, 14,
    7, 4, 10, 8, 13, 15, 12, 9, 0, 3, 5, 6, 11)


def _permute(value, table, width):
    out = 0
    for at in table:
        out = out << 1 | (value >> (width - at)) & 1
    return out


def des_block(key, block, decrypt=False):
    """One block of 8 bytes through DES with a key of 8"""
    k = _permute(int.from_bytes(key, "big"), DES_PC1, 64)
    c, d, sub = k >> 28, k & 0x0FFFFFFF, []
    for shift in DES_SHIFT:
        c = (c << shift | c >> (28 - shift)) & 0x0FFFFFFF
        d = (d << shift | d >> (28 - shift)) & 0x0FFFFFFF
        sub.append(_permute(c << 28 | d, DES_PC2, 56))
    b = _permute(int.from_bytes(block, "big"), DES_IP, 64)
    left, right = b >> 32, b & 0xFFFFFFFF
    for i in range(16):
        x = _permute(right, DES_E, 32) ^ sub[15 - i if decrypt else i]
        f = 0
        for s in range(8):
            six = x >> (42 - 6 * s) & 0x3F
            row, col = six >> 4 & 2 | six & 1, six >> 1 & 0xF
            f = f << 4 | DES_S[s * 64 + row * 16 + col]
        left, right = right, left ^ _permute(f, DES_P, 32)
    return _permute(right << 32 | left, DES_FP, 64).to_bytes(8, "big")


def tdes2_cbc(key, iv, data, decrypt=False):
    """Triple DES with two keys (16 bytes: K1, K2, and K1 again) in CBC
    over whole blocks: (the other text, the IV for what follows)"""
    k1, k2, out = key[:8], key[8:16], b""
    for at in range(0, len(data), 8):
        block = data[at:at + 8]
        if decrypt:
            x = des_block(k1, des_block(k2, des_block(k1, block, True)), True)
            out += bytes(a ^ b for a, b in zip(x, iv))
            iv = block
        else:
            x = bytes(a ^ b for a, b in zip(block, iv))
            iv = des_block(k1, des_block(k2, des_block(k1, x), True))
            out += iv
    return out, iv


# ----------------------------------------------------------------- Classic

class AuthFailed(Exception):
    pass


class Classic:
    """A MIFARE Classic reader's session with the card on the link"""

    TRIES = 4                           # of an authentication, of a read

    def __init__(self, link):
        self.link = link
        self.uid = None
        self.c = None                   # the cipher, once authenticated

    def select(self):
        self.c = None
        target = self.link.activate()
        if not target:
            return None
        self.uid, self.atqa, self.sak = target
        return target

    def restart(self):
        """The field off and on, and the card selected again: what a
        reader does with a card that has stopped answering.  (An emulated
        CPU on a busy computer can be too late with an answer, or miss a
        frame: the card is then somewhere in a command that the reader has
        given up.)"""
        self.c = None
        self.link.off()
        time.sleep(0.02)
        self.link.on()
        time.sleep(0.02)
        return self.select()

    def login(self, block, key, key_b=False):
        """auth() with a new start: after silence the field goes off and
        on and the key is tried again, TRIES times in all.  A card that does
        not take the key is silent each time"""
        for attempt in range(self.TRIES):
            try:
                return self.auth(block, key, key_b)
            except AuthFailed:
                if not self.restart() or attempt + 1 == self.TRIES:
                    raise

    def auth(self, block, key, key_b=False):
        """Authenticate for a block's sector; nested when a session is
        open.  AuthFailed: the card does not take the key (it is silent, and
        wants a new SELECT)"""
        cmd = bytes((0x61 if key_b else 0x60, block))
        cmd += crc_a(cmd)
        cuid = self.uid[-4:]
        new = Crypto1(key)
        try:
            if self.c is None:
                bits, nt, _ = self.link.send(cmd)
                if bits != 32:
                    raise AuthFailed(f"a nonce of {bits} bits")
                for i in range(32):
                    new.clock(((cuid[i // 8] ^ nt[i // 8]) >> (i % 8)) & 1)
            else:
                enc, par = self.c.crypt(cmd)
                bits, got, _ = self.link.send(enc, parity=par)
                if bits != 32:
                    raise AuthFailed(f"a nested nonce of {bits} bits")
                nt = new.decrypt(got, feed_with=cuid)
            nr = os.urandom(4)
            ar = prng(int.from_bytes(nt, "little"), 64).to_bytes(4, "little")
            e1, p1 = new.crypt(nr, feed=True)
            e2, p2 = new.crypt(ar)
            bits, got, _ = self.link.send(e1 + e2, parity=p1 + p2)
            at = new.decrypt(got)
            want = prng(int.from_bytes(nt, "little"), 96).to_bytes(4, "little")
            if bits != 32 or at != want:
                raise AuthFailed("the card's answer is not the nonce's")
        except Silence:
            self.c = None
            raise AuthFailed("no answer") from None
        self.c = new

    def read(self, block):
        """A block: (its 16 bytes or None, whether the card refused it with
        a NAK).  A NAK ends the session, as on a card; so does silence or an
        answer that is none"""
        if not self.c:
            return None, False
        cmd = bytes((0x30, block))
        enc, par = self.c.crypt(cmd + crc_a(cmd))
        try:
            bits, got, _ = self.link.send(enc, parity=par)
        except Silence:
            self.c = None
            return None, False
        if bits == 4:
            self.c = None
            return None, True
        data = self.c.decrypt(got)
        if bits != 18 * 8 or crc_a(data[:16]) != data[16:]:
            self.c = None
            return None, False
        return data[:16], False

    def halt(self):
        if self.c:
            enc, par = self.c.crypt(b"\x50\x00" + crc_a(b"\x50\x00"))
            try:
                self.link.send(enc, parity=par)
            except Silence:
                pass
        self.c = None


def wait_target(link, tries=50):
    """The card that the Flipper emulates, activated: (uid, atqa, sak).  The
    firmware may start late: half a second a try"""
    for _ in range(tries):
        target = link.activate()
        if target:
            return target
        time.sleep(0.1)
    raise RuntimeError("the Flipper is not emulating a card (no answer to a WUPA)")


def again(link, read, tries=3):
    """read() from its start, with the field off and on before each new
    try, while the card stops answering in the middle (see Classic.restart)"""
    for attempt in range(tries):
        try:
            return read()
        except Silence:
            if attempt + 1 == tries:
                raise RuntimeError("the card stopped answering") from None
            link.off()
            time.sleep(0.02)
            link.on()
            time.sleep(0.02)


def sector_blocks(sector):
    first = sector * 4 if sector < 32 else 128 + (sector - 32) * 16
    return list(range(first, first + (4 if sector < 32 else 16)))


def read_classic(link, card_text, say=lambda text: None):
    """Read the emulated card with the keys of a card file's text: the
    .nfc text of what was read, and (blocks read, blocks in all)"""
    fields = fe_nfc.parse_nfc_text(card_text)
    kind = fields.get("Mifare Classic type", "1K").upper()
    sectors = {"MINI": 5, "1K": 16, "4K": 40}[kind]
    block = lambda n: bytes.fromhex(fields.get(f"Block {n}", "").replace("??", "00")
                                    .replace(" ", "")).ljust(16, b"\0")
    rd = Classic(link)
    rd.uid, rd.atqa, rd.sak = uid, atqa, sak = wait_target(link)
    say(f"a card: UID {uid.hex(' ').upper()}, ATQA {atqa.hex(' ').upper()}, "
        f"SAK {sak:02X}")
    got, keys = {}, {}
    for sector in range(sectors):
        blocks = sector_blocks(sector)
        trailer = block(blocks[-1])
        for key_b, key in ((False, trailer[:6]), (True, trailer[10:])):
            try:
                rd.login(blocks[0], key, key_b)
            except AuthFailed:          # (the field is new after it)
                continue
            keys[(sector, key_b)] = key
            for n in blocks:
                if n in got:
                    continue
                data, naks = None, 0
                for _ in range(rd.TRIES):
                    data, refused = rd.read(n)
                    naks += refused
                    # (a refusal counts when it comes twice: a card that
                    # lost its place on a busy computer says NAK as well)
                    if data is not None or naks == 2:
                        break
                    try:                # once more, from the field's start
                        rd.restart()
                        rd.login(blocks[0], key, key_b)
                    except AuthFailed:
                        break
                if data is not None:
                    got[n] = data
                elif not rd.c:          # in again for the sector's next block
                    try:
                        rd.restart()
                        rd.login(blocks[0], key, key_b)
                    except AuthFailed:
                        break
        say(f"sector {sector}: {sum(n in got for n in blocks)} of "
            f"{len(blocks)} blocks")
    rd.halt()
    total = sum(len(sector_blocks(s)) for s in range(sectors))
    up = lambda b: bytes(b).hex(" ").upper()
    out = ["Filetype: Flipper NFC device", "Version: 4",
           "Device type: Mifare Classic", f"UID: {up(uid)}",
           f"ATQA: {up(atqa[::-1])}", f"SAK: {sak:02X}",
           f"Mifare Classic type: {kind if kind != 'MINI' else 'MINI'}",
           "Data format version: 2"]
    for sector in range(sectors):
        blocks = sector_blocks(sector)
        for n in blocks:
            data = bytearray(got.get(n, b""))
            if n == blocks[-1] and data:
                # a trailer is read without its keys: the ones that opened
                # the sector are known
                for key_b, at in ((False, 0), (True, 10)):
                    key = keys.get((sector, key_b))
                    if key:
                        data[at:at + 6] = key
            text = up(data) if data else " ".join(["??"] * 16)
            out.append(f"Block {n}: {text}")
    return "\n".join(out) + "\n", (len(got), total)


# ---------------------------------------------------------- Ultralight C

def read_ulc(link, card_text, say=lambda text: None):
    """Read the emulated MIFARE Ultralight C with the key of a card file's
    text (its pages 44 to 47, as the firmware keeps it): the .nfc text of
    what was read, and (pages read, pages in all).  The key is shown to the
    card and the card's answer checked: AUTHENTICATE, two-key triple DES in
    CBC, each side's number turned by a byte"""
    fields = fe_nfc.parse_nfc_text(card_text)
    key = b"".join(bytes.fromhex(fields.get(f"Page {n}", "00 00 00 00")
                                 .replace(" ", "")) for n in range(44, 48))

    def frame(data):
        bits, got, _ = link.send(bytes(data) + crc_a(data))
        if bits == 4 or bits % 8 or len(got) < 3 or crc_a(got[:-2]) != got[-2:]:
            return None                 # (a NAK, or nothing whole)
        return got[:-2]

    def login():
        """-> whether the card took the key, and showed that it has it"""
        first = frame(b"\x1A\x00")
        if not first or len(first) != 9 or first[0] != 0xAF:
            return False
        rnd_b, iv = tdes2_cbc(key, bytes(8), first[1:], decrypt=True)
        rnd_a = os.urandom(8)
        mine, iv = tdes2_cbc(key, iv, rnd_a + rnd_b[1:] + rnd_b[:1])
        second = frame(b"\xAF" + mine)
        if not second or len(second) != 9 or second[0] != 0x00:
            return False
        back, _ = tdes2_cbc(key, iv, second[1:], decrypt=True)
        return back == rnd_a[1:] + rnd_a[:1]

    def once():
        target = wait_target(link)
        known = login()
        pages = {}
        for first in range(0, 44, 4):
            got = frame(bytes((0x30, first)))
            if not got or len(got) != 16:
                wait_target(link, 3)    # (a NAK put the card to sleep)
                if known:
                    login()
                continue
            for i in range(4):
                if first + i < 44:      # (a READ rolls over before the key)
                    pages[first + i] = got[4 * i:4 * i + 4]
        return target, known, pages

    target, known, pages = again(link, once)
    say("the card took the key, and answered with it" if known else
        "the card did not take the key: the pages that it gives to anyone")
    if known:                           # (the key itself is never read)
        pages.update({44 + i: key[4 * i:4 * i + 4] for i in range(4)})
    up = lambda b: bytes(b).hex(" ").upper()
    uid, atqa, sak = target
    keep = ("Signature", "Mifare version", "Counter", "Tearing")
    out = ["Filetype: Flipper NFC device", "Version: 4",
           "Device type: NTAG/Ultralight", f"UID: {up(uid)}",
           f"ATQA: {up(atqa[::-1])}", f"SAK: {sak:02X}",
           "Data format version: 2",
           "NTAG/Ultralight type: Mifare Ultralight C"]
    out += [f"{k}: {v}" for k, v in fields.items() if k.startswith(keep)]
    out += ["Pages total: 48", f"Pages read: {len(pages)}"]
    out += [f"Page {n}: " + (up(pages[n]) if n in pages else "00 00 00 00")
            for n in range(48)]
    out.append("Failed authentication attempts: 0")
    return "\n".join(out) + "\n", (len(pages), 48)


# ----------------------------------------------------------- ISO14443-4

class IsoDep:
    """ISO14443-4's block layer from the reader's side: RATS, then APDUs in
    I-blocks, chained either way, a card's waiting time extensions granted"""

    FSC = (16, 24, 32, 40, 48, 64, 96, 128, 256)

    def __init__(self, link):
        self.link = link
        self.block = 0
        self.fsc = 32

    def frame(self, data):
        """A frame with its CRC there, the answer without it back"""
        bits, got, _ = self.link.send(bytes(data) + crc_a(data))
        if bits % 8 or len(got) < 3 or crc_a(got[:-2]) != got[-2:]:
            raise RuntimeError(f"an answer of {bits} bits with no CRC of its own")
        return got[:-2]

    def rats(self):
        """Request for answer to select (frames of 256 bytes, CID 0): the
        ATS, or None when the card has none"""
        self.block = 0
        try:
            ats = self.frame(b"\xE0\x80")
        except (Silence, RuntimeError):
            return None
        if not ats or ats[0] != len(ats):
            return None
        if len(ats) > 1:
            self.fsc = self.FSC[min(ats[1] & 0x0F, 8)]
        return ats

    def exchange(self, apdu):
        """An APDU to the card, its answer back"""
        room = self.fsc - 3             # (the PCB and the CRC)
        parts = [apdu[i:i + room] for i in range(0, len(apdu), room)] or [b""]
        answer = b""
        send = None
        for i, part in enumerate(parts):
            more = 0x10 if i + 1 < len(parts) else 0
            send = bytes((0x02 | more | self.block,)) + part
            while True:
                got = self.frame(send)
                pcb = got[0]
                if pcb & 0xC0 == 0xC0 and pcb & 0x30 == 0x30:
                    send = got[:2]      # S(WTX): granted, as it was asked
                    continue
                if pcb & 0xE0 == 0xA0:  # R(ACK): the next part
                    self.block ^= 1
                    break
                if pcb & 0xC0 == 0x00:  # an I-block: the answer, or a part
                    self.block ^= 1
                    answer += got[1:]
                    if not pcb & 0x10:
                        return answer
                    send = bytes((0xA2 | self.block,))
                    continue
                raise RuntimeError(f"a block the reader does not know: {got.hex()}")
        raise RuntimeError("the card acknowledged the command and did not answer")


def iso4_lines(kind, target, ats):
    """A card file's lines down to the ATS, as the firmware writes them"""
    uid, atqa, sak = target
    up = lambda b: bytes(b).hex(" ").upper()
    out = ["Filetype: Flipper NFC device", "Version: 4", f"Device type: {kind}",
           f"UID: {up(uid)}", f"ATQA: {up(atqa[::-1])}", f"SAK: {sak:02X}"]
    if ats and len(ats) > 1:
        out.append(f"T0: {ats[1]:02X}")
        at = 2
        for bit, name in ((0x10, "TA(1)"), (0x20, "TB(1)"), (0x40, "TC(1)")):
            if ats[1] & bit and at < len(ats):
                out.append(f"{name}: {ats[at]:02X}")
                at += 1
        if at < len(ats):
            out.append(f"T1...Tk: {up(ats[at:])}")
    return out


def read_iso_dep(link, say=lambda text: None):
    """Activate the emulated card as an ISO14443-4A card: the .nfc text of
    what it said of itself, and whether it has an ATS"""
    target = wait_target(link)
    say(f"a card: UID {target[0].hex(' ').upper()}, SAK {target[2]:02X}")
    ats = None
    for _ in range(3):                  # (a RATS is asked once of a card
        ats = IsoDep(link).rats()       # that is selected: from the start)
        if ats:
            break
        link.off()
        time.sleep(0.02)
        link.on()
        time.sleep(0.02)
        target = wait_target(link)
    say(f"its ATS: {ats.hex(' ').upper() if ats else 'none'}")
    return "\n".join(iso4_lines("ISO14443-4A", target, ats)) + "\n", bool(ats)


T4T_APP = bytes.fromhex("D2760000850101")


def read_t4t(link, say=lambda text: None):
    """Read the emulated card as an NFC Forum Type 4 tag: the .nfc text
    (the format of the firmwares that know one), and the NDEF message's
    length"""
    return again(link, lambda: t4t_once(link, say))


def t4t_once(link, say):
    target = wait_target(link)
    dep = IsoDep(link)
    ats = dep.rats()
    if not ats:
        raise RuntimeError("the card has no ATS: not an ISO14443-4 card")

    def ask(apdu, what):
        r = dep.exchange(bytes.fromhex(apdu))
        if r[-2:] != b"\x90\x00":
            raise RuntimeError(f"{what}: the card says {r[-2:].hex().upper()}")
        return r[:-2]

    ask("00A4040007" + T4T_APP.hex() + "00", "the NDEF application")
    ask("00A4000C02E103", "the capability container")
    cc = ask("00B000000F", "the capability container")
    if len(cc) < 15 or cc[7] != 0x04:
        raise RuntimeError(f"a capability container that names no NDEF file: "
                           f"{cc.hex().upper()}")
    mle = int.from_bytes(cc[3:5], "big")
    file_id, size = cc[9:11], int.from_bytes(cc[11:13], "big")
    say(f"the capability container: NDEF file {file_id.hex().upper()} of "
        f"{size} bytes, {mle} bytes a read")
    ask("00A4000C02" + file_id.hex(), "the NDEF file")
    length = int.from_bytes(ask("00B0000002", "the message's length"), "big")
    step = max(1, min(mle, 255, dep.fsc - 5))
    message = b""
    while len(message) < length:
        at = 2 + len(message)
        n = min(step, length - len(message))
        got = ask(f"00B0{at:04X}{n:02X}", "the message")
        if not got:
            raise RuntimeError("the NDEF file ends before its message does")
        message += got
    say(f"an NDEF message of {length} bytes")
    up = lambda b: bytes(b).hex(" ").upper()
    out = iso4_lines("Type 4 Tag", target, ats) + [f"NDEF Data Size: {len(message)}"] + [
        f"NDEF Data: {up(message[i:i + 16])}" for i in range(0, len(message), 16)]
    return "\n".join(out) + "\n", length


# ------------------------------------------------------------------ FeliCa

def crc_f(data):
    """FeliCa's CRC (x^16 + x^12 + x^5 + 1 from 0), high byte first"""
    crc = 0
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021 if crc & 0x8000 else crc << 1) & 0xFFFF
    return crc.to_bytes(2, "big")


# FeliCa Lite-S: the block numbers behind a dump's 28 blocks (S_PAD0-13,
# REG, RC, MAC, ID, D_ID, SER_C, SYS_C, CKV, CK, MC, WCNT, MAC_A, STATE,
# CRC_CHECK)
LITE_BLOCKS = list(range(15)) + [0x80, 0x81, 0x82, 0x83, 0x84, 0x85, 0x86,
                                 0x87, 0x88, 0x90, 0x91, 0x92, 0xA0]


class Felica:
    """A FeliCa reader's session with the card on the link"""

    def __init__(self, link):
        self.link = link
        self.idm = None

    def poll(self):
        target = self.link.poll_f()
        if target:
            self.idm, self.pmm, self.system = target
        return target

    def command(self, payload):
        """A command (its code and what follows) there, the response's
        back: the length and the CRC are added and checked here"""
        frame = bytes((len(payload) + 1,)) + bytes(payload)
        bits, got, _ = self.link.send(frame + crc_f(frame))
        if bits % 8 or len(got) < 4 or got[0] != len(got) - 2 or \
                crc_f(got[:-2]) != got[-2:]:
            raise RuntimeError(f"a FeliCa response that is none: {got.hex()}")
        return got[1:-2]

    def read(self, block, service=0x000B):
        """Read Without Encryption of one block: (status flag 1 and 2, its
        16 bytes or None)"""
        sf1, sf2, blocks = self.read_many([block], service)
        return sf1, sf2, blocks[0] if blocks else None

    def read_many(self, numbers, service=0x000B):
        """... of up to four: (the flags, each block's 16 bytes or None)"""
        r = self.command(b"\x06" + self.idm + b"\x01" + service.to_bytes(2, "little")
                         + bytes((len(numbers),))
                         + b"".join(bytes((0x80, n)) for n in numbers))
        if len(r) < 11 or r[0] != 0x07 or r[1:9] != self.idm:
            raise RuntimeError(f"not a read's response: {r.hex()}")
        if r[9] or len(r) < 12 + 16 * len(numbers):
            return r[9], r[10], None
        return r[9], r[10], [r[12 + 16 * i:28 + 16 * i]
                             for i in range(len(numbers))]

    def write_many(self, numbers, blocks):
        """Write Without Encryption: the two status flags"""
        r = self.command(b"\x08" + self.idm + b"\x01\x09\x00"
                         + bytes((len(numbers),))
                         + b"".join(bytes((0x80, n)) for n in numbers)
                         + b"".join(blocks))
        if len(r) < 11 or r[0] != 0x09 or r[1:9] != self.idm:
            raise RuntimeError(f"not a write's response: {r.hex()}")
        return r[9], r[10]

    # FeliCa Lite-S's authentication.  The card key and a challenge that
    # the reader writes make a session key; a MAC is triple DES in CBC over
    # a first block that says what is read or written and then the data,
    # every eight bytes turned round (the card model's fl_mac, from the
    # firmware's code for it)

    def mac(self, key, rc, first, data, write=False):
        rev = lambda b: bytes(b)[::-1]
        ck = rev(key[:8]) + rev(key[8:])
        sk, _ = tdes2_cbc(ck, bytes(8), rev(rc[:8]) + rev(rc[8:]))
        if write:                       # (the session key's halves swapped)
            sk = sk[8:] + sk[:8]
        iv = rev(rc[:8])
        for at in range(-8, len(data), 8):
            block = first if at < 0 else data[at:at + 8]
            _, iv = tdes2_cbc(sk, iv, rev(block))
        return rev(iv)

    def login(self, key):
        """Both ways: the card shows that it has the key (a MAC of its ID
        and key version), and the reader shows it to the card (a MAC of the
        state that it writes).  -> what went wrong, or None"""
        rc = os.urandom(16)
        if self.write_many([0x80], [rc])[0]:
            return "the card refuses a challenge"
        sf1, _, got = self.read_many([0x82, 0x86, 0x91])
        if sf1 or not got:
            return "the card gives no MAC of its ID"
        first = bytes((0x82, 0, 0x86, 0, 0x91, 0, 0xFF, 0xFF))
        if self.mac(key, rc, first, got[0] + got[1]) != got[2][:8]:
            return "the card's MAC is not the key's"
        sf1, _, got = self.read_many([0x90])
        if sf1 or not got:
            return "the card does not say how often it was written"
        wcnt = got[0][:3]
        state = b"\x01" + bytes(15)
        first = wcnt + bytes((0, 0x92, 0, 0x91, 0))
        proof = self.mac(key, rc, first, state, write=True)
        sf1, sf2 = self.write_many([0x92, 0x91], [state, proof + wcnt + bytes(5)])
        if sf1:
            return f"the card refuses the reader's MAC ({sf1:02X} {sf2:02X})"
        return None


def read_felica(link, say=lambda text: None, card_text=None):
    """Read the emulated card as a FeliCa Lite-S: the .nfc text, and
    (blocks read, blocks in all).  card_text: a card file's text with the
    card key in it (its block 22, as the firmware keeps a key it was given):
    the reader and the card show each other the key first, and the blocks
    that are kept for such a reader are read; without: read as by anyone"""
    key = None
    if card_text:
        ck = bytes.fromhex(fe_nfc.parse_nfc_text(card_text).get(
            "Block 22", "").replace(" ", ""))[2:18]
        key = ck if len(ck) == 16 and any(ck) else None
        if not key:
            raise RuntimeError("that card file has no card key (its block 22)")
    rd = Felica(link)
    for _ in range(50):                 # (the firmware may start late)
        if rd.poll():
            break
        time.sleep(0.1)
    else:
        raise RuntimeError("the Flipper is not emulating a FeliCa card (no "
                           "answer to a polling)")
    up = lambda b: bytes(b).hex(" ").upper()
    say(f"a card: IDm {up(rd.idm)}, PMm {up(rd.pmm)}, system {up(rd.system)}")
    if key:
        wrong = None
        for _ in range(3):
            try:
                wrong = rd.login(key)
                break
            except Silence:
                wrong = "the card stopped answering"
        say("the card and the reader have shown each other the key"
            if not wrong else f"no authentication: {wrong}")
        if wrong:
            key = None
    blocks = []
    for number in LITE_BLOCKS:
        sf1, sf2, data = 0xFF, 0xFF, None
        for _ in range(3):              # (a command stands for itself here:
            try:                        # once more is all a retry takes)
                sf1, sf2, data = rd.read(number)
                break
            except Silence:
                pass
        blocks.append(bytes((sf1, sf2)) + (data or bytes(16)))
    if key:                             # (the key is never read: as the
        blocks[22] = b"\x01\xA8" + key  #  firmware's file has one it knows)
    read = sum(1 for b in blocks if not b[0])
    out = ["Filetype: Flipper NFC device", "Version: 4", "Device type: FeliCa",
           f"UID: {up(rd.idm)}", "Data format version: 2",
           f"Manufacture id: {up(rd.idm)}", f"Manufacture parameter: {up(rd.pmm)}",
           "IC Type: FeliCa Lite-S RC-S966", f"Blocks total: {len(blocks)}",
           f"Blocks read: {read}"]
    out += [f"Block {i}: {up(b)}" for i, b in enumerate(blocks)]
    return "\n".join(out) + "\n", (read, len(blocks))


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("sock", help="the device's NFC link (devices/NAME/nfc.sock)")
    ap.add_argument("program", choices=["classic", "ulc", "iso-dep", "t4t", "felica"])
    ap.add_argument("card", nargs="?",
                    help="classic, ulc: the card's .nfc file, whose keys are "
                         "the reader's; felica: one with the card key, to "
                         "read with it")
    ap.add_argument("-o", "--out", help="write what was read as a .nfc file")
    ap.add_argument("-v", "--verbose", action="store_true",
                    help="print every frame")
    args = ap.parse_args()
    if args.program in ("classic", "ulc") and not args.card:
        ap.error(f"{args.program} CARD.nfc: the card's file")
    link = ReaderLink(args.sock)
    if args.verbose:
        link.log = lambda sent, got: print(f"  > {sent}\n  < {got}", flush=True)
    say = lambda t: print(f"[fe_nfc_reader] {t}", flush=True)
    try:
        link.on()
        if args.program == "classic":
            text, (read, total) = read_classic(
                link, pathlib.Path(args.card).read_text(), say)
            done, words = read == total, f"read {read} of {total} blocks"
        elif args.program == "iso-dep":
            text, done = read_iso_dep(link, say)
            words = "an ISO14443-4A card" if done else "a card with no ATS"
        elif args.program == "ulc":
            text, (read, total) = read_ulc(
                link, pathlib.Path(args.card).read_text(), say)
            done, words = read == total, f"read {read} of {total} pages"
        elif args.program == "felica":
            text, (read, total) = read_felica(
                link, say, pathlib.Path(args.card).read_text()
                if args.card else None)
            done, words = read > 0, f"read {read} of {total} blocks"
        else:
            text, length = read_t4t(link, say)
            done, words = True, f"read an NDEF message of {length} bytes"
    except RuntimeError as e:
        sys.exit(f"[fe_nfc_reader] nothing read: {e}")
    finally:
        link.close()
    if args.out:
        pathlib.Path(args.out).write_text(text)
    print(f"[fe_nfc_reader] {words}" + (f" -> {args.out}" if args.out else ""),
          flush=True)
    return 0 if done else 1


if __name__ == "__main__":
    sys.exit(main())
