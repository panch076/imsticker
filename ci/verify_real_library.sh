#!/bin/bash
# The checks that run against the sticker library stickersd keeps on this Mac.
# Unlike ci/experiments.sh, every one of them fails the job if it does not hold.
#
#   1. imsticker finds the library itself and imports into it by its default
#      route. Each new sticker must have the values Apple's own code gives one,
#      and every object of it must be in the store's history.
#   2. stickersd is restarted on top. The stickers must still be there and the
#      database must pass its integrity check.
#   3. Apple's own store code, run in this process on a copy, must return every
#      imported sticker with its still and keyboard images, and the animation
#      where there is one.
#
# What stickersd logs along the way is printed, not judged.
set -euo pipefail
cd "$(dirname "$0")/.."

PY=/usr/bin/python3
WORK="${RUNNER_TEMP:-${TMPDIR:-/tmp}}/imsticker-real"
rm -rf "$WORK"
mkdir -p "$WORK/copy"
export IMSTICKER_BACKUP_DIR="$WORK/backups"
F=ci/fixtures
ME=$(id -u)

start_daemon() {
    launchctl kickstart -p "gui/$ME/com.apple.stickersd" || true
    sleep 5
}

start_daemon
STORE="$HOME/Library/Group Containers/com.apple.stickersd.group/Stickers/stickers.stickerdb"
[ -f "$STORE" ] || STORE="$HOME/Library/Stickers/stickers.stickerdb"
if [ ! -f "$STORE" ]; then
    echo "::error title=No sticker library::stickersd has not created a library on this Mac, so nothing could be checked against one."
    exit 1
fi
echo "library: $STORE"

swiftc -o "$WORK/can_write_heics" ci/can_write_heics.swift
status=0
"$WORK/can_write_heics" || status=$?
case "$status" in
    0) IMPORTS=3; ANIMATED=2 ;;
    3) IMPORTS=1; ANIMATED=0 ;;
    *) echo "::error title=The animation probe broke::can_write_heics exited $status"; exit 1 ;;
esac

echo "::group::1. import into the real library, found without being told where"
"$PY" ci/real_library.py snapshot "$STORE" "$WORK/before.json"
"$PY" imsticker.py add --no-restart --max-edge 256 --name "imported still" "$F/still.png"
if [ "$ANIMATED" = 2 ]; then
    "$PY" imsticker.py add --no-restart --max-edge 256 "$F/anim.gif"
    "$PY" imsticker.py add --no-restart --max-edge 256 "$F/anim.png"
fi
"$PY" ci/real_library.py check "$STORE" "$WORK/before.json" "$WORK/imported.txt" "$IMPORTS"
echo "::endgroup::"

echo "::group::2. stickersd restarted on top of them"
killall stickersd || true
sleep 2
start_daemon
pgrep -lf stickersd || true
"$PY" ci/real_library.py check "$STORE" "$WORK/before.json" "$WORK/imported.txt" "$IMPORTS"
INTEGRITY=$(sqlite3 "$STORE" "pragma integrity_check")
echo "integrity check: $INTEGRITY"
[ "$INTEGRITY" = ok ]
echo "--- error-level lines from the sticker system since the import (printed, not judged):"
log show --last 2m --style compact --predicate 'subsystem BEGINSWITH "com.apple.stickers" AND messageType == error' 2>/dev/null \
    | grep -v "legacy data store" | cut -c1-200 | sort -k5 | uniq -c -f4 | tail -12 || true
echo "::endgroup::"

echo "::group::3. Apple's own store code reads them back (on a copy)"
bash ci/build_harness.sh "$WORK/harness"
sqlite3 "$STORE" ".backup '$WORK/copy/stickers.stickerdb'"
# shellcheck disable=SC2046
"$WORK/harness/exp_apple_store" "$WORK/copy/stickers.stickerdb" fetch $(cat "$WORK/imported.txt") > "$WORK/fetch.txt"
grep -v "description = " "$WORK/fetch.txt"
"$PY" ci/real_library.py fetched "$WORK/fetch.txt" "$WORK/imported.txt" "$ANIMATED"
echo "::endgroup::"
