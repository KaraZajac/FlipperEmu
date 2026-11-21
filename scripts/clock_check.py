#!/usr/bin/env python3
# Copyright (c) 2026, Kara Zajac
# SPDX-License-Identifier: BSD-3-Clause
"""Check the machine's clock: it is the CPU's own time, and what a busy host
takes of the emulated CPU is not in it.

    scripts/clock_check.py firmware/official-1.5.1/flipper-z-f7-update-1.5.1.tgz

A throwaway device starts and has the firmware sum a file of its card
(`storage md5`: the CPU at work all the while), in turns with the emulator's
threads on one core of the host's where a busy loop runs, and on the others.
Beside the loop the host takes its half of the CPU's time: the work lasts
longer by the host's clock, as long as apart from it by the machine's
(`qom-get /machine clock`), and what was taken is in `clock-steal`, which is
what the two clocks differ by.  (In turns, and each turn with the one
after it: a host that is slower itself for a while, as one that throttles,
is slower for the emulated CPU too, and no clock has that out.)  The
machine's timers are on time beside the loop as well, where the main loop
waits for its turn on the core: the CPU's thread runs those that are due
(the machine's `cpu-timers`), and the meter (`run -- -machine
clock-meter=on`, read as `clock-late`) has few of them half a millisecond
late.

An idle Flipper's clock goes with the host's: time that the CPU sleeps is
its own, and so is what a wake-up takes of the host's time, up to 60 us of
each (`clock-wake`), with the firmware waking a thousand times a second
while the USB cable is in.

One access to a device is one instruction, however long the device's model
takes over it: a block of the card is read in one, in about a millisecond.
That is not the machine's time (`clock-device`), and the machine's timers
are not late for it: of those run while the firmware read the file, few
were half a millisecond late (without this: one for nearly every block).

A snapshot keeps what was taken out: a device that is paused, saved, stopped
and loaded stands at the same microsecond, with the same count.  Then the
device with `run --host-clock`: nothing is taken out, and the work beside
the busy loop lasts longer by the machine's clock too.  Last with
`-machine cpu-timers=off`, where the main loop alone runs the timers:
beside the loop one in twenty or more comes half a millisecond late.

Linux only (the kernel's count of a thread's time, and threads put on a
core), and a host with two cores or more: otherwise nothing is checked and
the exit status is 77.  Exit status 0 = pass.
"""
import argparse
import json
import math
import os
import pathlib
import re
import shutil
import subprocess
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from fe_cli import FlipperCli, usb_endpoint                   # noqa: E402
from fe_monitor import Monitor                                # noqa: E402
from files_check import Bench, CRASH, check, results          # noqa: E402
import check_limit  # noqa: E402

QUIET = ("--headless", "--audio", "none")
FOLDERS = ("/ext/infrared/assets", "/ext/subghz/assets", "/ext/nfc/assets")
TURNS = 7               # the work apart from the loop and beside it, so often
TURN_S = 0.45           # ... and about so long apart from it
BUSY = "import time\nend = time.time() + 300\nwhile time.time() < end: pass"


class Device:
    """The device's monitor and CLI, for as long as the check talks to it
    (the launcher's own commands need the monitor: `shut` before them)"""

    def __init__(self, b):
        self.b = b
        self.mon = self.cli = None

    def open(self):
        self.mon = Monitor(str(self.b.dir / "monitor.sock"))
        self.cli = FlipperCli(usb_endpoint(self.mon))
        self.cli.read_until_prompt(20)
        return self

    def shut(self):
        for part in (self.cli, self.mon):
            if part:
                part.close()
        self.mon = self.cli = None

    def prop(self, name):
        said = self.mon.cmd(f"qom-get /machine {name}").strip()
        return int(said) if re.fullmatch(r"-?\d+", said) else said

    def pid(self):
        return json.loads((self.b.dir / "run.json").read_text())["pid"]

    def work(self, file, times):
        """The file summed so often: (seconds by the machine's clock, by
        the host's, seconds taken out, the sums, the meter's counts meanwhile
        (late: how many timers were run late by how much, as clock-late's
        count has them; ran: how often the CPU's thread ran timers))"""
        c0, s0, h0 = self.prop("clock"), self.prop("clock-steal"), time.monotonic()
        l0 = late(self)
        sums = {self.cli.run(f"storage md5 {file}", timeout=240).strip()
                for _ in range(times)}
        host = time.monotonic() - h0
        l1 = late(self)
        meter = {"late": [x - y for x, y in zip(l1["count"], l0["count"])],
                 "ran": l1.get("cpu-ran", 0) - l0.get("cpu-ran", 0),
                 "up-to-us": l1["up-to-us"]}
        return ((self.prop("clock") - c0) / 1e6, host,
                (self.prop("clock-steal") - s0) / 1e6, sums, meter)


def largest(dev):
    """The largest file of the folders that every firmware ships"""
    found = []
    for folder in FOLDERS:
        said = dev.cli.run(f"storage list {folder}")
        found += [(int(size), f"{folder}/{name}")
                  for name, size in re.findall(r"\[F\]\s+(\S+)\s+(\d+)b", said)]
    return max(found)[1] if found else None


def late(dev):
    """The meter's counts (the machine's clock-late, with clock-meter=on)"""
    return json.loads(json.loads(
        dev.mon.cmd("qom-get /machine clock-late").strip()))


def on_cores(pid, cores):
    for task in os.listdir(f"/proc/{pid}/task"):
        try:
            os.sched_setaffinity(int(task), cores)
        except OSError:                 # (a thread that has just ended)
            pass


class Loop:
    """A busy loop on one core of the host's, and the emulator's threads
    beside it on that core, or apart from it on the others"""

    def __init__(self, pid):
        self.pid = pid
        self.all = os.sched_getaffinity(pid)
        self.core = {max(self.all)}
        self.rest = self.all - self.core

    def __enter__(self):
        self.loop = subprocess.Popen([sys.executable, "-c", BUSY],
                                     stdin=subprocess.DEVNULL)
        os.sched_setaffinity(self.loop.pid, self.core)
        self.apart()
        time.sleep(0.3)
        return self

    def __exit__(self, *exc):
        self.loop.kill()
        self.loop.wait()
        on_cores(self.pid, self.all)

    def beside(self):
        on_cores(self.pid, self.core)
        time.sleep(0.05)

    def apart(self):
        on_cores(self.pid, self.rest)
        time.sleep(0.05)


def turns(dev, file, times):
    """The work apart from the busy loop and beside it, in turns:
    [(apart, beside)], each as Device.work gives it"""
    got = []
    with Loop(dev.pid()) as loop:
        for _ in range(TURNS):
            loop.apart()
            apart = dev.work(file, times)
            loop.beside()
            got.append((apart, dev.work(file, times)))
    return got


def middle(values):
    values = sorted(values)
    return values[len(values) // 2]


def summed(got):
    """Of such turns: the sums; how many times as long the work lasts
    beside the loop, by the machine's clock and by the host's (the middle
    one of the turns); beside the loop in all, the seconds of the machine's
    clock, of the host's, and taken out; and all of it in words"""
    sums = set().union(*(a[3] | b[3] for a, b in got))
    machine = middle(b[0] / a[0] for a, b in got)
    host = middle(b[1] / a[1] for a, b in got)
    beside = [sum(b[i] for a, b in got) for i in range(3)]
    said = (f"apart from the loop {sum(a[0] for a, b in got):.2f} s by the "
            f"machine's clock and {sum(a[1] for a, b in got):.2f} s by the "
            f"host's; beside it {beside[0]:.2f} s and {beside[1]:.2f} s, "
            f"{beside[2]:.2f} s taken out; a turn beside it {machine:.2f} "
            f"times as long by the machine's clock, {host:.2f} times by the "
            "host's")
    return sums, machine, host, beside, said


def lateness(got):
    """Of such turns, the timers run during the turns beside the loop: (how
    many were run, how many of them more than half a millisecond late, how
    often the CPU's thread ran some), and that in words"""
    counts = [sum(col) for col in zip(*(b[4]["late"] for a, b in got))]
    edges = got[0][1][4]["up-to-us"]
    over = sum(counts[edges.index(500) + 1:])
    ran = sum(b[4]["ran"] for a, b in got)
    said = (f"beside the loop {sum(counts)} timers run, {over} of them more "
            f"than 0.5 ms late, the CPU's thread ran some {ran} times; up to "
            f"{edges} us and more: {counts}")
    return sum(counts), over, ran, said


def one_sum(sums):
    return len(sums) == 1 and re.fullmatch(r"[0-9a-f]{32}", next(iter(sums)))


def own_checks(b, dev):
    """-> (the file, how often it is summed in a turn), or None"""
    check("the machine's clock is the CPU's own: `host-clock` is off, and "
          "the clock and what it left out can be read",
          dev.prop("host-clock") == "false"
          and isinstance(dev.prop("clock"), int) and dev.prop("clock") > 0
          and isinstance(dev.prop("clock-steal"), int),
          f"{dev.prop('host-clock')} {dev.prop('clock')} "
          f"{dev.prop('clock-steal')}")
    file = largest(dev)
    if not check("a file on the card for the firmware to sum", file):
        return None
    once = dev.work(file, 1)[0]
    times = max(1, min(20, math.ceil(TURN_S / max(once, 0.01))))
    got = turns(dev, file, times)
    sums, machine, host, beside, said = summed(got)
    check(f"{file} summed {2 * TURNS * times} times, in turns apart from a "
          "busy loop and beside it on its core: the same sum each time",
          one_sum(sums), sums)
    check("the host takes its part: beside the loop the work lasts 1.7 times "
          "as long by the host's clock, or more", host >= 1.7, said)
    check("... and about as long by the machine's clock (0.7 to 1.4 times)",
          0.7 <= machine <= 1.4, said)
    check("... what the host took is in `clock-steal` (a quarter of that "
          "time, or more), and is what the two clocks differ by",
          beside[2] >= 0.25 * beside[1]
          and abs(beside[1] - beside[0] - beside[2]) <= 0.3, said)
    print(f"        ({said})", flush=True)
    run, over, ran, said = lateness(got)
    check("... and the machine's timers are on time beside the loop as "
          "well: the CPU's thread runs those that are due, and of those run "
          "during the turns beside it, fewer than one in fifty is more than "
          "half a millisecond late (`clock-late`)",
          dev.prop("cpu-timers") == "true" and ran > 0 and run > 0
          and over * 50 < run, said)
    print(f"        ({said})", flush=True)

    c0, w0, h0 = dev.prop("clock"), dev.prop("clock-wake"), time.monotonic()
    time.sleep(4)
    clock, host = (dev.prop("clock") - c0) / 1e6, time.monotonic() - h0
    woke = (dev.prop("clock-wake") - w0) / 1e6
    said = (f"{clock:.3f} s of the machine's in {host:.3f} s of the host's "
            f"({100 * clock / host:.1f} %); its wake-ups took {woke:.3f} s "
            "of them")
    check("an idle Flipper's clock goes with the host's: time that the CPU "
          "sleeps is its own",
          0.9 * host <= clock <= host + 0.1, said)
    check("... and so is what its wake-ups take, in `clock-wake`: up to "
          "60 us of each, of some hundreds a second with the cable in",
          0.002 * host <= woke <= 0.07 * host, said)
    print(f"        ({said})", flush=True)

    size = re.search(r"size: (\d+)b", dev.cli.run(f"storage stat {file}"))
    blocks = 2 * times * int(size[1]) // 512 if size else 0
    l0, d0, h0 = late(dev), dev.prop("clock-device"), time.monotonic()
    dev.work(file, 2 * times)
    host = time.monotonic() - h0
    l1, device = late(dev), (dev.prop("clock-device") - d0) / 1e6
    counts = [a - b for a, b in zip(l1["count"], l0["count"])]
    over = sum(counts[l1["up-to-us"].index(500) + 1:])
    said = (f"{blocks} blocks in {host:.2f} s of the host's, {device:.2f} s "
            f"of them in long accesses to devices; timers run late, up to "
            f"{l1['up-to-us']} us and more: {counts}")
    check("a block of the card is one access to a device, which takes its "
          "model long: that is not the machine's time (`clock-device`: a "
          "fifth of the host's time for the file, or more)",
          l1["metered"] and blocks and device >= 0.2 * host, said)
    check("... and the machine's timers are not late for it: of those run "
          "meanwhile, fewer than one for every twenty blocks were more than "
          "500 us late (`clock-late`)",
          blocks and over <= blocks / 20, said)
    print(f"        ({said})", flush=True)
    return file, times


def snapshot_checks(b, dev, timeout):
    """A device that is paused, saved, stopped and loaded: the same clock"""
    dev.shut()
    b.fe("pause")
    dev.mon = Monitor(str(b.dir / "monitor.sock"))
    before = dev.prop("clock"), dev.prop("clock-steal")
    dev.shut()
    code, out = b.fe("snapshot", "save", "clock")
    b.stop()
    if not check("a paused device saved (`snapshot save`) and stopped",
                 code == 0 and before[1] > 0, f"{out} {before}"):
        return False
    code, out = b.fe("snapshot", "load", "clock", "--paused", "--detach", *QUIET)
    after = None
    if code == 0:
        dev.mon = Monitor(str(b.dir / "monitor.sock"))
        after = dev.prop("clock"), dev.prop("clock-steal")
        dev.shut()
    check("`snapshot load`: the machine stands at the microsecond of the "
          "save, with the same time taken out",
          code == 0 and after == before, f"{out} {before} -> {after}")
    code, out = b.fe("continue")
    code2, out2 = b.fe("wait", "--timeout", timeout)
    ok = code == 0 and code2 == 0
    if ok:
        dev.open()
        clock = dev.prop("clock"), dev.prop("clock-steal")
        ok = before[0] < clock[0] < before[0] + 60e6 and clock[1] >= before[1]
        out2 += f" {before} -> {clock}"
    check("... and goes on from there: its CLI answers, the clock a little "
          "later", ok, out + out2)
    dev.shut()
    b.fe("stop")
    return True


def host_checks(b, file, times):
    """The same device with `run --host-clock`"""
    if not check("the device starts with `run --host-clock`",
                 b.start(more=["--host-clock"]), b.uart_text()[-800:]):
        return
    b.fe("wait", "--timeout", b.timeout)
    dev = Device(b).open()
    try:
        check("`host-clock` is on, and no wake-up and no device access is "
              "counted",
              dev.prop("host-clock") == "true"
              and dev.prop("clock-wake") == 0
              and dev.prop("clock-device") == 0,
              f"{dev.prop('host-clock')} {dev.prop('clock-wake')} "
              f"{dev.prop('clock-device')}")
        sums, machine, host, beside, said = summed(turns(dev, file, times))
        check("the host's clock: beside the busy loop the work lasts longer "
              "by the machine's clock as well (1.7 times, or more), and "
              "nothing is taken out",
              one_sum(sums) and machine >= 1.7
              and abs(beside[0] - beside[1]) <= 0.3
              and dev.prop("clock-steal") == 0, said)
        print(f"        ({said})", flush=True)
    finally:
        dev.shut()
        b.stop()


def off_checks(b, file, times):
    """The same device with `-machine cpu-timers=off`: the main loop alone
    runs the timers"""
    if not check("the device starts with `-machine cpu-timers=off`",
                 b.start(more=["--", "-machine",
                               "cpu-timers=off,clock-meter=on"]),
                 b.uart_text()[-800:]):
        return
    b.fe("wait", "--timeout", b.timeout)
    dev = Device(b).open()
    try:
        got = turns(dev, file, times)
        run, over, ran, said = lateness(got)
        check("with `cpu-timers=off` the main loop alone runs the timers, "
              "and beside the loop more of them are late: one in twenty or "
              "more by more than half a millisecond",
              dev.prop("cpu-timers") == "false" and ran == 0 and run > 0
              and over * 20 >= run, said)
        print(f"        ({said})", flush=True)
    finally:
        dev.shut()


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("firmware")
    ap.add_argument("--timeout", type=int, default=240)
    ap.add_argument("--keep", action="store_true", help="keep the device")
    args = ap.parse_args()
    if not sys.platform.startswith("linux") or \
            not hasattr(os, "sched_setaffinity"):
        print("not Linux: the machine's clock is the host's here, and "
              "nothing is checked")
        return 77
    if len(os.sched_getaffinity(0)) < 2:
        print("one core: nothing for the emulator to be on apart from a "
              "busy loop, and nothing is checked")
        return 77
    check_limit.arm(900)
    b = Bench(f"clock-{os.getpid()}", args.firmware, args.timeout)
    dev = Device(b)
    try:
        if not check("the device starts", b.start(
                first=True, more=["--", "-machine", "clock-meter=on"]),
                     b.uart_text()[-800:]):
            return 1
        b.fe("wait", "--timeout", args.timeout)
        dev.open()
        work = own_checks(b, dev)
        if work and snapshot_checks(b, dev, args.timeout):
            host_checks(b, *work)
            off_checks(b, *work)
        log = b.uart_text(every=True)
        crash = CRASH.search(log)
        check("no crash in the firmware's log", not crash,
              crash and log[max(0, crash.start() - 300):crash.end() + 300])
    finally:
        dev.shut()
        b.stop()
        b.fe("stop")
        if not args.keep:
            shutil.rmtree(b.dir, ignore_errors=True)
    bad = [label for label, ok in results if not ok]
    print(f"{len(results) - len(bad)} of {len(results)} checks passed"
          + (f"; device kept: {b.dir}" if args.keep else ""))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
