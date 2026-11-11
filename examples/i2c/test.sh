#!/bin/sh -e
# The sensor app, tested on a Flipper of its own that has the sensor and the
# EEPROM on its header: by what it logs and by what its screen shows, while
# the temperature is changed from outside.  Run it here, in the app's folder,
# as ufbt is:
#
#     ./test.sh FIRMWARE [DEVICE]
#
# docs/DEVELOPING.md, "A sensor on the header's I2C bus", goes through it.
[ -n "$1" ] || { echo "usage: $0 FIRMWARE [DEVICE]" >&2; exit 2; }
fe() { ../../flipperemu -d "${device:-sensor-test}" "$@"; }
device=$2

fe run --detach --headless --audio none -f "$1" \
    --i2c tmp105@0x48,temperature=23.5 --i2c eeprom@0x50
trap 'fe stop' EXIT

fe wait                                           # the firmware is up
fe wait --log 'FeSensor.*started, run number [0-9]' -- ufbt launch
fe wait --log 'FeSensor.* 23\.50 C'               # what the sensor started with
fe wait --screen screens/warm.txt
fe wait --log 'FeSensor.* -7\.50 C' -- i2c 0x48 temperature -7.5
fe wait --screen screens/cold.txt                 # ... and the screen follows
fe wait --log 'FeSensor.*left' -- press back
echo "test.sh: the app reads what the sensor has"
