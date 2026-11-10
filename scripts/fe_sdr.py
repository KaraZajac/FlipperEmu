#!/usr/bin/env python3
# Copyright (c) 2026, Kara Zajac
# SPDX-License-Identifier: BSD-3-Clause
"""A real radio as the emulated Flipper's Sub-GHz antenna.

    ./flipperemu subghz --sdr SOURCE [options]     (a running device)
    fe_sdr.py devices/NAME/subghz.sock SOURCE [options]

SOURCE is where the IQ samples come from:

    rtl_tcp[:HOST[:PORT]]      an RTL-SDR served by `rtl_tcp` (librtlsdr's
                               server; default 127.0.0.1:1234). It can run on
                               another machine.
    hackrf[:SERIAL]            a HackRF One, through libhackrf
    FILE.cu8|.cs8|.cs16|.cf32  an IQ recording, played in real time. rtl_sdr
                               writes cu8, hackrf_transfer cs8. --center and
                               --rate say how it was made, unless its name
                               does (rtl_433's style: name_433.92M_250k.cu8).
    -                          IQ on stdin (--format, --center, --rate), from
                               any SDR's own tool

The bridge does what the CC1101 would do with that antenna. The emulated
chip reports every change of its receive set-up (state, frequency,
modulation, bandwidth, data rate, deviation). The bridge filters that
channel out of the SDR's band at the CC1101's bandwidth, demodulates it
(OOK, or 2-FSK) with an adaptive slicer behind a squelch, and streams the
data to GDO0, where the firmware's drivers capture it as from the real
chip. When the firmware stays tuned outside the band, the SDR follows.
Every 50 ms the band's power goes too: the CC1101's RSSI is read from it at
whatever frequency and bandwidth the firmware asks, so the Frequency
Analyzer, the RSSI bars and frequency hopping see the real band.

The protocol on the link socket is in docs/RADIO_LINK.md.
"""
import argparse
import ctypes
import ctypes.util
import math
import pathlib
import queue
import re
import select
import socket
import struct
import sys
import time

np = None                       # numpy: loaded when a bridge runs (load_numpy),
                                # so the launcher can take these options without it

SPEC_EVERY_S = 0.05             # the band's power, this often
FOLLOW_AFTER_S = 0.5            # tuned off the band this long: retune
EDGE_FRACTION = 0.45            # of the sample rate either side: usable band


def load_numpy():
    global np
    if np is None:
        try:
            import numpy
        except ImportError:
            sys.exit("fe_sdr needs numpy (dnf install python3-numpy, or pip install numpy)")
        np = numpy


def parse_hz(text):
    """433.92M, 2.048M, 250k, 433920000 -> Hz."""
    m = re.fullmatch(r"\s*([0-9.]+)\s*([kKmMgG]?)(?:[hH][zZ])?\s*", str(text))
    if not m:
        raise ValueError(f"not a frequency: {text!r}")
    return float(m.group(1)) * {"": 1, "k": 1e3, "m": 1e6, "g": 1e9}[m.group(2).lower()]


# ---------------------------------------------------------------- sources

class Source:
    """IQ samples from a radio or a file: complex64 blocks at .rate, centred
    on .center.  .skip counts samples still from before a retune."""
    tunable = False
    name = "?"
    skip = 0

    def tune(self, center):
        return False

    def close(self):
        pass


def _recv_exact(sock, n):
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("the SDR server closed the connection")
        buf += chunk
    return bytes(buf)


class RtlTcp(Source):
    """An RTL-SDR served by rtl_tcp: a 12-byte header ("RTL0", the tuner, its
    gain count), then unsigned 8-bit I/Q; commands are a byte and a
    big-endian 32-bit value."""
    tunable = True
    TUNERS = {1: "E4000", 2: "FC0012", 3: "FC0013", 4: "FC2580", 5: "R820T",
              6: "R828D"}

    def __init__(self, host, port, rate, center, gain, ppm):
        self.sock = socket.create_connection((host, port), timeout=5)
        magic, tuner, _ = struct.unpack(">4sII", _recv_exact(self.sock, 12))
        if magic != b"RTL0":
            raise ConnectionError(f"{host}:{port} is not an rtl_tcp server")
        self.name = (f"RTL-SDR ({self.TUNERS.get(tuner, f'tuner {tuner}')}) "
                     f"via rtl_tcp {host}:{port}")
        self.rate = rate
        self._cmd(0x02, int(rate))
        if ppm:
            self._cmd(0x05, int(ppm))
        if gain is None:
            self._cmd(0x03, 0)                  # the tuner's AGC
        else:
            self._cmd(0x03, 1)
            self._cmd(0x04, int(round(gain * 10)))
        self.tune(center)
        self.lut = ((np.arange(256) - 127.4) / 128).astype(np.float32)
        self.pending = b""

    def _cmd(self, cmd, value):
        self.sock.sendall(struct.pack(">BI", cmd, value & 0xFFFFFFFF))

    def tune(self, center):
        self._cmd(0x01, int(center))
        self.center = center
        self.skip = int(self.rate * 0.2)        # what rtl_tcp had buffered
        return True

    def read(self, n):
        want = 2 * n
        buf = bytearray(self.pending)
        while len(buf) < want:
            chunk = self.sock.recv(max(262144, want - len(buf)))
            if not chunk:
                return None
            buf += chunk
        self.pending = bytes(buf[want:])
        a = np.frombuffer(bytes(buf[:want]), np.uint8)
        return (self.lut[a[0::2]] + 1j * self.lut[a[1::2]]).astype(np.complex64)

    def close(self):
        self.sock.close()


class HackRF(Source):
    """A HackRF One through libhackrf: signed 8-bit I/Q from its RX callback."""
    tunable = True

    class _Transfer(ctypes.Structure):
        _fields_ = [("device", ctypes.c_void_p),
                    ("buffer", ctypes.POINTER(ctypes.c_uint8)),
                    ("buffer_length", ctypes.c_int),
                    ("valid_length", ctypes.c_int),
                    ("rx_ctx", ctypes.c_void_p),
                    ("tx_ctx", ctypes.c_void_p)]

    def __init__(self, serial, rate, center, lna, vga, amp):
        path = ctypes.util.find_library("hackrf") or "libhackrf.so.0"
        try:
            self.lib = lib = ctypes.CDLL(path)
        except OSError as e:
            raise ConnectionError(f"no libhackrf ({e})") from None
        lib.hackrf_error_name.restype = ctypes.c_char_p
        lib.hackrf_open.argtypes = [ctypes.POINTER(ctypes.c_void_p)]
        lib.hackrf_open_by_serial.argtypes = [ctypes.c_char_p,
                                              ctypes.POINTER(ctypes.c_void_p)]
        lib.hackrf_set_freq.argtypes = [ctypes.c_void_p, ctypes.c_uint64]
        lib.hackrf_set_sample_rate.argtypes = [ctypes.c_void_p, ctypes.c_double]
        lib.hackrf_set_lna_gain.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        lib.hackrf_set_vga_gain.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        lib.hackrf_set_amp_enable.argtypes = [ctypes.c_void_p, ctypes.c_uint8]
        lib.hackrf_set_baseband_filter_bandwidth.argtypes = [ctypes.c_void_p,
                                                             ctypes.c_uint32]
        self._cbtype = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.POINTER(self._Transfer))
        lib.hackrf_start_rx.argtypes = [ctypes.c_void_p, self._cbtype, ctypes.c_void_p]
        lib.hackrf_stop_rx.argtypes = lib.hackrf_close.argtypes = [ctypes.c_void_p]
        self._check(lib.hackrf_init(), "init")
        self.dev = ctypes.c_void_p()
        if serial:
            self._check(lib.hackrf_open_by_serial(serial.encode(),
                                                  ctypes.byref(self.dev)), "open")
        else:
            self._check(lib.hackrf_open(ctypes.byref(self.dev)), "open")
        self.rate = rate
        self._check(lib.hackrf_set_sample_rate(self.dev, rate), "set_sample_rate")
        self._check(lib.hackrf_set_baseband_filter_bandwidth(
            self.dev, int(rate * 0.75)), "set_baseband_filter_bandwidth")
        self._check(lib.hackrf_set_lna_gain(self.dev, lna), "set_lna_gain")
        self._check(lib.hackrf_set_vga_gain(self.dev, vga), "set_vga_gain")
        self._check(lib.hackrf_set_amp_enable(self.dev, 1 if amp else 0), "set_amp")
        self.q = queue.Queue(maxsize=256)
        self.dropped = 0
        self.pending = np.zeros(0, np.complex64)
        self._cb = self._cbtype(self._on_rx)    # kept: C holds it
        self.tune(center)
        self._check(lib.hackrf_start_rx(self.dev, self._cb, None), "start_rx")
        self.name = f"HackRF One{f' {serial}' if serial else ''}"

    def _check(self, err, what):
        if err != 0:
            raise ConnectionError(f"hackrf_{what}: "
                                  f"{self.lib.hackrf_error_name(err).decode()}")

    def _on_rx(self, transfer):
        t = transfer.contents
        try:
            self.q.put_nowait(ctypes.string_at(t.buffer, t.valid_length))
        except queue.Full:
            self.dropped += 1
        return 0

    def tune(self, center):
        self._check(self.lib.hackrf_set_freq(self.dev, int(center)), "set_freq")
        self.center = center
        self.skip = int(self.rate * 0.05)
        return True

    def read(self, n):
        chunks, have = [self.pending], len(self.pending)
        while have < n:
            try:
                data = self.q.get(timeout=5)
            except queue.Empty:
                raise ConnectionError("the HackRF stopped streaming") from None
            a = np.frombuffer(data, np.int8).astype(np.float32) / 128
            x = (a[0::2] + 1j * a[1::2]).astype(np.complex64)
            chunks.append(x)
            have += len(x)
        x = np.concatenate(chunks)
        self.pending = x[n:]
        return x[:n]

    def close(self):
        self.lib.hackrf_stop_rx(self.dev)
        self.lib.hackrf_close(self.dev)
        self.lib.hackrf_exit()


class IqStream(Source):
    """An IQ recording, or IQ on stdin: cu8 (rtl_sdr), cs8 (hackrf_transfer),
    cs16 or cf32.  A file plays in real time, looping with --loop."""
    FORMATS = {"cu8": ("uint8", 127.4, 128.0), "cs8": ("int8", 0.0, 128.0),
               "cs16": ("int16", 0.0, 32768.0), "cf32": ("float32", 0.0, 1.0)}

    def __init__(self, path, fmt, rate, center, loop):
        self.stdin = path == "-"
        self.f = sys.stdin.buffer if self.stdin else open(path, "rb")
        dtype, self.off, self.scale = self.FORMATS[fmt]
        self.dtype = np.dtype(dtype)
        self.size = 2 * self.dtype.itemsize
        self.rate, self.center, self.loop = rate, center, loop
        self.name = "stdin" if self.stdin else pathlib.Path(path).name
        self.name += f" ({fmt}, {rate / 1e6:g} MS/s at {center / 1e6:.3f} MHz)"
        self.t0, self.sent = None, 0

    def read(self, n):
        data = b""
        while len(data) < n * self.size:
            chunk = self.f.read(n * self.size - len(data))
            if not chunk:
                if self.loop and not self.stdin:
                    self.f.seek(0)
                    continue
                break
            data += chunk
        data = data[:len(data) // self.size * self.size]
        if not data:
            return None
        a = np.frombuffer(data, self.dtype).astype(np.float32)
        x = ((a[0::2] - self.off) + 1j * (a[1::2] - self.off)) / self.scale
        if not self.stdin:                      # as fast as the air would be
            if self.t0 is None:
                self.t0 = time.monotonic()
            ahead = self.t0 + self.sent / self.rate - time.monotonic()
            if ahead > 0:
                time.sleep(ahead)
        self.sent += len(x)
        return x.astype(np.complex64)

    def close(self):
        if not self.stdin:
            self.f.close()


def open_source(spec, args):
    """The Source a SOURCE argument names."""
    rate = parse_hz(args.rate) if args.rate else None
    center = parse_hz(args.center) if args.center else None
    if spec == "rtl_tcp" or spec.startswith("rtl_tcp:"):
        host, _, port = spec[8:].partition(":")
        return RtlTcp(host or "127.0.0.1", int(port or 1234), rate or 2.048e6,
                      center or 0, args.gain, args.ppm)
    if spec == "hackrf" or spec.startswith("hackrf:"):
        lna, vga = (int(g) for g in (args.hackrf_gain or "24,20").split(","))
        return HackRF(spec[7:] or None, rate or 2e6, center or 0, lna, vga, args.amp)
    path = pathlib.Path(spec)
    fmt = args.format or (path.suffix[1:].lower() if spec != "-" else "cu8")
    if fmt not in IqStream.FORMATS:
        raise ValueError(f"{spec}: say --format cu8|cs8|cs16|cf32")
    m = re.search(r"_(\d+(?:\.\d+)?)M_(\d+(?:\.\d+)?)([kM])", path.name)
    if m:                                       # rtl_433: name_433.92M_250k.cu8
        center = center or float(m.group(1)) * 1e6
        rate = rate or float(m.group(2)) * (1e3 if m.group(3) == "k" else 1e6)
    if not rate or not center:
        raise ValueError(f"{spec}: say --rate and --center (how it was recorded)")
    return IqStream(spec, fmt, rate, center, args.loop)


# ---------------------------------------------------------------- the DSP

class Channel:
    """The CC1101's channel out of the band: an overlap-save FFT filter.
    Each N-sample frame's spectrum gives up the M bins around the channel's
    offset (a multiple of 4 bins, so with frames 3N/4 apart the phase runs
    on across them), shaped by a raised-cosine passband of the CC1101's
    bandwidth; an M-point inverse FFT is the channel at baseband, at
    rate*M/N.  The first N/4 of each frame's output wrapped round and is
    dropped."""

    def __init__(self, rate, offset, bw, n):
        self.N, self.P = n, n // 4
        self.A = n - self.P
        bin_hz = rate / n
        m = n
        while m > 256 and rate * (m // 2) / n >= max(1.25 * bw, 200e3):
            m //= 2
        self.M = m
        self.rate = rate * m / n
        k0 = int(round(offset / bin_hz / 4)) * 4
        self.offset = k0 * bin_hz               # what it is centred on
        self.idx = (k0 + np.round(np.fft.fftfreq(m, 1 / m)).astype(int)) % n
        f = np.abs(np.fft.fftfreq(m, 1 / self.rate))
        edge = min(bw / 2, 0.49 * self.rate)
        tw = max(16 * bin_hz, 0.1 * bw)
        ramp = np.clip((edge + tw / 2 - f) / tw, 0, 1)
        self.H = ((0.5 - 0.5 * np.cos(np.pi * ramp)) * m / n).astype(np.complex64)
        self.buf = np.zeros(self.P, np.complex64)
        self.drop = self.P * m // n

    def process(self, x):
        buf = np.concatenate([self.buf, x])
        out, pos = [], 0
        while pos + self.N <= len(buf):
            spec = np.fft.fft(buf[pos:pos + self.N])
            out.append(np.fft.ifft(spec[self.idx] * self.H)[self.drop:])
            pos += self.A
        self.buf = buf[pos:]
        return (np.concatenate(out) if out else np.zeros(0)).astype(np.complex64)


def moving_average(x, w, tail):
    """x smoothed over w samples, carrying the last w-1 across calls."""
    if w <= 1:
        return x, tail
    xx = np.concatenate([tail, x])
    c = np.cumsum(xx, dtype=np.float64)
    c = np.concatenate([[0.0], c])
    return (c[w:] - c[:-w]) / w, xx[len(xx) - (w - 1):]


def schmitt(above, below, state):
    """A comparator with hysteresis: true from each `above` until a `below`."""
    n = len(above)
    idx = np.where(above | below, np.arange(n), -1)
    idx = np.maximum.accumulate(idx)
    return np.where(idx >= 0, above[np.maximum(idx, 0)], state)


class Squelch:
    """Channel power in dB against a noise floor (each frame's median: it
    falls at once and rises slowly, so a long burst cannot lift it) and a
    peak that falls slowly: open while the peak stands `db` above the floor.
    (The median, not a low percentile: an 8-bit recording's faint noise is a
    few levels of quantisation, whose low tail lies far under its peaks.)"""

    def __init__(self, rate, db, smooth_s):
        self.w = max(1, int(round(rate * smooth_s)))
        self.tail = np.zeros(self.w - 1)
        self.db = db
        self.floor = None
        self.peak = -300.0

    def power_db(self, y):
        p, self.tail = moving_average((y.real ** 2 + y.imag ** 2).astype(np.float64),
                                      self.w, self.tail)
        pdb = 10 * np.log10(p + 1e-20)
        lo, hi = np.percentile(pdb, 50), np.percentile(pdb, 99.5)
        self.floor = lo if self.floor is None else min(lo, self.floor + 0.5)
        self.peak = max(hi, self.peak - 2.0)
        return pdb

    @property
    def open(self):
        return self.floor is not None and self.peak > self.floor + self.db

    @property
    def snr(self):
        return 0.0 if self.floor is None else self.peak - self.floor


class OokSlicer:
    """OOK, as the CC1101's data slicer: the smoothed power against the
    chip's decision boundary below the peak (AGCCTRL0: 4, 8, 12 or 16 dB;
    the Flipper's presets have 8), or halfway in dB when the signal is too
    weak for that, with hysteresis.  Deep under the peak, a real
    transmitter's ripple (a beat with another carrier, multipath) cannot
    break a high up; the price, as on the chip, is highs a little long by
    the smoothing's ramp, kept short (1/16 bit)."""

    def __init__(self, rate, baud, squelch_db, boundary_db=8):
        smooth = min(max(1 / (16 * max(baud, 100)), 4e-6), 25e-6)
        self.sq = Squelch(rate, squelch_db, smooth)
        self.boundary = boundary_db
        self.min_us = max(8, smooth * 1e6 / 2)  # shorter than this: a glitch
        self.level = False

    def process(self, y):
        if not len(y):
            return np.zeros(0, bool)
        pdb = self.sq.power_db(y)
        if not self.sq.open:
            self.level = False
            return np.zeros(len(y), bool)
        mid = self.sq.peak - min(self.boundary, (self.sq.peak - self.sq.floor) / 2)
        h = min(1.0, (self.sq.peak - self.sq.floor) / 8)
        bits = schmitt(pdb > mid + h, pdb < mid - h, self.level)
        self.level = bool(bits[-1])
        return bits


class FskSlicer:
    """2-FSK: the phase step between samples is the frequency; above the
    transmitter's own centre (tracked per burst) is a 1, as the CC1101 has
    it.  Power below the squelch is silence."""

    def __init__(self, rate, baud, dev, squelch_db):
        self.rate = rate
        self.w = max(1, int(round(rate / (4 * max(baud, 100)))))
        self.tail = np.zeros(self.w - 1)
        self.sq = Squelch(rate, squelch_db, self.w / rate)
        self.prev = np.complex64(0)
        self.center = None
        self.h = 0.1 * dev
        self.min_us = max(8, self.w / rate * 1e6 / 2)
        self.level = False

    def process(self, y):
        if not len(y):
            return np.zeros(0, bool)
        z = np.concatenate([[self.prev], y])
        self.prev = y[-1]
        f = np.angle(z[1:] * np.conj(z[:-1])) * self.rate / (2 * np.pi)
        f, self.tail = moving_average(f.astype(np.float64), self.w, self.tail)
        pdb = self.sq.power_db(y)
        if not self.sq.open:
            self.level, self.center = False, None
            return np.zeros(len(y), bool)
        # the burst: power over the midpoint, as OOK's slicer has it.  Its
        # edges come half a window late, as the frequency's do, so a burst
        # neither starts early nor trails a glitch.
        on = pdb > 10 * math.log10((10 ** (self.sq.floor / 10) +
                                    10 ** (self.sq.peak / 10)) / 2)
        if on.any():
            mid = (np.percentile(f[on], 10) + np.percentile(f[on], 90)) / 2
            self.center = mid if self.center is None else 0.7 * self.center + 0.3 * mid
        c = self.center or 0.0
        bits = schmitt(f > c + self.h, f < c - self.h, self.level) & on
        self.level = bool(bits[-1])
        return bits


class Edges:
    """Levels at a sample rate -> +high/-low µs, the fractions carried on so
    the timeline keeps time with the samples.  A run shorter than min_us
    (no real pulse survives the slicer's smoothing that short) is a glitch,
    merged into the runs either side; so the newest run waits until the next
    has begun, or, if it is a long one, until 20 ms of it are in."""

    def __init__(self, rate, min_us):
        self.us = 1e6 / rate
        self.frac = 0.0
        self.min_us = min_us
        self.runs = []                          # [level, µs] not yet sent

    def push(self, bits):
        runs = self.runs
        if len(bits):
            cuts = np.flatnonzero(bits[1:] != bits[:-1]) + 1
            bounds = np.concatenate([[0], cuts, [len(bits)]])
            for run, level in zip(np.diff(bounds), bits[bounds[:-1]]):
                t = run * self.us + self.frac
                us = int(t)
                self.frac = t - us
                if runs and runs[-1][0] == bool(level):
                    runs[-1][1] += us
                elif us:
                    runs.append([bool(level), us])
        i = 1
        while i < len(runs) - 1:
            if runs[i][1] < self.min_us:        # a glitch: one run of either side's level
                runs[i - 1][1] += runs[i][1] + runs[i + 1][1]
                del runs[i:i + 2]
            else:
                i += 1
        out = [us if level else -us for level, us in runs[:-1]]
        del runs[:-1]
        if runs and runs[0][1] > 20000:         # a long one: send most of it
            out.append(runs[0][1] - 1000 if runs[0][0] else -(runs[0][1] - 1000))
            runs[0][1] = 1000
        return out


class Spectrum:
    """The band's power, Welch-averaged, as dBm per bin: the median bin is
    the noise floor, which is put where a CC1101's would be (-167 dBm/Hz with
    its noise figure), so RSSI over any channel comes out as the chip would
    read it.  The DC spike is patched over and the edges' roll-off left out."""

    def __init__(self, rate, offset_db):
        self.K = max(64, 1 << math.ceil(math.log2(rate / 4000)))
        self.win = np.hanning(self.K).astype(np.float32)
        self.acc = np.zeros(self.K)
        self.count = 0
        self.samples = 0
        self.rate = rate
        self.offset = offset_db

    def push(self, x):
        n = len(x) // self.K * self.K
        if n:
            seg = x[:n].reshape(-1, self.K) * self.win
            self.acc += (np.abs(np.fft.fft(seg, axis=1)) ** 2).sum(axis=0)
            self.count += n // self.K
        self.samples += len(x)

    def line(self, center):
        """The 'spectrum' line, once SPEC_EVERY_S has gone by, else None."""
        if self.samples < self.rate * SPEC_EVERY_S or not self.count:
            return None
        p = np.fft.fftshift(self.acc / self.count)
        self.acc[:], self.count, self.samples = 0, 0, 0
        k, c = self.K, self.K // 2
        p[c - 1:c + 2] = (p[c - 4] + p[c + 4]) / 2          # the LO's DC spike
        db = 10 * np.log10(p + 1e-30)
        lo, hi = int(k * 0.1), int(k * 0.9)
        step = self.rate / k
        dbm = db - np.median(db[lo:hi]) - 167 + 10 * math.log10(step) + self.offset
        f0 = center + (lo - c) * step
        return (f"spectrum {f0:.0f} {step:.3f} "
                + " ".join(f"{v:.1f}" for v in dbm[lo:hi]) + "\n")


# ---------------------------------------------------------------- the bridge

class Bridge:
    def __init__(self, link_path, src, args):
        self.src, self.args = src, args
        self.link = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.link.connect(str(link_path))
        self.inbuf = b""
        self.cfg = {}
        self.epoch = None                       # the set-up the DSP is for
        self.chan = self.slicer = self.edges = None
        self.off_band_since = None
        self.spectrum = Spectrum(src.rate, args.rssi_offset)
        self.block = 1 << max(10, math.ceil(math.log2(src.rate * 0.016)))
        self.nfft = 1 << max(12, math.ceil(math.log2(src.rate * 0.016)))
        self.stats = {"edges": 0, "t": time.monotonic()}
        self.send(f"hello fe_sdr: {src.name}, {src.rate / 1e6:g} MS/s\n")

    def send(self, line):
        self.link.sendall(line.encode())

    def poll_link(self):
        while select.select([self.link], [], [], 0)[0]:
            data = self.link.recv(65536)
            if not data:
                raise ConnectionError("the emulator closed the link")
            self.inbuf += data
        *lines, self.inbuf = self.inbuf.split(b"\n")
        for raw in lines:
            line = raw.decode(errors="replace")
            if line.startswith("cc1101 "):
                self.cfg = dict(kv.split("=", 1) for kv in line.split()[1:] if "=" in kv)
            elif line.startswith("tx "):
                self.transmitted(line)

    def transmitted(self, line):
        kv = dict(t.split("=", 1) for t in line.split()[1:] if "=" in t)
        n = kv.get("raw", "").count(",") + 1
        print(f"\n[fe_sdr] the Flipper transmitted at {int(kv.get('freq', 0)) / 1e6:.3f} "
              f"MHz ({kv.get('mod')}, {n} levels): not sent on (receive only)",
              flush=True)

    def in_band(self, freq, bw):
        """Can the SDR hear this channel?  A channel wider than the SDR's band
        (AM650 from a 250 kS/s recording) is heard over the whole band: what
        must be in it, clear of the edges, is the channel's middle."""
        half = min(bw, 0.5 * self.src.rate) / 2
        return abs(freq - self.src.center) + half <= EDGE_FRACTION * self.src.rate

    def retune_for(self, freq):
        """Put the SDR's centre a quarter of its rate above the channel: the
        LO's DC spike and its 1/f noise stay out of it."""
        if self.src.tune(freq + self.src.rate / 4):
            print(f"\n[fe_sdr] retuned to {self.src.center / 1e6:.3f} MHz "
                  f"for {freq / 1e6:.3f} MHz", flush=True)
        self.epoch = None

    def receive(self, x):
        cfg = self.cfg
        if cfg.get("state") != "rx" or cfg.get("mod") not in ("ook", "2fsk", "gfsk"):
            self.epoch, self.off_band_since = None, None
            return
        epoch = int(cfg["epoch"])
        freq, bw = int(cfg["freq"]), int(cfg["bw"])
        if not self.in_band(freq, bw):
            self.epoch = None
            now = time.monotonic()
            if self.off_band_since is None:
                self.off_band_since = now
            elif (self.args.follow and self.src.tunable and
                  now - self.off_band_since >= FOLLOW_AFTER_S):
                self.off_band_since = None
                self.retune_for(freq)
            return
        self.off_band_since = None
        if epoch != self.epoch:
            self.epoch = epoch
            baud, dev = int(cfg["rate"]), int(cfg["dev"])
            self.chan = Channel(self.src.rate, freq - self.src.center, bw, self.nfft)
            self.slicer = (OokSlicer(self.chan.rate, baud, self.args.squelch,
                                     int(cfg.get("boundary", 8)))
                           if cfg["mod"] == "ook" else
                           FskSlicer(self.chan.rate, baud, dev, self.args.squelch))
            self.edges = Edges(self.chan.rate, self.slicer.min_us)
        durs = self.edges.push(self.slicer.process(self.chan.process(x)))
        if durs:
            self.send(f"rx {epoch} " + " ".join(map(str, durs)) + "\n")
            self.stats["edges"] += len(durs)

    def status(self):
        now = time.monotonic()
        if now - self.stats["t"] < 1:
            return
        cfg, sq = self.cfg, self.slicer.sq if self.slicer else None
        rx = (f"CC1101 rx {int(cfg['freq']) / 1e6:.3f} MHz {cfg['mod']} "
              f"bw {int(cfg['bw']) / 1e3:.0f}k" if cfg.get("state") == "rx"
              else f"CC1101 {cfg.get('state', '?')}")
        if cfg.get("state") == "rx" and self.epoch is None:
            rx += " (off the SDR's band)"
        sig = (f"squelch {'open' if sq.open else 'shut'} {sq.snr:4.1f} dB"
               if sq and self.epoch is not None else "")
        rate = self.stats["edges"] / (now - self.stats["t"])
        msg = (f"[fe_sdr] SDR {self.src.center / 1e6:.3f} MHz | {rx} | {sig} | "
               f"{rate:.0f} levels/s")
        print(("\r" + msg.ljust(110)) if sys.stdout.isatty() else msg,
              end="" if sys.stdout.isatty() else "\n", flush=True)
        self.stats = {"edges": 0, "t": now}

    def run(self):
        while True:
            x = self.src.read(self.block)
            if x is None:
                return 0                        # the recording ended
            self.poll_link()
            if self.src.skip:
                n = min(self.src.skip, len(x))
                self.src.skip -= n
                x = x[n:]
                if not len(x):
                    continue
            self.spectrum.push(x)
            line = self.spectrum.line(self.src.center)
            if line:
                self.send(line)
            self.receive(x)
            if not self.args.quiet:
                self.status()


def add_options(p):
    """The bridge's options, shared with ./flipperemu subghz --sdr."""
    p.add_argument("--center", help="SDR centre frequency (Hz, or 434.4M): "
                   "default a quarter of the rate above 433.92 MHz; a file's "
                   "recording frequency")
    p.add_argument("--rate", help="sample rate (default 2.048M for rtl_tcp, 2M "
                   "for hackrf; a file's recording rate)")
    p.add_argument("--gain", type=float, help="rtl_tcp: tuner gain in dB "
                   "(default: the tuner's AGC)")
    p.add_argument("--ppm", type=int, default=0, help="rtl_tcp: frequency correction")
    p.add_argument("--hackrf-gain", help="hackrf: LNA,VGA dB (default 24,20)")
    p.add_argument("--amp", action="store_true", help="hackrf: the RF amplifier on")
    p.add_argument("--format", choices=sorted(IqStream.FORMATS),
                   help="IQ sample format (default: the file's extension; stdin cu8)")
    p.add_argument("--loop", action="store_true", help="play a recording over and over")
    p.add_argument("--squelch", type=float, default=12.0,
                   help="dB above the noise floor before anything is heard (default 12)")
    p.add_argument("--rssi-offset", type=float, default=0.0,
                   help="dB added to every RSSI, to match a real Flipper's readings")
    p.add_argument("--no-follow", dest="follow", action="store_false",
                   help="never retune the SDR to follow the firmware")
    p.add_argument("--quiet", action="store_true", help="no status line")


def run(link_path, spec, args):
    """Bridge SOURCE to the emulator's link socket until it ends or ^C."""
    load_numpy()
    radio = spec.split(":")[0] in ("rtl_tcp", "hackrf")
    if radio and not args.center:               # 433.92 MHz, clear of the DC spike
        rate = parse_hz(args.rate) if args.rate else (2e6 if spec.startswith("hackrf")
                                                      else 2.048e6)
        args.center = str(433.92e6 + rate / 4)
    src = open_source(spec, args)
    try:
        bridge = Bridge(link_path, src, args)
        print(f"[fe_sdr] {src.name} -> {link_path}", flush=True)
        return bridge.run()
    except KeyboardInterrupt:
        print()
        return 0
    finally:
        src.close()


def main():
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0],
                                formatter_class=argparse.RawDescriptionHelpFormatter,
                                epilog=__doc__.split("\n\n", 1)[1])
    p.add_argument("link", help="the device's Sub-GHz link socket "
                   "(devices/NAME/subghz.sock), or its directory")
    p.add_argument("source", help="rtl_tcp[:HOST[:PORT]], hackrf[:SERIAL], "
                   "FILE.cu8|cs8|cs16|cf32, or - (stdin)")
    add_options(p)
    args = p.parse_args()
    link = pathlib.Path(args.link)
    if link.is_dir():
        link = link / "subghz.sock"
    try:
        return run(link, args.source, args)
    except (OSError, ConnectionError, ValueError) as e:
        sys.exit(f"fe_sdr: {e}")


if __name__ == "__main__":
    sys.exit(main())
