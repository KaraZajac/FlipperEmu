# Bring-up log

How official firmware 1.5.1 went from "QEMU segfaults" to the desktop running apps from
the SD card, then to a CLI over emulated USB and over-the-air updates, 2026-11-07. Each entry has the symptom, how it was found, and the fix. Read
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

## USB (M2)

10. **BusFault in the USB ISR right after the first bus reset; reboot loop.** The crash
    dump only showed the fault handler's own frame. A gdb breakpoint on
    `BusFault_Handler` plus the stacked exception frame gave PC = `ep_deconfig+34`,
    `strd r2, r2, [r3]` with r3 = 0x40006C00, and CFSR = 0x8200 (precise, BFAR valid).
    GCC clears a buffer-table entry with one 64-bit store, and the packet-memory region
    only accepted up to 32-bit accesses. Fix: `valid.max_access_size = 8` with
    `impl.max_access_size = 4`, so QEMU splits it as the APB bridge does.

11. **Enumeration stalled at SET_ADDRESS (sometimes SET_CONFIGURATION), timing-dependent.**
    The status stage came back STALL. The cause: after a status-OUT stage the firmware
    clears CTR_RX and then re-arms EP0 with `EP_RX_VALID`, a read-modify-write of EP0R.
    The virtual host (in the main-loop thread) could slip the next SETUP in between
    those two accesses. The toggle write then flipped STAT_RX to VALID with a SETUP
    pending, `ep_read()` refused the endpoint, and `usbd_stall_pid()` fired. Silicon has
    the same window, but no real host sends a SETUP microseconds after a status stage.
    Fix: the host waits until EP0 is settled (no CTR, STAT_RX VALID or STALL) before a
    SETUP, and leaves a small gap between transfers. As a general guard, a CPU write
    clears a CTR or ISTR event bit only if the CPU's last read showed it set, which makes
    the firmware's RMW sequences atomic against the host.

12. **QEMU assertion when typing into the CLI.** `fifo8_peek_bufptr(fifo, max, …)`
    requires `max <= bytes queued`; the host asked for a full 64-byte packet. Fix: clamp.

13. **Orphaned emulators.** `kill <launcher pid>` stopped the Python launcher but not its
    QEMU child, which kept the device's flash and SD images open. Fix: the launcher
    forwards SIGTERM/SIGHUP to QEMU.

## OTA updates (M3)

14. **The RAM updater ran twice, the second time in boot mode 4.** The main firmware
    starts the updater by copying it to SRAM1, setting `SYSCFG_MEMRMP` to SRAM and
    jumping (`furi_hal_switch`, no reset). When the updater finishes it resets, and on
    silicon that reload of the boot configuration maps flash back at 0 before the core
    fetches SP/PC. Here SYSCFG restored the alias in its *exit* reset phase, but QEMU's
    M-profile CPU reads its vector table in the *hold* phase, so it booted the updater
    from SRAM again. Fix: the machine reset latches the boot configuration (flash at 0)
    before resetting any device.

15. **Resources, option bytes and splash silently skipped.** To leave out the radio stage
    (the emulated CPU2 can't install a stack), the test deleted the `Radio*` lines from
    `update.fuf`. The firmware reads a manifest's optional keys in file order with no
    rewind, so a missing key leaves the stream at EOF and hides every key after it. Fix:
    blank the radio the way `fbt updater_package` writes a package without a stack
    (`Radio:` empty, zero version), keeping every line. `./flipperemu run --update`
    does exactly this.

16. **Blank LCD after any warm reset (the updater, `power reboot`, a crash).** gdb
    on QEMU itself: the ST7567 had pixel data but `display_on = 0`, and a `dprintf` on
    its command decoder showed the init sequence (`E2` soft reset … `AF` display on)
    only on the cold boot. The bug is in QEMU's STM32L4x5 GPIO model: its reset zeroes
    IDR *before* recomputing it, so a pin that was high and is now low or floating
    notifies nobody. PB1 (the LCD's A0, command/data) was high from the last pixel
    write, so the LCD kept treating the next boot's commands as pixel data. Fix: after a
    machine reset the SoC re-sends every pin level the devices haven't seen. The smoke
    test now reboots through the CLI and checks the LCD comes back. It fails without the
    fix.

17. **Two emulators on one device.** While iterating, a test device's directory was
    recreated while an older QEMU still had the old (now unlinked) images open. The
    stale instance would have written its NVRAM over the new device's on exit. QEMU
    locks disk images, but not after they're deleted. Fix: `run`, `flash` and `sd`
    refuse to touch a device whose monitor socket answers, and there is a `stop`
    command.

18. **Idle burned 112 % of a host core after any update.** Updaters clear the firmware's
    legacy-sleep flag, as on a real Flipper, so the device tried STOP2.
    `furi_hal_clock_switch_pll2hse()` sends `SHCI_C2_SetSystemClock` (opcode 0xFC79, seen
    with `-d unimp`), which the FUS model refused. The function then returns before its
    WFI, so the idle task spins. Fix: CPU2 performs the PLL⇄HSE switch on the RCC and
    answers success. STOP2 then idles at 1–2 % of a core with uptime matching wall time,
    and it is now the default for new devices, as on a real Flipper.

19. **IR decoded by `ir rx` but "Unknown, 66 samples" in the Infrared app.** One fewer
    sample than sent, and a frame the decoder rejected. The injector's QEMU timer runs in
    the main loop. When it fired late, two edges reached TIM2 back to back, both capture
    flags were pending, and the firmware's ISR handles the rising edge (CC1) before the
    falling one (CC2). On silicon the ISR runs microseconds after each edge, and edges
    are at least ~400 µs apart. Fix: an edge waits until the previous capture has been
    serviced. Edges carry exact timestamps, so the durations don't change.

## Things that were right the first time (and why)

- HSEM modelled from RM0434: **reading RLRx is the 1-step lock**. `furi_hal_bt_init()`
  asserts it gets semaphore 5 that way, and the RNG path spins on RLR0. (A Renode project
  treated RLR as a passive view and needed per-semaphore hacks.)
- CPU2 as a FUS rather than absent (crypto-enclave mutex leak; see ARCHITECTURE.md).
- LPTIM one-shot with CMPM/ARRM and a bus reset that drops the IRQ: tickless idle
  worked as soon as the EXTI issue was gone (998 ticks/s, CPU parked in WFI).
- DMA beat-by-beat arbitration: SD block reads and writes worked as soon as the
  card answered.
- Runtime flash programming: the stock updater's erase/program/verify of ~770 KB, the
  option-byte check and the post-update reboot chain worked on the first run that got
  that far. The flash model flushes translated code for every programmed page.
- USB double-buffered endpoints: the STAT semantics derived from libusb's
  `ep_write()`/`ep_read()` (see ARCHITECTURE.md) worked for CDC and RPC the first time
  the control path was race-free.

## Numbers

- Cold boot to "Startup complete": 0.27 s of firmware time with NVRAM. The first boot of
  a new device adds ~2.3 s, because the firmware provisions the fuel gauge (real
  `BQ27220_CONFIG_APPLY_US` delays).
- `snake_game.fap` loads from the emulated SD in 48 ms (firmware's own measurement).
- Idle: the vCPU sleeps in WFI between ticks, so the host CPU stays near idle.
- OTA (`run --update`): Unleashed → official 1.5.1 in 68 s, including writing the
  flash and unpacking 1074 resource files to the SD. Momentum's 11 MB resource
  bundle takes longer.
- USB: 33 ms from D+ pull-up to enumerated: 25 ms of attach debounce, suspend and
  reset timing (as a PC takes), ~8 ms of control transfers. The CLI banner shows within
  a second of opening the pty (QEMU checks a pty for a reader once a second).
