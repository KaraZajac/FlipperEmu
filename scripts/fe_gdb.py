# Copyright (c) 2026, Kara Zajac
# SPDX-License-Identifier: BSD-3-Clause
"""gdb's side of `./flipperemu gdb`: sourced by gdb, once it is on a device.

The launcher's command file (devices/NAME/gdb.init) sets FLIPPEREMU_GDB and
sources this:

    python FLIPPEREMU_GDB = {"fapscript": "…/flipperapps.py", "apps": ["DIR"],
                             "links": "DIR", "name": "the device's"}
    source …/scripts/fe_gdb.py

What it is for.  The firmware has a flag that says a debugger is there
(furi_hal_debug_gdb_session_active), which the firmware's own gdb script
sets.  With it the firmware runs a `bkpt` where an app starts and where it
crashes, and a debug probe halts the processor there.  QEMU's gdb stub has
no probe behind it: the instruction is a fault there, and the firmware
would crash on it.  So the flag stays off, and breakpoints of gdb's stand
at those two places:

- where the loader has an app in memory and is about to start it.  The
  firmware's script (which knows that firmware's list of loaded apps) reads
  the app's symbols there, and the firmware goes on.  A breakpoint that was
  set in the app's code before it was loaded is in place by then.
- where a crash begins.  gdb stops, this says what the firmware says of it,
  and `bt` shows where it was.  After `continue` the firmware does what it
  does without a debugger: a release build restarts, and a debug build (what
  `./fbt` builds unless told otherwise) halts and waits.  This says which.
"""
import os

import gdb

FLAG = "furi_hal_debug_gdb_session_active"
APP_START = "flipper_application_alloc_thread"
CRASH = "__furi_crash_implementation"
RESET = "furi_hal_power_reset"          # how a release build ends a crash


def say(text):
    print(f"[flipperemu] {text}")


def flag_off(*_):
    try:
        gdb.execute(f"set variable {FLAG} = 0", to_string=True)
    except gdb.error:
        pass                    # no such firmware, or no symbols: no flag


def address(function):
    """Where a function of the firmware begins, or None"""
    try:
        return int(gdb.parse_and_eval(f"(unsigned long)&{function}")) & ~1
    except gdb.error:
        return None


def crash_message():
    """The firmware hands it over in r12: a text, or one of two marks"""
    try:
        r12 = int(gdb.parse_and_eval("(unsigned long)$r12"))
        if r12 == 0:
            return "Fatal Error"
        if r12 == 1:
            return "furi_assert failed"
        if r12 == 2:
            return "furi_check failed"
        text = bytes(gdb.selected_inferior().read_memory(r12, 200))
        return text.split(b"\0")[0].decode("utf-8", "replace")
    except (gdb.error, ValueError):
        return "?"


def restarts():
    """Whether this firmware starts again after a crash.  A release build's
    crash ends with a reset; a debug build's has none, and ends in a loop
    that waits for a debugger.  None: its code could not be read"""
    try:
        return RESET in gdb.execute(f"disassemble {CRASH}", to_string=True)
    except gdb.error:
        return None


class Links:
    """The firmware's script looks for an app's debug ELF in one folder, and
    there can be two: the stock apps', from the update server, and what was
    built here.  So it is given a folder of links to the files of both, made
    again as each app starts: one may have been built since.  Where two have
    a file of one name, the later folder's is the one."""

    def __init__(self, folder, sources):
        self.folder, self.sources = folder, sources

    def refresh(self):
        want = {}
        for source in self.sources:
            try:
                names = os.listdir(source)
            except OSError:
                continue                # not there: nothing was built yet
            for name in names:
                if name.endswith(".elf"):
                    want[name] = os.path.join(source, name)
        os.makedirs(self.folder, exist_ok=True)
        for name in os.listdir(self.folder):
            path = os.path.join(self.folder, name)
            if os.path.islink(path) and os.readlink(path) != want.get(name):
                os.unlink(path)
        for name, target in want.items():
            path = os.path.join(self.folder, name)
            if not os.path.lexists(path):
                os.symlink(target, path)


class AppStart(gdb.Breakpoint):
    """Where the loader has an app in memory and is about to start it.  Not a
    stop: the app's symbols are read, and the firmware goes on."""

    def __init__(self, sync, before=None):
        super().__init__(APP_START, internal=True)
        self.sync, self.before = sync, before

    def stop(self):
        try:
            if self.before:
                self.before()
            self.sync(None)
        except Exception as e:      # the firmware's script, whichever it is:
            # an error that got out of here would be a stop of the firmware
            say(f"no symbols of the app that starts: {type(e).__name__}: {e}")
        return False


class FlipperEmu:
    def __init__(self, fapscript, apps, links="", name=""):
        self.at_app_start = self.at_crash = None
        self.name = name
        if address(FLAG) is None:
            say("no symbols of a Flipper firmware: breakpoints by address, and "
                "no symbols of apps")
            return
        flag_off()
        if fapscript and apps and address(APP_START) is not None:
            self.apps(fapscript, apps, links)
        where = address(CRASH)
        if where is not None:
            self.at_crash = gdb.Breakpoint(f"*{where:#x}", internal=True)
            self.at_crash.silent = True
            self.restarts = restarts()
            gdb.events.stop.connect(self.stopped)

    def apps(self, fapscript, apps, links):
        import __main__
        try:
            gdb.execute(f"source {fapscript}")
            helper = __main__.helper
        except (gdb.error, AttributeError) as e:
            say(f"no symbols of apps: {fapscript}: {e}")
            return
        # (it has set the flag by now, and would at every stop; the firmware
        # has not run in between)
        helper.set_debug_mode = flag_off
        flag_off()
        root, before = apps[-1], None
        if len(apps) > 1 and links:
            try:
                before = Links(links, apps).refresh
                before()
                root = links
            except OSError as e:
                before = None
                say(f"no symbols of the stock apps: {e}")
        try:
            gdb.execute(f"fap-set-debug-elf-root {root}")
        except gdb.error as e:
            say(f"no symbols of apps: {e}")
            return
        self.at_app_start = AppStart(helper.handle_stop, before)

    def stopped(self, event):
        if (isinstance(event, gdb.BreakpointEvent)
                and self.at_crash in event.breakpoints):
            say(f"the firmware crashed: {crash_message()}")
            if self.restarts is False:
                device = f"-d {self.name} " if self.name else ""
                say("`bt` shows where; after `continue` it halts: this is a "
                    "debug build, which waits there for a debugger")
                say(f"(`./flipperemu {device}reset` in another terminal "
                    "starts it again)")
            else:
                say("`bt` shows where; `continue` lets it restart")


FLIPPEREMU = FlipperEmu(**globals().get("FLIPPEREMU_GDB",
                                        {"fapscript": "", "apps": []}))
