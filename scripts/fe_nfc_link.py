#!/usr/bin/env python3
# Copyright (c) 2026, Kara Zajac
# SPDX-License-Identifier: BSD-3-Clause
"""A card in the emulated Flipper's NFC field, played by a program.

    ./flipperemu nfc --link CARD [options]       (a running device)
    fe_nfc_link.py devices/NAME/nfc.sock CARD [options]

CARD:
    iso-dep          an ISO14443-4A card (ISO-DEP): the Flipper's NFC app reads
                     it, and `apdu` in its NFC shell talks to it.  It answers
                     SELECT (any AID), GET CHALLENGE and GET DATA; anything
                     else 6D00.  --uid, --sak, --ats
    log              no card: print every frame the Flipper's reader sends
    script FILE.py   a card of your own: FILE defines `card(args)` returning a
                     Card (below) whose frame() decides every answer

With no card set on the device (`./flipperemu nfc none`), each frame the
firmware sends as a reader comes here: its data bits, CRC included, and the
parity bit of each byte (MIFARE Classic's are encrypted).  The answer goes
back as the card's.  QEMU stops the machine while the answer is worked out,
so it may take as long as it likes, within a second: a Python card, or a
relay to real hardware over USB, keeps within the firmware's frame waiting
time all the same.  The protocol is in docs/RADIO_LINK.md.

A card of your own, for card types the emulator does not model, or to see
what the firmware does with answers no real card would give:

    import fe_nfc_link as nl

    class Mine(nl.IsoDep):
        def apdu(self, cmd):
            return bytes.fromhex("9000")

    def card(args):
        return Mine(uid=bytes.fromhex("04112233445566"))
"""
import argparse
import importlib.util
import os
import pathlib
import socket
import sys


def crc_a(data):
    """ISO14443A's CRC_A (x^16 + x^12 + x^5 + 1, from 0x6363), LSB first."""
    crc = 0x6363
    for b in data:
        b ^= crc & 0xFF
        b = (b ^ (b << 4)) & 0xFF
        crc = (crc >> 8) ^ (b << 8) ^ (b << 3) ^ (b >> 4)
    return bytes([crc & 0xFF, crc >> 8])


def with_crc(data):
    return bytes(data) + crc_a(data)


def crc_ok(frame):
    return len(frame) >= 3 and crc_a(frame[:-2]) == bytes(frame[-2:])


class Card:
    """What sits in the field.  frame() gets every frame the Flipper sends
    (nbits, data bytes, the parity bits of the full bytes packed LSB first)
    and returns its answer: None for silence, bytes (whole bytes, odd
    parity added), or (nbits, data[, parity]) for anything else."""
    name = "a card"

    def field(self, on):
        pass

    def frame(self, nbits, data, parity):
        return None


class Iso14443A(Card):
    """ISO14443-3A activation, as a card does it: REQA/WUPA get the ATQA;
    anticollision and SELECT, cascade level by cascade level, the UID (4, 7
    or 10 bytes) and the SAK; HLTA puts it to sleep (WUPA wakes it).  Once
    selected, frames with a good CRC go to active()."""
    IDLE, READY, ACTIVE, HALT = range(4)

    def __init__(self, uid, atqa=None, sak=0x20):
        self.uid = bytes(uid)
        if len(self.uid) not in (4, 7, 10):
            raise ValueError("a UID is 4, 7 or 10 bytes")
        levels = {4: 1, 7: 2, 10: 3}[len(self.uid)]
        self.atqa = atqa if atqa is not None else (0x0004, 0x0044, 0x0084)[levels - 1]
        self.sak = sak
        u = self.uid
        self.cl = ([u[0:4]] if levels == 1 else
                   [b"\x88" + u[0:3], u[3:7]] if levels == 2 else
                   [b"\x88" + u[0:3], b"\x88" + u[3:6], u[6:10]])
        self.state = self.IDLE
        self.name = f"ISO14443A UID {self.uid.hex(' ').upper()}"

    def field(self, on):
        self.state = self.IDLE
        self.reset()

    def reset(self):
        """Back to unselected (the field went off, or HLTA)."""

    def frame(self, nbits, data, parity):
        if nbits == 7:                          # REQA, WUPA
            if data[0] == 0x26 and self.state in (self.IDLE, self.READY) or \
               data[0] == 0x52 and self.state in (self.IDLE, self.HALT, self.READY):
                self.state = self.READY
                return bytes([self.atqa & 0xFF, self.atqa >> 8])
            return None
        if nbits % 8:
            return None
        if self.state == self.READY and len(data) >= 2 and data[0] in (0x93, 0x95, 0x97):
            level = (data[0] - 0x93) // 2
            if level >= len(self.cl):
                return None
            part = self.cl[level]
            bcc = part[0] ^ part[1] ^ part[2] ^ part[3]
            if data[1] == 0x20:                 # anticollision: the whole level
                return part + bytes([bcc])
            if data[1] == 0x70 and len(data) == 9 and crc_ok(data):
                if data[2:6] != part:
                    return None
                last = level == len(self.cl) - 1
                if last:
                    self.state = self.ACTIVE
                return with_crc([self.sak if last else 0x04])
            return None
        if self.state != self.ACTIVE or not crc_ok(data):
            return None
        if data[0] == 0x50 and data[1] == 0x00:  # HLTA
            self.state = self.HALT
            self.reset()
            return None
        return self.active(bytes(data[:-2]))

    def active(self, frame):
        """A frame (CRC checked and stripped) to the selected card."""
        return None


class IsoDep(Iso14443A):
    """ISO14443-4A on top: RATS gets the ATS; I-blocks carry APDUs to
    apdu() (chained ones are put together, each part acknowledged); an
    R(NAK) gets the last block again, S(DESELECT) deselects."""

    def __init__(self, uid, atqa=None, sak=0x20, ats=bytes.fromhex("0578807000")):
        super().__init__(uid, atqa, sak)
        self.ats = bytes(ats)
        self.name = f"ISO14443-4A card, UID {self.uid.hex(' ').upper()}"
        self.reset()

    def reset(self):
        self.rats = False
        self.chain = b""
        self.last = None

    def active(self, f):
        if not self.rats:
            if f[0] == 0xE0 and len(f) == 2:    # RATS
                self.rats = True
                return with_crc(self.ats)
            return None
        pcb = f[0]
        if pcb & 0xC0 == 0x00:                  # I-block
            inf = f[1 + bool(pcb & 0x08) + bool(pcb & 0x04):]
            if pcb & 0x10:                      # chaining: more to come
                self.chain += inf
                self.last = with_crc([0xA2 | (pcb & 1)])
                return self.last
            resp = self.apdu(self.chain + inf)
            self.chain = b""
            self.last = with_crc(bytes([0x02 | (pcb & 1)]) + bytes(resp))
            return self.last
        if pcb & 0xF6 == 0xB2:                  # R(NAK): again
            return self.last
        if pcb & 0xF6 == 0xA2:                  # R(ACK)
            return self.last
        if pcb & 0xF7 == 0xC2:                  # S(DESELECT)
            self.state = self.HALT
            self.reset()
            return with_crc([0xC2])
        return None

    def apdu(self, cmd):
        """An APDU in, its response (data and SW1 SW2) out."""
        return bytes.fromhex("6D00")


class DemoIsoDep(IsoDep):
    """The `iso-dep` card: SELECT any AID (an FCI naming it), GET CHALLENGE
    (8 bytes), GET DATA (the UID); anything else is 6D00."""

    def apdu(self, cmd):
        if len(cmd) < 4:
            return bytes.fromhex("6700")
        cla, ins, p1, p2 = cmd[:4]
        if ins == 0xA4 and p1 == 0x04 and len(cmd) >= 5:     # SELECT by AID
            aid = cmd[5:5 + cmd[4]]
            return bytes([0x6F, len(aid) + 2, 0x84, len(aid)]) + aid + b"\x90\x00"
        if ins == 0x84:                                       # GET CHALLENGE
            return os.urandom(8) + b"\x90\x00"
        if ins == 0xCA:                                       # GET DATA: the UID
            return self.uid + b"\x90\x00"
        return bytes.fromhex("6D00")


class Log(Card):
    """No card: the Flipper's frames, printed."""
    name = "nothing (logging the reader's frames)"

    def field(self, on):
        print(f"[fe_nfc_link] field {'on' if on else 'off'}", flush=True)

    def frame(self, nbits, data, parity):
        print(f"[fe_nfc_link] reader: {nbits:4} bits  {data.hex(' ').upper()}", flush=True)
        return None


def run(path, card, verbose=False):
    """Be `card` on the link at `path` until the emulator goes or ^C."""
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.connect(str(path))
    sock.sendall(f"hello fe_nfc_link: {card.name}\n".encode())
    print(f"[fe_nfc_link] {card.name} -> {path}", flush=True)
    buf = b""
    try:
        while True:
            data = sock.recv(65536)
            if not data:
                return 0
            buf += data
            *lines, buf = buf.split(b"\n")
            for raw in lines:
                words = raw.decode(errors="replace").split()
                if not words:
                    continue
                if words[0] == "field" and len(words) > 1:
                    card.field(words[1] == "on")
                elif words[0] == "poll" and len(words) >= 3:
                    nbits = int(words[1])
                    frame = bytes.fromhex(words[2])
                    par = bytes.fromhex(words[3]) if len(words) > 3 else b""
                    ans = card.frame(nbits, frame, par)
                    if verbose:
                        print(f"  > {frame.hex(' ').upper():40}  < "
                              f"{answer_text(ans)}", flush=True)
                    sock.sendall(answer_line(ans).encode())
    except KeyboardInterrupt:
        print()
        return 0
    finally:
        sock.close()


def answer_line(ans):
    if ans is None:
        return "silence\n"
    if isinstance(ans, (bytes, bytearray, list)):
        ans = (8 * len(ans), bytes(ans))
    nbits, data = ans[0], bytes(ans[1])
    par = (" " + bytes(ans[2]).hex()) if len(ans) > 2 and ans[2] is not None else ""
    return f"answer {nbits} {data.hex()}{par}\n"


def answer_text(ans):
    if ans is None:
        return "-"
    data = bytes(ans) if isinstance(ans, (bytes, bytearray, list)) else bytes(ans[1])
    return data.hex(" ").upper()


def make_card(spec, args):
    uid = bytes.fromhex(args.uid.replace(":", "").replace(" ", "")) if args.uid else \
        bytes.fromhex("04A1B2C3D4E5F6")
    if spec == "iso-dep":
        return DemoIsoDep(uid, sak=args.sak if args.sak is not None else 0x20,
                          ats=bytes.fromhex(args.ats) if args.ats else
                          bytes.fromhex("0578807000"))
    if spec == "log":
        return Log()
    if spec.startswith("script"):
        path = spec.split(":", 1)[1] if ":" in spec else args.script
        if not path:
            raise ValueError("script: say which file (script:FILE.py)")
        sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
        mod_spec = importlib.util.spec_from_file_location("nfc_card_script", path)
        mod = importlib.util.module_from_spec(mod_spec)
        mod_spec.loader.exec_module(mod)
        return mod.card(args)
    raise ValueError(f"unknown card {spec!r}: iso-dep, log, script:FILE.py")


def add_options(p):
    """The link card's options, shared with ./flipperemu nfc --link."""
    p.add_argument("--uid", help="iso-dep: the UID (4, 7 or 10 bytes hex; "
                   "default 04A1B2C3D4E5F6)")
    p.add_argument("--sak", type=lambda v: int(v, 0), help="iso-dep: SAK (default 0x20)")
    p.add_argument("--ats", help="iso-dep: the ATS, hex (default 0578807000)")
    p.add_argument("--verbose", action="store_true", help="print every exchange")


def main():
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0],
                                formatter_class=argparse.RawDescriptionHelpFormatter,
                                epilog=__doc__.split("\n\n", 1)[1])
    p.add_argument("link", help="the device's NFC link socket (devices/NAME/nfc.sock), "
                   "or its directory")
    p.add_argument("card", help="iso-dep, log, or script:FILE.py")
    add_options(p)
    args = p.parse_args()
    link = pathlib.Path(args.link)
    if link.is_dir():
        link = link / "nfc.sock"
    try:
        return run(link, make_card(args.card, args), args.verbose)
    except (OSError, ValueError) as e:
        sys.exit(f"fe_nfc_link: {e}")


if __name__ == "__main__":
    sys.exit(main())
