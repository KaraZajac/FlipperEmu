// Copyright (c) 2026, Kara Zajac
// SPDX-License-Identifier: BSD-3-Clause
//
// The SPI bus of the header, pins 2 (MOSI), 3 (MISO) and 5 (SCK): a serial
// flash, a W25Q32, with its chip select on pin 4.  The script reads the
// flash's IDs, erases a sector, writes seven bytes there and reads them
// back.  They are kept: its next run finds them.
//
//     ./flipperemu run --spi flash
//     ./flipperemu push examples/spi/flash.js /ext
//     ./flipperemu cli "js /ext/flash.js"
//     ./flipperemu spi                    (what the firmware clocked there)
//
// Momentum, Unleashed and RogueMaster have the spi module; the official
// firmware's JS has none.  The module's bus is the firmware's own handle for
// the header: 2 MHz, mode 0, the chip select on pin 4.
let spi = require("spi");

function hex(buf) {
    let a = Uint8Array(buf), s = "";
    for (let i = 0; i < a.length; i++) {
        s += (i ? " " : "") + (a[i] < 16 ? "0" : "") + a[i].toString(16);
    }
    return s;
}

// a command and its answer under one chip select: acquire() takes the bus
// and pulls pin 4 low, release() lets go of both
function ask(out, n) {
    spi.acquire();
    spi.write(out);
    let r = spi.read(n);
    spi.release();
    return hex(r);
}

// 03h and three bytes of address: read from there, here the sector at 0x1000
print("kept:", ask([0x03, 0x00, 0x10, 0x00], 8));
// 9Fh: the JEDEC ID.  ef is Winbond, 40 16 a W25Q32
print("jedec:", ask([0x9f], 3));
// 90h, two bytes of anything and an address: Winbond's older ID, the maker
// and the device (ef 15).  writeRead() clocks as many bytes in as it sends
// out, so the answer's first four are what came back under the command
print("id90:", hex(spi.writeRead([0x90, 0, 0, 0, 0, 0], 100)));
// 06h allows one write.  20h erases the 4 KiB sector at that address.  A
// write() by itself is a transfer of its own, with the chip select
spi.write([0x06]);
spi.write([0x20, 0x00, 0x10, 0x00]);
print("erased:", ask([0x03, 0x00, 0x10, 0x00], 8));
// 02h: program a page from that address on, here with "Flipper"
spi.write([0x06]);
spi.write([0x02, 0x00, 0x10, 0x00, 0x46, 0x6c, 0x69, 0x70, 0x70, 0x65, 0x72]);
print("written:", ask([0x03, 0x00, 0x10, 0x00], 8));
print("spi done");
