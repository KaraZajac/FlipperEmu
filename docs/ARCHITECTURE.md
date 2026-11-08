# Architecture

FlipperEmu is QEMU 11.1.3 plus an overlay directory, `qemu/hw/arm/flipper/`, which
`scripts/setup_qemu.sh` symlinks into the QEMU tree as `hw/arm/flipper` and hooks into the
arm build with one `subdir()` and one Kconfig `source`. Three small fixes to upstream QEMU
live in `qemu/patches/` (see BRINGUP_LOG.md). Nothing else in QEMU is modified.

Ground rule: **the firmware source is the specification.** Every behaviour below that is
more than "register reads back" is there because a specific firmware function depends on
it; the file comments name that function. Register layouts come from RM0434 and ST's
`stm32wb55xx.h`.

```
                 flipper_zero.c  (board: wiring, OTP, input, NVRAM, pin levels)
                        │
        ┌───────────────┴──────────────────────────────────────────┐
  stm32wb55_soc.c  (SoC: memory map, NVIC lines, DMA requests, GPIO fan-out)
        │   Cortex-M4F (QEMU armv7m, 63 IRQs, MPU, FPU)
        │   rcc pwr flash hsem ipcc(+CPU2) exti syscfg rtc lptim×2 spi×2 i2c×2
        │   dma(+dmamux) rng dwt dbgmcu usb(+virtual host) tim2
        │   gpio×6 usart1 lpuart1 (QEMU L4x5 models)
        │   tim1/16/17 adc aes pka crc comp iwdg … (register files)
        │
   SPI2 ── st7567 (LCD, CS PC11, A0 PB1, RST PB0)   ── ssi-sd + sd-card-spi (CS PC12, CD PC10)
   SPI1 ── cc1101 (CS PD0, GDO0 → PA1)              ── st25r3916 (CS PE4, IRQ → PA2)
   I2C1 ── bq27220 @0x55   bq25896 @0x6B   lp5562 @0x30
   TIM16 CH1 (PB8) ── piezo → flipper-speaker → QEMU audio (desktop or WAV)
   PA0 + TIM2 TI1  ←─ IR receiver ← /machine/ir rx (marks/spaces)
   PA1 + TIM2 TI2  ←─ CC1101 GDO0 (async RX) ← /machine/subghz rx (the "air")
   USB ─── built-in host ── CDC ACM ⇄ chardev "usb" (pty/socket)  ·  HID → text on it
```

## Memory map

| region | address | model |
|---|---|---|
| boot alias | 0x0000_0000 | flash, system memory or SRAM1, selected by `SYSCFG_MEMRMP` |
| main flash 1 MiB | 0x0800_0000 | ROM device: reads hit RAM, stores go through the controller |
| SRAM1 192 KiB | 0x2000_0000 | RAM |
| SRAM2a/2b 32+32 KiB | 0x2003_0000 | RAM (also aliased at 0x1000_0000); IPCC mailbox lives here |
| system memory | 0x1FFF_0000 | stand-in bootloader: vector table + `b .` so "DFU mode" parks |
| OTP | 0x1FFF_7000 | provisioned by the board (FuriHalVersionOTPv2: target 7, name, region) |
| UID64 / UID96 / flash size | 0x1FFF_7580… | derived from the device name |
| option bytes | 0x1FFF_8000 | value/complement pairs; factory = update.fuf "OB reference" |
| DWT / DBGMCU | 0xE000_1000 / 0xE004_2000 | overlaid on QEMU's RAZ/WI PPB default |

## Clocks and time

`rcc` computes HCLK from the clock tree the firmware programs: MSI 4 MHz at reset, then
HSE 32 MHz → PLL M=2 N=8 R=2 → 64 MHz in `furi_hal_clock_init()`. HCLK drives the CPU's
SysTick and the DWT cycle counter, and refclk is HCLK/8. Every `*RDY` flag mirrors its
enable bit, so clock bring-up never stalls.

`DWT->CYCCNT` counts HCLK cycles of QEMU virtual time. It's essential because
`furi_delay_us()` busy-waits on it, even before the scheduler starts (`main()` waits
100 ms to sample the buttons).

Idle: FreeRTOS tickless idle arms **LPTIM1** one-shot (CMP = n-4, ARR = n-1) and executes
WFI with PRIMASK=1. Afterwards it reads CNT and stops the timer with an RCC bus reset
(`APB1RSTR1.LPTIM1RST`). No LPTIM1 ISR exists, so that reset must drop the IRQ line. RCC
bus resets therefore reset the peripheral device itself, *not* its child buses: the
firmware pulses `I2C1RST`/`SPI2RST` around transactions, and resetting what hangs off
those buses would wipe the LED driver, the display RAM and the SD card.

New devices use the firmware's default Sleep Method, **STOP2**. Before stopping, CPU1 asks
CPU2 (`SHCI_C2_SetSystemClock`) to move SYSCLK from PLL to HSE, and back to PLL after
waking. The CPU2 model performs that switch on the RCC. It's the one radio-stack command
it answers: without it, `furi_hal_power_deep_sleep()` returns before its WFI, and the
idle task spins at 100 % of a host core. Updates clear the firmware's legacy-sleep flag,
so every updated device takes this path. `--legacy-sleep` starts a new device with Sleep
Method = Legacy (light sleep only). Either way, timekeeping holds: RTOS uptime matches
wall time across long sleeps.

While the USB cable is attached, the firmware never stops (USB insomnia, as on
hardware). It takes the 1 kHz tick with WFI in between, about 20 % of a host core,
versus 1–2 % with the cable out (`--usb none`, or `qom-set /machine/soc/usb attached
false`).

## Interrupts: EXTI and SYSCFG

WB55 EXTI layout (RTSR1 0x00 … PR1 0x0C … C1IMR1 0x80). SYSCFG `EXTICR` picks the port
per line and remembers every pin's level, so switching ports never fabricates an edge.

**Pending latches only on unmasked lines.** On every Flipper, `furi_hal_rfid_pins_reset()`
drives PA2 high while EXTI line 2 is masked but still rising-armed: `furi_hal_gpio_init_ex()`
only disarms the trigger if the interrupt is enabled. The idle code treats any PR1/PR2
bit as pending, so a latch there would mean no real Flipper ever sleeps. Lines 0–4 map to
NVIC 6–10; 5–9, 10–15 and 20/21 are OR-gated into 23, 40 and 22; RTC alarm, tamper and
wakeup arrive via lines 17, 18 and 19.

## Buses

**SPI**: master mode, transfers complete instantly. TXE is always 1, FTLVL and BSY are 0,
and the RX FIFO is 4 bytes with FRLVL and OVR, because `furi_hal_spi_bus_end_txrx()`
drains it by level. RXNE honours FRXTH. DMA request lines are levels: TX = TXDMAEN,
RX = RXDMAEN && RXNE.

**DMA**: DMAMUX routes requests to DMA1/DMA2 channels. The engine moves one beat at a time
and re-arbitrates after each (priority, then lower channel), so the SD card's DMA2 ch6 (RX)
/ ch7 (TX) pair interleaves byte by byte as on silicon, and transfer-complete interrupts
fire per channel.

**I2C**: I2C v2 with NBYTES, AUTOEND, SOFTEND and RELOAD, repeated start, NACK →
NACKF+STOPF, and the zero-byte presence probe. The firmware polls it.

## CPU2: ST FUS without a wireless stack (`stm32wb55_ipcc.c`)

CPU2 runs ST's closed radio firmware, so it's modelled by behaviour instead:
- `PWR_CR4.C2BOOT` (set by `HW_IPCC_Enable()`) boots it after 5 ms.
- It writes `MB_FUS_DeviceInfoTable_t` (FUS 1.2.0, no stack, UID64) where the
  transport table at SRAM2A + 4·`IPCCBR` points.
- It links an `SHCI_SUB_EVT_CODE_READY / FUS_FW_RUNNING` event into CPU1's
  system queue (spare buffer from the memory-manager table) and raises IPCC channel 2.
- System commands are answered synchronously in the command buffer: `FUS_GET_STATE` →
  idle, `SET_SYSTEM_CLOCK` → the clock switch STOP2 needs (see Clocks and time), and
  everything else → status 0x01, as a FUS with no keys and no stack would.
- Buffers released on channel 4 are taken back.

The firmware reacts exactly as on a stackless device: "C2 boot completed, mode: FUS",
"Stack isn't installed!", "Radio stack start failed". A **dead** CPU2 is not an option:
`furi_hal_crypto_enclave_load_key()` leaks its mutex when BT isn't alive, and the desktop
then deadlocks.

## Flash and option bytes (`stm32wb55_flash.c`)

KEYR/OPTKEYR unlock sequences, double-word programming (two 32-bit stores with PG set,
PROGERR on non-erased targets), 4 KiB page erase and mass erase, option-byte programming
(OPTSTRT) and reload (OBL_LAUNCH → system reset with OBLRSTF). EOP latches only when EOPIE
is set, because the firmware does `furi_check(FLASH->SR == 0)`. Programmed pages are
flushed from the TCG translation cache (`memory_region_flush_rom_device`), so freshly
written code runs; that's what XIP app loaders and the OTA updater need. With a
`-drive if=pflash` image every program/erase is written through to the file. Pages from
SFSA (0xD7) up belong to CPU2 and refuse writes.

## USB (`stm32wb55_usb.c`)

QEMU has no USB wire that leaves the machine, so the device model contains the other end
too: a small **virtual host**. Device side, modelled to what the firmware's driver
(`lib/libusb_stm32/src/usbd_stm32wb55_devfs.c`) relies on:
- **EPnR**: STAT_RX/STAT_TX/DTOG_RX/DTOG_TX toggle when written with 1, CTR_RX/CTR_TX are
  rc_w0, SETUP is read-only, EA/EP_TYPE/EP_KIND are plain fields. A bus reset clears
  everything except the CTR bits.
- **ISTR/CNTR**: CTR, DIR and EP_ID are derived from the endpoint registers
  (isochronous/double-buffered first, then the lowest number); event flags are rc_w0; CNTR
  masks everything onto USB_LP (IRQ 20).
- **Packet memory**: BTABLE and 1 KiB of PMA. The buffer table holds {ADDR_TX, COUNT_TX,
  ADDR_RX, COUNT_RX} per endpoint as 16-bit words. The RX buffer size comes from
  BL_SIZE/NUM_BLOCK. `ep_deconfig()` clears an entry with `STRD`, so 64-bit accesses must
  be split, not refused.
- **Double-buffered bulk** (the CDC data endpoints): the USB side uses the buffer DTOG
  selects and software the one SW_BUF selects; the endpoint NAKs while they are equal.
  libusb pins down the visible STAT bits. `ep_write()` accepts an IN endpoint only when
  STAT_TX reads NAK, so STAT_TX reads NAK while DTOG_TX == SW_BUF and VALID otherwise.
  `ep_read()` accepts an OUT endpoint only when STAT_RX reads VALID, so STAT_RX keeps its
  programmed value.
- **BCDR**: charger detection reports a standard PC port while the cable is attached;
  DPPU (the D+ pull-up) is "connect".

The host runs in QEMU's main loop, alongside the vCPU thread. Two measures stop it from
racing the firmware's read-modify-write sequences on EPnR and ISTR:
1. The CPU clears a CTR or ISTR event bit only if its last read of that register showed
   the bit set. A set that lands between the firmware's read and write survives, which
   makes the RMW atomic.
2. The host never touches an endpoint while one of its CTR bits is pending. It also waits
   until EP0 is re-armed (STAT_RX VALID) before the next SETUP.

Host sequence, as a PC does it:
1. On DPPU: debounce, report suspend, then resume plus bus reset. `usb.connected`
   (insomnia, CDC "connected") only becomes true on the WKUP event.
2. GET_DESCRIPTOR(device, 8), SET_ADDRESS, then the device, configuration and string
   descriptors, SET_CONFIGURATION.
3. Bind the class:
   - **CDC ACM** (CLI, protobuf RPC): SET_LINE_CODING, then SET_CONTROL_LINE_STATE whenever
     a client connects to or leaves the chardev (DTR is what starts and stops the
     firmware's CLI session). Bulk OUT is fed from the chardev. Bulk IN is read only while
     a client is connected, like a closed tty.
   - **HID** (Bad USB, USB remote): poll the interrupt endpoint every bInterval and decode
     Flipper's report IDs (1 keyboard, 2 mouse, 3 consumer) or boot keyboards. Output is
     text in a US layout: combos as `<CTRL c>`, Alt+Numpad decoded like Windows does.
   - Anything else is enumerated and configured, but not bridged.
4. On DPPU low (mode change, reinit), or `attached=false`, the host forgets the device.
   With `attached=false` the device sees a suspend, and VBUS drops at the charger.

Mode switches re-enumerate on their own. Opening Bad USB turns the CDC port into a HID
keyboard (`046d:c529`); leaving the app turns it back.

## Resets and the OTA updater

A system reset (SYSRESETREQ, or `OBL_LAUNCH` after option-byte programming) goes through the machine
reset hook, which does what silicon does around the core's reset:
1. **Latch the boot configuration first**: flash goes back to address 0 before any device
   resets. The firmware starts its updater by copying it to SRAM1, remapping SRAM to 0
   and jumping, and the updater's final reset must boot the new flash. QEMU's M-profile
   CPU fetches SP/PC in the hold phase, before SYSCFG's own reset would undo the remap.
2. Reset all devices, then re-apply the board's pin levels (buttons, card detect, MISO).
3. **Re-send pin levels the devices missed**: QEMU's L4x5 GPIO reset clears IDR without
   telling anyone, which left the LCD's A0 line stuck high after warm resets.

`./flipperemu run --update PKG` stages an update package on the SD card as qFlipper
does, then sends `update install` over the USB CLI. The emulated CPU2 can't install a
radio stack, so the launcher blanks the package's radio stage. It keeps every manifest
line, because the firmware reads the manifest's optional keys in order and stops at the
first missing one.

## Timers (`stm32wb55_tim.c`) and the IR receiver

TIM2 is a real timer model, written for TIM1/16/17's layout too:
- The counter runs in virtual time and is computed lazily (PSC, ARR, CNT, CEN, UG,
  one-pulse mode). Update and compare events set their flags as time passes. A QEMU timer
  is armed only for events with an enabled interrupt, rounded up so it never fires early.
- Input capture takes edges that the board reports with an exact virtual timestamp.
  CCxS selects direct or indirect inputs, CCxP/CCxNP the polarity, and CCxOF flags
  overcapture.
- The slave controller's reset mode works on TI1FP1, TI2FP2 and TI1F_ED.

`furi_hal_infrared` uses exactly that for receiving. A falling edge (a mark starts) is
captured into CCR2, and a rising edge into CCR1, which also resets the counter. CC3 is
the end-of-signal timeout.

The IR receiver (`/machine/ir`) drives PA0 and TIM2 TI1, idling high and going low for a
mark. Writing durations to its `rx` property plays them. On silicon the capture
interrupt is serviced long before the next edge. QEMU's main loop can run late and
deliver two edges together, which would reorder them for the firmware's ISR. So each
edge waits (up to 10 ms) until the previous capture has been serviced; timestamps keep
the measured durations exact. `scripts/fe_ir.py` builds signals with a port of the
firmware's common encoder (pulse distance/width, Manchester) and its per-protocol data
layouts.

## Storage

The SD card is QEMU's `ssi-sd` + `sd-card-spi` (with two fixes, see BRINGUP_LOG), behind
CS PC12. The board holds card detect (PC10) low when an image is attached. `make_sdcard.py`
builds FAT32 images from official resources (`.tgz`), update packages (heatshrink
`resources.ths` or `.tar.gz`) or a directory, using mtools, so no root is needed. Current
firmware keeps `/int` on the card as `/ext/.int`.

## Board

- **ST7567**: 132×65 RAM, page/column addressing, SEG/COM direction, start line, inverse,
  all-on, display on/off, contrast. The panel's top row is COM63, which makes the
  firmware's normal mode (A0/C8) upright and its left-handed mode (A1/C0, x offset 4)
  rotated 180°.
- **BQ27220**: standard commands, MAC subcommands, unseal/full-access keys, CFG_UPDATE,
  and a data memory written with the firmware's own checksum protocol. A fresh gauge is
  provisioned by the firmware once (~2 s), then persisted in `nvram.bin`. QOM properties
  `soc`, `voltage-mv`, `current-ma` and `temperature-dk` set the battery.
- **BQ25896**: register file with ADC readings derived from the gauge; property `usb`
  plugs in a charger.
- **LP5562**: register file. W PWM is the backlight and R/G/B the status LED, both drawn
  next to the LCD.
- **CC1101**: status byte, header/burst/strobe framing, config and status registers,
  MARCSTATE, PATABLE, and GDO0/GDO2 driven from IOCFG (self-test passes). Receive: a
  signal written to `/machine/subghz`'s `rx` property (+high/−low µs at a frequency, or
  `file:PATH` for long captures) plays on "the air". With IOCFG0 = 0x0D (async serial
  data), GDO0 follows the carrier while the chip is in RX and tuned within 500 kHz, and
  RSSI reads −40 dBm (−100 dBm otherwise). GDO0 is PA1 and TIM2 TI2, so
  `furi_hal_subghz_start_async_rx` captures it the same way the IR receiver path does,
  with the same pacing.
- **ST25R3916**: register spaces A and B, IC identity 0x2A, IRQ status (clear on read) and
  mask registers driving the IRQ pin, oscillator-ready and VDD measurement.
- **Speaker**: the piezo on PB8 is TIM16 CH1 in PWM mode. Every TIM16 write recomputes
  the tone: HCLK / ((PSC+1)(ARR+1)), duty CCR1/(ARR+1), and sound only while CEN, CC1E
  and MOE are set. `flipper-speaker` plays that pulse wave, with DC removed, through a
  QEMU audio backend (`-M …,audiodev=`), so the volume follows the duty cycle as on the
  piezo. It closes its voice from an exit notifier, because QEMU never finalizes
  machine devices and the `wav` driver only writes its RIFF sizes on close.
- **Pins held by the board** (re-applied after every reset, because a GPIO port reset
  releases external drivers): SD card detect, idle-low MISO lines (the CC1101 driver waits
  for CHIP_RDYn on MISO), and released buttons.

## Persistence per device (`devices/NAME/`)

| file | real-world equivalent |
|---|---|
| `flash.img` | the STM32WB55 internal flash |
| `sd.img` | the microSD card |
| `nvram.bin` | coin-cell / gauge state: BQ27220 data memory, RTC calendar offset, BKP0R–BKP19R |
| `firmware.elf` | symbols for gdb (copied when flashing an ELF) |
