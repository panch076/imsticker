#!/usr/bin/env python3
"""Print the structure of an HEIF file: its brands, tracks, and frame timing.

Used to see what an animated sticker really is on disk, and where the frame
delays are kept, without relying on the decoder that reads them back.

    python3 ci/heif_boxes.py file.heics
"""
import struct
import sys

CONTAINERS = {"moov", "trak", "mdia", "minf", "stbl", "dinf", "edts", "meta", "iprp", "ipco", "tref"}


def text(raw):
    return raw.decode("latin-1")


def describe(kind, body):
    if kind == "ftyp":
        brands = [text(body[index:index + 4]) for index in range(8, len(body), 4)]
        return "major brand {}, compatible {}".format(text(body[:4]), " ".join(brands))
    if kind == "hdlr":
        return "handler {}".format(text(body[8:12]))
    if kind == "mdhd":
        if body[0] == 1:
            timescale, duration = struct.unpack(">IQ", body[20:32])
        else:
            timescale, duration = struct.unpack(">II", body[12:20])
        seconds = duration / float(timescale) if timescale else 0
        return "timescale {}, duration {} ({:.3f} s)".format(timescale, duration, seconds)
    if kind == "tkhd":
        start = 88 if body[0] == 1 else 76
        track = struct.unpack(">I", body[20:24] if body[0] == 1 else body[12:16])[0]
        width, height = struct.unpack(">II", body[start:start + 8])
        return "track {}, {}x{}, flags {:#x}".format(track, width >> 16, height >> 16, struct.unpack(">I", body[:4])[0] & 0xFFFFFF)
    if kind == "stts":
        count = struct.unpack(">I", body[4:8])[0]
        entries = [struct.unpack(">II", body[8 + 8 * index:16 + 8 * index]) for index in range(count)]
        return "frame timing as (frames, ticks each): {}".format(entries)
    if kind == "stsz":
        return "{} samples".format(struct.unpack(">I", body[8:12])[0])
    if kind == "stsd":
        count = struct.unpack(">I", body[4:8])[0]
        kinds, offset = [], 8
        for _ in range(count):
            size = struct.unpack(">I", body[offset:offset + 4])[0]
            kinds.append(text(body[offset + 4:offset + 8]))
            offset += max(size, 8)
        return "sample format {}".format(" ".join(kinds))
    if kind == "elst":
        return "edit list, {} bytes".format(len(body))
    return ""


def walk(data, start, end, depth, parent):
    offset = start
    while offset + 8 <= end:
        size, raw_kind = struct.unpack(">I4s", data[offset:offset + 8])
        header = 8
        if size == 1:
            size = struct.unpack(">Q", data[offset + 8:offset + 16])[0]
            header = 16
        elif size == 0:
            size = end - offset
        if size < header or offset + size > end:
            break
        kind = text(raw_kind)
        body = data[offset + header:offset + size]
        if parent == "tref":
            ids = [struct.unpack(">I", body[index:index + 4])[0] for index in range(0, len(body), 4)]
            note = "refers to track {}".format(ids)
        else:
            note = describe(kind, body)
        print("{}{} ({} bytes){}".format("  " * depth, kind, size, "  " + note if note else ""))
        if kind in CONTAINERS:
            walk(data, offset + header + (4 if kind == "meta" else 0), offset + size, depth + 1, kind)
        offset += size


def main():
    for path in sys.argv[1:]:
        with open(path, "rb") as handle:
            data = handle.read()
        print("{} ({} bytes)".format(path, len(data)))
        walk(data, 0, len(data), 1, "")


if __name__ == "__main__":
    main()
