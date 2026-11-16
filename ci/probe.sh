#!/bin/bash
# Look at the real sticker system on this Mac: where its pieces live, what its
# Core Data model says, whether stickersd will build a store here, and whether
# stickers written by imsticker survive a restart of the daemon.
#
# Everything is best effort and read-only apart from the sticker store itself.
# This script never fails the job; read its output.
set +e
cd "$(dirname "$0")/.."

PY=/usr/bin/python3
WORK="${RUNNER_TEMP:-${TMPDIR:-/tmp}}/imsticker-probe"
rm -rf "$WORK"
mkdir -p "$WORK"
FW=/System/Library/PrivateFrameworks/Stickers.framework
ME=$(id -u)

section() {
    echo
    echo "=== $* ==="
}

section "sticker frameworks on disk"
ls -d /System/Library/PrivateFrameworks/*[Ss]ticker* /System/Library/Frameworks/*[Ss]ticker* 2>/dev/null
find "$FW" -maxdepth 5 2>/dev/null | head -150

section "core data model"
find /System/Library/PrivateFrameworks/*[Ss]ticker* -maxdepth 6 -name "*.momd" 2>/dev/null > "$WORK/models.txt"
cat "$WORK/models.txt"
while IFS= read -r model; do
    [ -n "$model" ] || continue
    ls -la "$model"
    swift ci/probe_model.swift "$model" 2>&1 | head -400
done < "$WORK/models.txt"

section "launchd jobs"
grep -l -i -r sticker /System/Library/LaunchAgents /System/Library/LaunchDaemons 2>/dev/null > "$WORK/plists.txt"
cat "$WORK/plists.txt"
LABEL=""
BIN=""
while IFS= read -r plist; do
    [ -n "$plist" ] || continue
    plutil -p "$plist"
    label=$(/usr/libexec/PlistBuddy -c "Print :Label" "$plist" 2>/dev/null)
    program=$(/usr/libexec/PlistBuddy -c "Print :ProgramArguments:0" "$plist" 2>/dev/null)
    [ -n "$program" ] || program=$(/usr/libexec/PlistBuddy -c "Print :Program" "$plist" 2>/dev/null)
    case "$label$program" in
        *stickersd*) LABEL="$label"; BIN="$program" ;;
    esac
done < "$WORK/plists.txt"
echo "stickersd label=[$LABEL] binary=[$BIN]"

section "stickersd binary"
if [ -n "$BIN" ] && [ -e "$BIN" ]; then
    ls -la "$BIN"
    file "$BIN"
    otool -L "$BIN" | head -80
    echo "--- entitlements"
    codesign -d --entitlements - --xml "$BIN" 2>/dev/null > "$WORK/entitlements.plist"
    plutil -p "$WORK/entitlements.plist" 2>/dev/null || codesign -d --entitlements - "$BIN" 2>&1 | head -150
    echo "--- strings"
    strings - "$BIN" | grep -i -E "sticker|role|heic|png|gif|uti|cloudkit|group\.|\.db|container|genmoji|emoji" | sort -u | head -400
else
    echo "no stickersd binary on disk at [$BIN]"
fi

section "framework exports: types"
dyld_info -exports "$FW/Versions/A/Stickers" 2>&1 | xcrun swift-demangle > "$WORK/exports.txt"
wc -l < "$WORK/exports.txt"
head -5 "$WORK/exports.txt"
grep -o "type metadata accessor for .*" "$WORK/exports.txt" | sort -u | head -500

section "framework exports: enum cases"
grep -o "enum case for .*" "$WORK/exports.txt" | sort -u | cut -c1-240 | head -600

section "framework exports: sticker data and import paths"
grep -i -E "role|representation|effect|uti|import|png|gif|animated|externalURI|libraryIndex|metadata|attribution" "$WORK/exports.txt" \
    | grep -v -E "protocol witness|merged |outlined |lazy protocol|witness table" | cut -c1-240 | sort -u | head -600

section "runtime classes and strings"
swift ci/probe_classes.swift "$FW/Stickers" > "$WORK/classes.txt" 2>&1
wc -l < "$WORK/classes.txt"
grep -c "^CLASS " "$WORK/classes.txt"
grep -v "^  S " "$WORK/classes.txt" | cut -c1-240 | head -2500
echo "--- strings that look like identifiers, types or paths"
grep "^  S " "$WORK/classes.txt" | grep -i -E "sticker|role|public\.|com\.apple|heic|png|gif|://|\.db|uti|genmoji|emoji|memoji|effect|animated|keyboard|variant" | cut -c1-240 | sort -u | head -900

section "the real store"
find_store() {
    find "$HOME/Library/Stickers" "$HOME/Library/Group Containers" "$HOME/Library/Containers" \
        -maxdepth 6 -name "*.stickerdb" 2>/dev/null | head -5
}
launchctl print "gui/$ME" 2>/dev/null | grep -i sticker | head -20
echo "before: [$(find_store)]"
if [ -n "$LABEL" ]; then
    launchctl kickstart -p "gui/$ME/$LABEL" 2>&1
    sleep 5
fi
pgrep -lf stickersd
echo "after starting stickersd: [$(find_store)]"
ls -la "$HOME/Library/Stickers" "$HOME/Library/Group Containers/com.apple.stickersd.group" \
    "$HOME/Library/Group Containers/com.apple.stickersd.group/Stickers" 2>&1 | head -60
STORE=$(find_store | head -1)

if [ -n "$STORE" ]; then
    section "real store: layout"
    echo "store: $STORE"
    sqlite3 "$STORE" ".tables"
    sqlite3 "$STORE" ".schema ZMANAGEDSTICKER" ".schema ZMANAGEDREPRESENTATION" ".schema Z_PRIMARYKEY"
    sqlite3 "$STORE" "select * from Z_PRIMARYKEY" "select Z_VERSION, Z_UUID from Z_METADATA" \
        "pragma journal_mode" "select count(*) from ZMANAGEDSTICKER" "select count(*) from ZMANAGEDREPRESENTATION"
    sqlite3 "$STORE" "select writefile('$WORK/store-metadata.plist', Z_PLIST) from Z_METADATA" > /dev/null
    plutil -p "$WORK/store-metadata.plist" 2>&1 | head -80
    echo "--- every table"
    sqlite3 "$STORE" ".schema" | cut -c1-400 | head -150

    section "real store: found without being told where"
    "$PY" imsticker.py list | head -20

    section "real store: import"
    export IMSTICKER_BACKUP_DIR="$WORK/backups"
    "$PY" imsticker.py --db "$STORE" add --max-edge 256 ci/fixtures/still.png ci/fixtures/anim.gif ci/fixtures/anim.png
    echo "imsticker exit code: $?"
    sleep 3
    if [ -n "$LABEL" ]; then
        launchctl kickstart -p "gui/$ME/$LABEL" 2>&1
    fi
    sleep 6
    pgrep -lf stickersd

    section "real store: after stickersd restarted"
    "$PY" imsticker.py --db "$STORE" list -v | head -80
    sqlite3 "$STORE" "pragma integrity_check" "select Z_NAME, Z_MAX from Z_PRIMARYKEY" \
        "select Z_PK, Z_ENT, Z_OPT, ZTYPE, ZEFFECT, ZLIBRARYINDEX, ZBYTECOUNT, ZEXTERNALURI from ZMANAGEDSTICKER"

    section "stickersd log"
    log show --last 5m --style compact --predicate 'process == "stickersd"' 2>&1 | cut -c1-400 | tail -150
else
    echo "stickersd did not create a store here, so the import could only be checked against the stand-in."
    section "stickersd log"
    log show --last 5m --style compact --predicate 'process == "stickersd"' 2>&1 | cut -c1-400 | tail -60
fi

section "messages sticker pieces on disk"
ls /System/Library/Messages/iMessageBalloons 2>/dev/null | head -40
find /System/Library/Messages /System/Applications/Messages.app -maxdepth 6 -iname "*ticker*" 2>/dev/null | head -40

exit 0
