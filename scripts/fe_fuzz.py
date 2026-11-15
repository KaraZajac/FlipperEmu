# Copyright (c) 2026, Kara Zajac
# SPDX-License-Identifier: BSD-3-Clause
"""Fuzzing the firmware's parsers on an emulated Flipper (`flipperemu fuzz`).

An input goes to the firmware the way a real one would, the Flipper runs
on, and three things are looked at after it: did the firmware crash (its
log, a halt, a lockup), did it stop answering, and did code run that never
ran before (the coverage plugin's `news`, with `run --coverage`).  An input
that got somewhere new is kept, and later inputs are made from the kept
ones: a change of a byte or a pulse, a piece cut out or put in twice, two
of them joined.  That is what finds a parser's inner branches, one check at
a time, where inputs made blindly would have to pass all of them at once.

The targets (what an input is, and how the firmware gets it):

  subghz   +high/-low µs on the air while `subghz rx` listens: the
           protocols' decoders
  ir       marks and spaces at the IR receiver while `ir rx` listens
  rpc      bytes into an RPC session on the USB serial port: the protobuf
           decoder and what handles its messages
  cli      a CLI command with the input in it: as hex ({hex}), as text
           ({text}), or as a file on the card ({file}) that the command
           reads

The Flipper is not reset between inputs: it runs as a Flipper does, and an
input meets what the ones before left.  So a crash is kept with the inputs
that came before it.
"""
import os
import pathlib
import random
import re
import time

CRASH = re.compile(rb"\[CRASH\][^\r\n]*|furi_check failed[^\r\n]*|"
                   rb"HardFault[^\r\n]*|Kernel is Dead[^\r\n]*")
INTERESTING = (0, 1, 2, 3, 4, 7, 8, 15, 16, 31, 32, 63, 64, 100, 127, 128,
               129, 255)
WORDS = (0, 1, 0x7F, 0x80, 0xFF, 0x100, 0x7FFF, 0x8000, 0xFFFF, 0x10000,
         0x7FFFFFFF, 0x80000000, 0xFFFFFFFF)


class Lost(Exception):
    """The firmware does not take inputs any more: it crashed, halted, or
    hangs (why, in words)"""


# ------------------------------------------------------------------ mutation

def mutate_bytes(data, rng, other=b"", most=4096):
    """One change to a byte string"""
    d = bytearray(data)
    kind = rng.randrange(12)
    if not d:
        kind = 5
    at = rng.randrange(len(d)) if d else 0
    if kind == 0:                               # a bit
        d[at] ^= 1 << rng.randrange(8)
    elif kind == 1:                             # a byte, any
        d[at] = rng.randrange(256)
    elif kind == 2:                             # a byte, a telling one
        d[at] = rng.choice(INTERESTING)
    elif kind == 3:                             # a little more or less
        d[at] = (d[at] + rng.choice((-2, -1, 1, 2, 16, -16))) & 0xFF
    elif kind == 4 and len(d) > 1:              # a piece out
        n = rng.randrange(1, min(len(d), 16))
        del d[at:at + n]
    elif kind == 5:                             # bytes in
        n = rng.randrange(1, 9)
        d[at:at] = bytes(rng.randrange(256) for _ in range(n))
    elif kind == 6:                             # a piece twice
        n = rng.randrange(1, min(len(d), 32) + 1)
        d[at:at] = d[at:at + n]
    elif kind == 7 and other:                   # joined with another
        cut = rng.randrange(len(other))
        d[at:] = other[cut:]
    elif kind == 8 and other:                   # a piece of another in
        a = rng.randrange(len(other))
        d[at:at] = other[a:a + rng.randrange(1, 33)]
    elif kind == 9 and len(d) >= 2:             # a 16-bit number
        v = rng.choice(WORDS) & 0xFFFF
        at = rng.randrange(len(d) - 1)
        d[at:at + 2] = v.to_bytes(2, rng.choice(("little", "big")))
    elif kind == 10 and len(d) >= 4:            # a 32-bit number
        v = rng.choice(WORDS)
        at = rng.randrange(len(d) - 3)
        d[at:at + 4] = v.to_bytes(4, rng.choice(("little", "big")))
    else:                                       # a run of one byte
        n = rng.randrange(1, 9)
        d[at:at + n] = bytes([rng.choice(INTERESTING)]) * n
    return bytes(d[:most])


def mutate_pulses(p, rng, other=(), most=600):
    """One change to a signal: durations in µs, a high one and a low one in
    turn"""
    p = list(p) or [500, 500]
    kind = rng.randrange(11)
    at = rng.randrange(len(p))
    if kind == 0:                               # one a little off
        p[at] = int(p[at] * rng.uniform(0.6, 1.6))
    elif kind == 1:                             # one another multiple
        base = min(p) if min(p) > 50 else 300
        p[at] = base * rng.choice((1, 2, 3, 4, 6, 8, 10, 16, 30, 40))
    elif kind == 2 and len(p) > 1:              # a bit: a pair turned round
        at = rng.randrange(len(p) - 1)
        p[at], p[at + 1] = p[at + 1], p[at]
    elif kind == 3:                             # all of it faster or slower
        f = rng.uniform(0.5, 2.0)
        p = [int(x * f) for x in p]
    elif kind == 4 and len(p) > 4:              # a piece out (pairs)
        n = 2 * rng.randrange(1, 9)
        at &= ~1
        del p[at:at + n]
    elif kind == 5:                             # a piece twice
        n = 2 * rng.randrange(1, 17)
        at &= ~1
        p[at:at] = p[at:at + n]
    elif kind == 6 and other:                   # joined with another
        cut = rng.randrange(len(other)) & ~1
        p[at & ~1:] = other[cut:]
    elif kind == 7:                             # a long gap
        p[at | 1 if (at | 1) < len(p) else at] = rng.choice(
            (3000, 5000, 9000, 12000, 20000, 40000))
    elif kind == 8:                             # shorter
        p = p[:max(2, rng.randrange(len(p)) & ~1)]
    elif kind == 9:                             # jitter everywhere
        p = [int(x * rng.uniform(0.9, 1.1)) for x in p]
    else:                                       # several bits
        for _ in range(rng.randrange(2, 6)):
            a = rng.randrange(len(p) - 1) if len(p) > 1 else 0
            if len(p) > 1:
                p[a], p[a + 1] = p[a + 1], p[a]
    return [max(1, min(int(x), 200000)) for x in p[:most]] or [500, 500]


# ------------------------------------------------------------------- targets

class Target:
    """How an input gets to the firmware.  An input is bytes; a signal's is
    its durations as text."""
    name = ext = ""
    alone = False                       # it wants no app beside it

    def __init__(self, env, opts):
        self.env, self.opts = env, opts
        self.cli = None

    def seeds(self):
        return [b"\0"]

    def mutate(self, data, rng, other):
        return mutate_bytes(data, rng, other, self.opts.get("max", 4096))

    def open(self):
        self.cli = self.env.open_cli()
        if self.alone:
            # an app that an input started before (the RPC's `app start`) is
            # closed: the CLI's commands for the radios do not run beside one
            self.env.close_app(self.cli)

    def close(self):
        if self.cli:
            try:
                self.cli.write(b"\x03")
                self.cli.read_until_prompt(3)
            except (OSError, TimeoutError):
                pass
            self.cli.close()
            self.cli = None

    def feed(self, data):
        raise NotImplementedError

    def drain(self):
        """What the firmware printed meanwhile is read and dropped"""
        try:
            self.cli._fill(0)
        except OSError as e:
            raise Lost(f"its serial port: {e}") from None
        self.cli.buf = b""


def pulses_of(data):
    try:
        return [abs(int(x)) for x in data.decode().replace(",", " ").split()
                if x.strip("+-")] or [500, 500]
    except ValueError:
        return [500, 500]


def pulses_text(p, signed):
    if signed:
        return " ".join(f"{'-' if i % 2 else ''}{x}" for i, x in enumerate(p))
    return " ".join(map(str, p))


class Pulses(Target):
    signed, most_us, alone = True, 400000, True

    def mutate(self, data, rng, other):
        p = mutate_pulses(pulses_of(data), rng, pulses_of(other) if other
                          else (), self.opts.get("max", 600))
        while sum(p) > self.most_us and len(p) > 2:
            p = p[:len(p) // 2 & ~1 or 2]
        return pulses_text(p, self.signed).encode()

    def listen(self):
        raise NotImplementedError

    def play(self, p):
        raise NotImplementedError

    def open(self):
        super().open()
        self.listen()

    def feed(self, data):
        p = pulses_of(data)
        err = self.play(p)
        if err and err.strip():
            raise Lost(f"the emulator did not take the signal ({err.strip()})")
        # the signal takes its own time on the air, and the decoder a moment
        time.sleep(sum(p) / 1e6 + 0.012)
        self.drain()


def pwm(te, bits, value, one=(3, 1), zero=(1, 3), sync=(1, 31), repeats=2):
    out = []
    for _ in range(repeats):
        for i in range(bits - 1, -1, -1):
            h, lo = one if value >> i & 1 else zero
            out += [h * te, lo * te]
        out += [sync[0] * te, sync[1] * te]
    return out


class SubGhz(Pulses):
    name, ext, signed = "subghz", ".txt", True

    def listen(self):
        freq = self.opts.get("frequency", 433920000)
        self.freq = freq
        self.cli.write(f"subghz rx {freq} 0\r".encode())
        end, said = time.time() + 5, b""
        while time.time() < end and b"istening" not in said:
            self.cli._fill(0.1)
            said = self.cli.buf
        if b"failed to start" in said:
            raise Lost("this firmware's `subghz rx` does not start the "
                       "internal radio from the CLI (\"Device 0 failed to "
                       "start\": Unleashed's and RogueMaster's do not): its "
                       "Sub-GHz decoders are not reached this way")
        if b"istening" not in said:
            raise Lost("`subghz rx` did not start: "
                       + said.decode(errors="replace")[-200:])
        self.cli.buf = b""

    def play(self, p):
        return self.env.mon(f"qom-set /machine/subghz rx @{self.freq},"
                            + ",".join(f"{'-' if i % 2 else '+'}{x}"
                                       for i, x in enumerate(p)))

    def seeds(self):
        out = [pwm(390, 24, 0x95D5D4, sync=(1, 30), repeats=3),     # Princeton
               pwm(320, 12, 0xA5C, one=(2, 1), zero=(1, 2), sync=(1, 36)),
               pwm(700, 12, 0x5A3, one=(2, 1), zero=(1, 2), sync=(1, 36)),
               pwm(400, 64, 0x5AC3F00D12345678, one=(1, 2), zero=(2, 1),
                   sync=(1, 40), repeats=1),
               pwm(500, 32, 0xDEADBEEF, one=(1, 3), zero=(3, 1), sync=(1, 20)),
               pwm(1000, 16, 0xC0DE, sync=(1, 10)),
               pwm(250, 40, 0x123456789A, one=(1, 1), zero=(2, 2), sync=(4, 16))]
        # a preamble of short pulses, a header gap and a body (KeeLoq's shape)
        out.append([400, 400] * 11 + [400, 4000]
                   + pwm(400, 66, 0x2A5C3F00D1234567F, one=(1, 2), zero=(2, 1),
                         sync=(1, 40), repeats=1))
        return [pulses_text(p, True).encode() for p in out]


class Ir(Pulses):
    name, ext, signed = "ir", ".txt", False

    def listen(self):
        self.cli.write(b"ir rx\r")
        end, said = time.time() + 5, b""
        while time.time() < end and b"eceiving" not in said:
            self.cli._fill(0.1)
            said = self.cli.buf
        if b"eceiving" not in said:
            raise Lost("`ir rx` did not start: "
                       + said.decode(errors="replace")[-200:])
        self.cli.buf = b""

    def play(self, p):
        return self.env.mon("qom-set /machine/ir rx " + ",".join(map(str, p)))

    def seeds(self):
        import fe_ir
        out = []
        for proto in fe_ir.SPECS:
            try:
                out.append(fe_ir.encode(proto, 0x04, 0x08))
            except (ValueError, KeyError):
                pass
        return [pulses_text(p, False).encode() for p in out] or [b"9000 4500"]


def varint(n):
    out = bytearray()
    while True:
        out.append(n & 0x7F | (0x80 if n > 0x7F else 0))
        n >>= 7
        if not n:
            return bytes(out)


def frames(stream):
    """An RPC stream's messages (each behind its length): [bodies], or None
    when it is not that"""
    out, at = [], 0
    while at < len(stream):
        n = shift = 0
        while True:
            if at >= len(stream) or shift > 28:
                return None
            b = stream[at]
            at += 1
            n |= (b & 0x7F) << shift
            shift += 7
            if not b & 0x80:
                break
        if at + n > len(stream):
            return None
        out.append(stream[at:at + n])
        at += n
    return out


def pb(number, value):
    """A protobuf field: a number as a varint, bytes with their length"""
    if isinstance(value, int):
        return varint(number << 3) + varint(value)
    return varint(number << 3 | 2) + varint(len(value)) + value


class Rpc(Target):
    """PB.Main messages (flipper.proto): command_id 1, has_next 3, and one
    request among its fields.  Mostly a message's body is changed and its
    length kept right, so that the change gets past the framing."""
    name, ext, alone = "rpc", ".bin", True

    def seeds(self):
        def main(n, field, body=b""):
            m = pb(1, n) + pb(field, body)
            return varint(len(m)) + m
        path = pb(1, b"/ext")
        return [main(1, 5, pb(1, b"ping")),                 # system ping
                main(2, 32),                                # device info
                main(3, 7, path),                           # storage list
                main(4, 24, pb(1, b"/ext/Manifest")),       # storage stat
                main(5, 28, pb(1, b"/ext")),                # storage info
                main(6, 61, pb(1, b"devinfo")),             # property get
                main(7, 39),                                # protobuf version
                main(8, 9, pb(1, b"/ext/Manifest")),        # storage read
                main(9, 47),                                # app lock status
                main(10, 16, pb(1, b"Clock") + pb(2, b"")),  # app start
                main(11, 47) + main(12, 5, pb(1, b"x"))]

    def mutate(self, data, rng, other):
        most = self.opts.get("max", 1024)
        parts = frames(data)
        if parts is None or not parts or rng.random() < 0.12:
            return mutate_bytes(data, rng, other, most)     # the framing too
        kind = rng.randrange(10)
        at = rng.randrange(len(parts))
        theirs = frames(other) if other else None
        if kind == 0 and len(parts) > 1:
            del parts[at]
        elif kind == 1:
            parts.insert(at, parts[at])
        elif kind == 2 and theirs:
            parts.insert(at, rng.choice(theirs))
        elif kind == 3:                         # another request's number
            body = bytearray(parts[at])
            where = [i for i in range(len(body) - 1)
                     if body[i] & 7 == 2 and body[i] > 0x10]
            if where:
                i = rng.choice(where)
                body[i] = rng.randrange(4, 127) << 3 & 0xFF | 2
                if body[i] & 0x80:              # (a field past 15: two bytes)
                    body[i:i + 1] = varint(rng.randrange(16, 80) << 3 | 2)
            parts[at] = bytes(body)
        else:
            parts[at] = mutate_bytes(parts[at], rng,
                                     rng.choice(theirs) if theirs else b"", most)
        return b"".join(varint(len(p)) + p for p in parts)[:most]

    def open(self):
        super().open()
        self.session = False
        self.stuck = getattr(self, "stuck", 0)

    def start(self):
        self.cli.write(b"start_rpc_session\r")
        end = time.time() + 3
        while time.time() < end:
            self.cli._fill(0.05)
            if b"\n" in self.cli.buf:
                break
        self.cli.buf = b""
        self.session = True

    def feed(self, data):
        if not self.session:
            self.start()
        try:
            self.cli.write(data)
        except OSError as e:
            raise Lost(f"its serial port: {e}") from None
        answered = self.listen(0.6, 0.03)
        if b">: " in self.cli.buf:              # the session ended: its prompt
            self.session = False
        self.cli.buf = b""
        if not answered and self.session:
            # Nothing came back.  An input may have begun a message that is
            # longer than itself (a length of megabytes): the session then
            # takes the next inputs for the rest of it.  A ping tells: with
            # no answer to it either, the port is closed and opened, which
            # ends the session, and the next input begins a new one.
            m = pb(1, 0x7E57) + pb(5, pb(1, b"fe-sync"))
            self.cli.write(varint(len(m)) + m)
            self.listen(0.4, 0.4, until=b"fe-sync")
            synced = b"fe-sync" in self.cli.buf
            self.cli.buf = b""
            if not synced:
                self.stuck += 1
                self.cli.close()
                self.cli = self.env.open_cli()
                self.session = False

    def listen(self, most, quiet_for, until=None):
        """Its answers, until it is quiet (or `until` came, or `most`
        seconds passed).  -> whether anything came"""
        end, quiet = time.time() + most, time.time() + quiet_for
        while time.time() < min(end, quiet):
            before = len(self.cli.buf)
            try:
                self.cli._fill(0.01)
            except OSError as e:
                raise Lost(f"its serial port: {e}") from None
            if len(self.cli.buf) != before:
                quiet = time.time() + quiet_for
                if until and until in self.cli.buf:
                    break
        return bool(self.cli.buf)

    def close(self):
        if self.cli:
            # the session ends with its port's close; at the CLI again, an
            # app that an input started is closed
            self.cli.close()
            self.cli = None
            try:
                cli = self.env.open_cli()
            except (SystemExit, OSError):
                return
            self.env.close_app(cli)
            cli.close()


class Cli(Target):
    """A CLI command with the input in it: `--command 'loader signal 100
    {hex}'`, `--command 'subghz decode_raw {file}' --ext .sub`"""
    name = "cli"

    def __init__(self, env, opts):
        super().__init__(env, opts)
        self.command = opts.get("command") or ""
        self.ext = opts.get("ext") or ".bin"
        self.path = f"/ext/fuzz-input{self.ext}"
        self.timeout = opts.get("timeout", 5.0)
        if not any(k in self.command for k in ("{hex}", "{text}", "{file}")):
            raise ValueError("--command needs the input's place in it: {hex}, "
                             "{text} or {file}")

    def seeds(self):
        return [b"\0\0\0\0", b"FUZZ", b"1234", b"\xff\xff\xff\xff"]

    def write_file(self, data):
        self.cli.run(f"storage remove {self.path}", timeout=self.timeout)
        if not data:
            data = b"\n"
        self.cli.write(f"storage write_chunk {self.path} {len(data)}\r".encode())
        end = time.time() + self.timeout
        while time.time() < end and b"Ready" not in self.cli.buf:
            self.cli._fill(0.05)
        if b"Ready" not in self.cli.buf:
            raise Lost("`storage write_chunk` did not ask for the bytes")
        self.cli.buf = b""
        self.cli.write(data)
        self.cli.read_until_prompt(self.timeout)

    def feed(self, data):
        text = "".join(chr(b) if 0x21 <= b < 0x7F and chr(b) not in "\"'\\"
                       else "." for b in data) or "."
        try:
            if "{file}" in self.command:
                self.write_file(data)
            self.cli.run(self.command.replace("{hex}", data.hex() or "00")
                         .replace("{text}", text).replace("{file}", self.path),
                         timeout=self.timeout)
        except TimeoutError:
            raise Lost(f"the CLI was not done with the command after "
                       f"{self.timeout:g} s") from None
        except OSError as e:
            raise Lost(f"its serial port: {e}") from None


class Nfc(Target):
    """A card whose contents the fuzzer made, held to the Flipper while the
    firmware's NFC shell reads it (`dump`).  An input is a card as the
    emulator's card model takes one (scripts/fe_nfc.py: a line for each
    value, its bytes in hex): what a card says of itself is the fuzzer's,
    and the card stays one that answers by its protocol's rules.  A card
    that the model does not take (a UID of five bytes) is no input, and
    costs nothing."""
    name = "nfc"
    ext = ".card"
    alone = True
    PROMPT = r"\[nfc\]>: "
    FIXED = ("type", "app", "blocksize", "slix", "ulc", "privacy",
             "accept-all")              # (what kind of card it is: kept)

    def __init__(self, env, opts):
        super().__init__(env, opts)
        self.timeout = opts.get("timeout") or 40.0
        self.path = pathlib.Path(env.dir) / "fuzz-card.txt"
        self.shell = False

    def seeds(self):
        import fe_nfc
        made = [("ntag215", "04A1B2C3D4E5F6"), ("mfc1k", "11223344")]
        out = []
        for kind, uid in made:
            try:
                out.append((fe_nfc.blank(kind, bytes.fromhex(uid),
                                         "https://flipperzero.one")
                            if kind == "ntag215" else
                            fe_nfc.blank_mfc(kind[3:], bytes.fromhex(uid))
                            ).encode())
            except (ValueError, KeyError):
                pass
        uids = {"felica": "012E4CD21A8B3C5F", "felica-standard": "0114B3C6D2F0A957",
                "slix": "E004010812345678", "st25tb": "D0021C0123456789",
                "iso14443b": "1A2B3C4D"}
        for kind, (make, _) in fe_nfc.SAMPLES.items():
            uid = bytes.fromhex(uids.get(kind, "04A1B2C3D4E5F6"))
            out.append(fe_nfc.describe(fe_nfc.parse_nfc_text(make(uid))).encode())
        return out

    def mutate(self, data, rng, other):
        lines = data.decode(errors="replace").splitlines()
        spots = [i for i, line in enumerate(lines)
                 if line and not line.startswith("#")
                 and line.split()[0] not in self.FIXED and len(line.split()) > 1]
        if not spots:
            return data
        for _ in range(rng.choice((1, 1, 1, 2, 3))):
            i = rng.choice(spots)
            key, *words = lines[i].split()
            try:
                value = bytes.fromhex("".join(words))
            except ValueError:
                continue
            keep = 1 if key == "rec" else 0     # (a record's kind stays)
            theirs = b""
            if other:
                # a value of another card's, of the same key if it has one
                same = [bytes.fromhex("".join(line.split()[1:]))
                        for line in other.decode(errors="replace").splitlines()
                        if line.split()[:1] == [key]
                        and all(c in "0123456789abcdefABCDEF"
                                for c in "".join(line.split()[1:]))]
                theirs = rng.choice(same)[keep:] if same else b""
            new = value[:keep] + mutate_bytes(value[keep:], rng, theirs,
                                             max(len(value) + 16, 64))
            lines[i] = f"{key} {new.hex(' ')}".rstrip()
        return ("\n".join(lines) + "\n").encode()

    FILE = "/ext/nfc/fuzz.nfc"          # (the dump's: removed after each)

    def enter(self):
        found, _ = self.cli.watch("nfc", self.PROMPT, timeout=15, stop=False)
        self.shell = bool(found)
        return self.shell

    def leave(self):
        """Out of the NFC shell, to the CLI's own prompt"""
        if self.shell:
            self.cli.watch("exit", r">: ", timeout=5, stop=False)
            self.shell = False

    def open(self):
        super().open()
        self.cli.run(f"storage remove {self.FILE}", timeout=10)
        if not self.enter():
            raise ValueError("this firmware's CLI has no `nfc` shell with a "
                             "`dump`: no reader to hold a card to")

    def close(self):
        try:
            self.env.mon("qom-set /machine/nfc card none")
        except (OSError, ConnectionError):
            pass
        if self.cli and self.shell:
            try:
                self.cli.write(b"\x03")
                self.leave()
            except (OSError, TimeoutError):
                pass
            self.shell = False
        super().close()

    def feed(self, data):
        try:
            self.path.write_bytes(data)
            if self.env.mon(f"qom-set /machine/nfc card file:{self.path}").strip():
                return                  # (no card: the model refused it)
            found, _ = self.cli.watch(f"dump -f {self.FILE}", self.PROMPT,
                                      timeout=self.timeout, stop=False)
            self.env.mon("qom-set /machine/nfc card none")
            if found:
                # (the shell writes no file over one that is there)
                self.leave()
                self.cli.run(f"storage remove {self.FILE}", timeout=10)
                found = self.enter()
        except (OSError, TimeoutError) as e:
            raise Lost(f"its serial port: {e}") from None
        if not found:
            raise Lost("the NFC shell was not done with `dump` after "
                       f"{self.timeout:g} s")


TARGETS = {t.name: t for t in (SubGhz, Ir, Rpc, Cli, Nfc)}


# -------------------------------------------------------------------- engine

class Fuzzer:
    """env: what the launcher gives it of a device (open_cli(), mon(cmd),
    news(forget=False) -> blocks new (forget: counted from now on) or
    None without coverage, log_new() -> what the
    firmware logged since it was last asked, as bytes; halted() -> why or
    None, recover() -> whether the firmware is up again, say(text),
    fault() -> (the CPU's faults so far, the last one in words, its place in
    a word) or None, where(address) -> a function's name or "",
    close_app(cli): whatever app runs is closed, with its Back button if it
    takes no request)."""

    def __init__(self, target, env, out, seed=None, keep_recent=8):
        self.target, self.env, self.out = target, env, out
        self.seed = seed if seed is not None else int.from_bytes(
            os.urandom(4), "little")
        self.rng = random.Random(self.seed)
        self.corpus, self.recent, self.keep_recent = [], [], keep_recent
        self.runs = self.crashes = self.hangs = self.restarts = 0
        self.kinds = {}                 # a crash's first line -> how often
        self.guided = True
        self.stop_at_first = False
        env.log_new()                   # (what it logged before is not ours)
        self.faults = (env.fault() or (0,))[0]
        (out / "corpus").mkdir(parents=True, exist_ok=True)
        (out / "crashes").mkdir(exist_ok=True)

    def keep(self, data):
        if data in self.corpus:
            return
        self.corpus.append(data)
        (self.out / "corpus" / f"{len(self.corpus):05d}{self.target.ext}"
         ).write_bytes(data)

    def crashed(self, lost=None, last=False):
        """What went wrong after the last input: (kind, its words), or None"""
        new = self.env.log_new()
        hit = CRASH.search(new)
        if hit and not new[hit.start():].count(b"\n") and not lost:
            time.sleep(0.2)                     # (the rest of its report)
            new += self.env.log_new()
        if hit:
            report = new[hit.start():hit.start() + 1500]
            return "crash", report.decode(errors="replace")
        # (a firmware that halted or locked up logs nothing: asked when an
        # input was not taken, and now and then)
        if lost or last or self.runs % 25 == 0:
            why = self.env.halted()
            if why:
                return "halt", why
        if lost:
            return "hang", str(lost)
        return None

    def place(self, words):
        """Where a crash was: (in a word, in a sentence).  A fault of the
        CPU's: from the emulator's note of it (its caller: the fault itself
        is often in a library's function).  A crash the firmware called for
        itself: from the LR its report has."""
        fault = self.env.fault()
        if fault and fault[0] != self.faults:
            self.faults = fault[0]
            return fault[2], "the CPU's fault: " + fault[1]
        lr = re.search(r"\blr : ([0-9a-f]{6,8})\b", words)
        name = self.env.where(int(lr.group(1), 16)) if lr else ""
        if name:
            return name, f"it was called for from {name} (the report's lr)"
        return (lr.group(1) if lr else ""), ""

    def record(self, kind, words):
        first = re.sub(r"\x1b\[[0-9;]*m", "", words).strip().splitlines()
        first = first[0][:160] if first else kind
        short, long = self.place(words) if kind != "hang" else ("", "")
        if short:
            first += f" (from {short})"
        if long:
            words = long + "\n\n" + words
        seen = self.kinds.get(first, 0)
        self.kinds[first] = seen + 1
        if kind == "hang":
            self.hangs += 1
        else:
            self.crashes += 1
        if seen < 3:                            # (three of a kind are kept)
            n = len(list((self.out / "crashes").iterdir())) + 1
            folder = self.out / "crashes" / f"{n:03d}-{kind}"
            folder.mkdir()
            for i, data in enumerate(self.recent):
                last = i == len(self.recent) - 1
                (folder / (f"input{self.target.ext}" if last else
                           f"before-{len(self.recent) - 1 - i}"
                           f"{self.target.ext}")).write_bytes(data)
            (folder / "report.txt").write_text(
                f"{kind}: {first}\n\nafter input {self.runs} of this run "
                f"(target {self.target.name}, seed {self.seed}); the inputs "
                "before it are beside it, the last one first (before-1)\n\n"
                + words + "\n")
            self.env.say(f"{kind} after input {self.runs}: {first}  "
                         f"({folder})")
        return first

    def one(self, data):
        """An input to the firmware.  -> whether it is worth keeping"""
        self.runs += 1
        self.recent = (self.recent + [data])[-self.keep_recent:]
        lost = None
        try:
            self.target.feed(data)
        except Lost as e:
            lost = e
        bad = self.crashed(lost)
        if bad:
            self.record(*bad)
            self.target.close()
            if not self.env.recover():
                raise Lost("the firmware did not come back after it")
            self.restarts += 1
            self.env.log_new()
            self.target.open()
            self.env.news()                     # (its start is nothing new)
            return False
        new = self.env.news()
        if new is None:
            self.guided = False
            return False
        return new > 0

    def leave(self):
        """The target is closed, and the firmware looked at once more: what
        an input began may end badly a moment later, or when the app that it
        started is closed"""
        try:
            self.target.close()
        except (OSError, Lost):
            pass
        time.sleep(0.5)
        bad = self.crashed(last=True)
        if bad:
            self.record(bad[0], bad[1] + "\n\n(after the last input, when "
                        "the fuzzer was done and closed what it had open)")
            self.env.recover()

    def settle(self):
        """Until nothing new runs by itself (timers, the services' own
        work): what an input is to be told from"""
        if self.env.news(forget=True) is None:
            self.guided = False
            return
        quiet, end = 0, time.time() + 8
        while quiet < 3 and time.time() < end:
            time.sleep(0.3)
            quiet = quiet + 1 if not self.env.news() else 0

    def run(self, seconds=None, runs=None, inputs=None, every=5.0):
        """inputs: these alone, once each (a replay); else the seeds first
        and then changed ones, for `seconds` or `runs`"""
        self.target.open()
        self.settle()
        start = shown = time.time()
        for data in (inputs if inputs is not None else self.target.seeds()):
            if self.one(data) or not self.corpus:
                self.keep(data)
        if inputs is not None:
            self.leave()
            return
        last_new = time.time()
        while ((seconds is None or time.time() - start < seconds)
               and (runs is None or self.runs < runs)
               and not (self.stop_at_first and self.crashes + self.hangs)):
            # (the newer half of what is kept is taken twice as often)
            pick = self.corpus if self.rng.random() < 0.5 else \
                self.corpus[len(self.corpus) // 2:]
            data = self.rng.choice(pick)
            for _ in range(self.rng.choice((1, 1, 1, 2, 2, 3, 5))):
                data = self.target.mutate(data, self.rng,
                                          self.rng.choice(self.corpus))
            if self.one(data):
                self.keep(data)
                last_new = time.time()
            if time.time() - shown >= every:
                shown = time.time()
                self.env.say(self.words(start, last_new))
        self.leave()
        self.env.say(self.words(start, last_new))

    def words(self, start, last_new):
        took = max(time.time() - start, 0.001)
        return (f"{self.runs} inputs in {took:.0f} s ({self.runs / took:.0f} a "
                f"second), {len(self.corpus)} kept"
                + (f" (the last {time.time() - last_new:.0f} s ago)"
                   if self.guided else " (no coverage: none tells an input "
                   "that got somewhere new)")
                + f", {self.crashes} crashes"
                + (f" of {len(self.kinds)} kinds" if self.kinds else "")
                + (f", {self.hangs} hangs" if self.hangs else ""))
