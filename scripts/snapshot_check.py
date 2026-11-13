#!/usr/bin/env python3
# Copyright (c) 2026, Kara Zajac
# SPDX-License-Identifier: BSD-3-Clause
"""Check the launcher's `snapshot`: a running device as it stands, kept, and
a device back at that moment.

    scripts/snapshot_check.py firmware/official-1.5.1/flipper-z-f7-update-1.5.1.tgz

A device that is not there has no snapshots, and says so.  Then a throwaway
device starts with a sensor, an EEPROM and a flash on its header, gets a
folder on its card, an NFC tag, a 125 kHz tag and an iButton key held to it,
and `snapshot save` keeps it: the device runs on, the snapshot's folder and
files are the user's alone, it has the state, the device's files and a note
(how the machine was made, which build of the emulator, the state's sum, what
was held), and the card's copy is not a second card on the disk.  A name that
is taken or wrong is refused, a device that is paused is saved and stays
paused, a save with no name gets the time as its name (and a number when that
is taken), and `snapshot` lists them, the oldest first.

The device goes on (another folder on the card, the tags taken away, the
sensor's value changed, twenty seconds) and is stopped.  `snapshot load` is
back at the moment of the save: the uptime, the calendar on the host's clock,
the card without the newer folder, the tags and the key held under their
names, the sensor's value, the EEPROM's and the flash's contents, the log
carried on and the log of the run before it kept.  The loaded machine writes
to its card, is saved again, and a plain `run` after it starts the firmware
on that card.  A load with no name takes the newest.

A load is refused, with the device left exactly as it was, when the state is
not the file that was saved, when the emulator refuses it (a file cut short),
when it has nothing for a part of the machine (one more device on the
header), and when it is of another build (`--any-build` tries it all the
same; `--paused` leaves it standing still for `continue`).  A load that was
cut off (its launcher killed) is undone by the next start, early or late.  A
load in the foreground prints the firmware's log and ends with SIGTERM; one
with the window, off-screen, ends with the window (skipped without PySide6).
`snapshot rm` removes one.

With a gdb on it (the one `./flipperemu gdb` finds): a load that stands
still with the gdb stub on, a gdb that steps, a save at that step, the gdb's
leaving, which lets the machine go on, a write to the card after it, and a
load of that state.  Where ufbt's SDK is the firmware's (the official
release), an app of the check's own (scripts/snapshot_check_app) has a key
of the enclave loaded across a save, a stop and a load: the device's own,
and factory key 1 with the block of the firmware's self-test.  What it
encrypts with the key after the load has to be what a run with no save
gives.  Last, a crash on purpose: a debug build's halt and its report, or a
release build's message of the crash, the same after a save and a load.
Nothing here prints what a state holds.  Exit status 0 = pass.
"""
import argparse
import contextlib
import hashlib
import importlib.util
import json
import os
import pathlib
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time

from fe_monitor import Monitor
from files_check import ROOT, Bench, CRASH, check, results
from gdb_check import Gdb, free_port
from gpio_check import skip, skipped
from uart_check import fe
from ufbt_check import ANSI, logged, tool

QEMU = ROOT / "build" / "qemu" / "qemu-system-arm"
HEADER = ("--i2c", "tmp105@0x48", "--i2c", "eeprom@0x50", "--spi", "flash")
QUIET = ("--headless", "--audio", "none")
HELD = {"nfc": "an NTAG215 04 A1 B2 C3 D4 E5 F6 with https://flipperzero.one",
        "rfid": "EM4100 DE AD BE EF 01",
        "ibutton": "DS1990 01 02 03 04 05 06 07 0F"}
# a snapshot of that machine: the state, the device's files, the note
# (and `nvram.bin` of a device that has one: it is written at a run's end)
FILES = {"state", "snapshot.json", "flash.img", "enclave.keys", "device.json",
         "sd.img", "i2c-50.bin", "spi-4.bin", "uart.log", "lpuart.log",
         "held.json"}
STAMP = r"\d{8}-\d{6}"
logs = []               # what the firmware logged, run by run


def sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def mode(path):
    return stat.S_IMODE(os.stat(path).st_mode)


def cli(b, *commands):
    """What the firmware's CLI answers"""
    code, out, err, took = fe(b, "cli", "--timeout", 30, *commands)
    return out


def folders(b):
    return set(re.findall(r"\[D\]\s+(\S+)", cli(b, "storage list /ext")))


def mkdir(b, name):
    cli(b, f"storage mkdir /ext/{name}")
    return name in folders(b)


def uptime(b):
    hit = re.search(r"Uptime: (\d+)h(\d+)m(\d+)s", cli(b, "uptime"))
    return hit and int(hit[1]) * 3600 + int(hit[2]) * 60 + int(hit[3])


def ahead(b):
    """The device's calendar, in seconds ahead of the host's clock"""
    hit = re.search(r"(\d{4})-(\d\d)-(\d\d) (\d\d):(\d\d):(\d\d)",
                    cli(b, "date"))
    return hit and time.mktime((*map(int, hit.groups()), 0, 0, -1)) \
        - time.time()


def state(b):
    """`status --json`"""
    code, out, err, took = fe(b, "status", "--json")
    try:
        return json.loads(out)
    except ValueError:
        return {}


def held(st):
    try:
        return {"nfc": st["nfc"]["card"], "rfid": st["rfid"]["tag"],
                "ibutton": st["ibutton"]["key"]}
    except (KeyError, TypeError):
        return {}


def temperature(b):
    code, out, err, took = fe(b, "i2c", "0x48", "temperature")
    return out.strip()


def up(b, timeout, *words):
    """A start that goes on by itself, until the CLI answers: (it does, what
    was said)"""
    code, out, err, took = fe(b, *words, "--detach", *QUIET)
    said = out + err
    if code == 0:
        code, out, err, took = fe(b, "wait", "--timeout", timeout)
        said += out + err
    if code:
        return False, said
    ok, more = b.answers()
    return ok, said + more


def stop(b):
    """Stop the device.  What the firmware logged is kept for the last
    check (a load puts the log of a snapshot in its place)."""
    try:
        logs.append(b.uart.read_text(errors="replace"))
    except OSError:
        pass
    fe(b, "stop")
    end = time.time() + 15
    while time.time() < end and state(b).get("running"):
        time.sleep(0.2)
    return logs[-1] if logs else ""


def fingerprint(folder):
    """What a stopped device's folder has: every file's sum, and the names
    of what else is there (`run.log` is a start's own)"""
    seen = {}
    for path in sorted(folder.iterdir()):
        if path.name == "run.log":
            continue
        if path.is_symlink():
            seen[path.name] = "a link"
        elif path.is_file():
            seen[path.name] = sha256(path)
        else:
            seen[path.name] = "a folder" if path.is_dir() else "a socket"
    return seen


def differing(a, b):
    """The names that two fingerprints do not agree on (names only: a sum
    of a file with keys says nothing, and is still not for a log)"""
    return "differ: " + (", ".join(sorted(
        k for k in set(a) | set(b) if a.get(k) != b.get(k))) or "nothing")


@contextlib.contextmanager
def changed(path, new):
    """A file with other contents for a while (bytes, or what to make of
    its JSON)"""
    old = path.read_bytes()
    if callable(new):
        note = json.loads(old)
        new(note)
        new = (json.dumps(note, indent=2) + "\n").encode()
    path.write_bytes(new)
    try:
        yield
    finally:
        path.write_bytes(old)


def front(b, *words, env=None):
    """A launcher command that stays in the foreground: (its process, the
    file with what it prints)"""
    fd, said = tempfile.mkstemp(prefix="fe-snap-", suffix=".txt")
    with os.fdopen(fd, "wb") as out:
        proc = subprocess.Popen(
            [sys.executable, str(ROOT / "flipperemu"), "-d", b.name, *words],
            stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT,
            env=dict(env or os.environ, PYTHONUNBUFFERED="1"))
    return proc, pathlib.Path(said)


def until(what, timeout):
    end = time.time() + timeout
    while time.time() < end:
        if what():
            return True
        time.sleep(0.1)
    return bool(what())


def ended(proc, timeout=30):
    try:
        return proc.wait(timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
        return "it did not end"


def nothing_checks(b):
    code, out, err, took = fe(b, "snapshot", "save", "first")
    check("a device that is not running: no snapshot of it, says so, exit 1",
          code == 1 and "not running" in err, out + err)
    code, out, err, took = fe(b, "snapshot")
    check("`snapshot` of a device with none: says so, exit 0",
          code == 0 and "no snapshots" in out, out + err)
    code, out, err, took = fe(b, "snapshot", "--json")
    check("... and `snapshot --json`: an empty list",
          code == 0 and out.strip() == "[]", out + err)
    code, out, err, took = fe(b, "snapshot", "load", *QUIET)
    code2, out2, err2, took = fe(b, "snapshot", "rm", "first")
    check("nothing to load, nothing to remove: says so, exit 1",
          code == 1 and "has no snapshot" in err
          and code2 == 1 and "has no snapshot first" in err2,
          out + err + out2 + err2)
    code, out, err, took = fe(b, "snapshot", "rm")
    check("`snapshot rm` with no name: asks which", code and "which" in err,
          out + err)
    check("none of that made a device", not b.dir.exists())


def save_checks(b):
    """-> the uptime and the calendar just before the first save"""
    snaps = b.dir / "snapshots"
    made = mkdir(b, "snap_before")
    fe(b, "nfc", "ntag215", "04A1B2C3D4E5F6", "https://flipperzero.one")
    fe(b, "rfid", "EM4100", "DEADBEEF01")
    fe(b, "ibutton", "--no-wait", "ds1990", "01", "02", "03", "04", "05",
       "06", "07")
    fe(b, "i2c", "0x48", "temperature", "31.5")
    st = state(b)
    check("a folder on the card, three things held to it, a value for the "
          "sensor", made and held(st) == HELD and temperature(b) == "31.5",
          f"{made} {held(st)} {temperature(b)}")
    u1, a1 = uptime(b), ahead(b)
    code, out, err, took = fe(b, "snapshot", "save", "first")
    st = state(b)
    ok, said = b.answers()
    check("`snapshot save first`: says so, and the device runs on",
          code == 0 and "snapshot first:" in out and st.get("running")
          and st.get("stopped") is None and ok, out + err + said)
    first = snaps / "first"
    names = {p.name for p in first.iterdir()} if first.is_dir() else set()
    check("the snapshot has the state, the device's files and a note (and "
          "no `nvram.bin`: the device has none in its first run)",
          names == FILES, f"lacks {sorted(FILES - names)}, has more: "
          f"{sorted(names - FILES)}")
    if names != FILES:
        return u1, a1
    wrong = [p.name for p in (snaps, first) if mode(p) != 0o700] \
        + [p.name for p in first.iterdir() if mode(p) != 0o600]
    check("its folder and its files are the user's alone (0700, 0600)",
          not wrong, wrong)
    check("nothing else in the snapshots' folder",
          [p.name for p in snaps.iterdir()] == ["first"],
          [p.name for p in snaps.iterdir()])
    try:
        note = json.loads((first / "snapshot.json").read_text())
    except ValueError:
        note = {}
    machine = note.get("machine") or {}
    check("the note: how the machine was made, the firmware, what was held",
          machine.get("i2c") == ["tmp105@0x48", "eeprom@0x50"]
          and machine.get("spi") == ["flash"] and note.get("held") == HELD
          and note.get("firmware")
          and note["firmware"] == st.get("firmware")
          and set(note.get("files") or []) == FILES - {"state",
                                                        "snapshot.json"},
          {k: v for k, v in note.items() if k not in ("state", "emulator")})
    check("... the build of the emulator and the state's sum",
          (note.get("emulator") or {}).get("sha256") == sha256(QEMU)
          and note.get("state") == sha256(first / "state"))
    card, copy = os.stat(b.dir / "sd.img"), os.stat(first / "sd.img")
    check("the card's copy is as large as the card, and not a second card "
          "on the disk (shared data, or holes kept)",
          copy.st_size == card.st_size
          and copy.st_blocks * 512 < card.st_size // 2,
          f"{copy.st_size} bytes, {copy.st_blocks * 512} on the disk; the "
          f"card: {card.st_size}, {card.st_blocks * 512}")
    check("the EEPROM's and the flash's contents are in it",
          all(sha256(first / n) == sha256(b.dir / n)
              for n in ("i2c-50.bin", "spi-4.bin")))

    code, out, err, took = fe(b, "snapshot", "save", "first")
    check("a name that is taken: refused, with the way to remove it",
          code == 1 and "already" in err and "snapshot rm first" in err,
          out + err)
    said = [fe(b, "snapshot", "save", name)
            for name in ("a b", "../up", ".hidden", "x" * 65)]
    check("a name that is no name for a folder: refused",
          all(code == 1 and "a name is of" in err
              for code, out, err, took in said)
          and [p.name for p in snaps.iterdir()] == ["first"]
          and not (b.dir / "up").exists(),
          "".join(out + err for code, out, err, took in said))

    fe(b, "pause")
    code, out, err, took = fe(b, "snapshot", "save", "second")
    st = state(b)
    check("a paused device is saved, and stays paused",
          code == 0 and "snapshot second:" in out
          and st.get("stopped") == "paused",
          f"{out + err} {st.get('stopped')}")
    code, out, err, took = fe(b, "continue")
    st = state(b)
    ok, said = b.answers()
    check("... and goes on with `continue`: its CLI answers",
          code == 0 and st.get("stopped") is None and ok, out + err + said)

    # a save with no name: the time, and a number where that is taken
    now = time.time()
    taken = [snaps / time.strftime("%Y%m%d-%H%M%S", time.localtime(now + s))
             for s in range(-1, 8)]
    for folder in taken:
        folder.mkdir()
    code, out, err, took = fe(b, "snapshot", "save")
    hit = re.search(r"snapshot (\S+):", out)
    numbered = hit and hit[1]
    for folder in taken:
        folder.rmdir()
    check("a save with no name where the time is taken: the time and -2",
          code == 0 and numbered
          and re.fullmatch(STAMP + "-2", numbered)
          and (snaps / numbered / "state").is_file(), out + err)
    code, out, err, took = fe(b, "snapshot", "save")
    hit = re.search(r"snapshot (\S+):", out)
    timed = hit and hit[1]
    check("a save with no name: the time is its name",
          code == 0 and timed and re.fullmatch(STAMP, timed)
          and (snaps / timed / "state").is_file(), out + err)

    order = ["first", "second", numbered, timed]
    code, out, err, took = fe(b, "snapshot")
    listed = [line.split()[0] for line in out.splitlines()[1:] if line.strip()]
    check("`snapshot` lists the four, the oldest first, with firmware and "
          "time", code == 0 and listed == order
          and "4 snapshots, the oldest first" in out
          and all(note.get("firmware") in line
                  and re.search(r"\d{4}-\d\d-\d\d \d\d:\d\d:\d\d", line)
                  for line in out.splitlines()[1:] if line.strip())
          and "another build" not in out, out + err)
    code, out, err, took = fe(b, "snapshot", "list", "--json")
    try:
        items = json.loads(out)
    except ValueError:
        items = []
    check("`snapshot list --json`: the same, with what was held and how "
          "the machine was made",
          code == 0 and [i.get("name") for i in items] == order
          and all(i.get("this_build") is True and i.get("held") == HELD
                  and (i.get("machine") or {}).get("spi") == ["flash"]
                  and i.get("path") == str(snaps / i["name"])
                  for i in items), out + err)
    code, out, err, took = fe(b, "snapshot", "load", "first", *QUIET)
    check("a load while the device runs: refused, `stop` first",
          code == 1 and "is running" in err and state(b).get("running"),
          out + err)
    return u1, a1


def load_checks(b, timeout, u1, a1):
    first = b.dir / "snapshots" / "first"
    # the device goes on, and is another one after a while
    mkdir(b, "snap_after")
    fe(b, "rfid", "none")
    fe(b, "nfc", "none")
    fe(b, "i2c", "0x48", "temperature", "12.5")
    st = state(b)
    later = until(lambda: (uptime(b) or 0) >= u1 + 20, 60)
    check("the device goes on: another folder, two tags taken away, "
          "another temperature, twenty seconds",
          later and "snap_after" in folders(b) and not held(st).get("nfc")
          and not held(st).get("rfid") and temperature(b) == "12.5",
          f"{held(st)} {temperature(b)} {uptime(b)} after {u1}")
    before = stop(b)
    # (what is on the header is the snapshot's too: other contents now)
    for name in ("i2c-50.bin", "spi-4.bin"):
        with open(b.dir / name, "r+b") as f:
            f.write(b"not as it was...")

    code, out, err, took = fe(b, "snapshot", "load", "first", "--detach",
                              *QUIET)
    ok = code == 0
    if ok:
        code2, out2, err2, took2 = fe(b, "wait", "--timeout", timeout)
        ok = code2 == 0
        out, err = out + out2, err + err2
    if not check("`snapshot load first`: says so, and the CLI answers",
                 ok and "snapshot first:" in out and "loaded: it goes on "
                 "from there" in out, out + err):
        return False
    print(f"        (the load took {took:.1f} s)", flush=True)
    u3, a3 = uptime(b), ahead(b)
    check("the uptime is the save's, not the stopped run's",
          u3 is not None and u1 <= u3 < u1 + 15, f"{u3}, at the save {u1}")
    check("the calendar is where the host's clock has it now",
          a3 is not None and abs(a3 - a1) <= 3,
          f"{a3} s ahead of the host, at the save {a1}")
    seen = folders(b)
    check("the card is the save's: the folder from before it, not the one "
          "after it", "snap_before" in seen and "snap_after" not in seen,
          sorted(seen))
    st = state(b)
    try:
        names = json.loads((b.dir / "held.json").read_text())
    except (OSError, ValueError):
        names = None
    check("the tags and the key are held to it again, under their names",
          held(st) == HELD and names == HELD, f"{held(st)} {names}")
    check("the sensor has the save's value", temperature(b) == "31.5",
          temperature(b))
    check("the EEPROM and the flash have the save's contents",
          all(sha256(first / n) == sha256(b.dir / n)
              for n in ("i2c-50.bin", "spi-4.bin")))
    check("the machine is made as it was: two devices on the I2C bus, one "
          "on the SPI bus, and it runs",
          st.get("stopped") is None
          and [d.get("type") for d in (st.get("i2c") or {}).get("devices", [])]
          == ["tmp105", "at24c-eeprom"]
          and [d.get("type") for d in (st.get("spi") or {}).get("devices", [])]
          == ["w25q32"], f"{st.get('i2c')} {st.get('spi')}")
    try:
        log = (b.dir / "uart.log").read_bytes()
        kept = (b.dir / "uart.log.1").read_text(errors="replace")
    except OSError:
        log, kept = b"", ""
    saved = (first / "uart.log").read_bytes()
    check("the log goes on from the snapshot's, and the log of the run "
          "before the load is kept beside it",
          saved and log.startswith(saved) and before
          and kept.startswith(before),
          f"{len(saved)} bytes in the snapshot, {len(log)} now; "
          f"{len(before)} before the stop, {len(kept)} kept")
    check("the loaded machine writes to its card", mkdir(b, "snap_loaded"),
          sorted(folders(b)))
    code, out, err, took = fe(b, "snapshot", "save", "third")
    ok, said = b.answers()
    check("... and is saved again", code == 0 and "snapshot third:" in out
          and ok, out + err + said)
    stop(b)

    ok, said = up(b, timeout, "run")
    seen = folders(b) if ok else set()
    st = state(b)
    check("a plain `run` after it: the firmware starts on the loaded "
          "machine's card, with nothing held to it",
          ok and {"snap_before", "snap_loaded"} <= seen
          and "snap_after" not in seen and (uptime(b) or 0) < u1
          and held(st) == dict.fromkeys(HELD), said + str(sorted(seen))
          + str(held(st)))
    code, out, err, took = fe(b, "snapshot", "save", "fourth")
    fourth = b.dir / "snapshots" / "fourth"
    names = {p.name for p in fourth.iterdir()} if fourth.is_dir() else set()
    try:
        note = json.loads((fourth / "snapshot.json").read_text())
    except (OSError, ValueError):
        note = {}
    check("a snapshot of that run: nothing on the header and nothing held "
          "in it, and the `nvram.bin` that the device has now",
          code == 0 and names == FILES - {"held.json", "i2c-50.bin",
                                          "spi-4.bin"} | {"nvram.bin"}
          and note.get("held") == {} and mode(fourth / "nvram.bin") == 0o600
          and not (note.get("machine") or {}).get("i2c"),
          f"{out + err} {sorted(names)} {note.get('held')}")
    mkdir(b, "snap_plain")
    stop(b)

    ok, said = up(b, timeout, "snapshot", "load")
    seen = folders(b) if ok else set()
    st = state(b)
    check("a load with no name: the newest, a machine with nothing on its "
          "header and nothing held to it",
          ok and "snapshot fourth:" in said
          and {"snap_before", "snap_loaded"} <= seen
          and "snap_plain" not in seen and held(st) == dict.fromkeys(HELD)
          and not (b.dir / "held.json").exists()
          and not (st.get("i2c") or {}).get("devices")
          and not (st.get("spi") or {}).get("devices"),
          said + str(sorted(seen)) + str(held(st)) + str(st.get("i2c")))
    stop(b)

    ok, said = up(b, timeout, "snapshot", "load", "third")
    seen = folders(b) if ok else set()
    check("a load of the save of the loaded machine: its card, its uptime, "
          "the tags and the key",
          ok and "snapshot third:" in said
          and {"snap_before", "snap_loaded"} <= seen
          and "snap_plain" not in seen and (uptime(b) or 0) >= u1
          and held(state(b)) == HELD, said + str(sorted(seen)))
    mkdir(b, "snap_newest")
    stop(b)
    return True


def refused(b, label, said, words, was):
    code, out, err, took = said
    now = fingerprint(b.dir)
    return check(label, code == 1 and all(w in out + err for w in words)
                 and not state(b).get("running") and now == was,
                 out + err + differing(was, now))


def refusal_checks(b, timeout):
    first = b.dir / "snapshots" / "first"
    note, file = first / "snapshot.json", first / "state"
    was = fingerprint(b.dir)
    whole = file.read_bytes()           # (never printed)
    half = len(whole) // 2
    load = ("snapshot", "load", "first", *QUIET)

    with changed(file, whole[:half] + bytes(16) + whole[half + 16:]):
        refused(b, "a state that is not the file that was saved (16 bytes "
                "of it): refused before anything is done",
                fe(b, *load),
                ["not the file that was saved", "The device is as it was"],
                was)

    short = whole[:half]
    sum_of = lambda data: lambda n: n.update(
        state=hashlib.sha256(data).hexdigest())
    with changed(file, short), changed(note, sum_of(short)):
        refused(b, "a state that the emulator refuses (cut short): not "
                "loaded, and the device is as it was",
                fe(b, *load, "--detach"),
                ["refused its state", "The device is as it was"], was)
        # a load that was cut off before anything was in the device's
        # places: two of the device's files aside, the rest still there
        aside = b.dir / ".before-load"
        aside.mkdir()
        (aside / "load.json").write_text('{"snapshot": "second"}\n')
        for name in ("nvram.bin", "sd.img"):
            os.replace(b.dir / name, aside / name)
        said = fe(b, *load)
        refused(b, "a load that was cut off early is undone by the next "
                "one, which says so (and is refused itself: the device is "
                "still as it was)", said,
                ["a load of the snapshot second did not come to its end",
                 "refused its state", "The device is as it was"], was)

    with changed(note, lambda n: n["machine"]["i2c"].append("tmp105@0x49")):
        refused(b, "a state with nothing for a part of the machine (one "
                "more sensor on the header): not loaded, the part is named",
                fe(b, *load, "--detach"),
                ["has nothing for TMP105", "as after a reset",
                 "The device is as it was"], was)

    with changed(note, lambda n: n["emulator"].update(sha256="0" * 64)):
        code, out, err, took = fe(b, "snapshot")
        refused(b, "a snapshot of another build of the emulator: listed as "
                "one, and not loaded", fe(b, *load, "--detach"),
                ["another build", "--any-build"], was) \
            and check("... (the list)", "first" in out and re.search(
                r"first .*\(of another build of the emulator\)", out), out)
        code, out, err, took = fe(b, *load, "--detach", "--any-build",
                                  "--paused")
        st = state(b)
        check("`--any-build --paused` loads it all the same, and it stands "
              "still", code == 0 and "tried all the same" in out
              and "it stands still" in out and st.get("stopped") == "paused",
              f"{out + err} {st.get('stopped')}")
        code, out, err, took = fe(b, "continue")
        code2, out2, err2, took = fe(b, "wait", "--timeout", timeout)
        seen = folders(b)
        check("... until `continue`: its CLI answers, on the save's card",
              code == 0 and code2 == 0 and state(b).get("stopped") is None
              and "snap_before" in seen and "snap_newest" not in seen,
              out + err + out2 + err2 + str(sorted(seen)))
    mkdir(b, "snap_kept")
    stop(b)


def killed_checks(b, timeout):
    """A launcher that is killed when its emulator is there: the snapshot's
    files are in the device's places, and the device's own aside"""
    aside = b.dir / ".before-load"
    proc, said = front(b, "snapshot", "load", "second", *QUIET)
    try:
        end = time.time() + 30
        while time.time() < end and proc.poll() is None \
                and not (b.dir / "monitor.sock").exists():
            time.sleep(0.002)
        proc.kill()
        proc.wait()
        try:
            put = json.loads((aside / "load.json").read_text())
        except (OSError, ValueError):
            put = {}
        time.sleep(1.0)
        stop(b)                         # (its emulator, if that lives on)
        ok, text = up(b, timeout, "run")
        seen = folders(b) if ok else set()
        check("a load whose launcher is killed with the snapshot's files in "
              "place: the next `run` says so, and starts the device as it "
              "was before that load",
              "sd.img" in (put.get("files") or []) and ok
              and "a load of the snapshot second did not come to its end"
              in text and "snap_kept" in seen and not aside.exists(),
              f"{put} {text} {sorted(seen)}")
        stop(b)
    finally:
        said.unlink()


def front_checks(b, timeout):
    proc, said = front(b, "snapshot", "load", "second", *QUIET)
    try:
        ok = until(lambda: "loaded: it goes on" in said.read_text(
            errors="replace") or proc.poll() is not None, 60)
        code, out, err, took = fe(b, "wait", "--timeout", timeout)
        made = mkdir(b, "snap_front")
        st = state(b)
        check("a load in the foreground (of the save of the paused device): "
              "it goes on, its CLI answers",
              ok and proc.poll() is None and code == 0 and made
              and st.get("stopped") is None,
              said.read_text(errors="replace")[-1500:] + out + err)
        logs.append(b.uart.read_text(errors="replace"))
        proc.send_signal(signal.SIGTERM)
        code = ended(proc)
        check("... and SIGTERM ends it: exit 0, nothing runs, nothing of "
              "the load is left", code == 0 and not state(b).get("running")
              and not [p.name for p in b.dir.iterdir()
                       if p.name.startswith(".before-load")
                       or p.name.endswith(".sock")],
              f"exit {code} {sorted(p.name for p in b.dir.iterdir())}")
    finally:
        if proc.poll() is None:
            proc.kill()
        said.unlink()

    label = "a load with the window: the Flipper's window on the loaded " \
            "machine, and closing it ends the run"
    if not importlib.util.find_spec("PySide6"):
        return skip(label, "no PySide6 (the window is of it)")
    # off-screen, with no display to fall back to and settings of its own
    work = pathlib.Path(tempfile.mkdtemp(prefix="fe-snap-gui-"))
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen",
               FLIPPEREMU_GUI_CONFIG=str(work / "gui.ini"))
    for name in ("DISPLAY", "WAYLAND_DISPLAY"):
        env.pop(name, None)
    proc, said = front(b, "snapshot", "load", "first", "--audio", "none",
                       env=env)
    try:
        text = lambda: said.read_text(errors="replace")
        ok = until(lambda: ("loaded: it goes on" in text()
                            and "panel link: FlipperEmu window" in text())
                   or proc.poll() is not None, 90)
        code, out, err, took = fe(b, "wait", "--timeout", timeout)
        pressed = [fe(b, "press", key)[0] for key in ("ok", "back")]
        st = state(b)
        seen = folders(b)
        logs.append(b.uart.read_text(errors="replace"))
        up_ = ok and proc.poll() is None and code == 0
        proc.send_signal(signal.SIGINT)
        code = ended(proc)
        check(label, up_ and pressed == [0, 0] and st.get("stopped") is None
              and held(st) == HELD and "snap_before" in seen
              and "snap_front" not in seen and code == 0
              and not state(b).get("running"),
              f"{text()[-1500:]} {out + err} {pressed} exit {code}")
    finally:
        if proc.poll() is None:
            proc.kill()
        said.unlink()
        shutil.rmtree(work, ignore_errors=True)


def rm_checks(b):
    snaps = b.dir / "snapshots"
    code, out, err, took = fe(b, "snapshot", "rm", "second")
    code2, out2, err2, took = fe(b, "snapshot", "--json")
    try:
        names = [i["name"] for i in json.loads(out2)]
    except (ValueError, KeyError, TypeError):
        names = None
    check("`snapshot rm second`: gone, the others stay",
          code == 0 and "removed" in out and not (snaps / "second").exists()
          and names and len(names) == 5 and names[0] == "first"
          and "second" not in names and names[-2:] == ["third", "fourth"],
          f"{out + err} {names}")
    code, out, err, took = fe(b, "snapshot", "rm", "second")
    check("... and once more: no such snapshot, with those it has, exit 1",
          code == 1 and "has no snapshot second" in err and "first" in err,
          out + err)


def said_by_qemu(b, command):
    """What the emulator's monitor answers, in QEMU's own words"""
    try:
        m = Monitor(str(b.dir / "monitor.sock"))
        try:
            return m.cmd(command)
        finally:
            m.close()
    except OSError:
        return ""


def debugger_checks(b, timeout):
    """A save and a load with a gdb on the machine.  At a debugger's step
    QEMU's run state is `debug`, which a state must not say (no machine
    could be loaded from it), and a debugger lets a machine go on without
    the monitor (flipper_panel.c has both)."""
    work = pathlib.Path(tempfile.mkdtemp(prefix="fe-snap-gdb-"))
    port = free_port()
    g = None
    try:
        code, out, err, took = fe(b, "snapshot", "load", "first", "--detach",
                                  "--paused", "--gdb", "--gdb-port", port,
                                  *QUIET)
        st = state(b)
        if not check("`snapshot load --gdb --paused`: it stands still, with "
                     "a gdb stub on the port", code == 0
                     and f"gdb: :{port}" in out and "it stands still" in out
                     and st.get("stopped") == "paused",
                     f"{out + err} {st.get('stopped')}"):
            return stop(b)
        g = Gdb(b, work, "stepi", "python import time; time.sleep(15)")
        until(lambda: "(debug)" in said_by_qemu(b, "info status")
              or g.p.poll() is not None, 60)
        if g.p.poll() is not None:
            code, text = g.end()
            if "no gdb" in text:
                stop(b)
                return skip("a save and a load with a gdb on the machine",
                            text.strip()[-300:])
        st = state(b)
        check("a gdb gets on the loaded machine and steps: the machine is "
              "the debugger's", g.p.poll() is None
              and st.get("stopped") == "debug",
              f"{st.get('stopped')} {said_by_qemu(b, 'info status')}")
        code, out, err, took = fe(b, "snapshot", "save", "dbg")
        st = state(b)
        check("a save at the debugger's step: saved, and the machine stays "
              "the gdb's", code == 0 and "snapshot dbg:" in out
              and st.get("stopped") == "debug" and g.p.poll() is None,
              f"{out + err} {st.get('stopped')}")
        code, text = g.end()
        g = None
        code2, out, err, took = fe(b, "wait", "--timeout", timeout)
        made = mkdir(b, "snap_dbg")
        ok, said = b.answers()
        check("the gdb leaves: its stub lets the machine go on (not the "
              "launcher), and the machine writes to its card",
              code == 0 and "left it stopped" not in text and code2 == 0
              and made and ok, text[-1500:] + out + err + said)
        stop(b)
        ok, said = up(b, timeout, "snapshot", "load", "dbg")
        seen = folders(b) if ok else set()
        check("a load of the state that was saved at the step: it goes on, "
              "on the save's card", ok and "snapshot dbg:" in said
              and "snap_before" in seen and "snap_dbg" not in seen,
              said + str(sorted(seen)))
        stop(b)
    finally:
        if g:
            g.end()
        shutil.rmtree(work, ignore_errors=True)


def crash_checks(b, timeout):
    """A firmware that has crashed, saved and loaded.  The crash is the one
    of app_check.py: the NFC app, asked to close while it reads.  A debug
    build halts there, and `status` reads its report from the log, which a
    snapshot carries on.  A release build starts again and keeps the
    crash's message in a register of the RTC, which is in the state.
    (Last: the logs of these runs have a crash in them, on purpose.)"""
    label = "a firmware that has crashed, saved and loaded"
    ok, said = up(b, timeout, "snapshot", "load", "first")
    if not check("`snapshot load first` once more, for a crash", ok, said):
        return
    fe(b, "nfc", "none")            # (with a card there the read ends)
    code, out, err, took = fe(b, "app", "NFC")
    if code:
        fe(b, "stop")
        return skip(label, "no NFC app in this firmware to close in the "
                    "middle of a read")
    time.sleep(2)
    fe(b, "press", "ok")            # its first item: Read
    time.sleep(3)
    code, out, err, took = fe(b, "app", "--close")
    if code == 0:
        fe(b, "stop")
        return skip(label, "this firmware's NFC app closes in the middle "
                    "of a read")
    halts = "has halted" in out + err
    if not halts:
        fe(b, "wait", "--timeout", timeout)     # (it starts again)
    before = state(b)
    crash = before.get("halted") if halts else before.get("fault")
    if halts:
        check("a debug build crashes and halts: `status` has where, and "
              "the crash's report", crash and crash.get("kind") == "CRASH"
              and crash.get("report") and crash.get("at"), out + err
              + str(crash))
    else:
        check("a release build crashes and starts again: `status` has the "
              "crash's message", crash, out + err + str(before))
    code, out, err, took = fe(b, "snapshot", "save", "crashed")
    fe(b, "stop")                   # (not stop(): its log has the crash)
    code2, out2, err2, took = fe(b, "snapshot", "load", "crashed",
                                 "--detach", *QUIET)
    if not halts:
        fe(b, "wait", "--timeout", timeout)
    after = state(b)
    check("saved and loaded: `status` says the same of the crash"
          + (" (from the log that the snapshot carries on)" if halts
             else ""),
          code == 0 and code2 == 0 and crash
          and (after.get("halted") if halts else after.get("fault"))
          == crash, f"{out + err + out2 + err2} {crash} -> "
          f"{after.get('halted')} {after.get('fault')}")
    if halts:
        code, out, err, took = fe(b, "wait", "--timeout", 60)
        check("... and `wait` says at once that it has halted",
              code == 1 and "has halted" in err and took < 15, out + err)
        code, out, err, took = fe(b, "wait", "--timeout", 90, "--",
                                  "reset")
        check("`wait -- reset`: it starts again, and its CLI answers",
              code == 0 and "its CLI answers" in out, out + err)
    fe(b, "stop")


KEYS_APP = ROOT / "scripts" / "snapshot_check_app"
KEYS_FAP = "/ext/apps/Debug/fe_keys_check.fap"
# its three lines: which, what each call said, the block that came out
KEYS_LINE = re.compile(r"\[FeKeys\] (\d) (.*?): ([0-9a-f]{32}|-)")


def keys_run(b, slot, between=None):
    """The check's app with a slot of the enclave: its three lines, or None
    when it did not get to the end.  between: what is done while it waits
    for OK with the key loaded (False: that did not work)."""
    log = lambda: ANSI.sub("", b.uart_text())
    wait = f"[FeKeys] slot {slot} loaded 1, waiting for OK"
    waits, ends = log().count(wait), log().count("[FeKeys] 3 ")
    code, out, err, took = fe(b, "app", KEYS_FAP, slot)
    if code or not logged(b, wait, 30, waits + 1):
        return None
    if between and not between():
        return None
    fe(b, "press", "ok")
    if not logged(b, "[FeKeys] 3 ", 30, ends + 1):
        return None
    lines = KEYS_LINE.findall(log())
    return lines[-3:] if len(lines) >= 3 else None


def enclave_checks(b, timeout):
    """An app has a key of the enclave loaded into AES1 at the moment of a
    save, and the loaded machine encrypts with it, unloads it and loads it
    again as a run with no save does.  Slot 11 is the device's own key.
    Slot 1 is a factory key, with the block of the firmware's self-test:
    AES1 answers that from a table, of which a state has no copy (the FUS
    puts it back at the first run).  The app is built by ufbt, whose SDK
    here is the official release's."""
    label = "an app with a key of the enclave loaded at a save"
    if not shutil.which("ufbt"):
        return skip(label, "no `ufbt` on the PATH")
    work = pathlib.Path(tempfile.mkdtemp(prefix="fe-snap-keys-"))
    try:
        folder = work / "fe_keys_check"
        shutil.copytree(KEYS_APP, folder)
        ok, said = up(b, timeout, "run")
        if not check("`run` once more, for an app of the check's own", ok,
                     said):
            return
        code, text = tool(b, folder)
        fap = folder / "dist" / "fe_keys_check.fap"
        if code or not fap.is_file():
            check("`ufbt` builds the check's app", False, text[-1500:])
            return stop(b)
        code, out, err, took = fe(b, "app", "--no-start", "--to",
                                  "/ext/apps/Debug", fap)
        if "which is this firmware's" not in out:
            stop(b)
            return skip(label, "ufbt's SDK is not for this firmware: "
                        + " ".join((out + err).split())[:200])
        for slot, what in ((11, "the device's own key"),
                           (1, "a factory key")):
            name = f"keys{slot}"

            def around():
                code, out, err, took = fe(b, "snapshot", "save", name)
                stop(b)
                code2, out2, err2, took = fe(b, "snapshot", "load", name,
                                             "--detach", *QUIET)
                return code == 0 and code2 == 0

            saved = keys_run(b, slot, around)
            plain = keys_run(b, slot)
            said = [s for _, s, _ in (saved or []) + (plain or [])]
            check(f"slot {slot} ({what}) loaded at the save: the loaded "
                  "machine encrypts on with it, unloads it and loads it "
                  "again, as a run with no save does",
                  saved and plain and saved == plain
                  and all("used 1" in s and not re.search(r"\b0\b", s)
                          for s in said)
                  and saved[2][2] == saved[0][2] != saved[1][2]
                  and (slot != 1 or "the self-test's answer 1"
                       in saved[0][1]),
                  f"with a save, then without: {said}; the blocks "
                  + ("agree" if saved == plain else "differ"))
        stop(b)
    finally:
        shutil.rmtree(work, ignore_errors=True)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("firmware")
    ap.add_argument("--timeout", type=float, default=120.0,
                    help="seconds to wait for the firmware to start")
    ap.add_argument("--keep", action="store_true",
                    help="keep the throwaway device")
    args = ap.parse_args()

    os.umask(0o022)     # (what is the user's alone is so by the launcher)
    b = Bench(f"snapcheck-{int(time.time())}-{os.getpid()}", args.firmware,
              args.timeout)
    loaded = False
    try:
        nothing_checks(b)
        ok, said = up(b, args.timeout, "run", "-f", args.firmware, *HEADER)
        if check("the firmware starts with a sensor, an EEPROM and a flash "
                 "on its header, and its CLI answers", ok, said):
            u1, a1 = save_checks(b)
            if (b.dir / "snapshots" / "first" / "state").is_file() \
                    and load_checks(b, args.timeout, u1, a1):
                refusal_checks(b, args.timeout)
                killed_checks(b, args.timeout)
                front_checks(b, args.timeout)
                rm_checks(b)
                debugger_checks(b, args.timeout)
                enclave_checks(b, args.timeout)
                loaded = True
            stop(b)
            log = "\n".join(logs)
            crash = CRASH.search(log)
            check("no crash in the firmware's logs", not crash,
                  crash and log[max(0, crash.start() - 300):crash.end() + 300])
            if loaded:
                crash_checks(b, args.timeout)
    finally:
        fe(b, "stop")
        if not args.keep:
            shutil.rmtree(b.dir, ignore_errors=True)
    bad = [label for label, ok in results if not ok]
    print(f"{len(results) - len(bad)} of {len(results)} checks passed"
          + (f", {len(skipped)} skipped" if skipped else "")
          + (f"; device kept: {b.dir}" if args.keep else ""))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
