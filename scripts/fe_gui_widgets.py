"""The parts of the window around the Flipper (fe_gui.py).

Runner runs the launcher's commands (`flipperemu -d NAME nfc card.nfc` ...)
one at a time, as a terminal would, and Background the ones that run until
they are stopped (a real radio, a card on the NFC link).  Tile is one place
on the bench under the Flipper, the dialogs make up new cards, tags, keys and
signals, RemoteWindow is an infrared remote made of an .ir file, LogDock is
the console (what happened, with a line for the Flipper's CLI) and
FirmwareLogDock is what the firmware itself logs, as it logs it.

Copyright (c) 2026, Kara Zajac
SPDX-License-Identifier: BSD-3-Clause
"""
import collections
import html
import os
import random
import re
import signal
import sys
import time

from PySide6 import QtCore, QtGui, QtWidgets
from PySide6.QtCore import Qt

import fe_log

COMMAND_MS = 90_000                 # no command of the launcher takes this long
COPY_MS = 30 * 60_000               # ... but one that copies files to the card
LAST_LINE_MS = 1500                 # a command's newest line: shown after this


def strip_name(line, name):
    prefix = f"[{name}] "
    return line[len(prefix):] if line.startswith(prefix) else line


def launcher_process(parent, launcher, name, args):
    p = QtCore.QProcess(parent)
    p.setProcessChannelMode(QtCore.QProcess.MergedChannels)
    env = QtCore.QProcessEnvironment.systemEnvironment()
    env.insert("PYTHONUNBUFFERED", "1")
    p.setProcessEnvironment(env)
    p.setProgram(sys.executable)
    p.setArguments([str(launcher), "-d", name, *map(str, args)])
    return p


class Runner(QtCore.QObject):
    """The launcher's commands, one after the other: the emulator's monitor
    takes one client at a time."""
    output = QtCore.Signal(str)         # a line a command printed
    failed = QtCore.Signal(str)         # what a command that failed said last
    changed = QtCore.Signal()

    def __init__(self, launcher, name, parent=None):
        super().__init__(parent)
        self.launcher, self.name = str(launcher), name
        self.queue = []
        self.proc = None
        self.current = None
        self.watchdog = QtCore.QTimer(self)
        self.watchdog.setSingleShot(True)
        self.watchdog.timeout.connect(self._too_long)
        self.lines, self.shown, self.quiet = [], 0, False
        self.newest = QtCore.QTimer(self)
        self.newest.setSingleShot(True)
        self.newest.setInterval(LAST_LINE_MS)
        self.newest.timeout.connect(self._show)

    def run(self, *args, then=None, quiet=False, limit=None):
        """then(code, text) once it is done; quiet: its output only if it
        fails; limit: it may take this many ms (COMMAND_MS).  What it prints
        is shown line by line as it comes."""
        self.queue.append(([str(a) for a in args], then, quiet,
                           limit or COMMAND_MS))
        if not self.proc:
            self._next()

    def busy(self):
        return self.proc is not None

    def _next(self):
        if not self.queue:
            self.current = None
            self.changed.emit()
            return
        args, then, quiet, limit = self.queue.pop(0)
        p = launcher_process(self, self.launcher, self.name, args)
        p.finished.connect(lambda code, status: self._done(p, args, then, quiet,
                                                           code, status))
        p.errorOccurred.connect(lambda err: self._error(p, args, then, err))
        p.readyRead.connect(lambda: self._read(p, quiet))
        self.proc, self.current, self.limit = p, args, limit
        self.lines, self.partial, self.shown, self.quiet = [], b"", 0, quiet
        self.watchdog.start(limit)
        p.start()
        self.changed.emit()

    def _show(self, keep=0):
        """Show its lines, but for the `keep` newest"""
        while not self.quiet and self.shown < len(self.lines) - keep:
            self.output.emit(self.lines[self.shown])
            self.shown += 1

    def _read(self, p, quiet, last=False):
        """Its lines as they come.  The newest is held back for a moment: of
        a command that fails, the last line is what went wrong, and that is
        said in another way.  A command that waits (for an answer on the
        Flipper's screen) has said why by then."""
        if p is not self.proc:
            return
        self.partial += bytes(p.readAll().data())
        *whole, self.partial = self.partial.split(b"\n")
        if last and self.partial:
            whole.append(self.partial)
            self.partial = b""
        for raw in whole:
            line = strip_name(raw.decode(errors="replace").rstrip(), self.name)
            if line.strip():
                self.lines.append(line)
        self._show(keep=1)
        if not last and self.shown < len(self.lines):
            self.newest.start()

    def _done(self, p, args, then, quiet, code, status):
        if p is not self.proc:
            return
        self.watchdog.stop()
        self.newest.stop()
        self._read(p, quiet, last=True)
        lines = self.lines
        if status != QtCore.QProcess.NormalExit and not code:
            code = -1
        if not quiet or code:
            for line in (lines[:-1] if code else lines)[self.shown:]:
                self.output.emit(line)
        if code:                        # its last words: what went wrong
            self.failed.emit(lines[-1] if lines else
                             f"{args[0]}: failed (exit status {code})")
        self.proc = None
        p.deleteLater()
        if then:
            then(code, "\n".join(lines))
        self._next()

    def _error(self, p, args, then, err):
        if err == QtCore.QProcess.FailedToStart and p is self.proc:
            self.watchdog.stop()
            self.failed.emit(f"{args[0]}: could not start {sys.executable}")
            self.proc = None
            if then:
                then(-1, "")
            self._next()

    def _too_long(self):
        if self.proc:
            self.output.emit(f"{self.current[0]}: no answer for "
                             f"{self.limit // 1000} s; stopped it")
            self.proc.kill()

    def stop(self):
        self.queue.clear()
        if self.proc:
            self.proc.kill()
            self.proc.waitForFinished(2000)


class Background(QtCore.QObject):
    """A launcher command that runs until it is stopped, as ^C stops it in a
    terminal: a real radio as the Sub-GHz antenna, a card on the NFC link."""
    output = QtCore.Signal(str)
    ended = QtCore.Signal(int)

    def __init__(self, launcher, name, args, label, parent=None):
        super().__init__(parent)
        self.name, self.label = name, label
        self.proc = launcher_process(self, launcher, name, args)
        self.proc.readyReadStandardOutput.connect(self._read)
        self.proc.finished.connect(self._finished)
        self.buf = b""

    def start(self):
        self.proc.start()

    def running(self):
        return self.proc.state() != QtCore.QProcess.NotRunning

    def _read(self):
        self.buf += bytes(self.proc.readAllStandardOutput().data())
        *lines, self.buf = self.buf.split(b"\n")
        for line in lines:
            text = strip_name(line.decode(errors="replace").rstrip(), self.name)
            if text.strip():
                self.output.emit(text)

    def _finished(self, code, status):
        self.buf += b"\n"
        self._read()
        self.ended.emit(code if status == QtCore.QProcess.NormalExit else -1)

    def stop(self, wait_ms=0):
        if not self.running():
            return
        pid = self.proc.processId()
        try:
            os.kill(pid, signal.SIGINT)
        except OSError:
            pass
        if wait_ms:
            if not self.proc.waitForFinished(wait_ms):
                self.proc.kill()
                self.proc.waitForFinished(1000)
        else:
            QtCore.QTimer.singleShot(2000, lambda: self.running()
                                     and self.proc.kill())


# ---------------------------------------------------------------- the bench

class Elided(QtWidgets.QLabel):
    """A line that ends in ... when it does not fit (all of it as a tip)"""

    def __init__(self, small=False, parent=None):
        super().__init__(parent)
        self.full = ""
        self.setSizePolicy(QtWidgets.QSizePolicy.Ignored,
                           QtWidgets.QSizePolicy.Preferred)
        self.setMinimumWidth(40)
        if small:
            font = self.font()
            font.setPointSizeF(font.pointSizeF() * 0.9)
            self.setFont(font)
            self.setForegroundRole(QtGui.QPalette.PlaceholderText)

    def set(self, text):
        self.full = text or ""
        self._elide()

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._elide()

    def _elide(self):
        shown = self.fontMetrics().elidedText(self.full, Qt.ElideRight,
                                              max(10, self.width()))
        super().setText(shown)
        self.setToolTip(self.full if shown != self.full else "")


class Tile(QtWidgets.QFrame):
    """One of the things on the bench: a title, what it is now, a button
    for what one does most, and a menu with the rest."""
    DOTS = {True: "#40a02b", "warn": "#df8e1d", False: "#9ca0b0"}

    def __init__(self, title, parent=None):
        super().__init__(parent)
        self.setFrameShape(QtWidgets.QFrame.StyledPanel)
        self.setFocusPolicy(Qt.NoFocus)
        grid = QtWidgets.QGridLayout(self)
        grid.setContentsMargins(8, 5, 5, 6)
        grid.setHorizontalSpacing(5)
        grid.setVerticalSpacing(1)
        self.dot = QtWidgets.QLabel("●")
        self.title = QtWidgets.QLabel(title)
        font = self.title.font()
        font.setBold(True)
        self.title.setFont(font)
        self.primary = QtWidgets.QToolButton()
        self.more = QtWidgets.QToolButton()
        self.more.setText("⋯")
        self.more.setToolTip("More")
        self.more.setPopupMode(QtWidgets.QToolButton.InstantPopup)
        self.more.setStyleSheet("QToolButton::menu-indicator { image: none; }")
        for b in (self.primary, self.more):
            b.setFocusPolicy(Qt.NoFocus)
            b.setAutoRaise(True)
        self.line1 = Elided()
        self.line2 = Elided(small=True)
        grid.addWidget(self.dot, 0, 0)
        grid.addWidget(self.title, 0, 1)
        grid.addWidget(self.primary, 0, 2)
        grid.addWidget(self.more, 0, 3)
        grid.addWidget(self.line1, 1, 0, 1, 4)
        grid.addWidget(self.line2, 2, 0, 1, 4)
        grid.setColumnStretch(1, 1)
        self.set_state("", "", False)
        self.primary.hide()

    def set_state(self, line1, line2="", active=None):
        self.line1.set(line1)
        self.line2.set(line2)
        if active is not None:
            self.dot.setStyleSheet(f"color: {self.DOTS[active]};")

    def set_primary(self, text, enabled=True, tip=""):
        self.primary.setText(text)
        self.primary.setEnabled(enabled)
        self.primary.setToolTip(tip)
        self.primary.setVisible(bool(text))

    def set_menu(self, menu):
        self.more.setMenu(menu)


# ---------------------------------------------------------------- dialogs

def hex_bytes(text):
    """b'...' for 'DE AD BE EF', 'deadbeef', 'de:ad:be:ef'; None: not hex"""
    t = "".join(text.replace(":", " ").replace("-", " ").split())
    if not t or len(t) % 2:
        return None
    try:
        return bytes.fromhex(t)
    except ValueError:
        return None


def spaced(data):
    return data.hex(" ").upper()


class Form(QtWidgets.QDialog):
    """A small dialog: fields in a form, a line that says what is wrong,
    OK and Cancel"""

    def __init__(self, parent, title, intro=None):
        super().__init__(parent)
        self.setWindowTitle(title)
        box = QtWidgets.QVBoxLayout(self)
        if intro:
            label = QtWidgets.QLabel(intro)
            label.setWordWrap(True)
            box.addWidget(label)
        self.form = QtWidgets.QFormLayout()
        box.addLayout(self.form)
        self.error = QtWidgets.QLabel()
        self.error.setStyleSheet("color: #d20f39;")
        self.error.setWordWrap(True)
        box.addWidget(self.error)
        self.buttons = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Cancel)
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        box.addWidget(self.buttons)
        self.checks = []

    def row(self, label, widget, extra=None):
        if extra is not None:
            line = QtWidgets.QHBoxLayout()
            line.addWidget(widget, 1)
            line.addWidget(extra)
            self.form.addRow(label, line)
        else:
            self.form.addRow(label, widget)
        return widget

    def combo(self, label, items, current=None):
        """items: [(text, data)]"""
        c = QtWidgets.QComboBox()
        for text, data in items:
            c.addItem(text, data)
        if current is not None:
            c.setCurrentIndex(max(0, c.findData(current)))
        c.currentIndexChanged.connect(self.validate)
        return self.row(label, c)

    def line(self, label, text="", placeholder="", extra=None):
        e = QtWidgets.QLineEdit(text)
        e.setPlaceholderText(placeholder)
        e.textChanged.connect(self.validate)
        return self.row(label, e, extra)

    def spin(self, label, lo, hi, value, suffix="", decimals=None):
        if decimals:
            s = QtWidgets.QDoubleSpinBox()
            s.setDecimals(decimals)
        else:
            s = QtWidgets.QSpinBox()
        s.setRange(lo, hi)
        s.setValue(value)
        s.setSuffix(suffix)
        s.valueChanged.connect(self.validate)
        return self.row(label, s)

    def tick(self, text, on=False):
        b = QtWidgets.QCheckBox(text)
        b.setChecked(on)
        return self.row("", b)

    def check(self, fn):
        """fn() -> what is wrong ('' when nothing is)"""
        self.checks.append(fn)

    def validate(self, *_):
        wrong = next((w for w in (fn() for fn in self.checks) if w), "")
        self.error.setText(wrong)
        self.buttons.button(QtWidgets.QDialogButtonBox.Ok).setEnabled(not wrong)
        return not wrong

    def ask(self):
        self.validate()
        return self.exec() == QtWidgets.QDialog.Accepted


def random_button(fill):
    b = QtWidgets.QPushButton("Random")
    b.setAutoDefault(False)
    b.clicked.connect(fill)
    return b


class NfcTagDialog(Form):
    KINDS = [("NTAG215 (504 bytes of user memory)", "ntag215", 7),
             ("NTAG213 (144 bytes)", "ntag213", 7),
             ("NTAG216 (888 bytes)", "ntag216", 7),
             ("MIFARE Classic 1K", "mfc1k", 4),
             ("MIFARE Classic 4K", "mfc4k", 4),
             ("MIFARE Classic Mini", "mfcmini", 4)]

    def __init__(self, parent):
        super().__init__(parent, "A new NFC tag",
                         "A factory-fresh tag, held to the Flipper. An NTAG can "
                         "carry a web address (NDEF); a MIFARE Classic has the "
                         "transport keys (FF FF FF FF FF FF) and empty blocks.")
        self.kind = self.combo("Kind", [(t, k) for t, k, _ in self.KINDS])
        self.uid = self.line("UID", "", "", random_button(self.shuffle))
        self.url = self.line("Web address", "", "optional: https://...")
        self.kind.currentIndexChanged.connect(self.shuffle)
        self.check(self.wrong)
        self.shuffle()

    def size(self):
        return self.KINDS[self.kind.currentIndex()][2]

    def shuffle(self, *_):
        n = self.size()
        uid = bytes([0x04]) + random.randbytes(6) if n == 7 else \
            bytes([random.choice((0x1A, 0x2B, 0x3C, 0x4D, 0x5E))]) + random.randbytes(3)
        self.uid.setText(spaced(uid))
        self.url.setEnabled(n == 7)

    def wrong(self):
        uid = hex_bytes(self.uid.text())
        if uid is None or len(uid) != self.size():
            return f"The UID is {self.size()} bytes, in hex."
        return ""

    def args(self):
        kind = self.kind.currentData()
        out = [kind, hex_bytes(self.uid.text()).hex().upper()]
        if self.size() == 7 and self.url.text().strip():
            out.append(self.url.text().strip())
        return out


class RfidTagDialog(Form):
    def __init__(self, parent):
        import fe_rfid
        super().__init__(parent, "A new 125 kHz tag",
                         "A tag of one of the protocols the Flipper reads, with "
                         "this data, held to it.")
        self.protocols = fe_rfid.PROTOCOLS
        self.proto = self.combo("Protocol", [
            (f"{name}  ({size} bytes, {mod.upper()})", name)
            for name, (_, size, mod) in self.protocols.items()])
        self.data = self.line("Data", "", "", random_button(self.shuffle))
        self.proto.currentIndexChanged.connect(self.shuffle)
        self.check(self.wrong)
        self.shuffle()

    def size(self):
        return self.protocols[self.proto.currentData()][1]

    def shuffle(self, *_):
        self.data.setText(spaced(random.randbytes(self.size())))

    def wrong(self):
        data = hex_bytes(self.data.text())
        if data is None or len(data) != self.size():
            return f"{self.proto.currentData()} data is {self.size()} bytes, in hex."
        return ""

    def args(self):
        return [self.proto.currentData(), hex_bytes(self.data.text()).hex().upper()]


class IbuttonKeyDialog(Form):
    KINDS = [("DS1990 (a ROM only)", "ds1990", (6, 7, 8)),
             ("DS1992 (and 128 bytes of memory)", "ds1992", (6, 7, 8)),
             ("DS1996 (and 8 KB of memory)", "ds1996", (6, 7, 8)),
             ("DS1971 (and a 32-byte EEPROM)", "ds1971", (6, 7, 8)),
             ("Cyfral", "cyfral", (2,)),
             ("Metakom", "metakom", (4,))]

    def __init__(self, parent):
        super().__init__(parent, "A new iButton key",
                         "A key touched to the Flipper's contact. For the Dallas "
                         "keys give the 6-byte serial (the family code and the "
                         "CRC are added), or the whole ROM.")
        self.kind = self.combo("Kind", [(t, k) for t, k, _ in self.KINDS])
        self.data = self.line("Data", "", "", random_button(self.shuffle))
        self.kind.currentIndexChanged.connect(self.shuffle)
        self.check(self.wrong)
        self.shuffle()

    def sizes(self):
        return self.KINDS[self.kind.currentIndex()][2]

    def shuffle(self, *_):
        self.data.setText(spaced(random.randbytes(self.sizes()[0])))

    def wrong(self):
        data = hex_bytes(self.data.text())
        sizes = self.sizes()
        if data is None or len(data) not in sizes:
            what = "6 bytes (the serial), 7 or 8 (a ROM)" if len(sizes) > 1 \
                else f"{sizes[0]} bytes"
            return f"The data is {what}, in hex."
        return ""

    def args(self):
        return [self.kind.currentData(), spaced(hex_bytes(self.data.text()))]


class IrSignalDialog(Form):
    def __init__(self, parent, last=None):
        import fe_ir
        super().__init__(parent, "An infrared signal",
                         "What a remote control of this protocol sends, pointed "
                         "at the Flipper's receiver.")
        last = last or ["NEC", "0x04", "0x08"]
        self.proto = self.combo("Protocol", [(p, p) for p in fe_ir.SPECS],
                                current=last[0])
        self.address = self.line("Address", last[1], "0x04")
        self.command = self.line("Command", last[2], "0x08")
        self.repeat = self.spin("Frames", 0, 20, 0)
        self.repeat.setSpecialValueText("as the protocol does")
        self.check(self.wrong)

    def wrong(self):
        for what, e in (("address", self.address), ("command", self.command)):
            try:
                if int(e.text().strip(), 0) < 0:
                    raise ValueError
            except ValueError:
                return f"The {what} is a number (0x04, or 4)."
        return ""

    def args(self):
        out = [self.proto.currentData(), self.address.text().strip(),
               self.command.text().strip()]
        if self.repeat.value():
            out += ["--repeat", str(self.repeat.value())]
        return out


class PrincetonDialog(Form):
    def __init__(self, parent):
        super().__init__(parent, "A Princeton key",
                         "A fixed-code remote (PT2262 and its kind), as a "
                         "garage door or a doorbell has, sending this key.")
        self.key = self.line("Key", "0x123456", "a number: 0x123456")
        self.bits = self.spin("Bits", 8, 32, 24)
        self.te = self.spin("TE", 100, 1500, 390, " µs")
        self.freq = self.spin("Frequency", 300, 950, 433.92, " MHz", decimals=3)
        self.check(self.wrong)

    def wrong(self):
        try:
            key = int(self.key.text().strip(), 0)
        except ValueError:
            return "The key is a number (0x123456)."
        if key < 0 or key >> self.bits.value():
            return f"The key does not fit in {self.bits.value()} bits."
        return ""

    def args(self):
        return ["princeton", self.key.text().strip(), "--bits", self.bits.value(),
                "--te", self.te.value(), "--freq", f"{self.freq.value():.3f}"]


class BatteryDialog(Form):
    def __init__(self, parent, percent=87, celsius=25.0):
        super().__init__(parent, "The battery",
                         "Its charge (with a voltage to match) and its "
                         "temperature. At 0 % the firmware, off USB, warns, "
                         "counts down and switches the Flipper off.")
        self.percent = self.spin("Charge", 0, 100, percent, " %")
        self.celsius = self.spin("Temperature", -20, 60, celsius, " °C",
                                 decimals=1)

    def args(self):
        return [self.percent.value(), "--temperature",
                f"{self.celsius.value():.1f}"]


class IqDialog(Form):
    def __init__(self, parent, path):
        super().__init__(parent, "An IQ recording as its antenna",
                         f"{os.path.basename(path)}: what the radio that "
                         "recorded it received, the Flipper's radio receives, "
                         "at the frequency the Flipper listens on. Say how it "
                         "was recorded.")
        self.rate = self.spin("Sample rate", 0.25, 20, 2.0, " Msps", decimals=3)
        self.center = self.spin("Centre frequency", 300, 950, 433.92, " MHz",
                                decimals=3)
        self.loop = self.tick("Play it over and over")

    def args(self):
        return (["--rate", f"{self.rate.value():.3f}M", "--center",
                 f"{self.center.value():.3f}M"]
                + (["--loop"] if self.loop.isChecked() else []))


class TextDialog(Form):
    """One line to fill in"""

    def __init__(self, parent, title, intro, label, text, placeholder="",
                 check=None):
        super().__init__(parent, title, intro)
        self.text = self.line(label, text, placeholder)
        self.text.setMinimumWidth(260)
        if check:
            self.check(lambda: check(self.text.text().strip()))

    def value(self):
        return self.text.text().strip()


# ---------------------------------------------------------------- remotes

def ir_signals(path):
    """The signals of a Flipper .ir file: [(name, what it is)]"""
    sigs, cur = [], None
    with open(path, errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            key, _, value = line.partition(":")
            key, value = key.strip().lower(), value.strip()
            if key == "name":
                cur = {"name": value}
                sigs.append(cur)
            elif cur is not None:
                cur[key] = value
    out = []
    for s in sigs:
        if s.get("type") == "parsed":
            out.append((s["name"], f"{s.get('protocol', '?')}, address "
                        f"{s.get('address', '?')}, command {s.get('command', '?')}"))
        elif s.get("type") == "raw":
            try:
                khz = float(s.get("frequency", 38000)) / 1000
            except ValueError:
                khz = 38
            out.append((s["name"], f"raw: {len(s.get('data', '').split())} "
                        f"marks and spaces at {khz:g} kHz"))
    return out


class RemoteWindow(QtWidgets.QWidget):
    """An infrared remote made of an .ir file: a button for each signal"""
    send = QtCore.Signal(str, str)          # the file, the signal's name

    def __init__(self, path, parent=None):
        super().__init__(parent, Qt.Tool)
        self.path = str(path)
        self.setWindowTitle(f"Remote: {os.path.basename(self.path)}")
        box = QtWidgets.QVBoxLayout(self)
        try:
            sigs = ir_signals(self.path)
        except OSError as e:
            sigs = []
            box.addWidget(QtWidgets.QLabel(str(e)))
        if not sigs:
            box.addWidget(QtWidgets.QLabel("No signals in this file."))
        grid = QtWidgets.QGridLayout()
        box.addLayout(grid)
        seen = set()
        for i, (name, what) in enumerate(sigs):
            b = QtWidgets.QPushButton(name)
            b.setToolTip(what + (" (a second signal of this name: the first "
                                 "is sent)" if name in seen else ""))
            b.setAutoDefault(False)
            b.clicked.connect(lambda _=False, n=name: self.send.emit(self.path, n))
            grid.addWidget(b, i // 3, i % 3)
            seen.add(name)
        box.addStretch(1)


# ---------------------------------------------------------------- the console

class LogDock(QtWidgets.QDockWidget):
    """The console.  What happened: what you did, what the Flipper did; and a
    line for commands of the Flipper's CLI"""
    cli = QtCore.Signal(str)
    leave = QtCore.Signal()                 # Esc in the CLI line
    COLOURS = {"info": None, "you": "#1e66f5", "flipper": "#8839ef",
               "error": "#d20f39", "dim": "#8c8fa1"}

    def __init__(self, parent=None):
        super().__init__("Console", parent)
        self.setObjectName("log")
        self.setFeatures(QtWidgets.QDockWidget.DockWidgetClosable)
        w = QtWidgets.QWidget()
        box = QtWidgets.QVBoxLayout(w)
        box.setContentsMargins(4, 2, 4, 4)
        self.text = QtWidgets.QPlainTextEdit()
        self.text.setReadOnly(True)
        self.text.setMaximumBlockCount(2000)
        self.text.setFocusPolicy(Qt.ClickFocus)
        self.text.setMinimumHeight(110)
        box.addWidget(self.text)
        row = QtWidgets.QHBoxLayout()
        row.addWidget(QtWidgets.QLabel("CLI"))
        self.line = QtWidgets.QLineEdit()
        self.line.setPlaceholderText("a command for the Flipper's CLI, as "
                                     "`storage list /ext` or `info device`")
        self.line.returnPressed.connect(self._enter)
        self.line.installEventFilter(self)
        row.addWidget(self.line, 1)
        run = QtWidgets.QPushButton("Run")
        run.setFocusPolicy(Qt.NoFocus)
        run.clicked.connect(self._enter)
        row.addWidget(run)
        box.addLayout(row)
        self.setWidget(w)
        self.history = []

    def eventFilter(self, obj, e):
        if obj is self.line and e.type() == QtCore.QEvent.KeyPress:
            if e.key() == Qt.Key_Escape:
                self.leave.emit()
                return True
            if e.key() in (Qt.Key_Up, Qt.Key_Down) and self.history:
                i = self.history.index(self.line.text()) \
                    if self.line.text() in self.history else len(self.history)
                i = max(0, min(len(self.history) - 1,
                               i + (-1 if e.key() == Qt.Key_Up else 1)))
                self.line.setText(self.history[i])
                return True
        return super().eventFilter(obj, e)

    def _enter(self):
        command = self.line.text().strip()
        if command:
            if command in self.history:
                self.history.remove(command)
            self.history.append(command)
            self.line.clear()
            self.cli.emit(command)

    def add(self, text, kind="info", pre=False):
        stamp = time.strftime("%H:%M:%S")
        colour = self.COLOURS.get(kind)
        body = html.escape(text.rstrip("\n"))
        if pre:
            body = body.replace(" ", "&nbsp;")
        body = body.replace("\n", "<br>")
        if pre:
            body = f'<span style="font-family: monospace;">{body}</span>'
        if colour:
            body = f'<span style="color: {colour};">{body}</span>'
        self.text.appendHtml(f'<span style="color: #8c8fa1;">{stamp}</span>'
                             f"&nbsp; {body}")
        bar = self.text.verticalScrollBar()
        bar.setValue(bar.maximum())


# ---------------------------------------------------------------- the firmware's log

class FirmwareLogDock(QtWidgets.QDockWidget):
    """What the firmware logs.  A real Flipper writes it to its UART, pins 13
    and 14 of the GPIO header; the emulator keeps it in the device's
    uart.log, and this reads that as it grows (fe_log.py: `flipperemu log`
    reads the same in a terminal)."""
    crashed = QtCore.Signal(str, str)       # CRASH or HALT, and what it said
    restarted = QtCore.Signal()             # the firmware started again
    level_wanted = QtCore.Signal(str)       # one of the firmware's levels
    save_wanted = QtCore.Signal()
    leave = QtCore.Signal()                 # Esc in the filter line
    KEEP = 5000                             # lines
    TAIL = 512 << 10                        # of a log that is there: its end
    POLL_MS = 100
    SHOW = (("Everything", ""), ("Debug and above", "D"),
            ("Info and above", "I"), ("Warnings and errors", "W"),
            ("Errors", "E"))
    # the firmware's levels, named as its own Settings name them
    LEVELS = (("default", "Default"), ("none", "None"), ("error", "Error"),
              ("warn", "Warning"), ("info", "Info"), ("debug", "Debug"),
              ("trace", "Trace"))
    # the firmware's colours, on a light and on a dark background
    LIGHT = {"red": "#d20f39", "green": "#40a02b", "yellow": "#df8e1d",
             "blue": "#1e66f5", "magenta": "#8839ef", "cyan": "#179299",
             "dim": "#8c8fa1"}
    DARK = {"red": "#f38ba8", "green": "#a6e3a1", "yellow": "#f9e2af",
            "blue": "#89b4fa", "magenta": "#cba6f7", "cyan": "#94e2d5",
            "dim": "#7f849c"}
    STAMP = re.compile(r"\d+ ")

    def __init__(self, path, parent=None):
        super().__init__("Firmware log", parent)
        self.setObjectName("fwlog")
        self.setFeatures(QtWidgets.QDockWidget.DockWidgetClosable)
        self.path = str(path)
        self.reader = fe_log.Log(self.path, tail=self.TAIL)
        self.entries = collections.deque(maxlen=self.KEEP)  # (Line, its
        # pieces of one colour), or (Mark, None)
        self.colour = None              # the one the last line left on
        self.marks = []                 # those since the last line shown
        self.caught_up = False          # what was there already has been read
        self.in_front = False           # (a tab behind another is not)
        self.stale = False              # the text is not what `entries` holds
        self.level = ""                 # the least serious one shown
        self.pattern = None
        self.formats = {}

        w = QtWidgets.QWidget()
        box = QtWidgets.QVBoxLayout(w)
        box.setContentsMargins(4, 2, 4, 4)
        row = QtWidgets.QHBoxLayout()
        self.show_box = QtWidgets.QComboBox()
        for text, level in self.SHOW:
            self.show_box.addItem(text, level)
        self.show_box.setFocusPolicy(Qt.NoFocus)
        self.show_box.setToolTip("Which of the lines it logged are shown")
        self.show_box.currentIndexChanged.connect(self._filter)
        row.addWidget(self.show_box)
        self.find = QtWidgets.QLineEdit()
        self.find.setPlaceholderText("only lines with…   (or /a regular "
                                     "expression/)")
        self.find.setClearButtonEnabled(True)
        self.find.setToolTip("Only the lines that hold this are shown: a tag "
                             "as [Loader], a word. Capitals matter only if "
                             "you type any.")
        self.find.installEventFilter(self)
        row.addWidget(self.find, 1)
        self.level_button = QtWidgets.QPushButton()
        self.level_button.setFocusPolicy(Qt.NoFocus)
        self.level_button.setAutoDefault(False)
        self.level_button.setToolTip(
            "How much the firmware logs: its own log level, the one of "
            "Settings > System > Log Level. It reads it when it starts, so "
            "choosing one here restarts it.")
        menu = QtWidgets.QMenu(self.level_button)
        menu.addAction("The firmware logs (it starts again):").setEnabled(False)
        group = QtGui.QActionGroup(self)
        self.level_actions = {}
        for name, text in self.LEVELS:
            a = menu.addAction(text + (" (which is Info)" if name == "default"
                                       else ""))
            a.setCheckable(True)
            a.triggered.connect(lambda _=False, n=name:
                                self.level_wanted.emit(n))
            group.addAction(a)
            self.level_actions[name] = a
        self.level_button.setMenu(menu)
        row.addWidget(self.level_button)
        for text, tip, slot in (
                ("Clear", "Empty this view (the log itself stays, in "
                 + self.path + ")", self.clear),
                ("Save…", "Save the lines shown here as text",
                 self.save_wanted.emit)):
            b = QtWidgets.QPushButton(text)
            b.setFocusPolicy(Qt.NoFocus)
            b.setAutoDefault(False)
            b.setToolTip(tip)
            b.clicked.connect(lambda _=False, s=slot: s())
            row.addWidget(b)
        box.addLayout(row)
        self.text = QtWidgets.QPlainTextEdit()
        self.text.setReadOnly(True)
        self.text.setUndoRedoEnabled(False)
        self.text.setMaximumBlockCount(self.KEEP)
        self.text.setFocusPolicy(Qt.ClickFocus)
        self.text.setMinimumHeight(110)
        font = QtGui.QFontDatabase.systemFont(QtGui.QFontDatabase.FixedFont)
        self.text.setFont(font)
        self.text.setTabStopDistance(
            8 * QtGui.QFontMetricsF(font).horizontalAdvance(" "))
        box.addWidget(self.text)
        self.setWidget(w)
        self.set_level(None)
        self._palette()
        self._empty()

        self.later = QtCore.QTimer(self, singleShot=True, interval=200)
        self.later.timeout.connect(self._filter)
        self.find.textChanged.connect(lambda: self.later.start())
        self.visibilityChanged.connect(self._front)
        self.timer = QtCore.QTimer(self, interval=self.POLL_MS)
        self.timer.timeout.connect(self.poll)
        self.timer.start()

    # ---- what the firmware wrote

    def poll(self):
        """What is new in the log"""
        new = []
        for item in self.reader.poll():
            if isinstance(item, fe_log.Mark):
                self.colour = None
                new.append((item, None))
                if item == fe_log.REBOOT and self.caught_up:
                    self.restarted.emit()
                continue
            pieces, self.colour = fe_log.segments(item.raw, self.colour)
            if not item.plain.strip():
                continue
            new.append((item, pieces))
            crash = fe_log.crash_of(item) if self.caught_up else None
            if crash:
                self.crashed.emit(*crash)
        if not self.reader.more:
            self.caught_up = True
        if not new:
            return
        self.entries.extend(new)
        if self.stale or not self.in_front or len(new) > self.KEEP // 2:
            self.stale = True
            if self.in_front:
                self.rebuild()
        else:
            self._append(new)
            self._empty()

    def _pieces(self, line, pieces):
        """A line as it is shown: its time in a column of its own"""
        if line.ms is None:
            return pieces
        skip = self.STAMP.match(line.plain).end()
        out = [(f"{line.ms:>7} ", "dim")]
        for text, colour in pieces:
            if skip < len(text):
                out.append((text[skip:], colour))
            skip = max(0, skip - len(text))
        return out

    def _append(self, entries):
        bar = self.text.verticalScrollBar()
        at_end = bar.value() >= bar.maximum() - 2
        document = self.text.document()
        cursor = QtGui.QTextCursor(document)
        cursor.movePosition(QtGui.QTextCursor.End)
        first = document.isEmpty()
        plain = self.formats[None]
        cursor.beginEditBlock()
        for item, pieces in entries:
            if isinstance(item, fe_log.Mark):
                self.marks.append(item)
                continue
            if not fe_log.passes(item, self.level, self.pattern):
                continue
            lines = [[(f"──── {mark} ────", "dim")] for mark in self.marks]
            self.marks = []
            for line in lines + [self._pieces(item, pieces)]:
                if not first:
                    cursor.insertBlock()
                first = False
                for text, colour in line:
                    cursor.insertText(text, self.formats.get(colour, plain))
        cursor.endEditBlock()
        if at_end:
            bar.setValue(bar.maximum())

    def rebuild(self):
        """The text again: of what is kept, what passes the filter"""
        self.stale = False
        self.marks = []
        self.text.clear()
        self._append(self.entries)
        bar = self.text.verticalScrollBar()
        bar.setValue(bar.maximum())
        self._empty()

    def _empty(self):
        """What an empty view says"""
        if self.entries:
            text = "None of the lines it logged passes the filter."
        else:
            text = ("Nothing logged yet.\n\nWhat the firmware writes to its "
                    "UART shows here: on a real Flipper that is pins 13 and "
                    "14 of the GPIO header.\nIt is kept in " + self.path
                    + ", and `flipperemu log -f` follows it in a terminal.")
        if text != self.text.placeholderText():
            self.text.setPlaceholderText(text)

    def _front(self, on):
        self.in_front = on
        if on and self.stale:
            self.rebuild()

    def clear(self):
        self.entries.clear()
        self.rebuild()

    def shown_text(self):
        return self.text.toPlainText()

    # ---- which of it is shown

    def _filter(self):
        self.later.stop()
        text = self.find.text().strip()
        pattern = None
        if len(text) > 2 and text[0] == text[-1] == "/":
            try:
                pattern = fe_log.smart(text[1:-1])
            except re.error:
                pass                    # (not one yet: the words as they are)
        if text and not pattern:
            pattern = fe_log.smart(re.escape(text))
        self.level, self.pattern = self.show_box.currentData(), pattern
        self.stale = True
        if self.in_front:
            self.rebuild()

    def eventFilter(self, obj, e):
        if obj is self.find and e.type() == QtCore.QEvent.KeyPress \
                and e.key() == Qt.Key_Escape:
            if self.find.text():
                self.find.clear()
            else:
                self.leave.emit()
            return True
        return super().eventFilter(obj, e)

    def _palette(self):
        dark = self.text.palette().color(QtGui.QPalette.Base).lightness() < 128
        self.formats = {None: QtGui.QTextCharFormat()}
        for name, colour in (self.DARK if dark else self.LIGHT).items():
            f = QtGui.QTextCharFormat()
            f.setForeground(QtGui.QColor(colour))
            self.formats[name] = f

    def changeEvent(self, e):
        super().changeEvent(e)
        if e.type() == QtCore.QEvent.PaletteChange and self.formats:
            self._palette()
            self.stale = True
            if self.in_front:
                self.rebuild()

    # ---- how much the firmware logs

    def set_level(self, name):
        """name: the firmware's log level as the emulator reports it (None:
        it does not)"""
        words = dict(self.LEVELS)
        for level, a in self.level_actions.items():
            a.setChecked(level == name)
        if name == "default":
            self.level_button.setText("Firmware level: default (info)")
        elif name in words:
            self.level_button.setText(f"Firmware level: {words[name].lower()}")
        else:
            self.level_button.setText("Firmware level")

    def set_settable(self, on):
        self.level_button.setEnabled(on)
