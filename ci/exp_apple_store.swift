// Experiment: run Apple's own sticker store code in this process, on a copy of
// a sticker store, instead of asking stickersd over XPC (which only answers
// Apple-signed apps).
//
// If Apple's code reads back a sticker this project wrote, then the system
// will too: it is the same code stickersd runs. And whatever Apple's code
// writes when handed an image shows what a sticker made by Apple looks like.
//
// The Stickers framework has no Swift interface in the SDK, so the three
// declarations needed here (the store class, its persistence choice and its
// initialiser) are written out by ci/experiments.sh from the names the
// framework exports.
//
//     exp_apple_store <store file> fetch <uuid>... | donate ... | touch <uuid>

import Foundation
import Stickers

setvbuf(stdout, nil, _IONBF, 0)

let arguments = CommandLine.arguments
if arguments.count < 3 {
    print("usage: exp_apple_store <store file> <command> ...")
    exit(2)
}
let storeURL = URL(fileURLWithPath: arguments[1])
let store = StickerStore(persistence: .customFile(storeURL))
print("apple-store: opened \(storeURL.path) with Apple's StickerStore: \(store)")
exit(STKExperimentRun(store, Array(arguments.dropFirst(2))))
