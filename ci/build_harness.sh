#!/bin/bash
# Build the two test programs that call Apple's sticker code.
#
#   exp_facade        uses Apple's Objective-C entry points as they are, which
#                     means over XPC to stickersd
#   exp_apple_store   opens a sticker store in this process with Apple's own
#                     store class and points those entry points at it
#
# The Stickers framework has no Swift interface in the SDK, so the three
# declarations exp_apple_store needs (the store class, its persistence choice
# and its initialiser) are written out here from the names the framework
# exports.
#
#     bash ci/build_harness.sh <output folder>
set -euo pipefail
cd "$(dirname "$0")/.."

OUT="$1"
mkdir -p "$OUT/shim"
SDK=$(xcrun --show-sdk-path)

cat > "$OUT/shim/Stickers.swiftinterface" <<EOF
// swift-interface-format-version: 1.0
// swift-compiler-version: Apple Swift version 5.9
// swift-module-flags: -target $(uname -m)-apple-macos14.0 -enable-library-evolution -swift-version 5 -module-name Stickers
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

clang -fobjc-arc -DEXP_NO_MAIN -c ci/exp_facade.m -o "$OUT/facade.o"
clang -fobjc-arc -framework Foundation -framework CoreGraphics ci/exp_facade.m -o "$OUT/exp_facade"
swiftc -I "$OUT/shim" -F "$SDK/System/Library/PrivateFrameworks" -framework Stickers \
    -framework Foundation -framework CoreGraphics \
    -import-objc-header ci/exp_bridge.h ci/exp_apple_store.swift "$OUT/facade.o" \
    -o "$OUT/exp_apple_store"
