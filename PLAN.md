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
- ☑ USB/IP export (`--usbip`, `sudo scripts/usbip_attach.py`): the PC's kernel attaches
      the emulated Flipper as a real USB device for qFlipper & co. Verified with a
      kernel-role client; real-kernel/qFlipper run pending
- ☑ Bus time (2026-11-08): bulk transactions take full-speed bus time and wait for the
      firmware's measured turnaround, so a slow emulated CPU can't overflow the CLI's
      CDC event queue (`furi_check` in USB_LP; BRINGUP_LOG 36)
- ☐ A minimal BLE stack on CPU2, so qFlipper's updates (radio stage) and the BT service work
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
- ☑ IR: receive (TIM2 input capture + virtual receiver; 14 protocols, raw, .ir files;
      decoded by the Infrared app and `ir rx`) and transmit capture (TIM1 with its
      repetition counter, two DMA channels on TIM1_UP; every transmission →
      `devices/NAME/ir-tx/*.ir`, within one carrier period of the encoder)

## M5: virtual RF ◐
TIM2 input capture (M4's IR receiver) is the same mechanism Sub-GHz async RX uses.
- ☑ Sub-GHz receive: `.sub` RAW captures and Princeton keys on a virtual air, heard by the
      CC1101 in RX within 500 kHz (GDO0 → TIM2 capture); decoded by Read / `subghz rx`
- ☑ Sub-GHz transmit capture: every transmission → `devices/NAME/subghz-tx/*.sub` (exact
      timings, frequency, custom preset from the live registers); TX → RX loopback decodes
- ☐ TX-side pacing against DMA refill lag; more key encoders (CAME, Nice FLO, …)
- ◐ NFC: the ST25R3916 frame by frame (FIFO, IRQs, timed TX/RX, parity and CRC
      handling), TIM17 as a real timer. Virtual ISO14443-3A cards from `.nfc` files:
      NTAG21x / Ultralight / Ultralight EV1 ☑ (app Read with NDEF, `mfu info`, `dump`);
      MIFARE Classic Mini/1K/4K ☑ (Crypto1 from the paper, nested auth, access bits;
      the app's dictionary attack, `dump` from the key cache).
      Card emulation ☑ (2026-11-08): a virtual reader in front of the emulating Flipper
      (`nfc --reader scan|ntag`; target mode, passive-target memory, I_wu_a); an NTAG
      reads back page for page → `devices/NAME/nfc-tx/*.nfc`. Classic emulation's
      transparent-mode answers are not decoded yet.
      Next: Ultralight C (3DES), ISO14443-4 (DESFire, EMV), ISO14443B, ISO15693/SLIX,
      FeliCa; decode transparent mode (Classic emulation)
- ☑ 125 kHz RFID read: COMP1 → TIM2 TI4 capture, a virtual tag in the reader's field
      (ASK/FSK/PSK, field noise), encoders ported from the firmware (`fe_rfid.py`, checked
      cycle for cycle against its emulation by `rfid_check.py`); app Read and `rfid read`
- ☑ 125 kHz RFID emulate: a virtual reader clocks TIM2 (ETR), records CH3's PWM load →
      `devices/NAME/rfid-tx/`; captures replay as tags (any protocol the firmware has)
- ☐ T5577 writes (carrier gaps on TIM1: a virtual T5577 that takes them and then
      answers with the new data), RFID reader detection (TIM1 is a full timer now:
      check `furi_hal_rfid_field_detect_*`), iButton 1-Wire / Cyfral / Metakom (the
      COMP1 interrupt is modelled)

## M6: research tooling ☐
- ☐ Snapshot workflow (`savevm` on qcow2 images; boot once, jump straight to an app)
- ☐ TCG plugin for coverage/trace of firmware or a single FAP
- ☐ Fuzzing harness (AFL++/libafl QEMU mode) for parsers in the firmware
- ☐ Upstream the three QEMU patches; consider upstreaming the machine

## Non-goals (for now)
- Running ST's radio stack on an emulated CPU2 (closed, encrypted binary)
- Cycle accuracy
