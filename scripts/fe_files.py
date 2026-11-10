#!/usr/bin/env python3
# Copyright (c) 2026, Kara Zajac
# SPDX-License-Identifier: BSD-3-Clause
"""Files on an emulated Flipper's microSD card.

Two ways to the same card, behind one interface:

  CliFiles    a running device: the firmware's own `storage` commands over its
              USB serial port, the ones fbt's scripts use on a real Flipper.
              What the firmware sees is what there is, /int included.
  ImageFiles  a stopped device: mtools on the card's image. Nothing emulated
              is in the way, so it is quick; /ext only.

push(), pull() and remove() copy and delete with either. The launcher's `ls`,
`push`, `pull`, `rm` and `mkdir` are this module.

Paths on the Flipper are absolute and begin with /ext (the card), /int (the
firmware's own small storage) or /any.
"""
import collections
import hashlib
import os
import pathlib
import re
import shutil
import subprocess
import tempfile

ROOTS = ("/ext", "/int", "/any")
# The firmware takes a write_chunk in pieces of 1024 bytes, and writes each
# when it has all of it.  A chunk of that size is written or it is not: after
# a failed write no bytes of the file are left on the line for the shell to
# read as commands.
WRITE_CHUNK = 1024
READ_CHUNK = 8192
READY = b"\r\nReady?\r\n"

# where a file of a kind lives, when no folder is named
HOMES = {
    ".sub": "/ext/subghz",
    ".ir": "/ext/infrared",
    ".nfc": "/ext/nfc",
    ".rfid": "/ext/lfrfid",
    ".ibtn": "/ext/ibutton",
    ".js": "/ext/apps/Scripts",
    ".fmf": "/ext/music_player",
}

Entry = collections.namedtuple("Entry", "name is_dir size")


class FilesError(Exception):
    """Something about files that the user is to read"""


class StorageError(FilesError):
    """The firmware's storage service said no"""
    WORDS = {
        "file/dir not exist": "no such file or folder on the Flipper",
        "file/dir already exist": "it exists already",
        "invalid name/path": "the firmware does not take that path",
        "access denied": "the firmware refuses (a folder that is not empty, "
                         "or a file where a folder is)",
        "filesystem not ready": "no SD card in the slot (`sd insert` pushes "
                                "it back in)",
        "file is already open": "an app on the Flipper has that file open",
    }

    def __init__(self, reason, path=None):
        self.reason = reason
        words = self.WORDS.get(reason, reason)
        super().__init__(f"{path}: {words}" if path else words)


def clean(path):
    """A path on the Flipper as the firmware takes it, or FilesError"""
    path = str(path)
    if not path.startswith("/"):
        raise FilesError(f"{path}: paths on the Flipper begin with /ext (its "
                         "SD card), /int or /any")
    parts = []
    for part in path.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            if parts:
                parts.pop()
            continue
        parts.append(part)
    if not parts or "/" + parts[0] not in ROOTS:
        raise FilesError(f"{path}: paths on the Flipper begin with /ext (its "
                         "SD card), /int or /any")
    for part in parts[1:]:
        check_name(part)
    return "/" + "/".join(parts)


def check_name(name):
    """A name the card (FAT) and the firmware's shell both take"""
    bad = sorted(set(c for c in name if c in '\\/:*?"<>|' or ord(c) < 32))
    if bad:
        raise FilesError(f"{name!r}: a name on the card cannot have "
                         f"{' '.join(repr(c)[1:-1] for c in bad)} in it")
    if not name.isascii():
        raise FilesError(f"{name!r}: the firmware's shell drops what is not "
                         "ASCII, so the name would arrive as another one")
    if name != name.strip(" ") or name.endswith("."):
        raise FilesError(f"{name!r}: a name on the card cannot begin or end "
                         "with a space, or end with a dot")


def join(folder, *names):
    return "/".join([folder.rstrip("/"), *(str(n).strip("/") for n in names)])


def parent(path):
    return path.rsplit("/", 1)[0]


def md5(data):
    return hashlib.md5(data).hexdigest()


def by_name(entries):
    """Folders first, then as a file manager sorts them"""
    return sorted(entries, key=lambda e: (not e.is_dir, e.name.lower(), e.name))


# ------------------------------------------------- a running device: its CLI

class CliFiles:
    """The card through the firmware (cli: an fe_cli.FlipperCli at its prompt)"""
    where = "through the firmware"

    def __init__(self, cli):
        self.cli = cli

    def _run(self, command, path, timeout=30.0):
        out = self.cli.run(f'storage {command} "{path}"', timeout=timeout)
        self._check(out, path)
        return out

    @staticmethod
    def _check(text, path):
        m = re.search(r"Storage error: *(.*)", text)
        if m:
            raise StorageError(m.group(1).strip(), path)

    @staticmethod
    def _entries(text, strip=0):
        out = []
        for line in text.splitlines():
            line = line.strip()
            m = re.fullmatch(r"\[F\] (.+) (\d+)b", line)
            if m:
                out.append(Entry(m.group(1)[strip:], False, int(m.group(2))))
                continue
            m = re.fullmatch(r"\[D\] (.+)", line)
            if m:
                out.append(Entry(m.group(1)[strip:], True, 0))
        return out

    def stat(self, path):
        """Entry, or None: there is none"""
        try:
            out = self._run("stat", path).strip()
        except StorageError as e:
            if e.reason == "file/dir not exist":
                return None
            raise
        name = path.rsplit("/", 1)[1]
        m = re.search(r"File, size: *(\d+)b", out)
        if m:
            return Entry(name, False, int(m.group(1)))
        if out.startswith(("Directory", "Storage")):
            return Entry(name, True, 0)
        raise FilesError(f"{path}: the firmware said {out!r}")

    def ls(self, path):
        return self._entries(self._run("list", path, 120))

    def tree(self, path):
        """All that is in a folder; names are paths from it"""
        return self._entries(self._run("tree", path, 600), len(path) + 1)

    def mkdir(self, path):
        self._run("mkdir", path)

    def makedirs(self, path):
        st = self.stat(path)
        if st:
            if not st.is_dir:
                raise FilesError(f"{path} is a file on the Flipper, not a folder")
            return
        if parent(path) not in ROOTS:
            self.makedirs(parent(path))
        self.mkdir(path)

    def remove(self, path):
        """A file, or a folder that is empty"""
        self._run("remove", path)

    def rmtree(self, path):
        entries = self.tree(path)
        deep = sorted(entries, key=lambda e: (e.is_dir, -e.name.count("/")))
        for e in deep:                      # files, then folders from the bottom
            try:
                self.remove(join(path, e.name))
            except StorageError as err:
                if err.reason != "file/dir not exist":
                    raise
        self.remove(path)

    def md5(self, path):
        out = self._run("md5", path, 300).strip()
        if not re.fullmatch(r"[0-9a-f]{32}", out):
            raise FilesError(f"{path}: the firmware said {out!r}")
        return out

    def write(self, path, data, progress=None):
        cli = self.cli
        try:
            self.remove(path)               # write_chunk appends
        except StorageError as e:
            if e.reason != "file/dir not exist":
                raise
        at = 0
        while True:
            piece = data[at:at + WRITE_CHUNK]
            cli.write(f'storage write_chunk "{path}" {len(piece)}\r'.encode())
            cli.read_line(15)               # the command, echoed
            word = cli.read_line(15)
            if word != "Ready":
                cli.read_until_prompt(15)
                self._check(word, path)
                raise FilesError(f"{path}: the firmware said {word!r}")
            cli.write(piece)
            tail = cli.read_until_prompt(60)
            self._check(tail.decode(errors="replace"), path)
            at += len(piece)
            if progress:
                progress(at, len(data))
            if at >= len(data):
                return

    def read(self, path, progress=None):
        cli = self.cli
        cli.write(f'storage read_chunks "{path}" {READ_CHUNK}\r'.encode())
        cli.read_line(15)                   # the command, echoed
        head = cli.read_line(15)
        m = re.fullmatch(r"Size: (\d+)", head)
        if not m:
            cli.read_until_prompt(15)
            self._check(head, path)
            raise FilesError(f"{path}: the firmware said {head!r}")
        size, out = int(m.group(1)), bytearray()
        while len(out) < size:
            ready = cli.read_exactly(len(READY), 30)
            if ready != READY:
                raise FilesError(f"{path}: the firmware said {ready!r} in the "
                                 "middle of the file")
            cli.write(b"y")
            out += cli.read_exactly(min(size - len(out), READ_CHUNK), 60)
            if progress:
                progress(len(out), size)
        cli.read_until_prompt(15)
        return bytes(out)


# -------------------------------------------- a stopped device: its card image

class ImageFiles:
    """The card's image, with mtools.  Only while no emulator has it."""
    where = "in the card's image"
    LINE = re.compile(r"^(.{8}) (.{3}) +(<DIR>|\d+) +\d{4}-\d\d-\d\d +"
                      r"\d{1,2}:\d\d ?(?: (.*))?$")

    def __init__(self, image):
        self.image = str(image)
        if not shutil.which("mdir"):
            raise FilesError("the card's image is read and written with "
                             "mtools, which is not installed")

    def _at(self, path, new=False):
        """mtools' name for a path.  Brackets are patterns to it, except in a
        name that it is to make."""
        if path.startswith("/int"):
            raise FilesError("/int is where the firmware puts it: start the "
                             "device to get at it")
        parts = path.split("/")[2:]
        names = [re.sub(r"[\[\]]", r"\\\g<0>", p) for p in parts]
        if new and parts:
            names[-1] = parts[-1]
        return "::/" + "/".join(names)

    def _m(self, tool, *args, check=True):
        r = subprocess.run([tool, "-i", self.image, *args],
                           stdin=subprocess.DEVNULL, capture_output=True,
                           env={**os.environ, "MTOOLS_SKIP_CHECK": "1"})
        if check and r.returncode:
            raise FilesError(r.stderr.decode(errors="replace").strip()
                             or f"{tool} failed on {self.image}")
        return r

    def _listing(self, path, recursive=False):
        """{folder, as a path from `path`: [Entry]} or None: mdir found none"""
        r = self._m("mdir", "-a", *(["-/"] if recursive else []),
                    self._at(path), check=False)
        if r.returncode:
            return None
        depth, folder, out = len(path.split("/")) - 2, "", {}
        for line in r.stdout.decode(errors="replace").splitlines():
            if line.startswith("Directory for ::"):
                parts = [p for p in line[len("Directory for ::"):].split("/") if p]
                folder = "/".join(parts[depth:])
                out.setdefault(folder, [])
                continue
            m = self.LINE.match(line)
            if not m:
                continue
            base, ext, size, long_name = m.groups()
            name = long_name or (base.strip() + ("." + ext.strip()
                                                 if ext.strip() else ""))
            if name not in (".", ".."):
                out.setdefault(folder, []).append(
                    Entry(name, size == "<DIR>", 0 if size == "<DIR>" else int(size)))
        return out

    def stat(self, path):
        if path in ROOTS:
            return Entry(path[1:], True, 0)
        name = path.rsplit("/", 1)[1]
        up = self.stat(parent(path))
        if not up or not up.is_dir:
            return None
        listing = self._listing(parent(path)) or {}
        for e in next(iter(listing.values()), []):
            if e.name.lower() == name.lower():      # FAT: one name, any case
                return e
        return None

    def _folder(self, path):
        st = self.stat(path)
        if not st:
            raise StorageError("file/dir not exist", path)
        if not st.is_dir:
            raise FilesError(f"{path} is a file on the Flipper, not a folder")

    def ls(self, path):
        self._folder(path)
        return next(iter((self._listing(path) or {}).values()), [])

    def tree(self, path):
        self._folder(path)
        out = []
        for folder, entries in (self._listing(path, True) or {}).items():
            for e in entries:
                out.append(e._replace(name=join(folder, e.name).lstrip("/")))
        return out

    def mkdir(self, path):
        if self.stat(path):
            raise StorageError("file/dir already exist", path)
        self._m("mmd", self._at(path, new=True))

    def makedirs(self, path):
        st = self.stat(path)
        if st:
            if not st.is_dir:
                raise FilesError(f"{path} is a file on the Flipper, not a folder")
            return
        if parent(path) not in ROOTS:
            self.makedirs(parent(path))
        self._m("mmd", self._at(path, new=True))

    def remove(self, path):
        st = self.stat(path)
        if not st:
            raise StorageError("file/dir not exist", path)
        if st.is_dir and self.ls(path):
            raise StorageError("access denied", path)
        self._m("mrd" if st.is_dir else "mdel", self._at(path))

    def rmtree(self, path):
        self._folder(path)
        self._m("mdeltree", self._at(path))

    def read(self, path, progress=None):
        st = self.stat(path)
        if not st:
            raise StorageError("file/dir not exist", path)
        if st.is_dir:
            raise FilesError(f"{path} is a folder on the Flipper")
        data = self._m("mtype", self._at(path)).stdout
        if progress:
            progress(len(data), len(data))
        return data

    def md5(self, path):
        return md5(self.read(path))

    def write(self, path, data, progress=None):
        with tempfile.NamedTemporaryFile(prefix="fe-file-") as tmp:
            tmp.write(data)
            tmp.flush()
            self._m("mcopy", "-o", tmp.name, self._at(path, new=True))
        if progress:
            progress(len(data), len(data))


# ------------------------------------------------------------ copy and delete

def home_for(files, local):
    """The folder where a file of this kind lives on the card"""
    local = pathlib.Path(local)
    kind = local.suffix.lower()
    if kind == ".fap":
        try:
            for e in files.tree("/ext/apps"):
                if not e.is_dir and e.name.rsplit("/", 1)[-1] == local.name:
                    return parent(join("/ext/apps", e.name))
        except StorageError:
            pass
        return "/ext/apps"
    if local.is_file() and kind in HOMES:
        return HOMES[kind]
    raise FilesError(f"{local}: where on the Flipper? Name a folder after it, "
                     "/ext/... (only "
                     + ", ".join(sorted([*HOMES, ".fap"])) + " have a place of "
                     "their own)")


def plan_push(files, sources, remote):
    """([folders to make], [(local file, path on the card)])"""
    sources = [pathlib.Path(s) for s in sources]
    for s in sources:
        if not s.exists():
            raise FilesError(f"{s}: no such file or folder")
    if remote is None:
        return [], [(s, clean(join(home_for(files, s), s.name))) for s in sources]
    into = remote.endswith("/") or len(sources) > 1 or sources[0].is_dir()
    remote = clean(remote)
    if not into:
        st = files.stat(remote)
        if not (st and st.is_dir):
            return [], [(sources[0], remote)]       # the file's new name
    folders, jobs = [], []
    for s in sources:
        if not s.is_dir():
            jobs.append((s, clean(join(remote, s.name))))
            continue
        base = clean(join(remote, s.resolve().name))
        folders.append(base)
        for here, dirs, names in os.walk(s):
            dirs.sort()
            rel = pathlib.Path(here).relative_to(s).as_posix()
            at = base if rel == "." else join(base, rel)
            folders += [clean(join(at, d)) for d in dirs]
            jobs += [(pathlib.Path(here) / n, clean(join(at, n)))
                     for n in sorted(names)]
    return folders, jobs


def push(files, sources, remote=None, force=False, say=print, progress=None):
    """Copy local files and folders to the card; (copied, unchanged).

    remote: the folder to put them in; for one file, also its new name.
    None: each file goes where its kind lives (HOMES).  A folder goes into
    `remote` with all that is in it.  A file that is there already with the
    same bytes is left alone, unless force."""
    folders, jobs = plan_push(files, sources, remote)
    made = set(ROOTS)

    def need(folder):
        if folder not in made:
            files.makedirs(folder)
            while folder not in made:
                made.add(folder)
                folder = parent(folder)

    for folder in folders:
        need(folder)
    copied = same = 0
    for local, target in jobs:
        data = local.read_bytes()
        need(parent(target))
        st = files.stat(target)
        if st and st.is_dir:
            raise FilesError(f"{target} is a folder on the Flipper: {local} "
                             "cannot take its place")
        if (st and not force and st.size == len(data)
                and files.md5(target) == md5(data)):
            say(f"{local} = {target} (unchanged)")
            same += 1
            continue
        files.write(target, data,
                    progress and (lambda a, b: progress(str(local), a, b)))
        if files.md5(target) != md5(data):
            raise FilesError(f"{target}: what the Flipper has is not what was "
                             "sent")
        say(f"{local} -> {target} ({len(data)} bytes)")
        copied += 1
    return copied, same


def safe_rel(name):
    """A path from a listing, as one that stays inside where it is put"""
    parts = [p for p in name.split("/") if p]
    if not parts or any(p in (".", "..") or "\\" in p or "\0" in p for p in parts):
        raise FilesError(f"{name!r}: not a name to make a file of here")
    return pathlib.Path(*parts)


def pull(files, sources, local=".", force=False, say=print, progress=None):
    """Copy files and folders from the card; (copied, unchanged).

    local: the folder to put them in (made if it is not there); for one file,
    also its new name."""
    as_folder = str(local).endswith(("/", os.sep))
    local = pathlib.Path(local)
    sources = [clean(s) for s in sources]
    stats = []
    for s in sources:
        st = files.stat(s)
        if not st:
            raise StorageError("file/dir not exist", s)
        stats.append(st)
    folders, jobs = [], []
    if not (as_folder or len(sources) > 1 or stats[0].is_dir or local.is_dir()):
        jobs.append((sources[0], local, stats[0].size))     # its new name
    else:
        for s, st in zip(sources, stats):
            name = safe_rel(s.rsplit("/", 1)[1])
            if not st.is_dir:
                jobs.append((s, local / name, st.size))
                continue
            folders.append(local / name)
            for e in files.tree(s):
                if e.is_dir:
                    folders.append(local / name / safe_rel(e.name))
                else:
                    jobs.append((join(s, e.name), local / name / safe_rel(e.name),
                                 e.size))
    for folder in folders:
        folder.mkdir(parents=True, exist_ok=True)
    copied = same = 0
    for remote, target, size in jobs:
        if target.is_dir():
            raise FilesError(f"{target} is a folder: {remote} cannot take its "
                             "place")
        target.parent.mkdir(parents=True, exist_ok=True)
        if (not force and target.is_file() and target.stat().st_size == size
                and md5(target.read_bytes()) == files.md5(remote)):
            say(f"{remote} = {target} (unchanged)")
            same += 1
            continue
        data = files.read(remote, progress and (lambda a, b: progress(remote, a, b)))
        target.write_bytes(data)
        say(f"{remote} -> {target} ({len(data)} bytes)")
        copied += 1
    return copied, same


def remove(files, path, recursive=False, missing_ok=False):
    """Delete a file or a folder; False: there was none"""
    path = clean(path)
    if path in ROOTS:
        raise FilesError(f"{path} is the storage itself, not something on it")
    st = files.stat(path)
    if not st:
        if missing_ok:
            return False
        raise StorageError("file/dir not exist", path)
    if st.is_dir and recursive:
        files.rmtree(path)
    elif st.is_dir and files.ls(path):
        raise FilesError(f"{path} is a folder with files in it: -r removes it "
                         "and them")
    else:
        files.remove(path)
    return True
