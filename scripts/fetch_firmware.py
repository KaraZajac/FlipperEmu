#!/usr/bin/env python3
# Copyright (c) 2026, Kara Zajac
# SPDX-License-Identifier: BSD-3-Clause
"""Download an official Flipper Zero firmware release (f7) and verify SHA-256.

Uses the same directory qFlipper uses: https://update.flipperzero.one/firmware/directory.json

    scripts/fetch_firmware.py                     # latest "release" channel
    scripts/fetch_firmware.py --channel development
    scripts/fetch_firmware.py --version 1.5.1     # a specific version, if still listed

Files land in firmware/official-<version>/ (git-ignored): the update package
that `./flipperemu run -f` takes, and for `./flipperemu gdb` the firmware's ELF
and, unpacked into appsymbols/ beside it, the debug ELFs of the stock apps.
Standard library only.
"""
import argparse
import hashlib
import io
import json
import pathlib
import sys
import tarfile
import urllib.request

DIRECTORY = "https://update.flipperzero.one/firmware/directory.json"
WANTED = ("update_tgz", "firmware_elf", "full_dfu", "full_bin", "resources_tgz", "updater_elf",
          "appsymbols_tgz")
UNPACKED = {"appsymbols_tgz": "appsymbols"}     # what `./flipperemu gdb` looks for


def fetch(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "FlipperEmu/0.1"})
    with urllib.request.urlopen(req, timeout=120) as r:
        return r.read()


def unpack(archive: bytes, folder: pathlib.Path) -> int:
    """The archive's .elf files into the folder, by their names alone: nothing
    in it says where else a file goes.  -> how many"""
    folder.mkdir(exist_ok=True)
    count = 0
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        for member in tar:
            name = pathlib.PurePosixPath(member.name).name
            if member.isfile() and name.endswith(".elf"):
                (folder / name).write_bytes(tar.extractfile(member).read())
                count += 1
    return count


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--channel", default="release", help="release | release-candidate | development")
    ap.add_argument("--version", help="pick this version instead of the newest in the channel")
    ap.add_argument("--dest", default=str(pathlib.Path(__file__).resolve().parent.parent / "firmware"))
    ap.add_argument("--types", default=",".join(WANTED), help="comma-separated file types to fetch")
    args = ap.parse_args()

    directory = json.loads(fetch(DIRECTORY))
    channel = next((c for c in directory["channels"] if c["id"] == args.channel), None)
    if channel is None:
        print(f"no channel {args.channel!r}; have {[c['id'] for c in directory['channels']]}", file=sys.stderr)
        return 1
    versions = channel["versions"]
    version = versions[0] if not args.version else next((v for v in versions if v["version"] == args.version), None)
    if version is None:
        print(f"version {args.version} not listed in channel {args.channel}", file=sys.stderr)
        return 1

    types = set(args.types.split(","))
    outdir = pathlib.Path(args.dest) / f"official-{version['version']}"
    outdir.mkdir(parents=True, exist_ok=True)
    for f in version["files"]:
        if f["target"] != "f7" or f["type"] not in types:
            continue
        name = f["url"].rsplit("/", 1)[1]
        path = outdir / name
        folder = UNPACKED.get(f["type"])
        if path.exists() and hashlib.sha256(path.read_bytes()).hexdigest() == f["sha256"]:
            print(f"ok (cached)  {name}")
            if folder and not (outdir / folder).is_dir():
                print(f"ok           {folder}/  ({unpack(path.read_bytes(), outdir / folder)} files)")
            continue
        data = fetch(f["url"])
        digest = hashlib.sha256(data).hexdigest()
        if digest != f["sha256"]:
            print(f"SHA-256 MISMATCH {name}: got {digest}, want {f['sha256']}", file=sys.stderr)
            return 2
        path.write_bytes(data)
        print(f"ok           {name}  ({len(data)} bytes, sha256 verified)")
        if folder:
            print(f"ok           {folder}/  ({unpack(data, outdir / folder)} files)")
    print(outdir)
    return 0


if __name__ == "__main__":
    sys.exit(main())
