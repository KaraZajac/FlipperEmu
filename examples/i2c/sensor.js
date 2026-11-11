// Copyright (c) 2026, Kara Zajac
// SPDX-License-Identifier: BSD-3-Clause
//
// The I2C bus of the header, pins 15 (SDA) and 16 (SCL): a TMP105
// temperature sensor at 0x48, and an EEPROM at 0x50 that counts the times
// this script ran.
//
//     ./flipperemu run --i2c tmp105@0x48,temperature=23.5 --i2c eeprom@0x50
//     ./flipperemu push examples/i2c/sensor.js /ext
//     ./flipperemu cli "js /ext/sensor.js"
//     ./flipperemu i2c 0x48 temperature 30.25        (and the script again)
//
// The firmware's i2c module has an address as it is on the wire, shifted
// left by one: 0x48 is 0x90.  Momentum, Unleashed and RogueMaster have the
// module; the official firmware's JS has none.
let i2c = require("i2c");
let SENSOR = 0x48 << 1;
let EEPROM = 0x50 << 1;

if (!i2c.isDeviceReady(SENSOR, 50)) {
    print("no sensor at 0x48");
} else {
    // register 1, its configuration: 12 bits (it starts with 9)
    i2c.write(SENSOR, [1, 0x60], 50);
    // register 0: the temperature in 1/256 of a degree, the high byte first
    let t = Uint8Array(i2c.writeRead(SENSOR, [0], 2, 50));
    let raw = t[0] * 256 + t[1];
    if (raw >= 32768) {
        raw -= 65536;
    }
    print("temperature:", raw / 256);
}

if (!i2c.isDeviceReady(EEPROM, 50)) {
    print("no EEPROM at 0x50");
} else {
    // its byte 0: how often this ran (a new EEPROM has 0xFF everywhere)
    let e = Uint8Array(i2c.writeRead(EEPROM, [0], 1, 50));
    let runs = e[0] + 1;
    if (e[0] === 255) {
        runs = 1;
    }
    i2c.write(EEPROM, [0, runs], 50);
    print("run number:", runs);
}
print("sensor done");
