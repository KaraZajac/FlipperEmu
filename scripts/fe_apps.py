#!/usr/bin/env python3
# Copyright (c) 2026, Kara Zajac
# SPDX-License-Identifier: BSD-3-Clause
"""Apps on a Flipper: what a .fap says about itself, and the firmware's loader.

A .fap is an ELF file with a section `.fapmeta`: the app's name, the firmware
API it was built against and the hardware it is for.  The loader refuses one
whose API major is not the firmware's own, so that is worth a look before the
file is copied anywhere.

The loader is driven through the firmware's CLI (an fe_cli.FlipperCli), as
`fbt launch` does it on a real Flipper: `loader close`, `loader open PATH`,
`loader info`.

    fe_apps.py FILE.fap ...             what each file says about itself
"""
import collections
import re
import struct
import sys
import time

FAP_MAGIC = 0x52474448
FAP_TARGET = 7                          # the Flipper Zero ("f7")

Fap = collections.namedtuple("Fap", "name api_major api_minor target stack "
                                    "version has_icon")


class AppError(Exception):
    """Said to the user as it is"""


def section(data, name):
    """The bytes of an ELF file's section, or None when it has none of that
    name.  AppError: not an ELF file of the kind a Flipper loads."""
    if len(data) < 52 or data[:4] != b"\x7fELF":
        raise AppError("not an ELF file")
    if data[4] != 1 or data[5] != 1:
        raise AppError("not a 32-bit little-endian ELF file")
    shoff, = struct.unpack_from("<I", data, 32)
    entsize, count, names = struct.unpack_from("<HHH", data, 46)
    if entsize < 40 or not count or names >= count \
            or shoff + entsize * count > len(data):
        raise AppError("an ELF file with no (or a damaged) section table")

    def header(i):
        _name, _type, _flags, _addr, offset, size = struct.unpack_from(
            "<6I", data, shoff + i * entsize)
        return _name, _type, offset, size

    _, _, str_at, str_size = header(names)
    if str_at + str_size > len(data):
        raise AppError("an ELF file with a damaged section table")
    strings = data[str_at:str_at + str_size]
    for i in range(count):
        at, kind, offset, size = header(i)
        end = strings.find(b"\0", at)
        if at >= len(strings) or end < 0 or strings[at:end] != name.encode():
            continue
        if kind == 8 or offset + size > len(data):      # NOBITS, or cut off
            raise AppError(f"an ELF file with a damaged {name} section")
        return data[offset:offset + size]
    return None


def manifest(data):
    """What a .fap's bytes say about the app (a Fap), or AppError"""
    meta = section(data, ".fapmeta")
    if meta is None:
        raise AppError("not a Flipper app: an ELF file without a .fapmeta "
                       "section (a firmware's .elf? `flash` takes that)")
    if len(meta) < 53:
        raise AppError("a Flipper app with a damaged .fapmeta section")
    magic, version, minor, major, target, stack, app_version = \
        struct.unpack_from("<IIHHHHI", meta, 0)
    if magic != FAP_MAGIC:
        raise AppError("a Flipper app with a damaged .fapmeta section")
    if version != 1:
        raise AppError(f"a Flipper app with manifest version {version}: only "
                       "version 1 is known here")
    name = meta[20:52].split(b"\0")[0].decode(errors="replace")
    return Fap(name, major, minor, target, stack, app_version, bool(meta[52]))


def about(fap, firmware=None):
    """(one line about the app, a warning or None).
    firmware: (API major, API minor, target), as firmware_api() gives it"""
    line = f'"{fap.name}", built for API {fap.api_major}.{fap.api_minor}'
    warn = None
    if fap.target != FAP_TARGET:
        warn = (f"it is built for hardware target {fap.target}, and a Flipper "
                f"Zero is target {FAP_TARGET}")
    if firmware:
        major, minor, target = firmware
        if (major, minor) == (fap.api_major, fap.api_minor):
            line += ", which is this firmware's"
        else:
            line += f" (this firmware has {major}.{minor})"
        if major != fap.api_major:
            warn = (f"this firmware's API is {major}: its loader refuses the "
                    "app, or asks on the Flipper's screen whether to go on. "
                    "Build it against this firmware")
        elif fap.api_minor > minor and not warn:
            warn = ("it is built against a newer API than this firmware has: "
                    "it starts only if it uses nothing that came after "
                    f"{major}.{minor}")
    return line, warn


# ----------------------------------------------------------- the loader's CLI

def firmware_api(cli):
    """(API major, API minor, hardware target) of the firmware, or None"""
    out = cli.run("info device", timeout=20)
    found = dict(re.findall(r"^(firmware\.api\.major|firmware\.api\.minor|"
                            r"hardware\.target)\s*:\s*(\d+)\s*$", out, re.M))
    try:
        return (int(found["firmware.api.major"]),
                int(found["firmware.api.minor"]),
                int(found["hardware.target"]))
    except KeyError:
        return None


def running(cli):
    """The name of the app that runs, or None"""
    out = cli.run("loader info", timeout=15)
    m = re.search(r'Application "(.*)" is running', out)
    if m:
        return m.group(1)
    if "No application is running" in out:
        return None
    raise AppError(f"the firmware's loader said: {out.strip() or 'nothing'}")


BACKS = 6                               # short presses of Back, then a long one


def close(cli, wait=8.0, back=None):
    """Close whatever runs.  [(name, pressed)] of what was closed, in order;
    pressed: it took its Back button to do it.

    The loader asks an app to close, and an app with a loop of its own (many
    small ones) does not hear that.  back: a function that presses the
    Flipper's Back button (long=False): what a person would do then.
    AppError: an app that is still there after all of it."""
    closed = []
    while True:
        out = cli.run("loader close", timeout=15)
        m = re.search(r'Application "(.*)" (was closed|has to be closed '
                      r'manually)', out)
        if "No application is running" in out:
            return closed
        if not m:
            raise AppError(f"the firmware's loader said: "
                           f"{out.strip() or 'nothing'}")
        name, asked = m.group(1), m.group(2) == "was closed"
        if asked:
            # said when the app has the request; it is gone a little later
            end = time.monotonic() + wait
            while (now := running(cli)) == name:
                if time.monotonic() > end:
                    raise AppError(f'"{name}" was asked to close, and still '
                                   "runs (`reset` starts the firmware again)")
                time.sleep(0.2)
        else:
            if not back:
                raise AppError(f'"{name}" takes no request to close: leave it '
                               "with its Back button")
            for press in range(BACKS + 1):
                back(long=press == BACKS)
                time.sleep(0.4)
                if (now := running(cli)) != name:
                    break
            else:
                raise AppError(
                    f'"{name}" takes no request to close, and is still there '
                    f"after Back was pressed {BACKS} times and held once: "
                    "leave it on the Flipper itself, or `reset`")
        closed.append((name, not asked))
        if now is None:
            return closed


def quoted(text):
    """As the firmware's argument reader takes a string with spaces in it"""
    if '"' in text:
        raise AppError(f'{text}: the firmware\'s CLI has no way to take a " '
                       "in a name")
    return f'"{text}"'


QUESTION = 8.0                          # no answer for that long: it may ask


def start(cli, what, args=None, wait=60.0, waiting=None, back=None):
    """Start an app: one of the firmware's own by name, or a .fap by its path
    on the Flipper.  (what the loader said against it, cancelled): '' = it
    was started.

    The loader answers when the app is loaded.  Some firmware first asks on
    the Flipper's screen (an app built for another API: "Continue anyways?")
    and answers when a button was pressed.  waiting(): called once when no
    answer has come for QUESTION seconds.  back(): presses the Back button;
    with it, a question that nobody answered in `wait` seconds is cancelled,
    so that the CLI is free again."""
    line = "loader open " + quoted(what)
    if args:
        # all that follows the name is the app's, spaces and all: every
        # firmware takes that, and only newer ones take quotes around it
        line += " " + args
    late = (f"the Flipper's CLI was not done with `{line}` after "
            f"{wait:g} s")
    cancelled = False
    cli.write(line.encode() + b"\r")
    try:
        out = cli.read_until_prompt(min(QUESTION, wait))
    except TimeoutError:
        if waiting:
            waiting()
        try:
            out = cli.read_until_prompt(max(0.0, wait - QUESTION),
                                        None if back else late)
        except TimeoutError:
            if not back:
                raise
            back()
            cancelled = True
            out = cli.read_until_prompt(15, late + ", and not after its Back "
                                        "button was pressed either")
    return cli.answer(out).strip(), cancelled


def builtin(cli):
    """{'Apps': [names], 'Settings': [names]}: what `loader open NAME` takes"""
    out, groups, into = cli.run("loader list", timeout=15), {}, None
    for line in out.splitlines():
        if line[:1] in ("\t", " ") and line.strip() and into is not None:
            into.append(line.strip())
        elif line.strip().endswith(":"):
            into = groups.setdefault(line.strip()[:-1], [])
    return groups


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    status = 0
    for name in sys.argv[1:]:
        try:
            with open(name, "rb") as f:
                fap = manifest(f.read())
            print(f"{name}: {about(fap)[0]}, hardware target {fap.target}, "
                  f"stack {fap.stack} bytes, version {fap.version >> 16}."
                  f"{fap.version & 0xffff}")
        except (OSError, AppError) as e:
            print(f"{name}: {e}", file=sys.stderr)
            status = 1
    return status


if __name__ == "__main__":
    sys.exit(main())
