# Copyright (c) 2026, Kara Zajac
# SPDX-License-Identifier: BSD-3-Clause
"""The window's phone: a central on the emulated Flipper's Bluetooth link
(scripts/fe_ble.py), for the Bluetooth tile of the bench.

It is the same phone as `./flipperemu ble`'s: it keeps its bonds in the
device's ble-central.json, so a Flipper that one of them has paired with
lets the other in with no PIN.  Its work goes on in a thread of its own (a
pairing waits for the user, an RPC answer for the Flipper); what it has to
say comes to the window as signals.  One central at a time is on the link:
while this one is connected, `./flipperemu ble` waits its turn.
"""
import queue
import threading

from PySide6 import QtCore

import fe_ble

PING = b"FlipperEmu window"
INFO_KEYS = ("hardware_name", "firmware_version", "firmware_commit",
             "radio_stack_major", "radio_stack_minor")


class Phone(QtCore.QObject):
    said = QtCore.Signal(str, str)      # a line for the console, and its kind
    changed = QtCore.Signal()           # connected or not, busy or not
    pin_wanted = QtCore.Signal()        # the PIN on the Flipper's screen, please
    pin_over = QtCore.Signal()          # ... no longer wanted (an answer came,
    #                                     or the phone gave up waiting)

    def __init__(self, sock, bonds, parent=None):
        super().__init__(parent)
        self.sock = str(sock)
        self.bonds = fe_ble.Bonds(bonds)
        self.jobs = queue.Queue()
        self.pins = queue.Queue()
        self.central = None
        self.rpc = None
        self.connected = False
        self.busy = False
        threading.Thread(target=self._run, daemon=True,
                         name="window-ble-phone").start()

    # ---- what the window asks for (any of them comes back as a signal)

    def connect_(self):
        self.jobs.put("connect")

    def disconnect_(self):
        self.jobs.put("disconnect")

    def ping(self):
        self.jobs.put("ping")

    def info(self):
        self.jobs.put("info")

    def forget(self):
        self.jobs.put("forget")

    def close(self):
        self.jobs.put("close")

    def give_pin(self, pin):
        """The window's answer to pin_wanted: six digits, or None"""
        self.pins.put(pin)

    # ---- in the thread

    def _run(self):
        while True:
            try:
                job = self.jobs.get(timeout=0.2)
            except queue.Empty:
                job = None
            if job == "close":
                self._drop()
                return
            if job:
                self.busy = True
                self.changed.emit()
            try:
                if job:
                    getattr(self, "_" + job)()
                elif self.central:
                    # indications to confirm, and the Flipper's own end
                    self.central.pump(0.05)
                    if not self.central.connected:
                        by, reason = self.central.disconnected or (None, 0)
                        self.said.emit("Bluetooth: the Flipper ended the "
                                       "connection (" + fe_ble.HCI_REASONS.get(
                                           reason, f"0x{reason:02x}") + ")",
                                       "info")
                        self._drop()
            except EOFError:
                # the emulator went away under the connection: switched
                # off, or loaded from a snapshot
                if self.central:
                    self.said.emit("Bluetooth: the phone's connection ended "
                                   "with the emulator", "info")
                self._drop()
            except (OSError, TimeoutError, fe_ble.LinkError,
                    fe_ble.PairingFailed, fe_ble.EncryptionFailed) as e:
                self.said.emit(f"Bluetooth: {e}", "error")
                self._drop()
            finally:
                if job:
                    self.busy = False
                    self.changed.emit()

    def _ask_pin(self):
        while not self.pins.empty():    # (an answer that came too late)
            self.pins.get_nowait()
        self.pin_wanted.emit()
        try:
            return self.pins.get(timeout=180)
        except queue.Empty:
            return None
        finally:
            self.pin_over.emit()

    def _drop(self):
        if self.central:
            try:
                self.central.close()
            except OSError:
                pass
        self.central = self.rpc = None
        if self.connected:
            self.connected = False
            self.changed.emit()

    def _connect(self):
        if self.central:
            return
        try:
            c = fe_ble.connected_central(self.sock, pin=self._ask_pin,
                                         timeout=10.0, quiet=True,
                                         bonds=self.bonds)
        except TimeoutError as e:
            if "hello" not in str(e):
                raise
            # the link takes one central at a time, and waits with the
            # next until the one before has gone
            raise fe_ble.LinkError(
                "its link did not answer: another central is on it "
                "(./flipperemu ble, or a program of yours). One at a "
                "time") from e
        self.central, self.connected = c, True
        name = (c.adv or {}).get("name") or "the Flipper"
        if getattr(c, "how", "") == "bond":
            self.said.emit(f"Bluetooth: connected to {name} as a phone, with "
                           "the bond from before: no PIN", "ok")
        else:
            self.said.emit(f"Bluetooth: connected to {name} as a phone, "
                           "paired with the PIN it showed; the bond is kept "
                           "(next time no PIN)", "ok")
        self.changed.emit()

    def _disconnect(self):
        if self.central:
            self.central.disconnect()
            self.said.emit("Bluetooth: the phone disconnected", "info")
        self._drop()

    def _rpc_session(self):
        if not self.central:
            raise OSError("not connected: Connect first")
        if not self.rpc:
            self.rpc = fe_ble.Rpc(self.central, timeout=20.0)
        return self.rpc

    def _ping(self):
        back = self._rpc_session().ping(PING)
        self.said.emit("Bluetooth: an RPC ping went through the serial "
                       "service and came back " +
                       ("whole" if back == PING else f"changed: {back!r}"),
                       "ok" if back == PING else "error")

    def _info(self):
        info = self._rpc_session().device_info()
        self.said.emit("Bluetooth, RPC device info: " + "; ".join(
            f"{k} {info[k]}" for k in INFO_KEYS if k in info)
            + f" ({len(info)} keys)", "info")

    def _forget(self):
        self.bonds.forget()
        self.said.emit("Bluetooth: the phone has forgotten its bonds: it pairs "
                       "again next time (the Flipper's side: Settings > "
                       "Bluetooth > Unpair All Devices)", "info")
