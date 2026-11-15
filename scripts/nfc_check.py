#!/usr/bin/env python3
# Copyright (c) 2026, Kara Zajac
# SPDX-License-Identifier: BSD-3-Clause
"""Check the cards beyond NTAG and MIFARE Classic: MIFARE Ultralight C,
ISO14443-4A, MIFARE DESFire, ISO14443B, ST25TB, FeliCa Lite-S and Standard,
ISO15693 with NXP's SLIX.

    scripts/nfc_check.py firmware/official-1.5.1/flipper-z-f7-update-1.5.1.tgz

Boots a throwaway device.  Each card is put into the field (`./flipperemu
nfc`), found by the firmware's `scanner` in its NFC shell under its own
protocol, and read by its `dump`: the file that the firmware writes has to
be the card's own file, key for key (a MIFARE DESFire's record file apart,
which the firmware asks for in a way no card answers).  Then the cards'
own commands, through the shell's `apdu` and `raw` where the firmware has
them: DESFire's wrapped commands with a file that takes two frames, an
application that is not there, a file that wants a key; ISO15693 blocks
read, written, locked, and the errors a card gives; FeliCa's polling and a
block written and read.  A card's written block lives through a snapshot.
A MIFARE Ultralight C gives a reader without its key the pages before
AUTH0, and the NFC app's Read finds its key in the firmware's dictionary
(two-key triple DES, both ways).
Needs the firmware's NFC shell (1.4 and later); without it nothing is
checked and the exit status is 77.  Exit status 0 = pass.
"""
import argparse
import os
import pathlib
import re
import shutil
import sys
import tempfile
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from files_check import Bench, CRASH, check, results         # noqa: E402
from fe_cli import FlipperCli, usb_endpoint                  # noqa: E402
from fe_monitor import Monitor                               # noqa: E402
import check_limit  # noqa: E402
import fe_nfc  # noqa: E402

SKIPPED = 77
PROMPT = r"\[nfc\]>: "
ISO4A = """Filetype: Flipper NFC device
Version: 4
Device type: ISO14443-4A
UID: 08 A1 B2 C3
ATQA: 00 04
SAK: 20
T0: 78
TA(1): 80
TB(1): 70
TC(1): 02
T1...Tk: 46 45 6D 75
"""


class Shell:
    """The firmware's NFC shell on the device's CLI"""

    def __init__(self, b):
        m = Monitor(str(b.dir / "monitor.sock"))
        try:
            self.cli = FlipperCli(usb_endpoint(m))
        finally:
            m.close()
        self.cli.read_until_prompt(15)
        found, _ = self.cli.watch("nfc", PROMPT, timeout=8, stop=False)
        self.there = bool(found)
        if not found:
            self.cli.write(b"\x03")
        self.commands = self.ask("help") if found else ""

    def ask(self, command, timeout=60):
        """A command's lines, without its echo and the prompt"""
        found, out = self.cli.watch(command, PROMPT, timeout=timeout, stop=False)
        lines = [line.strip() for line in out.splitlines() if line.strip()]
        return "\n".join(lines[1:-1] if found else lines[1:])

    def close(self):
        try:
            if self.there:
                self.cli.run("exit", timeout=5)
        except (OSError, TimeoutError):
            pass
        self.cli.close()


def read_card(b, work, name, words, card, protocol, but=(), may=()):
    """A card file in the field, found and dumped by the firmware: the dump
    is the card's file (but: the keys that the firmware leaves out; may:
    those that are a firmware's own, as its file format's number)"""
    path = work / f"{name}.nfc"
    path.write_text(card)
    code, text = b.fe("nfc", path)
    sh = Shell(b)
    try:
        found = sh.ask("scanner", 30)
        dumped = sh.ask(f"dump -f /ext/nfc/fe-check-{name}.nfc", 90)
    finally:
        sh.close()
    out = work / f"{name}-dump.nfc"
    b.fe("pull", f"/ext/nfc/fe-check-{name}.nfc", out)
    b.fe("rm", f"/ext/nfc/fe-check-{name}.nfc")
    want = fe_nfc.parse_nfc_text(card)
    got = fe_nfc.parse_nfc_text(out.read_text()) if out.exists() else {}
    differ = sorted(k for k in set(want) | set(got) if k not in may
                    and (want.get(k) or "").upper() != (got.get(k) or "").upper())
    check(f"{words}: in the field, the firmware's `scanner` finds it "
          f"({protocol}), and its `dump` writes the card's own file: "
          f"{len(want) - len(but) - len(may)} keys the same"
          + (f" (and not {', '.join(but)}: the firmware's own)" if but else ""),
          code == 0 and f"Protocols detected: {protocol}".lower() in found.lower()
          and got and differ == sorted(but),
          f"{text}\n{found}\n{dumped}\n" + "\n".join(
              f"{k}: {want.get(k)} | {got.get(k)}" for k in differ[:12]))


def ulc(b, work):
    """A MIFARE Ultralight C whose pages from 16 on want its key"""
    uid = bytes.fromhex("04A1B2C3D4E5F6")
    want = fe_nfc.parse_nfc_text(fe_nfc.sample_ulc(uid))
    code, text = b.fe("nfc", "ulc", uid.hex())
    sh = Shell(b)
    try:
        found = sh.ask("scanner", 30)
        sh.ask("dump -f /ext/nfc/fe-check-ulc.nfc", 60)
    finally:
        sh.close()
    out = work / "ulc-dump.nfc"
    b.fe("pull", "/ext/nfc/fe-check-ulc.nfc", out)
    b.fe("rm", "/ext/nfc/fe-check-ulc.nfc")
    got = fe_nfc.parse_nfc_text(out.read_text()) if out.exists() else {}
    open_pages = all(got.get(f"Page {i}") == want[f"Page {i}"] for i in range(16))
    shut = all(got.get(f"Page {i}") == "00 00 00 00" for i in range(16, 48))
    check("a MIFARE Ultralight C whose pages from 16 on want its key: found "
          "as an Ultralight, and read without a key (the shell's `dump`): "
          "its first 16 pages, and none of the others",
          code == 0 and "Ultralight" in found and open_pages and shut
          and got.get("Pages read") == "16"
          and got.get("NTAG/Ultralight type") == "Mifare Ultralight C",
          f"{text}\n{found}\n{got.get('Pages read')} "
          f"{got.get('NTAG/Ultralight type')} {got.get('Page 16')}")
    # (the shell's `dump -k KEY` of the official 1.5.1 tries another key than
    # the one it is given: the app's Read has the dictionary's keys right)
    seen = b.uart_text().count("Auth success")
    code, text = b.fe("app", "NFC")
    time.sleep(1.5)
    b.fe("press", "ok")
    ok = False
    end = time.time() + 60
    while time.time() < end and not ok:
        ok = b.uart_text().count("Auth success") > seen
        time.sleep(0.5)
    failed = b.uart_text().count("Auth failed")
    b.fe("app", "--close")
    check("the NFC app's Read of it: the firmware tries its dictionary's "
          "keys, and the card takes the right one and no other (two-key "
          f"triple DES, both ways: {failed} keys refused, one taken)",
          ok and failed >= 1, text + b.uart_text()[-600:])


def raw_b(sh, data, keep=False, select=False):
    """The answer's bytes to a frame on ISO14443B's air (with its CRC_B)"""
    text = sh.ask(f"raw -p 14b -c {'-k ' if keep else ''}"
                  f"{'-s ' if select else ''}-d {data}")
    return " ".join(re.findall(r"Rx: ([0-9A-F ]+)", text)).strip(), text


def st25tb(b, work):
    """ST's ST25TB04K: its own commands on ISO14443B's air"""
    uid = bytes.fromhex("D0021C0123456789")
    read_card(b, work, "st25tb", "an ST25TB04K", fe_nfc.sample_st25tb(uid),
              "St25tb")
    b.fe("nfc", "st25tb", uid.hex())
    sh = Shell(b)
    try:
        if "raw" not in sh.commands:
            return
        ask = lambda data: raw_b(sh, data, keep=True)[0]
        chip = ask("0600")[:2]                  # Initiate: an ID for now
        selected = ask("0E" + chip)
        got_uid = ask("0B")
        before = ask("0810")
        ask("0910DEADBEEF")                     # (a write has no answer)
        after = ask("0810")
        ask("0907AABBCCDD")                     # block 7: locked in the card
        locked = ask("0807")
        ask("0905FDFFFFFF")                     # a counter: down
        down = ask("0805")
        ask("0905FFFFFFFF")                     # and not up again
        up = ask("0805")
        otp = ask("08FF")
        beyond = ask("0880")
        raw_b(sh, "0F")                         # Completion; the field off
    finally:
        sh.close()
    b.fe("nfc", "none")
    text = (f"{chip} | {selected} | {got_uid} | {before} | {after} | {locked} "
            f"| {down} | {up} | {otp} | {beyond}")
    check("ST25TB commands (the shell's `raw -p 14b`, the field kept on): "
          "Initiate gives a chip ID, Select by it, Get_UID the UID from its "
          "low byte, a block written and read, a locked block and a counter "
          "upward refused, the system OTP block, no answer beyond the last "
          "block",
          len(chip) == 2 and selected.startswith(chip)
          and got_uid.startswith("89 67 45 23 01 1C 02 D0")
          and before.startswith("30 30 31 36") and after.startswith("DE AD BE EF")
          and locked.startswith("30 30 30 37") and down.startswith("FD FF FF FF")
          and up.startswith("FD FF FF FF") and otp.startswith("FF FF FF FE")
          and beyond == "", text)


def iso14443b(b, work):
    """ISO14443B: REQB, ATTRIB, and ISO14443-4's blocks after it"""
    pupi = bytes.fromhex("1A2B3C4D")
    card = fe_nfc.sample_iso14443b(pupi)
    read_card(b, work, "iso4b", "an ISO14443-4B card", card, "Iso14443-4b")
    b.fe("nfc", "iso14443b", pupi.hex())
    sh = Shell(b)
    have_raw = "raw" in sh.commands
    try:
        if have_raw:
            atqb = raw_b(sh, "050008")[0]
            block, said = raw_b(sh, "0200A4040000", select=True)
            nak = raw_b(sh, "B2", select=True)[0]
            deselect = raw_b(sh, "C2", select=True)[0]
    finally:
        sh.close()
    if have_raw:
        check("ISO14443B commands (the shell's `raw -p 14b`): REQB gives the "
              "ATQB with the card's PUPI, application data and protocol "
              "info; after ATTRIB (`-s`) an I-block is answered by one with "
              "its number, an R(NAK) by an R(ACK), DESELECT by itself",
              atqb.startswith("50 1A 2B 3C 4D 00 12 34 E1 00 81 85")
              and "Protocol: 1" in said and block.startswith("02 6D 00")
              and nak.startswith("A2") and deselect.startswith("C2"),
              f"{atqb} | {block} | {nak} | {deselect}\n{said}")
    # A card that is ISO14443-3B and no more.  (Not its `dump`: the official
    # 1.5.1's callback for it never stops the reader, whose thread, the
    # firmware's highest, then runs for ever: the Flipper is frozen.)
    path = work / "iso3b.nfc"
    path.write_text(card.replace("ISO14443-4B", "ISO14443-3B")
                    .replace("Protocol info: 00 81 85", "Protocol info: 00 80 80"))
    code, text = b.fe("nfc", path)
    sh = Shell(b)
    try:
        found = sh.ask("scanner", 30)
        silent, said = raw_b(sh, "0200A4040000", select=True) if have_raw \
            else ("", "Protocol: 0")
    finally:
        sh.close()
    b.fe("nfc", "none")
    check("a card that is ISO14443-3B and no more: the firmware's `scanner` "
          "finds it as that, and it has no answer to an ISO14443-4 block",
          code == 0 and "protocols detected: iso14443-3b" in found.lower()
          and "Protocol: 0" in said and silent == "",
          f"{text}\n{found}\n{said}")


def desfire(b):
    b.fe("nfc", "desfire", "04A1B2C3D4E5F6")
    sh = Shell(b)
    try:
        if "apdu" not in sh.commands:
            print("  (this firmware's NFC shell has no `apdu`: DESFire's "
                  "commands are not tried one by one)")
            return
        text = sh.ask("apdu -p 4a -d 9060000000 90AF000000 90AF000000 "
                      "905A00000356341200 906F000000 "
                      "90BD0000070100000000000000 90AF000000 "
                      "906C0000010200 90BD0000070400000000000000 "
                      "905A000003AABBCC00 900A0000010000")
    finally:
        sh.close()
    rx = re.findall(r"Rx: ([0-9A-F ]+)", text)
    data = bytes(range(0x20, 0x70)).hex(" ").upper()
    check("MIFARE DESFire's commands, wrapped in ISO 7816 (the shell's "
          "`apdu`): GetVersion in three frames; an application selected, "
          "its files listed; a file of 80 bytes read in two frames (59 and "
          "21); a value; a file that wants a key, an application that is "
          "not there and an Authenticate, each with its status",
          len(rx) == 11 and rx[0].strip() == "04 01 01 01 00 18 05 91 AF"
          and rx[1].strip() == "04 01 01 01 04 18 05 91 AF"
          and rx[2].strip() == "04 A1 B2 C3 D4 E5 F6 BA 34 56 78 90 25 20 91 00"
          and rx[3].strip() == "91 00" and rx[4].strip() == "01 02 03 04 91 00"
          and rx[5].strip() == data[:59 * 3 - 1] + " 91 AF"
          and rx[6].strip() == data[59 * 3:] + " 91 00"
          and rx[7].strip() == "FA 00 00 00 91 00" and rx[8].strip() == "91 AE"
          and rx[9].strip() == "91 A0" and rx[10].strip() == "91 AE", text)


def iso15693(b):
    """-> whether the shell has `raw`"""
    b.fe("nfc", "slix", "E004010812345678")
    sh = Shell(b)
    try:
        if "raw" not in sh.commands:
            print("  (this firmware's NFC shell has no `raw`: single "
                  "commands are not tried)")
            return False
        raw = lambda data: " ".join(re.findall(
            r"Rx: ([0-9A-F ]+)", sh.ask(f"raw -p 15 -c -d {data}"))).strip()
        before = raw("022005")
        wrote = raw("022105DEADBEEF")
        after = raw("022005")
        locked = raw("022219")          # block 25 is locked in the card
        refused = raw("02211901020304")
        beyond = raw("022050")
        info = raw("022B")
        several = raw("02230002")
    finally:
        sh.close()
    check("ISO15693 commands (the shell's `raw -p 15`): a block read, "
          "written and read again; a locked block's lock and write refused "
          "(errors 11 and 12); a block that is not there (10); the system "
          "information with the UID, 80 blocks of 4 bytes; three blocks in "
          "one answer",
          before.startswith("00 00 00 00 00") and wrote.startswith("00 ")
          and len(wrote.split()) == 3 and after.startswith("00 DE AD BE EF")
          and locked.startswith("01 11") and refused.startswith("01 12")
          and beyond.startswith("01 10")
          and info.startswith("00 0F 78 56 34 12 08 01 04 E0 00 00 4F 03 01")
          and several.startswith("00 E1 40 0D 01 03 0B D1 01 07 54 02 65"),
          f"{before} | {wrote} | {after} | {locked} | {refused} | {beyond} | "
          f"{info} | {several}")
    return True


def snapshot(b):
    """The SLIX with its block 5 written: kept by a snapshot's state"""
    code, text = b.fe("snapshot", "save", "nfc-check")
    b.fe("nfc", "none")
    b.stop()                            # (a load is a start)
    code2, text2 = b.fe("snapshot", "load", "nfc-check", "--headless",
                        "--audio", "none", "--detach", timeout=180)
    b.fe("wait", "--timeout", 120)
    code3, held = b.fe("status")
    held = "\n".join(line for line in held.splitlines() if "SLIX" in line)
    sh = Shell(b)
    try:
        again = " ".join(re.findall(r"Rx: ([0-9A-F ]+)",
                                    sh.ask("raw -p 15 -c -d 022005"))).strip()
    finally:
        sh.close()
    b.fe("snapshot", "rm", "nfc-check")
    check("a snapshot with the card in the field, the card taken away, the "
          "snapshot loaded: the card is held again, with the block that was "
          "written to it",
          code == 0 and code2 == 0 and "SLIX2" in held
          and again.startswith("00 DE AD BE EF"), text + text2 + held + again)


def felica(b):
    b.fe("nfc", "felica", "012E4CD21A8B3C5F")
    sh = Shell(b)
    try:
        if "raw" not in sh.commands:
            return
        raw = lambda data: " ".join(re.findall(
            r"Rx: ([0-9A-F ]+)", sh.ask(f"raw -p felica -c -d {data}"))).strip()
        idm = "012E4CD21A8B3C5F"
        poll = raw("0600FFFF0000")
        code = raw("0600FFFF0100")
        other = raw("060012FC0000")
        new = bytes(range(0xB0, 0xC0)).hex().upper()
        wrote = raw(f"2008{idm}01090001" + "8002" + new)
        back = raw(f"1006{idm}010B0001" + "8002")
        key = raw(f"1006{idm}010B0001" + "8087")
    finally:
        sh.close()
    check("FeliCa's commands (the shell's `raw -p felica`): a polling "
          "answered with the IDm and PMm, with the system code when asked, "
          "and not for another system; a block written and read again; the "
          "card key's block not given (status 01 A8)",
          poll.startswith("12 01 01 2E 4C D2 1A 8B 3C 5F 00 F1 00 00 00 01 43 00")
          and code.startswith("14 01") and " 88 B4 " in code + " "
          and other == "" and wrote.startswith("0C 09 01 2E 4C D2 1A 8B 3C 5F 00 00")
          and back.startswith("1D 07 01 2E 4C D2 1A 8B 3C 5F 00 00 01 B0 B1 B2")
          and key.startswith("0C 07 01 2E 4C D2 1A 8B 3C 5F 01 A8"),
          f"{poll} | {code} | {other!r} | {wrote} | {back} | {key}")


def felica_standard(b, work):
    """A FeliCa Standard card of two systems"""
    idm = "0114B3C6D2F0A957"
    card = fe_nfc.sample_felica_standard(bytes.fromhex(idm))
    path = work / "felica-standard.nfc"
    path.write_text(card)
    code, text = b.fe("nfc", path)
    sh = Shell(b)
    try:
        found = sh.ask("scanner", 30)
        dumped = sh.ask("dump -f /ext/nfc/fe-check-fs.nfc", 120)
    finally:
        sh.close()
    out = work / "felica-standard-dump.nfc"
    b.fe("pull", "/ext/nfc/fe-check-fs.nfc", out)
    b.fe("rm", "/ext/nfc/fe-check-fs.nfc")
    got_text = out.read_text() if out.exists() else ""
    want, got = fe_nfc.parse_nfc_text(card), fe_nfc.parse_nfc_text(got_text)
    systems = fe_nfc.felica_systems(card)
    try:
        got_systems = fe_nfc.felica_systems(got_text)
    except ValueError as e:
        got_systems = str(e)
    # A firmware whose files have the areas' ends and the keys' versions
    # (Unleashed, RogueMaster, the dev build): those that the card has for a
    # file without them.
    newer = False
    if isinstance(got_systems, list) and len(got_systems) == len(systems):
        for mine, theirs in zip(systems, got_systems):
            if theirs["key"] is not None:
                newer = True
                nodes = fe_nfc.felica_nodes(mine)
                mine["key"] = 0
                mine["ends"] = [end for _, _, end in nodes if end is not None]
                mine["area_keys"] = [0] * len(mine["areas"])
                mine["service_keys"] = [0] * len(mine["services"])
    head = ("UID", "Manufacture id", "Manufacture parameter", "System found")
    blocks = sum(len(s["blocks"]) for s in systems)
    check("a FeliCa Standard card of two systems: in the field, the "
          "firmware's `scanner` finds it (FeliCa), and its `dump` has the "
          f"card's own file's systems: their codes, {sum(len(s['areas']) for s in systems)} "
          f"areas, {sum(len(s['services']) for s in systems)} services and the "
          f"{blocks} blocks of those that are read without a key"
          + (" (and each area's end and each key's version, which this "
             "firmware asks for)" if newer else ""),
          code == 0 and "protocols detected: felica" in found.lower()
          and got_systems == systems
          and all(want[k].upper() == (got.get(k) or "").upper() for k in head),
          f"{text}\n{found}\n{dumped}\n{got_systems}\n"
          + "\n".join(f"{k}: {want[k]} | {got.get(k)}" for k in head))
    b.fe("nfc", "felica-standard", idm)
    sh = Shell(b)
    try:
        if "raw" not in sh.commands:
            return
        raw = lambda data: " ".join(re.findall(
            r"Rx: ([0-9A-F ]+)", sh.ask(f"raw -p felica -c -d {data}"))).strip()
        second = "1" + idm[1:]                  # the second system's IDm
        new = b"new words here!!".hex().upper()
        poll = raw("0600FE000100")
        codes = raw(f"0A0C{idm}")
        area = raw(f"0C0A{idm}0400")
        end = raw(f"0C0A{idm}0600")
        other = raw(f"0C0A{second}0100")
        private = raw(f"1006{idm}010810018000")
        wrote = raw(f"2008{idm}014900018001{new}")
        back = raw(f"1206{idm}0149000280008001")
        readonly = raw(f"2008{idm}018B00018000{new}")
        versions = raw(f"1102{idm}038B0008104B39")
    finally:
        sh.close()
    b.fe("nfc", "none")
    at = lambda answer, n: answer.split()[n:]
    spaced = " ".join(new[i:i + 2] for i in range(0, 32, 2))
    check("FeliCa Standard's commands (the shell's `raw -p felica`): a "
          "polling for the second system's code is answered with its number "
          "in the IDm; Request System Code has both; Search Service Code "
          "gives an area with its end, a service of the other system, FFFF "
          "past the last; a service with a key is not read (status A5); a "
          "block written and read with its neighbour; a read-only service "
          "not written; Request Service knows a service of the other system "
          "not",
          at(poll, 2)[:1] == ["11"] and at(poll, 18)[:2] == ["FE", "00"]
          and at(codes, 10)[:5] == ["02", "00", "03", "FE", "00"]
          and at(area, 10)[:4] == ["40", "00", "7F", "00"]
          and at(end, 10)[:2] == ["FF", "FF"] and end.startswith("0C")
          and at(other, 10)[:2] == ["49", "39"]
          and at(private, 10)[:2] == ["01", "A5"]
          and at(wrote, 10)[:2] == ["00", "00"]
          and at(back, 10)[:3] == ["00", "00", "02"] and spaced in back
          and at(readonly, 10)[:2] == ["01", "A5"]
          and at(versions, 10)[:7] == ["03", "00", "00", "00", "00", "FF", "FF"],
          f"{poll} | {codes} | {area} | {end} | {other} | {private} | {wrote} "
          f"| {back} | {readonly} | {versions}")


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("firmware")
    ap.add_argument("--timeout", type=float, default=120.0,
                    help="seconds to wait for the firmware to start")
    ap.add_argument("--keep", action="store_true",
                    help="keep the throwaway device")
    args = ap.parse_args()
    check_limit.arm(900)

    b = Bench(f"nfccheck-{int(time.time())}-{os.getpid()}", args.firmware,
              args.timeout)
    work = pathlib.Path(tempfile.mkdtemp(prefix="fe-nfc-")).resolve()
    skipped = False
    try:
        if check("boot: 'Startup complete'", b.start(first=True),
                 b.uart_text()[-2000:]):
            b.fe("wait", "--timeout", args.timeout)
            sh = Shell(b)
            sh.close()
            if not sh.there:
                skipped = True
            else:
                read_card(b, work, "desfire", "a MIFARE DESFire EV1 with two "
                          "applications",
                          fe_nfc.sample_desfire(bytes.fromhex("04A1B2C3D4E5F6")),
                          "Mifare DESFire", ("Application 563412 File 3",))
                read_card(b, work, "iso4a", "an ISO14443-4A card with no "
                          "application that is known", ISO4A, "Iso14443-4a")
                read_card(b, work, "felica", "a FeliCa Lite-S",
                          fe_nfc.sample_felica(bytes.fromhex("012E4CD21A8B3C5F")),
                          "FeliCa", may=("Data format version",))
                read_card(b, work, "slix", "an NXP SLIX2 (ISO15693)",
                          fe_nfc.sample_slix(bytes.fromhex("E004010812345678")),
                          "Slix")
                ulc(b, work)
                desfire(b)
                st25tb(b, work)
                iso14443b(b, work)
                felica(b)
                felica_standard(b, work)
                if iso15693(b):
                    snapshot(b)
                b.fe("nfc", "none")
            b.stop()
            b.fe("stop")
            log = b.uart_text(every=True)
            crash = CRASH.search(log)
            check("no crash in the firmware's logs", not crash,
                  crash and log[max(0, crash.start() - 300):crash.end() + 300])
    finally:
        b.stop()
        b.fe("stop")                    # (a loaded snapshot's emulator runs
        #                                  by itself: no child of this check)
        shutil.rmtree(work, ignore_errors=True)
        if not args.keep:
            shutil.rmtree(b.dir, ignore_errors=True)
    if skipped:
        print("skipped: this firmware has no NFC shell")
        return SKIPPED
    bad = [label for label, ok in results if not ok]
    print(f"{len(results) - len(bad)} of {len(results)} checks passed"
          + (f"; device kept: {b.dir}" if args.keep else ""))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
