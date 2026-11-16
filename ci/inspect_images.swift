// Decode image files with ImageIO, the decoder macOS and iOS use themselves,
// and print what is really in them as JSON: container type, frame count, loop
// count, per-frame delay, size, and the colour of a few sample pixels.
//
//     swiftc -o inspect ci/inspect_images.swift && ./inspect file...

import Foundation
import ImageIO
import CoreGraphics

struct Sample: Encodable {
    let name: String
    let x: Int
    let y: Int
    let rgba: [Int]
}

struct FrameFacts: Encodable {
    let index: Int
    let container: String
    let delay: Double?
    let unclampedDelay: Double?
    let width: Int
    let height: Int
    let samples: [Sample]
}

struct FileFacts: Encodable {
    let file: String
    let type: String
    let frameCount: Int
    let loopCount: Int?
    let frames: [FrameFacts]
    let error: String?
}

struct SamplePoint {
    let name: String
    let fx: Double
    let fy: Double
}

struct Bitmap {
    let data: [UInt8]
    let bytesPerRow: Int
}

struct Timing {
    let container: String
    let delay: Double?
    let unclamped: Double?
}

// Which animation dictionary a frame carries, and the delays in it.
func timing(_ properties: [CFString: Any]?) -> Timing {
    guard let properties = properties else {
        return Timing(container: "none", delay: nil, unclamped: nil)
    }
    let containers: [(String, CFString, CFString, CFString)] = [
        ("heics", kCGImagePropertyHEICSDictionary, kCGImagePropertyHEICSDelayTime, kCGImagePropertyHEICSUnclampedDelayTime),
        ("gif", kCGImagePropertyGIFDictionary, kCGImagePropertyGIFDelayTime, kCGImagePropertyGIFUnclampedDelayTime),
        ("png", kCGImagePropertyPNGDictionary, kCGImagePropertyAPNGDelayTime, kCGImagePropertyAPNGUnclampedDelayTime)
    ]
    for (name, dictionaryKey, delayKey, unclampedKey) in containers {
        guard let dictionary = properties[dictionaryKey] as? [CFString: Any] else { continue }
        let delay = dictionary[delayKey] as? Double
        let unclamped = dictionary[unclampedKey] as? Double
        if delay != nil || unclamped != nil {
            return Timing(container: name, delay: delay, unclamped: unclamped)
        }
    }
    return Timing(container: "none", delay: nil, unclamped: nil)
}

func loopCount(_ source: CGImageSource) -> Int? {
    guard let properties = CGImageSourceCopyProperties(source, nil) as? [CFString: Any] else {
        return nil
    }
    let keys: [(CFString, CFString)] = [
        (kCGImagePropertyHEICSDictionary, kCGImagePropertyHEICSLoopCount),
        (kCGImagePropertyGIFDictionary, kCGImagePropertyGIFLoopCount),
        (kCGImagePropertyPNGDictionary, kCGImagePropertyAPNGLoopCount)
    ]
    for (dictionaryKey, countKey) in keys {
        guard let dictionary = properties[dictionaryKey] as? [CFString: Any] else { continue }
        if let count = dictionary[countKey] as? Int {
            return count
        }
    }
    return nil
}

// Render into our own RGBA buffer. Row 0 of the buffer is the top of the image.
func render(_ image: CGImage) -> Bitmap? {
    let width = image.width
    let height = image.height
    let bytesPerRow = width * 4
    var data = [UInt8](repeating: 0, count: bytesPerRow * height)
    guard let colorSpace = CGColorSpace(name: CGColorSpace.sRGB) else {
        return nil
    }
    let drawn = data.withUnsafeMutableBytes { (raw: UnsafeMutableRawBufferPointer) -> Bool in
        guard let context = CGContext(
            data: raw.baseAddress,
            width: width,
            height: height,
            bitsPerComponent: 8,
            bytesPerRow: bytesPerRow,
            space: colorSpace,
            bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue
        ) else {
            return false
        }
        context.draw(image, in: CGRect(x: 0, y: 0, width: width, height: height))
        return true
    }
    if !drawn {
        return nil
    }
    return Bitmap(data: data, bytesPerRow: bytesPerRow)
}

// Straight (un-premultiplied) RGBA of one pixel.
func pixel(_ bitmap: Bitmap, _ x: Int, _ y: Int) -> [Int] {
    let offset = y * bitmap.bytesPerRow + x * 4
    let alpha = Int(bitmap.data[offset + 3])
    if alpha == 0 {
        return [0, 0, 0, 0]
    }
    var result: [Int] = []
    for channel in 0..<3 {
        let value = Int(bitmap.data[offset + channel])
        result.append(min(255, (value * 255 + alpha / 2) / alpha))
    }
    result.append(alpha)
    return result
}

func inspect(_ path: String, _ points: [SamplePoint]) -> FileFacts {
    let url = URL(fileURLWithPath: path)
    let name = url.lastPathComponent
    guard let source = CGImageSourceCreateWithURL(url as CFURL, nil) else {
        return FileFacts(file: name, type: "?", frameCount: 0, loopCount: nil, frames: [], error: "cannot open")
    }
    let type = (CGImageSourceGetType(source) as String?) ?? "?"
    let count = CGImageSourceGetCount(source)
    var frames: [FrameFacts] = []
    var problem: String? = nil
    for index in 0..<count {
        guard let image = CGImageSourceCreateImageAtIndex(source, index, nil) else {
            problem = "cannot decode frame \(index)"
            break
        }
        let properties = CGImageSourceCopyPropertiesAtIndex(source, index, nil) as? [CFString: Any]
        let when = timing(properties)
        var samples: [Sample] = []
        if let bitmap = render(image) {
            for point in points {
                let x = min(image.width - 1, Int(Double(image.width) * point.fx))
                let y = min(image.height - 1, Int(Double(image.height) * point.fy))
                samples.append(Sample(name: point.name, x: x, y: y, rgba: pixel(bitmap, x, y)))
            }
        } else {
            problem = "cannot render frame \(index)"
        }
        frames.append(FrameFacts(
            index: index,
            container: when.container,
            delay: when.delay,
            unclampedDelay: when.unclamped,
            width: image.width,
            height: image.height,
            samples: samples
        ))
    }
    return FileFacts(file: name, type: type, frameCount: count, loopCount: loopCount(source), frames: frames, error: problem)
}

// Same sample points as ci/fixtures/expected.json.
let samplePoints = [
    SamplePoint(name: "center", fx: 0.5, fy: 0.5),
    SamplePoint(name: "corner", fx: 0.04, fy: 0.06),
    SamplePoint(name: "stripe", fx: 0.5, fy: 0.875)
]
var allFacts: [FileFacts] = []
for path in CommandLine.arguments.dropFirst() {
    allFacts.append(inspect(path, samplePoints))
}
let jsonEncoder = JSONEncoder()
jsonEncoder.outputFormatting = [.prettyPrinted, .sortedKeys]
guard let json = try? jsonEncoder.encode(allFacts) else {
    FileHandle.standardError.write("cannot serialize the facts\n".data(using: .utf8)!)
    exit(1)
}
FileHandle.standardOutput.write(json)
FileHandle.standardOutput.write("\n".data(using: .utf8)!)
