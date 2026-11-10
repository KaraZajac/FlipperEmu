# Has anyone already built a Flipper Zero emulator?

*Prior-art survey, 2026-11-07. Repositories were inspected through the GitHub API
(READMEs, design docs, file trees, commit history); none of the surveyed projects' code
was run. Links below go to the originals.*

## Short answer

**Yes, and most of it happened in the last four months.** Until mid-2026 there were only
"UI simulators" that recompile apps for the desktop. Since July 2026, three projects have
appeared that run the **real, unmodified firmware binary** on an emulated STM32WB55:

| Project | Engine | Boots stock FW to desktop? | Licence | Activity |
|---|---|---|---|---|
| [vincenzo-esposit0/dryflash-FZ](https://github.com/vincenzo-esposit0/dryflash-FZ) | own Rust Cortex-M4 interpreter → WASM (browser) + Node CLI | **Yes**: Official 1.5.1, Momentum mntm-012, Unleashed unlshd-094 and RogueMaster in a nightly CI matrix (desktop, apps, OTA) | GPL-3.0+ | created 2026-10-31, very active |
| [d4rks1d33/Flipper-Zero-Emulator](https://github.com/d4rks1d33/Flipper-Zero-Emulator) | patched Renode 1.16.1 + C#/IronPython models + PySDL2 window | **Yes (self-reported)**: desktop, dolphin animation, SD read/write, OTA `.tgz` install, USART console on TCP | **none** (all rights reserved) | stale since 2026-07-10 |
| [tITAZ314/flipper-emu](https://github.com/tITAZ314/flipper-emu) | Renode 1.17 + C# plugin + Tk window, Windows-only | **Was** reaching the home screen; **currently blank screen** (regression), SD writes unfinished | GPL-3.0+ | last push 2026-10-24 |

**Nobody has built one on QEMU**, and none of these offers a native, fast, debugger-first
workflow (GDB stub plus ELF symbols, snapshots, instruction tracing). That gap is what
FlipperEmu fills (see "Recommendation" below).

For a quick check of a build without installing anything, dryflash's hosted app runs in
the browser's sandbox: <https://app.dryflash.workers.dev>. Drop a `.dfu` or
update `.tgz` on the page.

---

## 1. Full-system emulators (run the real firmware binary)

### 1.1 dryflash-FZ: the most complete (Rust/WASM, GPL-3.0+)

- "Try any Flipper Zero firmware in your browser before you flash it." Executes release
  `.dfu` / update `.tgz` on an instruction-level STM32WB55 model; also a headless Node
  runner (`dryflash run`, YAML scripts, JUnit, screenshots) and a GitHub Action that
  smoke-tests firmware PRs.
- **CPU**: hand-written ARMv7E-M interpreter (Thumb-2, DSP, FPv4-SP, PMSAv7 MPU,
  exceptions). It decodes all 219,464 instructions in fw 1.5.1. It is differentially
  fuzzed against Unicorn (522,079 identical steps), and its authors measured 150–220 MIPS
  native/browser. With the default STOP2 sleep path the firmware drops to ~45 MIPS (no WFI
  fast-forward), so they ship the "Legacy" sleep setting.
- **Peripherals**: register files generated from ST's SVD; real models for RCC, GPIO, EXTI,
  I2C, SPI, USART, RTC, HSEM, IPCC, RNG, LPTIM1/2, FLASH (program/erase plus option bytes),
  DMA and IWDG. **Board devices**: ST7567, BQ27220, BQ25896, LP5562, SD card (FAT32
  builder), CC1101 and ST25R3916 (init-only stubs), OTP provisioning, speaker and vibro.
- **CPU2** is a behavioural ST FUS with no wireless stack, so BLE is unavailable but the
  firmware's own paths work (ADR-0012).
- Crash reports are symbolised against the release ELF, with hang and boot-loop detection.
- **Missing (their roadmap)**: USB CDC CLI (M7, in progress), IR/LED/battery controls, RF
  signal injection (`.sub` → CC1101, `.nfc` → virtual tag; M8 stretch), BLE. No GDB.
- Positions itself honestly as a *smoke test*, not hardware certification.

### 1.2 d4rks1d33/Flipper-Zero-Emulator: Renode, broad but unlicensed

- Stock `firmware.bin` boots to the desktop, SD mounts and reads/writes, buttons work, and
  the USART1 log is served on `telnet localhost 3456`. It can **install a firmware `.tgz`
  through the stock updater**, with flash persisted to `firmware/flash.img`.
- Needs a **patched Renode** (MappedMemory made file-backed). The repo ships prebuilt
  patched Renode binaries plus 182 MB of Renode source and firmware blobs.
- **No licence file**, so its code can't be reused. Running the prebuilt binaries from an
  unknown author is also a supply-chain risk. Its `DIAGNOSIS.md` is an excellent record of
  hardware behaviours the firmware depends on (summarised in §4).
- Gauge and charger are absent (the firmware tolerates it); BLE is faked only during OTA.

### 1.3 tITAZ314/flipper-emu: Renode, careful but stalled

- Same goal, built from scratch on Renode 1.17 with C# models (EXTI, GPIO, HSEM, RNG, LPTIM,
  DWT, DMA, ST7567, SD) and generated platform files. Windows/PowerShell, Python 3.9.
- README admits a **current blank-screen regression** and unfinished SD writes.
- Its HSEM model treats an `RLRx` read as a passive "view", but **on STM32 the read *is* the
  1-step lock**. That is why it needed a "boot chain lock mask" and per-semaphore hand-overs.
  It's a warning to model from the reference manual (RM0434), not from observed accesses.
- Credits d4rks1d33 as conceptual reference only (no code taken, because of the missing
  licence).

### 1.4 Also found (minor or not real)

- [ZDStudios/Flipper-Zero-Emulator](https://github.com/ZDStudios/Flipper-Zero-Emulator)
  `RENODE.md`: a Renode scaffold that loads the image but doesn't reach the UI.
- [apfxtech/flipper-engine](https://github.com/apfxtech/flipper-engine): "Run Flipper Zero
  firmware on PC via Cortex-M4F emulation". **The repository is empty.**
- [wasabicover258/Flipper-Emulator](https://github.com/wasabicover258/Flipper-Emulator):
  "Universal Emulator". It is 69 MB of opaque multi-megabyte `.bat` files and `.exe`s with no
  source and no licence. **Treat it as untrusted; don't run it.**

## 2. App-level simulators (no real firmware)

| Project | What it does | State |
|---|---|---|
| Official firmware `target_lo` (2020) | Early "local" Linux target with a HAL stub; README listed "Task #22: Create Flipper Zero software emulator with display and buttons" | Removed long ago |
| [anna-oake/flipperzero_sim](https://github.com/anna-oake/flipperzero_sim) (2020) | SDL2 "UI simulator"; recompiles apps natively against a non-matching API | Prototype, abandoned |
| [Milk-Cool/flippulator](https://github.com/Milk-Cool/flippulator) (MIT, 75★) | Reimplements furi/gui/input/storage in C + SDL2 and recompiles an app for Linux | Unmaintained since Jan 2024 |
| [ZDStudios pyflipper](https://github.com/ZDStudios/Flipper-Zero-Emulator) | Python *reimplementation* of the Flipper UI and apps, with an optional ESP32/Arduino radio bridge | Toy; not the real firmware |
| [ZDStudios fapemu](https://github.com/ZDStudios/Flipper-Zero-Emulator/tree/main/fapemu) | Loads a real `.fap`, relocates it and runs its ARM code on **Unicorn**, answering ~180 firmware API calls in Python. 11/14 catalog apps playable | Interesting HLE idea; no ViewDispatcher, no threads |

These help with app UI work but can't tell you whether **a firmware build** boots, and they
don't exercise the real furi_hal or drivers.

## 3. Other routes people take

- **A Nucleo-WB55 dev board instead of a Flipper**
  ([forum](https://forum.flipper.net/t/about-run-firmware-on-nucleo-stm32wb55rn/25609)):
  real silicon, but no Flipper peripherals; the firmware checks the hardware.
- **Ask for an emulator.** The Flipper forum thread
  "[Safe way to debug while altering firmware](https://forum.flipper.net/t/safe-way-to-debug-while-altering-firmware/21512)"
  asks for exactly this; there is no official emulator. qFlipper and lab.flipper.net only
  *stream the screen* of a real device.
- NuttX has a [Flipper Zero board port](https://nuttx.apache.org/docs/12.3.0/platforms/arm/stm32wb/boards/flipperzer/index.html):
  a different OS on the same hardware, not emulation.

## 4. What the prior art proves the firmware needs from the hardware

The three full-system projects independently hit the same walls. These are facts about the
hardware and firmware, not anyone's code, and they form the checklist for FlipperEmu.

| # | Behaviour the firmware depends on | Symptom if missing |
|---|---|---|
| 1 | `DWT->CYCCNT` must advance. `furi_delay_us` busy-waits on it, including the 100 ms button-sampling delay in `main()` before the scheduler starts | hang at reset |
| 2 | Tickless idle uses **LPTIM1** as the wake timer and does `WFI` with PRIMASK=1. `RCC_APB1RSTR1.LPTIM1RST` must really reset LPTIM1 and drop its IRQ, because **no LPTIM1 ISR is registered** | `furi_check` crash in the ISR dispatcher every idle period |
| 3 | EXTI uses the **WB55 layout** (RTSR1 0x00, FTSR1 0x04, SWIER1 0x08, PR1 0x0C, C1IMR1 0x80, C1IMR2 0x90), routed by `SYSCFG_EXTICR`. The idle code checks raw `PR1`, so a stray pending bit livelocks idle | buttons dead / UI frozen |
| 4 | **HSEM 1-step lock = a read of `RLRx`** (returns `LOCK | COREID(4)`); release = write `R[x]` | `furi_check` in `furi_hal_bt_init`, RNG spin |
| 5 | IPCC: CPU2 must consume system commands (`C1TOC2SR` clears), or `HW_IPCC_SYS_SendCmd` spins ~33 s and crashes | crash in `gap_extra_beacon_init` |
| 6 | **CPU2 must be at least a FUS.** With CPU2 dead, `furi_hal_crypto_enclave_load_key` leaks its mutex (fw 1.5.1) and the desktop deadlocks. STOP2 sleep needs the wireless stack, so a stackless device should use the **Legacy sleep** flag in RTC backup registers | desktop ignores input; idle spin |
| 6a | **The FUS must hold the factory keys, and AES1 must compute.** The desktop runs `furi_hal_crypto_enclave_verify` at every start: one block encrypted with each of the ten factory keys (`FUS_LOAD_USR_KEY`, then AES-256-CBC in AES1), compared with ciphertexts in the firmware's source. U2F, the Sub-GHz keystores and `furi_hal_crypto_enclave_ensure_key` (the device's own key in slot 11) depend on it | "No Factory Keys Found / Secure Enclave is damaged" at every start; U2F shows "Certificate error" |
| 7 | SD card on **SPI2 + DMA2 ch6 (RX) / ch7 (TX)** with transfer-complete IRQ. SPI `FRLVL`/`FTLVL` must be accurate because the driver drains the RX FIFO by level. **`/int` lives on the SD (`/ext/.int`)**: no LittleFS on flash in current firmware | "DMA timeout", `FR_DISK_ERR` loops |
| 8 | BQ27220 gauge / BQ25896 charger on I2C1: a missing gauge triggers retries with **4 s** delays; SOC 0 can trigger low-battery paths | slow boot, battery warnings |
| 9 | CC1101: GDO0 (PA1) self-test follows `IOCFG0`. ST25R3916: `IC_IDENTITY & 0xF8 == 0x28` | "Init failed" (non-fatal) |
| 10 | The OTA updater runs **from SRAM1** (`MEMRMP`, VTOR), keeps its state machine in **RTC backup registers across resets**, and programs flash through the FLASH controller. It checks option bytes and the radio-stack version via SHCI | update loops / no-op |
| 11 | Buttons: Up PB10, Down PC6, Left PB11, Right PB12, Back PC13 (active-low); **OK PH3 (active-high)**. Left held at reset → DFU mode, Up held → recovery | boots into DFU splash |
| 12 | Log console is **USART1 @ 230400**; each official release also publishes a matching `.elf` (`update.flipperzero.one/builds/firmware/<ver>/flipper-z-f7-firmware-<ver>.elf`) for symbolising | — |

## 5. Recommendation for FlipperEmu

**Build it on QEMU.** It's the one engine nobody had used, and it suits firmware hacking:

- **Speed**: TCG compiles guest code to x86, so expect several times real-time for a 64 MHz
  M4, versus interpreters (dryflash) or Renode.
- **Debugging**: QEMU's built-in GDB stub (`-s -S`) plus the release `.elf`. You can set
  breakpoints in `furi_hal_*`, watch memory and single-step the scheduler.
- **Snapshots** (`savevm`/`loadvm`): boot once, then jump straight back to the desktop or
  into an app.
- **Tracing**: `-d in_asm,exec`, TCG plugins (coverage, memory traces), and `-icount` for
  deterministic record/replay.
- **Plumbing for free**: chardevs (UART to stdio, pty or telnet), GTK/SDL/VNC display,
  QMP for scripting, and `-drive` for SD and flash images.
- **Faithful flash programming.** Some custom firmwares load large apps execute-in-place:
  they *write app code into internal flash at runtime and run it there*. The emulator has
  to program and erase flash, invalidate translated code for those pages, and persist the
  image. That's first-class in the FlipperEmu design.

**Licensing**: QEMU device code is GPL-2.0-or-later, so code can't be copied from dryflash
(GPL-3.0+) or tITAZ314 (GPL-3.0+), and d4rks1d33 has no licence at all. FlipperEmu uses the
*facts* in §4 (behaviour of ST silicon and of the open-source Flipper firmware) and is
written from RM0434, the CMSIS headers and the firmware source.

The build plan, architecture and progress log live in `PLAN.md` and `docs/`.

## Sources

- dryflash-FZ: https://github.com/vincenzo-esposit0/dryflash-FZ · app: https://app.dryflash.workers.dev
- d4rks1d33: https://github.com/d4rks1d33/Flipper-Zero-Emulator
- tITAZ314: https://github.com/tITAZ314/flipper-emu
- ZDStudios (pyflipper/fapemu/Renode notes): https://github.com/ZDStudios/Flipper-Zero-Emulator
- Milk-Cool/flippulator: https://github.com/Milk-Cool/flippulator
- anna-oake/flipperzero_sim: https://github.com/anna-oake/flipperzero_sim
- apfxtech/flipper-engine (empty): https://github.com/apfxtech/flipper-engine
- wasabicover258 (untrusted binaries): https://github.com/wasabicover258/Flipper-Emulator
- Early official README with `target_lo` / Task #22: https://code.popov.link/mirrors/flipperzero-firmware/blame/commit/b6b5410bc3ae4f171fe88fd5f08ec3a3d5542b25/README.md
- Forum, debugging without flashing: https://forum.flipper.net/t/safe-way-to-debug-while-altering-firmware/21512
- Forum, firmware on Nucleo-WB55: https://forum.flipper.net/t/about-run-firmware-on-nucleo-stm32wb55rn/25609
- NuttX Flipper Zero port: https://nuttx.apache.org/docs/12.3.0/platforms/arm/stm32wb/boards/flipperzer/index.html
- QEMU STM32L4x5 machine (closest upstream SoC family): https://qemu.readthedocs.io/en/master/system/arm/b-l475e-iot01a.html
- Official firmware directory: https://update.flipperzero.one/firmware/directory.json
