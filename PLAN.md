# Plan

Goal: try Flipper firmware (official, Momentum, Unleashed, your own changes)
without flashing the device, with a debugger-first workflow. QEMU-based by design (see
RESEARCH.md §5).

## M1: boots real firmware ☑ (2026-11-07)
- ☑ STM32WB55 SoC: RCC/clock tree, PWR, FLASH (+OB, OTP, UID), HSEM, IPCC + FUS-only CPU2,
  EXTI, SYSCFG (+MEMRMP, VREFBUF), RTC (backup domain), LPTIM×2, SPI×2, I2C×2,
  DMA1/2 + DMAMUX, RNG, DWT, DBGMCU; QEMU L4x5 GPIO/USART reused
- ☑ Board: ST7567 LCD, buttons, microSD, BQ27220, BQ25896, LP5562, CC1101, ST25R3916
- ☑ Official 1.5.1 to the desktop, menus, SD mount/read/write, dolphin animations, FAPs
- ☑ Persistent devices (flash, SD, NVRAM), `flipperemu` launcher, gdb, monitor automation
- ☑ Three upstream QEMU fixes (SysTick clock updates, ssi-sd framing, SD SPI state)

## M2: USB (CLI, RPC, HID) ☑ (2026-11-07)
The Flipper's CLI (`log`, `info`, `ps`, `storage`, `loader`, …) and the protobuf RPC used by
qFlipper / lab.flipper.net / `ufbt launch` only run over USB CDC.
- ☑ STM32 USB FS device model (`stm32wb55_usb.c`): EPnR toggle/rc_w0 semantics,
      ISTR/CNTR, BTABLE + 1 KiB PMA, double-buffered bulk, BCDR charger detection
- ☑ Built-in virtual host: suspend → resume → bus reset → enumerate (descriptors,
      SET_ADDRESS, strings, SET_CONFIGURATION) → CDC SET_LINE_CODING /
      SET_CONTROL_LINE_STATE (DTR follows the chardev connection) → bulk pumping
- ☑ CDC bridged to a pty (default), unix socket or TCP port; `./flipperemu cli`;
      reconnects restart the CLI session like a real terminal
- ☑ Protobuf RPC over the same port (`start_rpc_session`: ping, device info verified)
- ☑ HID: the host plays a PC with a US layout and writes what Bad USB / the USB remote
      type as text (combos as `<GUI r>`, Alt+Numpad decoded); `demo_windows` runs to 100 %
- ☑ The charger sees VBUS while the virtual cable is attached; `qom-set
      /machine/soc/usb attached false` unplugs it at runtime
- ☑ Verified with official 1.5.1, Momentum mntm-012, Unleashed unlshd-094 (smoke test)
- ☐ Stretch: USB/IP export, so the host kernel (and qFlipper) sees a real Flipper
- ☐ Stretch: second CDC port of the dual-CDC mode; mass storage

The double-buffer question from the notes was settled by what libusb_stm32 requires:
`ep_write()` only accepts a double-buffered IN endpoint whose STAT_TX reads NAK (so STAT_TX
must read NAK while DTOG_TX == SW_BUF and VALID otherwise), and `ep_read()` only accepts an
OUT endpoint whose STAT_RX reads VALID (so STAT_RX keeps its programmed value; the
NAK when DTOG_RX == SW_BUF only shows on the bus). See docs/ARCHITECTURE.md.

## M3: verify the write paths ◐  *next: XIP*
- ☑ OTA (2026-11-07): `./flipperemu run --update PKG` stages a package on the SD and starts
      the stock updater over USB. The whole chain runs: /int backup, RAM updater (MEMRMP,
      option-byte check, DFU validate, flash write + verify), post-update (restore /int,
      resources, splash), normal boot. Verified official ⇄ Unleashed, official → Momentum.
      The radio stage is blanked, because a FUS-only CPU2 cannot install a stack.
- ☐ XIP app loaders (custom firmwares that program FAP code into internal flash and
      execute it there)
- ◐ Firmware matrix: official 1.5.1, Momentum mntm-012, Unleashed unlshd-094 pass
      `scripts/smoke_test.py` (now including a warm reboot); add RogueMaster and run it
      on a schedule

## M4: senses ◐
- ☑ Speaker: TIM16 CH1 PWM → QEMU audio (desktop driver or `--audio wav:FILE`); tones
      verified by FFT (440 Hz, 1 kHz, notes)
- ☐ LP5562 engine interpreter (blinks, ramps)
- ◐ Battery/USB power: monitor control done (`qom-set /machine/gauge soc 5`, `/machine/charger usb true`); window UI todo
- ☑ STOP2 deep sleep (CPU2 performs the SHCI clock switch); now the default, 1–2 % host CPU idle
- ◐ IR: receive ☑ (TIM2 input capture + virtual receiver; 14 protocols, raw, .ir files;
      decoded by the Infrared app and `ir rx`), transmit capture ☐ (TIM1 + DMA bursts)

## M5: virtual RF ◐
TIM2 input capture (M4's IR receiver) is the same mechanism Sub-GHz async RX uses.
- ☑ Sub-GHz receive: `.sub` RAW captures and Princeton keys on a virtual air, heard by the
      CC1101 in RX within 500 kHz (GDO0 → TIM2 capture); decoded by Read / `subghz rx`
- ☐ Sub-GHz transmit capture to `.sub` (TIM2 output compare + DMA), more key encoders
- ☐ NFC: ST25R3916 transparent mode / virtual tags from `.nfc` files
- ☐ 125 kHz RFID via COMP1 / TIM, iButton 1-Wire

## M6: research tooling ☐
- ☐ Snapshot workflow (`savevm` on qcow2 images; boot once, jump straight to an app)
- ☐ TCG plugin for coverage/trace of firmware or a single FAP
- ☐ Fuzzing harness (AFL++/libafl QEMU mode) for parsers in the firmware
- ☐ Upstream the three QEMU patches; consider upstreaming the machine

## Non-goals (for now)
- Running ST's radio stack on an emulated CPU2 (closed, encrypted binary)
- Cycle accuracy
