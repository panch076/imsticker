#!/usr/bin/env python3
"""Write the images the encoder is checked against, with no third-party code.

Each one is a 96x64 transparent canvas with an opaque block in the middle.
The PNG and the APNG also carry a half-transparent blue stripe, to catch any
mishandling of partial alpha. The two animations have four frames, a different
block colour per frame, and deliberately uneven delays, so a wrong frame order
or a lost delay shows up.

    python3 ci/make_fixtures.py        # rewrites ci/fixtures/
"""
import json
import struct
import zlib
from pathlib import Path

WIDTH, HEIGHT = 96, 64
BLOCK = (24, 16, 72, 48)  # x0, y0, x1, y1
STRIPE = (0, 52, 96, 60)
STRIPE_RGBA = (0, 0, 255, 128)
COLORS = [(255, 0, 0), (0, 255, 0), (0, 0, 255), (255, 255, 0)]
DELAYS_CS = [5, 20, 50, 10]  # hundredths of a second

# Where the checks look, as fractions of width and height.
POINTS = {"center": [0.5, 0.5], "corner": [0.04, 0.06], "stripe": [0.5, 0.875]}

SIGNATURE = b"\x89PNG\r\n\x1a\n"
IHDR = struct.pack(">IIBBBBB", WIDTH, HEIGHT, 8, 6, 0, 0, 0)  # 8-bit RGBA


def inside(box, x, y):
    return box[0] <= x < box[2] and box[1] <= y < box[3]


def rgba_rows(color, stripe):
    rows = []
    for y in range(HEIGHT):
        row = bytearray()
        for x in range(WIDTH):
            if inside(BLOCK, x, y):
                row += bytes(color) + b"\xff"
            elif stripe and inside(STRIPE, x, y):
                row += bytes(STRIPE_RGBA)
            else:
                row += b"\x00\x00\x00\x00"
        rows.append(bytes(row))
    return rows


def chunk(kind, data):
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)


def deflate(rows):
    return zlib.compress(b"".join(b"\x00" + row for row in rows), 9)


def png(rows):
    return SIGNATURE + chunk(b"IHDR", IHDR) + chunk(b"IDAT", deflate(rows)) + chunk(b"IEND", b"")


def apng(frames, delays_cs):
    parts = [SIGNATURE, chunk(b"IHDR", IHDR), chunk(b"acTL", struct.pack(">II", len(frames), 0))]
    sequence = 0
    for index, rows in enumerate(frames):
        # Full-canvas frame, delay as a fraction, dispose "none", blend "source".
        parts.append(chunk(b"fcTL", struct.pack(
            ">IIIIIHHBB", sequence, WIDTH, HEIGHT, 0, 0, delays_cs[index], 100, 0, 0)))
        sequence += 1
        if index == 0:
            parts.append(chunk(b"IDAT", deflate(rows)))
        else:
            parts.append(chunk(b"fdAT", struct.pack(">I", sequence) + deflate(rows)))
            sequence += 1
    parts.append(chunk(b"IEND", b""))
    return b"".join(parts)


def gif_image_data(indices, min_code_size=3):
    """LZW stream that never leaves 4-bit codes: a clear code every five pixels
    keeps the decoder's table from growing, so no real compression is needed."""
    clear = 1 << min_code_size
    codes = []
    for position, value in enumerate(indices):
        if position % 5 == 0:
            codes.append(clear)
        codes.append(value)
    codes.append(clear + 1)  # end of information

    packed = bytearray()
    bits = count = 0
    for code in codes:
        bits |= code << count
        count += min_code_size + 1
        while count >= 8:
            packed.append(bits & 0xFF)
            bits >>= 8
            count -= 8
    if count:
        packed.append(bits & 0xFF)

    blocks = bytearray([min_code_size])
    for start in range(0, len(packed), 255):
        piece = packed[start:start + 255]
        blocks += bytes([len(piece)]) + piece
    return bytes(blocks + b"\x00")


def gif(delays_cs):
    palette = [(0, 0, 0)] + COLORS + [(0, 0, 0)] * 3  # index 0 is the transparent one
    out = bytearray(b"GIF89a")
    out += struct.pack("<HHBBB", WIDTH, HEIGHT, 0xF2, 0, 0)  # global table of 8 colours
    for rgb in palette:
        out += bytes(rgb)
    out += b"\x21\xff\x0bNETSCAPE2.0\x03\x01\x00\x00\x00"  # loop forever
    for frame, delay in enumerate(delays_cs):
        indices = [
            frame + 1 if inside(BLOCK, x, y) else 0
            for y in range(HEIGHT) for x in range(WIDTH)
        ]
        # Dispose to background, palette index 0 transparent.
        out += b"\x21\xf9\x04" + struct.pack("<BHB", (2 << 2) | 1, delay, 0) + b"\x00"
        out += b"\x2c" + struct.pack("<HHHHB", 0, 0, WIDTH, HEIGHT, 0)
        out += gif_image_data(indices)
    out += b"\x3b"
    return bytes(out)


def main():
    target = Path(__file__).resolve().parent / "fixtures"
    target.mkdir(exist_ok=True)
    (target / "still.png").write_bytes(png(rgba_rows(COLORS[0], stripe=True)))
    (target / "anim.gif").write_bytes(gif(DELAYS_CS))
    (target / "anim.png").write_bytes(apng([rgba_rows(color, stripe=True) for color in COLORS], DELAYS_CS))

    clear = [0, 0, 0, 0]

    def frames(count, stripe):
        return [{
            "delay": DELAYS_CS[index] / 100.0 if count > 1 else None,
            "center": list(COLORS[index]) + [255],
            "corner": clear,
            "stripe": list(STRIPE_RGBA) if stripe else clear,
        } for index in range(count)]

    expected = {
        "canvas": [WIDTH, HEIGHT],
        "points": POINTS,
        "fixtures": {
            "still.png": {"frames": frames(1, True)},
            "anim.gif": {"frames": frames(4, False)},
            "anim.png": {"frames": frames(4, True)},
        },
    }
    (target / "expected.json").write_text(json.dumps(expected, indent=2) + "\n", encoding="utf-8")
    for path in sorted(target.iterdir()):
        print("{:>6} bytes  {}".format(path.stat().st_size, path.name))


if __name__ == "__main__":
    main()
