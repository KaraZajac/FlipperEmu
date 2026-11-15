#!/usr/bin/env python3
# Copyright (c) 2026, Kara Zajac
# SPDX-License-Identifier: BSD-3-Clause
"""Check `./flipperemu run --coverage` and `./flipperemu coverage`.

    scripts/coverage_check.py firmware/official-1.5.1/flipper-z-f7-update-1.5.1.tgz

Boots a throwaway device.  A run without `--coverage` has none, and says how
to get it.  A run with it: `status` says so; `coverage` says what ran of the
firmware, by function and line where the firmware's ELF is here (the
official builds': scripts/fetch_firmware.py puts it beside the package), in
bytes where it is not; `coverage reset` counts anew, and a CLI command
that is then given once, and twice more, has its function called once, and
three times; the list of functions, those of which nothing ran, the lcov
file, the dump.

Then an app, where `ufbt` is on the PATH with this firmware's SDK (skipped
without): the example is built and started, its keys pressed, and left; and
after it has ended its coverage is asked for, by its id: the functions that
ran and how often, the one that did not, the lines.  Another build of the
app than the one that ran is told.  A stopped device says the same of its
last run; a start without `--coverage` has none again.  Exit status 0 = pass.
"""
import argparse
import json
import os
import pathlib
import re
import shutil
import sys
import tempfile
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from files_check import Bench, CRASH, ROOT, check, results   # noqa: E402
from ufbt_check import logged, tool                          # noqa: E402
import check_limit  # noqa: E402
import fe_cov  # noqa: E402

PLUGIN = ROOT / "build" / "plugins" / "libfe_cov.so"


def report(b, *more, cwd=None):
    """`coverage --json ...` -> (exit status, the report or None, the text)"""
    code, text = b.fe("coverage", "--json", *more)
    try:
        return code, json.loads(text), text
    except ValueError:
        return code, None, text


def function(rep, name):
    return next((f for f in (rep or {}).get("by_function", [])
                 if f["name"] == name), None)


def firmware(b, work):
    """What ran of the firmware.  -> whether its ELF is here"""
    code, text = b.fe("status")
    check("`status` of a run with --coverage: its row says that the code "
          "that runs is counted",
          code == 0 and "coverage : on: " in text
          and "blocks of code ran since its start" in text, text)
    code, text = b.fe("coverage")
    with_elf = "no ELF of the firmware" not in text
    if not with_elf:
        check("`coverage`, of a firmware whose ELF is not here: the bytes of "
              "the flash that ran since its start, and how to name an ELF",
              code == 0 and "since its start" in text and "--elf FILE" in text
              and any(int(n.replace(",", "")) > 20000 for n in
                      re.findall(r"([\d,]+) bytes of the flash",
                                               text)), text)
    else:
        code, rep, raw = report(b)
        check("`coverage`: what ran of the firmware since its start, by its "
              "ELF: bytes, functions and lines, and the files with the most "
              "lines that ran"
              + (f" ({rep['functions_ran']} of {rep['functions']} functions, "
                 f"{rep['lines_ran']} of {rep['lines']} lines)" if rep else ""),
              code == 0 and rep and "since its start" in rep["since"]
              and 500 < rep["functions_ran"] < rep["functions"]
              and 5000 < rep["lines_ran"] < rep["lines"]
              and 30000 < rep["bytes_ran"] < rep["bytes"]
              and len(rep["by_file"]) > 100
              and rep["by_file"][0]["lines_ran"] > 100
              and "lines ran" in text and "functions" in text, text + raw[:600])
    before = (json.loads(b.fe("status", "--json")[1]).get("coverage") or {})
    code, text = b.fe("coverage", "reset")
    after = (json.loads(b.fe("status", "--json")[1]).get("coverage") or {})
    check("`coverage reset`: counted from then on (fewer blocks ran since "
          f"than before: {after.get('executed')}, {before.get('executed')}), "
          "and `status` says since when",
          code == 0 and "counted from now on" in text
          and after.get("resets") == 1 and before.get("resets") == 0
          and after.get("executed", 1 << 30) < before.get("executed", 0)
          and "since the last `coverage reset`" in b.fe("status")[1],
          f"{text} {before} {after}")
    if not with_elf:
        return False
    name = "cli_command_date"
    code, rep, raw = report(b, "--functions", name)
    was = function(rep, name)
    b.fe("cli", "date")
    code, rep, raw = report(b, "--functions", name)
    once = function(rep, name)
    b.fe("cli", "date", "date")
    code, rep, raw = report(b, "--functions", name)
    thrice = function(rep, name)
    check(f"a CLI command after the reset (`date`): {name} was not called, "
          "then once, and after two more three times; more of its bytes ran "
          "than none, and not all",
          was and was["calls"] == 0 and was["bytes_ran"] == 0
          and once and once["calls"] == 1 and thrice and thrice["calls"] == 3
          and 0 < thrice["bytes_ran"] < thrice["bytes"]
          and "cli_main_commands.c:" in thrice["at"]
          and "since the last `coverage reset`" in rep["since"],
          f"{was} {once} {thrice} {raw[:400]}")
    code, text = b.fe("coverage", "--functions", "cli_command_*")
    code2, missed = b.fe("coverage", "--functions", "cli_command_*", "--missed")
    check("`--functions PATTERN`: the functions that fit, each with its "
          "calls, its bytes and its place; `--missed`: those of which "
          "nothing ran, and not the one that did",
          code == 0 and code2 == 0 and name in text
          and "cli_main_commands.c:" in text and "calls" in text
          and name not in missed and "cli_command_" in missed
          and "of which nothing ran" in missed, text[-800:] + missed[-600:])
    lcov = work / "firmware.info"
    code, text = b.fe("coverage", "--lcov", lcov, "--source", "/src/fw")
    info = lcov.read_text() if lcov.exists() else ""
    record = info[info.find("cli_main_commands.c") - 200:][:6000]
    check("`--lcov FILE`: an lcov file of it: a record to a source file, "
          "under --source's folder, with its functions' calls and its "
          "lines' counts",
          code == 0 and info.count("end_of_record") > 100
          and "SF:/src/fw/applications/services/cli/cli_main_commands.c"
          in info and f"FNDA:3,{name}" in record
          and re.search(r"^DA:\d+,3$", record, 8)
          and re.search(r"^DA:\d+,0$", record, 8), text)
    return True


def dump(b, work):
    out = work / "ran.txt"
    code, text = b.fe("coverage", "dump", out)
    try:
        blocks = fe_cov.read_dump(out)
    except (OSError, fe_cov.Error) as e:
        blocks = []
        text += str(e)
    check("`coverage dump FILE`: the blocks that ran, each with its address, "
          f"its bytes and its count ({len(blocks)}), most of them in the "
          "flash",
          code == 0 and len(blocks) > 500
          and sum(1 for a, n, c, d in blocks if 0x08000000 <= a < 0x08100000)
          > len(blocks) * 0.8 and all(c > 0 and n > 0 for a, n, c, d in blocks),
          text)


def app(b, work):
    """An app's coverage.  -> None: no SDK for this firmware"""
    folder = work / "fe_hello"
    folder.mkdir()
    for name in ("application.fam", "hello.c"):
        shutil.copy(ROOT / "examples" / "hello" / name, folder)
    tool(b, folder, "-c")
    code, text = tool(b, folder)
    fap = folder / "dist" / "fe_hello.fap"
    if not check("`ufbt` alone: the example app is built",
                 code == 0 and fap.is_file(), text[-1500:]):
        return False
    code, text = b.fe("app", "--no-start", "--to", "/ext/apps/fe-check", fap)
    b.fe("rm", "-r", "/ext/apps/fe-check")
    if "which is this firmware's" not in text:
        print("  ufbt's SDK is not for this firmware: an app's coverage is "
              "not tried")
        return None
    b.fe("coverage", "reset")
    code, text = tool(b, folder, "launch")
    started = logged(b, "[FeHello] started", 60)
    b.fe("press", "up", "down")
    two = logged(b, "which makes 2")
    b.fe("press", "back")
    left = logged(b, "left after 2")
    time.sleep(1)
    check("the example app: started with `ufbt launch`, two keys, and left "
          "with Back", code == 0 and started and two and left, text[-800:])
    code, rep, raw = report(b, "--app", fap, "--functions")
    f = {x["name"]: x for x in (rep or {}).get("by_function", [])}
    check("`coverage --app FILE.fap`, after the app has ended: its code is "
          "known by its bytes among what ran in RAM (the debug ELF beside "
          "the .fap has its lines); its entry was called once, its draw and "
          "input callbacks more than once, its signal handler not; "
          + (f"{rep['lines_ran']} of {rep['lines']} lines ran" if rep else ""),
          code == 0 and rep and rep["not_this_file"] == 0
          and f.get("fe_hello_app", {}).get("calls") == 1
          and f.get("hello_draw", {}).get("calls", 0) >= 2
          and f.get("hello_input", {}).get("calls", 0) >= 2
          and f.get("hello_signal", {}).get("bytes_ran", 1) == 0
          and "hello.c:" in f["fe_hello_app"]["at"]
          and 20 < rep["lines_ran"] < rep["lines"]
          and rep["functions_ran"] == rep["functions"] - 1, raw[:2500])
    code, text = b.fe("coverage", "--app", "fe_hello", "--functions",
                      "--missed")
    check("`--app fe_hello`: the app by its id (what ufbt built of it "
          "last); `--missed` has the function that did not run, alone",
          code == 0 and "hello_signal" in text and "fe_hello_app" not in
          text.split("functions of which nothing ran")[-1]
          and "its code was at 0x200" in text, text)
    lcov = work / "app.info"
    code, text = b.fe("coverage", "--app", fap, "--lcov", lcov)
    info = lcov.read_text() if lcov.exists() else ""
    check("its lcov file: the app's source file, the entry's call, lines "
          "that ran and lines that did not",
          code == 0 and f"SF:{folder / 'hello.c'}" in info
          and "FNDA:1,fe_hello_app" in info and "FNDA:0,hello_signal" in info
          and re.search(r"^DA:\d+,[1-9]", info, 8)
          and re.search(r"^DA:\d+,0$", info, 8), text + info[:600])
    # another build than the one that ran: a line more in its source
    source = folder / "hello.c"
    source.write_text(source.read_text().replace(
        'FURI_LOG_I(TAG, "started");',
        'FURI_LOG_I(TAG, "started");\n    FURI_LOG_I(TAG, "and changed");', 1))
    code, text = tool(b, folder)
    code, text = b.fe("coverage", "--app", fap)
    check("another build of the app than the one that ran (a line more in "
          "its source, built and not started): said, with what of it is "
          "the same",
          "and changed" in source.read_text()
          and ("the app that ran is another build than this file" in text
               or "another build than the one on the Flipper" in text), text)
    return True


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("firmware")
    ap.add_argument("--timeout", type=float, default=120.0,
                    help="seconds to wait for the firmware to start")
    ap.add_argument("--keep", action="store_true",
                    help="keep the throwaway device")
    args = ap.parse_args()
    check_limit.arm(900)
    if not PLUGIN.exists():
        print(f"{PLUGIN} is not built: scripts/setup_qemu.sh builds it",
              file=sys.stderr)
        return 2

    b = Bench(f"covcheck-{int(time.time())}-{os.getpid()}", args.firmware,
              args.timeout)
    work = pathlib.Path(tempfile.mkdtemp(prefix="fe-cov-")).resolve()
    try:
        if check("boot, without --coverage: 'Startup complete'",
                 b.start(first=True), b.uart_text()[-2000:]):
            time.sleep(2)
            code, text = b.fe("coverage")
            code2, text2 = b.fe("coverage", "reset")
            check("a run without --coverage: `coverage` and `coverage reset` "
                  "say that it has none, and how to get it; `status` has no "
                  "row of it",
                  code == 1 and "this run has no coverage" in text
                  and "run --coverage" in text and code2 == 1
                  and "run --coverage" in text2
                  and "coverage :" not in b.fe("status")[1], text + text2)
            b.stop()
            code, text = b.fe("coverage")
            check("stopped: its last run left no coverage, said so",
                  code == 1 and "left no coverage" in text, text)
        if check("boot with --coverage: 'Startup complete'",
                 b.start(more=("--coverage",)), b.uart_text()[-2000:]):
            b.fe("wait", "--timeout", args.timeout)
            with_elf = firmware(b, work)
            dump(b, work)
            tried = None
            if shutil.which("ufbt"):
                tried = app(b, work)
            else:
                print("  (no `ufbt` on the PATH: an app's coverage is not "
                      "tried)")
            b.stop()
            code, text = b.fe("coverage")
            check("stopped: `coverage` says what ran in its last run, from "
                  "what the emulator wrote as it ended",
                  code == 0 and "in its last run" in text
                  and ("functions" in text if with_elf else
                       "bytes of the flash ran" in text), text)
            if tried:
                # (by its file, which is the changed build: what ufbt built
                # last under the id may be another check's by now)
                fap = work / "fe_hello" / "dist" / "fe_hello.fap"
                code, rep, raw = report(b, "--app", fap, "--functions")
                check("... and of the app, which is not in any memory now: "
                      "its changed build is told as another than the one "
                      "that ran",
                      code in (0, 1) and ("another build" in raw
                                          or (rep or {}).get("not_this_file")),
                      raw[:800])
            if check("a start without --coverage after it",
                     b.start(), b.uart_text()[-2000:]):
                b.fe("wait", "--timeout", args.timeout)
                code, text = b.fe("coverage")
                check("... has none: the run before's is gone with the start",
                      code == 1 and "this run has no coverage" in text, text)
                b.stop()
                code, text = b.fe("coverage")
                check("... and none is left when it has stopped",
                      code == 1 and "left no coverage" in text, text)
        log = b.uart_text(every=True)
        crash = CRASH.search(log)
        check("no crash in the firmware's logs", not crash,
              crash and log[max(0, crash.start() - 300):crash.end() + 300])
    finally:
        b.stop()
        shutil.rmtree(work, ignore_errors=True)
        if not args.keep:
            shutil.rmtree(b.dir, ignore_errors=True)
    bad = [label for label, ok in results if not ok]
    print(f"{len(results) - len(bad)} of {len(results)} checks passed"
          + (f"; device kept: {b.dir}" if args.keep else ""))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
