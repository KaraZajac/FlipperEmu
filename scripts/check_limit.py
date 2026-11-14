# Copyright (c) 2026, Kara Zajac
# SPDX-License-Identifier: BSD-3-Clause
"""A check that hangs ends: its time limit.

    import check_limit
    check_limit.arm(600)        # first thing in main()

Past the limit the check says so (a FAIL), shows where each of its threads
stands, stops the emulators it started and exits with status 124, as
timeout(1) does.  Its emulators: the devices it named (`device(NAME)`;
files_check.Bench names its own), stopped as `flipperemu stop` stops one,
then whatever it started that still runs (its launchers and QEMUs, found by
their parents), with SIGTERM and then SIGKILL.  The devices' folders are
kept for a look.  At its end, a check that ran its course says how long it
took.  A check that is stopped (SIGTERM, as `kill` sends it, or SIGHUP, a
terminal that closes) stops its emulators the same way.  (A check that runs
the launcher in its own process, as the window check does, has the
launcher's handler instead, which stops the emulator.)

FLIPPEREMU_CHECK_LIMIT changes every check's limit: seconds (`900`), a
factor (`x3`, on a busy machine) or `0` (none).
"""
import atexit
import faulthandler
import os
import pathlib
import signal
import subprocess
import sys
import threading
import time

ROOT = pathlib.Path(__file__).resolve().parent.parent
ENV = "FLIPPEREMU_CHECK_LIMIT"
EXIT = 124                              # (timeout(1)'s)

_devices = []
_state = {"t0": time.monotonic(), "limit": None, "timer": None}


def device(name):
    """A device of the check's own: stopped when its time is up"""
    if name not in _devices:
        _devices.append(name)


def limit_of(default):
    """The limit in seconds that FLIPPEREMU_CHECK_LIMIT makes of a check's
    own, or None: no limit"""
    word = os.environ.get(ENV, "").strip().lower()
    try:
        if not word:
            seconds = default
        elif word.startswith("x"):
            seconds = default * float(word[1:])
        else:
            seconds = float(word)
    except ValueError:
        sys.exit(f"{ENV}={word}: seconds (900), a factor (x3) or 0 (none)")
    return seconds if seconds > 0 else None


def arm(default):
    """The check's limit, counted from its start; and its emulators stopped
    with it when it is stopped"""
    for sig in (signal.SIGTERM, signal.SIGHUP):
        if signal.getsignal(sig) == signal.SIG_DFL:
            signal.signal(sig, _stopped)
    seconds = limit_of(default)
    _state["limit"] = seconds
    if seconds is None:
        return
    left = seconds - (time.monotonic() - _state["t0"])
    timer = threading.Timer(max(left, 0), _time_up)
    timer.daemon = True                 # (it must not keep a check alive)
    timer.start()
    _state["timer"] = timer
    atexit.register(_took)


def _took():
    timer = _state["timer"]
    if timer:
        timer.cancel()
    print(f"  (it took {time.monotonic() - _state['t0']:.0f} s; its time "
          f"limit is {_state['limit']:.0f} s, {ENV} changes it)", flush=True)


def _descendants():
    """The processes this one started, and theirs: [pid], children first"""
    parent = {}
    for entry in os.listdir("/proc"):
        if entry.isdigit():
            try:
                with open(f"/proc/{entry}/stat") as f:
                    stat = f.read()
            except OSError:
                continue
            # (the name, in brackets, may have spaces of its own)
            fields = stat[stat.rfind(")") + 2:].split()
            parent[int(entry)] = int(fields[1])
    found, todo = [], [os.getpid()]
    while todo:
        pid = todo.pop()
        for child, ppid in parent.items():
            if ppid == pid:
                found.append(child)
                todo.append(child)
    return found


def _time_up():
    limit = _state["limit"]
    print(f"\n  FAIL  time limit: the check still runs after {limit:.0f} s, "
          "its limit: it hangs, or this machine is too busy for it "
          f"({ENV}=x2 gives every check twice its time).  Where each of its "
          "threads stands:", flush=True)
    sys.stderr.flush()
    faulthandler.dump_traceback(file=sys.stdout, all_threads=True)
    sys.stdout.flush()
    _clean_up()
    os._exit(EXIT)


def _stopped(signum, frame):
    print(f"\n  FAIL  stopped by {signal.Signals(signum).name}: its "
          "emulators are stopped too", flush=True)
    _clean_up()
    os._exit(128 + signum)


def _clean_up():
    """The devices it named stopped, what it started ended; their folders
    kept"""
    for name in _devices:
        try:
            subprocess.run([sys.executable, str(ROOT / "flipperemu"), "-d",
                            name, "stop"], capture_output=True, timeout=30,
                           stdin=subprocess.DEVNULL)
        except (OSError, subprocess.SubprocessError):
            pass
        if (ROOT / "devices" / name).is_dir():
            print(f"  (its device is kept for a look: devices/{name})",
                  flush=True)
    left = _descendants()
    for sig in (signal.SIGTERM, signal.SIGKILL):
        for pid in left:
            try:
                os.kill(pid, sig)
            except OSError:
                pass
        end = time.monotonic() + 5
        while time.monotonic() < end:
            left = [pid for pid in left if _alive(pid)]
            if not left:
                break
            time.sleep(0.1)


def _alive(pid):
    """Still there, and not a zombie that waits for its parent"""
    try:
        with open(f"/proc/{pid}/stat") as f:
            return f.read().rsplit(")", 1)[1].split()[0] != "Z"
    except (OSError, IndexError):
        return False
