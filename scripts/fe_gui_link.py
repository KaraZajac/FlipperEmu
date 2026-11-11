"""The window's end of the front-panel link (docs/PANEL_LINK.md).

The emulator serves devices/NAME/panel.sock; PanelLink connects to it (again
and again, as the emulator comes and goes), turns the lines it sends into Qt
signals, and sends the buttons back.

Copyright (c) 2026, Kara Zajac
SPDX-License-Identifier: BSD-3-Clause
"""
import json
import os

from PySide6 import QtCore, QtNetwork

KEYS = ("up", "down", "left", "right", "ok", "back")


class PanelLink(QtCore.QObject):
    hello = QtCore.Signal()             # the emulator is there, and talks to us
    lost = QtCore.Signal()              # ... and now it is gone
    busy = QtCore.Signal()              # connected, but another window has it
    frame = QtCore.Signal(bytes)        # 1024 bytes: 64 rows of 16, MSB left
    backlight = QtCore.Signal(int)      # 0..255
    led = QtCore.Signal(int)            # 0xRRGGBB
    vibro = QtCore.Signal(bool)
    speaker = QtCore.Signal(int, int)   # milli-Hz, duty in ppm; 0, 0: silent
    prop = QtCore.Signal(str, str, object)      # path, name, value (None: none)
    power_off = QtCore.Signal()         # the firmware switched the Flipper off
    halted = QtCore.Signal(int)         # ... or has halted, with its PC there
    stopped = QtCore.Signal(str)        # time stands still on it ("paused",
                                        # "debug" ...: why), or goes on ("")

    RETRY_MS = 250
    PATIENCE_MS = 2000                  # no hello for this long: someone else's

    def __init__(self, path, who="FlipperEmu window", parent=None):
        super().__init__(parent)
        self.path = os.path.abspath(path)   # (a bare name means one in /tmp)
        self.who = who
        self.up = False                 # the emulator said hello
        self.version = 0
        self.watches = []               # (path, name): asked again after a hello
        self.buf = bytearray()
        self.sock = QtNetwork.QLocalSocket(self)
        self.sock.connected.connect(self._connected)
        self.sock.disconnected.connect(self._disconnected)
        self.sock.readyRead.connect(self._read)
        self.retry = QtCore.QTimer(self)
        self.retry.setInterval(self.RETRY_MS)
        self.retry.timeout.connect(self._try)
        self.patience = QtCore.QTimer(self)
        self.patience.setSingleShot(True)
        self.patience.setInterval(self.PATIENCE_MS)
        self.patience.timeout.connect(self.busy)

    # ---- the connection

    def start(self):
        self.retry.start()
        self._try()

    def close(self):
        self.retry.stop()
        self.patience.stop()
        self.sock.abort()

    def connected(self):
        """at socket level: with the emulator, or waiting behind another window"""
        return self.sock.state() == QtNetwork.QLocalSocket.ConnectedState

    def _try(self):
        if (self.sock.state() == QtNetwork.QLocalSocket.UnconnectedState
                and os.path.exists(self.path)):
            self.sock.connectToServer(self.path)

    def _connected(self):
        self.send("hello " + self.who)
        self.patience.start()

    def _disconnected(self):
        self._read()                    # its last words (power 0, usually)
        self.buf.clear()
        self.patience.stop()
        self.up = False
        self.lost.emit()

    def send(self, line):
        if self.connected():
            self.sock.write(line.encode() + b"\n")

    # ---- to the emulator

    def press(self, key):
        if self.up and key in KEYS:
            self.send("press " + key)

    def release(self, key):
        if self.up and key in KEYS:
            self.send("release " + key)

    def refresh(self):
        if self.up:
            self.send("refresh")

    def watch(self, path, name):
        """Be told this QOM property's value now, and whenever it changes."""
        if (path, name) not in self.watches:
            self.watches.append((path, name))
        if self.up:
            self.send(f"watch {path} {name}")

    # ---- from the emulator

    def _read(self):
        self.buf += self.sock.readAll().data()
        while True:
            end = self.buf.find(b"\n")
            if end < 0:
                break
            line = bytes(self.buf[:end])
            del self.buf[:end + 1]
            try:
                self._line(line.decode("ascii", "replace"))
            except ValueError:
                pass                    # not a line of ours: unknown ones pass

    def _line(self, line):
        word, _, rest = line.partition(" ")
        if word == "lcd":
            data = bytes.fromhex(rest)
            if len(data) == 1024:
                self.frame.emit(data)
        elif word == "backlight":
            self.backlight.emit(int(rest))
        elif word == "led":
            self.led.emit(int(rest, 16))
        elif word == "vibro":
            self.vibro.emit(int(rest) != 0)
        elif word == "speaker":
            f = dict(x.split("=", 1) for x in rest.split() if "=" in x)
            self.speaker.emit(int(f.get("freq", 0)), int(f.get("duty", 0)))
        elif word == "prop":
            path, name, value = rest.split(" ", 2)
            self.prop.emit(path, name, json.loads(value))
        elif word == "power":
            if int(rest) == 0:
                self.power_off.emit()
        elif word == "halted":
            self.halted.emit(int(rest, 16))
        elif word == "running":
            on, _, why = rest.partition(" ")
            self.stopped.emit("" if int(on) else why.strip() or "paused")
        elif word == "hello" and rest.startswith("flipperemu panel"):
            self.patience.stop()
            self.version = int(rest.split()[2]) if len(rest.split()) > 2 else 1
            self.up = True
            for path, name in self.watches:
                self.send(f"watch {path} {name}")
            self.hello.emit()
