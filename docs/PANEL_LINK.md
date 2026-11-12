# The front-panel link

The window that looks like a Flipper (`scripts/fe_gui.py`) is a program apart from QEMU.
It gets everything it shows, and sends every button press, over one link: the Flipper's
front panel. Any other program can be that window too: a test that clicks buttons, a
web page behind a small server, a recorder.

## Where

Each device has a Unix socket, `devices/NAME/panel.sock`. QEMU listens on it and takes
one client at a time. The launcher passes `-chardev socket,id=panel,path=…,server=on,
wait=off -global flipper-panel.link=panel`. Running QEMU by hand, give the panel any
chardev as `link`. Nothing on the panel's side runs while no program is connected.

A second program that connects while one has the link is accepted by the kernel, but
QEMU only reads from it, and says hello, once the first has gone. The window shows that
as "Busy" and waits.

`scripts/fe_record.py` is a small second program of that kind, and one to start from:
`./flipperemu record out.gif` connects, says hello, keeps each `lcd` line with the time
it came, and writes them as an animated GIF. It gives up after two seconds without the
emulator's hello, which means another program (the window) has the link. The window
records from the frames it gets anyway (Device > Record the screen).

## Lines

As on the [radio links](RADIO_LINK.md): a message is one line of ASCII text, ended by
`\n`, its fields separated by spaces. Unknown messages are ignored, so either side can
add to them. A `hello <text>` line from the program describes it, and QEMU logs it.

### From the emulator

`hello flipperemu panel 1` comes first, on connecting. The `1` is the protocol version.
Then everything below, once, so that the program can draw the panel as it is; after that,
each message when what it reports changes.

```
running 1
running 0 paused
```

Whether time goes on in the emulator. `running 0` is followed by why it stands still:
`paused` when somebody paused it (`./flipperemu pause`, the window's Device > Pause, the
monitor's `stop`), `debug` when a debugger has it stopped (a gdb that has just got on,
a breakpoint, its Ctrl-C), and otherwise QEMU's name for its run state (`io-error`,
`shutdown`, …). While it stands still no other
line comes: the screen, the lights and the properties are as the firmware left them.
The panel itself goes on (it runs on the host's clock), so a `press` is taken, and felt
by the firmware when it goes on. The NFC link stops the emulator for the moment a card
on it takes to answer; that is not reported, and `running 1` holds through it. An
emulator from before this message sends none: take that as `running 1`.

```
lcd 0000…ff00
```

The picture on the LCD: 2048 hex digits, 1024 bytes, the 64 rows of the 128×64 panel from
the top, 16 bytes a row, the leftmost pixel of each byte in its top bit, 1 for a pixel
that is on (dark). This is what the ST7567 shows, so a picture the firmware has not sent
yet is not in it. The panel looks every 16 ms. When the firmware is in the middle of
sending a frame, it waits 3 ms more (5 times at most), so that a frame does not arrive
half old and half new. While more than 64 KB wait to go to the program, no frames are
sent: a slow program gets the latest picture once it has caught up, not every one it
missed.

```
backlight 255
```

The backlight, 0 to 255 (the LP5562's white channel). The firmware dims it to 0 after its
idle timeout and lights it on a key press. It fades both ways, over about 125 ms: the
steps of the fade come as they are when the panel looks (every 16 ms), so eight to
twelve values on the way.

```
led ff0000
```

The RGB LED, as `rrggbb`. Red while charging, off on battery, blue in DFU mode; white
for a moment on a reset. While an app reads or sends, the firmware blinks it: 10 ms on
in every 90 ms, which comes as `led 00ffff`, `led 000000` and so on. A flash can be
shorter than the 16 ms between two looks of the panel, so a look reports the brightest
colour the LED has had since the last one, and the colour it has now with the next.
No flash is missed that way, and each is shown for 16 ms at least. A colour that the
LED has for less than 4 ms of the firmware's time is not reported at all: the firmware
sets the LED in several steps (red is still on for 3 ms when a blink in cyan starts),
and nobody sees those on a real one. To know whether it blinks without counting
flashes, read `/machine/led`'s `blinking` (below: `watch`).

```
vibro 1
```

The vibration motor, `1` on, `0` off.

```
speaker freq=440000 duty=500000
```

The speaker's tone: the frequency in thousandths of a hertz, and the duty cycle in
millionths (the firmware sets the volume with it). Both are 0 while it is silent. The
sound itself plays through QEMU's audio (`--audio`); this is for a program that shows it.

```
prop /machine/soc/usb attached true
prop /machine/nfc card "file:/home/…/devices/NAME/nfc-card.txt"
prop /machine/gauge soc 87
```

A QOM property the program asked to watch (below): its path, its name, and its value as
JSON, as the monitor's `qom-get` prints it. `null` when there is no such property.
Watched properties are reported when they are asked for, and when they change; the
panel looks every 100 ms.

```
power 0
```

The firmware switched the Flipper off (Settings > Power > Power OFF, the power-off dialog
of a long Back press, or a flat battery off USB). An emulator that is off has nothing to
run, so QEMU exits right after this line; the window that started it stays, shows the
Flipper off, and starts it again when Back is held.

```
halted 0x080123b0
```

The firmware has halted, with its PC there: a debug build after a crash (it waits for a
debugger), any build after `furi_halt()`. Sent once, when the emulator finds the CPU in
the halt loop (a `wfi` and a branch back to it; it looks every 250 ms and wants to see it
twice). From then on the CPU rests, which a reset ends. A program that connects later
does not get the line: `flipperemu status --json` has `halted` for that.

### To the emulator

```
hello <text>
```

What the program is; QEMU logs it.

```
press ok
release ok
```

A button goes down, and up: `up`, `down`, `left`, `right`, `ok`, `back`. A button stays
down until it is released, so a program can hold one as long as a finger would (Back for
5 s offers to switch the Flipper off). The firmware debounces its buttons over a few
milliseconds, and a touchpad tap is a press and a release at once: a press lasts at least
40 ms of emulated time, and a release that comes sooner waits for it. A `press` that
comes while that release waits continues the same press. These are the same buttons the
keyboard of QEMU's own window and the monitor's `sendkey` press, and the last of them to
press or release a button decides whether it is down.

When the program goes, the buttons it held are released.

```
refresh
```

Everything again, as on connecting: whether it runs, the lights, the LCD and the watched
properties.

```
watch /machine/soc/usb attached
```

Report this QOM property now, and whenever it changes (64 at most). Watches end with the
connection: a program asks again after each hello.

There is no message that changes a property, or that pauses the emulator. The window
makes its changes as a terminal would, with the launcher's commands (`flipperemu usb
unplug`, `flipperemu nfc card.nfc`, `flipperemu pause`, …), which go through the
monitor. Then what changed comes back over the link, whoever changed it.

## What the window watches

| property | what for |
|---|---|
| `/machine/soc/usb attached`, `device`; `/machine/charger usb` | the USB cable (computer, charger, unplugged) and what the computer makes of the Flipper |
| `/machine/gauge soc`, `voltage-mv`, `temperature-dk`, `current-ma` | the battery; also what a Flipper switched on again starts with |
| `/machine sd-card` | the microSD card, in the slot or pulled out |
| `/machine/nfc card`, `field`, `radio`, `tx`, `reader` | a card held to it, a reader's field, its own radio, what the virtual reader read |
| `/machine/rfid tag`, `t5577`, `reader`, `noise`, `field`, `tx` | the same for 125 kHz, and a T5577's blocks as the firmware writes them |
| `/machine/ibutton key`, `rom`, `tx`, `reader` | a key on the contact, its ROM, what the virtual reader read |
| `/machine/ir tx` | the latest infrared transmission (an `.ir` file) |
| `/machine/subghz radio` | the CC1101's state and tuning |
| `/machine/speaker mute` | the speaker muted |
| `/machine log-level` | the level of the firmware's log, as the RTC's register has it |
| `/machine header`, `header-record` | the pins of the GPIO header (what the firmware has each as, its level, what is held to it); a record of the pins and the changes it has so far |
| `/machine header-serial`, `header-i2c`, `header-i2c-record` | the header's two serial ports (off, or the baud rate and who takes what comes in); its I2C bus (the devices on it, how many addresses the firmware called and how many answered); a record of that bus and what it has so far |

`/machine/subghz tx` is the whole RAW recording of the latest transmission, too long to
watch; the window looks for new files in `devices/NAME/subghz-tx/` instead. What the
launcher held to the Flipper is named in `devices/NAME/held.json` (`{"nfc": "an NTAG215
04 A1 …"}`): the emulator has the card's bytes, not what it was called. The values of
a device on the I2C bus (a sensor's temperature) are properties of QEMU's own devices,
a different set for each type: the window asks the launcher for them (`i2c --json`)
when the bus's devices change, when the GPIO menu opens and after it set one.

## Trying it

```sh
socat - UNIX-CONNECT:devices/NAME/panel.sock      # a running device
hello me
watch /machine/gauge soc
press ok
release ok
```
