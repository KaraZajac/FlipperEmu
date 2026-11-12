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
  - ☐ A CLI command that waits takes a whole host core: a script in `delay()`, the
        firmware's `log` command with nothing to print. The CPU is in the idle task
        then (`prvGetExpectedIdleTime`), which does not go to sleep while such a
        command runs; an idle desktop on USB is a quarter of a core. Why is not known
        (seen 2026-11-12, BRINGUP_LOG 124)
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
        update is complete, but `update_check.py` has 32 of 33 then. Not known:
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
  - ☐ The UART model's other half: what the firmware sends at the baud rate, an
        overrun when the app does not read, a rate on the host's side (a wrong
        baud rate is understood now)
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
  - ☐ The SPI bus in the window's GPIO menu, as the I2C bus is there (next)
  - ☐ A record of what goes over the SPI bus (`spi record`, `spi watch`), as
        `i2c record` is one for the I2C bus
  - ☐ More devices for the SPI bus: a radio module (the external CC1101 that
        Momentum, Unleashed and RogueMaster have a setting for, an NRF24
        board), a display
  - ☐ More on the SPI bus: an example app in C beside the script, and the
        check's app on the other firmwares, each with its own SDK
  - ☐ A PWM's edges in the pins' record (`gpio record` has `x` for a pin with a
        PWM, and `gpio watch` no line); other timers' outputs on the header's
        pins than the firmware's two
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
- ☐ Snapshot workflow (`savevm` on qcow2 images; boot once, jump straight to an app)
- ☐ TCG plugin for coverage/trace of firmware or a single FAP
- ☐ Fuzzing harness (AFL++/libafl QEMU mode) for parsers in the firmware
- ☐ Upstream the nine QEMU patches (0004: open-drain GPIO lines, 2026-11-08; 0005:
      register reads without the big lock under TCG, 2026-11-10; 0006 to 0009: a
      chardev's log file, the UART's reception, a byte no receiver took, an EEPROM's
      file, 2026-11-11); consider upstreaming the machine

## Non-goals (for now)
- Running ST's radio stack on an emulated CPU2 (closed, encrypted binary)
- Cycle accuracy
