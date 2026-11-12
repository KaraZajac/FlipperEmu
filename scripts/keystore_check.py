#!/usr/bin/env python3
# Copyright (c) 2026, Kara Zajac
# SPDX-License-Identifier: BSD-3-Clause
"""Check the emulator's own Sub-GHz keystores (keystores/overlay; see
docs/keystores.md): the tool that makes them, the files, and what a firmware
makes of them.

    scripts/keystore_check.py               the tool and the files (no
                                            firmware needed)
    scripts/keystore_check.py FIRMWARE      ... and a firmware with them, on
                                            a throwaway device

The first part: scripts/rekey_keystore.py writes what the enclave check's
own encoder writes, and reads it back; every line of the tracked KeeLoq
keystore is a code under the emulator's key 1 and under no other, and making
each of the four files again gives the same bytes; the tool's commands and
what they refuse; git has the encrypted files and would not take a decrypted
list.

The second part boots FIRMWARE (an update package, as ./flipperemu run -f
takes one) on a new device.  Its card has the four files.  The firmware
loads the KeeLoq keystore and saves it again (`subghz encrypt_keeloq`, a
debug command): every line comes back, and with the same IV it is the
tracked file.  Its own `subghz encrypt_raw` makes the three RAW keystores
from their bytes.  Then remotes that this check makes up, as recordings for
`subghz decode_raw`: one KeeLoq remote for every code with simple or normal
learning, which the firmware's decoder is to name after that code, and Nice
Flor-S remotes, whose serial number and counter the firmware reads with the
RAW keystore.  And the same with the keystores of the firmware's own package
in their place, which only a real Flipper's key opens, and the firmware
started anew: nothing is named, and nothing read.  (The other two RAW
keystores, of Alutech AT-4N and CAME Atomo, are not tried with a remote.)

Nothing that is in a keystore is printed: counts, and for a failure the names
of makers.  Exit status 0 = pass.
"""
import argparse
import hashlib
import os
import pathlib
import random
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import enclave_check as ec                              # noqa: E402
import fe_enclave                                       # noqa: E402
import make_sdcard                                      # noqa: E402
import rekey_keystore as rk                             # noqa: E402
from files_check import check, results                  # noqa: E402

OVERLAY = ROOT / "keystores" / "overlay"
ASSETS = OVERLAY / "subghz" / "assets"
ON_CARD = "/ext/subghz/assets"
KEELOQ = "keeloq_mfcodes"
RAW = ("nice_flor_s", "alutech_at_4n", "came_atomo")
RAW_HEADER = "Filetype: Flipper SubGhz Keystore RAW File\nVersion: 0\n"
KEY_LINE = re.compile(r"[0-9A-F]{16}:\d+:\S{1,64}")


# ------------------------------------------------- what a remote would send

NLF = 0x3A5C742E


def bit(x, n):
    return (x >> n) & 1


def nlf(x, a, b, c, d, e):
    return bit(NLF, bit(x, a) | bit(x, b) << 1 | bit(x, c) << 2
               | bit(x, d) << 3 | bit(x, e) << 4)


def keeloq_encrypt(data, key):
    x = data
    for r in range(528):
        x = (x >> 1) ^ ((bit(x, 0) ^ bit(x, 16) ^ bit(key, r & 63)
                         ^ nlf(x, 1, 9, 20, 26, 31)) << 31)
    return x


def keeloq_decrypt(data, key):
    x = data
    for r in range(528):
        x = (((x << 1) & 0xFFFFFFFF) ^ bit(x, 31) ^ bit(x, 15)
             ^ bit(key, (15 - r) & 63) ^ nlf(x, 0, 8, 19, 25, 30))
    return x


def keeloq_normal(fix, key):
    """A remote's own key, by normal learning: from its serial number"""
    serial = fix & 0x0FFFFFFF
    return (keeloq_decrypt(serial | 0x60000000, key) << 32
            | keeloq_decrypt(serial | 0x20000000, key))


# What a decoder wants in the encrypted part beside the button: the low ten
# bits of the serial number, and for some makers something of their own
# (lib/subghz/protocols/keeloq.c: Centurion in every firmware here, the
# other three in Unleashed's and RogueMaster's)
OWN_RULE = {"Centurion": lambda s: 0x1CE, "KEY": lambda s: 0x2FF,
            "Monarch": lambda s: 0x100, "Pecinin": lambda s: s & 0xFFF}


def keeloq_packet(button, serial, counter, key, learning, name=None):
    """One KeeLoq transmission, as the durations of a RAW recording"""
    fix = button << 28 | serial
    disc = OWN_RULE[name](serial) if name else serial & 0x3FF
    hop = keeloq_encrypt(button << 28 | disc << 16 | counter,
                         key if learning == 1 else keeloq_normal(fix, key))
    data = int(f"{fix << 32 | hop:064b}"[::-1], 2)
    out = [400, -400] * 11 + [400, -4000]
    for i in range(63, -1, -1):
        out += [400, -800] if bit(data, i) else [800, -400]
    return out + [400, -800, 400, -16000]


def nice_encrypt(data, table):
    """subghz_protocol_nice_flor_s_encrypt (nice_flor_s.c), with the RAW
    keystore's bytes"""
    p = bytearray(data.to_bytes(8, "little"))

    def xor(k):
        for i in range(1, 6):
            p[i] ^= k

    for y in range(2):
        k = table[p[0] & 0x1F]
        xor(k)
        p[5] &= 0x0F
        p[0] ^= k & 0xE0
        k = (table[p[0] >> 3] + 0x25) & 0xFF
        xor(k)
        p[5] &= 0x0F
        p[0] ^= k & 0x07
        if y == 0:
            p[0], p[1] = p[1], p[0]
    p[5] = ~p[5] & 0x0F
    p[0], p[1], p[2], p[3], p[4] = (~p[2] & 0xFF, ~p[3] & 0xFF, ~p[4] & 0xFF,
                                    ~p[1] & 0xFF, ~p[0] & 0xFF)
    return int.from_bytes(p, "little")


def nice_packet(button, serial, counter, table):
    """One Nice Flor-S transmission of 52 bits"""
    data = nice_encrypt(button << 48 | serial << 16 | counter, table)
    out = [1500, -1500]
    for i in range(51, -1, -1):
        out += [1000, -500] if bit(data, i) else [500, -1000]
    return out + [1500, -19000]


def recording(packets, lead=(400, -19000)):
    values = list(lead) + [v for p in packets for v in p]
    text = ("Filetype: Flipper SubGhz RAW File\nVersion: 1\n"
            "Frequency: 433920000\nPreset: FuriHalSubGhzPresetOok650Async\n"
            "Protocol: RAW\n")
    for at in range(0, len(values), 512):
        text += "RAW_Data: " + " ".join(map(str, values[at:at + 512])) + "\n"
    return text.encode()


# ------------------------------------------------------------ the first part

def raw_bytes(name, key):
    return bytes.fromhex(rk.verify((ASSETS / name).read_text(), key)[0])


def git(*args):
    """(exit status, output) of git here; (None, "") with no git or no
    checkout"""
    try:
        r = subprocess.run(["git", "-C", str(ROOT), *args], capture_output=True,
                           text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return None, ""
    return r.returncode, r.stdout


def host_part():
    print("the tool and the files:")
    rnd = random.Random(1)
    key = fe_enclave.default_key(1)
    check("key 1 is the emulator's own: SHA-256 of a text that everybody has",
          key == hashlib.sha256(b"FlipperEmu enclave key 1").digest())
    ivs = [bytes(rnd.randrange(256) for _ in range(16)) for _ in range(2000)]
    ivs += [bytes(16), b"\xff" * 16, bytes(range(16))]
    check(f"the stored IV is scrambled as the enclave check does it ({len(ivs)} IVs)",
          all(rk.mess_with_iv(iv) == ec.keystore_iv(iv) for iv in ivs))
    same = True
    for _ in range(200):
        lines = ["%016X:%d:%s" % (rnd.getrandbits(64), rnd.randrange(12), "".join(
            rnd.choice("ABCdef_0123") for _ in range(rnd.randrange(1, 60))))
            for _ in range(rnd.randrange(1, 40))]
        iv = bytes(rnd.randrange(256) for _ in range(16))
        text = rk.rekey(lines, key, iv)
        same &= (text.splitlines()[4:] == ec.keystore_lines(key, iv, lines)
                 and text.splitlines()[3] == "IV: " + iv.hex(" ").upper()
                 and rk.verify(text, key) == lines)
    check("200 made-up key lists: written as the enclave check's encoder "
          "writes them, and read back", same)

    names = sorted(p.name for p in ASSETS.iterdir()) if ASSETS.is_dir() else []
    if not check(f"keystores/overlay has the four keystores ({', '.join(names)})",
                 names == sorted((KEELOQ,) + RAW)):
        return
    text = (ASSETS / KEELOQ).read_text()
    lines = rk.verify(text, key)
    check(f"{KEELOQ}: {len(lines)} lines, each a code (KEY:TYPE:NAME) under "
          "key 1, none twice",
          lines and all(KEY_LINE.fullmatch(l) for l in lines)
          and len(set(lines)) == len(lines))
    check(f"{KEELOQ}: made again from its codes, it is the same file",
          rk.rekey(lines, key, rk.DEFAULT_IV) == text)
    try:
        other = [l for l in rk.verify(text, fe_enclave.default_key(2))
                 if KEY_LINE.fullmatch(l)]
    except ValueError:
        other = []
    check(f"{KEELOQ}: under another key no line is a code", not other,
          f"{len(other)} lines")
    for name in RAW:
        text = (ASSETS / name).read_text()
        blob = raw_bytes(name, key)
        head = text.splitlines()[:5]
        check(f"{name}: a RAW keystore of {len(blob)} bytes; made again from "
              "them, it is the same file",
              "".join(l + "\n" for l in head[:2]) == RAW_HEADER
              and head[2] == "Encryption: 1" and head[4] == "Encrypt_data: RAW"
              and blob and rk.rekey_raw(blob, key, rk.DEFAULT_IV) == text)

    tool = [sys.executable, str(ROOT / "scripts" / "rekey_keystore.py")]

    def run(*args):
        r = subprocess.run(tool + [str(a) for a in args], capture_output=True,
                           text=True, timeout=60)
        return r.returncode, r.stdout, r.stderr

    with tempfile.TemporaryDirectory(prefix="fe-ks-") as tmp:
        tmp = pathlib.Path(tmp)
        made = ["0123456789ABCDEF:1:Made_Up_One", "FEDCBA9876543210:2:Made_Up_Two",
                "# a comment", "", "00000000000000AA:0:x"]
        (tmp / "in.txt").write_text("\n".join(made) + "\n")
        code, out, err = run("keys", tmp / "in.txt", tmp / "out")
        code2, out2, _ = run("verify", tmp / "out")
        check("the tool: `keys` writes a keystore of three made-up codes, and "
              "`verify` gives them back",
              code == 0 and "3 keys" in out and code2 == 0
              and out2.split() == [m for m in made if m and m[0] != "#"],
              out + err + out2)
        code, out, err = run("keys", tmp / "in.txt", tmp / "out2", "--iv",
                             "0x" + "11" * 16)
        check("... --iv is the IV in the file",
              code == 0 and "IV: " + " ".join(["11"] * 16)
              in (tmp / "out2").read_text(), out + err)
        (tmp / "bad.txt").write_text("not a key line\n")
        code, out, err = run("keys", tmp / "bad.txt", tmp / "out3")
        check("... a line that is no code: refused, and no file made",
              code == 1 and "expected KEY:TYPE:NAME" in err
              and not (tmp / "out3").exists(), out + err)
        (tmp / "raw.hex").write_text("00112233445566778899AABBCCDDEEFF\n# c\n"
                                     "B1B2B3B4B5B6B7B8090A0B0C0D0E0F10\n")
        code, out, err = run("raw", tmp / "raw.hex", tmp / "rawout")
        code2, out2, _ = run("verify", tmp / "rawout")
        check("... `raw` writes a RAW keystore of 32 made-up bytes, and "
              "`verify` gives them back",
              code == 0 and "32 RAW bytes" in out and code2 == 0
              and "00112233445566778899AABBCCDDEEFFB1B2B3B4B5B6B7B8090A0B0C0D0E"
              "0F10" in out2.upper().replace(" ", ""), out + err + out2)
        (tmp / "odd.hex").write_text("001122\n")
        code, out, err = run("raw", tmp / "odd.hex", tmp / "odd")
        check("... a part of a block: refused",
              code == 1 and "whole 16-byte blocks" in err, out + err)
        code, out, err = run("verify", tmp / "nothere")
        check("... a file that is not there: said so",
              code == 1 and err.startswith("rekey:"), out + err)
        code, out, err = run("--help")
        check("... --help", code == 0 and "raw" in out and "--enclave" in out,
              out + err)

        base = ["A1A2A3A4A5A6A7A8:1:Base_Simple", "0A0B0C0D0E0F1011:0:Base_Any",
                "B1B2B3B4B5B6B7B8:16:Base_Special"]
        more = ["# a comment", "A1A2A3A4A5A6A7A8:1:Other_Name",
                "A1A2A3A4A5A6A7A8:2:Base_Normal", "C1C2C3C4C5C6C7C8:0:New_Any",
                "D1D2D3D4D5D6D7D8:2:New_Normal", "11100F0E0D0C0B0A:2:Twin_Of_Any",
                "B8B7B6B5B4B3B2B1:0:Twin_Reversed", "D1D2D3D4D5D6D7D8:0:Twin_Of_New",
                "E1E2E3E4E5E6E7E8:20:No_Such_Type", "F1F2F3F4F5F6F7F8:19:Last_Type",
                "a1b2c3d4e5f60718:1:lower"]
        (tmp / "base.txt").write_text("\n".join(base) + "\n")
        (tmp / "more.txt").write_text("\n".join(more) + "\n")
        code, out, err = run("merge", tmp / "base.txt", tmp / "more.txt",
                             tmp / "merged")
        got = (tmp / "merged").read_text().split() if code == 0 else []
        check("... `merge`: the base list's lines first and as they were, then "
              "the new codes, those of type 0 last",
              got == base + [more[2], more[4], more[9], more[10].upper()
                             .replace("LOWER", "lower"), more[3]]
              and "8 lines" in out and "5 new" in out and "7 keys" in out,
              out + err)
        said = [l.split() for l in out.splitlines() if l.startswith("  ")]
        check("... left out and said by name: a code that the list has, a type "
              "that no firmware has, and a second line for a key that has one",
              said == [["Other_Name:1", "(Base_Simple:1)"], ["No_Such_Type:20"],
                       ["Twin_Of_Any:2", "(Base_Any:0)"],
                       ["Twin_Reversed:0", "(Base_Special:16)"],
                       ["Twin_Of_New:0", "(New_Normal:2)"]]
              and not re.search(r"[0-9A-Fa-f]{16}", out + err), out + err)
        code, out, err = run("merge", "--all", tmp / "base.txt",
                             tmp / "more.txt", tmp / "all")
        got = (tmp / "all").read_text().split() if code == 0 else []
        check("... --all leaves out only the code that the list has",
              got == base + more[2:10] + [more[10].upper()
                                          .replace("LOWER", "lower")]
              and "12 lines" in out, out + err)
        refused = []
        for n, (bad, word) in enumerate((
                ("9192939495969798:1:Two Words", "no blank"),
                ("919293949596979:1:Short_Key", "16 hex digits"),
                ("9192939495969798:1:A:B", "expected KEY:TYPE:NAME"))):
            (tmp / "bad.txt").write_text(bad + "\n")
            code, out, err = run("merge", tmp / "base.txt", tmp / "bad.txt",
                                 tmp / f"no{n}")
            refused.append(code == 1 and word in err and "91929394" not in err
                           and not (tmp / f"no{n}").exists())
        code, out, err = run("merge", tmp / "base.txt", tmp / "more.txt",
                             tmp / "base.txt")
        refused.append(code == 1 and "another file" in err and
                       (tmp / "base.txt").read_text().split() == base)
        check("... refused, with no file made and no key in the message: a name "
              "with a blank, a key of 15 digits, a line of four fields, and "
              "the new list over one that is read", all(refused),
              f"{refused} {out}{err}")

    code, out = git("ls-files", "--", "keystores")
    if code != 0:
        print("  (no git checkout here: what is tracked is not looked at)")
        return
    tracked = out.split()
    check("git has the encrypted keystores, and nothing else under "
          f"keystores/ ({len(tracked)} files)",
          sorted(tracked) == sorted(f"keystores/overlay/subghz/assets/{n}"
                                    for n in names), "\n".join(tracked))
    code, _ = git("check-ignore", "-q", "keystores/plaintext/any.keys")
    check("a decrypted list in keystores/plaintext/ is ignored by git",
          code == 0)


# ----------------------------------------------------------- the second part

def put(cli, path, data: bytes, chunk=512):
    cli.run(f"storage remove {path}", timeout=10)       # write_chunk appends
    for at in range(0, len(data), chunk):
        part = data[at:at + chunk]
        ready, out = cli.watch(f"storage write_chunk {path} {len(part)}",
                               r"Ready", timeout=10, stop=False)
        if not ready:
            raise ec.Failure(f"storage write_chunk {path}: {out.strip()[-80:]}")
        cli.write(part)
        cli.read_until_prompt(15)


def md5_on_card(cli, path):
    out = cli.run(f"storage md5 {path}", timeout=30)
    m = re.search(r"\b[0-9a-f]{32}\b", out)
    return m.group(0) if m else None


def same_on_card(cli, path, data: bytes):
    return md5_on_card(cli, path) == hashlib.md5(data).hexdigest()


def lines_on_card(cli, path):
    out = cli.run(f"storage read {path}", timeout=30)
    if "Storage error" in out or not out.startswith("Size:"):
        return []
    return [l for l in out.splitlines()[1:] if l.strip()]


def decode(cli, path, timeout=300):
    """`subghz decode_raw`: (packets it decoded, what it printed)"""
    found, out = cli.watch(f"subghz decode_raw {path}",
                           r"Packets received\D*(\d+)", timeout=timeout,
                           stop=False)
    try:
        cli.read_until_prompt(15)
    except TimeoutError:
        pass
    if not found:
        raise ec.Failure("decode_raw did not end: "
                         + out.strip()[-120:].replace("\n", " | "))
    return int(found.group(1)), out


def keeloq_named(out):
    """{serial: the maker it was named after}; official firmware ends the
    name with a line end, the others with nothing"""
    return {int(m.group(1), 16) & 0x0FFFFFFF: m.group(2) for m in re.finditer(
        r"Fix:(?:0x)?([0-9A-Fa-f]{8}).*?MF:(.*?)(?=KeeLoq \d+bit|\s|\Z)", out,
        re.S)}


def nice_read(out):
    """{(serial, counter, button)} of the Nice Flor-S packets it read"""
    return {tuple(int(x, 16) for x in m.groups()) for m in re.finditer(
        r"Sn:\s*(?:0x)?([0-9A-Fa-f]+)\s+Cnt:\s*(?:0x)?([0-9A-Fa-f]+)\s+"
        r"Btn:\s*(?:0x)?([0-9A-Fa-f]+)", out)}


def stock_keystores(firmware):
    """{name: bytes} of the keystores in the firmware's own package"""
    found = {}
    with tempfile.TemporaryDirectory(prefix="fe-ks-") as tmp:
        try:
            make_sdcard.stage_resources(pathlib.Path(firmware), pathlib.Path(tmp))
        except (tarfile.TarError, OSError, ValueError):
            return found
        for name in (KEELOQ,) + RAW:
            p = pathlib.Path(tmp) / "subghz" / "assets" / name
            if p.is_file():
                found[name] = p.read_bytes()
    return found


def keeloq_remotes(cli, codes, tag):
    """A made-up remote for each code with simple or normal learning, in one
    recording: (how many, {index: the name it got}, how many needed the
    maker's own rule)"""
    targets = [(i, int(k, 16), int(t), n) for i, (k, t, n) in enumerate(codes)
               if t in ("1", "2")]
    serial = {i: 0x0500000 + i * 0x1111 for i, *_ in targets}
    named, own = {}, set()
    for attempt in range(3):
        todo = [t for t in targets if named.get(t[0]) != t[3]]
        if not todo:
            break
        packets = [keeloq_packet(2, 0x0ABCDEF, 7, 0x1122334455667788, 1)]
        for i, key, learning, name in todo:
            rule = name if attempt and name in OWN_RULE else None
            if rule:
                own.add(i)
            packets.append(keeloq_packet(2, serial[i], 0x0100 + i + attempt * 0x1000,
                                         key, learning, rule))
        put(cli, f"/ext/{tag}.sub", recording(packets, lead=()))
        _, out = decode(cli, f"/ext/{tag}.sub")
        seen = keeloq_named(out)
        for i, *_ in todo:
            named[i] = seen.get(serial[i])
    return targets, named, len([i for i in own if named.get(i) == codes[i][2]])


NICE = [(1 << n % 4, 0x0123456 + n * 0x0102030, 0x0100 + n * 0x0111)
        for n in range(8)]                  # (button, serial, counter)


def nice_remotes(cli, table, tag):
    """Each remote's packet twice, as a remote repeats it while its button
    is held: Unleashed's decoder (RogueMaster has it too) takes a packet
    that comes once for another protocol of this length"""
    put(cli, f"/ext/{tag}.sub", recording([nice_packet(b, s, c, table)
                                           for b, s, c in NICE
                                           for _ in range(2)]))
    count, out = decode(cli, f"/ext/{tag}.sub")
    read = nice_read(out)
    return count, sum((s, c, b) in read for b, s, c in NICE)


def firmware_part(firmware, timeout, keep):
    print(f"a firmware with them ({pathlib.Path(firmware).name}):")
    key = fe_enclave.default_key(1)
    tracked = {n: (ASSETS / n).read_bytes() for n in (KEELOQ,) + RAW}
    text = tracked[KEELOQ].decode()
    codes = [l.split(":", 2) for l in rk.verify(text, key)]     # in memory only
    table = raw_bytes("nice_flor_s", key)
    stock = stock_keystores(firmware)
    emu = ec.Emu(f"kscheck-{int(time.time())}-{os.getpid()}",
                 pathlib.Path(firmware).resolve(), timeout)
    tag = "fe_keystore_check"
    try:
        try:
            emu.start()
        except ec.Failure as e:
            check("boot: 'Startup complete', and its CLI answers", False, e)
            return
        check("boot: 'Startup complete', and its CLI answers", True)
        cli = emu.cli
        wrong = [n for n, data in tracked.items()
                 if not same_on_card(cli, f"{ON_CARD}/{n}", data)]
        check("the new device's card has the emulator's four keystores",
              not wrong, ", ".join(wrong))

        cli.run("sysctl debug 1", timeout=10)
        commands = cli.run("subghz", timeout=10)
        if "encrypt_keeloq" in commands:
            one, two = f"/ext/{tag}.one", f"/ext/{tag}.two"
            for path in (one, two):
                cli.run(f"storage remove {path}", timeout=10)
            out = cli.run(f"subghz encrypt_keeloq {ON_CARD}/{KEELOQ} {one} "
                          f"{rk.DEFAULT_IV.hex()}", timeout=300)
            back = lines_on_card(cli, one)
            check(f"the firmware loads {KEELOQ} and saves it again: "
                  f"{max(0, len(back) - 4)} of {len(codes)} lines, and with "
                  "the same IV the tracked file",
                  "Failed" not in out and back == text.splitlines(),
                  f"{out.strip()[-200:]}\n{len(back)} lines")
            iv = bytes(range(0x31, 0x41))
            cli.run(f"subghz encrypt_keeloq {ON_CARD}/{KEELOQ} {two} {iv.hex()}",
                    timeout=300)
            try:
                again = rk.verify("\n".join(lines_on_card(cli, two)) + "\n", key)
            except (ValueError, IndexError):
                again = []
            check(f"... and with another IV: the same {len(codes)} codes, read "
                  "here", again == [":".join(c) for c in codes],
                  f"{len(again)} lines")
            for path in (one, two):
                cli.run(f"storage remove {path}", timeout=10)
        else:
            print("  (this firmware's CLI has no `subghz encrypt_keeloq`: not "
                  "tried)")
        if "encrypt_raw" in commands:
            plain, made = f"/ext/{tag}.raw", f"/ext/{tag}.enc"
            for name in RAW:
                blob = raw_bytes(name, key)
                put(cli, plain, (RAW_HEADER + "Encryption: 0\n"
                                 + blob.hex().upper()).encode())
                cli.run(f"storage remove {made}", timeout=10)
                out = cli.run(f"subghz encrypt_raw {plain} {made} "
                              f"{rk.DEFAULT_IV.hex()}", timeout=120)
                check(f"the firmware's own `subghz encrypt_raw` makes {name} "
                      f"from its {len(blob)} bytes: the tracked file",
                      "Failed" not in out
                      and lines_on_card(cli, made) == tracked[name].decode().splitlines(),
                      out.strip()[-200:])
            for path in (plain, made):
                cli.run(f"storage remove {path}", timeout=10)
        else:
            print("  (this firmware's CLI has no `subghz encrypt_raw`: not "
                  "tried)")

        targets, named, own = keeloq_remotes(cli, codes, tag)
        good = [i for i, _, _, n in targets if named.get(i) == n]
        by = {t: (sum(1 for i, _, l, n in targets if l == t and named.get(i) == n),
                  sum(1 for _, _, l, _ in targets if l == t)) for t in (1, 2)}
        off = [f"{n} (learning {l}: {named.get(i) or 'not decoded'})"
               for i, _, l, n in targets if named.get(i) != n]
        check(f"KeeLoq: {len(good)} of {len(targets)} made-up remotes are "
              "named after the code they were made with (simple learning "
              f"{by[1][0]} of {by[1][1]}, normal learning {by[2][0]} of "
              f"{by[2][1]}" + (f"; {own} by their maker's own rule" if own
                               else "") + ")",
              len(good) == len(targets), "not so: " + ", ".join(off))
        count, read = nice_remotes(cli, table, tag)
        check(f"Nice Flor-S: {read} of {len(NICE)} made-up remotes are read "
              "with their serial number, counter and button, with the RAW "
              f"keystore ({count} packets decoded)", read == len(NICE))
        logged = ""

        # the keystores of the firmware's own package, as a card had them
        # before, and has them again after the firmware's updater.  The
        # firmware is started anew for it: Unleashed's keeps a table that it
        # has read
        for name in (KEELOQ, "nice_flor_s"):
            if name in stock:
                put(cli, f"{ON_CARD}/{name}", stock[name])
        if KEELOQ in stock or "nice_flor_s" in stock:
            logged = emu.uart_text()
            emu.stop()
            try:
                emu.start()
            except ec.Failure as e:
                check("the firmware starts again, with the package's own "
                      "keystores on the card", False, e)
                return
            cli = emu.cli
        if KEELOQ in stock:
            targets, named, _ = keeloq_remotes(cli, codes, tag)
            got = sum(1 for i, _, _, n in targets if named.get(i) == n)
            check(f"with the package's own {KEELOQ} in its place, which a real "
                  f"Flipper's key opens: {got} of {len(targets)} are named",
                  same_on_card(cli, f"{ON_CARD}/{KEELOQ}", stock[KEELOQ])
                  and got == 0)
        else:
            print(f"  (no {KEELOQ} in the firmware's package: the other way "
                  "round is not tried)")
        if "nice_flor_s" in stock:
            count, read = nice_remotes(cli, table, tag)
            check("... and with its own nice_flor_s: "
                  f"{read} of {len(NICE)} are read as they were made "
                  f"({count} packets decoded)",
                  same_on_card(cli, f"{ON_CARD}/nice_flor_s", stock["nice_flor_s"])
                  and read == 0 and count >= len(NICE))
        for name in (KEELOQ, "nice_flor_s"):            # as it was
            put(cli, f"{ON_CARD}/{name}", tracked[name])
        cli.run(f"storage remove /ext/{tag}.sub", timeout=10)
        cli.run("sysctl debug 0", timeout=10)
        wrong = [n for n, data in tracked.items()
                 if not same_on_card(cli, f"{ON_CARD}/{n}", data)]
        check("the emulator's keystores are on the card as they were",
              not wrong, ", ".join(wrong))
        crash = ec.CRASH.search(logged + emu.uart_text())
        check("no crash in the firmware's log", not crash, crash and crash.group(0))
    except (ec.Failure, TimeoutError, OSError, ConnectionError) as e:
        check("the firmware's CLI did what was asked", False,
              f"{type(e).__name__}: {str(e)[:300]}")
    finally:
        emu.stop()
        if not keep:
            shutil.rmtree(emu.dir, ignore_errors=True)
        else:
            print(f"  device kept: {emu.dir}")


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("firmware", nargs="?",
                    help="an update package: also checked on a throwaway "
                         "device with it")
    ap.add_argument("--timeout", type=float, default=240.0,
                    help="seconds to wait for the firmware to start")
    ap.add_argument("--keep", action="store_true",
                    help="keep the throwaway device")
    args = ap.parse_args()
    if not ASSETS.is_dir():
        sys.exit(f"{OVERLAY}: no keystores here to check")
    host_part()
    if args.firmware:
        firmware_part(args.firmware, args.timeout, args.keep)
    bad = [label for label, ok in results if not ok]
    print(f"{len(results) - len(bad)} of {len(results)} checks passed")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
