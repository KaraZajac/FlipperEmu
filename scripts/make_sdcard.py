#!/usr/bin/env python3
# Copyright (c) 2026, Kara Zajac
# SPDX-License-Identifier: BSD-3-Clause
"""Build a FAT32 microSD image for the emulated Flipper.

    make_sdcard.py sd.img --resources flipper-z-f7-resources-1.5.1.tgz
    make_sdcard.py sd.img --resources flipper-z-f7-update-mntm-012.tgz
    make_sdcard.py sd.img --resources ./my_sd_dir --size 1G
    make_sdcard.py sd.img                                  # empty card

--resources accepts:
  * a firmware "resources" tarball (tar.gz with a top-level resources/ dir)
  * an update package .tgz (uses its resources.ths / resources.tar.gz)
  * a directory copied as the card root (e.g. a backup of a real SD card)

Needs mkfs.vfat (dosfstools) and mcopy/mmd (mtools); no root, no loop mounts.
The image size must be a power of two (QEMU's SD model requires it).
"""
import argparse
import io
import os
import pathlib
import shutil
import subprocess
import sys
import tarfile
import tempfile


def heatshrink_decode(data: bytes, window_sz2: int, lookahead_sz2: int) -> bytes:
    """Decode a raw heatshrink (LZSS) bitstream."""
    out = bytearray()
    acc = 0          # bit accumulator
    nbits = 0
    pos = 0
    n = len(data)

    def bits(k):
        nonlocal acc, nbits, pos
        while nbits < k:
            if pos >= n:
                return None
            acc = (acc << 8) | data[pos]
            pos += 1
            nbits += 8
        nbits -= k
        v = (acc >> nbits) & ((1 << k) - 1)
        acc &= (1 << nbits) - 1
        return v

    while True:
        tag = bits(1)
        if tag is None:
            break
        if tag:
            b = bits(8)
            if b is None:
                break
            out.append(b)
        else:
            idx = bits(window_sz2)
            cnt = bits(lookahead_sz2)
            if idx is None or cnt is None:
                break
            off = idx + 1
            for _ in range(cnt + 1):
                # the decoder window starts zero-filled; the encoder may
                # reference it before any output exists
                out.append(out[-off] if off <= len(out) else 0)
    return bytes(out)


def read_ths(blob: bytes) -> bytes:
    if blob[:4] != b"HSDS":
        raise ValueError("not a heatshrink stream (missing HSDS header)")
    window, lookahead = blob[5], blob[6]
    return heatshrink_decode(blob[7:], window, lookahead)


def safe_extract(tar: tarfile.TarFile, dest: pathlib.Path, strip: str = ""):
    """Extract regular files/dirs only, refusing paths that escape dest."""
    dest = dest.resolve()
    for m in tar.getmembers():
        name = m.name
        if strip and name.startswith(strip):
            name = name[len(strip):]
        name = name.lstrip("/")
        if not name or name in (".", "./"):
            continue
        target = (dest / name).resolve()
        if dest not in target.parents and target != dest:
            raise ValueError(f"unsafe path in archive: {m.name}")
        if m.isdir():
            target.mkdir(parents=True, exist_ok=True)
        elif m.isfile():
            target.parent.mkdir(parents=True, exist_ok=True)
            with tar.extractfile(m) as src, open(target, "wb") as dst:
                shutil.copyfileobj(src, dst)
        # links, devices etc. are ignored on purpose


def stage_resources(src: pathlib.Path, stage: pathlib.Path):
    if src.is_dir():
        shutil.copytree(src, stage, dirs_exist_ok=True)
        return
    with tarfile.open(src, "r:*") as tar:
        names = tar.getnames()
        inner = next((n for n in names if n.endswith(("resources.ths",
                                                      "resources.tar.gz",
                                                      "resources.tar"))), None)
        if inner:                                   # an update package
            blob = tar.extractfile(inner).read()
            if inner.endswith(".ths"):
                blob = read_ths(blob)
                mode = "r:"
            else:
                mode = "r:*"
            with tarfile.open(fileobj=io.BytesIO(blob), mode=mode) as rt:
                safe_extract(rt, stage)
            return
        strip = "resources/" if any(n.startswith("resources/") for n in names) else ""
        safe_extract(tar, stage, strip)


def parse_size(s: str) -> int:
    mult = {"K": 1 << 10, "M": 1 << 20, "G": 1 << 30}
    s = s.strip().upper()
    if s[-1] in mult:
        return int(s[:-1]) * mult[s[-1]]
    return int(s)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("image")
    ap.add_argument("--resources", help="resources tgz, update package or directory")
    ap.add_argument("--overlay", action="append", metavar="DIR", default=[],
                    help="a directory copied over the resources, after them "
                         "(repeatable). For keystores re-keyed to the emulator's "
                         "enclave (scripts/rekey_keystore.py): a stock card then "
                         "decrypts its Sub-GHz codes in the emulator.")
    ap.add_argument("--size", default="512M", help="power of two, e.g. 256M, 1G")
    ap.add_argument("--force", action="store_true", help="overwrite an existing image")
    args = ap.parse_args()

    img = pathlib.Path(args.image)
    size = parse_size(args.size)
    if size & (size - 1):
        print("size must be a power of two", file=sys.stderr)
        return 2
    if img.exists() and not args.force:
        print(f"{img} exists (use --force to overwrite)", file=sys.stderr)
        return 2
    for tool in ("mkfs.vfat", "mcopy", "mmd"):
        if not shutil.which(tool):
            print(f"missing {tool} (install dosfstools/mtools)", file=sys.stderr)
            return 2

    with open(img, "wb") as f:
        f.truncate(size)                            # sparse
    subprocess.run(["mkfs.vfat", "-F", "32", "-n", "FLIPPER", str(img)],
                   check=True, stdout=subprocess.DEVNULL)

    for d in args.overlay:
        if not pathlib.Path(d).is_dir():
            print(f"overlay {d} is not a directory", file=sys.stderr)
            return 2

    if args.resources or args.overlay:
        env = dict(os.environ, MTOOLS_SKIP_CHECK="1")
        with tempfile.TemporaryDirectory(prefix="fe-sd-") as tmp:
            stage = pathlib.Path(tmp)
            if args.resources:
                stage_resources(pathlib.Path(args.resources), stage)
            for d in args.overlay:                      # applied over resources
                shutil.copytree(d, stage, dirs_exist_ok=True)
            entries = sorted(stage.iterdir())
            if entries:
                subprocess.run(["mcopy", "-s", "-Q", "-i", str(img),
                                *map(str, entries), "::/"], check=True, env=env)
            nfiles = sum(1 for _ in stage.rglob("*") if _.is_file())
        src = args.resources or "overlay only"
        extra = f" + {len(args.overlay)} overlay(s)" if args.overlay and args.resources else ""
        print(f"{img}: {size >> 20} MiB FAT32, {nfiles} files from {src}{extra}")
    else:
        print(f"{img}: {size >> 20} MiB FAT32, empty")
    return 0


if __name__ == "__main__":
    sys.exit(main())
