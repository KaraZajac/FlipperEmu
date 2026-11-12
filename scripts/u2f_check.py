#!/usr/bin/env python3
# Copyright (c) 2026, Kara Zajac
# SPDX-License-Identifier: BSD-3-Clause
"""Check U2F end to end: the Flipper as a security key, and a host.

    scripts/u2f_check.py firmware/official-1.5.1/flipper-z-f7-update-1.5.1.tgz

Boots a throwaway device with `run --usbip` on a free port and is the host
itself (fe_u2f.py: USB/IP in user space, so no root, and nothing is plugged
into this PC).  At the desktop there is no token on the port.  With the
firmware's U2F app open there is one: its HID interface, U2FHID's INIT, PING
(short, and over several frames), WINK and its error for a command it does
not know, the raw messages' version.  A registration that nobody confirms is
refused, and the Flipper's screen asks meanwhile; with OK pressed it gives a
key, a key handle and the token's certificate.  Logins with that handle:
refused without OK, for another application and with one bit of the handle
wrong; with OK the signature is the registered key's, the Flipper says so for
a while (its screen, the LED green), and the counter counts (also for a login
that asks for no user).  Then the same from the command line (fe_u2f.py
register, login; a login for another application is refused at once, not
after a wait for OK).  What the firmware keeps on its card agrees with all
that, read on the host with the device's own enclave keys:
the key pair is the one the device key gives for that application, the
handle's check sum is right, the counter file has the last count, and the
registration was signed with what key 2 makes of the stock certificate's key
file (which is not that certificate's key: a real Flipper's key 2 is not
ours).  The handle still logs in after U2F was closed and opened, and after
the device was stopped and started.  Exit status 0 = pass.
"""
import argparse
import hashlib
import hmac
import os
import pathlib
import re
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time

import fe_enclave
import fe_u2f
from files_check import ROOT, Bench, CRASH, check, results
from gpio_check import skip, skipped
from uart_check import fe, start

APP = "u2f.fap"                         # found on the card, by this name
CONTROL = 0xAA5500FF                    # U2F_COUNTER_CONTROL_VAL, u2f_data.c
SITE = hashlib.sha256(b"https://flipperemu.example/check").digest()
OTHER = hashlib.sha256(b"https://other.example").digest()


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def screen(b):
    return fe(b, "shot", "--text")[1]


def settled(b, want, limit=6.0):
    """The screen is `want` within the limit"""
    end = time.time() + limit
    while screen(b) != want:
        if time.time() > end:
            return False
        time.sleep(0.3)
    return True


def refused(ask):
    """The status a message was refused with (0x9000: it was not)"""
    try:
        ask()
    except fe_u2f.Refused as e:
        return e.status
    return fe_u2f.OK


def with_ok(b, ask, limit=45.0):
    """A request that waits for the user, while OK is pressed on the Flipper
    (again every two seconds: a press counts only while the screen asks).
    -> (what it gave, the exception it ended with)"""
    box = {}

    def run():
        try:
            box["got"] = ask()
        except Exception as e:                  # said by the check
            box["error"] = e

    t = threading.Thread(target=run, daemon=True)
    t.start()
    end = time.time() + limit
    t.join(1.0)                                 # the screen asks first
    while t.is_alive() and time.time() < end:
        fe(b, "press", "ok")
        t.join(2.0)
    t.join(5.0)
    return box.get("got"), box.get("error") or (
        TimeoutError("no answer") if "got" not in box else None)


def open_u2f(b, fap):
    code, out, err, took = fe(b, "app", fap)
    said = out + err
    return (code == 0 and "has the Flipper's USB port for itself" in said
            and "HID" in said and "Traceback" not in said), f"{took:.1f} s: {said}"


def leave_u2f(b):
    fe(b, "press", "back")
    return b.answers()


def hid_checks(b, t):
    check("the token: a HID device with FIDO's usage, an interrupt endpoint "
          "each way, U2FHID protocol 2 with a wink",
          t.hid.ep_in and t.hid.ep_out and t.protocol == 2
          and t.capabilities & 1 and len(t.cid) == 4,
          f"{t.hid.vid:04x}:{t.hid.pid:04x} in {t.hid.ep_in} out "
          f"{t.hid.ep_out} protocol {t.protocol} version {t.version} "
          f"capabilities {t.capabilities}")
    code, out, err, took = fe(b, "status")
    check("`status` meanwhile: a PC has the port over USB/IP",
          code == 0 and "USB/IP" in out, out + err)
    small, big = os.urandom(8), os.urandom(300)
    try:
        echo = t.ping(small), t.ping(big)
        said = ""
    except (OSError, TimeoutError, fe_u2f.HidError) as e:
        echo, said = None, str(e)
    check("PING: 8 bytes come back, and 300 over six frames each way",
          echo == (small, big), said or "another answer")
    try:
        t.wink()
        said = None
    except (OSError, TimeoutError, fe_u2f.HidError) as e:
        said = str(e)
    check("WINK is answered", said is None, said)
    try:
        t.call(0x90)                    # CBOR: FIDO2's, which this is not
        code = None
    except fe_u2f.HidError as e:
        code = e.code
    except (OSError, TimeoutError) as e:
        code = str(e)
    check("a command it does not know (FIDO2's CBOR): the error 'invalid "
          "command'", code == 1, code)
    try:
        version = t.u2f_version()
    except (OSError, TimeoutError, fe_u2f.HidError, fe_u2f.Refused) as e:
        version = str(e)
    check("the raw messages' version: U2F_V2", version == "U2F_V2", version)
    check("a raw message it does not know: status 6D00",
          refused(lambda: t.message(0x40)) == fe_u2f.NOT_SUPPORTED)


def register_checks(b, t, idle):
    """-> (the registration, its challenge), or None"""
    challenge = os.urandom(32)
    asked = []

    def ask():                          # as a browser does: again and again
        try:
            t.register(challenge, SITE, wait=3)
        except fe_u2f.Refused as e:
            asked.append(e.status)

    host = threading.Thread(target=ask, daemon=True)
    host.start()
    time.sleep(1.5)
    shown = screen(b)
    host.join(20)
    check("a registration that nobody confirms: refused (6985) for as long "
          "as the host asks, and the Flipper's screen asks meanwhile",
          asked == [fe_u2f.NO_USER] and shown != idle,
          f"{[f'{s:04X}' for s in asked]}\n{shown}")
    check("the host gives up: the screen is as it was", settled(b, idle),
          screen(b))
    reg, error = with_ok(b, lambda: t.register(challenge, SITE, wait=30))
    if not check("with OK pressed: a public key on the curve, a key handle "
                 "of 64 bytes, a certificate",
                 reg and len(reg["handle"]) == 64
                 and fe_u2f.point(reg["public"]) is not None
                 and fe_u2f.certificate(reg["certificate"]),
                 error or "another answer"):
        return None
    return reg, challenge


def login_checks(b, t, reg, first, idle=None):
    """Logins with a registration's handle -> the last counter, or None
    (idle: the screen of a token that is asked nothing, to look for it)"""
    public, handle = reg["public"], reg["handle"]
    challenge = os.urandom(32)
    check("is this handle yours? (check only): yes, which is 6985",
          refused(lambda: t.authenticate(challenge, SITE, handle,
                                         fe_u2f.CHECK_ONLY)) == fe_u2f.NO_USER)
    wrong = handle[:40] + bytes([handle[40] ^ 1]) + handle[41:]
    check("a handle with one bit wrong: not this token's (6A80)",
          refused(lambda: t.authenticate(challenge, SITE, wrong,
                                         fe_u2f.CHECK_ONLY))
          == fe_u2f.WRONG_DATA)
    check("the handle for another application: 6A80",
          refused(lambda: t.authenticate(challenge, OTHER, handle,
                                         fe_u2f.CHECK_ONLY))
          == fe_u2f.WRONG_DATA)
    check("a login that nobody confirms: 6985",
          refused(lambda: t.authenticate(challenge, SITE, handle))
          == fe_u2f.NO_USER)
    time.sleep(1.0)                     # (its question leaves the screen)
    one, error = with_ok(b, lambda: t.authenticate(challenge, SITE, handle,
                                                   wait=30))
    if not check("with OK pressed: the user was there, the counter is "
                 f"{first}, the signature is the registered key's",
                 one and one["present"] and one["counter"] == first
                 and fe_u2f.authenticated(one, challenge, SITE, public),
                 error or {k: one[k] for k in ("flags", "counter")}):
        return None
    check("the same signature for another challenge is not",
          not fe_u2f.authenticated(one, os.urandom(32), SITE, public))
    if idle is not None:
        # this host stays, as a computer does.  (The tool at the command
        # line leaves with its answer: the firmware takes that for a new
        # connection, shows "Connected!" at once and leaves the LED green.)
        said = screen(b)
        lights = " ".join(line for line in fe(b, "status")[1].splitlines()
                          if line.startswith("lights"))
        check("the Flipper says so for a while (another screen than before, "
              "the LED green), and is then as it was",
              said != idle and "the LED: green" in lights
              and settled(b, idle, 8.0)
              and "the LED: green" not in fe(b, "status")[1],
              f"{lights}\n{said}")
    challenge = os.urandom(32)
    two, error = with_ok(b, lambda: t.authenticate(challenge, SITE, handle,
                                                   wait=30))
    check("a second login: the counter is one more",
          two and two["present"] and two["counter"] == first + 1
          and fe_u2f.authenticated(two, challenge, SITE, public),
          error or {k: two[k] for k in ("flags", "counter")})
    challenge = os.urandom(32)
    try:
        three = t.authenticate(challenge, SITE, handle, fe_u2f.DONT_ENFORCE)
        error = None
    except (OSError, TimeoutError, fe_u2f.Refused, fe_u2f.HidError) as e:
        three, error = None, e
    check("a login that asks for no user: answered at once, the user was "
          "not there, the counter is one more",
          three and not three["present"] and three["counter"] == first + 2
          and fe_u2f.authenticated(three, challenge, SITE, public),
          error or {k: three[k] for k in ("flags", "counter")})
    return three["counter"] if three else None


def tool(b, port, what, stdin="", *more):
    """scripts/fe_u2f.py at the command line, while OK is pressed"""
    p = subprocess.Popen([sys.executable, str(ROOT / "scripts" / "fe_u2f.py"),
                          "--port", str(port), "--wait", "30", *more, what],
                         stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                         stderr=subprocess.PIPE, text=True)
    box = {}
    t = threading.Thread(target=lambda: box.update(
        zip(("out", "err"), p.communicate(stdin))), daemon=True)
    t.start()
    end = time.time() + 45
    t.join(2.0 if what != "info" else 45)
    while t.is_alive() and time.time() < end:
        fe(b, "press", "ok")
        t.join(2.0)
    if t.is_alive():
        p.kill()
        t.join(5)
    return p.returncode, box.get("out", ""), box.get("err", "")


def tool_checks(b, port):
    code, out, err = tool(b, port, "info")
    check("fe_u2f.py: what the token is", code == 0 and "U2F_V2" in out
          and "protocol 2" in out, out + err)
    code, out, err = tool(b, port, "register")
    words = out.split()
    check("fe_u2f.py register: a key and its handle, and whose the "
          "certificate is", code == 0 and len(words) == 2
          and len(words[0]) == 130 and len(words[1]) == 128
          and "Flipper Zero U2F" in err, f"exit {code}: {err}")
    code2, out2, err2 = tool(b, port, "login", out)
    check("fe_u2f.py login with them: the signature is the registered key's",
          code == 0 and code2 == 0 and "is the registered key's" in out2
          and "the user was there" in out2, f"exit {code2}: {out2}{err2}")
    # the token looks for the user before it looks at the handle, so the tool
    # asks "is this handle yours?" first: no wait for an OK that cannot help
    t0 = time.time()
    code3, out3, err3 = tool(b, port, "login", out, "--application",
                             "https://other.example")
    took = time.time() - t0
    check("fe_u2f.py login for another application: refused at once, not "
          "after a wait for OK", code == 0 and code3 == 1 and not out3
          and "not this token's" in err3 and "Press OK" not in err3
          and took < 2.0, f"exit {code3} after {took:.1f} s: {out3}{err3}")
    m = re.search(r"counter (\d+)", out2)
    return int(m.group(1)) if m else None


def card_file(b, work, name):
    """A Flipper Format file of U2F's from the card: its lines by key"""
    code, out, err, took = fe(b, "pull", f"/ext/u2f/{name}", work)
    at = work / name.rsplit("/", 1)[-1]
    if code or not at.exists():
        return None
    if name.endswith(".der"):
        return at.read_bytes()
    lines = {}
    for line in at.read_text(errors="replace").splitlines():
        key, colon, value = line.partition(":")
        if colon and not line.startswith("#"):
            lines[key.strip()] = value.strip()
    return lines


def plain(keys, slot, lines, size=32):
    """What the firmware has of such a file: its first bytes, decrypted"""
    return fe_enclave.decrypt(keys[slot][1], bytes.fromhex(lines["IV"]),
                              bytes.fromhex(lines["Data"])[:size])


def card_checks(b, work, reg, challenge, counter):
    try:
        keys = fe_enclave.read_store(b.dir / "enclave.keys")
    except (OSError, ValueError) as e:
        keys = {}
        check("the device's enclave keys read", False, e)
    cert = card_file(b, work, "assets/cert.der")
    check("the registration's certificate is the card's cert.der, and that "
          "is signed with its own key",
          cert and cert == reg["certificate"]
          and fe_u2f.verify(fe_u2f.certificate(cert)["public"],
                            fe_u2f.certificate(cert)["signed"],
                            fe_u2f.certificate(cert)["signature"]),
          "another certificate" if cert else "no cert.der pulled")
    stock = card_file(b, work, "assets/cert_key.u2f")
    try:
        kind = int(stock["Type"])
        slot = {0: 2, 1: fe_enclave.UNIQUE_SLOT}[kind]
        signer = fe_u2f.public_key(plain(keys, slot, stock))
        said = ""
    except (TypeError, KeyError, ValueError) as e:
        kind = signer = None
        said = f"{type(e).__name__}: the file's keys: {sorted(stock or {})}"
    check("the registration is signed with the key that the enclave makes "
          "of the card's cert_key.u2f",
          signer and fe_u2f.registered(reg, challenge, SITE, signer), said)
    if kind == 0:
        check("which is not the stock certificate's key (ours is not a "
              "real Flipper's key 2): a host that checks the certificate's "
              "signature would not take it",
              signer != fe_u2f.certificate(reg["certificate"])["public"]
              and not fe_u2f.registered(reg, challenge, SITE))
    else:
        check("and that is the certificate's key",
              fe_u2f.registered(reg, challenge, SITE))
    device = card_file(b, work, "key.u2f")
    try:
        dk = plain(keys, fe_enclave.UNIQUE_SLOT, device)
        nonce = reg["handle"][32:]
        private = hmac.new(dk, SITE + nonce, hashlib.sha256).digest()
        mine = (fe_u2f.public_key(private) == reg["public"],
                hmac.new(dk, private + SITE, hashlib.sha256).digest()
                == reg["handle"][:32])
        said = ""
    except (TypeError, KeyError, ValueError) as e:
        mine = (False, False)
        said = f"{type(e).__name__}: the file's keys: {sorted(device or {})}"
    check("the device key (key.u2f, decrypted with key 11) gives that "
          "registration's key pair for the application and the handle's "
          "nonce", mine[0], said)
    check("and the handle's first half: the firmware's check of whose "
          "handle it is", mine[1], said)
    count = card_file(b, work, "cnt.u2f")
    try:
        n, salt, control = struct.unpack(
            "<I24sI", plain(keys, fe_enclave.UNIQUE_SLOT, count))
        said = f"counter {n}, control {control:08X}"
    except (TypeError, KeyError, ValueError, struct.error) as e:
        n = control = None
        said = f"{type(e).__name__}: the file's keys: {sorted(count or {})}"
    check(f"the counter file (cnt.u2f, key 11): {counter}, the last login's",
          n == counter and control == CONTROL, said)


def again(b, fap, port, reg, counter, what):
    """U2F opened anew: a login with the old handle -> its counter"""
    ok, said = open_u2f(b, fap)
    one = error = None
    good = False
    if ok:
        try:
            with fe_u2f.Token(port) as t:
                challenge = os.urandom(32)
                one, error = with_ok(b, lambda: t.authenticate(
                    challenge, SITE, reg["handle"], wait=30))
                good = one and fe_u2f.authenticated(one, challenge, SITE,
                                                    reg["public"])
        except (fe_u2f.NoToken, OSError, TimeoutError) as e:
            error = e
    check(f"{what}: the handle logs in, and the counter goes on from "
          f"{counter}", ok and one and good and one["present"]
          and one["counter"] == counter + 1,
          said if not ok else error or one["counter"])
    ok, said = leave_u2f(b)
    check("Back: the CLI answers", ok, said)
    return one["counter"] if one else counter


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("firmware")
    ap.add_argument("--timeout", type=float, default=120.0,
                    help="seconds to wait for the firmware to start")
    ap.add_argument("--keep", action="store_true",
                    help="keep the throwaway device")
    args = ap.parse_args()

    b = Bench(f"u2fcheck-{int(time.time())}-{os.getpid()}", args.firmware,
              args.timeout)
    work = pathlib.Path(tempfile.mkdtemp(prefix="fe-u2f-"))
    port = free_port()
    try:
        b.dir.mkdir(parents=True, exist_ok=True)
        ok, said = start(b, args.timeout, "-f", args.firmware, "--usbip",
                         str(port))
        if ok:
            ok, said = b.answers()
        if check("the firmware starts with its USB port on USB/IP too, and "
                 "its CLI answers", ok, said + b.uart_text()[-2000:]):
            run(b, args, port, work)
            log = b.uart_text(every=True)
            crash = CRASH.search(log)
            check("no crash in the firmware's log", not crash,
                  crash and log[max(0, crash.start() - 300):crash.end() + 300])
    finally:
        b.stop()
        fe(b, "stop")
        shutil.rmtree(work, ignore_errors=True)
        if not args.keep:
            shutil.rmtree(b.dir, ignore_errors=True)
    bad = [label for label, ok in results if not ok]
    print(f"{len(results) - len(bad)} of {len(results)} checks passed"
          + (f", {len(skipped)} skipped" if skipped else "")
          + (f"; device kept: {b.dir}" if args.keep else ""))
    return 1 if bad else 0


def run(b, args, port, work):
    try:
        with fe_u2f.Token(port, appear=2):
            said = "a token"
    except fe_u2f.NoToken as e:
        said = str(e)
    ok, more = b.answers()
    check("at the desktop: no token on the port (a serial port, no HID "
          "interface), and the CLI answers after the host has looked",
          "no HID interface" in said and ok, said + "\n" + more)
    code, out, err, took = fe(b, "app", "--list")
    fap = next((p for p in re.findall(r"^  (/ext/apps/\S.*\.fap)$", out, re.M)
                if p.rsplit("/", 1)[1] == APP), None)
    if not fap:
        skip("U2F", f"this firmware's card has no {APP}")
        return
    ok, said = open_u2f(b, fap)
    if not check("U2F opened with `app`: it has the USB port, said at once",
                 ok, said):
        return
    time.sleep(1.0)
    idle = screen(b)
    try:
        t = fe_u2f.Token(port)
    except (fe_u2f.NoToken, OSError, TimeoutError) as e:
        check("a host finds the token", False, e)
        return
    counter = got = None
    try:
        hid_checks(b, t)
        got = register_checks(b, t, idle)
        if got:
            counter = login_checks(b, t, got[0], 1, idle)
    finally:
        t.close()
    if counter is not None:
        n = tool_checks(b, port)
        counter = n if n is not None else counter
    ok, said = leave_u2f(b)
    check("Back: U2F is left, the serial port is back and the CLI answers",
          ok, said)
    if not got or counter is None:
        return
    reg, challenge = got
    card_checks(b, work, reg, challenge, counter)
    counter = again(b, fap, port, reg, counter, "U2F closed and opened")
    b.stop()
    fe(b, "stop")
    ok, said = start(b, args.timeout, "--usbip", str(port))
    if check("the device stopped and started", ok, said):
        again(b, fap, port, reg, counter, "after that")


if __name__ == "__main__":
    sys.exit(main())
