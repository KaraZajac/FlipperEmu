"""The module's side of the Flipper's expansion protocol: what a board on the
GPIO header says to the firmware's expansion service, over a serial port.

A frame is a type, its contents and a checksum (the XOR of all before it):

    heartbeat  01               keeps a quiet link; the Flipper answers one
    status     02 ERROR         0 OK, 1 an error, 2 no such baud rate
    baud rate  03 RATE          four bytes, the low one first
    control    04 COMMAND       0 start RPC, 1 stop RPC, 2 and 3: 5 V on, off
    data       05 SIZE BYTES    up to 64 bytes of the RPC session's stream

The module calls by pulling the Flipper's RX pin low for a moment.  Here that
is one byte: the machine puts its start bit on the pin.  The Flipper answers
with a heartbeat at 9600 baud, the module names the baud rate it wants, the
Flipper says OK, and both change to it.  From then on a side that hears
nothing for 250 ms takes the link as gone, so a module with nothing to say
sends heartbeats.  Every frame is confirmed: with a status, or a heartbeat
with a heartbeat.

Written from the protocol's description, documentation/ExpansionModules.md in
the firmware's tree.

Copyright (c) 2026, Kara Zajac
SPDX-License-Identifier: BSD-3-Clause
"""
import time

HEARTBEAT, STATUS, BAUD_RATE, CONTROL, DATA = 1, 2, 3, 4, 5
NAMES = {HEARTBEAT: "heartbeat", STATUS: "status", BAUD_RATE: "baud rate",
         CONTROL: "control", DATA: "data"}
ERRORS = {0: "OK", 1: "an error", 2: "no such baud rate"}
START_RPC, STOP_RPC, OTG_ON, OTG_OFF = 0, 1, 2, 3
COMMANDS = {START_RPC: "start RPC", STOP_RPC: "stop RPC", OTG_ON: "5 V on",
            OTG_OFF: "5 V off"}
TIMEOUT = 0.25          # who hears nothing for so long gives the link up
DEAD_TIME = 0.025       # nothing is said for so long after the rate changes
DATA_MOST = 64          # bytes in a data frame
FIRST_RATE = 9600       # the rate of the call's answer
PERIOD = 0.08           # between our heartbeats: well under the timeout


def checksum(data):
    x = 0
    for byte in data:
        x ^= byte
    return x


def frame(kind, contents=b""):
    body = bytes([kind]) + bytes(contents)
    return body + bytes([checksum(body)])


def frame_size(head):
    """How long the frame is that begins with these bytes (None: the next
    byte says).  ValueError: no frame begins so."""
    kind = head[0]
    if kind == HEARTBEAT:
        return 2
    if kind in (STATUS, CONTROL):
        return 3
    if kind == BAUD_RATE:
        return 6
    if kind == DATA:
        if len(head) < 2:
            return None
        if head[1] > DATA_MOST:
            raise ValueError(f"a data frame of {head[1]} bytes (it has "
                             f"{DATA_MOST} at most)")
        return 3 + head[1]
    raise ValueError(f"no frame begins with {kind:02x}")


def in_words(data):
    """What a frame says: (the name of its kind, its contents in words)"""
    kind = data[0]
    if kind == STATUS:
        note = ERRORS.get(data[1], f"error {data[1]}")
    elif kind == BAUD_RATE:
        note = str(int.from_bytes(data[1:5], "little"))
    elif kind == CONTROL:
        note = COMMANDS.get(data[1], f"command {data[1]}")
    elif kind == DATA:
        note = f"{data[1]} byte{'' if data[1] == 1 else 's'}"
    else:
        note = ""
    return NAMES[kind], note


class Lost(Exception):
    """The link is gone, or never was: what happened, in words"""


class Module:
    """A module on a port.  `send(bytes)` puts bytes on the Flipper's RX pin,
    `receive(seconds)` is what the Flipper sent within that time (b"": none),
    and `said(who, bytes, note)` is told of every frame, ours ("module") and
    the Flipper's ("Flipper")."""

    def __init__(self, send, receive, said=None, patience=1.0):
        self.send, self.receive = send, receive
        self.said = said or (lambda who, data, note: None)
        # The Flipper answers within its own 250 ms.  An emulated one on a
        # busy computer may take longer and still be right, so that is waited
        # for, and Lost says how long it was.
        self.patience = patience
        self.buffer = b""       # what came, and is no whole frame yet
        self.stream = b""       # what the RPC session sent, not yet read
        self.spoke = 0.0        # when we last sent a frame
        self.beats = 0          # heartbeats of ours that were answered
        self.quiet = False      # heartbeats are not told to `said`

    def _send(self, data, note=""):
        self.send(data)
        self.spoke = time.monotonic()
        if not (self.quiet and data[0] == HEARTBEAT):
            self.said("module", data, note)

    def _frame(self, seconds, must=True):
        """The Flipper's next frame.  When none comes in that time: Lost, or
        None if it need not come (a frame that has begun is waited for)."""
        end = time.monotonic() + seconds
        while True:
            if self.buffer:
                try:
                    size = frame_size(self.buffer)
                except ValueError as e:
                    raise Lost(f"the Flipper sent what is no frame: {e} "
                               f"({self.buffer[:8].hex(' ')})") from None
                if size and len(self.buffer) >= size:
                    data, self.buffer = self.buffer[:size], self.buffer[size:]
                    if checksum(data[:-1]) != data[-1]:
                        raise Lost("a frame of the Flipper's has a wrong "
                                   f"checksum ({data.hex(' ')})")
                    if not (self.quiet and data[0] == HEARTBEAT):
                        self.said("Flipper", data, "")
                    return data
            left = end - time.monotonic()
            if left <= 0:
                if not must and not self.buffer:
                    return None
                raise Lost(f"nothing came from the Flipper in {seconds:g} s"
                           + (f" after the start of a frame "
                              f"({self.buffer.hex(' ')})" if self.buffer
                              else ""))
            self.buffer += self.receive(min(left, 0.05))

    def _take(self, data):
        """A data frame of the Flipper's: the RPC session's, and confirmed"""
        self.stream += data[2:-1]
        self._send(frame(STATUS, [0]))

    def _expect(self, kind):
        """The Flipper's answer, a frame of that kind.  What its RPC session
        sends before it is taken."""
        while True:
            data = self._frame(self.patience)
            if data[0] == DATA:
                self._take(data)
            elif data[0] == kind:
                return data
            else:
                raise Lost(f"the Flipper sent a {NAMES[data[0]]} frame where "
                           f"a {NAMES[kind]} frame was due "
                           f"({data.hex(' ')})")

    def _confirmed(self, what):
        error = self._expect(STATUS)[1]
        if error:
            raise Lost(f"the Flipper refused {what}: "
                       + ERRORS.get(error, f"error {error}"))

    def call(self):
        """Call the Flipper, and have its heartbeat.  On the port of its log
        the end of a log line may come first."""
        self.send(b"\x00")
        self.spoke = time.monotonic()
        self.said("module", b"", "the call")
        seen = b""
        end = self.spoke + self.patience
        beat = frame(HEARTBEAT)
        while True:
            at = seen.find(beat)
            if at >= 0:
                self.buffer = seen[at + len(beat):]
                self.said("Flipper", beat, "")
                return
            left = end - time.monotonic()
            if left <= 0:
                raise Lost("the Flipper did not answer the call in "
                           f"{self.patience:g} s")
            seen += self.receive(min(left, 0.05))

    def baud(self, rate):
        """Name the baud rate: the first thing a module says, also when it
        stays at 9600.  The Flipper confirms at the old rate, and nobody
        speaks for a moment after."""
        self._send(frame(BAUD_RATE, rate.to_bytes(4, "little")))
        error = self._expect(STATUS)[1]
        if error == 2:
            raise Lost(f"the Flipper does not do {rate} baud")
        if error:
            raise Lost(f"the Flipper refused {rate} baud: "
                       + ERRORS.get(error, f"error {error}"))
        time.sleep(DEAD_TIME + 0.005)

    def beat(self):
        self._send(frame(HEARTBEAT))
        self._expect(HEARTBEAT)
        self.beats += 1

    def control(self, command):
        self._send(frame(CONTROL, [command]))
        self._confirmed(f"to {COMMANDS.get(command, command)}")

    def write(self, data):
        """Bytes for the RPC session, in data frames"""
        for at in range(0, len(data), DATA_MOST):
            piece = data[at:at + DATA_MOST]
            self._send(frame(DATA, bytes([len(piece)]) + piece))
            self._confirmed("a data frame")

    def keep(self, seconds, until=None):
        """Keep the link for so long (or until `until()` is true): a
        heartbeat whenever nothing else was said for a while.  What the RPC
        session sends meanwhile is taken.  -> whether `until()` was true"""
        end = time.monotonic() + seconds
        while True:
            if until and until():
                return True
            now = time.monotonic()
            if now >= end:
                return False
            wait = min(end, self.spoke + PERIOD) - now
            if wait <= 0:
                self.beat()
                continue
            data = self._frame(wait, must=False)
            if data is None:
                continue
            if data[0] != DATA:
                raise Lost(f"the Flipper sent a {NAMES[data[0]]} frame that "
                           f"nothing asked for ({data.hex(' ')})")
            self._take(data)


# The RPC session's stream is protobuf messages (PB.Main of the firmware's
# flipper.proto), each behind its length.  A ping is enough of it to see that
# the session is there: its bytes come back.

def varint(n):
    out = bytearray()
    while True:
        out.append(n & 0x7f | (0x80 if n > 0x7f else 0))
        n >>= 7
        if not n:
            return bytes(out)


def read_varint(data, at=0):
    """(the number, where the next thing begins); None: not whole yet"""
    n = shift = 0
    while at < len(data):
        n |= (data[at] & 0x7f) << shift
        at += 1
        if not data[at - 1] & 0x80:
            return n, at
        shift += 7
    return None


def fields(data):
    """A protobuf message's fields: {number: value} (a number, or bytes)"""
    out, at = {}, 0
    while at < len(data):
        key, at = read_varint(data, at)
        number, wire = key >> 3, key & 7
        if wire == 0:
            out[number], at = read_varint(data, at)
        elif wire == 2:
            size, at = read_varint(data, at)
            out[number], at = data[at:at + size], at + size
        elif wire in (1, 5):
            size = 8 if wire == 1 else 4
            out[number], at = data[at:at + size], at + size
        else:
            raise ValueError(f"wire type {wire}")
    return out


def ping(command, data):
    """An RPC ping (PB.Main: command_id 1, system_ping_request 5)"""
    request = b"\x0a" + varint(len(data)) + data
    main = b"\x08" + varint(command) + b"\x2a" + varint(len(request)) + request
    return varint(len(main)) + main


def message(stream):
    """The first whole message of the RPC session's stream: (its fields,
    what follows it); None: it is not whole yet"""
    head = read_varint(stream)
    if not head or len(stream) < head[1] + head[0]:
        return None
    size, at = head
    return fields(stream[at:at + size]), stream[at + size:]


def pong(main):
    """What a ping's answer has: (command_id, command_status, the bytes of
    system_ping_response 6; None: it is no such answer)"""
    answer = main.get(6)
    return (main.get(1, 0), main.get(2, 0),
            fields(answer).get(1, b"") if answer is not None else None)
