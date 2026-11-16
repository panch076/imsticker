#!/bin/bash
# Experiments against the real sticker store that stickersd keeps on this Mac.
#
# Three ways of putting a sticker in are tried and compared:
#   A. imsticker as it is: rows written with SQL
#   B. the same images saved through Core Data with Apple's own model
#   C. Apple's own entry point for donating a sticker, given a plain PNG, a GIF
#      and an APNG as they are
# Then Apple's own fetch entry point is asked for each of them, and the rows and
# the store's change history are printed, to see which route the system itself
# recognises and which would be picked up for iCloud.
#
# Best effort; never fails the job. Read the output.
set +e
cd "$(dirname "$0")/.."

PY=/usr/bin/python3
WORK="${RUNNER_TEMP:-${TMPDIR:-/tmp}}/imsticker-experiments"
rm -rf "$WORK"
mkdir -p "$WORK"
export IMSTICKER_BACKUP_DIR="$WORK/backups"
F=ci/fixtures
MODEL=/System/Library/PrivateFrameworks/Stickers.framework/Versions/A/Resources/Stickers.momd
ME=$(id -u)

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
    "$PY" - "$STORE" <<'PYTHON'
import sqlite3, sys, uuid
rows = sqlite3.connect(sys.argv[1]).execute("select ZIDENTIFIER from ZMANAGEDSTICKER order by Z_PK").fetchall()
print(" ".join(str(uuid.UUID(bytes=bytes(row[0]))).upper() for row in rows if row[0]))
PYTHON
}

dump_rows() {
    sqlite3 -header "$STORE" \
        "select Z_PK, Z_ENT, Z_OPT, ZTYPE, ZEFFECT, ZVERSION, ZBYTECOUNT, ZLIBRARYINDEX, ZCREATIONDATE, ZNAME, ZSEARCHTEXT, ZEXTERNALURI, ZATTRIBUTIONBUNDLEIDENTIFIER, length(ZMETADATA) as META from ZMANAGEDSTICKER order by Z_PK" \
        "select Z_PK, Z_ENT, Z_OPT, ZSTICKER, ZINDEX, ZISPREFERRED, ZVERSION, ZBYTECOUNT, ZSIZE_W, ZSIZE_H, ZROLE, ZUTI, length(ZDATA) as BYTES, hex(substr(ZDATA, 1, 12)) as HEAD from ZMANAGEDREPRESENTATION order by Z_PK" \
        "select Z_NAME, Z_MAX from Z_PRIMARYKEY where Z_ENT < 100"
}

dump_history() {
    sqlite3 -header "$STORE" \
        "select count(*) as TRANSACTIONS from ATRANSACTION" \
        "select Z_PK, Z_ENT, Z_OPT, ZAUTHORTS, ZBUNDLEIDTS, ZCONTEXTNAMETS, ZPROCESSIDTS, ZTIMESTAMP, ZAUTHOR, ZBUNDLEID, ZCONTEXTNAME, ZPROCESSID, hex(ZQUERYGEN) as QUERYGEN from ATRANSACTION order by Z_PK" \
        "select Z_PK, Z_ENT, Z_OPT, ZCHANGETYPE, ZENTITY, ZENTITYPK, ZTRANSACTIONID, hex(ZCOLUMNS) as COLUMNS from ACHANGE order by Z_PK" \
        "select * from ATRANSACTIONSTRING order by Z_PK" \
        "select Z_PK, ZENTITYID, ZENTITYPK, ZNEEDSUPLOAD, ZCKRECORDNAME from ANSCKRECORDMETADATA order by Z_PK"
}

section "the store"
start_daemon
pgrep -lf stickersd
STORE=$(find_store)
echo "store: [$STORE]"
if [ -z "$STORE" ]; then
    echo "stickersd has no store on this Mac; nothing to experiment on."
    log show --last 3m --style compact --predicate 'process == "stickersd" AND subsystem BEGINSWITH "com.apple.stickers"' 2>&1 | cut -c1-300 | tail -40
    exit 0
fi
ls -la "$(dirname "$STORE")"
sqlite3 "$STORE" ".tables" | head -20
dump_rows

section "build the helpers"
clang -fobjc-arc -framework Foundation -framework CoreGraphics ci/exp_facade.m -o "$WORK/exp_facade" 2>&1 | head -40
ls -la "$WORK/exp_facade"
"$WORK/exp_facade" signatures

section "A. imsticker as it is (SQL rows)"
"$PY" imsticker.py --db "$STORE" add --no-restart --max-edge 256 --name "sql rows" "$F/still.png"
dump_history | head -40

section "B. the same images through Core Data with Apple's model"
"$PY" imsticker.py --db "$STORE" export "$WORK/exported" --sticker 1
ls -la "$WORK/exported"
swift ci/exp_coredata.swift "$STORE" "$MODEL" "core data" \
    "com.apple.stickers.role.still|public.heic|256|171|1|0|$WORK/exported/sticker1_0_still.heic" \
    "com.apple.stickers.role.keyboard|public.heic|96|64|0|1|$WORK/exported/sticker1_1_keyboard.heic"
dump_history

section "C. Apple's donate entry point, given the source files as they are"
PNG_ID=$(uuidgen)
GIF_ID=$(uuidgen)
APNG_ID=$(uuidgen)
HEIC_ID=$(uuidgen)
"$WORK/exp_facade" donate "$PNG_ID" "donated png" - public.png 96 64 - "$F/still.png"
"$WORK/exp_facade" donate "$GIF_ID" "donated gif" - com.compuserve.gif 96 64 - "$F/anim.gif"
"$WORK/exp_facade" donate "$APNG_ID" "donated apng" - public.png 96 64 com.apple.stickers.role.animated "$F/anim.png"
"$WORK/exp_facade" donate "$HEIC_ID" "donated heic" "sticker:///user/identifier/$HEIC_ID" public.heic 256 171 com.apple.stickers.role.still "$WORK/exported/sticker1_0_still.heic"
sleep 2

section "rows after all three"
dump_rows

section "history after all three"
dump_history

section "what Apple's fetch entry point returns"
ALL=$(uuids)
echo "in the store: $ALL"
"$WORK/exp_facade" fetch $ALL $PNG_ID $GIF_ID $APNG_ID $HEIC_ID

section "the same after stickersd restarts"
killall stickersd 2>/dev/null
sleep 2
start_daemon
"$WORK/exp_facade" fetch $ALL $PNG_ID $GIF_ID $APNG_ID $HEIC_ID | head -80
dump_rows
sqlite3 "$STORE" "pragma integrity_check"

section "touching an SQL-row sticker through Apple's entry point, then the history"
FIRST=$(echo "$ALL" | cut -d' ' -f1)
"$WORK/exp_facade" touch "$FIRST"
sleep 1
dump_history

section "stickersd's own log"
log show --last 6m --style compact --predicate 'process == "stickersd" AND (subsystem BEGINSWITH "com.apple.stickers" OR eventMessage CONTAINS[c] "sticker")' 2>&1 | cut -c1-360 | tail -150

section "what the helper processes logged"
log show --last 6m --style compact --predicate 'process == "exp_facade" AND (subsystem BEGINSWITH "com.apple.stickers" OR eventMessage CONTAINS[c] "sticker")' 2>&1 | cut -c1-360 | tail -60

exit 0
