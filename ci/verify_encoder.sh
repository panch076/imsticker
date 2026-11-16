#!/bin/bash
# Run the real Swift encoder through the real tool on a Mac, into a stand-in
# store, then decode what was stored and compare it with the source images.
set -euo pipefail
cd "$(dirname "$0")/.."

PY=/usr/bin/python3
WORK="${RUNNER_TEMP:-${TMPDIR:-/tmp}}/imsticker-verify"
rm -rf "$WORK"
mkdir -p "$WORK"
STORE="$WORK/stand-in.stickerdb"
export IMSTICKER_BACKUP_DIR="$WORK/backups"
F=ci/fixtures

"$PY" - "$STORE" <<'PYTHON'
import sys
sys.path.insert(0, "tests")
import test_imsticker
test_imsticker.make_store(sys.argv[1])
PYTHON

add() {
    "$PY" imsticker.py --db "$STORE" add --no-restart "$@"
}

# The order here is the sticker numbering ci/check_facts.py expects.
echo "::group::import with the compiled encoder"
time add --max-edge 256 "$F/still.png" "$F/anim.gif" "$F/anim.png"
time add "$F/still.png"
time add --no-upscale --resample nearest "$F/anim.gif"
echo "::endgroup::"

echo "::group::import through the interpreter, as when swiftc cannot build"
mkdir -p "$WORK/nobuild"
printf '#!/bin/sh\necho "swiftc is disabled for this check" >&2\nexit 1\n' > "$WORK/nobuild/swiftc"
chmod +x "$WORK/nobuild/swiftc"
time env PATH="$WORK/nobuild:$PATH" "$PY" imsticker.py --db "$STORE" add --no-restart --max-edge 128 "$F/anim.png"
echo "::endgroup::"

"$PY" imsticker.py --db "$STORE" list -v
"$PY" imsticker.py --db "$STORE" export "$WORK/out"

echo "::group::what the system tools say about the files"
file "$WORK/out"/* || true
sips -g pixelWidth -g pixelHeight -g hasAlpha -g format "$WORK/out"/sticker1_*.heic || true
echo "::endgroup::"

echo "::group::decode with ImageIO"
swiftc -o "$WORK/inspect" ci/inspect_images.swift
"$WORK/inspect" "$F/still.png" "$F/anim.gif" "$F/anim.png" > "$WORK/source-facts.json"
"$WORK/inspect" "$WORK/out"/* > "$WORK/facts.json"
echo "::endgroup::"

"$PY" ci/check_facts.py "$WORK/facts.json" "$WORK/source-facts.json" "$F/expected.json"
