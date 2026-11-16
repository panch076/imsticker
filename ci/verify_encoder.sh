#!/bin/bash
# Run the real Swift helper through the real tool on a Mac, then decode what
# was stored with ImageIO and compare it with the source images.
#
# Two routes are checked. --raw-sql goes into stand-in stores built by the
# tests. The default route, a save through Core Data, goes into a store that
# Core Data itself creates here from the model Apple ships, so it has the real
# layout without being anyone's sticker library.
#
# Some Macs (GitHub's virtual Apple-silicon ones among them) cannot write an
# HEIF image sequence at all. That is asked of ImageIO directly, so a machine
# that cannot is reported as such instead of being mistaken for a broken tool.
set -euo pipefail
cd "$(dirname "$0")/.."

PY=/usr/bin/python3
WORK="${RUNNER_TEMP:-${TMPDIR:-/tmp}}/imsticker-verify"
rm -rf "$WORK"
mkdir -p "$WORK"
export IMSTICKER_BACKUP_DIR="$WORK/backups"
F=ci/fixtures
MODEL=/System/Library/PrivateFrameworks/Stickers.framework/Versions/A/Resources/Stickers.momd

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

check() {
    local name="$1"
    shift
    "$WORK/inspect" "$WORK/$name"/* > "$WORK/$name-facts.json"
    "$PY" ci/check_facts.py "$WORK/$name-facts.json" "$WORK/source-facts.json" "$F/expected.json" "$@"
}

swiftc -o "$WORK/inspect" ci/inspect_images.swift
"$WORK/inspect" "$F/still.png" "$F/anim.gif" "$F/anim.png" > "$WORK/source-facts.json"

echo "::group::can this Mac write an animation at all?"
swiftc -o "$WORK/can_write_heics" ci/can_write_heics.swift
if "$WORK/can_write_heics"; then
    CAN_ANIMATE=yes
else
    CAN_ANIMATE=no
fi
echo "::endgroup::"

echo "::group::stills, rows written with SQL"
STILLS="$WORK/stills.stickerdb"
new_store "$STILLS"
time imsticker "$STILLS" add --raw-sql --no-restart --max-edge 256 "$F/still.png"   # sticker 1
time imsticker "$STILLS" add --raw-sql --no-restart "$F/still.png"                  # sticker 2, default size
# Sticker 3 goes through the interpreter, as when swiftc cannot build.
mkdir -p "$WORK/nobuild"
printf '#!/bin/sh\necho "swiftc is disabled for this check" >&2\nexit 1\n' > "$WORK/nobuild/swiftc"
chmod +x "$WORK/nobuild/swiftc"
time env PATH="$WORK/nobuild:$PATH" "$PY" imsticker.py --db "$STILLS" add --raw-sql --no-restart --max-edge 128 "$F/still.png"
imsticker "$STILLS" export "$WORK/stills"
sips -g pixelWidth -g pixelHeight -g hasAlpha -g format "$WORK/stills"/sticker1_0_still.heic || true
check stills 1=still.png:256:up 2=still.png:768:up 3=still.png:128:up
echo "::endgroup::"

if [ "$CAN_ANIMATE" = yes ]; then
    echo "::group::animations, rows written with SQL"
    ANIMATED="$WORK/animated.stickerdb"
    new_store "$ANIMATED"
    time imsticker "$ANIMATED" add --raw-sql --no-restart --max-edge 256 "$F/anim.gif" "$F/anim.png"       # stickers 1 and 2
    time imsticker "$ANIMATED" add --raw-sql --no-restart --no-upscale --resample nearest "$F/anim.gif"   # sticker 3
    imsticker "$ANIMATED" list -v
    imsticker "$ANIMATED" export "$WORK/animated"
    file "$WORK/animated"/* || true
    check animated 1=anim.gif:256:up 2=anim.png:256:up 3=anim.gif:768:noup
    echo "--- what an animated sticker is on disk"
    "$PY" ci/heif_boxes.py "$WORK/animated/sticker2_2_animated.heics" || true
    echo "::endgroup::"
fi

echo "::group::the default route: a Core Data save into a store made from Apple's model"
"$PY" -c "import imsticker, sys; sys.stdout.write(imsticker.SWIFT_HELPER)" > "$WORK/helper.swift"
swiftc -o "$WORK/helper" "$WORK/helper.swift"
echo '{"stickers": []}' > "$WORK/empty.json"
APPLE="$WORK/apple-model.stickerdb"
"$WORK/helper" insert --create --store "$APPLE" --model "$MODEL" --manifest "$WORK/empty.json"
sqlite3 "$APPLE" ".tables"
time imsticker "$APPLE" add --no-restart --max-edge 256 --name "a still" "$F/still.png"            # sticker 1
IMPORTS="1=still.png:256:up"
EXPECT_OBJECTS=8
EXPECT_SAVES=1
if [ "$CAN_ANIMATE" = yes ]; then
    time imsticker "$APPLE" add --no-restart --max-edge 256 "$F/anim.gif" "$F/anim.png"          # stickers 2 and 3
    IMPORTS="$IMPORTS 2=anim.gif:256:up 3=anim.png:256:up"
    EXPECT_OBJECTS=12
    EXPECT_SAVES=2
fi
imsticker "$APPLE" list -v
sqlite3 -header "$APPLE" \
    "select Z_PK, Z_ENT, Z_OPT, ZTYPE, ZVERSION, ZLIBRARYINDEX, ZSEARCHTEXT, ZEXTERNALURI from ZMANAGEDSTICKER" \
    "select Z_PK, ZTIMESTAMP, ZAUTHORTS, ZBUNDLEIDTS from ATRANSACTION" \
    "select ZCHANGETYPE, ZENTITY, ZENTITYPK, ZTRANSACTIONID from ACHANGE order by Z_PK" \
    "select Z_PK, ZNAME from ATRANSACTIONSTRING" \
    "pragma integrity_check"
imsticker "$APPLE" export "$WORK/apple"
# shellcheck disable=SC2086
check apple $IMPORTS
# Every object saved must be in the store's history, which is what iCloud reads.
"$PY" - "$APPLE" "$EXPECT_OBJECTS" "$EXPECT_SAVES" <<'PYTHON'
import sqlite3, sys
conn = sqlite3.connect(sys.argv[1])
inserts = conn.execute("select count(*) from ACHANGE where ZCHANGETYPE = 0").fetchone()[0]
saves = conn.execute("select count(*) from ATRANSACTION").fetchone()[0]
authors = [row[0] for row in conn.execute("select ZNAME from ATRANSACTIONSTRING")]
print("history: {} objects inserted in {} saves, strings {}".format(inserts, saves, authors))
assert inserts == int(sys.argv[2]), "expected {} inserted objects in the history".format(sys.argv[2])
assert saves == int(sys.argv[3]), "expected {} saves in the history".format(sys.argv[3])
assert "imsticker" in authors, "the saves should be attributed to imsticker"
PYTHON
echo "::endgroup::"

if [ "$CAN_ANIMATE" = yes ]; then
    echo "::notice title=Animated stickers checked::This runner can write HEIF image sequences, and the animated path passed on both routes."
else
    echo "::warning title=Animated stickers not checked on this runner::ImageIO here cannot write an HEIF image sequence, with or without imsticker, so only stills were exercised."
fi
