# FlipperEmu

Run real Flipper Zero firmware on your PC, so you can try builds without flashing your
Flipper. This is a full-system emulator built on **QEMU 11.1.3**. The machine model is a
new `flipper-zero` board (STM32WB55RG SoC with the Flipper's display, SD card, power ICs
and radios), and it runs the **unmodified firmware binary**: the same `.elf`, `.dfu` or
update `.tgz` you would flash.

![The window: a Flipper drawn around the emulated LCD, with a bench of virtual devices under it](docs/img/26-window-nfc-read.png)

*The window. The NFC app has just read the NTAG215 that the bench holds to the emulated
Flipper. Click its buttons, plug its USB cable in and out, pull its microSD card, hold
cards, tags and keys to it, and switch it off and on: [The window](#the-window).*

| | |
|:-:|:-:|
| ![desktop](docs/img/03-desktop-sd-animation.png) | ![menu](docs/img/04-main-menu.png) |
| Desktop: dolphin animation streamed from the emulated microSD | Main menu, driven by keyboard / `press` |
| ![apps](docs/img/05-apps-on-sd.png) | ![snake](docs/img/06-snake-fap.png) |
| `/ext/apps` browser | `snake_game.fap` loaded from SD and running |

Why build another one? See [RESEARCH.md](RESEARCH.md). Three other emulators that run real
firmware appeared in 2026 (dryflash-FZ, plus two Renode projects), but none is built on
QEMU, so none has QEMU's speed, built-in GDB stub, snapshots or tracing plugins.

## Status

Verified on 2026-11-10 with `scripts/smoke_test.py` (boot, SD mount, LCD, input, the
backlight's fade, a recording of the screen, pause and continue, CLI over USB, the
secure enclave, IR receive and transmit,
Sub-GHz receive and transmit, 125 kHz RFID read, emulate and T5577 write, iButton read
and emulate, NFC read (NTAG215, MIFARE Classic 1K) and emulate, USB/IP export, warm
reboot, no crash): **official 1.5.1**, **Momentum mntm-012**, **Unleashed
unlshd-094**. The window passes `scripts/gui_check.py` (clicked buttons, a recording
of the screen, Pause, the firmware log and its level, the backlight fading in, a file
copied to the card, an app dropped on it, the LED blinking while NFC reads, the bench,
switching off and on, the Left + Back reboot, installing firmware) on the same three,
off-screen. `scripts/update_check.py` (an update package sent to a running device
through its CLI and installed there, and the updater's error screen) passes on the
official firmware and Unleashed, and from the one to the other.
`scripts/ufbt_check.py` (ufbt 0.2.6 building `examples/hello`, launching it there, and
`ufbt flash_usb`) and `scripts/gdb_check.py` (gdb with the firmware's and that app's
symbols) pass on the official one.

| | |
|:-:|:-:|
| ![momentum](docs/img/07-momentum-desktop.png) | ![momentum menu](docs/img/08-momentum-menu.png) |
| Momentum desktop (battery 87 % from the emulated fuel gauge) | Momentum's Apps carousel |

**Works**
- Boots to the desktop: every `furi_hal` init, all services, the dolphin and the menus.
- A window that looks like a Flipper: the 128×64 ST7567 LCD and its backlight, the RGB
  LED, the vibration motor and the speaker, in a body whose buttons you click. Its USB
  plug and microSD card come out and go back in, and a bench under it holds cards, tags
  and keys to it, points remotes at it and puts signals on the air.
- Buttons from the window (mouse or keyboard), the QEMU monitor, or `./flipperemu
  press`, held as long as you like: Back held for 5 s offers to switch the Flipper off,
  and Left + Back reboots it.
- microSD over SPI2 + DMA: card detect, mount, read, write. Settings land in
  `/ext/.int`, dolphin animations load from the card, and `.fap` apps launch.
- Files to and from the card with `./flipperemu ls`, `push`, `pull`, `rm` and `mkdir`:
  through the firmware when the device runs, in the card's image when it does not.
- An app from your build directory onto the Flipper and started in one command,
  `./flipperemu app my_app.fap`: what it was built for, the app that runs closed
  first, and what the firmware logged about the start.
- The firmware's own build tools with the emulated Flipper as their Flipper:
  `./flipperemu ufbt launch` in an app's folder builds it, puts it there and starts
  it, as on a real one. A real Flipper that is plugged in is left alone.
  [docs/DEVELOPING.md](docs/DEVELOPING.md) is the page for whoever writes an app or a
  firmware.
- Persistent devices: the internal flash (`flash.img`) and the battery-backed state (fuel
  gauge data memory and RTC backup domain, `nvram.bin`) survive restarts.
- The firmware's log (USART1: `[I][Flipper] Startup complete`, …) on stdio, a pty or
  TCP, and kept in a file whatever else. `./flipperemu log` reads and follows it, the
  window shows it as it comes, and both set how much the firmware logs, up to trace.
- USB: the firmware enumerates on a built-in virtual host. Its USB serial port (the CLI
  and the protobuf RPC) is bridged to a pty or socket, and `./flipperemu cli` talks to
  it. When an app switches the port to HID (Bad USB, USB remote), the host plays the
  PC and shows what the Flipper types.
- USB/IP: `--usbip` exports the USB device so this PC's kernel can attach it as a real
  Flipper (`0483:5740`, `/dev/ttyACM*`), for qFlipper, lab.flipper.net and other tools.
- Over-the-air updates through the firmware's own updater (`run --update`): /int backup,
  updater in RAM, flash write and verify, resources and splash, then a normal boot.
  `update` does it on a device that runs: the package goes to its card through the
  firmware's CLI, as `ufbt flash_usb` and `fbt flash_usb` send one.
- Infrared receive: `./flipperemu ir NEC 0x04 0x08` points a virtual remote at the
  Flipper (14 protocols, raw timings, or signals from `.ir` files). The Infrared app's
  Learn screen and the `ir rx` CLI command decode it.
- Infrared transmit: every transmission (a remote's button, a universal remote, `ir tx`)
  is saved to `devices/NAME/ir-tx/` as a raw `.ir` file, with the carrier frequency
  and duty, timed to the carrier period as the LED sent it.
- Sub-GHz receive: `./flipperemu subghz capture.sub` puts a recorded RAW capture (or a
  Princeton key) on the air at its frequency. The CC1101 hears it while it listens on that
  frequency, so the Sub-GHz app's Read and Read RAW, and `subghz rx`, receive and decode
  it.
- Sub-GHz transmit: every transmission is saved to `devices/NAME/subghz-tx/` as a `.sub`
  RAW file. It has the exact timings, the tuned frequency, and the radio's live register
  set as a custom preset, so a real Flipper can replay it.
- Real radios: `./flipperemu subghz --sdr rtl_tcp` makes an RTL-SDR (through `rtl_tcp`),
  a HackRF or an IQ recording the CC1101's antenna. The bridge follows the firmware's
  tuning, demodulates OOK or 2-FSK as the CC1101 would, and supplies the band's power,
  so Read, Read RAW, the Frequency Analyzer and hopping work on real signals.
- 125 kHz RFID read: `./flipperemu rfid EM4100 DEADBEEF01` (or a `.rfid` file) holds a
  tag to the Flipper. ASK, FSK and PSK tags are generated by ports of the firmware's own
  encoders, checked cycle for cycle against its emulation. The 125 kHz RFID app's Read
  and `rfid read` decode them, switching ASK/PSK as on hardware.
- 125 kHz RFID emulate: a virtual reader powers the emulated tag and records it to
  `devices/NAME/rfid-tx/`. The capture can go back in as a tag, so any protocol the
  firmware emulates can be read back.
- 125 kHz RFID write: `./flipperemu rfid t5577` holds a blank T5577. The firmware's
  Write (app or `rfid write`) programs it through the gaps in its field and verifies it,
  and the T5577 then answers as the protocol it was written with.
- iButton read: `./flipperemu ibutton ds1990 ROM` (or a `.ibtn` file) touches a key to
  the contact. Dallas keys (DS1990A, DS1992, DS1996, DS1971, any other family) answer
  the firmware's 1-Wire master, memory reads included (at overdrive speed on a DS1996),
  and Cyfral and Metakom keys play their waveform into the comparator. The iButton
  app's Read and `ikey read` decode them all.
- iButton write: `./flipperemu ibutton rw1990` (or `rw1990.2`, `tm2004`, `tm01x`) holds
  a blank. The firmware's Write programs it in that blank's own protocol and verifies
  it, and the blank then reads as the key that was written.
- iButton emulate: `./flipperemu ibutton --reader` touches a virtual 1-Wire reader to a
  Flipper that is emulating a Dallas key, and it reads the ROM and the whole memory
  (8 KB of a DS1996 in about 5 s) into `devices/NAME/ibutton-tx/`. An emulated Cyfral or
  Metakom key's waveform is captured there too, and can go back on the contact.
- Field detection: the virtual readers' 125 kHz and 13.56 MHz fields are what
  `furi_hal_rfid`/`furi_hal_nfc` field detection sees (Momentum's NFC/RFID detector).
- NFC read: `./flipperemu nfc card.nfc` holds a card to the Flipper (from a Flipper
  `.nfc` file, or a new one). It can be an NTAG21x, a MIFARE Ultralight or Ultralight
  EV1, or a MIFARE Classic Mini, 1K or 4K with real Crypto1 authentication and access
  conditions. The ST25R3916 is modelled frame by frame, so the NFC app's Read
  (dictionary attack included), NDEF parsing, `nfc scanner`, `mfu info` and `dump` all
  work against it.
- NFC emulate: `./flipperemu nfc --reader ntag` holds a virtual reader to a Flipper
  that is emulating a card, reads it like a phone reads an NTAG, and saves what it read
  to `devices/NAME/nfc-tx/` as a `.nfc` file.
- NFC link: `./flipperemu nfc --link iso-dep` puts a card played by a program in the
  field (an ISO14443-4A card here; any card type you write). The machine stops while it
  answers, so it can take its time, or relay to real hardware.
- Proxmark3: `./flipperemu nfc --link pm3` lets the emulated Flipper read real cards
  through a Proxmark3, frame by frame, MIFARE Classic included with current firmware.
  `./flipperemu nfc dump.json` puts a card the Proxmark3 dumped in the field.
- Speaker: TIM16's PWM tone plays through QEMU audio (desktop sound or a WAV recording).
  The duty cycle sets the volume, as on the real piezo.
- Lights: the LED chip (LP5562) runs the programs the firmware loads into its three
  engines, so the LED blinks while an app reads or sends, and the backlight fades in
  and out. `./flipperemu status` says what they do (`the LED: blinks cyan (00ffff)`).
- Power: STOP2 deep sleep (1–2 % of a host core when idle), a fuel gauge and charger you
  can set (`./flipperemu battery`, `usb`), and a USB cable that carries VBUS. The
  firmware switches the Flipper off as on hardware (the power-off dialog, `power off`,
  a flat battery off USB), and the window switches it on again when Back is held.
- Secure enclave: the key store of ST's FUS on CPU2 and the AES peripheral it loads keys
  into, with factory keys of FlipperEmu's own. The firmware's check of its ten keys
  passes, so there is no "Secure Enclave is damaged" at start. `furi_hal_crypto` and the
  CLI's `crypto` commands work, apps can store keys, and U2F starts and makes the
  device's own key. The keys persist in `devices/NAME/enclave.keys`;
  `./flipperemu enclave` shows them and computes what the firmware encrypts.
- CPU2 modelled as ST's FUS with no wireless stack. That's a real device state, and it
  keeps the BT paths healthy.
- gdb on a device that runs, with the firmware's symbols and those of the apps it
  loads: a breakpoint in an app's code, and a stop at a crash.
- The screen as a picture (PNG) and as a recording (an animated GIF, a few kB for a
  few seconds of an app), from the launcher and from the window.

**Not yet** (see [PLAN.md](PLAN.md))
- qFlipper's firmware *update* includes a radio-stack stage that the emulated CPU2
  cannot install (the updater stops there with "CRC mismatch #[3-99]"); use
  `./flipperemu update`, or `run --update` for a device that does not run: both leave
  that stage out.
- Bluetooth (needs a radio stack).
- NFC: Ultralight C, ISO14443-4 (DESFire, bank cards), ISO14443B, ISO15693 and FeliCa
  cards. When the Flipper emulates a MIFARE Classic, the virtual reader gets as far as
  its UID, ATQA and SAK: the Crypto1 answers go out through a bit-banged pin that the
  model does not decode.
- Not yet exercised: XIP app loaders that run code from internal flash, and the
  radio-stack stage of updates (the emulated CPU2 can't install a stack).
- The window has been checked off-screen (Qt's offscreen platform, `scripts/gui_check.py`),
  not yet by hand on a desktop session.
- DFU mode (Left held through a reboot) shows the firmware's DFU splash, but there is
  no USB DFU device behind it: reboot again to leave it.

The enclave's factory keys are FlipperEmu's, not Flipper Devices': those never leave a
real Flipper, and they are not in this project. So the files a real Flipper's keys
encrypted stay closed to an emulated one, the stock Sub-GHz keystore among them; see
[The secure enclave](#the-secure-enclave).

## Quick start

```sh
# 1. build QEMU with the Flipper machine (about 6 min the first time; no root needed:
#    needs gcc, ninja, python3, glib2/pixman/gtk3 headers; for live sound also the
#    PipeWire or PulseAudio headers, e.g. pipewire-devel / libpipewire-0.3-dev)
scripts/setup_qemu.sh

#    the window that looks like a Flipper needs PySide6 (without it: the bare LCD)
pip install PySide6                           # or: dnf/apt install python3-pyside6

# 2. fetch an official release (SHA-256 verified)
scripts/fetch_firmware.py                     # -> firmware/official-<ver>/

# 3. flash and boot. The update package also provides the SD card resources.
./flipperemu run -f firmware/official-1.5.1/flipper-z-f7-update-1.5.1.tgz
```

A Flipper opens in a window. Click its buttons, or use the **arrows**; **Enter/Space**
is OK and **Backspace/Esc** is Back. The firmware log streams to the terminal, and
[the window has it too](#the-firmware-log).

### Your own builds and other firmwares

```sh
./fbt                                          # in your firmware tree
./flipperemu -d mine run -f ~/flipperzero-firmware/build/f7-firmware-D/firmware.elf
# or a whole update package (firmware + SD resources), e.g. a release or the
# one `./fbt updater_package` puts in dist/f7-*/:
./flipperemu -d mntm run -f flipper-z-f7-update-mntm-012.tgz
```

Each `-d NAME` is an independent virtual Flipper in `devices/NAME/`, with its own flash, SD
card and NVRAM. Re-running with `-f` reflashes it the way DFU does (only the touched pages
are erased). Without `-f` it boots whatever is already flashed.

Writing an app or a firmware? [docs/DEVELOPING.md](docs/DEVELOPING.md) has the loop:
`ufbt launch` and `fbt` against the emulated Flipper, its log, a crash.

## The window

`./flipperemu run` opens a window with a Flipper in it: a body of FlipperEmu's own
drawing around the emulated LCD, with its lights, its buttons, and its ports where a
real one has them. It needs PySide6. `--gtk` gives QEMU's own window with the bare LCD
instead, and `--headless` no window at all.

- **The buttons**: click them, or use the keyboard (arrows, Enter/Space = OK,
  Backspace/Esc = Back), and hold them as long as you would a real one. Back held for
  5 s on the desktop opens the firmware's power-off dialog. Left + Back held for 3 s
  reboots the Flipper; keep Left held after letting go of Back and it starts in DFU mode.
- **Off and on**: when the firmware switches the Flipper off (that dialog, Settings >
  Power, a flat battery off USB), the window stays and shows it off. Hold Back to switch
  it on, or plug its USB cable in. It starts with its battery as it was, and with the
  cards, tags and keys that were held to it.
- **The USB plug** in its right side: click it to unplug the cable,
  again to plug it back in. The USB menu also has a charger (power without data).
  Unplugged, the Flipper runs on its battery and sleeps deeply when idle.
- **The microSD card** at the bottom right: click it to pull it out (as rudely as a
  finger would) and push it back in.
- **The infrared window** in the upper left face: click it for the Infrared menu, to
  point a remote at it. It glows when the Flipper transmits.
- **The GPIO pins** along the top: click them for the firmware log, which on a real
  Flipper comes out of two of them.
- **The lights and sounds** show on the body: the LED glows, the body shakes when the
  motor runs, and a speaker sign shows a tone (it plays through `--audio`).
- **The bench** under it has a tile for each of USB, the microSD card, the battery, NFC,
  RFID, iButton, infrared and Sub-GHz. A tile says what is plugged in or held to the
  Flipper and what its radio is doing, with a button for what you do most and a menu
  for the rest. The menu bar has the same menus, and so does a right click on the
  Flipper.
- **What can be held to it**: NFC cards (Flipper `.nfc` files, Proxmark3 `.json`
  dumps, new NTAG21x and MIFARE Classic tags), 125 kHz tags (`.rfid` files, new tags of
  24 protocols, a blank T5577) and iButton keys (`.ibtn` files, new Dallas, Cyfral and
  Metakom keys, blanks it can write). A reader's field around it, and virtual readers
  held to an emulating Flipper, whose reads land in the console. An `.ir` file opens as a
  remote of buttons; `.sub` captures and Princeton keys go on the air. A real radio (an
  RTL-SDR through `rtl_tcp`, a HackRF, an IQ recording), a Proxmark3 or a script can be
  its antenna. Files dropped on the window go where they belong.
- **Apps**: drop a `.fap` on the window (or Apps > Install and start an app, Ctrl+O).
  It is copied to the card and started, and the app that ran is closed first. **F5**
  does that again with the same file: build, F5, look. The console has what
  [`./flipperemu app`](#apps) says: what the file was built for, where it went, what
  the firmware logged about the start. Apps > Close the app that runs; Apps > List its
  apps.
- **Files to the card**: Device > microSD card > Copy files to it (Ctrl+Shift+O) or
  Copy a folder to it, also in the menu of the card's slot and of its tile. You name
  the folder on the card; left empty for files that have a place of their own, each
  goes there (`.sub` to `/ext/subghz`, ...). A file of no known kind that is dropped
  on the window is copied the same way. This works while the Flipper is off too: then
  the files go into the card's image, and a dropped app is copied and not started.
- **The console** (View > The console, Ctrl+L) says what you did and what the Flipper
  did (its transmissions, what a reader read), and has a line for the Flipper's CLI.
- **The firmware log** (View > The firmware log, Ctrl+Shift+L) is what the firmware
  itself logs, in its colours, as it logs it: see [The firmware log](#the-firmware-log).
  When the firmware crashes, the window says so, and the log has the rest.
- **View**: zoom from 2× to 6×, a white, black or Catppuccin Mocha body, the LCD's pixel
  grid. Device > Save the screen saves the LCD as a PNG; Install firmware flashes another
  one and starts it.
- **Device > Record the screen** (Ctrl+Shift+R) records what the LCD shows from then
  on, with a red "● REC" and the time in the status bar. Choose it again to end it: the
  window asks where to save the animated GIF. `./flipperemu record` does the same for
  a device that runs without a window.
- **Device > Pause** (Ctrl+P) makes time stand still on it: the screen, the firmware's
  timers, a transmission half sent. "PAUSED" stands on the glass above the screen and
  in the title. Choose it again and it goes on from there. A button you click meanwhile
  is felt when it goes on. `./flipperemu pause` and `continue` do the same from a
  terminal, and the window shows it, as it shows a debugger that has the firmware
  stopped at a breakpoint.

Closing the window stops the emulator, as `./flipperemu stop` does. `./flipperemu gui`
opens the window for a device that runs already (started with `--headless`); closing
that one leaves the emulator running. The window remembers
its zoom, colours and recent files in `~/.config/FlipperEmu/gui.ini`.

| | |
|:-:|:-:|
| ![off](docs/img/27-window-off.png) | ![console](docs/img/29-window-log.png) |
| Off: hold Back to switch it on, or plug it in | The console, with a line for the CLI |

<p align="center"><img src="docs/img/28-window-colours.png" width="50%"
alt="The three colours: white, black and Catppuccin Mocha"></p>

<p align="center"><img src="docs/img/32-window-paused.png" width="60%"
alt="Paused in the middle of the main menu: PAUSED above the screen, and in the status bar"></p>

The window and the emulator talk over the [front-panel link](docs/PANEL_LINK.md): the
LCD, the lights and the buttons, and the QOM properties the bench shows. Any other
program can use it too. `scripts/gui_check.py FIRMWARE` checks the window off-screen: it
boots a throwaway device, clicks its buttons, records its screen, pauses it, reads the
firmware's log and sets its level, counts the steps of the backlight's fade, copies a
file to its card, drops an app on it and starts it again with F5, watches the LED blink
while NFC reads, switches it off and on, and reboots it.

## The `flipperemu` tool

| command | what it does |
|---|---|
| `run [-f FW] [--headless] [--gdb\|--wait-gdb] [--serial S] [--usb U] [--no-sd] [--log-level L]` | (flash and) boot |
| `run --update PKG` | install an update package through the firmware's own updater (keeps settings and files) |
| `update PKG [--keep-radio] [--no-wait] [--timeout S]` | the same on a device that runs: the package goes to its card through the firmware's CLI, and the launcher waits until the Flipper is back |
| `run --unplugged` \| `run --battery PERCENT` \| `run --gtk` | start on battery (USB cable out); with this charge; in QEMU's own window (the bare LCD) |
| `stop` | shut a running device down cleanly (saves its NVRAM) |
| `gui [--scale N]` | the window that looks like a Flipper, for a device that runs already |
| `status [--json]` | what is plugged into a running device and held to it |
| `usb plug` \| `usb unplug` \| `usb charger` | its USB cable: into a computer, out, into a charger (no argument: show it) |
| `battery [PERCENT] [--voltage MV] [--temperature C] [--current MA]` | its battery (no argument: show it) |
| `sd eject` \| `sd insert` | pull its microSD card out, push it back in |
| `sound off` \| `sound on` | mute its speaker, and back |
| `reset` | reboot it, as holding Left + Back does |
| `pause` \| `continue` | time stands still on it (screen, timers, radios), and goes on from there |
| `run --usbip [PORT]` | export the USB device over USB/IP (then `sudo scripts/usbip_attach.py`) |
| `cli ["CMD" …]` | the device's CLI over USB: interactive, or run commands and print their output |
| `ls [-R] [PATH]` | what is on the SD card (`/ext` when no path is given); `-R`: and in every folder under it |
| `push [-f] LOCAL… [REMOTE]` \| `pull [-f] REMOTE… [LOCAL]` | copy files and folders to the card, and from it |
| `rm [-r] [-f] PATH…` \| `mkdir PATH…` | remove files and folders on the card; make folders |
| `app FILE.fap [ARGS]` \| `app NAME [ARGS]` \| `app /ext/….fap [ARGS]` | put an app on the card and start it; start one of the firmware's own, or one on the card |
| `app` \| `app --close` \| `app --list` | which app runs; close it; the apps the firmware has and the ones on the card |
| `ufbt [ARGS]` \| `fbt [ARGS]` | your `ufbt`, or the `fbt` of the firmware tree you are in, with this device as its Flipper: `ufbt launch`, `fbt cli`, … |
| `log [-f] [-n N] [-l LEVEL] [-g REGEX] [--previous]` | what the firmware logged (its UART), in its colours: follow it, the last N lines, this level and worse, the lines that match |
| `log --set-level LEVEL` | how much the firmware logs, from its next start: none, error, warn, info, debug, trace or default |
| `ir PROTO ADDR CMD` \| `ir raw …` \| `ir FILE.ir [NAME]` | send an infrared signal to the device's receiver |
| `subghz FILE.sub` \| `subghz princeton KEY` \| `subghz raw +H -L …` | put a Sub-GHz signal on the air (`--freq`) |
| `subghz --sdr rtl_tcp[:HOST[:PORT]]` \| `--sdr hackrf` \| `--sdr FILE.cu8` | a real radio (or a recording) as the CC1101's antenna, until ^C |
| `rfid PROTO DATA` \| `rfid FILE.rfid` \| `rfid CAPTURE.txt` \| `rfid none` | hold a 125 kHz tag to the device (`--list`; no argument: show state) |
| `rfid t5577 [BLOCK0 …]` | hold a writable T5577 (blank, or with these blocks); `rfid` shows its blocks |
| `rfid --reader on\|off` \| `nfc --field on\|off` | a reader's field around the device (125 kHz: on by default) |
| `ibutton FILE.ibtn` \| `ibutton ds1990 ROM` \| `ibutton cyfral DATA` \| `ibutton none` | touch an iButton key to the device (also ds1992, ds1996, ds1971, dallas, metakom) |
| `ibutton rw1990` \| `ibutton tm2004` \| … | touch a blank the device can write; `ibutton` shows its ROM |
| `ibutton --reader` | read the Dallas key the device emulates, ROM and memory (to `devices/NAME/ibutton-tx/`) |
| `nfc FILE.nfc` \| `nfc ntag215 UID [URL]` \| `nfc mfc1k UID` \| `nfc none` | hold an NFC card to the device |
| `nfc --reader ntag` \| `nfc --reader scan` | hold an NFC reader to an emulating device and print what it reads (`--no-wait`: leave it there) |
| `nfc --link iso-dep` \| `--link script:FILE.py` \| `--link log` | a card played by a program, in the field until ^C |
| `nfc --link pm3[:PORT]` \| `nfc DUMP.json` | real cards through a Proxmark3; a Proxmark3 dump as the card |
| `enclave` \| `enclave encrypt SLOT IV TEXT` \| `enclave wrap KEY` \| `enclave reset` | the keys in the device's secure enclave, and what the firmware computes with them |
| `flash FW [--erase-all]` | program `.elf` / `.dfu` / `.bin` / update `.tgz` into `flash.img` |
| `sd [--resources X] [--size 512M]` | rebuild the microSD from a resources tgz, update package or folder |
| `press ok down down ok …` | press buttons on a running device |
| `shot out.png` | save the LCD of a running device |
| `record out.gif [--seconds N] [--scale N]` | what its LCD shows, as an animated GIF: until Ctrl-C, or for N seconds (a device without a window; with one: Device > Record the screen) |
| `gdb [--elf FILE] [--apps DIR] [--with GDB] [-- GDB'S ARGUMENTS]` | gdb on a running device, with the firmware's symbols and its apps' |
| `gdb --serve PORT` | its gdb stub on a TCP port of this computer, for an IDE |
| `info` | which firmware is on the device |

`--serial` (the USART1 log) takes `stdio` (default), `file:PATH`, `pty`, `tcp:PORT`
(on this computer only; `tcp:HOST:PORT` for another address) or `null`. Whatever it is,
the log is also kept in `devices/NAME/uart.log`. `--audio` takes `auto` (default),
`wav:FILE`, a QEMU audio driver or `none`.
`--usb` (the USB serial port) takes `pty` (default), `unix`
(`devices/NAME/usb.sock`), `tcp:PORT` or `none` (cable unplugged). Anything after `--` is
passed straight to QEMU, e.g. `-- -icount shift=auto` for deterministic timing, or
`-- -d in_asm,cpu -D trace.log`.

### The firmware log

What the firmware logs (`FURI_LOG_I` and the rest) leaves a real Flipper on USART1, pins
13 and 14 of its GPIO header, for whoever holds a serial adapter to them. FlipperEmu
keeps it in `devices/NAME/uart.log`, so it is there to read while the device runs and
after it has stopped:

```sh
./flipperemu log                      # what it logged in this run, in the firmware's colours
./flipperemu log -f                   # ... and what it logs from now on, until ^C
./flipperemu log -n 20 -l warn        # the last 20 warnings and errors
./flipperemu log -f -g '\[Loader\]'   # only the lines that match (here: of one tag)
./flipperemu log --previous           # the run before this one (uart.log.1)

./flipperemu log --set-level debug    # how much the firmware logs ...
./flipperemu reset                    # ... which it reads when it starts
./flipperemu run --log-level trace    # or from the first line of a run
```

A run starts a new `uart.log` and keeps the one before it as `uart.log.1`. Reboots,
and switching the Flipper off and on in the window, stay in one log: `log` marks them
with `--- it started again ---`, and `log -f` goes on through them, and through `stop`
and `run`. A crash report is in it as the firmware wrote it, registers and all.

The level is the firmware's own, the one of Settings > System > Log Level: none, error,
warn, info, debug or trace, and `default` for the firmware's default, which is info. It
is kept where the firmware keeps it (the RTC's backup registers, in `nvram.bin`), so it
stays until it is set again, here or in Settings. `-l` only chooses among the lines it
logged. What more there is to see at debug and trace is the build's to say: official
1.5.1 and Momentum log their services starting and much else, while Unleashed's release
(unlshd-094) is built without its debug lines, and starts with the same ones at trace as
at info.

In the window the firmware log is a tab beside the console (View > The firmware log,
Ctrl+Shift+L, or a click on the GPIO pins). It shows the last 5000 lines as they come,
and keeps at the newest one unless you scroll up. Choose which levels show, type a tag or
a word to see only the lines with it (`/…/` for a regular expression), and set the
firmware's level from the button: the Flipper restarts to take it. Save… writes what is
shown to a text file. When a `[CRASH]` goes by, the status bar and the console say what
it was.

<p align="center"><img src="docs/img/30-window-fwlog.png" width="75%"
alt="The firmware log in the window, with the firmware's level at debug"></p>

### Files on the SD card

```sh
./flipperemu ls /ext/apps/Games                   # sizes and names, folders first
./flipperemu ls -R /ext/subghz                    # ... and what is in the folders under it
./flipperemu push my_app.fap /ext/apps/Tools      # a file into a folder (made if it is not there)
./flipperemu push gate.sub tv.ir                  # no folder named: each to where its kind lives
./flipperemu push assets /ext/apps_data/my_app    # a folder and all in it: .../my_app/assets
./flipperemu pull /ext/nfc backup                 # a folder from the card: backup/nfc
./flipperemu pull /ext/subghz/gate.sub .          # one file
./flipperemu rm -r /ext/apps_data/my_app
./flipperemu mkdir /ext/apps/Mine/New             # with the folders above it
```

A device that runs is reached through its firmware: the `storage` commands of its CLI
on the USB serial port, as `fbt`'s scripts do it on a real Flipper. The firmware sees
each file as it arrives, and nothing has to be restarted. That moves some tens of
kilobytes a second. For a stopped device the same commands read and write the card's
image (`devices/NAME/sd.img`) with mtools, which takes megabytes in a moment: to copy a
lot, stop the device first. The last line of a copy says which way it went:

```
$ ./flipperemu push assets /ext/apps_data/my_app
assets/icon.png -> /ext/apps_data/my_app/assets/icon.png (412 bytes)
assets/levels.bin = /ext/apps_data/my_app/assets/levels.bin (unchanged)
[default] 1 copied, 1 unchanged (through the firmware)
```

A file that is there already with the same bytes is left alone (`-f` copies it all the
same), and what was written is read back as an MD5 and compared. With no folder named,
`push` puts `.sub` in `/ext/subghz`, `.ir` in `/ext/infrared`, `.nfc` in `/ext/nfc`,
`.rfid` in `/ext/lfrfid`, `.ibtn` in `/ext/ibutton`, `.js` in `/ext/apps/Scripts` and
`.fmf` in `/ext/music_player`; a `.fap` goes over the app of that name under
`/ext/apps`.

Paths are the firmware's: `/ext` is the card and `/int` its own small storage, which
current firmware keeps on the card as `/ext/.int` (that is where a stopped device has
it). Names are what FAT and the firmware's shell both take: ASCII, and none of
`\ / : * ? " < > |`.

The USB serial port carries one conversation at a time. The launcher's commands take
turns: one that finds the port taken waits ten seconds, then says who has it. A program
that opened the pty by itself (`screen`, a script) is not seen, so close it first. With
the cable out, or while an app on the Flipper has turned the port into a keyboard, the
commands say that there is no CLI and why.

When it stops in the middle:

- **Ctrl-C in a `push`** ends it after the piece that is on its way, and takes the
  unfinished file off the card again.
- **Ctrl-C in a `pull`** waits for the file that is on its way and throws it away. The
  firmware cannot be told to stop sending a file. A second Ctrl-C leaves at once; the
  Flipper then sends the rest to nobody, and its CLI answers again when it is through.
- **The card pulled out** (`sd eject`) ends a `push` with the card's error, and `ls`
  says that the slot is empty. `sd insert` brings back what was on it, the beginning of
  that file included.
- **A program killed** while it had the port leaves nothing in it for the next one: what
  it had written and the firmware had not taken is dropped, and so is what the firmware
  was still saying to it.

One thing the emulator leaves as it is, because it is the firmware's: a CLI command
that is cut off (a terminal closed in the middle of `storage tree` of a full card) is
still finished by the firmware, slowly, and until then no new CLI session starts. The
launcher then says that there is no prompt, and that this may be why.

`scripts/files_check.py FIRMWARE` checks all of this on a throwaway device: a folder
copied there and back through the firmware and through the image, each reading what the
other wrote, and then the interruptions above.

### Apps

```sh
./flipperemu app dist/my_app.fap                  # copy it to the card, close what runs, start it
./flipperemu app dist/my_app.fap /ext/my_app/level2.bin    # ... with a file for it to open
./flipperemu app --to /ext/apps/Mine my_app.fap   # into this folder
./flipperemu app --no-start my_app.fap            # only copy it (a stopped device takes that too)
./flipperemu app Sub-GHz "/ext/subghz/My Gate.sub"     # one of the firmware's own, by its name
./flipperemu app /ext/apps/Games/snake_game.fap   # one that is on the card
./flipperemu app                                  # which app runs?
./flipperemu app --close
./flipperemu app --list                           # the names it takes, and the .fap files on the card
```

This is the loop for an app you are writing: build it, `app` it, look, change it,
`app` it again. It is what `fbt launch` does on a real Flipper, through the same
`loader` commands of the firmware's CLI.

```
$ ./flipperemu app dist/my_app.fap
dist/my_app.fap: "My App", built for API 88.2, which is this firmware's
[default] "My App" closed
dist/my_app.fap -> /ext/apps/Tools/my_app.fap (26916 bytes)
[default] "My App" runs
```

- **What the file is built for** is read from its manifest before anything is copied:
  its name and the firmware API it was built against, beside the API of the firmware
  that runs. A file that is no app (a firmware's `.elf`, a text file) is refused there.
- **Where it goes**: over the app with that file name anywhere under `/ext/apps`, so a
  new build lands where the menu has it; into `/ext/apps` when there is none; or into
  `--to FOLDER`. The copy is `push`'s: unchanged bytes are not sent again.
- **The app that runs is closed first.** The loader asks it to close. An app with a
  loop of its own (many small ones) does not hear that, and then its Back button is
  pressed, as a person would: a few short presses, then a long one. If it is still
  there, the command says so and starts nothing.
- **When the firmware does not live through that.** The loader's request ends an app's
  event loop where it is, and an app that frees something which is still at work
  crashes the firmware. The firmware's own NFC app does, asked to close in the middle
  of a read. `app` and `app --close` then say `the firmware crashed ([NFC] furi_check
  failed) when the app was asked to close, and has started again`, with exit status 1.
  If it is your app: when its event loop returns, stop what is still at work (a
  worker, a timer, a scan) before you free it.
- **After the start** it says whether the app runs. If the firmware logged warnings or
  errors since, they are shown; if the firmware crashed, its crash message is, and the
  exit status is 1. `./flipperemu log -f` in another terminal shows all of it.
- **When the loader refuses** (no such app, an app for another API), its reason is
  printed, exit status 1.
- **When the firmware asks first.** Momentum and Unleashed put a question on the
  Flipper's screen for an app built for another API ("App Too New ... Continue
  anyways?") and start nothing until a button is pressed. `app` says after eight
  seconds that the Flipper may be asking, and goes on waiting: answer in the window, or
  with `./flipperemu press right` from another terminal. A question nobody answered in
  `--wait` seconds (60) is cancelled with Back, so that a script does not hang and the
  Flipper's CLI is free again.

ARGS is what the app is started with, spaces and all: for the firmware's own apps the
path of a file to open.

In [the window](#the-window) the same command is behind a `.fap` dropped on it and
behind F5, with its lines in the console.

`scripts/app_check.py FIRMWARE` checks this on a throwaway device with an app the
firmware brought along, and with the firmware's NFC app closed in the middle of a
read, for the crash: 25 checks on each of the three firmwares.

### ufbt and fbt

```sh
cd examples/hello                      # an app's folder (this one comes along, to try it)
../../flipperemu ufbt launch           # build it, put it on the Flipper, start it
../../flipperemu log -f -g FeHello     # what it logs
```

`./flipperemu ufbt …` is your own [ufbt](https://github.com/flipperdevices/flipperzero-ufbt)
with one thing added: the emulated Flipper's USB serial port as the port to use
(`FLIP_PORT=devices/NAME/usb.tty`). That argument matters. Left to themselves the
firmware's tools look for a real Flipper first, and one that is plugged in gets the
app. `./flipperemu fbt …` does the same for the `fbt` of the firmware tree you are in.

Targets that talk to the Flipper over USB work as on a real one: `launch`, `cli`,
and `fbt flash_usb`. `ufbt flash_usb` sends the SDK's update package, which has a radio
stack in it that the emulated second core cannot install: the launcher sends that
package itself, without the stage (`./flipperemu update`,
[below](#updating-like-qflipper-does)).
Targets that go through a debug probe (`flash`, `debug`, `blackmagic`, …) are refused,
with the launcher's command for it, and so is `fbt flash_usb_full`, which builds a
package with a radio stack and sends it in one go.
Everything else (a plain build, `ufbt create`, `ufbt update`) is passed on as it is.

[docs/DEVELOPING.md](docs/DEVELOPING.md) goes through it: the loop for an app, the SDK
that has to match the firmware, a firmware tree, and what the usual failures mean.
`scripts/ufbt_check.py FIRMWARE` checks it with the `ufbt` on the PATH (29 checks on
the official firmware). For `fbt` it checks what the launcher hands to it, with a
stand-in: a firmware built by `fbt` in its tree has not been tried yet.

### Smoke-testing a build

```sh
scripts/smoke_test.py path/to/firmware.elf|.dfu|update.tgz [--no-sd] [--keep] [-- QEMU-ARGS]
```

Boots a throwaway device headless and checks that:
- the firmware reports "Startup complete" and "card mounted";
- the LCD draws, and OK changes the screen;
- the LED chip ran the firmware's fade of the backlight to its end (engine 1 holds at
  the brightness that `status` shows);
- `flipperemu record` writes the screen as a GIF while keys are pressed;
- `flipperemu pause` makes time stand still (the firmware's uptime misses those
  seconds, and `press` and `ls` say why they cannot) until `continue`;
- `info device` answers on the USB CLI;
- the secure enclave holds its ten factory keys, and `crypto encrypt` gives what the host
  computes from the key file.

It then tests each radio through the firmware's own CLI:
- infrared: an NEC frame from the virtual remote is decoded, and an `ir tx` capture
  decodes back;
- Sub-GHz: a Princeton key on the virtual air is decoded, a `subghz tx` capture keeps
  its timings, and a key in an SDR recording comes through the radio link and decodes;
- 125 kHz RFID: an EM4100 tag is read, an H10301 emulation reads back, and an H10301
  is written to a virtual T5577;
- iButton: a DS1990A is read over 1-Wire, a virtual reader reads an emulated DS1990A,
  and an emulated Metakom key is captured, decoded and read back;
- NFC: an NTAG215 is read with `mfu info`, a MIFARE Classic 1K is dumped, the virtual
  reader reads an emulated NTAG215, and an ISO-DEP card on the NFC link answers an APDU,
  directly and through a simulated Proxmark3.

Finally, a USB/IP client enumerates the device, the LCD comes back after `power reboot`,
and the log must have no `[CRASH]`. Exit code 0 means pass, so it slots into a
pre-flash habit or CI. `--keep` keeps the device directory (`uart.log`, `qemu.log`, the
captures) to look at afterwards. `scripts/gui_check.py FIRMWARE` does the same for the
window, off-screen.

Each area has a closer check of its own in `scripts/*_check.py`. For the enclave:

```sh
scripts/enclave_check.py                 # the AES peripheral against published vectors (under a second)
scripts/enclave_check.py -f FIRMWARE     # and the enclave through the firmware (four boots, under a minute)
```

### qFlipper, lab.flipper.net and other PC tools (USB/IP)

```sh
./flipperemu run --usbip                   # serve the USB device on 127.0.0.1:3240
sudo scripts/usbip_attach.py --watch       # plug it into this PC, and again after reboots
```

The emulator speaks Linux's USB/IP protocol. The attach script imports the device and
hands the connection to the kernel's `vhci-hcd` driver, which is what `usbip attach`
does, without needing the `usbip` package. From then on the PC sees a Flipper on USB:
cdc_acm binds `/dev/ttyACM*`, and qFlipper, lab.flipper.net (Chrome WebSerial), `ufbt`
and serial tools find it by VID/PID. When the firmware leaves the bus (reboot, USB mode
change), the device is unplugged from the PC too. `--watch` plugs it back in when it
returns. `sudo scripts/usbip_attach.py --detach` unplugs it.

The URB path is tested with a client that plays the kernel's part (`scripts/fe_usbip.py`):
import, descriptors, SET_CONFIGURATION, CDC line state, bulk URBs carrying the CLI, and
unlinks. Attaching to the real kernel needs root, so that part is yours to try.

### Updating like qFlipper does

`-f` writes a firmware straight into the flash, like a DFU flash. `--update` installs an
update package the way a real Flipper does. The firmware backs up `/int`, reboots into
its updater in RAM, writes and verifies the flash, then unpacks the new resources onto the
card. Your settings and files stay:

```sh
./flipperemu -d mine run --update firmware/momentum-mntm-012/flipper-z-f7-update-mntm-012.tgz
```

`update` does the same for a device that runs already, over its USB cable: the files
go to the card one by one through the firmware's CLI, then `update install`. The
Flipper starts a few times, without USB, while its updater works. The launcher waits
for it and says what runs at the end:

```console
$ ./flipperemu -d mine update firmware/official-1.5.1/flipper-z-f7-update-1.5.1.tgz
[mine] sending 1.5.1 (2.5 MB, without its radio stage) to /ext/update/f7-update-1.5.1 …
  f7-update-1.5.1/firmware.dfu -> /ext/update/f7-update-1.5.1/firmware.dfu (771593 bytes)
  …
[mine] updater started: the Flipper starts a few times, without USB, until it is done
[mine] updated in 96 s: it runs 1.5.1 (commit 9e810dc3, built 15-10-2026)
```

The CLI takes some 20 KB/s, as a real Flipper's does: two minutes for the official
package, nine or so for Momentum's 12 MB. A file that is on the card already with the
same bytes is not sent again. The package is a `.tgz`, the folder that was in one
(`dist/f7-D/f7-update-local` of a firmware tree, ufbt's `current/f7-update-*`), or
its `update.fuf`. `./flipperemu ufbt flash_usb` is this command with the SDK's package.
`--no-wait` leaves when the updater has started. `--keep-radio` leaves the radio stage
in, for a look at what the updater does with it: it stops, the launcher says so within
seconds, and a held Back (`press --hold 1500 back`) starts the firmware that is there.

An updater decides on the firmware's log level when it is done, on a real Flipper too:
the official one sets the default, Unleashed's sets none, and its log is silent from
then on. `update` says when the level is another than before, with the command that
sets it again (`log --set-level`). If the level hides the updater's steps when you
start, it gets `info` for as long as it works, so that an error on its screen is seen.

`scripts/update_check.py FIRMWARE [--package PKG]` checks all of that on a throwaway
device (25 checks: the official firmware and Unleashed with their own packages,
and the official one updated to Unleashed).

| | |
|:-:|:-:|
| ![ota](docs/img/10-ota-writing-flash.png) | The stock updater writing the emulated flash. The radio stage of the package is left out: the emulated CPU2 is a FUS without a radio stack and can't install one. |

### USB: CLI, RPC and Bad USB

```sh
./flipperemu run …        # prints: USB serial (CLI/RPC): /dev/pts/7 -> devices/default/usb.tty
./flipperemu cli          # interactive CLI session; Ctrl-] quits
./flipperemu cli "info device" "storage list /ext" "loader open /ext/apps/USB/bad_usb.fap"
screen devices/default/usb.tty      # or minicom, picocom, a pyserial script, …
```

Connecting raises DTR and the firmware starts a CLI session, the same as `/dev/ttyACM0` on a
real Flipper; disconnecting ends it. `start_rpc_session` switches the same port to the
protobuf RPC, so tools that open a serial port by path work against the emulator.

When an app turns the port into a USB keyboard (Bad USB, the USB remote), the virtual
host acts as a PC with a US layout, and what the Flipper types appears as text on the
same port. Key combinations are shown the way DuckyScript writes them. Bad USB's
`demo_windows` starts like this:

```
<GUI r>notepad
Hello World!
<UP><HOME><SHIFT DOWN><CTRL c><RIGHT><CTRL v><CTRL v><ALT 7>This line was print using Alt+Numpad input method. …
```

| | |
|:-:|:-:|
| ![badusb](docs/img/09-badusb-hid.png) | Bad USB with the emulator's host as the "PC": the script runs to 100 %, and its keystrokes show up on `devices/NAME/usb.tty` |

### Infrared

```sh
./flipperemu ir NEC 0x04 0x08            # NEC NECext NEC42 NEC42ext Samsung32 SIRC SIRC15
./flipperemu ir Samsung32 0x07 0x02      #   SIRC20 RC5 RC5X RC6 RCA Pioneer Kaseikyo
./flipperemu ir raw 9000 4500 560 560 …  # mark/space µs, starting with a mark
./flipperemu ir tv.ir Power              # a signal from a Flipper .ir file (parsed or raw)
```

The signals are built the way the firmware's own encoders build them, so the firmware's
decoders see what a real remote would send. Open Infrared → Learn New Remote, or run `ir
rx` on the CLI, and the signal is decoded:

| | |
|:-:|:-:|
| ![ir](docs/img/11-ir-learn.png) | `./flipperemu ir Samsung32 0x07 0x02` with the Infrared app's Learn screen open |

Transmissions go the other way. Whatever the Flipper sends through its IR LED (a saved
remote, a universal remote, `ir tx NEC 04 08` on the CLI) is written to
`devices/NAME/ir-tx/tx-NNNNN.ir` as a raw signal. A real Flipper can play that file, and
`./flipperemu ir devices/NAME/ir-tx/tx-00001.ir` sends it back into the receiver:

```
Filetype: IR signals file
Version: 1
#
name: tx_00001
type: raw
frequency: 38004
duty_cycle: 0.330166
data: 8999 4499 553 579 553 553 553 1710 553 553 …
```

The durations are whole carrier periods (26.3 µs at 38 kHz), as the LED sends them.

### Sub-GHz

```sh
./flipperemu subghz garage.sub                  # a RAW capture, at the file's frequency
./flipperemu subghz princeton 0x95D5D4          # a PT2262 fixed code (default 433.92 MHz)
./flipperemu subghz raw 390 -1170 1170 -390 … --freq 315
```

The signal goes "on the air". The emulated CC1101 only hears it while it's in RX and
tuned within 500 kHz, as a real radio would, and it reports a strong RSSI during the
burst. Open Sub-GHz → Read (or Read RAW, or run `subghz rx 433920000 0` on the CLI) and
the firmware's own decoders take it from there:

| | |
|:-:|:-:|
| ![subghz](docs/img/12-subghz-read.png) | Sub-GHz → Read after `./flipperemu subghz princeton 0x95D5D4` and a RAW `.sub` capture |

Key files are encoded for Princeton. For other protocols, use a RAW capture (Read RAW
on a real Flipper records one).

Transmissions go the other way. Whatever the emulated Flipper sends (Sub-GHz → Send, a
saved signal, `subghz tx …` on the CLI) is written to `devices/NAME/subghz-tx/tx-NNNNN.sub`.
That file can go straight back in with `./flipperemu subghz` (decoded exactly), or onto a
real Flipper:

```
Filetype: Flipper SubGhz RAW File
Frequency: 433920000
Preset: FuriHalSubGhzPresetCustom
Custom_preset_module: CC1101
Custom_preset_data: 02 0D 03 07 08 32 0B 06 10 17 11 32 12 30 … 00 00 00 C0 00 00 00 00 00 00
Protocol: RAW
RAW_Data: 1170 -390 390 -1170 390 -1170 1170 -390 …
```

#### Real radios

A real SDR can be the emulated CC1101's antenna, so the firmware hears the real band:

```sh
rtl_tcp -a 127.0.0.1 &                          # an RTL-SDR (here, or on another machine)
./flipperemu subghz --sdr rtl_tcp               # until ^C; rtl_tcp:HOST:PORT for elsewhere
./flipperemu subghz --sdr hackrf                # a HackRF One (libhackrf)
./flipperemu subghz --sdr remote_433.92M_250k.cu8 --loop    # a recording (rtl_sdr: cu8)
rtl_sdr -f 434.432M -s 2.048M - | ./flipperemu subghz --sdr - --center 434.432M --rate 2.048M
```

The CC1101 model announces its receive set-up whenever the firmware changes it: RX or
not, frequency, modulation, channel bandwidth, data rate, deviation, OOK decision
boundary. The bridge (`scripts/fe_sdr.py`, numpy) does what the chip would with that
antenna:

- It filters the channel out of the SDR's band at the CC1101's bandwidth, and demodulates
  it. OOK is sliced at the chip's decision boundary below the peak (8 dB in the Flipper's
  presets); 2-FSK by the discriminator. A squelch (`--squelch`, 12 dB) keeps noise off
  GDO0.
- It streams the result to GDO0 as a continuous timeline, which the model plays through a
  60 ms jitter buffer. The firmware's own drivers capture it as they would the real chip's
  output.
- It sends the band's power every 50 ms. The firmware's RSSI reads come from it at
  whatever frequency and bandwidth it asks for, so the Frequency Analyzer finds real
  transmitters, Read RAW shows their RSSI, and hopping stops where there is a signal.
  `--rssi-offset` matches a real Flipper's readings.
- It retunes the SDR when the firmware stays tuned outside its band for half a second, a
  quarter of the sample rate above the channel, clear of the DC spike. `--center` picks
  the band to start with (433.92 MHz by default), `--no-follow` keeps it there.

| | |
|:-:|:-:|
| ![frequency analyzer](docs/img/21-sdr-frequency-analyzer.png) | ![read raw](docs/img/22-sdr-read-raw.png) |
| Frequency Analyzer on a simulated RTL-SDR (`rtl_tcp`), a remote at 433.95 MHz | Read RAW recording the same remote through the bridge |

Real recordings from the rtl_433 test corpus (PT2262, EV1527, a TE275 remote, a KeeLoq
HCS200) played through the bridge decode in the firmware's own `subghz rx`, matching
rtl_433's results. `scripts/sdr_check.py DEVICE` checks the bridge with no radio. It
covers the DSP on synthetic OOK and 2-FSK, a recording, and a simulated `rtl_tcp` dongle
the bridge has to retune; `sdr_check.py rtl-tcp-sim` runs that simulator by hand. The
link is a line protocol on `devices/NAME/subghz.sock`, documented in
[docs/RADIO_LINK.md](docs/RADIO_LINK.md), so other radios and tools can drive it.

Receive only for now: the emulated Flipper's transmissions reach the bridge (it prints
them), but nothing goes out over the air. Packet-mode presets (the CC1101's FIFO: MSK,
GFSK at 100 kBaud) aren't received.

### 125 kHz RFID

```sh
./flipperemu rfid EM4100 DE AD BE EF 01        # an EM-Micro EM4100 (RF/64)
./flipperemu rfid H10301 01 02 03              # HID Prox 26-bit, FC 1, card 515 (FSK)
./flipperemu rfid Indala26 11 22 33 40         # Indala 26-bit (PSK)
./flipperemu rfid badge.rfid                   # a Flipper .rfid key file
./flipperemu rfid none                         # take the tag away
./flipperemu rfid --list                       # every protocol
```

The tag stays in the field until you take it away, like a card lying on the Flipper. It
answers whenever the firmware's reader powers it: 125 kHz RFID → Read, Read RAW, or
`rfid read` on the CLI. The waveforms come from ports of the firmware's own protocol
encoders, so its decoders see what a real tag sends. With no tag in the field, the
reader sees noise, as on hardware, and switches between ASK and PSK every 2.5 s.

All 24 protocols of the official firmware are supported:
- ASK: EM4100 (RF/64, /32, /16), Electra, FDX-B, Viking, Jablotron, PAC/Stanley,
  Gallagher, Radio Key, GProxII, Noralsy.
- FSK: H10301, HIDProx, HIDExt, IoProxXSF, AWID, FDX-A, Pyramid, Paradox.
- PSK: Indala26, Keri, Nexwatch, Idteck.

Data is the protocol's bytes as a `.rfid` file holds them (`Data:`).

| | |
|:-:|:-:|
| ![rfid read](docs/img/13-rfid-read.png) | ![rfid emulate](docs/img/14-rfid-emulate.png) |
| 125 kHz RFID → Read after `./flipperemu rfid EM4100 DE AD BE EF 01` | Emulate: a virtual reader records the tag to `rfid-tx/` |

Emulation goes the other way. When the Flipper emulates a tag (Emulate in the app, or
`rfid emulate EM4100 DEADBEEF01` on the CLI), a virtual 125 kHz reader powers it and
records the load it switches. The recording is in carrier cycles, saved to
`devices/NAME/rfid-tx/tx-NNNNN.txt` once the field goes quiet. Hold that capture to a
reading Flipper with `./flipperemu rfid devices/NAME/rfid-tx/tx-00001.txt` and its
reader decodes it. That works for any protocol your firmware can emulate, including ones
`fe_rfid.py` has no encoder for.

Writing needs a tag that can be written:

```sh
./flipperemu rfid t5577                        # a blank T5577 (factory configuration)
# then on the Flipper: 125 kHz RFID -> Saved -> a key -> Write, or `rfid write H10301 2A1F7B`
./flipperemu rfid                              # shows the T5577's blocks and configuration
```

The T5577 decodes the firmware's write commands from the gaps in the reader's field, as
the real chip does. It stores the blocks, then answers in the modulation and bit rate
that block 0 sets. The firmware's Write only reports success after reading the tag back
and comparing it, so a "Success!" is a full round trip. A password-protected T5577 (block
0's PWD bit, password in block 7) refuses writes without it, and every write and refusal
is logged. `./flipperemu rfid t5577 BLOCK0 … BLOCK7` puts back a T5577 saved that way.

`scripts/rfid_check.py DEVICE` runs three checks for every protocol: the Python encoder
against the firmware's emulation, the Python tag through `rfid read`, and `rfid write`
onto a blank T5577.

| | |
|:-:|:-:|
| ![t5577 write](docs/img/17-t5577-write.png) | ![field detect](docs/img/18-field-detect.png) |
| 125 kHz RFID → Saved → Write onto the virtual T5577, verified by the firmware | Momentum's NFC/RFID detector in the virtual readers' fields |

The Flipper sits in a virtual reader's 125 kHz field by default. That field powers
emulated tags, and field detection reads its frequency. `./flipperemu rfid --reader off`
takes it away, and `./flipperemu nfc --field on|off` does the same for a 13.56 MHz
reader's field.

### iButton

```sh
./flipperemu ibutton ds1990 01 A1 B2 C3 D4 E5 F6    # a DS1990A (7 bytes: the CRC is added)
./flipperemu ibutton key.ibtn                       # a Flipper .ibtn file
./flipperemu ibutton ds1996 0C A1 B2 C3 D4 E5 F6    # a DS1996, 8 KB of memory (zeroed)
./flipperemu ibutton cyfral A1 B2                   # a Cyfral key
./flipperemu ibutton metakom C3 55 33 0F            # a Metakom key (each byte: even parity)
./flipperemu ibutton none                           # take it away
```

| | |
|:-:|:-:|
| ![ibutton read](docs/img/19-ibutton-read.png) | ![ibutton emulate](docs/img/20-ibutton-emulate.png) |
| iButton → Read after `./flipperemu ibutton ds1996 0C A1 B2 C3 D4 E5 F6` | Emulate: a virtual reader reads the key, all 8 KB of memory |

The key stays on the contact until you take it away: iButton → Read, or `ikey read` on
the CLI. Dallas keys are 1-Wire slaves on PB14. They answer the firmware's master with
presence pulses, SEARCH ROM, READ ROM, MATCH and SKIP ROM. Memory keys also answer READ
MEMORY: a DS1992 or DS1971 at standard speed, a DS1996 at overdrive speed, as the
firmware reads it. Cyfral and Metakom keys are read through the comparator, as on the
real board, from waveforms that `scripts/fe_ibutton.py` makes with ports of the
firmware's encoders.

Writing needs a blank:

```sh
./flipperemu ibutton rw1990          # an RW1990.1 blank (also rw1990.2, tm2004, tm01x)
# then on the Flipper: iButton -> Saved -> a key -> Write, or `ikey write Dallas 015A6B7C8D9EAF84`
./flipperemu ibutton                 # the blank's ROM now
```

The firmware tries each blank protocol in turn (RW1990.1, RW1990.2, TM2004, TM01x) and
reads the ROM back. Each virtual blank answers only its own protocol, so every path can
be tested.

The other way round, the Flipper emulates a key and a virtual reader reads it:

```sh
# on the Flipper: iButton -> Saved -> a key -> Emulate, or `ikey emulate Dallas 0123456789ABCDE9`
./flipperemu ibutton --reader        # read it: ROM, and all of a DS1992/DS1996/DS1971's memory
# a Cyfral or Metakom emulation is captured by itself:
./flipperemu ibutton devices/NAME/ibutton-tx/tx-00001.txt    # put that key on the contact
```

The reader is a 1-Wire master: READ ROM, then READ MEMORY for a memory key. It saves a
`.ibtn` file to `devices/NAME/ibutton-tx/`, which the Flipper can load. The firmware's
1-Wire slave busy-waits on its own clock with interrupts off, so the reader is paced by
that polling, not by time. When a host stall makes the slave miss a slot (the guest's
clock runs on while the host preempts it), the reader resets it and resumes the memory
read where it stopped, after rereading a few bytes to check the address. A Cyfral or
Metakom emulation is recorded as the firmware sets each level and its period, to
`tx-NNNNN.txt` once 4096 levels are in (well under a second) or the emulation stops;
`scripts/fe_ibutton.py` decodes it. `scripts/ibutton_check.py DEVICE`
reads every kind of key, writes every kind of blank, and emulates every kind of key:
the memory keys with random memory, compared byte for byte.

### NFC

```sh
./flipperemu nfc tag.nfc                                    # a Flipper .nfc dump
./flipperemu nfc ntag215 04F11E7A2C4180 https://flipperzero.one   # a new tag, NDEF URL
./flipperemu nfc mfc1k 11223344                             # a factory MIFARE Classic 1K
./flipperemu nfc none                                       # take it away
```

The card stays in the field until you take it away. Open NFC → Read, or the `nfc`
shell on the CLI (`scanner`, `mfu info`, `dump`, `raw`). The reader gets the same
answers a real card gives. That covers anticollision and select over 4, 7 or 10-byte
UIDs, and GET_VERSION, READ, FAST_READ, READ_SIG, the counters and tearing flags. It
also covers PWD_AUTH with the tag's AUTH0/PROT, and writes. So a password-protected
NTAG reads only as far as AUTH0, and a factory one unlocks with the default password.

| | |
|:-:|:-:|
| ![nfc read](docs/img/15-nfc-read.png) | ![mfc read](docs/img/16-mfc-read.png) |
| NFC → Read: an NTAG215 and its NDEF URL record | A MIFARE Classic 1K: the dictionary attack finds all 32 keys |

MIFARE Classic cards use Crypto1, written from its published description, with the
nonce PRNG, nested authentication, encrypted parity and the access bits of a real
card. The NFC app's dictionary attack finds the keys that are in its dictionary,
reads what those keys may read, and writes with what they may write. In a `.nfc`
dump, blocks that were never read are zeros, and unknown keys are FF FF FF FF FF FF.

So far: NTAG213/215/216, MIFARE Ultralight and Ultralight EV1 (Type 2), MIFARE
Classic Mini/1K/4K. Other card families are next.

Emulation goes the other way, as with 125 kHz RFID. Start the Flipper emulating a card
(NFC → Saved → a card → Emulate, or `emulate -f /ext/nfc/tag.nfc` in the `nfc` shell),
then hold a reader to it:

```sh
./flipperemu nfc --reader ntag     # read it like a phone reads an NTAG
./flipperemu nfc --reader scan     # only activate it: UID, ATQA and SAK
```

The reader's field wakes the emulation. The ST25R3916 runs anticollision and select by
itself from what the firmware loaded into its passive-target memory, as the real chip
does. After that, each reader command reaches the firmware through the FIFO, and what
the firmware transmits is the answer. `ntag` asks for the version, the signature, every
page and the NFC counter. What the reader read is printed and saved to
`devices/NAME/nfc-tx/read-NNNNN.nfc`, a Flipper `.nfc` file; an `ntag` read can go back
in as a card with `./flipperemu nfc`. To make a file for the Flipper to emulate:

```sh
scripts/fe_nfc.py --nfc-file --ntag215 04A1B2C3D4E5F6 https://flipperzero.one > tag.nfc
```

Or read a virtual card with NFC → Read and save it on the Flipper.

#### A card from a program (the NFC link)

With no card in the field, the Flipper's reader talks to whatever is on the device's
NFC link, `devices/NAME/nfc.sock`. That can be a card written in Python, or a relay to
real hardware:

```sh
./flipperemu nfc none                               # no built-in card in the field
./flipperemu nfc --link iso-dep --verbose           # an ISO14443-4A card, until ^C
./flipperemu nfc --link script:mycard.py            # a card of your own
./flipperemu nfc --link log                         # no card: print the reader's frames
```

Each frame the firmware sends goes out on the link as data bits (CRC included) and the
parity of each byte, so MIFARE Classic's encrypted parity survives. The answer comes
back as the card's. While the program works it out, QEMU stops the machine and the
virtual clock with it. However long the answer takes (a Python card, or USB to a real
reader), the firmware still sees it within its frame waiting time.

`scripts/fe_nfc_link.py` has the layers a card needs:
- `Iso14443A`: REQA/WUPA, anticollision and select over 4, 7 or 10-byte UIDs, HLTA;
- `IsoDep`: RATS/ATS, I-blocks with chaining, R- and S-blocks, and the APDUs handed to
  `apdu()`.

The `iso-dep` card answers SELECT, GET CHALLENGE and GET DATA. The NFC app reads it as an
ISO14443-4A card, and `apdu -p 4a -d 00A4040007A000000003101000` in the `nfc` shell gets
its answers through the firmware's own ISO-DEP stack. A card type the emulator doesn't
model is a subclass and a method away, and so is an answer no real card would give, for
seeing what the firmware's parsers make of it:

```python
import fe_nfc_link as nl

class Mine(nl.IsoDep):
    def apdu(self, cmd):
        return bytes.fromhex("9000")

def card(args):
    return Mine(uid=bytes.fromhex("04112233445566"))
```

| | |
|:-:|:-:|
| ![nfc link](docs/img/23-nfc-link-iso-dep.png) | |
| NFC → Read: the `iso-dep` card, played by `fe_nfc_link.py` over the link | |

The protocol is in [docs/RADIO_LINK.md](docs/RADIO_LINK.md). For now the link is the card
and the firmware the reader. The other way round still uses the built-in virtual reader
(`--reader`).

#### Real cards, through a Proxmark3

```sh
./flipperemu nfc --link pm3                   # the first Proxmark3 found; pm3:/dev/ttyACM1
./flipperemu nfc hf-mf-A1B2C3D4-dump.json     # a card the Proxmark3 dumped, in the field
scripts/fe_nfc.py --nfc-file hf-mfu-dump.json > card.nfc     # and for a real Flipper
```

With `pm3`, every frame the emulated Flipper's reader sends goes out of a real
Proxmark3's antenna, and the real card's answer comes back. That includes anticollision
and select, so what the firmware sees is the card itself. It talks the Proxmark3's own
USB protocol, so no client is needed, against Iceman/RRG firmware:
- From v4.23346 (September 2026) the relay is exact. Each frame goes out as air bits with
  its own parity, and the answer comes back with the card's (`ISO14A_NO_PARITY`), so
  MIFARE Classic's encrypted exchanges get through: the firmware's dictionary attack
  and `dump` run against the real card.
- Older firmware takes the frame's bytes and adds the parity itself, so everything
  relays except Classic after authentication.

Only ISO14443A goes out. The firmware's ISO14443B, FeliCa and ISO15693 polls get
silence. The emulated machine stops while each frame makes the round trip.

A dump is the client's JSON: `hf mf autopwn` or `hf mf dump` for MIFARE Classic Mini, 1K
and 4K (FileType `mfcard`, `mfc v2`, `mfc v3`), and `hf mfu dump` for Ultralight and NTAG
(`mfu`, with version, signature and counters). `scripts/nfc_link_check.py DEVICE` checks
all of it with no Proxmark3. It converts dumps, then relays through a simulated Proxmark3
(`scripts/fe_pm3.py sim`), for both firmware generations. The relay is written from the
Proxmark3 firmware's source and checked against that simulation. It has not met a real
Proxmark3 yet.

### Sound

The speaker plays through QEMU's audio. `--audio` picks the output: by default a desktop
driver if your QEMU build has one (PipeWire, PulseAudio, ALSA or SDL; silent when
`--headless`). `wav:FILE` records instead, which works everywhere:

```sh
./flipperemu run --audio wav:flipper.wav     # e.g. then: ./flipperemu cli "buzzer note C5 1s"
./flipperemu sound off                       # mute it (the firmware notices nothing); sound on
```

The window's Device > Mute (Ctrl+M) does the same, and shows a tone that plays.

### Battery and USB power

```sh
./flipperemu battery 15                 # 15 %, with a voltage to match
./flipperemu battery 0                  # flat: off USB the firmware warns, counts down
                                        # and switches the Flipper off
./flipperemu battery --temperature 60   # °C (also --voltage MV, --current MA)
./flipperemu usb unplug                 # pull the USB cable (data and VBUS)
./flipperemu usb charger                # a charger: power without data
./flipperemu usb plug                   # into a computer again
./flipperemu status                     # all of it, and what is held to it
./flipperemu run --unplugged --battery 40   # start that way
```

The fuel gauge and charger are QOM objects (`/machine/gauge`: `soc`, `voltage-mv`,
`temperature-dk`, `current-ma`, `capacity-mah`; `/machine/charger`: `usb`;
`/machine/soc/usb`: `attached`), which the commands set through the monitor.

When the firmware switches the Flipper off, the emulator exits: an emulator that is off
has nothing to run. The window stays and switches it on again when Back is held. With the
cable plugged in, `power off` leaves the Flipper on its "It's now safe to unplug the USB
cable" screen until the cable is pulled.

With `--usb` (the default) the virtual cable is plugged in, so the Flipper shows
charging. As on hardware, a Flipper on USB never enters deep sleep: expect about 20 % of
a host core while idle, against 1–2 % with `--usb none` or the cable pulled.

### The secure enclave

A Flipper keeps its keys where its firmware cannot read them: in the key store of ST's
FUS, on the radio core. The firmware names a slot, the FUS puts that key into the AES
peripheral, and the firmware encrypts with a key it never sees. The emulated machine has
both halves, so the firmware's check of its factory keys passes at every start, and what
is built on `furi_hal_crypto` runs: the CLI's `crypto` commands, U2F's key and counter
files, apps that keep secrets.

| slot | |
|---|---|
| 0 | the master key: it only decrypts keys that are delivered encrypted, and can never be loaded |
| 1–10 | the factory keys, the same on every device (1 encrypts the Sub-GHz keystores, 2 the stock U2F certificate's key) |
| 11 | the device's own key, which the firmware makes from random bytes the first time one is needed (U2F does) |
| 12–100 | what apps and `crypto store_key` add, one after the other |

```sh
./flipperemu enclave                  # this device's keys, and how many the firmware accepts
./flipperemu cli "crypto has_key 1"   # the firmware's side: Successfully loaded key from slot 1
./flipperemu cli                      # then e.g.: crypto encrypt 1 000102030405060708090a0b0c0d0e0f
./flipperemu enclave encrypt 1 000102030405060708090a0b0c0d0e0f "fifteen chars.."   # the same, computed here
./flipperemu enclave decrypt 1 000102030405060708090a0b0c0d0e0f HEX
./flipperemu enclave wrap KEY         # KEY under the master key, for
                                      # `crypto store_key 12 encrypted 256 …`
./flipperemu enclave reset            # the factory keys again (device stopped)
./flipperemu enclave blank            # no keys at all: the firmware reports a damaged enclave
```

(`crypto encrypt` encrypts its text in whole blocks, and fills the last one with whatever
followed the text in the firmware's memory. A text of 15, 31, 47… characters leaves no
room for that, and only then is the ciphertext the same every time, and the same as
computed here.)

The keys are in `devices/NAME/enclave.keys`, one per line, yours to read and to edit
while the device is stopped. As on the chip, a key is written once, in the next free
slot: the firmware cannot change or remove one. The file is rewritten when it stores
one.

**The factory keys are FlipperEmu's own, and they are public.** Flipper Devices' keys
never leave a real Flipper and are not in this project. Every emulated Flipper gets
`SHA-256("FlipperEmu enclave key N")` in their place. Two things follow:

- Nothing an emulated Flipper encrypts is secret, because anybody can compute its keys.
  That suits development and testing. Don't register an emulated U2F token with an
  account that matters.
- What a real Flipper's keys encrypted stays closed. The stock Sub-GHz keystore
  (`/ext/subghz/assets/keeloq_mfcodes`) decrypts to noise: the firmware logs
  `Failed to load line` for each line and carries on without those manufacturer codes.
  The stock U2F certificate's private key is encrypted the same way, so the
  certificate of an emulated token does not match the key it signs with.

Manufacturer codes of your own go where they go on a real Flipper: in
`/ext/subghz/assets/keeloq_mfcodes_user`, in plain text (`keeloq_mfcodes_user.example`
beside it shows the format). The firmware can also encrypt such a file the way the stock
one is: after `sysctl debug 1`, `subghz encrypt_keeloq PLAIN_FILE ENCRYPTED_FILE IV`
(the IV as 16 bytes of hex) writes a keystore under key 1, which this emulated Flipper
reads and no real one does.

The firmware's own check of the enclave encrypts one block with each factory key and
compares it with a ciphertext that only the real key gives. For those ten blocks, and
for nothing else, the emulated enclave answers what the firmware expects; the expected
values are in the firmware's source. Every other block is plain AES with the key in the
file. Put a key of your own in a factory slot and that slot fails the check, as a wrong
key would on hardware.

| | |
|:-:|:-:|
| ![u2f](docs/img/24-u2f-connected.png) | ![blank enclave](docs/img/25-enclave-blank.png) |
| U2F, which needs the ten factory slots filled, and makes the device's own key | After `./flipperemu enclave blank`: the firmware finds no factory keys |

### Debugging firmware

```sh
./flipperemu gdb                       # on a device that runs
(gdb) break furi_hal_subghz_init
(gdb) break my_app_entry               # an app's code: before the app is started, too
(gdb) continue
```

`./flipperemu gdb` asks the emulator for its gdb stub, on a socket in the device's
folder, and starts gdb on it with what it finds:

- the firmware toolchain's gdb (the one ufbt and fbt download), then the PATH's;
- the firmware's symbols, from an ELF whose contents are what the device's flash holds:
  the `.elf` it was flashed from, a build in the source tree you are in, or ufbt's
  SDK, which has the ELF of its release;
- the symbols of each app the firmware loads, read by the firmware's own gdb script
  when the app starts. A breakpoint set in an app's code beforehand is hit when the
  app is launched;
- a stop where a crash begins, with the firmware's message, before it restarts.

The Flipper stands still while gdb has it stopped, and runs on when gdb leaves.

```sh
./flipperemu run --wait-gdb -f build/f7-firmware-D/firmware.elf     # waits at reset
./flipperemu gdb                                                     # second terminal

./flipperemu gdb --serve 3333          # the stub on localhost:3333, for an IDE
./flipperemu gdb -- -batch -ex bt      # after --: gdb's own arguments
```

[docs/DEVELOPING.md](docs/DEVELOPING.md#gdb-with-the-firmwares-symbols-and-your-apps)
has a session with an app, and what `--elf`, `--apps` and `--with` are for.
`scripts/gdb_check.py FIRMWARE` checks it (20 checks on the official firmware, with
ufbt's SDK and toolchain). `run --gdb` still opens the stub at the start, on
localhost:1234 (`--gdb-port`).

QEMU's own `-d` tracing works too. `scripts/fe_monitor.py` drives the HMP monitor
(`devices/NAME/monitor.sock`) for screenshots, key presses and `info registers`. What the
firmware says about itself, at any log level, is in [the firmware log](#the-firmware-log).

### Running QEMU by hand

```sh
build/qemu/qemu-system-arm -M flipper-zero,name=Qemu,nvram=nv.bin,usb-serial=usb \
    -chardev pty,id=usb \
    -drive if=pflash,format=raw,file=flash.img \
    -drive if=sd,format=raw,file=sd.img \
    -rtc base=localtime -serial stdio
# or simply: -M flipper-zero -kernel firmware.elf   (volatile flash, no USB)
```

Machine options: `name=` (OTP device name, ≤8 chars), `lcd-scale=`, `region=` (OTP
region), `legacy-sleep=on|off` (default off: STOP2), `nvram=`, `usb-serial=` (chardev id for the USB port;
plugs the cable in), `audiodev=` (the speaker's `-audiodev` id). `-global
stm32wb55-usb.debug=on` traces the USB host.

## Layout

```
RESEARCH.md            prior-art survey (who else has done this, and how)
PLAN.md                roadmap
docs/ARCHITECTURE.md   how the machine is modelled, device by device
docs/BRINGUP_LOG.md    every bug found on the way (incl. 3 QEMU fixes and the USB races)
docs/RADIO_LINK.md     the protocol between an emulated radio and a real one
docs/PANEL_LINK.md     the protocol between the emulator and the window
docs/DEVELOPING.md     writing an app or a firmware with it: ufbt, fbt, gdb, the loop
examples/hello/        an app to try that with
flipperemu             launcher / device manager
qemu/hw/arm/flipper/   the QEMU machine + STM32WB55 SoC + board devices (C)
qemu/patches/          fixes to upstream QEMU (SysTick, ssi-sd, SD SPI state)
scripts/               setup_qemu.sh, fetch_firmware.py, make_sdcard.py, fe_monitor.py,
                       fe_cli.py, fe_files.py (files on the card), fe_apps.py
                       (apps and the loader), fe_ir.py,
                       fe_subghz.py, fe_sdr.py (real radios),
                       fe_nfc_link.py (cards from programs), fe_pm3.py (Proxmark3),
                       fe_enclave.py (the enclave's keys and ciphers), fe_gdb.py
                       (gdb's side of `flipperemu gdb`), fe_record.py (the
                       screen as a GIF), fe_usbip.py,
                       usbip_attach.py, fe_gui.py (the window: fe_gui_view.py,
                       fe_gui_link.py, fe_gui_widgets.py), smoke_test.py, *_check.py
devices/ firmware/ build/ third_party/    generated, git-ignored
```

## Licence

- FlipperEmu's own code (the machine and device models in `qemu/hw/arm/flipper/`,
  the launcher, scripts and docs) is **BSD-3-Clause**; see [LICENSE](LICENSE).
- The patches in `qemu/patches/` change QEMU's own source files, so they are
  **GPL-2.0-or-later** like those files; see [qemu/patches/README.md](qemu/patches/README.md).
- The `qemu-system-arm` you build is QEMU, a GPL-2.0 program that includes these
  BSD-3 files. If you distribute that binary, the GPL-2.0 applies to it.
- No code was taken from other Flipper emulators (see [RESEARCH.md](RESEARCH.md)). The
  firmware's behaviour was learned by reading its source, which is GPL-3.0; none of its
  code is copied here. One table of numbers is: the IVs, inputs and expected
  ciphertexts of its enclave self-test (`qemu/hw/arm/flipper/flipper_enclave.c`), which
  the emulated enclave has to answer with.
- Screenshots show firmware UI artwork that belongs to its authors (Flipper Devices,
  Momentum). Flipper Zero is a trademark of Flipper Devices Inc. This project is not
  affiliated with or endorsed by them.
