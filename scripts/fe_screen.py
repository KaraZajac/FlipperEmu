#!/usr/bin/env python3
# Copyright (c) 2026, Kara Zajac
# SPDX-License-Identifier: BSD-3-Clause
"""The Flipper's screen as something a script can read and compare.

The LCD is 128 by 64 pixels, each on or off.  As text it is 32 lines of up to
128 characters, two pixels to a character, one above the other:

    ' ' neither     '▀' the upper one     '▄' the lower one     '█' both

`flipperemu shot --text` prints that, and a file of it is what `flipperemu
wait --screen FILE` compares the screen with.  In such a file a '?' stands
for two pixels that may be anything (a clock, the battery, an animation), a
line that ends early is off from there on, and so are lines that are not
there.

    fe_screen.py PICTURE            # a `shot` (PNG or PPM), or such a text
    fe_screen.py PICTURE REFERENCE  # ... and how many pixels differ; 1 if any
"""
import sys

W, H = 128, 64
CELLS = " ▀▄█"                          # by (upper) + 2 * (lower)
ANY = "?"
# The emulator draws its console with the LCD inside a bezel that is four LCD
# pixels wide.  Lit or not, the red of a pixel that is off is 0x3a or more and
# that of one that is on 0x24 or less (st7567.c).
BEZEL = 4
RED_ON = 0x2f


def from_rgb(data, width, height):
    """A picture of the emulator's console, three bytes a pixel -> the LCD's
    8192 pixels, row by row, 1 for one that is on"""
    scale, rest = divmod(width, W + 2 * BEZEL)
    if scale < 1 or rest or height < (H + 2 * BEZEL) * scale \
            or len(data) < width * height * 3:
        raise ValueError(f"{width} by {height}: not a picture of the "
                         "emulator's screen")
    out = bytearray(W * H)
    for y in range(H):
        row = ((BEZEL + y) * scale * width + BEZEL * scale) * 3
        for x in range(W):
            out[y * W + x] = data[row + x * scale * 3] <= RED_ON
    return bytes(out)


def from_ppm(data):
    """A PPM as QEMU's `screendump` writes it -> pixels"""
    parts = data.split(None, 4)
    if len(parts) < 5 or parts[0] != b"P6" or parts[3] != b"255":
        raise ValueError("not a PPM picture")
    return from_rgb(parts[4], int(parts[1]), int(parts[2]))


def from_text(text):
    """Text as to_text() makes it -> (pixels, which of them count: 1 or 0)"""
    lines = text.replace("\r", "").split("\n")
    while lines and not lines[-1]:
        lines.pop()
    if len(lines) > H // 2 or any(len(line) > W for line in lines):
        raise ValueError(f"more than {H // 2} lines of {W} characters: not a "
                         "screen as text")
    bits, counts = bytearray(W * H), bytearray(b"\1" * (W * H))
    for row, line in enumerate(lines):
        for x, c in enumerate(line):
            up, low = row * 2 * W + x, (row * 2 + 1) * W + x
            if c == ANY:
                counts[up] = counts[low] = 0
                continue
            cell = CELLS.find(c)
            if cell < 0:
                raise ValueError(f"line {row + 1}, column {x + 1}: {c!r} is "
                                 "none of ' ', '▀', '▄', '█' and '?'")
            bits[up], bits[low] = cell & 1, cell >> 1
    return bytes(bits), bytes(counts)


def to_text(bits):
    """Pixels -> 32 lines, each without the blanks at its end"""
    lines = []
    for row in range(0, H, 2):
        up, low = bits[row * W:(row + 1) * W], bits[(row + 1) * W:(row + 2) * W]
        lines.append("".join(CELLS[a + 2 * b] for a, b in zip(up, low)).rstrip())
    return "\n".join(lines) + "\n"


def load(path):
    """A file with a screen in it, a picture (`shot`) or text (`shot --text`)
    -> (pixels, which of them count)"""
    with open(path, "rb") as f:
        data = f.read()
    if data.startswith(b"P6"):
        bits = from_ppm(data)
    elif data.startswith(b"\x89PNG"):
        try:
            from PIL import Image
        except ImportError:
            raise ValueError("a PNG needs Pillow to be read (a screen as text "
                             "does not: `shot --text`)") from None
        with Image.open(path) as im:
            rgb = im.convert("RGB")
            bits = from_rgb(rgb.tobytes(), *rgb.size)
    else:
        try:
            return from_text(data.decode("utf-8"))
        except UnicodeDecodeError:
            raise ValueError("neither a picture nor a screen as text") from None
    return bits, b"\1" * (W * H)


def differing(bits, reference, counts=None):
    """How many pixels are not as in the reference, of those that count"""
    if counts is None:
        return sum(a != b for a, b in zip(bits, reference))
    return sum(a != b for a, b, c in zip(bits, reference, counts) if c)


def write(text, out=None):
    """The text is UTF-8, whatever the terminal says it takes"""
    out = out or sys.stdout
    out.flush()
    out.buffer.write(text.encode("utf-8"))
    out.buffer.flush()


def main():
    if len(sys.argv) not in (2, 3):
        print(__doc__)
        return 2
    try:
        bits, _ = load(sys.argv[1])
        if len(sys.argv) == 2:
            write(to_text(bits))
            return 0
        reference, counts = load(sys.argv[2])
    except (OSError, ValueError) as e:
        sys.exit(f"fe_screen: {e}")
    n = differing(bits, reference, counts)
    print(f"{n} pixels differ" if n != 1 else "1 pixel differs")
    return 1 if n else 0


if __name__ == "__main__":
    sys.exit(main())
