#!/usr/bin/env python3
# Copyright (c) 2026, Kara Zajac
# SPDX-License-Identifier: BSD-3-Clause
"""Headless smoke test: does this firmware boot and react in FlipperEmu?

    scripts/smoke_test.py firmware/official-1.5.1/flipper-z-f7-update-1.5.1.tgz
    scripts/smoke_test.py ~/fw/build/f7-firmware-D/firmware.elf --no-sd

Boots a throwaway device, waits for the firmware's own log milestones, presses
Back (dismiss the first scene) and OK (main menu), checks the LCD changed,
runs `info device` on the CLI over the emulated USB serial port, receives an
NEC frame through the virtual IR receiver (`ir rx`), captures an `ir tx` and
decodes it back, receives a Princeton key over the virtual air (`subghz rx`),
captures a `subghz tx`, receives a key from an SDR recording through the
radio link (scripts/fe_sdr.py), reads a 125 kHz EM4100 tag (`rfid read`),
reads back its own H10301 emulation (`rfid emulate`), writes an H10301 to a
virtual T5577 (`rfid write`), reads a DS1990A iButton (`ikey read`), has a
virtual 1-Wire reader read an emulated DS1990A, captures an emulated Metakom
key and reads it back, reads an NTAG215 (`mfu info`) and dumps a MIFARE
Classic 1K over NFC, has a virtual reader read an NTAG215 the firmware
emulates, sends an APDU to an ISO-DEP card a program plays on the NFC link
(scripts/fe_nfc_link.py), and to the same card behind a simulated Proxmark3
(scripts/fe_pm3.py), enumerates over USB/IP, then reboots through the CLI and
checks the LCD comes back after the warm reset.
Exit status 0 = pass. Screenshots are kept in the --out directory.
"""
import argparse
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
import time

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
from fe_monitor import Monitor  # noqa: E402
from fe_cli import FlipperCli, usb_endpoint  # noqa: E402

CRASH = re.compile(r"\[CRASH\]|furi_check failed|HardFault|Kernel is Dead")


def wait_for(path: pathlib.Path, pattern: str, timeout: float):
    rx = re.compile(pattern)
    end = time.time() + timeout
    while time.time() < end:
        text = path.read_text(errors="replace") if path.exists() else ""
        if rx.search(text):
            return True
        if CRASH.search(text):
            return False
        time.sleep(0.2)
    return False


def dark_pixels(png: pathlib.Path, scale: int = 4) -> int:
    """Dark pixels inside the LCD area (excludes the bezel)."""
    from PIL import Image
    b = 4 * scale
    img = Image.open(png).convert("L").crop((b, b, b + 128 * scale, b + 64 * scale))
    return sum(n for level, n in enumerate(img.histogram()) if level < 60)


def reboot_check(mon: Monitor, uart: pathlib.Path, out: pathlib.Path, timeout: float) -> bool:
    """`power reboot` over USB; after the warm reset the LCD must redraw."""
    before = uart.read_text(errors="replace").count("Startup complete")
    try:
        cli = FlipperCli(usb_endpoint(mon))
        try:
            cli.read_until_prompt(15)
            cli.write(b"power reboot\r")
            time.sleep(0.5)
        finally:
            cli.close()
    except (OSError, TimeoutError, ConnectionError, TypeError) as e:
        print(f"  reboot: {e}")
        return False
    end = time.time() + timeout
    while time.time() < end:
        if uart.read_text(errors="replace").count("Startup complete") > before:
            break
        time.sleep(0.2)
    else:
        return False
    time.sleep(2.0)
    shot = out / "after-reboot.png"
    mon.screenshot(str(shot))
    return dark_pixels(shot) > 500


def ir_check(mon: Monitor) -> bool:
    """`ir rx` on the CLI, then an NEC frame into the virtual IR receiver."""
    import select
    import fe_ir
    try:
        cli = FlipperCli(usb_endpoint(mon))
        try:
            cli.read_until_prompt(15)
            cli.write(b"ir rx\r")
            time.sleep(1.0)
            mon.cmd("qom-set /machine/ir rx " + ",".join(map(str, fe_ir.encode("NEC", 4, 8))))
            out, end = b"", time.time() + 3
            while time.time() < end and b"NEC, A:0x04, C:0x08" not in out:
                r, _, _ = select.select([cli.fd], [], [], 0.1)
                if r:
                    out += cli.sock.recv(4096) if cli.sock else os.read(cli.fd, 4096)
            cli.write(b"\x03")
            time.sleep(0.3)
        finally:
            cli.close()
    except (OSError, TimeoutError, ConnectionError, TypeError) as e:
        print(f"  ir: {e}")
        return False
    return b"NEC, A:0x04, C:0x08" in out


def ir_tx_check(mon: Monitor) -> bool:
    """`ir tx NEC 04 08`: the transmission saved as .ir must hold the NEC
    frame's timings, and played back into the receiver, `ir rx` decodes it."""
    import fe_ir
    try:
        cli = FlipperCli(usb_endpoint(mon))
        try:
            cli.read_until_prompt(15)
            cli.run("ir tx NEC 04 08", timeout=10)
            path = ""
            for _ in range(30):
                path = mon.cmd("qom-get /machine/ir tx").strip().strip('"')
                if path:
                    break
                time.sleep(0.1)
            if not path:
                return False
            _, sent = fe_ir.parse_ir_file(path)
            want = fe_ir.encode("NEC", 4, 8)
            if len(sent) < len(want) or any(abs(a - b) > max(30, b // 50)
                                            for a, b in zip(sent, want)):
                print(f"  ir tx: {sent[:8]}... vs {want[:8]}...")
                return False
            cli.write(b"ir rx\r")
            time.sleep(1.0)
            mon.cmd("qom-set /machine/ir rx " + ",".join(map(str, sent)))
            found, _ = cli.watch("", r"NEC, A:0x04, C:0x08", timeout=3)
        finally:
            cli.close()
    except (OSError, TimeoutError, ConnectionError, TypeError, ValueError) as e:
        print(f"  ir tx: {e}")
        return False
    return found is not None


def subghz_check(mon: Monitor) -> bool:
    """`subghz rx` at 433.92 MHz, then a Princeton key on the air.

    Some firmwares' `subghz rx` refuses the internal radio ("Device 0 failed
    to start": Unleashed's begin() check has no handler for it); there the
    raw capture (`subghz rx_raw`) must show the key's 390/1170 µs timings."""
    import re
    import select
    import fe_subghz
    want = b"Key:0x0095D5D4"
    sig = fe_subghz.princeton(0x95D5D4)

    def listen(cli, command, until):
        cli.write(command)
        time.sleep(1.5)
        mon.cmd("qom-set /machine/subghz rx @433920000," + ",".join(map(str, sig)))
        out, end = b"", time.time() + 4
        while time.time() < end and not until(out):
            r, _, _ = select.select([cli.fd], [], [], 0.1)
            if r:
                out += cli.sock.recv(4096) if cli.sock else os.read(cli.fd, 4096)
        cli.write(b"\x03")
        time.sleep(0.5)
        return out

    def princeton_raw(out):
        vals = [int(v) for v in re.findall(rb"[+-](\d+)", out)]
        return sum(abs(v - 390) < 60 or abs(v - 1170) < 90 for v in vals) >= 40

    try:
        cli = FlipperCli(usb_endpoint(mon))
        try:
            cli.read_until_prompt(15)
            out = listen(cli, b"subghz rx 433920000 0\r", lambda o: want in o)
            if want in out:
                return True
            if b"failed to start" in out:
                cli.read_until_prompt(5)
                return princeton_raw(listen(cli, b"subghz rx_raw 433920000\r",
                                            princeton_raw))
        finally:
            cli.close()
    except (OSError, TimeoutError, ConnectionError, TypeError) as e:
        print(f"  subghz: {e}")
    return False


def subghz_tx_check(mon: Monitor):
    """`subghz tx` of a Princeton key; the capture must hold its timings.
    None (skip) where the CLI cannot start the internal radio (Unleashed)."""
    import re
    try:
        cli = FlipperCli(usb_endpoint(mon))
        try:
            cli.read_until_prompt(15)
            out = cli.run("subghz tx 95D5D4 433920000 390 4 0", timeout=20)
        finally:
            cli.close()
    except (OSError, TimeoutError, ConnectionError, TypeError) as e:
        print(f"  subghz tx: {e}")
        return False
    if "failed to start" in out:
        return None
    tx = mon.cmd("qom-get /machine/subghz tx").strip().strip('"')
    durs = [int(v) for v in re.findall(r"[+-]?\d+", tx.split(",", 1)[-1])]
    # 0x95D5D4: 1 = 1170/-390, 0 = 390/-1170, then the 30-TE guard
    bits = [1 if d > 700 else 0 for d in durs[0:48:2]]
    return tx.startswith("@433920000") and int("".join(map(str, bits)), 2) == 0x95D5D4


def rfid_read_check(mon: Monitor, devdir: pathlib.Path) -> bool:
    """An EM4100 tag in the 125 kHz field; `rfid read` must decode it."""
    import fe_rfid
    tagfile = devdir / "rfid-tag.txt"
    tagfile.write_text(fe_rfid.tag("EM4100", bytes.fromhex("DEADBEEF01")))
    try:
        if mon.cmd(f"qom-set /machine/rfid tag file:{tagfile}"):
            return False
        cli = FlipperCli(usb_endpoint(mon))
        try:
            cli.read_until_prompt(15)
            found, _ = cli.watch("rfid read", r"EM4100 DEADBEEF01", timeout=12)
        finally:
            cli.close()
            mon.cmd("qom-set /machine/rfid tag none")
    except (OSError, TimeoutError, ConnectionError, TypeError) as e:
        print(f"  rfid read: {e}")
        return False
    return found is not None


def rfid_emulate_check(mon: Monitor, devdir: pathlib.Path) -> bool:
    """`rfid emulate H10301 010203` for the virtual reader, whose capture
    goes back into the field: `rfid read` must find the same card."""
    import fe_rfid
    try:
        cli = FlipperCli(usb_endpoint(mon))
        try:
            cli.read_until_prompt(15)
            cli.watch("rfid emulate H10301 010203", r"NEVER", timeout=1.2)
            path = ""
            for _ in range(50):         # written once the field is quiet
                path = mon.cmd("qom-get /machine/rfid tx").strip().strip('"')
                if path:
                    break
                time.sleep(0.1)
            if not path:
                return False
            frame, mod = fe_rfid.capture_frame(fe_rfid.parse_capture(path))
            _, sent, _ = fe_rfid.encode("H10301", bytes.fromhex("010203"))
            if not fe_rfid.same_frame(frame, sent):
                print(f"  rfid emulate: the capture's frame is not the encoder's ({path})")
                return False
            tagfile = devdir / "rfid-tag.txt"
            tagfile.write_text(fe_rfid.tag_string(frame, mod))
            if mon.cmd(f"qom-set /machine/rfid tag file:{tagfile}"):
                return False
            # Should read the first time (BRINGUP_LOG, entry 30). If not,
            # read again as a person holding the card would, and say so.
            for attempt in (1, 2):
                found, out = cli.watch("rfid read", r"H10301 010203", timeout=12)
                if found:
                    if attempt > 1:
                        print("  rfid emulate: read back on the second try")
                    break
                print(f"  rfid emulate: read {attempt} found nothing "
                      f"({out.strip().splitlines()[-1]!r})")
        finally:
            cli.close()
            mon.cmd("qom-set /machine/rfid tag none")
    except (OSError, TimeoutError, ConnectionError, TypeError, ValueError) as e:
        print(f"  rfid emulate: {e}")
        return False
    return found is not None


def rfid_write_check(mon: Monitor) -> bool:
    """A blank T5577 in the field: `rfid write` must answer "Written!", which
    the firmware says only after reading the tag back and comparing it."""
    try:
        if mon.cmd("qom-set /machine/rfid t5577 blank"):
            return False
        cli = FlipperCli(usb_endpoint(mon))
        try:
            cli.read_until_prompt(15)
            found, out = cli.watch("rfid write H10301 2A1F7B", r"Written!", timeout=40)
        finally:
            cli.close()
            blocks = mon.cmd("qom-get /machine/rfid t5577").strip().strip('"')
            mon.cmd("qom-set /machine/rfid tag none")
    except (OSError, TimeoutError, ConnectionError, TypeError) as e:
        print(f"  rfid write: {e}")
        return False
    if not found:
        print(f"  rfid write: {out.strip()[-200:]!r} (T5577: {blocks})")
    return bool(found) and blocks.startswith("00107060")      # FSK2a RF/50, 3 blocks


def subghz_sdr_check(mon: Monitor, devdir: pathlib.Path):
    """A Princeton key in an RTL-SDR recording (cu8), played through the
    CC1101's radio link by the SDR bridge (fe_sdr.py): `subghz rx` must
    decode it.  None (skip) without numpy, which the bridge needs."""
    try:
        import numpy  # noqa: F401
    except ImportError:
        return None
    import sdr_check
    try:
        ok, info = sdr_check.check_file(devdir.name, mon)
    except (OSError, TimeoutError, ConnectionError, ValueError) as e:
        ok, info = False, str(e)
    if not ok:
        print(f"  subghz sdr: {info}")
    return ok


def ibutton_check(mon: Monitor, devdir: pathlib.Path) -> bool:
    """A DS1990A on the iButton contact: `ikey read` (1-Wire SEARCH ROM and
    READ ROM on PB14) must print its ROM."""
    import fe_ibutton
    desc = fe_ibutton.describe_new("ds1990", bytes.fromhex("01A1B2C3D4E5F6"))
    rom = next(l[4:] for l in desc.splitlines() if l.startswith("rom ")).replace(" ", "")
    key = devdir / "ibutton-key.txt"
    key.write_text(desc)
    try:
        if mon.cmd(f"qom-set /machine/ibutton key file:{key}"):
            return False
        cli = FlipperCli(usb_endpoint(mon))
        try:
            cli.read_until_prompt(15)
            found, out = cli.watch("ikey read", f"Dallas {rom.upper()}", timeout=15)
        finally:
            cli.close()
            mon.cmd("qom-set /machine/ibutton key none")
    except (OSError, TimeoutError, ConnectionError, TypeError) as e:
        print(f"  ibutton: {e}")
        return False
    if not found:
        print(f"  ibutton: {out.strip()[-200:]!r}")
    return bool(found)


def ibutton_emulate_check(mon: Monitor) -> bool:
    """`ikey emulate Dallas` a DS1990A: the virtual 1-Wire reader, a master
    paced by the firmware's 1-Wire slave, must read its ROM."""
    import fe_ibutton
    rom = fe_ibutton.rom_of(bytes.fromhex("012B3C4D5E6F70")).hex().upper()
    try:
        cli = FlipperCli(usb_endpoint(mon))
        try:
            cli.read_until_prompt(15)
            found, out = cli.watch(f"ikey emulate Dallas {rom}", r"Emulating key",
                                   timeout=5, stop=False)
            if not found:
                print(f"  ibutton emulate: {out.strip()[-200:]!r}")
                return False
            time.sleep(0.5)
            before = mon.cmd("qom-get /machine/ibutton tx").strip().strip('"')
            if mon.cmd("qom-set /machine/ibutton reader read"):
                return False
            for _ in range(300):
                if not mon.cmd("qom-get /machine/ibutton reader").strip().strip('"'):
                    break
                time.sleep(0.1)
            path = mon.cmd("qom-get /machine/ibutton tx").strip().strip('"')
        finally:
            # stop it, and see the prompt: a ^C written just before closing
            # the port can be lost, and the next command would land in it
            cli.write(b"\x03")
            try:
                cli.read_until_prompt(5)
            finally:
                cli.close()
    except (OSError, TimeoutError, ConnectionError, TypeError) as e:
        print(f"  ibutton emulate: {e}")
        return False
    if path == before or not path.endswith(".ibtn"):
        print("  ibutton emulate: the reader read nothing")
        return False
    got = fe_ibutton.parse_ibtn(path).get("Rom Data", "").replace(" ", "")
    if got != rom:
        print(f"  ibutton emulate: read ROM {got}, emulated {rom} ({path})")
    return got == rom


def ibutton_wave_check(mon: Monitor, devdir: pathlib.Path) -> bool:
    """`ikey emulate Metakom C355330F`: its waveform, captured, must decode
    back; put on the contact, `ikey read` must read the same key."""
    import fe_ibutton
    key = devdir / "ibutton-key.txt"
    try:
        cli = FlipperCli(usb_endpoint(mon))
        try:
            cli.read_until_prompt(15)
            before = mon.cmd("qom-get /machine/ibutton tx")
            cli.watch("ikey emulate Metakom C355330F", r"NEVER", timeout=1.5)
            for _ in range(50):         # written after 100 ms without a level
                after = mon.cmd("qom-get /machine/ibutton tx")
                if after != before:
                    break
                time.sleep(0.1)
            else:
                print("  ibutton wave: no capture")
                return False
            path = after.strip().strip('"')
            proto, data = fe_ibutton.decode_capture(fe_ibutton.parse_capture(path))
            if (proto, data.hex().upper()) != ("Metakom", "C355330F"):
                print(f"  ibutton wave: decoded {proto} {data.hex().upper()} ({path})")
                return False
            key.write_text(fe_ibutton.describe_new(proto, data))
            if mon.cmd(f"qom-set /machine/ibutton key file:{key}"):
                return False
            found, out = cli.watch("ikey read", r"Metakom C355330F", timeout=15)
        finally:
            cli.close()
            mon.cmd("qom-set /machine/ibutton key none")
    except (OSError, TimeoutError, ConnectionError, TypeError, ValueError) as e:
        print(f"  ibutton wave: {e}")
        return False
    if not found:
        print(f"  ibutton wave: {out.strip()[-200:]!r}")
    return bool(found)


def nfc_check(mon: Monitor, devdir: pathlib.Path):
    """An NTAG215 in the NFC field: `mfu info` in the NFC shell must read
    its UID and all 135 pages.  None (skip) without the NFC shell."""
    import fe_nfc
    uid = bytes.fromhex("04F11E7A2C4180")
    card = devdir / "nfc-card.txt"
    card.write_text(fe_nfc.blank("ntag215", uid, "https://flipperzero.one"))
    try:
        if mon.cmd(f"qom-set /machine/nfc card file:{card}"):
            return False
        cli = FlipperCli(usb_endpoint(mon))
        try:
            cli.read_until_prompt(15)
            found, _ = cli.watch("nfc", r"\[nfc\]>: ", timeout=8, stop=False)
            if not found:
                cli.write(b"\x03")
                return None
            _, out = cli.watch("mfu info", r"\[nfc\]>: ", timeout=15, stop=False)
            cli.run("exit", timeout=5)  # leave the NFC shell
        finally:
            cli.close()
            mon.cmd("qom-set /machine/nfc card none")
    except (OSError, TimeoutError, ConnectionError, TypeError) as e:
        print(f"  nfc: {e}")
        return False
    ok = "Type: NTAG215" in out and "04 F1 1E 7A 2C 41 80" in out and "135/135" in out
    if not ok:
        print(f"  nfc: {out.strip()[-300:]!r}")
    return ok


def nfc_link_check(mon: Monitor, devdir: pathlib.Path):
    """An ISO14443-4A card played by a program on the NFC link
    (fe_nfc_link.py iso-dep): `apdu` in the NFC shell must get its answer
    through the firmware's ISO-DEP stack.  None (skip) without the shell."""
    import subprocess
    card = subprocess.Popen([sys.executable, str(ROOT / "scripts" / "fe_nfc_link.py"),
                             str(devdir / "nfc.sock"), "iso-dep"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
    try:
        time.sleep(1.0)
        cli = FlipperCli(usb_endpoint(mon))
        try:
            cli.read_until_prompt(15)
            found, _ = cli.watch("nfc", r"\[nfc\]>: ", timeout=8, stop=False)
            if not found:
                cli.write(b"\x03")
                return None
            _, out = cli.watch("apdu -p 4a -d 00CA000000", r"Rx:[^\n]*\n|\[nfc\]>: ",
                               timeout=20, stop=False)
            cli.run("exit", timeout=5)
        finally:
            cli.close()
    except (OSError, TimeoutError, ConnectionError, TypeError) as e:
        print(f"  nfc link: {e}")
        return False
    finally:
        card.terminate()
        card.wait(5)
    if "Rx:" not in out and ("not found" in out or "nknown" in out):
        return None                     # this firmware's shell has no `apdu`
    ok = "Rx: 04 A1 B2 C3 D4 E5 F6 90 00" in out
    if not ok:
        print(f"  nfc link: {out.strip()[-300:]!r}")
    return ok


def nfc_pm3_check(mon: Monitor, devdir: pathlib.Path):
    """The same card behind a simulated Proxmark3 (fe_pm3.py sim), relayed
    frame by frame by the `pm3` link card in the Proxmark3's own protocol:
    `apdu` must get its answer.  None (skip) without the NFC shell."""
    import re
    import subprocess
    sim = subprocess.Popen([sys.executable, str(ROOT / "scripts" / "fe_pm3.py"), "sim"],
                           stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    card = None
    try:
        pty = re.search(r"on (/dev/pts/\d+)", sim.stdout.readline()).group(1)
        card = subprocess.Popen([sys.executable, str(ROOT / "scripts" / "fe_nfc_link.py"),
                                 str(devdir / "nfc.sock"), "pm3:" + pty],
                                stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
        time.sleep(2.0)
        cli = FlipperCli(usb_endpoint(mon))
        try:
            cli.read_until_prompt(15)
            found, _ = cli.watch("nfc", r"\[nfc\]>: ", timeout=8, stop=False)
            if not found:
                cli.write(b"\x03")
                return None
            _, out = cli.watch("apdu -p 4a -d 00CA000000", r"Rx:[^\n]*\n|\[nfc\]>: ",
                               timeout=30, stop=False)
            cli.run("exit", timeout=5)
        finally:
            cli.close()
    except (OSError, TimeoutError, ConnectionError, TypeError, AttributeError) as e:
        print(f"  nfc pm3: {e}")
        return False
    finally:
        for p in (card, sim):
            if p:
                p.terminate()
                p.wait(5)
    if "Rx:" not in out and ("not found" in out or "nknown" in out):
        return None
    ok = "Rx: 04 A1 B2 C3 D4 E5 F6 90 00" in out
    if not ok:
        print(f"  nfc pm3: {out.strip()[-300:]!r}")
    return ok


def mfc_check(mon: Monitor, devdir: pathlib.Path):
    """A MIFARE Classic 1K in the field and its keys in the NFC app's key
    cache: `dump` must authenticate every sector (Crypto1) and save all 64
    blocks.  None (skip) without the NFC shell."""
    import fe_nfc
    uid = bytes.fromhex("11223344")
    card = devdir / "nfc-card.txt"
    card.write_text(fe_nfc.blank_mfc("1k", uid))
    keys = ("Filetype: Flipper NFC keys\nVersion: 1\nMifare Classic type: 1K\n"
            "Key A map: 000000000000FFFF\nKey B map: 000000000000FFFF\n" +
            "".join(f"Key A sector {s}: FF FF FF FF FF FF\n"
                    f"Key B sector {s}: FF FF FF FF FF FF\n" for s in range(16)))
    try:
        if mon.cmd(f"qom-set /machine/nfc card file:{card}"):
            return False
        cli = FlipperCli(usb_endpoint(mon))
        try:
            cli.read_until_prompt(15)
            cli.run("storage mkdir /ext/nfc/.cache", timeout=5)
            cli.run(f"storage remove /ext/nfc/.cache/{uid.hex().upper()}.keys", timeout=5)
            cli.write(f"storage write_chunk /ext/nfc/.cache/{uid.hex().upper()}.keys "
                      f"{len(keys)}\r".encode())
            time.sleep(0.5)
            cli.write(keys.encode())
            cli.read_until_prompt(5)
            found, _ = cli.watch("nfc", r"\[nfc\]>: ", timeout=8, stop=False)
            if not found:
                cli.write(b"\x03")
                return None
            saved, out = cli.watch("dump", r"Dump saved to '([^']+)'", timeout=40,
                                   stop=False)
            cli.read_until_prompt(5)
            cli.run("exit", timeout=5)  # leave the NFC shell (^C only cancels)
            if not saved:
                print(f"  mfc: {out.strip()[-200:]!r}")
                return False
            text = cli.run(f"storage read {saved.group(1).decode()}", timeout=10)
        finally:
            cli.close()
            mon.cmd("qom-set /machine/nfc card none")
    except (OSError, TimeoutError, ConnectionError, TypeError) as e:
        print(f"  mfc: {e}")
        return False
    return ("Block 63: FF FF FF FF FF FF FF 07 80 69 FF FF FF FF FF FF" in text and
            "Block 0: 11 22 33 44 44 08 04 00" in text)


def nfc_emulate_check(mon: Monitor):
    """`emulate -f` an NTAG215 (with an NDEF URL) in the NFC shell, then a
    virtual reader reads it: all 135 pages must come back.  None (skip)
    without the NFC shell."""
    import fe_nfc
    uid = bytes.fromhex("04A1B2C3D4E5F6")
    nfc = fe_nfc.nfc_file(fe_nfc.blank("ntag215", uid, "https://flipperzero.one"))
    want = [l for l in nfc.splitlines() if l.startswith("Page ")]
    try:
        cli = FlipperCli(usb_endpoint(mon))
        try:
            cli.read_until_prompt(15)
            cli.run("storage remove /ext/nfc/emu.nfc", timeout=5)   # write_chunk appends
            cli.write(f"storage write_chunk /ext/nfc/emu.nfc {len(nfc)}\r".encode())
            time.sleep(0.5)
            cli.write(nfc.encode())
            cli.read_until_prompt(5)
            found, _ = cli.watch("nfc", r"\[nfc\]>: ", timeout=8, stop=False)
            if not found:
                cli.write(b"\x03")
                return None
            found, _ = cli.watch("emulate -f /ext/nfc/emu.nfc", r"Emulating",
                                 timeout=8, stop=False)
            if not found:
                return False
            before = mon.cmd("qom-get /machine/nfc tx").strip().strip('"')
            mon.cmd("qom-set /machine/nfc reader ntag")
            for _ in range(100):
                time.sleep(0.1)
                if not mon.cmd("qom-get /machine/nfc reader").strip().strip('"'):
                    break
            path = mon.cmd("qom-get /machine/nfc tx").strip().strip('"')
            cli.write(b"\x03")
            time.sleep(0.3)
            cli.read_until_prompt(5)
            cli.run("exit", timeout=5)
        finally:
            cli.close()
    except (OSError, TimeoutError, ConnectionError, TypeError) as e:
        print(f"  nfc emulate: {e}")
        return False
    if not path or path == before:
        print("  nfc emulate: the reader read nothing")
        return False
    got = [l for l in pathlib.Path(path).read_text().splitlines() if l.startswith("Page ")]
    if got != want:
        print(f"  nfc emulate: {len(got)} pages read, {len(want)} expected ({path})")
    return got == want


def usbip_check(port: int) -> bool:
    """Play the PC's kernel over USB/IP: import, enumerate, configure, CLI."""
    import select
    import struct
    import fe_usbip
    try:
        if not any(d["busid"] == "1-1" for d in fe_usbip.devlist(port=port)):
            return False
        c = fe_usbip.UsbipClient(port=port)
        try:
            pend = []
            dd = c.control(0x80, 6, 0x0100, 0, 18, pending=pend)
            c.control(0x00, 9, 1, 0, pending=pend)                 # SET_CONFIGURATION
            c.control(0x21, 0x20, 0, 0, data=struct.pack("<IBBB", 115200, 0, 0, 8),
                      pending=pend)
            reads = [c.submit_in(2, 128) for _ in range(4)]
            c.control(0x21, 0x22, 3, 0, pending=pend)              # DTR
            out, end = b"", time.time() + 15
            for _, seq, _, data, _ in pend:
                out += data if seq in reads else b""
            while time.time() < end and b">: " not in out:
                if select.select([c.sock], [], [], 0.2)[0]:
                    cmd, seq, status, data, _ = c.receive()
                    if seq in reads:
                        out += data
                        reads.remove(seq)
                        reads.append(c.submit_in(2, 128))
        finally:
            c.close()
    except (OSError, ConnectionError) as e:
        print(f"  usbip: {e}")
        return False
    return dd[8:12] == bytes.fromhex("83044057") and b">: " in out


def usb_cli_check(mon: Monitor) -> bool:
    try:
        cli = FlipperCli(usb_endpoint(mon))
        try:
            cli.read_until_prompt(15)
            out = cli.run("info device", timeout=15)
        finally:
            cli.close()
    except (OSError, TimeoutError, ConnectionError, TypeError) as e:
        print(f"  usb: {e}")
        return False
    return re.search(r"hardware[._]name", out) is not None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("firmware")
    ap.add_argument("--no-sd", action="store_true")
    ap.add_argument("--no-usb", action="store_true", help="skip the USB CLI check")
    ap.add_argument("--timeout", type=float, default=30)
    ap.add_argument("--out", default=None, help="keep screenshots here")
    ap.add_argument("--keep", action="store_true",
                    help="keep the device directory (UART and QEMU logs)")
    ap.add_argument("qemu_args", nargs="*", help="extra QEMU arguments after --")
    args = ap.parse_args()

    name = f"smoke-{int(time.time())}"
    devdir = ROOT / "devices" / name
    out = pathlib.Path(args.out) if args.out else pathlib.Path(tempfile.mkdtemp(prefix="fe-smoke-"))
    out.mkdir(parents=True, exist_ok=True)
    uart = devdir / "uart.log"
    cmd = [str(ROOT / "flipperemu"), "-d", name, "run", "-f", args.firmware,
           "--headless", "--serial", f"file:{uart}"]
    if args.no_sd:
        cmd.append("--no-sd")
    cmd += ["--usb", "none" if args.no_usb else "pty"]
    usbip_port = None
    if not args.no_usb:
        import socket
        with socket.socket() as probe:                 # a free TCP port
            probe.bind(("127.0.0.1", 0))
            usbip_port = probe.getsockname()[1]
        cmd += ["--usbip", str(usbip_port)]

    if args.qemu_args:
        cmd += ["--", *args.qemu_args]

    results = []
    devdir.mkdir(parents=True, exist_ok=True)
    qemu_log = open(devdir / "qemu.log", "wb")     # a pipe nobody reads fills up
    proc = subprocess.Popen(cmd, stdout=qemu_log, stderr=subprocess.STDOUT)
    try:
        ok = wait_for(uart, r"Startup complete", args.timeout)
        results.append(("boot: 'Startup complete'", ok))
        if ok and not args.no_sd:
            results.append(("storage: 'card mounted'",
                            wait_for(uart, r"card mounted", args.timeout)))
        if ok:
            wait_for(uart, r"Secure Enclave|AnimationManager", 10)
            time.sleep(1.0)
            mon = Monitor(str(devdir / "monitor.sock"))
            mon.press("back")
            time.sleep(1.2)
            desk = out / "desktop.png"
            mon.screenshot(str(desk))
            mon.press("ok")
            time.sleep(1.2)
            menu = out / "menu.png"
            mon.screenshot(str(menu))
            d1, d2 = dark_pixels(desk), dark_pixels(menu)
            results.append((f"lcd: desktop drawn ({d1} dark px)", d1 > 500))
            results.append(("input: OK changed the screen",
                            desk.read_bytes() != menu.read_bytes()))
            if not args.no_usb:
                results.append(("usb: CLI 'info device' over CDC", usb_cli_check(mon)))
                results.append(("ir: NEC frame received and decoded", ir_check(mon)))
                results.append(("subghz: Princeton key received at 433.92 MHz",
                                subghz_check(mon)))
                results.append(("subghz: transmission captured with exact timings",
                                subghz_tx_check(mon)))
                results.append(("subghz: a key in an SDR recording, via the radio link",
                                subghz_sdr_check(mon, devdir)))
                # after Sub-GHz TX: firmware 1.5.1's IR transmitter leaves DMA2
                # ch1's half-transfer flag set, which garbles the next
                # Sub-GHz transmission (BRINGUP_LOG, entry 27)
                results.append(("ir: NEC transmission captured, decoded back",
                                ir_tx_check(mon)))
                results.append(("rfid: EM4100 tag read at 125 kHz",
                                rfid_read_check(mon, devdir)))
                results.append(("rfid: H10301 emulation captured, read back",
                                rfid_emulate_check(mon, devdir)))
                results.append(("rfid: H10301 written to a T5577, verified",
                                rfid_write_check(mon)))
                results.append(("ibutton: DS1990A read over 1-Wire",
                                ibutton_check(mon, devdir)))
                results.append(("ibutton: emulated DS1990A read by a virtual reader",
                                ibutton_emulate_check(mon)))
                results.append(("ibutton: Metakom emulation captured, read back",
                                ibutton_wave_check(mon, devdir)))
                results.append(("nfc: NTAG215 read by `mfu info` (all 135 pages)",
                                nfc_check(mon, devdir)))
                results.append(("nfc: MIFARE Classic 1K dumped (Crypto1, 16 sectors)",
                                mfc_check(mon, devdir)))
                results.append(("nfc: emulated NTAG215 read by a virtual reader",
                                nfc_emulate_check(mon)))
                results.append(("nfc: an ISO-DEP card on the link answers an APDU",
                                nfc_link_check(mon, devdir)))
                results.append(("nfc: the same card through a (simulated) Proxmark3",
                                nfc_pm3_check(mon, devdir)))
                results.append(("usbip: enumerated by a USB/IP client, CLI over URBs",
                                usbip_check(usbip_port)))
                results.append(("reboot: LCD redrawn after a warm reset",
                                reboot_check(mon, uart, out, args.timeout)))
            mon.cmd("quit")
        text = uart.read_text(errors="replace") if uart.exists() else ""
        results.append(("no crash in log", not CRASH.search(text)))
    finally:
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        qemu_log.close()
        if not args.keep:
            shutil.rmtree(devdir, ignore_errors=True)

    width = max(len(r[0]) for r in results)
    for label, ok in results:
        print(f"  {'SKIP' if ok is None else 'PASS' if ok else 'FAIL'}  {label:{width}}")
    print(f"  screenshots: {out}")
    if args.keep:
        print(f"  device:      {devdir}")
    return 0 if all(ok is not False for _, ok in results) else 1


if __name__ == "__main__":
    sys.exit(main())
