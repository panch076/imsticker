#!/bin/bash
# What does StickerHack's own encoder do with an APNG's frame timing?
# README.md says it plays every APNG at a fixed 10 fps because it only reads
# GIF delays. This runs that encoder, pinned to one commit, to check the claim.
# The GIF is the control: its delays should come through.
set -euo pipefail
cd "$(dirname "$0")/.."

WORK="${RUNNER_TEMP:-${TMPDIR:-/tmp}}/stickerhack-compare"
rm -rf "$WORK"
mkdir -p "$WORK/apng" "$WORK/gif"
COMMIT=66e728abacfc59a4bf174a328368ee351d10c854
curl -fsSL "https://raw.githubusercontent.com/AdamEXu/StickerHack/$COMMIT/helpers/encode_sticker.swift" \
    -o "$WORK/encode_sticker.swift"

swiftc -o "$WORK/stickerhack_encoder" "$WORK/encode_sticker.swift"
"$WORK/stickerhack_encoder" --input ci/fixtures/anim.png --output-dir "$WORK/apng" --max-edge 256 --resample auto > /dev/null
"$WORK/stickerhack_encoder" --input ci/fixtures/anim.gif --output-dir "$WORK/gif" --max-edge 256 --resample auto > /dev/null

swiftc -o "$WORK/inspect" ci/inspect_images.swift
"$WORK/inspect" "$WORK/apng/animated.heics" "$WORK/gif/animated.heics" > "$WORK/facts.json"

/usr/bin/python3 - "$WORK/facts.json" <<'PYTHON'
import json
import sys

with open(sys.argv[1]) as handle:
    apng, gif = json.load(handle)
source = [0.05, 0.2, 0.5, 0.1]
for label, facts in (("APNG", apng), ("GIF", gif)):
    delays = [frame.get("unclampedDelay", frame.get("delay")) for frame in facts["frames"]]
    kept = len(delays) == len(source) and all(
        delay is not None and abs(delay - want) < 0.011 for delay, want in zip(delays, source))
    print("StickerHack {}: frames={} delays={} source={} -> timing {}".format(
        label, facts["frameCount"], delays, source, "kept" if kept else "NOT kept"))
PYTHON
