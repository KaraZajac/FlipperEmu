# The radio links

An emulated radio can have a real one, or any program, as its antenna. Two radios have
such a link: the CC1101 (Sub-GHz) and the ST25R3916 (NFC).
- `scripts/fe_sdr.py` drives the CC1101's from an SDR. So can anything else that speaks
  the protocol: another SDR stack, a GNU Radio flowgraph, a simulator, a test.
- `scripts/fe_nfc_link.py` is the card on the ST25R3916's, written in Python or relayed.

## Where

Each device has two Unix sockets, `devices/NAME/subghz.sock` and `devices/NAME/nfc.sock`.
QEMU listens on each and takes one client at a time. The launcher passes `-chardev
socket,id=subghz-link,path=…,server=on,wait=off -global cc1101.link=subghz-link`, and the
same for `st25r3916`. Running QEMU by hand, give the device any chardev as `link`.

## Lines

Both links speak the same way. A message is one line of ASCII text, ended by `\n`.
Fields are separated by spaces, and `key=value` pairs can come in any order. Unknown
messages and keys are ignored, so either side can add to them. A `hello <text>` line from
the program describes it, and QEMU logs it.

# Sub-GHz (`subghz.sock`)

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

### Trying it by hand

```sh
socat - UNIX-CONNECT:devices/NAME/subghz.sock
```

This shows the set-up lines as the firmware tunes; Sub-GHz → Read hops through several
frequencies. Typing `rx EPOCH +390 -1170 +390 -1170 …` with the current epoch puts a
signal on GDO0, and `qom-get /machine/subghz rssi` on the monitor shows what the
firmware reads as RSSI.

# NFC (`nfc.sock`)

The program is the card in the field, and the firmware is the reader, while no card is
set on the device (`./flipperemu nfc none`). A built-in card answers instead of the link.

### From the emulator

`hello flipperemu nfc 1` comes first, on connecting.

`field on` and `field off` report the reader's field, sent on connecting and whenever it
changes. A card loses its state when the field goes.

```
poll 72 93708804A1B29FAE4B 6501
```

This is a frame the firmware sent as the reader, while its field is on:
- the number of data bits: 7 for REQA/WUPA, 8 per byte otherwise;
- the data, hex, CRC_A included where the frame has one. An incomplete last byte's bits
  are its low bits;
- the parity bit sent after each full byte, packed LSB first (byte 0's in bit 0). These
  are odd parity, except in MIFARE Classic's encrypted frames.

The example is a SELECT at cascade level 1, `93 70 88 04 A1 B2 9F AE 4B`: nine bytes,
so nine parity bits in two bytes.

### To the emulator

`answer <bits> <hex> [<parity hex>]` is the card's answer, in the same form. Without
parity, odd parity is sent, as a card computes it. Give it for MIFARE Classic's
encrypted answers. `answer 16 4400` is an ATQA; `answer 24 04da17` a SAK with its CRC.

`silence` means no answer: the firmware's no-response timer runs out.

### Time

Each poll stops the machine, and with it the virtual clock, until the answer or silence
comes: `vm_stop` from a bottom half, then `vm_start`. However long the program takes,
over a socket or USB to a real reader, the firmware sees the answer within its frame
waiting time. After a second with no answer, it gets silence and a warning, once.

### Trying it by hand

```sh
./flipperemu nfc --link log                      # print the reader's frames
socat - UNIX-CONNECT:devices/NAME/nfc.sock       # and answer them yourself
```

Start `scanner` in the `nfc` shell. The first poll is a WUPA, `poll 7 52`; answer
`answer 16 4400` within a second, and the next poll is the anticollision, `poll 16 9320 01`.
