"""Stand-in for the Swift/ImageIO encoder so the database logic can be tested
on any OS. It speaks the same command line and prints the same JSON, but the
"images" are just headers plus padding.

A source file whose name contains "anim" is treated as animated, and one whose
name contains "broken" fails to decode. Environment switches: FAKE_BAD_BRAND=1
writes a non-msf1 sequence, FAKE_GARBAGE=1 prints something that is not the
expected JSON.
"""
import json
import os
import sys
from pathlib import Path

STILLS = (
    ("com.apple.stickers.role.still", None, True),
    ("com.apple.stickers.role.keyboard", 100, False),
    ("com.apple.stickers.role.stillVariant_320", None, False),
    ("com.apple.stickers.role.stillVariant_160", 160, False),
    ("com.apple.stickers.role.stillVariant_96", 96, False),
    ("com.apple.stickers.role.stillVariant_64", 64, False),
    ("com.apple.stickers.role.stillVariant_40", 40, False),
)


def main():
    args = sys.argv[1:]
    options = {"--no-upscale": False}
    index = 0
    while index < len(args):
        if args[index] == "--no-upscale":
            options["--no-upscale"] = True
            index += 1
        else:
            options[args[index]] = args[index + 1]
            index += 2
    source = Path(options["--input"])
    if "broken" in source.name:
        sys.stderr.write("cannot open image: {}\n".format(source))
        return 1
    if "bridging" in source.name:  # the toolchain fault from StickerHack issue #2
        sys.stderr.write("error: redefinition of module 'SwiftBridging'\n" + "note: more\n" * 40)
        return 1
    if os.environ.get("FAKE_GARBAGE"):
        print(json.dumps({"representations": [{"role": "x"}]}))
        return 0

    out_dir = Path(options["--output-dir"])
    out_dir.mkdir(parents=True, exist_ok=True)
    edge = int(options["--max-edge"])
    if options["--no-upscale"]:
        edge = min(edge, 200)  # pretend the source is 200 px wide
    reps = []
    if "anim" in source.name:
        brand = b"ftypheic" if os.environ.get("FAKE_BAD_BRAND") else b"ftypmsf1"
        path = out_dir / "animated.heics"
        # Size grows with the square of the edge: 768 px is about 2.36 MB.
        path.write_bytes(b"\x00\x00\x00\x1c" + brand + b"A" * (edge * edge * 4))
        reps.append({
            "role": "com.apple.stickers.role.animated", "uti": "public.heics",
            "width": edge, "height": edge, "byteCount": path.stat().st_size,
            "preferred": True, "index": 2, "path": str(path),
        })
        mode, frames = "animated", 12
    else:
        for position, (role, fixed, preferred) in enumerate(STILLS):
            size = fixed or edge
            path = out_dir / "rep-{}.heic".format(position)
            path.write_bytes(b"\x00\x00\x00\x18ftypheic" + bytes([position]) * (size * 3))
            reps.append({
                "role": role, "uti": "public.heic", "width": size, "height": size,
                "byteCount": path.stat().st_size, "preferred": preferred,
                "index": position, "path": str(path),
            })
        mode, frames = "static", 1
    print(json.dumps({
        "mode": mode, "sourceWidth": 200, "sourceHeight": 200, "frameCount": frames,
        "resample": "smooth", "representations": reps,
    }))
    return 0


if __name__ == "__main__":
    sys.exit(main())
