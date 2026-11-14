#!/usr/bin/env python3
# Copyright (c) 2026, Kara Zajac
# SPDX-License-Identifier: BSD-3-Clause
"""A central on the emulated Flipper's Bluetooth: what a phone does, played
by a program.

    ./flipperemu ble scan | gatt | read | write | rpc     (a running device)
    fe_ble.py devices/NAME/ble.sock scan | gatt | ...

The emulated CPU2's BLE stack has a link, devices/NAME/ble.sock.  A program
on it is a central in range of the Flipper: it sees the advertising,
connects, pairs with the PIN that the Flipper shows, and reads, writes and
subscribes to what the firmware's services hold (the device information,
the battery, the serial service that carries the phone app's RPC session).
The link's lines are in docs/RADIO_LINK.md; this is a client of them.

    import fe_ble

    with fe_ble.Central("devices/NAME/ble.sock") as c:
        print(c.advertising()["name"])
        c.connect()
        c.mtu(517)
        c.pair(pin="123456")            # or a function that returns it
        print(c.read(c.char("2a26")))   # the firmware's revision
        print(fe_ble.Rpc(c).ping(b"hello"))

The PIN is on the Flipper's screen (the window shows it), and in its log
("Pass key request event. Pin: ...") unless the Flipper is locked:
`pin_from_log()` reads it there.  The pairing bonds: a central that keeps
its side (`Bonds`) comes back with `encrypt()`, no PIN, until the Flipper
forgets it (Settings > Bluetooth > Unpair All Devices).
"""
import argparse
import dataclasses
import os
import pathlib
import re
import socket
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import fe_expansion as pb                                   # noqa: E402

# The Flipper's serial service (targets/f7/ble_glue/services): the phone
# app's RPC session.  The central writes requests to RX; the Flipper answers
# by indications on TX, one at a time, each confirmed before the next; flow
# control says how many bytes it takes (big-endian), RPC status is 1 while a
# session is open.
SERIAL = "8fe5b3d5-2e7f-4a98-2a48-7acc60fe0000"
SERIAL_TX = "19ed82ae-ed21-4c9d-4145-228e61fe0000"
SERIAL_RX = "19ed82ae-ed21-4c9d-4145-228e62fe0000"
SERIAL_FLOW = "19ed82ae-ed21-4c9d-4145-228e63fe0000"
SERIAL_STATUS = "19ed82ae-ed21-4c9d-4145-228e64fe0000"

NAMES = {
    "1800": "Generic Access", "1801": "Generic Attribute",
    "180a": "Device Information", "180f": "Battery",
    SERIAL: "Flipper serial (RPC)",
    "2a00": "Device Name", "2a01": "Appearance",
    "2a04": "Peripheral Preferred Connection Parameters",
    "2a05": "Service Changed", "2a19": "Battery Level",
    "2a1a": "Battery Power State", "2a25": "Serial Number",
    "2a26": "Firmware Revision", "2a28": "Software Revision",
    "2a29": "Manufacturer Name",
    "03f6666d-ae5e-47c8-8e1a-5d873eb5a933": "RPC version",
    SERIAL_TX: "serial TX (the Flipper's answers)",
    SERIAL_RX: "serial RX (requests)",
    SERIAL_FLOW: "serial flow control (bytes it takes)",
    SERIAL_STATUS: "serial RPC status",
}

ATT_ERRORS = {
    0x01: "invalid handle", 0x02: "read not permitted",
    0x03: "write not permitted",
    0x05: "insufficient authentication (pair first)",
    0x0D: "invalid length",
}
SMP_REASONS = {
    0x01: "passkey entry failed", 0x03: "authentication requirements",
    0x04: "confirm value failed (a wrong PIN)",
    0x0C: "numeric comparison failed",
}
HCI_REASONS = {
    0x05: "authentication failure", 0x08: "connection timeout",
    0x13: "remote user terminated the connection",
    0x16: "connection terminated by the local host",
}
PROPS = ((0x01, "broadcast"), (0x02, "read"), (0x04, "write w/o response"),
         (0x08, "write"), (0x10, "notify"), (0x20, "indicate"))


def fields(line):
    """A line's key=value words"""
    return dict(w.split("=", 1) for w in line.split()[1:] if "=" in w)


def name_of(uuid):
    return NAMES.get(uuid, "")


def props_text(props):
    return ", ".join(name for bit, name in PROPS if props & bit) or "none"


class LinkError(Exception):
    """An `error` line from the link: what was refused, and why"""

    def __init__(self, line):
        self.line = line
        f = fields(line)
        self.op = f.get("op")
        self.code = int(f["code"], 16) if "code" in f else None
        why = (ATT_ERRORS.get(self.code, f"ATT error 0x{self.code:02x}")
               if self.code is not None else f.get("reason", line))
        where = f" at {f['handle']}" if "handle" in f else ""
        super().__init__(f"{self.op}{where}: {why}")


class PairingFailed(Exception):
    def __init__(self, reason):
        self.reason = reason
        super().__init__("the pairing failed: " +
                         SMP_REASONS.get(reason, f"reason 0x{reason:02x}"))


class EncryptionFailed(Exception):
    """The Flipper has no bond with this central (any more): it ends the
    connection, and a phone would pair again"""

    def __init__(self, reason):
        self.reason = reason
        super().__init__(f"the Flipper has no bond with this central "
                         f"({reason}): it was forgotten there")


class Bonds:
    """What a central keeps of the Flipper it has bonded with, as a phone
    does: a small JSON file ({"80:E1:26:...": {"authenticated": "1", ...}})"""

    def __init__(self, path):
        self.path = pathlib.Path(path)

    def _load(self):
        import json
        try:
            return json.loads(self.path.read_text())
        except (OSError, ValueError):
            return {}

    def _save(self, data):
        import json
        if data:
            self.path.write_text(json.dumps(data, indent=2) + "\n")
        else:
            self.path.unlink(missing_ok=True)

    def has(self, flipper):
        return flipper in self._load()

    def add(self, flipper, words):
        data = self._load()
        data[flipper] = words
        self._save(data)

    def forget(self, flipper=None):
        data = self._load() if flipper else {}
        data.pop(flipper, None)
        self._save(data)


def parse_ad(data):
    """Advertising data's AD structures: what a scanner shows"""
    out = {"flags": None, "name": None, "uuids": [], "manufacturer": None,
           "raw": data.hex()}
    at = 0
    while at < len(data) and data[at]:
        size, kind = data[at], data[at + 1] if at + 1 < len(data) else None
        body = data[at + 2:at + 1 + size]
        if kind == 0x01 and body:
            out["flags"] = body[0]
        elif kind in (0x08, 0x09):
            out["name"] = body.decode(errors="replace")
        elif kind in (0x02, 0x03):
            out["uuids"] += [f"{int.from_bytes(body[i:i + 2], 'little'):04x}"
                             for i in range(0, len(body) - 1, 2)]
        elif kind in (0x06, 0x07):
            out["uuids"] += [uuid128(body[i:i + 16])
                             for i in range(0, len(body) - 15, 16)]
        elif kind == 0xFF:
            out["manufacturer"] = body.hex()
        at += 1 + size
    return out


def uuid128(le):
    h = le[::-1].hex()
    return f"{h[:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:]}"


def parse_adv(line):
    f = fields(line)
    adv = {"addr": f.get("addr"), "addr_type": f.get("addr_type"),
           "connectable": f.get("connectable") == "1",
           "interval": f.get("interval")}
    adv.update(parse_ad(bytes.fromhex(f.get("data", ""))))
    scan = parse_ad(bytes.fromhex(f.get("scan", "")))
    adv["scan"] = scan["raw"]
    adv["name"] = adv["name"] or scan["name"]
    adv["manufacturer"] = adv["manufacturer"] or scan["manufacturer"]
    return adv


@dataclasses.dataclass
class Char:
    handle: int                 # its declaration
    value: int                  # its value's handle
    props: int
    perms: int
    uuid: str
    cccd: int = 0
    descs: list = dataclasses.field(default_factory=list)


@dataclasses.dataclass
class Service:
    handle: int
    end: int
    uuid: str
    primary: bool
    chars: list = dataclasses.field(default_factory=list)


class Central:
    """A central on the link: one program, one connection"""

    def __init__(self, path, timeout=10.0, name="fe_ble.py"):
        self.path = str(path)
        self.timeout = timeout
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            self.sock.connect(self.path)
        except OSError as e:
            self.sock.close()
            raise OSError(f"no Bluetooth link at {self.path} ({e.strerror}): "
                          "is the device running, with an emulator that has "
                          "one (2026-11-14 on)?") from None
        self.buf = b""
        self.adv = None             # the advertising, while there is some
        self.connected = None       # the `connected` line's words
        self.disconnected = None    # the last end: (by, reason)
        self.security_request = None    # the Flipper's, on this connection
        self.notes = []             # (kind, handle, bytes) not yet taken
        self.services = []
        self.agreed_mtu = 23
        self.auto_ack = True        # confirm each indication at once
        self.say(f"hello {name}")
        self._until(lambda l: l.startswith("hello flipperemu ble"),
                    "hello from the link")

    def close(self):
        self.sock.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # ---- the lines

    def say(self, line):
        self.sock.sendall(line.encode() + b"\n")

    def _line(self, deadline):
        while b"\n" not in self.buf:
            # (past the deadline: what is in the socket already, no wait)
            self.sock.settimeout(max(deadline - time.monotonic(), 0))
            try:
                got = self.sock.recv(65536)
            except (socket.timeout, BlockingIOError):
                return None
            if not got:
                raise EOFError("the Bluetooth link closed (the device stopped?)")
            self.buf += got
        line, self.buf = self.buf.split(b"\n", 1)
        return line.decode(errors="replace").strip()

    def _take(self, line):
        """What a line says, whoever waits for something else"""
        w = line.split()
        if not w:
            return
        if w[0] == "adv":
            self.adv = parse_adv(line) if w[1:2] == ["on"] else None
        elif w[0] in ("notify", "indicate") and len(w) >= 2:
            data = bytes.fromhex(w[2]) if len(w) > 2 else b""
            self.notes.append((w[0], int(w[1], 16), data))
            if w[0] == "indicate" and self.auto_ack:
                self.say("ack")
        elif w[0] == "connected":
            self.connected = fields(line)
            self.agreed_mtu = int(self.connected.get("mtu", "23"))
            self.disconnected = None
            self.security_request = None
        elif w[0] == "security-request":
            self.security_request = fields(line)    # the Flipper asks to pair
        elif w[0] == "disconnected":
            f = fields(line)
            self.disconnected = (f.get("by"), int(f.get("reason", "0"), 16))
            self.connected = None
            self.agreed_mtu = 23

    def _until(self, match, what, timeout=None, op=None):
        """Lines, until one that `match` takes (it is returned); an error
        line for `op` raises"""
        timeout = self.timeout if timeout is None else timeout
        deadline = time.monotonic() + timeout
        while True:
            line = self._line(deadline)
            if line is None:
                raise TimeoutError(f"no {what} within {timeout:g} s")
            self._take(line)
            if op and line.startswith(f"error op={op}"):
                raise LinkError(line)
            if match(line):
                return line

    def pump(self, timeout=0.0):
        """Take what has come (the advertising, notifications), waiting up
        to `timeout` seconds for more"""
        deadline = time.monotonic() + timeout
        while self._line_taken(deadline):
            pass

    def _line_taken(self, deadline):
        line = self._line(deadline)
        if line is None:
            return False
        self._take(line)
        return True

    # ---- GAP

    def advertising(self, timeout=None):
        """The advertising once there is some: addr, name, uuids, ..."""
        self.pump()
        if self.adv is None:
            self._until(lambda l: l.startswith("adv on"),
                        "advertising (is Bluetooth on? Settings > Bluetooth)",
                        timeout)
        return self.adv

    def connect(self, addr=None, interval=None, timeout=None):
        self.advertising(timeout)
        line = "connect"
        if addr:
            line += f" addr={addr}"
        if interval:
            line += f" interval={interval}"
        self.say(line)
        self._until(lambda l: l.startswith("connected "), "connection",
                    op="connect")
        return self.connected

    def disconnect(self, timeout=None):
        self.say("disconnect")
        self._until(lambda l: l.startswith("disconnected"), "disconnection",
                    timeout, op="disconnect")

    def wait_disconnected(self, timeout=None):
        """Until the Flipper ends the connection: (by, reason)"""
        if self.connected:
            self._until(lambda l: l.startswith("disconnected"),
                        "end of the connection", timeout)
        return self.disconnected

    def mtu(self, n=517):
        self.say(f"mtu {n}")
        line = self._until(lambda l: l.startswith("mtu "), "the MTU",
                           op="mtu")
        self.agreed_mtu = int(line.split()[1])
        return self.agreed_mtu

    def pair(self, pin=None, confirm=None, io=None, timeout=60):
        """Pair as a phone does.  pin: the six digits, or a function called
        for them when the Flipper shows them (None from it: cancel);
        confirm(value) -> bool for a numeric comparison.  -> the `paired`
        line's words; PairingFailed with SMP's reason otherwise"""
        self.say("pair" + (f" io={io}" if io else ""))
        while True:
            line = self._until(lambda l: l.startswith(("pairing", "paired")),
                               "the pairing", timeout, op="pair")
            if line.startswith("paired"):
                return fields(line)
            if line.startswith("pairing failed"):
                raise PairingFailed(int(fields(line).get("reason", "0"), 16))
            if line.startswith("pairing passkey"):
                key = pin() if callable(pin) else pin
                self.say("passkey cancel" if key is None
                         else f"passkey {int(key):06d}")
            elif line.startswith("pairing compare"):
                value = line.split()[2]
                ok = confirm(value) if confirm else True
                self.say(f"confirm {'yes' if ok else 'no'}")

    def encrypt(self, timeout=None):
        """A central with a bond: encryption with the key it keeps, no PIN.
        -> the `encrypted` line's words; EncryptionFailed when the Flipper
        has none (it ends the connection then)"""
        self.say("encrypt")
        line = self._until(lambda l: l.startswith(("encrypted",
                                                   "encryption failed")),
                           "the encryption", timeout, op="encrypt")
        if line.startswith("encryption failed"):
            raise EncryptionFailed(fields(line).get("reason", "?"))
        return fields(line)

    # ---- ATT / GATT

    def gatt(self):
        """The database, as discovery finds it: services with their
        characteristics"""
        self.say("gatt")
        services = []
        while True:
            line = self._until(lambda l: l.split()[:1] in (
                ["service"], ["char"], ["cccd"], ["desc"], ["gatt"]),
                "the GATT table", op="gatt")
            w, f = line.split(), fields(line)
            if w[0] == "gatt":
                break
            handle = int(w[1], 16)
            if w[0] == "service":
                services.append(Service(handle, int(f["end"], 16), f["uuid"],
                                        f.get("primary") == "1"))
            elif w[0] == "char" and services:
                services[-1].chars.append(Char(
                    handle, int(f["value"], 16), int(f["props"], 16),
                    int(f["perms"], 16), f["uuid"]))
            elif w[0] == "cccd" and services and services[-1].chars:
                services[-1].chars[-1].cccd = handle
            elif w[0] == "desc" and services and services[-1].chars:
                services[-1].chars[-1].descs.append((handle, f.get("uuid")))
        self.services = services
        return services

    def char(self, uuid):
        """A characteristic by its UUID ("2a26", or the 128-bit form)"""
        if not self.services:
            self.gatt()
        for svc in self.services:
            for c in svc.chars:
                if c.uuid == uuid.lower():
                    return c
        raise KeyError(f"no characteristic {uuid}")

    def read(self, target):
        handle = target.value if isinstance(target, Char) else int(target)
        self.say(f"read 0x{handle:04x}")
        line = self._until(lambda l: l.startswith(f"value 0x{handle:04x}"),
                           "the value", op="read")
        w = line.split()
        return bytes.fromhex(w[2]) if len(w) > 2 else b""

    def write(self, target, data, response=True):
        handle = target.value if isinstance(target, Char) else int(target)
        if response:
            self.say(f"write 0x{handle:04x} {bytes(data).hex()}")
            self._until(lambda l: l.startswith(f"written 0x{handle:04x}"),
                        "the write's answer", op="write")
        else:
            self.say(f"write-cmd 0x{handle:04x} {bytes(data).hex()}")

    def subscribe(self, char, indicate=False, on=True):
        """The characteristic's CCCD: notifications, or indications"""
        if not char.cccd:
            raise KeyError(f"{char.uuid} has no CCCD: it neither notifies nor "
                           "indicates")
        value = (2 if indicate else 1) if on else 0
        self.write(char.cccd, value.to_bytes(2, "little"))

    def take(self, handle=None, timeout=None):
        """The next notification or indication (of `handle`): (kind, handle,
        bytes)"""
        timeout = self.timeout if timeout is None else timeout
        deadline = time.monotonic() + timeout
        while True:
            for i, (kind, h, data) in enumerate(self.notes):
                if handle is None or h == handle:
                    return self.notes.pop(i)
            if not self._line_taken(deadline):
                raise TimeoutError(f"nothing from the Flipper within "
                                   f"{timeout:g} s")


class Rpc:
    """The phone app's RPC session, through the serial service"""

    def __init__(self, central, timeout=10.0):
        c = self.c = central
        self.timeout = timeout
        self.tx = c.char(SERIAL_TX)
        self.rx = c.char(SERIAL_RX)
        self.flow = c.char(SERIAL_FLOW)
        self.status = c.char(SERIAL_STATUS)
        c.subscribe(self.tx, indicate=True)
        c.subscribe(self.flow)
        c.subscribe(self.status)
        self.credit = int.from_bytes(c.read(self.flow)[:4], "big")
        self.stream = b""
        self.command = 0

    def _absorb(self, timeout=0.0):
        """Indications and notifications that came: the stream, the credit"""
        c = self.c
        c.pump(timeout)
        keep = []
        for kind, handle, data in c.notes:
            if handle == self.tx.value:
                self.stream += data
            elif handle == self.flow.value:
                self.credit = int.from_bytes(data[:4], "big")
            elif handle != self.status.value:
                keep.append((kind, handle, data))
        c.notes = keep

    def send(self, frame):
        """Bytes to the Flipper's RPC session, as far as its flow control
        lets them through, in writes the MTU carries"""
        at, deadline = 0, time.monotonic() + self.timeout
        while at < len(frame):
            self._absorb()
            if self.credit <= 0:
                if time.monotonic() > deadline:
                    raise TimeoutError("the Flipper takes no more bytes (its "
                                       "flow control stays at 0)")
                self._absorb(0.1)
                continue
            n = min(len(frame) - at, self.c.agreed_mtu - 3, self.credit)
            self.c.write(self.rx, frame[at:at + n])
            self.credit -= n
            at += n

    def answers(self, command):
        """The answers to a command, up to the one without has_next"""
        out, deadline = [], time.monotonic() + self.timeout
        while True:
            got = pb.message(self.stream)
            if got:
                main, self.stream = got
                if main.get(1, 0) == command:
                    out.append(main)
                    if not main.get(3):
                        return out
                continue
            if time.monotonic() > deadline:
                raise TimeoutError(f"no answer to RPC command {command} "
                                   f"within {self.timeout:g} s")
            self._absorb(0.1)

    def request(self, field, payload=b""):
        """A request of PB.Main: its field and its message's bytes"""
        self.command += 1
        main = (b"\x08" + pb.varint(self.command) + pb.varint(field << 3 | 2)
                + pb.varint(len(payload)) + payload)
        self.send(pb.varint(len(main)) + main)
        return self.answers(self.command)

    def ping(self, data=b""):
        """system_ping_request (5) -> what the answer carries"""
        answer = self.request(5, b"\x0a" + pb.varint(len(data)) + data)[-1]
        status = answer.get(2, 0)
        if status:
            raise RuntimeError(f"RPC status {status}")
        return pb.fields(answer.get(6, b"")).get(1, b"")

    def device_info(self):
        """system_device_info_request (32) -> its keys and values"""
        info = {}
        for answer in self.request(32):
            kv = pb.fields(answer.get(33, b""))
            key = kv.get(1, b"").decode(errors="replace")
            if key:
                info[key] = kv.get(2, b"").decode(errors="replace")
        return info


def pin_from_log(log, after=0, timeout=5.0):
    """The PIN that the firmware logged after offset `after` of its log
    ("Pass key request event. Pin: 123456"), waiting up to timeout; None:
    not there (a locked Flipper logs ******, a log level of none nothing)"""
    deadline = time.monotonic() + timeout
    while True:
        try:
            with open(log, "rb") as f:
                f.seek(after)
                text = f.read().decode(errors="replace")
        except OSError:
            text = ""
        hit = re.findall(r"Pass key request event\. Pin: (\d{6})", text)
        if hit:
            return hit[-1]
        if time.monotonic() > deadline:
            return None
        time.sleep(0.1)


def log_size(log):
    try:
        return os.path.getsize(log)
    except OSError:
        return 0


def pin_source(how, log):
    """What `--pin` says: the digits, `ask` (the terminal), `log` (the
    firmware's log): a function for Central.pair()"""
    if how and re.fullmatch(r"\d{6}", how):
        return lambda: how
    mark = log_size(log) if log else 0
    if how == "log" or (how is None and not sys.stdin.isatty() and log):
        def from_log():
            pin = pin_from_log(log, mark)
            if pin is None:
                print("no PIN in the firmware's log (a locked Flipper, or its "
                      "log level): --pin ask, or the six digits", file=sys.stderr)
            return pin
        return from_log

    def ask():
        try:
            got = input("The PIN on the Flipper's screen: ").strip()
        except EOFError:
            return None
        return got if re.fullmatch(r"\d{6}", got) else None
    return ask


def connected_central(path, pin=None, log=None, timeout=10.0, mtu=517,
                      quiet=False, bonds=None):
    """A central that has connected, and paired or encrypted: what `gatt`,
    `read`, `write` and `rpc` start with.  bonds: a Bonds, kept as a phone
    keeps them (a central with a bond encrypts, no PIN; one that the
    Flipper has forgotten forgets it too, and pairs again)"""
    def say(text):
        if not quiet:
            print(text, file=sys.stderr)

    c = Central(path, timeout)
    try:
        adv = c.advertising(timeout)
        c.connect()
        c.mtu(mtu)
        if bonds and bonds.has(adv["addr"]):
            try:
                c.encrypt()
                say(f"connected to {adv.get('name') or '?'} ({adv['addr']}), "
                    "with the bond from before: no PIN")
                return c
            except EncryptionFailed as e:
                say(f"{e}: pairing again")
                bonds.forget(adv["addr"])
                c.wait_disconnected(10)
                adv = c.advertising(15)
                c.connect()
                c.mtu(mtu)
        say(f"connected to {adv.get('name') or '?'} ({adv['addr']}); "
            "pairing: the Flipper shows a PIN")
        paired = c.pair(pin_source(pin, log))
        if bonds and paired.get("bonded") == "1":
            bonds.add(adv["addr"], paired)
    except BaseException:
        c.close()
        raise
    return c


def show_adv(adv):
    flags = adv.get("flags")
    print(f"address       {adv['addr']} ({adv['addr_type']})")
    print(f"name          {adv.get('name') or '-'}")
    print(f"connectable   {'yes' if adv['connectable'] else 'no'}")
    print(f"services      {', '.join(adv['uuids']) or '-'}")
    if flags is not None:
        print(f"flags         0x{flags:02x}"
              + (" (LE General Discoverable, no BR/EDR)" if flags == 0x06
                 else ""))
    if adv.get("manufacturer"):
        print(f"manufacturer  {adv['manufacturer']}")
    lo, _, hi = (adv.get("interval") or "").partition("-")
    if lo and hi:
        print(f"interval      {int(lo) * 0.625:g} to {int(hi) * 0.625:g} ms")


# characteristics whose value is UTF-8 text, as the Bluetooth SIG's are
TEXT = {"2a00", "2a24", "2a25", "2a26", "2a27", "2a28", "2a29",
        "03f6666d-ae5e-47c8-8e1a-5d873eb5a933"}


def show_value(data, uuid=None):
    text = data.decode(errors="replace")
    if uuid == "2a19" and len(data) == 1:
        return f"{data[0]} %"
    if data and text.isprintable() and (uuid in TEXT or len(data) >= 3):
        return f'"{text}"'
    return data.hex(" ") or "(empty)"


def show_gatt(c, values=True):
    for svc in c.services:
        print(f"service {svc.uuid}  {name_of(svc.uuid)}  "
              f"[0x{svc.handle:04x}-0x{svc.end:04x}]")
        for ch in svc.chars:
            line = (f"  {ch.uuid}  {name_of(ch.uuid)}  "
                    f"0x{ch.value:04x}  ({props_text(ch.props)})")
            if values and ch.props & 0x02:
                try:
                    line += f"  = {show_value(c.read(ch), ch.uuid)}"
                except LinkError as e:
                    line += f"  ({e})"
            print(line)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("link", help="devices/NAME/ble.sock")
    ap.add_argument("action", choices=("scan", "gatt", "read", "write", "rpc",
                                       "forget"))
    ap.add_argument("what", nargs="*", help="read: a characteristic's UUID "
                    "(2a26) or a handle (0x0010); write: that and the bytes "
                    "in hex")
    ap.add_argument("--pin", help="the six digits; `ask`; `log`: the "
                    "firmware's log (--log)")
    ap.add_argument("--log", help="the firmware's log (devices/NAME/uart.log)")
    ap.add_argument("--bonds", help="where this central keeps its bonds (a "
                    "JSON file): with one, it comes back without a PIN")
    ap.add_argument("--timeout", type=float, default=10.0)
    args = ap.parse_args(argv)
    return run(args.link, args.action, args.what, args.pin, args.log,
               args.timeout, bonds=args.bonds)


def run(link, action, what=(), pin=None, log=None, timeout=10.0,
        as_json=False, bonds=None):
    """The launcher's `ble` actions.  bonds: the file in which this central
    keeps its bonds, or None (it pairs every time)"""
    import json
    bonds = Bonds(bonds) if bonds else None
    try:
        if action == "forget":
            if bonds:
                bonds.forget()
            print("this central has forgotten its bonds: it pairs again "
                  "next time (the Flipper's side: Settings > Bluetooth > "
                  "Unpair All Devices)")
            return 0
        if action == "scan":
            with Central(link, timeout) as c:
                adv = c.advertising(timeout)
                if as_json:
                    print(json.dumps(adv, indent=2))
                else:
                    show_adv(adv)
            return 0
        with connected_central(link, pin, log, timeout, quiet=as_json,
                               bonds=bonds) as c:
            c.gatt()
            if action == "gatt":
                show_gatt(c)
            elif action in ("read", "write"):
                if not what or (action == "write" and len(what) != 2):
                    sys.exit("read UUID|HANDLE, write UUID|HANDLE HEX")
                # a handle is 0x...; anything else a UUID ("2a26", ...)
                target = (int(what[0], 16)
                          if re.fullmatch(r"0x[0-9a-fA-F]{1,4}", what[0])
                          else c.char(what[0]))
                if action == "read":
                    data = c.read(target)
                    print(data.hex() if as_json else show_value(
                        data, target.uuid if isinstance(target, Char) else None))
                else:
                    c.write(target, bytes.fromhex(what[1]))
                    print("written")
            elif action == "rpc":
                rpc = Rpc(c, timeout)
                said = b"FlipperEmu ble rpc"
                back = rpc.ping(said)
                info = rpc.device_info()
                if as_json:
                    print(json.dumps({"ping": back == said, "info": info},
                                     indent=2))
                else:
                    print(f"RPC over Bluetooth: a ping came back "
                          f"{'whole' if back == said else 'changed: ' + repr(back)}")
                    for key in ("hardware_name", "firmware_version",
                                "firmware_commit", "firmware_build_date",
                                "radio_stack_major", "radio_stack_minor",
                                "protobuf_version_major",
                                "protobuf_version_minor"):
                        if key in info:
                            print(f"  {key:24s} {info[key]}")
            c.disconnect()
        return 0
    except (PairingFailed, EncryptionFailed) as e:
        print(e, file=sys.stderr)
        return 1
    except (LinkError, KeyError, TimeoutError, EOFError, OSError) as e:
        print(e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
