#!/bin/bash
# Read-only look at the sticker frameworks on this Mac: what is on disk, the
# Core Data model, what each framework exports, and what the Objective-C
# runtime and the string tables say. Best effort; never fails the job.
set +e
cd "$(dirname "$0")/.."

WORK="${RUNNER_TEMP:-${TMPDIR:-/tmp}}/imsticker-frameworks"
rm -rf "$WORK"
mkdir -p "$WORK"
PF=/System/Library/PrivateFrameworks
PUB=/System/Library/Frameworks

section() {
    echo
    echo "=== $* ==="
}

section "sticker frameworks on disk"
ls -d "$PF"/*[Ss]ticker* "$PUB"/*[Ss]ticker* 2>/dev/null
for framework in "$PF"/*[Ss]ticker* "$PUB"/*[Ss]ticker*; do
    find "$framework" -maxdepth 5 -not -name "*.lproj" 2>/dev/null | grep -v -E "\.lproj/|_CodeSignature" | head -60
done

section "is any of it in the SDK"
SDK=$(xcrun --show-sdk-path)
echo "sdk: $SDK"
ls "$SDK/System/Library/Frameworks" | grep -i sticker
ls "$SDK/System/Library/PrivateFrameworks" 2>/dev/null | grep -i sticker
echo "--- every file the SDK has for them"
find "$SDK/System/Library/Frameworks/StickerKit.framework/" "$SDK/System/Library/Frameworks/StickerFoundation.framework/" \
    "$SDK/System/Library/PrivateFrameworks/Stickers.framework/" "$SDK/System/Library/PrivateFrameworks/StickerKitInternal.framework/" \
    "$SDK/System/Library/PrivateFrameworks/StickerFoundationInternal.framework/" -type f 2>/dev/null \
    | sed "s|$SDK||" | while IFS= read -r file; do
        echo "$(wc -c < "$SDK$file" | tr -d ' ') bytes  $file"
    done
echo "--- what the SDK's headers and interfaces declare (declaration lines only)"
find "$SDK/System/Library/Frameworks/StickerKit.framework/" "$SDK/System/Library/Frameworks/StickerFoundation.framework/" \
    -type f \( -name "*.h" -o -name "*.swiftinterface" -o -name "*.modulemap" \) 2>/dev/null | sort | while IFS= read -r file; do
        echo "## ${file#"$SDK"}"
        grep -v -E "^\s*(//|/\*|\*)" "$file" \
            | grep -E "@interface|@protocol|@property|^[-+] *\(|NS_ENUM|NS_OPTIONS|typedef|extern|FOUNDATION_EXPORT|#import|module |header |export |public |open |@objc|@available|^ *(final )?(class|struct|enum|protocol|extension|func|var|let|init|case|typealias) " \
            | cut -c1-220 | head -200
    done
echo "--- what the private frameworks' link stubs export (first lines)"
for stub in "$SDK"/System/Library/PrivateFrameworks/Stickers.framework/*.tbd "$SDK"/System/Library/PrivateFrameworks/Stickers.framework/Versions/A/*.tbd; do
    [ -f "$stub" ] || continue
    echo "## ${stub#"$SDK"}"
    head -12 "$stub" | cut -c1-300
    break
done

section "core data model"
MODEL="$PF/Stickers.framework/Versions/A/Resources/Stickers.momd"
ls -la "$MODEL"
plutil -p "$MODEL/VersionInfo.plist" 2>/dev/null | head -60
swift ci/probe_model.swift "$MODEL" 2>&1 | head -200

section "stickersd"
plutil -p /System/Library/LaunchAgents/com.apple.stickersd.plist
BIN="$PF/Stickers.framework/Support/stickersd"
codesign -d --entitlements - --xml "$BIN" 2>/dev/null > "$WORK/entitlements.plist"
plutil -p "$WORK/entitlements.plist"
echo "--- strings in the daemon"
strings - "$BIN" 2>/dev/null | LC_ALL=C grep -a -i -E "sticker|role|heic|png|gif|cloudkit|group\.|container|genmoji|emoji|entitle" | LC_ALL=C sort -u | head -300

# Everything a framework exports, with Swift names made readable.
exports() {
    dyld_info -exports "$1" 2>&1 | xcrun swift-demangle
}

section "Stickers.framework: every export"
exports "$PF/Stickers.framework/Versions/A/Stickers" | sed -E 's/^ +0x[0-9A-Fa-f]+ +//' \
    | grep -v -E "^(protocol witness|merged |outlined |lazy protocol witness|associated type witness|base witness table|method lookup function|class metadata base offset|reflection metadata|nominal type descriptor|full type metadata|type metadata for|metaclass for|protocol conformance descriptor|property descriptor|method descriptor|dispatch thunk)" \
    | cut -c1-300 | LC_ALL=C sort -u

section "Stickers.framework: what its protocols require"
exports "$PF/Stickers.framework/Versions/A/Stickers" | sed -E 's/^ +0x[0-9A-Fa-f]+ +//' \
    | grep -E "^method descriptor for Stickers\.(StickerStoreProtocol|StickerStoreProtocolInternal|StickerStoreDataMapper|ProxyProvider)\." \
    | sed -E 's/^method descriptor for //' | cut -c1-400 | LC_ALL=C sort -u
echo "--- conformances"
exports "$PF/Stickers.framework/Versions/A/Stickers" | sed -E 's/^ +0x[0-9A-Fa-f]+ +//' \
    | grep -E "^protocol conformance descriptor for Stickers\.(StickerStore|CoreDataDataMapper|XPCDataMapper|InMemoryDataMapper) " | cut -c1-300 | LC_ALL=C sort -u

for name in StickerFoundation StickerKit StickerFoundationInternal StickerKitInternal; do
    for root in "$PUB" "$PF"; do
        binary="$root/$name.framework/Versions/A/$name"
        [ -d "$root/$name.framework" ] || continue
        section "$name: exports"
        exports "$binary" | sed -E 's/^ +0x[0-9A-Fa-f]+ +//' > "$WORK/$name.exports"
        wc -l < "$WORK/$name.exports"
        echo "--- types"
        grep -o "type metadata accessor for .*" "$WORK/$name.exports" | LC_ALL=C sort -u | cut -c1-200 | head -400
        echo "--- enum cases"
        grep -o "enum case for .*" "$WORK/$name.exports" | LC_ALL=C sort -u | cut -c1-260 | head -500
        echo "--- anything about importing, roles, variants and formats"
        grep -i -E "role|variant|import|drop|paste|png|gif|heic|animated|uttype|uniformtype|subject|lift|addSticker|createSticker|saveSticker|donate" "$WORK/$name.exports" \
            | grep -v -E "^(protocol witness|merged |outlined |lazy protocol witness|associated type witness|base witness table|method lookup function|class metadata base offset|reflection metadata|nominal type descriptor|full type metadata|type metadata for|metaclass for|protocol conformance descriptor|property descriptor|method descriptor|dispatch thunk)" \
            | cut -c1-300 | LC_ALL=C sort -u | head -500

        section "$name: classes and strings"
        swift ci/probe_classes.swift "$root/$name.framework/$name" > "$WORK/$name.classes" 2>&1
        grep -E "^(IMAGE|CLASSES|SECTION|dlopen|loaded)" "$WORK/$name.classes"
        echo "--- class list"
        grep "^CLASS " "$WORK/$name.classes" | cut -c1-200 | head -600
        echo "--- members of classes that look relevant"
        awk '/^CLASS /{keep = ($0 ~ /[Ss]ticker|[Ii]mport|[Dd]rop|[Pp]aste|[Vv]ariant|[Rr]epresentation|[Ss]tore|[Ll]ibrary|[Ss]ubject|[Aa]nimat/)} keep' "$WORK/$name.classes" \
            | grep -v "^  S " | cut -c1-240 | head -1500
        echo "--- strings"
        grep "^  S " "$WORK/$name.classes" \
            | LC_ALL=C grep -a -i -E "sticker|role|variant|public\.|com\.apple|heic|png|gif|://|uti|genmoji|emoji|memoji|effect|animated|keyboard|import|drop|paste|subject|lift" \
            | cut -c1-240 | LC_ALL=C sort -u | head -900
    done
done

section "Stickers.framework: classes, the XPC protocols, strings"
swift ci/probe_classes.swift "$PF/Stickers.framework/Stickers" \
    _TtP8Stickers25RemoteStickersAPIProtocol_ _TtP8Stickers29LaunchStateManagerAPIProtocol_ > "$WORK/Stickers.classes" 2>&1
grep -v "^  S " "$WORK/Stickers.classes" | cut -c1-260
echo "--- every string constant"
grep "^  S " "$WORK/Stickers.classes" | cut -c1-240 | LC_ALL=C sort -u

exit 0
