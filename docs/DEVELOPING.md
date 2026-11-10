# Developing for the Flipper with FlipperEmu

How to work on an app, or on a firmware, with an emulated Flipper where the real one
would be: the build tools you already use, pointed at it.

- [An app, from its source to the screen](#an-app-from-its-source-to-the-screen)
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

Everything else ufbt does is passed on as it is: `./flipperemu ufbt` alone builds,
`ufbt create APPID=my_app` makes a new app's folder, `ufbt vscode_dist` sets up VS
Code, `ufbt cli` opens the Flipper's command line (Ctrl-] leaves it).

Two more ways to the same place, without ufbt in between:

- `./flipperemu app dist/my_app.fap` takes a `.fap` that is built already
  ([Apps](../README.md#apps)). It reads what the app was built for before it copies it,
  and it closes an app that does not take the loader's request by pressing its Back
  button.
- In the window, a `.fap` dropped on the Flipper is started, and F5 does it again
  after the next build.

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
- refuses `ufbt flash_usb`. See [below](#when-it-goes-wrong).

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

## gdb, with the firmware's symbols and your app's

```sh
../../flipperemu gdb                   # with the Flipper running: gdb is on it
```

```
[default] the firmware's symbols: ~/.ufbt/current/firmware.elf
[default] apps' symbols, as they start: from ~/.ufbt/build
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
[FURI] Loading debug information from ~/.ufbt/build/fe_hello_d.elf

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

A crash stops in gdb before the firmware restarts. Hold OK in the example:

```
[flipperemu] the firmware crashed: fe_hello: on purpose
[flipperemu] `bt` shows where; `continue` lets it restart
(gdb) bt
#0  __furi_crash_implementation () at lib/cmsis_core/cmsis_gcc.h:968
#1  0x20009350 in fe_hello_app (p=<optimized out>) at examples/hello/hello.c:74
#2  0x08062b98 in flipper_application_thread (context=0x20008688) at lib/flipper_application/flipper_application.c:239
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
  flash holds. It looks at the `.elf` the device was flashed from, a build in the
  source tree you are in, and ufbt's SDK, which has the ELF of the release it is of.
  A device with that release from its update package has the same bytes in its flash,
  and so gets those symbols. `--elf FILE` names another. The SDK has no source files
  of the firmware: `directory PATH` in gdb, with a checkout of it, shows its lines.
- **Your app's symbols** are read by the firmware's own gdb script
  (`scripts/debug/flipperapps.py`, from its source tree or from the SDK). It goes
  through the firmware's list of loaded apps, and loads each app's debug ELF at the
  addresses the loader put the app at. An app that runs when gdb gets there has its
  symbols at once; one that is started later has them when it starts, before its first
  instruction. The debug ELFs are looked for where ufbt leaves them (`~/.ufbt/build`),
  or fbt (`.extapps` beside the firmware's ELF). `--apps DIR` is another place.
- **Whatever follows `--`** goes to gdb. `./flipperemu gdb -- -batch -ex bt` prints
  where the firmware is, and leaves.

For an IDE, or a gdb of your own:

```sh
../../flipperemu gdb --serve 3333      # the stub, on localhost:3333
```

It prints the gdb command that has all of the above (`gdb -x devices/NAME/gdb.init`).
A front end that takes a gdb server's address takes `localhost:3333`. The stub stays
there until the device stops; a `./flipperemu gdb` after it uses it where it is.

`ufbt debug` and `fbt debug` are refused, as they go to a debug probe. They would also
set the firmware's flag for one (`furi_hal_debug_gdb_session_active`), with which the
firmware runs a breakpoint instruction where an app starts and where it crashes. A
probe halts the processor there. QEMU's stub has no probe behind it, and the firmware
would crash on the instruction. `./flipperemu gdb` keeps the flag off, and has
breakpoints of gdb's at those two places.

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

flipperemu -d mine fbt flash_usb       # with the device running: fbt builds the
                                       # package and sends it over USB
```

(`flipperemu` stands for the path to the launcher. It may be a link to it from a
folder on your PATH.)

`flipperemu fbt …` is `./fbt …` of the tree you are in, with the emulated port named
as for ufbt, and the same targets refused. `fbt launch APPSRC=my_app`, `fbt cli` and
`fbt flash_usb` talk to the emulated Flipper. `fbt flash_usb_full` is refused: its
package has a radio stack in it.

Then `./flipperemu log -f` for what the firmware says, at the level you set
(`log --set-level debug`), and
[gdb](#gdb-with-the-firmwares-symbols-and-your-apps).

## When it goes wrong

- **"Update Failed! CRC mismatch #[3-99]" on the Flipper's screen.** An update package
  with a radio stack in it was installed (`flash_usb_full`, a release's package sent
  with the official tools). The emulated second core is ST's FUS without a radio
  stack, and cannot install one. The updater stops at that stage and gives it this
  name; nothing was written. Hold Back, and the Flipper starts as it was. `./flipperemu
  run --update PACKAGE` installs the same package without that stage.
- **`ufbt launch` ends with "Unexpected response: Application … has to be closed
  manually".** The app that runs does not take the loader's request to close: an app
  with a loop of its own has to ask for it (`furi_thread_set_signal_callback`, as
  `examples/hello` does; a view dispatcher or an event loop does it for you).
  `./flipperemu app --close` closes it with its Back button, and then `launch` again.
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
  app starts. `--apps DIR` names the folder it is in.
- **The loader refuses the app, or the Flipper asks "Continue anyways?".** It was built
  for another API: [the SDK has to be the firmware's](#the-sdk-has-to-be-the-firmwares).

## What was checked

`scripts/ufbt_check.py FIRMWARE` goes this way on a throwaway device, with
`examples/hello` and the `ufbt` on the PATH: build, `launch`, the app's log lines,
buttons, `launch` over the app that runs, the crash and the restart, `ufbt cli` in a
terminal of its own with a second command kept waiting, the refused targets, a
stopped device. For `fbt` it has a stand-in that says what it was called with: the
tree's `fbt` is found from a folder inside it and run in its root, the emulated port
is named, a port you name yourself is left alone, and the same targets are refused. It
passes on the official 1.5.1 with ufbt 0.2.6 and its SDK (25 checks). Without `ufbt`,
or with an SDK for another API than the firmware's, it checks nothing and its exit
status is 77.

`scripts/gdb_check.py FIRMWARE` does what the gdb section shows, with gdb in batch
mode: a backtrace of the firmware with functions and lines, the breakpoint in the
app's entry that `ufbt launch` runs into, a breakpoint on a line of the app that runs
and a variable printed there, the crash with its message and the app's line in the
backtrace, the firmware running on after each, the debug flag off throughout, the
stub on a TCP port with a gdb of its own on it, a second gdb turned away, a gdb that
dies with the firmware stopped. It passes on the official 1.5.1 with the SDK's
toolchain (gdb 13.2) and ELF (19 checks), and is skipped (77) without them.

By hand: the official self-update script sending a package without a radio stack
(what `fbt flash_usb` sends) to a running device, which installed it and came back.

Not tried: a firmware built by `fbt` in its own tree, and then run, updated and
launched to with the commands above. The stand-in shows what `fbt` is handed, and not
that a build of it works here. Neither is ufbt with another firmware's SDK. gdb was
tried with the SDK's ELF and an app built by ufbt, and not with a firmware tree's
ELF and `.extapps`, nor from an IDE.
