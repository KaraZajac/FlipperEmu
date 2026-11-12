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
A source tree that was set up before a patch came (0006 to 0009: 2026-11-11; 0010 and
0011: 2026-11-12) gets it from one more run of that script.

How the script finds out what a tree has: it takes the patches out of a copy of the
files they touch, the last one first, and what does not come out there is what the tree
lacks. Asking each patch by itself is not enough: 0008 builds on 0007 in the same file,
so 0007 alone no longer comes out of a whole tree. What is to be applied is tried on a
second copy first, so a patch that does not fit leaves the tree as it was.
Two patches to one file need not be neighbours there (0003 and 0011 in `hw/sd/sd.c`):
a tree that has the later one and lacks the earlier gets the earlier some lines from
where its header says, and no backup file is left of that.

**Until 2026-11-11 the script did ask each patch by itself**, and a run on a tree that
had 0008 applied 0007 a second time, in part, and stopped before the build. Such a
tree has `hw/char/stm32l4x5_usart.c.rej` in it. The script names it and does not try
to mend it: remove `third_party/qemu-11.1.3` and run the script again (it extracts the
tarball beside it, applies every patch and builds, about 6 min).

`scripts/setup_check.py` checks the script on stand-in trees (the patched files alone,
from the tarball; nothing is built): a new tree, a second and a third run, a tree from
before each patch, a tree that lacks one patch, the damaged tree, a file that is not
this QEMU's, and last that the tree here has every patch and nothing else in those
files. 32 checks in ten seconds. A patch that is added to this folder is in them
without a change to the check.

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

**Licence:** these patches modify QEMU's own source files, which are GPL-2.0-or-later,
so the patches are GPL-2.0-or-later too, unlike the rest of this repository (BSD-3-Clause).
