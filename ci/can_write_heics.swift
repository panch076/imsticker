// Can this Mac write an HEIF image sequence at all?
//
// GitHub's virtual Apple-silicon Macs cannot (the system has no HEVC video
// encoder to hand the frames to), and then no sticker tool can make an animated
// sticker there. This asks ImageIO for the smallest possible sequence, with
// none of imsticker's code involved, so a missing capability is not mistaken
// for a bug. Exit status 0 = yes, 3 = no.

import Foundation
import ImageIO
import CoreGraphics

func solidFrame(red: CGFloat, green: CGFloat, blue: CGFloat) -> CGImage? {
    guard let colorSpace = CGColorSpace(name: CGColorSpace.sRGB) else { return nil }
    guard let context = CGContext(
        data: nil,
        width: 64,
        height: 64,
        bitsPerComponent: 8,
        bytesPerRow: 0,
        space: colorSpace,
        bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue
    ) else {
        return nil
    }
    context.setFillColor(red: red, green: green, blue: blue, alpha: 1)
    context.fill(CGRect(x: 16, y: 16, width: 32, height: 32))
    return context.makeImage()
}

let target = URL(fileURLWithPath: NSTemporaryDirectory()).appendingPathComponent("can-write-\(getpid()).heics")
guard let destination = CGImageDestinationCreateWithURL(target as CFURL, "public.heics" as CFString, 2, nil) else {
    print("no: ImageIO will not create a public.heics destination")
    exit(3)
}
guard let first = solidFrame(red: 1, green: 0, blue: 0), let second = solidFrame(red: 0, green: 0, blue: 1) else {
    print("no: could not draw the test frames")
    exit(3)
}
let frameProperties: [CFString: Any] = [
    kCGImagePropertyHEICSDictionary: [kCGImagePropertyHEICSDelayTime: 0.1]
]
CGImageDestinationAddImage(destination, first, frameProperties as CFDictionary)
CGImageDestinationAddImage(destination, second, frameProperties as CFDictionary)
let written = CGImageDestinationFinalize(destination)
try? FileManager.default.removeItem(at: target)
if written {
    print("yes: this Mac can write HEIF image sequences")
    exit(0)
}
print("no: ImageIO could not write an HEIF image sequence on this Mac")
exit(3)
