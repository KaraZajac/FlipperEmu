#!/usr/bin/env python3
# Copyright (c) 2026, Kara Zajac
# SPDX-License-Identifier: BSD-3-Clause
"""Check the secure enclave: the AES peripheral, the key store, the firmware.

    scripts/enclave_check.py                the AES model alone, register by
                                            register (no firmware needed)
    scripts/enclave_check.py -f FIRMWARE    ... and the enclave as a firmware
                                            uses it, on a throwaway device

The first part starts the machine without a firmware (QEMU's qtest) and
drives AES1 and AES2 through their registers, in the sequences of the
Flipper firmware's furi_hal_crypto.c: ECB, CBC, CTR and GCM against the
vectors of FIPS 197, SP 800-38A and the GCM specification, key derivation,
the DATATYPE swaps, the error flags, the interrupts as the NVIC sees them,
and the reset through RCC.

The second part boots FIRMWARE (anything ./flipperemu run -f takes) four
times: a new device gets FlipperEmu's factory keys and passes the firmware's
own enclave check; `crypto encrypt`, `decrypt` and `store_key` give what
scripts/fe_enclave.py computes on the host, and so does a Sub-GHz keystore the
firmware encrypts and loads back; the device's own key appears when
U2F first asks for it, and decrypts U2F's counter file; the keys survive a
restart; a key of one's own in a factory slot fails the firmware's check, and
that slot alone; a blank enclave is reported damaged, and can be filled
through the firmware's CLI; a key file that makes no sense stops the emulator
at once.

Exit status 0 = pass.
"""
import argparse
import os
import pathlib
import re
import select
import shutil
import stat
import struct
import subprocess
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import fe_enclave  # noqa: E402
from fe_cli import ANSI, FlipperCli, usb_endpoint  # noqa: E402
from fe_monitor import Monitor  # noqa: E402

QEMU = ROOT / "build" / "qemu" / "qemu-system-arm"


class Failure(Exception):
    pass


class Skip(Exception):
    """This check cannot be made here, and that is no failure."""


def show(value):
    if isinstance(value, (bytes, bytearray)):
        return value.hex() or "(nothing)"
    if isinstance(value, (tuple, list)):
        return "(" + ", ".join(show(v) for v in value) + ")"
    if isinstance(value, int) and not isinstance(value, bool):
        return f"{value:#x}"
    return repr(value)


def expect(got, want, what):
    if got != want:
        raise Failure(f"{what}: {show(got)}, expected {show(want)}")


def h(text: str) -> bytes:
    return bytes.fromhex(text)


# -------------------------------------------------------- published vectors

# FIPS 197, appendix C.1 and C.3
FIPS_PLAIN = h("00112233445566778899aabbccddeeff")
FIPS_KEY128 = bytes(range(16))
FIPS_KEY256 = bytes(range(32))
FIPS_CIPHER128 = h("69c4e0d86a7b0430d8cdb78070b4c55a")
FIPS_CIPHER256 = h("8ea2b7ca516745bfeafc49904b496089")
FIPS_LAST_ROUND_KEY128 = h("13111d7fe3944a17f307a78b4d2b30c5")   # round[10].k_sch

# NIST SP 800-38A, appendix F: the first two blocks of F.1.1, F.2.1, F.5.1
SP_KEY = h("2b7e151628aed2a6abf7158809cf4f3c")
SP_PLAIN = h("6bc1bee22e409f96e93d7e117393172a" "ae2d8a571e03ac9c9eb76fac45af8e51")
SP_ECB = h("3ad77bb40d7a3660a89ecaf32466ef97" "f5d3d58503b9699de785895a96fdbaaf")
SP_CBC_IV = bytes(range(16))
SP_CBC = h("7649abac8119b246cee98e9b12e9197d" "5086cb9b507219ee95db113a917678b2")
SP_CTR_BLOCK = h("f0f1f2f3f4f5f6f7f8f9fafbfcfdfeff")
SP_CTR = h("874d6191b620e3261bef6864990db6ce" "9806f66b7970fdff8617187bb9fffdff")

# McGrew & Viega, "The Galois/Counter Mode of Operation", test cases 4 and 16
GCM_KEY = h("feffe9928665731c6d6a8f9467308308")
GCM_IV = h("cafebabefacedbaddecaf888")
GCM_AAD = h("feedfacedeadbeeffeedfacedeadbeefabaddad2")
GCM_PLAIN = h("d9313225f88406e5a55909c5aff5269a86a7a9531534f7da2e4c303d8a318a72"
              "1c3c0c95956809532fcf0e2449a6b525b16aedf5aa0de657ba637b39")
GCM_CASES = (
    (GCM_KEY,
     h("42831ec2217774244b7221b784d0d49ce3aa212f2c02a4e035c17e2329aca12e"
       "21d514b25466931c7d8f6a5aac84aa051ba30b396a0aac973d58e091"),
     h("5bc94fbc3221a5db94fae95ae7121a47")),
    (GCM_KEY * 2,
     h("522dc1f099567d07f47f37a32a84427d643a8cdcbfe5c0c97598a2bd2555d1aa"
       "8cb08e48590dbb3da7b08b1056828838c5f61e6393ba7a0abcc9f662"),
     h("76fc6ece0f4e1768cddf8853bb2d551b")),
)


def gcm(key: bytes, iv: bytes, aad: bytes, data: bytes, decrypt=False):
    """AES-GCM with a 12-byte IV, the slow way: (output, tag)."""
    aes = fe_enclave.AES(key)
    hkey = int.from_bytes(aes.encrypt(bytes(16)), "big")
    out = fe_enclave.ctr(key, iv, 2, data)
    cipher = data if decrypt else out

    def pad(b):
        return b + bytes(-len(b) % 16)

    hashed = (pad(aad) + pad(cipher) + (len(aad) * 8).to_bytes(8, "big") +
              (len(cipher) * 8).to_bytes(8, "big"))
    y = 0
    for i in range(0, len(hashed), 16):
        x, v, y = y ^ int.from_bytes(hashed[i:i + 16], "big"), hkey, 0
        for bit in range(127, -1, -1):          # SP 800-38D, algorithm 1
            if (x >> bit) & 1:
                y ^= v
            v = (v >> 1) ^ (0xE1 << 120 if v & 1 else 0)
    tag = bytes(a ^ b for a, b in zip(y.to_bytes(16, "big"),
                                      aes.encrypt(iv + b"\0\0\0\1")))
    return out, tag


def host_check():
    """The references the other checks compare against."""
    expect(fe_enclave.AES(FIPS_KEY128).encrypt(FIPS_PLAIN), FIPS_CIPHER128, "FIPS 197 C.1")
    expect(fe_enclave.AES(FIPS_KEY256).encrypt(FIPS_PLAIN), FIPS_CIPHER256, "FIPS 197 C.3")
    expect(fe_enclave.AES(FIPS_KEY256).decrypt(FIPS_CIPHER256), FIPS_PLAIN, "FIPS 197 C.3 inverse")
    expect(bytes(fe_enclave.AES(FIPS_KEY128).rk[-1]), FIPS_LAST_ROUND_KEY128,
           "FIPS 197 C.1 key schedule")
    expect(fe_enclave.cbc_encrypt(SP_KEY, SP_CBC_IV, SP_PLAIN), SP_CBC, "SP 800-38A F.2.1")
    expect(fe_enclave.cbc_decrypt(SP_KEY, SP_CBC_IV, SP_CBC), SP_PLAIN, "SP 800-38A F.2.2")
    expect(fe_enclave.ctr(SP_KEY, SP_CTR_BLOCK[:12], int.from_bytes(SP_CTR_BLOCK[12:], "big"),
                          SP_PLAIN), SP_CTR, "SP 800-38A F.5.1")
    for n, (key, cipher, tag) in zip((4, 16), GCM_CASES):
        expect(gcm(key, GCM_IV, GCM_AAD, GCM_PLAIN), (cipher, tag), f"GCM test case {n}")
        expect(gcm(key, GCM_IV, GCM_AAD, cipher, decrypt=True), (GCM_PLAIN, tag),
               f"GCM test case {n}, decryption")
    return "FIPS 197, SP 800-38A, GCM test cases 4 and 16"


# ------------------------------------------------------------------- qtest

class QTest:
    """The machine without a CPU running: registers through QEMU's qtest."""

    def __init__(self, qemu=QEMU):
        self.p = subprocess.Popen(
            [str(qemu), "-M", "flipper-zero", "-accel", "qtest", "-qtest", "stdio",
             "-display", "none", "-monitor", "none", "-serial", "none"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        self.buf = b""

    def close(self):
        self.p.kill()           # it does not leave at the end of its input
        self.p.wait()

    def cmd(self, line: str) -> str:
        self.p.stdin.write(line.encode() + b"\n")
        self.p.stdin.flush()
        while True:
            while b"\n" not in self.buf:
                ready, _, _ = select.select([self.p.stdout], [], [], 10)
                chunk = os.read(self.p.stdout.fileno(), 4096) if ready else b""
                if not chunk:
                    raise Failure(f"qtest: no answer to '{line}'")
                self.buf += chunk
            answer, _, self.buf = self.buf.partition(b"\n")
            answer = answer.decode().strip()
            if answer.startswith("OK"):
                return answer[2:].strip()
            if answer.startswith(("ERR", "FAIL")):
                raise Failure(f"qtest: '{line}': {answer}")

    def readl(self, addr: int) -> int:
        return int(self.cmd(f"readl {addr:#x}"), 16)

    def writel(self, addr: int, value: int):
        self.cmd(f"writel {addr:#x} {value:#x}")


# AES registers (RM0434, chapter 24)
CR, SR, DINR, DOUTR, IVR0 = 0x00, 0x04, 0x08, 0x0C, 0x20
KEYR = (0x10, 0x14, 0x18, 0x1C, 0x30, 0x34, 0x38, 0x3C)
EN, CCFC, ERRC, CCFIE, ERRIE = 1, 1 << 7, 1 << 8, 1 << 9, 1 << 10
DATATYPE, MODE, CHMOD, GCMPH = 3 << 1, 3 << 3, (3 << 5) | (1 << 16), 3 << 13
KEYSIZE, NPBLB = 1 << 18, 0xF << 20
ENCRYPT, DERIVE, DECRYPT, DERIVE_DECRYPT = 0 << 3, 1 << 3, 2 << 3, 3 << 3
ECB, CBC, CTR, GCM = 0 << 5, 1 << 5, 2 << 5, 3 << 5
PH_INIT, PH_HEADER, PH_PAYLOAD, PH_FINAL = 0 << 13, 1 << 13, 2 << 13, 3 << 13
CCF, RDERR, WRERR = 1, 2, 4

RCC = 0x58000000
NVIC_ISPR1, NVIC_ICPR1 = 0xE000E204, 0xE000E284     # interrupts 32..63


class Aes:
    """One AES peripheral, driven like furi_hal_crypto.c drives AES1."""

    def __init__(self, qt, base, rstr, rst, irq):
        self.qt, self.base = qt, base
        self.rstr, self.rst = RCC + rstr, rst
        self.irq = 1 << (irq - 32)

    def rd(self, reg):
        return self.qt.readl(self.base + reg)

    def wr(self, reg, value):
        self.qt.writel(self.base + reg, value)

    def set(self, bits):                            # SET_BIT(AES1->CR, ...)
        self.wr(CR, self.rd(CR) | bits)

    def clear(self, bits):                          # CLEAR_BIT
        self.wr(CR, self.rd(CR) & ~bits)

    def modify(self, mask, value):                  # MODIFY_REG
        self.wr(CR, (self.rd(CR) & ~mask) | value)

    def reset(self):
        """furi_hal_bus_disable() and _enable(): RCC's reset bit, up and down."""
        old = self.qt.readl(self.rstr)
        self.qt.writel(self.rstr, old | self.rst)
        self.qt.writel(self.rstr, old & ~self.rst)
        self.qt.writel(NVIC_ICPR1, self.irq)

    def pending(self) -> bool:
        return bool(self.qt.readl(NVIC_ISPR1) & self.irq)

    def unpend(self):
        self.qt.writel(NVIC_ICPR1, self.irq)

    def key(self, key: bytes, order="big"):
        """KEYR7 (KEYR3 for 128 bits) takes the first four bytes."""
        n = len(key) // 4
        for i in range(n):
            self.wr(KEYR[n - 1 - i], int.from_bytes(key[4 * i:4 * i + 4], order))

    def get_key(self, size: int) -> bytes:
        n = size // 4
        return b"".join(self.rd(KEYR[n - 1 - i]).to_bytes(4, "big") for i in range(n))

    def iv(self, iv: bytes, order="big"):
        for i in range(4):
            self.wr(IVR0 + 4 * (3 - i), int.from_bytes(iv[4 * i:4 * i + 4], order))

    def get_iv(self) -> bytes:
        return b"".join(self.rd(IVR0 + 4 * (3 - i)).to_bytes(4, "big") for i in range(4))

    def feed(self, data: bytes, order="big", swap=None):
        for i in range(0, 16, 4):
            word = int.from_bytes(data[i:i + 4], order)
            self.wr(DINR, swap(word) if swap else word)

    def take(self, order="big", swap=None) -> bytes:
        words = [self.rd(DOUTR) for _ in range(4)]
        return b"".join((swap(w) if swap else w).to_bytes(4, order) for w in words)

    def block(self, data: bytes, read=True, order="big", swap=None):
        """crypto_process_block(): four words in, CCF, CCFC, four words out."""
        self.feed(data, order, swap)
        if not self.rd(SR) & CCF:
            raise Failure("no CCF after the fourth word of a block")
        self.set(CCFC)
        return self.take(order, swap) if read else None

    def blocks(self, data: bytes, **kw) -> bytes:
        """A buffer block by block; a short last one is padded with zeros and
        cut again, as furi_hal_crypto_process_block_bswap() does."""
        out = b""
        for i in range(0, len(data), 16):
            chunk = data[i:i + 16]
            out += self.block(chunk.ljust(16, b"\0"), **kw)[:len(chunk)]
        return out


def aes1(qt):
    return Aes(qt, 0x50060000, 0x2C, 1 << 16, 51)       # AHB2RSTR.AES1RST


def aes2(qt):
    return Aes(qt, 0x58001800, 0x30, 1 << 17, 52)       # AHB3RSTR.AES2RST


def keysize(key: bytes) -> int:
    return KEYSIZE if len(key) == 32 else 0


def fw_init(a, key, iv, chmod, order="big"):
    """crypto_key_init() / crypto_key_init_bswap() (which is order="big")"""
    a.clear(EN)
    a.modify(DATATYPE | KEYSIZE | CHMOD, keysize(key) | chmod)
    a.key(key, order)
    a.iv(iv, order)


def fw_encrypt(a, data, order="big"):
    """furi_hal_crypto_encrypt()"""
    a.set(EN)
    a.modify(MODE, ENCRYPT)
    out = a.blocks(data, order=order)
    a.clear(EN)
    return out


def fw_decrypt(a, data, order="big", derive=True):
    """furi_hal_crypto_decrypt(): the key is derived before the first use"""
    if derive:
        a.modify(MODE, DERIVE)
        a.set(EN)
        if not a.rd(SR) & CCF:
            raise Failure("no CCF after key derivation")
        if a.rd(CR) & EN:
            raise Failure("EN still set after key derivation")
        a.set(CCFC)
    a.modify(MODE, DECRYPT)
    a.set(EN)
    out = a.blocks(data, order=order)
    a.clear(EN)
    return out


def fw_unload(a):
    """furi_hal_crypto_unload_key(): EN down and the bus off, which is a
    reset. The next furi_hal_crypto_load_key() finds every register at 0."""
    a.clear(EN)
    a.reset()


def fw_ctr(a, key, counter_block, data, unload=True):
    """furi_hal_crypto_ctr(), with the whole first counter block given"""
    fw_init(a, key, counter_block, CTR)
    a.set(EN)
    a.modify(MODE, ENCRYPT)
    out = a.blocks(data)
    a.clear(EN)
    if unload:
        fw_unload(a)
    return out


def fw_gcm(a, key, iv, aad, data, decrypt=False):
    """furi_hal_crypto_gcm(): (output, tag)"""
    fw_init(a, key, iv + b"\0\0\0\2", GCM)
    a.modify(GCMPH, PH_INIT)
    a.modify(MODE, DECRYPT if decrypt else ENCRYPT)
    a.set(EN)
    if not a.rd(SR) & CCF:
        raise Failure("no CCF after the GCM init phase")
    if a.rd(CR) & EN:
        raise Failure("EN still set after the GCM init phase")
    a.set(CCFC)
    if aad:
        a.modify(GCMPH, PH_HEADER)
        a.set(EN)
        for i in range(0, len(aad), 16):
            a.block(aad[i:i + 16].ljust(16, b"\0"), read=False)
    a.modify(GCMPH, PH_PAYLOAD)
    a.set(EN)
    out = b""
    for i in range(0, len(data), 16):
        chunk = data[i:i + 16]
        if len(chunk) < 16 and not decrypt:
            a.modify(NPBLB, (16 - len(chunk)) << 20)
        out += a.block(chunk.ljust(16, b"\0"))[:len(chunk)]
    a.modify(GCMPH, PH_FINAL)
    tag = a.block(bytes(4) + (len(aad) * 8).to_bytes(4, "big") +
                  bytes(4) + (len(data) * 8).to_bytes(4, "big"))
    fw_unload(a)        # NPBLB would count for the next message's blocks too
    return out, tag


# ------------------------------------------------- the model, by register

def t_reset(qt):
    a, b = aes1(qt), aes2(qt)
    for dev in (a, b):
        dev.reset()
        for reg in (CR, SR, *KEYR, *range(IVR0, IVR0 + 16, 4)):
            expect(dev.rd(reg), 0, f"register {reg:#x} after reset")
    a.key(FIPS_KEY256)
    a.iv(SP_CBC_IV)
    a.wr(CR, KEYSIZE | CBC | CCFIE)
    a.set(EN)
    a.feed(FIPS_PLAIN)
    expect(a.rd(SR), CCF, "SR after a block")
    expect(a.pending(), True, "the interrupt, CCF with CCFIE")
    b.key(FIPS_KEY128)
    b.wr(CR, CTR)
    a.reset()                           # AES1RST: AES2 is not touched
    for reg in (CR, SR, *KEYR, *range(IVR0, IVR0 + 16, 4)):
        expect(a.rd(reg), 0, f"AES1 register {reg:#x} after AES1RST")
    expect(b.get_key(16), FIPS_KEY128, "AES2's key after AES1RST")
    expect(b.rd(CR), CTR, "AES2's CR after AES1RST")
    a.set(EN)
    expect(a.block(bytes(16)), fe_enclave.AES(bytes(16)).encrypt(bytes(16)),
           "a block after the reset (the key is gone)")
    b.reset()
    expect(b.get_key(16), bytes(16), "AES2's key after AES2RST")
    return "AHB2RSTR.AES1RST, AHB3RSTR.AES2RST"


def t_ecb(qt):
    a = aes1(qt)
    for key, cipher in ((FIPS_KEY128, FIPS_CIPHER128), (FIPS_KEY256, FIPS_CIPHER256)):
        bits = len(key) * 8
        a.reset()
        a.wr(CR, keysize(key))
        a.key(key)
        a.set(EN)
        expect(a.block(FIPS_PLAIN), cipher, f"AES-{bits} encryption")
        expect(a.rd(SR), 0, "SR after CCFC")
        a.clear(EN)
        a.modify(MODE, DERIVE_DECRYPT)      # the plain key stays in KEYR
        a.set(EN)
        expect(a.block(cipher), FIPS_PLAIN, f"AES-{bits} decryption (MODE 11)")
        expect(a.get_key(len(key)), key, "KEYR after MODE 11")
    a.reset()
    a.key(SP_KEY)
    a.set(EN)
    expect(a.blocks(SP_PLAIN), SP_ECB, "two blocks (SP 800-38A F.1.1)")
    return "FIPS 197 C.1 and C.3, SP 800-38A F.1.1"


def t_derive(qt):
    a = aes1(qt)
    for key, cipher in ((FIPS_KEY128, FIPS_CIPHER128), (FIPS_KEY256, FIPS_CIPHER256)):
        bits = len(key) * 8
        a.reset()
        a.wr(CR, keysize(key) | DERIVE)
        a.key(key)
        a.set(EN)
        expect(a.rd(SR), CCF, f"SR after AES-{bits} key derivation")
        expect(a.rd(CR) & EN, 0, "EN after key derivation")
        a.set(CCFC)
        if bits == 128:
            expect(a.get_key(16), FIPS_LAST_ROUND_KEY128, "the derived key in KEYR")
        a.modify(MODE, DECRYPT)
        a.set(EN)
        expect(a.block(cipher), FIPS_PLAIN, f"AES-{bits} decryption with the derived key")
        expect(a.block(cipher), FIPS_PLAIN, "a second block")
    return "MODE 01, then MODE 10"


def t_cbc(qt):
    a = aes1(qt)
    a.reset()
    fw_init(a, SP_KEY, SP_CBC_IV, CBC)
    expect(fw_encrypt(a, SP_PLAIN), SP_CBC, "AES-128 encryption (SP 800-38A F.2.1)")
    expect(a.get_iv(), SP_CBC[16:], "IVR after two blocks")
    fw_init(a, SP_KEY, SP_CBC_IV, CBC)
    expect(fw_decrypt(a, SP_CBC), SP_PLAIN, "AES-128 decryption (F.2.2)")
    expect(a.get_iv(), SP_CBC[16:], "IVR after two blocks of decryption")

    key, iv = bytes(range(0x40, 0x60)), bytes(range(0xA0, 0xB0))
    plain = bytes((7 * i + 3) & 0xFF for i in range(64))
    cipher = fe_enclave.cbc_encrypt(key, iv, plain)
    a.reset()
    fw_init(a, key, iv, CBC)
    expect(fw_encrypt(a, plain[:32]), cipher[:32], "AES-256, the first half")
    # EN went down in between: the chain is in IVR, and goes on from there
    expect(fw_encrypt(a, plain[32:]), cipher[32:], "AES-256, the second half")
    fw_init(a, key, iv, CBC)
    expect(fw_decrypt(a, cipher[:16]), plain[:16], "AES-256 decryption, one block")
    expect(fw_decrypt(a, cipher[16:], derive=False), plain[16:],
           "... and the rest, the key already derived")
    return "SP 800-38A F.2.1 and F.2.2; AES-256 in two goes"


def t_ctr(qt):
    a = aes1(qt)
    a.reset()
    expect(fw_ctr(a, SP_KEY, SP_CTR_BLOCK, SP_PLAIN, unload=False), SP_CTR,
           "SP 800-38A F.5.1")
    expect(a.get_iv(), SP_CTR_BLOCK[:12] + h("fcfdff01"), "the counter after two blocks")
    fw_unload(a)
    expect((a.rd(CR), a.get_key(16), a.get_iv()), (0, bytes(16), bytes(16)),
           "CR, key and IV after the firmware unloaded its key")
    expect(fw_ctr(a, SP_KEY, SP_CTR_BLOCK, SP_CTR), SP_PLAIN, "F.5.2 (the same again)")

    key, iv = bytes(range(0x10, 0x30)), bytes(range(0xC0, 0xCC))
    plain = bytes((5 * i + 1) & 0xFF for i in range(36))        # a short last block
    expect(fw_ctr(a, key, iv + b"\0\0\0\1", plain), fe_enclave.ctr(key, iv, 1, plain),
           "AES-256, 36 bytes")
    # the counter is IVR0 alone: it wraps without a carry into IVR1
    expect(fw_ctr(a, key, iv + b"\xff\xff\xff\xff", plain, unload=False),
           fe_enclave.ctr(key, iv, 0xFFFFFFFF, plain), "the counter wrapping")
    expect(a.get_iv(), iv + b"\0\0\0\2", "the counter after wrapping")
    fw_unload(a)
    return "SP 800-38A F.5.1; a short last block; the 32-bit counter"


def t_gcm(qt):
    a = aes1(qt)
    a.reset()
    for n, (key, cipher, tag) in zip((4, 16), GCM_CASES):
        expect(fw_gcm(a, key, GCM_IV, GCM_AAD, GCM_PLAIN), (cipher, tag),
               f"test case {n}")
        expect(fw_gcm(a, key, GCM_IV, GCM_AAD, cipher, decrypt=True), (GCM_PLAIN, tag),
               f"test case {n}, decryption")
    return "test cases 4 and 16: 20 bytes of header, 60 of text, both ways"


def t_gcm_shapes(qt):
    a = aes1(qt)
    a.reset()
    key, iv = bytes(range(0x60, 0x80)), bytes(range(0x30, 0x3C))
    data = bytes((11 * i + 5) & 0xFF for i in range(80))
    shapes = ((0, 0), (0, 16), (0, 64), (0, 13), (16, 0), (20, 0), (32, 48), (7, 33),
              (48, 1), (1, 79))
    for alen, dlen in shapes:
        aad, plain = data[:alen], data[80 - dlen:] if dlen else b""
        want = gcm(key, iv, aad, plain)
        what = f"{alen} bytes of header, {dlen} of text"
        expect(fw_gcm(a, key, iv, aad, plain), want, what)
        expect(fw_gcm(a, key, iv, aad, want[0], decrypt=True), (plain, want[1]),
               what + ", decryption")
    return f"{len(shapes)} more shapes: no header, no text, short last blocks"


def t_datatype(qt):
    a = aes1(qt)
    swaps = {
        1: lambda w: ((w << 16) | (w >> 16)) & 0xFFFFFFFF,                 # half-words
        2: lambda w: int.from_bytes(w.to_bytes(4, "big"), "little"),      # bytes
        3: lambda w: int(f"{w:032b}"[::-1], 2),                            # bits
    }
    for datatype, swap in swaps.items():
        a.reset()
        a.wr(CR, CBC | (datatype << 1))
        a.key(SP_KEY)                   # key and IV are not swapped
        a.iv(SP_CBC_IV)
        a.set(EN)
        expect(a.blocks(SP_PLAIN, swap=swap), SP_CBC, f"CBC with DATATYPE {datatype:02b}")
        expect(a.get_iv(), SP_CBC[16:], f"IVR with DATATYPE {datatype:02b}")
    # DATATYPE 10 is the block as a little-endian machine has it in memory
    a.reset()
    a.wr(CR, 2 << 1)
    a.key(FIPS_KEY128)
    a.set(EN)
    expect(a.block(FIPS_PLAIN, order="little"), FIPS_CIPHER128,
           "DATATYPE 10 with little-endian words")
    return "half-words, bytes, bits: DINR and DOUTR, not the key or the IV"


def t_firmware_cbc(qt):
    """furi_hal_crypto_load_key() + encrypt/decrypt: DATATYPE 00, and every
    word as the little-endian CPU read it from the buffer."""
    a = aes1(qt)
    a.reset()
    key, iv = bytes(range(0x21, 0x41)), bytes(range(0x91, 0xA1))
    plain = b"Flipper Zero in an emulator: 48 bytes of text...".ljust(48, b".")
    # what the key registers then hold is the key with its words reversed
    stored = bytes(key[i ^ 3] for i in range(32))
    cipher = fe_enclave.encrypt(stored, iv, plain)
    fw_init(a, key, iv, CBC, order="little")
    expect(a.get_key(32), stored, "the key registers")
    expect(fw_encrypt(a, plain, order="little"), cipher, "encryption")
    fw_init(a, key, iv, CBC, order="little")
    expect(fw_decrypt(a, cipher, order="little"), plain, "decryption")
    expect(fe_enclave.decrypt(stored, iv, cipher), plain, "fe_enclave.decrypt")
    return "little-endian words, as fe_enclave.py computes it"


def t_flags(qt):
    a = aes1(qt)
    a.reset()
    a.key(FIPS_KEY128)
    expect(a.rd(DOUTR), 0, "DOUTR while disabled")
    expect(a.rd(SR), 0, "SR after reading DOUTR while disabled")
    a.set(EN)
    expect(a.rd(DOUTR), 0, "DOUTR with nothing computed")
    expect(a.rd(SR), RDERR, "SR after reading DOUTR too early")
    a.set(ERRC)
    expect(a.rd(SR), 0, "SR after ERRC")
    a.feed(FIPS_PLAIN)
    expect(a.rd(SR), CCF, "SR after a block")
    a.wr(DINR, 0x00112233)              # the result has not been read
    expect(a.rd(SR), CCF | WRERR, "SR after writing DINR too early")
    a.set(ERRC)
    expect(a.rd(SR), CCF, "SR after ERRC")
    a.set(CCFC)
    expect(a.rd(SR), 0, "SR after CCFC")
    a.clear(EN)
    a.set(EN)                           # starts over
    expect(a.block(FIPS_PLAIN), FIPS_CIPHER128, "a block after the errors")
    expect(a.rd(DOUTR), 0, "a fifth read of DOUTR")
    expect(a.rd(SR), RDERR, "SR after the fifth read")
    return "RDERR, WRERR, ERRC, CCFC"


def t_irq(qt):
    for a, name in ((aes1(qt), "AES1 (51)"), (aes2(qt), "AES2 (52)")):
        a.reset()
        a.key(FIPS_KEY128)
        a.set(EN)
        a.feed(FIPS_PLAIN)
        expect(a.pending(), False, f"{name} pending without CCFIE")
        a.set(CCFC)
        a.take()
        a.set(CCFIE)
        expect(a.pending(), False, f"{name} pending with CCFIE, before a block")
        a.feed(FIPS_PLAIN)
        expect(a.pending(), True, f"{name} pending, CCF with CCFIE")
        a.set(CCFC)                     # the line goes down ...
        a.unpend()
        expect(a.pending(), False, f"{name} pending after CCFC")
        a.take()
        a.feed(FIPS_PLAIN)              # ... and up again
        expect(a.pending(), True, f"{name} pending after the next block")
        a.set(CCFC)
        a.unpend()
        a.take()
        a.modify(CCFIE, ERRIE)
        expect(a.pending(), False, f"{name} pending with ERRIE, no error")
        a.rd(DOUTR)
        expect(a.rd(SR), RDERR, "SR")
        expect(a.pending(), True, f"{name} pending, RDERR with ERRIE")
        a.set(ERRC)
        a.unpend()
        expect(a.pending(), False, f"{name} pending after ERRC")
        a.reset()
    other = aes1(qt)
    b = aes2(qt)
    b.key(FIPS_KEY128)
    b.wr(CR, EN | CCFIE)
    b.feed(FIPS_PLAIN)
    expect((b.pending(), other.pending()), (True, False), "AES2's interrupt is its own")
    b.reset()
    return "CCFIE and ERRIE, at the NVIC: 51 for AES1, 52 for AES2"


def t_aes2(qt):
    a, b = aes1(qt), aes2(qt)
    a.reset()
    b.reset()
    a.wr(CR, KEYSIZE)
    a.key(FIPS_KEY256)
    b.key(SP_KEY)
    a.set(EN)
    b.set(EN)
    b.feed(SP_PLAIN[:16])               # interleaved: nothing is shared
    a.feed(FIPS_PLAIN)
    expect(b.take(), SP_ECB[:16], "AES2")
    expect(a.take(), FIPS_CIPHER256, "AES1")
    fw_init(b, SP_KEY, SP_CBC_IV, CBC)
    expect(fw_encrypt(b, SP_PLAIN), SP_CBC, "CBC on AES2")
    expect(fw_gcm(b, GCM_CASES[1][0], GCM_IV, GCM_AAD, GCM_PLAIN), GCM_CASES[1][1:],
           "GCM on AES2")
    a.reset()
    b.reset()
    return "a second peripheral, with its own key"


MODEL_CHECKS = (
    ("aes: reset by RCC, registers and key", t_reset),
    ("aes: ECB, AES-128 and AES-256", t_ecb),
    ("aes: key derivation, decryption with the derived key", t_derive),
    ("aes: CBC, the IV following the chain", t_cbc),
    ("aes: CTR in the firmware's sequence", t_ctr),
    ("aes: GCM in the firmware's sequence", t_gcm),
    ("aes: GCM, other lengths of header and text", t_gcm_shapes),
    ("aes: DATATYPE", t_datatype),
    ("aes: the firmware's CBC, as the host computes it", t_firmware_cbc),
    ("aes: error flags", t_flags),
    ("aes: interrupts", t_irq),
    ("aes: AES2", t_aes2),
)


def run_check(results, label, fn, *args):
    try:
        detail = fn(*args)
        results.append((label, True, detail or ""))
        return True
    except Failure as e:
        results.append((label, False, str(e)))
    except (OSError, TimeoutError, ConnectionError, ValueError) as e:
        results.append((label, False, f"{type(e).__name__}: {e}"))
    return False


def model_part(results):
    run_check(results, "host: the reference implementation", host_check)
    if not QEMU.exists():
        results.append(("aes: the model, by register", False,
                        f"QEMU not built yet: run scripts/setup_qemu.sh ({QEMU})"))
        return
    qt = QTest()
    try:
        for label, fn in MODEL_CHECKS:
            run_check(results, label, fn, qt)
    finally:
        qt.close()


# ------------------------------------------------ the firmware's enclave

CRASH = re.compile(r"\[CRASH\]|furi_check failed|HardFault|Kernel is Dead")
U2F_APP = "/ext/apps/USB/u2f.fap"
U2F_COUNTER = "/ext/u2f/cnt.u2f"
U2F_CONTROL = 0xAA5500FF                # U2F_COUNTER_CONTROL_VAL, u2f_data.c
FOREIGN_SLOT, FOREIGN_KEY = 3, bytes(range(0x30, 0x50))
KEYSTORE_HEADER = "Filetype: Flipper SubGhz Keystore File\nVersion: 0\n"
KEYSTORE = (                            # made up: lines of 2, 2, 3 and 5 blocks
    "0123456789ABCDEF:1:Emu",
    "FEDCBA9876543210:2:FlipperEmu_13",                 # 32 characters: no padding
    "0F1E2D3C4B5A6978:3:FlipperEmu_Secure_Learning",
    "8796A5B4C3D2E1F0:4:FlipperEmu_" + "long_" * 8 + "name",
)


class Emu:
    """A throwaway device, started and stopped through ./flipperemu."""

    def __init__(self, name, firmware, timeout):
        self.name, self.firmware, self.timeout = name, firmware, timeout
        self.dir = ROOT / "devices" / name
        self.uart = self.dir / "uart.log"
        self.keys = self.dir / "enclave.keys"
        self.proc = self.log = self.mon = self.cli = None
        self.flashed = False

    def launcher(self, *args, timeout=30):
        """./flipperemu -d NAME ...: (exit status, output)"""
        p = subprocess.Popen([str(ROOT / "flipperemu"), "-d", self.name, *args],
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        try:
            out, _ = p.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            p.terminate()               # the launcher passes it on to QEMU
            try:
                out, _ = p.communicate(timeout=10)
            except subprocess.TimeoutExpired:
                p.kill()
                out, _ = p.communicate()
            return None, out
        return p.returncode, out

    def start(self):
        self.dir.mkdir(parents=True, exist_ok=True)
        self.uart.unlink(missing_ok=True)
        cmd = [str(ROOT / "flipperemu"), "-d", self.name, "run", "--headless",
               "--serial", f"file:{self.uart}", "--usb", "pty"]
        if not self.flashed:
            cmd += ["-f", str(self.firmware)]
            self.flashed = True
        self.log = open(self.dir / "qemu.log", "ab")    # a pipe nobody reads fills up
        self.proc = subprocess.Popen(cmd, stdout=self.log, stderr=subprocess.STDOUT)
        if not self.wait_uart(r"Startup complete", self.timeout):
            raise Failure("no 'Startup complete' on the UART: "
                          + (self.uart_text().strip().splitlines() or ["(nothing)"])[-1])
        self.wait_uart(r"Secure Enclave|AnimationManager", 10)
        time.sleep(1.5)
        self.mon = Monitor(str(self.dir / "monitor.sock"))
        self.connect()

    def uart_text(self):
        return self.uart.read_text(errors="replace") if self.uart.exists() else ""

    def wait_uart(self, pattern, timeout):
        rx, end = re.compile(pattern), time.time() + timeout
        while time.time() < end:
            text = self.uart_text()
            if rx.search(text):
                return True
            if CRASH.search(text) or self.proc.poll() is not None:
                return False
            time.sleep(0.2)
        return False

    def connect(self, timeout=30):
        """A new CLI session on the USB serial port."""
        self.disconnect()
        end, error = time.time() + timeout, None
        while time.time() < end:
            try:
                cli = FlipperCli(usb_endpoint(self.mon), timeout=10)
                try:
                    cli.read_until_prompt(5)
                    self.cli = cli
                    return cli
                except TimeoutError as e:
                    cli.close()
                    error = e
            except (OSError, ConnectionError, TypeError) as e:
                error = e
            time.sleep(1)
        raise Failure(f"no CLI prompt on the USB serial port ({error})")

    def disconnect(self):
        if self.cli:
            self.cli.close()
            self.cli = None

    def release_monitor(self):
        """The monitor takes one client: let ./flipperemu have it."""
        if self.mon:
            self.mon.close()
            self.mon = None

    def take_monitor(self):
        if not self.mon:
            self.mon = Monitor(str(self.dir / "monitor.sock"))

    def stop(self):
        self.disconnect()
        if self.proc and self.proc.poll() is None:
            try:
                self.take_monitor()
                self.mon.s.sendall(b"quit\n")
            except (OSError, ConnectionError):
                self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait()
        if self.mon:
            self.mon.close()
            self.mon = None
        if self.log:
            self.log.close()
            self.log = None
        self.proc = None

    def store(self):
        return fe_enclave.read_store(self.keys)


def feed(cli, command, text, timeout=20, during=None):
    """An interactive `crypto` command: the command, what is typed, Ctrl+C.
    `during` runs while the firmware waits for input, its key loaded."""
    found, out = cli.watch(command, r"Ctrl\+C to complete \w+:\r?\n", 10, stop=False)
    if not found:
        cli.write(b"\x03")
        try:
            cli.read_until_prompt(5)
        except TimeoutError:
            pass
        raise Failure(f"`{command}`: {out.strip().splitlines()[-1] if out.strip() else 'no answer'}")
    try:
        if during:
            during()
    finally:
        cli.write(text.encode())
        time.sleep(0.2)
        cli.write(b"\x03")
        out = cli.read_until_prompt(timeout)
    return ANSI.sub(b"", out[:-3]).decode(errors="replace").replace("\r\n", "\n")


def fw_cli_encrypt(cli, slot, iv, text, during=None) -> bytes:
    """`crypto encrypt SLOT IV` with `text` typed in. The firmware encrypts
    the text as a C string in whole blocks, and what follows the string's NUL
    in the last block is whatever its heap held there: crypto_cli.c reserves
    the room and does not clear it. Only a text that ends one character short
    of a block (15, 31, 47... characters) gives a ciphertext that can be
    compared with anything."""
    if len(text) % 16 != 15:
        raise ValueError(f"a text of {len(text)} characters: the firmware pads it "
                         "with what its heap holds; use 15, 31, 47...")
    out = feed(cli, f"crypto encrypt {slot} {iv.hex()}", text, during=during)
    m = re.search(r"Hex-encoded encrypted data:\n((?:\n?[0-9a-f]+)+)", out)
    if not m:
        raise Failure(f"`crypto encrypt {slot}`: {out.strip().splitlines()[-1]}")
    return bytes.fromhex(m.group(1).replace("\n", ""))


def fw_cli_decrypt(cli, slot, iv, data) -> str:
    out = feed(cli, f"crypto decrypt {slot} {iv.hex()}", data.hex())
    m = re.search(r"Decrypted data:\n(.*)\n", out)
    if not m:
        raise Failure(f"`crypto decrypt {slot}`: {out.strip().splitlines()[-1]}")
    return m.group(1)


def enclave_info(cli):
    """(valid keys, valid) from `info device`"""
    out = cli.run("info device", timeout=15)
    count = re.search(r"enclave(?:[._]keys[._]valid|_valid_keys)\s*:\s*(\d+)", out)
    valid = re.search(r"enclave[._]valid\s*:\s*(true|false)", out)
    if not count or not valid:
        raise Failure("`info device` says nothing about the enclave")
    return int(count.group(1)), valid.group(1) == "true"


def has_key(cli, slot) -> bool:
    out = cli.run(f"crypto has_key {slot}", timeout=10)
    if "Successfully loaded key" in out:
        return True
    if "Unable to load key" in out:
        return False
    raise Failure(f"`crypto has_key {slot}`: {out.strip()}")


def f_first_run(emu):
    keys = emu.store()
    expect(keys, fe_enclave.factory_store(), "the keys of a new device")
    expect(stat.S_IMODE(emu.keys.stat().st_mode), 0o600, "the key file's mode")
    return f"{emu.keys.name}: {len(keys)} keys, FlipperEmu's, owner only"


def f_verify(emu):
    expect(enclave_info(emu.cli), (10, True), "`info device`: valid keys, valid")
    text = emu.uart_text()
    if "Secure Enclave verification failed" in text:
        raise Failure("the desktop logged 'Secure Enclave verification failed'")
    return "10 keys valid, no complaint from the desktop"


def f_has_key(emu):
    keys = emu.store()
    for slot in (1, 5, 10, 11, 12, 50, 100):
        expect(has_key(emu.cli, slot), slot in keys, f"`crypto has_key {slot}`")
    return "1, 5 and 10 load; empty slots do not"


def f_secure(emu):
    """While the firmware waits for the text of `crypto encrypt`, the key is
    loaded: its registers must read as zero from CPU1's side, and the IV be
    there, word for word."""
    iv, seen = bytes(range(0x50, 0x60)), {}

    def words(addr):
        out = emu.mon.cmd(f"xp /4xw {addr:#x}")
        found = re.findall(r"0x([0-9a-f]{8})", out)
        if len(found) != 4:
            raise Failure(f"xp {addr:#x}: {out.strip()}")
        return [int(w, 16) for w in found]

    def during():
        seen["cr"] = words(0x50060000)[0]
        seen["key"] = words(0x50060010) + words(0x50060030)
        seen["iv"] = words(0x50060020)

    text = "the key is not readable"[:15]
    got = fw_cli_encrypt(emu.cli, 1, iv, text, during=during)
    expect(seen.get("key"), [0] * 8, "KEYR0..7 with a key of the enclave loaded")
    expect(seen["iv"], [int.from_bytes(iv[4 * (3 - i):4 * (4 - i)], "little")
                        for i in range(4)], "IVR0..3")
    expect(seen["cr"] & (KEYSIZE | CHMOD | DATATYPE), KEYSIZE | CBC, "AES1_CR")
    expect(got, fe_enclave.cli_encrypt(emu.store()[1][1], iv, text),
           "what it encrypted meanwhile")
    return "KEYR reads as zero; the block is encrypted with the key all the same"


def f_crypt(emu):
    keys, n = emu.store(), 0
    texts = ("fifteen chars..", "thirty-one characters of text..",
             "forty-seven characters, which makes 3 blocks...")
    for slot in (1, 2, 10):
        iv = bytes((slot * 16 + i) & 0xFF for i in range(16))
        for text in texts:
            want = fe_enclave.cli_encrypt(keys[slot][1], iv, text)
            expect(fw_cli_encrypt(emu.cli, slot, iv, text), want,
                   f"`crypto encrypt {slot}`, {len(text)} characters")
            expect(fw_cli_decrypt(emu.cli, slot, iv, want), text,
                   f"`crypto decrypt {slot}`, {len(want)} bytes")
            n += 1
    emu.first = (1, bytes((16 + i) & 0xFF for i in range(16)), texts[0],
                 fe_enclave.cli_encrypt(keys[1][1], bytes((16 + i) & 0xFF for i in range(16)),
                                        texts[0]))
    return f"{n} texts of 1 to 3 blocks, slots 1, 2 and 10, both ways"


def keystore_iv(iv: bytes) -> bytes:
    """What subghz_keystore.c makes of a keystore file's IV before it uses
    it: every byte plus the one before it."""
    return bytes((b + (iv[i - 1] if i else 0)) & 0xFF for i, b in enumerate(iv))


def keystore_lines(key: bytes, iv: bytes, lines) -> list:
    """The lines of an encrypted keystore file: each line padded with zeros
    to whole blocks, one CBC chain through all of them, in hex."""
    padded = [line.encode() + bytes(-len(line) % 16) for line in lines]
    cipher = fe_enclave.encrypt(key, keystore_iv(iv), b"".join(padded))
    out, at = [], 0
    for block in padded:
        out.append(cipher[at:at + len(block)].hex().upper())
        at += len(block)
    return out


def read_file(cli, path) -> list:
    out = cli.run(f"storage read {path}", timeout=15)
    if "Storage error" in out or not out.startswith("Size:"):
        raise Failure(f"{path}: {out.strip()[-80:] or 'nothing'}")
    return [line for line in out.splitlines()[1:] if line.strip()]


def f_keystore(emu):
    """The Sub-GHz keystores are encrypted with key 1, line after line in
    one CBC chain. The firmware has a debug command that encrypts a plain
    one: the file must be what the host computes, and loading it back (to
    encrypt it once more, with another IV) must give every line again."""
    cli, key = emu.cli, emu.store()[1][1]
    plain, first, second = (f"/ext/enclave_check.{x}" for x in ("txt", "one", "two"))
    text = KEYSTORE_HEADER + "Encryption: 0\n" + "".join(l + "\n" for l in KEYSTORE)
    cli.run("sysctl debug 1", timeout=10)
    try:
        if "encrypt_keeloq" not in cli.run("subghz", timeout=10):
            raise Skip("this firmware's CLI has no `subghz encrypt_keeloq`")
        for path in (plain, first, second):
            cli.run(f"storage remove {path}", timeout=10)   # write_chunk appends
        ready, out = cli.watch(f"storage write_chunk {plain} {len(text)}", r"Ready",
                               timeout=5, stop=False)
        if not ready:
            raise Failure(f"storage write_chunk: {out.strip()[-80:]}")
        cli.write(text.encode())
        cli.read_until_prompt(10)
        for source, target, iv in ((plain, first, bytes(range(0xA0, 0xB0))),
                                   (first, second, bytes(range(0x0F, 0xFF, 0x0F))[:16])):
            out = cli.run(f"subghz encrypt_keeloq {source} {target} {iv.hex()}", timeout=30)
            if "Failed" in out:
                raise Failure(f"`subghz encrypt_keeloq {source}`: {out.strip()}")
            want = (KEYSTORE_HEADER.splitlines()
                    + ["Encryption: 1", "IV: " + iv.hex(" ").upper()]
                    + keystore_lines(key, iv, KEYSTORE))
            got = read_file(cli, target)
            if got != want:
                n = next((i for i, (a, b) in enumerate(zip(got, want)) if a != b),
                         min(len(got), len(want)))
                raise Failure(f"{target}, line {n + 1} of {len(got)} (expected {len(want)}): "
                              f"{(got[n] if n < len(got) else 'missing')[:40]}, expected "
                              f"{(want[n] if n < len(want) else 'nothing')[:40]}")
    finally:
        cli.run("sysctl debug 0", timeout=10)
        for path in (plain, first, second):
            cli.run(f"storage remove {path}", timeout=10)
    blocks = sum(-(-len(l) // 16) for l in KEYSTORE)
    return (f"{len(KEYSTORE)} made-up codes, {blocks} blocks: encrypted as the host "
            "computes, and read back whole")


def f_unique(emu):
    """The device's own key: nobody has it until something asks for it
    (furi_hal_crypto_enclave_ensure_key), and U2F does when it starts."""
    before = emu.store()
    if fe_enclave.UNIQUE_SLOT in before:
        return "already there"
    cli = emu.cli
    if "Storage error" in cli.run(f"storage stat {U2F_APP}", timeout=10):
        return None
    cli.write(f"loader open {U2F_APP}\r".encode())    # U2F takes the USB port
    end = time.time() + 20
    while time.time() < end and fe_enclave.UNIQUE_SLOT not in emu.store():
        time.sleep(0.3)
    time.sleep(1.5)                     # ... and writes its counter file
    emu.disconnect()
    emu.mon.press("back")               # leave U2F: the serial port is back
    time.sleep(2.0)
    emu.connect()
    after = emu.store()
    if fe_enclave.UNIQUE_SLOT not in after:
        raise Failure("U2F ran, and there is no key 11: "
                      + "; ".join(l for l in emu.uart_text().splitlines()
                                  if "U2f" in l or "Crypto" in l)[-200:])
    kind, key = after[fe_enclave.UNIQUE_SLOT]
    expect((kind, len(key)), ("simple", 32), "key 11")
    expect({i: after[i] for i in before}, before, "the other keys")
    expect(stat.S_IMODE(emu.keys.stat().st_mode), 0o600, "the key file's mode")
    expect(has_key(emu.cli, fe_enclave.UNIQUE_SLOT), True, "`crypto has_key 11`")
    return "made when U2F started, 256 bits, saved"


def f_u2f_counter(emu):
    keys = emu.store()
    if fe_enclave.UNIQUE_SLOT not in keys:
        return None
    out = emu.cli.run(f"storage read {U2F_COUNTER}", timeout=10)
    iv = re.search(r"^IV: ((?:[0-9A-F]{2} ?){16})\s*$", out, re.M)
    data = re.search(r"^Data: ((?:[0-9A-F]{2} ?){48})\s*$", out, re.M)
    if not iv or not data:
        if "Storage error" in out:
            return None
        raise Failure(f"{U2F_COUNTER}: {out.strip()[-120:]}")
    plain = fe_enclave.decrypt(keys[fe_enclave.UNIQUE_SLOT][1], bytes.fromhex(iv.group(1)),
                               bytes.fromhex(data.group(1))[:32])
    counter, _salt, control = struct.unpack("<I24sI", plain)
    expect(control, U2F_CONTROL, f"the control word of {U2F_COUNTER}, decrypted")
    return f"{U2F_COUNTER} decrypts with key 11 on the host: counter {counter}"


def f_store(emu):
    cli, keys = emu.cli, emu.store()
    master, first = keys[0][1], max(keys) + 1
    new = (("simple", bytes(range(32))), ("encrypted", bytes(range(0x40, 0x60))),
           ("simple", bytes(range(0x80, 0x90))), ("encrypted", bytes(range(0x9F, 0x8F, -1))))
    for slot, (kind, key) in enumerate(new, first):
        arg = key if kind == "simple" else fe_enclave.wrap(master, key, os.urandom(12))
        out = cli.run(f"crypto store_key {slot} {kind} {len(key) * 8} {arg.hex()}", timeout=15)
        if f"Stored to slot: {slot}" not in out:
            raise Failure(f"`crypto store_key {slot} {kind} {len(key) * 8}`: {out.strip()}")
    after = emu.store()
    expect({i: after[i] for i in keys}, keys, "the keys that were there")
    for slot, (kind, key) in enumerate(new, first):
        expect(after.get(slot), (kind, key), f"key {slot} in the file")
        iv, text = bytes((slot + i) & 0xFF for i in range(16)), f"stored key #{slot:03}"
        expect(fw_cli_encrypt(cli, slot, iv, text), fe_enclave.cli_encrypt(key, iv, text),
               f"`crypto encrypt {slot}` ({kind}, {len(key) * 8} bits)")
    expect(stat.S_IMODE(emu.keys.stat().st_mode), 0o600, "the key file's mode")
    emu.stored = list(range(first, first + len(new)))
    return (f"slots {first} to {first + len(new) - 1}: simple and encrypted, "
            "256 and 128 bits")


def f_store_refused(emu):
    cli, keys = emu.cli, emu.store()
    top, key = max(keys), bytes(range(32)).hex()
    for command, answer in (
            (f"crypto store_key 0 master 256 {key}", "Failure"),
            (f"crypto store_key {top} simple 256 {key}", "already used"),
            (f"crypto store_key {top + 2} simple 256 {key}", "empty, which is not allowed"),
            # an encrypted key comes with its 12-byte IV: the CLI says no itself
            (f"crypto store_key {top + 1} encrypted 256 {key}", "expected hex encoded key"),
    ):
        out = cli.run(command, timeout=15)
        if "Stored to slot" in out or answer not in out:
            raise Failure(f"`{command[:32]}...`: {out.strip()!r}, expected '{answer}'")
    expect(emu.store(), keys, "the key file after four refusals")
    return "a second master key, a used slot, a gap, a key without its IV"


def f_reset_refused(emu):
    before = emu.keys.read_bytes()
    emu.release_monitor()
    try:
        status, out = emu.launcher("enclave", "reset")
    finally:
        emu.take_monitor()
    if status in (0, None) or "is running" not in out:
        raise Failure(f"`enclave reset` on a running device: status {status}, {out.strip()!r}")
    expect(emu.keys.read_bytes(), before, "the key file")
    return "`./flipperemu enclave reset` waits for the device to be stopped"


def f_restart(emu, before):
    expect(emu.keys.read_bytes(), before, "the key file after a restart")
    keys = emu.store()
    for slot in range(1, max(keys) + 2):
        expect(has_key(emu.cli, slot), slot in keys, f"`crypto has_key {slot}`")
    expect(enclave_info(emu.cli), (10, True), "`info device`: valid keys, valid")
    iv = bytes(range(0xE0, 0xF0))
    for slot in sorted(keys)[fe_enclave.FACTORY_KEYS + 1:]:
        text = f"again, key #{slot:03}"
        expect(fw_cli_encrypt(emu.cli, slot, iv, text),
               fe_enclave.cli_encrypt(keys[slot][1], iv, text), f"`crypto encrypt {slot}`")
    return f"{len(keys)} keys, the same file, the same ciphertexts"


def f_foreign(emu):
    """A key of the user's own where a factory key belongs: to the firmware's
    check it is a wrong key, as it would be on hardware. It encrypts like any
    other key all the same."""
    expect(emu.store().get(FOREIGN_SLOT), ("simple", FOREIGN_KEY), "the key file")
    expect(enclave_info(emu.cli), (fe_enclave.FACTORY_KEYS - 1, False),
           "`info device`: valid keys, valid")
    emu.release_monitor()
    try:
        status, out = emu.launcher("enclave")
    finally:
        emu.take_monitor()
    if status != 0 or f"{fe_enclave.FACTORY_KEYS - 1} of {fe_enclave.FACTORY_KEYS}" not in out:
        raise Failure(f"`./flipperemu enclave`: status {status}, {out.strip()[-120:]!r}")
    iv, text = bytes(range(0x70, 0x80)), "not the factory"
    expect(fw_cli_encrypt(emu.cli, FOREIGN_SLOT, iv, text),
           fe_enclave.cli_encrypt(FOREIGN_KEY, iv, text), f"`crypto encrypt {FOREIGN_SLOT}`")
    return (f"{fe_enclave.FACTORY_KEYS - 1} of {fe_enclave.FACTORY_KEYS} valid, "
            "the enclave not; the slot encrypts with the key in the file")


def f_blank(emu):
    expect(emu.store(), {}, "the key file")
    expect(enclave_info(emu.cli), (0, False), "`info device`: valid keys, valid")
    expect(has_key(emu.cli, 1), False, "`crypto has_key 1`")
    logged = "Secure Enclave verification failed" in emu.uart_text()
    return "no keys: `info device` says enclave.valid false" + \
        (", the desktop logs the failure" if logged else "")


def f_provision(emu):
    """Fill an empty enclave the way a factory does: through the firmware."""
    cli, master = emu.cli, fe_enclave.default_key(0)
    wrapped = fe_enclave.wrap(master, fe_enclave.default_key(1), bytes(12)).hex()
    out = cli.run(f"crypto store_key 1 encrypted 256 {wrapped}", timeout=15)
    if "Failure" not in out:
        raise Failure(f"an encrypted key without a master key: {out.strip()!r}")
    want = {}
    for slot in range(fe_enclave.FACTORY_KEYS + 1):
        key = fe_enclave.default_key(slot)
        kind = "master" if slot == 0 else "simple" if slot % 2 else "encrypted"
        arg = fe_enclave.wrap(master, key, os.urandom(12)) if kind == "encrypted" else key
        out = cli.run(f"crypto store_key {slot} {kind} 256 {arg.hex()}", timeout=15)
        if f"Stored to slot: {slot}" not in out:
            raise Failure(f"`crypto store_key {slot} {kind} 256`: {out.strip()!r}")
        want[slot] = (kind, key)
        if slot in (1, 5):
            expect(enclave_info(cli), (slot, False), f"`info device` with {slot} keys")
    expect(enclave_info(cli), (10, True), "`info device`: valid keys, valid")
    expect(emu.store(), want, "the key file")
    return "master key, then keys 1 to 10, plain and encrypted: valid again"


def f_malformed(emu):
    good = emu.keys.read_bytes()
    cases = (
        ("0 master 256 abcd\n", f"{emu.keys}:1: a 256-bit key is 64 hex digits"),
        (f"0 master 256 {'11' * 32}\n2 simple 256 {'22' * 32}\n",
         f"{emu.keys}: key 2 without a key 1: the FUS stores them in order"),
        ("1 simple 256\n", f"{emu.keys}:1: expected INDEX TYPE BITS KEY"),
    )
    try:
        for text, message in cases:
            emu.keys.write_text(text)
            status, out = emu.launcher("run", "--headless", "--usb", "none",
                                       "--serial", "null", timeout=20)
            if status in (0, None):
                raise Failure(f"the emulator started on a key file saying {text.strip()!r}")
            if message not in out:
                raise Failure(f"no '{message}' in: {out.strip()[-200:]!r}")
            expect(emu.keys.read_text(), text, "the key file (it must be left alone)")
            status, out = emu.launcher("enclave")
            if status != 1 or message not in out:
                raise Failure(f"`./flipperemu enclave`: status {status}, {out.strip()!r}")
    finally:
        emu.keys.write_bytes(good)
    return f"{len(cases)} broken files: the emulator stops, naming file and line"


def f_tool(emu):
    """./flipperemu enclave ... on the stopped device"""
    status, out = emu.launcher("enclave", "reset")
    expect(status, 0, f"`enclave reset` ({out.strip()[-80:]})")
    expect(emu.store(), fe_enclave.factory_store(), "the keys after `enclave reset`")
    expect(stat.S_IMODE(emu.keys.stat().st_mode), 0o600, "the key file's mode")
    slot, iv, text, cipher = emu.first
    status, out = emu.launcher("enclave", "encrypt", str(slot), iv.hex(), text)
    expect((status, out.strip()), (0, cipher.hex()), "`enclave encrypt`")
    status, out = emu.launcher("enclave", "decrypt", str(slot), iv.hex(), cipher.hex())
    expect((status, out.strip()), (0, text), "`enclave decrypt`")
    key, iv12 = bytes(range(32)), bytes(range(12))
    status, out = emu.launcher("enclave", "wrap", key.hex(), iv12.hex())
    expect((status, out.strip()),
           (0, fe_enclave.wrap(fe_enclave.default_key(0), key, iv12).hex()), "`enclave wrap`")
    status, out = emu.launcher("enclave")
    if status != 0 or "10 of 10" not in out:
        raise Failure(f"`enclave`: status {status}, {out.strip()[-120:]!r}")
    status, out = emu.launcher("enclave", "encrypt", "11", iv.hex(), text)
    if status != 1 or "slot 11 is empty" not in out:
        raise Failure(f"`enclave encrypt 11`: status {status}, {out.strip()!r}")
    status, out = emu.launcher("enclave", "blank")
    expect((status, emu.store()), (0, {}), "`enclave blank`")
    return "reset, encrypt, decrypt and wrap give what the firmware gave"


def firmware_part(results, firmware, keep, timeout):
    emu = Emu(f"enclave-{int(time.time())}", firmware, timeout)
    emu.first = emu.stored = None

    def check(label, fn, *args):
        try:
            detail = fn(emu, *args)
        except Skip as e:
            results.append((label, None, str(e)))
            return True
        except Failure as e:
            results.append((label, False, str(e)))
            return False
        except (OSError, TimeoutError, ConnectionError, ValueError, TypeError) as e:
            results.append((label, False, f"{type(e).__name__}: {e}"))
            try:                        # a command may still be running
                emu.connect(10)
            except Failure:
                pass
            return False
        results.append((label, None if detail is None else True, detail or "not on this SD card"))
        return True

    def boot(label):
        try:
            emu.start()
        except (Failure, OSError, ConnectionError) as e:
            results.append((label, False, str(e)))
            return False
        results.append((label, True, ""))
        return True

    try:
        if not boot("boot: a new device"):
            return
        check("first run: a key file with the factory keys", f_first_run)
        check("firmware: its own check of the enclave passes", f_verify)
        check("firmware: `crypto has_key`", f_has_key)
        check("firmware: the loaded key cannot be read", f_secure)
        check("firmware: `crypto encrypt` and `decrypt` = the host's", f_crypt)
        check("firmware: a Sub-GHz keystore, encrypted and loaded back", f_keystore)
        check("firmware: the device's own key (slot 11)", f_unique)
        check("host: a file U2F encrypted with that key", f_u2f_counter)
        check("firmware: `crypto store_key`", f_store)
        check("firmware: `crypto store_key`, what is refused", f_store_refused)
        check("launcher: no reset under a running device", f_reset_refused)
        before = emu.keys.read_bytes()
        emu.stop()

        if boot("boot: again"):
            check("restart: the keys are still there", f_restart, before)
        emu.stop()

        keys = emu.store()
        keys[FOREIGN_SLOT] = ("simple", FOREIGN_KEY)
        fe_enclave.write_store(emu.keys, keys)
        if boot(f"boot: with another key in factory slot {FOREIGN_SLOT}"):
            check("own key in a factory slot: the firmware's check fails for it", f_foreign)
        emu.stop()

        status, out = emu.launcher("enclave", "blank")
        if status != 0:
            results.append(("blank enclave", False, f"`enclave blank`: {out.strip()[-120:]}"))
        elif boot("boot: with a blank enclave"):
            check("blank enclave: the firmware reports it damaged", f_blank)
            check("blank enclave: filled through the firmware's CLI", f_provision)
        emu.stop()

        check("key file: nonsense stops the emulator", f_malformed)
        if emu.first:
            check("launcher: `./flipperemu enclave`", f_tool)
        text = emu.uart_text()
        results.append(("no crash in the log", not CRASH.search(text), ""))
    finally:
        emu.stop()
        if keep:
            print(f"  device: {emu.dir}")
        else:
            shutil.rmtree(emu.dir, ignore_errors=True)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-f", "--firmware", help="also check the enclave under this firmware")
    ap.add_argument("--timeout", type=float, default=60, help="for a boot, in seconds")
    ap.add_argument("--keep", action="store_true",
                    help="keep the device directory (UART and QEMU logs)")
    args = ap.parse_args()

    results = []
    model_part(results)
    if args.firmware:
        firmware_part(results, args.firmware, args.keep, args.timeout)

    width = max(len(r[0]) for r in results)
    for label, ok, detail in results:
        print(f"  {'SKIP' if ok is None else 'PASS' if ok else 'FAIL'}  {label:{width}}"
              + (f"  {detail}" if detail else ""))
    return 0 if all(ok is not False for _, ok, _ in results) else 1


if __name__ == "__main__":
    sys.exit(main())
