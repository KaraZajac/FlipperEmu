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
- The enclave check through the firmware (`enclave_check.py -f`): four boots and 22
  checks in 40 to 48 s, on each of the three firmwares.
- USB: 33 ms from D+ pull-up to enumerated: 25 ms of attach debounce, suspend and
  reset timing (as a PC takes), ~8 ms of control transfers. The CLI banner shows within
  a second of opening the pty (QEMU checks a pty for a reader once a second).
