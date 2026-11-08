#!/usr/bin/env python3
# Copyright (c) 2026, Kara Zajac
# SPDX-License-Identifier: BSD-3-Clause
"""125 kHz RFID tags for FlipperEmu's virtual reader field (/machine/rfid).

A tag is described as the firmware's own encoders produce it for emulation
(lib/lfrfid/protocols): a frame of (level, duration in carrier cycles).
This script ports those encoders, then turns the frame into what the
Flipper's comparator sees while reading: +high/-low µs at 125 kHz.

    fe_rfid.py EM4100 "DE AD BE EF 01"     -> "+256,-256,..."
    fe_rfid.py tag.rfid                    -> a tag from a Flipper .rfid file
    fe_rfid.py rfid-tx/tx-00001.txt        -> a tag from an emulation capture
    fe_rfid.py --list                      -> supported protocols
"""
import pathlib
import sys

CYCLE_US = 8                    # one 125 kHz carrier cycle


def _bits(value, n):
    """n bits of value, MSB first."""
    return [(value >> (n - 1 - i)) & 1 for i in range(n)]


def _manchester(bits, half):
    """The encoders' Manchester: a bit is (bit, half) then (!bit, half)."""
    out = []
    for b in bits:
        out += [(b, half), (1 - b, half)]
    return out


# ---------------------------------------------------------------- EM4100

def em4100_frame(data):
    """64 bits: 9-bit header, 10 rows of nibble + even parity, 4 column
    parities, stop bit (protocol_em4100_encoder_start)."""
    if len(data) != 5:
        raise ValueError("EM4100 data is 5 bytes")
    enc = 0b111111111
    for byte in data:
        for nib in (byte >> 4, byte & 0xF):
            enc = (enc << 4 | nib) << 1 | (bin(nib).count("1") & 1)
    for col in range(4):
        par = 0
        for row in range(1, 11):
            par += (enc >> (row * 5 - 1)) & 1
        enc = enc << 1 | (par & 1)
    enc <<= 1                                   # stop bit 0
    return _bits(enc & (1 << 64) - 1, 64)


def em4100(data, clock=64):
    return _manchester(em4100_frame(data), clock // 2)


# ------------------------------------------------------- bit_lib, in Python

class Bits:
    """A bit_lib buffer: bit n is bit 7 - n % 8 of byte n / 8 (MSB first)."""

    def __init__(self, nbits, data=b""):
        self.b = bytearray(data) + bytearray(max(0, (nbits + 7) // 8 - len(data)))
        self.n = nbits

    def get(self, pos):
        return (self.b[pos // 8] >> (7 - pos % 8)) & 1

    def set(self, pos, bit):
        if bit:
            self.b[pos // 8] |= 1 << (7 - pos % 8)
        else:
            self.b[pos // 8] &= ~(1 << (7 - pos % 8)) & 0xFF

    def set_bits(self, pos, value, length):
        for i in range(length):
            self.set(pos + i, (value >> (length - 1 - i)) & 1)

    def get_bits(self, pos, length):
        v = 0
        for i in range(length):
            v = v << 1 | self.get(pos + i)
        return v

    def copy(self, pos, length, src, src_pos):
        """bit_lib_copy_bits(self, pos, length, src, src_pos); src: Bits or bytes."""
        if not isinstance(src, Bits):
            src = Bits(len(src) * 8, src)
        for i in range(length):
            self.set(pos + i, src.get(src_pos + i))

    def list(self):
        return [self.get(i) for i in range(self.n)]


def _parity(value):
    """1 if value has an odd number of 1 bits (__builtin_parity)."""
    return bin(value).count("1") & 1


def _rev8(byte):
    """bit_lib_reverse_8_fast."""
    return int(f"{byte:08b}"[::-1], 2)


def _crc8(data, poly, init, ref_in=False, ref_out=False, xor_out=0):
    """bit_lib_crc8."""
    crc = init
    for byte in data:
        crc ^= _rev8(byte) if ref_in else byte
        for _ in range(8):
            crc = (crc << 1 ^ poly if crc & 0x80 else crc << 1) & 0xFF
    return (_rev8(crc) if ref_out else crc) ^ xor_out


def _crc16(data, poly, init):
    """bit_lib_crc16, unreflected."""
    crc = init
    for byte in data:
        for j in range(8):
            top = (crc >> 15) ^ (byte >> (7 - j) & 1)
            crc = crc << 1 & 0xFFFF
            if top:
                crc ^= poly
    return crc


def _pairs(enc, pos, src, nbits):
    """The first nbits of src into enc from pos as pairs, 1 = 10 and 0 = 01
    (the HID, FDX-A and Paradox encoders)."""
    for i in range(nbits):
        enc.set_bits(pos + 2 * i, 0b10 if src.get(i) else 0b01, 2)


# ------------------------------------------------------------ modulations

def fsk(bits, f0, f1, rf):
    """fsk_osc: square cycles of f0 (bit 0) or f1 (bit 1) carrier cycles; a
    bit lasts while the accumulated phase stays <= rf.  The phase carries
    over between frames: the loop runs from the first frame whose starting
    phase comes round again."""
    out, phase, starts = [], 0, {}
    for _ in range(64):
        if phase in starts:
            return out[starts[phase]:]
        starts[phase] = len(out)
        for bit in bits:
            while True:
                period = f1 if bit else f0
                phase += period
                out += [(1, period // 2), (0, period // 2)]
                if phase > rf:
                    phase -= rf
                    break
    raise RuntimeError("FSK frame does not loop")


def psk(bits, pulses_per_bit, polarity=True):
    """PSK1 on an RF/2 subcarrier (Indala/Keri/Nexwatch/Idteck encoders):
    pulses of one carrier cycle high and one low, the phase flipping at a
    bit boundary when the bit differs from the previous one."""
    out, last = [], bits[-1]
    for bit in [last] + bits[:-1]:
        if bit != last:
            polarity = not polarity
        last = bit
        out += [(int(polarity), 1), (int(not polarity), 1)] * pulses_per_bit
    return out


def biphase(bits, short, long, double=0):
    """Bi-phase (FDX-B and Jablotron; GProxII inverted, double=1): every
    pulse flips the level, starting high; a bit equal to `double` is two
    short pulses, the other bit one long pulse.  The level carries over, so
    a frame of an odd number of pulses loops after two."""
    out, level = [], 0
    while True:
        for bit in bits:
            for cycles in (short, short) if bit == double else (long,):
                level ^= 1
                out.append((level, cycles))
        if not level:                           # back to the starting level
            return out


def nrz(bits, clock):
    """Direct modulation (PAC/Stanley): the level is the bit, clock cycles a
    bit, one level per run of equal bits; runs wrap round the frame end."""
    start = next(i for i in range(len(bits)) if bits[i] != bits[i - 1])
    out = []
    for bit in bits[start:] + bits[:start]:
        if out and out[-1][0] == bit:
            out[-1] = (bit, out[-1][1] + clock)
        else:
            out.append((bit, clock))
    return out


# -------------------------------------------------------- HID H10301 (FSK)

def h10301(data):
    """HID Prox 26-bit (protocol_h10301_encode): 0x1D preamble, then
    Manchester-like pairs (0 = 01, 1 = 10) of OEM, format, parities and
    FC:card, FSK RF/8 and RF/10 at RF/50."""
    if len(data) != 3:
        raise ValueError("H10301 data is 3 bytes: facility code, card number")
    fc_cn = data[0] << 16 | data[1] << 8 | data[2]
    even = bin(fc_cn >> 12 & 0xFFF).count("1") & 1
    odd = (1 + bin(fc_cn & 0xFFF).count("1")) & 1
    bits = [0, 0, 0, 1, 1, 1, 0, 1]
    words = [0] * 6 + [1] + [0] * 10 + [1] + [even] + _bits(fc_cn, 24) + [odd]
    for w in words:
        bits += [w, 1 - w]
    return fsk(bits, 8, 10, 50)


# --------------------------------------------------------- Indala26 (PSK)

def indala26(data):
    """Indala 26-bit (protocol_indala26_encoder_start): 64 bits, PSK RF/32."""
    if len(data) != 4:
        raise ValueError("Indala26 data is 4 bytes")
    enc = Bits(64)
    enc.b[0] = 0b10100000                       # *(uint32_t*)&enc[0] = 0xA0
    enc.set(32, 1)
    enc.copy(33, 22, data, 0)
    enc.copy(55, 5, data, 22)
    enc.copy(62, 2, data, 27)
    return psk(enc.list(), 16)


def idteck(data):
    """Idteck (protocol_idteck_encoder_start): "IDTK", then data bits 32-63
    (bits 0-31 are that preamble); PSK RF/32."""
    enc = Bits(64, b"IDTK")                     # *(uint32_t*)&enc[0] = 0x4B544449
    enc.copy(32, 32, data, 32)
    return psk(enc.list(), 16)


def keri(data):
    """Keri (protocol_keri_encoder_start): 0xE0 and 24 zero bits, then the
    32 data bits with the first one set; PSK RF/32."""
    enc = Bits(64, b"\xE0")                     # *(uint32_t*)&enc[0] = 0xE0
    enc.copy(32, 32, data, 0)
    enc.set(32, 1)
    return psk(enc.list(), 16)


def nexwatch(data):
    """Nexwatch (protocol_nexwatch_encoder_start): 0x56 and 24 zero bits,
    then the 64 data bits; PSK RF/32."""
    enc = Bits(96, b"\x56")                     # *(uint32_t*)&enc[0] = 0x56
    enc.copy(32, 64, data, 0)
    return psk(enc.list(), 16)


# ------------------------------------------------------------- FSK tags

def awid(data):
    """AWID (protocol_awid_encode): 0x01 preamble, then 22 groups of three
    data bits and their odd parity; FSK RF/8 and RF/10 at RF/50."""
    src, enc = Bits(72, data), Bits(96)
    enc.set_bits(0, 0b00000001, 8)
    for i in range(22):
        value = src.get_bits(i * 3, 3) << 1
        enc.set_bits(8 + i * 4, value | (1 - _parity(value)), 4)
    return fsk(enc.list(), 8, 10, 50)


def fdx_a(data):
    """FDX-A (protocol_fdx_a_encode): 0x55 0x1D preamble, then the 40 data
    bits as pairs (1 = 10, 0 = 01); FSK RF/8 and RF/10 at RF/50."""
    enc = Bits(96, b"\x55\x1D")
    _pairs(enc, 16, Bits(40, data), 40)
    return fsk(enc.list(), 8, 10, 50)


def hid_generic(data):
    """HID Prox (protocol_hid_generic_encode): 0x1D preamble, then data bits
    0-43 as pairs (1 = 10, 0 = 01); FSK RF/8 and RF/10 at RF/50."""
    enc = Bits(96, b"\x1D")
    _pairs(enc, 8, Bits(48, data), 44)
    return fsk(enc.list(), 8, 10, 50)


def hid_ex_generic(data):
    """HID extended (protocol_hid_ex_generic_encode): 0x1D preamble, then
    data bits 0-91 as pairs (1 = 10, 0 = 01); FSK RF/8 and RF/10 at RF/50."""
    enc = Bits(192, b"\x1D")
    _pairs(enc, 8, Bits(96, data), 92)
    return fsk(enc.list(), 8, 10, 50)


def io_prox_xsf(data):
    """Kantech ioProx XSF (protocol_io_prox_xsf_encode): nine 0s, 0xF0, then
    FC, version and two code bytes, each byte followed by a 1, and an 8-bit
    checksum (0xFF minus the sum of the five bytes) and 11; FSK RF/8 and
    RF/10 at RF/64."""
    enc = Bits(64)
    enc.set_bits(9, 0b11110000, 8)
    enc.set(17, 1)
    for i, byte in enumerate(data):
        enc.set_bits(18 + 9 * i, byte, 8)
        enc.set(26 + 9 * i, 1)
    checksum = (0xFF - sum(enc.get_bits(9 * i, 8) for i in range(1, 6))) & 0xFF
    enc.set_bits(54, checksum, 8)
    enc.set_bits(62, 0b11, 2)
    return fsk(enc.list(), 8, 10, 64)


def paradox(data):
    """Paradox (protocol_paradox_encode): 0x0F preamble, then data bits 0-43
    as pairs (1 = 10, 0 = 01); FSK RF/8 and RF/10 at RF/50."""
    enc = Bits(96, b"\x0F")
    _pairs(enc, 8, Bits(48, data), 44)
    return fsk(enc.list(), 8, 10, 50)


def pyramid(data):
    """Farpointe Pyramid (protocol_pyramid_encode): a start bit and 26-bit
    Wiegand of FC (byte 1) and card (bytes 2-3) ending at bit 105, an odd
    parity bit after every 7 bits from bit 8 (bit_lib_add_parity), and a
    CRC-8/Maxim of bytes 2-14; FSK RF/8 and RF/10 at RF/50."""
    wiegand = Bits(24)
    wiegand.copy(0, 24, data, 8)
    pre = Bits(128)
    pre.set(79, 1)                              # format start bit
    pre.set(80, _parity(wiegand.get_bits(0, 12)))
    pre.copy(81, 24, wiegand, 0)
    pre.set(105, 1 - _parity(wiegand.get_bits(12, 12)))
    enc = Bits(128)
    for word in range(15):                      # 102 bits in words of 7
        value = pre.get_bits(8 + word * 7, 7)
        enc.set_bits(8 + word * 8, value << 1 | (1 - _parity(value)), 8)
    crc = _crc8([enc.get_bits(16 + i * 8, 8) for i in range(13)], 0x31, 0, True, True)
    enc.set_bits(120, crc, 8)
    return fsk(enc.list(), 8, 10, 50)


# ------------------------------------------------------- Manchester tags

def electra(data):
    """Electra (protocol_electra_encoder_start): an EM4100 frame of bytes
    0-4, then a 64-bit epilogue of bytes 5 and 6 and byte 7 six times;
    Manchester RF/64."""
    epilogue = Bits(64, data[5:7] + data[7:8] * 6)
    return _manchester(em4100_frame(data[:5]) + epilogue.list(), 32)


_GALLAGHER_SCRAMBLE = bytes.fromhex(
    "a3b080c6b2f45c6c81f1bbeb55673c051a0e61f622ceaa8fbd3b1f5e4404512e"
    "4d9a84eaf86674297f70d8317a6da40082b95fb416abffc239dc1965577c20fa"
    "5a4913d0fba89173b13318be217248b6dba05dcce61727e5d45342f3dd7b24ac"
    "2b581ea7e78640d3989771cb3a0f019b6e1bfc34a6da070cae37ca54fd26fe0a"
    "45a22ac4120df54f69e08a77603f9995d2383662b7327e79c046932fa5ba5baf"
    "521dc375cfd64c83e83d304ebc082d0906d9259e89f29688c18c940b28f04763"
    "d5b368569cf96f4150858b9d59bf9fe28e6a1123a1cdb57dc7a9c8efdf02b803"
    "6b353e2c76c9de1c4bd1ed14c5ade9644aec8df71043781587e4d792e1eee390")


def gallagher(data):
    """Gallagher (protocol_gallagher_encoder_start): 0x7FEA preamble, then
    region, FC, card and issue level packed into 8 scrambled bytes, each
    followed by the inverse of its last bit, and their CRC-8 (0x07, init
    0x2C); Manchester RF/32."""
    src = Bits(64, data)
    rc, il = src.get_bits(0, 4), src.get_bits(4, 4)
    fc, cn = src.get_bits(8, 24), src.get_bits(32, 32)
    payload = bytes(_GALLAGHER_SCRAMBLE[b] for b in (
        (cn & 0xffffff) >> 16, (fc & 0xfff) >> 4, (cn & 0x7ff) >> 3,
        (cn & 0x7) << 5 | (rc & 0xf) << 1, (cn & 0xffff) >> 11,
        (fc & 0xffff) >> 12, 0, (fc & 0xf) << 4 | (il & 0xf)))
    enc = Bits(96)
    enc.set_bits(0, 0b01111111, 8)
    enc.set_bits(8, 0b11101010, 8)
    for i, byte in enumerate(payload):
        enc.set_bits(16 + i * 9, byte, 8)
        enc.set(16 + i * 9 + 8, 1 - (byte & 1))
    enc.set_bits(88, _crc8(payload, 0x7, 0x2c), 8)
    return _manchester(enc.list(), 16)


def noralsy(data):
    """Noralsy (protocol_noralsy_encoder_start): the 96 data bits are the
    frame; Manchester RF/32."""
    return _manchester(Bits(96, data).list(), 16)


def securakey(data):
    """Radio Key (protocol_securakey_encoder_start): with bytes 0-1 zero, a
    64-bit RKKTH frame (preamble, card bytes 2-5 between 0 spacers);
    otherwise a 96-bit RKKT frame of preamble, length 26 (byte 0 zero) or
    32, Wiegand parities, FC, card and bytes 4-5 between 0 spacers;
    Manchester RF/40."""
    src = Bits(48, data)
    if src.get_bits(0, 16) == 0:
        enc = Bits(64)
        enc.set_bits(0, 0b01111111, 8)
        enc.set_bits(8, 0b110, 3)
        for i, pos in enumerate((29, 38, 47, 56)):
            enc.copy(pos, 8, data, 16 + i * 8)
        return _manchester(enc.list(), 20)
    enc = Bits(96)
    enc.set_bits(0, 0b01111111, 8)
    enc.set_bits(8, 0b11001, 5)
    if src.get_bits(0, 8) == 0:
        enc.set_bits(13, 26, 6)
        enc.set(35, _parity(src.get_bits(8, 12)))
        enc.set(63, 1 - _parity(src.get_bits(20, 12)))
        enc.copy(36, 1, data, 8)
        enc.copy(38, 7, data, 9)
    else:
        enc.set_bits(13, 32, 6)
        enc.set(29, _parity(src.get_bits(2, 15)))
        enc.set(63, 1 - _parity(src.get_bits(17, 15)))
        enc.copy(30, 7, data, 2)
        enc.copy(38, 7, data, 3)                # sic: FC bits 3-9, not 9-15
    enc.copy(45, 1, data, 16)
    enc.copy(47, 8, data, 17)
    enc.copy(56, 7, data, 25)
    enc.copy(65, 8, data, 32)
    enc.copy(74, 8, data, 40)
    return _manchester(enc.list(), 20)


def viking(data):
    """Viking (protocol_viking_encoder_start): 0xF20000, the 32-bit ID and
    the XOR of all bytes with 0xA8; Manchester RF/32."""
    enc = Bits(64, b"\xF2\x00\x00")
    enc.copy(24, 32, data, 0)
    enc.set_bits(56, data[0] ^ data[1] ^ data[2] ^ data[3] ^ 0xF2 ^ 0xA8, 8)
    return _manchester(enc.list(), 16)


# -------------------------------------------- bi-phase and direct tags

def fdx_b(data):
    """FDX-B (protocol_fdx_b_encoder_start): 10000000000, then 13 groups of
    a 1 and a byte: data bytes 0-7, their CRC-16 (0x1021) high and low, and
    data bytes 8-10; bi-phase RF/32."""
    enc = Bits(128)
    enc.set(0, 1)
    for i in range(13):
        enc.set(11 + 9 * i, 1)
        if i in (8, 9):
            continue
        enc.copy(12 + 9 * i, 8, data, (i if i < 8 else i - 2) * 8)
    crc = _crc16(data[:8], 0x1021, 0x0000)
    enc.set_bits(84, crc >> 8, 8)
    enc.set_bits(93, crc & 0xFF, 8)
    return biphase(enc.list(), 16, 32)


def jablotron(data):
    """Jablotron (protocol_jablotron_encoder_start): 16 ones, the 40 data
    bits and their byte sum XOR 0x3A; bi-phase RF/64."""
    enc = Bits(64, b"\xFF\xFF" + bytes(data))
    enc.set_bits(56, (sum(data) & 0xFF) ^ 0x3A, 8)
    return biphase(enc.list(), 32, 64)


def gproxii(data):
    """Guardall G-Prox II (protocol_gproxii_encoder_yield): the 96 data bits
    are the frame; inverted bi-phase RF/64 (a 1 is two short pulses)."""
    return biphase(Bits(96, data).list(), 32, 64, double=1)


def pac_stanley(data):
    """PAC/Stanley (protocol_pac_stanley_encoder_start): 0xFF, then STX, "20"
    and the data as 8 hex digits, and their XOR, as 7-bit LSB-first
    characters with odd parity between start and stop bits; direct RF/32."""
    idbytes = b"20" + data.hex().upper().encode()
    enc = bytearray(0x40 >> ((i + 3) % 5 * 2) for i in range(16))
    enc[0] = 0xFF                               # mark + stop
    enc[1] = 0x20                               # start + reflect8(STX)
    checksum = 0
    for i in range(2, 13):
        shift = 7 - (i + 3) % 4 * 2
        index = i + (i - 1) // 4
        if i < 12:
            pattern = _rev8(idbytes[i - 2])
            pattern |= 1 - _parity(pattern)
            if i > 3:
                checksum ^= idbytes[i - 2]
        else:
            pattern = (_rev8(checksum) & 0xFE) | (1 - _parity(checksum))
        pattern <<= shift
        enc[index] |= pattern >> 8 & 0xFF
        enc[index + 1] |= pattern & 0xFF
    return nrz(Bits(128, enc).list(), 32)


PROTOCOLS = {
    # name: (encoder(data) -> [(level, cycles)], data size, modulation),
    # in the firmware's order (lfrfid_protocols.c)
    "EM4100":      (lambda d: em4100(d, 64), 5, "ask"),
    "EM4100/32":   (lambda d: em4100(d, 32), 5, "ask"),
    "EM4100/16":   (lambda d: em4100(d, 16), 5, "ask"),
    "Electra":     (electra, 8, "ask"),
    "H10301":      (h10301, 3, "fsk"),
    "Idteck":      (idteck, 8, "psk"),
    "Indala26":    (indala26, 4, "psk"),
    "IoProxXSF":   (io_prox_xsf, 4, "fsk"),
    "AWID":        (awid, 9, "fsk"),
    "FDX-A":       (fdx_a, 5, "fsk"),
    "FDX-B":       (fdx_b, 11, "ask"),
    "HIDProx":     (hid_generic, 6, "fsk"),
    "HIDExt":      (hid_ex_generic, 12, "fsk"),
    "Pyramid":     (pyramid, 4, "fsk"),
    "Viking":      (viking, 4, "ask"),
    "Jablotron":   (jablotron, 5, "ask"),
    "Paradox":     (paradox, 6, "fsk"),
    "PAC/Stanley": (pac_stanley, 4, "ask"),
    "Keri":        (keri, 4, "psk"),
    "Gallagher":   (gallagher, 8, "ask"),
    "Nexwatch":    (nexwatch, 8, "psk"),
    "Radio Key":   (securakey, 6, "ask"),
    "GProxII":     (gproxii, 12, "ask"),
    "Noralsy":     (noralsy, 12, "ask"),
}


# ------------------------------------------------- what the reader sees

def psk_demod(frame):
    """A PSK subcarrier (pairs of one cycle each level) as the reader's
    comparator sees it in PSK mode: the phase, as levels 2 cycles long."""
    cycles = [level for level, n in frame for _ in range(n)]
    return [(level, 2) for level in cycles[0::2]]


def comparator(frame, modulation="ask", invert=False):
    """The level/cycle frame as the reader's comparator sees it: signed µs
    (+ high, - low), equal levels merged.  ASK and FSK modulation reach it
    as they are; PSK as its phase."""
    if modulation == "psk":
        frame = psk_demod(frame)
    out = []
    for level, cycles in frame:
        high = (not level) if invert else bool(level)
        us = cycles * CYCLE_US
        if out and (out[-1] > 0) == high:
            out[-1] += us if high else -us
        else:
            out.append(us if high else -us)
    if len(out) > 1 and (out[0] > 0) == (out[-1] > 0):
        out[0] += out.pop()                     # the frame loops: join ends
    return out


def tag_string(frame, modulation):
    """The /machine/rfid tag string for a (level, cycles) frame."""
    durs = comparator(frame, modulation)
    return ("psk:" if modulation == "psk" else "") + ",".join(f"{d:+d}" for d in durs)


def encode(protocol, data):
    """(name, frame, modulation) for a protocol and its data bytes."""
    for name, (enc, size, mod) in PROTOCOLS.items():
        if name.lower() == protocol.lower():
            if len(data) != size:
                raise ValueError(f"{name} data is {size} bytes, got {len(data)}")
            return name, enc(bytes(data)), mod
    raise ValueError(f"no encoder for {protocol!r}; have {', '.join(PROTOCOLS)}")


def tag(protocol, data):
    """The /machine/rfid tag string for a protocol and its data bytes."""
    _, frame, mod = encode(protocol, data)
    return tag_string(frame, mod)


# ------------------------------------------- emulation captures (rfid-tx)

def parse_capture(path):
    """Signed carrier cycles (+ load on) from a FlipperEmu RFID capture."""
    cycles, kind = [], None
    for line in pathlib.Path(path).read_text().splitlines():
        key, _, value = line.partition(":")
        if key == "Filetype":
            kind = value.strip()
        elif key == "Cycles":
            cycles += [int(x) for x in value.split()]
    if kind != "FlipperEmu RFID capture":
        raise ValueError(f"{path}: not a FlipperEmu RFID capture")
    return cycles


def _per_cycle(cycles):
    return bytes(1 if c > 0 else 0 for c in cycles for _ in range(abs(c)))


def capture_frame(cycles):
    """The repeating frame of a capture, as (level, cycles), and its
    modulation: a tag repeats its frame for as long as it is powered, so the
    frame is the shortest period of the capture.  It is looked for from the
    middle, clear of a glitch in the first half (after an infrared
    transmission the firmware's first DMA refill lands on the half being
    sent), and must hold for all the rest, as parts of a frame can repeat
    too (a run of 0 bits).  If no period does, a later glitch broke it (a
    DMA refill can fall behind): then the frame is the period that holds
    the longest, at least twice, from the middle, else from cycle 1000
    (which also serves short captures), the shortest if several hold as
    long."""
    seq = _per_cycle(cycles)
    end = len(seq) - abs(cycles[-1])            # the last level may be cut short
    probe, found, best = 2000, False, None      # best: (cycles held, -period, body)
    if end < 1000 + 2 * probe:
        raise ValueError("capture too short to find its frame")
    for skip in (len(seq) // 2 & ~1, 1000):     # even: PSK is sampled in pairs
        if best is not None or end - skip < 2 * probe:
            continue
        window, period = seq[skip:skip + probe], 0
        while True:
            period = seq.find(window, skip + period + 1) - skip
            if period <= 0 or skip + 2 * period > end:
                break
            found = True
            ahead, back = seq[skip + period:end], seq[skip:end - period]
            if ahead == back:                   # holds for all the rest
                return _frame_of(seq[skip:skip + period])
            diff = int.from_bytes(ahead, "big") ^ int.from_bytes(back, "big")
            held = len(ahead) - (diff.bit_length() + 7) // 8 + period
            if held >= 2 * period and (best is None or (held, -period) > best[:2]):
                best = held, -period, seq[skip:skip + period]
    if best is None:
        raise ValueError("the capture does not repeat exactly" if found else
                         "no repeating frame in the capture (is it longer "
                         "than two frames?)")
    return _frame_of(best[2])


def _frame_of(body):
    """(level, cycles) runs and modulation of a frame given per cycle."""
    frame, run = [], 0
    for i, level in enumerate(body):
        run += 1
        if i + 1 == len(body) or body[i + 1] != level:
            frame.append((level, run))
            run = 0
    short = sum(n for _, n in frame if n <= 2)
    return frame, "psk" if short > len(body) // 2 else "ask"


def same_frame(a, b):
    """Are two (level, cycles) frames the same loop (rotations allowed)?"""
    sa, sb = _per_cycle([n if l else -n for l, n in a]), \
        _per_cycle([n if l else -n for l, n in b])
    return len(sa) == len(sb) and sb in sa + sa


def parse_hex(text):
    text = text.replace(":", " ").replace("-", " ")
    parts = text.split()
    if len(parts) == 1 and len(parts[0]) > 2:   # "DEADBEEF01"
        return bytes.fromhex(parts[0])
    return bytes(int(x, 16) for x in parts)


def parse_rfid(path):
    """(protocol, data) from a Flipper .rfid file."""
    fields = {}
    for line in pathlib.Path(path).read_text().splitlines():
        key, _, value = line.partition(":")
        fields[key.strip()] = value.strip()
    if fields.get("Filetype") != "Flipper RFID key":
        raise ValueError(f"{path}: not a Flipper RFID key file")
    return fields["Key type"], parse_hex(fields["Data"])


def main():
    args = sys.argv[1:]
    if args == ["--list"]:
        for name, (_, size, mod) in PROTOCOLS.items():
            print(f"{name:12} {size} bytes  {mod}")
        return 0
    if len(args) == 1 and args[0].endswith(".rfid"):
        print(tag(*parse_rfid(args[0])))
    elif len(args) == 1 and pathlib.Path(args[0]).is_file():
        print(tag_string(*capture_frame(parse_capture(args[0]))))
    elif len(args) >= 2:
        print(tag(args[0], parse_hex(" ".join(args[1:]))))
    else:
        print(__doc__)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
