# Bring-up log

How official firmware 1.5.1 went from "QEMU segfaults" to the desktop running apps from
the SD card, 2026-11-07. Each entry has the symptom, how it was found, and the fix. Read
these before modelling anything new; most traps repeat.

Tools that did the work: the firmware's own USART1 log (`-serial stdio`), QEMU's gdb stub
with the release `.elf` (`llvm-addr2line`, `p pxCurrentTCB->pcTaskName`), sampling `info
registers` through the monitor to histogram PC and IPSR, and QEMU trace points
(`-trace systick_*`, `-trace sdcard_*`, `-msg timestamp=on`).

## Board and SoC

1. **Segfault at machine init.** `DEVICE(&s->soc)` was cast before
   `object_initialize_child()`. Fix: initialise first.

2. **"CS index 0x0 in use".** QEMU 11's SSI bus needs a distinct `cs` property per
   peripheral. Display/SD use 0/1 on SPI2; CC1101/NFC use 0/1 on SPI1.

3. **PGSERR "Known ERRATA" warning at boot.** QEMU's ELF loader zero-fills `.bss` at
   its *load* address. Flipper's `.data`+`.bss` segment has LMA in flash, so the zero-fill
   hit the flash array through the normal store path. DFU never writes there. Fix: the
   flash ignores stores that don't come from a CPU (`current_cpu == NULL`). The launcher
   also converts ELFs itself (file contents only).

4. **Sub-GHz "Init Fail".** No SPI traffic reached the CC1101: the board's pin levels
   (MISO idle-low, SD card detect) were set at machine init, and the first system reset
   released them (a GPIO port reset disconnects external drivers). The CC1101 driver waits
   for MISO (CHIP_RDYn) low, and PB4 fell back to its reset pull-up. Fix: re-apply board
   levels in the machine's reset hook. While there: QEMU's `set_cs(dev, level)` receives
   the line *level*, not "selected".

5. **Backlight never on.** `lp5562_execute_program()` reads ENABLE and writes it back
   with engine bits. The read returned 0, because every I2C1 transaction pulses
   `I2C1RST` (via `furi_hal_bus`) and the RCC hook used `device_cold_reset()`, which also
   resets the devices on the controller's bus. Fix: RCC resets run the peripheral's own
   reset phases only.

6. **Idle never sleeps (busy loop).** EXTI PR1 bit 2 stayed set. RFID's pin reset drives
   PA2 high while EXTI line 2 is masked but still rising-armed (the firmware only disarms
   triggers of enabled lines). `furi_hal_os_is_pending_irq()` treats any PR bit as pending.
   Real devices sleep, so masked lines must not latch on WB55. Fix: PR latches only for
   lines unmasked in C1IMR or C2IMR.

## Upstream QEMU bugs (patches in `qemu/patches/`)

7. **The RTOS tick ran at 1/8 speed** (125 Hz), but only while SysTick was active (WFI
   path, during SD activity). Timestamped `systick_timer_tick` traces showed 1 ms ticks on
   MSI and 8 ms ticks after the PLL switch: LOAD=63999 counted at 8 MHz, which is refclk,
   although CTRL.CLKSOURCE selected the CPU clock. Cause:
   `systick_cpuclk_update()`/`systick_refclk_update()` say "we can ignore … changes" but
   lack the `return`, so the RCC's refclk update (HCLK/8) re-timed SysTick.
   **0001-armv7m_systick-ignore-updates-of-the-unselected-clock.patch.**

8. **SD init looped on CMD0.** `sdcard_*` traces showed `CMD00 arg 0x0001aa87`: CMD8
   (`48 00 00 01 AA 87`) shifted by one byte. `ssi-sd` stays in RESPONSE state for one byte
   after a response; the Flipper driver clocks its padding byte with CS high (which the
   model never sees), so the next command's first byte was swallowed.
   **0002-ssi-sd-accept-a-command-right-after-a-response.patch**: recognise a command
   start (01xxxxxx) there, and ignore non-command bytes when idle.

9. **"CMD17 in a wrong state: ready".** In SPI mode QEMU only entered transfer state on
   CMD9/CMD10 (what Linux and U-Boot send). The spec has no identification phase in SPI
   mode, and Flipper reads sector 0 right after CMD16.
   **0003-sd-spi-mode-enter-transfer-state-after-ACMD41.patch**: report idle until
   power-up, then accept data commands.

All three are small, self-contained, and suitable for sending upstream.

## Things that were right the first time (and why)

- HSEM modelled from RM0434: **reading RLRx is the 1-step lock**. `furi_hal_bt_init()`
  asserts it gets semaphore 5 that way, and the RNG path spins on RLR0. (A Renode project
  treated RLR as a passive view and needed per-semaphore hacks.)
- CPU2 as a FUS rather than absent (crypto-enclave mutex leak; see ARCHITECTURE.md).
- LPTIM one-shot with CMPM/ARRM and a bus reset that drops the IRQ: tickless idle
  worked as soon as the EXTI issue was gone (998 ticks/s, CPU parked in WFI).
- DMA beat-by-beat arbitration: SD block reads and writes worked as soon as the
  card answered.

## Numbers

- Cold boot to "Startup complete": 0.27 s of firmware time with NVRAM. The first boot of
  a new device adds ~2.3 s, because the firmware provisions the fuel gauge (real
  `BQ27220_CONFIG_APPLY_US` delays).
- `snake_game.fap` loads from the emulated SD in 48 ms (firmware's own measurement).
- Idle: the vCPU sleeps in WFI between ticks, so the host CPU stays near idle.
