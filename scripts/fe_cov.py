# Copyright (c) 2026, Kara Zajac
# SPDX-License-Identifier: BSD-3-Clause
"""Coverage: what a Flipper executed, against an ELF's functions and lines.

The emulator's coverage plugin (qemu/plugins/fe_cov.c, `run --coverage`)
counts each block of code that runs.  This reads its counters (a socket in
the device's folder, or the dump it writes when the emulator ends), and
maps them: to the functions of an ELF (its symbol table), to source lines
(its line table, which binutils' `readelf` decodes), into a summary, a
list, or an lcov file (`genhtml`, an editor's coverage gutters).

A byte's count is the sum of the blocks that hold it: two blocks may
overlap (a jump into the middle of one makes another), and each run of
either runs the bytes they share.
"""
import bisect
import collections
import fnmatch
import pathlib
import re
import shutil
import socket
import struct
import subprocess

HEADER = "# flipperemu coverage 1"


class Error(Exception):
    pass


# ---------------------------------------------------------------- the plugin

def ask(sock, command, timeout=10.0):
    """A line to the plugin's socket -> its answer's words after `ok`"""
    s = socket.socket(socket.AF_UNIX)
    s.settimeout(timeout)
    try:
        s.connect(str(sock))
        s.sendall(command.encode() + b"\n")
        said = b""
        while True:
            got = s.recv(4096)
            if not got:
                break
            said += got
    except OSError as e:
        raise Error(f"the coverage plugin does not answer ({e})") from None
    finally:
        s.close()
    said = said.decode(errors="replace").strip()
    if not said.startswith("ok"):
        raise Error(f"the coverage plugin: {said or 'no answer'}")
    return said[2:].strip()


def numbers(words):
    """"blocks=10592 executed=9733" -> {"blocks": 10592, "executed": 9733}"""
    return {k: int(v) for k, v in re.findall(r"(\w+)=(\d+)", words)}


def read_dump(path):
    """A dump -> [(address, bytes, count, the bytes themselves or None)]:
    the plugin keeps the bytes of a block in RAM, where an app's code is"""
    blocks = []
    with open(path) as f:
        first = f.readline().strip()
        if first != HEADER:
            raise Error(f"{path} is no coverage dump (its first line is not "
                        f"`{HEADER}`)")
        for line in f:
            w = line.split()
            if len(w) in (3, 4):
                code = bytes.fromhex(w[3]) if len(w) == 4 else None
                blocks.append((int(w[0], 16), int(w[1]), int(w[2]), code))
    return blocks


class Ran:
    """What ran: pieces of memory, none overlapping, each with how often"""

    def __init__(self, blocks):
        edge = collections.defaultdict(int)
        for addr, size, count, *_ in blocks:
            if size and count:
                edge[addr] += count
                edge[addr + size] -= count
        self.starts, self.ends, self.counts = [], [], []
        level, last = 0, None
        for at in sorted(edge):
            if level and last is not None and at > last:
                if (self.ends and self.ends[-1] == last
                        and self.counts[-1] == level):
                    self.ends[-1] = at
                else:
                    self.starts.append(last)
                    self.ends.append(at)
                    self.counts.append(level)
            level += edge[at]
            last = at
        self.blocks = len(blocks)

    def pieces(self, lo, hi):
        """The pieces within [lo, hi): (start, end, count), cut to it"""
        i = bisect.bisect_right(self.ends, lo)
        while i < len(self.starts) and self.starts[i] < hi:
            yield max(self.starts[i], lo), min(self.ends[i], hi), self.counts[i]
            i += 1

    def bytes(self, lo=0, hi=1 << 32):
        return sum(e - s for s, e, _ in self.pieces(lo, hi))

    def most(self, lo, hi):
        """The highest count of a byte in [lo, hi), 0: none of it ran"""
        return max((c for _, _, c in self.pieces(lo, hi)), default=0)

    def at(self, addr):
        return self.most(addr, addr + 1)


# ------------------------------------------------------------------- the ELF

SHF_ALLOC, SHF_EXEC = 0x2, 0x4
STT_FUNC = 2


class Elf:
    """An ELF32 little-endian file's sections and functions (a firmware's,
    linked; or an app's, whose sections each begin at 0)"""

    def __init__(self, path):
        self.path = pathlib.Path(path)
        d = self.data = self.path.read_bytes()
        if d[:6] != b"\x7fELF\x01\x01":
            raise Error(f"{path} is no 32-bit little-endian ELF")
        (self.type,) = struct.unpack_from("<H", d, 16)
        shoff, = struct.unpack_from("<I", d, 32)
        shentsize, shnum, shstrndx = struct.unpack_from("<HHH", d, 46)
        raw = [struct.unpack_from("<IIIIIIIIII", d, shoff + i * shentsize)
               for i in range(shnum)]
        names = raw[shstrndx] if shnum else None

        def name(off):
            end = d.index(b"\0", names[4] + off)
            return d[names[4] + off:end].decode(errors="replace")
        # (name, type, flags, addr, offset, size, link, info)
        self.sections = [(name(s[0]), s[1], s[2], s[3], s[4], s[5], s[6], s[7])
                         for s in raw]
        self.relocatable = self.type == 1

    def section(self, name):
        for i, s in enumerate(self.sections):
            if s[0] == name:
                return i, s
        return None, None

    def code(self):
        """The sections that hold code: [(index, name, address, size)]"""
        return [(i, s[0], s[3], s[5]) for i, s in enumerate(self.sections)
                if s[2] & SHF_ALLOC and s[2] & SHF_EXEC and s[1] == 1 and s[5]]

    def bytes_of(self, index):
        s = self.sections[index]
        return self.data[s[4]:s[4] + s[5]]

    def functions(self, section=None):
        """[(address, size, name)], by address; a Thumb function's address
        without its low bit; one address under several names once.  With
        `section` (an index): those of that section alone"""
        i, tab = self.section(".symtab")
        if tab is None:
            return []
        strings = self.sections[tab[6]]
        d, found = self.data, {}
        for off in range(tab[4], tab[4] + tab[5], 16):
            n, value, size, info, _, shndx = struct.unpack_from("<IIIBBH", d, off)
            if info & 0xF != STT_FUNC or not size or shndx in (0, 0xFFF1):
                continue
            if section is not None and shndx != section:
                continue
            end = d.index(b"\0", strings[4] + n)
            name = d[strings[4] + n:end].decode(errors="replace")
            addr = value & ~1
            if addr not in found or len(name) < len(found[addr][1]):
                found[addr] = (size, name)
        return [(a, s, n) for a, (s, n) in sorted(found.items())]

    def relocations(self, index):
        """Where a section's bytes are not its file's when it is loaded:
        [(offset, type)] of its relocations (REL entries: offset, info)"""
        sites = []
        for s in self.sections:
            if s[1] in (9, 4) and s[7] == index:         # SHT_REL, SHT_RELA
                step = 8 if s[1] == 9 else 12
                for off in range(s[4], s[4] + s[5], step):
                    at, info = struct.unpack_from("<II", self.data, off)
                    sites.append((at, info & 0xFF))
        return sorted(sites)


def readelf():
    for name in ("arm-none-eabi-readelf", "readelf"):
        found = shutil.which(name)
        if found:
            return found
    return None


ROW = re.compile(r"(\S+)\s+(\d+|-)\s+0x([0-9a-f]+)(?:\s+\d+)?(?:\s+x)?\s*$")


def lines_of(elf, tool=None):
    """An ELF's line table: [(start, end, file, line)], each a run of bytes
    that is a line's.  Several lines may have the same bytes (a call that
    was put in place).  None: no `readelf` here, or no line table."""
    tool = tool or readelf()
    if not tool:
        return None
    try:
        out = subprocess.run([tool, "--debug-dump=decodedline", "-W",
                              str(elf.path)], capture_output=True, text=True,
                             errors="replace", timeout=300).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    code = [(a, a + n) for _, _, a, n in elf.code()]

    def in_code(addr):
        return any(lo <= addr < hi for lo, hi in code)
    rows, seq = [], []                  # a sequence: [(addr, file, line)]
    known, current = {}, None           # this unit's files, by their names

    def close(end):
        """A sequence ends at `end`: its rows get their bytes"""
        # (a function that the linker left out has its lines still, at 0:
        # no code is there in a firmware, and those go)
        if seq and in_code(seq[0][0]):
            i = 0
            while i < len(seq):
                j = i
                while j < len(seq) and seq[j][0] == seq[i][0]:
                    j += 1
                stop = seq[j][0] if j < len(seq) else end
                if stop > seq[i][0]:
                    for addr, name, line in seq[i:j]:
                        rows.append((addr, stop, name, line))
                i = j
        seq.clear()
    for text in out.splitlines():
        text = text.rstrip()
        if not text or text.startswith(("File name", "Contents of")):
            continue
        if text.endswith(":") and not ROW.match(text):
            unit = text.startswith("CU: ")
            path = text[4:-1] if unit else text[:-1]
            if unit:
                known = {}
            known[path.rsplit("/", 1)[-1]] = path
            current = path
            continue
        hit = ROW.match(text)
        if not hit:
            continue
        name, line, addr = hit.group(1), hit.group(2), int(hit.group(3), 16)
        if line == "-":
            close(addr)
            continue
        if current is None or current.rsplit("/", 1)[-1] != name:
            current = known.get(name, name)
        seq.append((addr, current, int(line)))
    close(0)
    return rows or None


# ---------------------------------------------------------------- the report

class Report:
    """What ran of an ELF's code.  `ran`: in the ELF's own addresses (an
    app's: from its code section's first byte)"""

    def __init__(self, elf, ran, section=None, tool=None):
        self.elf, self.ran = elf, ran
        self.functions = []             # (addr, size, name, bytes ran, calls)
        for addr, size, name in elf.functions(section):
            self.functions.append((addr, size, name, ran.bytes(addr, addr + size),
                                   ran.at(addr)))
        self.rows = lines_of(elf, tool)
        self.lines = {}                 # file -> {line: count}
        self.where = {}                 # a function's address -> (file, line)
        if self.rows:
            starts = {}
            for start, end, name, line in self.rows:
                per = self.lines.setdefault(name, {})
                per[line] = max(per.get(line, 0), ran.most(start, end))
                starts.setdefault(start, (name, line))
            for addr, *_ in self.functions:
                if addr in starts:
                    self.where[addr] = starts[addr]

    def totals(self):
        f = self.functions
        lines = [c for per in self.lines.values() for c in per.values()]
        return {"bytes": sum(x[1] for x in f), "bytes_ran": sum(x[3] for x in f),
                "functions": len(f), "functions_ran": sum(1 for x in f if x[3]),
                "lines": len(lines), "lines_ran": sum(1 for c in lines if c)}

    def files(self):
        """[(file, lines, lines ran, functions, functions ran)], the file
        with the most lines that ran first"""
        per = {name: [len(v), sum(1 for c in v.values() if c), 0, 0]
               for name, v in self.lines.items()}
        for addr, size, name, ran, calls in self.functions:
            where = self.where.get(addr)
            if where and where[0] in per:
                per[where[0]][2] += 1
                per[where[0]][3] += bool(ran)
        return sorted(((n, *v) for n, v in per.items()),
                      key=lambda x: (-x[2], x[0]))

    def lcov(self, root=None):
        """The report as an lcov file's text.  `root`: the folder that the
        ELF's file names are from (they are as the build had them)"""
        out = []
        by_file = collections.defaultdict(list)
        for addr, size, name, ran, calls in self.functions:
            where = self.where.get(addr)
            if where:
                by_file[where[0]].append((where[1], name, calls or (1 if ran else 0)))
        for name in sorted(self.lines):
            path = pathlib.PurePosixPath(name)
            if root and not path.is_absolute():
                path = pathlib.PurePosixPath(str(root)) / path
            out.append("TN:")
            out.append(f"SF:{path}")
            funcs = sorted(by_file.get(name, []))
            for line, func, _ in funcs:
                out.append(f"FN:{line},{func}")
            for _, func, calls in funcs:
                out.append(f"FNDA:{calls},{func}")
            out.append(f"FNF:{len(funcs)}")
            out.append(f"FNH:{sum(1 for f in funcs if f[2])}")
            per = self.lines[name]
            for line in sorted(per):
                out.append(f"DA:{line},{per[line]}")
            out.append(f"LF:{len(per)}")
            out.append(f"LH:{sum(1 for c in per.values() if c)}")
            out.append("end_of_record")
        return "\n".join(out) + "\n"

    def listed(self, pattern=None, missed=False):
        """Functions, for a list: [(name, file:line or "", size, bytes ran,
        calls)], those whose name or file fits `pattern`; missed: those of
        which nothing ran"""
        out = []
        for addr, size, name, ran, calls in self.functions:
            where = self.where.get(addr)
            place = f"{where[0]}:{where[1]}" if where else ""
            if pattern and not (fnmatch.fnmatch(name, pattern)
                                or (where and fnmatch.fnmatch(where[0], pattern))):
                continue
            if missed and ran:
                continue
            out.append((name, place, size, ran, calls))
        return out


def percent(part, whole):
    return f"{100 * part / whole:.1f} %" if whole else "none"


# ----------------------------------------------------------- an app's part

# relocations of a word of data (an address in a literal pool): R_ARM_ABS32,
# REL32, TARGET1, PREL31.  The others change an instruction (a call).
DATA_RELOCS = (2, 3, 38, 42)


def app_ran(elf, blocks):
    """An app's part of what ran: (Ran in the ELF's own addresses, the index
    of its code section, the addresses its code was loaded at, the blocks
    that ran in those places and are not its code), or None: nothing of
    what ran is its code.

    The loader copies the app's code to where the heap has room and changes
    the places its relocations name: an instruction's (a call) is not
    compared, and a word of data's (an address in a literal pool) is no
    code, so no block of the app's has it.  The blocks in RAM come with the
    bytes they had: one that fits the app's code at some place says where
    the app was (address less place), and at each such base every block
    that fits is the app's.  An app that ran twice, in two places, has both
    runs counted.  Blocks there that do not fit are another build of the
    app than this file, or another app that had the place."""
    code = elf.code()
    if not code:
        return None
    index, _, addr, size = max(code, key=lambda c: c[3])
    text = elf.bytes_of(index)
    skip = bytearray(len(text))         # 1: an instruction's, 2: data
    for off, kind in elf.relocations(index):
        for k in range(off, min(off + 4, len(text))):
            skip[k] = 2 if kind in DATA_RELOCS else 1

    def fits(data, off):
        if off < 0 or off + len(data) > len(text):
            return False
        same = 0
        for k in range(len(data)):
            if skip[off + k] == 2:
                return False
            if not skip[off + k]:
                if data[k] != text[off + k]:
                    return False
                same += 1
        return same >= 2 or len(data) == 4      # (a call alone is a block)
    kept = [(a, n, c, d) for a, n, c, d in blocks if d]
    # where it was: from blocks long enough to be told from others (a
    # block's last instruction may be a call, which a relocation changed)
    votes = collections.Counter()
    for a, n, c, data in kept:
        probe = data[:-4] if len(data) >= 12 else data
        if len(probe) < 8:
            continue
        at = text.find(probe)
        while at >= 0:
            if fits(data, at):
                votes[a - at] += len(data)
            at = text.find(probe, at + 1)
    mine, bases, others = [], [], 0
    for base, weight in votes.most_common():
        if weight < 16:
            break
        bases.append(base)
        for a, n, c, data in kept:
            if fits(data, a - base):
                mine.append((a - base + addr, n, c, None))
            elif base <= a < base + len(text):
                others += 1
    if not mine:
        return None
    return Ran(mine), index, sorted(bases), others
