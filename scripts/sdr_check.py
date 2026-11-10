#!/usr/bin/env python3
# Copyright (c) 2026, Kara Zajac
# SPDX-License-Identifier: BSD-3-Clause
"""Check the Sub-GHz radio bridge (scripts/fe_sdr.py) with no radio at all.

    scripts/sdr_check.py DEVICE [CHECK ...]     dsp, file, rtl_tcp, rssi
    scripts/sdr_check.py synth OUT.cu8 [--key 0x5A3C96] [--freq 433.92M]
                         [--center 434.432M] [--rate 2.048M] [--snr 20]
    scripts/sdr_check.py rtl-tcp-sim [--port 1234] [--freq 433.92M]
                         [--key 0x5A3C96] [--every 1] [--snr 25] [--cw]

The checks:
- dsp: OOK and 2-FSK at the Flipper's three deviations, made from known
  timings at 10-25 dB SNR, come out of the bridge's channel filter, slicer
  and edges within a few microseconds (no device needed).
- file: a Princeton key in a cu8 recording (an RTL-SDR's format), played
  into the device: `subghz rx` must decode it.
- rtl_tcp: the same key from a simulated rtl_tcp server, with the SDR first
  tuned to 315 MHz: the bridge must follow the firmware to 433.92 MHz.
- rssi: a carrier 30 dB over the noise at 433.95 MHz: the RSSI the firmware
  reads at 433.92 MHz (650 kHz wide) must stand well over -90 dBm, and with
  no carrier sit at the noise floor (-109 dBm for 650 kHz).

`synth` writes such a recording; `rtl-tcp-sim` serves one as an rtl_tcp
server would, retuning on command, so the bridge can be tried by hand.
"""
import argparse
import os
import pathlib
import re
import select
import socket
import struct
import subprocess
import sys
import threading
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import fe_sdr  # noqa: E402
import fe_subghz  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parent.parent
KEY = 0x5A3C96
np = None


def levels_of(durs, rate):
    """+high/-low µs -> a level per sample."""
    out = []
    for d in durs:
        out.append(np.full(int(round(abs(d) * rate / 1e6)), d > 0))
    return np.concatenate(out) if out else np.zeros(0, bool)


def synth(durs, rate, offset, mod="ook", snr_db=20.0, dev=0.0, lead=0.05,
          trail=0.05, rng=None):
    """IQ of a transmission at `offset` Hz, noise of unit power per sample."""
    rng = rng or np.random.default_rng(1)
    lv = np.concatenate([np.zeros(int(lead * rate), bool), levels_of(durs, rate),
                         np.zeros(int(trail * rate), bool)])
    n_lead, n_trail = int(lead * rate), int(trail * rate)
    t = np.arange(len(lv)) / rate
    amp = 10 ** (snr_db / 20)
    if mod == "ook":
        sig = amp * lv * np.exp(2j * np.pi * offset * t)
    else:
        on = np.zeros(len(lv), bool)
        on[n_lead:len(lv) - n_trail] = True
        f = offset + np.where(lv, dev, -dev)
        sig = amp * on * np.exp(2j * np.pi * np.cumsum(f) / rate)
    noise = (rng.normal(size=len(lv)) + 1j * rng.normal(size=len(lv))) / np.sqrt(2)
    return (sig + noise).astype(np.complex64)


def to_cu8(x, noise_lsb=3.0):
    """IQ (unit noise) as an RTL-SDR's bytes: noise about 3 LSB rms."""
    q = np.empty(2 * len(x), np.float32)
    q[0::2], q[1::2] = x.real, x.imag
    return np.clip(np.round(127.5 + q * noise_lsb), 0, 255).astype(np.uint8).tobytes()


def demod(x, rate, offset, bw, mod, baud, dev, squelch=12.0):
    """The bridge's DSP on a recording: merged +high/-low µs."""
    nfft = 1 << max(12, int(np.ceil(np.log2(rate * 0.016))))
    ch = fe_sdr.Channel(rate, offset, bw, nfft)
    sl = (fe_sdr.OokSlicer(ch.rate, baud, squelch) if mod == "ook" else
          fe_sdr.FskSlicer(ch.rate, baud, dev, squelch))
    ed = fe_sdr.Edges(ch.rate, sl.min_us)
    out = []
    for i in range(0, len(x), nfft):
        out += ed.push(sl.process(ch.process(x[i:i + nfft])))
    rng = np.random.default_rng(7)
    out += ed.push(sl.process(ch.process(((rng.normal(size=nfft) + 1j *
                                           rng.normal(size=nfft)) / np.sqrt(2))
                                         .astype(np.complex64))))
    merged = []
    for d in out:
        if merged and (merged[-1] > 0) == (d > 0):
            merged[-1] += d
        else:
            merged.append(d)
    while merged and merged[0] < 0:
        merged.pop(0)
    while merged and merged[-1] < 0:
        merged.pop()
    return merged


def check_dsp(_):
    key = fe_subghz.princeton(KEY)
    while key[-1] < 0:
        key.pop()
    fsk = [d for p in [(500, -500), (1000, -500), (500, -1000)] * 20 for d in p][:-1]
    cases = [("OOK 650k", key, "ook", 650e3, 3794, 0, snr) for snr in (25, 15, 10)]
    cases += [(f"2-FSK dev {dev / 1e3:g}k", fsk, "fsk", 270833, 4798, dev, snr)
              for dev in (47607, 12700, 2380) for snr in (25, 15)]
    worst, bad = 0, []
    for name, sent, mod, bw, baud, dev, snr in cases:
        x = synth(sent, 2.048e6, -512e3, mod, snr, dev)
        got = demod(x, 2.048e6, -512e3, bw, "ook" if mod == "ook" else "2fsk", baud, dev)
        if len(got) != len(sent):
            bad.append(f"{name} at {snr} dB: {len(got)} levels for {len(sent)}")
            continue
        err = max(abs(abs(a) - abs(b)) for a, b in zip(got, sent))
        worst = max(worst, err)
        if err > 25:
            bad.append(f"{name} at {snr} dB: off by {err} µs")
    if bad:
        return False, "; ".join(bad)
    return True, f"{len(cases)} signals, timing within {worst} µs"


# ------------------------------------------------------------ rtl_tcp

class RtlTcpSim(threading.Thread):
    """An rtl_tcp server with a key (or a carrier) on the air: noise, and the
    transmission every `every` seconds at its own frequency, wherever the
    client tunes.  It streams in real time as cu8."""

    def __init__(self, port=0, freq=433.92e6, durs=None, every=1.0, snr_db=25.0,
                 cw=False, quiet=True):
        super().__init__(daemon=True)
        self.srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.srv.bind(("127.0.0.1", port))
        self.srv.listen(1)
        self.port = self.srv.getsockname()[1]
        self.freq, self.every, self.cw, self.quiet = freq, every, cw, quiet
        self.amp = 10 ** (snr_db / 20)
        self.center, self.rate = 100e6, 2.048e6
        self.durs = durs or []
        self.tunes = []
        self.stop = False

    def run(self):
        conn, _ = self.srv.accept()
        conn.sendall(b"RTL0" + struct.pack(">II", 5, 29))     # an R820T
        rng = np.random.default_rng(3)
        key = levels_of(self.durs, self.rate)
        t0, sent, chunk = time.monotonic(), 0, 16384
        cmds = b""
        try:
            while not self.stop:
                while select.select([conn], [], [], 0)[0]:
                    data = conn.recv(4096)
                    if not data:
                        return
                    cmds += data
                while len(cmds) >= 5:
                    cmd, val = struct.unpack(">BI", cmds[:5])
                    cmds = cmds[5:]
                    if cmd == 0x01:
                        self.center = val
                        self.tunes.append(val)
                        if not self.quiet:
                            print(f"[rtl-tcp-sim] tuned to {val / 1e6:.3f} MHz", flush=True)
                    elif cmd == 0x02:
                        self.rate = val
                        key = levels_of(self.durs, self.rate)
                n = np.arange(sent, sent + chunk)
                x = (rng.normal(size=chunk) + 1j * rng.normal(size=chunk)) / np.sqrt(2)
                off = self.freq - self.center
                if abs(off) < self.rate / 2:
                    if self.cw:
                        on = np.ones(chunk, bool)
                    else:
                        pos = n % int(self.every * self.rate)
                        on = np.zeros(chunk, bool)
                        inkey = pos < len(key)
                        on[inkey] = key[pos[inkey]]
                    x = x + self.amp * on * np.exp(2j * np.pi * off * n / self.rate)
                conn.sendall(to_cu8(x.astype(np.complex64)))
                sent += chunk
                ahead = t0 + sent / self.rate - time.monotonic()
                if ahead > 0:
                    time.sleep(ahead)
        except OSError:
            return
        finally:
            conn.close()


# ------------------------------------------------------------ against a device

def bridge(dev, source, *extra):
    """./flipperemu subghz --sdr SOURCE for the device, in the background."""
    return subprocess.Popen([str(ROOT / "flipperemu"), "-d", dev, "subghz", "--sdr",
                             source, "--quiet", *extra],
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)


def listen_for_key(mon, start, seconds):
    """`subghz rx` at 433.92 MHz while `start()` puts the key on the air:
    the decode, or None.  (Unleashed's `subghz rx` cannot start the internal
    radio; `subghz rx_raw` then must show the key's 390/1170 µs.)"""
    from fe_cli import FlipperCli, usb_endpoint
    want = f"Key:0x{KEY:08X}".encode()
    cli = FlipperCli(usb_endpoint(mon))
    proc = None
    try:
        cli.read_until_prompt(15)
        for command in (b"subghz rx 433920000 0\r", b"subghz rx_raw 433920000\r"):
            cli.write(command)
            time.sleep(1.0)
            if proc is None:
                proc = start()
            out, end = b"", time.time() + seconds
            while time.time() < end:
                r, _, _ = select.select([cli.fd], [], [], 0.1)
                if r:
                    out += cli.sock.recv(4096) if cli.sock else os.read(cli.fd, 4096)
                if want in out:
                    return want.decode()
                if b"failed to start" in out:
                    break
                vals = [int(v) for v in re.findall(rb"[+-](\d+)", out)]
                if sum(abs(v - 390) < 60 or abs(v - 1170) < 90 for v in vals) >= 40:
                    return "the key's timings (rx_raw)"
            cli.write(b"\x03")
            time.sleep(0.5)
            cli.read_until_prompt(5)
        return None
    finally:
        cli.write(b"\x03")
        time.sleep(0.3)
        cli.close()
        if proc:
            proc.terminate()
            proc.wait(5)


def check_file(dev, mon=None):
    """(Takes the device's monitor if the caller holds it: one client at a time.)"""
    from fe_monitor import Monitor
    global np
    if np is None:
        fe_sdr.load_numpy()
        np = fe_sdr.np
    rate, center = 2.048e6, 434.432e6
    x = synth(fe_subghz.princeton(KEY) * 3, rate, 433.92e6 - center, "ook", 20, lead=1.5)
    path = ROOT / "devices" / dev / "sdr-check_434.432M_2048k.cu8"
    path.write_bytes(to_cu8(x))
    own = mon is None
    if own:
        mon = Monitor(str(ROOT / "devices" / dev / "monitor.sock"))
    try:
        got = listen_for_key(mon, lambda: bridge(dev, str(path)), 8)
    finally:
        if own:
            mon.close()
        path.unlink()
    return (True, f"decoded {got} from a cu8 recording") if got else (False, "nothing decoded")


def check_rtl_tcp(dev):
    from fe_monitor import Monitor
    sim = RtlTcpSim(durs=fe_subghz.princeton(KEY), every=0.6)
    sim.start()
    mon = Monitor(str(ROOT / "devices" / dev / "monitor.sock"))
    try:
        got = listen_for_key(mon, lambda: bridge(dev, f"rtl_tcp:127.0.0.1:{sim.port}",
                                                 "--center", "315M"), 10)
    finally:
        sim.stop = True
        mon.close()
    tunes = ", ".join(f"{t / 1e6:.3f}" for t in sim.tunes)
    if not got:
        return False, f"nothing decoded (tuned to {tunes} MHz)"
    return True, f"followed the firmware ({tunes} MHz) and decoded {got}"


def check_rssi(dev):
    from fe_cli import FlipperCli, usb_endpoint
    from fe_monitor import Monitor
    readings = {}
    for label, cw in (("carrier", True), ("noise", False)):
        sim = RtlTcpSim(freq=433.95e6, cw=cw, snr_db=30)
        sim.start()
        mon = Monitor(str(ROOT / "devices" / dev / "monitor.sock"))
        cli = FlipperCli(usb_endpoint(mon))
        proc = None
        try:
            cli.read_until_prompt(15)
            cli.write(b"subghz rx 433920000 0\r")
            time.sleep(1.0)
            proc = bridge(dev, f"rtl_tcp:127.0.0.1:{sim.port}")
            time.sleep(2.0)
            vals = []
            for _ in range(5):
                vals.append(float(mon.cmd("qom-get /machine/subghz rssi").strip().strip('"')))
                time.sleep(0.1)
            readings[label] = sum(vals) / len(vals)
        finally:
            cli.write(b"\x03")
            time.sleep(0.5)
            cli.close()
            mon.close()
            sim.stop = True
            if proc:
                proc.terminate()
                proc.wait(5)
    ok = readings["carrier"] > -80 and -115 < readings["noise"] < -100
    return ok, (f"{readings['carrier']:.1f} dBm with a carrier at 433.95 MHz, "
                f"{readings['noise']:.1f} dBm without")


CHECKS = {"dsp": check_dsp, "file": check_file, "rtl_tcp": check_rtl_tcp,
          "rssi": check_rssi}


def main():
    global np
    fe_sdr.load_numpy()
    np = fe_sdr.np
    if len(sys.argv) > 1 and sys.argv[1] in ("synth", "rtl-tcp-sim"):
        p = argparse.ArgumentParser()
        p.add_argument("what")
        p.add_argument("out", nargs="?")
        p.add_argument("--key", default=hex(KEY))
        p.add_argument("--freq", default="433.92M")
        p.add_argument("--center", default=None)
        p.add_argument("--rate", default="2.048M")
        p.add_argument("--snr", type=float, default=25.0)
        p.add_argument("--port", type=int, default=1234)
        p.add_argument("--every", type=float, default=1.0)
        p.add_argument("--cw", action="store_true", help="a steady carrier instead")
        a = p.parse_args()
        freq, rate = fe_sdr.parse_hz(a.freq), fe_sdr.parse_hz(a.rate)
        durs = fe_subghz.princeton(int(a.key, 0))
        if a.what == "synth":
            center = fe_sdr.parse_hz(a.center) if a.center else freq + rate / 4
            pathlib.Path(a.out).write_bytes(to_cu8(synth(durs * 3, rate, freq - center,
                                                         "ook", a.snr, lead=0.5)))
            print(f"{a.out}: Princeton {int(a.key, 0):#x} at {freq / 1e6:.3f} MHz; "
                  f"play it with --center {center / 1e6:.6g}M --rate {rate / 1e6:g}M")
            return 0
        sim = RtlTcpSim(a.port, freq, durs, a.every, a.snr, a.cw, quiet=False)
        print(f"[rtl-tcp-sim] on 127.0.0.1:{sim.port}: "
              f"{'a carrier' if a.cw else f'Princeton {int(a.key, 0):#x} every {a.every:g} s'} "
              f"at {freq / 1e6:.3f} MHz", flush=True)
        sim.run()
        return 0
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    dev, names = sys.argv[1], sys.argv[2:] or list(CHECKS)
    failed = 0
    for name in names:
        t0 = time.time()
        try:
            ok, info = CHECKS[name](dev)
        except (OSError, ConnectionError, TimeoutError, ValueError) as e:
            ok, info = False, str(e)
        failed += not ok
        print(f"{'PASS' if ok else 'FAIL'}  {name:8} {info} ({time.time() - t0:.1f} s)",
              flush=True)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
