#!/bin/bash
# Run the real Swift encoder through the real tool on a Mac, into stand-in
# stores, then decode what was stored with ImageIO and compare it with the
# source images.
#
# Stills and animations are checked separately, because some Macs (GitHub's
# virtual Apple-silicon ones among them) cannot write an HEIF image sequence at
# all. That is asked of ImageIO directly first, so a machine that cannot is
# reported as such instead of being mistaken for a broken encoder.
set -euo pipefail
cd "$(dirname "$0")/.."

PY=/usr/bin/python3
WORK="${RUNNER_TEMP:-${TMPDIR:-/tmp}}/imsticker-verify"
rm -rf "$WORK"
mkdir -p "$WORK"
export IMSTICKER_BACKUP_DIR="$WORK/backups"
F=ci/fixtures

new_store() {
    "$PY" - "$1" <<'PYTHON'
import sys
sys.path.insert(0, "tests")
import test_imsticker
test_imsticker.make_store(sys.argv[1])
PYTHON
}

imsticker() {
    local store="$1"
    shift
    "$PY" imsticker.py --db "$store" "$@"
}

swiftc -o "$WORK/inspect" ci/inspect_images.swift
"$WORK/inspect" "$F/still.png" "$F/anim.gif" "$F/anim.png" > "$WORK/source-facts.json"

echo "::group::stills"
STILLS="$WORK/stills.stickerdb"
new_store "$STILLS"
time imsticker "$STILLS" add --no-restart --max-edge 256 "$F/still.png"   # sticker 1
time imsticker "$STILLS" add --no-restart "$F/still.png"                  # sticker 2, default size
# Sticker 3 goes through the interpreter, as when swiftc cannot build.
mkdir -p "$WORK/nobuild"
printf '#!/bin/sh\necho "swiftc is disabled for this check" >&2\nexit 1\n' > "$WORK/nobuild/swiftc"
chmod +x "$WORK/nobuild/swiftc"
time env PATH="$WORK/nobuild:$PATH" "$PY" imsticker.py --db "$STILLS" add --no-restart --max-edge 128 "$F/still.png"
imsticker "$STILLS" list -v
imsticker "$STILLS" export "$WORK/stills"
file "$WORK/stills"/sticker1_* || true
sips -g pixelWidth -g pixelHeight -g hasAlpha -g format "$WORK/stills"/sticker1_0_still.heic || true
"$WORK/inspect" "$WORK/stills"/* > "$WORK/still-facts.json"
"$PY" ci/check_facts.py "$WORK/still-facts.json" "$WORK/source-facts.json" "$F/expected.json" \
    1=still.png:256:up 2=still.png:768:up 3=still.png:128:up
echo "::endgroup::"

echo "::group::can this Mac write an animation at all?"
swiftc -o "$WORK/can_write_heics" ci/can_write_heics.swift
if "$WORK/can_write_heics"; then
    CAN_ANIMATE=yes
else
    CAN_ANIMATE=no
fi
echo "::endgroup::"
if [ "$CAN_ANIMATE" = no ]; then
    echo "::warning title=Animated stickers not checked on this runner::ImageIO here cannot write an HEIF image sequence, with or without imsticker, so the animated path was not exercised."
    exit 0
fi

echo "::group::animations"
ANIMATED="$WORK/animated.stickerdb"
new_store "$ANIMATED"
time imsticker "$ANIMATED" add --no-restart --max-edge 256 "$F/anim.gif" "$F/anim.png"       # stickers 1 and 2
time imsticker "$ANIMATED" add --no-restart --no-upscale --resample nearest "$F/anim.gif"   # sticker 3
imsticker "$ANIMATED" list -v
imsticker "$ANIMATED" export "$WORK/animated"
file "$WORK/animated"/* || true
"$WORK/inspect" "$WORK/animated"/* > "$WORK/animated-facts.json"
"$PY" ci/check_facts.py "$WORK/animated-facts.json" "$WORK/source-facts.json" "$F/expected.json" \
    1=anim.gif:256:up 2=anim.png:256:up 3=anim.gif:768:noup
echo "::endgroup::"
echo "::notice title=Animated stickers checked::This runner can write HEIF image sequences, and the animated path passed."
