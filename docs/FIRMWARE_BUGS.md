# Firmware bugs found with the emulator

Bugs in Flipper Zero firmware that turned up while FlipperEmu was built and checked.
They are the firmware's, not the emulator's: each one is either read in the firmware's
source or shows the same way on every run with nothing of the model in its path.

- **None has been tried on a real Flipper.** Where the cause is in the source, a real
  one should do the same; where it is only seen in the emulator, the entry says so.
- **None has been reported upstream.** Whether to report is Kara's decision; the
  "Reported" column is there for it.
- Each bug has a number that stays its own (`FW-01`), the firmwares it was seen on,
  and the entry of `BRINGUP_LOG.md` that has the whole story.
- "Official" is flipperdevices' firmware. Versions tried: official 1.5.1 (others
  where named), the official dev build `3060cee9`, Momentum `mntm-012`, Unleashed
  `unlshd-094`, RogueMaster `RM1024-1347`. Source was read for official 1.5.1 only;
  for the forks "does the same" means it behaves the same.

A new bug gets the next number and a row in the table. A bug that is fixed upstream
keeps its row, with the version that fixed it.

## The list

| No. | What happens | Firmwares | Kind | Reported |
| --- | --- | --- | --- | --- |
| FW-01 | An RPC storage list request with no path restarts the Flipper | official 1.5.1, Momentum, Unleashed, RogueMaster | crash | no |
| FW-02 | The NFC shell's `dump` of a card that is ISO14443-3B and no more never returns; the Flipper is frozen | official 1.5.1 and dev, Momentum, Unleashed | freeze | no |
| FW-03 | Reading an ST25TB card whose UID names no chip the firmware knows restarts the Flipper | official 1.5.1 | crash | no |
| FW-04 | The NFC shell's `scanner` with a Type 4 tag, an EMV card or an NTAG 424 DNA restarts the Flipper | Momentum mntm-012 | crash | no |
| FW-05 | The NFC shell's `dump` of an EMV card restarts the Flipper | Unleashed unlshd-094 | crash | no |
| FW-06 | The NFC app, asked to close by the loader while it reads or emulates, restarts the Flipper | official 1.5.1, Momentum, Unleashed | crash | no |
| FW-07 | The CLI's USB service crashes when its queue of eight events is full | official 1.5.1, Unleashed | crash | no |
| FW-08 | After an update the Desktop sometimes draws its slideshow before it is made | Momentum mntm-012 | crash, now and then | no |
| FW-09 | Sub-GHz Send held for more than about 8 s while charging: the transmission does not end at the release | official 1.5.1 | stuck transmitter | no |
| FW-10 | `dump -k KEY` of a MIFARE Ultralight C tries a mangled key | official 1.5.1 | wrong result | no |
| FW-11 | A DESFire record file is asked for with the wrong count, and its contents are never read | official 1.5.1 | wrong result | no |
| FW-12 | A 32-bit Radio Key (Securakey) card is emulated with the wrong facility code | official 1.5.1 | wrong result | no |
| FW-13 | An infrared transmission garbles the start of the next Sub-GHz transmission or RFID emulation | official 1.5.1 | wrong result | no |
| FW-14 | The USB-UART bridge on "USB Channel 1" has no port at all | official 1.3.4 and 1.5.1, Momentum, Unleashed | feature lost | no |
| FW-15 | `storage read_chunks` cannot be stopped, by Ctrl-C or by closing the port | official 1.5.1 | CLI blocked | no |
| FW-16 | `crypto encrypt` encrypts what the heap held behind the text | official 1.5.1 | leak into ciphertext | no |
| FW-17 | The Bluetooth serial service's RPC status sends three bytes of the stack | official 1.5.1 | leak over the air | no |
| FW-18 | While a CLI command waits, the CPU never sleeps | official 1.5.1 | battery | no |
| FW-19 | The NFC shell's `emulate -f` of a Type 4 tag's file restarts the Flipper | Momentum mntm-012 | crash | no |
| FW-20 | A serial port closed right after a write loses the last two bytes | official 1.5.1 | wrong result | no |
| FW-21 | After an ISO15693 card was emulated, the ISO14443A listener takes its own answers for failed, and an ISO14443-4 card of the Flipper's is not activated any more | official 1.5.1 and dev, Momentum, Unleashed, RogueMaster | wrong result, until a restart | no |

## Crashes and freezes

### FW-01: an RPC storage list request with no path

- **What happens:** one RPC message of 11 bytes, alone, restarts the firmware: "NULL
  pointer dereference", a MemManage fault at address 0.
- **Why:** `rpc_system_storage_list_process()` does `strcmp(list_request->path, "/")`
  with a path that is NULL when the request has none
  (`applications/services/rpc/rpc_storage.c:267`).
- **Reach:** over USB, and over Bluetooth once paired.
- **To see it:** the message is `0a 08 03 3a 06 4a 04 2f 65 88 74` after the session
  is started; `./flipperemu fuzz rpc` finds it within its first minute, and
  `./flipperemu status` names the place of the fault.
- **Seen on:** official 1.5.1, Momentum, Unleashed, RogueMaster.
- **Log:** entry 154 (and 164 for the trace of it).

### FW-02: the NFC shell's `dump` of a card that is ISO14443-3B and no more

- **What happens:** `scanner` finds the card; `dump` never returns, the shell answers
  no more and neither does anything else. Only a reboot (Left + Back) ends it.
- **Why:** `nfc_cli_dump_poller_callback_iso14443_3b()` ends with `return
  NfcCommandContinue;` where the other protocols' callbacks have `return command;`
  (`applications/main/nfc/cli/commands/dump/protocols/iso14443_3b/nfc_cli_dump_iso14443_3b.c:26`).
  The reader is never told to stop and calls the callback again at once. Its thread
  has the highest priority, so the shell's thread, which was to stop it, never runs.
- **To see it:** `./flipperemu nfc iso14443b` with a card file whose device type is
  `ISO14443-3B`, then `nfc` and `dump -f /ext/nfc/x.nfc` on the CLI.
  `./flipperemu fuzz nfc` reports it as a hang.
- **Seen on:** official 1.5.1, the official dev build, Momentum, Unleashed.
- **Log:** entries 156 and 166.

### FW-03: an ST25TB card of a kind the firmware does not know

- **What happens:** reading the card restarts the firmware: `[CRASH][NfcWorker]
  unsupported st25tb type`. The shell's `scanner` only names the protocol and is
  safe; `dump` crashes.
- **Why:** `st25tb_get_type_from_uid()` knows a chip by the upper six bits of the
  UID's third byte and ends in `furi_crash()` for any other value
  (`lib/nfc/protocols/st25tb/st25tb.c:261`). The UID comes from the card, so a card
  decides it.
- **To see it:** an ST25TB card file with the UID `D0 02 40 01 23 45 67 89`, held with
  `./flipperemu nfc FILE`, and `dump` in the NFC shell.
- **Seen on:** official 1.5.1 (in the emulator, 2026-11-15). Not tried: the NFC app's
  Read, which uses the same reader code; the other firmwares.
- **Log:** entry 156.

### FW-04: Momentum's NFC shell and the newer ISO14443-4 cards

- **What happens:** `scanner` with a Type 4 tag, an EMV card or an NTAG 424 DNA in
  the field, and the firmware is gone: "NULL pointer dereference", a MemManage fault
  at 0x080010a2 (a read of address 0), called from 0x08001162. Six times in six
  tries.
- **Why:** not known. There are no symbols for this build here, and Momentum's source
  was not read. The three are the kinds of ISO14443-4 card that this firmware knows
  and the official 1.5.1 does not.
- **Not affected:** Momentum's NFC app reads all three. Unleashed's and the official
  dev build's shells name the card ("Type 4 Tag", "NTAG4xx").
- **To see it:** `./flipperemu nfc t4t`, `nfc emv` or `nfc ntag4xx` with a UID, then
  `nfc` and `scanner` on the CLI. `./flipperemu fuzz nfc` finds it at its first
  inputs.
- **Seen on:** Momentum mntm-012.
- **Log:** entries 158, 166 and 170.

### FW-05: Unleashed's `dump` of an EMV card

- **What happens:** the NFC shell's `dump` of an EMV card ends the firmware with
  `[CRASH][dump] furi_check failed`. A Type 4 tag dumps.
- **Why:** not known; Unleashed's source was not read.
- **To see it:** `./flipperemu nfc emv`, then `nfc` and `dump -f /ext/nfc/x.nfc`.
  Found by `./flipperemu fuzz nfc` with its seeds.
- **Seen on:** Unleashed unlshd-094.
- **Log:** entry 166.

### FW-06: the NFC app, asked to close while it works

- **What happens:** `loader close` (which is how `fbt launch` and `ufbt launch` end
  the app that runs) while the NFC app reads a card, or while it emulates a file it
  was opened with: `[CRASH][NFC] furi_check failed`, and a restart.
- **Why:** the loader's request ends the app's event loop where it is. The app then
  frees its scanner or its NFC instance, and both check that they are idle
  (`nfc_scanner_free()`, `lib/nfc/nfc_scanner.c:231`; `nfc_free()`,
  `lib/nfc/nfc.c:260`). In the middle of a read they are not.
- **To see it:** NFC > Read on the screen, then `./flipperemu app --close`.
  `scripts/app_check.py` does it on purpose and wants the report.
- **Seen on:** official 1.5.1, Momentum, Unleashed.
- **Log:** entries 83 and 133.

### FW-07: the CLI's USB service and its queue of eight

- **What happens:** `[CRASH][ISR USB_LP] furi_check failed`, and a restart.
- **Why:** the CLI's CDC service puts one event per packet, and two per opening and
  closing of the port, into a queue of eight, with
  `furi_check(furi_message_queue_put(queue, &event, 0) == FuriStatusOk)` in the USB
  interrupt (`applications/services/cli/cli_vcp.c`). A ninth event while the
  service's thread is held up fails the check.
- **Two ways to it:**
  - A host that sends packets faster than the firmware takes them. The emulator's
    host did before it was paced to the firmware; a real host is held back by the
    bus and the endpoint's NAKs, so this way may be the emulator's alone.
  - The port opened and closed a few times while the shell is busy with a command
    that cannot be stopped (FW-15), or while an app's question waits on the screen.
    Nothing of that needs fast packets.
- **Seen on:** Unleashed (the first way), official 1.5.1 (the second), in the
  emulator. The launcher keeps out of both.
- **Log:** entries 36, 74, 76 and 77.

### FW-08: Momentum's Desktop after an update

- **What happens:** now and then, a few seconds after an update is installed, the
  log has `[CRASH][ISR MemMgmt] NULL pointer dereference`. The firmware restarts and
  goes on; the update is complete each time.
- **How often:** 6 of 50 updates, and 3 of 32 in a second count. In 16 updates the
  official 1.5.1 had none.
- **Why:** caught twice with a debugger at the same place: the Desktop draws its
  slideshow view, and the draw asks about a slideshow pointer that is still zero. The
  view becomes the current one before its enter callback makes the slideshow, and a
  redraw between the two finds none. (The names are the official source's, which the
  code has the shape of; Momentum's source was not read.)
- **Seen on:** Momentum mntm-012, on a loaded host. Not known: whether a real
  Flipper's timing lets the redraw in.
- **Log:** entry 120.

### FW-09: Sub-GHz Send held for long while charging

- **What happens:** OK held on a saved key's Send screen for more than about 8 s
  with the USB cable in: the release does not end the transmission, and the radio
  stays in transmit. With the cable out, or with a hold of 4 s, it ends as it should.
- **Why:** the Send scene asks the notification service for a blink every 100 ms.
  With another light on (the charging LED) a blink takes 110 ms, so the service's
  queue of eight fills in about 8 s. The timer task then waits at that full queue
  behind the app, and the input service waits for the timer task to stop the key's
  timer, so the release is never published.
- **To see it:** `./flipperemu press ok --hold 12` on a key's Send screen, the cable
  in; `./flipperemu status` shows the radio in `tx` afterwards.
- **Seen on:** official 1.5.1, with gdb and in its source.
- **Log:** entry 133.

### FW-19: Momentum's NFC shell, asked to emulate a Type 4 tag

- **What happens:** `emulate -f FILE` in the NFC shell with a Type 4 tag's file,
  and the firmware is gone: `[CRASH][ISR MemMgmt] NULL pointer dereference`. The
  shell's banner comes where "Emulating" should.
- **Why:** not known (see FW-04: the same shell, the same kind of card, the other
  direction). The official dev build's, Unleashed's and RogueMaster's shells say that
  emulating a Type 4 tag "is not supported"; the official 1.5.1 does not load the
  file.
- **To see it:** a Type 4 tag's file on the card (`scripts/fe_nfc.py --nfc-file
  --t4t 04D1E2F3A4B5C6`), then `nfc` and `emulate -f /ext/nfc/FILE.nfc` on the CLI.
- **Not affected:** Momentum's NFC app emulates the same file, and a reader reads
  its NDEF message.
- **Seen on:** Momentum mntm-012.
- **Log:** entry 170.

## Wrong results

### FW-10: `dump -k KEY` of a MIFARE Ultralight C

- **What happens:** the NFC shell's `dump -k` with the card's right key does not
  open the card. The firmware's log shows the key it tried:
  `49454D4B 49454D4B41455242214E4143` for the key
  `49454D4B41455242214E4143554F5946`.
- **Why:** the Ultralight poller reads `key_request_data.key_provided` from an event
  whose data the shell's callback filled as `auth_context`; the two are members of
  one union. A byte of the key is taken for "provided", and the key is read from the
  union's start, where the password's four bytes come first
  (`applications/main/nfc/cli/commands/dump/protocols/mf_ultralight/nfc_cli_dump_mf_ultralight.c:23`).
  A key of zeros hides it.
- **Not affected:** the NFC app's Read, which finds the same card's key in the
  dictionary.
- **Seen on:** official 1.5.1.
- **Log:** entry 155.

### FW-11: a DESFire record file

- **What happens:** a dump of a DESFire card has no contents for its record files
  (unless a record is one byte long).
- **Why:** the firmware reads a record file with ReadRecords and a count that is the
  record's size in bytes (it uses its ReadData routine), and wants that many bytes
  back. A card counts records, and answers a count beyond what it has with a
  boundary error.
- **Seen on:** official 1.5.1, against the emulator's card model (which follows the
  DESFire command set here). A real card was not asked.
- **Log:** entry 155.

### FW-12: a 32-bit Radio Key card

- **What happens:** a 32-bit Securakey ("Radio Key") card read by a Flipper is
  emulated with the wrong facility code.
- **Why:** the 32-bit encoder copies encoded bits 38 to 44 from data bit 3, where
  the decoder and the 26-bit encoder use data bit 9
  (`lib/lfrfid/protocols/protocol_securakey.c:267` and `:252`).
- **Seen on:** official 1.5.1, in its source and in the capture of its emulation.
- **Log:** entry 28.

### FW-13: an infrared transmission and the next user of DMA2 channel 1

- **What happens:** after `ir tx`, the next `subghz tx` loses its start or sends a
  256-entry loop for ever; `rfid emulate` after `ir tx` glitches its first
  half-buffer once.
- **Why:** the IR transmitter uses DMA2 channel 1 with only the transfer-complete
  interrupt on, so the channel's half-transfer flag is left set.
  `furi_hal_subghz_start_async_tx()` reuses the channel, enables the half-transfer
  interrupt, and its refill routine runs at once, before the first fill.
- **Caveat:** the emulator sets the half-transfer flag whether or not its interrupt
  is enabled, as the reference manual (RM0434) says. If the chip does otherwise, this
  is not a bug.
- **Seen on:** official 1.5.1.
- **Log:** entry 27.

### FW-14: the USB-UART bridge on "USB Channel 1"

- **What happens:** with the bridge set to USB channel 1 the host sees one serial
  port, not two, and the bridge has none.
- **Why:** the bridge sets the two-port configuration and then enables the CLI's USB
  service, whose "enable" sets the one-port configuration whatever was there
  (`applications/services/cli/cli_vcp.c:173`). The older code set it only "if it is
  not set yet".
- **Seen on:** official 1.3.4 and 1.5.1, Momentum, Unleashed. Official 1.1.2 has the
  two ports and carries bytes both ways (`scripts/usb_dual_check.py`).
- **Log:** entry 162.

### FW-15: `storage read_chunks` cannot be stopped

- **What happens:** the command goes on to the file's end after Ctrl-C and after the
  port is closed: it writes into a pipe that nobody reads, 100 ms a write. For a
  file of 300 kB that is minutes without a CLI, and a way to FW-07.
- **Why:** `storage_cli_read_chunks()` has no look at the pipe's state or at Ctrl-C
  in its loop (`applications/services/storage/storage_cli.c:271`).
- **Seen on:** official 1.5.1. Not tried on the forks: the launcher reads an
  interrupted file to its end on all of them and drops it.
- **Log:** entry 76.

### FW-16: `crypto encrypt` and what follows the text

- **What happens:** the ciphertext of a text that does not fill its last block
  carries bytes of the heap behind the text's end. The same text encrypts to
  different ciphertexts from run to run.
- **Why:** `applications/services/crypto/crypto_cli.c` takes the text as a C string, rounds its length up to
  whole blocks, and encrypts that much without clearing what lies behind the NUL.
- **Seen on:** official 1.5.1.
- **Log:** entry 60.

### FW-17: the Bluetooth serial service's RPC status

- **What happens:** the characteristic's value is four bytes, of which the first is
  the status and the other three are whatever was on the stack (`01 2c 14 0a` for
  "active").
- **Why:** `ble_svc_serial_update_rpc_char()` hands
  `ble_gatt_characteristic_update()` the address of a one-byte enum for a value of
  four bytes (`targets/f7/ble_glue/services/serial_service.c:150`).
- **Seen on:** official 1.5.1. A reader looks at the first byte, so nothing breaks.
- **Log:** entry 140.

### FW-18: a CLI command that waits keeps the CPU awake

- **What happens:** while a CLI command waits (`log`, for one), the CPU never
  reaches its sleep.
- **Why:** the shell runs a command in a thread of its own and waits for it with
  `furi_thread_join()`, which looks every 2 ticks (`furi/core/thread.c:426`).
  FreeRTOS's idle task sleeps only when nothing is due for 4 ticks.
- **Seen on:** official 1.5.1, by its source and under gdb. What it costs a real
  battery was not measured.
- **Log:** entry 129.

### FW-20: a serial port closed right after a write

- **What happens:** the last two bytes of what was written do not go out. A script
  that does `serial.write(text); serial.end();` at 9600 baud sends all but the end
  of its text.
- **Why:** `furi_hal_serial_tx()` puts each byte into the UART's transmit register
  as soon as that is free and returns after the last one, with that byte still in
  the register and the one before still on the line
  (`targets/f7/furi_hal/furi_hal_serial.c:745`). `furi_hal_serial_deinit()` switches
  the UART off at once (`:694`); nothing between the two waits for the transmission
  to be complete (`furi_hal_serial_tx_wait_complete()` is there for it, and the
  JavaScript module's `end()` does not call it).
- **How much it matters:** the window is two byte times, 2 ms at 9600 baud and
  174 µs at 115200, where the time a script's interpreter takes between two calls
  may cover it.
- **To see it:** a script as above with a text of some length at 9600 baud, and
  `./flipperemu uart wait` for its last word. `scripts/uart_check.py` says how many
  bytes did not come.
- **Seen on:** official 1.5.1, in the emulator (QEMU patch 0017 gives a UART's
  transmission its time) and in its source. Not tried on a real Flipper.
- **Log:** entry 175.

### FW-21: after an ISO15693 emulation, the NFC chip's interrupt comes at both edges

- **What happens:** once the Flipper has emulated an ISO15693 card (a SLIX; read by
  a reader or not), every answer that it sends as an ISO14443A card through the
  chip's FIFO is logged as `[W][Iso14443_3aListener] Tx error: 1`, though it went
  out. A card with ISO14443-4 on top (a Type 4 tag) answers the RATS and nothing
  after it: the listener that was told its ATS had failed is not active. It stays
  so through later emulations, until a restart.
- **Why:** `signal_reader_start()` (`lib/signal_reader/signal_reader.c`) sets the
  chip's interrupt pin (PA2) to `GpioModeInterruptRiseFall`, for the DMAMUX's request
  generator. `signal_reader_stop()` only disables the pin's callback. The NFC HAL
  then sets the pin to `GpioModeInterruptRise` again
  (`furi_hal_nfc_iso15693_listener_transparent_mode_exit()` calls
  `furi_hal_nfc_init_gpio_isr()`), and `furi_hal_gpio_init_ex()`
  (`targets/f7/furi_hal/furi_hal_gpio.c`) only ever enables a trigger for an
  interrupt mode: the falling one stays on.
  From then on the pin's fall interrupts as well, and the pin falls when the firmware
  reads the chip's interrupt registers. `furi_hal_nfc_wait_event_common()`
  (`furi_hal_nfc_event.c`) clears its thread flag before that read, so the read sets
  it again. The next to wait is `furi_hal_nfc_iso4443a_listener_tx()`, with
  `furi_hal_nfc_event_wait_for_specific_irq()` for the end of its transmission and
  10 ms: that returns at once on the stale flag, reads the registers while the frame
  is still going out, finds no TXE and says so, and the listener gets a timeout.
- **To see it:** in the NFC shell, `emulate -f` a SLIX's file and Ctrl-C. Then
  emulate an ISO14443-4A card's file and read it (`./flipperemu nfc --reader
  iso-dep`): the log has the Tx error, and has none without the ISO15693 emulation
  before. EXTI's falling trigger for line 2 (FTSR1 at 0x58000804, bit 2) is 0 before
  that emulation and 1 after it, also after a later emulation of another card. With
  the bit cleared by gdb, a Type 4 tag that Momentum's NFC app emulates is read
  whole again; with the bit as the firmware left it, three tries get the ATS and
  no more.
- **To mend it:** the pin back to what it was in `signal_reader_stop()`, or a
  `furi_hal_gpio_init_ex()` that disables the trigger it is not asked for.
- **Seen on:** all five, in the emulator. The trigger bit: official 1.5.1 and
  Momentum. The Tx error: official 1.5.1, its dev build, Unleashed, RogueMaster. The
  Type 4 tag that is not read: the dev build, Momentum, Unleashed, RogueMaster (the
  official 1.5.1 emulates none). The cause is in the source of official 1.5.1. Not
  tried on a real Flipper; there the read of the registers has to be over before the
  answer is, and an ATS takes some 0.7 ms on the air.
- **Log:** entry 179.

## Seen once, not explained

Not in the list, because nobody knows yet whose they are:

- **A lockup after RPC fuzzing** (official 1.5.1, once): the firmware crashed after
  the last input and then faulted inside its crash handler's walk of the heap. Four
  more runs did not bring it back (entry 154).
- **The shell stopped answering during a MIFARE Plus `scanner`** (official 1.5.1,
  once in eight runs). `scripts/nfc_check.py` prints the device's state if it comes
  again (entry 159).
- **Unleashed's and RogueMaster's `subghz tx` and `subghz rx` on the CLI** say that
  the radio "failed to start". Their Sub-GHz app transmits and receives. Their
  source was not read (entries 86 and 154).
