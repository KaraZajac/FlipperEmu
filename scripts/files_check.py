#!/usr/bin/env python3
# Copyright (c) 2026, Kara Zajac
# SPDX-License-Identifier: BSD-3-Clause
"""Check the launcher's file commands: ls, push, pull, rm, mkdir.

    scripts/files_check.py firmware/official-1.5.1/flipper-z-f7-update-1.5.1.tgz

Boots a throwaway device and copies a folder to its SD card through the
firmware (the `storage` commands of its CLI), lists it, copies it back and
compares.  Then it stops the device and does the same in the card's image
(mtools), which has to show what the firmware wrote; starts it again, and the
firmware has to show what was written into the image.  Also: what is said
when a path is wrong, when two commands want the USB serial port, and when
the cable is unplugged; and what is left when it goes wrong in the middle: a
transfer interrupted, a command killed, the card pulled out.  Exit status
0 = pass.
"""
import argparse
import filecmp
import os
import pathlib
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time

ROOT = pathlib.Path(__file__).resolve().parent.parent
CRASH = re.compile(r"\[CRASH\]|furi_check failed|HardFault|Kernel is Dead")
results = []


def check(label, ok, detail=""):
    results.append((label, bool(ok)))
    print(f"  {'PASS' if ok else 'FAIL'}  {label}", flush=True)
    if not ok and detail:
        print("        " + str(detail).strip().replace("\n", "\n        "), flush=True)
    return ok


def same_tree(a, b):
    """Two folders with the same files, folders and bytes"""
    c = filecmp.dircmp(a, b)
    if c.left_only or c.right_only or c.funny_files:
        return False
    _, differ, errors = filecmp.cmpfiles(a, b, c.common_files, shallow=False)
    return not differ and not errors and all(
        same_tree(os.path.join(a, d), os.path.join(b, d)) for d in c.common_dirs)


class Bench:
    def __init__(self, name, firmware, timeout):
        self.name, self.firmware, self.timeout = name, firmware, timeout
        self.dir = ROOT / "devices" / name
        self.uart = self.dir / "uart.log"
        self.proc = self.log = None
        self.runs = 0

    def fe(self, *args, timeout=600):
        """A launcher command: (exit status, what it printed)"""
        r = subprocess.run([sys.executable, str(ROOT / "flipperemu"), "-d",
                            self.name, *map(str, args)], capture_output=True,
                           text=True, timeout=timeout, stdin=subprocess.DEVNULL)
        return r.returncode, r.stdout + r.stderr

    def bg(self, *args):
        """A launcher command that is not waited for"""
        return subprocess.Popen([sys.executable, str(ROOT / "flipperemu"), "-d",
                                 self.name, *map(str, args)], text=True,
                                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT)

    def answers(self):
        """The CLI gives two commands their two answers, and nothing else:
        (it does, what it said)"""
        code, text = self.fe("cli", "loader info", "uptime")
        lines = text.strip().splitlines()
        return (code == 0 and len(lines) == 2 and "pplication" in lines[0]
                and lines[1].startswith("Uptime:")), text

    def start(self, first=False):
        self.dir.mkdir(parents=True, exist_ok=True)
        if self.uart.exists():          # each run its own log
            self.uart.rename(self.dir / f"uart-run{self.runs}.log")
        self.runs += 1
        cmd = [sys.executable, str(ROOT / "flipperemu"), "-d", self.name, "run",
               "--headless", "--audio", "none", "--serial", f"file:{self.uart}"]
        if first:
            cmd += ["-f", self.firmware]
        self.log = open(self.dir / "qemu.log", "ab")
        self.proc = subprocess.Popen(cmd, stdout=self.log, stderr=subprocess.STDOUT)
        end = time.time() + self.timeout
        while time.time() < end and "Startup complete" not in self.uart_text():
            time.sleep(0.2)
        return "Startup complete" in self.uart_text()

    def uart_text(self, every=False):
        """What the firmware logged in this run (every: in all of them)"""
        text = ""
        for f in sorted(self.dir.glob("uart*.log")) if every else [self.uart]:
            try:
                text += f.read_text(errors="replace")
            except OSError:
                pass
        return text

    def stop(self):
        if self.proc:
            self.fe("stop", timeout=30)
            try:
                self.proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                self.proc.kill()
            self.log.close()
            self.proc = None


def make_tree(src):
    """What is copied: names and sizes that have gone wrong somewhere before"""
    (src / "tree/sub/deep").mkdir(parents=True)
    (src / "tree/empty").mkdir()
    (src / "hello.txt").write_text("hello\n")
    (src / "empty.bin").write_bytes(b"")
    (src / "tree/five.bin").write_bytes(os.urandom(5000))
    (src / "tree/chunk.bin").write_bytes(os.urandom(8192))      # one read chunk
    (src / "tree/big.bin").write_bytes(os.urandom(70000))
    (src / "tree/sub/with space.txt").write_text("a")
    (src / "tree/sub/deep/brack[1].sub").write_text("b")
    (src / "tree/sub/deep/prompt.bin").write_bytes(b"ends like a prompt\r\n>: ")
    (src / "tree/sub" / LONG).write_text("c")
    (src / "key.sub").write_text("Filetype: Flipper SubGhz Key File\n")


LONG = "long-name-" * 6 + ".txt"


def file_checks(b, src, out, way):
    """Copy the folder there, list it, copy it back: `way` says through what"""
    code, text = b.fe("push", src / "tree", src / "hello.txt", src / "empty.bin",
                      "/ext/fe-check")
    check(f"{way}: push a folder and two files",
          code == 0 and "9 copied" in text and f"({way})" in text, text)
    code, text = b.fe("ls", "-R", "/ext/fe-check")
    want = sorted(["empty.bin", "hello.txt", "tree/", "tree/empty/", "tree/sub/",
                   "tree/sub/deep/", "tree/big.bin", "tree/chunk.bin",
                   "tree/five.bin", "tree/sub/with space.txt",
                   "tree/sub/deep/brack[1].sub", "tree/sub/deep/prompt.bin",
                   "tree/sub/" + LONG])
    got = sorted(line[12:] for line in text.splitlines())
    sizes = {line[12:]: line[:10].strip() for line in text.splitlines()}
    check(f"{way}: ls -R shows all of it, with the sizes",
          code == 0 and got == want and sizes.get("tree/big.bin") == "70000"
          and sizes.get("empty.bin") == "0" and sizes.get("tree/") == "", text)
    code, text = b.fe("ls", "/ext/fe-check")
    check(f"{way}: ls shows folders first",
          code == 0 and [line[12:] for line in text.splitlines()]
          == ["tree/", "empty.bin", "hello.txt"], text)
    code, text = b.fe("push", src / "tree", "/ext/fe-check")
    check(f"{way}: a second push copies nothing",
          code == 0 and "0 copied, 7 unchanged" in text, text)
    shutil.rmtree(out, ignore_errors=True)
    code, text = b.fe("pull", "/ext/fe-check", out)
    check(f"{way}: pull brings the same bytes back",
          code == 0 and "9 copied" in text
          and same_tree(src / "tree", out / "fe-check/tree")
          and (out / "fe-check/empty.bin").stat().st_size == 0, text)
    code, text = b.fe("pull", "/ext/fe-check/hello.txt", out / "renamed.txt")
    check(f"{way}: pull one file under a new name",
          code == 0 and (out / "renamed.txt").read_text() == "hello\n", text)
    code, text = b.fe("push", src / "hello.txt", "/ext/fe-check/tree/sub/new.txt")
    code2, text2 = b.fe("ls", "/ext/fe-check/tree/sub/new.txt")
    check(f"{way}: push one file under a new name",
          code == 0 and code2 == 0 and text2.split() == ["6", "new.txt"],
          text + text2)
    code, text = b.fe("push", src / "key.sub")
    code2, text2 = b.fe("rm", "/ext/subghz/key.sub")
    check(f"{way}: a .sub pushed with no folder named goes to /ext/subghz",
          code == 0 and "-> /ext/subghz/key.sub" in text and code2 == 0,
          text + text2)
    code, text = b.fe("mkdir", "/ext/fe-check/a/b/c", "/ext/fe-check/tree")
    code2, text2 = b.fe("ls", "/ext/fe-check/a/b")
    check(f"{way}: mkdir makes the folders above too",
          code == 0 and code2 == 0 and text2.strip() == "c/", text + text2)

    code, text = b.fe("rm", "/ext/fe-check/tree")
    check(f"{way}: rm of a folder with files in it asks for -r",
          code == 1 and "-r removes it" in text, text)
    code, text = b.fe("rm", "/ext/fe-check/nothing")
    code2, text2 = b.fe("rm", "-f", "/ext/fe-check/nothing")
    check(f"{way}: rm of what is not there says so; rm -f does not",
          code == 1 and "no such file or folder" in text
          and code2 == 0 and not text2.strip(), text + text2)
    code, text = b.fe("push", src / "hello.txt", "/ext/fe-check/tree")
    code2, text2 = b.fe("push", src / "hello.txt", "/ext/fe-check/hello.txt/x.txt")
    check(f"{way}: a file pushed at a folder goes into it; none goes into a file",
          code == 0 and "/ext/fe-check/tree/hello.txt" in text
          and code2 == 1 and "is a file on the Flipper" in text2, text + text2)
    code, text = b.fe("ls", "/ext/fe-check/tree/five.bin")
    check(f"{way}: ls of a file", code == 0 and text.split() == ["5000", "five.bin"],
          text)


def rough_checks(b, big, out):
    """It goes wrong in the middle.  /ext/fe-rough/big.bin is on the card."""
    def ended(p, sig, after):
        time.sleep(after)
        p.send_signal(sig)
        try:
            text = p.communicate(timeout=120)[0]
        except subprocess.TimeoutExpired:
            p.kill()
            text = p.communicate()[0] + "\n(did not end)"
        return p.returncode, text

    # the firmware cannot be told to stop sending a file: it is read to its
    # end and dropped, and then the port is free
    code, text = ended(b.bg("pull", "/ext/fe-rough/big.bin", out / "back.bin"),
                       signal.SIGINT, 4.0)
    ok, said = b.answers()
    check("a pull interrupted: said in a line, nothing copied, and the next "
          "command gets its answer",
          code == 130 and "was not copied" in text and "Traceback" not in text
          and not (out / "back.bin").exists() and ok, text + said)

    code, text = ended(b.bg("push", big, "/ext/fe-rough/again.bin"),
                       signal.SIGINT, 5.0)
    code2, listed = b.fe("ls", "/ext/fe-rough")
    ok, said = b.answers()
    check("a push interrupted: said in a line, no half of the file left, and "
          "the next command gets its answer",
          code == 130 and "off the card again" in text
          and "Traceback" not in text and code2 == 0
          and "again.bin" not in listed and "big.bin" in listed and ok,
          text + listed + said)

    # No goodbye at all.  What the killed one had written and the firmware
    # had not taken is not left in the port for the next command, ...
    code, text = ended(b.bg("push", big, "/ext/fe-rough/killed.bin"),
                       signal.SIGKILL, 5.0)
    ok, said = b.answers()
    code2, listed = b.fe("ls", "/ext/fe-rough")
    check("a push killed: the next command gets its answer; the file's "
          "beginning is on the card",
          ok and code2 == 0 and "killed.bin" in listed, said + listed)
    # ... and what the firmware was still saying to it is not the next one's
    # to read.  (`top` ends when its listener goes.  A command that does not
    # look, `storage tree` of a full card, is finished first, to nobody, and
    # the CLI is gone that long: on a Flipper on a desk as well.)
    for after in (2.3, 2.65, 3.0):
        ended(b.bg("cli", "top"), signal.SIGKILL, after)
        ok, said = b.answers()
        if not ok:
            break
    check("a `cli` killed while the firmware talks: the next command gets "
          "its own answer, and only that", ok, said)

    p = b.bg("push", big, "/ext/fe-rough/third.bin")
    time.sleep(5.0)
    b.fe("sd", "eject")
    try:
        text = p.communicate(timeout=60)[0]
    except subprocess.TimeoutExpired:
        p.kill()
        text = p.communicate()[0] + "\n(did not end)"
    code2, text2 = b.fe("ls", "/ext")       # (the firmware's CLI says that)
    check("the card pulled out in the middle of a push: the push ends and "
          "says so, and ls says that there is no card",
          p.returncode == 1
          and ("was it pulled out?" in text or "SD card" in text)
          and code2 == 1 and "no SD card" in text2, text + text2)
    b.fe("sd", "insert")
    end = time.time() + 15
    while True:
        code, text = b.fe("ls", "/ext/fe-rough")
        if code == 0 or time.time() > end:
            break
        time.sleep(0.5)
    code2, text2 = b.fe("rm", "-r", "/ext/fe-rough")
    ok, said = b.answers()      # (some firmware has its commands on the card)
    check("pushed in again: the card is back, with what was on it",
          code == 0 and "big.bin" in text and code2 == 0 and ok,
          text + text2 + said)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("firmware")
    ap.add_argument("--timeout", type=float, default=60)
    ap.add_argument("--keep", action="store_true", help="keep the device directory")
    args = ap.parse_args()

    b = Bench(f"filescheck-{int(time.time())}-{os.getpid()}", args.firmware, args.timeout)
    work = pathlib.Path(tempfile.mkdtemp(prefix="fe-files-"))
    src, out = work / "src", work / "out"
    src.mkdir()
    make_tree(src)
    big = work / "big.bin"
    big.write_bytes(os.urandom(800_000))
    try:
        if not check("boot: 'Startup complete'", b.start(first=True)):
            return 1
        file_checks(b, src, out, "through the firmware")

        code, text = b.fe("ls", "/nowhere")
        check("a path that is not on the Flipper is refused",
              code == 1 and "begin with /ext" in text, text)
        code, text = b.fe("push", src / "hello.txt", "/ext/fe-check/what?.txt")
        code2, text2 = b.fe("push", src / "hello.txt", "/ext/fe-check/café.txt")
        check("a name that the card or the shell cannot take is refused",
              code == 1 and "cannot have ?" in text
              and code2 == 1 and "not ASCII" in text2, text + text2)
        code, text = b.fe("push", src / "hello.txt")
        check("a file with no place of its own needs a folder named",
              code == 1 and "where on the Flipper?" in text, text)
        code, text = b.fe("ls", "/int")
        check("/int is listed", code == 0, text)

        # the port is one line: a second command waits, then says who has it
        holder = subprocess.Popen(
            [sys.executable, str(ROOT / "flipperemu"), "-d", b.name, "cli"],
            stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL)
        time.sleep(2.0)
        code, text = b.fe("ls", "/ext/fe-check")
        holder.stdin.close()
        holder.wait(timeout=10)
        code2, text2 = b.fe("ls", "/ext/fe-check")
        check("while a `cli` session has the port, ls says who has it; "
              "afterwards it works",
              code == 1 and "in use by" in text and " cli`" in text
              and code2 == 0, text + text2)

        b.fe("usb", "unplug")
        code, text = b.fe("ls")
        b.fe("usb", "plug")
        code2, text2 = b.fe("ls", "/ext/fe-check")
        check("unplugged: no CLI, and why; plugged in again: it works",
              code == 1 and "no CLI" in text and "usb plug" in text
              and code2 == 0, text + text2)

        code, live = b.fe("ls", "-R", "/ext/fe-check")
        b.stop()
        code2, image = b.fe("ls", "-R", "/ext/fe-check")
        check("stopped: the card's image shows what the firmware wrote",
              code == 0 and code2 == 0 and live == image and live.strip(),
              live + image)
        code, text = b.fe("rm", "-r", "/ext/fe-check")
        code2, text2 = b.fe("ls", "/ext/fe-check")
        check("in the card's image: rm -r", code == 0 and code2 == 1, text + text2)
        file_checks(b, src, out, "in the card's image")
        code, text = b.fe("ls", "/int")
        check("stopped: /int is the firmware's to show",
              code == 1 and "start the device" in text, text)
        code, image = b.fe("ls", "-R", "/ext/fe-check")
        code, text = b.fe("push", big, "/ext/fe-rough/")
        check("in the card's image: a big file", code == 0, text)

        if check("boot again: 'Startup complete'", b.start()):
            code2, live = b.fe("ls", "-R", "/ext/fe-check")
            check("running: the firmware shows what was written into the image",
                  code == 0 and code2 == 0 and live == image and live.strip(),
                  image + live)
            shutil.rmtree(out, ignore_errors=True)
            code, text = b.fe("pull", "/ext/fe-check/tree", out)
            (out / "tree/hello.txt").unlink(missing_ok=True)
            (out / "tree/sub/new.txt").unlink(missing_ok=True)
            check("running: and reads the same bytes from it",
                  code == 0 and same_tree(src / "tree", out / "tree"), text)
            code, text = b.fe("rm", "-r", "/ext/fe-check")
            code2, text2 = b.fe("ls", "/ext/fe-check")
            check("through the firmware: rm -r", code == 0 and code2 == 1,
                  text + text2)
            rough_checks(b, big, work)
        check("no crash in the firmware's log",
              not CRASH.search(b.uart_text(every=True)))
    finally:
        b.stop()
        shutil.rmtree(work, ignore_errors=True)
        if not args.keep:
            shutil.rmtree(b.dir, ignore_errors=True)
    if args.keep:
        print(f"  device: {b.dir}")
    return 0 if all(ok for _, ok in results) else 1


if __name__ == "__main__":
    sys.exit(main())
