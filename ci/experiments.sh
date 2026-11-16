#!/bin/bash
# Experiments against the real sticker store that stickersd keeps on this Mac.
#
#   1. Put stickers in both ways imsticker can: rows written with SQL, and the
#      default, a save through Core Data. Look at the store's change history
#      (what iCloud sync reads) after each, then restart stickersd and see what
#      it makes of them.
#   2. Run Apple's own sticker store code in this process, on a copy of the
#      store, and ask it for those stickers. If Apple's code reads them back,
#      the system will.
#   3. Hand Apple's own code a plain PNG, a GIF and an APNG, and look at what
#      it stores, to learn what a sticker made by Apple looks like.
#   4. Show that stickersd itself does not answer a program Apple did not sign.
#
# Best effort; never fails the job. Read the output.
set +e
cd "$(dirname "$0")/.."

PY=/usr/bin/python3
WORK="${RUNNER_TEMP:-${TMPDIR:-/tmp}}/imsticker-experiments"
rm -rf "$WORK"
mkdir -p "$WORK/copy" "$WORK/shim"
export IMSTICKER_BACKUP_DIR="$WORK/backups"
F=ci/fixtures
ME=$(id -u)
SDK=$(xcrun --show-sdk-path)

section() {
    echo
    echo "=== $* ==="
}

start_daemon() {
    launchctl kickstart -p "gui/$ME/com.apple.stickersd" 2>&1
    sleep 4
}

find_store() {
    find "$HOME/Library/Stickers" "$HOME/Library/Group Containers" -maxdepth 4 -name "*.stickerdb" 2>/dev/null | head -1
}

uuids() {
    "$PY" - "$1" <<'PYTHON'
import sqlite3, sys, uuid
rows = sqlite3.connect(sys.argv[1]).execute("select ZIDENTIFIER from ZMANAGEDSTICKER order by Z_PK").fetchall()
print(" ".join(str(uuid.UUID(bytes=bytes(row[0]))).upper() for row in rows if row[0]))
PYTHON
}

dump_rows() {
    sqlite3 -header "$1" \
        "select Z_PK, Z_ENT, Z_OPT, ZTYPE, ZEFFECT, ZVERSION, ZBYTECOUNT, ZLIBRARYINDEX, ZCREATIONDATE, ZNAME, ZSEARCHTEXT, ZACCESSIBILITYNAME, ZEXTERNALURI, ZATTRIBUTIONBUNDLEIDENTIFIER, length(ZMETADATA) as META, hex(ZIDENTIFIER) as IDENT from ZMANAGEDSTICKER order by Z_PK" \
        "select Z_PK, Z_ENT, Z_OPT, ZSTICKER, ZINDEX, ZISPREFERRED, ZVERSION, ZBYTECOUNT, ZSIZE_W, ZSIZE_H, ZROLE, ZUTI, length(ZDATA) as BYTES, hex(substr(ZDATA, 1, 12)) as HEAD from ZMANAGEDREPRESENTATION order by Z_PK"
}

dump_history() {
    sqlite3 -header "$1" \
        "select Z_PK, ZAUTHORTS, ZBUNDLEIDTS, ZPROCESSIDTS, ZTIMESTAMP from ATRANSACTION order by Z_PK" \
        "select Z_PK, ZCHANGETYPE, ZENTITY, ZENTITYPK, ZTRANSACTIONID, hex(ZCOLUMNS) as COLUMNS from ACHANGE order by Z_PK" \
        "select Z_PK, ZNAME from ATRANSACTIONSTRING order by Z_PK"
}

sticker_log() {
    log show --last "$1" --style compact --predicate 'subsystem BEGINSWITH "com.apple.stickers" OR (process == "stickersd" AND messageType == error)' 2>&1 \
        | grep -v -E "com.apple.xpc|CoreAnalytics|corespotlight:default|Connection invalid" | cut -c1-420 | tail -"$2"
}

section "the store"
start_daemon
pgrep -lf stickersd
STORE=$(find_store)
echo "store: [$STORE]"
if [ -z "$STORE" ]; then
    echo "stickersd has no store on this Mac; nothing to experiment on."
    exit 0
fi
dump_rows "$STORE"

section "can this Mac write an animation"
swiftc -o "$WORK/can_write_heics" ci/can_write_heics.swift 2>&1 | head -5
if "$WORK/can_write_heics"; then CAN_ANIMATE=yes; else CAN_ANIMATE=no; fi

section "1a. rows written with SQL (--raw-sql)"
"$PY" imsticker.py --db "$STORE" add --raw-sql --no-restart --max-edge 256 --name "sql rows" "$F/still.png"
echo "--- history afterwards (SQL rows leave no trace in it):"
dump_history "$STORE"

section "1b. the default: a save through Core Data"
"$PY" imsticker.py --db "$STORE" add --no-restart --max-edge 256 --name "core data" "$F/still.png"
if [ "$CAN_ANIMATE" = yes ]; then
    "$PY" imsticker.py --db "$STORE" add --no-restart --max-edge 256 "$F/anim.gif" "$F/anim.png"
fi
echo "--- history afterwards:"
dump_history "$STORE"
dump_rows "$STORE"

section "1c. stickersd restarted on top of them"
killall stickersd 2>/dev/null
sleep 2
start_daemon
sleep 4
"$PY" imsticker.py --db "$STORE" list
sqlite3 "$STORE" "pragma integrity_check"
echo "--- what the sticker system logged:"
sticker_log 3m 60
echo "--- iCloud bookkeeping rows (none expected without an iCloud account):"
sqlite3 -header "$STORE" "select count(*) as RECORD_METADATA from ANSCKRECORDMETADATA" "select Z_PK, ZCLOUDKITEVENTTYPE, ZSUCCEEDED, ZERRORDOMAIN, ZERRORCODE from ANSCKEVENT order by Z_PK desc limit 5"

section "2. build Apple's store code into a test program"
ARCH=$(uname -m)
cat > "$WORK/shim/Stickers.swiftinterface" <<EOF
// swift-interface-format-version: 1.0
// swift-compiler-version: Apple Swift version 5.9
// swift-module-flags: -target $ARCH-apple-macos14.0 -enable-library-evolution -swift-version 5 -module-name Stickers
import Foundation
import Swift
public class StickerStore {
  public enum PersistenceType {
    case inMemory
    case sharedFile
    case customFile(Foundation.URL)
    case remote
  }
  public init(persistence: Stickers.StickerStore.PersistenceType)
  @objc deinit
}
EOF
ls "$SDK/System/Library/PrivateFrameworks/Stickers.framework" 2>&1 | head -10
clang -fobjc-arc -DEXP_NO_MAIN -c ci/exp_facade.m -o "$WORK/facade.o" 2>&1 | head -30
clang -fobjc-arc -framework Foundation -framework CoreGraphics ci/exp_facade.m -o "$WORK/exp_facade" 2>&1 | head -30
swiftc -I "$WORK/shim" -F "$SDK/System/Library/PrivateFrameworks" -framework Stickers \
    -framework Foundation -framework CoreGraphics \
    -import-objc-header ci/exp_bridge.h ci/exp_apple_store.swift "$WORK/facade.o" \
    -o "$WORK/exp_apple_store" 2>&1 | head -60
ls -la "$WORK/exp_apple_store" "$WORK/exp_facade" 2>&1

section "2a. Apple's store code reads back what imsticker wrote (on a copy)"
sqlite3 "$STORE" ".backup '$WORK/copy/stickers.stickerdb'"
ALL=$(uuids "$WORK/copy/stickers.stickerdb")
echo "stickers in the copy: $ALL"
if [ -x "$WORK/exp_apple_store" ]; then
    # shellcheck disable=SC2086
    "$WORK/exp_apple_store" "$WORK/copy/stickers.stickerdb" fetch $ALL
    echo "exit: $?"
fi

section "3. what Apple's own code stores when handed a file as it is"
if [ -x "$WORK/exp_apple_store" ]; then
    COPY="$WORK/copy/stickers.stickerdb"
    "$WORK/exp_apple_store" "$COPY" donate "$(uuidgen)" "donated png" - public.png 96 64 - "$F/still.png"
    "$WORK/exp_apple_store" "$COPY" donate "$(uuidgen)" "donated gif" - com.compuserve.gif 96 64 - "$F/anim.gif"
    "$WORK/exp_apple_store" "$COPY" donate "$(uuidgen)" "donated apng" - public.png 96 64 com.apple.stickers.role.animated "$F/anim.png"
    "$PY" imsticker.py --db "$COPY" export "$WORK/exported" --sticker 1 > /dev/null
    "$WORK/exp_apple_store" "$COPY" donate "$(uuidgen)" "donated heic" - public.heic 256 171 com.apple.stickers.role.still "$WORK/exported/sticker1_0_still.heic"
    echo "--- and the entry point StickerKit has for adding to the library itself:"
    "$WORK/exp_apple_store" "$COPY" stickerkit "$F/still.png" 96 64 add
    "$WORK/exp_apple_store" "$COPY" stickerkit "$F/anim.gif" 96 64 add
    "$WORK/exp_apple_store" "$COPY" stickerkit "$F/anim.png" 96 64 add
    echo "--- the copy afterwards:"
    dump_rows "$COPY"
    dump_history "$COPY"
    echo "--- and read back through Apple's code:"
    # shellcheck disable=SC2046
    "$WORK/exp_apple_store" "$COPY" fetch $(uuids "$COPY")
fi

section "3b. what StickerKit builds from image data"
"$WORK/exp_facade" stickerkit "$F/still.png" 96 64 add
"$WORK/exp_facade" stickerkit "$F/anim.gif" 96 64
"$WORK/exp_facade" stickerkit "$F/anim.png" 96 64

section "4. stickersd asked directly by a program Apple did not sign"
# shellcheck disable=SC2086
"$WORK/exp_facade" fetch $ALL
"$WORK/exp_facade" donate "$(uuidgen)" "donated over xpc" - public.png 96 64 - "$F/still.png"
sleep 1
echo "--- what the sticker system logged about it:"
sticker_log 1m 25

exit 0
