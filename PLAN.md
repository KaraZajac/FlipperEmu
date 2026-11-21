# Plan

Goal: try Flipper firmware (official, Momentum, Unleashed, your own changes)
without flashing the device, with a debugger-first workflow. QEMU-based by design (see
RESEARCH.md §5).

## M1: boots real firmware ☑ (2026-11-07)
- ☑ STM32WB55 SoC: RCC/clock tree, PWR, FLASH (+OB, OTP, UID), HSEM, IPCC + CPU2 (FUS,
  BLE stack), EXTI, SYSCFG (+MEMRMP, VREFBUF), RTC (backup domain), LPTIM×2, SPI×2, I2C×2,
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
      CDC event queue (`furi_check` in USB_LP; BRINGUP_LOG 36). Since 2026-11-10 the
      host waits for the firmware to go idle rather than for the clock: file
      transfers over the CLI are up to twice as fast (BRINGUP_LOG 74)
- ☑ A minimal BLE stack on CPU2, so qFlipper's updates (radio stage) and the BT service work.
      In five parts:
    - ☑ The stack starts (2026-11-13): CPU2 runs ST's BLE Light 1.20.0, the stack of the
          five firmwares' update packages, as a Flipper from the factory does; the firmware
          starts it, its GATT services are in the model's database, Settings > Bluetooth
          switches advertising on and off, `status` has a `bluetooth` row, a snapshot keeps
          it; `run --radio-stack none` is a FUS with no stack. An update package whose
          stack is the device's keeps its radio stage, which the updater finds up to
          date. `scripts/ble_check.py`: 10 on the five (BRINGUP_LOG 138)
    - ☑ A central on the host (2026-11-14): a link (`devices/NAME/ble.sock`) through which
          a program on the computer sees the advertising, connects, pairs with the PIN on
          the Flipper's screen (or a numeric comparison, or Just Works, as both sides' IO
          capabilities have it), and reads, writes and subscribes to characteristics; the
          serial service carries the phone app's RPC session. `./flipperemu ble`,
          `scripts/fe_ble.py`; `scripts/ble_check.py`: 21 on the five (BRINGUP_LOG 140)
    - ☑ Bonds (2026-11-14): the stack's security database in CPU1's RAM (`C2_CONFIG`'s
          address), which the firmware saves to `/int/.bt.keys`: a central that has paired
          comes back without a PIN (`encrypt`), after a reboot too, with its CCCDs as it
          left them; one that has lost its side pairs anew (bond lost, rebond); Unpair All
          Devices forgets them. `scripts/ble_check.py`: 26 on the five (BRINGUP_LOG 141)
    - ☑ The radio update path (2026-11-14): the switches between the stack and the FUS
          with the restarts of the whole MCU that go with them, and the FUS deleting a
          stack and installing one from the image that the updater wrote into the
          flash (its version from the image's footers; the secure area's start moved
          with it), so a package's radio stage of another version, or for a device with
          none, is installed as on a Flipper. CPU2 keeps what its FUS installed across
          runs (`devices/NAME/radio.state`). A stack of another kind than BLE Light is
          still left out (the model is BLE Light). `scripts/update_check.py`: 35,
          `scripts/ble_check.py`: 27 (BRINGUP_LOG 142)
    - ☑ The window (2026-11-14): Bluetooth on the bench. A tile with what the stack does
          (advertising, a connection, the bonds, the FUS at work), and a phone: Connect
          pairs with the PIN that the Flipper shows (typed into a dialog), or comes back
          with its bond (`./flipperemu ble`'s: the same phone); the Bluetooth menu pings
          over RPC, asks the device info, forgets the bond, and opens Settings >
          Bluetooth. `scripts/gui_check.py` 76 (77 with an SPI script),
          `scripts/ble_check.py` 28 (BRINGUP_LOG 143)
- ☐ Custom firmware with another radio stack, as MoonOS (Kara, 2026-11-14): what a
      firmware built for BLE Full extended needs of the emulator
    - ☑ Reserved RAM (2026-11-14): CPU2's parts of SRAM2a and SRAM2b, from the option
          bytes (SBRSA, SNBRSA) at reset; CPU1 gets a bus error there (a write's
          imprecise, QEMU patch 0014; a read's precise), and a fault it cannot take is a
          Lockup that stops the processor (QEMU patch 0015) instead of ending QEMU;
          `status` and `wait` say so; `scripts/ble_check.py` 30 (BRINGUP_LOG 148)
    - ☑ BLE Full and Full extended (2026-11-14): types 1 and 6, installed by the FUS from
          a package's radio stage as BLE Light is (ST's 1.20.0 images, told apart by
          their footers), with their flash and SRAM2; `run --radio-stack full|full-ext`
          (BRINGUP_LOG 149)
    - ☑ The proof (2026-11-14): old MoonOS builds with Full extended fail as on the
          devices: attempt 2's updater loops deleting a stack that is gone; attempt 3's
          last package has the FUS install Full extended, and the firmware's start locks
          CPU1 up in CPU2's SRAM2a; `scripts/update_check.py` 37 does it (BRINGUP_LOG
          150)
    - ☑ The board's chips over a reset of the MCU (2026-11-14): the LCD keeps its
          picture (its RST pulled up), the LEDs, the charger and the radios what they
          had, until the firmware's start resets each; a power-on and a load reset them.
          The FUS says an operation is done before its restart, so the updater's "Core 2
          busy" is the last screen of a Flipper that locks up then, as on MoonOS's
          devices (BRINGUP_LOG 151)
    - ☑ CPU2 hard-faults when CPU1 gives it a buffer in CPU2's part of SRAM2
          (2026-11-14; ST's 1.20.0 does): the transport layer's buffers at
          SHCI_C2_BLE_Init and the NVM buffer at SHCI_C2_Config, its record at
          SRAM2A_BASE; official 1.5.1 waits 33 s for its answer, crashes with that
          record in its report, and starts again; `scripts/ble_check.py` moves the NVM
          buffer there with gdb (BRINGUP_LOG 152)
    - ☑ LE Enhanced Connection Complete (2026-11-15): on BLE Full extended 1.17.0 or
          later with EXT_ADV in the options of SHCI_C2_BLE_Init, in the place of LE
          Connection Complete (ST's release notes). Awaits a firmware that brings
          Bluetooth up there to be seen heard: Moon-Shell's handler is written and
          not built yet; `scripts/ble_check.py` has the check for then (BRINGUP_LOG
          160)
    - ☐ What the Full stacks have more besides: the central role, a GATT client,
          advertising sets, L2CAP CoC. No firmware here calls them (Moon-Shell's
          commands are the stock firmware's): nothing to check a model against
    - ◐ XIP app loaders: what one does is checked (2026-11-15): code programmed
          into the free flash while the firmware runs, run there, replaced and run
          again; `scripts/xip_check.py` 8 (BRINGUP_LOG 161). A loader itself
          (Moon-Shell's .rock apps) when one is built
- ☑ The second CDC port of the dual-CDC mode (2026-11-15): the built-in host
      bridges a second CDC ACM function to `devices/NAME/usb2.tty`; the USB-UART
      bridge on channel 1 carries bytes both ways with the CLI on the first port
      (official 1.1.2). The firmwares from 1.3 on undo the dual mode themselves.
      `scripts/usb_dual_check.py` 7 / 3 (BRINGUP_LOG 162)
- ☐ Stretch: mass storage

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
      The radio stage was blanked, because a FUS-only CPU2 could not install a stack;
      since 2026-11-13 a stage whose stack is the device's stays in (up to date), and
      since 2026-11-14 CPU2's FUS installs a BLE Light stack of any version from it.
- ◐ XIP app loaders (custom firmwares that program FAP code into internal flash and
      execute it there): the write and the run in place ☑ (2026-11-15,
      `scripts/xip_check.py`); a loader's own format when a firmware has one
- ◐ Firmware matrix: official 1.5.1, the official dev build 3060cee9 (a debug build;
      BRINGUP_LOG 87), Momentum mntm-012, Unleashed unlshd-094 and RogueMaster
      RM1024-1347 (2026-11-10, BRINGUP_LOG 86) pass `scripts/smoke_test.py` (now
      including a warm reboot) and the files, app, window, enclave and update checks,
      and so does the release candidate 1.5.1-rc; run them on a schedule
  - ◐ Older official releases (2026-11-10, BRINGUP_LOG 92; 2026-11-11, 96). The
        smoke test: 1.4.3 passes all 28 steps; 1.3.4, 1.1.2 and 0.99.1 pass 23, the
        five NFC steps skipped (no `nfc` shell before 1.4). The files (55), app (26)
        and window (36) checks: 1.4.3 and 1.0.1 pass all of them. Not run on older
        releases: the enclave, update, ufbt and gdb checks
  - ☐ 1.0.1 and 0.105.0 fail one step: `ikey read` finds a Metakom key in 8 to 10
        tries of 15 (1.5.1: 15 of 15, with the same code in the firmware). Every
        edge reaches the firmware's handler once and with its exact time; deep sleep,
        late delivery and a starved reading thread are ruled out (BRINGUP_LOG 92).
        Next: what the decoder is fed, read out under gdb. 1.5.1 fails it too while
        other programs keep the computer busy (a load average of about 20 on 16 cores,
        2026-11-13): 0 of 10, with this push's emulator and with the one before it,
        and some reads give another key (BRINGUP_LOG 136)

## M4: senses ◐
- ☑ Speaker: TIM16 CH1 PWM → QEMU audio (desktop driver or `--audio wav:FILE`); tones
      verified by FFT (440 Hz, 1 kHz, notes)
- ☑ LP5562 engine interpreter (2026-11-10): the three program engines on the chip's
      32768 Hz clock (ramp/wait, set PWM, go to start, branch, end, trigger), so the
      LED blinks and the backlight fades; `status` has a `lights` row
- ☑ Battery/USB power (2026-11-10): `./flipperemu battery`, `usb plug|unplug|charger`
      and the window; SYSDWN under 3.15 V, so a flat battery switches the Flipper off
- ☑ STOP2 deep sleep (CPU2 performs the SHCI clock switch); now the default, 1–2 % host CPU idle
  - ☐ Today an idle 1.5.1 takes 5 % of a host core off USB (it draws its desktop) and a
        quarter on USB: 1000 ticks a second, 190 µs of host CPU each. Over a third of
        the emulated CPU's waking time there is QEMU's slow path for pages that hold an
        MPU region smaller than themselves: the firmware's 32-byte stack guards put the
        whole main stack and the idle task's stack in such pages, so every stack access
        of every interrupt handler takes it. A cache for parts of a page in QEMU's TLB
        would make every handler faster, which matters where microseconds do
        (BRINGUP_LOG 94). Looked at again on 2026-11-12, and not changed: for an idle
        Flipper that cache is worth 5 or 6 of the 24 points at most (19 % with the MPU
        switched off), and there is no shortcut to it. An entry that is kept for such
        a page must not carry QEMU's invalid flag, since `tlb_flush_page()` and the
        dirty tracking pass over entries that do: it needs a state of its own, which
        the slow path and every probe have to know (BRINGUP_LOG 129)
  - ☑ Explained, and nothing to change (2026-11-12): a CLI command that waits takes a
        whole host core (a script in `delay()`, the firmware's `log` command with
        nothing to print). It is the firmware's own. The shell waits for a command's
        thread in `furi_thread_join()`, which looks every 2 ticks, and FreeRTOS's idle
        task goes to sleep only when nothing is due for 4. So the idle task never
        reaches its WFI while a command runs: a real Flipper's CPU stays awake then as
        well (by the official source; not measured on one), and an emulated CPU that
        is awake is a host core (BRINGUP_LOG 129)
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
- ☑ More key encoders (2026-11-15): CAME, Nice FLO, GateTX, Holtek HT12X and
      Linear keys beside Princeton's, each as the firmware's own encoder sends it
      (`subghz came KEY`, a .sub key file); the firmware's `subghz rx` reads each
      back; `scripts/subghz_check.py` 12 (BRINGUP_LOG 167)
- ☑ TX-side pacing against DMA refill lag (2026-11-15): TIM2 waits at each half of
      the transmitter's ring until the firmware's DMA interrupt handler is done with
      it (pending or running in the NVIC), the update's time kept; RFID emulation's
      buffers with it. 3000 pulses come out to the microsecond with three times more
      load than cores; what remains there is the firmware's own (its file reader
      falls behind and it stretches the signal). Found on the way: a second
      transmission's first pulse was a millisecond too long (the capture kept a
      level that was not the pin's). BRINGUP_LOG 171
  - ☐ A Send that is held for more than about 8 s does not end when OK is let go, if
        the charging LED is on (official 1.5.1, in the emulator): the radio stays in
        `tx`. It is the firmware's own. The Send scene asks for a blink every 100 ms, a
        blink takes the notification service 110 ms while another light is on, the
        service's queue of 8 fills, and the timer task, which has a held key's repeats
        to publish, waits at that queue for good: the key's release is never
        published. With the cable out (`usb unplug`) the same hold ends at the
        release. Not known: whether a real Flipper does it (BRINGUP_LOG 133)
- ◐ NFC: the ST25R3916 frame by frame (FIFO, IRQs, timed TX/RX, parity and CRC
      handling), TIM17 as a real timer. Virtual ISO14443-3A cards from `.nfc` files:
      NTAG21x / Ultralight / Ultralight EV1 ☑ (app Read with NDEF, `mfu info`, `dump`);
      MIFARE Classic Mini/1K/4K ☑ (Crypto1 from the paper, nested auth, access bits;
      the app's dictionary attack, `dump` from the key cache).
      Card emulation ☑ (2026-11-08): a virtual reader in front of the emulating Flipper
      (`nfc --reader scan|ntag`; target mode, passive-target memory, I_wu_a); an NTAG
      reads back page for page → `devices/NAME/nfc-tx/*.nfc`. Classic emulation's
      transparent-mode answers are not decoded yet.
      MIFARE Ultralight C ☑ (2026-11-15): AUTHENTICATE with two-key triple DES (DES
      in the card model), AUTH0/AUTH1; the app's dictionary finds its key.
      ISO14443-4A and MIFARE DESFire ☑ (2026-11-15): RATS/ATS, the blocks, DESFire's
      native and wrapped commands from a dump's tables. FeliCa Lite-S ☑ and
      ISO15693-3 / SLIX ☑ (2026-11-15): the chip model's technologies whose frames
      are bytes (FeliCa, ISO15693's stream coding), the cards' commands. Each dumps
      to its own file again (BRINGUP_LOG 155).
      ISO14443-3B / -4B and ST25TB ☑ (2026-11-15): REQB/ATQB, ATTRIB, HLTB, the
      ISO14443-4 blocks shared with Type A; ST25TB's Initiate/Select, blocks, one-time
      bits, counters and lock bits (BRINGUP_LOG 156).
      FeliCa Standard ☑ (2026-11-15): systems, areas, services and their public blocks
      from a dump of either file format; Search Service Code, Request Service, reading
      and writing without encryption (BRINGUP_LOG 157). MIFARE Plus ☑ (2026-11-15):
      GetVersion (EV1, EV2) or the ATS (S, X, SE), which is all the firmware reads of
      one. ISO 7816-4 ☑ (2026-11-15): applications, files, records, data objects;
      Type 4 tags and EMV cards from the files of the firmwares that read them, read
      and saved by their NFC apps (BRINGUP_LOG 158). `scripts/nfc_check.py` 25 to
      27 on five.
      FeliCa Lite-S's authentication ☑ (2026-11-15): the MAC both ways with the
      card key of a keyed dump; `dump -k` reads the blocks kept for it (BRINGUP_LOG
      159).
      MIFARE Classic emulation ☑ (2026-11-15): the firmware's answers in the chip's
      transparent mode decoded from the edges it puts on MOSI (TIM2's DMA paced
      at its ring's end marker, so an emulated CPU is not overtaken); a reader as
      a program on the NFC link (`reader on|activate|send`), and one for MIFARE
      Classic with Crypto1 from the reader's side: `nfc --reader classic
      CARD.nfc` reads every block and key of an emulated Mini, 1K and 4K
      (BRINGUP_LOG 169).
      More readers ☑ (2026-11-15): `nfc --reader felica` (the chip's polling from
      the passive-target memory's FeliCa part, then a Lite-S's blocks without its
      key), `--reader iso-dep` (RATS and the ATS), `--reader t4t` (a Type 4 tag's
      NDEF message through ISO14443-4's blocks; the NFC apps of the dev build and
      the forks emulate one, 2026-11-15, BRINGUP_LOG 177; a tag played in Python
      for the official 1.5.1). A reader starts again (field off and on) when the
      Flipper stops answering, which a busy computer brings about.
      `scripts/nfc_emulate_check.py` 15 / 16 on five (BRINGUP_LOG 170).
      NTAG 413/424 DNA ☑ (2026-11-15): the card that the dev build and the three
      forks know by its ISO-wrapped GetVersion; `nfc ntag4xx UID`; read and saved
      by their NFC apps (nfc_check 25, 27 on the dev build, 28 on the forks).
      Readers with keys ☑ (2026-11-15): `nfc --reader ulc CARD.nfc` (MIFARE
      Ultralight C: AUTHENTICATE with two-key triple DES both ways, then its 48
      pages) and `nfc --reader felica CARD.nfc` (FeliCa Lite-S: a challenge, the
      card's MAC checked, the reader's MAC written, 27 of 28 blocks); DES in
      Python for the reader (the card model's tables); each also with a wrong
      key. nfc_emulate_check 21 / 22 on five (BRINGUP_LOG 178).
      A record of the air ☑ (2026-11-15): `nfc record FILE`, `nfc watch`: every
      frame and its answer, both ways, all four technologies, with the parity
      bits where they are not odd; nfc_emulate_check 15 on five (BRINGUP_LOG 172).
      A reader for ISO15693 ☑ (2026-11-15): `nfc --reader iso15693` reads the
      SLIX that the firmware emulates. The firmware receives that card itself
      (a DMAMUX request generator on the chip's pin, TIM16's compare requests,
      the DMA sampling GPIOA) and answers it edge by edge without leaving the
      chip's transparent mode: the chip model plays a reader's frame as the
      samples, eight at a time as the firmware takes them, and decodes the
      answer off the edges. No TIM16 for it (it stays the speaker's register
      file). Transparent mode is in a saved state now. nfc_emulate_check 24 /
      25 on five; firmware bug FW-21 found with it (BRINGUP_LOG 179).
      MIFARE Plus in security level 3 ☑ (2026-11-15): a card whose file has its
      AES keys answers the first authentication, the reads with a MAC both ways
      (plain and encrypted), the configuration blocks and the signature; `nfc
      mfplus-sl3 UID`. Unleashed's and RogueMaster's NFC apps read it with their
      dictionary and save its file, `??` where a key of no dictionary keeps it.
      nfc_check 25 / 27 / 28 / 29 / 29 (BRINGUP_LOG 180).
      A reader for the MIFARE Plus that Unleashed and RogueMaster emulate ☑
      (2026-11-15): `nfc --reader mfplus CARD.nfc`, AES-128, CBC and CMAC in
      Python; reads with the file's keys, a sector without its keys left
      unread, a block written both ways. Firmware bug FW-22 found with it (the
      emulated card gives and takes every block in a session of any key).
      nfc_emulate_check 25 / 26 / 26 / 29 / 29 (BRINGUP_LOG 181).
      Writes to a Plus that is held to the Flipper ☑ (2026-11-15): `A1`, `A3`,
      checked by the forks' "Write to Initial Card" (32 sessions, 128 blocks)
      and a read of the card after it. nfc_check 25 / 27 / 28 / 30 / 30
      (BRINGUP_LOG 182).
      Next: MIFARE DESFire's authentication (no firmware here asks for it:
      first what would)
- ☑ 125 kHz RFID read: COMP1 → TIM2 TI4 capture, a virtual tag in the reader's field
      (ASK/FSK/PSK, field noise), encoders ported from the firmware (`fe_rfid.py`, checked
      cycle for cycle against its emulation by `rfid_check.py`); app Read and `rfid read`
- ☑ 125 kHz RFID emulate: a virtual reader clocks TIM2 (ETR), records CH3's PWM load →
      `devices/NAME/rfid-tx/`; captures replay as tags (any protocol the firmware has)
- ☑ T5577 writes (2026-11-08): a virtual T5577 (`rfid t5577`) decodes the firmware's
      writes from the gaps in TIM1's field and answers as block 0 says (Manchester,
      bi-phase, diphase, direct, FSK, PSK; password and lock bits); all 24 protocols
      write and verify (`rfid_check.py`; Radio Key through the app)
- ☑ Field detection (2026-11-08): RFID (TIM2 counting the virtual reader's carrier,
      TIM1 + DMA every 100 ms) and NFC (efd_o; `nfc --field on`); Momentum's NFC/RFID
      detector shows both. Timer update DMA requests are now pulses (BRINGUP_LOG 39)
- ☑ iButton read (2026-11-08): a 1-Wire slave on PB14 for Dallas keys (DS1990A,
      DS1992, DS1971, DS1996 at overdrive, any family) that lets go once the
      master has sampled (IDR tap); Cyfral and Metakom waveforms into COMP1, the
      ISR's CYCCNT read stamped with the edge's time; `ikey read` and the app read
      all of them (`ibutton_check.py`)
- ☑ iButton write (2026-11-08): virtual RW1990.1/.2, TM2004 and TM01x blanks take
      the firmware's writes and verify (`ibutton rw1990`; the slave follows the
      master's own drive through a GPIO overlay)
- ☑ iButton emulation (2026-11-08): a virtual 1-Wire reader paced by the
      firmware's busy-polling slave reads the ROM and all memory (DS1996: 8 KB,
      resumed after host-stall dropouts, overlap-checked); Cyfral/Metakom captured
      at the TIM1 ISR's own writes, decoded and replayed (`ibutton --reader`,
      `ibutton_check.py`; BRINGUP_LOG 45-49)
- ☑ 1-Wire timing (2026-11-10): `DWT->CYCCNT` is read without QEMU's big lock
      (`qemu/patches/0005`) and a slot's low is timed to the master's last read of
      it. `ikey read` of a DS1990A: 40 of 40 on 0.105.0 and on 1.0.1, where one
      read in 13 and one in 40 had printed `Dallas FFFFFFFFFFFFFFFF` (BRINGUP_LOG 92)
  - ☐ A Cyfral emulation's capture cut short (138 levels, once in 56 runs); the
        capture's log line now says how it ended

## M7: real radios ☐
- ☑ Sub-GHz receive from an SDR (2026-11-09): the CC1101's link socket announces the
      receive set-up and plays a timeline for GDO0; RSSI from the band's power, bin by
      bin. `scripts/fe_sdr.py` (`./flipperemu subghz --sdr`): rtl_tcp, HackRF
      (libhackrf), recordings, stdin; follows the firmware's tuning; OOK at the chip's
      decision boundary, 2-FSK, squelch. Real rtl_433 captures decode in the
      firmware (`sdr_check.py`; BRINGUP_LOG 50-53; docs/RADIO_LINK.md)
- ☐ Sub-GHz transmit through a TX-capable SDR (HackRF): the link already carries each
      transmission; modulate it (OOK/2-FSK) at the tuned frequency. Opt-in only
- ☑ NFC: a card from a program (2026-11-09): the ST25R3916's link sends each reader
      frame (data bits and parity) and takes the card's answer, the machine stopped
      meanwhile; `scripts/fe_nfc_link.py` (`./flipperemu nfc --link`): ISO14443-3A,
      ISO-DEP, an `iso-dep` card, `script:FILE.py` (BRINGUP_LOG 54)
- ☑ NFC: real cards through a Proxmark3 (2026-11-09): `nfc --link pm3` relays every
      Type A frame over the Proxmark3's own USB protocol, exactly (NO_PARITY: MIFARE
      Classic too) with firmware from v4.23346, with odd parity before; Proxmark3 JSON
      dumps as cards and .nfc files. Checked against `fe_pm3.py sim`
      (`nfc_link_check.py`; BRINGUP_LOG 55); not yet against a real Proxmark3
- ☑ NFC, the other way round (2026-11-15): a program on the link as a reader in
      front of an emulating Flipper (`reader on`, `reader activate`, `reader send`;
      docs/RADIO_LINK.md), ISO14443A (BRINGUP_LOG 169)
- ☐ NFC: a PN532/PN533 (ACR122U over PC/SC) relay; a real reader in front of an
      emulating Flipper (a Proxmark3 or PN532 as the card, relayed to the link's
      reader)
- ☐ 125 kHz RFID and IR through real front ends (a Proxmark3's LF, an IR transceiver),
      and iButton through a USB 1-Wire master (DS9490R)

## M8: a Flipper on the desktop ◐
- ☑ Secure enclave (2026-11-10): the FUS's customer key storage (the four SHCI key
      commands; `devices/NAME/enclave.keys`) and AES1/AES2 (ECB, CBC, CTR, GCM, key
      registers the FUS closes to CPU1). Factory keys of FlipperEmu's own, public; the
      firmware's known-answer check answered from its own source. The self-test passes,
      and `crypto` in the CLI, stored keys, the device's own key (U2F) and Sub-GHz
      keystores of one's own work on official, Momentum and Unleashed
      (`enclave_check.py`; `./flipperemu enclave`; BRINGUP_LOG 56-63). Content encrypted
      with a real Flipper's keys (the stock KeeLoq keystore, the U2F certificate's key)
      stays unreadable
- ☑ Sub-GHz keystores under the emulator's key (2026-11-11): the manufacturer codes
      of the four keystores (KeeLoq, Nice Flor-S, Alutech AT-4N, CAME Atomo) encrypted
      under the emulator's key 1 (`keystores/overlay`, `scripts/rekey_keystore.py`,
      docs/keystores.md), on every card the launcher builds and again after a firmware
      update. The firmware names a made-up KeeLoq remote for each line with simple or
      normal learning (74) and reads Nice Flor-S remotes on official, dev, Momentum,
      Unleashed and RogueMaster
      (`keystore_check.py`; BRINGUP_LOG 117). Not tried: an Alutech AT-4N or CAME Atomo
      remote. `rekey_keystore.py merge` puts decrypted key lists together and leaves
      out the lines that only add tries (BRINGUP_LOG 118). Since 2026-11-12 the
      overlay has two KeeLoq keystores: `keeloq_mfcodes`, the list that a firmware
      reads, is a list of Unleashed's (126 lines; unlshd-094's own file is that list
      without one line, by the lengths of its encrypted lines), and
      `keeloq_mfcodes_kat` beside it has the 105 lines more of a longer collected
      list (`merge --new-only`). No firmware reads the second file by itself (0 of 74
      made-up remotes of its codes named on the five); as `keeloq_mfcodes_user` each
      reads it (74 of 74). A program of one's own loads it after the first list
      (`subghz_environment_load_keystore`; docs/keystores.md has the way from the
      source, and no program here does it). `keystore_check.py` 45 on the five
      (BRINGUP_LOG 119-121)
    - ☐ `keeloq_mfcodes` is Unleashed's list for every firmware, with Unleashed's
      type numbers. The official firmware has other numbers for magic-serial
      learning and names none of 15 such made-up remotes with it; the second lines
      with its numbers (KGB/Subaru, Magic_2 to Magic_4; no such line exists for
      Magic_1) and a type-1 line for its Kingates Stylo 4K decoder are in
      `keeloq_mfcodes_kat`. A list of each firmware's own would be its package's
      file, and only a real Flipper's key opens those
    - ☐ As a firmware's second list, `keeloq_mfcodes_kat` costs names for remotes
      that no line is for: of 300 with random keys 37 where the first list alone
      gives 23 (official), 36 for 22 (Momentum), 87 for 73 (Unleashed). Some of its
      lines have type numbers that a firmware reads as another kind of learning
- ☑ U2F from a host (2026-11-12): `scripts/fe_u2f.py`, a U2F host in user space on the
      USB/IP client (the HID interface, U2FHID's INIT, PING, WINK and MSG, the raw
      messages VERSION, REGISTER and AUTHENTICATE, the signatures checked on P-256 in
      plain Python), with `register` and `login` at the command line. The firmware's
      token registers a key after OK on the Flipper and logs in with it, refuses
      without OK, counts its logins, and keeps its key and counter on the card under
      the device's own key. `scripts/u2f_check.py`: 39 checks on each of the five
      firmwares, with what is on the card computed again on the host from the
      enclave's keys (BRINGUP_LOG 128)
    - ☐ A browser or libfido2, which reach a token through the kernel (`sudo
      scripts/usbip_attach.py`, the token as a hidraw device): needs root, not tried
    - ☐ A registration that a host which checks the attestation accepts. The stock
      certificate's key is under a real Flipper's key 2, so the firmware signs with
      another number. A certificate and key of the emulator's own would do it (the
      firmware takes a user's `cert_key.u2f`): to be decided
- ☑ The window (2026-11-10): `scripts/fe_gui.py` (PySide6), the default of `run`. The
      LCD in a Flipper-shaped body of its own drawing, with buttons to click or hold, the
      LED, the vibration motor, the speaker's tone and the IR window; a USB plug and a
      microSD card to pull and push back; a bench and menus that hold cards, tags and
      keys to it, point remotes at it, put signals on the air, hold virtual readers to
      it and make real radios its antenna; a log with a line for the CLI. Over a
      front-panel link (`flipper_panel.c`, docs/PANEL_LINK.md): frames, lights, held
      buttons and watched QOM properties. Power as on hardware: the firmware's power-off
      (dialog, `power off` on USB, a flat battery) ends the emulator, and the window
      switches it on again on Back held or USB plugged in, with the battery and what was
      held to it. New commands for it and for scripts: `gui`, `status`, `usb`, `battery`,
      `sound`, `sd eject|insert`, `reset`, `run --unplugged|--battery|--gtk`. Checked
      off-screen by `gui_check.py` on official, Momentum and Unleashed (BRINGUP_LOG 64-69)
- ☐ The window by hand on a desktop session (it has only run on Qt's offscreen
      platform here); a USB DFU device behind the firmware's DFU mode
- ☐ `gui_check.py`, the step that switches it off with Back held and Power Off: it
      failed once on Momentum (2026-11-12, the Flipper still on its desktop after the
      5.6 s), with a round of ten other checks running beside it. It passed alone
      right after, and on Unleashed in that same round. Not known: whether the hold
      was too short for a firmware that was slowed, or the key that follows too early

## M9: a bench for firmware developers ◐
- ☑ The firmware log (2026-11-10): USART1 kept in `devices/NAME/uart.log` whatever
      `--serial` is (and the run before in `uart.log.1`); `./flipperemu log` reads and
      follows it (last lines, a level, a regex, through reboots and restarts); the
      firmware's own log level as the machine property `log-level` (`run --log-level`,
      `log --set-level`); in the window a tab beside the console, with the firmware's
      colours, filters, the level, and a notice when the firmware crashes
- ☑ Files and apps: copy files to and from the card of a running device, install a
      `.fap` and start it, from the launcher and by dropping it on the window
  - ☑ `ls`, `push`, `pull`, `rm`, `mkdir` in the launcher (2026-11-10): through the
        firmware's `storage` CLI when the device runs, in the card's image (mtools)
        when it is stopped; `scripts/files_check.py` (BRINGUP_LOG 75)
  - ☑ `app` (2026-11-10): what a `.fap` is built for, from its manifest; copy it
        over the app of its name, close what runs (with its Back button if it takes no
        request), start it with arguments, say whether it runs and what the firmware
        logged; a question on the Flipper's screen is waited for and then cancelled;
        `scripts/app_check.py` (BRINGUP_LOG 77)
  - ☑ `app` and an app that takes the USB port (2026-11-12): U2F and the USB keyboard
        make another device of the port, Bad USB switches it off, and the loader's
        answer is lost with the CLI's serial port. `app` waited for it, pressed Back
        (which closed the app) and said that the loader had not started it. It asks
        the emulated host what it sees there now and says that, with exit status 0;
        a command that needs the CLI says that an app may have the port off.
        `app_check.py` has the step for U2F and Bad USB: 32 checks on the official
        1.5.1 and Unleashed, 33 on the dev build, 29 on Momentum and RogueMaster,
        whose cards have no Bad USB (BRINGUP_LOG 127)
  - ☑ The window (2026-11-10): a dropped `.fap` is installed and started, F5 does
        it again after a build, an Apps menu; files and folders to the card from the
        microSD menu and by dropping them, also while it is off; the launcher's
        lines come into the console as they are printed
  - ☑ Apps > Start it again when its file changes (2026-11-11): the window looks at
        the `.fap` twice a second and does the F5 itself when a build has written it
        (two looks that find the same, so that a half-written file is not taken)
- ◐ The firmware's own tools against an emulated Flipper, and a page on the
      developer's loop
  - ☑ `./flipperemu ufbt …` and `fbt …` (2026-11-10): the tool as it is, with the
        emulated port named (`FLIP_PORT=`), so that a real Flipper that is plugged in
        is not the one it finds; the port held meanwhile; probe targets and packages
        with a radio stack refused with what does it here; `examples/hello`;
        `docs/DEVELOPING.md`; `scripts/ufbt_check.py` (BRINGUP_LOG 78), which has a
        stand-in `fbt` for what the launcher hands to a tree's own
  - ☐ `fbt` in a firmware's tree, tried: build, `run -f` the `.elf`, `flash_usb`,
        `launch APPSRC=`; ufbt with Momentum's and Unleashed's SDK
  - ☑ `./flipperemu update PKG` (2026-11-10): an update package sent to a running
        device by the launcher itself, file by file through the CLI and then `update
        install`, without the radio stage; waited for on the USB serial port, with the
        updater's stages read from the log, so that its error screen is reported in
        seconds (a log level that hides them is lent one meanwhile; a level that the
        updater leaves changed is said); `ufbt flash_usb` through the wrapper is this
        with the SDK's package;
        `scripts/update_check.py` (BRINGUP_LOG 84)
  - ☐ Momentum's first start after an update ends in a crash report now and then:
        `[CRASH][ISR MemMgmt] NULL pointer dereference`, the Desktop's slideshow view
        drawn before it has a slideshow (9 of 82 updates, eight to eleven emulators at
        a time; none of 16 with the official 1.5.1). The firmware restarts and the
        update is complete, but `update_check.py` has 33 of 34 then. Not known:
        whether a real Flipper does it, how often an emulator that runs alone does,
        and what brings a redraw at that moment (BRINGUP_LOG 120)
  - ☑ A quicker way for a big package onto the card of a running device than the CLI's
        20 KB/s (2026-11-10): `update` takes the card out of its slot, writes the
        package to the card's image and puts the card in again, with the firmware
        waited for both times (Momentum's 12 MB: 4 s, where the CLI took thirteen
        minutes); `update --cli` is the way through the CLI; `push --card` is the
        same move for files (BRINGUP_LOG 85)
  - ☑ The window sends an update (2026-11-11): Device > Update with a package, F6
        for the same package after the next build, a dropped package (Update or
        Install is asked where both can be done); "updating" while the launcher
        waits, with what would wait behind it greyed; Stop waiting ends the wait as
        Ctrl-C does; a crash that its screen said before the update is not told
        again after it (BRINGUP_LOG 95)
  - ☑ Debugging an app: `./flipperemu gdb` on a device that runs. The stub on
        demand, on a socket in the device's folder; the toolchain's gdb; the firmware's
        ELF, found by what the flash holds; the apps' symbols through the SDK's
        `flipperapps.py`, with the firmware's probe flag kept off and breakpoints of
        gdb's where an app starts and where a crash begins; `--serve PORT` for an
        IDE; `scripts/gdb_check.py` (BRINGUP_LOG 79)
  - ☑ The stock apps under gdb, and an official build's symbols with nothing to
        name: `scripts/fetch_firmware.py` gets the build's ELF and unpacks the update
        server's archive of the apps' debug ELFs beside it; `gdb` finds the ELF beside
        the package the device was flashed from, and has the firmware's script look in
        both folders, the stock apps' and what was built (2026-11-10, BRINGUP_LOG 91)
  - ☐ gdb with a firmware tree's ELF and its `.extapps`
  - ☑ gdb from VS Code (2026-11-11): `gdb --vscode` writes a debug configuration
        for the Cortex-Debug extension into `.vscode/launch.json` of the app's folder
        (a file that is there keeps all else, ufbt's comments too), with
        `gdb-ide.init` for the rest; an IDE's gdb that leaves no longer leaves the
        Flipper stopped. Used by a gdb driven over GDB/MI as the extension drives
        one, in the gdb check (30 checks); VS Code itself is ☐ (BRINGUP_LOG 99)
  - ☑ A test of an app as a script (2026-11-11): `./flipperemu wait` until the CLI
        answers, the log has a line (`--log`) or the screen is as in a file
        (`--screen`), alone or after a command of the launcher's (`-- COMMAND`);
        the screen as text (`shot --text`, `scripts/fe_screen.py`: two pixels to a
        character, `?` for what may differ); a failed wait says what was there, and
        a crash, a halt, a pause or a stopped emulator as its reason;
        `examples/hello/test.sh`; `scripts/wait_check.py` (BRINGUP_LOG 97)
  - ☑ `run --detach` (2026-11-11), for a test that does not have to put the
        emulator in the background itself: back when the emulator is up, or with
        what went wrong; a firmware file that is not there, or is none, is one line
        and no traceback, for `run` and `flash` alike (BRINGUP_LOG 98)
- ◐ The GPIO header
  - ☑ `./flipperemu gpio` (2026-11-11): what the firmware has each pin as and its
        level, as a table and as one word; a pin held high or low from outside
        (over a reset too), or let go of; `--wait` for a level; in `status`. The
        machine's property `header`, on the L4x5 GPIO model's outside connections;
        `scripts/gpio_check.py`, with the firmware's `gpio` CLI command on the
        other side (BRINGUP_LOG 100)
  - ☑ The window's pins (2026-11-11): lit by their level, a click on one to hold
        it high or low or let go of it (a jumper wire shows on a held one), a line
        for each under the pointer, and a GPIO menu with all eighteen; the window
        watches the property `header` over the front-panel link, and holds a pin
        with the launcher's `gpio`; `scripts/fe_header.py` reads the property for
        both; six more checks in `gui_check.py` (BRINGUP_LOG 101)
  - ☑ A record of what a pin did (2026-11-11): `gpio record FILE.vcd` until
        `gpio record stop`, every change of a level with its time, written by the
        machine (property `header-record`) where a level can change: the
        firmware's write to a GPIO register, a hold from outside, a reset, and
        the iButton model's side of pin 17; `gpio watch [PIN,PIN] [--for S]`
        prints the changes as they happen, from the file; fifteen more checks in
        `gpio_check.py` (BRINGUP_LOG 102). Times are the emulator's clock, some
        microseconds off for the shortest pulses (measured there)
  - ☑ The record in the window (2026-11-11): GPIO > Record what the pins do…
        (a file dialog, then the launcher's `gpio record`), GPIO > End the record,
        and the file with its count of changes in the status bar, from a watch of
        `header-record`; three more checks in `gui_check.py` (BRINGUP_LOG 103)
  - ☐ The file opened once in GTKWave or PulseView (neither is installed here)
  - ☑ The serial port on pins 15 and 16 (2026-11-11): LPUART1 on a chardev hub, a
        pty or TCP port for the user's program (`run --lpuart`) and a socket for
        the launcher; `./flipperemu uart`, `uart send TEXT [--expect TEXT]`, `uart
        wait TEXT`, `uart term`; `examples/uart/echo.js`; `scripts/uart_check.py`,
        38 checks on the five firmwares. Under it: the clock controller resets a
        peripheral on both edges of its reset bit (an app that opened the port
        after a deep sleep hung), and QEMU patch 0006 (a chardev's log file had a
        byte twice when its backend was busy) (BRINGUP_LOG 104)
  - ☑ The same for pins 13 and 14 (2026-11-11): USART1 on a hub too, what
        `--serial` said and a socket for the launcher; `uart send --port usart`,
        `uart term --port usart`. The firmware logs there with the receiver off,
        so what is sent before an app has the port is lost, as on LPUART1;
        `scripts/uart_check.py` is 54 checks now (BRINGUP_LOG 105)
  - ☑ The pin itself (2026-11-11, QEMU patch 0008): a byte that comes while a
        port's receiver is off is a start bit on its RX pin (pin 14 or 16, low for
        a bit's time at 9600 baud, in the pins' record too). The firmware's
        expansion service takes it as a module's call and answers with its
        heartbeat; `gpio`, `status`, `uart` and the window say that it waits for
        one (`header-serial`: `edge`), and `uart send` what it took the byte as;
        `uart_check.py` is 73 checks (BRINGUP_LOG 108)
  - ☑ The module's side of the expansion protocol as a tool (2026-11-11):
        `expansion` calls the firmware on the port it waits on, names a baud rate,
        keeps the link with heartbeats, and with `--rpc` starts an RPC session and
        has a ping answered; every frame printed with its time; a refused baud
        rate and a port nobody waits on in words; `scripts/fe_expansion.py` for a
        program of one's own; `uart_check.py` is 79 checks (BRINGUP_LOG 109)
  - ☐ More of that module: an RPC client of one's own behind the link (the
        session's stream on a pty or a TCP port), the 5 V commands, a module that
        goes wrong on purpose; a real module on a USB serial adapter as the port
  - ☑ The UART model's reception (2026-11-11, QEMU patch 0007): the request to
        the DMA controller, the IDLE flag, bytes at the baud rate (BRR and the
        PRESC register, the LPUART's own formula), `qemu_chr_fe_accept_input()`
        after a read, registers read a byte wide. The firmware's USB-UART bridge
        passes bytes both ways; `uart send --for` is how long nothing may move;
        `scripts/uart_check.py` is 64 checks (BRINGUP_LOG 106)
  - ☑ What the firmware sends, at the baud rate (2026-11-15, QEMU patch 0017):
        the transmitter's shift register, TXE when TDR is free, TC when a frame
        ends with nothing behind it; 964 bytes at 9600 baud take their second.
        With it a firmware bug shows: a port closed right after a write loses its
        last two bytes (FW-20). `uart_check.py` 80 (BRINGUP_LOG 175)
  - ☑ A baud rate for the wire's other end (2026-11-15, QEMU patch 0018):
        `uart --line-baud N|auto`; with another rate than the firmware's each side
        takes the other's bytes as a UART at the wrong speed samples them (other
        bytes, more or none, framing errors); the same within a few percent.
        `uart_check.py` 86 (BRINGUP_LOG 176)
  - ☐ An overrun in the UART itself: left out on purpose. The firmware reads a
        port by interrupt or DMA, so an overrun is an interrupt that came late,
        and an emulated CPU's lateness is the host's as often as the firmware's;
        a byte waits on the wire for the reader instead
  - ☑ The serial ports by name (2026-11-11): what the firmware has each port as
        (off, or its baud rate and frame, and who takes what comes in: an
        interrupt, the DMA, nobody) in `gpio`, `status`, `uart` and the window's
        GPIO menu and console; the two pins by the port's name; a `uart send` that
        nobody hears says so. Machine property `header-serial`; `uart_check.py`
        is 68 checks, `gpio_check.py` 54, `gui_check.py` 47 (BRINGUP_LOG 107)
  - ☑ I2C devices on the header (2026-11-11): `run --i2c TYPE@ADDRESS` puts one
        of QEMU's I2C devices on I2C3, pins 15 and 16 (a `tmp105` sensor, an
        `at24c-eeprom`, …); `i2c` shows the bus, the devices' values and what the
        firmware called, and sets a value while it runs (a temperature in °C); an
        EEPROM's contents are a file of the device (QEMU patch 0009); the bus is in
        `gpio` and `status` (machine property `header-i2c`); `examples/i2c`;
        `scripts/i2c_check.py` is 35 checks, `gpio_check.py` 55 (BRINGUP_LOG 110)
  - ☑ An app in C on that bus (2026-11-11): `examples/i2c/sensor.c`, built with
        ufbt for the official firmware (its JS has no `i2c` module), with its own
        `test.sh`; `run --i2c help` lists the types, and one that QEMU has not is
        refused before anything is flashed; `scripts/i2c_check.py` is 37 checks
        (BRINGUP_LOG 111)
  - ☑ That bus in the window (2026-11-11): the GPIO menu has the bus, each
        device with its values and a sensor's temperature to set; the console
        says what the firmware's calls came to (one line for a scan, none for
        each read of an app that reads every second); `i2c --json` for a
        program; `gui_check.py` is 50 checks, `i2c_check.py` 38 (BRINGUP_LOG 112)
  - ☑ A record of that bus's transfers (2026-11-11): `i2c record FILE` writes
        a line of text for each START (its time, the address, `w` or `r`, the
        bytes), `i2c watch [ADDRESS]` prints them as they happen; a call that
        nobody answers while a device is at twice the address gets a word on
        the shift (0x24, and a sensor at 0x48: pass `0x48 << 1`), in `i2c` and
        in the watch. Machine property `header-i2c-record`. `gpio watch` and
        `i2c watch` end on a script's `kill` as on Ctrl+C. `scripts/i2c_check.py`
        is 63 checks (60 on the official 1.5.1, 55 on its dev build),
        `gpio_check.py` 56 (BRINGUP_LOG 114)
  - ☑ That record in the window (2026-11-11): GPIO > Record what goes over the
        I2C bus… asks for the file and is `i2c record`; the status bar counts
        the addresses called, GPIO > End the record of the I2C bus ends it, and
        a record or a watch that a shell began shows there too. The word on
        the shift under the console's line for the firmware's calls and under
        the bus's line in the GPIO menu. `gui_check.py` is 55 checks
        (BRINGUP_LOG 115)
  - ☑ A repeated START and a refused byte from a firmware (2026-11-11): the
        record's `+`, `!` and `...` are written from a firmware's transfers,
        with QEMU's `i2c-echo` at 0x42 as the device that refuses a fourth
        byte. An app of the check's own (`scripts/i2c_check_app`,
        `furi_hal_i2c_tx_ext` and `rx_ext`; the official 1.5.1) and a script
        (Momentum, Unleashed, RogueMaster). It found that a START with no STOP
        for another address went to the device that had the bus: fixed in the
        I2C model. `scripts/i2c_check.py` is 66 checks on the official 1.5.1,
        64 on those three, 55 on the dev build (BRINGUP_LOG 116)
  - ☐ More on that bus: the C app on the other firmwares, each with its own
        SDK
  - ☑ A record of what goes over the SPI bus (2026-11-15): `spi record FILE`,
        `spi watch [PIN]`, a line for each run of frames under one chip select
        with the bytes out and in; `scripts/spi_check.py` 49 on the official
        1.5.1 (BRINGUP_LOG 163)
  - ☐ More devices for the SPI bus: a radio module (the external CC1101 that
        Momentum, Unleashed and RogueMaster have a setting for, an NRF24
        board), a display
  - ☐ More on the SPI bus: an example app in C beside the script, and the
        check's app on the other firmwares, each with its own SDK
  - ☑ A PWM's edges in the pins' record (2026-11-15): written from the PWM's
        period and its high part, to the nanosecond, up to 100 kHz; a change of
        the timer's clock in the RCC is a change of the PWM; `gpio watch` has a
        line for what a fast pin does, not one for each edge. `gpio_check.py`
        112, 83 without the SDK (BRINGUP_LOG 174)
  - ☐ Other timers' outputs on the header's pins than the firmware's two
  - ☑ SPI devices on the header (2026-11-12): `run --spi TYPE@PIN` puts one of
        QEMU's SPI devices on SPI1 as the header has it, MOSI on pin 2, MISO on
        pin 3 and SCK on pin 5, with a pin of the header as its chip select (4,
        6, 7, 10, 12, 15 or 16; 4 is the one of the firmware's own handle). The
        types are QEMU's serial flashes (`flash` is a W25Q32), each with its
        contents in a file of the device, `spi-PIN.bin`. A device hears a byte
        while pin 5 is SPI1's clock (the radios are on the same SPI1, on pins
        inside) and its own pin is low, as the firmware's output or held from
        outside. `spi` and `spi --json` say what is there, who is selected, how
        many bytes the firmware clocked with a device selected and with none,
        and how the last one was clocked; `gpio` and `status` have the bus's
        line. In the machine: a device on SPI1's bus with a bus of its own,
        `spi-header`, and the property `header-spi`. QEMU patch 0010 gives
        Winbond's flashes the read-ID commands 90h and ABh, which the model had
        for SST's alone (a QEMU tree that was set up before re-runs
        `setup_qemu.sh`). `examples/spi/flash.js` for the three firmwares whose
        JavaScript has an `spi` module, and an app of the check's own
        (`scripts/spi_check_app`, the official 1.5.1) for what the firmware's
        handle never sets: another chip select, mode 3, the least significant
        bit first, frames of 16 bits, a pin of the three that is not the bus's,
        nobody selected. `scripts/spi_check.py`: 45 checks on the official
        1.5.1, 30 on Momentum, Unleashed and RogueMaster, 25 on the dev build;
        `gpio_check.py` 111 and 82; `setup_check.py` 30. No real Flipper with a
        flash on its header was asked (BRINGUP_LOG 125)
  - ☑ That bus in the window (2026-11-12): the GPIO menu has the SPI bus beneath
        the I2C bus, in `spi`'s words, and each device with the pin of its chip
        select, whether it is selected, and where a flash keeps its contents
        (asked from the launcher, `spi --json`). The pointer over a pin says
        whose chip select it is, and MOSI, MISO or SCK while the firmware has
        the bus. The console says what the firmware clocked there: one line for
        a script's run of transfers, none for a chip select alone. Nothing is
        set there: a chip select is held as any pin is. `gui_check.py` is 64
        checks on Momentum, Unleashed and RogueMaster, and 63 on the two
        official firmwares, whose JavaScript has no `spi` (BRINGUP_LOG 126)
  - ☑ PWM on a pin (2026-11-12): the two outputs of the firmware's
        `furi_hal_pwm`, pin 2 (PA7, TIM1) and pin 4 (PA4, LPTIM2), are on the
        header as what they are: `gpio` has such a pin as `pwm`, with the timer,
        the frequency and the part of the time that the pin is high, worked out
        from the timer's registers (no edges are made). `gpio PIN --wait pwm`,
        the numbers in `status --json`, a line in `status`; in the window the
        pin is lit for that part of its width. The low-power timers have the
        clock that RCC picks for them now (LPTIM2's PWM runs from 64 MHz), and
        one that runs with no interrupt on costs nothing. In `gpio_check.py`: a
        script in the firmware's JavaScript on all five firmwares, and an app of
        the check's own (`scripts/pwm_check_app`, the official 1.5.1) for what
        the firmware's calls never set: the other polarity, PWM mode 2, a
        prescaler in LPTIM2, an open-drain pin, a timer without its pin;
        110 checks there, 81 on the other four; `gui_check.py` 60. The duty
        rules are the reference manual's, not measured on a real Flipper
        (BRINGUP_LOG 124)
  - ☑ A voltage for the ADC (2026-11-12): `gpio PA7 1.25V` holds a voltage to one
        of the six pins that the firmware's ADC has as inputs, and the firmware
        measures it. An ADC model in place of a stand-in that gave one number and
        crashed the firmware on a second read (`stm32wb55_adc.c`: one conversion
        at a time, ideal; the reference from VREFBUF; the chip's own reference,
        temperature sensor and battery input, with calibration values to match).
        `gpio`, `status` and the window's GPIO menu have the ADC in a line: on or
        off, how many conversions, the last one's pin, voltage and number; the
        window holds a voltage from a pin's menu. In `gpio_check.py`: a script
        in the firmware's JavaScript on all five firmwares, and an app of the
        check's own (`scripts/adc_check_app`, the official 1.5.1) with the
        firmware's own conversions in both ranges; 93 checks there,
        70 on the other four; `gui_check.py` 57 (BRINGUP_LOG 123)
  - ☑ An interrupt on a held pin and an open-drain output (2026-11-12): both
        worked as they were, on the path that a key's interrupt takes. An app of
        the check's own, `scripts/gpio_check_app` (C, ufbt, the official 1.5.1):
        four inputs with an interrupt, held from outside, and PA7 as an
        open-drain output; 16 more checks in `gpio_check.py` (72 there, 56 on
        the other four). New: `gpio`, `status --json` and the window say which
        edges of an input the firmware has an interrupt for (`in+r`, `in+f`,
        `in+rf` in the property `header`). The firmware's interrupt lines, which
        leave the header four pins with one to give, are in the README
        (BRINGUP_LOG 122)
- ☑ A recording of the screen as an animated GIF: `./flipperemu record OUT.gif`
      (a client of the front-panel link, for a device without a window) and
      Device > Record the screen in the window; `scripts/fe_record.py` writes the
      GIF itself (two colours, only the rectangle that changed, the time each
      picture was shown for); checked by `smoke_test.py` (Pillow reads it) and
      `gui_check.py` (Qt's reader does)
- ☑ Pause and continue: `./flipperemu pause` / `continue`, Device > Pause in the
      window (Ctrl+P, "PAUSED" on the glass). The panel has the pause as a property
      (`paused`), says why the machine stands still (`stopped`, and `running 0 WHY`
      on the front-panel link: a pause, a debugger), and keeps the NFC link's own
      stops for a card's answer apart from it; what needs the firmware says why it
      cannot (BRINGUP_LOG 81)
- ☑ A firmware that has halted (2026-11-10): a debug build after a crash, any build
      after `furi_halt()`. `status`, every command that needs the CLI, `app` and the
      window ("HALTED" on the glass) say so, with the crash and the ways on (`gdb`,
      `reset`); known by the halt loop's code at the PC (BRINGUP_LOG 87)
  - ☑ It kept one host core busy (the loop's `wfi` returns at once: an interrupt is
        pending and masked). The machine looks for the halt loop itself and lets the
        CPU run a hundredth of the time from then on: 7 % of a core, where a firmware
        that idles takes 25 %; a reset ends it, gdb is not slowed (2026-11-11,
        BRINGUP_LOG 93)
  - ☑ A crash in the middle of another thread's log line leaves no report in the log
        (the firmware drops it; seen on Momentum). The reason is in the fault register
        (RTC backup register 4, a pointer into the flash) until the crash screen is
        left: `app`, `app --close` and `status` read it there (2026-11-10, BRINGUP_LOG
        88)
  - ☑ The window asks `status` when it finds the emulator, when the firmware started
        again and when a command failed, not only after a `[CRASH]` in the log: one
        opened on a firmware that has halted shows it, and a crash without a report is
        said from the fault register (2026-11-10, BRINGUP_LOG 89)
  - ☑ A debug build whose crash printed nothing halts without a line in the log: the
        window knew of it only with its next command. The machine says it now:
        `halted` on the panel link, and a line in the terminal of a headless `run`
        (2026-11-11, BRINGUP_LOG 93)
- ☑ The setup script, run again (2026-11-11): on a tree that had patch 0008 it took
      0007 for missing (0008 builds on it in the same file), applied it a second
      time, in part, and stopped. It now takes the patches out of a copy of the
      files, the last one first, to know what a tree has; tries what it is to apply
      on a copy; and names a tree that was damaged that way, with the way out.
      `scripts/setup_check.py`: 28 checks on stand-in trees, nothing built
      (BRINGUP_LOG 113)

## M6: research tooling ☐
- ☑ Snapshots (boot once, save, and be back at that moment later). By QEMU's migration
      to a file, not `savevm`: the launcher's tools read and write the card's raw image,
      and a qcow2 image would shut them out. In four parts:
    - ☑ The first try and what stood in the way (2026-11-12). A save works as it is
          (`stop`, `migrate file:…`: 6 ms, 1.2 MB, RAM and flash in it) and holds
          QEMU's own models alone: none of this project's models had a saved state. A
          load ended QEMU in an assertion of its SD card model (patch 0011). With the
          patch the state loads, and the firmware then stops with `furi_check failed`
          and starts again: its chip's registers are at their reset values
          (BRINGUP_LOG 130)
    - ☑ The chip's models save their state (2026-11-12): clocks, timers, DMA, RTC,
          USB, the flash controller, CPU2's key store and the rest, 22 models. A desktop
          saved to a file answers the CLI after a load, on the five firmwares, with no
          crash and no new USB enumeration. A state that is loaded and saved again
          before it runs is the same: the RAM, and every section but the USB host's
          timer. The host's files follow at the loaded machine's first run, not in the
          load: the flash image, the key store's file, the calendar (the host's clock
          plus the distance it had). Who was at the other end of the USB port stays
          behind: an open serial port is closed as when its program goes away, and a
          device that a PC had over USB/IP is the built-in host's again. Found with it:
          every start of a device lost the part of a second of its clock, half a second
          on average. Not there: the board (the display stays dark after a load)
          (BRINGUP_LOG 131)
    - ☑ The board's models (display, power chips, LED driver, radios, RFID, iButton, infrared,
          the header, the card slot). The rule: what is held to the Flipper (a card, a tag, a
          key) is held to it again after a load; a signal that was in the air at the moment of
          the save ends there. In three pushes. Done (2026-11-12): the face and the power. The
          picture of a loaded machine is the saved one before it runs, with the LED, the
          backlight, the motor and the battery as they were set; a blink that was under way goes
          on, a tone sounds until the firmware ends it, and a button that was held at the save
          is let go at the first run (BRINGUP_LOG 132). Done (2026-11-12): the radios and what
          is held. A tag, a card and a key are held to the loaded machine with all that was
          written to them, on the five firmwares, and a read of a MIFARE Classic card that was
          saved in the middle of its dictionary attack finds every key after the load. Saved in
          an app that has a radio (official 1.5.1): an emulation goes on for the next reader, a
          Read hears the next signal, a transmission is captured from the load on and ends when
          its button is let go, an infrared signal that was being played is not learned. A state
          with a length or an index out of its bounds is refused (BRINGUP_LOG 133). Done
          (2026-11-12): the header, the card slot and the cable. A pin that the outside holds
          (low, high or at a voltage) is held on the loaded machine and let go by `gpio PIN
          free`; a card that was out is out; the cable is in or out as it was, on the five
          firmwares. A sensor, an EEPROM and a flash on the header's buses are as they were: an
          I2C write to the EEPROM that was saved before its STOP ends in the loaded machine, and
          a read of the flash that was saved in its middle goes on at the next byte (official
          1.5.1, an app). QEMU's EEPROM saved nothing (patch 0013), and its address counter
          could leave its memory after an address that was cut short (patch 0012, an older
          defect). A halted firmware is found halted again. Not there: header devices whose QEMU
          models save no state are as after a reset (BRINGUP_LOG 134)
    - ☑ A `snapshot` command in the launcher, a check for it and the docs (2026-11-13).
          `snapshot save [NAME]` keeps a running device as it stands, under
          `devices/NAME/snapshots/`: the state, a copy of the card, of the flash image and of
          the header devices' files (their data shared with the originals where the file
          system can), the run's arguments, the names of what is held, the firmware's log
          and the emulator's build. `snapshot load [NAME]` starts the device at that moment
          again in two to three seconds, with its window or without; the device's files of
          before the load are kept aside until the load has worked, and are put back when it
          has not. A state of another build of the emulator is not loaded unless that is
          asked for, and one whose bytes have changed is refused before anything is done.
          Found with it: a machine that had saved its state while it was paused ended QEMU
          in an assertion at its next write to the card, and no machine could load a state
          that was saved at a debugger's stop (both in the panel's model now).
          `scripts/snapshot_check.py`: 56 checks on the five firmwares (BRINGUP_LOG 135)
    - ☑ Tests of what the command had left untried (2026-11-13): a save and a load with a
          gdb on the machine (a save at its step, its leaving lets the machine go on, that
          state loads), a firmware that had crashed (a debug build's halt and report after
          a load, a release build's message of the crash), and an app with a key of the
          enclave loaded at the save (`scripts/snapshot_check_app`, ufbt, official 1.5.1:
          the device's own key, and a factory key with the self-test's block). Found with
          it: the launcher's load saves the loaded machine to check it, and AES1 said in
          that state that it had no answer for the factory key (in the model now).
          `scripts/snapshot_check.py`: 67 checks on the official 1.5.1 and its dev build,
          65 on Momentum, Unleashed and RogueMaster (BRINGUP_LOG 136)
    - ☑ `status` names the snapshot that a run began at, the window has Device > Save a
          snapshot…, and `scripts/gui_check.py` loads a snapshot that its window saved, in
          a new window that has the picture of the save to the pixel and the tag that was
          held, and goes on with Pause (2026-11-13; BRINGUP_LOG 137)
      - ☑ A load from the window's own menu (2026-11-14): Device > Load a snapshot, the
            device's newest first; the window's emulator stops and its session starts
            again with the snapshot's files and `-incoming` (`Session.load()`), in the
            same window, running on or standing still. One that cannot be loaded is
            refused before anything stops. `scripts/gui_check.py` 79 (80 with an SPI
            script; BRINGUP_LOG 144)
- ☐ Checks that hold on a busy computer (Kara, 2026-11-13)
    - ☐ The smoke test with twice as many busy loops as threads beside it
          (2026-11-15): 26, 25 and 18 of 28. The SDR recording, the T5577's
          write, the 1-Wire read and the infrared each fail now and then; once
          in three the firmware's CLI queue overflows (FW-07) while the NFC app
          reads and the check opens the port for its next step. Alone: 28
          (BRINGUP_LOG 171). After the timer's waits were mended (BRINGUP_LOG
          179): 27, 26 and 26. The T5577's write fails each time, the 1-Wire
          read and a Sub-GHz reception once: what the firmware times with its
          own waiting loops against the host's clock.
          QEMU's instruction-counted clock with a fixed shift, tried at last
          (`-icount shift=4,align=on`; `FLIPPEREMU_QEMU_ARGS` gives it to a
          check's emulators): alone 28 of 28 in the same 70 s. Beside 32 busy
          loops the emulated time is right and slow to come, a first start in
          42 s for the host clock's 7, more than the smoke test waits for one;
          with ten times the patience 26 of 28 in 151 s, the three above among
          the passes, and the two that fail are the cards that a program on
          the host plays over the NFC link. Not the default: one run, a start
          twice as long alone, and the link under it not understood
          (BRINGUP_LOG 183).
          With the machine's clock the CPU's own time (BRINGUP_LOG 188): 26,
          27 and 26 on 2026-11-16, 28 and 21 on the 21st (27 and 28 without
          the 60 µs for a wake-up). The 21 is an NFC shell that stands still
          in `mfu info` (FW-23 by its looks) and the firmware's end by FW-07
          after it: what is left to do here is in the last items below
    - ☑ What the firmware times with its waits, on a busy host (2026-11-15):
          the cycle counter does not count the time that the host has the
          emulated CPU's thread waiting for a core (the kernel's count of it
          for the thread, taken out in a wait's loop and around a device's
          edges), and the T5577 and the 1-Wire slave go by how long the
          firmware waited, not by the machine's clock. Beside 32 busy loops:
          `rfid write` to a T5577 succeeds 6 times of 6 with four block writes a
          command (before: 1 of 5, with blocks of anything and one locked);
          `ikey read` of a DS1990A 23 of 24, most under a second (before: 16 of
          24). Alone nothing changes: the host takes nothing (BRINGUP_LOG 184)
    - ☑ Reception on a busy host (2026-11-15): beside 32 busy loops the
          infrared's and the Sub-GHz's reception failed most runs of the smoke
          test. Not the models' doing (their edges wait for the firmware): the
          test started `ir rx` or `subghz rx`, slept a second and sent its
          signal, before the receiver was on. It waits for the command's own
          "Press Ctrl+C" now, and 15 s for what was heard. Five runs beside the
          loops: 27, 28, 21, 28, 21 of 28, both receptions, the T5577's write
          and the 1-Wire read in all five; the two of 21 are the firmware's
          crash (FW-07) and what comes after it (BRINGUP_LOG 185)
    - ☑ An NFC `dump` of the shell that never comes back, beside 24 busy loops
          about one in forty (11 in 466 dumps of a MIFARE Classic 1K). Found:
          it is the firmware's (FW-23). `furi_thread_flags_clear()` reads the
          thread's flags and writes them back in two calls, and the flag that
          the chip's interrupt handler sets between the two is gone
          (`scripts/fw23_lost_flag.py` does it with gdb). Not mended here, and
          not to be mended by a rule for one pin: a late firmware clears its
          timers' flags when the chip's interrupts come, whenever that is
          (BRINGUP_LOG 186, 187: two rules tried and taken out). What would
          keep the firmware from being late is a machine whose clock does not
          run while the host has the CPU's thread off its core: not the
          cycle counter alone (BRINGUP_LOG 184) but every timer. That is
          QEMU's `-icount` done by the host's count of the thread's running;
          `-icount` itself was not it (BRINGUP_LOG 183). Done, the next
          item: on 2026-11-16 once in 431 dumps with it, against 3 times in
          36 in turns without. (Five days later, the host cooler and slower:
          2 or 3 in 500 with either clock. What is left is in the item after
          the next.) The smoke test's steps after such a shell
          opened and closed the port on it, which ends the firmware by FW-07
          (its two runs of 21 in five): they leave the shell first now
          (BRINGUP_LOG 186)
    - ☑ The machine's clock is the CPU's own time (2026-11-21): QEMU's
          virtual clock, and so every timer of the machine, leaves out the
          time that the host has the CPU's thread off its core while the CPU
          is awake (the hook of `-icount`'s clock, the host's count of the
          thread's running, a look every 20 µs at most). Time that the CPU
          sleeps counts, and 60 µs of each wake-up (an idle Flipper with the
          cable in wakes a thousand times a second: 99.4 % of the wall
          clock's speed on an idle host, 95 to 97 without those). The
          calendar stays on the host's clock, what was left out is in a
          saved state, `run --host-clock` is the clock as it was, and the
          machine says what it left out and gave (`qom-get /machine
          clock-steal`, `clock-wake`). `scripts/clock_check.py` makes the
          host take time and looks: 16 checks. Beside 32 busy loops, in
          turns with the host's clock: on the 16th the smoke test 26, 27 and
          26 of 28 against 16, 20 and 20, `rfid write` to a T5577 18 of 18
          against 15, `ikey read` 36 of 36 against 31; on the 21st, the host
          at half the speed and cooler, 28 and 21 against 26 and 27, 12 of
          12 with either, 24 of 24 against 14, and the NFC emulation check
          72 of 75 against 70. Alone every check as before (BRINGUP_LOG 188,
          which has the build of it that took nothing out, too)
    - ☐ What that clock does not take out. A timer is the main loop's to
          run, and a main loop that the host holds up is late with it while
          the CPU runs on (`-icount` waits for it; nothing here does). The
          `dump` that FW-23 stops still comes once in two hundred beside 24
          loops, with any clock on a cool host (2026-11-21), and a guess is
          the block timer's interrupt brought late, with the frame going out
          by then. To try: a clock that does not go past the first timer
          that the main loop has yet to run (its time is
          `timerlist_deadline_ns()` of the main loop's list, asked with the
          hook answering 0), the lateness taken out like the rest; in turns
          with the clock as it is, beside the loops, and kept only if that
          shows. A host that is slower itself is slower for the emulated
          CPU: this one is by a factor of 2 to 2.5 when its turbo is off,
          for half a minute when it is hot or for a day by its power
          profile. And the checks' own limits are the wall clock's: beside
          16 loops the RFID check's `write` (40 s) and `read` (12 s) run out
          now and then with either clock
    - ☐ The card's image is read a block at a time through QEMU's pool of
          threads, and the CPU's thread waits for each (4430 times while the
          firmware summed 2.2 MB, with 15000 waits for the main loop's lock
          beside them: 5 to 6 % of that time, on an idle host). Not in the
          machine's clock, but in the host's time for anything on the card
    - ☐ The fuzz check's step that looks at which inputs were kept fails now
          and then when all checks run side by side
    - ☐ Reading an emulated ISO15693 card, alone: a read with five to seven
          requests sent again, three times in about 150 reads since BRINGUP_LOG
          184 and in none of about 30 before it. Too few to say that it is that
          entry's; sixty traced reads had none, and the cycle counter took out
          nothing in them that the kernel had not counted
    - ☑ A time limit in every check (2026-11-14): past it, a FAIL, where each of its
          threads stands, its emulators stopped, exit status 124; `FLIPPEREMU_CHECK_LIMIT`
          changes them (`x2`, seconds, `0`). After a window check waited three hours in a
          dialog unseen (BRINGUP_LOG 145)
    - ☑ Checks that build an app with ufbt, side by side (2026-11-15): ufbt has
          one build folder for every app; `./flipperemu ufbt` takes a lock on it for
          as long as a ufbt runs, and gdb_check takes the app's symbols from its own
          folder. coverage, ufbt, app and gdb checks together, twice: all pass
          (BRINGUP_LOG 165)
    - ☐ The Metakom read of the smoke test under load (0 of 10 at a load of 20 on 16
          cores, BRINGUP_LOG 136 and 141). Not reproduced on 2026-11-14 with busy loops
          and with other emulated checks at a load of 15 to 20 (BRINGUP_LOG 146): to be
          caught when it comes back, with its device kept
    - ☑ The window check under load (2026-11-14): 77 of 79 at a load of 21, an I2C
          scan's call that the firmware skips when more than 2 ms pass between two of
          its instructions (about one in three thousand calls); the scan's checks take a
          skipped address now (BRINGUP_LOG 147). Not tried: QEMU's `-icount` with a fixed
          shift, which would keep a busy host out of the emulated time altogether
    - ☑ A card's answers that the firmware modulates itself, under load (2026-11-15):
          TIM2's wait at the marker of the firmware's ring gave way on a busy host in
          three ways (its limit counted from the update's time, the firmware's read
          of ARR for an underrun taken for the end's, a ring with no marker between
          two writes), and an answer came cut short or with a ring's worth of it
          repeated. Mended; the samples of an ISO15693 frame go by the CPU's own
          running time. Beside 32 busy loops on 16 threads: seven ISO15693 reads, all
          whole, six of them with no frame lost, where none of four was read
          (BRINGUP_LOG 179)
    - ☐ The NFC emulation check beside 32 busy loops: 22 or 23 of 24, another check
          each time (an Ultralight C's read, a MIFARE Classic's with wrong keys): a
          frame that the firmware does not answer. It passes at the regression's load.
          Tried and not kept: the reader's patience and its gap after an answer by
          the CPU's own time (BRINGUP_LOG 179)
- ☑ Coverage of the firmware or of an app (2026-11-14): `run --coverage` loads a TCG
      plugin of our own (`qemu/plugins/fe_cov.c`: a counter to each translated block,
      added to inline, about 2 % slower; the bytes of blocks in RAM kept),
      `./flipperemu coverage` maps the counts with an ELF's symbols and line table:
      functions' calls, lines' counts, files, an lcov file; `coverage reset` before a
      test; an app by its id or its `.fap`, found by its bytes after it has ended;
      a stopped device's last run. `scripts/coverage_check.py` 23 (BRINGUP_LOG 153)
    - ☑ A trace (2026-11-15): `run --trace`, `coverage trace [--before start|
          fault]`: the last 65,536 blocks in their order, by function, kept at
          each start of the firmware and at each fault; `scripts/coverage_check.py`
          31 (BRINGUP_LOG 164)
    - ☑ Apps that run in place from the flash (2026-11-15): the plugin keeps the
          bytes of what runs above the firmware's end in the flash (`keep=`)
- ☑ Fuzzing harness for parsers in the firmware (2026-11-15): `./flipperemu fuzz
      subghz|ir|rpc|cli`, our own (`scripts/fe_fuzz.py`), guided by the coverage
      plugin's `news`; inputs as a Flipper gets them (a signal on the air, an RPC
      session, a CLI command), no reset between them; crashes kept with the inputs
      before them and the place of the CPU's fault (QEMU patch 0016: the NVIC's
      `last-fault`, in `status` too); `--replay`. Not AFL++'s QEMU mode: that is
      another QEMU, and its speed comes from resets that a Flipper with its
      peripherals does not have here. `scripts/fuzz_check.py` 16 (BRINGUP_LOG 154)
    - ☑ Found with it, in its first minute: the official 1.5.1's RPC crashes on a
          storage list request without a path (`strcmp(NULL, "/")`,
          `rpc_storage.c:266`); Momentum, Unleashed and RogueMaster too. Not
          reported upstream (Kara's to decide)
    - ☑ The firmware's bugs in one list (2026-11-15): `docs/FIRMWARE_BUGS.md`,
          20 of them (FW-01 to FW-20) with firmwares, cause, how to see each and
          whether it was reported; a new one gets the next number (BRINGUP_LOG 168)
    - ☐ A lockup seen once after three RPC runs on one device, in the crash
          handler's walk of the heap (`vPortGetHeapStats`): what crashed first is
          not known (the fuzzer looks once more after its target is closed now)
    - ☑ NFC (2026-11-15): `fuzz nfc`: cards whose contents the fuzzer made (the
          card model's descriptions, a value at a time), held to the Flipper and
          read by the NFC shell's `dump`; about two a second. It finds Momentum's
          shell crash at its seeds and the official 1.5.1's `dump` that never
          returns as a hang (BRINGUP_LOG 166)
    - ☑ A card's raw answers (2026-11-15): `fuzz nfc-frames`: an exchange as
          `nfc record` writes it, each answer the fuzzer's to change (bytes, a
          length that is not whole bytes, silence, another frame's answer; the
          CRC made right again most times), given by a card on the NFC link.
          Seeds: what the emulator's ISO14443A cards answer to `dump`, recorded
          at the start. About one a second. Momentum's and Unleashed's known
          shell crashes come again through it; nothing new in the first runs.
          fuzz_check 20 / 14 (BRINGUP_LOG 173)
    - ☐ More targets: the cards that are not ISO14443A on the link (a card on
          the link is one), iButton and RFID pulses,
          the Sub-GHz decoders on Unleashed and RogueMaster (their CLI does not
          start the radio: through the app's Read), BLE's GATT writes
- ☐ Upstream the eighteen QEMU patches (0004: open-drain GPIO lines, 2026-11-08; 0005:
      register reads without the big lock under TCG, 2026-11-10; 0006 to 0009: a
      chardev's log file, the UART's reception, a byte no receiver took, an EEPROM's
      file, 2026-11-11; 0010 to 0013: Winbond's read-ID commands, the load of a saved
      state with an SD card on SPI, an EEPROM's address counter after an address that
      was cut short, an EEPROM's saved state, 2026-11-12; 0014 and 0015: an imprecise
      BusFault, a Lockup that stops the processor, 2026-11-14; 0016: where the last
      fault was, 0017: a UART's transmission at the baud rate, and 0018: a baud
      rate for the line's other end, 2026-11-15); consider upstreaming the machine

## Non-goals (for now)
- Running ST's radio stack on an emulated CPU2 (closed, encrypted binary)
- Cycle accuracy
