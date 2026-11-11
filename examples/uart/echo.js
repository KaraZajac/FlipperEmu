// Copyright (c) 2026, Kara Zajac
// SPDX-License-Identifier: BSD-3-Clause
//
// A serial port on header pins 15 (TX) and 16 (RX): says "ready", answers
// each line that comes in, and closes the port at a line that says "bye".
//
//     ./flipperemu push examples/uart/echo.js /ext
//     ./flipperemu cli "js /ext/echo.js" &
//     ./flipperemu uart wait ready
//     ./flipperemu uart send "hello\n" --expect "you said: hello"
//     ./flipperemu uart send "bye\n"
//
// (or with a program of yours on the device's lpuart.tty.)  readln() gives
// undefined for an empty line (the \n after a \r) and when nothing came in
// its time.
let serial = require("serial");
serial.setup("lpuart", 115200);
serial.write("ready\r\n");
let turns = 0;
while (turns < 400) {
    turns++;
    let line = serial.readln(2000);
    if (line === undefined) {
        continue;
    }
    if (line === "bye") {
        break;
    }
    serial.write("you said: " + line + "\r\n");
}
serial.write("closing\r\n");
serial.end();
print("echo done");
