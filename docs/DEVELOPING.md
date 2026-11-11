# Developing for the Flipper with FlipperEmu

How to work on an app, or on a firmware, with an emulated Flipper where the real one
would be: the build tools you already use, pointed at it.

- [An app, from its source to the screen](#an-app-from-its-source-to-the-screen)
- [A GIF of your app](#a-gif-of-your-app)
- [Pause it](#pause-it)
- [The LED and the backlight](#the-led-and-the-backlight)
- [The pins of the GPIO header](#the-pins-of-the-gpio-header)
- [A serial port on the header](#a-serial-port-on-the-header)
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
   2  PA7   output                         high
   3  PA6   input                          floating
   4  PA4   analog                         floating
…
$ ../../flipperemu gpio PA6 high
[flipper] PA6 (pin 3 of the header): held high from outside; it is high (the firmware has it as: input)
$ ../../flipperemu gpio PA7
high
```

The first column is the pin's number on the header, the third what the firmware has
the pin as (what your app's `furi_hal_gpio_init` made of it), the fourth its level.
A pin is named as the MCU has it (`PA7`), as the case has it (`A7`) or by that number.

- **Your app drives a pin:** `gpio PA7` prints `high`, `low`, `floating` or `unknown`
  and nothing else, for a script; `gpio PA7 --wait high --timeout 5` waits for it and
  fails after the five seconds, with what the pin was instead. It looks twenty times a
  second, so it is for a level that stays, not for a pulse.
- **Your app reads a pin:** `gpio PA6 high` and `gpio PA6 low` hold it from outside as
  a wire to 3V3 or to GND would, and `gpio PA6 free` lets go (`gpio free`: of all).
  The firmware's own `gpio read PA6` in the CLI is a quick look from the inside.
- A reset of the firmware leaves what is held, as it would leave a wire. A pin your
  app drives as a push-pull output keeps the app's level while it does (`gpio` says
  that something is held to it, and that it does not show).
- What a peripheral puts on a pin (a UART, PWM, SPI, I2C) is `unknown`: only the levels
  of plain inputs and outputs are on the header so far. The ADC reads nothing. (The
  bytes of the UART on pins 15 and 16 are there, as bytes: the next section.)
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
- `uart wait TEXT` waits for what the app sends by itself. It reads on from where
  the wait before it found its text, so a script that looks late misses nothing.
- `uart term` is a terminal on the port, to try a protocol by hand (Ctrl-] leaves).
- The program that will talk to the real board can talk to this one: the port is
  also a pty, `devices/NAME/lpuart.tty`, and that program and `uart send` can be on
  it at once. `run --lpuart tcp:PORT` makes it a TCP port instead.
- What the app sent is in `devices/NAME/lpuart.log`, whether or not anything
  listened. What is sent to an app that has not opened the port is lost, as it is
  on a wire.

The example needs no build: `examples/uart/echo.js` is a script for the firmware's
`js` command that opens the port and answers every line (the README's "The serial
port on pins 15 and 16" has the session). What ran here is that script, on five
firmwares; the JavaScript `serial` module is the calls above. Two limits matter for
an app: reception by DMA is not there yet (`furi_hal_serial_dma_rx_start`, which the
firmware's USB-UART bridge uses), and the baud rate means nothing, so a wrong one
is not found here.

In a test script:

```sh
fe uart wait "ready" --for 10                    # the app has opened the port
fe uart send "AT+VERSION\r\n" --expect "OK"      # ... and answers
```

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

`scripts/gpio_check.py FIRMWARE` has the firmware's own `gpio` command in the CLI
on the other side of the pins: the header after a start, a pin the firmware makes an
output and sets (as a table, as a word, by its three names, in `status`, and a
`--wait` that returns when the firmware sets it), a pin held high and low and let go
of with the firmware's `gpio read` for each, a pin held while the firmware drives it,
what is held over a reset, and what is refused. Then the record: what the firmware
does to PA7 and what is held to PA6 are in the file in their order, each with a later
time; a second record is refused while one runs; `gpio record stop` says the number
of changes that the file has, and nothing is written after it; `gpio watch` prints
the two changes the firmware makes while it watches and leaves no record behind; a
record goes on over a reset; a record of pin 17 while the firmware reads a DS1990A
has the reset pulse and, right after it, the key's presence pulse; and a record the
emulator was stopped under is whole. It passes on the official 1.5.1, the
official dev build, Momentum, Unleashed and RogueMaster (53 checks). It has no app of
its own: an interrupt on a held pin and an open-drain output were not tried. The
window's pins are in `scripts/gui_check.py`: a click on pin 2 and Hold it high (the
window has it high with a wire on it, and the launcher's `gpio PA7` from outside
says `high`), pin 3 held low, the GPIO menu's lines, Let go of all pins, the
clicks on 13 and 17 that hold nothing (the firmware log, the iButton menu), and a
record begun from the GPIO menu: the status bar counts a pin held and let go of, and
the file read back after End the record has both changes.

`scripts/uart_check.py FIRMWARE` has `examples/uart/echo.js` on the Flipper's side
of pins 15 and 16: where `uart` says the ports go; a line sent while no app has the
port open; the port opened after the firmware has slept, and the script's first
line; a line answered, in under a second; `--hex` and the codes for what cannot be
typed; 900 characters back in their order; what was found once not found again; an
answer that does not come; the two pins in `gpio` while the port is open and after;
a program on the pty answered, with `uart send` beside it; `uart term` in a terminal
of its own, and a second `uart` told who has the pins; the port closed, opened
again, and opened after a reset; pins 13 and 14 read with `uart wait --port usart`;
what is refused; a run with `--lpuart none`, and one with `--lpuart tcp:PORT` and a
TCP client. It passes on the official 1.5.1, the official dev build, Momentum,
Unleashed and RogueMaster (38 checks). It has no app in C, and nothing that
receives by DMA.

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
card still there, a manifest on the card without the radio stage, the updater's
stages in the log, no crash, and the log level as before or else named. Then the same
with the radio stage kept and through the CLI (`--cli`): what was written to the
card's image is the same to the firmware, so only the two changed files are sent, the
updater's error is reported with the way out of it, and a held Back brings the
firmware back. That part runs with the firmware's log level at none: the launcher has
to lend the updater a level to see its error, and to give none back. And the
refusals: a folder and a file that are no package, a pulled USB cable, a card that is
out of its slot, a device that does not run. 29 checks; it passes on the official
1.5.1, Momentum, Unleashed and RogueMaster with their own packages (3, 6, 4 and 10
minutes), on the official dev build with its own, and from the official firmware to
Unleashed and to the dev build with `--package`.
`ufbt_check.py` has the real `ufbt flash_usb` go through the wrapper (29 checks now).

By hand: the official self-update script sending a package without a radio stack
(what `fbt flash_usb` sends) to a running device, which installed it and came back.

Not tried: a firmware built by `fbt` in its own tree, and then run, updated and
launched to with the commands above. The stand-in shows what `fbt` is handed, and not
that a build of it works here. Neither is ufbt with another firmware's SDK. gdb was
tried with the SDK's ELF and the update server's (the release's and the dev build's),
an app built by ufbt and the server's symbols of the stock apps, and not with a
firmware tree's ELF and `.extapps`. The configuration for VS Code was used by a gdb
driven as the extension drives one, and not by VS Code itself.
