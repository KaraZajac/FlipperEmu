#!/usr/bin/env python3
# Copyright (c) 2026, Kara Zajac
# SPDX-License-Identifier: BSD-3-Clause
"""Off-screen check of the window that looks like a Flipper (scripts/fe_gui.py).

    scripts/gui_check.py firmware/official-1.5.1/flipper-z-f7-update-1.5.1.tgz

Boots a throwaway device with `flipperemu run`, which opens the window: here
off-screen (QT_QPA_PLATFORM=offscreen) and silent (--audio none), so nothing
shows on the desktop.  Then it checks the front-panel link (frames, the
backlight and its fade when the firmware starts, the LED's blink while NFC >
Read looks for a card), clicks buttons with the mouse, records the screen as
a GIF
(Device > Record the screen), pauses it and lets it go on (Device > Pause,
and the launcher's `pause` from outside), opens the firmware's log with a
click on the GPIO pins and sets the firmware's log level from it, copies a
file to its card, drops an app on the window (which installs and starts it),
does that again with F5 and closes the app, drops the same app as if it were
built for another API (refused, or asked about on the Flipper's screen and
then answered with a button), holds pins of the GPIO header high and low with
a click on them (they light, and the firmware reads them so), lets go of
them, records what a pin does to a file (GPIO > Record what the pins do),
has a script in the firmware's JavaScript make a PWM on a pin (lit for the
part of its width that it is high for, said under the pointer and in the
console; skipped on a firmware whose JavaScript makes none), finds a sensor and an EEPROM on the header's I2C bus in the GPIO menu (the
device starts with `--i2c tmp105@0x48 --i2c eeprom@0x50`), sets the sensor's
temperature from there, lets the firmware scan the bus (which the console
says), records a scan of that bus to a file (GPIO > Record what goes over the
I2C bus), ends from the menu a record that a shell began, gives the window a
call to an address that was not shifted (the word on that in the console and
the menu), finds a flash on the header's SPI bus in the GPIO menu (the device
starts with `--spi flash` as well), holds its chip select low from outside,
has a script in the firmware's JavaScript read and write that flash (which
the console says; skipped on a firmware whose JavaScript has no `spi`), gives
the window bytes that no device heard, holds an NFC tag to it from the
bench, unplugs USB, switches it off as a real one is (Back held for 5 s, then
Power Off: the window stays, the emulator exits), copies a file to the card
of the switched-off Flipper, switches
it on again (Back held: the tag is there again), switches it off the hard way
and on again by plugging USB in, reboots it with Left + Back, lets an app
crash the firmware (said in the console; a debug build halts then, which is
said too, and Reboot starts it), updates it with its own package (Device >
Update with a package: once left to itself half-way, once waited for),
installs the firmware again from the Device menu, and closes the window
(which stops the emulator).  Early on, it saves a snapshot from the Device
menu (paused, with its menu open and a tag held), and at the end it loads
that snapshot standing still, in a new window: the picture of the save to
the pixel, the tag, the snapshot's name in the console and in `status`, and
Pause, which lets it go on and write to its card.  Exit status 0 = pass.
Pictures are kept in --out.
"""
import argparse
import importlib.machinery
import importlib.util
import json
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
os.environ["QT_QPA_PLATFORM"] = "offscreen"

from PySide6 import QtCore, QtGui, QtWidgets  # noqa: E402
from PySide6.QtCore import QPoint, Qt  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402

import fe_gui  # noqa: E402
import fe_gui_view  # noqa: E402
import fe_header  # noqa: E402
from app_check import other_api  # noqa: E402

# a PWM on pin 2, another one when pin 3 is held high, none when it is low
PWM_JS = """\
let eventLoop = require("event_loop");
let gpio = require("gpio");
let a7 = gpio.get("pa7");
let a6 = gpio.get("pa6");
a6.init({ direction: "in", inMode: "plain_digital", pull: "down" });
function until(level) {
    while (a6.read() !== level) {
        delay(50);
    }
}
a7.pwmWrite(1000, 25);
until(true);
a7.pwmWrite(2000, 75);
until(false);
a7.pwmStop();
print("end");
"""
CRASH = re.compile(r"\[CRASH\]|furi_check failed|HardFault|Kernel is Dead")
results = []
SAVED = {}      # what the window showed at the save of phase one's snapshot


def check(label, ok, detail=""):
    results.append((label, bool(ok)))
    print(f"  {'PASS' if ok else 'FAIL'}  {label}", flush=True)
    if detail and not ok:
        print("        " + str(detail).strip().replace("\n", "\n        "),
              flush=True)
    return ok


class Driver:
    """fe_gui.run's stand-in: the same window, driven by this script"""

    def __init__(self, uart, out, timeout, firmware):
        self.uart, self.out, self.timeout = uart, out, timeout
        self.firmware = firmware
        self.seen = 0                   # boots that were waited for

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
        """how often it has started: the log is the whole run's"""
        try:
            return self.uart.read_text(errors="replace").count("Startup complete")
        except OSError:
            return 0

    def booted(self):
        """it has started (again, since the last time this was asked)"""
        ok = self.until(lambda: self.boots() > self.seen, self.timeout)
        self.seen = self.boots()
        return ok

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

    def recording(self):
        """Device > Record the screen (its file dialog left out), and what
        Qt's own GIF reader makes of the file"""
        w = self.win
        w.a_record.trigger()            # as the menu and Ctrl+Shift+R do
        self.spin(0.4)
        started = w.recording is not None and "REC" in w.rec_label.text()
        self.click("ok")
        self.spin(1.0)
        self.click("back")
        self.spin(1.0)
        rec = w.stop_recording()
        path = self.out / "window-recording.gif"
        done = []
        w.recorded.connect(lambda *told: done.append(told))
        if rec is not None and rec.shots:
            w.save_recording(rec, str(path))
            self.until(lambda: done, 30)
        pictures, ms = [], 0
        reader = QtGui.QImageReader(str(path))
        while path.is_file():
            picture = reader.read()
            if picture.isNull():
                break
            pictures.append(picture.copy())
            ms += reader.nextImageDelay()
        seconds = rec.seconds() if rec else 0
        check("recording: Device > Record the screen, two clicks: a GIF of "
              f"the LCD ({len(pictures)} pictures, {ms / 1000:.1f} s)",
              started and not w.a_record.isChecked() and not w.rec_label.text()
              and bool(done) and not done[0][2]
              and path.read_bytes()[:6] == b"GIF89a" and len(pictures) > 2
              and pictures[0].size() == QtCore.QSize(512, 256)
              and any(q != pictures[0] for q in pictures[1:])
              and abs(ms / 1000 - seconds) < 0.1 and seconds > 2)

    def pausing(self):
        """Device > Pause; and a pause that is not the window's own"""
        w, v = self.win, self.win.view
        frames = []
        w.link.frame.connect(frames.append)
        w.a_pause.trigger()             # as the menu and Ctrl+P do
        said = self.until(lambda: w.stopped == "paused", 10)
        self.settle()
        frames.clear()
        self.click("ok")                # felt when it goes on: the menu opens
        self.spin(1.5)
        check("pause: Device > Pause: the window says so, and the screen "
              "stands still",
              said and w.a_pause.isChecked() and v.paused == "paused"
              and w.state_label.text() == "paused"
              and w.windowTitle().endswith("(paused)") and not frames
              and "paused" in w.statusBar().currentMessage())
        self.shot("paused")
        w.a_pause.trigger()
        went = self.until(lambda: not w.stopped, 10)
        moved = self.until(lambda: frames, 5)
        self.settle()
        check("pause: once more and it goes on, with the button that was "
              "pressed meanwhile",
              went and moved and not w.a_pause.isChecked() and not v.paused
              and w.state_label.text() == "running"
              and "paused" not in w.windowTitle())
        self.click("back")
        self.spin(1.0)

        there = self.launcher("pause")
        said = self.until(lambda: w.stopped == "paused", 10)
        shown = w.a_pause.isChecked() and w.state_label.text() == "paused"
        back = self.launcher("continue")
        went = self.until(lambda: not w.stopped, 10)
        check("pause: the launcher's `pause` and `continue`, from outside: "
              "the window shows what the emulator says",
              there.returncode == 0 and said and shown
              and back.returncode == 0 and went
              and not w.a_pause.isChecked())

    def snapshot_save(self):
        """Device > Save a snapshot (its name dialog left out), of the Flipper
        paused with its menu open and an RFID tag held.  Phase two loads it
        (LoadedDriver): the picture of this moment is kept for that."""
        w, v = self.win, self.win.view
        w.hold("rfid", ["EM4100", "DEADBEEF01"])
        self.click("ok")                # the menu
        self.spin(1.5)
        w.a_pause.trigger()
        paused = self.until(lambda: w.stopped == "paused", 10)
        self.settle()
        self.spin(0.5)
        SAVED["frame"] = v.frame
        w.save_snapshot("gui")
        self.settle()
        saved = ("snapshot gui:" in self.console()
                 and "snapshot gui:" in w.statusBar().currentMessage())
        still = w.stopped == "paused"
        w.a_pause.trigger()
        went = self.until(lambda: not w.stopped, 10)
        state = self.uart.parent / "snapshots" / "gui" / "state"
        if not check("snapshot: Device > Save a snapshot of the paused "
                     "Flipper (its menu open, a tag held): saved, it stays "
                     "paused, and goes on with Pause",
                     paused and saved and still and went and state.is_file()):
            SAVED.clear()               # (no phase two)
        w.take_away("rfid")
        self.click("back")
        self.spin(1.0)
        self.settle()

    def launcher(self, *args):
        """the launcher, from outside the window"""
        w = self.win
        return subprocess.run(
            [sys.executable, str(w.launcher), "-d", w.name, *args],
            capture_output=True, text=True, timeout=30)

    def pins(self):
        """The GPIO header: its pins lit by their level, and a click on one
        to hold it from outside"""
        w, v = self.win, self.win.view

        def press(number):
            QTest.mouseClick(v, Qt.LeftButton, Qt.NoModifier,
                             v.pin_point(number))
            self.spin(0.4)

        def state(number):
            return v.header.get(number)

        self.until(lambda: len(w.header) == 13, 5)
        tip = v.pin_tips.get(2, "")
        check("pins: the header as the machine has it: thirteen signal pins, "
              "none held, and a line for each one under the pointer",
              len(w.header) == 13 and not any(p["held"] for p in w.header)
              and tip.startswith("Pin 2, A7 (PA7): ")
              and tip.endswith("Click to hold it high or low from outside")
              and v.pin_tips.get(1) == "Pin 1: +5V"
              and v.tip("gpio", 2) == tip and v.pin_at(v.pin_point(2)) == 2,
              f"{len(w.header)} pins; {tip!r}")

        press(2)
        acts = [a for a in w.pin_menu.actions() if not a.isSeparator()]
        shown = w.pin_menu.isVisible() and len(acts) == 5
        check("pins: a click on pin 2 (A7): its menu, with what the firmware "
              "has it as, a voltage to hold to it (the ADC has it as an "
              "input), and nothing to let go of yet",
              shown and acts[0].text().startswith("Pin 2, A7 (PA7): ")
              and not acts[0].isEnabled() and not acts[4].isEnabled()
              and acts[3].text() == "Hold it at a voltage (the ADC measures "
                                    "it)…" and not acts[3].isChecked(),
              "; ".join(a.text() for a in acts))
        if shown:
            acts[1].trigger()           # Hold it high
        w.pin_menu.close()
        held = self.until(lambda: state(2) == ("high", "high"), 15)
        self.settle()
        said = self.launcher("gpio", "PA7").stdout.strip()
        check("pins: Hold it high: the pin is lit and has a wire on it; the "
              "firmware reads it high",
              held and said == "high" and "held high" in v.pin_tips.get(2, "")
              and "PA7 (pin 2 of the header): held high" in self.console(),
              f"{state(2)}; gpio PA7: {said!r}")

        press(3)
        acts = [a for a in w.pin_menu.actions() if not a.isSeparator()]
        if w.pin_menu.isVisible() and len(acts) == 5:
            acts[2].trigger()           # Hold it low
        w.pin_menu.close()
        low = self.until(lambda: state(3) == ("low", "low"), 15)
        self.settle()
        self.shot("pins")
        w.fill_gpio_menu()              # as the menu bar's GPIO menu opens
        line = w.gpio_items[2]
        subs = line.actions() if isinstance(line, QtWidgets.QMenu) else []
        check("pins: pin 3 held low; the GPIO menu has every pin, the held "
              "ones ticked",
              low and len(w.gpio_items) == 18 and len(subs) == 4
              and "held high from outside" in line.title()
              and subs[0].isChecked() and not subs[1].isChecked()
              and not subs[2].isChecked()
              and subs[3].isEnabled() and w.a_gpio_free.isEnabled()
              and not w.gpio_items[1].isEnabled()
              and w.gpio_items[1].text() == "Pin 1: +5V",
              f"{state(3)}; {len(w.gpio_items)} lines")

        w.a_gpio_free.trigger()
        free = self.until(lambda: state(2) is not None
                          and not state(2)[1] and not state(3)[1], 15)
        self.settle()
        said = self.launcher("gpio").stdout
        check("pins: Let go of all pins: nothing is held any more",
              free and "held" not in said
              and "nothing is held to the GPIO header any more"
              in self.console(), f"{state(2)} {state(3)}")

        # a voltage, to a pin that the ADC has as an input
        was = []

        def answer():                   # the dialog, once it is there
            for d in w.findChildren(fe_gui.W.VoltsDialog):
                if d.isVisible():
                    was.append((d.windowTitle(), d.volts.value()))
                    d.volts.setValue(1.25)
                    d.accept()

        timer = QtCore.QTimer(interval=100)
        timer.timeout.connect(answer)
        press(2)
        acts = [a for a in w.pin_menu.actions() if not a.isSeparator()]
        w.pin_menu.close()
        if len(acts) == 5:
            timer.start()
            acts[3].trigger()           # Hold it at a voltage…
            timer.stop()
        held = self.until(lambda: state(2) == ("low", "low"), 15)
        self.settle()
        said = self.launcher("gpio").stdout
        tip = v.pin_tips.get(2, "")
        press(2)
        acts = [a for a in w.pin_menu.actions() if not a.isSeparator()]
        w.pin_menu.close()
        check("pins: Hold it at a voltage: the dialog starts at half of what "
              "the chip runs on; 1.25 V there, and the pin has a wire on it, "
              "is low as a level, and its menu has the voltage, ticked",
              was == [("A voltage on pin 2, A7 (PA7)", 1.65)] and held
              and "held at 1.25 V from outside (low as a level)" in tip
              and "held at 1.25 V from outside (low as a level)" in said
              and len(acts) == 5 and acts[3].isChecked()
              and acts[3].text() == "Hold it at 1.25 V: another voltage…"
              and not acts[1].isChecked() and not acts[2].isChecked()
              and acts[4].isEnabled(),
              f"{was}; {state(2)}; {tip!r}; "
              + "; ".join(a.text() for a in acts) + "\n" + said)
        press(5)
        acts = [a for a in w.pin_menu.actions() if not a.isSeparator()]
        w.pin_menu.close()
        w.fill_gpio_menu()
        line = w.a_gpio_adc.text() if w.a_gpio_adc else ""
        check("pins: pin 5 (B3) is no input of the ADC's: no voltage in its "
              "menu; and the GPIO menu has the ADC in a line, as `gpio` has "
              "it",
              len(acts) == 4 and acts[3].text() == "Let go of it"
              and line.startswith("ADC1: ") and line.split(": ", 1)[1] in said
              and not w.a_gpio_adc.isEnabled(),
              "; ".join(a.text() for a in acts) + f"\n{line!r}\n{said}")
        self.launcher("gpio", "PA7", "free")
        self.until(lambda: state(2) is not None and not state(2)[1], 15)
        self.settle()

        w.show_log()
        self.spin(0.3)
        behind = not w.front.get(w.fwlog)
        press(13)
        log = bool(w.front.get(w.fwlog))
        press(17)
        ibutton = w.menu_ibutton.isVisible()
        w.menu_ibutton.close()
        check("pins: a click on 13 (TX) opens the firmware log, one on 17 "
              "(1W) the iButton menu; neither is a pin to hold",
              behind and log and ibutton and not w.pin_menu.isVisible()
              and "Click to read the firmware's log" in v.pin_tips.get(13, "")
              and "Click for the iButton menu" in v.pin_tips.get(17, ""),
              f"log behind {behind}, in front {log}; iButton menu {ibutton}")

        w.fill_gpio_menu()
        ports = {name: a.text() for name, a in w.gpio_ports.items()}
        said = self.launcher("gpio").stdout
        first = ports.get("USART1", "")
        check("pins: the two serial ports as the firmware has them, in the "
              "GPIO menu as `gpio` has them, and on pin 13: the log's, which "
              "waits for a call, and one that is off",
              re.match(r"Pins 13 and 14 \(USART1\): \d+ baud, 8N1; its "
                       r"receiver is off, and the firmware waits for a call "
                       r"on pin 14", first)
              and first.split(": ", 1)[1] in said
              and ports.get("LPUART1") == "Pins 15 and 16 (LPUART1): off "
                                          "(nothing has opened it)"
              and "USART1's TX, " + first.split(": ", 1)[1]
              in v.pin_tips.get(13, "")
              and not w.gpio_ports["USART1"].isEnabled(),
              f"{ports}\npin 13: {v.pin_tips.get(13)}")
        real = w.props.get(("/machine", "header-serial"))
        w.on_serial("USART1:13:14:230215:8N1:on:unread "
                    "LPUART1:15:16:9600:8N1:on:irq", tell=True)
        tip = v.pin_tips.get(15, "")
        w.on_serial(real, tell=True)
        self.spin(0.2)
        said = self.console()
        check("pins: a port that an app opens, and closes, is in the log "
              "with its speed (the machine's word for it given here)",
              "Pins 15 and 16 (LPUART1): 9600 baud, 8N1; what comes in is "
              "read (by interrupt)" in said
              and "Pins 15 and 16 (LPUART1): off (nothing has opened it)"
              in said and "USART1" not in said.split("9600 baud")[-1]
              and "LPUART1" not in tip, f"{said[-400:]}\npin 15: {tip}")

    def i2c_bus(self):
        """The header's I2C bus: the devices that `run --i2c` put there in
        the GPIO menu, a sensor's temperature set from it, and what the
        firmware calls there in the console"""
        w, v = self.win, self.win.view
        self.until(lambda: w.i2c is not None, 5)
        w.fill_gpio_menu()              # as the menu bar's GPIO menu opens
        self.settle()                   # ... and the launcher said the values
        said = self.launcher("i2c").stdout
        bus = w.a_gpio_i2c.text() if w.a_gpio_i2c else ""
        lines = {at: (m.title(), [(a.text(), a.isEnabled())
                                  for a in m.actions()])
                 for at, m in w.gpio_i2c.items()}
        sensor, rom = lines.get(0x48, ("", [])), lines.get(0x50, ("", []))
        kept = re.search(r"kept in \S+i2c-50\.bin", said)
        check("i2c: the bus in the GPIO menu as `i2c` has it: a sensor at "
              "0x48 with its temperature, which can be set, an EEPROM at "
              "0x50 and the file of its contents; both on pins 15 and 16",
              bus.startswith("Pins 15 and 16 (I2C3): tmp105 at 0x48, "
                             "at24c-eeprom at 0x50; ")
              and bus.split(": ", 1)[1] in said and not w.a_gpio_i2c.isEnabled()
              and sensor == ("0x48: tmp105, temperature 23.5 °C",
                             [("temperature 23.5 °C: set it…", True)])
              and rom[0].startswith("0x50: at24c-eeprom, ")
              and "kept in i2c-50.bin" in rom[0] and kept
              and (kept[0], False) in rom[1] and ("rom-size 256", False) in rom[1]
              and "I2C3's SDA, tmp105 at 0x48, at24c-eeprom at 0x50 on it; "
              in v.pin_tips.get(15, "")
              and "I2C3's SCL, tmp105 at 0x48, " in v.pin_tips.get(16, ""),
              f"{bus}\n{lines}\npin 15: {v.pin_tips.get(15)}\n{said}")

        was = []

        def answer():                   # the dialog, once it is there
            for d in w.findChildren(fe_gui.W.DegreesDialog):
                if d.isVisible():
                    was.append(d.celsius.value())
                    d.celsius.setValue(30.25)
                    d.accept()

        timer = QtCore.QTimer(interval=100)
        timer.timeout.connect(answer)
        acts = w.gpio_i2c[0x48].actions() if 0x48 in w.gpio_i2c else []
        if acts and acts[0].isEnabled():
            timer.start()
            acts[0].trigger()           # temperature 23.5 °C: set it…
            timer.stop()
        self.settle()
        bare = self.launcher("i2c", "0x48", "temperature").stdout.strip()
        w.fill_gpio_menu()
        self.settle()
        title = w.gpio_i2c[0x48].title() if 0x48 in w.gpio_i2c else ""
        check("i2c: the sensor's line, 'set it': a dialog with the "
              "temperature as it is; 30.25 °C given there is what the sensor "
              "has, the console says so, and the menu",
              was == [23.5] and bare == "30.25"
              and "tmp105 at 0x48: temperature 30.25 °C" in self.console()
              and title == "0x48: tmp105, temperature 30.25 °C",
              f"the dialog had {was}; i2c 0x48 temperature: {bare!r}; "
              f"{title!r}\n{self.console()[-300:]}")

        scan = self.launcher("cli", "--timeout", "25", "i2c")
        news = ("Pins 15 and 16 (I2C3): the firmware called 128 addresses, 2 "
                "answered; the last: 0x7f to write, no answer")
        told = self.until(lambda: news in self.console(), 10)
        open_ = w.a_gpio_i2c.text() if w.a_gpio_i2c else ""
        w.fill_gpio_menu()
        self.settle()
        bus = w.a_gpio_i2c.text() if w.a_gpio_i2c else ""
        said = self.launcher("i2c").stdout
        check("i2c: the firmware scans the bus (its `i2c` command): one line "
              "in the console, 128 addresses of which two answered, and the "
              "GPIO menu has the calls as `i2c` has them, a menu that was "
              "open meanwhile too",
              scan.returncode == 0 and told and open_ == bus
              and self.console().count("(I2C3): the firmware called") == 1
              and re.search(r"the firmware called \d+ addresses, \d+ "
                            r"answered; the last: 0x7f to write, no answer",
                            bus) and bus.split(": ", 1)[1] in said,
              f"{scan.returncode}: {scan.stdout[-200:]}{scan.stderr[-200:]}\n"
              f"{bus}\nwhile open: {open_}\n{self.console()[-300:]}")

    def i2c_record(self):
        """The record of the header's I2C bus, from the GPIO menu (the file
        is given here: the menu's line asks for one), and the window's word
        on an address that was not shifted"""
        w = self.win
        path = self.out / "window-i2c.txt"
        name = path.name
        offer = "Record what goes over the I2C bus…"

        def line():                     # the menu's, as it opens
            w.fill_gpio_menu()
            a = w.a_gpio_i2c_record
            return (a.text(), a.isEnabled()) if a else ("", False)

        def calls():
            return w.i2c_record["calls"] if w.i2c_record else -1

        offered = line() == (offer, True) and not w.bus_label.text()
        w.start_i2c_record(str(path))
        began = self.until(lambda: w.i2c_record is not None, 15)
        self.settle()
        menu, label = line()[0], w.bus_label.text()
        check("i2c record: GPIO > Record what goes over the I2C bus: it "
              "runs, and the status bar and the menu say so",
              offered and began and w.i2c_record["path"] == str(path)
              and label == f"● {name}: no address called"
              and menu == ("End the record of the I2C bus "
                           f"({name}: no address called so far)")
              and "every address called" in self.console(),
              f"offered {offered}; {w.i2c_record}; {label!r}; {menu!r}")

        scan = self.launcher("cli", "--timeout", "25", "i2c")
        counted = self.until(lambda: calls() >= 128, 15)
        self.settle()
        self.shot("i2c-record")
        menu, label = line()[0], w.bus_label.text()
        check("i2c record: the firmware scans the bus: 128 addresses, "
              "counted in the status bar, and in the menu with the two that "
              "answered",
              scan.returncode == 0 and counted
              and label == f"● {name}: 128 addresses called"
              and menu.startswith("End the record of the I2C bus "
                                  f"({name}: 128 addresses called, 2 "
                                  "answered; ")
              and menu.endswith(" so far)")
              and name in w.bus_label.toolTip(),
              f"{scan.returncode}; {w.i2c_record}; {label!r}; {menu!r}")

        line()
        if w.a_gpio_i2c_record:
            w.a_gpio_i2c_record.trigger()       # End the record of the bus
        ended = self.until(lambda: w.i2c_record is None, 15)
        self.settle()
        try:
            on, parts = fe_header.read_i2c_record(path)
            got = (len(parts), sorted(p["address"] for p in parts
                                      if p["taken"]),
                   [(d["type"], d["address"]) for d in on])
        except (OSError, ValueError) as e:
            got = str(e)
        check("i2c record: End the record of the I2C bus: the file has the "
              "128 calls, the sensor's answer and the EEPROM's among them, "
              "and nothing says that a record runs",
              ended and got == (128, [0x48, 0x50], [("tmp105", 0x48),
                                                    ("at24c-eeprom", 0x50)])
              and line() == (offer, True) and not w.bus_label.text()
              and f"the record is {path}" in self.console(),
              f"ended {ended}; {got}; {w.bus_label.text()!r}; {line()}")

        # A record that a shell began, and a watch in a shell, which keeps a
        # record of its own for as long as it watches: the window has both,
        # and its menu ends the first
        other = self.out / "shell-i2c.txt"
        shell = self.launcher("i2c", "record", str(other))
        seen = self.until(lambda: bool(w.i2c_record)
                          and w.i2c_record["path"] == str(other), 15)
        label = w.bus_label.text()
        line()
        if seen and w.a_gpio_i2c_record:
            w.a_gpio_i2c_record.trigger()
        ended = self.until(lambda: w.i2c_record is None, 15)
        self.settle()
        after = self.launcher("i2c", "record").stdout
        watch = subprocess.Popen(
            [sys.executable, str(w.launcher), "-d", w.name, "i2c", "watch",
             "--for", "4"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True)
        own = self.until(lambda: bool(w.i2c_record) and w.i2c_record["path"]
                         .endswith("/i2c-watch.txt"), 15)
        watching = w.bus_label.text()
        gone = self.until(lambda: w.i2c_record is None, 15)
        try:
            said = watch.communicate(timeout=15)[0]
        except subprocess.TimeoutExpired:
            watch.kill()
            said = "the watch did not end: " + watch.communicate()[0]
        check("i2c record: one that a shell began (`i2c record FILE`): the "
              "status bar has it, and the menu's End ends it; `i2c watch` in "
              "a shell: the record that it keeps is there for as long as it "
              "watches",
              shell.returncode == 0 and seen
              and label == f"● {other.name}: no address called" and ended
              and "not being recorded" in after and other.exists()
              and own and watching == "● i2c-watch.txt: no address called"
              and gone and watch.returncode == 0 and not w.bus_label.text(),
              f"{shell.returncode}: {shell.stdout}{shell.stderr}seen {seen}, "
              f"{label!r}, ended {ended}; {after}the watch's: {own}, "
              f"{watching!r}, gone {gone}; {watch.returncode}: {said}")

        # An address that was not shifted.  No firmware calls one by itself,
        # and only some can from a script (i2c_check.py has that, with `i2c`
        # and `i2c watch`).  So the window gets the machine's word on its bus
        # as the link gives it, with three calls to 0x24 that nobody took,
        # and after that the true one again.
        key = ("/machine", "header-i2c")
        true = w.props.get(key)
        word = ("nobody is at 0x24. If the tmp105 at 0x48 was meant: the "
                "firmware takes an address shifted left by one bit, 0x48 << 1 "
                "(0x90)")
        news = ("Pins 15 and 16 (I2C3): the firmware called 3 addresses, "
                f"none answered; the last: 0x24 to write, no answer\n({word})")

        def hint():                     # the menu's line under the bus's
            a = w.a_gpio_i2c_hint
            return (a.text(), a.isVisible(), a.isEnabled()) if a else None

        w.fill_gpio_menu()
        before = hint()
        made, n = re.subn(
            r"^(\w+:\d+:\d+):(\d+):(\d+):\S+", lambda m:
            f"{m[1]}:{int(m[2]) + 3}:{int(m[3]) + 3}:24w-", true or "")
        if n:
            w.on_prop(*key, made)
        told = self.until(lambda: news in self.console(), 5)
        open_ = hint()                  # the menu was open meanwhile
        w.fill_gpio_menu()
        # (the console by itself: the firmware log is the tab in front)
        w.log.grab().save(str(self.out / "window-i2c-hint.png"))
        anew = hint()
        if n:
            w.on_prop(*key, true)
        self.spin(1.5)
        w.fill_gpio_menu()
        check("i2c: a call to 0x24 that nobody took, while a sensor is at "
              "0x48 (the machine's word on the bus, given to the window as "
              "the link gives it): the console's line has the word on the "
              "shift, once, and the GPIO menu a line under the bus's, in a "
              "menu that was open as well; the bus as it is: no such line",
              n == 1 and before == ("", False, False) and told
              and open_ == (f"({word})", True, False) and anew == open_
              and hint() == before and self.console().count(word) == 1
              and w.i2c and w.i2c["last"]["address"] == 0x7f,
              f"{true!r} -> {made!r}\nbefore {before}, open {open_}, anew "
              f"{anew}, after {hint()}\n{self.console()[-400:]}")

    def spi_bus(self):
        """The header's SPI bus: the flash that `run --spi flash` put there
        in the GPIO menu, its chip select held low from outside, and what
        the firmware clocks there in the console"""
        w, v = self.win, self.win.view
        self.until(lambda: w.spi is not None, 5)
        off = "Pin 4 (PA4): w25q32, not selected; kept in spi-4.bin"
        on = "Pin 4 (PA4): w25q32, selected; kept in spi-4.bin"

        def lines():                    # the GPIO menu's, as they are
            return (w.a_gpio_spi.text() if w.a_gpio_spi else "",
                    {pin: (m.title(), [(a.text(), a.isEnabled())
                                       for a in m.actions()])
                     for pin, m in w.gpio_spi.items()})

        def menu():
            w.fill_gpio_menu()          # as the menu bar's GPIO menu opens
            self.settle()               # ... and the launcher said the file
            return lines()

        def selected():
            return bool(w.spi and w.spi["devices"]
                        and w.spi["devices"][0]["selected"])

        def counts():
            try:
                bus = json.loads(self.launcher("spi", "--json").stdout)
                return bus["heard"], bus["unheard"]
            except (ValueError, KeyError):
                return None

        bus, devices = menu()
        said = self.launcher("spi").stdout
        flash = devices.get(4, ("", []))
        kept = re.search(r"its contents are kept in \S+spi-4\.bin", said)
        tip, pin = v.pin_tips.get(4, ""), w.gpio_items.get(4)
        check("spi: the bus in the GPIO menu as `spi` has it: a flash with "
              "its chip select on pin 4, not selected, and the file of its "
              "contents; pin 4's line says whose chip select it is",
              bus.startswith("Pins 2, 3 and 5 (SPI1): w25q32 with its chip "
                             "select on pin 4; ")
              and bus.split(": ", 1)[1] in said and not w.a_gpio_spi.isEnabled()
              and len(devices) == 1 and flash[0] == off and kept
              and flash[1] == [(kept[0][0].upper() + kept[0][1:], False)]
              and tip.startswith("Pin 4, A4 (PA4): w25q32's chip select; ")
              and isinstance(pin, QtWidgets.QMenu) and pin.title().startswith(
                  "Pin 4, A4 (PA4): w25q32's chip select; "),
              f"{bus}\n{devices}\npin 4: {tip}\n{said}")

        w.hold_pin("PA4", "low")        # a chip select that is wired to GND
        chosen = self.until(selected, 15)
        self.settle()
        open_, tip = lines(), v.pin_tips.get(4, "")
        said = self.launcher("spi").stdout
        anew = menu()
        w.hold_pin("PA4", "free")
        freed = self.until(lambda: not selected(), 15)
        self.settle()
        after = lines()
        check("spi: pin 4 held low from outside: the flash is selected, "
              "which its line in the GPIO menu says (a menu that was open "
              "meanwhile too), pin 4's line under the pointer and `spi`; "
              "let go of, it is not selected any more",
              chosen and open_[1].get(4, ("",))[0] == on and anew == open_
              and "w25q32's chip select, which selects it now; " in tip
              and "held low" in tip
              and re.search(r"pin 4 +\(PA4\) +w25q32 +selected; ", said)
              and freed and after[1].get(4, ("",))[0] == off
              and "selects it now" not in v.pin_tips.get(4, "x"),
              f"open {open_}\nanew {anew}\nafter {after}\npin 4: {tip}\n{said}")
        self.shot("spi")

        news = re.compile(
            r"  Pins 2, 3 and 5 \(SPI1\): the firmware clocked (\d+) bytes "
            r"there, all with a device selected; the last at 2 MHz, mode 0, "
            r"MSB first, 8 bits$", re.M)
        before = counts()
        self.launcher("push", str(ROOT / "examples/spi/flash.js"),
                      "/ext/fe-gui-flash.js")
        run = self.launcher("cli", "--timeout", "25", "js /ext/fe-gui-flash.js")
        text = run.stdout + run.stderr
        if "spi done" not in text and "spi" in text and re.search(
                r"load fail|not found|no such", text, re.I):
            print("  SKIP  spi: a script reads and writes the flash: this "
                  "firmware's JavaScript has no `spi` ("
                  + " / ".join(x.strip() for x in text.splitlines()
                               if "spi" in x)[:160] + ")", flush=True)
        else:
            told = self.until(lambda: news.search(self.console()), 10)
            open_ = lines()[0]          # the menu was open meanwhile
            self.spin(1.5)              # (a second line would come by now)
            now, said = counts(), self.launcher("spi").stdout
            bus = menu()[0]
            m = news.search(self.console())
            check("spi: a script on the Flipper reads and writes the flash "
                  "(examples/spi/flash.js): one line in the console for its "
                  "bytes, all with a device selected, and the GPIO menu has "
                  "them as `spi` has them, a menu that was open meanwhile too",
                  run.returncode == 0 and "written: 46 6c 69 70 70 65 72 ff"
                  in text and "spi done" in text and told and m
                  and 1 <= int(m[1]) <= 63 and before and now
                  and (now[0] - before[0], now[1] - before[1]) == (63, 0)
                  and self.console().count("(SPI1): the firmware clocked") == 1
                  and open_ == bus and "all with a device selected; the last "
                  "at 2 MHz, mode 0, MSB first, 8 bits" in bus
                  and bus.split(": ", 1)[1] in said,
                  f"{run.returncode}: {text[-300:]}\n{before} -> {now}\n{bus}\n"
                  f"while open: {open_}\n{self.console()[-300:]}")
        self.launcher("rm", "/ext/fe-gui-flash.js")

        # Bytes that no device heard: a firmware with the flash's chip select
        # on another pin.  No firmware here has an app that does it, so the
        # window gets the machine's word on its bus as the link gives it,
        # with four such bytes more, and after that the true one again.  And
        # the header's with pin 5 as SPI1's clock, which a script has it as
        # for less time than the window looks at the header in.
        pins = ("/machine", "header")
        idle = w.props.get(pins)
        busy, n5 = re.subn(r"(?<!\S)5:PB3:[^:\s]+:([^:\s]+):[^:\s]+:(\S+)",
                           r"5:PB3:af5:\1:x:\2", idle or "")
        tips = [v.pin_tips.get(5, "")]
        if n5:
            w.on_prop(*pins, busy)
            tips.append(v.pin_tips.get(5, ""))
            w.on_prop(*pins, idle)
            tips.append(v.pin_tips.get(5, ""))
        key = ("/machine", "header-spi")
        true = w.props.get(key)
        word = ("the firmware clocked 4 bytes there, none with a device "
                "selected; the last at 1 MHz, mode 3, MSB first, 8 bits")
        had = self.console().count("(SPI1): the firmware clocked")
        menu()
        made, n = re.subn(
            r"^(\w+:\d+:\d+:\d+):(\d+):(\d+):\S+", lambda m:
            f"{m[1]}:{m[2]}:{int(m[3]) + 4}:1000000/3/msb/8", true or "")
        was = fe_header.parse_spi(true) if n else {"heard": 0, "unheard": 0}
        all_ = (f"the firmware clocked {was['heard'] + was['unheard'] + 4} "
                "bytes there, " + (f"{was['heard']} of them" if was["heard"]
                                   else "none") + " with a device selected; "
                "the last at 1 MHz, mode 3, MSB first, 8 bits")
        if n:
            w.on_prop(*key, made)
        told = self.until(
            lambda: f"Pins 2, 3 and 5 (SPI1): {word}" in self.console(), 5)
        open_ = lines()[0]              # the menu was open meanwhile
        if n:
            w.on_prop(*key, true)
        self.spin(1.5)
        bus, said = menu()[0], self.launcher("spi").stdout
        check("spi: four bytes with no device selected (the machine's word "
              "on the bus, given to the window as the link gives it): one "
              "line in the console that says so, and the GPIO menu's line, "
              "in a menu that was open; the bus as it is: as `spi` has it "
              "again, and no line more.  Pin 5 as the bus's clock (the "
              "header's word, given so too): the pin's line under the "
              "pointer says SPI1's SCK for as long",
              n5 == 1 and len(tips) == 3 and "SPI1's" not in tips[0] + tips[2]
              and tips[1].startswith("Pin 5, B3 (PB3): SPI1's SCK; "
                                     "alternate function 5")
              and n == 1 and told and open_ == "Pins 2, 3 and 5 (SPI1): w25q32 "
              f"with its chip select on pin 4; {all_}" and open_ != bus
              and bus.split(": ", 1)[1] in said
              and self.console().count("(SPI1): the firmware clocked")
              == had + 1,
              f"{true!r} -> {made!r}\nopen {open_}\nafter {bus}\n"
              f"pin 5: {tips}\n{self.console()[-400:]}")

    def pwm(self):
        """A PWM on a pin of the header, which a script in the firmware's
        JavaScript makes: the pin is lit for the part of its width that it
        is high for, the line under the pointer says what it is, and the
        console when it goes on and off"""
        w, v = self.win, self.win.view
        js = self.out / "fe-gui-pwm.js"
        js.write_text(PWM_JS)
        self.launcher("push", str(js), "/ext/fe-gui-pwm.js")
        script = subprocess.Popen(
            [sys.executable, str(w.launcher), "-d", w.name, "cli", "--timeout",
             "90", "js /ext/fe-gui-pwm.js"], stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)

        def duty(part):
            return (v.header.get(2) == ("pwm", None)
                    and abs(v.header_duty.get(2, -1) - part) < 1e-6)

        def hold(level):
            w.hold_pin("PA6", level)
            self.settle()

        def ended():
            try:
                return script.communicate(timeout=60)[0]
            except subprocess.TimeoutExpired:
                script.kill()
                return "(still running) " + script.communicate()[0]

        on = self.until(lambda: duty(0.25), 30)
        said = self.launcher("gpio", "PA7").stdout.strip()
        if not on and said != "pwm":
            hold("high")
            self.spin(1.0)
            hold("low")
            text = " ".join(ended().split())[:300]
            hold("free")
            self.launcher("rm", "/ext/fe-gui-pwm.js")
            print("  SKIP  pwm: this firmware's JavaScript made none: "
                  + text, flush=True)
            return
        self.spin(0.3)
        self.shot("pwm")
        tip = v.pin_tips.get(2, "")
        w.fill_gpio_menu()              # as the menu bar's GPIO menu opens
        line = w.gpio_items[2]
        line = line.title() if isinstance(line, QtWidgets.QMenu) else ""
        what = "PWM from TIM1: 1 kHz, high 25 % of the time"
        check("pwm: a script's PWM on pin 2 (1 kHz, high for a quarter of "
              "the time): the pin is lit for a quarter of its width, and the "
              "line under the pointer, the GPIO menu and the console say "
              "what it is",
              on and tip.startswith("Pin 2, A7 (PA7): alternate function 1, "
                                    + what)
              and what in line and f"Pin 2: {what}" in self.console(),
              f"{v.header.get(2)} {v.header_duty}; gpio PA7: {said!r}; "
              f"{tip!r}; {line!r}")

        hold("high")
        other = self.until(lambda: duty(0.75), 15)
        tip = v.pin_tips.get(2, "")
        check("pwm: other numbers from the script (2 kHz, 75 %): the pin and "
              "its line have them; the console has no line for that, as the "
              "PWM was on before",
              other and "PWM from TIM1: 2 kHz, high 75 % of the time" in tip
              and self.console().count("Pin 2: PWM from TIM1") == 1,
              f"{v.header.get(2)} {v.header_duty}; {tip!r}")

        hold("low")
        off = self.until(lambda: v.header.get(2) == ("floating", None), 15)
        text = ended()
        tip = v.pin_tips.get(2, "")
        check("pwm: the script stops it: the pin is dark, as one that "
              "floats, and the console says that the PWM is off",
              off and not v.header_duty and "PWM" not in tip
              and "Pin 2: the PWM from TIM1 is off" in self.console()
              and "end" in text.split(),
              f"{v.header.get(2)} {v.header_duty}; {tip!r}; {text!r}")
        hold("free")
        self.launcher("rm", "/ext/fe-gui-pwm.js")

    def pin_record(self):
        """The record of what the pins do, from the GPIO menu (the file is
        given here: the menu's line asks for one)"""
        w = self.win
        path = self.out / "window-pins.vcd"
        name = path.name

        def count():
            return w.pin_record[0] if w.pin_record else -1

        w.fill_gpio_menu()              # as the menu bar's GPIO menu opens
        offered = (w.a_gpio_record.text() == "Record what the pins do…"
                   and w.a_gpio_record.isEnabled()
                   and not w.pins_label.text())
        w.start_pin_record(str(path))
        began = self.until(lambda: w.pin_record is not None, 15)
        self.settle()
        w.fill_gpio_menu()
        line, label = w.a_gpio_record.text(), w.pins_label.text()
        check("pin record: GPIO > Record what the pins do: it runs, and the "
              "status bar and the menu say so",
              offered and began and w.pin_record[2] == str(path)
              and label.startswith(f"● {name}: ")
              and line.startswith(f"End the record ({name}, ")
              and "every change of a level" in self.console(),
              f"offered {offered}; {w.pin_record}; {label!r}; {line!r}")

        n = count()
        w.hold_pin("PA7", "high")
        self.until(lambda: count() > n, 15)
        self.settle()
        w.hold_pin("PA7", "free")
        two = self.until(lambda: count() >= n + 2, 15)
        self.settle()
        self.shot("pin-record")
        label = w.pins_label.text()
        check("pin record: pin 2 held high and let go of: two changes more, "
              "counted in the status bar",
              two and label == f"● {name}: {count()} changes",
              f"{n} before, {count()} after; {label!r}")

        w.fill_gpio_menu()
        w.a_gpio_record.trigger()       # End the record
        ended = self.until(lambda: w.pin_record is None, 15)
        self.settle()
        try:
            got = [level for _, pin, level, _ in
                   fe_header.read_record(path)[1] if pin == "PA7"]
        except (OSError, ValueError) as e:
            got = str(e)
        w.fill_gpio_menu()
        check("pin record: End the record: the file has what pin 2 did, and "
              "nothing says that a record runs",
              ended and got == ["high", "floating"]
              and not w.pins_label.text()
              and w.a_gpio_record.text() == "Record what the pins do…"
              and f"the record is {path}" in self.console(),
              f"ended {ended}; PA7: {got}; {w.pins_label.text()!r}")

    def blink(self):
        """The LED chip's engines, as the window gets them: NFC > Read blinks
        the LED while it looks for a card (a loop on engine 2)"""
        w, v = self.win, self.win.view
        w.command("app", "NFC")
        self.settle()
        self.spin(1.5)
        self.leds.clear()
        self.click("ok")                # its first item: Read
        came = self.until(lambda: len(self.leds) >= 10, 12)
        last = self.leds[-6:]
        self.shot("blink")
        said = self.launcher("status", "--json")
        try:
            lights = json.loads(said.stdout)["lights"]
        except (ValueError, KeyError):
            lights = {}
        # Back ends the reading, as a person would end it.  (Asked to close
        # while it reads, this app fails a check of its own on the way out,
        # nfc_scanner_free's, and the firmware starts again: app_check.py.)
        self.click("back")
        self.spin(1.5)
        n = len(self.leds)
        self.spin(1.0)
        still = n > 0 and len(self.leds) == n and v.led == self.leds[-1]
        w.close_app()
        self.settle()
        self.spin(1.0)
        colours = {c for c in last if c}
        check("led: it blinks while NFC > Read looks for a card: flashes of "
              f"one colour ({', '.join(f'{c:06x}' for c in colours)}) with "
              "dark between; `status` says that it blinks, in which colour; "
              "Back ends it",
              came and len(colours) == 1 and last.count(0) == 3
              and lights.get("blinking") is True
              and lights.get("led") == f"{max(colours):06x}" and still)

    def console(self):
        return self.win.log.text.toPlainText()

    def crash(self):
        """A crash under the window: NFC, asked to close while it reads, fails
        a check of its own (app_check.py).  The console says so.  A release
        build starts again; a debug build halts, which the console says too,
        and Reboot starts it.

        The firmware's own report can be missing: what a crash prints is
        dropped while another thread is in the middle of a log line (its log
        stops there, and goes on with the next start).  Then the console has
        what the launcher saw, the firmware starting again under its request,
        with the crash from the firmware's fault register."""
        w = self.win
        begun = len(self.console())
        heard = []                      # the emulator's own word of a halt
        w.link.halted.connect(heard.append)
        w.take_away("nfc")              # nothing to find: it goes on reading
        self.settle()
        w.command("app", "NFC")
        self.settle()
        self.spin(1.5)
        self.click("ok")                # its first item: Read
        self.spin(3)
        mark = len(self.console())
        new = lambda: self.console()[mark:]
        w.close_app()
        self.settle()
        if '"NFC" closed' in new():
            print("  SKIP  crash: this firmware's NFC app closes in the "
                  "middle of a read", flush=True)
            return
        again = "when the app was asked to close, and has started again"

        def lose():
            """the window loses the emulator, and finds it again: what one
            opened now would find"""
            w.link.sock.abort()

        self.until(lambda: "The firmware crashed: " in new() or again in new(),
                   20)
        said = "The firmware crashed: " in new()
        self.spin(fe_gui.HALT_ASK_MS / 1000 + 0.5)
        self.settle()
        try:
            halted = json.loads(self.launcher("status", "--json").stdout)["halted"]
        except (ValueError, KeyError):
            halted = None
        if halted:
            told = self.until(lambda: "The firmware has halted: a debug "
                                      "build" in new(), 10)
            shown = (w.state_label.text() == "halted"
                     and w.view.paused == "halted"
                     and w.windowTitle().endswith("(halted)"))
            self.shot("halted")
            self.until(lambda: heard, 5)
            check("crash: the emulator tells the window of the halt itself, "
                  "with the place `status` has: a halt that left nothing in "
                  "the log is shown too",
                  heard == [int(halted["at"], 16)],
                  f"heard: {[f'{at:#010x}' for at in heard]}, status: "
                  f"{halted['at']}")

            def finds(how):
                """the window forgets the halt; after how(): it knows again"""
                self.until(lambda: not w.halt_asked, 10)
                w.halt_timer.stop()
                w.set_halted(False)
                how()
                return self.until(
                    lambda: w.state_label.text() == "halted", 10)
            found = finds(lose)
            failed = finds(lambda: w.command("ls", "/ext"))
            check("crash: a window that finds the firmware halted already "
                  "shows that, with no [CRASH] going by; so does one whose "
                  "command gets no answer from it", found and failed,
                  f"found: {found}, after a command: {failed}")
            w.reboot()
            back = self.booted() and self.until(
                lambda: w.state_label.text() == "running"
                and not w.view.paused, 10)
            check("crash: an app takes the firmware with it: the console "
                  "says that it crashed, and that this build has halted, "
                  "which the window shows as its state; Reboot starts it "
                  "again, and the window says running", said and told
                  and shown and back,
                  f"crash said: {said}, halt said: {told}, shown: {shown}, "
                  f"back: {back}; the console since:\n{new()}")
        else:
            back = self.booted()
            self.until(lambda: again in new(), 20)
            if not said:
                print("  note  the firmware's log has no report of this "
                      "crash: it came in the middle of another thread's log "
                      "line", flush=True)
            check("crash: an app takes the firmware with it: the console "
                  "says so, with what the crash said (from the firmware's "
                  "log, or from its fault register); it starts again by "
                  "itself",
                  re.search(r"the firmware crashed \(.*furi_check failed\) "
                            + again, new())
                  and back and "has halted" not in new()
                  and w.state_label.text() == "running",
                  f"crash said: {said}, back: {back}, state: "
                  f"{w.state_label.text()!r}; the console in this step:\n"
                  f"{self.console()[begun:]}")
            # the crash is on the Flipper's screen now, which the window says
            # where the log did not (once), and when it finds the emulator
            screen = ('The firmware started again after a crash: its screen '
                      'says "furi_check failed"')
            idle = lambda: not (w.halt_asked or w.halt_timer.isActive())
            self.spin(fe_gui.HALT_ASK_MS / 1000 + 0.5)
            self.until(idle, 10)
            lost = screen in new()
            then = len(self.console())
            lose()
            found = self.until(lambda: screen in self.console()[then:], 10)
            # started again with that screen still up: nothing about a crash
            # in the log since its last start, as after a report that was lost
            self.until(idle, 10)
            self.settle()
            then = len(self.console())
            w.reboot()
            unsaid = self.booted() and self.until(
                lambda: screen in self.console()[then:], 10)
            check("crash: a window that finds the firmware after its crash "
                  "says what the Flipper's screen says of it, and so does "
                  "this one when the firmware starts again with no report of "
                  "a crash in its log (by itself only then)",
                  found and unsaid and lost == (not said),
                  f"found: {found}, after a start without a report: {unsaid}, "
                  f"said by itself: {lost}, the log had the report: {said}; "
                  f"the console in this step:\n{self.console()[begun:]}")
        self.settle()
        self.spin(2)

    def update(self):
        """Device > Update with a package, with the package it runs: the
        firmware's own updater installs it, and the window waits for it"""
        w = self.win
        if not self.firmware.endswith(fe_gui.PACKAGES):
            print("  SKIP  update: the firmware is no update package",
                  flush=True)
            return

        def usb():
            return w.props.get(("/machine/soc/usb", "device")) or ""

        def said(mark):
            return self.console()[mark:]

        # An updater leaves the firmware's log level as it likes, as on a
        # real Flipper (the official one: default; Momentum's and Unleashed's:
        # none, and then the log has nothing of the next start)
        level = lambda: w.props.get(("/machine", "log-level"))
        was = level()

        def set_back():
            """the level as it was, with the firmware log's button"""
            self.seen = self.boots()
            w.fwlog.level_actions[was].trigger()
            again = (self.until(lambda: "CDC ACM" not in usb(), 20)
                     and self.until(lambda: "CDC ACM" in usb(), self.timeout))
            self.settle()
            return again and self.booted()

        # once with the wait given up: the updater goes on by itself
        self.settle()
        mark = len(self.console())
        w.update_with(self.firmware)
        self.spin(0.2)
        waits = w.state_label.text() == "updating" \
            and "Stop waiting" in w.a_update.text() \
            and not w.a_reboot.isEnabled() and not w.a_install.isEnabled()
        started = self.until(lambda: "updater started" in said(mark)
                             or not w.updating, 120)
        self.shot("updating")
        w.a_update.trigger()
        left = self.until(lambda: not w.updating, 10)
        self.spin(0.5)
        check("update: the window waits for the updater, and Stop waiting "
              "leaves it to itself",
              waits and started and left and "no longer waiting" in said(mark)
              and w.a_reboot.isEnabled()
              and "Update with" in w.a_update_again.text()
              and w.state_label.text() != "updating", said(mark)[-600:])
        gone = self.until(lambda: "CDC ACM" not in usb(), 60)
        # (RogueMaster's updater takes seven and a half minutes)
        back = gone and self.until(lambda: "CDC ACM" in usb(), 900)
        self.settle()
        self.spin(2)
        if level() != was:              # (nobody waited to say so)
            set_back()
            self.spin(2)
        self.seen = self.boots()

        # and once to its end, with F6: the same package again
        mark = len(self.console())
        w.a_update_again.trigger()
        self.spin(0.2)
        began = bool(w.updating)
        done = began and self.until(lambda: not w.updating, 900)
        self.settle()
        self.spin(1)
        text = self.uart.read_text(errors="replace")
        check("update: Device > Update with a package: the firmware's own "
              "updater installed it, and the Flipper is back (a crash that "
              "its screen said before is no news after it)",
              back and done and "updated in" in said(mark)
              and "updated in" in w.statusBar().currentMessage()
              and "started again after a crash" not in said(mark)
              and w.state == "on" and "CDC ACM" in usb()
              and self.boots() > self.seen and w.a_reboot.isEnabled()
              and not CRASH.search(text.split("Startup complete")[-1]),
              said(mark)[-600:])
        self.seen = self.boots()
        self.shot("updated")

        left = level()
        if left == was:
            return
        told = "left the firmware's log level" in said(mark) \
            and '"Firmware level" button' in said(mark) \
            and left in w.fwlog.level_button.text()
        again = set_back()
        check(f"update: its updater left the firmware's log level at {left}: "
              "the window says so and where it is set, and the firmware log "
              f"sets it to {was} again",
              told and again and level() == was,
              f"told: {told}, started again: {again}, the level: {level()}, "
              f"the button: {w.fwlog.level_button.text()!r}; the console:\n"
              f"{said(mark)[-600:]}")
        self.spin(2)

    def drop(self, path):
        """a file dragged from a file manager and let go over the window"""
        mime = QtCore.QMimeData()
        mime.setUrls([QtCore.QUrl.fromLocalFile(str(path))])
        self.win.dropEvent(QtGui.QDropEvent(
            QtCore.QPointF(40, 40), Qt.CopyAction, mime, Qt.LeftButton,
            Qt.NoModifier))

    def files_and_apps(self):
        """Files to the card and an app started, as the menus and a drop do
        it.  The app is one the firmware brought along on its card."""
        w, v = self.win, self.win.view
        note = self.out / "fe-gui note.txt"
        note.write_text("from the window\n")
        w.copy_to_card([str(note)], "/ext/fe-gui")
        self.settle()
        check("files: a file copied to the card (microSD card > Copy files "
              "to it)",
              f"-> /ext/fe-gui/{note.name}" in self.console()
              and "1 copied (through the firmware)" in self.console())

        w.command("pull", "/ext/apps/GPIO/gpio.fap", self.out)
        self.settle()
        fap = self.out / "gpio.fap"
        if not check("apps: an app from its card to work with", fap.is_file()):
            return
        runs = lambda: re.findall(r'"(.+)" runs', self.console())
        before = v.frame
        self.drop(fap)
        ran = self.until(lambda: len(runs()) >= 1, 90)
        self.settle()
        check("apps: a .fap dropped on the window is put on the card and "
              "started",
              ran and v.frame != before and w.a_app_again.isEnabled()
              and w.a_app_again.text() == "Install and start gpio.fap again")
        self.shot("app")
        w.a_app_again.trigger()         # F5
        again = self.until(lambda: len(runs()) >= 2, 90)
        self.settle()
        check("apps: F5 closes it and starts the same file again",
              again and f'"{runs()[0]}" closed' in self.console())
        # Apps > Start it again when its file changes: a build puts a new
        # file in the old one's place, and the window does the F5
        w.a_app_watch.trigger()
        self.spin(1.5)
        calm = len(runs()) == 2 and w.watch_timer.isActive()
        built = fap.with_suffix(".new")
        built.write_bytes(fap.read_bytes())
        os.replace(built, fap)
        seen = self.until(lambda: len(runs()) >= 3, 90)
        self.settle()
        w.a_app_watch.trigger()
        check("apps: with Apps > Start it again when its file changes, a new "
              "build of the file is installed and started by itself",
              calm and seen and "gpio.fap changed" in self.console()
              and not w.watch_timer.isActive(),
              f"nothing before the build: {calm}, started: {seen}")
        w.close_app()
        w.command("app")
        self.settle()
        check("apps: closed from the Apps menu: no app runs",
              ran and self.console().count(f'"{runs()[0]}" closed') >= 2
              and "no app runs" in self.console())
        self.spin(1.0)

        # The same app, as if it were built for the next API.  The official
        # firmware's loader refuses it; Momentum and Unleashed ask on the
        # Flipper's screen, and the launcher waits for the answer
        other = self.out / "other-api.fap"
        other.write_bytes(other_api(fap.read_bytes()))
        had = len(self.console())
        new = lambda: self.console()[had:]
        self.drop(other)
        self.until(lambda: "did not start it" in new()
                   or "no answer yet" in new(), 90)
        if "no answer yet" in new():
            self.hold(Qt.Key_Right, 150)            # Continue
            ok = self.until(lambda: '" runs' in new(), 60)
            self.settle()
            check("apps: one built for another API: the Flipper asks on its "
                  "screen, the console says so, and Continue starts it",
                  ok and "this firmware's API is" in new())
            w.close_app()
            self.settle()
        else:
            self.settle()
            check("apps: one built for another API: the loader's refusal is "
                  "in the console",
                  "did not start it" in new()
                  and "this firmware's API is" in new())
        self.spin(1.0)

    # ---- the checks

    def checks(self):
        w, v = self.win, self.win.view
        v.setFocus()
        self.backlights, self.leds = [], []     # as the link reports them
        w.link.backlight.connect(self.backlights.append)
        w.link.led.connect(self.leds.append)
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
        self.recording()
        self.pausing()
        self.snapshot_save()

        # what the firmware logs: a click on the GPIO pins, where a real one
        # has its UART
        level = lambda: w.props.get(("/machine", "log-level"))
        gpio = fe_gui_view.ZONES["gpio"].boundingRect().center()
        QTest.mouseClick(v, Qt.LeftButton, Qt.NoModifier,
                         QPoint(round(gpio.x() * v.k), round(gpio.y() * v.k)))
        self.spin(0.6)
        check("firmware log: a click on the GPIO pins opens it, with what "
              "the firmware logged",
              w.front.get(w.fwlog) and "[I][" in w.fwlog.shown_text()
              and level() == "default"
              and w.fwlog.level_button.text() == "Firmware level: default (info)")
        # Its level, which the firmware reads when it starts.  Errors only is
        # what every build shows: debug says nothing more on one that is built
        # without its debug lines (Unleashed's releases).  No 'Startup
        # complete' at that level: the computer seeing it again is the sign
        usb = lambda: w.props.get(("/machine/soc/usb", "device"), "")
        had = len(w.fwlog.shown_text())
        self.backlights.clear()
        w.fwlog.level_actions["error"].trigger()
        again = (self.until(lambda: not usb().startswith("0483:"), 20)
                 and self.until(lambda: usb().startswith("0483:"), self.timeout))
        self.settle()
        self.spin(2)
        text, mark = w.fwlog.shown_text(), "it started again"
        new = text.split(mark)[-1] if mark in text[had:] else text[had:]
        check("firmware log: its level set to error: it starts again, and "
              "logs errors only",
              again and level() == "error"
              and "[I][" not in new and "[W][" not in new)
        self.shot("fwlog")
        # The firmware started again with the window looking: it fades the
        # backlight in (a ramp on engine 1 of the LED chip), 16 ms a look.
        # The last rise is the one to count: Momentum lights the screen early,
        # and begins again from dark when its notification service starts
        lit = self.backlights[:] or [v.backlight]
        i = len(lit) - 1
        while i > 0 and lit[i - 1] < lit[i]:
            i -= 1
        rise = lit[i:]
        check("backlight: the firmware fades it in when it starts: "
              f"{len(rise)} values on the way from {rise[0]} to {rise[-1]}",
              len(rise) >= 5 and rise[0] < 100 and rise[-1] == v.backlight)
        self.click("back")
        self.spin(1.0)

        self.files_and_apps()
        self.blink()
        self.pins()
        self.pin_record()
        self.pwm()
        self.i2c_bus()
        self.i2c_record()
        self.spi_bus()

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
        w.fwlog.level_actions["debug"].trigger()        # ... while it is off
        w.copy_to_card([str(self.out / "fe-gui note.txt")], "/ext/fe-gui-off")
        self.settle()
        check("files: copied while it is off: into the card's image",
              "-> /ext/fe-gui-off/fe-gui note.txt" in self.console()
              and "1 copied (in the card's image)" in self.console())

        self.hold(Qt.Key_Escape, fe_gui.POWER_ON_MS + 300)
        on = self.until(lambda: w.state == "on", 20) and self.booted()
        self.settle()
        self.spin(1.5)
        check("power: Back held switches it on; the tag is held to it again",
              on and "NTAG215" in self.tile("nfc"))
        check("firmware log: a level chosen while it is off is the one it "
              "starts with", level() == "debug")

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
        QTest.keyPress(v, Qt.Key_Left)
        QTest.keyPress(v, Qt.Key_Escape)
        self.spin(fe_gui.RESET_MS / 1000 + 0.3)
        blank = v.blank
        QTest.keyRelease(v, Qt.Key_Escape)
        self.spin(0.05)
        QTest.keyRelease(v, Qt.Key_Left)
        check("reset: Left + Back for 3 s reboot it", blank and self.booted())
        self.spin(2)
        self.shot("rebooted")
        text = self.uart.read_text(errors="replace")
        check("no crash in log", not CRASH.search(text))

        self.crash()

        QtWidgets.QMessageBox.question = \
            lambda *a, **k: QtWidgets.QMessageBox.Yes
        self.update()

        # Device > Install firmware: off, flashed, on again
        w.install(self.firmware)
        went = self.until(lambda: w.state in ("off", "installing"), 20)
        back = went and self.until(lambda: w.state == "on", 120) and self.booted()
        check("install: Device > Install firmware flashed it and started it",
              back and "flashed" in w.log.text.toPlainText())


class LoadedDriver(Driver):
    """Phase two: the window on the machine of phase one's snapshot, loaded
    standing still (`snapshot load gui --paused`)"""

    def checks(self):
        w, v = self.win, self.win.view
        v.setFocus()
        on = self.until(lambda: w.state == "on" and v.frame
                        and w.stopped == "paused", 30)
        self.settle()
        self.spin(1.0)
        check("load: the window on the loaded Flipper, standing still; its "
              "console names the snapshot",
              on and w.windowTitle().endswith("(paused)")
              and "Loaded from the snapshot gui (saved" in self.console())
        check("load: the screen is the one of the save, to the pixel",
              v.frame is not None and v.frame == SAVED.get("frame"))
        self.shot("loaded")
        check("load: the RFID tag that was held at the save is held again, "
              "and the bench says so", "EM4100" in self.tile("rfid"))
        r = self.launcher("status", "--json")
        try:
            st = json.loads(r.stdout)
        except ValueError:
            st = {}
        r2 = self.launcher("status")
        check("load: `status` names the snapshot that the run began at",
              (st.get("snapshot") or {}).get("name") == "gui"
              and (st.get("snapshot") or {}).get("saved")
              and st.get("stopped") == "paused"
              and "the run began at gui" in r2.stdout,
              r.stdout[-600:] + r2.stdout[-600:])
        frames = []
        w.link.frame.connect(frames.append)
        w.a_pause.trigger()             # it was paused: once more, it goes on
        went = self.until(lambda: not w.stopped, 10)
        self.click("back")              # out of the menu
        moved = self.until(lambda: frames, 5)
        self.settle()
        made = self.launcher("cli", "storage mkdir /ext/fe-gui-loaded")
        seen = self.launcher("cli", "storage list /ext")
        check("load: Device > Pause lets it go on: the screen moves, and it "
              "writes to its card",
              went and moved and w.state_label.text() == "running"
              and made.returncode == 0 and "fe-gui-loaded" in seen.stdout,
              made.stdout[-300:] + made.stderr[-300:] + seen.stdout[-600:])


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("firmware")
    ap.add_argument("--timeout", type=float, default=60)
    ap.add_argument("--out", help="keep the pictures here")
    ap.add_argument("--keep", action="store_true", help="keep the device directory")
    args = ap.parse_args()

    name = f"guicheck-{int(time.time())}-{os.getpid()}"
    devdir = ROOT / "devices" / name
    out = pathlib.Path(args.out) if args.out else pathlib.Path(
        tempfile.mkdtemp(prefix="fe-gui-"))
    out.mkdir(parents=True, exist_ok=True)
    devdir.mkdir(parents=True, exist_ok=True)
    uart = devdir / "uart.log"
    os.environ["FLIPPEREMU_GUI_CONFIG"] = str(out / "gui.ini")
    fe_gui.run = Driver(uart, out, args.timeout, str(pathlib.Path(args.firmware).resolve()))
    # (the launcher is a file with no .py: its loader by name.  Python 3.15
    # has no load_module() any more: a module from the spec, then run)
    loader = importlib.machinery.SourceFileLoader(
        "flipperemu_launcher", str(ROOT / "flipperemu"))
    launcher = importlib.util.module_from_spec(
        importlib.util.spec_from_loader(loader.name, loader))
    sys.modules[loader.name] = launcher
    loader.exec_module(launcher)
    sys.argv = ["flipperemu", "-d", name, "run", "-f", args.firmware,
                "--audio", "none", "--serial", f"file:{uart}",
                "--i2c", "tmp105@0x48,temperature=23.5", "--i2c", "eeprom@0x50",
                "--spi", "flash"]
    try:
        code = launcher.main()
        check("exit status of the run: 0", code == 0)
        if SAVED:                       # phase two: its snapshot, loaded
            fe_gui.run = LoadedDriver(uart, out, args.timeout,
                                      str(pathlib.Path(args.firmware).resolve()))
            sys.argv = ["flipperemu", "-d", name, "snapshot", "load", "gui",
                        "--paused", "--audio", "none"]
            code = launcher.main()
            check("exit status of the load: 0", code == 0)
    finally:
        if not args.keep:
            shutil.rmtree(devdir, ignore_errors=True)
    print(f"  pictures: {out}")
    if args.keep:
        print(f"  device:   {devdir}")
    return 0 if all(ok for _, ok in results) else 1


if __name__ == "__main__":
    sys.exit(main())
