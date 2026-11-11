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
      CDC event queue (`furi_check` in USB_LP; BRINGUP_LOG 36). Since 2026-11-10 the
      host waits for the firmware to go idle rather than for the clock: file
      transfers over the CLI are up to twice as fast (BRINGUP_LOG 74)
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
        Next: what the decoder is fed, read out under gdb

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
        (BRINGUP_LOG 94)
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
- ☐ NFC: a PN532/PN533 (ACR122U over PC/SC) relay; and the other way round, listen
      mode: an external reader (or a real one: a Proxmark3 or PN532 as the card) in
      front of an emulating Flipper
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
- ☐ U2F from a host: the app starts, takes the USB port as a HID token and shows
      "Connected!"; a registration and a login from a browser or libfido2 over USB/IP
      have not been tried
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
  - ☐ gdb with a firmware tree's ELF and its `.extapps`; from an IDE (VS Code)
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

## M6: research tooling ☐
- ☐ Snapshot workflow (`savevm` on qcow2 images; boot once, jump straight to an app)
- ☐ TCG plugin for coverage/trace of firmware or a single FAP
- ☐ Fuzzing harness (AFL++/libafl QEMU mode) for parsers in the firmware
- ☐ Upstream the five QEMU patches (0004: open-drain GPIO lines, 2026-11-08; 0005:
      register reads without the big lock under TCG, 2026-11-10); consider upstreaming
      the machine

## Non-goals (for now)
- Running ST's radio stack on an emulated CPU2 (closed, encrypted binary)
- Cycle accuracy
