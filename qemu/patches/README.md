# Fixes to upstream QEMU

`scripts/setup_qemu.sh` applies these to the QEMU 11.1.3 source tree once (it checks
whether each patch is already applied, so a later run adds only the new ones, then
builds). Each patch header explains the bug, how it showed up with the Flipper firmware,
and the fix. All are small and self-contained, written to be suitable for upstream
submission. 0005 is less a bug than a limit: the machine builds and runs without it, and
its 1-Wire timing is then as it was (about one iButton read in twenty goes wrong).
0007 is an addition more than a fix: without it the machine runs, and its serial ports
receive as they did (a byte a millisecond at any baud rate, nothing by DMA).
A source tree that was set up before a patch came (0006 and 0007: 2026-11-11) gets it
from one more run of that script.

| patch | QEMU file | bug |
|---|---|---|
| 0001 | `hw/timer/armv7m_systick.c` | clock-update callbacks lack `return`, so a refclk change re-times SysTick even when CLKSOURCE selects the CPU clock |
| 0002 | `hw/sd/ssi-sd.c` | after a response the model eats one byte, swallowing the next command when the host clocks its padding with CS high |
| 0003 | `hw/sd/sd.c` | SPI mode only reached transfer state via CMD9/CMD10; data commands right after ACMD41 were rejected |
| 0004 | `hw/gpio/stm32l4x5_gpio.c` | a device on an open-drain line could pull it low but never let it go: an external high was refused, disconnected on the next MODER/OTYPER write, and read as floating |
| 0005 | `accel/tcg/cputlb.c` | TCG takes the big lock for every device register access, also where the region asked for lockless I/O: a guest that busy-waits on a counter register sleeps 20 to 50 µs on the lock whenever the main loop has it |
| 0006 | `chardev/char.c` | a chardev with a `logfile` logged a byte at once when its backend could not take it yet (EAGAIN), and again when it was written: every byte that had to wait was in the log twice |
| 0007 | `hw/char/stm32l4x5_usart.c` | reception lacked what a driver uses: no request to a DMA channel (and no byte-wide register access for it), no IDLE flag, no PRESC register, the LPUART's baud rate by the USART's formula, and bytes taken at the main loop's pace instead of the baud rate |

**Licence:** these patches modify QEMU's own source files, which are GPL-2.0-or-later,
so the patches are GPL-2.0-or-later too, unlike the rest of this repository (BSD-3-Clause).
