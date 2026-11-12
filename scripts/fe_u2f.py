#!/usr/bin/env python3
# Copyright (c) 2026, Kara Zajac
# SPDX-License-Identifier: BSD-3-Clause
"""A U2F host for an emulated Flipper, in user space.

A browser or libfido2 reaches a U2F token through the kernel: usbhid, hidraw.
This does the same over USB/IP with no kernel in between (fe_usbip.UsbipClient
plays vhci-hcd's part), so it needs no root and plugs nothing into this PC:
the HID interface and its report descriptor, U2FHID's frames (INIT, PING,
WINK, MSG) on the two interrupt endpoints, and U2F's raw messages (VERSION,
REGISTER, AUTHENTICATE) with their signatures checked on NIST P-256.

    ./flipperemu -d mine run --usbip          # then open U2F on the Flipper
    scripts/fe_u2f.py                         # what the token says it is
    scripts/fe_u2f.py register > handle.txt   # press OK on the Flipper
    scripts/fe_u2f.py login < handle.txt      # and once more

The curve's arithmetic is here in plain Python, as the AES is in
fe_enclave.py: a check of what the firmware signed, not something to keep a
secret with (it takes no care about timing).
"""
import argparse
import collections
import hashlib
import os
import select
import struct
import sys
import time

import fe_usbip

# ---- NIST P-256 (secp256r1)
P = 0xFFFFFFFF00000001000000000000000000000000FFFFFFFFFFFFFFFFFFFFFFFF
N = 0xFFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551
B = 0x5AC635D8AA3A93E7B3EBBD55769886BC651D06B0CC53B0F63BCE3C3E27D2604B
G = (0x6B17D1F2E12C4247F8BCE6E563A440F277037D812DEB33A0F4A13945D898C296,
     0x4FE342E2FE1A7F9B8EE7EB4A7C0F9E162BCE33576B315ECECBB6406837BF51F5)


def on_curve(pt):
    x, y = pt
    return 0 <= x < P and 0 <= y < P and (y * y - (x * x * x - 3 * x + B)) % P == 0


def _add(a, b):
    """The sum of two points; None is the point at infinity."""
    if a is None or b is None:
        return a or b
    (x1, y1), (x2, y2) = a, b
    if x1 == x2:
        if (y1 + y2) % P == 0:
            return None
        k = (3 * x1 * x1 - 3) * pow(2 * y1, -1, P) % P
    else:
        k = (y2 - y1) * pow(x2 - x1, -1, P) % P
    x = (k * k - x1 - x2) % P
    return x, (k * (x1 - x) - y1) % P


def _mul(k, pt):
    out = None
    while k:
        if k & 1:
            out = _add(out, pt)
        pt = _add(pt, pt)
        k >>= 1
    return out


def public_key(private: bytes) -> bytes:
    """The point of a private key, as U2F writes one: 04 X Y."""
    d = int.from_bytes(private, "big")
    if not 0 < d < N:
        raise ValueError("no private key on P-256")
    return point_bytes(_mul(d, G))


def point_bytes(pt) -> bytes:
    return b"\x04" + pt[0].to_bytes(32, "big") + pt[1].to_bytes(32, "big")


def point(raw: bytes):
    """A point from 04 X Y; ValueError unless it is on the curve."""
    if len(raw) != 65 or raw[0] != 4:
        raise ValueError("a public key is 04 and two numbers of 32 bytes")
    pt = (int.from_bytes(raw[1:33], "big"), int.from_bytes(raw[33:], "big"))
    if not on_curve(pt):
        raise ValueError("the public key is no point of P-256")
    return pt


def verify(public: bytes, message: bytes, signature: bytes) -> bool:
    """An ECDSA signature (DER, as U2F has it) over SHA-256 of a message."""
    try:
        pt = point(public)
        r, s = der_signature(signature)
    except ValueError:
        return False
    if not (0 < r < N and 0 < s < N):
        return False
    z = int.from_bytes(hashlib.sha256(message).digest(), "big")
    w = pow(s, -1, N)
    got = _add(_mul(z * w % N, G), _mul(r * w % N, pt))
    return got is not None and got[0] % N == r


# ---- DER, as far as a signature and a certificate need it
def der(data: bytes, at=0):
    """One element at an offset: (tag, contents, the offset after it)."""
    if at + 2 > len(data):
        raise ValueError("DER: cut short")
    tag, n, at = data[at], data[at + 1], at + 2
    if n & 0x80:
        k = n & 0x7F
        if not 1 <= k <= 3 or at + k > len(data):
            raise ValueError("DER: a length that is none")
        n, at = int.from_bytes(data[at:at + k], "big"), at + k
    if at + n > len(data):
        raise ValueError("DER: cut short")
    return tag, data[at:at + n], at + n


def der_all(data: bytes):
    """The elements of a sequence's contents: [(tag, contents)]."""
    out, at = [], 0
    while at < len(data):
        tag, body, at = der(data, at)
        out.append((tag, body))
    return out


def der_signature(sig: bytes):
    tag, body, end = der(sig)
    parts = der_all(body)
    if tag != 0x30 or end != len(sig) or [t for t, _ in parts] != [2, 2]:
        raise ValueError("no ECDSA signature in DER")
    return tuple(int.from_bytes(v, "big") for _, v in parts)


def _name(body: bytes) -> str:
    """An X.509 name as text: C=US, CN=..."""
    short = {"2.5.4.3": "CN", "2.5.4.6": "C", "2.5.4.10": "O", "2.5.4.11": "OU"}
    out = []
    for _, rdn in der_all(body):
        for _, pair in der_all(rdn):
            (_, oid), (_, value) = der_all(pair)[:2]
            dotted = f"{oid[0] // 40}.{oid[0] % 40}" + "".join(
                f".{b}" for b in oid[1:] if b < 0x80)
            out.append(f"{short.get(dotted, dotted)}="
                       f"{value.decode('utf-8', 'replace')}")
    return ", ".join(out)


def certificate(cert: bytes) -> dict:
    """What a host needs of an X.509 certificate with a P-256 key: its
    names, its public key, and the part that is signed with its signature."""
    try:
        tag, body, end = der(cert)
        if tag != 0x30 or end != len(cert):
            raise ValueError
        (_, tbs), _, (_, sig) = der_all(body)[:3]
        tbs_raw = body[:der(body)[2]]       # with its tag and length
        fields = der_all(tbs)
        if fields[0][0] == 0xA0:            # [0] version
            fields = fields[1:]
        issuer, subject, spki = fields[2][1], fields[4][1], fields[5][1]
        key = der_all(spki)[1][1]           # BIT STRING: 00, then the point
        return {"issuer": _name(issuer), "subject": _name(subject),
                "public": bytes(key[1:]), "signed": bytes(tbs_raw),
                "signature": bytes(sig[1:])}
    except (ValueError, IndexError):
        raise ValueError("no X.509 certificate") from None


# ---- the HID interface, over USB/IP
FIDO_PAGE = bytes.fromhex("06d0f1")         # Usage Page (FIDO alliance, F1D0)
FIDO_U2F = bytes.fromhex("0901")            # Usage (U2F authenticator)
PACKET = 64


class NoToken(Exception):
    """What is on the port is no U2F token (or nothing is)."""


class Hid:
    """The USB device behind a USB/IP port, taken as a PC takes a HID device:
    descriptors, SET_CONFIGURATION, SET_IDLE, the report descriptor, and then
    reports of 64 bytes on its two interrupt endpoints."""

    def __init__(self, port=3240, host="127.0.0.1", timeout=10, appear=5):
        # appear: seconds to wait for the device to be on the bus (an app
        # that has just taken the port, a host that has just let go of it)
        end = time.monotonic() + appear
        while True:
            try:
                if any(d["busid"] == "1-1" for d in
                       fe_usbip.devlist(host, port, timeout=timeout)):
                    self.c = fe_usbip.UsbipClient(host=host, port=port,
                                                  timeout=timeout)
                    break
                why = "the Flipper's USB port is not on the bus now"
            except (ConnectionRefusedError, TimeoutError) as e:
                raise NoToken(f"nothing answers USB/IP on {host}:{port} "
                              f"({e.strerror or e}): the device runs, and was "
                              "started with `run --usbip`?") from e
            except ConnectionError as e:        # gone between the two
                why = str(e)
            except OSError as e:
                raise NoToken(f"no USB/IP on {host}:{port}: {e}") from e
            if time.monotonic() >= end:
                raise NoToken(why)
            time.sleep(0.2)
        self.late, self.packets = [], collections.deque()
        self.reading = None
        try:
            self.enumerate()
        except Exception:
            self.close()
            raise

    def control(self, *a, **kw):
        return self.c.control(*a, pending=self.late, **kw)

    def enumerate(self):
        dd = self.control(0x80, 6, 0x0100, 0, 18)
        self.vid, self.pid = struct.unpack_from("<HH", dd, 8)
        head = self.control(0x80, 6, 0x0200, 0, 9)
        cfg = self.control(0x80, 6, 0x0200, 0, struct.unpack_from("<H", head, 2)[0])
        self.interface = self.ep_in = self.ep_out = None
        self.report_len, at, cls = 0, 0, None
        while at + 2 <= len(cfg) and cfg[at]:
            n, kind = cfg[at], cfg[at + 1]
            d = cfg[at:at + n]
            if kind == 4:                               # interface
                cls = d[5]
                if cls == 3 and self.interface is None:
                    self.interface = d[2]
            elif kind == 0x21 and cls == 3 and not self.report_len:
                self.report_len = struct.unpack_from("<H", d, 7)[0]
            elif kind == 5 and cls == 3 and d[3] & 3 == 3:  # interrupt endpoint
                if d[2] & 0x80:
                    self.ep_in = self.ep_in or d[2] & 0xF
                else:
                    self.ep_out = self.ep_out or d[2] & 0xF
            at += n
        if self.interface is None:
            raise NoToken(f"the USB device there ({self.vid:04x}:{self.pid:04x}) "
                          "has no HID interface: U2F is open on the Flipper?")
        self.control(0x00, 9, cfg[5], 0)                # SET_CONFIGURATION
        self.control(0x21, 0x0A, 0, self.interface)     # SET_IDLE
        self.report = self.control(0x81, 6, 0x2200, self.interface,
                                   self.report_len)
        if not (self.report.startswith(FIDO_PAGE + FIDO_U2F)
                and self.ep_in and self.ep_out):
            raise NoToken("the HID device there is no U2F token (its report "
                          "descriptor has another usage): U2F is open on the "
                          "Flipper?")

    def close(self):
        self.c.close()

    def _take(self, reply, done):
        cmd, seq, status, data, _ = reply
        if seq == self.reading:
            self.reading = None
            if status:
                raise IOError(f"a read of the token failed (status {status})")
            if data:
                self.packets.append(data)
        elif seq == done and status:
            raise IOError(f"a write to the token failed (status {status})")
        return seq == done

    def _wait(self, end, done=None):
        """Take replies until that of the URB `done` (or one packet, with no
        such URB); TimeoutError when the time is over."""
        while self.late:
            if self._take(self.late.pop(0), done):
                return
        while done is not None or not self.packets:
            if self.reading is None:
                self.reading = self.c.submit_in(self.ep_in, PACKET)
            left = end - time.monotonic()
            if left <= 0 or not select.select([self.c.sock], [], [], left)[0]:
                raise TimeoutError("the token did not answer in time")
            if self._take(self.c.receive(), done):
                return

    def write(self, packet: bytes, timeout=5):
        seq = self.c.submit(self.ep_out, False, data=packet.ljust(PACKET, b"\0"))
        self._wait(time.monotonic() + timeout, seq)

    def read(self, timeout=5) -> bytes:
        self._wait(time.monotonic() + timeout)
        return self.packets.popleft()


# ---- U2FHID (FIDO U2F HID protocol 1.2) and the raw messages
PING, MSG, LOCK, INIT, WINK, ERROR = 0x81, 0x83, 0x84, 0x86, 0x88, 0xBF
BROADCAST = b"\xff" * 4
HID_ERRORS = {1: "invalid command", 2: "invalid parameter", 3: "invalid length",
              4: "invalid sequence", 5: "message timed out", 6: "channel busy",
              10: "the command needs a lock", 11: "sync failed", 127: "other"}
OK, NO_USER, WRONG_DATA, NOT_SUPPORTED = 0x9000, 0x6985, 0x6A80, 0x6D00
STATUS = {OK: "done", NO_USER: "the user was not there (nobody pressed OK)",
          WRONG_DATA: "the key handle is not this token's, or not for this "
          "application", NOT_SUPPORTED: "not supported"}
ENFORCE, CHECK_ONLY, DONT_ENFORCE = 0x03, 0x07, 0x08


class HidError(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(f"the token refused the frame: "
                         f"{HID_ERRORS.get(code, f'error {code:#04x}')}")


class Refused(Exception):
    """A raw message that the token answered with another status than 9000."""

    def __init__(self, status):
        self.status = status
        super().__init__(STATUS.get(status, f"status {status:04X}"))


class Token:
    """A U2F token on a USB/IP port, with a channel of its own."""

    def __init__(self, port=3240, host="127.0.0.1", appear=5):
        self.hid = Hid(port, host, appear=appear)
        nonce = os.urandom(8)
        try:
            answer = self.call(INIT, nonce, cid=BROADCAST)
        except Exception:
            self.close()
            raise
        if len(answer) != 17 or answer[:8] != nonce:
            self.close()
            raise NoToken("the token's answer to INIT is none")
        self.cid = answer[8:12]
        self.protocol, self.version = answer[12], tuple(answer[13:16])
        self.capabilities = answer[16]

    def close(self):
        self.hid.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def call(self, cmd, payload=b"", cid=None, timeout=5) -> bytes:
        """One U2FHID transaction: a command with its payload in frames of
        64 bytes, and the answer put together from its frames."""
        cid = self.cid if cid is None else cid
        self.hid.write(cid + struct.pack(">BH", cmd, len(payload)) + payload[:57])
        rest, seq = payload[57:], 0
        while rest:
            self.hid.write(cid + bytes([seq]) + rest[:59])
            rest, seq = rest[59:], seq + 1
        end = time.monotonic() + timeout
        while True:                         # (frames of other channels pass)
            first = self.hid.read(max(end - time.monotonic(), 0.01))
            if first[:4] == cid and first[4] & 0x80:
                break
        got, size = first[4], struct.unpack_from(">H", first, 5)[0]
        data, seq = first[7:], 0
        while len(data) < size:
            more = self.hid.read(max(end - time.monotonic(), 0.01))
            if more[:4] != cid:
                continue
            if more[4] != seq:
                raise IOError(f"the token's frame {more[4]} where {seq} was due")
            data, seq = data + more[5:], seq + 1
        data = data[:size]
        if got == ERROR:
            raise HidError(data[0] if data else 0x7F)
        if got != cmd:
            raise IOError(f"the token answered command {cmd:#04x} with {got:#04x}")
        return data

    def ping(self, data: bytes) -> bytes:
        return self.call(PING, data)

    def wink(self):
        self.call(WINK)

    def message(self, ins, data=b"", p1=0) -> bytes:
        """A raw message (an APDU with an extended length); the answer
        without its status, Refused for another status than 9000."""
        apdu = bytes([0, ins, p1, 0, 0]) + struct.pack(">H", len(data)) + data
        answer = self.call(MSG, apdu + b"\0\0", timeout=15)
        if len(answer) < 2:
            raise IOError("a message's answer with no status")
        status = struct.unpack(">H", answer[-2:])[0]
        if status != OK:
            raise Refused(status)
        return answer[:-2]

    def u2f_version(self) -> str:
        return self.message(3).decode("ascii", "replace")

    def _present(self, ask, wait, every):
        """Ask as a host does while the user is not there: again and again."""
        end = time.monotonic() + wait
        while True:
            try:
                return ask()
            except Refused as e:
                if e.status != NO_USER or time.monotonic() >= end:
                    raise
            time.sleep(every)

    def register(self, challenge: bytes, application: bytes, wait=0, every=0.1):
        """A registration: a new key for an application (both parameters are
        SHA-256 hashes).  wait: for how many seconds to ask again while
        nobody has pressed OK.  Returns what the token sent, taken apart; the
        signature is not checked here (see registered())."""
        raw = self._present(lambda: self.message(1, challenge + application,
                                                 ENFORCE), wait, every)
        if len(raw) < 67 or raw[0] != 5:
            raise IOError("the registration's answer is none")
        public, n = raw[1:66], raw[66]
        handle, rest = raw[67:67 + n], raw[67 + n:]
        _, _, end = der(rest)
        return {"public": public, "handle": handle, "certificate": rest[:end],
                "signature": rest[end:]}

    def authenticate(self, challenge: bytes, application: bytes, handle: bytes,
                     mode=ENFORCE, wait=0, every=0.1):
        """A login with a key handle: the user's presence, the counter and
        the signature (not checked here: see authenticated())."""
        data = challenge + application + bytes([len(handle)]) + handle
        raw = self._present(lambda: self.message(2, data, mode), wait, every)
        if len(raw) < 6:
            raise IOError("the login's answer is none")
        return {"present": bool(raw[0] & 1), "flags": raw[0],
                "counter": struct.unpack(">I", raw[1:5])[0], "signature": raw[5:]}

    def knows(self, application: bytes, handle: bytes) -> bool:
        """Whether a key handle is this token's, and for this application: a
        login that is only checked, which needs no user for its answer.  A
        browser asks this first, and so does `login` here: a token that is
        asked to log in looks for the user before it looks at the handle.
        (The Flipper shows its question for this request too, for the half
        second that it shows one after any request.)"""
        try:
            self.authenticate(bytes(32), application, handle, CHECK_ONLY)
        except Refused as e:
            if e.status == NO_USER:         # "it is mine: now ask the user"
                return True
            if e.status == WRONG_DATA:
                return False
            raise
        return True


def registered(reg, challenge, application, key=None) -> bool:
    """Whether a registration's signature is one of the certificate's key
    (or of another public key, when one is given)."""
    signed = b"\0" + application + challenge + reg["handle"] + reg["public"]
    key = key or certificate(reg["certificate"])["public"]
    return verify(key, signed, reg["signature"])


def authenticated(auth, challenge, application, public) -> bool:
    """Whether a login's signature is one of the registered key."""
    signed = (application + bytes([auth["flags"]])
              + struct.pack(">I", auth["counter"]) + challenge)
    return verify(public, signed, auth["signature"])


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("what", nargs="?", choices=("info", "register", "login"),
                    default="info")
    ap.add_argument("--port", type=int, default=3240)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--application", default="https://flipperemu.example",
                    help="whose key it is: hashed to the application "
                         "parameter, as a site's address is")
    ap.add_argument("--wait", type=float, default=30,
                    help="seconds to wait for OK on the Flipper")
    args = ap.parse_args(argv)
    application = hashlib.sha256(args.application.encode()).digest()
    challenge = os.urandom(32)
    try:
        with Token(args.port, args.host) as t:
            if args.what == "info":
                print(f"USB {t.hid.vid:04x}:{t.hid.pid:04x}, a HID device with "
                      f"FIDO's usage (U2F), reports of {PACKET} bytes on "
                      f"endpoints {t.hid.ep_in} in and {t.hid.ep_out} out")
                print(f"U2FHID protocol {t.protocol}, device version "
                      f"{'.'.join(map(str, t.version))}, "
                      f"{'winks' if t.capabilities & 1 else 'no wink'}; "
                      f"raw messages: {t.u2f_version()}")
            elif args.what == "register":
                print("Press OK on the Flipper to register.", file=sys.stderr)
                reg = t.register(challenge, application, wait=args.wait)
                cert = certificate(reg["certificate"])
                good = registered(reg, challenge, application)
                print(f"registered for {args.application}; the token's "
                      f"certificate: {cert['subject']}; the registration's "
                      "signature " + ("is one of that certificate's key"
                                      if good else "is NOT one of that "
                                      "certificate's key"), file=sys.stderr)
                print(reg["public"].hex(), reg["handle"].hex())
            else:
                public, handle = (bytes.fromhex(w) for w in
                                  sys.stdin.read().split()[:2])
                if not t.knows(application, handle):
                    raise Refused(WRONG_DATA)
                print("Press OK on the Flipper to log in.", file=sys.stderr)
                auth = t.authenticate(challenge, application, handle,
                                      wait=args.wait)
                good = authenticated(auth, challenge, application, public)
                print(f"logged in to {args.application}: counter "
                      f"{auth['counter']}, the user "
                      f"{'was' if auth['present'] else 'was not'} there, the "
                      "signature " + ("is the registered key's" if good
                                      else "is NOT the registered key's"))
                return 0 if good else 1
    except (NoToken, HidError, Refused, TimeoutError, OSError, ValueError) as e:
        print(f"fe_u2f: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
