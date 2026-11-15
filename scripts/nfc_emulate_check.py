#!/usr/bin/env python3
# Copyright (c) 2026, Kara Zajac
# SPDX-License-Identifier: BSD-3-Clause
"""Check the cards that the firmware emulates, with readers in front of it.

    scripts/nfc_emulate_check.py firmware/official-1.5.1/flipper-z-f7-update-1.5.1.tgz

Boots a throwaway device.  A card's file is put on its SD card and emulated
by the firmware (`emulate -f` in its NFC shell); a reader then reads the
Flipper, and what it read has to be the card's file again.
An NTAG215 by the machine's own reader (`./flipperemu nfc --reader ntag`).
MIFARE Classic Mini, 1K and 4K by a reader that is a program on the NFC link
and has the card's keys (`nfc --reader classic CARD.nfc`): the firmware sends
those answers itself, edge by edge, in the chip's transparent mode, with
parity bits that Crypto1 has encrypted; every block and both keys of every
sector come back, a sector whose data is for key B alone among them.  A
reader with a wrong key for one sector reads the others, and the firmware
goes on emulating.  With nothing emulated the reader says so.
An ISO14443-4A card by a reader that asks for its ATS (`nfc --reader iso-dep`).
A FeliCa Lite-S by a FeliCa reader without the card key (`nfc --reader
felica`).  A Type 4 tag whose NDEF message of 910 bytes is read through
ISO14443-4's blocks (`nfc --reader t4t`), where the firmware emulates one;
none here does from its shell, so that reader also reads a tag that is
played in Python.  `nfc record` writes every frame on the air and its answer
to a file, both ways, and `nfc watch` shows them as they happen.
Needs the firmware's NFC shell (1.4 and later); without it nothing is
checked and the exit status is 77.  Exit status 0 = pass.
"""
import argparse
import json
import os
import pathlib
import re
import shutil
import sys
import tempfile
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from files_check import Bench, CRASH, check, results         # noqa: E402
from nfc_check import ISO4A, PROMPT, Shell                   # noqa: E402
import check_limit  # noqa: E402
import fe_nfc  # noqa: E402
import fe_nfc_link  # noqa: E402
import fe_nfc_reader  # noqa: E402

SKIPPED = 77


class Emulated:
    """A card's file on the Flipper's SD card, emulated by the NFC shell
    for as long as the block lasts"""

    def __init__(self, b, work, name, text):
        self.b, self.name = b, name
        self.path = work / f"{name}.nfc"
        self.path.write_text(text)
        self.on = False

    def __enter__(self):
        self.b.fe("rm", f"/ext/nfc/fe-emu-{self.name}.nfc")
        (self.path.parent / f"fe-emu-{self.name}.nfc").write_text(self.path.read_text())
        self.b.fe("push", self.path.parent / f"fe-emu-{self.name}.nfc", "/ext/nfc")
        self.sh = Shell(self.b)
        # ("Emulating", or the prompt again with the reason why not: a
        # card that this firmware does not know, or does not emulate)
        found, out = self.sh.cli.watch(f"emulate -f /ext/nfc/fe-emu-{self.name}.nfc",
                                       r"Emulating|" + PROMPT, timeout=15,
                                       stop=False)
        self.on, self.said = bool(found) and "Emulating" in out, out
        return self

    def __exit__(self, *exc):
        try:
            if self.on:
                self.sh.cli.write(b"\x03")
                time.sleep(0.3)
                self.sh.cli.read_until_prompt(8)
        except (OSError, TimeoutError):
            pass
        self.sh.close()
        self.b.fe("rm", f"/ext/nfc/fe-emu-{self.name}.nfc")
        return False


def same(card, read, but=()):
    """The keys of a .nfc text that the other does not have the same"""
    want, got = fe_nfc.parse_nfc_text(card), fe_nfc.parse_nfc_text(read)
    return sorted(k for k in set(want) | set(got) if k not in but
                  and (want.get(k) or "").upper() != (got.get(k) or "").upper())


def last_read(b):
    code, text = b.fe("status", "--json")
    try:
        return json.loads(text)["nfc"]["last_read"]
    except (ValueError, KeyError):
        return None


def classic(b, work, kind, uid):
    card = fe_nfc.sample_classic(bytes.fromhex(uid), kind)
    blocks = sum(1 for line in card.splitlines() if line.startswith("Block "))
    with Emulated(b, work, f"mfc{kind.lower()}", card) as emu:
        t = time.time()
        code, text = b.fe("nfc", "--reader", "classic", emu.path, "--quiet-read")
        took = time.time() - t
        path = last_read(b)
        read = pathlib.Path(path).read_text() if path and os.path.exists(path) else ""
        differ = same(card, read)
        check(f"a MIFARE Classic {kind} that the firmware emulates, read by a "
              f"reader with its keys: {blocks} of {blocks} blocks and both "
              "keys of every sector are the card's (Crypto1 both ways, nested "
              "authentication, the firmware's answers from the chip's "
              "transparent mode); `status` names the file",
              emu.on and code == 0 and f"{blocks} of {blocks} blocks" in text
              and read and not differ,
              f"{emu.said[-200:]}\n{text}\n" + "\n".join(
                  f"{k}: {fe_nfc.parse_nfc_text(card).get(k)} | "
                  f"{fe_nfc.parse_nfc_text(read).get(k) if read else None}"
                  for k in differ[:8]) + f"\n({took:.1f} s)")
        if kind != "1K":
            return
        # a reader that has another key A and B for sector 3: that sector's
        # blocks are not read, the others are, and the card is still there
        lines = card.splitlines()
        at = next(i for i, line in enumerate(lines) if line.startswith("Block 15:"))
        lines[at] = "Block 15: 11 22 33 44 55 66 FF 07 80 69 66 55 44 33 22 11"
        wrong = work / "mfc1k-wrong.nfc"
        wrong.write_text("\n".join(lines) + "\n")
        code, text = b.fe("nfc", "--reader", "classic", wrong, "--quiet-read")
        path = last_read(b)
        read = pathlib.Path(path).read_text() if path and os.path.exists(path) else ""
        differ = same(card, read)
        code2, text2 = b.fe("nfc", "--reader", "classic", emu.path, "--quiet-read")
        check("a reader with wrong keys for sector 3: exit status 1, 60 of 64 "
              "blocks, that sector's four are the ones not read; the firmware "
              "goes on emulating, and the reader with the keys reads all 64 "
              "after it",
              code == 1 and "60 of 64 blocks" in text
              and differ == ["Block 12", "Block 13", "Block 14", "Block 15"]
              and code2 == 0 and "64 of 64 blocks" in text2,
              f"{text}\n{differ}\n{text2}")


def read_back(b, emu, card, reader, words, wants):
    """A reader that is a program, and the file it wrote against the card's"""
    code, text = b.fe("nfc", "--reader", reader, "--quiet-read")
    path = last_read(b)
    read = pathlib.Path(path).read_text() if path and os.path.exists(path) else ""
    differ = same(card, read)
    check(words, code == 0 and wants in text and read and not differ,
          f"{emu.said[-200:]}\n{text}\n" + "\n".join(
              f"{k}: {fe_nfc.parse_nfc_text(card).get(k)} | "
              f"{fe_nfc.parse_nfc_text(read).get(k) if read else None}"
              for k in differ[:8]))


def iso_dep(b, work):
    """ISO14443-4A: the firmware answers a RATS with the card's ATS"""
    with Emulated(b, work, "iso4a", ISO4A) as emu:
        if not check("the firmware emulates an ISO14443-4A card's file", emu.on,
                     emu.said[-300:]):
            return
        read_back(b, emu, ISO4A, "iso-dep",
                  "an ISO14443-4A card that the firmware emulates, activated by "
                  "an ISO14443-4 reader (RATS): UID, ATQA, SAK and each part of "
                  "the ATS are the card's", "an ISO14443-4A card")


T4T_TEXT = ("FlipperEmu's long NDEF text record. " * 25)[:900]


def t4t(b, work):
    """An NFC Forum Type 4 tag, on the firmwares that emulate one: the NDEF
    message read through ISO14443-4's blocks, 900 bytes in several reads.
    The check's last part with the firmware: a shell that knows no such
    card can end the firmware (Momentum mntm-012's does, FW-19 in
    docs/FIRMWARE_BUGS.md), which is said and not counted."""
    card = fe_nfc.sample_t4t(bytes.fromhex("04D1E2F3A4B5C6"), text=T4T_TEXT)
    before = len(CRASH.findall(b.uart_text(every=True)))
    with Emulated(b, work, "t4t", card) as emu:
        if not emu.on:
            time.sleep(1.0)
            if len(CRASH.findall(b.uart_text(every=True))) > before:
                print("  (this firmware's NFC shell crashes when it is asked to "
                      "emulate a Type 4 tag's file: the firmware's own)")
            else:
                print("  (this firmware does not emulate a Type 4 tag: "
                      + " ".join(emu.said.split()[-8:]) + ")")
            return
        read_back(b, emu, card, "t4t",
                  "a Type 4 tag that the firmware emulates, read by a reader "
                  "of NFC Forum Type 4 tags (the NDEF application, the "
                  "capability container, the NDEF file in the reads that the "
                  "container allows): its NDEF message of 910 bytes is the "
                  "card's", "an NDEF message of 910 bytes")
    crash = CRASH.search(b.uart_text(every=True))
    check("no crash in the firmware's logs after it", not crash)


class Loop:
    """A reader's link (fe_nfc_reader.ReaderLink's part of it) straight to
    a card of fe_nfc_link's, with no Flipper between them"""

    def __init__(self, card):
        self.card = card
        self.frames = 0

    def on(self):
        self.card.field(True)

    def off(self):
        self.card.field(False)

    def activate(self):
        atqa = self.card.frame(7, b"\x52", b"")
        uid, sak = b"", None
        for sel in (0x93, 0x95, 0x97):
            part = atqa and self.card.frame(16, bytes((sel, 0x20)), b"")
            if not part:
                return None
            sak = self.card.frame(72, fe_nfc_link.with_crc(
                bytes((sel, 0x70)) + part), b"")
            if not sak:
                return None
            uid += part[1:4] if part[0] == 0x88 else part[:4]
            if not sak[0] & 0x04:
                break
        return uid, atqa, sak[0]

    def send(self, data, nbits=None, parity=None):
        self.frames += 1
        answer = self.card.frame(nbits or len(data) * 8, bytes(data), b"")
        if not answer:
            raise fe_nfc_reader.Silence()
        return len(answer) * 8, bytes(answer), []


class T4tCard(fe_nfc_link.IsoDep):
    """An NFC Forum Type 4 tag for the link: the NDEF application, its
    capability container (59 bytes a read) and its NDEF file"""

    def __init__(self, uid, message):
        super().__init__(uid, atqa=0x0344)
        ndef = len(message).to_bytes(2, "big") + message
        cc = bytes.fromhex("000F20003B00340406E104") + \
            len(ndef).to_bytes(2, "big") + b"\x00\x00"
        self.files = {b"\xE1\x03": cc, b"\xE1\x04": ndef}
        self.app, self.file = False, None

    def apdu(self, cmd):
        if len(cmd) < 5:
            return bytes.fromhex("6700")
        ins, p1, p2 = cmd[1:4]
        if ins == 0xA4 and p1 == 0x04:
            self.app = cmd[5:5 + cmd[4]] == fe_nfc_reader.T4T_APP
            return bytes.fromhex("9000" if self.app else "6A82")
        if ins == 0xA4 and self.app:
            self.file = self.files.get(bytes(cmd[5:7]))
            return bytes.fromhex("9000" if self.file else "6A82")
        if ins == 0xB0 and self.file:
            at = p1 << 8 | p2
            if cmd[4] > 0x3B:
                return bytes.fromhex("6700")    # more than the container allows
            return self.file[at:at + cmd[4]] + b"\x90\x00"
        return bytes.fromhex("6D00")


def t4t_reader():
    """No firmware here emulates a Type 4 tag in its shell, so that reader
    reads one that is played in Python: no emulator in this part"""
    uid = bytes.fromhex("04D1E2F3A4B5C6")
    card = fe_nfc.sample_t4t(uid, text=T4T_TEXT)
    message = bytes.fromhex("".join(
        line.split(":")[1] for line in card.splitlines()
        if line.startswith("NDEF Data:")).replace(" ", ""))
    link = Loop(T4tCard(uid, message))
    link.on()
    try:
        text, length = fe_nfc_reader.read_t4t(link)
    except (RuntimeError, fe_nfc_reader.Silence) as e:
        text, length = str(e), None
    differ = same(card, text) if length else ["nothing read"]
    check("the reader of Type 4 tags, with a tag that is played in Python "
          "(the link's ISO14443-4 card, no Flipper between them): the NDEF "
          "application, the capability container, and the NDEF message of "
          f"910 bytes in reads of 59, {link.frames} frames; the file it "
          "writes has the message (and that card's own ATS)",
          length == 910 and differ == ["T0", "T1...Tk", "TA(1)", "TB(1)", "TC(1)"]
          and link.frames > 18, f"{differ}\n{text[:300]}")


def felica(b, work):
    """FeliCa Lite-S: the chip answers the polling from what the firmware
    loaded, the firmware the reads"""
    card = fe_nfc.sample_felica(bytes.fromhex("012E4CD21A8B3C5F"), keyed=False)
    with Emulated(b, work, "felica", card) as emu:
        if not check("the firmware emulates a FeliCa Lite-S's file", emu.on,
                     emu.said[-300:]):
            return
        code, text = b.fe("nfc", "--reader", "felica", "--quiet-read")
        path = last_read(b)
        read = pathlib.Path(path).read_text() if path and os.path.exists(path) else ""
        differ = same(card, read, but=("Blocks read",))
        # (the dev build and the forks answer the polling with a PMm of
        # their own, 00 F1 FF FF FF FF FF FF; the card's is in its D_ID block)
        pmm = "Manufacture parameter"
        own = pmm in differ and \
            fe_nfc.parse_nfc_text(read).get(pmm) == "00 F1 FF FF FF FF FF FF"
        check("a FeliCa Lite-S that the firmware emulates, polled and read by "
              "a FeliCa reader that has no card key: IDm, "
              + ("a PMm that is this firmware's own" if own else "PMm")
              + " and 25 of its 28 blocks; the two blocks that are kept for "
              "the key are refused (01 B1), as the card's file has them.  The "
              "firmware's own: the card key reads as zeros where the file has "
              "a refusal, and MAC_A is refused before a challenge was written",
              code == 0 and "25 of 28 blocks" in text and read
              and [k for k in differ if not (own and k == pmm)]
              == ["Block 22", "Block 25"],
              f"{text}\n" + "\n".join(
                  f"{k}: {fe_nfc.parse_nfc_text(card).get(k)} | "
                  f"{fe_nfc.parse_nfc_text(read).get(k) if read else None}"
                  for k in differ[:8]))


def record(b, work):
    """`nfc record`, `nfc watch`: the frames on the air and their answers,
    the firmware's to a card and a reader's to the card that it emulates"""
    rec = work / "air.txt"
    none = b.fe("nfc", "record")
    began = b.fe("nfc", "record", rec)
    twice = b.fe("nfc", "record", work / "other.txt")
    watch = b.bg("nfc", "watch", "--for", "6")
    time.sleep(1.0)
    b.fe("nfc", "ntag215", "04A1B2C3D4E5F6", "https://flipperzero.one")
    sh = Shell(b)
    try:
        sh.ask("scanner", 30)
    finally:
        sh.close()
    b.fe("nfc", "none")
    card = fe_nfc.sample_classic(bytes.fromhex("5E6F7081"), "MINI")
    with Emulated(b, work, "rec-mini", card) as emu:
        read = b.fe("nfc", "--reader", "classic", emu.path, "--quiet-read")
    try:
        watched = watch.communicate(timeout=30)[0]
    except Exception:                   # (a watch that does not end)
        watch.kill()
        watched = watch.communicate()[0]
    runs = b.fe("nfc", "record")
    ended = b.fe("nfc", "record", "stop")
    after = b.fe("nfc", "record", "stop")
    lines = [line for line in (rec.read_text() if rec.exists() else "").splitlines()
             if line and not line.startswith("#")]
    said = [line.split(" ", 1)[1] for line in lines]
    times = [float(line.split(" ", 1)[0]) for line in lines]
    has = lambda pattern: any(re.fullmatch(pattern, s) for s in said)
    frames = sum(" > " in s for s in said)
    check("`nfc record FILE`: every frame on the air and its answer in a text "
          f"file ({frames} frames here), in the order of their times.  The "
          "firmware as a reader: its field, the WUPA and the ATQA, the SELECT "
          "with the card's UID, a frame with no answer.  A reader in front of "
          "the Flipper as a MIFARE Classic: its field, the card selected, the "
          "AUTH and the nonce, and encrypted frames with the parity bits of "
          "both sides.  `nfc record` says whether one runs, a second is "
          "refused, `nfc record stop` ends it and says where it is",
          "not being recorded" in none[1] and began[0] == 0 and twice[0] != 0
          and "already" in twice[1] and "frames in" in runs[1] and ended[0] == 0
          and str(rec) in ended[1] and after[0] != 0 and read[0] == 0
          and times == sorted(times) and frames > 60
          and "flipper field on" in said and "flipper a 7 52 > 16 4400" in said
          and "flipper a 72 93708804A1B29FAE4B > 24 04DA17" in said
          and has(r"flipper \w \d+ \w+ > -")
          and "reader field on" in said
          and "reader a select > 5E6F7081 0400 09" in said
          and has(r"reader a 32 6000F57B > 32 \w{8}")
          and has(r"reader a 32 \w{8} p\w\w > 144 \w{36} p\w{6}"),
          "\n".join([none[1], began[1], twice[1], runs[1], ended[1], after[1]]
                    + said[:12] + [s for s in said if s.startswith("reader")][:8]))
    shown = [line for line in watched.splitlines() if " > " in line]
    check("`nfc watch --for 6` beside it: the same frames as they happen, "
          f"from the record that runs ({len(shown)} here), and its end by itself",
          watch.returncode == 0 and len(shown) > 20
          and any("flipper a 7 52 > 16 4400" in line for line in shown)
          and "from the record that runs" in watched, watched[-600:])


def ntag(b, work):
    uid = bytes.fromhex("04A1B2C3D4E5F6")
    card = fe_nfc.nfc_file(fe_nfc.blank("ntag215", uid, "https://flipperzero.one"))
    with Emulated(b, work, "ntag215", card) as emu:
        code, text = b.fe("nfc", "--reader", "ntag")
        want = [line for line in card.splitlines() if line.startswith("Page ")]
        got = [line for line in text.splitlines() if line.startswith("Page ")]
        check("an NTAG215 that the firmware emulates, read by the machine's "
              "own reader: its 135 pages",
              emu.on and code == 0 and len(want) == 135 and got == want,
              f"{emu.said[-200:]}\n{text[-600:]}")


def nothing(b, work):
    card = work / "none.nfc"
    card.write_text(fe_nfc.sample_classic(bytes.fromhex("0A0B0C0D")))
    code, text = b.fe("nfc", "--reader", "classic", card, "--quiet-read")
    other = work / "ntag.nfc"
    other.write_text(fe_nfc.nfc_file(fe_nfc.blank(
        "ntag215", bytes.fromhex("04A1B2C3D4E5F6"), "https://flipperzero.one")))
    code2, text2 = b.fe("nfc", "--reader", "classic", other)
    check("with nothing emulated the reader says that it read nothing and "
          "asks whether the Flipper emulates (exit status 1); a file of "
          "another kind of card is refused by name",
          code != 0 and "nothing read" in text and "emulating" in text
          and code2 != 0 and "not a MIFARE Classic" in text2, f"{text}\n{text2}")


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("firmware")
    ap.add_argument("--timeout", type=float, default=120.0,
                    help="seconds to wait for the firmware to start")
    args = ap.parse_args()
    check_limit.arm(600)

    b = Bench(f"nfcemu-{int(time.time())}-{os.getpid()}", args.firmware,
              args.timeout)
    work = pathlib.Path(tempfile.mkdtemp(prefix="fe-nfcemu-")).resolve()
    skipped = False
    try:
        if check("boot: 'Startup complete'", b.start(first=True),
                 b.uart_text()[-2000:]):
            b.fe("wait", "--timeout", args.timeout)
            sh = Shell(b)
            sh.close()
            if not sh.there or "emulate" not in sh.commands:
                skipped = True
            else:
                nothing(b, work)
                ntag(b, work)
                classic(b, work, "1K", "A1B2C3D4")
                classic(b, work, "MINI", "5E6F7081")
                classic(b, work, "4K", "9A8B7C6D")
                iso_dep(b, work)
                felica(b, work)
                record(b, work)
            log = b.uart_text(every=True)
            crash = CRASH.search(log)
            check("no crash in the firmware's logs", not crash,
                  crash and log[max(0, crash.start() - 300):crash.end() + 300])
            if not skipped:
                t4t(b, work)
                t4t_reader()
            b.stop()
            b.fe("stop")
    except TimeoutError:
        print("the device, when its shell stopped answering:\n"
              + b.fe("status", timeout=60)[1])
        raise
    finally:
        b.stop()
        b.fe("stop")
        shutil.rmtree(b.dir, ignore_errors=True)
        shutil.rmtree(work, ignore_errors=True)
    if skipped:
        print("skipped: this firmware's NFC shell has no `emulate`")
        return SKIPPED
    bad = [label for label, ok in results if not ok]
    print(f"{len(results) - len(bad)} of {len(results)} checks passed")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
