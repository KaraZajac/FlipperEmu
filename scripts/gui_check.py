#!/usr/bin/env python3
# Copyright (c) 2026, Kara Zajac
# SPDX-License-Identifier: BSD-3-Clause
"""Off-screen check of the window that looks like a Flipper (scripts/fe_gui.py).

    scripts/gui_check.py firmware/official-1.5.1/flipper-z-f7-update-1.5.1.tgz

Boots a throwaway device with `flipperemu run`, which opens the window: here
off-screen (QT_QPA_PLATFORM=offscreen) and silent (--audio none), so nothing
shows on the desktop.  Then it checks the front-panel link (frames, the
backlight), clicks buttons with the mouse, holds an NFC tag to it from the
bench, unplugs USB, switches it off as a real one is (Back held for 5 s, then
Power Off: the window stays, the emulator exits), switches it on again (Back
held: the tag is there again), switches it off the hard way and on again by
plugging USB in, reboots it with Left + Back, installs the firmware again from
the Device menu, and closes the window (which stops the emulator).  Exit status
0 = pass. Pictures are kept in --out.
"""
import argparse
import importlib.machinery
import os
import pathlib
import re
import shutil
import sys
import tempfile
import time

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
os.environ["QT_QPA_PLATFORM"] = "offscreen"

from PySide6 import QtCore, QtWidgets  # noqa: E402
from PySide6.QtCore import QPoint, Qt  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402

import fe_gui  # noqa: E402
import fe_gui_view  # noqa: E402

CRASH = re.compile(r"\[CRASH\]|furi_check failed|HardFault|Kernel is Dead")
results = []


def check(label, ok):
    results.append((label, bool(ok)))
    print(f"  {'PASS' if ok else 'FAIL'}  {label}", flush=True)
    return ok


class Driver:
    """fe_gui.run's stand-in: the same window, driven by this script"""

    def __init__(self, uart, out, timeout, firmware):
        self.uart, self.out, self.timeout = uart, out, timeout
        self.firmware = firmware

    def __call__(self, name, directory, launcher, session=None, scale=None):
        self.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
        self.win = fe_gui.MainWindow(name, directory, launcher, session, scale=2)
        self.session = session
        self.win.show()
        self.win.fit()
        try:
            self.checks()
        finally:
            if not self.win.closing:
                self.win.close()
            self.spin(0.3)
        check("close: closing the window stopped the emulator",
              session.poll() is not None)
        return self.win.exit_code

    # ---- helpers

    def spin(self, seconds):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            self.app.processEvents(QtCore.QEventLoop.AllEvents, 20)
            time.sleep(0.01)

    def until(self, cond, timeout):
        end = time.monotonic() + timeout
        while not cond():
            if time.monotonic() > end:
                return False
            self.spin(0.05)
        return True

    def settle(self):
        w = self.win
        self.until(lambda: not (w.runner.busy() or w.runner.queue), 30)
        self.spin(0.5)

    def boots(self):
        try:
            return self.uart.read_text(errors="replace").count("Startup complete")
        except OSError:
            return 0

    def booted(self):
        return self.until(lambda: self.boots() >= 1, self.timeout)

    def click(self, key):
        """a mouse click on a button of the drawn Flipper"""
        k = self.win.view.k
        centre = {"ok": fe_gui_view.PAD, "back": fe_gui_view.BACK}[key]
        pos = QPoint(round(centre.x() * k), round(centre.y() * k))
        QTest.mousePress(self.win.view, Qt.LeftButton, Qt.NoModifier, pos)
        self.spin(0.12)
        QTest.mouseRelease(self.win.view, Qt.LeftButton, Qt.NoModifier, pos)

    def hold(self, qtkey, ms):
        QTest.keyPress(self.win.view, qtkey)
        self.spin(ms / 1000)
        QTest.keyRelease(self.win.view, qtkey)

    def shot(self, tag):
        self.win.grab().save(str(self.out / f"window-{tag}.png"))

    def tile(self, kind):
        return self.win.tiles[kind].line1.full

    # ---- the checks

    def checks(self):
        w, v = self.win, self.win.view
        v.setFocus()
        check("link: the window connected; a frame arrived",
              self.until(lambda: w.state == "on" and v.frame, 20))
        if not check("boot: 'Startup complete'", self.booted()):
            return
        self.spin(3)
        self.click("back")              # whatever the first scene is
        self.spin(1.2)
        before = v.frame
        self.click("ok")
        self.spin(1.2)
        check("keys: a click on OK changed the screen", v.frame != before)
        check("backlight: on after a key press", v.backlight > 0)
        self.click("back")
        self.spin(1.0)
        self.shot("on")

        w.hold("nfc", ["ntag215", "04A1B2C3D4E5F6", "https://example.com"])
        w.set_cable("unplugged")
        self.settle()
        self.spin(1.0)
        check("bench: an NFC tag held to it", "NTAG215" in self.tile("nfc"))
        check("bench: USB unplugged",
              self.tile("usb") == "Unplugged"
              and w.props.get(("/machine/soc/usb", "attached")) is False)

        # as on a real one: Back held on the desktop, then Power Off (Right)
        self.hold(Qt.Key_Escape, 5600)
        self.spin(1.0)
        self.hold(Qt.Key_Right, 150)
        off = self.until(lambda: w.state == "off" and not w.qemu_up, 20)
        self.spin(0.5)
        check("power: Back held, Power Off: the window stays, the emulator "
              "exited", off and self.session.poll() == 0)
        self.shot("off")
        if not off:
            return

        self.hold(Qt.Key_Escape, fe_gui.POWER_ON_MS + 300)
        on = self.until(lambda: w.state == "on", 20) and self.booted()
        self.settle()
        self.spin(1.5)
        check("power: Back held switches it on; the tag is held to it again",
              on and "NTAG215" in self.tile("nfc"))

        w.hard_off()
        off = self.until(lambda: w.state == "off" and not w.qemu_up, 20)
        w.set_cable("computer")
        on = off and self.until(lambda: w.state == "on", 20) and self.booted()
        self.settle()
        self.until(lambda: w.props.get(("/machine/soc/usb", "device"), "")
                   .startswith("0483:"), 15)
        check("power: off the hard way, on again by plugging USB in",
              on and self.tile("usb") == "Plugged into a computer")

        # Left + Back for 3 s: held in reset; let go: it starts again
        boots = self.boots()
        QTest.keyPress(v, Qt.Key_Left)
        QTest.keyPress(v, Qt.Key_Escape)
        self.spin(fe_gui.RESET_MS / 1000 + 0.3)
        blank = v.blank
        QTest.keyRelease(v, Qt.Key_Escape)
        self.spin(0.05)
        QTest.keyRelease(v, Qt.Key_Left)
        check("reset: Left + Back for 3 s reboot it",
              blank and self.until(lambda: self.boots() > boots, self.timeout))
        self.spin(2)
        self.shot("rebooted")
        text = self.uart.read_text(errors="replace")
        check("no crash in log", not CRASH.search(text))

        # Device > Install firmware: off, flashed, on again
        QtWidgets.QMessageBox.question = \
            lambda *a, **k: QtWidgets.QMessageBox.Yes
        w.install(self.firmware)
        went = self.until(lambda: w.state in ("off", "installing"), 20)
        back = went and self.until(lambda: w.state == "on", 120) and self.booted()
        check("install: Device > Install firmware flashed it and started it",
              back and "flashed" in w.log.text.toPlainText())


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("firmware")
    ap.add_argument("--timeout", type=float, default=60)
    ap.add_argument("--out", help="keep the pictures here")
    ap.add_argument("--keep", action="store_true", help="keep the device directory")
    args = ap.parse_args()

    name = f"guicheck-{int(time.time())}"
    devdir = ROOT / "devices" / name
    out = pathlib.Path(args.out) if args.out else pathlib.Path(
        tempfile.mkdtemp(prefix="fe-gui-"))
    out.mkdir(parents=True, exist_ok=True)
    devdir.mkdir(parents=True, exist_ok=True)
    uart = devdir / "uart.log"
    os.environ["FLIPPEREMU_GUI_CONFIG"] = str(out / "gui.ini")
    fe_gui.run = Driver(uart, out, args.timeout, str(pathlib.Path(args.firmware).resolve()))
    launcher = importlib.machinery.SourceFileLoader(
        "flipperemu_launcher", str(ROOT / "flipperemu")).load_module()
    sys.argv = ["flipperemu", "-d", name, "run", "-f", args.firmware,
                "--audio", "none", "--serial", f"file:{uart}"]
    try:
        code = launcher.main()
        check("exit status of the run: 0", code == 0)
    finally:
        if not args.keep:
            shutil.rmtree(devdir, ignore_errors=True)
    print(f"  pictures: {out}")
    if args.keep:
        print(f"  device:   {devdir}")
    return 0 if all(ok for _, ok in results) else 1


if __name__ == "__main__":
    sys.exit(main())
