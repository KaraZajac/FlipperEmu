# Developing for the Flipper with FlipperEmu

How to work on an app, or on a firmware, with an emulated Flipper where the real one
would be: the build tools you already use, pointed at it.

- [An app, from its source to the screen](#an-app-from-its-source-to-the-screen)
- [A GIF of your app](#a-gif-of-your-app)
- [Pause it](#pause-it)
- [The LED and the backlight](#the-led-and-the-backlight)
- [The pins of the GPIO header](#the-pins-of-the-gpio-header)
- [A serial port on the header](#a-serial-port-on-the-header)
- [A sensor on the header's I2C bus](#a-sensor-on-the-headers-i2c-bus)
- [A flash on the header's SPI bus](#a-flash-on-the-headers-spi-bus)
- [A test that runs by itself](#a-test-that-runs-by-itself)
- [Why `./flipperemu ufbt`, and not `ufbt` alone](#why-flipperemu-ufbt-and-not-ufbt-alone)
- [The SDK has to be the firmware's](#the-sdk-has-to-be-the-firmwares)
- [gdb, with the firmware's symbols and your app's](#gdb-with-the-firmwares-symbols-and-your-apps)
- [A firmware: `fbt`](#a-firmware-fbt)
- [When it goes wrong](#when-it-goes-wrong)
- [What was checked](#what-was-checked)

## An app, from its source to the screen

[ufbt](https://github.com/flipperdevices/flipperzero-ufbt) is Flipper Devices' tool for
apps: it gets the SDK and the compiler by itself, builds a `.fap`, and on a real
Flipper `ufbt launch` puts it there over USB and starts it. Here it does the same:

```sh
pipx install ufbt                      # once (or pip, in a virtual environment)

./flipperemu run -f firmware/official-1.5.1/flipper-z-f7-update-1.5.1.tgz
                                       # a Flipper, in its window: leave it running

cd examples/hello                      # in another terminal: an app's folder
../../flipperemu ufbt launch           # build it, put it on the Flipper, start it
```

`examples/hello` is a small app to try this with: it draws a few lines, counts the
buttons you press, writes to the log, and crashes when you ask it to.

```
$ ../../flipperemu ufbt launch
        CC      hello.c
        LINK    ~/.ufbt/build/fe_hello_d.elf
        APPCHK  ~/.ufbt/build/fe_hello.fap
                Target: 7, API: 88.2
[INFO] Installing "~/.ufbt/build/fe_hello.fap" to /ext/apps/Examples/fe_hello.fap
[INFO] Closing current app, if any
[INFO] Launching app: /ext/apps/Examples/fe_hello.fap
```

The app is on the screen. From here on it is the loop of any app:

```sh
../../flipperemu log -f -g FeHello     # what it logs (FURI_LOG_I and the others), as it runs
../../flipperemu press up down ok      # buttons, when the window is not at hand
../../flipperemu shot hello.png        # its screen, as a picture
../../flipperemu record hello.gif      # ... and as an animated GIF, until Ctrl-C
../../flipperemu ufbt launch           # after a change: the one that runs is closed,
                                       # the new build started
```

```
$ ../../flipperemu log -n 4 -g FeHello
25620 [I][FeHello] started
26195 [I][FeHello] Up, which makes 1
26615 [I][FeHello] Down, which makes 2
28994 [I][FeHello] left after 2
```

A crash looks as it does on a real Flipper. Hold OK in the example, and:

```
207877 [E][FeHello] a long OK: crashing on purpose

[CRASH][Hello FlipperEmu] fe_hello: on purpose
        r0 : 0
        r1 : 100
        r2 : 20000108
        ...
Rebooting system.
```

The Flipper restarts and says "Flipper crashed and was rebooted" on its screen. The
log goes on in the same place, so the crash is a few lines up in `./flipperemu log`.
`./flipperemu status` says that it started again after a crash, with the words on
that screen, until a button is pressed there. For a message of the firmware's own
those are the message ("furi_check failed"); for an app's own, as here, "Check serial
logs": the firmware keeps a pointer over the restart, and only one into its flash.

That is a release build, as the release channel's packages are. A debug build (the dev
channel's packages, and what `./fbt` builds unless `DEBUG=0` is given) ends its report
with `System halted. Connect debugger for more info` and stands still, as it does on a
real Flipper: the screen stays as it was, and no button and no CLI answers.
`./flipperemu status` has that in its first row, and every command that needs the CLI
says it at once:

```
$ ./flipperemu status
[mine] running: dev-08112026-3060cee9
firmware : it crashed ([NFC] furi_check failed) and has halted: a debug build ...
           `./flipperemu -d mine gdb` shows where it stands, `./flipperemu -d mine ...
usb      : plugged into a computer, which sees 0483:5740 Flipper mine: CDC ACM
```

`./flipperemu gdb` is the debugger it waits for, and `./flipperemu reset` starts it
again. In the window HALTED stands above the screen until then, and Device > Reboot
(Ctrl+R) is the same reset. A window that is opened on a firmware that has halted
already shows it a second later, and one whose command got no answer asks at once. The
emulator notices a halt by itself as well, within half a second: the terminal of a
headless `run` gets a line (`the firmware has halted at 0x080123b0 …`), the window is
told, also of a halt that left nothing in the log, and the CPU rests from then on. The
halt loop does not sleep, on a real Flipper neither, and would keep a core of your
computer busy; a halted firmware now costs less than one that idles (7 % of a core, not
25 %). gdb and `reset` are not slowed by it.

Everything else ufbt does is passed on as it is: `./flipperemu ufbt` alone builds,
`ufbt create APPID=my_app` makes a new app's folder, `ufbt vscode_dist` sets up VS
Code, `ufbt cli` opens the Flipper's command line (Ctrl-] leaves it).

Two more ways to the same place, without ufbt in between:

- `./flipperemu app dist/my_app.fap` takes a `.fap` that is built already
  ([Apps](../README.md#apps)). It reads what the app was built for before it copies it,
  and it closes an app that does not take the loader's request by pressing its Back
  button.
- In the window, a `.fap` dropped on the Flipper is started, and F5 does it again
  after the next build. With Apps > Start it again when its file changes, the build
  is all there is to do: `ufbt` in the editor's terminal, and the new app runs on
  the Flipper a second later.

## A GIF of your app

For the app's README, or a bug report that shows what happens:

```
$ ../../flipperemu record hello.gif --seconds 7 &
[default] recording its screen for 7 s: Ctrl-C ends it
$ for key in up down right ok; do ../../flipperemu press $key; done
[default] 5 pictures, 7.0 s: hello.gif (6384 bytes)
```

![The example app counting four button presses](img/31-record-hello.gif)

*That file: `examples/hello`, and four buttons.*

Without `--seconds` it records until Ctrl-C.

It holds each picture the LCD showed, for as long as it showed it, in the colours of
the lit screen, 512×256 (`--scale 1` to `16`: 1 is the LCD's own 128×64), and it plays
in a loop.

In the window it is Device > Record the screen (Ctrl+Shift+R), and the same once more
to end it and save the file. The emulator shows its screen to one program at a time,
so with the window open the recording is the window's to make, and
`./flipperemu record` says so.

The file is small because a picture after the first holds only the part of the screen
that changed: in the one above the whole screen is 2.9 kB, and each step of the
counter under one. The time is the time you watched it for. An emulated Flipper that
runs slower or faster than a real one (under gdb, or with a host that is busy) is
recorded as it was seen.

## Pause it

```sh
../../flipperemu pause                 # time stands still on it
../../flipperemu shot frame.png        # ... for a look at a screen that is gone in a moment
../../flipperemu continue              # and it goes on from there
```

A paused Flipper is in the middle of whatever it was doing: an animation between two
pictures, a timer that has not run out, a tone that plays. Nothing is lost, and
`continue` goes on from that instant. In the window it is Device > Pause (Ctrl+P),
with "PAUSED" on the glass above the screen.

What needs the firmware's answer says so at once on a paused device, and does not
wait: `ls`, `push`, `app`, `cli`, the `ufbt` and `fbt` targets ("no CLI: it is
paused"), and `press` ("feels no button"). What does not need it works: `shot`,
`status` (which has a line for it, and `"stopped": "paused"` with `--json`), `usb
unplug`, a tag held to it. They take effect when it goes on. `reset` on a paused
device is a reset that starts when it goes on. A button clicked in the window
meanwhile is felt then too.

With a gdb on it the stop is gdb's: `status` says "a debugger has it stopped", the
window says "STOPPED BY A DEBUGGER", and `./flipperemu continue` leaves it to gdb's
`continue`, because a firmware that runs under a gdb that takes it to be stopped is no
help to anyone. `./flipperemu pause` with a gdb attached is gdb's Ctrl-C.

## Back at the same place: a snapshot

```sh
../../flipperemu snapshot save ready   # the Flipper as it stands, your app open
../../flipperemu stop
../../flipperemu snapshot load ready --headless --detach   # there again, in 2 to 3 s
../../flipperemu wait                  # its CLI answers: a new session
```

A test of one screen of an app begins with getting there: the app started, a file
picked, three menus down. A snapshot keeps the Flipper at that place, with its memory,
its screen, its card and what is held to it, and a load is that place again: the same
picture, the same heap, the same file open. A script that begins with a load begins
with the same machine every time, whatever the run before it left on the card.
[The README](../README.md#snapshots) has all of it. For an app's developer:

- **The card comes back too**, with your app's `.fap` as it was at the save, and the
  app that was open at the save is the one in the memory. After a new build, `ufbt
  launch` puts the new one there and starts it, as on any running device.
- **So does the flash.** A snapshot is of one firmware, and a load on a device that
  has another one since brings the old firmware back with it.
- **The firmware does not notice a load.** Its uptime goes on from the save, and its
  timers with it. The calendar is the host's of now: an app that compares dates sees
  the time that went by. A CLI or RPC session that was open at the save is not there
  after it.
- **The machine is the saved run's**: a sensor on the header with the value it had, an
  EEPROM with its contents, a level held on a pin. A load takes no `--i2c` and no
  `--spi` of its own.
- **A snapshot is for the emulator that made it.** After `scripts/setup_qemu.sh` has
  built another, make the snapshots of a test anew (a load says so, and refuses).
- **In the window**: Device > Save a snapshot…, with a name or none (the date and the
  time). `status` and the window's console say which snapshot a run began at.
- **gdb may be on it.** A save while gdb has your app stopped at a breakpoint leaves it
  stopped there, until gdb lets it go on. `snapshot load NAME --gdb --paused` has the
  stub on before the Flipper runs: gdb gets on, sets its breakpoints, and the Flipper
  goes on from the save's moment when gdb lets it.

## The LED and the backlight

```
$ ../../flipperemu status
…
lights   : the LED: blinks cyan (00ffff); the backlight: 255 of 255
$ ../../flipperemu status --json
…  "lights": {"led": "00ffff", "blinking": true, "backlight": 255,
              "engines": "hold@0=255 run@4=0 off"},
```

Both hang on one chip, an LP5562, which has three small engines that run programs of
their own. The firmware gives them what would cost it a timer: the fade of the
backlight (engine 1) and the blink of a notification sequence such as
`sequence_blink_start_cyan` (engine 2). The emulator runs those programs on the chip's
clock, so a sequence of yours looks in the window as it does on a Flipper: the
backlight fades over an eighth of a second, and a blink is 10 ms of light in every 90.

- `led` is the LED's colour as `rrggbb`; while it blinks, the colour of the flashes,
  so that a look between two of them does not say "off". `blinking` says which it is.
- `backlight` is 0 to 255. After a fade it is where the chip's ramp ended, which is
  one or two above what the firmware asked for unless that was 255.
- `engines` has each of the three: `off`, `load` (the firmware writes its program),
  `direct`, or what it does, the command it is at, and its PWM: `run@4=0`,
  `hold@0=255`, `wait@2=0` (for a trigger from another engine).

A recording (`record`) is of the screen, without the lights. The window shows both.

## The pins of the GPIO header

An app that reads a pin needs something on the pin, and an app that drives one needs
somebody to look. `gpio` is both:

```
$ ../../flipperemu gpio
[flipper] the GPIO header (1 is +5V, 9 is 3V3; 8, 11 and 18 are GND)
   2  PA7   output                                   high
   3  PA6   input                                    floating
   4  PA4   analog                                   floating
…
$ ../../flipperemu gpio PA6 high
[flipper] PA6 (pin 3 of the header): held high from outside; it is high (the firmware has it as: input)
$ ../../flipperemu gpio PA7
high
```

The first column is the pin's number on the header, the third what the firmware has
the pin as (what your app's `furi_hal_gpio_init` made of it), the fourth its level.
A pin is named as the MCU has it (`PA7`), as the case has it (`A7`) or by that number.

- **Your app drives a pin:** `gpio PA7` prints `high`, `low`, `floating`, `pwm` or
  `unknown` and nothing else, for a script; `gpio PA7 --wait high --timeout 5` waits for it and
  fails after the five seconds, with what the pin was instead. It looks twenty times a
  second, so it is for a level that stays, not for a pulse.
- **Your app reads a pin:** `gpio PA6 high` and `gpio PA6 low` hold it from outside as
  a wire to 3V3 or to GND would, and `gpio PA6 free` lets go (`gpio free`: of all).
  The firmware's own `gpio read PA6` in the CLI is a quick look from the inside.
- **Your app has an interrupt on a pin:** the table says so beside the pin
  (`input, pull-up, interrupt when it falls`), and `gpio PA4 low` is the edge: your
  handler runs then, once. Not every pin has an interrupt to give. A line is a pin
  number's, and the firmware's keys have lines 3, 6 and 10 to 13: on pins 3, 5 and 7
  (PA6, PB3, PC3) `furi_hal_gpio_add_int_callback` ends in `furi_check failed`, and
  on pin 2 (PA7) too while the firmware waits for an expansion module on pins 13 and
  14. Pins 4, 6, 15 and 16 (PA4, PB2, PC1, PC0) are the ones to take; PB2 gets its
  rising edge too, whatever you asked for, a leftover of the NFC chip's interrupt on
  the same line. (The README has more on the lines.)
- **Your app shares a line with something outside** (an open-drain output): the pin
  is low while your app pulls it low, and when it lets go, the pin has what is held
  to it, or its pull-up. `furi_hal_gpio_read` gives the line, not what you wrote.
- A reset of the firmware leaves what is held, as it would leave a wire. A pin your
  app drives as a push-pull output keeps the app's level while it does (`gpio` says
  that something is held to it, and that it does not show).
- **Your app measures a voltage:** `gpio PA7 1.25V` holds one to a pin, as a sensor's
  output would be, and `furi_hal_adc_read` gives what an ideal converter makes of it:
  2500 of 4095 with the firmware's usual reference of 2.048 V, the same number every
  time, so a test can ask for that number. Six pins are the ADC's: 2, 3, 4, 7, 15 and
  16 (PA7, PA6, PA4, PC3, PC1, PC0). `gpio`'s last line has what the ADC converted
  last (the pin, the voltage on it, the number), which says whether your app measured
  the pin you think it did, and whether it turned the ADC off again
  (`furi_hal_adc_release`). A pin that nothing holds measures as 0 V. The chip's own
  three inputs are there too (its reference, its temperature at 25 degrees, the
  battery pin at 3.3 V), with calibration values that make the firmware's
  `furi_hal_adc_convert_*` come out at those.
- **Your app makes a PWM:** the firmware's `furi_hal_pwm_start` has two outputs, pin
  2 (PA7, from TIM1) and pin 4 (PA4, from LPTIM2). `gpio` has such a pin as `pwm`,
  with the timer, the frequency and the part of the time that the pin is high, all
  worked out from the timer's registers as your app left them: `PWM from TIM1: 1 kHz,
  high 50 % of the time`. `gpio PA7 --wait pwm` waits for one, and `status --json`
  has the numbers themselves for a test (the timer's clock, its divider, the ticks of
  a period and those of them that the pin is high for). That is what the timer makes,
  which is not always what was asked for: 10 kHz at 30 % on pin 4 comes out as 9.998
  kHz at 30.01 %, since the firmware sets LPTIM2 one tick long. The pin has to be the
  timer's too: a timer that runs while the pin is something else is no PWM in the
  table, and neither is a pin that waits for a timer. A record has no edges of a PWM,
  only an `x` for as long as it is one.
- What another peripheral puts on a pin (a UART, SPI, I2C, a timer other than those
  two outputs) is `unknown`: only the levels
  of plain inputs and outputs are on the header so far. (The
  bytes of the UART on pins 15 and 16 are there, as bytes: the next section. And so
  are devices for the I2C bus on those pins, and for the SPI bus on pins 2, 3 and 5:
  the two after it.)
- **What your app did to a pin, edge by edge:** `gpio record pins.vcd` has the
  emulator write every change of a pin's level to a file, with its time, until `gpio
  record stop`; a pulse too short for `--wait` is in it. The file is a VCD, which
  GTKWave and PulseView show as a logic analyzer's capture. `gpio watch PA7` prints
  the changes as they happen instead (`--for 5`: for five seconds), each with how long
  the pin had the level before. The times are the emulator's clock: right for the
  order of things and for milliseconds, some microseconds off for a pulse that short
  (the README has what was measured).
- **By hand, in the window:** the pins on top of the drawn Flipper light with their
  level (amber for high, blue for low), so an app that drives one is seen driving it.
  A click on a pin holds it high or low, or lets go of it; the GPIO menu has the list.
  It is the same `gpio` command underneath, and what it answered is in the console.
  GPIO > Record what the pins do… is `gpio record` with a file dialog, and the status
  bar counts the changes while it runs.

In a test script (see below) that is two lines:

```sh
fe gpio PA6 low                       # the "button" on the header is down
fe gpio PA7 --wait high --timeout 5   # ... and the app lit its LED
```

## A serial port on the header

An app for a GPS receiver, an ESP32 board or a serial adapter opens LPUART1, pins 15
(TX) and 16 (RX) of the header: `furi_hal_serial_control_acquire(FuriHalSerialIdLpuart)`,
`furi_hal_serial_init`, `furi_hal_serial_tx`, and `furi_hal_serial_async_rx_start` for
what comes in. `uart` is what is wired there:

```
$ ../../flipperemu uart wait "ready" --for 10
ready
$ ../../flipperemu uart send "hello\n" --expect "you said: hello"
you said: hello
```

- `uart send TEXT` writes to the app (`\r`, `\n`, `\xNN` as in C; `--hex "55 aa"` for
  bytes), and is back when the firmware has taken it. With `--expect TEXT` it then
  waits for that in the app's answer and prints what came; after five seconds
  (`--for S`) it fails, with what came instead.
- The app gets the bytes at the baud rate it set, a frame's time for each: a
  thousand of them take a second at 9600 baud. `uart send` waits that long, and
  gives up only when nothing has moved for `--for S`. After the last byte the UART's
  idle interrupt comes, as on the chip.
- `uart wait TEXT` waits for what the app sends by itself. It reads on from where
  the wait before it found its text, so a script that looks late misses nothing.
- `uart term` is a terminal on the port, to try a protocol by hand (Ctrl-] leaves).
- The program that will talk to the real board can talk to this one: the port is
  also a pty, `devices/NAME/lpuart.tty`, and that program and `uart send` can be on
  it at once. `run --lpuart tcp:PORT` makes it a TCP port instead.
- What the app sent is in `devices/NAME/lpuart.log`, whether or not anything
  listened. What is sent to an app that has not opened the port is lost, as it is
  on a wire.
- An app that opens the other port, USART1 on pins 13 (TX) and 14 (RX)
  (`FuriHalSerialIdUsart`, the default of most GPS and ESP32 apps), gets the same
  with `--port usart`: `uart send --port usart "AT\r\n" --expect OK`. The firmware
  logs on those pins until the app has the port and again when it has closed it,
  so the app's bytes are in `uart.log` among the log's lines, and `uart term
  --port usart` shows both.
- **Has the app the port, and at which speed?** `uart` alone says for each port what
  the firmware has it as: `off (nothing has opened it)`, or `9600 baud, 8N1; what
  comes in is read (by interrupt)`. `gpio`, `status` and the window's GPIO menu have
  the same line, and the window's console says when an app opens a port and when it
  closes it. An app that took the other port, or set another rate than the board's,
  is found there. A `uart send` that nobody hears still exits 0, as a wire would
  have it, and says so on stderr.

The example needs no build: `examples/uart/echo.js` is a script for the firmware's
`js` command that opens the port and answers every line (the README's "The serial
port on pins 15 and 16" has the session). What ran here is that script, on five
firmwares; the JavaScript `serial` module is the calls above. Reception by DMA
(`furi_hal_serial_dma_rx_start`) works too: the firmware's USB-UART bridge uses it,
and passes bytes both ways here. Three limits matter for an app. A wrong baud rate
is not found: your side of the wire has no rate, so the app is understood at any
(`uart` says which one it set).
What the app sends is there at once, not at its baud rate. And a byte the app does
not read waits on the wire, where a real UART would lose the next one and set its
overrun flag.

In a test script:

```sh
fe uart wait "ready" --for 10                    # the app has opened the port
fe uart send "AT+VERSION\r\n" --expect "OK"      # ... and answers
```

## A sensor on the header's I2C bus

An app for a sensor board talks to I2C3, SDA on pin 15 and SCL on pin 16:
`furi_hal_i2c_acquire(&furi_hal_i2c_handle_external)`, then
`furi_hal_i2c_is_device_ready`, `furi_hal_i2c_tx`, `furi_hal_i2c_rx` and
`furi_hal_i2c_trx`, and `furi_hal_i2c_release`. `run --i2c` is the board, and `i2c`
the hand that warms the sensor:

```
$ ../../flipperemu run --detach --i2c tmp105@0x48,temperature=23.5 --i2c eeprom@0x50
$ ../../flipperemu cli i2c
…
4 | - - - - - - - - # - - - - - - -
5 | # - - - - - - - - - - - - - - -
…
$ ../../flipperemu i2c 0x48 temperature 30.25
[flipper] tmp105 at 0x48: temperature 30.25 °C
$ ../../flipperemu i2c
[flipper] pins 15 and 16 (I2C3): tmp105 at 0x48, at24c-eeprom at 0x50; the firmware called 128 addresses, 2 answered; the last: 0x7f to write, no answer
[flipper]   0x48  tmp105        temperature 30.25 °C
[flipper]   0x50  at24c-eeprom  address-size 1, kept in /home/you/FlipperEmu/devices/flipper/i2c-50.bin, rom-size 256, writable on
```

- **The device** is one of QEMU's I2C devices by its name there, at its address in
  hex (seven bits): a `tmp105` or `tmp421` temperature sensor, an `eeprom`, a
  `ds1338` clock, a `pca9554` port expander and some more (`run --i2c help` lists
  them, with a few words on each). Your chip may not be among them. One with the
  same registers can stand in, and what the app does when nothing answers is there
  in any case: an address with no device is a NACK, as on the wire.
- **Did the app reach it?** The first line of `i2c` counts the addresses that the
  firmware called and those that answered, and has the last one. The `furi_hal_i2c`
  functions take an address as it is on the wire, shifted left by one: `0x48 << 1`.
  An app that passes 0x48 calls 0x24, and the line says so: `the last: 0x24 to
  write, no answer`, with a line beneath it that names the sensor at 0x48 and the
  address to pass (`0x48 << 1 (0x90)`).
- **What did it send?** `i2c record bus.txt` before the app's run and `i2c record
  stop` after it: the file has a line for each START, with its time, the address,
  `w` or `r` and the bytes (`1.281528054 0x48 w 01 60`: the sensor set to 12 bits;
  `1.284154074 0x48 r 17 80`: 23.5 °C read). A test compares it with what the app
  should have sent: `grep -v '^#' bus.txt | cut -d' ' -f2-` is the transfers
  without their times, the same on every run. `0x24 w no answer` is a call to
  nobody, `hh!` a byte that the device refused, and a `+` at a line's end a
  transfer that went on with no STOP: a repeated START, which an app makes with
  `furi_hal_i2c_tx_ext(…, FuriHalI2cBeginStart, FuriHalI2cEndAwaitRestart, …)` and
  after it `furi_hal_i2c_rx_ext(…, FuriHalI2cBeginRestart, FuriHalI2cEndStop, …)`.
  The firmware's `furi_hal_i2c_trx` and the JavaScript `writeRead` are a write
  with its STOP and then a read, two lines with no `+`. The record is the place to
  look for a refused byte: in the emulator the firmware's write was true when the
  device refused its last byte, and false only when it refused one before the
  last (a real Flipper was not asked). `i2c watch` in a second terminal prints the
  same while the app runs, with the device's type beside each address, and `i2c
  watch 0x50` only the calls to the EEPROM. In a test's background it is ended with
  `kill`, and says its count as it would after Ctrl+C.
- **Another value, for a test:** `i2c 0x48 temperature -40` is a winter that the
  desk does not have; the app's next read has it. `i2c 0x48 temperature` prints the
  value and nothing else. What QEMU's model does not take is refused in words
  (a TMP105's range ends below 128 °C).
- **By hand,** the window's GPIO menu has the bus and each device with its values,
  and a sensor's temperature is set there (`set it…`). The console says when the
  firmware has talked on the bus and how many of its calls were answered: an app
  that calls the wrong address is a line there (`the firmware called 2 addresses,
  none answered; the last: 0x24 to write, no answer`) without a look at `i2c`, and
  under it the word on the shift when a device is at twice that address (`nobody
  is at 0x24. If the tmp105 at 0x48 was meant: …`). GPIO > Record what goes over
  the I2C bus… is `i2c record` with a file dialog: the status bar counts the
  addresses called until GPIO > End the record of the I2C bus.
- **For a program,** `i2c --json` is the bus and every device with its values (a
  temperature in °C; an address as a number, 72 for 0x48).
- **The EEPROM** keeps its contents in `devices/NAME/i2c-50.bin`, over a reset and
  over `stop` and the next `run`: a test can compare the file with what the app
  should have written, or copy a prepared image there before `run`. A new one is
  all 0xFF.
- What is on the bus is said with each `run`. A device cannot be added while the
  emulator runs.
- **In JavaScript** it is `require("i2c")` on Momentum, Unleashed and RogueMaster:
  `examples/i2c/sensor.js` reads the sensor and counts its runs in the EEPROM. The
  official firmware's JavaScript has no such module, so there it is an app in C.

`examples/i2c/sensor.c` is that app. It sets the TMP105 to 12 bits, reads its
temperature once a second and shows it, and counts its runs in the EEPROM's first
byte. It has the bus for the length of a transfer and no longer:

```c
furi_hal_i2c_acquire(bus);                      // &furi_hal_i2c_handle_external
if(furi_hal_i2c_tx(bus, SENSOR, setup, sizeof(setup), WAIT) &&
   furi_hal_i2c_trx(bus, SENSOR, &temperature, 1, raw, sizeof(raw), WAIT)) {
    hundredths = (int16_t)(raw[0] << 8 | raw[1]) * 100 / 256;
}
furi_hal_i2c_release(bus);
```

`SENSOR` is `0x48 << 1`. A transfer that nobody answers comes back false within
`WAIT` milliseconds, and the app then says "does not answer" where the value was:
start the Flipper with `--i2c eeprom@0x50` alone to see that.

```
$ cd examples/i2c
$ ../../flipperemu ufbt launch
…
[INFO] Launching app: /ext/apps/Examples/fe_sensor.fap
$ ../../flipperemu log -g FeSensor
12876 [I][FeSensor] started, run number 1 by the EEPROM at 0x50
12882 [I][FeSensor] 23.50 C at 0x48
```

`examples/i2c/test.sh` is its test, of the kind that the next section goes through
with `examples/hello`. The board is part of the test: the Flipper it starts has the
two devices, and one of its steps is the weather.

```sh
fe() { ../../flipperemu -d "${device:-sensor-test}" "$@"; }

fe run --detach --headless --audio none -f "$1" \
    --i2c tmp105@0x48,temperature=23.5 --i2c eeprom@0x50
trap 'fe stop' EXIT

fe wait                                           # the firmware is up
fe wait --log 'FeSensor.*started, run number [0-9]' -- ufbt launch
fe wait --log 'FeSensor.* 23\.50 C'               # what the sensor started with
fe wait --screen screens/warm.txt
fe wait --log 'FeSensor.* -7\.50 C' -- i2c 0x48 temperature -7.5
fe wait --screen screens/cold.txt                 # ... and the screen follows
fe wait --log 'FeSensor.*left' -- press back
echo "test.sh: the app reads what the sensor has"
```

```
$ ./test.sh ../../firmware/official-1.5.1/flipper-z-f7-update-1.5.1.tgz
…                                      (the emulator's lines as it starts, and ufbt's)
4485 [I][FeSensor] started, run number 2 by the EEPROM at 0x50
4489 [I][FeSensor] 23.50 C at 0x48
[sensor-test] its screen is as in screens/warm.txt (0.1 s)
[sensor-test] tmp105 at 0x48: temperature -7.5 °C
6492 [I][FeSensor] -7.50 C at 0x48
[sensor-test] its screen is as in screens/cold.txt (0.1 s)
7726 [I][FeSensor] left
test.sh: the app reads what the sensor has
[sensor-test] stopped
```

It was the second run on that device, and the EEPROM had counted the first: its
file stays with `devices/sensor-test`. `rm -r devices/sensor-test` is a new chip.

What ran here is the firmware's own scan (the CLI's `i2c`) on five firmwares, that
script on the three that have the module, and this app on the official 1.5.1. The
app was not built for the other four: each wants its own SDK (see "The SDK has to
be the firmware's"), and the ufbt here has the one of 1.5.1. Three limits matter
for an app. Nothing checks the pins: I2C3 answers here also when the app did not
set pins 15 and 16 up for it, where a Flipper would be silent. A transfer takes no
time, at any bus speed, and no device stretches the clock. And the two lines are
not on the header as levels: a record of the pins has no bits of a transfer.

In a test script:

```sh
fe run --detach --headless --i2c tmp105@0x48,temperature=23.5
fe i2c 0x48 temperature -7.5                 # the sensor says -7.5 °C from here on
test "$(fe i2c 0x48 temperature)" = -7.5
```

## A flash on the header's SPI bus

An app for a board with a serial flash talks to SPI1, MOSI on pin 2, MISO on pin 3
and SCK on pin 5, and has one more pin as the chip select. The firmware's handle for
that is `furi_hal_spi_bus_handle_external`, with the chip select on pin 4 (A4), at 2
MHz in mode 0: `furi_hal_spi_bus_handle_init` once, then `furi_hal_spi_acquire`,
`furi_hal_spi_bus_tx`, `furi_hal_spi_bus_rx` and `furi_hal_spi_release` for each
transfer, and `furi_hal_spi_bus_handle_deinit` at the app's end. `run --spi` is the
board, and `spi` says what reached it. With these lines in an app:

```c
#include <furi_hal_spi.h>
#include <furi_hal_spi_config.h>

const FuriHalSpiBusHandle* flash = &furi_hal_spi_bus_handle_external;
const uint8_t ask = 0x9f;
uint8_t id[3] = {0};                     // rx sends what its buffer has: zeros

furi_hal_spi_bus_handle_init(flash);     // pin 4 an output, high
furi_hal_spi_acquire(flash);             // pins 2, 3 and 5 the bus's, pin 4 low
furi_hal_spi_bus_tx(flash, &ask, 1, 100);
furi_hal_spi_bus_rx(flash, id, 3, 100);  // ef 40 16
furi_hal_spi_release(flash);             // pin 4 high, the three pins analog
furi_hal_spi_bus_handle_deinit(flash);   // pin 4 analog

FURI_LOG_I("FeFlash", "JEDEC ID: %02x %02x %02x", id[0], id[1], id[2]);
```

```
$ ../../flipperemu run --detach --spi flash
$ ../../flipperemu ufbt launch
…
$ ../../flipperemu log -g FeFlash
13522 [I][FeFlash] JEDEC ID: ef 40 16
$ ../../flipperemu spi
[flipper] pins 2, 3 and 5 (SPI1): w25q32 with its chip select on pin 4; the firmware clocked 4 bytes there, all with a device selected; the last at 2 MHz, mode 0, MSB first, 8 bits
[flipper]   pin 4  (PA4)  w25q32  not selected; its contents are kept in /home/you/FlipperEmu/devices/flipper/spi-4.bin
```

- **The device** is one of QEMU's serial flashes by its name there, and after the
  `@` the pin of the header that is its chip select: `--spi w25q32@PA4`, or `@4`
  by the pin's number, or `@A4` as on the case. `flash` alone is a W25Q32 on pin
  4, where the firmware's handle has it. `run --spi help` lists the parts, more
  than a hundred of them, and the pins that a chip select may be. Tried here are the
  W25Q32 and the W25Q80BL. A part that is not among them has one of its maker's
  family to stand in; the JEDEC ID is then that part's.
- **When it does not answer,** the first line of `spi` is where to look: it counts
  the bytes that the firmware clocked on the header, and how many of them a device
  heard. The same app with the flash's chip select on pin 7 (`--spi w25q32@PC3`),
  as on a board that has it wired to C3:

  ```
  $ ../../flipperemu log -g FeFlash
  4355 [I][FeFlash] JEDEC ID: 00 00 00
  $ ../../flipperemu spi
  [flipper] pins 2, 3 and 5 (SPI1): w25q32 with its chip select on pin 7; the firmware clocked 4 bytes there, none with a device selected; the last at 2 MHz, mode 0, MSB first, 8 bits
  [flipper]   pin 7  (PC3)  w25q32  not selected; its contents are kept in /home/you/FlipperEmu/devices/flipper/spi-7.bin
  ```

  `none with a device selected`: the bus works, and the pin that the app pulls low
  is not the one that the flash is on. `the firmware has clocked nothing there
  yet`: the app never had the bus on those pins. `all with a device selected`, and
  the answer is still wrong: the bytes themselves, which the app's log should
  have. The `00 00 00` is MISO's pull: the firmware's handle pulls pin 3 down, and
  with nobody selected nothing else has it.
- **`furi_hal_spi_bus_rx` sends while it reads,** and what it sends is what the
  buffer has: the firmware's function is `furi_hal_spi_bus_trx` with one buffer
  for both ways. A flash does not listen while it answers. A chip that does gets
  the last answer back, so zero the buffer first, or use `furi_hal_spi_bus_trx`
  with a buffer of what to send.
- **Another chip select, speed or mode** is a handle of the app's own: a
  `FuriHalSpiBusHandle` on `furi_hal_spi_bus_r` with a callback that makes the
  chip select an output on Init and takes the bus and the three pins on Activate,
  as the firmware's own in `targets/f7/furi_hal/furi_hal_spi_config.c` do.
  `scripts/spi_check_app/spi.c` has such a handle, with its chip select on pin 7
  at 4 MHz in mode 3, and `spi` then says `the last at 4 MHz, mode 3`. One thing
  to know with two handles: a handle's de-initialisation makes its chip select
  analog. Two handles with one chip select, and one of them de-initialised while
  the other is still used, select nothing from then on.
- **From outside:** `gpio PC3 low` holds pin 7 low, as a chip select that is wired
  to GND, and the device there is selected for as long. `gpio PA6 high` holds
  MISO, which is what the firmware reads when nobody is selected. While an app
  has the bus, `gpio` has pins 2, 3 and 5 as the bus's (alternate function 5) and
  says beside the chip select's pin that it selects now.
- **For a program,** `spi --json` is the bus with its counts (`heard`, `unheard`),
  how the last byte was clocked (`last`) and every device, each with its pin,
  whether it is selected, and its file.
- **The flash's contents** are a file, `devices/NAME/spi-4.bin` for the flash on
  pin 4, as large as the part and 0xFF throughout when new. It is written as the
  firmware writes, and kept over a reset, over `stop` and the next `run`. A test
  compares it with what the app should have written (`od -A x -t x1z -j 0x1000 -N
  16 devices/NAME/spi-4.bin` is a look at sixteen bytes from 0x1000), or puts
  bytes there before `run`, for the app to find. Remove the file for an empty
  flash.
- What is on the bus is said with each `run`. A device cannot be added while the
  emulator runs.
- **By hand,** the window's GPIO menu has the bus's line and each device with its
  chip select's pin, whether it is selected, and its file. The console says when the
  firmware has clocked bytes there and whether a device heard them: an app with the
  wrong chip select is a line there (`the firmware clocked 4 bytes there, none with
  a device selected; …`) without a look at `spi`. One line for a run of transfers,
  and the next only when the bytes are heard differently or after ten seconds of
  silence.
- **In JavaScript** it is `require("spi")` on Momentum, Unleashed and RogueMaster,
  which is the firmware's handle: `examples/spi/flash.js` reads the flash's IDs,
  erases a sector and writes seven bytes there (the README has its run). The
  official firmware's JavaScript has no such module, so there it is an app in C.

In a test script:

```sh
fe run --detach --headless --spi flash
fe wait
fe wait --log 'FeFlash.*JEDEC ID: ef 40 16' -- ufbt launch     # the app's own word
fe spi --json | grep -q '"unheard": 0'                         # no byte went to nobody
fe stop

# what the flash has before the app's next run: eight bytes at address 0
printf 'Flipper!' | dd of=../../devices/NAME/spi-4.bin bs=1 seek=0 conv=notrunc
fe run --detach --headless --spi flash
```

Those lines ran here with an app of the snippet above and one more read (03h from
address 0), which logged `46 6c 69 70 70 65 72 21` after the second start. There
is no example app in C for this bus yet: what ran in C is that snippet and the
check's own app, both on the official 1.5.1, and the script on the three firmwares
that have the module. Momentum's own example for the bus (`Examples/spi.js` on its
card, written for a W25Q32 on these pins) was run once by hand and found the
flash: `Found Winbond device`, `Device ID: W25Q32`. Four limits matter for an app.
A transfer takes no time, at any speed, and the flash answers in any mode, where a
real one wants mode 0 or 3. While a selected flash takes a command it answers 00
here; on hardware it leaves MISO alone, and the app reads the pin's pull. The
three pins are not on the header as levels: a record of the pins has no bits of a
transfer. And no real Flipper with a flash on its header was asked.

## A test that runs by itself

A test of an app is the loop above with nobody at the keys: a Flipper, the app on it,
some buttons, and a look at what came of them. Two things make a script of that.
`wait` holds the script until the Flipper is where the next step needs it, and
`shot --text` is its screen as text, which a script can compare and a repository can
keep.

`examples/hello/test.sh` is that for the example app:

```sh
fe() { ../../flipperemu -d "${device:-hello-test}" "$@"; }

fe run --detach --headless --audio none -f "$1"    # a Flipper, running by itself
trap 'fe stop' EXIT

fe wait                                           # the firmware is up
fe wait --log 'FeHello.*started' -- ufbt launch   # build, install, start: its line
fe wait --screen screens/start.txt                # ... and its first screen
fe wait --log 'which makes 2' -- press up down
fe wait --screen screens/two.txt
fe wait --log 'left after 2' -- press back
echo "test.sh: the app does what it should"
```

```
$ ./test.sh ../../firmware/official-1.5.1/flipper-z-f7-update-1.5.1.tgz
…                                      (the emulator's lines as it starts)
[hello-test] it runs on by itself, until `./flipperemu -d hello-test stop`  (…)
[hello-test] its CLI answers (5.4 s)
…                                      (ufbt's)
8519 [I][FeHello] started
[hello-test] its screen is as in screens/start.txt (0.1 s)
10129 [I][FeHello] Down, which makes 2
[hello-test] its screen is as in screens/two.txt (0.1 s)
11509 [I][FeHello] left after 2
test.sh: the app does what it should
[hello-test] stopped
```

It runs in the app's folder, as `ufbt` does, on a Flipper it makes for itself
(`devices/hello-test`, or the name given after the firmware), and takes a quarter of
a minute once the app is built. The two screens it compares with were taken on the
official firmware: one that draws its text in another font needs its own.

`run --detach` starts the Flipper and comes back to the script: with 0 when the
emulator is up, which takes three seconds for a new device (its card made, its
firmware flashed), and with what went wrong when it never came up. For a firmware
that is not there that is one line, at once, and under `sh -e` the end of the test:

```
$ ./test.sh no-such.tgz
no-such.tgz: No such file or directory
```

The firmware has not started by then, which is what the `wait` after it is for. The
emulator runs on in the background until `stop`, which the `trap` sees to however
the script ends. What it prints from then on is in `devices/NAME/run.log`, and the
firmware's log is where it always is (`flipperemu log`). A plain `run … &` does as
well, but it tells of a wrong firmware only to a `wait` that runs out a minute later.

Each `wait` ends with 0 when what it waits for is there, and with 1 and the reason
when it is not there in time (`--timeout S`, 60 seconds unless told), which under
`sh -e` ends the script with that status.

- **`wait`** alone: until the firmware's CLI answers. It has started then, and
  `ufbt launch`, `push`, `app` and the rest get their answers. It also waits for the
  emulator itself, so it may follow a `run … &` directly.
- **`wait --log REGEX`**: until the firmware's log has a line with it, which is
  printed. Capitals matter only if the expression has any, as with `log -g`.
- **`wait --screen FILE`**: until the screen is as in the file, pixel for pixel.

With a command after `--`, that command of the launcher's is run first and the wait
is for what comes of it: the time counts from its end, and of the log only what was
written since it began. That is the form for a step of a test. Without a command,
`wait --log` reads the log from the firmware's start, which is right for "has it
come up", and wrong where the line of an earlier step may already stand there. A
command that fails ends the `wait` with that command's exit status, and nothing is
waited for.

### The screen as text

```
$ ../../flipperemu shot --text



                      ██  ██       ██ ██             ██▀▀▀ ██ ▀▀                       ██▀▀▀
                      ██▄▄██ ▄█▀█▄ ██ ██ ▄█▀█▄       ██▄▄  ██ ██ █▄▀█▄ █▄▀█▄ ▄█▀█▄ █▄█▀██▄▄▄ █▄▀█▄▀█▄ ██ ██
                      ██  ██ ██▀▀▀ ██ ██ ██ ██       ██    ██ ██ ██ ██ ██ ██ ██▀▀▀ ██  ██    ██ ██ ██ ██ ██
                      ██  ██ ▀█▄█▀ ██ ██ ▀█▄█▀ ██    ██    ██ ██ ██▄█▀ ██▄█▀ ▀█▄█▀ ██  ██▄▄▄ ██ ██ ██ ▀█▄▀█
                                               ▄▀                ██    ██





               ▄▀▀▀▄                                 █       █  █           █           █
               █ ▄▀█   █▀▀▄ █▀▀ ▄▀▀▄ ▄▀▀ ▄▀▀ ▄▀▀▄ ▄▀▀█      ▀█▀ █▀▀▄ ▄▀▀▄   █ ▄▀▀█ ▄▀▀ ▀█▀ ▀   █▀▀▄ ▄▀▀▄ █▀▀▄ ▄▀▀▄
               █▀  █   █  █ █   █▀▀▀  ▀▄  ▀▄ █▀▀▀ █  █  ▄    █  █  █ █▀▀▀   █ █  █  ▀▄  █  ▄   █  █ █  █ █  █ █▀▀▀
                ▀▀▀    █▀▀  ▀    ▀▀  ▀▀  ▀▀   ▀▀   ▀▀▀ ▄▀    ▀  ▀  ▀  ▀▀    ▀  ▀▀▀ ▀▀   ▀      ▀  ▀  ▀▀  ▀  ▀  ▀▀
                       ▀






     █▀▀▀▄           █      █                              █                  ▄▀▀▀▄ █  ▄▀                     █
     █▄▄▄▀ ▄▀▀█ ▄▀▀▄ █ ▄▀   █ ▄▀▀▄ ▄▀▀█ █  █ ▄▀▀▄ ▄▀▀      █ ▄▀▀▄ █▀▀▄ ▄▀▀█   █   █ █▄▀     ▄▀▀▄ █▀▀ ▄▀▀█ ▄▀▀ █▀▀▄ ▄▀▀▄ ▄▀▀
     █   █ █  █ █  ▄ █▀▄    █ █▀▀▀ █  █ █  █ █▀▀▀  ▀▄  ▄   █ █  █ █  █ █  █   █   █ █ ▀▄    █  ▄ █   █  █  ▀▄ █  █ █▀▀▀  ▀▄
     ▀▀▀▀   ▀▀▀  ▀▀  ▀  ▀   ▀  ▀▀   ▀▀▀  ▀▀   ▀▀  ▀▀  ▄▀   ▀  ▀▀  ▀  ▀  ▀▀█    ▀▀▀  ▀   ▀    ▀▀  ▀    ▀▀▀ ▀▀  ▀  ▀  ▀▀  ▀▀
                                                                        ▀▀
```

Two pixels to a character, one above the other: `▀` is the upper one, `▄` the lower,
`█` both, and a blank neither. That makes 32 lines of up to 128 characters; the
blanks at a line's end are left out. `shot --text FILE`, or `shot` with a name that
ends in `.txt`, writes it to a file.

As a reference such a file has advantages over a picture: `git diff` shows what
changed on the screen, and an editor changes it. In a reference a `?` stands for two
pixels that may be anything: a clock, the battery, a number that counts. A picture
from `shot` is taken as a reference too (a PNG needs Pillow to be read), and
`scripts/fe_screen.py PICTURE` prints one as text, or with a reference after it the
number of pixels that differ.

To make a reference, bring the app to the screen by hand and take the shot. For a
screen that moves, `pause` first and put `?` where it moves. The desktop is a poor
thing to wait for: its animation takes six seconds and more to come round (seven
pictures on the official 1.5.1), and the firmware picks another one from time to
time. Wait for a screen of your app's.

### When a wait fails

It says what was there instead, on stderr:

```
$ ../../flipperemu -d hello-test wait --log 'which makes 9' --timeout 3
[hello-test] no line with 'which makes 9' in the firmware's log since it started, in 3.0 s.
[hello-test] its last line: 6825 [I][FeHello] Down, which makes 2
$ ../../flipperemu -d hello-test wait --screen screens/start.txt --timeout 3
[hello-test] its screen is not as in screens/start.txt after 3.2 s: 317 pixels differ.
[hello-test] it shows:
…
             ▄▀▀▀▄                                 █       █  █           █           █      █▀▀▀▄
                ▄▀   █▀▀▄ █▀▀ ▄▀▀▄ ▄▀▀ ▄▀▀ ▄▀▀▄ ▄▀▀█      ▀█▀ █▀▀▄ ▄▀▀▄   █ ▄▀▀█ ▄▀▀ ▀█▀ ▀   █   █ ▄▀▀▄ █  █  █ █▀▀▄
              ▄▀     █  █ █   █▀▀▀  ▀▄  ▀▄ █▀▀▀ █  █  ▄    █  █  █ █▀▀▀   █ █  █  ▀▄  █  ▄   █   █ █  █ █  █  █ █  █
             ▀▀▀▀▀   █▀▀  ▀    ▀▀  ▀▀  ▀▀   ▀▀   ▀▀▀ ▄▀    ▀  ▀  ▀  ▀▀    ▀  ▀▀▀ ▀▀   ▀      ▀▀▀▀   ▀▀   ▀▀ ▀▀  ▀  ▀
…
```

The firmware itself may be the reason, and then the message has it:

- **It crashed and started again** (a release build): "The firmware started again
  after a crash: its screen says "…" until a button is pressed there", with the
  crash's own message where the firmware kept one. A wait for a log line goes on to
  its timeout all the same, because the line may still come.
- **It crashed and halted** (a debug build): the wait ends in a second or two, with
  the crash, and with `gdb` and `reset` as what can be done.
- **It is paused**, or a debugger has it stopped: "Mind that it is paused".
- **The emulator ended**: "It is not running any more", without waiting further.

## Why `./flipperemu ufbt`, and not `ufbt` alone

**Because the tools go for a real Flipper first.** `ufbt launch` looks for its Flipper
among the serial ports of the computer, by name. One that is plugged in is found, and
the app is installed on it: the emulated one is not on that list. The tools take a
port by name only when they are told one, with `FLIP_PORT=`.

`./flipperemu ufbt …` runs your `ufbt` with exactly that added: the emulated Flipper's
USB serial port (`devices/NAME/usb.tty`). `-d NAME` before `ufbt` picks another device
than `default`. It also

- holds the port while a tool talks on it, so that a `./flipperemu push` in another
  terminal waits for its turn, or says who has it;
- says so when the device does not run, before the tool goes looking for a port;
- refuses the targets that go through a debug probe (`flash`, `debug`, `blackmagic`,
  `jflash`, …). They would reach the real Flipper on your desk, or nothing.
  `./flipperemu flash` and `./flipperemu gdb` are what does that here;
- does `ufbt flash_usb` itself: the SDK's update package has a radio stack in it, which
  the emulated Flipper cannot install, so the launcher installs that package without
  the stage (`./flipperemu update`). See [below](#when-it-goes-wrong).

Without the wrapper it is one argument, and it has to be there every time:

```sh
ufbt launch FLIP_PORT=$HOME/FlipperEmu/devices/default/usb.tty
```

## The SDK has to be the firmware's

An app is built against one version of the firmware's API, and the loader on the
Flipper compares. ufbt's SDK is the official release's unless you tell it otherwise,
so an app built with it starts on the official firmware of that release.

`./flipperemu app FILE.fap` says what a file was built for, beside what the firmware
that runs has:

```
dist/fe_hello.fap: "Hello FlipperEmu", built for API 88.2, which is this firmware's
```

For another version or another firmware, ufbt gets another SDK:

```sh
ufbt update --channel=dev              # the official firmware's development branch
ufbt update --index-url=URL            # a firmware that publishes its own SDK
ufbt status                            # which one it has
```

Momentum and Unleashed publish theirs. Their documentation has the URL.

It works the other way round too: `./flipperemu ufbt flash_usb` puts the firmware of
the SDK that ufbt has on the running Flipper, through the firmware's own updater, as
`ufbt flash_usb` does with a real one. Files and settings stay, and it takes a minute
and a half.

## gdb, with the firmware's symbols and your app's

```sh
../../flipperemu gdb                   # with the Flipper running: gdb is on it
```

```
[default] the firmware's symbols: firmware/official-1.5.1/flipper-z-f7-firmware-1.5.1.elf
[default] apps' symbols, as they start: from ~/.ufbt/build, and the stock apps' from firmware/official-1.5.1/appsymbols
vPortSuppressTicksAndSleep (expected_idle_ticks=287) at targets/f7/furi_hal/furi_hal_os.c:172
(gdb) break fe_hello_app
Function "fe_hello_app" not defined.
Breakpoint 1 (fe_hello_app) pending.
(gdb) continue
```

The app is not on the Flipper yet, so its breakpoint waits. `../../flipperemu ufbt
launch` in another terminal, and:

```
[FURI] New application loaded. Adding debug info
[FURI] Loading debug information from devices/default/gdb-apps/fe_hello_d.elf

Breakpoint 1, fe_hello_app (p=0x0) at examples/hello/hello.c:56
56	    Hello hello = {
(gdb) next
62	    ViewPort* view_port = view_port_alloc();
(gdb) next
63	    view_port_draw_callback_set(view_port, hello_draw, &hello);
(gdb) info locals
hello = {keys = 0x20008eb8, mutex = 0x20008d58, presses = 0, last = 0x20009475 "none"}
view_port = 0x20008f58
```

The Flipper stands still while gdb has it stopped: its screen, its clock, what it says
on USB. `continue` lets it run, Ctrl-C stops it again, `quit` leaves it running.

A crash stops in gdb before the firmware restarts (a debug build: before it halts).
Hold OK in the example:

```
[flipperemu] the firmware crashed: fe_hello: on purpose
[flipperemu] `bt` shows where; `continue` lets it restart
(gdb) bt
#0  __furi_crash_implementation () at lib/cmsis_core/cmsis_gcc.h:968
#1  0x20009350 in fe_hello_app (p=<optimized out>) at examples/hello/hello.c:74
#2  0x08062b98 in flipper_application_thread (context=0x20008688) at lib/flipper_application/flipper_application.c:239
```

On a debug build the second line says what `continue` leads to there:

```
[flipperemu] `bt` shows where; after `continue` it halts: this is a debug build, which waits there for a debugger
[flipperemu] (`./flipperemu -d mine reset` in another terminal starts it again)
```

What `./flipperemu gdb` puts together, and how to have it otherwise:

- **The stub** is QEMU's. It is opened when gdb is asked for, on a socket in the
  device's folder: nothing has to be decided when the device is started, and two
  devices do not want the same port. For a gdb that has to be there from the first
  instruction, `./flipperemu run --wait-gdb` has the device wait at reset.
- **The gdb** is the firmware toolchain's `arm-none-eabi-gdb-py3`, which ufbt and fbt
  download; then what the PATH has. `--with GDB` names another. One without Python
  gets the firmware's symbols and nothing of the rest.
- **The firmware's symbols** come from an ELF whose contents are what the device's
  flash holds. It looks at the `.elf` the device was flashed from, the ELFs beside
  the package it was flashed from, a build in the source tree you are in, and ufbt's
  SDK, which has the ELF of the release it is of. `scripts/fetch_firmware.py` puts an
  official build's ELF beside its package (`flipper-z-f7-firmware-VERSION.elf` on the
  update server, for the dev channel's builds too), so a device with a firmware from
  there has its symbols with nothing more to say. `--elf FILE` names another. None
  of these has source files of the firmware: `directory PATH` in gdb, with a checkout
  of it, shows its lines.
- **Your app's symbols** are read by the firmware's own gdb script
  (`scripts/debug/flipperapps.py`, from its source tree or from the SDK). It goes
  through the firmware's list of loaded apps, and loads each app's debug ELF at the
  addresses the loader put the app at. An app that runs when gdb gets there has its
  symbols at once; one that is started later has them when it starts, before its first
  instruction. The debug ELFs are looked for where ufbt leaves them (`~/.ufbt/build`),
  or fbt (`.extapps` beside the firmware's ELF). `--apps DIR` is another place.
- **The stock apps' symbols.** NFC, Sub-GHz, Infrared and the others are apps on the
  card as well, and the official update server has their debug ELFs, one archive for
  each build (`flipper-z-f7-appsymbols-VERSION.tgz`). `scripts/fetch_firmware.py`
  unpacks it into `appsymbols/` beside the firmware's ELF, and gdb looks there too.
  `break nfc_app` waits for NFC to be started, and a crash in a stock app has the
  app's files and lines in its backtrace:

  ```
  [flipperemu] the firmware crashed: furi_check failed
  (gdb) bt
  #0  __furi_crash_implementation () at lib/cmsis_core/cmsis_gcc.h:968
  #1  0x08042df2 in nfc_free (instance=0x20010750) at lib/nfc/nfc.c:260
  #2  0x2002289c in nfc_app_free (instance=instance@entry=0x2000db50) at applications/main/nfc/nfc_app.c:143
  #3  0x20022fb4 in nfc_app (p=0x0) at applications/main/nfc/nfc_app.c:536
  #4  0x0806607c in flipper_application_thread (context=0x200085a8) at lib/flipper_application/flipper_application.c:239
  ```

  The firmware's script looks in one folder. With the stock apps' symbols there are
  two, so it is given `gdb-apps/` in the device's folder: links to the files of both,
  made again as each app starts (yours may have been built since). An app of yours
  with the name of a stock one is the one that is taken.
- **Whatever follows `--`** goes to gdb. `./flipperemu gdb -- -batch -ex bt` prints
  where the firmware is, and leaves.

For an IDE, or a gdb of your own:

```sh
../../flipperemu gdb --serve 3333      # the stub, on localhost:3333
```

It prints the gdb command that has all of the above (`gdb -x devices/NAME/gdb.init`).
A front end that takes a gdb server's address takes `localhost:3333`. The stub stays
there until the device stops; a `./flipperemu gdb` after it uses it where it is.
For VS Code there is more than the port: [below](#from-vs-code).

`ufbt debug` and `fbt debug` are refused, as they go to a debug probe. They would also
set the firmware's flag for one (`furi_hal_debug_gdb_session_active`), with which the
firmware runs a breakpoint instruction where an app starts and where it crashes. A
probe halts the processor there. QEMU's stub has no probe behind it, and the firmware
would crash on the instruction. `./flipperemu gdb` keeps the flag off, and has
breakpoints of gdb's at those two places.

### From VS Code

```
$ ../../flipperemu gdb --vscode
[default] the firmware's symbols: …/official-1.5.1/flipper-z-f7-firmware-1.5.1.elf
[default] apps' symbols, as they start: from ~/.ufbt/build, and the stock apps' from …/official-1.5.1/appsymbols
[default] its gdb stub is on localhost:3333, for an IDE (`target remote localhost:3333`), or with all of the above:
    ~/.ufbt/toolchain/current/bin/arm-none-eabi-gdb-py3 -q -x …/devices/default/gdb.init
[default] .vscode/launch.json: "FlipperEmu (default)" added, for the Cortex-Debug extension: open this folder in VS Code, and Run and Debug has it (F5)
[default] Once more after the device was started again, or has another firmware: the stub and the ELF are those of now
```

In your app's folder, with the device running, `gdb --vscode` opens the stub on a
port (3333, or `--serve PORT`) and writes a debug configuration into
`.vscode/launch.json`. It is for Cortex-Debug (`marus25.cortex-debug`), the extension
that ufbt's own configurations are for and that `ufbt vscode_dist` recommends for the
folder:

```json
{
    "name": "FlipperEmu (default)",
    "type": "cortex-debug",
    "request": "attach",
    "servertype": "external",
    "gdbTarget": "localhost:3333",
    "executable": "…/official-1.5.1/flipper-z-f7-firmware-1.5.1.elf",
    "gdbPath": "…/toolchain/current/bin/arm-none-eabi-gdb-py3",
    "cwd": "${workspaceFolder}",
    "postAttachCommands": [
        "source …/devices/default/gdb-ide.init"
    ],
    "armToolchainPath": "…/toolchain/current/bin",
    "svdFile": "…/scripts/debug/STM32WB55_CM4.svd"
}
```

The extension starts that gdb with the firmware's ELF, puts it on the stub, and has
it read `gdb-ide.init`: the rest of what `./flipperemu gdb` tells a gdb, which is the
apps' symbols as they start, the stop at a crash, and the debug flag kept off.

- **A launch.json that is there** keeps all it has, its comments too. The one from
  `ufbt vscode_dist` gets the configuration after ufbt's own. Those are for a debug
  probe and a real Flipper; "FlipperEmu (default)" is the one for the emulated one.
- **Run it again** when the device was started again or has another firmware. The
  port and the ELF are those of the moment, and the configuration of that name is
  written anew.
- **A breakpoint in your app** can be set before the app is on the Flipper. gdb holds
  it until the app starts (`../../flipperemu ufbt launch` in a terminal), and has it
  in place before the app's first instruction.
- **Leaving.** A gdb that detaches and exits leaves the Flipper running. A `kill`
  from gdb ends the emulator, as it would end a program it debugs: `run` starts the
  device again. `./flipperemu reset` in a terminal restarts the firmware.
- **A breakpoint that is never reached, on a line that runs.** gdb looks for the file
  by the full path in the app's debug info. ufbt has one build folder for all
  projects (`~/.ufbt/build`), and a source file that was built before from another
  folder, a copy of the project, keeps that folder's path there. `ufbt -c` and a new
  build put it right.
- **No symbols of your app at all.** `gdb` says so when it finds no SDK of ufbt's
  ("no symbols of apps: …"): it looks in `~/.ufbt`, or where `UFBT_HOME` says.

This was tried without VS Code, which the machine it was written on does not have.
A gdb was driven over its machine interface the way the extension drives one: the
firmware running while commands go on, a breakpoint by the file's full path set
before the app starts and run into, a pause, variables, a detach and an exit. The
gdb check does that each time. What the extension's own views make of it was not
seen: if something there is off, the configuration is a plain one to change, and
`gdb --vscode` only ever writes the one of its name.

## A firmware: `fbt`

A firmware's source tree has its own tool, `./fbt`. What it builds goes on the emulated
Flipper in one of three ways:

```sh
./fbt                                  # in the firmware's tree
flipperemu -d mine run -f build/f7-firmware-D/firmware.elf
                                       # programmed as a DFU does it, and started.
                                       # `flipperemu gdb` has its symbols, and those of
                                       # the tree's apps

./fbt updater_package
flipperemu -d mine run --update dist/f7-D/f7-update-local
                                       # through the firmware's own updater, as
                                       # qFlipper does: settings and files stay
flipperemu -d mine update dist/f7-D/f7-update-local
                                       # the same with the device running: written
                                       # to its card, installed, waited for

flipperemu -d mine fbt flash_usb       # with the device running: fbt builds the
                                       # package and sends it over USB
```

(`flipperemu` stands for the path to the launcher. It may be a link to it from a
folder on your PATH.)

`flipperemu fbt …` is `./fbt …` of the tree you are in, with the emulated port named
as for ufbt, and the same targets refused. `fbt launch APPSRC=my_app`, `fbt cli` and
`fbt flash_usb` talk to the emulated Flipper. `fbt flash_usb_full` is refused: its
package has a radio stack in it. `./fbt updater_package` builds that package, and
`flipperemu update` installs it without the stage. In the window that is Device >
Update with a package, and F6 after each build from then on.

What `./fbt` builds is a debug build (the `D` of `f7-firmware-D`) unless you give it
`DEBUG=0`. A debug build does not start again after a crash: it
[halts](#an-app-from-its-source-to-the-screen), and `flipperemu status` says so.

Then `./flipperemu log -f` for what the firmware says, at the level you set
(`log --set-level debug`), and
[gdb](#gdb-with-the-firmwares-symbols-and-your-apps).

## When it goes wrong

- **"Update Failed! CRC mismatch #[3-99]" on the Flipper's screen.** An update package
  with a radio stack in it was installed (`flash_usb_full`, a release's package sent
  with the official tools). The emulated second core is ST's FUS without a radio
  stack, and cannot install one. The updater stops at that stage and gives it this
  name; nothing was written. Hold Back, and the Flipper starts as it was. `./flipperemu
  update PACKAGE` installs the same package without that stage on a device that runs,
  `./flipperemu run --update PACKAGE` on one that does not.
- **`./flipperemu update` takes a minute or more.** That is the firmware's updater,
  which works with no USB and starts the Flipper a few times: a minute and a half for
  the official firmware, two minutes for Unleashed, four for Momentum, seven and a
  half for RogueMaster. The package
  itself is on the card within seconds, with the card out of its slot for that
  moment.
- **`update --cli` and `fbt flash_usb` take minutes more.** They send the package
  through the firmware's CLI, which takes some 20 KB/s, on a real Flipper too, and a
  release's package is 2.5 to 26 MB. A second `update --cli` with the same package
  sends only what changed.
- **No log after an update.** The updater sets the firmware's log level when it is
  done: the official one to the default, Momentum's, Unleashed's and RogueMaster's
  to none. That is
  the firmware's doing, and a real Flipper has it too. `update` says so when the level
  is another than before; `./flipperemu log --set-level info` and `./flipperemu reset`
  bring the log back, and `status` shows the level.
- **`ufbt launch` ends with "Unexpected response: Application … has to be closed
  manually".** The app that runs does not take the loader's request to close: an app
  with a loop of its own has to ask for it (`furi_thread_set_signal_callback`, as
  `examples/hello` does; a view dispatcher or an event loop does it for you).
  `./flipperemu app --close` closes it with its Back button, and then `launch` again.
- **"the firmware crashed (…) when the app was asked to close, and has started
  again".** The other side of the same request. It ends the app's event loop where it
  is, the app goes on to free what it has, and something of that was still at work: a
  worker thread, a timer, a scan. The firmware's own NFC app does this when it is asked
  in the middle of a read (`nfc_scanner_free` checks that the scanner is idle). `fbt
  launch` and `ufbt launch` ask in the same way, so an app that crashes here crashes
  on a `launch` over itself on a real Flipper too. Stop what is at work after the loop
  returns, then free it; `./flipperemu gdb` has the place (the backtrace of the crash).
- **The firmware started again, and its log has no `[CRASH]`.** What a crash prints
  is dropped when another thread is in the middle of a log line at that moment (the
  log's lock is taken, and a crash does not wait): the log stops in that line and goes
  on with the next start. A real Flipper does the same. The reason is still on the
  Flipper's screen, and `./flipperemu status` has it from the same place, the
  firmware's fault register, until a button is pressed there. `app` and `app --close`
  take it from there when the firmware started again under them and the log says
  nothing. The window's console has it then too ("The firmware started again after a
  crash: its screen says …"), and when a window is opened while that screen is still
  up. A debug build keeps nothing there: it halts.
- **"no CLI: the firmware crashed (…) and has halted".** A debug build does not start
  again after a crash: it waits for a debugger, and answers nothing until then. The
  same words end the message above on such a build. `./flipperemu gdb` shows where it
  stands (with symbols, if the firmware was flashed from an `.elf` or built in the
  tree you are in); `./flipperemu reset` starts it again.
- **"its USB serial port is in use by …".** Another command talks to the Flipper: a
  `ufbt cli` left open, a long `push`. One at a time.
- **"no CLI: it is plugged into a computer, which sees …, and that is no serial
  port".** An app on the Flipper has the USB port for itself: U2F and the USB keyboard
  make another device of it, and Bad USB switches it off until a script runs (then the
  computer "does not see it (yet)"). `./flipperemu app` says the same when it starts
  such an app. Leave the app (`./flipperemu press back`; held, `--hold 800`, for the
  USB keyboard), and the CLI is there again.
- **"no USB serial port for `ufbt launch`".** The cable is out (`./flipperemu usb
  plug`), or the firmware has the port for something else (Bad USB, U2F), or the device
  was started with `--usb unix` or `tcp:`, which the firmware's tools cannot open.
- **The Flipper stands still.** A gdb has it stopped: `continue` there, or `quit`.
  A gdb that was started by `./flipperemu gdb` and died lets the firmware go, and
  the launcher says so. One of your own on a `--serve` port does not: get on it
  once more (`./flipperemu gdb`), and leave.
- **gdb says "No symbol … in current context" for your app.** Its debug ELF was not
  found, or is not of the build that runs: gdb says which file it looked for when the
  app starts. `--apps DIR` names the folder it is in. For a stock app of an official
  build, `scripts/fetch_firmware.py` gets the symbols.
- **The loader refuses the app, or the Flipper asks "Continue anyways?".** It was built
  for another API: [the SDK has to be the firmware's](#the-sdk-has-to-be-the-firmwares).

## What was checked

`scripts/ufbt_check.py FIRMWARE` goes this way on a throwaway device, with
`examples/hello` and the `ufbt` on the PATH: build, `launch`, the app's log lines,
buttons, `launch` over the app that runs, the crash and the restart, `ufbt cli` in a
terminal of its own with a second command kept waiting, the refused targets,
`ufbt flash_usb` (the SDK's package sent without its radio stack, and installed), a
stopped device. For `fbt` it has a stand-in that says what it was called with: the
tree's `fbt` is found from a folder inside it and run in its root, the emulated port
is named, a port you name yourself is left alone, and the same targets are refused.
At its end it runs `examples/hello/test.sh` as it is, and wants it to pass and to
leave no Flipper running. It passes on the official 1.5.1 with ufbt 0.2.6 and its SDK
(31 checks). Without `ufbt`, or with an SDK for another API than the firmware's, it
checks nothing and its exit status is 77.

`scripts/gpio_check.py FIRMWARE` has the firmware's own `gpio` command in the CLI on
the other side of the pins: the header after a start, a pin the firmware makes an
output and sets (as a table, as a word, by its three names, in `status`, and a
`--wait` that returns when the firmware sets it), a pin held high and low and let go
of with the firmware's `gpio read` for each, a pin held while the firmware drives it,
what is held over a reset, and what is refused. Then the record: what the firmware
does to PA7 and what is held to PA6 are in the file in their order, each with a later
time; a second record is refused while one runs; `gpio record stop` says the number
of changes that the file has, and nothing is written after it; `gpio watch` prints
the two changes the firmware makes while it watches and leaves no record behind, also
when it has no `--for` and is ended with `kill`, as a script ends what it began in
its background; a record goes on over a reset; a record of pin 17 while the firmware
reads a DS1990A has the reset pulse and, right after it, the key's presence pulse;
and a record the emulator was stopped under is whole. Beneath the pins it wants the
two serial ports as the firmware starts with them: the log's with a baud rate, its
receiver off and a call waited for on pin 14, the other one off; and beneath those
the I2C bus, with no device and no address called, the SPI bus, with no device and
nothing clocked (which `spi` has to say too), and the ADC, off, with nothing
converted. Then voltages: six of them held to the ADC's six pins, in the table and in
`status --json`; a script in the firmware's JavaScript that measures each pin twice
in a row with `readAnalog()` and prints what is held (1250 for 1.25 V, 2047, the most
that it has, for 3V3); the launcher's ADC line while the script has the ADC (on, its
reference at 2.048 V) and after it (off, twelve conversions, the last one's pin,
voltage and number); a voltage as a level, with the firmware's `gpio read` on both
sides of 1.65 V; a voltage to a pin that is not the ADC's; and what is refused (more
than 3.3 V, a number with no unit). Then PWM, from a script in the firmware's
JavaScript that starts one on pin 2 and one on pin 4 with `pwmWrite()` and goes on
to its next setting when the check holds PA6: a `--wait pwm` that fails while there
is none and returns when the script starts; the two rows of the table and the
pins' word; the timers' numbers in `status --json` for four settings, each worked
out beforehand from the firmware's arithmetic (1 kHz at 50 % and 10 kHz at 30 %,
440 Hz at 25 % and 5 Hz at 50 %, where the firmware changes LPTIM2 to its 32768 Hz
clock, 100 Hz at 10 % with a prescaler in both timers, and 0 % and 100 %, which are
a low and a high pin); 3V3 held to a pin that has a PWM; what the script's own
`isPwmSupported()` and `isPwmRunning()` say; and the record, which has no line for
a PWM's edges. It passes on the official 1.5.1,
the official dev build, Momentum, Unleashed and RogueMaster (82 checks). Then, where
`ufbt` is there with this firmware's SDK (the official 1.5.1; skipped otherwise),
three apps of its own. `scripts/adc_check_app` measures with `furi_hal_adc` and logs each
number with what the firmware's own `furi_hal_adc_convert_*` makes of it: the chip's
reference, temperature sensor and battery pin, then the six pins of the firmware's
`gpio_pins` table, each twice, all of it with the reference at 2.048 V and 64 samples
for a result and again at 2.5 V with one; and one pin as the app's own output, high
and low, and as an input with a pull-up and a pull-down. The check wants every
number, each of which it works out from the voltage. `scripts/pwm_check_app` makes
the PWMs that the firmware's own two calls never make, with the registers set from
the app: the other polarity on both timers, LPTIM2 with a prescaler, TIM1 in PWM
mode 2, pin 2 as an open-drain output of the timer (held low from outside it is low,
and a PWM again with 3V3 on it), a timer that runs with its pin given away, and a
pin that is still the timer's after the timer was stopped (`unknown`, as before).
And `scripts/gpio_check_app`:
inputs with an interrupt on PA4 (both
edges), PC0 (its fall), PC1 (its rise) and PB2, and PA7 as an open-drain output. The
table and `status --json` have each pin's edges. A pin held low and high is one
logged interrupt for each edge asked for and none for the other; PC1's interrupt
turns PC0's off and on again, and none arrives or is kept while it is off; PA7 is low
while the app pulls it low, also with 3V3 held to it, high when let go of, and read
as low by the firmware while it is held low from outside; a record has PA7's six
levels in their order; and the NFC, whose chip has PB2's interrupt line, reads a tag
afterwards (111 checks then).
The window's pins are in `scripts/gui_check.py`: a click on pin 2 and Hold it high
(the window has it high with a wire on it, and the launcher's `gpio PA7` from outside
says `high`), pin 3 held low, the GPIO menu's lines, Let go of all pins, the clicks
on 13 and 17 that hold nothing (the firmware log, the iButton menu), the two serial
ports in the GPIO menu in `gpio`'s words and on pin 13, a port that opens and closes
in the console, and a record begun from the GPIO menu: the status bar counts a pin
held and let go of, and the file read back after End the record has both changes. The
I2C bus is there with a sensor and an EEPROM on it: the GPIO menu's line for the bus
and the one for each device in `i2c`'s words, the two pins' tips, the sensor's
temperature set through the menu's dialog (the launcher's `i2c` from outside has it
after), and one line in the console for the firmware's scan of 128 addresses. A script's
PWM on pin 2: the pin lit for a quarter of its width at 25 %, the words under the
pointer and in the GPIO menu, one line in the console when the PWM begins and one
when it ends, and none for a change of its numbers in between. A
record of the bus begun from the GPIO menu: the status bar counts a scan's 128
addresses, the menu has the two that answered, and the file read back after End
the record of the I2C bus has the 128 calls. A record that a shell began shows in
the status bar and is ended from the menu, and a shell's `i2c watch` shows there
with the record it keeps, for as long as it watches. The word on the shift: no
firmware calls a wrong address by itself, so the window is given the machine's
word on its bus as the link gives it, with three calls to 0x24 that nobody took,
and has to say the word under the console's line and under the bus's line in the
menu, and to drop it when the true word comes again (`i2c_check.py` has such a
call from a script, for `i2c` and `i2c watch`). The SPI bus is there with a flash on
it (`--spi flash`): the GPIO menu's line for the bus in `spi`'s words and the flash's
line with its pin and its file, pin 4's tip with whose chip select it is; pin 4
held low from outside, which selects the flash in the menu, in the tip and in `spi`;
`examples/spi/flash.js` run on the Flipper, which has to be one line in the console
for its 63 bytes (on Momentum, Unleashed and RogueMaster; the official firmware's
JavaScript has no `spi`, and the step says that it was skipped). Bytes that no
device heard and pin 5 as the bus's clock are given to the window as the link gives
them, as the wrong address is: no firmware here does the first, and a script has
the bus for less time than the window looks at the header in.

`scripts/uart_check.py FIRMWARE` has `examples/uart/echo.js` on the Flipper's side
of pins 15 and 16: where `uart` says the ports go; a line sent while no app has the
port open; the port opened after the firmware has slept, and the script's first
line; a line answered, in under a second; `--hex` and the codes for what cannot be
typed; 900 characters back in their order; what was found once not found again; an
answer that does not come; the two pins in `gpio` while the port is open and after;
a program on the pty answered, with `uart send` beside it; `uart term` in a terminal
of its own, and a second `uart` told who has the pins; the port closed, opened
again, and opened after a reset; then pins 13 and 14: the log read with `uart wait
--port usart`, a line sent while the firmware only logs, the script on that port
(a line, 900 characters, `uart term`, a `uart` for the other port meanwhile), and
the firmware's log on the pins again between two runs of it; what is refused; a run
with `--lpuart none` and `--serial pty`, with a program on the pty of pins 13 and
14; and one with `--lpuart tcp:PORT` and a TCP client. Before those two runs, the
port's speed (240 bytes for the script at 1200 baud take their two seconds, with a
`--for` of one) and the firmware's USB-UART bridge, which receives by DMA: the USB
serial port's bytes out of pin 13, a short line and 600 bytes into pin 14 at the
host, the CLI back after Back and Exit. All along, what the firmware has each port
as: both in `uart` after a start, the note of a send that nobody heard, the
script's port at 115200 baud and read by interrupt in `gpio` and in `status
--json`, 1200 baud when the script asks for that, and the bridge's read by DMA. And
a module's call, right after the start: the log's port waits for one, a byte sent
there is answered with the expansion service's heartbeat (`01 01`) and `uart send`
says what the firmware took it as, a record of the pins has pin 14 low for the time
of a start bit and then as the port's, a byte to the other port (off, nobody
waiting) is a start bit on pin 16 and no more, and the firmware waits again and
answers a second call. Then `expansion`, the launcher as that module: 115200 baud
confirmed and a second of heartbeats answered, the port at that rate and read by
the firmware while the link is up, an RPC session with a ping through it, a baud
rate the firmware refuses, the port it does not wait on, and the log's port as it
was when the module is gone. It passes on the official 1.5.1, the official dev
build, Momentum, Unleashed and RogueMaster (79 checks). It has no app in C.

`scripts/i2c_check.py FIRMWARE` starts a device with a TMP105 at 0x48 and an EEPROM
at 0x50. Before that, the forms of `--i2c` that are refused, each in words and with
no device left behind (a type that QEMU has not among them), and `run --i2c help`
with its list. Then: `i2c` lists both, with the temperature that was given at the
start and the EEPROM's file, which is new and 0xFF throughout; the firmware's scan
finds the two addresses and no other, and the bus's line has its 128 calls, two of
them answered and the last one not; `gpio`, `status` and `status --json` have the
bus; a value read bare, a value set, and six things refused with the value left as it
was; `i2c --json` has the bus, both devices and their values, and one device alone;
the record: none runs at first and `i2c record stop` says so, one begins and a second
is refused with no file made, the scan's 128 calls are in the file while it runs and
in its count, `i2c record stop` says what the file has, the file names both devices
and has every address from 0x00 to 0x7f once and in that order, each to write, with
no byte and a STOP, two of them answered, each with a time not before the one it
follows; `i2c watch 0x50 --for 8` has of another scan the one call to that address
and ends by itself, `i2c watch` ended with `kill` has a third scan's 128 calls and
their count, and neither leaves a record running, its own file, or a byte more in the
record that had ended; what `i2c record` and `i2c watch` refuse, in words, and that
none of it began a record; the word on an address that was not shifted, for 0x24 with
a sensor at 0x48 and for 0x28 with a device at 0x51, for no other address and not
when the call was answered; the launcher's reading of a line with a mark (`+`, `!`,
`...`), from a file that the check writes; `examples/i2c/sensor.js` reads the
temperature that was
set and counts one in the EEPROM, reads another after that was set from outside, the
file has the count, and after a reset of the firmware the temperature is still as set
and the count goes on; the record of such a run has it byte for byte (the sensor
asked for, `01 60`, register 0 named and `f8 80` read; the EEPROM asked for, `01`
read and `00 02` written) and `i2c watch` has it as it happened, ended with Ctrl+C; a
script that gives the firmware 0x48 as it is gets no answer, and the watch and `i2c`
have that call to 0x24 with the word on the shift; `examples/i2c/sensor.c`, built and
started by `ufbt launch`, logs the run that the EEPROM counts and the temperature
that was set, has another one within a second of its being set from outside, and
leaves the count in the file when it is closed; the record of the app's run has the
EEPROM's byte read and the run's number written, the sensor set to 12 bits and both
temperatures byte for byte, every call answered; `examples/i2c/test.sh` passes on a
Flipper of its own; a `run` with another `rom-size` is refused and the file is as it
was; and a start without the sensor has none in the scan, in the script or in the
app, and the count from before. Before that last start, a record goes on over a
reset of the firmware, the scan after it counted, and is whole when the device is
stopped under it: 128 calls and no line cut.

The last start has a second device beside the EEPROM, `i2c-echo` at 0x42: QEMU's
device for tests, which keeps three bytes, gives them back to a read, and refuses a
fourth. It is there for the marks that neither example makes. A script writes four
bytes to it and then five, and reads three: the record has the fourth byte refused
in both writes, and the firmware's call is true for the first and false for the
second. The check's own app in C (`scripts/i2c_check_app`, built and started by `ufbt
launch`) logs how each of its calls went, and the record has to say the same: three
bytes written and read back after a START with no STOP before it (`+`); the two
writes with a byte refused (`!`); after a byte to the one device, a START with no
STOP for the EEPROM, which answers with its own bytes, and one for an address where
nobody is, which the call says and the record has as `no answer`; 300 bytes in one
read, more than the peripheral counts at once, the EEPROM's 256 and then its first
44 again, in one line; two bytes written, the transfer held, and a third (one line);
and a byte written with the bus held for four seconds, in which the check ends the
record: that line ends with `...` and is the file's last, the app's read after it
goes well, and the firmware's scan finds the two devices afterwards.

66 checks on the official 1.5.1, with the scripts' part skipped, since the official
JavaScript has no `i2c` module. 64 on Momentum, Unleashed and RogueMaster, with the
apps' part skipped: the SDK of the ufbt here is for the official 1.5.1. On the
official dev build it is 55, with both skipped: only the scan talks on the bus there.

`scripts/spi_check.py FIRMWARE` starts a device with two serial flashes on the
header's SPI bus, a W25Q32 with its chip select on pin 4 and a W25Q80BL on pin 7.
Before the start it wants what is refused, in words and with no device made: an `@`
with no pin, a pin with no type, a pin of the bus itself and one that is no signal's
as a chip select, `cs`, `bus` and `id` as a property, a property with no value, one
of the Flipper's own chips, a type that QEMU has not, and two devices with one chip
select; and `run --spi help`, with the pins and the flashes. After the start: `spi`
with both devices, neither selected and nothing clocked; the two new files, as large
as the parts and 0xFF throughout; `spi --json`; the bus and the two chip selects in
`gpio`, and the bus in `status` and `status --json`. Then `examples/spi/flash.js`,
where the firmware's JavaScript has an `spi` module: the flash's IDs, a sector
erased, seven bytes written and read back, 63 bytes in the launcher's count, all
with a device selected, and the bytes in the flash's file. Then, where `ufbt` is
there with this firmware's SDK, the check's own app in C (`scripts/spi_check_app`,
built and started by `ufbt launch`), which logs what it reads at each step. Through
the firmware's handle: the JEDEC ID and Winbond's older IDs (90h, also from address
1, and ABh), a sector erased and a page written. Through a handle of its own, with
its chip select on pin 7, at 4 MHz and in mode 3: the other flash's ID (86 bytes so
far, and `spi` has how the last was clocked). The least significant bit first (f9
goes out for 9f, and f7 02 68 comes back for ef 40 16); frames of 16 bits; the
clock's pin as something else than the bus's (nothing reaches the header), and
MISO's (the flash is asked, and nobody hears its answer); MOSI's pin as a plain
output, whose level the flash gets for every bit (00 ff 00 in a page, and not what
the firmware sent); bytes with nobody selected, which read as MISO's pull (00 with
a pull-down, ff with a pull-up); and the launcher's count of all that. The app then
holds the bus in the middle of a read: `spi` and `gpio` have the flash on pin 4 as
selected, pin 7 held low from outside selects the other flash too, and MISO held
high from outside is what the firmware reads with nobody selected. The files have
the page and the three bytes, and nothing else. A second run of the app from the
card finds the page; a reset of the firmware while its handle has the bus leaves
nobody selected and the count as it was, and the app runs to its end after it. Last,
a flash of another size on pin 4 is refused while the file is there (both sizes,
the file, what to do), and a start with that one flash has a count that began anew,
the page that was written before the stop, and a call to pin 7, where nobody is now,
counted as heard by none.

45 checks on the official 1.5.1, with the script's part skipped, since the official
JavaScript has no `spi` module. 30 on Momentum, Unleashed and RogueMaster, with the
app's part skipped: the SDK of the ufbt here is for the official 1.5.1. On the
official dev build it is 25, with both skipped: the firmware clocks nothing there,
and what is checked is the launcher's side and the files.

`scripts/gdb_check.py FIRMWARE` does what the gdb section shows, with gdb in batch
mode: a backtrace of the firmware with functions and lines, the breakpoint in the
app's entry that `ufbt launch` runs into, a breakpoint on a line of the app that runs
and a variable printed there, the crash with its message and the app's line in the
backtrace, the firmware running on after each, the debug flag off throughout, a
breakpoint in a stock app by its name (where `appsymbols/` is beside the firmware),
the stub on a TCP port with a gdb of its own on it, `gdb --vscode` (the configuration
in a new folder, in the launch.json `ufbt vscode_dist` writes with nothing else of
that file changed, and not in a file that is no launch.json; then that configuration
used by a gdb that is driven the way VS Code's debugger drives one, to a breakpoint
by a file's full path, a pause, and a disconnect after which the firmware runs), a
second gdb turned away, what `status` and `continue` say of a firmware a gdb has
stopped, a gdb that dies with the firmware stopped. It passes on the official 1.5.1
with the SDK's toolchain (gdb 13.2) and the update server's ELF and app symbols (30
checks), and is skipped (77) without ufbt.

`scripts/wait_check.py FIRMWARE` is for `wait` and the screen as text, with an app the
firmware brought along: the three kinds of wait, alone and after a command; a line
from before the command that does not count; the screen as text and as a picture
being the same pixels; one character changed in a reference (the wait fails, with
the number of pixels and the screen), and a `?` put there (it passes); what is
refused, with a button the Flipper has not among it; the NFC app's crash under a
wait, which a release build has to name as a restart after a crash and a debug build
as a halt, within seconds; a device stopped under a wait; and `run --detach`: back
when the emulator is up, refused for a device that runs, one line and no device left
for a firmware that is not there or is none, and no launcher left after `stop`. It
passes on the official 1.5.1, Momentum, Unleashed and RogueMaster (38 checks), and
on a debug build of the official firmware (40).

A recording is checked where the screen is: `scripts/smoke_test.py` runs
`./flipperemu record` for four seconds with two key presses and has Pillow read the
file (its size, its two colours, more than one picture, the four seconds, the loop),
and `scripts/gui_check.py` records through the window's menu action and has Qt's
reader do the same.

A pause is checked by the firmware's own clock: `scripts/smoke_test.py` asks for its
`uptime`, pauses it for some seconds (`status`, `press` and `ls` meanwhile), lets it go
on and asks again; the seconds have to be gone from the uptime. `scripts/gui_check.py`
pauses through the window's menu action, clicks OK while it stands still (no picture
comes, and the click is felt when it goes on), and has the window follow a `pause` and
a `continue` given in a terminal.

The lights are checked against the firmware's own use of the chip:
`scripts/gui_check.py` counts the values of the backlight that reach the window while
the firmware fades it in after a restart (more than four, each above the one before,
ending where it stays), and starts NFC > Read for the blink: flashes of one colour
with dark between, `status` saying that it blinks and in which colour, and an end to
it with Back. `scripts/smoke_test.py` reads engine 1 after the fade: on hold, at the
brightness the backlight has.

An app that does not live through the request to close is the NFC app in the middle
of a read: `scripts/app_check.py` asks it, and wants the crash named and the firmware
back with no app running. A debug build does not come back by itself: there the check
wants `status` and the next command at the CLI to say that the firmware has halted,
the second within five seconds, and starts it again with `reset` (27 checks). On a
release build it wants `status` to have the crash from the fault register until a
button is pressed on the Flipper (26). `scripts/gui_check.py` has the same crash
under the window: the
console has to name it, a debug build has to show as halted (the state, the title,
HALTED above the screen), the emulator's own word of the halt has to have reached the
window with the address `status` has, and Reboot has to start it again. Then the window
loses the
emulator and finds it again, as one that is opened at that moment would: it has to
show the halt again (and once more after a command that got no answer), or to say
what the Flipper's crash screen says on a release build. There the check then starts
the firmware again with that screen still up, a start after a crash with no report in
the log: the window has to say it by itself.

`scripts/update_check.py FIRMWARE` gives a running device its own update package with
`update` (`--package PKG`: another firmware's). It wants the package on the card
within seconds, the card in its slot again and the firmware using it, the launcher
waiting until the Flipper is back and saying what runs on it, a file that was on the
card still there, the emulator's own Sub-GHz keystores (`keystores/overlay`) on the
card again after the updater unpacked the package's over them and the launcher saying
so, a manifest on the card without the radio stage, the updater's stages in the log,
no crash, and the log level as before or else named. Then the same
with the radio stage kept and through the CLI (`--cli`): what was written to the
card's image is the same to the firmware, so only the two changed files are sent, the
updater's error is reported with the way out of it, and a held Back brings the
firmware back. That part runs with the firmware's log level at none: the launcher has
to lend the updater a level to see its error, and to give none back. And the
refusals: a folder and a file that are no package, a pulled USB cable, a card that is
out of its slot, a device that does not run. At the end the Flipper is stopped and
started with `run --update`: the updater has to start, the keystores to be on the card
again when it is done and the run to say so, the package's firmware to run with its
CLI answering, and no crash. 33 checks; it passes on the official
1.5.1, Momentum, Unleashed and RogueMaster with their own packages (4, 9, 6 and 15
minutes), on the official dev build with its own, and from the official firmware to
Unleashed and to the dev build with `--package`.
`ufbt_check.py` has the real `ufbt flash_usb` go through the wrapper (29 checks now).

`scripts/snapshot_check.py FIRMWARE` starts a device with a TMP105, an EEPROM and a
serial flash on its header. Before that it wants what a device with no snapshot and one
that does not run are told, and that none of it made a device. On the running device: a
folder on the card, an NTAG215, an EM4100 tag and a DS1990 key held to it, 31.5 °C for
the sensor, and `snapshot save first`. The snapshot has to have the state, the device's
files and the note, and nothing else; folder and files have to be the user's alone,
under a umask of 022; the note has to say how the machine was made, which firmware it
is, what was held, which build of the emulator it is and the state's SHA-256; the card's
copy has to be as large as the card and not a second card on the disk; the EEPROM's and
the flash's contents have to be in it. Then a name that is taken and four that are no
names, a save of a paused device (which stays paused, and goes on with `continue`), a
save with no name whose name of the date and the time is taken (it gets a `-2`) and one
where it is free, the list and `--json`, and a load while the device runs, which is
refused. The device then goes on for twenty seconds: another folder, the two tags taken
away, 12.5 °C, and other bytes in the EEPROM's and the flash's files after its stop.
`snapshot load first` has to bring all of it back: the uptime of the save and not of the
stopped run, the calendar where the host's clock has it now, the first folder on the
card and not the second, the three things held under their names, 31.5 °C, the files'
contents, two devices on the I2C bus and one on the SPI bus, and a log that goes on from
the snapshot's, with the log of the run before the load beside it. The loaded machine
has to write to its card and to be saved again; a plain `run` after it starts on the
loaded machine's card with nothing held, and its snapshot has nothing of the header and
has the `nvram.bin` that the device has by then (in its first run it has none, and
neither has the first snapshot); a load with no name takes that newest one; and the save
of the loaded machine loads with its own card and its three things. Then what is not
loaded, each with the device's folder compared before and after, file for file: a state
with 16 bytes changed (refused by its sum, before anything is done), a state cut to its
half with the note's sum made to fit (the emulator refuses it), a load that was cut off
before the snapshot's files were put (the next load undoes it, and says so), a note that
asks for one more sensor on the header than the state has (the load names the part), and
a snapshot of another build of the emulator, which the list marks and
`--any-build --paused` loads standing still until `continue`. A load whose launcher is
killed with SIGKILL while the snapshot's files are in the device's places: the next
`run` has to say so and to start the device as it was before that load. A load in the
foreground has to end with 0 on SIGTERM and to leave nothing of itself, and a load with
the window (off-screen) has to have the Flipper's window on the loaded machine, two
presses, and an end with 0. Then `snapshot rm`. With a gdb (the one `./flipperemu gdb`
finds): `snapshot load --gdb --paused` has to stand still with the stub on the port it
was given; a gdb that gets on and steps (`stepi`) has the machine stopped as its own;
`snapshot save` at that step has to leave it the gdb's; the gdb's leaving has to let it
go on through QEMU's stub (and not through the launcher's `cont`), and the machine has
to write to its card; that state has to load and go on, on the save's card. Where ufbt's
SDK is the firmware's (the official 1.5.1), an app of the check's own
(`scripts/snapshot_check_app`) has a key of the enclave loaded into AES1 while the
machine is saved, stopped and loaded, and then encrypts a block with it, once more, and
after an unload and a load of the key again: the three blocks have to be those of a run
with no save, for the device's own key and for factory key 1 with the block of the
firmware's self-test. Then no crash in any of the runs' logs. Last, a crash on purpose
(app_check.py's: the NFC app closed in the middle of a read): a debug build halts, and
after a save and a load `status` has to say the same of the halt and its report, `wait`
has to say at once that it has halted and `wait -- reset` has to start it again; a
release build starts again, and `status` has to have the crash's message after the load
as before it. 67 checks on the official 1.5.1 and on the official dev build (one skipped
there: the app is for the official SDK), 65 on Momentum, Unleashed and RogueMaster (the
same one skipped). A load took 2.1 to 2.4 seconds in them.

By hand: the official self-update script sending a package without a radio stack
(what `fbt flash_usb` sends) to a running device, which installed it and came back.

Not tried: a firmware built by `fbt` in its own tree, and then run, updated and
launched to with the commands above. The stand-in shows what `fbt` is handed, and not
that a build of it works here. Neither is ufbt with another firmware's SDK. gdb was
tried with the SDK's ELF and the update server's (the release's and the dev build's),
an app built by ufbt and the server's symbols of the stock apps, and not with a
firmware tree's ELF and `.extapps`. The configuration for VS Code was used by a gdb
driven as the extension drives one, and not by VS Code itself.
