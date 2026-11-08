#!/usr/bin/env python3
# Copyright (c) 2026, Kara Zajac
# SPDX-License-Identifier: BSD-3-Clause
"""Headless smoke test: does this firmware boot and react in FlipperEmu?

    scripts/smoke_test.py firmware/official-1.5.1/flipper-z-f7-update-1.5.1.tgz
    scripts/smoke_test.py ~/fw/build/f7-firmware-D/firmware.elf --no-sd

Boots a throwaway device, waits for the firmware's own log milestones, presses
Back (dismiss the first scene) and OK (main menu), checks the LCD changed, and
runs `info device` on the CLI over the emulated USB serial port.
Exit status 0 = pass. Screenshots are kept in the --out directory.
"""
import argparse
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
        print(f"  {'PASS' if ok else 'FAIL'}  {label:{width}}")
    print(f"  screenshots: {out}")
    return 0 if all(ok for _, ok in results) else 1


if __name__ == "__main__":
    sys.exit(main())
