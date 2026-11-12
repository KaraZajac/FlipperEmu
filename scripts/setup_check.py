#!/usr/bin/env python3
# Copyright (c) 2026, Kara Zajac
# SPDX-License-Identifier: BSD-3-Clause
"""Check how scripts/setup_qemu.sh applies the patches in qemu/patches.

    scripts/setup_check.py

Runs the script itself on stand-in source trees: folders that hold only the
files the patches touch (taken from the QEMU tarball in third_party/), with a
`ninja` that builds nothing.  No emulator is started and nothing is built; the
tree in third_party/ is read and never written.

A new tree gets every patch, in order.  The script once more, and a third
time, applies nothing.  A tree from before each patch gets that one and those
after it; a tree that lacks one patch alone gets that one.  A tree the script
damaged as it was until 2026-11-11 (a second run applied 0007 again, in part,
because 0008 builds on it in the same file) is refused with the way out, and
so is a file that is not this QEMU's: both without a change to the tree.
Last, the tree in third_party/ here: it has every patch and nothing else in
those files.  Exit status 0 = pass.
"""
import hashlib
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from files_check import ROOT, check, results                # noqa: E402

SCRIPT = ROOT / "scripts" / "setup_qemu.sh"
PATCHES = sorted((ROOT / "qemu" / "patches").glob("*.patch"))
VERSION = re.search(r'QEMU_VERSION="\$\{QEMU_VERSION:-([^}]+)\}"',
                    SCRIPT.read_text()).group(1)
QEMU = f"qemu-{VERSION}"
TARBALL = ROOT / "third_party" / f"{QEMU}.tar.xz"
HOOKED = ("hw/arm/meson.build", "hw/arm/Kconfig")           # the overlay's two


def number(p):
    return p.name[:4]


def touched(p):
    """The files a patch changes"""
    return re.findall(r"^\+\+\+ b/(\S+)", p.read_text(), re.M)


FILES = sorted({f for p in PATCHES for f in touched(p)})


def patch(src, p, *more):
    """Apply a patch the plain way; True when all of it went in"""
    with open(p) as f:
        return subprocess.run(["patch", "-d", str(src), "-p1", "-s", "-f", *more],
                              stdin=f, capture_output=True).returncode == 0


class Stand:
    """A folder that is to the script what a checkout is"""

    def __init__(self, work, name, have):
        self.root = work / name
        self.src = self.root / "third_party" / QEMU
        (self.root / "scripts").mkdir(parents=True)
        (self.root / "build" / "qemu").mkdir(parents=True)
        (self.root / "scripts" / "setup_qemu.sh").symlink_to(SCRIPT)
        (self.root / "qemu").symlink_to(ROOT / "qemu")
        shutil.copytree(work / "pristine" / QEMU, self.src)
        (self.root / "build" / "qemu" / "build.ninja").touch()  # no configure
        self.env = dict(os.environ, PATH=f"{work / 'bin'}:{os.environ['PATH']}",
                        TMPDIR=str(work / "tmp"))
        self.made = all([patch(self.src, p) for p in have])

    def run(self):
        """The script on it: (exit status, the patches it said it applied,
        whether it went on to build, all it said)"""
        r = subprocess.run(["bash", str(self.root / "scripts" / "setup_qemu.sh")],
                           env=self.env, capture_output=True, text=True,
                           stdin=subprocess.DEVNULL)
        out = r.stdout + r.stderr
        return (r.returncode, re.findall(r"^>> applying (\d{4})", out, re.M),
                bool(re.search(r"^ninja -C \S+ qemu-system-arm", out, re.M)), out)

    def same(self, other):
        """The patched files are the other tree's, byte for byte"""
        return all((self.src / f).read_bytes() == (other / f).read_bytes()
                   for f in FILES)

    def litter(self):
        return sorted(str(p.relative_to(self.src)) for p in self.src.rglob("*")
                      if p.suffix in (".rej", ".orig"))

    def digest(self):
        h = hashlib.sha256()
        for p in sorted(self.src.rglob("*")):
            if p.is_symlink() or not p.is_file():
                h.update(f"{p.relative_to(self.src)} {p.is_symlink()}\n".encode())
            else:
                h.update(f"{p.relative_to(self.src)}\n".encode() + p.read_bytes())
        return h.hexdigest()


def main():
    if len(sys.argv) > 1:
        print(__doc__.strip())
        return 0 if sys.argv[1] in ("-h", "--help") else 2
    if not TARBALL.exists():
        print(f"{TARBALL} is not there: this check takes the files as QEMU "
              "released them from it. scripts/setup_qemu.sh fetches it.",
              file=sys.stderr)
        return 2
    work = pathlib.Path(tempfile.mkdtemp(prefix="fe-setup-check-"))
    try:
        (work / "pristine").mkdir()
        (work / "tmp").mkdir()
        (work / "bin").mkdir()
        ninja = work / "bin" / "ninja"
        ninja.write_text('#!/bin/sh\necho "ninja $*"\n')
        ninja.chmod(0o755)
        subprocess.run(["tar", "-C", str(work / "pristine"), "-xf", str(TARBALL),
                        *(f"{QEMU}/{f}" for f in (*FILES, *HOOKED))], check=True)
        every = [number(p) for p in PATCHES]
        whole = Stand(work, "whole", PATCHES).src       # the plain way, in order

        s = Stand(work, "new", [])
        code, applied, built, out = s.run()
        check(f"a new tree: all {len(PATCHES)} applied, in their order",
              applied == every, out)
        check("a new tree: exit 0, the machine hooked into the build, and the "
              "build started",
              code == 0 and built and (s.src / "hw/arm/flipper").is_symlink()
              and "subdir('flipper')" in (s.src / HOOKED[0]).read_text()
              and "source flipper/Kconfig" in (s.src / HOOKED[1]).read_text(),
              out)
        check("a new tree: its files are what the patches make of QEMU's, one "
              "after another", s.same(whole) and not s.litter(), s.litter())
        before = s.digest()
        code, applied, built, out = s.run()
        check("the script once more on that tree: nothing applied, exit 0, "
              "nothing changed, the build started",
              code == 0 and not applied and built and s.digest() == before,
              out + "\n".join(s.litter()))
        code, applied, built, out = s.run()
        check("and a third time: the same",
              code == 0 and not applied and built and s.digest() == before,
              out + "\n".join(s.litter()))

        for k in range(1, len(PATCHES)):
            s = Stand(work, f"before-{every[k]}", PATCHES[:k])
            code, applied, built, out = s.run()
            check(f"a tree from before {every[k]}: {' '.join(every[k:])} "
                  "applied, and the tree is whole then",
                  s.made and code == 0 and applied == every[k:] and built
                  and s.same(whole) and not s.litter(), out)
        for k in range(len(PATCHES) - 1):
            s = Stand(work, f"without-{every[k]}", PATCHES[:k] + PATCHES[k + 1:])
            if not s.made:              # a later one builds on it: no such tree
                continue
            code, applied, built, out = s.run()
            check(f"a tree without {every[k]} alone: that one applied, and the "
                  "tree is whole then",
                  code == 0 and applied == [every[k]] and built
                  and s.same(whole) and not s.litter(), out)

        # the script as it was: each patch asked by itself whether it comes out
        s = Stand(work, "damaged", PATCHES)
        again = [number(p) for p in PATCHES
                 if not patch(s.src, p, "-R", "--dry-run") and not patch(s.src, p)]
        rejects = [f for f in s.litter() if f.endswith(".rej")]
        check("the script as it was until 2026-11-11, on a whole tree: a patch "
              "applied a second time, in part (the case for the next checks)",
              again and rejects, f"applied again: {again}; left: {s.litter()}")
        before = s.digest()
        code, applied, built, out = s.run()
        check("a tree it damaged that way: exit 1, nothing applied, no build",
              code == 1 and not applied and not built, out)
        check("a tree it damaged that way: the reject named, and the way out",
              rejects and f"{rejects[0]}: a patch was applied to that file in "
              "part" in out and "Remove" in out and str(s.src) in out, out)
        check("a tree it damaged that way: not changed", s.digest() == before)

        # the last patch's file cut short: the ones before it fit, it does
        # not (nor does an earlier one to that file, which is named then)
        s = Stand(work, "other", [])
        cut = touched(PATCHES[-1])[0]
        first = next(p for p in PATCHES if cut in touched(p))
        (s.src / cut).write_text(
            "".join((s.src / cut).read_text().splitlines(True)[:5]))
        before = s.digest()
        code, applied, built, out = s.run()
        check(f"a tree where {number(first)} does not fit (its file is not "
              "this QEMU's): exit 1, that patch named, no build",
              code == 1 and not built and f"{first.name} is not in the "
              "tree and does not fit it" in out, out)
        check("that tree: not changed, also not by the patches that would "
              "have fitted", not applied and s.digest() == before, out)

        left = sorted(p.name for p in (work / "tmp").iterdir())
        check("no copy of the script's left in the temporary folder, after a "
              "refusal either", not left, left)

        here = ROOT / "third_party" / QEMU
        if here.is_dir():
            differ = [f for f in FILES if not (here / f).exists() or
                      (here / f).read_bytes() != (whole / f).read_bytes()]
            litter = [f for f in FILES if (here / (f + ".rej")).exists()]
            check(f"the tree here (third_party/{QEMU}): every patch, and "
                  "nothing else in those files",
                  not differ and not litter,
                  "\n".join([f"differs: {f}" for f in differ] +
                            [f"a reject: {f}.rej" for f in litter] +
                            ["scripts/setup_qemu.sh says what it lacks, or "
                             "how to start over"]))
    finally:
        shutil.rmtree(work, ignore_errors=True)
    bad = [label for label, ok in results if not ok]
    print(f"{len(results) - len(bad)} of {len(results)} checks passed")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
