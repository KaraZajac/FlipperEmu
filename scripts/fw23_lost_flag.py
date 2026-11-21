#!/usr/bin/env python3
# Copyright (c) 2026, Kara Zajac
# SPDX-License-Identifier: BSD-3-Clause
"""FW-23 of docs/FIRMWARE_BUGS.md, shown on purpose: a thread flag that an
interrupt sets while furi_thread_flags_clear() is between its two calls of
the kernel is lost, and the NFC poller waits for its chip for ever.

    scripts/fw23_lost_flag.py [--control] [FIRMWARE]

FIRMWARE: an update package of a firmware whose ELF is beside it (default:
the official 1.5.1 that scripts/fetch_firmware.py gets).  Needs the gdb of
the firmware's toolchain, as `./flipperemu gdb` does; without it nothing is
shown and the exit status is 77.

Boots a throwaway device with a MIFARE Classic 1K in the field and dumps
the card in the NFC shell, once as it is: the dump returns.  Then again with
gdb on the device, which holds the NFC thread in two places as a thread of a
higher priority, a long interrupt handler or (in the emulator) a busy host
would: by a branch to itself in a corner of the thread's stack that is not
in use, with everything else running.
- Before a frame is sent (the anticollision, 0.1 ms after the ATQA), until
  the block timer has run out.  Nobody takes its flag then: the sender
  waits for it only while the timer runs.  (In the emulator on an idle host
  the firmware is there before the timer is; beside busy loops it is not,
  nor under gdb, whose breakpoints make the code near them slow: then the
  flag is stale already and there is nothing to hold.)
- The frame goes out, and the poller's first wait returns with that stale
  flag.  furi_hal_nfc_wait_event_common() clears it; gdb goes on to the
  instruction after the first of the two calls in furi_thread_flags_clear()
  (the flags are read, and not yet written back) and holds the thread there
  until the handler of the chip's interrupt has run.
The handler's flag is in the thread's flags then; the write puts the old
value over it, the dump does not return, and the chip's pin (PA2) stays up.
(Where gdb is too slow at the second place and the interrupt is there before
the flags are read, nothing is lost and the next dump is taken: code under a
breakpoint runs slowly the first time.)

--control: the first hold, the stop where the timer's flag is to be cleared,
and gdb gone before the flags are read: the dump returns.  (Not the same stop
between the two calls without the hold: under gdb the way there is slow, the
chip's interrupt is often due by then, and taken in the gap as soon as the
machine goes on, which is the bug again.)

Exit status 0: what is described happened (the flag lost and the dump
stuck; with --control: the dump back).  1: it did not.
"""
import argparse
import pathlib
import re
import shutil
import sys
import tempfile
import threading
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import fe_nfc                                                 # noqa: E402
from fe_cli import FlipperCli, usb_endpoint                   # noqa: E402
from fe_monitor import Monitor                                # noqa: E402
from files_check import Bench, ROOT                           # noqa: E402

FIRMWARE = "firmware/official-1.5.1/flipper-z-f7-update-1.5.1.tgz"
DUMP = "dump -f /ext/nfc/fw23.nfc"
PROMPT = r"\[nfc\]>: "

# What gdb is told.  Nothing here is an address or a line of one version:
# the functions and the names of the flags and the state are the firmware's.
GDB = r"""
set pagination off
set confirm off
python
import gdb
def after_first_call(function, callee):
    start = int(gdb.parse_and_eval("(unsigned) &" + function)) & ~1
    code = gdb.selected_inferior().architecture().disassemble(start, count=48)
    for i, ins in enumerate(code[:-1]):
        if ins["asm"].split()[0] == "bl" and callee in ins["asm"]:
            return code[i + 1]["addr"]
    raise gdb.GdbError("no call of " + callee + " in " + function)
gdb.set_convenience_variable(
    "after", after_first_call("furi_thread_flags_clear", "xTaskGenericNotify"))
end
define fw23_hold
  set $back = $pc
  set $loop = ((unsigned) pxCurrentTCB->pxStack + 64) & ~3
  if $loop + 256 > (unsigned) $sp
    printf "fw23: no room in the thread's stack\n"
    detach
    quit
  end
  set $kept = {unsigned short} $loop
  set {unsigned short} $loop = 0xe7fe
  set $pc = $loop
  tbreak $arg0
  continue
  tbreak *$loop
  continue
  set {unsigned short} $loop = $kept
  set $pc = $back
end
python open("%(ready)s", "w").close()
while 1
  break nfc_iso14443a_poller_trx_sdd_frame
  continue
  set $nfc = instance
  delete
  if (((TCB_t *) pxCurrentTCB)->ulNotifiedValue[1] & FuriHalNfcEventInternalTypeTimerBlockTxExpired) == 0
    if (furi_hal_nfc_timers[FuriHalNfcTimerBlockTx].timer->CR1 & 1) == 0
      printf "fw23: (before this frame no block timer runs, and none has run out: the next frame)\n"
      loop_continue
    end
    fw23_hold furi_hal_nfc_timer_irq_callback
    printf "fw23: %%s held before a frame until the block timer had run out: flags %%#x, and nobody waits for them\n", pxCurrentTCB->pcTaskName, ((TCB_t *) pxCurrentTCB)->ulNotifiedValue[1]
  else
    if (((TCB_t *) pxCurrentTCB)->ulNotifiedValue[1] & FuriHalNfcEventInternalTypeIrq) != 0
      printf "fw23: (at this frame the chip's flag is stale as well: the next frame)\n"
      loop_continue
    end
    printf "fw23: %%s comes to a frame after the block timer has run out: flags %%#x, and nobody waits for them\n", pxCurrentTCB->pcTaskName, ((TCB_t *) pxCurrentTCB)->ulNotifiedValue[1]
  end
  break *&furi_thread_flags_clear if $r0 == FuriHalNfcEventInternalTypeTimerBlockTxExpired && $nfc->comm_state == NfcCommStateWaitTxEnd
  continue
  delete
%(between)s
end
"""

# the demonstration: to the place between the two calls, and held there.
# (Unless the wait that returned the timer's flag had the chip's interrupt
# too and took its frame end: under gdb the way there is slow, and a frame
# with no answer then has no interrupt left to come.)
BETWEEN = r"""  up
  set $ev = event
  down
  if ($ev & FuriHalNfcEventTxEnd) != 0
    printf "fw23: (the chip's frame end was taken in the same wait, and the frame may get no answer: nothing to hold for; the next frame)\n"
    loop_continue
  end
  tbreak *$after
  continue
  printf "fw23: the frame is going out, and %s clears the timer's flag; between the two calls: the flags as read %#x\n", pxCurrentTCB->pcTaskName, cflags
  if (cflags & FuriHalNfcEventInternalTypeIrq) == 0
    fw23_hold furi_hal_nfc_int_callback
    printf "fw23: held there until the handler of the chip's interrupt had run: back in %s, the flags as they are %#x\n", pxCurrentTCB->pcTaskName, ((TCB_t *) pxCurrentTCB)->ulNotifiedValue[1]
    detach
    quit
  end
  printf "fw23: (the chip's interrupt was there before the read: nothing to lose; once more)\n"
"""
# the control: gdb leaves before the flags are read, and the clear runs at
# its own speed (the breakpoints gone).  (Not a stop between the two calls:
# under gdb the way there is slow, the chip's interrupt is often due by
# then, and taken there at once when the machine goes on.)
CONTROL = r"""  printf "fw23: the frame is going out, and %s is to clear the timer's flag; not held: gdb leaves before the flags are read\n", pxCurrentTCB->pcTaskName
  detach
  quit
"""

TRIES = 6


def heard(gdb, timeout):
    """What the launcher's gdb said, when it is done or the time is up.  (A
    gdb that waits for a place that does not come is left to the end of the
    device: it holds the pipe, and is not this process's child to end.)"""
    lines = []
    reader = threading.Thread(target=lambda: lines.extend(gdb.stdout),
                              daemon=True)
    reader.start()
    reader.join(timeout)
    return "".join(lines)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("firmware", nargs="?", default=str(ROOT / FIRMWARE))
    ap.add_argument("--control", action="store_true",
                    help="the same stops, the thread not held")
    args = ap.parse_args()
    b = Bench(f"fw23-{int(time.time())}", args.firmware, 240)
    work = pathlib.Path(tempfile.mkdtemp(prefix="fe-fw23-")).resolve()
    try:
        if not b.start(first=True):
            print("the device did not start")
            return 1
        b.fe("wait", "--timeout", "240")
        card = work / "card.nfc"
        card.write_text(fe_nfc.sample_classic(bytes.fromhex("5E6F7081"), "1K"))
        b.fe("nfc", card)
        script = work / "fw23.gdb"
        ready = work / "ready"          # gdb is on the device and lets it run
        script.write_text(GDB % {"ready": ready,
                                 "between": CONTROL if args.control else BETWEEN})
        mon = Monitor(str(b.dir / "monitor.sock"))
        cli = FlipperCli(usb_endpoint(mon))
        mon.close()                     # (`./flipperemu gdb` asks it for the stub)
        try:
            cli.read_until_prompt(20)
            cli.watch("nfc", PROMPT, timeout=30, stop=False)
            found, _ = cli.watch(DUMP, PROMPT, timeout=60, stop=False)
            print("a dump as it is:", "it returns" if found else "it does NOT return")
            if not found:
                return 1
            gdb = b.bg("gdb", "--", "-batch", "-x", script)
            end = time.time() + 120
            while not ready.exists() and gdb.poll() is None and time.time() < end:
                time.sleep(0.2)
            if not ready.exists():
                print(heard(gdb, 1)[-600:])
                print("no gdb on the device: nothing is shown")
                return 77
            time.sleep(0.5)
            # (a dump that returns with gdb still at it: gdb was too late
            # at the place, with code that it runs for the first time)
            for _ in range(TRIES):
                started = time.time()
                found, _ = cli.watch(DUMP, PROMPT, timeout=30, stop=False)
                took = time.time() - started
                if not found or gdb.poll() is not None:
                    break
            said = heard(gdb, 20)
            notes = [l for l in said.splitlines() if l.startswith("fw23: ")]
            print("\n".join(notes) if notes else said[-800:])
            mon = Monitor(str(b.dir / "monitor.sock"))
            idr = int(mon.cmd("xp/1wx 0x48000010").split()[1], 16)
            mon.close()
            high = bool(idr >> 2 & 1)
            print(f"the dump with gdb at it: "
                  f"{'it returns' if found else 'it does NOT return'} "
                  f"({took:.0f} s); the chip's interrupt pin is "
                  f"{'up' if high else 'down'}")
            if args.control:
                return 0 if any("not held" in n for n in notes) and found else 1
            m = re.search(r"back in \w+, the flags as they are (0x[0-9a-f]+|0)",
                          said)
            lost = bool(m and int(m.group(1), 16) & 2)   # ...TypeIrq
            return 0 if lost and not found and high else 1
        finally:
            cli.close()
    finally:
        b.stop()
        shutil.rmtree(b.dir, ignore_errors=True)
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
