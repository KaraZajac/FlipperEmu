# The radio link

An emulated radio can have a real one, or any program, as its antenna. The CC1101
(Sub-GHz) has such a link today. `scripts/fe_sdr.py` drives it from an SDR, and anything
else that speaks this protocol can too: another SDR stack, a GNU Radio flowgraph, a
simulator, a test.

## Where

Each device has a Unix socket, `devices/NAME/subghz.sock`. QEMU listens on it (the
launcher passes `-chardev socket,id=subghz-link,path=…,server=on,wait=off -global
cc1101.link=subghz-link`) and takes one client at a time. Running QEMU by hand, give the
`cc1101` device any chardev as `link`.

## Lines

The protocol is ASCII text, one message per line, ended by `\n`. Fields are separated by
spaces, and `key=value` pairs can come in any order. Unknown messages and keys are
ignored, so either side can add to them.

### From the emulator

`hello flipperemu subghz 1` comes first, on connecting. The `1` is the protocol version.

```
cc1101 epoch=7 state=rx freq=433919830 mod=ook bw=650000 rate=3793 dev=47607 boundary=8
```

This is the receive set-up, sent on connecting and whenever it changes. The firmware
loads a preset one register at a time, so the line waits until the writes have settled
for 1 ms.

| key | meaning |
|---|---|
| `epoch` | a number bumped on every change of the receive set-up |
| `state` | `rx`, `idle`, `tx`, `fstxon` or `sleep` |
| `freq` | the tuned frequency, Hz (FREQ2..0 × 26 MHz / 2¹⁶) |
| `mod` | `ook`, `2fsk`, `gfsk`, `4fsk` or `msk` (MDMCFG2.MOD_FORMAT) |
| `bw` | the channel filter bandwidth, Hz (MDMCFG4) |
| `rate` | the data rate, baud (MDMCFG4/3) |
| `dev` | the FSK deviation, Hz (DEVIATN) |
| `boundary` | the OOK decision boundary below the peak, dB (AGCCTRL0: 4, 8, 12 or 16) |

The Flipper receives in asynchronous serial mode: GDO0 carries the demodulated data with
no clock. Its presets AM270, AM650, FM238, FM476 and the 12 kHz FSK one are all this
way.

```
tx freq=433920000 mod=ook rate=3793 dev=47607 raw=+390,-1170,+1170,-390,...
```

This is a transmission, sent once it is over, as `+high/-low` microseconds. It is the
same data the model saves as a `.sub` file in `devices/NAME/subghz-tx/`.

### To the emulator

`hello <text>` describes the bridge. QEMU logs it.

```
rx 7 +390 -1170 +1170 -390 -11700 ...
```

This is what the CC1101 would put on GDO0 for set-up `7`, as `+high` / `-low`
microseconds. It is a continuous timeline: silence is a long low, and the chunks join
up, so a run may be split between lines (`-11000` then `-700` is one low). Data for any
epoch but the current one is dropped, along with whatever was queued for it when the
set-up changed. While the chip is not in RX, nothing is heard.

The model plays the timeline 60 ms behind what has come in, so chunks a few tens of
milliseconds apart join seamlessly. An edge waits for the firmware to read the previous
capture of TIM2, and keeps its exact timestamp. If the backlog grows beyond twice the
delay, long silences are shortened to make it up. If the timeline runs dry, the line
goes low until more arrives.

```
spectrum 433400000 4000.000 -131.2 -130.8 -129.9 ... -74.1 ... -131.0
```

This is the band's power, bin by bin: the first bin's centre frequency and the bin
spacing in Hz, then dBm per bin. The firmware's RSSI reads come from the latest one. For
the chip's frequency and bandwidth, the model sums the bins inside the channel. A part
of the channel the band doesn't cover counts as thermal noise, −167 dBm/Hz with the
chip's noise figure. A channel off the band entirely reads that noise floor:
−108.9 dBm for 650 kHz. A spectrum older than 1.5 s is ignored.

So a bridge should put each bin's noise at −167 dBm + 10·log₁₀(bin width). `fe_sdr.py`
takes the median bin as the noise floor and scales from there. Send one every 50 ms or
so: the Frequency Analyzer sweeps 2 ms per frequency and needs an answer at once.

A bridge with no spectrum gets −60 dBm while its timeline is high, and −100 dBm
otherwise.

## Trying it by hand

```sh
socat - UNIX-CONNECT:devices/NAME/subghz.sock
```

This shows the set-up lines as the firmware tunes; Sub-GHz → Read hops through several
frequencies. Typing `rx EPOCH +390 -1170 +390 -1170 …` with the current epoch puts a
signal on GDO0, and `qom-get /machine/subghz rssi` on the monitor shows what the
firmware reads as RSSI.
