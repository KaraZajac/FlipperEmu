# Copyright (c) 2026, Kara Zajac
# SPDX-License-Identifier: BSD-3-Clause
"""gdb's side of `./flipperemu gdb`: sourced by gdb, once it is on a device.

The launcher's command file (devices/NAME/gdb.init) sets FLIPPEREMU_GDB and
sources this:

    python FLIPPEREMU_GDB = {"fapscript": "…/flipperapps.py", "apps": "DIR"}
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
  and `bt` shows where it was.  `continue` lets the firmware restart, as it
  does without a debugger.
"""
import gdb

FLAG = "furi_hal_debug_gdb_session_active"
APP_START = "flipper_application_alloc_thread"
CRASH = "__furi_crash_implementation"


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


class AppStart(gdb.Breakpoint):
    """Where the loader has an app in memory and is about to start it.  Not a
    stop: the app's symbols are read, and the firmware goes on."""

    def __init__(self, sync):
        super().__init__(APP_START, internal=True)
        self.sync = sync

    def stop(self):
        try:
            self.sync(None)
        except Exception as e:      # the firmware's script, whichever it is:
            # an error that got out of here would be a stop of the firmware
            say(f"no symbols of the app that starts: {type(e).__name__}: {e}")
        return False


class FlipperEmu:
    def __init__(self, fapscript, apps):
        self.at_app_start = self.at_crash = None
        if address(FLAG) is None:
            say("no symbols of a Flipper firmware: breakpoints by address, and "
                "no symbols of apps")
            return
        flag_off()
        if fapscript and address(APP_START) is not None:
            self.apps(fapscript, apps)
        where = address(CRASH)
        if where is not None:
            self.at_crash = gdb.Breakpoint(f"*{where:#x}", internal=True)
            self.at_crash.silent = True
            gdb.events.stop.connect(self.stopped)

    def apps(self, fapscript, apps):
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
        try:
            gdb.execute(f"fap-set-debug-elf-root {apps}")
        except gdb.error as e:
            say(f"no symbols of apps: {e}")
            return
        self.at_app_start = AppStart(helper.handle_stop)

    def stopped(self, event):
        if (isinstance(event, gdb.BreakpointEvent)
                and self.at_crash in event.breakpoints):
            say(f"the firmware crashed: {crash_message()}")
            say("`bt` shows where; `continue` lets it restart")


FLIPPEREMU = FlipperEmu(**globals().get("FLIPPEREMU_GDB",
                                        {"fapscript": "", "apps": ""}))
