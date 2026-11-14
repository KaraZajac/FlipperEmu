# The radio links

An emulated radio can have a real one, or any program, as its antenna. Three radios have
such a link: the CC1101 (Sub-GHz), the ST25R3916 (NFC) and CPU2's BLE stack (Bluetooth).
- `scripts/fe_sdr.py` drives the CC1101's from an SDR. So can anything else that speaks
  the protocol: another SDR stack, a GNU Radio flowgraph, a simulator, a test.
- `scripts/fe_nfc_link.py` is the card on the ST25R3916's, written in Python or relayed.
- `scripts/fe_ble.py` is the phone on the BLE stack's (`./flipperemu ble`).

## Where

Each device has three Unix sockets, `devices/NAME/subghz.sock`, `devices/NAME/nfc.sock`
and `devices/NAME/ble.sock`. QEMU listens on each and takes one client at a time. The
launcher passes `-chardev socket,id=subghz-link,path=…,server=on,wait=off -global
cc1101.link=subghz-link`, the same for `st25r3916`, and `-global
stm32wb55-ipcc.ble-link=ble-link` for Bluetooth. Running QEMU by hand, give the device
any chardev as `link` (`ble-link` for the IPCC).

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
poll 72 93708804A1B29FAE4B 6501 mode=a
```

This is a frame the firmware sent as the reader, while its field is on:
- the number of data bits: 7 for REQA/WUPA, 8 per byte otherwise;
- the data, hex, CRC_A included where the frame has one. An incomplete last byte's bits
  are its low bits;
- the parity bit sent after each full byte, packed LSB first (byte 0's in bit 0). These
  are odd parity, except in MIFARE Classic's encrypted frames.

The example is a SELECT at cascade level 1, `93 70 88 04 A1 B2 9F AE 4B`: nine bytes,
so nine parity bits in two bytes. `mode` is what the reader polls for, from the chip's
MODE register: `a` (ISO14443A), `b` (ISO14443B), `f` (FeliCa), `v` (ISO15693, as a
subcarrier stream), `t1` (Topaz), `bpsk` or `other`. A relay that only speaks Type A
(`pm3`) answers the rest with silence at once.

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

Start `scanner` in the `nfc` shell. The first poll is a WUPA, `poll 7 52 mode=a`; answer
`answer 16 4400` within a second, and the next poll is the anticollision, `poll 16 9320 01 mode=a`.

# Bluetooth (`ble.sock`)

The program is a central in range of the Flipper: a phone, as far as the firmware can
tell. The link carries what goes through the air at the level of GAP, SMP and ATT, not
packets and not keys. To the firmware, ST's stack on CPU2 says what a real central did
(the connection's events, the pairing's, a client's writes), and what the firmware
updates goes to the central as a notification or an indication, if the central asked
for them in the characteristic's CCCD. One connection at a time.

Numbers are hex with `0x` (handles, reasons) or decimal; data is hex, two digits a
byte. Handles are those of the stack's GATT table, which `gatt` lists.

### From the emulator

`hello flipperemu ble 1` comes first, on connecting.

```
adv on addr=80:E1:26:94:C0:EE addr_type=public connectable=1 interval=128-160 data=0201060d09466c6970706572206274326103028130 scan=
adv off
```

The advertising, sent on connecting and whenever it changes: the address and its type,
whether a central may connect, the interval in units of 0.625 ms (128-160 is the
Flipper's fast advertising, 1600-4000 its slow one after a minute), the advertising data
as the stack builds it (the flags, the local name, the services' UUIDs: AD structures,
each a length and a type) and the scan response's. `adv off` while it does not
advertise, and while a central is connected.

| line | when |
|---|---|
| `connected handle=0x0801 interval=24 latency=0 timeout=400 mtu=23` | the answer to `connect`; the interval in 1.25 ms, the supervision timeout in 10 ms |
| `security-request bond=1 mitm=1 sc=1` | the firmware asks the central to pair (it does at every connection) |
| `mtu 414` | the answer to `mtu`: what both take (the Flipper's stack takes 414 at most) |
| `params interval=6 latency=0 timeout=400` | the firmware asked for other connection parameters, and the central took them |
| `pairing passkey enter` | the Flipper shows a PIN: the central is to send it (`passkey`) |
| `pairing passkey show` | the central is to show a PIN of its own, for the Flipper to enter (no firmware here asks for it) |
| `pairing compare 123456` | numeric comparison: both are to confirm that they show this value (`confirm`) |
| `paired authenticated=1 bonded=1 sc=1` | the link is encrypted; authenticated: by a PIN or a comparison; sc: LE Secure Connections; bonded: both sides keep the bond |
| `encrypted authenticated=1 bonded=1 sc=1` | the answer to `encrypt`: the Flipper has the central's bond, and the link is encrypted with it, no PIN |
| `encryption failed reason=key-missing` | the Flipper has no bond with this central (it was forgotten there): the firmware ends the connection, and a phone pairs again |
| `pairing failed reason=0x04` | SMP's reason: 0x01 passkey entry failed, 0x03 authentication requirements, 0x04 confirm value failed (a wrong PIN), 0x0C numeric comparison failed |
| `value 0x0012 312e352e31` | the answer to `read` |
| `written 0x0023` | the answer to `write` |
| `notify 0x0028 00000400` | a notification: a value that the firmware updated, its CCCD's bit 0 set |
| `indicate 0x0025 18080732…` | an indication: the same with bit 1; the central confirms it with `ack`, and the firmware waits for that before the next one |
| `disconnected by=flipper reason=0x05` | the connection ended: `by=central` (its `disconnect`), or `by=flipper` with the reason the firmware gave (0x05 after a failed pairing, 0x13 when Bluetooth goes off) or 0x08 when its stack stopped |
| `error op=read handle=0x000e code=0x05` | a request refused: ATT's code (0x01 invalid handle, 0x02 read not permitted, 0x03 write not permitted, 0x05 insufficient authentication: pair first, 0x0D invalid length), or `reason=` for the link's own (`not-connected`, `not-advertising`, ...) |

A notification or an indication carries as much of the value as the MTU does (MTU − 3
bytes): 20 until an `mtu` exchange.

### To the emulator

| line | |
|---|---|
| `connect [addr=C6:89:67:45:23:01] [addr_type=random\|public] [interval=24]` | connect, while it advertises connectably |
| `disconnect [reason=0x13]` | end the connection |
| `mtu 517` | exchange MTUs |
| `encrypt` | a central with a bond: encryption with the key it keeps, as a phone does when it comes back |
| `pair [io=keyboard-display\|keyboard\|display\|yesno\|none] [bond=0] [sc=0] [mitm=0]` | the central's pairing request. The method follows from both sides' IO capabilities as Bluetooth's table has it: with the Flipper's (DisplayOnly for the serial profile) and the default `keyboard-display`, a PIN that the Flipper shows. Just Works, where the capabilities give it, fails when either side wants protection against a man in the middle (the Flipper does) |
| `passkey 123456` \| `passkey cancel` | the PIN, as the central's user typed it |
| `confirm yes` \| `confirm no` | the central's side of a numeric comparison |
| `read 0x0012` | a characteristic's value, a descriptor, or a declaration |
| `write 0x0023 HEX` \| `write-cmd 0x0023 HEX` | a Write Request (answered `written`) or a Write Command; at most MTU − 3 bytes |
| `ack` | the confirmation of an indication |
| `gatt` | the table: `service`, `char`, `cccd` and `desc` lines, then `gatt end` |
| `adv` | the advertising line again |

Permissions are the firmware's own: a characteristic that wants authentication (the
device information, the battery, the serial service) answers 0x05 until the link is
paired with a PIN. A write to a CCCD reaches the firmware too, as on ST's stack.

### Bonds

A pairing in which both sides bond (the Flipper's profiles do) leaves a bond: the stack
keeps the central's address and how it was paired, and the firmware saves that to
`/int/.bt.keys`, so it outlives a reboot. The central keeps its side (a phone does;
`./flipperemu ble` in `devices/NAME/ble-central.json`) and comes back with `encrypt`:
encrypted, no PIN, its CCCDs as it left them. A central that has lost its side and sends
`pair` again pairs anew, with the PIN: the firmware hears that the bond was lost and
allows it. Settings > Bluetooth > Unpair All Devices forgets every bond, and an
`encrypt` after it fails.

### Time and flow

What the central sends becomes the stack's events at once, in order, and waits for
nothing but room: the firmware takes events from a queue of a few, and while it is full
the link stops reading (the central's lines wait in its socket, as a controller holds
back its acknowledgements), so a fast central loses nothing. The serial service has
flow control of its own on top (its flow-control value: how many bytes the firmware's
RPC buffer takes), which `fe_ble.Rpc` keeps to.

A program that goes away while connected is a central out of range: the firmware hears
of a supervision timeout (0x08). So does a machine loaded from a snapshot that was saved
with a connection: the program of then is gone.

### Trying it by hand

```sh
./flipperemu ble scan                              # the advertising
socat - UNIX-CONNECT:devices/NAME/ble.sock         # or the lines themselves
```

Settings > Bluetooth on (it is off on a new Flipper), then `connect`, `mtu 517`, `pair`;
the Flipper shows a PIN (`./flipperemu log` has it too): `passkey` and the PIN. Then
`gatt`, and `read` a handle of the device information. An RPC session: `write` the
serial TX characteristic's CCCD `0200`, then a protobuf request to RX, and the answer
comes as an indication on TX; `ack` it.
