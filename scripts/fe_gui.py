"""The window that looks like a Flipper.

`flipperemu run` opens it around the emulator it starts (and owns: closing
the window stops the emulator, and when the firmware switches the Flipper off
the window stays, so that holding Back switches it on again).  `flipperemu
gui` opens it for a device that runs already (closing it leaves the emulator
running).

The Flipper in the middle (fe_gui_view.py) gets its picture, its lights and
its buttons over the front-panel link (fe_gui_link.py, docs/PANEL_LINK.md),
which also reports the properties the bench under it shows: the USB cable,
the microSD card, the battery, what is held to its NFC, RFID and iButton
front ends, its infrared and Sub-GHz radios.  What the menus and the bench
change, they change with the launcher's own commands (`flipperemu usb
unplug`, `flipperemu nfc card.nfc` ...), as you would in a terminal.

Copyright (c) 2026, Kara Zajac
SPDX-License-Identifier: BSD-3-Clause
"""
import json
import os
import pathlib
import shutil
import signal
import sys
import threading
import time

from PySide6 import QtCore, QtGui, QtWidgets
from PySide6.QtCore import Qt

import fe_files
import fe_gui_widgets as W
import fe_record
from fe_gui_link import PanelLink
from fe_gui_view import (LIT_BG, LIT_FG, VIEW_H, VIEW_W, FlipperView,
                         app_icon)

WATCHES = (
    ("/machine/soc/usb", "attached"), ("/machine/soc/usb", "device"),
    ("/machine/charger", "usb"),
    ("/machine/gauge", "soc"), ("/machine/gauge", "voltage-mv"),
    ("/machine/gauge", "temperature-dk"), ("/machine/gauge", "current-ma"),
    ("/machine", "sd-card"),
    ("/machine/nfc", "card"), ("/machine/nfc", "field"),
    ("/machine/nfc", "radio"), ("/machine/nfc", "tx"), ("/machine/nfc", "reader"),
    ("/machine/rfid", "tag"), ("/machine/rfid", "t5577"),
    ("/machine/rfid", "reader"), ("/machine/rfid", "noise"),
    ("/machine/rfid", "field"), ("/machine/rfid", "tx"),
    ("/machine/ibutton", "key"), ("/machine/ibutton", "rom"),
    ("/machine/ibutton", "tx"), ("/machine/ibutton", "reader"),
    ("/machine/ir", "tx"), ("/machine/subghz", "radio"),
    ("/machine/speaker", "mute"),
    ("/machine", "log-level"),
)

# what can come from a file: (the menu's word for them, file filter, where
# the emulator saves what the Flipper itself sent or emulated)
FILES = {
    "nfc": ("NFC cards", "NFC cards (*.nfc *.json)", "nfc-tx"),
    "rfid": ("125 kHz tags", "125 kHz tags (*.rfid *.txt)", "rfid-tx"),
    "ibutton": ("iButton keys", "iButton keys (*.ibtn *.txt)", "ibutton-tx"),
    "ir": ("remotes", "Infrared remotes (*.ir)", "ir-tx"),
    "subghz": ("Sub-GHz captures", "Sub-GHz captures (*.sub)", "subghz-tx"),
}
FIRMWARE = "Firmware (*.tgz *.tar.gz *.tar *.dfu *.elf *.bin)"
APPS = "Flipper apps (*.fap)"
APP_WAIT_S = 45             # a question about an app on its screen: this long
SCALES = (2, 3, 4, 5, 6)
POWER_ON_MS = 1500          # hold Back this long, and an off Flipper starts
RESET_MS = 3000             # Left + Back held this long: it reboots
TERMINALS = (("xdg-terminal-exec", []), ("konsole", ["-e"]),
             ("gnome-terminal", ["--"]), ("kgx", ["-e"]), ("kitty", []),
             ("alacritty", ["-e"]), ("foot", []), ("wezterm", ["start", "--"]),
             ("xterm", ["-e"]))
KEYS_HELP = """\
<p><b>The buttons</b>: click them, or use the keyboard while the Flipper has
the focus: the arrow keys, <b>Enter</b> or <b>Space</b> for OK,
<b>Backspace</b> or <b>Esc</b> for Back. Hold them as long as you would hold a
real one: Back held for 5 seconds on the desktop offers to switch it off.</p>
<p><b>Left + Back</b> held for 3 seconds reboots it, as on a real one; keep
Left held after letting go of Back and it starts in DFU mode.</p>
<p>When it is <b>off</b>, hold Back to switch it on, or plug its USB cable in
(click the plug).</p>
<p>Its ports are where a real one has them. Click the <b>USB plug</b> in its
right side to plug it in and out, the <b>microSD card</b>
at the bottom right to pull it out and push it back, the <b>infrared
window</b> in the upper left face to point a remote at it, and the <b>GPIO
pins</b> along the top for what the firmware logs (it comes out of two of
them). Right-click for their menus, and anywhere else on the Flipper for the
Device menu.</p>
<p>Files dropped on the window go where they belong: .nfc and Proxmark3
.json cards, .rfid tags, .ibtn keys, .sub captures, .ir remotes, and
firmware. An app (<b>.fap</b>) is copied to its card and started, and
<b>F5</b> does that again with the same file, after a new build. Anything
else dropped is copied to its microSD card, into a folder you name.</p>"""


def config_path():
    if os.environ.get("FLIPPEREMU_GUI_CONFIG"):
        return pathlib.Path(os.environ["FLIPPEREMU_GUI_CONFIG"])
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return pathlib.Path(base) / "FlipperEmu" / "gui.ini"


class Settings:
    """What the window remembers, as JSON in an INI file"""

    def __init__(self, path):
        self.q = QtCore.QSettings(str(path), QtCore.QSettings.IniFormat)

    def get(self, key, default=None):
        value = self.q.value(key)
        if value is None:
            return default
        try:
            return json.loads(value)
        except (TypeError, ValueError):
            return default

    def set(self, key, value):
        self.q.setValue(key, json.dumps(value))


def default_scale():
    """3 screen pixels for one of the LCD's, or 2 on a screen too small for
    the Flipper, its bench and the menus at that size"""
    screen = QtGui.QGuiApplication.primaryScreen()
    if screen:
        room = screen.availableGeometry()
        if room.width() < VIEW_W * 3 + 60 or room.height() < VIEW_H * 3 + 280:
            return 2
    return 3


def short(path):
    return os.path.basename(path) if path else ""


def radio_fields(text):
    return dict(f.split("=", 1) for f in (text or "").split() if "=" in f)


def mhz(hz):
    try:
        return f"{int(hz) / 1e6:.2f} MHz"
    except (TypeError, ValueError):
        return "?"


def human_size(n):
    for unit in ("bytes", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}"
        n /= 1024


class MainWindow(QtWidgets.QMainWindow):
    recorded = QtCore.Signal(str, str, str)     # a recording is written: the
    # file, what it holds, or what went wrong (from the thread that wrote it)

    def __init__(self, name, directory, launcher, session=None, settings=None,
                 scale=None):
        super().__init__()
        self.name = name
        self.dir = pathlib.Path(directory)
        self.launcher = pathlib.Path(launcher)
        self.session = session          # ours to start and stop, or None
        self.settings = settings or Settings(config_path())
        self.exit_code = 0
        self.props = {}                 # (path, name): value, as reported
        self.state = None
        self.expect_off = False         # it said it is switching off
        self.qemu_up = session is not None      # ours, and running
        self.started_at = time.monotonic()
        self.started_by_us = False      # (not by `flipperemu run`)
        self.installing = None          # a firmware to flash while it is off
        self.cable = "computer"
        self.gauge = {}                 # the battery as it was last reported
        self.muted = False
        self.tone = (0, 0)
        self.recording = None           # a fe_record.Recording, while one runs
        self.recording_since = 0.0
        self.sd_none = (session is not None and session.args.no_sd) or \
            not (self.dir / "sd.img").exists()
        self.held = {}                  # held.json: what the commands held to it
        self.held_args = {}             # kind: the command's arguments
        self.switches = {}              # nfc field, rfid reader/noise, as set
        self.after_hello = []           # commands for a Flipper switched on
        self.links = {}                 # kind: W.Background
        self.remotes = {}               # .ir path: W.RemoteWindow
        self.readers = {}               # kind: its tx before a virtual reader
        self.last_ir = None
        self.last_subghz = None
        self.resetting = False
        self.closing = False
        self.subghz_seen = self.newest_file("subghz-tx")
        self.ir_at = 0.0

        k = scale or self.settings.get("scale") or default_scale()
        self.view = FlipperView(name, k, self.settings.get("theme", "white"))
        self.view.set_grid(self.settings.get("grid", True))
        self.link = PanelLink(self.dir / "panel.sock",
                              f"FlipperEmu window (pid {os.getpid()})", self)
        self.runner = W.Runner(self.launcher, name, self)
        self.log = W.LogDock(self)
        self.fwlog = W.FirmwareLogDock(self.dir / "uart.log", self)
        self.front = {}                 # dock: it is the tab in front
        self.build()
        self.build_menus()
        self.connect_all()
        for path, prop in WATCHES:
            self.link.watch(path, prop)

        self.power_hold = QtCore.QTimer(self, singleShot=True,
                                        interval=POWER_ON_MS)
        self.power_hold.timeout.connect(self.power_on)
        self.combo = QtCore.QTimer(self, singleShot=True, interval=RESET_MS)
        self.combo.timeout.connect(self.combo_fired)
        self.tiles_due = QtCore.QTimer(self, singleShot=True, interval=0)
        self.tiles_due.timeout.connect(self.refresh_tiles)
        self.poll = QtCore.QTimer(self, interval=250)
        self.poll.timeout.connect(self.tick)
        self.poll.start()

        self.set_state("starting" if session else "connecting")
        self.refresh_tiles()
        self.link.start()
        self.view.setFocus()

    # ------------------------------------------------------------ building

    def build(self):
        self.setWindowIcon(app_icon())
        self.setAcceptDrops(True)
        central = QtWidgets.QWidget()
        box = QtWidgets.QVBoxLayout(central)
        box.setContentsMargins(10, 6, 10, 10)
        box.setSpacing(8)
        box.addWidget(self.view, 0, Qt.AlignHCenter)
        self.bench = QtWidgets.QWidget()
        grid = QtWidgets.QGridLayout(self.bench)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setSpacing(6)
        self.tiles = {}
        for i, (kind, title) in enumerate((
                ("usb", "USB"), ("sd", "microSD"), ("battery", "Battery"),
                ("nfc", "NFC"), ("rfid", "RFID 125 kHz"), ("ibutton", "iButton"),
                ("ir", "Infrared"), ("subghz", "Sub-GHz"))):
            tile = W.Tile(title)
            grid.addWidget(tile, i // 4, i % 4)
            self.tiles[kind] = tile
        for c in range(4):
            grid.setColumnStretch(c, 1)
        box.addWidget(self.bench)
        self.bench.setVisible(self.settings.get("bench", True))
        self.setCentralWidget(central)
        # the console and the firmware's log: two tabs of one place
        self.addDockWidget(Qt.BottomDockWidgetArea, self.log)
        self.addDockWidget(Qt.BottomDockWidgetArea, self.fwlog)
        self.tabifyDockWidget(self.log, self.fwlog)
        for dock, key in ((self.log, "log"), (self.fwlog, "fwlog")):
            dock.setVisible(self.settings.get(key, False))
            dock.visibilityChanged.connect(
                lambda on, d=dock: self.front.__setitem__(d, on))
        (self.fwlog if self.settings.get("front") == "fwlog"
         else self.log).raise_()
        self.rec_label = QtWidgets.QLabel()
        self.rec_label.setStyleSheet("color: #d20f39; font-weight: bold")
        self.tone_label = QtWidgets.QLabel()
        self.state_label = QtWidgets.QLabel()
        self.statusBar().addPermanentWidget(self.rec_label)
        self.statusBar().addPermanentWidget(self.tone_label)
        self.statusBar().addPermanentWidget(self.state_label)
        if self.settings.get("on_top", False):
            self.setWindowFlag(Qt.WindowStaysOnTopHint, True)

    def act(self, menu, text, slot=None, shortcut=None, checkable=False,
            tip=None, live=False):
        a = QtGui.QAction(text, self)
        if shortcut:
            a.setShortcut(QtGui.QKeySequence(shortcut))
            a.setShortcutContext(Qt.WindowShortcut)
        a.setCheckable(checkable)
        if tip:
            a.setStatusTip(tip)
            a.setToolTip(tip)
        if slot:
            a.triggered.connect(slot)
        if live:
            self.live.append(a)
        if menu is not None:
            menu.addAction(a)
        self.addAction(a)               # its shortcut works while hidden too
        return a

    def build_menus(self):
        self.live = []                  # actions for an emulator that runs
        self.live_menus = []            # ... and menus
        bar = self.menuBar()
        owner = self.session is not None
        A = self.act

        m = self.menu_device = bar.addMenu("&Device")
        self.a_on = A(m, "Switch on", lambda: self.power_on(),
                      tip="As holding Back does: start the emulator again")
        self.a_reboot = A(m, "Reboot", self.reboot, "Ctrl+R", live=True,
                          tip="As holding Left + Back does")
        self.a_off = A(m, "Switch off now" if owner else "Stop the emulator",
                       self.hard_off, live=True,
                       tip="Pull its battery, as it were: the firmware is not "
                           "asked (the emulator saves its NVRAM)")
        m.addSeparator()
        self.a_install = A(m, "Install firmware…", self.ask_install,
                           tip="Program its flash with a firmware (an update "
                               "package, .dfu, .elf or .bin); its files stay")
        self.a_install.setVisible(owner)
        A(m, "Save the screen…", self.save_screen, "Ctrl+S")
        A(m, "Save a picture of the Flipper…", self.save_picture)
        A(m, "Copy the screen", self.copy_screen, "Ctrl+Shift+C")
        self.a_record = A(m, "Record the screen", self.set_recording,
                          "Ctrl+Shift+R", checkable=True,
                          tip="What the screen shows from now on, as an "
                              "animated GIF: once more ends it, and asks "
                              "where to save it")
        self.a_terminal = A(m, "Open its CLI in a terminal", self.open_terminal,
                            live=True)
        m.addSeparator()
        self.menu_sd = m.addMenu("microSD card")
        self.a_sd_out = A(self.menu_sd, "Pull it out",
                          lambda: self.set_sd(False), live=True)
        self.a_sd_in = A(self.menu_sd, "Push it in",
                         lambda: self.set_sd(True), live=True)
        self.menu_sd.addSeparator()
        A(self.menu_sd, "Copy files to it…", self.ask_copy_files,
          "Ctrl+Shift+O",
          tip="Through the firmware while it runs, into the card's image "
              "while it is off")
        A(self.menu_sd, "Copy a folder to it…", self.ask_copy_folder)
        self.menu_battery = m.addMenu("Battery")
        for text, percent in (("Full (100 %)", 100), ("Good (87 %)", 87),
                              ("Low (15 %)", 15), ("Very low (5 %)", 5),
                              ("Flat (0 %)", 0)):
            A(self.menu_battery, text,
              lambda _=False, p=percent: self.command("battery", p), live=True)
        self.menu_battery.addSeparator()
        A(self.menu_battery, "Set…", self.ask_battery, live=True)
        self.a_mute = A(m, "Mute", self.set_mute, "Ctrl+M", checkable=True,
                        live=True)
        m.addSeparator()
        A(m, "Quit" if owner else "Close", self.close, "Ctrl+Q",
          tip="Stops the emulator" if owner else "Leaves the emulator running")

        m = self.menu_apps = bar.addMenu("&Apps")
        self.a_app = A(m, "Install and start an app…", self.ask_app, "Ctrl+O",
                       tip="Copy a .fap from this computer to its card, close "
                           "the app that runs, and start this one")
        self.a_app_again = A(m, "Install and start it again", self.app_again,
                             "F5", tip="The same file again: after a new build")
        self.recent_menu(m, "app")
        m.addSeparator()
        A(m, "Close the app that runs", self.close_app, live=True,
          tip="As the loader asks it to; with its Back button if it does not "
              "listen")
        A(m, "List its apps", self.list_apps, live=True,
          tip="The apps the firmware has, and the .fap files on its card")

        m = self.menu_usb = bar.addMenu("&USB")
        group = QtGui.QActionGroup(self)
        self.a_cable = {}
        for cable, text in (("computer", "Plugged into a computer"),
                            ("charger", "Plugged into a charger"),
                            ("unplugged", "Unplugged")):
            a = A(m, text, lambda _=False, c=cable: self.set_cable(c),
                  checkable=True)
            group.addAction(a)
            self.a_cable[cable] = a
        m.addSeparator()
        A(m, "Plug in or unplug", self.toggle_usb, "Ctrl+U")

        m = self.menu_nfc = bar.addMenu("&NFC")
        A(m, "Hold a card to it…", lambda: self.ask_file("nfc"), live=True)
        self.recent_menu(m, "nfc")
        self.own_menu(m, "nfc", "What the virtual reader read")
        A(m, "A new tag…", self.ask_nfc_tag, live=True)
        self.a_nfc_away = A(m, "Take the card away",
                            lambda: self.take_away("nfc"), live=True)
        m.addSeparator()
        self.a_nfc_field = A(m, "A reader's field around it",
                             self.set_nfc_field, checkable=True, live=True,
                             tip="What the Flipper's field detection sees, "
                                 "with no reader talking")
        r = m.addMenu("Hold a virtual reader to it")
        A(r, "Scan: UID, ATQA, SAK", lambda: self.reader("nfc", "scan"),
          live=True)
        A(r, "Read an NTAG", lambda: self.reader("nfc", "ntag"), live=True)
        m.addSeparator()
        A(m, "Real cards through a Proxmark3…", self.ask_pm3, live=True)
        A(m, "A test ISO-DEP card", lambda: self.nfc_link("iso-dep"), live=True)
        A(m, "Log the reader's frames", lambda: self.nfc_link("log"), live=True)
        A(m, "A script as the card…", self.ask_nfc_script, live=True)
        self.a_nfc_stop = A(m, "Stop the link", lambda: self.stop_link("nfc"))

        m = self.menu_rfid = bar.addMenu("&RFID")
        A(m, "Hold a tag to it…", lambda: self.ask_file("rfid"), live=True)
        self.recent_menu(m, "rfid")
        self.own_menu(m, "rfid", "What it emulated")
        A(m, "A new tag…", self.ask_rfid_tag, live=True)
        A(m, "A blank T5577", lambda: self.hold("rfid", ["t5577"]), live=True)
        self.a_rfid_away = A(m, "Take the tag away",
                             lambda: self.take_away("rfid"), live=True)
        m.addSeparator()
        self.a_rfid_reader = A(m, "A reader's field around it",
                               lambda on: self.set_switch("rfid", "--reader", on),
                               checkable=True, live=True,
                               tip="It clocks the Flipper's emulation and "
                                   "records it; field detection sees it")
        self.a_rfid_noise = A(m, "Field noise",
                              lambda on: self.set_switch("rfid", "--noise", on),
                              checkable=True, live=True,
                              tip="Comparator noise while no tag answers, as "
                                  "on hardware")

        m = self.menu_ibutton = bar.addMenu("i&Button")
        A(m, "Touch a key to it…", lambda: self.ask_file("ibutton"), live=True)
        self.recent_menu(m, "ibutton")
        self.own_menu(m, "ibutton", "What it emulated")
        A(m, "A new key…", self.ask_ibutton_key, live=True)
        b = m.addMenu("A blank it can write")
        for text, kind in (("RW1990.1", "rw1990.1"), ("RW1990.2", "rw1990.2"),
                           ("TM2004", "tm2004"), ("TM01x", "tm01x")):
            A(b, text, lambda _=False, k=kind: self.hold("ibutton", [k]),
              live=True)
        self.a_ibutton_away = A(m, "Take the key away",
                                lambda: self.take_away("ibutton"), live=True)
        m.addSeparator()
        A(m, "Touch a virtual reader to it", lambda: self.reader("ibutton"),
          live=True, tip="Read the Dallas key the Flipper emulates")

        m = self.menu_ir = bar.addMenu("&Infrared")
        A(m, "Send a signal…", self.ask_ir, live=True)
        A(m, "Open a remote (.ir)…", lambda: self.ask_file("ir"))
        self.recent_menu(m, "ir", "Recent remotes")
        self.own_menu(m, "ir", "What it sent")
        self.a_ir_again = A(m, "The last signal again", self.ir_again,
                            "Ctrl+I", live=True)

        m = self.menu_subghz = bar.addMenu("&Sub-GHz")
        A(m, "Send a capture (.sub)…", lambda: self.ask_file("subghz"),
          live=True)
        self.recent_menu(m, "subghz")
        self.own_menu(m, "subghz", "What it sent")
        A(m, "A Princeton key…", self.ask_princeton, live=True)
        self.a_sub_again = A(m, "The last signal again", self.subghz_again,
                             live=True)
        m.addSeparator()
        r = m.addMenu("A real radio as its antenna")
        A(r, "An RTL-SDR (rtl_tcp)…", self.ask_rtl_tcp, live=True)
        A(r, "A HackRF", lambda: self.start_sdr("hackrf", "a HackRF"), live=True)
        A(r, "An IQ recording…", self.ask_iq, live=True)
        r.addSeparator()
        self.a_sdr_stop = A(r, "Stop", lambda: self.stop_link("subghz"))

        m = self.menu_view = bar.addMenu("&View")
        z = m.addMenu("Zoom")
        group = QtGui.QActionGroup(self)
        self.a_scale = {}
        for k in SCALES:
            a = A(z, f"{k}×", lambda _=False, k=k: self.set_scale(k),
                  checkable=True)
            group.addAction(a)
            self.a_scale[k] = a
        z.addSeparator()
        A(z, "Bigger", lambda: self.zoom(1), "Ctrl+=")
        A(z, "Smaller", lambda: self.zoom(-1), "Ctrl+-")
        self.a_scale.get(self.view.k, self.a_scale[3]).setChecked(True)
        c = m.addMenu("Colour")
        group = QtGui.QActionGroup(self)
        theme = self.settings.get("theme", "white")
        for key, text in (("white", "White"), ("black", "Black"),
                          ("mocha", "Catppuccin Mocha")):
            a = A(c, text, lambda _=False, t=key: self.set_theme(t),
                  checkable=True)
            a.setChecked(key == theme)
            group.addAction(a)
        a = A(m, "LCD pixel grid", self.set_grid, checkable=True)
        a.setChecked(self.view.grid)
        self.a_bench = A(m, "The bench", self.show_bench, checkable=True)
        self.a_bench.setChecked(self.bench.isVisibleTo(self))
        self.a_log = self.dock_action(
            m, self.log, "The console", "Ctrl+L",
            "What happened, and a line for the Flipper's CLI")
        self.a_fwlog = self.dock_action(
            m, self.fwlog, "The firmware log", "Ctrl+Shift+L",
            "What the firmware logs, as it logs it")
        a = A(m, "Always on top", self.set_on_top, checkable=True)
        a.setChecked(self.settings.get("on_top", False))

        m = bar.addMenu("&Help")
        A(m, "Keys and buttons", lambda: QtWidgets.QMessageBox.information(
            self, "Keys and buttons", KEYS_HELP))
        A(m, "About FlipperEmu", self.about)

        # the bench: a button for what one does most, the menu for the rest
        t = self.tiles
        t["usb"].set_menu(self.menu_usb)
        t["usb"].primary.clicked.connect(self.toggle_usb)
        t["sd"].set_menu(self.menu_sd)
        t["sd"].primary.clicked.connect(self.on_sd_click)
        t["battery"].set_menu(self.menu_battery)
        t["battery"].primary.clicked.connect(self.ask_battery)
        for kind in ("nfc", "rfid", "ibutton"):
            t[kind].set_menu(getattr(self, "menu_" + kind))
            t[kind].primary.clicked.connect(
                lambda _=False, k=kind: self.primary_hold(k))
        t["ir"].set_menu(self.menu_ir)
        t["ir"].primary.clicked.connect(self.ask_ir)
        t["subghz"].set_menu(self.menu_subghz)
        t["subghz"].primary.clicked.connect(lambda: self.ask_file("subghz"))

    def dock_action(self, menu, dock, text, shortcut, tip):
        """Shows a dock; one that is a tab behind the other comes to the
        front, and the one in front is closed"""
        a = self.act(menu, text, None, shortcut, checkable=True, tip=tip)

        def toggle():
            if dock.isHidden():
                self.show_dock(dock)
            elif self.front.get(dock, True):
                dock.hide()
            else:
                dock.raise_()
            a.setChecked(not dock.isHidden())
        a.triggered.connect(toggle)
        a.setChecked(not dock.isHidden())
        dock.toggleViewAction().toggled.connect(a.setChecked)
        return a

    def recent_menu(self, menu, kind, title="Recent"):
        sub = menu.addMenu(title)
        sub.aboutToShow.connect(lambda: self.fill_recent(sub, kind))
        if kind != "ir":
            self.live_menus.append(sub)

    def own_menu(self, menu, kind, title):
        sub = menu.addMenu(title)
        sub.aboutToShow.connect(lambda: self.fill_own(sub, kind))
        if kind != "ir":
            self.live_menus.append(sub)

    def fill_recent(self, sub, kind):
        sub.clear()
        paths = self.recent(kind)
        for path in paths:
            a = sub.addAction(short(path))
            a.setToolTip(path)
            a.triggered.connect(lambda _=False, p=path: self.use_file(kind, p))
        if paths:
            sub.addSeparator()
            sub.addAction("Forget them").triggered.connect(
                lambda: self.settings.set(f"recent/{kind}", []))
        else:
            sub.addAction("(none yet)").setEnabled(False)

    def fill_own(self, sub, kind):
        sub.clear()
        folder = self.dir / FILES[kind][2]
        try:
            files = sorted((p for p in folder.iterdir() if p.is_file()),
                           key=lambda p: p.stat().st_mtime, reverse=True)
        except OSError:
            files = []
        if kind == "ibutton":       # what a reader read, or a capture
            files = [p for p in files if p.suffix in (".ibtn", ".txt")]
        for p in files[:15]:
            when = time.strftime("%H:%M:%S", time.localtime(p.stat().st_mtime))
            a = sub.addAction(f"{p.name}   ({when})")
            a.setToolTip(str(p))
            a.triggered.connect(lambda _=False, p=str(p):
                                self.use_file(kind, p, remember=False))
        if not files:
            sub.addAction("(nothing yet)").setEnabled(False)

    def connect_all(self):
        L, V = self.link, self.view
        L.hello.connect(self.on_hello)
        L.lost.connect(self.on_lost)
        L.busy.connect(lambda: self.set_state("busy"))
        L.frame.connect(V.set_frame)
        L.frame.connect(self.on_frame)
        self.recorded.connect(self.on_recorded)
        L.backlight.connect(V.set_backlight)
        L.led.connect(V.set_led)
        L.vibro.connect(V.set_vibro)
        L.speaker.connect(self.on_speaker)
        L.prop.connect(self.on_prop)
        L.power_off.connect(self.on_power_off)
        V.key.connect(self.on_key)
        V.usb_clicked.connect(self.toggle_usb)
        V.sd_clicked.connect(self.on_sd_click)
        V.port_clicked.connect(self.on_port_click)
        V.menu_wanted.connect(self.on_context_menu)
        self.runner.output.connect(lambda line: self.log.add(line, "you"))
        self.runner.failed.connect(self.on_failed)
        self.log.cli.connect(self.run_cli)
        self.log.leave.connect(lambda: self.view.setFocus())
        self.fwlog.leave.connect(lambda: self.view.setFocus())
        self.fwlog.crashed.connect(self.on_crash)
        self.fwlog.level_wanted.connect(self.set_log_level)
        self.fwlog.save_wanted.connect(self.save_fwlog)

    # ------------------------------------------------------------ the states

    def hint(self, state):
        name = self.name
        if state == "off":
            if self.session:
                return ("Off", "hold Back to switch it on, or plug it in")
            return ("Off", f"flipperemu -d {name} run switches it on")
        return {
            "starting": ("Starting",),
            "connecting": ("Connecting",),
            "busy": ("Busy", "another window has this Flipper; this one waits "
                     "for it to close"),
            "lost": ("Not running", f"flipperemu -d {name} run starts it"),
            "installing": ("Installing", "the firmware, into its flash"),
        }.get(state, ("",))

    def set_state(self, state):
        if state == self.state:
            return
        self.state = state
        if state == "on":
            self.view.set_power("on")
        else:
            self.view.set_power(state, *self.hint(state))
            self.view.set_blank(False)
            self.resetting = False
            self.on_speaker(0, 0)
        words = {"on": "running", "starting": "starting", "connecting":
                 "connecting", "busy": "running (in another window)",
                 "off": "off", "lost": "not running", "installing":
                 "installing firmware"}
        self.state_label.setText(words.get(state, state))
        self.setWindowTitle(f"FlipperEmu: {self.name}"
                            + ("" if state == "on" else f" ({words.get(state)})"))
        self.update_actions()
        self.refresh_tiles()

    def is_live(self):
        """an emulator runs (and its monitor takes commands)"""
        return self.state in ("on", "busy")

    def update_actions(self):
        live = self.is_live()
        for a in self.live:
            a.setEnabled(live)
        for m in self.live_menus:
            m.setEnabled(live)
        owner = self.session is not None
        self.a_on.setVisible(owner)
        self.a_on.setEnabled(owner and self.state == "off")
        self.a_install.setEnabled(owner and self.state in ("on", "off", "busy"))
        self.a_nfc_stop.setEnabled("nfc" in self.links)
        self.a_sdr_stop.setEnabled("subghz" in self.links)
        self.a_ir_again.setEnabled(live and bool(self.last_ir))
        self.a_sub_again.setEnabled(live and bool(self.last_subghz))
        for cable, a in self.a_cable.items():
            a.setEnabled(live or (owner and self.state == "off"))
        self.a_terminal.setEnabled(live and self.cable == "computer")
        self.menu_sd.setEnabled(self.can_copy())
        again = self.recent("app")
        self.a_app.setEnabled(self.can_copy())
        self.a_app_again.setEnabled(live and bool(again))
        self.a_app_again.setText(f"Install and start {short(again[0])} again"
                                 if again else "Install and start it again")
        self.menu_battery.setEnabled(live)
        self.fwlog.set_settable(live or (owner and self.state == "off"))

    def on_hello(self):
        self.props.clear()
        self.expect_off = False
        self.set_state("on")
        for args in self.after_hello:
            self.command(*args, quiet=True)
        self.after_hello = []

    def on_lost(self):
        self.view.release_all()
        self.combo.stop()
        if self.closing:
            return
        if self.expect_off:
            self.set_state("off")
        else:
            self.set_state("connecting" if self.session else "lost")

    def on_power_off(self):
        self.expect_off = True
        self.log.add("It switched itself off.", "flipper")

    def tick(self):
        if self.qemu_up and self.session.poll() is not None:
            self.qemu_up = False
            # its last lines (power 0) come over the link first
            proc = self.session.proc
            QtCore.QTimer.singleShot(300, lambda: self.qemu_gone(proc))
        if self.state == "on":
            self.scan_subghz()
        if self.recording is not None:
            self.show_recording()

    def qemu_gone(self, proc):
        s = self.session
        if self.closing or s.proc is not proc:
            return                      # (and switched on again since)
        code = s.poll()
        s.tidy()
        self.stop_links()
        quick = self.started_by_us and time.monotonic() - self.started_at < 4
        if self.expect_off or self.installing or quick:
            if quick and not self.expect_off and not self.installing:
                self.log.add(f"The emulator stopped as it started (exit status "
                             f"{code}): see the terminal.", "error")
                self.show_log()
            self.expect_off = False
            self.set_state("off")
            if self.installing:
                self.install_now()
        else:                           # stopped from outside, or it failed
            self.exit_code = code or 0
            self.closing = True
            self.close()

    # ------------------------------------------------------------ power

    def power_on(self, cable=None, tries=0):
        if not self.session or self.state != "off" or self.closing:
            return
        cable = cable or self.cable
        if self.session.poll() is None:
            # the emulator that switched off is still on its way out: one
            # emulator at a time on a device's files
            if tries < 50:
                QtCore.QTimer.singleShot(
                    100, lambda: self.power_on(cable, tries + 1))
            return
        if cable == "unplugged" and self.gauge.get("voltage-mv", 4000) <= 3150:
            self.log.add("Its battery is flat: plug it in to switch it on.",
                         "error")
            self.statusBar().showMessage("Its battery is flat: plug it in", 5000)
            return
        self.cable = cable
        self.after_hello = []
        if cable == "charger":
            self.after_hello.append(("usb", "charger"))
        if self.muted:
            self.after_hello.append(("sound", "off"))
        for (kind, _), args in self.switches.items():
            self.after_hello.append((kind, *args))
        for kind, args in self.held_args.items():
            if args:
                self.after_hello.append((kind, *args))
        gauge = {k: v for k, v in self.gauge.items()
                 if k in ("soc", "voltage-mv", "temperature-dk")
                 and isinstance(v, int)}
        self.expect_off = False
        self.view.set_cable(cable)
        self.started_at = time.monotonic()
        self.started_by_us = True
        self.session.start(unplugged=cable != "computer", gauge=gauge or None)
        self.qemu_up = True
        self.set_state("starting")
        self.log.add("Switched on.", "you")

    def hard_off(self):
        if self.session:
            self.expect_off = True
            self.log.add("Switched off (the hard way).", "you")
            self.stop_links()
            self.session.stop()
        else:
            self.command("stop")

    def reboot(self):
        self.view.set_blank(True)
        self.command("reset", then=lambda code, text: self.view.set_blank(False))

    def combo_fired(self):
        if self.view.pressed("left") and self.view.pressed("back"):
            self.resetting = True
            self.view.set_blank(True)       # held in reset while they are

    def on_key(self, key, down):
        if self.resetting:
            if not down:
                self.link.release(key)
                if key in ("left", "back"):
                    # let go of one: it starts, with the other still held
                    # (Left: DFU, as on a real one)
                    self.resetting = False
                    QtCore.QTimer.singleShot(150, self.reboot)
            return
        if self.state == "on":
            (self.link.press if down else self.link.release)(key)
            both = self.view.pressed("left") and self.view.pressed("back")
            if both and not self.combo.isActive():
                self.combo.start()
            elif not both:
                self.combo.stop()
        elif self.state == "off" and key == "back" and self.session:
            if down:
                self.power_hold.start()
            else:
                self.power_hold.stop()

    # ------------------------------------------------------------ the link

    def on_speaker(self, freq_mhz, duty):
        self.tone = (freq_mhz, duty)
        sounding = freq_mhz > 0 and duty > 0
        self.view.set_tone(sounding)
        self.view.set_muted(self.muted)
        if self.muted:
            self.tone_label.setText("🔇 muted")
        elif sounding:
            self.tone_label.setText(f"♪ {freq_mhz / 1000:.0f} Hz")
        else:
            self.tone_label.setText("")

    def P(self, path, name, default=None):
        value = self.props.get((path, name))
        return default if value is None else value

    def on_prop(self, path, name, value):
        key = (path, name)
        first = key not in self.props
        old = self.props.get(key)
        self.props[key] = value
        if path == "/machine/gauge" and isinstance(value, int):
            self.gauge[name] = value
        if key == ("/machine/speaker", "mute"):
            self.muted = bool(value)
            self.a_mute.setChecked(self.muted)
            self.on_speaker(*self.tone)
        if key == ("/machine", "log-level"):
            self.fwlog.set_level(value)
        if not first and old != value:
            self.changed(path, name, old, value)
        self.tiles_due.start()

    def changed(self, path, name, old, value):
        """a watched property changed while it ran: what happened"""
        add = self.log.add
        if (path, name) == ("/machine/ir", "tx") and value:
            self.view.flash_ir()
            self.ir_at = time.monotonic()
            QtCore.QTimer.singleShot(2100, self.tiles_due.start)
            add(f"It sent infrared: {self.ir_summary(value)}", "flipper")
        elif (path, name) == ("/machine/rfid", "tx") and value:
            add(f"It emulated a 125 kHz tag (what the reader saw: "
                f"{short(value)})", "flipper")
        elif (path, name) == ("/machine/rfid", "t5577") and value and old:
            add(f"T5577 blocks now: {value}", "flipper")
        elif (path, name) == ("/machine/ibutton", "rom") and value and old:
            add(f"The key's ROM now: {value}", "flipper")
        elif name == "reader" and path in ("/machine/nfc", "/machine/ibutton") \
                and old and not value:
            kind = path.rsplit("/", 1)[1]
            QtCore.QTimer.singleShot(400, lambda: self.reader_result(kind))
        elif (path, name) == ("/machine/soc/usb", "device") and value not in (
                "none", "enumerating") and self.cable == "computer":
            add("USB: " + self.usb_line(value), "flipper")

    def ir_summary(self, path):
        try:
            import fe_ir
            _, durs = fe_ir.parse_ir_file(path)
            return (f"{len(durs)} marks and spaces, {sum(durs) / 1000:.0f} ms "
                    f"({short(path)})")
        except (OSError, ValueError, KeyError, ImportError):
            return short(path)

    def newest_file(self, folder):
        try:
            files = [p for p in (self.dir / folder).iterdir() if p.is_file()]
        except OSError:
            return None
        if not files:
            return None
        p = max(files, key=lambda p: p.stat().st_mtime)
        return (str(p), p.stat().st_mtime)

    def scan_subghz(self):
        """Sub-GHz transmissions: a new file in subghz-tx/ (its tx property is
        the whole RAW recording: too much to watch)"""
        now = self.newest_file("subghz-tx")
        if now and now != self.subghz_seen:
            self.subghz_seen = now
            text = short(now[0])
            try:
                import fe_subghz
                freq, durs = fe_subghz.parse_sub(now[0])
                text = (f"{mhz(freq)}, {len(durs)} levels, "
                        f"{sum(abs(d) for d in durs) / 1000:.0f} ms "
                        f"({short(now[0])})")
            except (OSError, ValueError, KeyError, ImportError):
                pass
            self.log.add(f"It transmitted on Sub-GHz: {text}", "flipper")
            self.tiles_due.start()

    # ------------------------------------------------------------ the bench

    def usb_line(self, device):
        return {"none": "the computer does not see it (yet)",
                "enumerating": "the computer is finding out what it is",
                "failed": "the computer could not make sense of it",
                "usbip": "a PC has it over USB/IP"}.get(
                    device, f"the computer sees {device}")

    def read_held(self):
        try:
            self.held = json.loads((self.dir / "held.json").read_text())
        except (OSError, ValueError):
            self.held = {}

    def refresh_tiles(self):
        t, P = self.tiles, self.P
        live = self.is_live()
        if live and self.props:
            attached = P("/machine/soc/usb", "attached")
            charger = P("/machine/charger", "usb")
            self.cable = "computer" if attached else "charger" if charger \
                else "unplugged"
        self.view.set_cable(self.cable)
        for cable, a in self.a_cable.items():
            a.setChecked(cable == self.cable)
        self.a_terminal.setEnabled(live and self.cable == "computer")
        self.read_held()

        # USB
        line2 = {"computer": self.usb_line(P("/machine/soc/usb", "device",
                                             "none")) if live else "",
                 "charger": "power without data: it charges",
                 "unplugged": "it runs on its battery"}[self.cable]
        t["usb"].set_state({"computer": "Plugged into a computer",
                            "charger": "On a charger",
                            "unplugged": "Unplugged"}[self.cable], line2,
                           {"computer": True, "charger": "warn",
                            "unplugged": False}[self.cable])
        can = live or (self.session is not None and self.state == "off")
        t["usb"].set_primary("Unplug" if self.cable != "unplugged" else "Plug in",
                             can)

        # microSD
        inserted = P("/machine", "sd-card", True)
        sd = None if self.sd_none else bool(inserted)
        self.view.set_sd(sd)
        try:
            size = human_size((self.dir / "sd.img").stat().st_size)
        except OSError:
            size = ""
        if sd is None:
            t["sd"].set_state("No card in this run", "", False)
        else:
            t["sd"].set_state("In the slot" if sd else "Pulled out",
                              f"a {size} card (sd.img)" if size else "", sd)
        t["sd"].set_primary("Pull out" if sd else "Push in",
                            live and sd is not None)
        self.a_sd_out.setEnabled(live and sd is True)
        self.a_sd_in.setEnabled(live and sd is False)

        # the battery
        g = self.gauge
        if "soc" in g:
            soc, mv = g.get("soc", 0), g.get("voltage-mv", 0)
            ma = g.get("current-ma", 0)
            c = g.get("temperature-dk", 2981) / 10 - 273.15
            flow = f"charging at {ma} mA" if ma > 0 else f"drawing {-ma} mA"
            t["battery"].set_state(f"{soc} %, {mv / 1000:.2f} V",
                                   f"{flow}, {c:.1f} °C",
                                   True if soc > 20 else "warn")
        else:
            t["battery"].set_state("", "", False)
        t["battery"].set_primary("Set…", live)

        # NFC
        card = P("/machine/nfc", "card", "")
        radio = P("/machine/nfc", "radio", "")
        bits = [{"field": "its reader is on", "listen": "it waits for a "
                 "reader, as a card", "idle": "its radio is off"}.get(radio, "")]
        if P("/machine/nfc", "field"):
            bits.append("a reader's field around it")
        if P("/machine/nfc", "reader"):
            bits.append(f"a virtual reader ({P('/machine/nfc', 'reader')}) in "
                        "front of it")
        if "nfc" in self.links:
            bits.append(self.links["nfc"].label)
        t["nfc"].set_state(self.held.get("nfc") or ("a card" if card else
                                                    "No card held to it"),
                           "; ".join(b for b in bits if b) if live else "",
                           radio in ("field", "listen"))
        t["nfc"].set_primary("Take away" if card else "Card…", live)
        self.a_nfc_away.setEnabled(live and bool(card))
        self.a_nfc_field.setChecked(bool(P("/machine/nfc", "field")))

        # RFID
        tag = P("/machine/rfid", "tag", "")
        t5577 = P("/machine/rfid", "t5577", "")
        field = P("/machine/rfid", "field", "off")
        reader = P("/machine/rfid", "reader", True)
        noise = P("/machine/rfid", "noise", True)
        if field and field != "off":
            hz, _, mod = field.partition(" ")
            try:
                bits = [f"its reader is on ({int(hz) / 1000:g} kHz, "
                        f"{mod.upper()})"]
            except ValueError:
                bits = ["its reader is on"]
        else:
            bits = ["its reader is off"]
        if not reader:
            bits.append("no reader's field around it")
        if not noise:
            bits.append("no field noise")
        name = self.held.get("rfid") if (tag or t5577) else None
        t["rfid"].set_state(name or ("a T5577" if t5577 else "a tag" if tag
                                     else "No tag held to it"),
                            "; ".join(bits) if live else "",
                            bool(field) and field != "off")
        t["rfid"].set_primary("Take away" if tag or t5577 else "Tag…", live)
        self.a_rfid_away.setEnabled(live and bool(tag or t5577))
        self.a_rfid_reader.setChecked(bool(reader))
        self.a_rfid_noise.setChecked(bool(noise))

        # iButton
        key = P("/machine/ibutton", "key", "")
        rom = P("/machine/ibutton", "rom", "")
        bits = [f"ROM now {rom}"] if rom else []
        if P("/machine/ibutton", "reader"):
            bits.append("a virtual reader on its contact")
        t["ibutton"].set_state((self.held.get("ibutton") or "a key") if key
                               else "No key on its contact", "; ".join(bits),
                               bool(key) or bool(P("/machine/ibutton", "reader")))
        t["ibutton"].set_primary("Take away" if key else "Key…", live)
        self.a_ibutton_away.setEnabled(live and bool(key))

        # infrared
        tx = P("/machine/ir", "tx", "")
        t["ir"].set_state(f"It sent: {short(tx)}" if tx else
                          "It has sent nothing yet",
                          f"you sent: {self.ir_label()}" if self.last_ir else
                          "point a remote at it",
                          time.monotonic() - self.ir_at < 2)
        t["ir"].set_primary("Send…", live)

        # Sub-GHz
        f = radio_fields(P("/machine/subghz", "radio", ""))
        st = f.get("state", "")
        mod = f.get("mod", "").upper()
        line1 = {"sleep": "Its radio is asleep", "idle": "Its radio is idle",
                 "rx": f"It listens at {mhz(f.get('freq'))}" +
                       (f" ({mod})" if mod else ""),
                 "fstxon": f"It is about to send at {mhz(f.get('freq'))}",
                 "tx": f"It sends at {mhz(f.get('freq'))}"}.get(st, "")
        if "subghz" in self.links:
            line2 = self.links["subghz"].label
        elif self.subghz_seen:
            line2 = f"it sent: {short(self.subghz_seen[0])}"
        else:
            line2 = ""
        t["subghz"].set_state(line1 if live else "", line2,
                              st in ("rx", "tx", "fstxon"))
        t["subghz"].set_primary("Send…", live)
        self.update_actions()

    def ir_label(self):
        a = self.last_ir or []
        if a and a[0].endswith(".ir"):
            return f"{a[1] if len(a) > 1 else 'the first'} of {short(a[0])}"
        return " ".join(a[:3])

    # ------------------------------------------------------------ commands

    def command(self, *args, then=None, quiet=False, limit=None):
        self.runner.run(*args, then=then, quiet=quiet, limit=limit)

    def on_failed(self, text):
        self.log.add(text, "error")
        self.statusBar().showMessage(text, 8000)
        if "started without -drive if=sd" in text:
            self.sd_none = True
            self.tiles_due.start()

    def show_dock(self, dock):
        dock.setVisible(True)
        dock.raise_()                   # (of the two tabs, the one in front)

    def show_log(self):
        """The console"""
        self.show_dock(self.log)

    def show_fwlog(self):
        self.show_dock(self.fwlog)

    def on_crash(self, kind, message):
        """The firmware's log says that it crashed (or halted)"""
        text = (f"The firmware {'halted' if kind == 'HALT' else 'crashed'}: "
                f"{message or 'it does not say why'}")
        self.log.add(f"{text}  (the firmware log has the rest: Ctrl+Shift+L)",
                     "error")
        self.statusBar().showMessage(text, 15000)

    def set_log_level(self, level):
        """The firmware's own log level.  It reads it when it starts: set,
        and started again"""
        word = dict(self.fwlog.LEVELS)[level].lower()
        if self.state == "off" and self.session:
            self.session.args.log_level = level     # for when it is switched on
            self.fwlog.set_level(level)
            self.log.add(f"The firmware's log level: {word}, when it is "
                         "switched on.", "you")
            return

        def done(code, text):
            if code:                    # as it was
                self.fwlog.set_level(self.P("/machine", "log-level"))
                return
            self.log.add(f"The firmware's log level: {word}. It starts again "
                         "to take it.", "you")
            self.reboot()
        self.command("log", "--set-level", level, then=done, quiet=True)

    def save_fwlog(self):
        start = os.path.join(self.settings.get("dir/logs",
                                               os.path.expanduser("~")),
                             f"{self.name}-{time.strftime('%H%M%S')}.log")
        path, _ = QtWidgets.QFileDialog.getSaveFileName(
            self, "Save the firmware log", start, "Logs (*.log *.txt)")
        if not path:
            return
        self.settings.set("dir/logs", os.path.dirname(path))
        try:
            with open(path, "w") as f:
                f.write(self.fwlog.shown_text() + "\n")
        except OSError as e:
            self.on_failed(f"saving the log: {e}")
            return
        self.statusBar().showMessage(f"Saved {path}", 5000)

    def run_cli(self, line):
        if not self.is_live():
            self.log.add("It is not running.", "error")
            return
        if self.cable != "computer":
            self.log.add("Its CLI is on USB: plug it into a computer first.",
                         "error")
            return
        self.log.add(f"> {line}", "you")
        self.runner.run("cli", "--timeout", "10", "--", line, quiet=True,
                        then=lambda code, text: self.log.add(
                            text or "(nothing)", "error" if code else "info",
                            pre=True))

    def recent(self, kind):
        return [p for p in self.settings.get(f"recent/{kind}", [])
                if os.path.isfile(p)]

    def remember(self, kind, path):
        path = os.path.abspath(path)
        paths = [p for p in self.recent(kind) if p != path]
        self.settings.set(f"recent/{kind}", [path] + paths[:9])
        self.settings.set(f"dir/{kind}", os.path.dirname(path))

    def ask_file(self, kind):
        title = {"nfc": "Hold a card to it", "rfid": "Hold a tag to it",
                 "ibutton": "Touch a key to it", "ir": "Open a remote",
                 "subghz": "Send a capture"}[kind]
        start = self.settings.get(f"dir/{kind}", os.path.expanduser("~"))
        path, _ = QtWidgets.QFileDialog.getOpenFileName(
            self, title, start, f"{FILES[kind][1]};;All files (*)")
        if path:
            self.use_file(kind, path)

    def use_file(self, kind, path, remember=True):
        if kind == "app":
            return self.start_app(path, remember)
        if remember:
            self.remember(kind, path)
        if kind == "ir":
            self.open_remote(path)
        elif kind == "subghz":
            self.send_subghz([path])
        else:
            self.hold(kind, [path])

    def hold(self, kind, args):
        self.held_args[kind] = list(args)
        self.command(kind, *args, then=lambda c, t: self.tiles_due.start())

    def take_away(self, kind):
        self.held_args[kind] = None
        self.command(kind, "none", then=lambda c, t: self.tiles_due.start())

    def primary_hold(self, kind):
        held ={"nfc": self.P("/machine/nfc", "card"),
                "rfid": self.P("/machine/rfid", "tag") or
                self.P("/machine/rfid", "t5577"),
                "ibutton": self.P("/machine/ibutton", "key")}[kind]
        if held:
            self.take_away(kind)
        else:
            self.ask_file(kind)

    def ask_nfc_tag(self):
        d = W.NfcTagDialog(self)
        if d.ask():
            self.hold("nfc", d.args())

    def ask_rfid_tag(self):
        d = W.RfidTagDialog(self)
        if d.ask():
            self.hold("rfid", d.args())

    def ask_ibutton_key(self):
        d = W.IbuttonKeyDialog(self)
        if d.ask():
            self.hold("ibutton", d.args())

    def set_nfc_field(self, on):
        self.set_switch("nfc", "--field", on)

    def set_switch(self, kind, option, on):
        args = [option, "on" if on else "off"]
        defaults = {"--field": False, "--reader": True, "--noise": True}
        if on == defaults[option]:
            self.switches.pop((kind, option), None)
        else:
            self.switches[(kind, option)] = args
        self.command(kind, *args)

    def reader(self, kind, mode=None):
        self.readers[kind] = self.P(f"/machine/{kind}", "tx", "")
        if kind == "nfc":
            self.command("nfc", "--reader", mode, "--no-wait")
        else:
            self.command("ibutton", "--reader", "--no-wait")
        self.show_log()

    def reader_result(self, kind):
        if kind not in self.readers:
            return
        before = self.readers.pop(kind)
        after = self.P(f"/machine/{kind}", "tx", "")
        if after and after != before and (kind == "nfc"
                                           or after.endswith(".ibtn")):
            self.log.add(f"The virtual reader read: {short(after)}", "flipper")
            try:
                self.log.add(pathlib.Path(after).read_text(), "dim", pre=True)
            except OSError as e:
                self.log.add(str(e), "error")
        else:
            what = ("a card (NFC → Saved → a card → Emulate)" if kind == "nfc"
                    else "a Dallas key (iButton → Saved → a key → Emulate)")
            self.log.add(f"The virtual reader read nothing: is it emulating "
                         f"{what}?", "error")

    def ask_battery(self):
        c = self.gauge.get("temperature-dk", 2981) / 10 - 273.15
        d = W.BatteryDialog(self, self.gauge.get("soc", 87), round(c, 1))
        if d.ask():
            self.command("battery", *d.args())

    def set_mute(self, on):
        self.muted = on
        self.command("sound", "off" if on else "on")
        self.on_speaker(*self.tone)

    def set_cable(self, cable):
        if self.state == "off" and self.session:
            self.cable = cable
            self.tiles_due.start()
            if cable != "unplugged":
                self.power_on(cable)
            return
        if not self.is_live() or cable == self.cable:
            self.tiles_due.start()
            return
        self.command("usb", {"computer": "plug", "charger": "charger",
                             "unplugged": "unplug"}[cable])

    def toggle_usb(self):
        self.set_cable("computer" if self.cable == "unplugged" else "unplugged")

    def on_sd_click(self):
        inserted = self.P("/machine", "sd-card")
        if self.is_live() and not self.sd_none:
            self.set_sd(not inserted)

    def set_sd(self, insert):
        self.command("sd", "insert" if insert else "eject")

    # infrared

    def ask_ir(self):
        last = self.last_ir if self.last_ir and not self.last_ir[0].endswith(
            ".ir") else None
        d = W.IrSignalDialog(self, last)
        if d.ask():
            self.send_ir(d.args())

    def send_ir(self, args):
        self.last_ir = list(args)
        self.command("ir", *args, then=lambda c, t: self.tiles_due.start())

    def ir_again(self):
        if self.last_ir:
            self.send_ir(self.last_ir)

    def open_remote(self, path):
        path = os.path.abspath(path)
        w = self.remotes.get(path)
        if w is None:
            w = W.RemoteWindow(path, self)
            w.send.connect(lambda p, n: self.send_ir([p, n]) if self.is_live()
                           else self.statusBar().showMessage(
                               "It is not running", 4000))
            self.remotes[path] = w
        w.show()
        w.raise_()

    # Sub-GHz

    def ask_princeton(self):
        d = W.PrincetonDialog(self)
        if d.ask():
            self.send_subghz(d.args())

    def send_subghz(self, args):
        self.last_subghz = list(args)
        self.command("subghz", *args)

    def subghz_again(self):
        if self.last_subghz:
            self.send_subghz(self.last_subghz)

    def ask_rtl_tcp(self):
        d = W.TextDialog(self, "An RTL-SDR as its antenna",
                         "An RTL-SDR behind rtl_tcp (`rtl_tcp -a 127.0.0.1`): "
                         "what it receives, the Flipper's radio receives, at "
                         "the frequency the Flipper listens on.",
                         "rtl_tcp at", self.settings.get("rtl_tcp",
                                                         "127.0.0.1:1234"),
                         "HOST:PORT")
        if d.ask():
            self.settings.set("rtl_tcp", d.value())
            self.start_sdr(f"rtl_tcp:{d.value()}" if d.value() else "rtl_tcp",
                           f"an RTL-SDR (rtl_tcp {d.value()})")

    def ask_iq(self):
        start = self.settings.get("dir/iq", os.path.expanduser("~"))
        path, _ = QtWidgets.QFileDialog.getOpenFileName(
            self, "An IQ recording as its antenna", start,
            "IQ recordings (*.cu8 *.cs8 *.cs16 *.cf32);;All files (*)")
        if not path:
            return
        self.settings.set("dir/iq", os.path.dirname(path))
        d = W.IqDialog(self, path)
        if d.ask():
            self.start_sdr(path, f"an IQ recording ({short(path)})", d.args())

    def start_sdr(self, source, label, options=()):
        # (--quiet: no status line, which a terminal redraws in place)
        self.start_link("subghz", ["subghz", "--sdr", source, "--quiet",
                                   *options],
                        f"a real radio as its antenna: {label}")

    # NFC links

    def ask_pm3(self):
        d = W.TextDialog(self, "Real cards through a Proxmark3",
                         "Cards on the Proxmark3's antenna answer the "
                         "Flipper's reader. Leave the port empty to find it.",
                         "Port", self.settings.get("pm3", ""), "/dev/ttyACM0")
        if d.ask():
            self.settings.set("pm3", d.value())
            self.nfc_link(f"pm3:{d.value()}" if d.value() else "pm3",
                          "real cards through a Proxmark3")

    def ask_nfc_script(self):
        start = self.settings.get("dir/script", os.path.expanduser("~"))
        path, _ = QtWidgets.QFileDialog.getOpenFileName(
            self, "A script as the card", start, "Python (*.py)")
        if path:
            self.settings.set("dir/script", os.path.dirname(path))
            self.nfc_link(f"script:{path}", f"a script as the card "
                                            f"({short(path)})")

    def nfc_link(self, card, label=None):
        label = label or {"iso-dep": "a test ISO-DEP card on the link",
                          "log": "the reader's frames, logged"}.get(card, card)
        self.start_link("nfc", ["nfc", "--link", card], label)
        self.show_log()

    def start_link(self, kind, args, label):
        self.stop_link(kind, wait=True)
        b = W.Background(self.launcher, self.name, args, label, self)
        b.output.connect(lambda line: self.log.add(line, "dim"))
        b.ended.connect(lambda code: self.link_ended(kind, b, code))
        self.links[kind] = b
        b.start()
        self.log.add(f"Started: {label}", "you")
        self.tiles_due.start()

    def stop_link(self, kind, wait=False):
        b = self.links.pop(kind, None)
        if b:
            b.stop(wait_ms=2500 if wait else 0)
            self.log.add(f"Stopped: {b.label}", "you")
        self.tiles_due.start()

    def stop_links(self):
        for kind in list(self.links):
            self.stop_link(kind, wait=True)

    def link_ended(self, kind, b, code):
        if self.links.get(kind) is b:
            del self.links[kind]
            self.log.add(f"Ended{'' if code == 0 else f' (exit status {code})'}: "
                         f"{b.label}", "error" if code else "info")
            if code:
                self.show_log()
            self.tiles_due.start()

    # ------------------------------------------------------------ the device

    def ask_install(self):
        start = self.settings.get("dir/firmware", os.path.expanduser("~"))
        path, _ = QtWidgets.QFileDialog.getOpenFileName(
            self, "Install firmware", start, f"{FIRMWARE};;All files (*)")
        if path:
            self.settings.set("dir/firmware", os.path.dirname(path))
            self.install(path)

    def install(self, path):
        if not self.session:
            return
        ok = QtWidgets.QMessageBox.question(
            self, "Install firmware",
            f"Install {short(path)} on {self.name}?\n\nIt is switched off, "
            "its flash programmed, and it is switched on again. The files on "
            "its microSD card stay.")
        if ok != QtWidgets.QMessageBox.Yes:
            return
        self.installing = path
        if self.state == "off":
            self.install_now()
        else:
            self.expect_off = True
            self.stop_links()
            self.session.stop()

    def install_now(self):
        path, self.installing = self.installing, None
        self.set_state("installing")

        def done(code, text):
            self.state = None
            self.set_state("off")
            if code == 0:
                self.power_on()
            else:
                self.show_log()
        self.command("flash", path, then=done)

    # ------------------------------------------------------------ apps, files

    def can_copy(self):
        """its card takes files: through the firmware while it runs, in the
        card's image while it does not"""
        return not self.sd_none and self.state in ("on", "busy", "off", "lost")

    def ask_app(self):
        start = self.settings.get("dir/app", os.path.expanduser("~"))
        path, _ = QtWidgets.QFileDialog.getOpenFileName(
            self, "Install and start an app", start, f"{APPS};;All files (*)")
        if path:
            self.start_app(path)

    def start_app(self, path, remember=True):
        """Copy a .fap to the card and start it (the launcher's `app`)"""
        if remember:
            self.remember("app", path)
            self.update_actions()
        if not self.can_copy():
            self.on_failed(f"{short(path)}: there is no Flipper with a microSD "
                           "card to put it on just now")
        elif not self.is_live():
            self.log.add(f"{short(path)}: the Flipper is off, so it is only "
                         "copied to its card")
            self.command("app", "--no-start", path, limit=W.COPY_MS)
        else:
            self.command("app", "--wait", APP_WAIT_S, path,
                         then=self.app_done, limit=W.COPY_MS)

    def app_done(self, code, text):
        if code:
            self.show_log()             # what the loader and the log said
        elif text:
            self.statusBar().showMessage(text.splitlines()[-1], 8000)

    def app_again(self):
        paths = self.recent("app")
        if paths:
            self.start_app(paths[0], remember=False)

    def close_app(self):
        self.command("app", "--close", then=self.app_done)

    def list_apps(self):
        self.show_log()
        self.command("app", "--list")

    def ask_copy_files(self):
        start = self.settings.get("dir/copy", os.path.expanduser("~"))
        paths, _ = QtWidgets.QFileDialog.getOpenFileNames(
            self, "Copy files to the microSD card", start)
        if paths:
            self.settings.set("dir/copy", os.path.dirname(paths[0]))
            self.ask_copy(paths)

    def ask_copy_folder(self):
        start = self.settings.get("dir/copy", os.path.expanduser("~"))
        path = QtWidgets.QFileDialog.getExistingDirectory(
            self, "Copy a folder to the microSD card", start)
        if path:
            self.settings.set("dir/copy", os.path.dirname(path.rstrip("/")))
            self.ask_copy([path])

    def ask_copy(self, paths):
        """Which folder on the card?  Then copy them there"""
        homed = all(os.path.isfile(p) and os.path.splitext(p)[1].lower()
                    in (*fe_files.HOMES, ".fap") for p in paths)
        what = (short(paths[0].rstrip("/")) if len(paths) == 1
                else f"these {len(paths)}")
        label = f"Copy {what} into this folder on the card:"
        if homed:
            label += ("\n(empty: where the Flipper keeps files of that kind, "
                      "as .sub in /ext/subghz)")
        folder, ok = QtWidgets.QInputDialog.getText(
            self, "Copy to the microSD card", label,
            text="" if homed else self.settings.get("card/folder", "/ext"))
        if ok:
            self.copy_to_card(paths, folder.strip())

    def copy_to_card(self, paths, folder=""):
        """The launcher's `push`: into the folder, or each file where its
        kind lives"""
        if folder and not folder.startswith("/ext"):
            self.on_failed(f"copy: {folder}: a folder on the card starts with "
                           "/ext, as /ext/my_files")
            return
        if not self.can_copy():
            self.on_failed("copy: there is no Flipper with a microSD card to "
                           "copy to just now")
            return
        if folder:
            self.settings.set("card/folder", folder)
        self.command("push", *paths, *([folder.rstrip("/") + "/"]
                                       if folder else []),
                     then=self.app_done, limit=W.COPY_MS)

    def save_screen(self):
        self.save_image(self.view.lcd_picture(4), "screen")

    def save_picture(self):
        self.save_image(self.view.picture(), "flipper")

    def save_image(self, image, what):
        start = os.path.join(self.settings.get("dir/shots",
                                               os.path.expanduser("~")),
                             f"{self.name}-{what}-{time.strftime('%H%M%S')}.png")
        path, _ = QtWidgets.QFileDialog.getSaveFileName(
            self, "Save a picture", start, "PNG (*.png)")
        if path:
            self.settings.set("dir/shots", os.path.dirname(path))
            if image.save(path):
                self.statusBar().showMessage(f"Saved {path}", 5000)
            else:
                self.on_failed(f"could not save {path}")

    def copy_screen(self):
        QtWidgets.QApplication.clipboard().setImage(self.view.lcd_picture(4))
        self.statusBar().showMessage("The screen is on the clipboard", 3000)

    # ---- a recording of the screen (fe_record.py), as an animated GIF

    def set_recording(self, on):
        """Device > Record the screen: on; and off, which asks where to"""
        if on:
            self.start_recording()
            return
        rec = self.stop_recording()
        if rec is None:
            return
        if not rec.shots:
            self.statusBar().showMessage(
                "Nothing was recorded: the screen showed no picture", 5000)
            return
        start = os.path.join(self.settings.get("dir/shots",
                                               os.path.expanduser("~")),
                             f"{self.name}-screen-{time.strftime('%H%M%S')}.gif")
        path, _ = QtWidgets.QFileDialog.getSaveFileName(
            self, f"Save the recording ({rec.seconds():.0f} s)", start,
            "GIF (*.gif)")
        if path:
            self.settings.set("dir/shots", os.path.dirname(path))
            self.save_recording(rec, path)
        else:
            self.statusBar().showMessage("The recording was not saved", 5000)

    def start_recording(self):
        if self.recording is None:
            self.recording = fe_record.Recording()
            self.recording_since = time.monotonic()
            if self.state == "on" and self.view.frame is not None:
                self.recording.add(self.view.frame)     # what it shows now
        self.a_record.setChecked(True)
        self.show_recording()

    def stop_recording(self):
        """-> the Recording that ran, or None"""
        rec, self.recording = self.recording, None
        if rec is not None:
            rec.stop()
        self.a_record.setChecked(False)
        self.show_recording()
        return rec

    def show_recording(self):
        if self.recording is None:
            self.rec_label.setText("")
        else:
            s = int(time.monotonic() - self.recording_since)
            self.rec_label.setText(f"● REC {s // 60}:{s % 60:02d}")

    def on_frame(self, frame):
        rec = self.recording
        if rec is not None:
            rec.add(frame)
            if rec.full():              # (minutes of a screen that never
                # stands still)  The file dialog: not from in here
                QtCore.QTimer.singleShot(0, self.recording_full)

    def recording_full(self):
        if self.recording is not None and self.recording.full():
            self.log.add("The recording is as long as one gets "
                         f"({fe_record.LIMIT} pictures): it ends here.", "info")
            self.set_recording(False)

    def save_recording(self, rec, path):
        """Written by a thread of its own: a long one takes its time"""
        self.statusBar().showMessage(f"Writing {path} …")

        def write():
            try:
                frames, seconds, size = rec.save(path, scale=4, bg=LIT_BG,
                                                 fg=LIT_FG)
                done = (path, f"{frames} picture{'s' if frames != 1 else ''}, "
                              f"{seconds:.1f} s, {max(1, size // 1024)} KB", "")
            except (OSError, ValueError) as e:
                done = (path, "", str(e))
            try:
                self.recorded.emit(*done)
            except RuntimeError:
                pass                    # the window has closed meanwhile

        threading.Thread(target=write, name="recording").start()

    def on_recorded(self, path, what, error):
        if error:
            self.on_failed(f"could not save the recording: {error}")
        else:
            self.statusBar().showMessage(f"Saved {path} ({what})", 8000)
            self.log.add(f"Recorded the screen: {path} ({what})", "info")

    def open_terminal(self):
        cmd = [sys.executable, str(self.launcher), "-d", self.name, "cli"]
        for program, before in TERMINALS:
            if shutil.which(program):
                QtCore.QProcess.startDetached(program, before + cmd)
                return
        self.on_failed("no terminal program found: run `flipperemu -d "
                       f"{self.name} cli` in one")

    def on_port_click(self, zone, pos):
        if zone == "ir":
            self.menu_ir.popup(pos)
        elif zone == "gpio":
            self.show_fwlog()

    def on_context_menu(self, zone, pos):
        menu = {"usb": self.menu_usb, "sd": self.menu_sd,
                "ir": self.menu_ir}.get(zone, self.menu_device)
        menu.popup(pos)

    # ------------------------------------------------------------ the view

    def set_scale(self, k):
        self.view.set_scale(k)
        self.a_scale[k].setChecked(True)
        self.settings.set("scale", k)
        QtCore.QTimer.singleShot(0, self.fit)

    def zoom(self, step):
        i = SCALES.index(self.view.k) if self.view.k in SCALES else 1
        self.set_scale(SCALES[max(0, min(len(SCALES) - 1, i + step))])

    def fit(self):
        self.centralWidget().adjustSize()
        self.adjustSize()

    def set_theme(self, theme):
        self.view.set_theme(theme)
        self.settings.set("theme", theme)

    def set_grid(self, on):
        self.view.set_grid(on)
        self.settings.set("grid", on)

    def show_bench(self, on):
        self.bench.setVisible(on)
        self.settings.set("bench", on)
        QtCore.QTimer.singleShot(0, self.fit)

    def set_on_top(self, on):
        self.settings.set("on_top", on)
        self.setWindowFlag(Qt.WindowStaysOnTopHint, on)
        self.show()

    def about(self):
        QtWidgets.QMessageBox.about(
            self, "About FlipperEmu",
            "<p><b>FlipperEmu</b>: Flipper Zero firmware in QEMU, no Flipper "
            "required.</p><p>This window draws a Flipper of its own around "
            "the emulator's screen; it is not Flipper Devices' artwork, and "
            "FlipperEmu is not made by Flipper Devices.</p>"
            "<p>BSD-3-Clause. github.com/KaraZajac/FlipperEmu</p>")

    # ------------------------------------------------------------ dropping

    def kind_of(self, path):
        p = path.lower()
        for ext, kind in ((".nfc", "nfc"), (".json", "nfc"), (".rfid", "rfid"),
                          (".ibtn", "ibutton"), (".ir", "ir"),
                          (".sub", "subghz")):
            if p.endswith(ext):
                return kind
        if p.endswith((".tgz", ".tar.gz", ".tar", ".dfu", ".elf", ".bin")):
            return "firmware"
        if p.endswith(".fap"):
            return "app"
        return "file" if os.path.exists(path) else None

    def dragEnterEvent(self, e):
        urls = e.mimeData().urls() if e.mimeData().hasUrls() else []
        if any(u.isLocalFile() and self.kind_of(u.toLocalFile()) for u in urls):
            e.acceptProposedAction()

    def dropEvent(self, e):
        others = []                     # of no kind: for the card
        for u in e.mimeData().urls():
            path = u.toLocalFile()
            kind = self.kind_of(path) if path else None
            if kind == "file":
                others.append(path)
            elif kind == "app":
                self.start_app(path)
            elif kind == "firmware":
                if self.session:
                    self.install(path)
                else:
                    self.on_failed("installing firmware needs the window of "
                                   "`flipperemu run`")
            elif kind == "ir":
                self.use_file(kind, path)
            elif kind and self.is_live():
                self.use_file(kind, path)
        if others:                      # (asked when the drop is over)
            QtCore.QTimer.singleShot(0, lambda: self.ask_copy(others))
        e.acceptProposedAction()

    # ------------------------------------------------------------ closing

    def closeEvent(self, e):
        self.closing = True
        self.save_settings()
        for w in list(self.remotes.values()):
            w.close()
        self.stop_links()
        self.runner.stop()
        self.link.close()
        if self.session:
            self.session.stop()
            self.session.tidy()
        e.accept()
        QtWidgets.QApplication.instance().quit()

    def save_settings(self):
        self.settings.set("scale", self.view.k)
        self.settings.set("bench", self.bench.isVisibleTo(self))
        self.settings.set("log", self.log.isVisibleTo(self))
        self.settings.set("fwlog", self.fwlog.isVisibleTo(self))
        self.settings.set("front", "fwlog" if self.front.get(self.fwlog)
                          else "log")


def run(name, directory, launcher, session=None, scale=None):
    """The window, until it is closed; QEMU's exit status, or 0"""
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(
        sys.argv[:1])
    app.setApplicationName("FlipperEmu")
    app.setApplicationDisplayName("FlipperEmu")
    app.setWindowIcon(app_icon())
    win = MainWindow(name, directory, launcher, session, scale=scale)
    win.show()
    win.fit()
    # ^C in the terminal closes the window (and so stops what it owns); the
    # timer lets Python see the signal while Qt waits for events
    signal.signal(signal.SIGINT, lambda *_: win.close())
    wake = QtCore.QTimer(interval=200)
    wake.timeout.connect(lambda: None)
    wake.start()
    app.exec()
    return win.exit_code
