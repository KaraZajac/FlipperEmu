# Fixes to upstream QEMU

`scripts/setup_qemu.sh` applies these to the QEMU 11.1.3 source tree once (it checks
whether each patch is already applied). Each patch header explains the bug, how it
showed up with the Flipper firmware, and the fix. All are small and self-contained,
written to be suitable for upstream submission.

| patch | QEMU file | bug |
|---|---|---|
| 0001 | `hw/timer/armv7m_systick.c` | clock-update callbacks lack `return`, so a refclk change re-times SysTick even when CLKSOURCE selects the CPU clock |
| 0002 | `hw/sd/ssi-sd.c` | after a response the model eats one byte, swallowing the next command when the host clocks its padding with CS high |
| 0003 | `hw/sd/sd.c` | SPI mode only reached transfer state via CMD9/CMD10; data commands right after ACMD41 were rejected |

**Licence:** these patches modify QEMU's own source files, which are GPL-2.0-or-later,
so the patches are GPL-2.0-or-later too, unlike the rest of this repository (BSD-3-Clause).
