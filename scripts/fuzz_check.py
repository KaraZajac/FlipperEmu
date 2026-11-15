#!/usr/bin/env python3
# Copyright (c) 2026, Kara Zajac
# SPDX-License-Identifier: BSD-3-Clause
"""Check `./flipperemu fuzz`.

    scripts/fuzz_check.py firmware/official-1.5.1/flipper-z-f7-update-1.5.1.tgz

Boots a throwaway device.  Without `--coverage` a fuzzer runs blindly and
says so; a target that is none, a `cli` target with no place for the input
and a device that does not run are refused.  With `--coverage`: the `ir`,
`subghz` and `rpc` targets take their own seeds and some inputs made from
them, keep those that reached new code, and leave the firmware running; an
input given again (`--replay`).

Then, where `ufbt` is on the PATH with this firmware's SDK (skipped
without), the fuzzer has something to find: the check's own app
(scripts/fuzz_check_app), which crashes on an input that passes three
checks of its bytes, one after another.  The fuzzer has to find it, by the
inputs it kept on the way (one that passed the first check, one that
passed two); the crash's folder has the input and the firmware's report,
and given again it crashes again.  The app's other signal writes to
address 0: the crash's report and `status` have the place of the CPU's
fault, as the emulator noted it.  Exit status 0 = pass.
"""
import argparse
import os
import pathlib
import re
import shutil
import sys
import tempfile
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from files_check import Bench, ROOT, check, results          # noqa: E402
from ufbt_check import logged, tool                          # noqa: E402
import check_limit  # noqa: E402

PLUGIN = ROOT / "build" / "plugins" / "libfe_cov.so"
APP = ROOT / "scripts" / "fuzz_check_app"
SIGNAL = "loader signal 100 {hex}"


def numbers(text):
    """"99 inputs in 12 s (8 a second), 17 kept ..., 0 crashes" of the last
    such line -> (inputs, kept, crashes)"""
    hit = re.findall(r"(\d+) inputs in .*?, (\d+) kept.*?, (\d+) crashes", text)
    return tuple(map(int, hit[-1])) if hit else (0, 0, -1)


def files(folder):
    return sorted(folder.iterdir()) if folder.is_dir() else []


def blind(b, work):
    code, text = b.fe("fuzz", "ir", "--runs", 14, "--seed", 1, "--out",
                      work / "blind")
    inputs, kept, crashes = numbers(text)
    check("a run without --coverage: `fuzz ir` says that it has no coverage "
          "and makes its inputs from the seeds alone; they are given all the "
          f"same ({inputs})",
          code == 0 and "this run has no coverage" in text and inputs >= 14
          and crashes == 0 and "run --coverage" in text, text)
    code, text = b.fe("fuzz", "nosuch")
    code2, text2 = b.fe("fuzz", "cli", "--command", "loader info")
    check("a target that is none: the targets named; `cli` with no place "
          "for the input in its command: said",
          code == 1 and "one of subghz, ir, rpc, cli" in text
          and code2 == 1 and "{hex}" in text2 and "{file}" in text2,
          text + text2)


def targets(b, work):
    for name, runs, least in (("ir", 30, 3), ("subghz", 30, 3), ("rpc", 11, 3)):
        out = work / name
        code, text = b.fe("fuzz", name, "--runs", runs, "--seed", 1, "--out", out)
        inputs, kept, crashes = numbers(text)
        ok, said = b.answers()
        if name == "subghz" and code == 2 and "failed to start" in text:
            check("`fuzz subghz`: this firmware's `subghz rx` does not "
                  "start the internal radio from the CLI: said, and the "
                  "firmware's CLI answers after it",
                  "does not start the internal radio" in text and ok,
                  text + said)
            continue
        check(f"`fuzz {name} --runs {runs}`: its seeds"
              + ("" if name == "rpc" else " and inputs made from them")
              + f", {kept} of {inputs} kept for the new code they reached "
              "(corpus/), no crash, and the firmware's CLI answers after it",
              code == 0 and inputs >= runs and kept >= least and crashes == 0
              and len(files(out / "corpus")) == kept
              and not files(out / "crashes") and ok
              and "no coverage" not in text, text + said)
    nfc(b, work)
    first = files(work / "ir" / "corpus")[0]
    code, text = b.fe("fuzz", "ir", "--replay", first, "--out", work / "again")
    check("`fuzz ir --replay FILE`: a kept input given again, and what came "
          "of it: no crash and no hang",
          code == 0 and "1 inputs again: no crash and no hang" in text, text)


def nfc(b, work):
    """`fuzz nfc`: cards of the fuzzer's making, read by the NFC shell's
    `dump`"""
    out = work / "nfc"
    code, text = b.fe("fuzz", "nfc", "--runs", 16, "--seed", 1, "--out", out,
                      timeout=900)
    if code == 2 and "no `nfc` shell" in text:
        print("  (this firmware's CLI has no NFC shell: `fuzz nfc` says so)")
        return
    inputs, kept, crashes = numbers(text)
    ok, said = b.answers()
    # (Momentum's shell with a Type 4 tag and an EMV card, entry 158;
    # Unleashed's `dump` with an EMV card)
    known = ("NULL pointer dereference" in text
             or "[dump] furi_check failed" in text)
    check("`fuzz nfc --runs 16`: a card of each kind and cards made from "
          f"them, held to the Flipper and dumped by its NFC shell: {kept} of "
          f"{inputs} kept for the new code they reached; "
          + ("no crash" if not crashes else
             f"{crashes} crashes, of the one kind that this firmware's shell "
             "has with a Type 4 tag or an EMV card, each found and gone on "
             "from") + "; and the firmware's CLI answers after it",
          inputs >= 16 and kept >= 5 and ok
          and ((code == 0 and crashes == 0)
               or (code == 1 and known and "of 1 kinds" in text)),
          text[-1500:] + said)
    # a card that is ISO14443-3B and no more: the official 1.5.1's `dump`
    # never comes back from it (entry 156), which is a hang to the fuzzer
    card = work / "3b.card"
    card.write_text("# ISO14443-3B\ntype iso14443b\nuid 1a 2b 3c 4d\n"
                    "appdata 00 12 34 e1\nprotinfo 00 80 80\napp none\n")
    code, text = b.fe("fuzz", "nfc", "--replay", card, "--out", work / "nfc-3b",
                      timeout=600)
    ok, said = b.answers()
    hung = "hang after input 1" in text
    check("`fuzz nfc --replay` of a card that is ISO14443-3B and no more: "
          + ("this firmware's `dump` never comes back from it: a hang, said "
             "and kept (crashes/001-hang), and the Flipper started again"
             if hung else "read, with no crash and no hang")
          + "; its CLI answers after it",
          ok and ((code == 1 and hung
                   and (work / "nfc-3b" / "crashes" / "001-hang").is_dir())
                  or (code == 0 and "no crash and no hang" in text)),
          text[-800:] + said)


def planted(b, work):
    """-> None: no SDK for this firmware"""
    folder = work / "fe_fuzz_check"
    shutil.copytree(APP, folder)
    tool(b, folder, "-c")
    code, text = tool(b, folder)
    fap = folder / "dist" / "fe_fuzz_check.fap"
    if not check("`ufbt` alone: the check's own app is built "
                 "(scripts/fuzz_check_app)", code == 0 and fap.is_file(),
                 text[-1500:]):
        return False
    code, text = b.fe("app", "--no-start", "--to", "/ext/apps/fe-check", fap)
    b.fe("rm", "-r", "/ext/apps/fe-check")
    if "which is this firmware's" not in text:
        print("  ufbt's SDK is not for this firmware: the app with a crash "
              "to find is not tried")
        return None

    def launch():
        seen = b.uart_text().count("waiting for inputs")
        code, text = tool(b, folder, "launch")
        return code == 0 and logged(b, "waiting for inputs", 60, seen + 1), text
    ok, text = launch()
    if not check("the app is started (`ufbt launch`)", ok, text[-800:]):
        return False
    out = work / "found"
    start = time.time()
    code, text = b.fe("fuzz", "cli", "--command", SIGNAL, "--max", 4,
                      "--first", "--seconds", 400, "--seed", 1, "--out", out,
                      timeout=600)
    took = time.time() - start
    inputs, kept, crashes = numbers(text)
    crash = out / "crashes" / "001-crash"
    try:
        found = (crash / "input.bin").read_bytes()
        report = (crash / "report.txt").read_text()
    except OSError:
        found, report = b"", ""
    corpus = [f.read_bytes() for f in files(out / "corpus")]
    check("`fuzz cli --command 'loader signal 100 {hex}' --first`: the crash "
          "behind the app's three checks is found, after "
          f"{inputs} inputs in {took:.0f} s; the corpus has the steps to it "
          "(an input that passed the first check, one that passed two)",
          code == 1 and crashes == 1 and found[:3] == b"\x80\x7f\x10"
          and any(c[:1] == b"\x80" and c[1:2] != b"\x7f" for c in corpus)
          and any(c[:2] == b"\x80\x7f" for c in corpus)
          and "fe_fuzz_check: found" in text, text[-1500:])
    check("the crash's folder: the input, the inputs before it, and the "
          "firmware's report with its message",
          found and "fe_fuzz_check: found" in report
          and (crash / "before-1.bin").is_file()
          and "target cli, seed 1" in report, report[:800])
    code, text = b.fe("wait", "--timeout", 120)
    ok, text2 = launch()
    code2, again = b.fe("fuzz", "cli", "--command", SIGNAL, "--replay", crash,
                        "--out", work / "again2")
    code3, wait = b.fe("wait", "--timeout", 120)
    check("the firmware started again after the crash; the app started "
          "again and `--replay` of the crash's folder: the same crash",
          code == 0 and ok and code2 == 1 and "fe_fuzz_check: found" in again
          and "inputs again:" in again and code3 == 0, text + text2 + again)
    ok, text2 = launch()
    kept = next(f for f in files(out / "corpus") if f.read_bytes()[:1] == b"\0")
    code, text = b.fe("fuzz", "cli", "--command", SIGNAL, "--replay", kept,
                      "--out", work / "again3")
    check("... and of a kept input that is no crash: none",
          ok and code == 0 and "no crash and no hang" in text, text2 + text)
    # a fault of the CPU's: its place
    zero = work / "zero.bin"
    zero.write_bytes(b"\0")
    code, text = b.fe("fuzz", "cli", "--command", "loader signal 101 {hex}",
                      "--replay", zero, "--out", work / "fault")
    try:
        report = (work / "fault" / "crashes" / "001-crash" / "report.txt"
                  ).read_text()
    except OSError:
        report = ""
    b.fe("wait", "--timeout", 120)
    code2, status = b.fe("status")
    row = next((line for line in status.splitlines()
                if line.startswith("fault")), "")
    check("the app's write to address 0 (signal 101): a crash whose report "
          "begins with the CPU's fault as the emulator noted it, a MemManage "
          "fault at the app's code in RAM that touched 0x00000000; `status` "
          "has it in a row of its own after the restart",
          code == 1 and "NULL pointer dereference" in text
          and re.search(r"the CPU's fault: a MemManage fault at 0x200[0-9a-f]"
                        r"{5}, called from 0x[0-9a-f]{8}", report)
          and "it touched 0x00000000" in report
          and "the CPU's last fault (1 since the emulator started): a "
          "MemManage fault at 0x200" in row and "it touched 0x00000000" in row,
          text + report[:600] + row)
    return True


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("firmware")
    ap.add_argument("--timeout", type=float, default=120.0,
                    help="seconds to wait for the firmware to start")
    ap.add_argument("--keep", action="store_true",
                    help="keep the throwaway device")
    args = ap.parse_args()
    check_limit.arm(1500)
    if not PLUGIN.exists():
        print(f"{PLUGIN} is not built: scripts/setup_qemu.sh builds it",
              file=sys.stderr)
        return 2

    b = Bench(f"fuzzcheck-{int(time.time())}-{os.getpid()}", args.firmware,
              args.timeout)
    work = pathlib.Path(tempfile.mkdtemp(prefix="fe-fuzz-")).resolve()
    try:
        code, text = b.fe("fuzz", "ir")
        check("a device that does not run: `fuzz` says so, and how to start "
              "one with coverage",
              code == 1 and "is not running" in text and "--coverage" in text,
              text)
        if check("boot, without --coverage: 'Startup complete'",
                 b.start(first=True), b.uart_text()[-2000:]):
            b.fe("wait", "--timeout", args.timeout)
            blind(b, work)
            b.stop()
        if check("boot with --coverage: 'Startup complete'",
                 b.start(more=("--coverage",)), b.uart_text()[-2000:]):
            b.fe("wait", "--timeout", args.timeout)
            targets(b, work)
            if shutil.which("ufbt"):
                planted(b, work)
            else:
                print("  (no `ufbt` on the PATH: the app with a crash to "
                      "find is not tried)")
            b.stop()
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
