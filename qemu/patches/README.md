# Fixes to upstream QEMU

`scripts/setup_qemu.sh` applies these to the QEMU 11.1.3 source tree once (it finds
out which of them the tree has, so a later run adds only the new ones, then
builds). Each patch header explains the bug, how it showed up with the Flipper firmware,
and the fix. All are small and self-contained, written to be suitable for upstream
submission. 0005 is less a bug than a limit: the machine builds and runs without it, and
its 1-Wire timing is then as it was (about one iButton read in twenty goes wrong).
0007 is an addition more than a fix: without it the machine runs, and its serial ports
receive as they did (a byte a millisecond at any baud rate, nothing by DMA).
0008 is an addition too: without it the machine runs, and a byte that comes while a
port's receiver is off is dropped and no more (no start bit on the RX pin, so the
firmware's expansion service never sees a module call).
0009 lifts a limit: without it the machine runs, and `run --i2c eeprom@…` is refused
by QEMU unless the EEPROM's size is a multiple of 512 bytes.
0010 is an addition: without it the machine runs, and a Winbond flash on the header's
SPI bus (`run --spi`) answers the read-ID commands 90h and ABh with zeros.
0011 is a fix that the machine runs without: a saved machine state cannot be loaded
then (QEMU ends in an assertion of its SD card model).
0012 is a fix that the machine runs without: an EEPROM of more than 256 bytes on the
header's I2C bus (`run --i2c eeprom@…,rom-size=4096`) then gives a firmware that sets
one address byte of two before a read a byte from outside the EEPROM's memory.
0013 is an addition: without it the machine runs, and an EEPROM on the header's I2C bus
is as after a reset in a loaded machine (its address counter at 0, a write that was
under way lost).
0014 and 0015 are needed by the machine since 2026-11-14 (CPU2's SRAM2: the flash model
calls `armv7m_nvic_set_pending_async()`, and the launcher reads the NVIC's `lockup`);
without them it does not build.
0016 is an addition: without it the machine runs, and `status` and a fuzzer's crash
report have no place of a fault.
0017 is an addition: without it the machine runs, and what the firmware sends on a
serial port is there at once at any baud rate.
0018 is an addition: without it the machine runs, and what is wired to a serial port
has the firmware's baud rate, whatever that is (`uart --line-baud` says so).
0019 is needed by the machine since 2026-11-21 (its clock leaves out what an access to
a device takes its model beyond 20 µs, and reads how long accesses took from
`tcg_mmio_ns`); without it it does not build.
0020 is needed by the machine since 2026-11-21 too (the CPU's thread runs the machine's
due timers through its two hooks, `-machine cpu-timers`); without it it does not build.
0021 and 0022 are fixes that the machine runs without: reading the card then costs the
host a sixth more of its time for each.
A source tree that was set up before a patch came (0006 to 0009: 2026-11-11; 0010 to
0013: 2026-11-12; 0014 and 0015: 2026-11-14; 0016 to 0018: 2026-11-15; 0019 to 0022:
2026-11-21) gets it from one more run of that script.

How the script finds out what a tree has: it takes the patches out of a copy of the
files they touch, the last one first, and what does not come out there is what the tree
lacks. Asking each patch by itself is not enough: 0008 builds on 0007 in the same file,
so 0007 alone no longer comes out of a whole tree. What is to be applied is tried on a
second copy first, so a patch that does not fit leaves the tree as it was.
Patches to one file need not be neighbours there (0003, 0011 and 0021 in `hw/sd/sd.c`):
a tree that has a later one and lacks an earlier gets the earlier some lines from
where its header says, and no backup file is left of that. `hw/nvram/eeprom_at24c.c` has
three (0009, 0012, 0013).

**Until 2026-11-11 the script did ask each patch by itself**, and a run on a tree that
had 0008 applied 0007 a second time, in part, and stopped before the build. Such a
tree has `hw/char/stm32l4x5_usart.c.rej` in it. The script names it and does not try
to mend it: remove `third_party/qemu-11.1.3` and run the script again (it extracts the
tarball beside it, applies every patch and builds, about 6 min).

`scripts/setup_check.py` checks the script on stand-in trees (the patched files alone,
from the tarball; nothing is built): a new tree, a second and a third run, a tree from
before each patch, a tree that lacks one patch, the damaged tree, a file that is not
this QEMU's, and last that the tree here has every patch and nothing else in those
files. 55 checks in about thirty seconds. A patch that is added to this folder is in
them without a change to the check.

| patch | QEMU file | bug |
|---|---|---|
| 0001 | `hw/timer/armv7m_systick.c` | clock-update callbacks lack `return`, so a refclk change re-times SysTick even when CLKSOURCE selects the CPU clock |
| 0002 | `hw/sd/ssi-sd.c` | after a response the model eats one byte, swallowing the next command when the host clocks its padding with CS high |
| 0003 | `hw/sd/sd.c` | SPI mode only reached transfer state via CMD9/CMD10; data commands right after ACMD41 were rejected |
| 0004 | `hw/gpio/stm32l4x5_gpio.c` | a device on an open-drain line could pull it low but never let it go: an external high was refused, disconnected on the next MODER/OTYPER write, and read as floating |
| 0005 | `accel/tcg/cputlb.c` | TCG takes the big lock for every device register access, also where the region asked for lockless I/O: a guest that busy-waits on a counter register sleeps 20 to 50 µs on the lock whenever the main loop has it |
| 0006 | `chardev/char.c` | a chardev with a `logfile` logged a byte at once when its backend could not take it yet (EAGAIN), and again when it was written: every byte that had to wait was in the log twice |
| 0007 | `hw/char/stm32l4x5_usart.c` | reception lacked what a driver uses: no request to a DMA channel (and no byte-wide register access for it), no IDLE flag, no PRESC register, the LPUART's baud rate by the USART's formula, and bytes taken at the main loop's pace instead of the baud rate |
| 0008 | `hw/char/stm32l4x5_usart.c` | a byte that came while the receiver was off was dropped without a word: on hardware its start bit is still a falling edge on the RX pin, which a board may wait for with an interrupt. A line `rx-unheard` tells the board |
| 0009 | `hw/nvram/eeprom_at24c.c` | an EEPROM with a backing file wanted the image's length to be its own size, and the block layer gives lengths in whole 512-byte sectors: no file could back an EEPROM smaller than a sector (a 24C02's 256 bytes), or one that ends within a sector |
| 0010 | `hw/block/m25p80.c` | the read-ID commands 90h and ABh were answered for SST's flashes alone; Winbond's W25X and W25Q have both (90h: the maker and the device's ID, the device first from address 1; ABh: the device alone), and a host that tells a flash by them read zeros |
| 0011 | `hw/sd/sd.c` | the load of a saved state powers the card up first, with a function that asserts the card is not powered up yet; a card in SPI mode is, from its reset on, so every load into a machine with an SD card on SPI ended QEMU in that assertion |
| 0012 | `hw/nvram/eeprom_at24c.c` | with two address bytes, a write that ended after the first left the address counter as it was shifted, not reduced to the EEPROM's size: the next read took its byte from up to 60 KiB past the end of a 4096-byte EEPROM's memory (QEMU's heap, one byte for each such write) |
| 0013 | `hw/nvram/eeprom_at24c.c` | the EEPROM saved no state: in a loaded machine its address counter was 0, the bytes of a write that was under way at the save were lost (as was all that had been written to an EEPROM without a backing file), and the bus did not have it as the device of a transfer under way |
| 0014 | `hw/intc/armv7m_nvic.c`, `include/hw/intc/armv7m_nvic.h` | every fault was synchronous: a board's memory that refuses a write the bus took (an imprecise BusFault, asynchronous on the chip) could only raise a precise one, escalated at once, and a Lockup where it came inside the HardFault handler. `armv7m_nvic_set_pending_async()` pends such a fault: not escalated for its priority, a disabled one a pending HardFault, never a Lockup |
| 0015 | `hw/intc/armv7m_nvic.c`, `include/hw/intc/armv7m_nvic.h` | a Lockup (a fault that cannot be taken) ended QEMU with `cpu_abort()`; on the chip the processor stops until it is reset and the rest runs on. Now it stops so (powered off, as PSCI does, until a reset), and the NVIC's `lockup` property says where and why |
| 0016 | `hw/intc/armv7m_nvic.c`, `include/hw/intc/armv7m_nvic.h` | nothing kept where a fault was: a guest's handler seldom says (the Flipper's prints a message and its own registers, and resets). The NVIC notes each fault as it is pended: the exception, the PC and LR it was raised at, the fault status and addresses, and whether it is imprecise; its `last-fault` property has that as JSON, with a count, over a reset too |
| 0017 | `hw/char/stm32l4x5_usart.c`, `include/hw/char/stm32l4x5_usart.h` | a character written to TDR was the backend's at once, with TXE and TC set again before the write returned: a guest sent any amount in no time, and one that waits for TC before it switches the UART off never waited. The transmitter has its shift register now: a character is on the line for a frame's time at the baud rate, TXE when TDR is free, TC when a frame ends with nothing behind it |
| 0018 | `hw/char/stm32l4x5_usart.c`, `include/hw/char/stm32l4x5_usart.h` | the line's other end had no baud rate: a guest with the wrong one for its peripheral was understood all the same. A property `line-baud` gives it one; a character is then taken as a UART at the receiver's speed samples the sender's frame (the same character within a few percent, otherwise other characters, more or none, and framing errors), in both directions |
| 0019 | `accel/tcg/cputlb.c`, `include/exec/cputlb.h` | nothing said how long a vCPU is in the model of a device: one access is one instruction of the guest's, and some take a model a millisecond (a write that starts a DMA transfer of a block, moved before it returns). With the virtual clock the host's, that is the guest's time, and timers that ran out in it are late. With `tcg_mmio_timed` set, `tcg_mmio_ns` is what every access under the big lock took beyond `tcg_mmio_free_ns`: for a machine that keeps a clock of the CPU's own running, to leave out. Nothing is timed for a machine that does not set it |
| 0020 | `accel/tcg/cputlb.c`, `include/exec/cputlb.h`, `accel/tcg/tcg-accel-ops-mttcg.c`, `accel/tcg/tcg-accel-ops-rr.c` | without -icount the virtual clock's timers are the main loop's alone, and a main loop that the host keeps waiting runs them late while the vCPU runs on. Two hooks for a machine that runs them on the vCPU's thread: `tcg_mmio_before`, before every device access under the big lock (the access the last instruction of its block), and `tcg_cpu_exit_hook`, where a vCPU's thread has left cpu_exec, with the big lock (a machine's signal to that thread with an exit request gets it there). Nothing is called for a machine that does not set them |
| 0021 | `hw/sd/sd.c` | the card asked `blk_is_inserted()` for every byte it moved and every command, and `ssi-sd` asked again through `sdbus_get_inserted()` for every byte; that is a co_wrapper, which outside a coroutine makes one and polls it to its end. The card keeps the answer: asked when it is realized, on every change of the medium (its `change_media_cb`) and before a state is loaded |
| 0022 | `accel/tcg/translate-all.c`, `accel/tcg/translator.c`, `accel/tcg/internal-common.h` | a device access that was not the last instruction of its block had `cpu_io_recompile()` leave the block and run the access alone, every time the block came by (a guest polling a device in a loop paid it each time round). The place is kept and the block translated again; a block ends after an instruction at a kept place, so the access is its last |

**Licence:** these patches modify QEMU's own source files, which are GPL-2.0-or-later,
so the patches are GPL-2.0-or-later too, unlike the rest of this repository (BSD-3-Clause).
