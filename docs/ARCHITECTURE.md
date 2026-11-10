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
        │   rcc pwr flash hsem ipcc(+CPU2, FUS key store) aes×2 exti syscfg rtc
        │   lptim×2 spi×2 i2c×2 dma(+dmamux) rng dwt dbgmcu usb(+virtual host) tim2
        │   gpio×6 usart1 lpuart1 (QEMU L4x5 models)
        │   tim1/16/17 adc pka crc comp iwdg … (register files)
        │
   SPI2 ── st7567 (LCD, CS PC11, A0 PB1, RST PB0)   ── ssi-sd + sd-card-spi (CS PC12, CD PC10)
   SPI1 ── cc1101 (CS PD0, GDO0 → PA1)              ── st25r3916 (CS PE4, IRQ → PA2)
   I2C1 ── bq27220 @0x55   bq25896 @0x6B   lp5562 @0x30
   TIM16 CH1 (PB8) ── piezo → flipper-speaker → QEMU audio (desktop or WAV)
   PA0 + TIM2 TI1  ←─ IR receiver ← /machine/ir rx (marks/spaces)
   PA1 + TIM2 TI2  ←─ CC1101 GDO0 (async RX) ← /machine/subghz rx (the "air"),
                                                 or a real radio on its link (fe_sdr.py)
   TIM2 CH2 (DMA → ARR) ──→ CC1101 GDO0 (async TX) → devices/NAME/subghz-tx/*.sub
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
  idle, `SET_SYSTEM_CLOCK` → the clock switch STOP2 needs (see Clocks and time), the four
  key commands → the key store (next section), and everything else → status 0x01, as a
  FUS with no stack would. The answer is a command-complete event in the form of
  AN5185 6.3.4 (NumHCI 0xFF, the opcode, the status, then any payload). It is written
  over the command, so the command's payload is copied out first.
- Buffers released on channel 4 are taken back.

The firmware reacts exactly as on a stackless device: "C2 boot completed, mode: FUS",
"Stack isn't installed!", "Radio stack start failed". A **dead** CPU2 is not an option:
`furi_hal_crypto_enclave_load_key()` leaks its mutex when BT isn't alive, and the desktop
then deadlocks.

## The secure enclave (`stm32wb55_fus.c`, `stm32wb55_aes.c`, `flipper_enclave.c`)

What the firmware calls its secure enclave is two parts of the STM32WB55. ST's FUS keeps
AES keys for the application in flash that CPU1 cannot read (AN5185, "Customer key
storage"), and the AES1 peripheral can be handed a key that CPU1 can use but not read
back. `furi_hal_crypto_enclave_load_key(slot, iv)` sends `FUS_LOAD_USR_KEY`, the FUS
writes the key into AES1's key registers and closes them, and the firmware runs AES-CBC
on data it supplies.

**The key store** (`stm32wb55_fus.c`) holds a master key at index 0 and up to 100 others.
A key is written once, at the next free index, and never changed or removed.

| SHCI command | |
|---|---|
| `FUS_STORE_USR_KEY` 0xFC58 | type, size, key (and a 12-byte IV) → status, index. A master key goes to index 0, once. A simple or encrypted key gets the next index from 1. An encrypted key is AES-GCM under the master key with its tag left off, which is CTR starting at counter block IV‖2 |
| `FUS_LOAD_USR_KEY` 0xFC59 | index → the key into AES1. Refused for the master key, a locked key and an empty index |
| `FUS_UNLOAD_USR_KEY` 0xFC5E | AES1's key registers zeroed and opened again |
| `FUS_LOCK_USR_KEY` 0xFC5D | no more loads of that index until the next reset |

The store is a text file on the host (`devices/NAME/enclave.keys`, machine option
`enclave=FILE`, SoC property `fus-keys`): one `INDEX TYPE BITS HEX` line per key, kept
decrypted, mode 0600, rewritten through a temporary file after every key stored. A file
that exists is taken as it is, an empty one included. A file that doesn't exist is filled
by the board's provisioning hook. A file that doesn't parse stops QEMU with
`FILE:LINE: message`, because starting with part of a key store would hide the mistake.

**AES** (`stm32wb55_aes.c`) models AES1 and AES2 from RM0434 chapter 24: AES-128/256 in
ECB, CBC, CTR and GCM/GMAC, a block per four writes of `DINR`, computed at once, with
`CCF` (and the interrupt, if enabled) up when the fourth word is in.
- Byte order: a `DINR`/`DOUTR` word is four bytes of the block as a big-endian number,
  swapped by half-words, bytes or bits for `DATATYPE` 01/10/11. `KEYR7` (`KEYR3` for a
  128-bit key) holds the key's first four bytes, `IVR3` the IV's, and `IVR0` is the
  block counter of CTR and GCM.
- Key derivation (`MODE` 01) replaces `KEYR` with the last round keys of the schedule.
  Decryption in `MODE` 10 expects them there, and with a plain key the result is as
  wrong as on the chip; `MODE` 11 derives on the fly.
- CBC keeps its chain in `IVR` across `EN` going down, so a message can be fed in parts.
- GCM runs its four phases (`GCMPH`): H = E(K, 0) on enabling, header blocks into
  GHASH, payload as CTR with the ciphertext into GHASH, then the block of lengths in
  and the tag out. `NPBLB` strips the padding of a short last block when encrypting.
- Secure mode: when the FUS loads a key, `KEYR` reads as zero and ignores writes until
  the key is unloaded. The rest of the peripheral works as before.
- An RCC reset (`AHB2RSTR.AES1RST`, `AHB3RSTR.AES2RST`) clears every register, the key
  included. The firmware resets AES1 after every operation by turning its bus off.
- Not modelled: CCM, DMA, computation time. The suspend registers are storage.

The firmware uses AES1 in two ways (`targets/f7/furi_hal/furi_hal_crypto.c`). With
enclave keys it does CBC with `DATATYPE` 00 and hands over key, IV and data as the
32-bit words they are in memory, little-endian. Every group of four bytes is therefore
reversed on its way in and out, and what `furi_hal_crypto_encrypt()` computes is

```
ciphertext = swap(AES-256-CBC-encrypt(K, swap(iv), swap(plaintext)))     swap: reverse each 4-byte group
```

`scripts/fe_enclave.py` computes the same on the host. With keys of its own
(`furi_hal_crypto_ctr()`, `furi_hal_crypto_gcm()`) the firmware swaps the bytes itself,
and the result is standard CTR and GCM.

One thing to know when comparing with the CLI: `crypto encrypt` takes its text as a C
string, rounds the length up to whole blocks, and encrypts what its buffer holds after
the string's NUL without clearing it (`crypto_cli.c`). Those bytes are whatever the heap
had there, so the last block is the same from run to run only when the text ends one
character short of a block: 15, 31, 47… characters. The code is the firmware's, so a real
Flipper does the same. The host tool fills with zeros, and the checks use texts of those
lengths.

**The factory's part** (`flipper_enclave.c`). Flipper Devices fills the store at the
factory: the master key, ten factory keys that are the same on every Flipper, and an
empty index 11 where the firmware puts a key of the device's own the first time one is
needed. Those keys never leave a Flipper, and they are not in FlipperEmu. An emulated
Flipper is provisioned with keys of its own, the same everywhere and public by
construction:

```
key N = SHA-256("FlipperEmu enclave key N")      N = 0 (master), 1 … 10
```

The firmware checks its enclave with a known-answer test
(`furi_hal_crypto_enclave_verify()`): one block encrypted with each factory key,
compared with a ciphertext in the firmware's source. The desktop runs it at every start
and shows "Secure Enclave is damaged" if a key fails. `info device` reports its two
counts, and `furi_hal_crypto_enclave_ensure_key()` goes by the first of them: it makes
the device's own key only when all ten factory slots hold a key that loads.
Only the real keys give those ten ciphertexts. So the board gives the FUS a second hook:
when the FUS loads one of FlipperEmu's own factory keys, it hands AES1 the firmware's
IV, input and expected output for that index along with the key, and AES1 answers that
one CBC block from the table. Every other block, with every key, is plain AES. A factory
slot that holds some other key gets no such answer, and fails the test as a wrong key
does on hardware. The ten ciphertexts say nothing about the real keys that the
firmware's source doesn't already say.

What this gives and what it cannot:
- The firmware's enclave paths run as on a device: the self-test, `crypto` in the CLI,
  key storage, the device's own key (U2F makes it and keeps its counter file under it).
- Nothing an emulated Flipper encrypts is secret.
- What a real Flipper's factory keys encrypted cannot be read: the stock Sub-GHz
  keystore decrypts to noise (the firmware logs a `Failed to load line` error per line
  and has no manufacturer codes), and so does the stock U2F certificate's private key.

AN5185 leaves several things open, and no real device was there to ask. They are
decided as follows, and marked in the source:
- The FUS writes only AES1's key registers. `KEYSIZE` and the rest of `AES_CR` are the
  application's. The firmware always selects 256 bits, so a 128-bit enclave key works
  as an AES-256 key of 16 zero bytes followed by the key, and that is what the host
  tools compute too.
- A key goes into the key registers in the order its bytes were stored: the first four
  are `KEYR7` (`KEYR3` for a 128-bit key), each word a big-endian number, which is how
  the manual writes a key. Nothing inside the emulator depends on that choice, since the
  host tools compute the same way. A real Flipper given the same key of one's own might
  produce another ciphertext.
- The first key's index when there is no master key is 1.
- An encrypted key's cipher follows the master key's size (AES-256 for a 256-bit
  master key; the note only says "AES-128 GCM").
- Unloading a different index than the loaded one unloads all the same. Locking an
  empty index, or the key now loaded, is accepted.
- In the AES model: `WRERR`/`RDERR` details, writes to `KEYR`/`IVR` ignored while `EN`
  is set, and `NPBLB` applying to every payload block written while it is set (the
  peripheral cannot know which block is the last).

`scripts/enclave_check.py` tests the model against published vectors (FIPS 197,
SP 800-38A, the GCM specification's test cases) under qtest, driving the registers in
the firmware's order, and then the whole enclave through a firmware's CLI: the
self-test, `crypto encrypt`/`decrypt` against the host's computation, a Sub-GHz
keystore of made-up codes that the firmware encrypts with key 1 and loads back
(`subghz encrypt_keeloq`), key storage and its refusals, the device's own key,
persistence, a key of one's own in a factory slot, a blank and a malformed store.

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

**Hanging up.** When the chardev's client goes, the order matters. What it wrote and the
device has not taken yet still goes out, for half a second at most; then DTR and RTS
drop; what is left after that is thrown away, and nothing is taken from the chardev in
between. The firmware is the reason. `cli_vcp.c` reads a bulk OUT packet when the
endpoint's interrupt comes and a session exists, and at no other time. A packet that
arrives after DTR dropped is acknowledged by the interrupt handler and never read; with
both buffers of the double-buffered endpoint in software's hands the endpoint NAKs from
then on, in every later session, until the device is reset. So no byte is sent to a
port that is closed. The other direction has a smaller case: the firmware arms one IN
packet at a time, and one may be left armed for the client that went. The host takes it
and drops it before it raises DTR for the next client. Unplugging the cable empties the
queue as well.

What the model cannot shorten is the firmware's own end of a session. On DTR low its CLI
service joins the shell's thread, and a command that does not look at its pipe
(`storage read_chunks`, `storage tree`) runs to its end first, each write waiting 100 ms
for a reader that is gone. Until then the service takes no events: opening the port
gives no prompt, and each open and close queues two more events in its 8-entry queue
(see "Bus time" below for what the ninth does).

**Bus time.** A full-speed bulk transaction (token, data, handshake and the gaps between
them) takes about 103 + 8n bit times at 12 Mbit/s: 51 µs for 64 bytes. The host
completes one no sooner than that after the CPU arms the endpoint, or after the bus
last carried a transaction.

Firmware also depends on being fast next to the bus. The CLI's CDC service (`cli_vcp.c`)
queues one event per completed packet, from USB_LP, in an 8-entry queue. Its thread
takes one event per pass, but it also re-arms the endpoints from two other callbacks
(the shell read from its pipe; the shell wrote to it). A packet that completes between
such a re-arm and the next pass leaves one more event queued, and the ninth fails a
`furi_check()` in the interrupt. A real STM32 finishes the pass in a few microseconds,
while the next packet takes 51 µs. An emulated CPU can be a hundred times slower, and
slower still on a busy host.

So a bulk endpoint gets its next transaction when the firmware has caught up with the
last one: the USB interrupt is not pending and the CPU sleeps in WFI. The idle thread
does that only when every other thread waits, so the CDC service waits too, with its
queue empty. A packet that is delivered raises the interrupt, so one look at an idle CPU
gives the firmware one packet. Nothing tells the host when the CPU goes idle, and a look
while it is still at work costs it time (the main loop takes the lock that the CPU's
register accesses need). So the host looks as long after the re-arm as that took lately.
That is an estimate per endpoint: a look that found the CPU at work lengthens it by an
eighth and one that found it idle shortens it by as much, so it settles at the median.
The next look comes a quarter of it later, and 50 µs at least.

Firmware that never sleeps (a thread that polls, a busy loop) gets the clock instead.
The host measures the firmware's turnaround: from a bulk packet's completion to the CPU
re-arming that endpoint, averaged, each sample capped at 2 ms. The transaction comes when
twice that has passed since the re-arm, and 1 ms at least. Interrupt endpoints get no
such wait: the host polls them every bInterval, as a PC does, because Bad USB drops a
report that the host has not taken within two intervals.

**USB/IP export** (`usbip` chardev, a TCP server, default port 3240). The device can be
handed to the PC's kernel instead of the built-in class drivers:
- Before import, OP_REQ_DEVLIST and OP_REQ_IMPORT are answered from the descriptors the
  built-in host read.
- vhci-hcd handles SET_ADDRESS itself and forwards everything else as CMD_SUBMIT URBs.
  The built-in host keeps the bus-level jobs (reset, address).
- Control URBs run through the same control-transfer engine.
- Bulk and interrupt URBs queue per endpoint and direction, and complete as the firmware
  supplies or takes data: short packet, full buffer, ZLP flag, STALL → −EPIPE.
- UNLINK removes a pending URB without a RET_SUBMIT. For non-ISO URBs, start_frame and
  number_of_packets are echoed back, since the protocol document's prose and examples
  disagree on them.
- When the firmware drops off the bus, the socket closes, so the PC sees an unplug. When
  the client disconnects, the device gets a fresh reset back on the built-in host.

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
- Update DMA requests (UDE) and output-compare toggle at update (CCRx = 0). While these
  are in use, each update is processed at its exact time. The pin toggles, the request
  pulses, and the DMA (which runs synchronously) writes as if at that instant. Its write
  to the timer is the request's acknowledgement, so exactly one beat happens per update.
  This is furi_hal_subghz's transmitter: a circular DMA buffer feeds ARR, CH2 toggles
  GDO0, and ARR = 0 blocks the counter to end the transmission. Pacing, as on the receive
  side, isn't done yet. A guest lagging by more than half the DMA buffer (several ms)
  would get stale durations from it.

Added for 125 kHz RFID (next section):
- External clock mode 2 (SMCR.ECE), and mode 1 on ETRF, counting a clock the board puts
  on ETR (`wb55_tim_set_ext_clock`). With no clock there the counter stands still.
- PWM channels (OCxM 110/111) report both edges of every period to the board, with
  exact timestamps. The falling edge is reported as the period starts, so it can be
  ahead of virtual time.
- When no timer interrupt is enabled (DMA only), updates are replayed in batches every
  1 ms rather than as one QEMU timer event each, because a PSK tag reprograms the timer
  every 16 µs. A register access first replays the updates that are due. Their DMA then
  writes back into the timer from inside its own MMIO handler, so TIM2's region opts
  out of QEMU's re-entrancy guard (`tim_write` is written for it).
- A notifier (`cap_read`) fires when the firmware reads the last pending capture, so
  paced edge sources need not poll.
- A write to CNT made while a capture is being serviced (its flag already cleared, CCRx
  not read yet) restarts the counter at that capture's time. On silicon the RFID capture
  ISR's `LL_TIM_SetCounter(0)` runs within a microsecond or two of the edge, but here it
  can run much later.

The DMA model gained one silicon behaviour for it: when several channels select the same
request, each rising edge owes every one of them a beat, even after the first beat has
made the peripheral drop the request. RFID emulation feeds TIM2 ARR and CCR3 from two
channels on TIM2_UP, and the IR transmitter TIM1 RCR and CCMR2 from two on TIM1_UP.

Timer update requests are also pulses: on silicon the DMA acknowledges one with its first
transfer, whatever that transfer touches. So for TIM1_UP and TIM2_UP only that per-edge
latch makes a channel ready, never the level of the line. RFID field detection shows why:
TIM1's update copies TIM2's counter to RAM through two circular channels that never touch
TIM1. Treated as a level, they beat a million times per update. QEMU's main loop then
spent about a second on each 100 ms update and fell further behind for good.

`furi_hal_infrared` uses exactly that for receiving. A falling edge (a mark starts) is
captured into CCR2, and a rising edge into CCR1, which also resets the counter. CC3 is
the end-of-signal timeout.

The IR receiver (`/machine/ir`) drives PA0 and TIM2 TI1, idling high and going low for a
mark. Writing durations to its `rx` property plays them. On silicon the capture
interrupt is serviced long before the next edge. Here the main loop can fire late, and
the vCPU can lag: virtual time follows the host clock even while the vCPU thread is
descheduled. So each edge waits until the firmware has *read* the previous capture
register (up to 250 ms). The flag alone isn't enough, because the ISR clears it before
reading CCRx. Timestamps keep the measured durations exact: 200/200 NEC frames decode in
a soak test. `scripts/fe_ir.py` builds signals with a port of the
firmware's common encoder (pulse distance/width, Manchester) and its per-protocol data
layouts.

## Infrared transmit (`flipper_ir.c`, TIM1)

TIM1 is now the full timer model too, with its repetition counter (16-bit RCR) and its
four interrupt vectors. `furi_hal_infrared` makes the carrier with PWM 2 on CH3N (the
IR LED, PB9). Two DMA channels on TIM1_UP feed it at every update event:
- one loads CCMR2, switching OC3M between PWM 2 (a mark) and forced inactive (a
  space);
- the other loads RCR, the number of carrier periods until the next update.

Two UG events prime the pipeline (UG raises the update DMA request unless URS is set).
The board's `update_hook` sees each update at its exact time and passes mark or space
to `/machine/ir`. When the firmware stops TIM1, the transmission is written as a raw
`.ir` file with the carrier frequency and duty.

Those DMA channels run in normal mode, and an ISR re-arms them between two updates. The
last buffer gives it one carrier period (26 µs). So an update whose DMA channels have
not been re-armed yet waits for them, keeping its exact time (`wb55_dma_ready_for`, up
to 250 ms). This is the same idea as the receive side's pacing on capture reads.

## 125 kHz RFID (`flipper_rfid.c`, `stm32wb55_comp.c`)

**Reading.** TIM1 drives the antenna on CH1N (PB13). It runs at 125 kHz and 50 % duty
for ASK and FSK tags, or at 62.5 kHz and 25 % duty for PSK tags; the antenna tank still
rings at 125 kHz, and the comparator then sees a PSK tag's phase. The tag's load
modulation, demodulated by the analog front end, reaches COMP1 on PC5. TIM2's TI4,
remapped to COMP1 by TIM2_OR, captures its edges: CH3 on falling (indirect) and CH4 on
rising (direct). The ISR restarts the counter on every rising edge, so the firmware
measures pulse and period.

COMP1/2 are now modelled (`stm32wb55_comp.c`). The analog side is one boolean per
comparator, which the board sets with an exact time ("input+ above input-"). EN,
POLARITY, the VALUE bit, LOCK and EXTI lines 20/21 are modelled.

`/machine/rfid` watches TIM1's registers. If CEN, MOE and CC1NE are set with PWM at
100–150 kHz, there is an ASK field; at 50–75 kHz, a PSK field. A tag in the field is
what COMP1 sees: one frame of +high/−low µs, repeated for as long as the field powers
it. A `psk:` frame only answers a PSK-mode read.

Its edges are paced like the IR receiver's: an edge waits until the firmware has read
the previous capture. The `cap_read` notifier resumes the tag from inside the ISR's
read, with no polling. Any delay is added to the tag's own timeline (`shift_ns`) instead
of being caught up in a burst. The firmware's worker decodes in a thread with about
200 ms of buffers, so bursts overran it on FSK tags (~28k edges/s). Each edge keeps its
exact timestamp on that timeline, so every captured duration is exact.

After a read, the next edge also waits 3x the measured capture latency (capped at 8x
the tag's own gap). Here the capture and comparator ISRs take ~73 µs per edge, more
than an FSK tag's 32 µs. Without that gap the ISRs run back to back and the decoding
thread starves (BRINGUP_LOG entry 30). `qom-get /machine/rfid stats` shows the
pacing counters and TIM2's capture state.

With no tag answering, the field carries noise. The firmware depends on it:
`lfrfid_worker` only checks its 2 s ASK/PSK switch timeout after a buffer of edges
arrives, so a silent field would keep `rfid read` in ASK mode forever and never find a
PSK tag. The noise is 17–40 µs spikes (past the worker's 16 µs filter) between gaps of
150–1500 µs. It is too irregular to decode, and its pulse/period ratio is under the 0.2
the worker takes for a card.

**Emulating.** TIM2 counts the reader's carrier on ETR (PA15) in external clock mode 2.
Two DMA channels on TIM2_UP load ARR and CCR3 every period, and CH3 in PWM mode switches
the antenna load (PA2, RFID_PULL). A virtual 125 kHz reader (`reader`, on by default)
puts the carrier on ETR and records CH3's edges in carrier cycles. After 100 ms without
an edge it writes the capture to `devices/NAME/rfid-tx/tx-NNNNN.txt`.

**Writing a T5577** (`t5577.c`). `rfid t5577` puts a T5577 in the field instead of a fixed
tag. Its downlink is the reader's field itself: `t5577_write()` pauses TIM1's outputs
(MOE) for gaps, timed with `furi_delay_us()` on DWT->CYCCNT, which counts virtual time.
So the gaps reach `flipper_rfid_set_drive()` with exact virtual timestamps, and the tag
decodes them as a T5577 does:
- a start gap after enough field powers it up and opens a command;
- each stretch of field between two gaps is a bit (24 field clocks a 0, 56 a 1);
- a long stretch closes the command.

The decoder accepts what the firmware sends (opcode, optional password, lock bit, 32 data
bits, 3-bit address). Its windows are wide, because an emulated reader's gaps run long
when the host is busy. The EM4305 command that follows each write starts with a 0 bit,
which makes it an opcode 0x that a T5577 ignores. Password mode and lock bits are
honoured.

The tag then answers as block 0 says: blocks 1..MAXBLK, in Manchester, bi-phase,
differential bi-phase, direct, FSK1/2(a) or PSK1/2/3, at RF/8–RF/128, or RF/(2n+2) in
extended mode (Keri). The waveforms follow the firmware's encoders, which its T5577 write
requests are made for: FSK as `fsk_osc`'s whole cycles, PSK1 on an RF/2 subcarrier.
`rfid_check.py` writes every protocol the CLI can name, through the firmware's own
write-and-verify. All 23 passed on the first run. Radio Key, whose name the CLI cuts at
the space, passed through the app's Write: all 24 protocols write.

**Field detection.** `furi_hal_rfid_field_detect_*` counts the reader's carrier on TIM2's
ETR. TIM1 at 1 kHz copies the count to RAM every 100 ms by DMA and clears it. The virtual
reader's carrier (`reader`, on by default) is that field, so the Flipper always sits on a
125 kHz reader until `rfid --reader off`. Momentum's NFC/RFID detector app reads
125.00 kHz.

**Encoders.** `scripts/fe_rfid.py` ports the firmware's protocol encoders (`lib/lfrfid`)
with a Python `bit_lib`. Their output is the emulation waveform, in carrier cycles. The
comparator sees it unchanged for ASK and FSK (verified: the inverted Manchester does not
decode), and as the phase for PSK. `scripts/rfid_check.py DEVICE` checks every encoder
both ways against a running Flipper:
1. `rfid emulate`, captured by the virtual reader, must be the same loop as the Python
   encoder, cycle for cycle.
2. The Python tag must make `rfid read` print the protocol and data.

`capture_frame()` turns any capture into a tag by finding its repeating frame. So a
protocol only a fork has can still be read back: emulate it, then hold the capture to a
reading Flipper.

## iButton (`flipper_ibutton.c`)

The iButton pad is PB14, open drain with a pull-up. `/machine/ibutton` holds one key on
it, from `scripts/fe_ibutton.py` (a `.ibtn` file, or a new key).

**Dallas keys** are a 1-Wire slave. The firmware is the master
(`lib/one_wire/one_wire_host.c`): with interrupts off it drives PB14 low, busy-waits on
DWT->CYCCNT, and samples the pin. The SoC overlays each GPIO port's registers with a
region that passes every access through to QEMU's model and tells the board about IDR
reads and about MODER/ODR/BSRR/BRR writes. So the slave follows the master's own drive,
not the pin level, which its own pulls would mask, with virtual times:
- A long low is a reset. The slave answers with a presence pulse, and a standard-length
  reset also returns it to standard speed.
- Every other low is a slot. The master writes a 1 as a 9 µs low and a 0 as 64 µs (1 and
  8 µs in overdrive). An emulated master's lows only ever stretch: overdrive's 1 µs takes
  2–4 µs. So the threshold sits just under a 0's time (56 µs, 7 µs), not at the midpoint.
- To send a 0, the slave pulls the line low at the master's falling edge. On silicon it
  lets go after a fixed time, well after the master samples. Here the host can preempt
  the vCPU while virtual time runs on, so the sample can land past any fixed time. So the
  slave lets go once the master has sampled: a read of GPIOB's IDR with the firmware's
  own drive of PB14 off, reported after its value is taken. The master's next falling
  edge also ends a hold (the master gave up on a byte mid-way), and a 1 ms timer lets go
  anyway.

The slave answers SEARCH ROM, READ ROM, MATCH ROM, SKIP ROM and the overdrive ROM
commands. Memory keys also answer READ MEMORY: a 2-byte address on a DS1992 or DS1996
(the firmware reads a DS1996's 8 KB at overdrive speed), and one byte on a DS1971.

**Blanks** are 1-Wire keys whose ROM can be written, as the firmware writes the four it
knows (`lib/ibutton/protocols/blanks`):
- RW1990.1: `D1` and a 0 bit enable writing, `D5` and 64 inverted bits write the ROM, and
  `D1` with a 1 bit locks it again;
- RW1990.2: the same, with `1D`, a 1 bit to enable, and plain bits;
- TM01x: `C1` and a 1 bit, then `C5` and 64 plain bits;
- TM2004: `3C` and a 2-byte address, then per byte the byte, its CRC back, a programming
  pulse, and the byte as programmed back.

Each blank answers only its own protocol, and the firmware tries them in that order,
verifying each with READ ROM. QEMU's
STM32L4x5 GPIO model needed a fix to let a device release an open-drain line
(`qemu/patches/0004`).

**Cyfral and Metakom keys** modulate their current on the contact. The Flipper senses
that through the RFID front end, with COMP1 on and the 125 kHz field off. While the
firmware has COMP1 enabled with no field (`en_changed`), such a key plays its waveform
into COMP1. These edges are timed in software: the COMP ISR reads COMP1's VALUE, then
DWT->CYCCNT. On silicon the ISR runs within a fraction of a microsecond. The emulated
one runs tens of microseconds later, by a varying amount, which turned 41 µs segments
into 82 µs ones and produced wrong keys with valid parity. So:
- the ISR's read of COMP1 (`val_read`) stamps the next CYCCNT read with the edge's time
  on the key's own timeline (`wb55_dwt_stamp_next`), as the hardware would have read it;
- each edge waits until the ISR has read the previous one, so two edges never merge into
  one interrupt;
- the timeline resyncs if delivery falls 1 ms behind it.

Both kinds then read in about half a second.

**Emulation** turns this round: the Flipper is the key.
- A Dallas key is the firmware's 1-Wire slave (`lib/one_wire/one_wire_slave.c`). It
  catches a reset by EXTI, then busy-polls PB14 with interrupts off, timing each wait on
  DWT->CYCCNT. The virtual reader (`reader`; `./flipperemu ibutton --reader`) is a
  1-Wire master paced by that polling, through the same IDR and write reports:
  - a slot starts at the slave's second read while it waits for one;
  - a 1 is written by letting go at the read that saw the slot. A 0 is written by
    holding on until 30 µs after the slave's next read: its 20 µs window starts between
    the two, so a hold measured from the earlier read could end inside it after a host
    stall;
  - a read slot's bit is the slave's own drive: a 0 is a write low, then a release.
    The slave makes no IDR read in between (it waits on DWT), so a read there means it
    is not sending.

  It sends READ ROM, then READ MEMORY for a memory key, and saves a `.ibtn` file. The
  slave reads DWT before the pin in each wait, so a host stall at the wrong moment
  makes it miss a slot, or take a reset and answer it with a presence pulse. Either way
  the reader sees a read where a bit should be, or nothing. It then resets the slave,
  and SKIP ROM / READ MEMORY resume from the last whole byte. The 4 bytes before it are
  read again and must match, so a misheard address is caught. A mismatch drops those
  bytes as well, so a bad byte that got kept is read again, not checked against forever.
  A DS1996's 8 KB takes about 5 s, with a few resumes.
- A Cyfral or Metakom key is TIM1's update ISR (`furi_hal_ibutton_emulate_*`). It sets
  the next period in ARR, then the level PB14 holds for it. The board takes each
  BSRR/BRR write of PB14 while TIM1's update interrupt is enabled, with ARR, as one
  level (`flipper_ibutton_emulated`). Sampling at the update events instead fails:
  the guest's ISR runs 20–80 µs late (a host thread waking the vCPU from WFI), and
  TIM1 meanwhile repeats the old period with the old level. A capture
  (`ibutton-tx/tx-NNNNN.txt`) is written once it holds 4096 levels (about 0.3 s, while
  the key emulates on), or when the emulation stops and PB14 is no longer an output.
  An idle timer would be cut short by a host stall of the vCPU, so it is only a 1 s
  fallback. `fe_ibutton.py` takes the frame that repeats most often in the capture,
  and the launcher puts it back on the contact.

## NFC (`st25r3916.c`, `nfc_card.c`)

The ST25R3916 is modelled one frame at a time:
- the SPI protocol (register spaces A and B, FIFO load and read, passive-target
  memory, direct commands);
- the 512-byte FIFO with its byte count and the bits of an incomplete last byte;
- the four clear-on-read interrupt registers driving the IRQ pin (PA2).

A TRANSMIT command turns the FIFO (NUM_TX_BYTES bits) into a frame on the air: a bit
stream, LSB first, with an odd parity bit after each full byte. With
ISO14443A_NFC.no_tx_par set, the FIFO already holds the parity bits, as MIFARE
Classic's encrypted parity needs. The card in the field answers with a bit stream,
which is decoded back into the FIFO (raw with no_rx_par) and checked for parity and,
unless AUX.no_crc_rx is set, CRC. The interrupts follow 106 kbit/s: TXE, then RXS and
RXE. The answer starts 40 µs after TX end rather than the real ~90 µs. That keeps RXS
well inside the firmware's frame waiting time (1172+500 fc, counted by TIM1), so the
chip's interrupt never races the timer interrupt that would declare a timeout. The NFC
stack also needs TIM17 (block-TX guard time, one-pulse), now a real timer sharing its
vector with TIM1's trigger and commutation interrupts.

`nfc_card.c` works on those air bits:
- ISO14443-3A: power-up with the field, REQA/WUPA, anticollision and select over
  cascade levels, HLTA, and the IDLE/READY/ACTIVE/HALT states.
- Type 2 commands, with AUTH0/PROT access control and a NAK that sends the tag back
  to IDLE.

MIFARE Classic uses Crypto1, written from its published description (Garcia et al.,
"Dismantling MIFARE Classic", 2008). It is a plain 48-bit LFSR with the paper's
feedback taps and its filter built from the fa/fb/fc boolean functions. Keys, nonces
and data enter in transmission order, LSB first. The card's nonces come from the
16-bit PRNG (x^16+x^14+x^13+x^11+1), advanced one step per bit time since the field
came on, as on silicon, so they pass the firmware's "weak PRNG" test. Authentication
runs the three passes:
- the card sends nT, plain, or encrypted for a nested AUTH;
- the reader's {nR} is fed back into the LFSR;
- {aR} must equal suc^64(nT), and {aT} = suc^96(nT).

After that every frame is encrypted, parity bits included (each one with the next
keystream bit). READ, WRITE and HALT obey the sector trailer's access bits, by the
MF1S50 tables, and Key A always reads back as zeros.

**Listen mode** (the Flipper emulating a card). The firmware sets the chip's target
mode and loads its passive-target memory: the UID (`AUX.nfc_id` selects 4 or 7 bytes),
the ATQA at bytes 10 and 11, and a SAK per cascade level from byte 12. A real ST25R3916
then runs anticollision and select by itself and reports only the outcome (I_wu_a). The
virtual reader (`qom-set /machine/nfc reader scan|ntag`) does the same:
- its field raises I_eon, and AUX_DISPLAY's efd_o;
- it takes the UID, ATQA and SAK from that memory and raises I_wu_a;
- each later command goes into the FIFO with its CRC, behind I_rxs and I_rxe;
- what the firmware transmits (TRANSMIT_WITHOUT_CRC, with the CRC in the FIFO) is the
  answer.

GOTO_SLEEP after a HLTA, and GOTO_SENSE, are tracked. The `field` property is a reader's
field with no reader talking: it raises I_eon/I_eof and holds efd_o, which is all NFC
field detection (`furi_hal_nfc_field_is_present`) reads. The reader waits up to 100 ms for
an answer. It sends a command up to three times when the answer is missing or garbled
(a NAK is final), and leaves 1 ms between an answer and its next command: an emulated
CPU on a busy host can miss a command that comes right on its heels. At the end the
field goes off (I_eof) and the reader writes what it read as a `.nfc` file. A MIFARE
Classic emulation answers through transparent mode, bit-banged on a GPIO with Crypto1's
own parity bits, which is not decoded.

`scripts/fe_nfc.py` converts a Flipper `.nfc` file into the card description (newer
files store ATQA MSB first, and this swaps it back). It can also make a new NTAG with
an NDEF URL record, or a factory MIFARE Classic, and write an NTAG as a `.nfc` file for
the Flipper to emulate (`--nfc-file`).

## Storage

The SD card is QEMU's `ssi-sd` + `sd-card-spi` (with two fixes, see BRINGUP_LOG), behind
CS PC12. The board holds card detect (PC10) low when an image is attached. `make_sdcard.py`
builds FAT32 images from official resources (`.tgz`), update packages (heatshrink
`resources.ths` or `.tar.gz`) or a directory, using mtools, so no root is needed. Current
firmware keeps `/int` on the card as `/ext/.int`.

A card that is pulled out (the machine's `sd-card=off`, the launcher's `sd eject`) is
gone from the bus as well, not only from the detect pin: the firmware looks at that pin
when its storage service has nothing else to do, so in the middle of a transfer the
card's silence is what it notices. The slot is a second SPI peripheral on PC12 beside
the card
(`flipper-sd-slot`). With no card in it, the card's own chip select stays high and the
slot answers 0xFF, which is the idle level the firmware's driver reads as "no answer".
A card that is pushed in is reset first, as one that just got power, so the firmware's
mount begins with a card in its idle state.

The launcher's `ls`, `push`, `pull`, `rm` and `mkdir` (`scripts/fe_files.py`) have two
ways to that card, and take the one that cannot disagree with the firmware. While the
device runs, the firmware has the filesystem mounted and its FAT in memory, so the
image must not be written from outside: the commands go through the firmware's own
`storage` CLI on the USB serial port (`list`, `tree`, `stat`, `md5`, `mkdir`, `remove`,
and `write_chunk` / `read_chunks` for the bytes). When it is stopped, nothing has the
image open, and mtools reads and writes it directly. Both are the same six operations
behind one interface, and `scripts/files_check.py` holds them to the same results.

A file goes to the firmware in pieces of 1024 bytes, each announced by its own
`storage write_chunk PATH N` and sent when the firmware answers `Ready`. The firmware
reads a piece whole before it writes it, so when a write fails (a full card, a card
that was pulled out) no bytes of the file are left on the line for its shell to read as
commands. It comes back in pieces of 8192 (`read_chunks`), each asked for with a `y`.
The port is one line with one reader: a command holds `devices/NAME/usb.lock` (an
flock with its holder's name in it) for as long as it has the CLI.

Between two pieces of a write the firmware is at its prompt, so that is where a `push`
that gets Ctrl-C stops, and removes what it wrote. `read_chunks` has no such place: once
begun, the firmware sends the file to its end, and a port that is closed on it does not
end the command (see "Hanging up" under USB). So a `pull` that gets Ctrl-C keeps asking
for the pieces and drops them.

## Board

- **ST7567**: 132×65 RAM, page/column addressing, SEG/COM direction, start line, inverse,
  all-on, display on/off, contrast. The panel's top row is COM63, which makes the
  firmware's normal mode (A0/C8) upright and its left-handed mode (A1/C0, x offset 4)
  rotated 180°.
- **BQ27220**: standard commands, MAC subcommands, unseal/full-access keys, CFG_UPDATE,
  and a data memory written with the firmware's own checksum protocol. A fresh gauge is
  provisioned by the firmware once (~2 s), then persisted in `nvram.bin`. QOM properties
  `soc`, `voltage-mv`, `current-ma`, `temperature-dk` and `capacity-mah` set the battery
  (and `-global bq27220.soc=…` the one a run starts with; they survive a reset). SYSDWN
  rises under 3.15 V and falls over 3.40 V: off USB, the firmware then counts down and
  switches the Flipper off.
- **BQ25896**: register file with ADC readings derived from the gauge; property `usb`
  plugs in a charger. BATFET_DIS in REG09 is how the firmware switches the Flipper off:
  with nothing on VBUS the board powers off (it says `power 0` on the panel link and
  QEMU exits); while USB power is there it keeps running, and goes off when the cable
  does.
- **LP5562**: register file. W PWM is the backlight and R/G/B the status LED, both drawn
  next to the LCD.
- **CC1101**: status byte, header/burst/strobe framing, config and status registers,
  MARCSTATE, PATABLE, and GDO0/GDO2 driven from IOCFG (self-test passes). Receive: a
  signal written to `/machine/subghz`'s `rx` property (+high/−low µs at a frequency, or
  `file:PATH` for long captures) plays on "the air". With IOCFG0 = 0x0D (async serial
  data), GDO0 follows the carrier while the chip is in RX and tuned within 500 kHz, and
  RSSI reads −40 dBm (−100 dBm otherwise). GDO0 is PA1 and TIM2 TI2, so
  `furi_hal_subghz_start_async_rx` captures it the same way the IR receiver path does,
  with the same pacing. Transmit: in TX, GDO0 is the data input. The model records it
  from TIM2's output compare with exact timestamps. On leaving TX it writes a `.sub` RAW
  file (frequency and a custom preset from the live registers and PATABLE) to `tx-dir`,
  and keeps it as the `tx` property. A real radio can also be the antenna: see below.
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
- **The microSD slot**: the machine property `sd-card` pulls the card out and pushes it
  back, at run time: card detect says so, and the firmware leaves a card that is not
  there alone (pushed back, it is the same image); `usb-cable=off`
  starts a run with the USB cable unplugged. The speaker's `mute` silences it without
  the firmware knowing.
- **The log**: the firmware logs to USART1, QEMU's first serial port. The launcher gives
  its chardev a `logfile` (`devices/NAME/uart.log`, appended to by every QEMU of a run),
  so the log is kept whatever `--serial` connects the port to. How much the firmware
  logs is the low nibble of RTC BKP1R (`FuriLogLevel`; 0 is its default, info), which
  it reads once, when it starts. The machine property `log-level` is that nibble by
  name: `-M flipper-zero,log-level=debug` sets it before the first instruction, and
  `qom-set /machine log-level …` for the next start; reading it gives what the
  firmware's own Settings wrote. `scripts/fe_log.py` reads the log for `flipperemu
  log` and for the window: lines, levels, the colours the firmware left on, and where
  it started again (a timestamp that goes back).

## The front panel (`flipper_panel.c`, `scripts/fe_gui*.py`)

The window that looks like a Flipper is a program of its own, `scripts/fe_gui.py`
(PySide6), on a link of the same kind as the radios': a line protocol on a chardev,
`devices/NAME/panel.sock` (docs/PANEL_LINK.md). The `flipper-panel` device sends it the
LCD as 1024 bytes of pixels, the backlight, the LED, the vibration motor and the
speaker's tone, each when it changes, and the QOM properties it asks to watch; it takes
button presses and releases. Nothing of it runs while no program is connected.

- **Frames**: a 16 ms tick on the realtime clock reads the ST7567's pixels and sends
  them when they changed. While the firmware is writing the display RAM (its byte count
  moved since the last tick) the tick looks again 3 ms later, up to five times, so a
  frame goes out whole. While more than 64 KB wait to be sent, no frames go: a slow
  window gets the latest one.
- **Buttons**: the panel's presses go to the same pins as the keyboard of QEMU's window
  and the monitor's `sendkey`, and the last of them decides. A press lasts at least
  40 ms of virtual time, because the firmware's input service debounces; a release that
  comes sooner is deferred on a virtual-clock timer. When the window goes, the buttons
  it held are released.
- **Properties**: watched paths are read every 100 ms with `object_property_get_qobject`
  and sent as JSON when it changed. The window changes nothing through the link: it
  runs the launcher's commands, which go through the monitor, and sees the result come
  back.
- **Power**: when the board powers off (above) the panel says `power 0` before the
  guest shutdown. The launcher's `Session` started that QEMU; the window keeps the
  battery's last values and what the launcher held to the Flipper, and starts a new
  QEMU with them (`-global bq27220.*`, `usb-cable=off`) when Back is held or USB is
  plugged in.

The window itself (`fe_gui_view.py`) draws a body of FlipperEmu's own in QPainter, in
units of one LCD pixel, so it scales without blurring; the LCD's colours follow the
backlight. `fe_gui_link.py` is its end of the link, `fe_gui_widgets.py` the bench, the
dialogs, the console and the firmware log (which is `uart.log`, read as it grows: it
does not come over the link), and `fe_gui.py` the menus and what ties them together.

## Real radios: the CC1101's link (`flipper_rf.c`, `scripts/fe_sdr.py`)

The CC1101 has a chardev, `link` (`devices/NAME/subghz.sock`), with a line protocol on it
([RADIO_LINK.md](RADIO_LINK.md)). The model announces its receive set-up whenever a
register write or a strobe changes it: state, frequency, modulation, bandwidth, data
rate, deviation, OOK decision boundary. The firmware loads a preset one register at a
time, so the announcement waits for the writes to settle. Each change bumps an epoch, and
anything queued or still in flight for an older one is dropped.

The other end answers with two things:
- **A timeline for GDO0**: what the chip would demodulate, as `+high/-low` µs. The model
  queues it in a ring and plays it 60 ms behind arrival, so chunks join up. Each edge
  goes through the same TIM2 capture pacing as the one-shot air's, with its exact
  timestamp. A backlog is made up in long silences; a dry queue drops the line low.
- **The band's power**, as dBm per bin. RSSI is the sum over the bins inside the chip's
  current channel, so the firmware's reads are answered at once for any frequency and
  bandwidth. That matters because the Frequency Analyzer spends 2 ms per frequency, and
  the hopper decides on one read right after retuning. A channel off the band reads
  thermal noise for its bandwidth (−167 dBm/Hz with the noise figure; −108.9 dBm at
  650 kHz). The `rssi` property shows the reading.

`fe_sdr.py` is such a bridge for an SDR. It reads IQ from `rtl_tcp` (the protocol
directly, so no librtlsdr is needed and the dongle can be remote), from a HackRF through
libhackrf (ctypes), or from a recording or stdin.
- **Channel.** An overlap-save FFT filter cuts the CC1101's channel out of the band. Each
  frame's spectrum gives up the bins around the channel's offset, shaped by a
  raised-cosine passband of the chip's bandwidth, and an inverse FFT of just those bins
  is the decimated baseband. The offset is a multiple of 4 bins, so with frames 3N/4
  apart the phase runs on across them, which FSK needs.
- **OOK.** The smoothed power (1/16 bit) is sliced at the chip's decision boundary below
  the peak (`boundary`, 8 dB in the Flipper's presets), with hysteresis. At the midpoint
  instead, a real transmitter's ripple (a beat with another carrier) broke highs up,
  and the KeeLoq capture failed. Below a 2×boundary SNR it slices halfway in dB.
- **2-FSK.** The phase step between samples, smoothed over a quarter bit, against the
  transmitter's own centre (tracked per burst). A burst is gated by power over its
  midpoint, whose edges come half a window late like the discriminator's, so a burst
  neither starts early nor trails a glitch.
- **Squelch.** The floor is each frame's median power, falling at once and rising slowly,
  and the peak falls slowly; the squelch opens 12 dB over the floor. A low percentile
  failed on faint 8-bit noise, which is a few levels of quantisation.
- **Edges and spectrum.** Runs become µs with the fractions carried on, so the timeline
  keeps time with the samples. Runs shorter than half the smoothing are merged away as
  glitches. Every 50 ms a Welch spectrum goes out, normalised so its median bin is the
  noise floor, with the DC spike patched and the edges' roll-off left out.
- **Tuning.** The SDR sits a quarter of its rate above the channel, clear of its DC
  spike. When the firmware stays off the band for 0.5 s, it retunes, and the samples
  still buffered from before are skipped. Hopping and the analyzer move faster than any
  SDR retunes, so they just read the band the SDR is on.

## A card from a program: the ST25R3916's link (`st25r3916.c`, `scripts/fe_nfc_link.py`)

The ST25R3916 has a link too (`devices/NAME/nfc.sock`, the same line protocol). While no
card is set on the device and a program is connected, each frame the firmware sends as a
reader goes out on it (`poll`). That happens where the built-in card would be asked: at
the end of the transmission. The frame is decoded from its air bits into data bits and
the parity bit after each byte, so MIFARE Classic's encrypted parity goes through as
sent. The answer comes back the same way and joins the normal receive path: RX start,
then the FIFO as the chip is set up, raw or decoded, CRC checked. `silence` lets the
firmware's no-response timer run out.

The answer can't be timed against the firmware. The firmware's frame waiting time is
from 0.1 ms to tens of milliseconds, and a Python card, let alone a USB reader, misses it.
So each poll stops the machine, and the virtual clock with it, until the answer comes
(`vm_stop`, then `vm_start`), or a second has passed in host time. The firmware sees
every answer at its usual 40 µs. The stop has to come from a bottom half. The poll is
made inside a virtual-clock timer, and `vm_stop` there deadlocks: `pause_all_vcpus`
waits for that clock's running timers to finish, the caller among them, while the vCPU
waits for the BQL.

`fe_nfc_link.py` has the card's layers: ISO14443-3A activation over 4, 7 or 10-byte UIDs
(REQA/WUPA, anticollision and select per cascade level, HLTA), and ISO-DEP (RATS/ATS,
I-blocks with chaining, R- and S-blocks, APDUs to a method). Its `iso-dep` card is read
by the NFC app as an ISO14443-4A card, and the shell's `apdu` reaches it through the
firmware's ISO-DEP stack.

### Real cards: a Proxmark3 on the link (`scripts/fe_pm3.py`)

The `pm3` link card relays every Type A frame to a Proxmark3 (Iceman/RRG firmware) over
its own USB protocol. Frames are `PM3a` commands and `PM3b` replies: a preamble, the
data and a postamble. With the postamble's magic in place of a CRC, which USB allows,
no client or library is needed.
- **Two firmware generations.** On 2026-08-29 `CMD_HF_ISO14443A_READER` moved from MIX
  arguments (flags; length and bit count; timeout) to an NG payload, `iso14a_raw_cmd_t`,
  and gained `ISO14A_NO_PARITY`. The relay asks with a too-short NG payload. New
  firmware answers it with an error; old firmware reads its absent MIX arguments as
  zero and stays silent.
- **New firmware: exact.** The frame goes out as its air bits, data and parity
  interleaved LSB first, which `CodeIso14443aBitsAsReaderPar` sends as they are when
  no parity array is given. The answer comes back raw, every bit packed eight to a
  byte. The count is lost, but a k-byte answer is 9k bits, so ⌊8n/9⌋ of n raw bytes
  recovers it, and one raw byte can only be a 4-bit ACK/NAK. MIFARE Classic's encrypted
  parity therefore survives both ways.
- **Old firmware: bytes.** The Proxmark3 adds odd parity, and an answer's parity is
  recomputed.

The field follows the firmware's (`ISO14A_CONNECT | ISO14A_NO_SELECT |
ISO14A_NO_DISCONNECT` on, `CMD_HF_DROPFIELD` off). The `mode` on each poll keeps the
firmware's ISO14443B, FeliCa and ISO15693 probes off the 14A reader. `fe_pm3.py sim` is
a Proxmark3 on a pty, for both generations, with an fe_nfc_link card in its field; the
relay's checks run against it.

`fe_nfc.py` also reads the client's JSON dumps (`mfcard`, `mfc v2`, `mfc v3`, `mfu`) into
card descriptions, or Flipper `.nfc` files, so a card the Proxmark3 dumped can sit in
the emulated field.

## Persistence per device (`devices/NAME/`)

| file | real-world equivalent |
|---|---|
| `flash.img` | the STM32WB55 internal flash |
| `sd.img` | the microSD card |
| `nvram.bin` | coin-cell / gauge state: BQ27220 data memory, RTC calendar offset, BKP0R–BKP19R |
| `enclave.keys` | the FUS's key store on CPU2: the secure enclave's keys (text, one key per line) |
| `held.json` | (while it runs) the names of what the launcher held to it, for `status` and the window |
| `uart.log`, `uart.log.1` | what a serial adapter on pins 13 and 14 would have recorded: the firmware's log of this run, and of the one before |
| `firmware.elf` | symbols for gdb (copied when flashing an ELF) |
