#!/bin/sh -e
# The example app, tested on a Flipper of its own: by what it logs and by what
# its screen shows.  Run it here, in the app's folder, as ufbt is:
#
#     ./test.sh FIRMWARE [DEVICE]
#
# docs/DEVELOPING.md, "A test that runs by itself", goes through it.
[ -n "$1" ] || { echo "usage: $0 FIRMWARE [DEVICE]" >&2; exit 2; }
fe() { ../../flipperemu -d "${device:-hello-test}" "$@"; }
device=$2

fe run --headless --audio none --serial null -f "$1" &
trap 'fe stop' EXIT

fe wait                                           # the firmware is up
fe wait --log 'FeHello.*started' -- ufbt launch   # build, install, start: its line
fe wait --screen screens/start.txt                # ... and its first screen
fe wait --log 'which makes 2' -- press up down
fe wait --screen screens/two.txt
fe wait --log 'left after 2' -- press back
echo "test.sh: the app does what it should"
