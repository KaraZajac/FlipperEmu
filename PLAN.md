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

## M2: USB CDC (CLI + RPC) ☐  *next*
The Flipper's CLI (`log`, `info`, `ps`, `storage`, `loader`, …) and the protobuf RPC used by
qFlipper / lab.flipper.net / `ufbt launch` only run over USB CDC.
- ☐ STM32 USB FS device model (EPnR toggle semantics, ISTR/CNTR, BTABLE + 1 KiB PMA,
      double-buffered bulk), driven by libusb_stm32's `usbd_stm32wb55_devfs.c`
- ☐ Built-in "virtual host": reset → enumerate (descriptors, SET_ADDRESS,
      SET_CONFIGURATION) → CDC SET_LINE_CODING / SET_CONTROL_LINE_STATE(DTR) → bulk
      pumping between the CDC endpoints and a QEMU chardev (pty/socket)
- ☐ Charger reports VBUS while the virtual cable is "plugged"
- ☐ Result: `screen /dev/pts/N`, `scripts/storage.py`, `ufbt launch` against the emulator
- Stretch: USB/IP export so the host kernel (and qFlipper) sees a real Flipper

Notes from reading the driver (2026-11-07), to start M2 from:
- `furi_hal_usb_init()`: `usbd_init(&udev, &usbd_hw, …)`, USB_LP and USB_HP IRQs enabled,
  driver thread "UsbDriver". `usbd_connect(true)` sets `BCDR.DPPU` when a mode starts.
- `usbd_stm32wb55_devfs.c`: BTABLE = 0, EP table at PMA 0x40006C00 + ep*8 = {tx.addr, tx.cnt,
  rx.addr, rx.cnt} (u16 each), PMA byte-addressed as u16. EPnR toggle semantics:
  STAT/DTOG toggle on write-1, CTR rc_w0, SETUP read-only. `evt_poll()` reads ISTR and
  handles CTR (per-EP CTR_TX/CTR_RX), RESET (BTABLE=0, deconfig all), SOF, WKUP, SUSP.
- The CDC interface (`furi_hal_usb_cdc.c`) uses EP 0x01 OUT and EP 0x82 IN as
  **double-buffered bulk** (64 B), plus EP 0x83 interrupt. `usb.connected` (insomnia,
  CDC "connected") becomes true on a **WKUP** event, so the virtual host should signal
  SUSP → WKUP → RESET before enumerating. DTR via SET_CONTROL_LINE_STATE starts the CLI
  session; drive it from the chardev's open/close events.
- Open question to settle from RM0434 §"Double-buffered endpoints" before coding:
  libusb's `ep_write()` accepts a double-buffered IN endpoint only when `STAT_TX` reads
  NAK, while `ep_read()` accepts a double-buffered OUT endpoint only when `STAT_RX` reads
  VALID, and `ep_config()` leaves both VALID with DTOG == SW_BUF (TX) / DTOG != SW_BUF (RX).
  The model must reproduce exactly when hardware moves STAT to NAK in double-buffer mode.

## M3: verify the write paths ☐
- ☐ OTA: stage an update package on the SD, run the stock updater (RAM execution via
      MEMRMP, flash programming, option bytes, radio-stack check, resources) through
      its reboot chain
- ☐ XIP app loaders (custom firmwares that program FAP code into internal flash and
      execute it there)
- ◐ Firmware matrix: official 1.5.1, Momentum mntm-012, Unleashed unlshd-094 pass
      `scripts/smoke_test.py`; add RogueMaster and run it on a schedule

## M4: senses ☐
- ☐ Speaker: TIM16 CH1 PWM → QEMU audio
- ☐ LP5562 engine interpreter (blinks, ramps)
- ◐ Battery/USB power: monitor control done (`qom-set /machine/gauge soc 5`, `/machine/charger usb true`); window UI todo
- ☐ IR TX decoding to the log, IR RX injection

## M5: virtual RF ☐
- ☐ Sub-GHz: CC1101 async RX from `.sub` RAW timings (GDO0 → TIM2 capture), TX capture
      to `.sub`
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
