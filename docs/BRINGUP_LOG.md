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

All three are small, self-contained, and suitable for sending upstream. Two more came
later: 0004 (open-drain GPIO lines, entry 40) and 0005 (a register read without the
big lock, entry 92).

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

## 125 kHz RFID: writing and field detection (M5)

38. **T5577 writes verified on the first run, all 24 protocols** (23 with `rfid write`;
    Radio Key, whose name the CLI cuts at its space, through the app). The T5577's downlink
    is the reader's own field. The firmware pauses TIM1's outputs for gaps and times
    them with `furi_delay_us()`, and DWT->CYCCNT counts virtual time, so the gaps reach
    the board model with exact timestamps. The uplink reuses the conventions of the
    firmware's emulation encoders, which its T5577 write requests are made for. One
    convention was the opposite of the first guess: FSK2a means "1 = RF/10", because
    HID's encoder sends a 0 as 8-cycle periods and a 1 as 10. The EM4305 commands the
    firmware sends after each T5577 write decode as an opcode 0x, which a real T5577
    ignores too; logged block writes confirm they changed nothing.

39. **The NFC/RFID detector app froze QEMU (the monitor stopped answering).** The main
    thread ran at 100 % while the vCPU sat idle. gdb showed `tim_run_updates()` for
    TIM1 replaying update events 35 s behind virtual time, each event inside
    `dma_run()`. RFID field detection runs TIM1 at 1 kHz with two circular DMA channels
    on TIM1_UP. Every 100 ms they copy TIM2's counter (the reader's carrier on ETR) to
    RAM and clear it. They never touch TIM1, so the request line, held high during the
    synchronous run, was never "acknowledged" by an access to the timer. `dma_ready()`
    took the level as a request, and each update ran the 1M-beat guard. On silicon the
    DMA acknowledges a timer's request with its first transfer. TIM1_UP and TIM2_UP are
    now pulse requests: one beat per channel per edge (the latch).
    - The same session found that screenshots could catch a half-written screen dump;
      `fe_monitor.screenshot()` now waits for the dump's size to settle and the image
      to decode.

## iButton (M5)

40. **QEMU's GPIO model could not let a 1-Wire key release the line.** The first read
    saw one falling edge and then nothing. `stm32l4x5_gpio` refused an external high
    on any output pin, open drain included. A MODER or OTYPER write also disconnected
    an open-drain pin held high from outside (dropping the pull-up), and
    `update_gpio_idr()` had no case for an open-drain pin connected high. So the pin
    floated and kept reading low after the master's first low. All three are fixed in
    `qemu/patches/0004`.

41. **SEARCH ROM failed at a random bit: the master read a 0 as a 1.** The slave held
    each 0 for a fixed 30 µs. Virtual time is host time, so when the host preempts the
    vCPU during the master's 9 µs wait, the master samples past the hold, sees both the
    bit and its complement as 1 (no device), and gives up. Releasing on CYCCNT reads
    made it worse: the read that ends the master's wait came first and let go just
    before it sampled. So the slave now lets go once the master has sampled. The SoC
    overlays every GPIO port's IDR and tells the board after each read, and a GPIOB
    read with the firmware's drive of PB14 off is the sample.

42. **Overdrive (DS1996) write slots misread.** The master's overdrive write-1 is a
    1 µs low, which took 2–4 µs emulated; 480 slots measured exactly 4 µs, the old
    threshold. Judging a bit by its slot's shape (low against the following high) fixed
    those but broke the last bit of each byte, whose high includes the master's
    function-call overhead (`33` read as `B3`). The master busy-waits at least the
    nominal time and only ever runs longer. So the threshold now sits just under a 0's
    time (56 µs, 7 µs overdrive), and a 1 may stretch that far.

43. **Cyfral and Metakom: slow reads, then a wrong key.** The firmware times these
    edges with DWT->CYCCNT inside the COMP ISR. With emulated interrupt latency, Cyfral
    took 11 s or failed, and Metakom once read `EB77BB0F` for `C355330F`: two 41 µs
    segments per byte measured as 82 µs, which keeps the byte's parity valid. STOP2
    was not the cause (`--legacy-sleep` was no better).
    - Pacing each edge on the ISR's read of COMP1 helped (0.4–6 s) but still misread
      once.
    - Exact now: the ISR's CYCCNT read is stamped with the edge's own time
      (`wb55_dwt_stamp_next`). On silicon the ISR runs within a fraction of a
      microsecond, so that is the value it would read. Sixteen reads of assorted values
      then all decoded correctly, most in 0.4 s.

44. **Blank writes: the slave hid the master's edges from itself.** RW1990.1 and TM01x
    wrote at once; RW1990.2 and TM2004 never did. The firmware's verify stops reading
    the ROM at the first byte that differs and goes straight on to the next blank's
    reset. The slave, still sending, took that reset's falling edge for a read slot
    and pulled the line. While a slave holds the line low, the pin shows none of the
    master's edges. So it saw the reset only as its own release, answered presence
    after the master had already sampled, and held that into the next command (`1D`
    arrived as `6D`). Now the SoC overlays each GPIO port's whole register block and
    reports writes as well as reads. The slave follows the master's own drive (MODER
    and ODR of PB14): a new master slot ends whatever the slave still holds, and a
    reset is a reset whatever the line shows. TM2004 then only needed its address and
    data states added to the receiving states.

45. **Reading an emulated key: a master paced by the slave.** The firmware's 1-Wire
    slave catches a reset by EXTI, then polls PB14 with interrupts off, timing every
    wait on DWT. A master timed on the virtual clock missed it: the vCPU runs at its
    own pace between polls. So the virtual reader acts on the slave's IDR reads. It
    starts a slot at the second read of a wait, lets go of a 1 at the read that saw the
    slot, and takes a read slot's bit from the slave's own writes. DS1990, DS1992 and
    DS1971 read at once. A DS1996's 8 KB stalled after 713, 2854 and 4083 bytes:
    a host stall longer than a 135 µs window had made the slave take a reset and drop
    out. So the reader now resets it and resumes READ MEMORY (SKIP ROM, `F0`, the
    address) at the last whole byte.

46. **Resumed reads came from the wrong address.** Random memory (not zeros) showed
    it: the bytes after about half the resumes came from an address with one 0 bit
    turned to 1 (+4, +128, +4096...). The reader held a written 0 for 30 µs from the
    read that saw the slot. The slave times its 20 µs window from just after that read,
    so a host stall between the two shortened the hold below the window, and the 0
    read as a 1. The hold is now timed from the slave's next read, the first one in
    its window. The slave's elapsed time then exceeds the hold before it can see the
    release. Most "natural" dropouts went with it; they had been misheard 0 bits in
    command bytes. Each resume also rereads the 4 bytes before it, and they must match.
    With forced dropouts every 13 bytes, a DS1996 read correctly through 638 resumes.

47. **A presence pulse inside a read slot.** One DS1996 read gave up at byte 8102: a
    stored byte had a 0 in bit 7. A stall had made the slave take a short reset just
    before the slot (the pin was already high, so it guessed overdrive). Its overdrive
    presence pulse, a write low and then a release, landed inside the read slot and
    read as a 0. Every later resume's check then failed against that byte. But a slave
    sending a bit never reads the pin between seeing the slot and its write (its
    delays are DWT waits), and the reset path does. So any IDR read during a read slot
    now counts as a dropout. A mismatch on resume also drops the bytes it checked
    against, so a bad byte is read again. And the give-up count is retries without a
    new byte, not retries in total.

48. **Cyfral and Metakom emulation: TIM1 updates were the wrong clock.** The
    firmware's ISR sets ARR, then the pin, at every TIM1 update. The board first
    recorded the pin at each update, but the timer model runs an interrupt-only timer
    by catch-up and never reported those updates. Once it did, the captures held runs
    of equal levels: the guest's ISR ran 20–80 µs late (each interrupt wakes the vCPU
    thread from WFI) and sometimes missed an update, so TIM1 repeated the old period.
    The capture now comes from the ISR's own writes: each BSRR/BRR of PB14 while TIM1's
    update interrupt is enabled, held for ARR+1 ticks. That is exactly the encoder's
    level/duration sequence, and the timer model is unchanged. The decoder also had a
    bug: a Metakom frame's last low runs into the next sync, so its last bit is now
    read from the high alone.

49. **A lost ^C took the CLI with it.** The smoke test's new emulate check stopped
    `ikey emulate Dallas` with a ^C and closed the serial port at once. QEMU's pty
    chardev stops reading at the hangup, so that byte can be lost. The command ran on,
    and the next check's `ikey emulate Metakom` line waited in its input. That check's
    own ^C then stopped the Dallas emulation, the waiting line started a Metakom
    emulation nobody stopped, and every later CLI check found no prompt. So a ^C is now
    followed by waiting for the prompt (`cli.watch` already did this). A capture is
    also written as soon as it is full, so an emulation left running still yields one.
    It used to end after 100 ms without a level, and once on Unleashed that cut a
    Cyfral capture after 75 levels. The virtual clock runs on through a host stall of
    the vCPU, so the gap was the host's, not the firmware's. A capture now ends when it
    is full or when the firmware lets go of PB14, and only falls back to a 1 s timer.

## Real radios (M7)

50. **A live stream, not a one-shot.** The CC1101's air was one finite signal, and its
    RSSI was −40 or −100 dBm. A real radio needs more, and the SDR bridge provides it
    over a socket (`docs/RADIO_LINK.md`):
    - a timeline that never ends, tied to the chip's tuning. An epoch drops data still in
      flight from before a retune, and a 60 ms jitter buffer joins the chunks;
    - RSSI that answers at once at any frequency. The Frequency Analyzer spends 2 ms per
      frequency (650 kHz wide), then sweeps ±300 kHz in 20 kHz steps 58 kHz wide. The
      hopper stays only where its first read after retuning is over −90 dBm. So the
      bridge sends the band's power bin by bin, and the model sums the bins inside the
      channel.

    With the noise floor at −167 dBm/Hz, a 650 kHz channel reads −108.9 dBm with no
    signal, and −74 dBm with a carrier 30 dB over the noise (the estimate was −74). The
    analyzer then shows 433.939 MHz for a transmitter at 433.95 MHz: the nearest of its
    20 kHz steps.

51. **Slicing OOK: where the threshold sits.** On synthetic signals, highs came out
    30 µs long, because the midpoint in dB sits far below the peak and is crossed early
    on the rising ramp and late on the falling one. The midpoint in linear power fixed
    that, to within 3–10 µs. Real recordings (the rtl_433 test corpus) then showed what a
    real transmitter does:
    - a KeeLoq HCS200's highs ripple by 3 dB at 21 kHz, a beat with another carrier, and
      its gaps sit 10–17 dB over the floor. A threshold 3 dB under the peak broke the
      highs up. The CC1101 slices at its decision boundary (AGCCTRL0: 8 dB in the
      Flipper's presets), now announced and used, with the smoothing cut to 1/16 bit so
      highs are only about 10 µs long;
    - a Nice FLO capture's first 130 ms is 8-bit noise, a few quantisation levels, whose
      10th percentile lies 10.5 dB under its peaks. That opened the 10 dB squelch, and the
      noise came out as 30 µs pulses. The floor is now the frame's median (still falling
      at once and rising slowly), and the squelch 12 dB.

    The firmware's own `subghz rx` then decodes the PT2262 capture (`Key:0x00755555`,
    which is rtl_433's id 30037, cmd 85), both EV1527 remotes, a TE275 remote and the
    HCS200 (serial 0174ACD and hop A44415AA, as rtl_433 has them). The Nice FLO captures
    don't decode: the firmware's Nice FLO decoder wants a 25 ms ±7 ms gap before a frame,
    and the capture's only frame follows a 58 ms one. That is a gap in the recording, not
    in the bridge.

52. **A channel wider than the SDR's band.** No real recording decoded at first, though
    the bridge's own output was right (480/1480 µs, PT2262). The bridge called the
    AM650 channel (650 kHz) "off the band" of a 250 kS/s recording, because it wanted
    the whole channel inside the band, and so it sent nothing. Now only the channel's
    middle has to be in the band, clear of its edges. What the SDR doesn't cover is
    noise the CC1101 would have filtered anyway.

53. **2-FSK: one gate, one delay.** The first high of every burst came out 29 µs long.
    The gate (power over the squelch) opened at once, at 1% of the power ramp, while
    every frequency transition came half a smoothing window late. Gating at the power's
    midpoint gives the gate the discriminator's delay, at the start and at the end, and
    the trailing 14–39 µs glitches went with it. Timings are now within 2–12 µs at all
    three of the Flipper's deviations (47.6, 12.7 and 2.38 kHz), down to 12–15 dB SNR.

54. **A card from a program: time has to stand still.** The firmware's NFC poller
    waits forever for an event, but its timeout is an MCU timer on the virtual clock:
    the frame waiting time, 0.1 ms to tens of ms. A card on the other end of a socket
    can't answer that fast, and real hardware over USB even less. So each poll on the
    NFC link stops the machine (and the virtual clock) until the answer comes. The first
    try hung QEMU outright after one exchange (WUPA, then ATQA). The backtrace showed
    the main loop in `vm_stop` → `pause_all_vcpus` → `qemu_clock_enable(VIRTUAL,
    false)`, which waits for the clock's running timers to finish. The caller was one
    of them (the end of transmission is a virtual-clock timer), and the vCPU was stuck
    on the BQL in an MMIO write. Stopping from a bottom half fixed it: it runs moments
    later, outside the timers, and does nothing if the answer is already in. A Python
    ISO-DEP card then went through anticollision over two cascade levels, RATS/ATS and
    I-blocks. The firmware's `scanner` names it ISO14443-4A, the NFC app reads it, and
    `apdu` gets SELECT, GET CHALLENGE and GET DATA answers through the firmware's own
    ISO-DEP stack.

55. **A Proxmark3 with no Proxmark3.** None was plugged in, so the relay was written
    from the Iceman/RRG source and checked against a simulation of it. The source
    held three surprises:
    - `CMD_HF_ISO14443A_READER` changed shape on 2026-08-29, from MIX arguments to
      an NG payload, and old frames are now refused. Releases up to v4.21611 want
      MIX, and v4.23346 wants NG. A too-short NG payload tells them apart: the new
      reader answers with an error, the old one hears nothing it understands.
    - The client's `hf 14a raw` has no parity option, but the firmware has
      `ISO14A_NO_PARITY`. Given no parity array, the coder sends the bits as they
      are, so a frame can go out as air bits with whatever parity the emulated
      reader computed. That is what MIFARE Classic after authentication needs.
    - Received without parity handling, the bits come back eight to a byte and the
      count is dropped. Every Type A answer is 4 bits or 9k bits, so ⌊8n/9⌋ bytes, or
      one byte as a 4-bit ACK/NAK, recovers it exactly.

    The emulated Flipper's `scanner` and `apdu` then reach a Python card behind the
    simulated Proxmark3, with both generations. Only Type A frames go out: each
    poll now says what the chip's MODE register polls for (`mode=a`, `b`, `f`, `v`),
    and the firmware's B, FeliCa and ISO15693 probes get silence without a round
    trip.

## The secure enclave (M8)

56. **A check that only the real keys pass.** At every start the desktop encrypts one
    block with each of the ten factory keys and compares the results with ciphertexts
    in the firmware's source. The keys are in the FUS of every Flipper and nowhere
    else, so an AES that computes correctly with any other key fails that check, and
    the desktop says "Secure Enclave is damaged", as it did while the emulated FUS had
    no key store at all. Patching the check out is not open to an emulator that runs
    release packages unchanged. So the enclave answers it: the source gives IV, input
    and expected output for each slot, and for those ten blocks AES1 returns the
    expected output, as long as the slot holds FlipperEmu's own key. Every other block
    is plain AES with the key in the file. The table tells nothing about the real keys
    that the firmware's source doesn't tell already.

    Two different counts come out of that check, and they matter to different code.
    The desktop's warning and `info device` go by the keys that pass. The device's own
    key (slot 11) is made when all ten factory slots hold a key that loads, passing or
    not. Seen both ways: with a blank enclave U2F shows "Certificate error", and with
    a foreign key in slot 3 (nine keys valid) it starts and the firmware logs
    `Generating keys 11..11`.

57. **Which way round the bytes go.** With enclave keys the firmware hands AES1 its IV
    and data as the 32-bit words they are in memory, `DATATYPE` 00. With keys of its
    own (CTR, GCM) it byte-swaps every word first. The model follows RM0434, where a
    data word is four bytes of the block as a big-endian number, and the firmware's
    unit tests gave the fixed point: their CTR and GCM vectors are standard ones (RFC
    3686's, and test cases 13 to 16 of the GCM specification), so on a real Flipper
    the byte-swapping register sequence gives standard results. The model check drives
    AES1 with the same sequence and gets SP 800-38A's CTR example and GCM test cases 4
    and 16. It follows that on the enclave path every group of four bytes is reversed
    on the way in and out:
    `swap(AES-256-CBC(K, swap(iv), swap(data)))`. The self-test table is stored with
    the same swap, and `scripts/fe_enclave.py` computes it on the host, where
    `crypto encrypt` and `decrypt` can be compared with it byte for byte.

58. **A tag that was wrong for two messages.** In the model check, GCM test case 16
    and one more shape came out with the right ciphertext and the wrong tag. The
    model was right. The test driver had left `NPBLB` set by an earlier message with
    a short last block, so the ends of the next message's blocks counted as padding
    and stayed out of the tag. The firmware cannot do this: it turns the AES bus off
    after every operation, and that resets the peripheral. The driver now does the
    same.

59. **Who refuses what.** `crypto store_key` names a slot, and the FUS's store
    command takes none: it gives a new key the next free index and reports it. The CLI
    checks the number before it asks, by loading that slot (which must fail) and the
    one before it (which must not), and prints messages of its own: "already used",
    "empty, which is not allowed". An encrypted key without its IV was expected to
    come back from the FUS as "Failure"; the CLI stops that one too, wanting a "hex
    encoded key with or without IV". Of the four refusals in the check only the second
    master key is the FUS's own answer.

60. **`crypto encrypt` and what follows the text.** A new check passed once and
    failed on the next run, in the second block of a 21-character text. Decrypted on
    the host, the firmware's output was the text, its NUL, and then
    `00 00 0D F0 AD 8B 28 00 00 00` where zeros were expected. `crypto_cli.c` takes
    the text as a C string, rounds its length up to whole blocks, reserves that much
    room and encrypts it without clearing what lies behind the NUL: whatever the heap
    held. The code is the firmware's, so a real Flipper does the same. The other
    checks never met it, because their texts were 15, 31 and 47 characters long,
    where the NUL is all the filling there is. The README's example was not, and
    would have given readers a ciphertext that differs from the host's now and then.
    The helper refuses other lengths now, and the documents say why.

61. **A keystore of one's own.** The stock Sub-GHz keystore is encrypted with the real
    key 1, so it could not show that keystores work with the emulated enclave. (It
    still "loads": the firmware logs `Failed to load line` for each of its lines, 63
    in 1.5.1, and goes on with no manufacturer codes.) The firmware has a debug
    command, `subghz encrypt_keeloq`, that loads a keystore and saves it encrypted
    under key 1 with a given IV. The check gives it four made-up codes and compares
    the file with the host's computation. That needed two details from
    `subghz_keystore.c`: the IV is changed before use (every byte plus the one before
    it), and all lines run through one CBC chain, each filled with zeros to whole
    blocks. Then the firmware loads its own encrypted file and encrypts it again with
    another IV, which comes out right only if every line was decrypted and parsed.
    `subghz rx` reports `keeloq_mfcodes_user OK` for the plain file and for the
    encrypted one.

62. **NumHCI.** AN5185 says every FUS response carries 0xFF in the event's NumHCI
    field. The CPU2 model had written 1 there since the first boot, and nothing ever
    noticed, because the system channel's transport does not read the field. It
    writes 0xFF now.

63. **One RFID read-back that failed once.** During these runs one smoke test on
    Unleashed failed in "rfid: H10301 emulation captured, read back": both read
    attempts found no tag. The enclave has no part in that path. 42 emulate and
    read-back cycles on their own and eight more full smoke runs on Unleashed all
    passed, and the failing run's device directory was gone before anyone looked at
    its logs. It stays unexplained. A busy host during that one run is the likeliest
    cause, and a guess.

## The window (M8)

64. **Switching off.** The firmware switches a Flipper off by setting BATFET_DIS in the
    BQ25896, which opens the battery's switch. With nothing on VBUS the system rail is
    gone; with USB plugged in it stays, and the firmware shows "It's now safe to unplug
    the USB cable" until the cable is pulled. The charger model now does both. An
    emulator that is off has nothing to run, so the board exits through a guest
    shutdown, and first says `power 0` on the front-panel link: the window that
    started it stays, and starts it again when Back is held. A flat battery takes the
    same way. The BQ27220 raises SYSDWN ("the system should shut down") under 3.15 V
    and drops it over 3.40 V, and the firmware, off USB, says "Battery low!", counts
    down from 86 s, and switches the Flipper off.

65. **Five seconds of Back.** `press back --hold 1500` on the desktop did nothing. The
    power-off dialog takes Back held for 5 s, so the window lets a button be held as
    long as a finger holds it: the panel link has a press and a release of its own.
    The other way round, a press and a release that come together (a touchpad tap, a
    test's click) can fall between the firmware's debounced looks at the pins, so a
    press now lasts at least 40 ms of emulated time, and a release that comes sooner
    waits for it.

66. **A socket name that was not a path.** The window's first run never connected,
    though the emulator listened. Qt's QLocalSocket takes a name that is not an
    absolute path as one in the temporary directory, so `devices/NAME/panel.sock`
    became `/tmp/devices/NAME/panel.sock`. The window makes the path absolute.

67. **Readers that held the monitor.** `nfc --reader` waited up to 15 s for the virtual
    reader, and `ibutton --reader` up to 120 s, with the emulator's monitor open the
    whole time. The monitor takes one client, and the window runs every command
    through it one after another, so a reader held everything else up. With
    `--no-wait` they put the reader there and return; the window watches the reader's
    `reader` and `tx` properties over the panel link and says what it read, or that it
    read nothing.

68. **One emulator at a time.** `gui_check.py` failed its reboot check once, and left
    an emulator running after it exited. When an emulator exits, the window waits
    300 ms before it decides what happened, so that the link's last lines (`power 0`)
    come first. The check switched the Flipper off the hard way and plugged USB in
    within those 300 ms, which started a new emulator; the old decision then took the
    new one for gone, showed the Flipper off while it ran, and the next Back held
    started a second emulator on the same device files. Now the decision is about the
    process that exited, and the window never starts one while another still runs.

69. **A serial that looked like a ROM.** A new DS1990 with the serial `01 02 03 04 05
    06` was refused: "a 1-Wire ROM is 7 bytes". Six bytes are taken as a serial, and
    get the family code in front, unless they start with the family code, as these do
    (01 is a DS1990's), when they were taken for a ROM without its CRC, one byte
    short. Six bytes are always a serial now.

## A bench for developers (M9)

70. **A level it reads once.** The firmware's log level is the low four bits of RTC
    backup register 1, and the firmware reads them when it starts: written while it
    runs, they change nothing until the next start. So the machine has the level as a
    property, `log-level`, which goes into the backup domain before the first
    instruction, or on a running machine for its next start. `log --set-level` says
    so, and the window sets it and reboots. Read back, the property is what is in the
    register, so a level chosen in the firmware's own Settings shows too. 0 is the
    firmware's default, which it takes for info. The window's check first asked for
    debug and looked for `[D]` lines after the restart: there on official and
    Momentum, and none on Unleashed, whose release has no debug lines in it (the
    words of "Starting service %s" are not in its flash; at trace it starts with the
    lines of info). The level it does take, as every build must, is one that says
    less: the check asks for errors only, and sees the info lines stop.

71. **One log, one writer.** The log is kept by the serial chardev's `logfile`, which
    every QEMU of a run appends to: the window switches the Flipper off and on by
    starting a new QEMU, and the log stays one. `--serial file:` on the same path
    would have written every byte twice, so there the file is the chardev and has no
    `logfile`. `gui_check.py` counted "Startup complete" in the log to see a boot; with
    every boot of a run in one file it counts the ones it has waited for. And
    `--serial tcp:PORT` had listened on every interface of the computer, for a port
    the firmware can read as well as write: it is 127.0.0.1 now, unless another
    address is asked for.

72. **Red that is not on the line.** A crash report is `[CRASH][thread] message` in
    red, the registers, and only then the colour's reset. The register lines carry no
    colour code: on a terminal they are red because the line before left red on. A
    reader that drops lines (`log -g`, the window's filter) has to carry the colour
    from line to line, and put it in front of a line whose predecessor it dropped.

73. **A tab that is not hidden.** The console and the firmware log are two docks in
    one place, so they are tabs. Qt does not call the one behind hidden (`isHidden()`
    is false, `isVisibleTo()` true); what tells them apart is `visibilityChanged`,
    which the window keeps. View > The firmware log on the tab behind brings it to the
    front, and only on the one in front closes it; the log behind keeps its lines
    and shows them when it comes to the front.

74. **Waiting for the firmware, not for the clock.** Entry 36's rule gave every bulk
    packet twice the firmware's measured turnaround after the re-arm, and file copies
    over the CLI (`storage write_chunk`, `storage read_chunks`) ran at 16 to 35 KiB/s.
    Much of that wait passed with the firmware done and asleep. What the rule protects
    is the CDC service's 8-entry event queue, and there is an exact sign of that queue
    being empty: the CPU halted in WFI with the USB interrupt not pending. The idle
    thread runs only when no other thread is ready, so the service's thread waits with
    nothing queued. A packet delivered then raises the interrupt, so each look at an
    idle CPU hands the firmware one packet.
    - The first try was no faster. Its estimate of when the CPU goes idle moved in
      unequal steps and ran to its ceiling, and the hook on the endpoint register's
      write woke the host only at the clock rule's time: the old rule with more code.
    - Equal steps of an eighth, up for a look that found the CPU at work and down for
      one that found it idle, bring the estimate to the median of what the firmware
      takes. A look that came later than planned (the main loop was busy) says nothing
      about the plan and changes nothing.
    - A look is not free: the main loop takes the lock that the emulated CPU's register
      accesses need. So the host looks again no sooner than 50 µs later.
    - Firmware that never sleeps keeps a clock rule: twice its turnaround since the
      re-arm, 1 ms at least. That is no shorter than what entry 36 gave it.
    - Measured back to back on the loaded host (official 1.5.1, a 200 KiB file, chunks
      of 1024, 4096 and 8192 bytes): writes went from 16, 35 and 26 KiB/s to 22, 44 and
      48, reads from 28, 24 and 25 to 50, 49 and 55. The host's own load moves these
      numbers a lot: the same build an hour earlier wrote at 57, 74 and 83 KiB/s and
      read at 85, 94 and 94.
    - The smoke test passed on the three firmwares, Unleashed's key cache upload
      (entry 36) among its checks, and `gui_check.py` on official and Unleashed.

75. **Two ways to one card.** `push` and `pull` could write the card's image with
    mtools, as `make_sdcard.py` does, and be done in a moment. But a running firmware
    has the FAT mounted, and keeps what it read of it: a file put into the image behind
    its back is not there for it, and the next thing it writes may go over it. So a
    device that runs gets its files through the firmware's `storage` commands, and only
    a stopped one through mtools. What each of the two would not take as given:
    - The firmware's `mkdir` makes one folder, and `remove` takes one file or one empty
      folder ("access denied" is its word for a folder with files in it), so the
      launcher walks the tree itself, deepest first.
    - Its shell drops every byte that is not ASCII, so `café.txt` would arrive under
      another name with nothing said. Such names are refused before anything is sent.
    - `write_chunk` appends. A file is removed before it is written again, and a file
      of no bytes is one chunk of none.
    - mtools reads `[` and `]` in a name as a pattern, so `brack[1].sub` matched nothing
      until they were escaped. Not in the last name of something to be made, though:
      there the backslash would become part of the name.
    - mtools shows a short name in the case FAT stores, without the long one when there
      is none: the listing is put together from both columns.
    - The check's second boot waited for a second "Startup complete" in a log that
      `run` had started afresh: each run's log is looked at on its own now.
    `scripts/files_check.py` copies a folder (an empty one in it, a name with a space,
    one with brackets, a file that ends like the CLI's prompt, one of exactly one read
    chunk, one of no bytes) there and back both ways and has each way read what the
    other wrote: 41 checks on official 1.5.1, Momentum mntm-012 and Unleashed
    unlshd-094.
76. **A card that is out, and a caller who hangs up.** Two things a transfer met when it
    did not get to its end.
    - `sd eject` moved the card-detect pin and nothing else, and the firmware reads that
      pin only when its storage service is idle. A `push` under way went on writing to
      a card that was "out", to its last byte. The slot is now on the SPI bus itself:
      with the card out it answers 0xFF and the card is not selected, and the write in
      progress fails (`internal error` is the firmware's word for it). A card pushed
      in is reset first.
    - A `push` stopped with Ctrl-C left the CLI dead until the emulator was restarted:
      no prompt for any later command. The endpoint registers showed why: bulk OUT at
      0x7141, receive valid but both of its two buffers with software, which NAKs for
      good. The model had dropped DTR the moment the pty closed and then gone on sending
      the rest of what the launcher had written. `cli_vcp.c` reads a packet only when
      the endpoint's interrupt comes while a session exists; these came when there was
      none, were acknowledged, and nobody looked again. Unplugging did not help, because
      the model kept those bytes for the next plug. Now the last bytes go first, then
      DTR drops, and the rest is discarded (ARCHITECTURE, "Hanging up").
    - With that gone, a `pull` stopped with Ctrl-C showed what is the firmware's own.
      `storage read_chunks` cannot be stopped, and a closed port does not stop it: the
      shell goes on sending the file into a pipe nobody reads, 100 ms a write. For
      300 kB that is minutes without a CLI. Trying the port a few times in that
      time filled the CLI service's queue of eight events (two per open and close,
      and its thread was waiting for the shell), and the ninth failed its `furi_check`
      in the USB interrupt: `[CRASH][ISR USB_LP] furi_check failed`, and a reboot.
      Nothing in the model was wrong there, so the launcher keeps out of it: a `pull`
      that is interrupted reads its file to the end and drops it, and a `push` stops
      between two pieces and takes the file off the card.
    `files_check.py` has seven checks more for this, 48 on each of the three firmwares:
    a pull and a push interrupted, a push and a `cli` killed, the card pulled out in
    the middle of a push and pushed in again.

77. **Starting an app is a conversation with the loader.** `app FILE.fap` is `push`
    and then `loader close` and `loader open`, as `fbt launch` does it. Three things
    the firmwares answered that the first version did not expect:
    - Snake Game, the smallest app on the official card, cannot be closed that way:
      `Application "Snake Game" has to be closed manually`. It has a message queue of
      its own and takes no request to exit, as most small apps (and most first apps)
      do. Every later `app` then failed at its first step. Now the launcher presses
      Back, which is what "manually" means.
    - The same `.fap` with the API major in its manifest raised by one: the official
      firmware refuses it at once (`Preload failed, ...: Update Firmware to use with
      this Application (ApiTooNew)`). On Momentum and Unleashed `loader open` did not
      answer at all. Their screens showed why: "App Too New / APP:88 > FW:87 / This
      app might not work / Continue anyways?", and the CLI command waits for the
      button. The launcher gave up after 60 s and closed the port, the check's next
      commands opened it three times more, and that was entry 76's queue of eight
      again: `[CRASH][ISR USB_LP] furi_check failed`. Now `app` says after 8 s that
      the Flipper may be asking, waits, and presses Back itself before it leaves
      (`--wait`); answered with Continue, the app starts and `app` says that it runs.
    - `loader list` of official 1.5.1 has an empty `Apps:` group. Its main apps are
      `.fap` files on the card now, and `loader open "Sub-GHz"` finds them by name all
      the same. `app --list` leaves the empty group out and lists the card's files.
    `scripts/app_check.py`: 23 checks on each of the three firmwares. The Back button
    for an app that takes no request is checked on the official one only (the others
    do not bring Snake Game along under that name); the question on the screen on
    Momentum and Unleashed only (the official one does not ask).

78. **The tools find the real Flipper first.** `ufbt launch` against the emulated port
    needed no change to the model at all: built, installed, started, the app's log
    lines in the log, its crash and the restart as on hardware. What it needed was
    care about which Flipper. The tools' port search takes the first serial port named
    `flip_…` before it looks at `FLIPPER_PATH`, and a real Flipper was plugged into
    the machine this was written on: an `ufbt launch` without a port would have
    installed the test app on it. So nothing here runs a tool without `FLIP_PORT=`,
    and `./flipperemu ufbt` and `fbt` exist to add it. Three more things on the way:
    - `ufbt flash_usb` uploaded the SDK's update package, the Flipper restarted into
      its updater, and that stopped: "Update Failed! CRC mismatch #[3-99]". The files
      on the card were the SDK's, byte for byte, and the radio image's CRC is the
      manifest's. Stage 3 is the radio image's check, and it has no failure of its
      own for what comes right after the CRC: the address check before the stack is
      written. The option bytes say that the secure area begins at 0x080D7000 (the
      manifest's "OB reference", which the updater also compares), the stack is to go
      there, and an address that is not below the secure area is refused. On a real
      Flipper the stage is skipped when the stack that is installed is the package's,
      and before another one is written the FUS removes the old one, which moves the
      secure area up. Here the option bytes are those of a Flipper with its stack and
      the CPU2 is a FUS without one, which is the known limit. A held Back leaves the
      updater, and nothing was written. The same package without its radio stage (what `fbt flash_usb` sends,
      and what `run --update` makes of any package), sent with the official
      `selfupdate.py`, installed and came back. The launcher refuses the two targets
      that send a stack, with the command that works.
    - The example app could not be launched twice: `loader close` answered "has to be
      closed manually" (entry 77's Snake Game again, in an app of our own), and
      `runfap.py` ends there with "Unexpected response". An app with a message queue
      of its own has to take the request itself: `furi_thread_set_signal_callback`,
      and `FuriSignalExit` queued like a key. `examples/hello` does that now, and
      the page says what the message means.
    - ufbt's scripts print carriage-return progress bars: a log of them read with
      `tail` is one line of 40 kB.
    `scripts/ufbt_check.py`: 25 checks on the official 1.5.1 with ufbt 0.2.6, eight
    of them on what the launcher hands to `fbt` (a stand-in script that prints its
    arguments). A firmware built by `fbt` in its tree is not tried yet.
79. **The firmware's flag for a debugger is for a probe.** `./flipperemu gdb` is the
    toolchain's gdb on QEMU's stub, with the SDK's ELF and the SDK's `flipperapps.py`
    for the symbols of apps. The script sets `furi_hal_debug_gdb_session_active`
    when it is loaded and at every stop. With the flag the loader runs `bkpt 0`
    where an app is about to start, and `furi_crash` runs one where it would restart.
    A probe halts the processor on that instruction, the script reads the list of
    loaded apps, and the user sees the stop. Under QEMU's stub nothing halts: the
    instruction is a fault, and the firmware crashes on the debugger's own flag. So
    `scripts/fe_gdb.py` takes the script's helper as it is, replaces the one method
    that sets the flag, and puts breakpoints of gdb's at the two places. On the way:
    - A breakpoint with `silent` and `commands = "continue"` at the app's start was
      not enough. The script's stop handler had not run when the command continued:
      the app started without its symbols, the user's pending breakpoint in it was
      never placed, and a batch gdb waited for ever. A `gdb.Breakpoint` whose
      `stop()` calls the script's sync and returns False does it: the symbols are
      there, the pending breakpoint is placed, and nothing is shown.
    - The stub is asked of the monitor when gdb is wanted (`gdbserver unix:…`), and
      `info chardev` says where a device's stub is and whether a gdb is on it, so
      there is no state of the launcher's to go stale. A second `gdbserver` while
      one is open fails ("duplicate yank instance"): `gdbserver none` first.
    - The monitor takes one client at a time and its socket has a backlog of one.
      Three connections in a row (is it running, is it running, the command) got
      `EAGAIN` on the third, after a gdb that had died: the launcher fell over just
      where it was to let the firmware go. The monitor client waits for its turn
      now, and a device whose monitor is busy counts as running.
    - A gdb that dies leaves the machine stopped, as QEMU has it. The launcher
      looks after its gdb has gone, and continues.
    - The same sources built in another folder are not rebuilt by ufbt (its build
      folder is one for all), so the debug ELF keeps the first folder's paths.
      The check names its breakpoint by the file's name alone.
80. **The screen as a GIF, from the link the window has.** `./flipperemu record`
    and the window's Device > Record the screen write what the LCD showed as an
    animated GIF (`scripts/fe_record.py`).
    - The pictures are the front-panel link's `lcd` lines. The monitor's
      `screendump` would have been a poll, a PNG each time, and no time of the
      change. The panel sends a picture when it has changed (it looks every 16 ms
      of the host's clock, and waits for a frame the firmware is still sending), so
      the recording is the screen as it was seen, with the time on the host.
    - The link serves one program. So there are two ways in: the launcher's command
      is a client of the link for a device without a window, and the window records
      from the frames it gets anyway. With the window open the launcher gets no
      hello, and says after two seconds whose the recording is to make.
    - The GIF is written without a library (Pillow is wanted by `shot` for a PNG
      and by the smoke test, and the window does not need it): two colours, LZW
      with 2-bit codes to start with, each LCD pixel four wide. A picture after the first is the rectangle
      that changed, left in place ("do not dispose"). Times are in slots of 2/100 s:
      viewers play a delay under that as 1/10 s. A still longer than a GIF's 16-bit
      delay (655 s) goes on as one-pixel pictures. Pillow and Qt's reader both give
      back the pictures bit for bit, and the length to the slot.
    - Numbers: the example app with four key presses, 7 s, is 6.4 kB (2.9 kB the
      first picture, under 1 kB a step); five seconds through the main menu with
      its animated icons, 35 kB. A full-screen picture of noise takes 14 ms to
      compress, so the window writes the file in a thread of its own.
81. **Two kinds of stop.** `./flipperemu pause` and `continue`, and the window's
    Device > Pause. QEMU has a stop and a start, and they were in use already: the
    NFC link holds time still for each of the reader's frames until the card on the
    other end has answered (up to a second of the host's time), and starts it again.
    - A pause given as the monitor's `stop` during such a hold did nothing (QEMU's
      `vm_stop()` on a machine that stands still is no event), and the link's start
      ended it. So the panel keeps the two apart: a pause somebody asked for is a
      property (`/machine/panel paused`), a link's hold is a count
      (`flipper_vm_hold()`, `flipper_vm_release()`), and the machine starts when
      neither is left. Whatever else starts it (the monitor's `cont`, a debugger)
      ends the pause, which a handler of the run state sees.
    - To everybody else the machine runs through a link's hold: `stopped` is empty,
      and the window is not told. Otherwise an NFC read would flicker "PAUSED".
    - Why it stands still is QEMU's run state by its name (`paused`, `debug`),
      with two corrections. A machine reset while it stood still is `prelaunch` to
      QEMU, and paused to anybody who looks at it. And QEMU has `debug` at a
      breakpoint only: a gdb that has just got on, or pressed Ctrl-C, is `paused`
      to it. The panel calls it the debugger's whenever a gdb is on the stub and
      nobody asked for a pause, so the window does not say "PAUSED" over a stop
      that its Pause cannot end.
    - The window does not pause by itself. It runs the launcher's command, and is
      told over the link (`running 0 paused`), as it is when a terminal pauses, or
      a gdb stops at a breakpoint. The panel looks on the host's clock, so it can
      say so while the machine stands still.
    - Keys: the monitor's `sendkey` is dropped by QEMU on a stopped machine (and
      its release comes later, with no press before it), so `press` refuses with
      the reason. The panel's own press is kept, with its release on the machine's
      clock: a button clicked in the paused window is one press of 40 ms when it
      goes on.
    - With a gdb attached `continue` is refused: QEMU would run, and the gdb would
      still take the firmware to be stopped. A pause there is given to gdb as an
      interrupt, which is what it is.
    - Not seen: a monitor's own `stop`, or a gdb's Ctrl-C, that falls into a link's
      hold. QEMU tells nobody, and the card's answer ends it.
    - The check is the firmware's uptime: the seconds it stood still are gone from it
      (`smoke_test.py`); in the window, a click on OK that changes nothing until
      it goes on (`gui_check.py`).

82. **The LED chip runs its programs.** The LP5562 was four PWM registers: what the
    firmware wrote there was the light. But the firmware leaves two things to the
    chip's own engines, and neither was seen. The backlight is faded by a program
    on engine 1 (`lp5562_execute_ramp`: set PWM, ramp, branch back once, end), and
    the blink of an app that reads or sends is a loop on engine 2 (set PWM, wait
    10 ms, set PWM 0, wait, go to start). The backlight jumped, and NFC > Read
    showed a dark LED.
    - The three engines are interpreted now: the 16 commands each, on the chip's
      32768 Hz clock (a step is 16 or 512 cycles times the step time), ramp and
      wait, set PWM, go to start, branch with its loop counts, end, the triggers;
      run, step, run once and hold; the map of which engine drives which LED
      (it resets to 0x39, not 0).
    - Time is the machine's. An engine is worked out up to now whenever the
      firmware reads or writes the chip, and by a timer that is not set more often
      than every 2 ms: a fade steps every half millisecond, and nobody looks that
      often. A set PWM is seen up to 1.5 ms late for that; the blink's 10 ms came
      out as 8.7.
    - A ramp of n increments takes n + 1 steps, as the datasheet has it. The
      firmware's fade program asks for half the difference and runs the ramp
      twice, so a fade to anything but 255 ends one or two above what was asked
      for, and stays there: the white LED remains mapped to the engine. On the
      chip too, by the datasheet.
    - A full fade is 125 ms and the panel looks every 16 ms: the window gets eight
      to twelve values on the way, and draws them.
    - Momentum fades it in twice. It lights the screen early in its start; a
      second later its notification service starts, puts every light out and
      the screen on again, and the ramp begins from 0 a second time. The window
      check, which wanted one rise from dark, counts the last one now.
    - A flash of 10 ms falls between two looks of the panel more often than not.
      So the LCD model keeps the brightest colour the LED had since each reader
      (the panel link; QEMU's own picture) last looked, and gives that first.
    - Then every blink began with a white flash. It is in the firmware: the
      notification app starts the blink while red (charging) is still written,
      and takes it away 2.6 ms later, at the end of its sequence. Nobody sees that
      on a real one, and the latch made 16 ms of it. A colour that the LED has for
      less than 4 ms of the firmware's time is not reported now.
    - `/machine/led` has `led`, `blinking`, `backlight` and `engines` (`hold@0=255
      run@4=0 off`: each engine's state, program counter and PWM); `status` has a
      `lights` row from them ("blinks cyan (00ffff)"). While an engine blinks a
      channel, `led` is the colour of its flashes and not the instant's.
    - The CLI's `led bl N` is no test for the fade: after a key press the
      notification service holds the backlight, and the command changes nothing.
      The window check counts the steps of the fade when the firmware starts
      (ten or so on the way to 255) and the flashes of NFC > Read; the smoke test
      reads engine 1's end (`hold@0=255`).

83. **An app asked to close in the middle of its work.** The first version of that
    check closed NFC while it read, with the launcher's `app --close` (the loader's
    `loader close`, as `fbt launch` does it). A few checks later a Back held for 5.6 s
    did not bring the power-off dialog, and it looked like the firmware's clock
    running slow.
    - It was not. `input dump` on the CLI showed press and release 5.6 s apart, and
      the firmware's `uptime` kept pace with the host's clock, plugged in and on
      battery. The log's time stamps gave it away: 12 s where it should have been
      minutes. The firmware had started again, and Back was held on a desktop that
      was not there yet.
    - `[CRASH][NFC] furi_check failed`. The loader's request ends the app's event
      loop where it is, the app frees its scanner, and `nfc_scanner_free()` checks
      that it is idle. In the middle of a read it is not. This is the firmware's
      own, in its source; the emulator only went along.
    - The launcher made nothing of it. A firmware that starts again greets the CLI
      with its banner, and the banner was taken for the loader's answer ("the
      firmware's loader said:" and a dolphin), or, when the new firmware's `No
      application is running` came in the same read, for success: `"NFC" closed`.
      Now a greeting in an answer is a restart (`fe_apps.Restarted`), and `app
      --close` says so with the crash from the firmware's log: `the firmware
      crashed ([NFC] furi_check failed) when the app was asked to close, and has
      started again`. Exit status 1.
    - For a developer this is a result and not an accident: an app of their own
      that does not clean up when its loop ends shows the same, and `fbt launch`
      closes the app that runs in just this way.
    - The window check leaves the read with Back first, as a person does.
      `app_check.py` closes NFC in the middle of a read on purpose, after its
      "no crash" check, and wants the report: 25 checks now, on each of the three
      firmwares. Momentum's and Unleashed's NFC app crash in the same way.

84. **An update for a Flipper that runs.** `run --update` writes the package into the
    card's image and then starts the device. A device that runs has its card to
    itself, and `ufbt flash_usb` was refused for the radio stack in the SDK's package
    (78): the one target of the app loop with no way through. `./flipperemu update
    PKG` sends the package as the firmware's own tools send it, file by file through
    the CLI, and then `update install`.
    - The CLI takes 22 KB/s (`storage write_chunk`, 1024 bytes a time, as `push`
      does it): 111 s for the official package's 2.5 MB, 13 minutes for Momentum's
      12 (16 KB/s there). A real Flipper is no faster. A file that is on the card with
      the same size and md5 is not sent again, so a second time costs seconds.
    - `update install` gives no prompt back. "Verifying update package", then
      `Error: …. Stopping update.`, or `OK.` and "Restarting to apply update. BRB",
      and 100 ms later the firmware is gone. The launcher reads for one of the two.
    - The updater has no USB: in a special boot mode the firmware does not start the
      CLI's serial port (`[W][CliVcp] Skipping start in special boot mode`). So
      "done" is the CDC ACM device there again after it was gone. The official
      1.5.1 starts four times on the way and takes 96 to 100 s.
    - What it does meanwhile is in the UART log: `[I][UpdWorker] Stage N, progress
      P`, a line when the stage changes (unpacking the resources says nothing for
      40 s). The numbers are the firmware's enum, and another firmware may count
      differently, so the launcher learns them: the stage that a start ended with
      at progress 100 is "completed", and a later stage above it is the error
      stage. With the radio stage kept (`--keep-radio`) that is `Stage 3` and then
      `Stage 18, progress 0`. The launcher says so after seconds and not after its
      timeout: the updater waits on "Update Failed! CRC mismatch #[3-99]" for a
      held Back, which starts the firmware that is there.
    - Updated to Unleashed, the Flipper came back and its log said nothing more,
      from the updater's `Stage 11` on. `status`: "the firmware's level: none".
      Unleashed's updater ends its flash stage with
      `furi_hal_rtc_set_log_level(FuriLogLevelNone)` where the official one has
      `FuriLogLevelDefault` (production builds; `update_task_worker_flasher.c`). So
      a real Flipper is as silent after it. Momentum's updater left the level at
      none as well (seen in the check; its source was not read for it). The
      launcher says so when the level is another than before the update, with
      `log --set-level` to set it again.
    - That made the next update blind: no stage lines, and a wait of 15 minutes in
      front of an error screen. The level is in the RTC register, and the updater
      is the firmware's next start: when the level hides `info`, the launcher
      writes `info` there before `update install`, and the old level again when
      the update does not end well. With that the error is seen after 4 s at level
      none too.
    - The launcher counted the firmware's starts from the log and said "4 starts"
      at the end. With a log that goes silent on the way it said 2. It says none
      now.
    - `ufbt flash_usb` through the wrapper does not run ufbt. Its package is the
      SDK's (`$UFBT_HOME/current/f7-update-*`) and nothing is built for it, so the
      launcher sends that one without the stage. `fbt flash_usb_full` builds its
      package first and sends it in the same run: it stays refused, with `fbt
      flash_usb` and `update dist/…` named.
    - A fright on the way: the first screenshot after an update said "Flipper
      crashed and was rebooted: furi_check failed", with no `[CRASH]` in the log.
      The firmware keeps a pointer to a crash's message in an RTC backup register,
      which is in the device's `nvram.bin`, and shows the message at every start
      until a button is pressed. This one was left from entry 83's test on that
      device. A new device showed "Firmware update: Success".
    - `scripts/update_check.py`: 25 checks. Official 1.5.1 with its own
      package: 218 s, 100 of them the updater. Unleashed with its own (5.3 MB):
      368 s, 127 the updater. Official to Unleashed: 354 s, 138 the updater.
      Momentum with its own (12 MB, 11 of them `resources.tar.gz`): 1016 s, 255
      the updater.
      `ufbt_check.py` runs the real `ufbt flash_usb` now: 29 checks.

85. **The card on the desk.** Thirteen minutes for Momentum's package through the CLI
    (84). Somebody with a real Flipper and a big package takes the card out, puts it
    into the computer's reader and copies 12 MB in seconds. The device has that move
    (`sd eject`, `sd insert`), and since 76 its slot is a real one. `update` makes
    the move itself now, around the write, and 75's two ways to one card are both
    open to a device that runs.
    - The firmware notices a pulled card after 1.4 s (`storage info /ext`: "Storage
      error: filesystem not ready") and has one that is pushed in mounted after 1.1
      to 1.5 s (`[I][StorageExt] card mounted`). The launcher asks at the CLI both
      times and counts no seconds (74).
    - With the card out, the image is the launcher's, though QEMU has the file open:
      a raw image, which QEMU and mtools both reach through the host's page cache,
      and the emulated card is reset when it goes in. 3 MB written in 0.01 s, and
      the firmware's `storage md5` of it was the file's.
    - Official to Momentum, 12.2 MB: 228 s, 223 of them the updater and 4 s the
      package. Through the CLI it was 1016 s.
    - A card that is out already stays out: `update` says so, with `sd insert`. The
      launcher does not push in a card that somebody else pulled.
    - `update --cli` is the old way, and it stays for what it is: the path that the
      firmware's tools take with a real Flipper. The check uses it for its second
      run, with the radio stage kept. The files that mtools wrote are "unchanged"
      to the firmware's md5 there, and only `radio.bin` and the manifest are sent:
      the two ways, compared by the firmware.
    - `push --card` is the same move for files: 3 MB in 2 s, where the firmware's
      CLI takes two and a half minutes. What can be refused is refused before the
      card moves (a destination in `/int`, a file that is not there). An error
      after that, such as a file where a folder is wanted, leaves with the card in
      again and the firmware waited for, so that the next command finds a card.
      `files_check.py` has seven checks for it, 55 in all.
    - Two checks that were started in the same second shared one device. A
      throwaway device was named after its check and the time in seconds. All
      eight checks have their process ID in that name now.
    - `scripts/update_check.py`: 29 checks. Official 1.5.1 with its own package:
      84 s, 80 of them the updater. Unleashed with its own: 127 s, 122 the updater.
      Official to Unleashed: 118 s, 114 the updater. Momentum with its own: 222 s,
      218 the updater. `ufbt flash_usb` through the wrapper: 83 s.

86. **A fourth firmware, and words that changed.** RogueMaster (RM1024-1347,
    `rm-420`, API 88.23: a firmware.dfu of 828 KB and 25 MB of resources) started on
    the first try, to its desktop and its CLI, and passed the smoke test as Unleashed
    does: its `subghz tx` at the CLI "failed to start", which that check skips.
    - The files check then failed 20 of its 55, all through the firmware: "Storage
      error: file/dir doesn't exist". The official firmware, Momentum and Unleashed
      say "file/dir not exist" there and "file/dir already exist"; RogueMaster says
      "file/dir doesn't exist" and "file/dir already exists". The launcher went by
      those words to know that a file is not there yet (which is no error for a
      push). `StorageError` takes both wordings for the same reason now.
    - A wrong guess, written down here and in that commit's message at first: that
      this is the wording of all firmware after 1.5.1, and that the next official
      release would have failed the same way. The official dev build of 2026-11-08
      (3060cee9) has the old words in its image. The wording is RogueMaster's own.
    - That was behind the two failures of the window check (a file copied to the
      card; a `.fap` that is not on the card yet) and the four of the update check
      (a file pushed before the update; `update --cli`) as well.
    - An app for another API: RogueMaster asks on the screen like Momentum and
      Unleashed ("App Too New ... Continue anyways?"), and `app` waits and cancels as
      it does there.
    - `update` with its own package: 448 s, 444 of them the updater, most of that the
      25 MB of resources unpacked to the card. Its updater leaves the log level at
      none, as Unleashed's and Momentum's do.
    - On RogueMaster: smoke test 27 and one skipped, files 55, app 25, window 30,
      enclave 35, update 29 (ten minutes). Files again on the official firmware after
      the change: 55.

87. **A fifth firmware, and a crash that ends differently.** The official dev build
    (3060cee9 of 2026-11-08, from the dev channel) started on the first try and
    passed the smoke test (28) and the files check (55). The app check failed 2 of
    its 25, both after the crash it asks for: "the Flipper's CLI was not done with
    `loader close` after 15 s", and then no prompt at all.
    - The dev channel's builds are debug builds, as `./fbt` makes them by default
      (`DEBUG = 1` in `fbt_options.py`, so no `FURI_NDEBUG`).
      `__furi_crash_implementation` ends in a reset only in a release build. A debug
      build prints "System halted. Connect debugger for more info" and stays in
      `wfi; b .-2` with interrupts off, on a real Flipper too. The launcher knew a
      crash only as something the firmware comes back from.
    - Telling a halted firmware from an idle one: not by PRIMASK (the tickless idle
      sleeps in WFI with PRIMASK set) and not by a PC that stands still (the idle
      task's does). By the code at the PC: `bf30 e7fd` there or two bytes before it,
      read through the monitor, with no change to the machine. Each of the five
      images has that pair in exactly two places, the end of a crash and the end of
      `furi_halt()`. An idle firmware: 0 of 40 samples; one that reads NFC: 0 of 20.
    - `status` has it in its first row, with the crash from the log and the ways on
      (`gdb`, `reset`). A command that needs the CLI asks before it waits for a
      prompt (0.2 s to say so, where it ran into a timeout), and the CLI client asks
      when an answer has stayed away for two seconds: `app --close` reports the
      crash and the halt 2.4 s after its request, where it had waited 15 s to know
      nothing.
    - The window asks `status` a second after a `[CRASH]` in the log. The console and
      the status bar say that the firmware has halted, HALTED stands above the
      screen, and the state reads "halted" until the firmware runs again (asked
      every three seconds). The window check has an app crash the firmware on every
      firmware now: 31 checks.
    - A halted firmware keeps one host core at 100 %: an interrupt is pending and
      masked, so the loop's `wfi` returns at once. The chip does the same. Left as
      it is, and written into PLAN.md.
    - gdb on the halted firmware shows where it stands: addresses only for the dev
      channel's package, which has no ELF; a build from a tree has its symbols.
    - The window check's new crash step failed on Momentum, twice in a row, and
      for a reason that is the firmware's: its log had no `[CRASH]` at all. The log
      stopped in the middle of another thread's line ("[D][DolphinState] icounter 1,
      butthurt 0") and went on with the next start. `furi_log_tx()` drops what it is
      given while interrupts are masked and another thread holds the log's mutex,
      and a crash prints with interrupts masked: a crash in the middle of somebody
      else's log line leaves no report, on a real Flipper too. The launcher still
      says that the firmware started again under its request, and the check takes
      that. (The reason is still in the firmware's fault register then, which
      nothing here reads yet: PLAN.md.)
    - Results. The dev build: smoke test 28, files 55, app 27, window 31, enclave
      35, update 29 (101 s, 97 of them in the updater), and 29 from 1.5.1 to it.
      1.5.1 after the change: smoke test 28, files 55, app 25, window 31, update
      29. Momentum, Unleashed and RogueMaster: app 25, window 31.
88. **The crash that the log lost is in the fault register.** Entry 87's lost report:
    a release build writes a pointer to its crash message into RTC backup register 4
    before it resets, and the desktop shows it after the restart ("Flipper crashed and
    was rebooted"). The monitor's `xp/1wx 0x40002860` read 0 before the NFC crash on
    1.5.1 and 0x080a3bca after it, and the bytes there are "furi_check failed".
    - `app` and `app --close` name the crash from there when the firmware started
      again under them and its log has no `[CRASH]` since the request. They say that
      the log has none, and why.
    - `status` has it in its first row, and as "fault" in `--json`, until a button is
      pressed on the Flipper: the firmware clears the register when the crash screen
      is left. A message that is not in the flash (an app's own `furi_crash("...")`)
      is "Check serial logs" there, as on the screen.
    - A debug build writes nothing there: it halts (entry 87).
    - Results. App check: 26 on 1.5.1, Momentum, Unleashed and RogueMaster, 27 on
      the dev build. Window check: 31 on the four release firmwares. RogueMaster's
      log lost the report in that run, and the console named the crash from the
      register.

89. **The window asks, where it only listened.** It learned of a halt from a `[CRASH]`
    in the firmware's log. One opened on a firmware that had halted already showed a
    running Flipper with a still screen, and after a crash that the log lost (entry
    87) only the failed command said anything.
    - It asks `status` a second after the emulator's hello, a second after the
      firmware started again (the log's timestamps went back) and at once after a
      command of its own failed, besides the second after a `[CRASH]`.
    - The answer's "fault" (entry 88) becomes a line in the console, "The firmware
      started again after a crash: its screen says …", when the window has just
      found the emulator or the log had no report before the restart. After a
      reported crash it stays unsaid: the console has that crash already.
    - The status ask is a quiet command, and a quiet command that fails is still a
      failed command to the window: the ask after a failure does not follow an ask.
    - The window check drops the panel link's socket for this (the link finds the
      emulator again within a quarter of a second), which is what a window opened at
      that moment goes through, with the bench's state asked for again.
    - No firmware lost its report in the first round of the window check, so the
      line that the window says by itself had not been seen. The check now makes the
      case: Reboot with the crash screen still up is a start after a crash with
      nothing in the log about one (the register outlives a reset, and the desktop
      shows the screen again).
    - Not covered: a debug build whose crash printed nothing halts with no line in
      the log. The window knows of it with its next command.
    - Results. Window check: 32 on the dev build, 1.5.1, Momentum, Unleashed and
      RogueMaster. RogueMaster's log lost its report for real in the last run, and
      the window said the crash by itself there too.

90. **gdb's line at a crash was a release build's.** "`continue` lets it restart" is
    wrong on a debug build, and a debug build is what a developer with a firmware
    tree has under gdb. `fe_gdb.py` reads the crash function's code once: a release
    build's calls `furi_hal_power_reset`, a debug build's has no such call (seen in
    both official ELFs). On a debug build the line says that it halts after
    `continue`, and which command starts it again.
    - The dev channel's ELF is on the update server next to its package, so `gdb
      --elf` had the debug build's symbols: at the NFC crash gdb said the new line,
      `bt` had `nfc_free` at `lib/nfc/nfc.c:260`, and the firmware stood halted
      afterwards, as `status` said.
    - The gdb check wants the release wording on 1.5.1 (20 of 20). It builds its
      app with ufbt's SDK, which is the release's: it does not run on the dev build.

91. **A crash in a stock app had no app in its backtrace.** NFC, Sub-GHz and the
    rest are apps on the card. The firmware's gdb script looked for `nfc_d.elf` in
    ufbt's build folder, said "Failed to load debug info", and the backtrace had
    no name or line of the app. The update server has the files: `appsymbols_tgz`
    in its directory, one archive of `*_d.elf` for each build and channel.
    (`debugapps_tgz` beside it is something else: the apps of the debug category,
    as FAPs.)
    - `scripts/fetch_firmware.py` fetches that archive with the rest and unpacks its
      `.elf` files, by their names alone, into `appsymbols/` beside the firmware's
      ELF.
    - `gdb` now finds an ELF beside the package the device was flashed from (by what
      the flash holds, as before), so a device run from the fetched package has the
      firmware's symbols without `--elf`, on the dev channel's builds too, where
      ufbt's SDK has nothing that fits.
    - The firmware's script takes one folder and there are two now: the stock apps'
      and what was built. It is given `devices/NAME/gdb-apps/`, links to the files of
      both, made again where an app starts and before the script reads: an app may
      have been built while gdb waits at `continue`. What was built wins over a
      stock app of the same name; the script checks the CRC the app carries either
      way, and says so when the file is of another build.
    - Results. On the dev build with no flags: the NFC crash's backtrace has
      `nfc_app_free` at `applications/main/nfc/nfc_app.c:143` and `nfc_app` at
      `nfc_app.c:536`. On 1.5.1: `break snake_game_app` before the game is started
      stops at `snake_game.c:323`. The gdb check has that case (22 of 22 on 1.5.1,
      its own app's symbols through the same links), and skips it where no
      `appsymbols/` is.

92. **One `ikey read` in twenty printed `Dallas FFFFFFFFFFFFFFFF`.** Seen when older
    releases ran the smoke test for the first time: they print what a read gave
    without a look at its CRC (0.105.0: 3 of 40 reads of a DS1990A, 1.0.1: 1 of 40).
    Newer ones read again, so there it had only cost time.
    - The firmware times a 1-Wire bit with `furi_delay_us()`, a busy loop on
      `DWT->CYCCNT`. Under TCG every access to a device register takes QEMU's big
      lock, and whenever the main loop wants that lock (about a thousand times a
      second) the vCPU thread sleeps 20 to 57 µs on it. A 9 µs low is then 60 µs
      long, the key (a low under 56 µs is a 1) takes a 0, READ ROM (0x33) is another
      command, and the key says nothing: all ones.
    - A probe in the key printed both times of each long low: `low 57 us, to the
      last CYCCNT read 10 us` (the delay had ended in time, the pin write behind it
      waited) and `low 91 us, to the last CYCCNT read 65 us` (a read of the counter
      itself waited).
    - Timing the low by the vCPU thread's CPU time made it worse and was taken out.
    - The fix has two parts. CYCCNT is read without the big lock: the DWT asks for
      lockless I/O, and `qemu/patches/0005` has TCG keep to that for a load (it took
      the lock regardless). The read takes the lock only when an edge's time waits
      for it (entry 43). And the key times a slot's low from the fall to the
      master's last read of CYCCNT, where that lies inside the low: the busy loop's
      own end, not the pin write after it. A reset is still told by the time that
      passed, since a firmware may sleep through one.
    - After it: 40 of 40 on 0.105.0 and 40 of 40 on 1.0.1; the six current firmwares
      pass the smoke test as before. An existing checkout runs `scripts/setup_qemu.sh`
      once more (it applies 0005 and builds). Without the patch the machine builds
      and runs, with the timing it had.
    - What is left: a host that takes the vCPU thread off its core inside a 9 µs low,
      and a pin write that waits 290 µs or more (taken as a reset). Both rare.
    - Seen once in 56 runs while checking this: a Cyfral emulation's capture of 138
      levels, not 4096. Not found; the capture's log line now says how it ended
      (`full`, `the firmware stopped`, `no level for a second`).
    - The older releases themselves (smoke test): 1.4.3 passes all 28 steps; 1.3.4,
      1.1.2 and 0.99.1 pass 23, the five NFC steps skipped (they use the `nfc`
      command's shell, which releases before 1.4 do not have). 1.0.1 and 0.105.0 fail
      one of the 23: reading a Metakom key. `ikey read` finds it there in 8 of 15
      tries of 10 s (10 of 15 with the emulator as it was before this entry, so not
      from the fix), and in 15 of 15 on 1.5.1, whose code for it is the same. Open.
    - That read on 1.0.1, looked at again and not found (2026-11-11). It takes 1.4 s
      to over 30 s there (1.5.1: about 2 s every time), so nothing is stuck: each of
      the firmware's 100-tick listening windows has poor odds. Counted in the
      emulator: the comparator's interrupt handler takes every edge once, in order,
      each with its exact time (two missed in a window at most). Ruled out by trying
      it: deep sleep (`--legacy-sleep`), the key model catching up after a late
      delivery (only at a frame's start: 0 to 3 a window, not 6 to 160), another
      handler reading the cycle counter first (none does), and a reading thread left
      without time (edges no closer than 60 µs: windows back to 110 to 220 ms, reads
      no better). None of these changes was kept. The iButton library and the
      comparator's handler are the same source in both releases; what differs in the
      path is the 5 V supply (1.0.1 switches it on itself, 1.5.1 asks the power
      service). Next: what the decoder is fed, read out under gdb.
93. **A firmware that had halted cost a host core.** Known since entry 87: the halt
    loop's `wfi` returns at once (an interrupt is pending, and masked), on the chip as
    well, and QEMU runs that loop as fast as it can. A debug build left on its crash
    over lunch is a fan at full speed for nothing.
    - QEMU cannot sleep there by itself, and should not: the NVIC keeps its line to the
      CPU up whatever PRIMASK says (the CPU checks the mask when it takes the
      interrupt), so the CPU "has work". Taking the line down from the machine lasts
      until the next timer interrupt pends, a millisecond.
    - So the machine looks for the loop, as the launcher does (entry 87): `bf30 e7fd`
      at the PC or just before it, every 250 ms of virtual time, at the same place
      twice in a row. Then QEMU's vCPU throttle at 99 %: made for migration, there in
      every system binary, and it gives way to a stop request, so a pause, a reset and
      a debugger do not wait for it.
    - Measured on the dev build, NFC closed in the middle of a read: the emulator
      takes 6.8 % of a core halted (the vCPU's thread 1.0 %), 24.6 % alive and idle
      (18.4 %). After `reset` the CLI answers as soon as it did.
    - gdb: the rest ends when the machine stops, and starts again half a second after
      it goes on if the PC is still in the loop. A backtrace and five single steps
      there take 1.0 s together.
    - The machine says it once per halt: a line on stderr (`the firmware has halted at
      0x080123b0 …`), which a headless `run` shows in its terminal after the
      firmware's own last words, and `halted 0x080123b0` on the panel link. That
      closes entry 89's gap: the window asks `status` when the line comes, and shows a
      halt that left nothing in the log.
    - Results: the window check has a step for it on the debug build (the machine's
      word has reached the window, with the address `status` has) and passes 33
      there, the app check 27; on official 1.5.1 the window check passes 32, the gdb
      check 22, the smoke test 28.

94. **What an idle firmware costs, and where it goes.** Looked at after entry 93, where
    a halted firmware came out cheaper than an idle one. Nothing in the emulator
    changed for it: the README's numbers were out of date, and this is what a later
    attempt needs.
    - Official 1.5.1 on its desktop, headless, on an i9-9980HK: 24 % of a core with
      the cable in (the vCPU's thread 17.5 %, the main loop 6.5 %), 5 % with it out or
      with `--usb none` (4.5 % and 0.7 %). The README said 20 % and 1–2 %.
    - The firmware's interrupts say why (`log int` through the monitor): with the
      cable in, 1000 ticks a second and 24 thread switches, and nothing else; with it
      out, 45 to 49 ticks and the same 24 switches. The firmware does not sleep
      deeply on USB, as on a real Flipper. So a tick costs 190 µs of host CPU, 130 of
      them in the vCPU's thread.
    - The main loop is asleep in `ppoll` in 96 % of 700 samples of its stack
      (eu-stack), and the rest is its own bookkeeping: nothing of ours runs there too
      often.
    - The vCPU's thread is awake in 16 % of 900 samples. Of those: 37 % in the TLB's
      slow path (`mmu_lookup` → `tlb_fill_align` → `tlb_set_page_full`), 26 % in
      translated code, 14 % looking up the next block, 13 % entering and leaving the
      exception.
    - The slow path is the firmware's stack guards. Each is an MPU region of 32 bytes;
      QEMU's pages are 1 KiB here, and a page that holds a region smaller than itself
      is never cached: `get_phys_addr_pmsav7()` reports it with `lg_page_size` 0,
      `tlb_set_page_full()` marks the entry invalid, and every access to the page asks
      the MPU model and writes the entry again. The main stack is 1 KiB, one page
      with its guard at the bottom (`_stack_size = 0x400`, `_stack_end = 0x20030000`).
      The idle task's stack is 512 bytes (`configIDLE_TASK_STACK_DEPTH 128`): under
      gdb its guard is at 0x20001dc0 (RASR 0x060f0009: 32 bytes, read-only) and its
      stack pointer at 0x20001f58, in the same page. So every stack access of every
      interrupt handler, and of the idle task, takes that path; so does the lowest
      kilobyte of each thread's stack.
    - Not changed. A cache for parts of a page is a change to QEMU's TLB, not a small
      patch, and the most it gives an idle Flipper is that third. It would make every
      handler faster as well, which matters where microseconds do (1-Wire, entry 92):
      it is in the plan for that reason.
    - With no cable the 5 % is the firmware's own work: the vCPU is awake in 4.6 % of
      the samples, drawing the desktop's animation and reading the fuel gauge.

95. **The window could not send an update.** A firmware developer's loop is "build a
    package, send it, look", and in the window it ended in a terminal: Install
    firmware is the programmer's way (off, flash, on), and only the window that
    started the emulator has it. Now Device > Update with a package runs the
    launcher's `update`, F6 does it again with the same package, and a dropped
    package is updated with (where Install can be done too, the window asks which).
    Three things came up on the way.
    - The launcher's commands take turns, since the emulator's monitor takes one
      client at a time, and `update` keeps its turn for as long as the updater works:
      a minute and a half at least. A Reboot chosen meanwhile would have come when
      the update was over. So the window says "updating", greys what would wait
      (Reboot, Install, the card, the apps) and says why where a click cannot be
      greyed (the cable, the card's slot). Power off stays for the window that owns
      the emulator.
    - Stop waiting is Ctrl-C for the launcher: `update` then says that the update
      goes on on the Flipper, and gives a log level back that it had lent. Under the
      window's check that would have done nothing. The check runs from a script in
      the background, and a shell without job control starts such a command with
      SIGINT ignored (a test script printed it so); an ignored signal stays ignored
      in every child, and Python installs its Ctrl-C handler only where the signal is
      not ignored. The window ends a command that does not listen the hard way after
      three seconds, and the lent log level would have stayed. So the window sets
      `FLIPPEREMU_INTERRUPT` for the launcher it starts, and the launcher takes
      Python's handler back when it finds SIGINT ignored. A window started from a
      script's background would have met the same.
    - After "updated in 91 s: it runs 1.5.1" the window said "The firmware started
      again after a crash: its screen says "furi_check failed"": the crash of the
      check's step before. The fault register (entry 88) outlives the updater's
      restarts, and the crash screen is up again after them, so the line was true,
      and read as if the new firmware had crashed. The window now asks `status`
      before the update: a crash that its screen says then is no news when the
      updater is done, until that screen is left or another crash is on it.
    - Momentum's updater leaves the firmware's log level at none (entry 84), and
      the check's next step, which reads the log to see the firmware start, saw
      nothing. The launcher says what the updater left and names its own command for
      it; the window adds where that is in the window (the "Firmware level" button of
      the firmware log), and the check sets the level back there.
    - `scripts/gui_check.py` updates the Flipper twice with the package it runs:
      once with the wait given up after "updater started" (the updater has to finish
      by itself), once to the end with F6, and sets the log level again that the
      updater left. 35 checks pass on official 1.5.1, Momentum, Unleashed and
      RogueMaster (whose updater takes seven and a half minutes each time: the
      check waits fifteen), and 35 on the official debug build: its updater leaves
      the level as it was, so that step is not taken there, and the halt after a
      crash is one more.

96. **A killed `push` left official 1.0.1 silent, or with a letter typed.** Found when
    the files, app and window checks ran on older releases for the first time. 1.4.3
    passed all three (55, 26 and 36 checks); 1.0.1 passed the app and window checks
    and 51 of the files check's 55: after "a push killed" the next command got "no CLI
    prompt", and three later steps with it.
    - Newer firmwares end a command when the port closes. 1.0.1's `storage write_chunk
      PATH 1024` reads 1024 bytes, whatever comes and however long it takes, and its
      USB serial service puts a byte into the same stream when the port closes and
      one when it opens (at that one the CLI prints its banner). A chunk that is
      waited for takes both as data.
    - Measured with a file whose bytes say where they are, 24 pushes killed. 8 times
      the firmware was at a clean prompt. 6 times it said nothing, and prompted after
      1022 more bytes: 1024 less the two marks, so the kill had come between the
      command and its data. 10 times it showed its banner, its prompt and one letter
      typed there, always the chunk's last byte: the chunk was full one byte early.
      (Not traced, but it fits: the firmware fetches the last packet from the endpoint
      only when its stream has room for 64 bytes, and the closing mark needs room for
      one.) The command after that would have been `tuptime`.
    - Nothing in this is the emulator's. The USB model hands the device all that was
      written and then drops DTR, as a computer does (ARCHITECTURE.md, "Hanging up").
    - The launcher can tell that the program before it was killed: a command writes
      its name into the port's lock and takes it out when it leaves, so a name found
      there is a holder that never left. It then listens before it types. Silence
      gets Ctrl-C, 64 at a time, until the firmware speaks; a prompt with something
      typed at it gets one, which clears the line. Both are said on stderr, with the
      command that was cut off. A firmware that prompts at once pays 0.3 s of
      listening after a kill, and nothing otherwise.
    - The files check counted the launcher's own notes as lines of the firmware's
      answer ("and only that"); it now counts what is on stdout.
    - 14 of 14 killed pushes on 1.0.1 are followed by a command that gets its answer
      (before: 6 of 8). The files check passes there with 55, and on 1.5.1 as before.

97. **A test of an app could not be a script.** Everything a test does was there:
    `run --headless`, `ufbt launch`, `press`, `log`, `shot`. What was missing is the
    part between two steps. A script had to sleep and hope, or to read the log file
    itself, and a picture of the screen is nothing a shell can compare or a
    repository likes to keep. Now `./flipperemu wait` holds a script until the CLI
    answers, until the log has a line (`--log REGEX`) or until the screen is as in a
    file (`--screen FILE`), and `shot --text` is the screen as text: two pixels to a
    character, 32 lines, with `?` in a reference for what may differ.
    `examples/hello/test.sh` is the example app tested that way, and
    `scripts/wait_check.py` the check. What came up while it was tried:
    - A line that is waited for after a button has to come after the button. Without
      a command, `wait --log` reads the log from the firmware's start, which is right
      for "has it started" and wrong for a counter that said "2" a minute ago. With
      `-- COMMAND` the launcher notes where the log ends, runs the command, and
      counts only what was written since. (A wait that counted from its own start
      would have been simpler, and would lose the line that a `press` in the
      script's line before it had already brought.)
    - A picture of the desktop as the reference did not match in five seconds, and
      nothing was wrong: the desktop's animation on the official 1.5.1 has seven
      pictures and takes 6.4 s to come round. The page for developers now says to
      wait for a screen of the app's own, and to pause for a reference of one that
      moves.
    - A wait that fails has to say what was there instead, or the test's author
      starts the emulator by hand to look. For the log it is the last line with
      something in it (a halted debug build ends its log with an empty one, and "its
      last line:" with nothing after it was the first version); for the screen it is
      the number of pixels that differ and the screen itself, as text.
    - A crash under a wait. A release build starts again, and its log then ends with
      "USB Mode change done": no word of a crash in what the wait showed. The wait
      now takes the restart from the log's own mark, and the crash's message from the
      fault register (entry 88), and goes on waiting, because a line can still come
      after a restart (`wait --log … -- reset` is a wait for one). A debug build
      halts, and nothing comes any more: the wait ends within two seconds with the
      crash and what can be done, also the one for a screen, which first ran to its
      timeout in front of a firmware that had halted. The same for an emulator that
      was stopped under a wait.
    - `press sideways` pressed nothing and ended with 0. The launcher handed the name
      to QEMU's `sendkey`, which refused it, and nobody read the answer; found when a
      command that should have failed for the page's example was followed by a
      minute's wait. `press` now takes the six buttons, in capitals too, and says
      which there are for anything else.
    - The check passes on the official 1.5.1, Momentum, Unleashed and RogueMaster
      (30 checks) and on the official debug build (32, with the halt). The ufbt check
      runs `examples/hello/test.sh` at its end (31 checks; the test takes 13 s there).

98. **A test had to put its Flipper in the background itself.** The first line of
    `examples/hello/test.sh` was `run … &`, and a shell that starts something that way
    learns nothing of it. With a wrong path for the firmware the script went on to
    its `wait`, which said "not running" a minute later, while the reason had scrolled
    by; and that reason was a Python traceback, because nothing looked at a firmware
    file before reading it. Now `run --detach` comes back when the emulator is up
    (3 s for a new device with the official 1.5.1, its card made and its firmware
    flashed) or with what the run said and its status (0.6 s for the wrong path), and
    a file that is not there, a folder, a text file called `.tgz` or a firmware cut
    short is one line from `run` and `flash`. How it is done:
    - The launcher starts itself once more, with the same arguments, in a session of
      its own (the terminal's Ctrl-C and its hangup do not reach it) and with a file
      for what it prints. It shows what that file gains and waits for one of two
      things: the device's monitor answers, or the other launcher has ended. An
      environment variable tells the second one not to detach again; it takes it out
      of its environment, so that nothing it starts inherits it.
    - Where that file is. A device's folder is made when its firmware is flashed,
      and a run that fails for its firmware must leave no device behind: a later
      `run` would take the empty folder for a Flipper. So the file begins beside the
      devices' folders and becomes `devices/NAME/run.log` once the emulator is up;
      a run that failed removes it.
    - "Is running: stop first" is said by the first launcher, before it starts
      anything, and the firmware's log is not printed into `run.log` (`--serial
      null` unless told otherwise): it is where `log` and `wait --log` read it.
    - `scripts/wait_check.py` has eight checks for it (38 on the four release
      firmwares now, 40 on the debug build), one of them that no launcher is left
      for the device after `stop`. `examples/hello/test.sh` starts its Flipper this
      way, so the ufbt check runs it too.

99. **An IDE's gdb left the Flipper standing still when it left.** `gdb --vscode`
    writes a debug configuration for VS Code's Cortex-Debug extension. VS Code is
    not on this machine, so the configuration was used by a gdb driven over its
    machine interface (GDB/MI, with `mi-async`) the way that extension drives one.
    All of it worked, and then the Flipper stood still after the gdb had gone.
    What was found on the way:
    - After the detach the firmware ran; after gdb's exit it was paused. The
      firmware's own gdb script (`flipperapps.py`) sets the debug flag back to 0 as
      gdb leaves (`gdb.events.gdb_exiting`), and QEMU's stub stops the machine at
      any byte it receives while the machine runs: that is how it takes a Ctrl-C.
      With `target remote`, which `./flipperemu gdb` uses, a detach ends the
      connection, and the write fails without a sound. An IDE connects with `target
      extended-remote`, where the connection outlives the detach: the write went
      out, the machine stopped, and gdb was gone. `scripts/fe_gdb.py` (which has
      that write in its hands already, to keep the flag off) now writes only when
      gdb has the firmware and it stands still.
    - One trial had a breakpoint that stayed pending for ever, with nothing said.
      The shell had no `UFBT_HOME`, the launcher found no SDK and so no script for
      the apps' symbols, and said nothing of it. It says "no symbols of apps" now,
      and where it looked.
    - The launch.json that `ufbt vscode_dist` writes is not JSON: it has comments,
      before the first brace too, and commas before closing brackets. The first
      version gave up on such a file and printed the configuration to paste, which
      would have been the answer for exactly the people this is for. Now the file's
      text is kept: comments and those commas are blanked in a copy of the same
      length, the configurations are found in the copy, and the new one is put into
      the original at that place, after the last one or in place of the one of its
      name. The result is read once more before it is written; a file that cannot be
      read that way is left alone, and the configuration printed.
    - A breakpoint by a file's full path, an IDE's kind, did not bind in a copy of
      the example: ufbt has one build folder for all projects, and the object from
      the first folder was kept, with that folder's path in its debug info. `ufbt
      -c` and a build put it right; the gdb check cleans before it builds.
    - `monitor halt` and `monitor reset halt`, which such front ends send to a probe's
      gdb server, are "unknown command" to QEMU's monitor and do no harm. gdb's `kill`
      ends the emulator.
    - The gdb check has eight more checks (30): the missing SDK said, the
      configuration in a new folder, in ufbt's launch.json (nothing else of the file
      changed, a second time leaves the same file), not in a file that is none, and
      then the session over GDB/MI: connected, a pending breakpoint by full path, the
      app started and the breakpoint run into with the app's variables there, a
      pause, and a detach and exit after which the CLI answers and the app has
      logged its line.

100. **The GPIO header had pins, and nothing on them.** An app that reads a pin or
    drives one could not be tried: the header was a picture. `./flipperemu gpio`
    shows what the firmware has each pin as and its level, and holds a pin high or
    low from outside. What it rests on, and what was found:
    - QEMU's L4x5 GPIO model, which the WB55 uses, has the outside of a pin already:
      a line that is connected high or low, or not connected, and a refusal (with a
      guest-error line) to connect one that the firmware drives push-pull. The
      machine's property `header` is a table of the header's pins over that: read, it
      has each pin's mode from MODER, OTYPER and PUPDR, and its level from what
      drives it (the firmware's ODR for an output, else the outside connection,
      else the pull); written (`PA6=1`, `=0`, `=z`), it remembers what is held and
      puts it on the line.
    - The model disconnects a pin when the firmware makes it a push-pull output,
      and all of them at a reset. A wire would still be there after both. So what is
      held is kept by the machine and put on again: after a reset, and (through the
      tap the SoC has on every port's MODER, there for the 1-Wire pin) when the
      firmware changes a pin's mode. A pin held high under an output that is low
      shows low, and high when it is an input again.
    - All of that is read from the model's state, and not through the address
      space: a read of IDR there runs the 1-Wire pin's hook, which is not for a
      monitor to call.
    - The first trial of the property failed at every step, with "the monitor did
      not answer" and then a traceback: the trial's script kept the monitor open
      while it started the launcher, and the monitor takes one client at a time.
      Two things came of it. `gpio --wait` opens the monitor for each look and
      closes it, so a test can send a command while it waits (the check has one
      that does). And a monitor that stays busy is one line now, and no traceback.
    - A level is `floating` for a pin that nothing drives, holds or pulls, and
      `unknown` for a pin in an alternate function: the emulated peripherals talk
      to their devices directly, and put no levels on pins. That is the header's
      next gap (a UART on 15 and 16, PWM), with the ADC, which reads nothing.
    - PB6/PB7 (the log's UART) and PB14 (the iButton pin) are in the table and
      cannot be held: the first two have a character device on them, and the third
      a 1-Wire model with timing of its own that a held level would break.
    - `scripts/gpio_check.py` uses the firmware's own `gpio mode`, `gpio set` and
      `gpio read` in its CLI as the other side, so it needs no app: 38 checks, on
      the official 1.5.1, the official dev build, Momentum, Unleashed and
      RogueMaster. Without an app it cannot have an interrupt on a pin or an
      open-drain output; both are untried.
101. **The window's GPIO pins.** The drawn Flipper had eighteen sockets along its top
    that did one thing: a click anywhere on them opened the firmware log. Now each
    socket is its pin: lit amber while the pin is high, blue while it is low, dark
    while nothing drives it; a click on one holds it high or low from outside, or
    lets go of it, and a held pin has a jumper wire standing on it.
    - The window does not ask the monitor. It watches the machine's property
      `header` over the front-panel link, as it watches the other properties its
      bench shows: the emulator looks ten times a second and sends the property
      when it changed. The monitor stays free for the launcher, which matters:
      it takes one client at a time (100).
    - A click holds a pin by running the launcher's `gpio PA7 high`, like every
      other action of the window, so what the window does a script can do, and the
      answer is in the console. The pin lights when the property comes back
      changed, and not because it was clicked.
    - `scripts/fe_header.py` is the one place that reads the property and words a
      pin (`output, high; held low from outside`): the launcher's table, its
      `status` line, the window's menus and the lines under the pointer all come
      from it.
    - Which socket a click is on: the nearest pin within half the pitch. Between
      the two rows, and on the five power pins, a click opens the firmware log as
      before; so do 13 and 14, where the log's UART is. Pin 17, the iButton pin,
      opens the iButton menu. None of those can be held.
    - The pin menu is one menu, emptied and filled for the pin clicked. The GPIO
      menu in the menu bar is filled when it opens: all eighteen pins, a submenu
      for each of the ten that can be held, with what is held ticked.
    - Ten looks a second is what a pin's light can follow. A pin an app toggles
      faster shows some of its levels; a record of every edge is the next piece
      (`gpio watch`).
    - `gui_check.py` has six more checks (42, on all five firmwares): the header as
      the machine has it and the line under the pointer, the menu of pin 2, Hold
      it high (seen in the window, and `gpio PA7` from outside says `high`), pin 3
      held low and the GPIO menu's lines, Let go of all pins, and the clicks on
      13 and 17. Off-screen, like the rest of the window's checks.

102. **A record of what a pin did.** `gpio PA7 --wait high` looks twenty times a
    second and the window ten times: an app that pulses a pin for a millisecond was
    never seen doing it. `gpio record FILE.vcd` writes every change of a header
    pin's level with its time, and `gpio watch` prints them as they happen.
    - The machine writes the file itself (property `header-record`). It looks at
      the header's levels where one can change: after the firmware's write to a
      GPIO register (the SoC's tap had MODER, ODR, BSRR and BRR for the 1-Wire pin;
      OTYPER and PUPDR are new, for an open-drain pin and a pull), after a hold from
      outside, and after a reset. A pin whose level is as before writes nothing.
    - The file is a VCD: a `$var` for each of the thirteen pins, the levels at the
      start in `$dumpvars`, then `#ns` and a line for each pin that changed. `z` is
      a floating pin, `x` one that a peripheral has. Two looks in one tick of the
      clock get two times, a nanosecond apart, so every change has its own.
    - The property takes `off` to end a record, and not an empty value: HMP's
      `qom-set` cannot send one. It wants the file's whole path, since the emulator's
      directory is not the caller's; the launcher makes it one.
    - The file is flushed every 50 ms of real time. So `gpio watch` reads the file
      as it grows, and needs no second channel; the monitor has one client at a
      time and stays free meanwhile. A watch with no record running keeps its own
      in the device's directory and removes it afterwards.
    - Pin 17 had only the firmware's half at first. The iButton model's line was
      wired to the GPIO port directly, so a key's presence pulse and its 0 bits
      changed the pin and no look followed. It goes through the machine now
      (`flipper_ibutton_pull`), which looks. A record of `ikey read` with a DS1990A
      on the contact: 552 changes, the reset pulse low for 508 µs, 3 µs later the
      key's presence pulse for 102 µs, then the slots. `scripts/ibutton_check.py`
      passes with that wiring (18 of 18), and the same with a record running, which
      took 367 702 changes in 37 s (5.7 MB) and read back whole.
    - How exact are the times? They are the virtual clock's at the write, and the
      1-Wire model's notes say what is wrong with that: the write needs the big
      lock and comes 20 to 50 µs late when the main loop has it. The model times a
      slot by the firmware's last read of CYCCNT instead. Tried the same for the
      record (a change the firmware makes stamped with that read, when it was
      shortly before): the lengths of the lows of three reads were spread as
      before, since a late write is time that passes for the firmware too, and the
      other stamp only moves it to the next edge. Taken out again. What was
      measured stays in the README: 480 µs is 493 to 516, 60 µs is 64 to 72, 6 µs
      is 8 to 16, and on a busy host some of the short ones 30 to 50.
    - No program that reads VCD is installed here, and no library either. The file
      follows the format's text (IEEE 1364, the four-state dump) and is read back
      by `fe_header.Record`, which the checks use; GTKWave and PulseView have not
      seen one.
    - `gpio_check.py` has fifteen more checks (53, on all five firmwares): none
      running, a record's header, a second one refused, the firmware's four changes
      of PA7 and the hold of PA6 in their order with rising times, `stop` and what
      it says, nothing written after it, `gpio watch`, 1-Wire on pin 17, a record
      over a reset, three refusals that leave nothing running, and the file whole
      after the emulator stopped under it.

103. **The record of the pins, from the window.** The GPIO menu has a line for it:
    Record what the pins do…, which asks for a file and runs the launcher's `gpio
    record`, and End the record while one runs.
    - The window watches `header-record` like `header`, ten times a second. While
      a record runs that property is new at every look, since it has the time so
      far in it; the window takes the count and the file from it and leaves the
      rest of what a changed property sets off (the bench's tiles, the console)
      alone.
    - What it shows is the machine's, not its own: a record begun with
      `./flipperemu gpio record` in a shell is in the status bar and the menu the
      same, and the menu's End the record ends it.
    - `gui_check.py` has three more checks (45, on all five firmwares): the menu's
      line and the status bar once it runs, pin 2 held high and let go of with the
      count going up by two, and after End the record the file with those two
      changes of PA7 and nothing left that says a record runs. The file dialog is
      not in them: the check hands the file over where the dialog would.

104. **The serial port on pins 15 and 16, and the two bugs under it.** LPUART1 was in
    the machine from the first day, as QEMU's own model with nothing on its other
    end. `run` now gives it one (`--lpuart pty`, the default, `tcp:PORT` or `none`),
    and `./flipperemu uart` says where it is, sends to it, waits for what the
    firmware sends, and is a terminal on it. The first app that opened the port hung.
    - **An app that opened the port never came back from it.** `serial.setup("lpuart",
      115200)` in a script, and the script's thread sat in
      `furi_hal_serial_init_lpuart()` for good, in the loop that waits for TEACK and
      REACK. Read over the monitor, CR1 had UE and nothing else: no TE, no RE, so
      the model never raised either flag. The firmware had written TE and RE; the
      LL driver's `LL_LPUART_Init()` had not, because it configures only a
      peripheral that is off, and returns an error nobody reads when UE is set
      already. UE was set by the firmware itself: before every deep sleep
      `furi_hal_serial_control_suspend()` switches both UARTs off, and after it
      `furi_hal_serial_resume()` switches on each one that is not marked enabled,
      whether or not anybody has opened it. On a real Flipper that write goes
      nowhere: the bus clock of a UART nobody has is off, and `furi_hal_bus`
      holds it in reset besides (the RSTR bit stays set from the last
      `furi_hal_bus_disable()` to the next enable). Here the registers of a
      peripheral in reset took writes. So the port worked in a firmware that had
      not slept yet, a second after the start, and hung in one that had.
    - The fix is in the clock controller's model: a peripheral with a reset line is
      reset on both edges of its RSTR bit, when the firmware asserts it and again
      when it lets go, which throws away what was written between. (Refusing the
      writes would be the same and need a gate in every model.) USART1 and LPUART1
      got reset lines; they had none. The LPTIM, SPI, I2C, USB, timer, ADC and AES
      models, which had theirs, are now reset twice where they were reset once.
    - **Where the other end is.** A pty alone was the first try, as for the USB
      serial port, and it is wrong for a launcher command that sends one line and
      leaves: QEMU looks only once a second whether a pty has been opened, what is
      written to a pty and closed stays in it for whoever opens it next (a later
      `uart send` got the line before), and the kernel does not say how much of
      what was written the other side has read (TIOCOUTQ is 0 at once). A socket
      has none of that, and a program that wants a serial port cannot open one. So
      the port is a chardev hub (`chardev/char-hub.c`, in QEMU since 10.0) of
      both: the pty or TCP port for the user's program, a unix socket for the
      launcher. What the firmware sends goes to both, what either sends goes to the
      firmware.
    - Three things the socket taught. Closing it right after the write loses what
      QEMU had not read yet, so `uart send` stays until the socket's queue is empty
      (SIOCOUTQ), which also tells it that the firmware has taken everything: the
      model takes a byte only when the firmware has read the one before. A client
      that is connected and does not read stops the firmware's sending after some
      277 bytes: the model writes a byte at a time, each costs the socket 768 bytes
      of its buffer, and a full buffer is a firmware that waits in its TX loop. So
      the launcher's end reads whatever it is doing. And the firmware gets about a
      byte a millisecond, at any baud rate: the model does not tell the chardev
      that the firmware has read a byte (no `qemu_chr_fe_accept_input()`), so the
      next one comes at the main loop's next turn.
    - **A character twice, every 277.** The check sends a line of 900 characters and
      wants it back in order, from the port's log file, where `uart wait` and
      `--expect` read. It came back 903 long: `rstuuvwxyz`. The socket's stream was
      right, and a trace of the model showed each byte sent once. The log is
      written by QEMU's `qemu_chr_write_buffer()`, which logs what the backend took,
      and logs the whole buffer when the backend reported an error and took
      nothing, "assuming this method won't be invoked again". EAGAIN is such an
      error, and the caller does come again with the same byte, which is then
      logged a second time. Every byte that had to wait for room in the socket was
      in the file twice. `qemu/patches/0006` leaves EAGAIN out of that branch. It
      was not this port's alone: `uart.log` of a run with `--serial pty` or
      `tcp:PORT` had the same, whenever the program on the port fell behind.
    - `uart wait` and `--expect` read the log file and not the socket, so that what
      the firmware sent while no command was running is not lost to them. A mark
      beside the log (`lpuart.mark`) has how far the last one read.
    - `scripts/uart_check.py` (38 checks, on all five firmwares) has
      `examples/uart/echo.js` on the Flipper's side: the port opened after the
      firmware has slept (the hang), a line answered in under a second, the codes
      and `--hex`, the 900 characters, what is not found twice, a program on the
      pty at the same time and one on a TCP port, `uart term` in a terminal of its
      own, the port closed and opened again and after a reset, `--lpuart none`. One
      check failed for a reason of its own: it typed into `uart term` before the
      terminal was raw, and setting a terminal raw throws away what is waiting.
    - Not there: reception by DMA (the firmware's USB-UART bridge does not hear),
      the IDLE flag, a baud rate that means something, `send` and `term` for
      pins 13 and 14.

105. **Pins 13 and 14: `uart --port usart`.** USART1 got what LPUART1 has: its
    chardev is a hub of what `--serial` said (`uart-user`: the terminal, a pty, a
    TCP port, a file, nothing) and a socket of the launcher's (`uart-own`,
    `devices/NAME/usart.sock`), with `uart.log` as the hub's log file. `uart send
    --port usart` and `uart term --port usart` are the same code with another
    socket and another lock.
    - What was expected to be in the way was not. The firmware has USART1 on from
      its first lines, for the log, and the model takes no second byte while the
      first is unread: a line sent to a port that only logs would have sat in the
      socket until the command gave up. The firmware logs with the receiver off
      (CR1 is `0x20000009` there: UE, TE, the FIFO), and the model drops what
      comes while RE is clear. So it is as on pins 15 and 16: what is sent while
      no app has the port is taken and lost, and `uart send` exits 0.
    - An app that takes the port takes it from the log (`furi_hal_serial_control`
      stops the log on it, and starts it again at the release). `uart.log` then
      has the app's bytes among the log's lines, which is what a serial adapter on
      the pins has, and the check looks for a line of the log between two runs of
      its script.
    - A run with `--serial file:PATH` where the path is `uart.log` itself (the
      checks start their devices so) has the file as the hub's first end and no
      log file on the hub. A `--serial` in QEMU's own words is passed through as
      before, without a hub, and `uart send --port usart` says what it needs.
    - Not there: the pin. The expansion service of the firmware waits for a pulse
      on pin 14 as a GPIO interrupt before it opens the port, and what is sent
      here goes into the UART, not onto PB7.
    - `scripts/uart_check.py`: 54 checks (38 before), on the five firmwares.

106. **The UART hears by DMA, at the baud rate (QEMU patch 0007).** The firmware's
    USB-UART bridge sent and did not hear, and every port took about a byte a
    millisecond whatever its baud rate. Both were the stock UART model's reception,
    which had four gaps.
    - No request to the DMA controller. The bridge receives into a circular buffer
      of 256 bytes on DMA1 channel 6 (USART1; channel 7 for LPUART1), with the
      channel's half and full interrupts and the UART's idle interrupt. The model
      has a `dma-rx` line now, high while DMAR and RXNE are set, wired to the DMA
      model's requests 14 and 16.
    - With the line wired the bridge still heard nothing, and the channel was off:
      TEIF6 in the DMA's ISR, EN cleared, CNDTR still 256. The channel reads RDR a
      byte wide, as PSIZE says, and the UART's registers refused every access that
      was not four bytes; the DMA model answers a refused access with a transfer
      error, as the chip does for a bus error. Behind an APB bridge an access of
      any size is one of the whole register, so the model takes them now.
    - No IDLE flag. It is set two frame times after the last byte, 2 ms at least
      (the bytes come from a socket, not from a line that keeps bit times), once
      for a run of bytes. The firmware has the idle interrupt on in its byte-wise
      mode too (`furi_hal_serial_async_rx_start`), so every app got a new
      interrupt; the JavaScript `serial` module and its 900-character line did not
      mind.
    - No pacing. A byte was taken whenever RXNE was clear, and the backend was not
      told when RDR had been read, so it looked again at the main loop's next
      turn. Now a byte is taken when the one before has had its frame time, with a
      timer on the virtual clock and `qemu_chr_fe_accept_input()`. Measured with
      `examples/uart/echo.js` at other rates, 240 bytes: 2.3 s at 1200 baud (2.0 s
      on a wire), 0.57 s at 9600 (0.25 s), 0.21 s at 921600; most of what is over
      the wire's time is the launcher starting. At 115200 a byte comes every
      130 µs (87 µs on a wire): the rest is the main loop's turn for each.
    - No PRESC register. The firmware's driver picks a prescaler for the LPUART
      from 9600 baud down (/2 there, /16 at 1200) and writes BRR for it; without
      the register the pace at 9600 baud would have been that of 19200. The
      LPUART's rate was also taken from 16 bits of BRR by the USART's formula; it
      is 256 x clock / prescaler / BRR, with 20 bits.
    - Order inside the model: the DMA request is set before the interrupt line,
      because the channel's read of RDR comes back into the model while the first
      call is still on its way; and the time of the next byte is set before either.
    - `uart send` assumed 400 bytes a second at the least for its time limit. At
      1200 baud that is wrong, so `--for` is now how long nothing may move. It
      sends in pieces of 16 bytes, because an AF_UNIX socket takes a piece out of
      its count (SIOCOUTQ) only when the last byte of it has been read.
    - The machine connects `dma-rx` only where the device has the line. A QEMU
      tree without the patch gives a machine that starts, with the ports as they
      were; a plain `qdev_connect_gpio_out_named()` would have ended it at its
      start. Built that way once to see it: the CLI answers, and the check below
      is 61 of 64, the three being the baud rate and the bridge's two receptions.
    - The bridge asks before it ends ("Exit USB-UART?"): Back, then Left. Until
      then the CLI has no USB port, and `cli`, `push` and the rest say that there
      is no prompt.
    - Not there: pacing of what the firmware sends; overrun (a byte the app does
      not read waits in the socket, and `uart send` says after `--for` that the
      rest was not taken); a rate on the host's side, so a wrong baud rate is
      still understood.
    - `scripts/uart_check.py`: 64 checks (54 before), on the five firmwares: the
      two seconds of 240 bytes at 1200 baud with `--for 1`, and the bridge both
      ways (a short line, which only the idle interrupt delivers, and 600 bytes,
      which go round the buffer twice).

107. **The serial ports by name.** What an app had made of a port was not to be seen
    from outside. `gpio` had `alternate function 8` on two pins; a `uart send` to a
    port that no app had open took the bytes, exited 0 and said nothing; and an app
    with another baud rate than the board's was understood all the same (entry 106),
    so nothing showed that either.
    - The machine has a property for it, `header-serial`, read only: a word for
      each port, `USART1:13:14:230215:8N1:on:off`. The name, the two pins, the
      baud rate from BRR and PRESC (0 while UE is clear), the frame, whether TE is
      set, and who is told of a byte that comes in: `irq` (RXNEIE), `dma` (DMAR),
      `off` (RE clear) or `unread` (RE set and neither). It is made from the UART
      model's registers when asked. PRESC is read through the bus, because the
      model has that register only with patch 0007.
    - `gpio` has the ports beneath its pins, `status` has them as `serial`, `uart`
      alone has a second line for each port, and the window has them in the GPIO
      menu, in a pin's tip, and in the console when one opens or closes. A rate
      within half a percent of a usual one is said as that one: the firmware's
      divider for 230400 baud gives 230215.
    - The two pins of a port are named for it in `gpio`: `USART1's TX` on 13 and
      `USART1's RX` on 14 always, `LPUART1's TX` and `LPUART1's RX` on 15 and 16
      while the firmware has them as alternate function 8.
    - `uart send` to a port that hears nothing still exits 0, as a wire takes what
      nobody reads, and says on stderr which of the two it is: the port is off, or
      its receiver is.
    - Seen with it: the firmware logs at 230400 baud with USART1's receiver off,
      and the receiver goes on as soon as an app has either port open, with nothing
      reading it. That is the firmware's doing. Its serial control keeps the
      receiver off while the expansion service has an interrupt on pin 14, to see a
      module's pulse; an app that takes a port disables that service first, and
      the receiver is put back on. The log's port then takes one byte into RDR,
      which stays there, and the next one waits (a real UART would lose it and set
      its overrun flag, which nothing reads either). `uart send` says so for that
      port after its `--for`.
    - The JavaScript `serial` module reads by interrupt and the USB-UART bridge by
      DMA at 115200 baud, on all five firmwares.
    - Not there: other peripherals by name (SPI, I2C, a timer's PWM on a pin); a
      rate on the host's side, so the wrong baud rate is now shown, and still
      understood.
    - `scripts/uart_check.py`: 68 checks (64 before); `scripts/gpio_check.py`: 54
      (53); `scripts/gui_check.py`: 47 (45), on the five firmwares.
108. **A byte as a module's call (QEMU patch 0008).** The firmware's expansion
    service was never woken here. It waits for a module with the log port's
    receiver off and an interrupt on the fall of pin 14 (entry 107), and a module
    says that it is there by pulling that pin low for a moment. What `uart send`
    wrote to the port went to the UART model, which dropped it, its receiver being
    off, and the pin never moved.
    - QEMU's model now says when that happens: patch 0008 gives it a line,
      `rx-unheard`, pulsed for each byte that comes while the receiver is off.
    - The machine puts the byte's start bit on the pin: PB7 or PC0 low through the
      GPIO model's input, and a timer on the virtual clock that lets go a bit's
      time at 9600 baud later, back to what was held there or to nothing. The
      pins' record has both edges. A pin the firmware drives is left alone.
    - The firmware does the rest on its own: the EXTI interrupt, its expansion
      worker takes the port from the log, sets 9600 baud, sends its heartbeat
      (`01 01`), waits 250 ms for a module's baud rate, gives the port back and
      waits again. In a record of it pin 14 is low for 137 µs, the port's own
      (`x`) 1.1 ms after that, and let go 254 ms later.
    - `header-serial` has a fifth word for who hears, `edge`: the receiver off
      (or the whole port) and the RX pin an input whose EXTI line is on this
      port, with the falling edge and the interrupt on. `gpio`, `status`, `uart`
      and the window say it as `its receiver is off, and the firmware waits for a
      call on pin 14 (an interrupt on its fall, as for an expansion module)`, and
      `uart send` says after such a byte what the firmware took it as, where it
      said before that nobody had heard it.
    - With Listen UART at LPUART in the firmware's settings the same goes for pin
      16, with LPUART1 off, and the heartbeat comes out of pin 15 (by hand, on
      the official 1.5.1).
    - Only the start bit is on the pin. A byte's data bits are not, with the
      receiver on or off: the handshake needs the edge alone, and the firmware
      has the port open before it reads anything.
    - Not there: the module's side of the protocol as a tool (the baud rate,
      heartbeats, an RPC session over the port).
    - `scripts/uart_check.py`: 73 checks (68 before), on the five firmwares;
      `gpio_check.py` (54) and `gui_check.py` (47) want the new words.
109. **The launcher as an expansion module (`expansion`).** With a byte as the call
    (entry 108) the firmware's expansion service could be woken, and no more: what
    a module says next had to come from a program of one's own.
    - `scripts/fe_expansion.py` is the module's side, written from the protocol's
      description in the firmware's documentation: the five frames with their
      checksum, the call, the baud rate, heartbeats, control commands and data
      frames, each confirmed, and `Lost` with the reason when the firmware does
      something else. It talks through two functions (send, and what came within a
      time), so the launcher's socket and a pty carry it alike.
    - `./flipperemu expansion` runs it on the port the firmware waits on
      (`header-serial`: `edge`), prints every frame of both sides with its time,
      and with `--rpc` starts an RPC session, sends it a ping and has the bytes
      back. The official 1.5.1 answers the call in 2 ms, confirms 115200 baud 6 ms
      later, and answered 12 heartbeats in a second; 9600 and 921600 baud are
      confirmed as well, 100 baud is refused with status 2.
    - A call right after a link has ended goes wrong: the firmware is still in
      that link for 250 ms after its last frame, and takes the call byte as a bad
      frame. So `expansion` waits for `edge` before it calls, and at its end until
      the firmware waits again; two runs one after the other work.
    - While the link is up `header-serial` has the port at 115200 baud, read by
      interrupt. With Listen UART at LPUART the same on pins 15 and 16, found
      without `--port`; with None `expansion` says that no port waits (both by
      hand, on the official 1.5.1).
    - Tried beside it: a key in the terminal of a `run` in the foreground is a
      call (the heartbeat's two bytes come out), and so is a byte from a program
      on the pty of a `--serial pty` run, where the module's side was answered as
      on the launcher's socket. The first byte after a pty is opened can be late
      (QEMU looks for that once a second).
    - The frames are in `uart.log` between the log's lines, and `log` shows their
      bytes in front of the next line.
    - Not there: more of the RPC session than a ping, the 5 V commands, a module
      that goes wrong on purpose.
    - `scripts/uart_check.py`: 79 checks (73 before), on the five firmwares.
110. **I2C devices on the header (`run --i2c`, `i2c`; QEMU patch 0009).** I2C3 was
    there on pins 15 and 16 with nothing on it: the firmware's scan found no
    address, and an app for a sensor board had nobody to talk to.
    - QEMU has the devices (`tmp105`, `at24c-eeprom`, clocks, port expanders), and
      `-device TYPE,bus=NAME,address=N` puts one on a bus by the bus's name. Both
      buses of the SoC were named `i2c`. They are `i2c1` (the power bus) and `i2c3`
      now, a property of the I2C model that the SoC sets, and `run --i2c
      TYPE@ADDRESS` is that `-device` on `i2c3`, with the id `i2c-NN`.
    - Address 0. The firmware's scan calls all 128 addresses, and QEMU's bus has
      every device answer the first of them, the general call. No device here
      listens to that, so the model takes address 0 as unanswered, and does not
      ask the bus.
    - The model counts what the firmware did on its bus (addresses called, those
      that nobody took, the last one), and the machine has that with the devices
      as the property `header-i2c`. `i2c`, `gpio` and `status` say it in words. A
      scan is `the firmware called 128 addresses, 2 answered; the last: 0x7f to
      write, no answer`.
    - A temperature given with the device was gone at the start: QEMU's sensors
      set their registers when they are made, after the properties of `-device`
      were applied. The launcher sets such a value through the monitor as soon as
      the emulator answers. QEMU has a temperature in thousandths of a degree;
      `i2c` takes and prints °C. (A TMP421 rounds: -12.25 reads back as -12.249.)
    - `i2c ADDRESS NAME VALUE` is `qom-set` on `/machine/peripheral/i2c-NN`, and
      the device's line is its properties without those every device has. What
      QEMU refuses comes back in its words (`value 200.000 C is out of range`); a
      property that is fixed once the device is made (`rom-size`) is said to be.
    - The EEPROM forgot its contents with the emulator. A drive keeps them, and
      QEMU refused the drive: `Backing file size 512 != 256`. The block layer
      gives a raw image's length in whole sectors of 512 bytes and the model
      wanted the EEPROM's size, so only an EEPROM of a multiple of 512 bytes could
      have a file. `qemu/patches/0009` compares with the size rounded up to a
      sector; the file of a 256-byte EEPROM stays 256 bytes long. The launcher
      makes the file (`devices/NAME/i2c-NN.bin`, 0xFF throughout) and the drive,
      and refuses a start when a file of another length is there.
    - On Momentum `examples/i2c/sensor.js` reads 23.5 from the TMP105 (with 12
      bits, after it wrote the configuration register), -7.5 after `i2c 0x48
      temperature -7.5`, and counts its runs in the EEPROM's first byte: 1, 2, 3
      after a reset of the firmware, 4 after a stop and a start.
    - The official firmware's JavaScript (1.5.1 and the dev build) has no `i2c`
      module (`"i2c" module load fail`): there the check has the scan alone.
    - Not there: an app in C as the example, the bus in the window, a record of
      the transfers, the two lines as levels, a device added while it runs.
    - `scripts/i2c_check.py`: 35 checks on Momentum, Unleashed and RogueMaster, 31
      and one skipped on the official 1.5.1 and the official dev build;
      `gpio_check.py` is 55 (54 before), on the five.
111. **An app in C on the header's I2C bus; `run --i2c help`.** The official
    firmware's JavaScript has no `i2c` module, so on it nothing but the CLI's scan
    had talked on the bus.
    - `examples/i2c/sensor.c` does in C what `sensor.js` does: the TMP105's
      configuration written for 12 bits, its temperature read once a second
      (`furi_hal_i2c_tx`, `furi_hal_i2c_trx` on `furi_hal_i2c_handle_external`),
      the EEPROM's first byte read, counted up and written back. Built by ufbt
      for the official 1.5.1 it ran as written: 23.50 at the start, -7.50 within
      a second of `i2c 0x48 temperature -7.5`, run number 1, then 2.
    - Nothing in the model had to change for it. The bus's line counted its
      calls: `the firmware called 36 addresses, all answered`.
    - Without a sensor (a start with the EEPROM alone) a transfer comes back
      false and the app says "does not answer"; the count goes on.
    - `examples/i2c/test.sh` is the app's test on a Flipper of its own, with the
      two devices given to `run` and the temperature set between two looks at
      the screen (`screens/warm.txt`, `screens/cold.txt`).
    - A type that QEMU has not was found late. With `--i2c nosuchchip@0x48` the
      firmware was flashed and the card made, and then QEMU ended with
      `'nosuchchip' is not a valid device model name`. An `lm75` goes the same
      way: QEMU's name for those registers is `tmp105`. The launcher asks the
      QEMU it starts for its I2C devices (`-device help`, those of `i2c-bus`) and
      refuses the type before anything is done, with their names.
    - `run --i2c help` prints that list. QEMU describes a third of them; for the
      others the launcher has a few words each (`tmp105`: a temperature sensor,
      with the registers of an LM75 or TMP75).
    - Seen and left: QEMU's TMP105 takes a temperature in steps of its own, and
      `i2c 0x48 temperature 0.0625` reads back as 0.058; the app shows 0.00.
    - The app was built for the official 1.5.1 alone, the SDK that the ufbt here
      has. On the other four the check skips it, in words: `ufbt's SDK is not
      for this firmware`.
    - `scripts/i2c_check.py`: 37 checks and one skipped on the official 1.5.1
      (the script), on Momentum, Unleashed and RogueMaster (the app); 33 and two
      skipped on the official dev build.

112. **The I2C bus in the window; `i2c --json`.** The window had the header's pins
    and its serial ports, and nothing of the bus: a sensor on it was set from a
    shell.
    - The GPIO menu has a line for the bus in `i2c`'s words and, beneath it, one
      for each device that opens to the device's values. A temperature has `set
      it…`, a dialog in °C. The window sets it with the launcher's `i2c ADDRESS
      NAME VALUE`, as it makes every change.
    - A device's values are not on the front-panel link. They are properties of
      QEMU's own devices, other ones for each type, and the link has 64 watches
      at most. The launcher already knows which of them are a device's own and
      that a temperature is in thousandths of a degree, so it got `i2c --json`:
      the bus, and each device with its values. The window asks for it when the
      devices of the bus change, when the GPIO menu opens, and after it set a
      value. A program can use it as well.
    - The words for a value are in `fe_header.py` now, for the launcher and the
      window alike: the menu's line cannot differ from `i2c`'s.
    - The console and the firmware's calls. `header-i2c` changes with each call,
      and a scan is 128 of them in under a second. The window waits until the
      bus has been quiet for 0.7 s (3 s at most, for an app that never stops)
      and writes one line with what the calls since its last line came to.
    - An app that reads a sensor every second would be a line a second that way.
      A line is written only when the calls went differently than those of the
      last line (all answered, some, none) or after ten seconds of silence. The
      menu's line has the numbers at any time.
    - Tried with the firmware's scan: one line, `Pins 15 and 16 (I2C3): the
      firmware called 128 addresses, 2 answered; the last: 0x7f to write, no
      answer`.
    - Left for the record of transfers: a word on an address that was not
      shifted (the app calls 0x24, the sensor is at 0x48).
    - `scripts/gui_check.py` has three more checks (50, on the five firmwares),
      with a TMP105 and an EEPROM on the bus of its device. The dialog is
      answered by a timer that finds it, since `exec()` does not return before
      that. `scripts/i2c_check.py` has one more (38 and one skipped; 34 and two
      skipped on the official dev build).

113. **The setup script applied a patch twice (`scripts/setup_qemu.sh`).** Found
    while looking whether the tree here had all nine patches: asked by itself
    (`patch -R --dry-run`), 0007 did not come out of it, though the tree's files
    were the tarball's with the nine patches, byte for byte.
    - 0008 builds on 0007 in the same file (`hw/char/stm32l4x5_usart.c`). Lines
      that 0007 added are no longer as it left them, so it does not come out
      while 0008 is in. The script asked each patch that way, and applied what
      did not come out.
    - On a tree with 0008 it therefore applied 0007 again: two of its 21 hunks
      went in a second time, the other 19 were refused, `.rej` and `.orig` files
      were left, and the script stopped before the build. A checkout that ran
      the script between 0008 and 0009 met that with 0009 (the README said "once
      more" for it); every other one would have with the next patch.
    - Now the patches are taken out of a copy of the files they touch, the last
      one first. What does not come out there is what the tree lacks.
    - Those are tried on a second copy before the tree is touched. A patch that
      does not fit leaves nothing of itself in the tree, and nothing of the
      patches before it in the series.
    - A tree that the script damaged is known by the `.rej` beside a patched
      file. The script names the file and the folder and says to remove the
      folder and run again (the tarball is beside it: extract, nine patches,
      build, about 6 min). It does not try to mend the tree.
    - The patches come before the machine is hooked into `hw/arm/meson.build`
      and `Kconfig` now, so a refusal changes nothing at all.
    - `scripts/setup_check.py` runs the script itself on stand-in trees: the
      patched files alone, from the tarball, and a `ninja` that prints its
      arguments. A new tree; a second and a third run; a tree from before each
      patch; a tree that lacks one patch (0007 has none: 0008 does not fit
      without it); the damaged tree, which the check makes the way the script
      did; a file that is not this QEMU's; and the tree here. 28 checks in ten
      seconds, and a patch that is added to `qemu/patches` is in them. 14 of
      them fail on the script as it was.
    - The tree here was whole all along, so the emulator was not built again.

114. **A record of the I2C bus's transfers; a watch that would not end.** `i2c`
    counted the firmware's calls and had the last one. What an app wrote to a
    device and what it read back was nowhere.
    - The I2C model got a tap: the machine gives it a function, and the model
      calls it at a START (the address, and whether a device took it), for each
      byte written or read, and at the end of what the START began (a STOP, or a
      START that follows with none). Only I2C3 has one. The machine writes a line
      of text for each such part to a file, with the virtual clock at the START:
      `1.281528054 0x48 w 01 60`. The property `header-i2c-record` begins and
      ends it, as `header-record` does for the pins, and both files are flushed
      by the same timer.
    - Text, and not a VCD as the pins' record is: the model has transfers and no
      levels, so there are no bits to draw, and a line can be compared with
      `grep` and `cut` in a test.
    - `i2c record FILE`, `i2c record stop`, `i2c record` (does one run, and what
      it has counted), and `i2c watch [ADDRESS] [--for S]`, which follows the
      file as it grows and has the device's type beside each address. With no
      record running the watch keeps one of its own and removes it.
    - The file's second line names what is on the bus. It had the devices in the
      wrong order, the EEPROM at 0x50 before the sensor at 0x48: QEMU's list of a
      bus's children has the last one made first. They are written in the order
      of their addresses now.
    - Seen in the first record: the script's `writeRead` and C's
      `furi_hal_i2c_trx` are a write with its STOP and then a read with its own
      START, two lines. The firmware makes no repeated START there (it has
      `furi_hal_i2c_tx_ext` for one, which neither example uses).
    - The word on an address that was not shifted, left over from entry 112: a
      call that nobody took, to an address while a device is at twice that or
      one above, is what an app makes that passes 0x48 where `0x48 << 1` is
      wanted. `i2c` has it beneath the bus's line, and the watch beneath the
      call, once for an address and not in a scan, where every address is
      called. `gpio`, `status` and the window's console do not have it yet.
    - **The watch that would not end.** The check on five firmwares at once:
      three passed, and on Unleashed and RogueMaster two checks failed, both of
      a watch that the check ends with Ctrl+C (SIGINT). Their output had `(still
      watching)`. Nothing differed between the firmwares. Those two runs had
      been started as `( … &)` from a shell that was not interactive, and such
      a shell starts its background jobs with SIGINT ignored; that is inherited,
      and Python installs its KeyboardInterrupt only where SIGINT is not
      ignored. The watch never saw the signal.
    - `gpio watch` had been shipped that way: a test that begins one in its
      background and ends it with `kill -INT` waits for ever, and with plain
      `kill` (SIGTERM) the watch went and left its record running in the
      emulator, with its file. Both watches now take SIGINT and SIGTERM as their
      end, whatever they inherited (`watch_ends()` in the launcher): the count
      is said, the record ended, the file removed, exit 0.
    - `scripts/gpio_check.py` and `scripts/i2c_check.py` each have a watch with
      no `--for` that is ended with `kill` (SIGTERM). For this entry the five
      runs of each were started as a script's background jobs on purpose, with
      SIGINT ignored: the checks that end a watch with Ctrl+C pass there too.
    - A record goes on over a reset of the firmware (the tap is the machine's,
      and a reset of I2C3 ends an open line) and is whole when the emulator is
      stopped under it: both are checks.
    - Not seen from a firmware: a line with `+` (no STOP before the next START),
      a byte with `!` (the device refused it) and a line with `...` (the record
      ended inside a transfer). The check gives the launcher such lines to read,
      and that is all: the machine's writing of them was not tried.
    - `scripts/i2c_check.py`: 63 checks and one skipped on Momentum, Unleashed
      and RogueMaster (the app), 60 and one skipped on the official 1.5.1 (the
      script), 55 and two skipped on the official dev build.
      `scripts/gpio_check.py`: 56 on the five.

115. **The I2C bus's record in the window, and the word on the shift.** Both
    were the launcher's alone (entry 114).
    - The window watches `header-i2c-record` over the panel link, as it does
      the pins' `header-record`: a red label in the status bar with the file
      and the addresses called, and in the GPIO menu, under the bus and its
      devices, Record what goes over the I2C bus… (a file dialog) or End the
      record of the I2C bus, with what the record has so far. Both lines are
      the launcher's `i2c record`. So a record that a shell began is the
      window's to show and to end, and a shell's `i2c watch` shows there with
      the record it keeps, for as long as it watches.
    - The property's value has the record's time in it and changes at every
      look. The window takes it for the label and nothing more, as it does the
      pins'.
    - The word on the shift is `i2c_hint()`, the function that `i2c` has it
      from: under the console's line for the firmware's calls, as often as
      that line comes (one for a burst of calls, the next only when the calls
      go differently or after ten seconds), and under the bus's line in the
      GPIO menu while that call is the last one.
    - The window cannot leave a scan out as the watch does: it has the count
      and the last call at each of its looks, and not every call. A scan is
      over in some ten milliseconds of the Flipper's time and ends at 0x7f, so
      the word would need the console's line to come in the middle of one.
    - No firmware calls an address that was not shifted by itself, and a
      script can on three of the five only (the official firmware's JavaScript
      has no `i2c`). The check gives the window the machine's word on its bus
      as the link gives it, with three calls to 0x24 in it, and after that the
      true one: the word has to come, and to go. That is the window's part of
      it; the call itself is in `scripts/i2c_check.py`, for `i2c` and the
      watch.
    - The check's picture of the word was of the firmware log, the tab in
      front at that point. It is of the console by itself now.
    - `scripts/gui_check.py`: 55 checks on the five.

116. **A START with no STOP, and a byte that a device refuses, from a
    firmware.** The record's `+` and `!` (entry 114) had been read by the
    launcher from a file of the check's and never written for a firmware:
    nothing here made either.
    - A device that refuses a byte: QEMU's `i2c-echo`, a device for its own
      tests. It keeps three bytes, gives them back to a read (0xff after
      them), and refuses a fourth. The EEPROM and the TMP105 take whatever
      comes. The check's last start has it at 0x42, beside the EEPROM.
    - A START with no STOP is not in the firmware's plain calls
      (`furi_hal_i2c_trx` is a write, a STOP, and a read) and not in the
      JavaScript module. `furi_hal_i2c_tx_ext` and `furi_hal_i2c_rx_ext` have
      it: a write that ends with `FuriHalI2cEndAwaitRestart`, and a read that
      begins with `FuriHalI2cBeginRestart`. So the check has an app in C of
      its own, `scripts/i2c_check_app`, which logs how each call went; the
      record has to say the same.
    - **It found a fault in the model.** After a byte to the echo device, a
      START with no STOP for the EEPROM read `21 12`: the echo's bytes. And
      one for 0x33, where nobody is, was answered. QEMU's bus looks for a
      device only when it is idle (`i2c_do_start_transfer`); a START inside a
      transfer goes to the device that has it, whatever the address. That is
      right for a START again to the same device, which is all that a write
      and a read of one register need, and all that had been tried. Now
      `i2c_start()` ends the transfer on QEMU's bus first when the address is
      another: the EEPROM answers with `e1 e2`, its own, and the call to
      nobody is `no answer` and false in the app. The first device is told of
      a STOP that the wire did not have, since QEMU has no other word for it.
    - **A refused byte is not always a failed call.** Four bytes to the echo
      device: the record has `01 02 03 04!`, and the firmware's
      `furi_hal_i2c_tx` is true. Five bytes: the fourth has the `!` again,
      the fifth is never sent, and the call is false.
      `furi_hal_i2c_transfer` does not look at NACKF. It sees a STOP that
      came early, and takes that for a failure only while bytes are still to
      send ("If the transfer was over, still a success"): with the last one
      refused, none is. The JavaScript `write` gives the same two answers.
      Seen in the emulator; a real Flipper was not asked.
    - More than 255 bytes in one transfer (RELOAD, which the HAL uses by
      itself) and a write that is held and gone on with (`FuriHalI2cEndPause`,
      `FuriHalI2cBeginResume`) are one line each in the record, as they are
      one transfer on the wire: 300 bytes read from the EEPROM of 256 are its
      contents and then the first 44 again.
    - A record that is ended while the app holds the bus after a byte has
      that line with ` ...`, and the read that follows is in no file.
    - The echo device asks the bus at every STOP to let it send as a master.
      Nothing lets it: the peripheral's model is a master only.
    - Where: the `+` on the official 1.5.1 (the SDK of the ufbt here); the
      `!` there and, from a script, on Momentum, Unleashed and RogueMaster;
      neither on the dev build.
    - `scripts/i2c_check.py`: 66 checks and one skipped on the official
      1.5.1, 64 and one skipped on Momentum, Unleashed and RogueMaster, 55
      and three skipped on the dev build. The model changed, no patch: a
      QEMU tree is built again (`scripts/setup_qemu.sh`).

117. **Sub-GHz keystores under the emulator's key, and what an update did to
    them.** A firmware package's Sub-GHz keystores are encrypted with a real
    Flipper's key 1 and stayed closed here (entry 61). Now every
    card the launcher builds gets the same contents under the emulator's own
    key 1: `keystores/overlay`, made by `scripts/rekey_keystore.py`
    ([keystores.md](keystores.md)). What came out when that was tried:
    - **A firmware update put the package's files back.** The updater
      unpacks its package's resources to the card when it has written the
      flash, the keystores among them, before the USB serial port is there
      again. The firmware that then starts names no KeeLoq remote's maker.
      `update` writes the emulator's four files to the card again when the
      Flipper is back (through its CLI: they are a few KB) and says so;
      `run --update` does the same from the run's own thread. After
      `update --no-wait`, and after an update that was started on the
      Flipper itself, nobody is there to do it: the launcher names the
      command, `push keystores/overlay/subghz /ext/`, which works for a
      device that runs and for one that is stopped. A card that was built
      before the overlay has the package's files too.
    - **The firmware's log is no witness.** `Load_keystore keeloq_mfcodes OK`
      is printed for a file that decrypts to noise as well. So the check
      asks the decoders: it makes up remotes, as recordings for
      `subghz decode_raw`.
    - KeeLoq: a remote for each of the 72 codes with simple or normal
      learning (41 and 31 of the 124), made with that code, and the firmware
      has to name it after the code. The decoder accepts a code when the
      encrypted part holds the low ten bits of the serial number; for some
      makers it wants something of their own there (Centurion in every
      firmware here, three more in Unleashed and RogueMaster), and for
      those the check makes the remote a second time by that rule. 72 of 72
      on five firmwares; none of 72 with the package's own file.
    - Nice Flor-S needs the 32 bytes of the `nice_flor_s` RAW keystore to
      read a remote's serial number and counter: 8 made-up remotes, 8 read.
      **Unleashed's decoder (RogueMaster has it too) read none at first.**
      It also knows Nice O-Code, a protocol with a frame of the same 52
      bits, and tells the two apart by what follows: the same frame again
      is a Flor-S remote, whose button is held; a frame that comes once is
      taken for O-Code. The check now sends each packet twice, as a remote
      does. And that decoder keeps the table in memory once it has read it,
      so a `nice_flor_s` that changes on the card counts from the firmware's
      next start: for the control with the package's own files the firmware
      is stopped and started.
    - The firmware's own debug commands make the tracked files:
      `subghz encrypt_keeloq` loads `keeloq_mfcodes` and saves all 124 lines
      again, the same bytes with the same IV, and `subghz encrypt_raw` makes
      each of the three RAW keystores from its decrypted bytes.
    - The overlay's `keeloq_mfcodes` has 124 codes. The packages' own have
      63 lines (official 1.5.1 and dev), 65 (Momentum) and 125 (Unleashed,
      RogueMaster): one more than the overlay in those two, and which one
      cannot be seen without a real Flipper's key. Unleashed's package has
      no `came_atomo`; its card gets one.
    - Not tried: an Alutech AT-4N or a CAME Atomo remote (those two
      keystores are checked as files, and made by `encrypt_raw`), and the
      52 KeeLoq codes with other kinds of learning.
    - `scripts/keystore_check.py`: 32 checks on the official 1.5.1, its dev
      build, Momentum, Unleashed and RogueMaster (19 of them need no
      firmware). `scripts/update_check.py`: 33 checks now, with the
      keystores after `update` and after `run --update`.

118. **What a longer KeeLoq key list costs, and whose type numbers the overlay
    has.** Two more decrypted key lists were to go into the local list
    (`keystores/plaintext/`, ignored by git; the tracked overlay is as it
    was). Together with the overlay's 124 codes they are 298 lines for 215
    keys: 174 new lines with 94 new keys, 17 of which are another key's
    bytes in the other order. What came out when the lists were tried on
    the firmwares:
    - **Every line costs every remote.** The KeeLoq decoder tries the lines
      in their order and takes the first whose decryption has the packet's
      button and the low byte of its serial number, or a zero there: a
      foreign packet passes about once in 2048 tries. 300 made-up remotes
      with random keys: with the overlay's 124 lines the official firmware
      and its dev build give 23 of them a maker's name (7.7 %), Momentum 22,
      Unleashed and RogueMaster 73 (24.3 %). With all 298 lines the official
      firmware names 88 (29.3 %) and Unleashed 130 (43.3 %).
    - **Unleashed's `JCM_Gen2` line is 256 tries**, one for each value of a
      byte that the packet does not carry (`keeloq.c`). It is line 91 of the
      overlay, and a remote whose line comes after it has that name for
      about one packet in nine: of 147 made-up remotes, 13 at the first try
      on Unleashed and none on the official firmware, which has no type 16.
    - **A type-0 line is eight tries**: simple, normal, secure and magic-XOR
      learning, each with the key and with its bytes in the other order. So
      a type-0 line for a key that has a line with its type adds tries and
      nothing else, and so does a line of type 1 to 4 for a key that has a
      type-0 line. `scripts/rekey_keystore.py merge` leaves those out, and a
      type that no firmware here has: 230 lines for 201 keys, no key of the
      298 without a line (in one byte order at least). The official firmware
      then names 37 of the 300 (12.3 %), Momentum 36, Unleashed and
      RogueMaster 87 (29.0 %). `merge --all` keeps everything but a repeat
      of key and type.
    - Each of the five firmwares loads the 230 lines and saves them again
      (`subghz encrypt_keeloq`), line for line, and names a made-up remote
      for each of the 147 lines with simple or normal learning: at the
      second try on the official firmware, its dev build and Momentum, at
      the third on Unleashed and RogueMaster.
    - **The overlay has Unleashed's type numbers, and the official firmware
      has others above 4.** Magic-serial learning 1, 2 and 3 is type 5, 6
      and 7 in the official firmware and 6, 7 and 8 in Momentum and
      Unleashed (5 is FAAC there). Made-up remotes for the overlay's five
      codes of that kind, three each: the official firmware names none of
      the 15, Momentum 14, Unleashed 12. With the 230 lines, which have a
      second line with the official number for four of the five, the
      official firmware names those 12. The same holds for Kingates Stylo
      4K, by the source and not tried: the official decoder tries lines of
      type 1, Momentum's takes the line with the maker's name, Unleashed's
      the first line of type 10, which is what the overlay has. Entry 117
      said that one overlay serves every firmware; for these six codes it
      does not serve the official one yet.
    - `merge` prints names and types, never a key, and a line that is not
      `KEY:TYPE:NAME` is no longer shown in the tool's message.
      `scripts/keystore_check.py`: 36 checks on the five firmwares, 23 of
      them without one.

119. **An encrypted keystore shows how long each line is.** The firmware
    pads each line of a keystore to whole blocks of 16 bytes and writes it
    as a line of hex of its own. So a file that no key here opens still
    gives the number of its lines and the length of each, to the block.
    Unleashed's and RogueMaster's own `keeloq_mfcodes` has 125 lines where
    the overlay has 124, and entry 117 said that the line cannot be told.
    By the lengths it is the overlay's list with one line of 17 to 32
    characters more, somewhere after the overlay's lines 3 to 45.
    - Then a later list of Unleashed's came, decrypted (126 lines; local,
      as the lists of entry 118). It is the overlay's 124 lines, line for
      line, with `HomeGate` (simple learning) after line 22 and
      `Phantom_Hiland` (normal learning) after line 26. The first fits the
      package's file. The second is a block too long for it: unlshd-094 has
      the one and not yet the other. That is by length: a line as long with
      another name would look the same.
    - With that list as the base, `merge` gives 231 lines for 202 keys, and
      299 lines with `--all`. Each of the five firmwares loads the 126 and
      the 231 lines and saves them again line for line, and names a made-up
      remote for each line with simple or normal learning (74 and 148).
      Of 300 made-up remotes with random keys they name as many as with 124
      and 230 lines: the official firmware and its dev build 23 and 37,
      Momentum 22 and 36, Unleashed and RogueMaster 73 and 87. The
      magic-serial remotes of entry 118 come out the same too (official 0
      of 15 with the 126 lines and 12 with the 231, Momentum 14, Unleashed
      12).
    - The tracked overlay is as it was, 124 lines: it is Unleashed's list
      of before unlshd-094.

120. **The overlay's KeeLoq list is the merged one.** The tracked
    `keeloq_mfcodes` was Unleashed's list of before unlshd-094, 124 lines.
    It is now what `merge` makes of the later list of entry 119 and the
    longer list of entry 118: 231 lines for 202 keys, the 126 first and as
    they are, then 105 that they have not. The decrypted lists stay out of
    the repository, as before. The file is under the emulator's key 1,
    which is public: `verify` prints its lines.
    - Only the file changed, and no script. `keystore_check.py` takes its
      numbers from the tracked file: each of the five firmwares loads 231
      of 231 lines and saves them again as the tracked file, and gives 148
      of 148 made-up remotes their line's name (simple learning 61, normal
      87) within the check's three tries. 93 of those lines come after
      Unleashed's `JCM_Gen2` line, and three tries are enough there too.
      36 checks of 36 on each.
    - What it costs was measured for entry 119: of 300 made-up remotes
      with random keys, 37 have a maker's name on the official firmware
      where Unleashed's list alone gives 23, 36 for 22 on Momentum, 87 for
      73 on Unleashed.
    - What it mends: the official firmware names the made-up magic-serial
      remotes of four of the five makers (12 of 15; there is no second line
      for Magic_1), and its Kingates Stylo 4K decoder has a line of type 1
      (not tried with a remote).
    - What it does not mend: 13 lines have type numbers of other tools,
      which Unleashed reads as another kind of learning, and four have a
      magic-serial key as normal learning. Such a line is one more try for
      every packet, and it names no remote of its maker.
    - The other checks with the file in place: the smoke test on the five
      firmwares, `files_check.py` on the official firmware and Unleashed,
      `enclave_check.py` on the official firmware, its dev build and
      RogueMaster, and `update_check.py` with 33 of 33 on the official
      firmware (updated to itself, to Unleashed and to the dev build), on
      Unleashed, on RogueMaster and on the dev build.
    - On Momentum `update_check.py` had 32 of 33, twice: after the first
      update the firmware's log had a crash report, `[CRASH][ISR MemMgmt]
      NULL pointer dereference`. The list has no part in it. Counted on
      throwaway devices, eight to eleven emulators at a time: 6 of 50
      updates with the 231 lines in the tree, and 3 of 32 in a copy of the
      tree with the 124. A debugger that waited at the fault handler
      (`gdb --serve`, a breakpoint at the MemManage handler) caught it
      twice, at the same place: the Desktop draws its slideshow view, and
      `desktop_view_slideshow_draw` asks `slideshow_is_loaded` of a
      pointer that is still zero. (The names are the official source's,
      which the code has the shape of; Momentum's source was not read.)
      An updater leaves a slideshow for the Desktop's next start (in the
      official source, the package's splash picture). The view dispatcher
      makes a view the current one and then calls its enter callback, and
      that callback makes the slideshow: a redraw between the two draws a
      view that has none yet. The firmware restarts and goes on. Each
      report came 3 to 5 seconds before the `update` command was done,
      and in the two that were looked at more closely before the launcher
      had the USB port back, so before it wrote a file. In each of the 82
      updates the update was complete and the four keystores were on the
      card afterwards.
    - 16 updates of the official 1.5.1, counted the same way, gave no
      such report. The other three firmwares were not counted so; their
      update checks passed. Not known: whether a real Flipper with
      Momentum does it, how often an emulator that runs alone does, and
      what brings a redraw at that moment.
    - A card that a device has already keeps the 124 lines until an
      `update` or `push keystores/overlay/subghz /ext/`.

121. **Two KeeLoq keystores where there was one.** Entry 120 put the 231
    lines of both lists into `keeloq_mfcodes`, the file that every
    firmware reads. That file is Unleashed's list again, 126 lines, and
    the other 105 are a keystore of their own beside it,
    `keeloq_mfcodes_kat`: the same format and the same key, for a program
    that asks for it. The first file and then the second are the 231
    lines in their order.
    - Which KeeLoq lists a firmware reads by itself: two,
      `keeloq_mfcodes` and `keeloq_mfcodes_user`. The official source has
      the two names in `lib/subghz/types.h`; the Sub-GHz app loads them
      when it starts (`helpers/subghz_txrx.c`), and `subghz decode_raw`
      with each call (`subghz_cli.c`). The binaries of Momentum and
      Unleashed have these two paths and no third. (The official
      firmware, its dev build and RogueMaster keep the Sub-GHz app
      outside `firmware.dfu`, and `strings` finds no such path there.)
    - Measured on the five firmwares: with `keeloq_mfcodes_kat` beside
      the first list, none of 74 made-up remotes of its codes is named;
      with the same file as `keeloq_mfcodes_user`, all 74 are. The
      firmware's `subghz encrypt_keeloq` loads either keystore and saves
      it again: 126 of 126 lines, and 105 of 105.
    - The tool: `merge --new-only` writes the new codes without the base
      list's lines. For the two decrypted lists that is the 231 from its
      line 127 on, byte for byte.
    - The check gives a remote five packets where it gave three, and
      says how many were needed. The first run on Unleashed failed with
      three: one remote of the second list (`Leopard`, simple learning)
      had the name `JCM_Gen2` three times. Its fourth packet has its
      line's name, on RogueMaster too (the made-up packets are the same
      in every run). That line of Unleashed's is 256 tries, so a packet
      of a later line has its name about one time in nine, and three in a
      row for one of 74 remotes comes in about one such set of remotes in
      nine. With the 231 in one file no remote needed more than three:
      the remotes had other serial numbers. The official firmware, its
      dev build and Momentum need two packets for both lists.
      `keystore_check.py`: 45 checks of 45 on each of the five (28
      without a firmware).
    - What the split gives back: a firmware's false names are those of
      the 126 lines (entry 119: 23 of 300 on the official firmware, 22 on
      Momentum, 73 on Unleashed; the 231 had 37, 36 and 87). What it
      takes: with the first list alone the official firmware names no
      made-up magic-serial remote (the second lines with its numbers are
      in the second file), and its Kingates Stylo 4K decoder has no line
      of type 1.
    - An earlier card: on one with the 231 in one file and no second
      file, `push keystores/overlay/subghz /ext/` wrote the two KeeLoq
      keystores and left the other three, on a device that ran and on a
      stopped one. The second file as the user list, by `push` with the
      new name, and `rm` of it: tried on both too.
    - An updater leaves `keeloq_mfcodes_kat` alone, because no package
      has a file of that name: after an update the launcher writes 4 of
      the 5 files, and 3 after one from Unleashed to itself, whose
      package has no `came_atomo` either. `update_check.py`: 33 of 33
      on the official firmware (updated to itself and to Unleashed),
      its dev build, Momentum, Unleashed and RogueMaster. The smoke
      test passes on the five.
    - No program here loads both lists yet. What stands for one: the
      loader's round trip with the second file, and a firmware that reads
      it as its user list. `keeloq_mfcodes` is Unleashed's list for every
      firmware: the official firmware's own 63 lines and Momentum's 65
      are in their packages under a real Flipper's key, and no decrypted
      copy is here.

122. **Interrupts from the header's pins, and an open-drain output.** The two
    things that the firmware's `gpio` CLI command cannot do with a pin were
    untried since entry 100. Both worked as they were. A held header pin's
    edge goes the way a key's does (the GPIO model's line, SYSCFG's choice of
    the port, the EXTI's edges, the NVIC), and the GPIO model has had
    open-drain lines right since QEMU patch 0004. What was missing: a way to
    see them, and a check.
    - The check has an app of its own, `scripts/gpio_check_app` (C, built with
      ufbt and the official 1.5.1's SDK, as `scripts/i2c_check_app` is). PA4
      has an interrupt on both edges, PC0 on its fall, PC1 on its rise, PB2 on
      its fall, which ends the app. PA7 is an open-drain output with a
      pull-up, pulled low while PA4 is low. Each interrupt is a line in the
      log with its number and with what PA7 reads then; every rise of PC1
      turns PC0's interrupt off, or on again.
    - `gpio`, `status --json` (`"interrupt"`) and the window's GPIO menu say
      which edges of an input the firmware has an interrupt for: `input,
      pull-up, interrupt when it falls`. The machine's property `header` has
      `in+r`, `in+f` or `in+rf` as the mode. It is read from the registers: the
      pin is an input, SYSCFG has its port for the line, the line is not
      masked, and the edges are RTSR's and FTSR's. After a start one pin has
      the words, pin 14: the fall with which the firmware waits for an
      expansion module's call.
    - 16 checks more in `gpio_check.py`: 72 on the official 1.5.1; 56 and the
      app's part skipped on its dev build, Momentum, Unleashed and
      RogueMaster, whose SDKs ufbt has not. A pin held low and high from
      outside is one interrupt for each edge that was asked for, and none for
      the other. An interrupt that is off gives none, and keeps none for when
      it is on again (a masked line sets no pending bit: entry 6). PA7 is low
      while the app pulls it low, also with 3V3 held to it; high by its
      pull-up when the app lets go; and held low from outside then, the
      firmware reads it low. A record has PA7's six levels in their order.
    - "PB2 rose", the app logged, for a pin asked for its fall alone. Not the
      model. Line 2 has its rising edge from the firmware's start
      (`furi_hal_resources.c` makes PA2, the NFC chip's interrupt,
      `GpioModeInterruptRise`), and `furi_hal_gpio_init` sets the edge it is
      asked for and takes none away: RTSR is 0x00163c4c after a start. So
      PB2's interrupt has both edges, and the table says so. The app got PC0
      for the fall alone and PB2 for its end.
    - A line is its pin number's, with one handler in the firmware
      (`furi_hal_gpio_add_int_callback` has a `furi_check` for a line that is
      taken). The keys have lines 3, 6 and 10 to 13 (OK is PH3, Down is PC6),
      the expansion service line 7 while it waits on pin 14, the NFC line 2
      while it is on. That leaves the header PA4, PB2, PC1 and PC0. Tried: the
      app with PA6 in PA4's place ends in `furi_check failed` in the emulator
      too.
    - How the app ends matters to the next one. `furi_hal_gpio_init` to
      analog takes a pin's edges off its line only while the pin's interrupt
      is on, so the app turns PC0's on again, makes each pin analog and then
      removes its handler. With that RTSR is 0x00163c48 afterwards: line 2
      has lost the rise that it had from the start. The NFC asks for it again
      when it starts (`furi_hal_nfc_init_gpio_isr`), and the check has the
      firmware read a tag after the app: it does.
    - All of this is the firmware's source and the emulator. No real Flipper
      was asked.

123. **A voltage for the ADC.** The header's pins were levels. Six of them
    are inputs of the chip's ADC, and a script's `readAnalog()` or an app's
    `furi_hal_adc_read` had nothing to measure.
    - What was there: ADC1 as one of the plain register blocks that keep what
      is written to them, with a fixed result. A script that read PA4 got 750
      (the block's 1500, halved by the JavaScript module), whatever was held.
      The second `readAnalog()` ended the firmware: `furi_check failed` on
      the screen, and a restart. `LL_ADC_REG_StartConversion` writes ADSTART
      to CR without ADEN, as the chip wants (ADEN, ADDIS and ADSTART are set
      by a 1 and are not cleared by a 0), the block stored that as it came,
      and `furi_hal_adc_read` begins with a check that the ADC is enabled. So
      no app or script could measure twice. The factory's calibration values
      read 0xFFFF.
    - `stm32wb55_adc.c`: one conversion at a time, of the channel in the
      first rank, which is all that `furi_hal_adc` asks for. ADCAL ends at
      once, ADEN gives ADRDY, ADSTART puts the result in DR with EOC, and
      reading DR takes EOC. The result is the input over the reference in
      CFGR's resolution, and with oversampling that many equal samples added
      and shifted, which comes out as the same number. The reference is
      VREFBUF's, 2.048 V or 2.5 V by its range bit (3.3 V with VREFBUF off).
      A conversion takes no time; on a Flipper it is 260 us with the
      firmware's 64 samples.
    - Three inputs are inside the chip: VREFINT (1.212 V), the temperature
      sensor (0.76 V at 30 degrees and 2.5 mV for a degree, the chip at 25)
      and a third of VBAT (3.3 V). The flash model writes the calibration
      values that belong to those numbers into the system memory
      (VREFINT_CAL 1379, TS_CAL1 1038, TS_CAL2 1379), so the firmware's own
      macros give 2048 or 2500 mV for the reference, 25 degrees and 3300 mV.
      The battery's voltage in the firmware's Power screen is the fuel
      gauge's, not this.
    - The machine has a voltage for each pin of the header: the firmware's
      own drive first (a push-pull output, or an open-drain one that pulls
      low), then what is held, then the pin's pull-up; else 0 V. `gpio PA7
      1.25V` (or `1250mV`) holds a voltage, 0 to 3.3 V; `high` and `low` are
      3.3 V and 0 V. To a pin that the firmware reads as an input a voltage
      is a level, high from 1.65 V up. The property `header-adc` has the
      ADC's state, the count of conversions and the last one (channel,
      result, microvolts), which `gpio`, `status`, `status --json` (`"adc"`)
      and the window's GPIO menu show; a pin's menu in the window has Hold it
      at a voltage… for the six.
    - The numbers, each worked out from the voltage before the emulator was
      asked: 1.25 V is 2500 at 2.048 V and 2048 at 2.5 V; 2 V is 4000 and
      3276; 0.1 V is 200 and 163 (which the firmware turns into 99 mV); 3.3 V
      is 4095 in both. VREFINT is 2424 and 1985, the temperature sensor 1495
      and 1224, the battery input 2200 and 1802. All came out so the first
      time, in the check's app (`scripts/adc_check_app`, which goes by the
      firmware's `gpio_pins` table, so the machine's map from channel to pin
      is checked against the firmware's: channels 12, 11, 9, 4, 2, 1 for
      pins 2, 3, 4, 7, 15, 16).
    - The JavaScript module wants `require("event_loop")` before
      `require("gpio")`, or the script ends with `"gpio" module load fail`.
      It takes the ADC when it is loaded and gives it back when the script
      ends, so the launcher has the ADC as on for as long as a script with
      the module runs; the check looks at that while the script waits.
    - `gpio_check.py`: 93 checks on the official 1.5.1 (21 more: one at the start,
      13 with held voltages, the firmware's JavaScript and its `gpio read`,
      7 with the app), 70 on its dev build, Momentum, Unleashed and
      RogueMaster. `gui_check.py`: 57, with a voltage held from a pin's
      menu.
    - The model's choices, where a Flipper would differ: no noise and no
      offset; a pin that nothing holds is 0 V, not what it has picked up; the
      chip is at 25 degrees; a level's threshold is one voltage and not a
      band. A sequence of conversions, continuous mode, DMA, the interrupt
      and a hardware trigger are not there (said once under `-d unimp`). No
      real Flipper's ADC was asked for any of these numbers.

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
- Idle, official 1.5.1 on its desktop, of one host core (an i9-9980HK): 5 % with no
  USB cable (STOP2 between some 50 ticks and 24 thread switches a second), 24 % with
  one (no deep sleep on USB, as on hardware: 1000 ticks a second), 7 % once the
  firmware has halted (entries 93 and 94).
- OTA (`run --update`): Unleashed → official 1.5.1 in 68 s, including writing the
  flash and unpacking 1074 resource files to the SD. Momentum's 11 MB resource
  bundle takes longer.
- `update` on a device that runs, official 1.5.1's package (2.5 MB without the radio
  stack): 84 s, 80 of them in the updater, where the firmware starts four times. The
  card is out of its slot, written and in again within 4 s. Momentum's package
  (12 MB): 222 s, 218 of them in the updater. Through the CLI (`update --cli`,
  22 KB/s, 16 on Momentum): 218 s and 1016 s. The updater's error with the radio
  stage kept: reported 4 s after `update install`.
- The enclave check through the firmware (`enclave_check.py -f`): four boots and 22
  checks in 40 to 48 s, on each of the three firmwares.
- USB: 33 ms from D+ pull-up to enumerated: 25 ms of attach debounce, suspend and
  reset timing (as a PC takes), ~8 ms of control transfers. The CLI banner shows within
  a second of opening the pty (QEMU checks a pty for a reader once a second).
