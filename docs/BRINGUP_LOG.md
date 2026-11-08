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

20. **The first Sub-GHz transmission hung QEMU.** A host gdb backtrace showed the main
    loop inside `dma_run`, called from the timer's update. The DMA model is
    level-triggered and runs synchronously while a request is high, and the timer only
    lowered its request after `qemu_set_irq()` returned. So one update DMAed the
    circular buffer a million times. Fix: the DMA's write to the timer acknowledges the
    request, as the hardware handshake does.

21. **About 3 % of IR frames decoded as RAW garbage** (durations like 1073741254, i.e.
    −570 in the firmware's 30-bit LevelDuration). Pacing on CCxIF released the next edge
    between the ISR clearing the flag and reading CCRx, so the capture was overwritten
    first. Then the vCPU sometimes lagged virtual time by more than the 10 ms cap
    (virtual time runs with the host clock even when the vCPU thread isn't). Fix: wait
    for the CCRx *read*, with a 250 ms cap. 200/200 frames decode in a soak test.

## 125 kHz RFID (M5)

22. **EM4100 read, but HID (FSK) tags never did: "Read overrun, recovering".** The raw
    capture (`rfid raw_read` + `raw_analyze`) showed exact 32/40 µs timings and found
    the card, so the signal was right and the throughput was wrong. An FSK tag produces
    ~28k edges/s. The tag polled every 10 µs while a capture was pending, which kept
    QEMU's main loop at 64 % of a core and starved the vCPU through the BQL. Worse, the
    exact-timestamp catch-up delivered whole stretches of edges at ISR speed. The ISR
    kept up, but `lfrfid_worker` decodes in a thread with ~200 ms of buffers, and those
    bursts overran it. Fix: TIM2 notifies when the firmware reads its last pending
    capture, so there is no polling. Any delay is added to the tag's own timeline
    rather than caught up, and timestamps on that timeline keep the durations exact.
    HID now reads in 0.8 s with no overruns.

23. **The capture ISR restarts the counter in software.** `furi_hal_rfid`'s ISR clears
    CC4IF, writes CNT = 0, then reads CCR4. With paced edges the next edge can be
    stamped *before* that write, which would give garbage durations. Fix: a CNT write
    made while a capture is being serviced (flag cleared, CCRx unread) restarts the
    counter at that capture's time, as a prompt ISR would.

24. **PSK tags were never read in auto mode.** `rfid read indala` found an Indala26 in
    1 s, but plain `rfid read` never left ASK. In `lfrfid_worker_read_internal`, the
    2 s switch timeout is only checked after a buffer of edges arrives (`buffer ==
    NULL` → `continue`). A real comparator is never silent, but the emulated one was.
    Fix: field noise while no tag answers, made of short spikes and long gaps. It
    doesn't decode and doesn't trip the "card sensed" ratio. With no tag the reader now
    alternates ASK/PSK every 2.5 s, as on hardware.

25. **Emulation: only one of two DMA channels on TIM2_UP fired.** The level-triggered
    DMA served ch1 (ARR), whose write acknowledged the timer's request, so ch2 (CCR3)
    never saw it. On silicon both channels transfer. Fix: a request edge latches a beat
    for every channel that selects it. Batched updates then ran their DMA from inside a
    TIM2 register access, and QEMU blocked that ("Blocked re-entrant IO"). TIM2's
    region now opts out of the guard; `tim_write` already handled it. The firmware's
    EM4100/H10301/Indala26 emulation now matches the Python encoders cycle for cycle.

## Infrared transmit (M4)

26. **The last mark of a transmission ran ~1 ms long (or 9 ms).** `furi_hal_infrared`
    keeps TIM1's update DMA in normal mode, and an ISR re-arms both channels at every
    buffer boundary. The last buffer is two 1-period spaces, so that ISR has 26 µs on
    silicon. The emulated vCPU was sometimes later than the next update, which then
    found the polarity channel dry, and the carrier stayed on until the counter
    stopped. Fix: an update waits, keeping its exact time, until the channels that
    served the last one are re-armed (`wb55_dma_ready_for`). A 5 ms cap still let
    one in three overrun on a heavily loaded host. With a 250 ms cap (a real end
    stops the counter anyway), 20/20 transmissions of six protocols come out within
    one carrier period of the encoder.

27. **Firmware finding: an IR transmission garbles the next DMA2 ch1 user.**
    After `ir tx`, the next `subghz tx` lost its start, or transmitted a 256-entry
    loop forever. DMA2 ch1 carries the IR transmitter's polarity stream with only TC
    interrupts enabled, so its HTIF is set and nothing clears it.
    `furi_hal_subghz_start_async_tx()` then reuses ch1 (`LL_DMA_Init` keeps flags)
    and enables HTIE, and its refill ISR runs at once, before the first fill. It
    consumes the first half-buffer of the signal; if the signal ends inside that
    refill, it also disables its own interrupts, and the circular buffer repeats with
    no end marker. `rfid emulate` after `ir tx` likewise glitches its first
    half-buffer once. The emulator follows RM0434 here (HTIF is set whether or not
    HTIE is), so a real Flipper should do the same. This has not been checked on
    hardware. The smoke test runs the Sub-GHz checks before `ir tx`.

28. **Firmware finding: 32-bit Radio Key cards don't round-trip.** Porting the
    encoders for `fe_rfid.py` turned up a mismatch. `protocol_securakey.c`'s 32-bit
    encoder writes encoded bits 38–44 from data bit 3 (`bit_lib_copy_bits(...,
    38, 7, protocol->data, 3)`), but its decoder, and the 26-bit encoder, use data
    bit 9. A 32-bit card read by a Flipper is therefore emulated with the wrong
    facility code. `fe_rfid.py` keeps the firmware's behaviour (marked `sic`), and
    `rfid_check.py` tests Radio Key with a 26-bit card.

29. **All 24 LF protocols verified both ways.** `rfid_check.py` emulates each one on
    the firmware, compares the capture with the Python encoder cycle for cycle, and
    reads the Python tag back with `rfid read`. Several encoders carry state across
    frames (FDX-B, Jablotron and GProxII carry their level; AWID and Pyramid their FSK
    phase), so their loops span two or four frames. `capture_frame()` therefore looks
    for the period from the middle of a capture: zero runs repeat inside a frame
    (Pyramid has 70 zero bits), and the start can carry a glitch (entry 27). The
    fastest FSK tags sometimes failed to read; see entry 30.

30. **FSK reads stuck in ASK: a livelock.** After an EM4100 read and an emulation,
    `rfid read` of an H10301 card failed 5 times in 6 (on Momentum and official). The
    field state showed the reader never leaving ASK. The stats showed 20k edges/s
    delivered, every capture correct (32/40 µs, 64/80 µs), and thousands of paced
    waits; gdb samples put the guest in the comparator and capture ISRs. The guest
    took ~73 µs from an edge to reading its capture, more than an FSK tag's 32 µs
    gap. So pacing released each edge at the read, and the ISRs ran
    back to back. The decoding thread never ran, no buffer reached it, and the 2 s
    switch timeout is only checked when one does. Some transient tips the system
    into this state, and pacing then holds it there. Fix: after a capture read, the
    next edge also waits 3x the measured capture latency, at most 8x the tag's own
    gap. The guest then always has time between edges, and fast FSK tags slow down
    on a slow host (1.5–2.7 s reads). Results:
    - the failing sequence passes 6 times in 6;
    - all 24 protocols pass both ways;
    - with the emulator pinned to one core next to a CPU hog, HIDProx, HIDExt,
      FDX-A, Pyramid and AWID read in 1.4–2.2 s.

    A dead end first: stretching the tag while the vCPU was not halted (WFI). With a
    CLI session open, FreeRTOS's idle task spins instead of sleeping, because
    something wakes sooner than `configEXPECTED_IDLE_TIME_BEFORE_SLEEP` (4 ticks). So
    the guest never looks idle, and the tag ran at 1/16 speed.

## NFC (M5)

31. **NFC needed TIM17.** After every answer, `furi_hal_nfc` starts a one-pulse
    "block TX" guard timer on TIM17 and waits for its update interrupt before it
    transmits again. TIM17 was a plain register file, so it never would have fired.
    It is now the timer model too, on the vector it shares with TIM1's trigger and
    commutation interrupts. The frame waiting time is TIM1's, and the frame model
    raises RXS 40 µs after TX end so that the chip's interrupt always beats that
    timer's.

32. **An Ultralight EV1 dump read "UID: , Pages Read: 0/0".** The debug log (set
    through gdb: `set var furi_log.log_level = FuriLogLevelDebug`) said "Read
    signature failed". That dump comes from a non-NXP clone with an all-zero
    signature, and the converter had dropped it, so the card NAKed READ_SIG. Every
    EV1 and NTAG21x answers READ_SIG; the card now does too.

33. **Crypto1 interoperated on the first run.** The firmware's `crypto1.c` is GPL-3,
    which cannot go into GPL-2 QEMU. So the card's Crypto1 is written from the
    published description, as a plain 48-bit LFSR, and only the bit-order conventions
    were read from the firmware. Before any C, a check confirmed that the paper's
    boolean filter functions reproduce the reference tables under the chosen indexing.
    The NFC app then read a virtual 1K with 32/32 keys and 16/16 sectors.
    - A dump with a key-B-only sector reads 15/16, which is the firmware's own logic:
      it reads with the key it found first and moves on after a NAK.
    - Two bugs were caught in review before the first build: {aR} begins at air
      bit 36, not byte 4, and the nonce must come from the 16-bit LFSR.

34. **Smoke test: `^C` does not leave the NFC shell.** In the `nfc` sub-shell, Ctrl+C
    only cancels the line, and its prompt `[nfc]>: ` also ends in `>: `. So the next
    command went to the NFC shell ("could not find command"). `exit` leaves it. The
    Classic check writes the NFC app's key cache (`/ext/nfc/.cache/UID.keys`) over the
    CLI so that `dump` can authenticate every sector without the GUI.

35. **Card emulation: the chip does the anticollision.** When the Flipper emulates a
    card, the firmware loads the ST25R3916's passive-target memory (UID, ATQA, a SAK
    per cascade level) and sets target mode. The real chip then answers REQA,
    anticollision and select in hardware and raises only I_wu_a. So the virtual reader
    does the same from that memory, then puts each command in the FIFO behind
    I_rxs/I_rxe and takes whatever the firmware TRANSMITs as the answer. The official
    firmware's NTAG215 emulation read back identically: UID, version, signature, all
    135 pages and the counter. `--reader scan` also activates a MIFARE Classic
    emulation. Classic's encrypted answers go out by transparent mode on a bit-banged
    pin, which is not decoded.

36. **Unleashed crashed in USB_LP during the NFC checks: `furi_check failed`.** The
    crash LR (0x0808c859) pointed at Unleashed's `cli_vcp_signal_internal_event()`:
    `furi_check(furi_message_queue_put(queue, &event, 0) == FuriStatusOk)`, with r0 =
    FuriStatusErrorResource, a full queue. The CLI's CDC service queues one event per
    completed packet in an 8-entry queue and takes one per pass. Its thread also
    re-arms the endpoints from the pipe callbacks.
    - A trace of every transaction, written to stderr, hid the race. A ring buffer
      dumped when the firmware dropped off the bus caught it. The crash came during
      the 1.2 KB `storage write_chunk` upload of the key cache. Every OUT packet was
      read at once, but by the "space freed" callback, so the queued Rx events piled
      up behind the reads. The ninth Rx event (r5 = 5, `usbd_evt_eprx`; r6 = 1, the
      CDC OUT endpoint) crashed it.
    - The host was answering 20 µs after each re-arm, while the emulated firmware took
      150–700 µs from interrupt to re-arm. The PC was under heavy IO pressure. On a
      real STM32 that is a few microseconds against a 51 µs packet.
    - Same code in official 1.5.1 (`cli_vcp.c`, pipe, event loop), so this is not an
      Unleashed bug; Unleashed's timing just hit it. A full queue in the ISR is a crash
      rather than a lost wakeup, so a slow consumer takes the device down.
    - The fix is in the host. Bulk transactions now take full-speed bus time. The host
      also waits twice the firmware's measured turnaround (a packet's completion to the
      CPU re-arming that endpoint) after each re-arm (ARCHITECTURE.md, USB).
    - Unleashed then ran 3/3 clean. On the loaded host, CLI transfers dropped from 65 to
      30 KiB/s; the firmware itself needed ~1 ms per packet there.
    - The first version paced the HID interrupt endpoint too, and Bad USB typed "Hlo"
      for "Hello". `hid_send_report()` waits only `HID_INTERVAL * 2` ms for the previous
      report, then drops the next one. Interrupt endpoints keep plain bInterval polling.
    - The smoke test had sent QEMU's output into a pipe it never read; a full pipe
      would stall QEMU. The output now goes to `qemu.log` in the device directory, and
      `--keep` keeps that directory.

37. **The virtual NFC reader gave up after one silence.** On the same busy host, the
    reader sometimes read 120 or 128 of 135 pages: one READ unanswered within 20 ms
    ended the read. It now waits 100 ms and sends a command up to three times when the
    answer is missing or garbled (a NAK is final). It also leaves 1 ms between an
    answer and the next command, instead of 0.5 ms.

## Things that were right the first time (and why)

- **The ST25R3916 frame model.** `nfc scanner` found the first virtual NTAG215 on the
  first build: the TX/RX interrupt sequence, the FIFO status bits and 3A activation
  with a 7-byte UID.
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
