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
captures a `subghz tx`, reads a 125 kHz EM4100
tag (`rfid read`) and reads back its own H10301 emulation (`rfid emulate`),
enumerates over USB/IP, then reboots through the CLI and checks the LCD comes
back after the warm reset.
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

    results = []
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
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
                # after Sub-GHz TX: firmware 1.5.1's IR transmitter leaves DMA2
                # ch1's half-transfer flag set, which garbles the next
                # Sub-GHz transmission (BRINGUP_LOG, entry 27)
                results.append(("ir: NEC transmission captured, decoded back",
                                ir_tx_check(mon)))
                results.append(("rfid: EM4100 tag read at 125 kHz",
                                rfid_read_check(mon, devdir)))
                results.append(("rfid: H10301 emulation captured, read back",
                                rfid_emulate_check(mon, devdir)))
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
        shutil.rmtree(devdir, ignore_errors=True)

    width = max(len(r[0]) for r in results)
    for label, ok in results:
        print(f"  {'SKIP' if ok is None else 'PASS' if ok else 'FAIL'}  {label:{width}}")
    print(f"  screenshots: {out}")
    return 0 if all(ok is not False for _, ok in results) else 1


if __name__ == "__main__":
    sys.exit(main())
