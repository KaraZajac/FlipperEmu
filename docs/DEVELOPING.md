# Developing for the Flipper with FlipperEmu

How to work on an app, or on a firmware, with an emulated Flipper where the real one
would be: the build tools you already use, pointed at it.

- [An app, from its source to the screen](#an-app-from-its-source-to-the-screen)
- [Why `./flipperemu ufbt`, and not `ufbt` alone](#why-flipperemu-ufbt-and-not-ufbt-alone)
- [The SDK has to be the firmware's](#the-sdk-has-to-be-the-firmwares)
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

## A firmware: `fbt`

A firmware's source tree has its own tool, `./fbt`. What it builds goes on the emulated
Flipper in one of three ways:

```sh
./fbt                                  # in the firmware's tree
flipperemu -d mine run -f build/f7-firmware-D/firmware.elf
                                       # programmed as a DFU does it, and started.
                                       # From an .elf, `flipperemu gdb` has its symbols

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
(`log --set-level debug`), and [gdb](../README.md#debugging-firmware).

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

By hand: the official self-update script sending a package without a radio stack
(what `fbt flash_usb` sends) to a running device, which installed it and came back.

Not tried: a firmware built by `fbt` in its own tree, and then run, updated and
launched to with the commands above. The stand-in shows what `fbt` is handed, and not
that a build of it works here. Neither is ufbt with another firmware's SDK.
