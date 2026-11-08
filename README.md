# FlipperEmu

Run real Flipper Zero firmware on your PC, so you can try builds without flashing your
Flipper. This is a full-system emulator built on **QEMU 11.1.3**. The machine model is a
new `flipper-zero` board (STM32WB55RG SoC with the Flipper's display, SD card, power ICs
and radios), and it runs the **unmodified firmware binary**: the same `.elf`, `.dfu` or
update `.tgz` you would flash.

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

Verified on 2026-11-08 with `scripts/smoke_test.py` (boot, SD mount, LCD, input, CLI over
USB, IR receive, Sub-GHz receive and transmit, warm reboot, no crash): **official 1.5.1**,
**Momentum mntm-012**, **Unleashed unlshd-094**.

| | |
|:-:|:-:|
| ![momentum](docs/img/07-momentum-desktop.png) | ![momentum menu](docs/img/08-momentum-menu.png) |
| Momentum desktop (battery 87 % from the emulated fuel gauge) | Momentum's Apps carousel |

**Works**
- Boots to the desktop: every `furi_hal` init, all services, the dolphin and the menus.
- 128×64 ST7567 LCD with backlight, plus RGB LED, vibro and speaker indicators in the
  window.
- Buttons from the keyboard, the QEMU monitor, or `./flipperemu press`.
- microSD over SPI2 + DMA: card detect, mount, read, write. Settings land in
  `/ext/.int`, dolphin animations load from the card, and `.fap` apps launch.
- Persistent devices: the internal flash (`flash.img`) and the battery-backed state (fuel
  gauge data memory and RTC backup domain, `nvram.bin`) survive restarts.
- USART1 log console (`[I][Flipper] Startup complete`, …) on stdio, a file, a pty or TCP.
- USB: the firmware enumerates on a built-in virtual host. Its USB serial port (the CLI
  and the protobuf RPC) is bridged to a pty or socket, and `./flipperemu cli` talks to
  it. When an app switches the port to HID (Bad USB, USB remote), the host plays the
  PC and shows what the Flipper types.
- Over-the-air updates through the firmware's own updater (`run --update`): /int backup,
  updater in RAM, flash write and verify, resources and splash, then a normal boot.
- Infrared receive: `./flipperemu ir NEC 0x04 0x08` points a virtual remote at the
  Flipper (14 protocols, raw timings, or signals from `.ir` files). The Infrared app's
  Learn screen and the `ir rx` CLI command decode it.
- Sub-GHz receive: `./flipperemu subghz capture.sub` puts a recorded RAW capture (or a
  Princeton key) on the air at its frequency. The CC1101 hears it while it listens on that
  frequency, so the Sub-GHz app's Read and Read RAW, and `subghz rx`, receive and decode
  it.
- Sub-GHz transmit: every transmission is saved to `devices/NAME/subghz-tx/` as a `.sub`
  RAW file. It has the exact timings, the tuned frequency, and the radio's live register
  set as a custom preset, so a real Flipper can replay it.
- Speaker: TIM16's PWM tone plays through QEMU audio (desktop sound or a WAV recording).
  The duty cycle sets the volume, as on the real piezo.
- Power: STOP2 deep sleep (1–2 % of a host core when idle), fuel gauge and charger you can
  set from the monitor, and a USB cable that carries VBUS.
- CPU2 modelled as ST's FUS with no wireless stack. That's a real device state, and it
  keeps the crypto-enclave and BT paths healthy.
- The NFC ST25R3916 initialises (chip ID, oscillator, VDD measurement), at register
  level only, with no field.
- GDB stub with the release `.elf` symbols.

**Not yet** (see [PLAN.md](PLAN.md))
- qFlipper and lab.flipper.net: they look for a USB device (VID/PID), not a serial
  port, so they don't see the emulator yet (a USB/IP bridge would fix that). Tools that
  open a serial port path work.
- Bluetooth (needs a radio stack); NFC, 125 kHz RFID and iButton signals; IR transmit.
- LP5562 engine programs are approximated (steady-state brightness only).
- Not yet exercised: XIP app loaders that run code from internal flash, and the
  radio-stack stage of updates (the emulated CPU2 can't install a stack).
- The GTK window is QEMU's standard display. It hasn't been tested on a desktop session
  here; all testing so far was headless, with screenshots taken through the monitor.

You'll see "No Factory Keys Found / Secure Enclave is damaged" on the first screen. That
is the firmware's own message: the per-device factory keys only exist inside a real
Flipper's CPU2. Press Back.

## Quick start

```sh
# 1. build QEMU with the Flipper machine (about 6 min the first time; no root needed:
#    needs gcc, ninja, python3, glib2/pixman/gtk3 headers; for live sound also the
#    PipeWire or PulseAudio headers, e.g. pipewire-devel / libpipewire-0.3-dev)
scripts/setup_qemu.sh

# 2. fetch an official release (SHA-256 verified)
scripts/fetch_firmware.py                     # -> firmware/official-<ver>/

# 3. flash and boot. The update package also provides the SD card resources.
./flipperemu run -f firmware/official-1.5.1/flipper-z-f7-update-1.5.1.tgz
```

The window shows the LCD. Use the **arrows**; **Enter/Space** is OK and
**Backspace/Esc** is Back. The firmware log streams to the terminal.

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

## The `flipperemu` tool

| command | what it does |
|---|---|
| `run [-f FW] [--headless] [--gdb\|--wait-gdb] [--serial S] [--usb U] [--no-sd]` | (flash and) boot |
| `run --update PKG` | install an update package through the firmware's own updater (keeps settings and files) |
| `stop` | shut a running device down cleanly (saves its NVRAM) |
| `cli ["CMD" …]` | the device's CLI over USB: interactive, or run commands and print their output |
| `ir PROTO ADDR CMD` \| `ir raw …` \| `ir FILE.ir [NAME]` | send an infrared signal to the device's receiver |
| `subghz FILE.sub` \| `subghz princeton KEY` \| `subghz raw +H -L …` | put a Sub-GHz signal on the air (`--freq`) |
| `flash FW [--erase-all]` | program `.elf` / `.dfu` / `.bin` / update `.tgz` into `flash.img` |
| `sd [--resources X] [--size 512M]` | rebuild the microSD from a resources tgz, update package or folder |
| `press ok down down ok …` | press buttons on a running device |
| `shot out.png` | save the LCD of a running device |
| `gdb` | attach gdb (with symbols when the device was flashed from an `.elf`) |
| `info` | which firmware is on the device |

`--serial` (the USART1 log) takes `stdio` (default), `file:PATH`, `pty`, `tcp:PORT` or
`null`. `--audio` takes `auto` (default), `wav:FILE`, a QEMU audio driver or `none`.
`--usb` (the USB serial port) takes `pty` (default), `unix`
(`devices/NAME/usb.sock`), `tcp:PORT` or `none` (cable unplugged). Anything after `--` is
passed straight to QEMU, e.g. `-- -icount shift=auto` for deterministic timing, or
`-- -d in_asm,cpu -D trace.log`.

### Smoke-testing a build

```sh
scripts/smoke_test.py path/to/firmware.elf|.dfu|update.tgz [--no-sd]
```

Boots a throwaway device headless and checks: "Startup complete", "card mounted", the
LCD draws, OK changes the screen, `info device` answers on the USB CLI, an NEC frame from the
virtual IR remote and a Princeton key over the virtual air are decoded, the LCD comes back
after `power reboot`, and no `[CRASH]` in the log. Exit code 0 means pass, so
it slots into a pre-flash habit or CI.

### Updating like qFlipper does

`-f` writes a firmware straight into the flash, like a DFU flash. `--update` installs an
update package the way a real Flipper does. The firmware backs up `/int`, reboots into
its updater in RAM, writes and verifies the flash, then unpacks the new resources onto the
card. Your settings and files stay:

```sh
./flipperemu -d mine run --update firmware/momentum-mntm-012/flipper-z-f7-update-mntm-012.tgz
```

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

### Sound

The speaker plays through QEMU's audio. `--audio` picks the output: by default a desktop
driver if your QEMU build has one (PipeWire, PulseAudio, ALSA or SDL; silent when
`--headless`). `wav:FILE` records instead, which works everywhere:

```sh
./flipperemu run --audio wav:flipper.wav     # e.g. then: ./flipperemu cli "buzzer note C5 1s"
```

### Battery and USB power

The fuel gauge and charger are live QOM objects. From the monitor
(`scripts/fe_monitor.py devices/NAME/monitor.sock hmp "…"`):

```
qom-set /machine/gauge soc 5           # 5 % battery: low-battery warnings
qom-set /machine/gauge temperature-dk 3330   # 60 °C
qom-set /machine/charger usb true      # plug in a charger (charging icon)
qom-set /machine/soc/usb attached false   # pull the USB cable (data and VBUS)
```

With `--usb` (the default) the virtual cable is plugged in, so the Flipper shows
charging. As on hardware, a Flipper on USB never enters deep sleep: expect about 20 % of
a host core while idle, against 1–2 % with `--usb none` or the cable pulled.

### Debugging firmware

```sh
./flipperemu run --wait-gdb -f build/f7-firmware-D/firmware.elf     # halts at reset
./flipperemu gdb                                                     # second terminal
(gdb) break furi_hal_subghz_init
(gdb) continue
```

QEMU's own `-d` tracing works too. `scripts/fe_monitor.py` drives the HMP monitor
(`devices/NAME/monitor.sock`) for screenshots, key presses and `info registers`.

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
flipperemu             launcher / device manager
qemu/hw/arm/flipper/   the QEMU machine + STM32WB55 SoC + board devices (C)
qemu/patches/          fixes to upstream QEMU (SysTick, ssi-sd, SD SPI state)
scripts/               setup_qemu.sh, fetch_firmware.py, make_sdcard.py, fe_monitor.py,
                       fe_cli.py, fe_ir.py, fe_subghz.py, smoke_test.py
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
  firmware's behaviour was learned by reading its source, which is GPL-3.0; none of it
  is copied here.
- Screenshots show firmware UI artwork that belongs to its authors (Flipper Devices,
  Momentum). Flipper Zero is a trademark of Flipper Devices Inc. This project is not
  affiliated with or endorsed by them.
