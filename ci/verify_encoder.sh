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
status=0
"$WORK/can_write_heics" || status=$?
case "$status" in
    0) CAN_ANIMATE=yes ;;
    3) CAN_ANIMATE=no ;;
    *)
        echo "::error title=The animation probe broke::can_write_heics exited $status; it should exit 0 (can) or 3 (cannot)."
        exit 1
        ;;
esac
echo "::endgroup::"
# One runner is relied on to exercise animations. If it cannot, say so loudly
# rather than let every job pass without the animated path having run anywhere.
if [ "$CAN_ANIMATE" = no ] && [ "${REQUIRE_ANIMATION:-no}" = yes ]; then
    echo "::error title=Animated stickers not checked::The runner relied on for HEIF sequences could not write one, so no job checked the animated path."
    exit 1
fi

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
    time imsticker "$ANIMATED" add --raw-sql --no-restart --max-edge 128 --animation-only "$F/anim.png"   # sticker 4
    imsticker "$ANIMATED" list -v
    imsticker "$ANIMATED" export "$WORK/animated"
    file "$WORK/animated"/* || true
    check animated 1=anim.gif:256:up 2=anim.png:256:up 3=anim.gif:768:noup 4=anim.png:128:up:only
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
    # One at a time: Core Data numbers the stickers of a single save in no particular order.
    time imsticker "$APPLE" add --no-restart --max-edge 256 "$F/anim.gif"                        # sticker 2
    time imsticker "$APPLE" add --no-restart --max-edge 256 "$F/anim.png"                        # sticker 3
    IMPORTS="$IMPORTS 2=anim.gif:256:up 3=anim.png:256:up"
    EXPECT_OBJECTS=16
    EXPECT_SAVES=3
fi
# And once with the helper interpreted rather than built, saving included.
time env PATH="$WORK/nobuild:$PATH" "$PY" imsticker.py --db "$APPLE" add --no-restart --max-edge 128 "$F/still.png"
IMPORTS="$IMPORTS $((EXPECT_SAVES + 1))=still.png:128:up"
EXPECT_OBJECTS=$((EXPECT_OBJECTS + 8))
EXPECT_SAVES=$((EXPECT_SAVES + 1))
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
# Every object saved must be in the store's history, which is what iCloud reads,
# and every sticker must carry the values Apple's own code gives one of its own.
"$PY" - "$APPLE" "$EXPECT_OBJECTS" "$EXPECT_SAVES" <<'PYTHON'
import sqlite3, sys, uuid
conn = sqlite3.connect(sys.argv[1])
conn.row_factory = sqlite3.Row
inserts = conn.execute("select count(*) from ACHANGE where ZCHANGETYPE = 0").fetchone()[0]
saves = conn.execute("select count(*) from ATRANSACTION").fetchone()[0]
authors = [row[0] for row in conn.execute("select ZNAME from ATRANSACTIONSTRING")]
print("history: {} objects inserted in {} saves, strings {}".format(inserts, saves, authors))
assert inserts == int(sys.argv[2]), "expected {} inserted objects in the history".format(sys.argv[2])
assert saves == int(sys.argv[3]), "expected {} saves in the history".format(sys.argv[3])
assert "imsticker" in authors, "the saves should be attributed to imsticker"

stickers = conn.execute("select * from ZMANAGEDSTICKER order by Z_PK").fetchall()
assert len(stickers) == saves, "one sticker per save in this script"
assert stickers[0]["ZSEARCHTEXT"] == "a still", "sticker 1: search text is {!r}".format(stickers[0]["ZSEARCHTEXT"])
for number, row in enumerate(stickers, 1):
    where = "sticker {}".format(number)
    identifier = str(uuid.UUID(bytes=bytes(row["ZIDENTIFIER"]))).upper()
    assert (row["ZTYPE"], row["ZEFFECT"], row["ZVERSION"]) == (1, 0, 1), where + ": type, effect, version"
    assert row["ZEXTERNALURI"] == "sticker:///user/identifier/" + identifier, where + ": " + row["ZEXTERNALURI"]
    assert row["ZLIBRARYINDEX"] == 1024.0 * number, where + ": library index {}".format(row["ZLIBRARYINDEX"])
    assert row["ZNAME"] == "", where + ": name"
    assert row["ZCREATIONDATE"] == row["ZLASTUSEDDATE"] and row["ZCREATIONDATE"] > 800000000, where + ": dates"
    images = conn.execute(
        "select ZROLE, ZUTI, ZISPREFERRED, ZVERSION, ZBYTECOUNT, length(ZDATA) as BYTES, length(ZIDENTIFIER) as IDENT "
        "from ZMANAGEDREPRESENTATION where ZSTICKER = ? order by ZINDEX", (row["Z_PK"],)).fetchall()
    preferred = [image["ZROLE"].rsplit(".", 1)[-1] for image in images if image["ZISPREFERRED"]]
    animated = any(image["ZUTI"] == "public.heics" for image in images)
    assert preferred == (["animated"] if animated else ["still"]), where + ": preferred image is {}".format(preferred)
    assert all(image["ZVERSION"] == 1 and image["IDENT"] == 16 for image in images), where + ": image version, identifier"
    assert all(image["ZBYTECOUNT"] == image["BYTES"] > 0 for image in images), where + ": image byte counts"
    assert row["ZBYTECOUNT"] == sum(image["BYTES"] for image in images), where + ": total byte count"
print("stored values: as Apple's own code writes them, for {} stickers".format(len(stickers)))
PYTHON
echo "::endgroup::"

if [ "$CAN_ANIMATE" = yes ]; then
    echo "::notice title=Animated stickers checked::This runner can write HEIF image sequences, and the animated path passed on both routes."
else
    echo "::warning title=Animated stickers not checked on this runner::ImageIO here cannot write an HEIF image sequence, with or without imsticker, so only stills were exercised."
fi
