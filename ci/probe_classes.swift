// Load a system framework and print what the Objective-C runtime knows about
// it: classes, their properties, instance variables and methods, plus the
// string constants and Swift field names stored in the image. This is how the
// names in README.md (roles, types, identifiers) can be checked against the
// operating system itself.
//
//     swift ci/probe_classes.swift /System/Library/PrivateFrameworks/Stickers.framework/Stickers

import Foundation
import ObjectiveC
import MachO

let requested = CommandLine.arguments.count > 1
    ? CommandLine.arguments[1]
    : "/System/Library/PrivateFrameworks/Stickers.framework/Stickers"

if dlopen(requested, RTLD_NOW) == nil {
    let reason = String(cString: dlerror())
    print("dlopen failed for \(requested): \(reason)")
    exit(0)
}

// dyld reports the image under its real path (.../Versions/A/Name), so find it.
let binaryName = (requested as NSString).lastPathComponent
let frameworkName = ((requested as NSString).deletingLastPathComponent as NSString).lastPathComponent
var imagePath: String? = nil
var imageSlot: UInt32 = 0
for slot in 0..<_dyld_image_count() {
    guard let raw = _dyld_get_image_name(slot) else { continue }
    let name = String(cString: raw)
    if name.hasSuffix("/" + binaryName) && name.contains("/" + frameworkName + "/") {
        imagePath = name
        imageSlot = slot
        break
    }
}
guard let image = imagePath else {
    print("loaded, but not found among dyld's images: \(requested)")
    exit(0)
}
print("IMAGE \(image)")

func printMethods(_ cls: AnyClass, _ prefix: String) {
    var count: UInt32 = 0
    guard let list = class_copyMethodList(cls, &count) else { return }
    for index in 0..<Int(count) {
        print("  \(prefix) \(NSStringFromSelector(method_getName(list[index])))")
    }
    free(list)
}

func printProperties(_ cls: AnyClass) {
    var count: UInt32 = 0
    guard let list = class_copyPropertyList(cls, &count) else { return }
    for index in 0..<Int(count) {
        let name = String(cString: property_getName(list[index]))
        var attributes = ""
        if let raw = property_getAttributes(list[index]) {
            attributes = String(cString: raw)
        }
        print("  prop \(name) \(attributes)")
    }
    free(list)
}

func printIvars(_ cls: AnyClass) {
    var count: UInt32 = 0
    guard let list = class_copyIvarList(cls, &count) else { return }
    for index in 0..<Int(count) {
        var name = "?"
        if let raw = ivar_getName(list[index]) {
            name = String(cString: raw)
        }
        var encoding = ""
        if let raw = ivar_getTypeEncoding(list[index]) {
            encoding = String(cString: raw)
        }
        print("  ivar \(name) \(encoding)")
    }
    free(list)
}

var classCount: UInt32 = 0
if let names = objc_copyClassNamesForImage(image, &classCount) {
    print("CLASSES \(classCount)")
    for index in 0..<Int(classCount) {
        let className = String(cString: names[index])
        guard let cls: AnyClass = NSClassFromString(className) else {
            print("CLASS \(className) (not resolvable)")
            continue
        }
        var parent = "-"
        if let superclass = class_getSuperclass(cls) {
            parent = NSStringFromClass(superclass)
        }
        print("CLASS \(className) : \(parent)")
        printProperties(cls)
        printIvars(cls)
        printMethods(cls, "-")
        if let meta: AnyClass = object_getClass(cls) {
            printMethods(meta, "+")
        }
    }
    free(names)
} else {
    print("CLASSES none")
}

// NUL-separated strings of one section of the image, as loaded in memory.
func printStrings(_ header: UnsafePointer<mach_header_64>, _ segment: String, _ section: String, limit: Int) {
    var size: UInt = 0
    guard let start = getsectiondata(header, segment, section, &size), size > 0 else {
        print("SECTION \(segment),\(section): not present")
        return
    }
    print("SECTION \(segment),\(section): \(size) bytes")
    let bytes = UnsafeBufferPointer(start: start, count: Int(size))
    var current: [UInt8] = []
    var printed = 0
    for byte in bytes {
        if byte != 0 {
            current.append(byte)
            continue
        }
        if current.count >= 3, let text = String(bytes: current, encoding: .utf8) {
            print("  S \(text)")
            printed += 1
            if printed >= limit {
                print("  ... stopped after \(limit) strings")
                return
            }
        }
        current.removeAll(keepingCapacity: true)
    }
}

if let rawHeader = _dyld_get_image_header(imageSlot) {
    let header = UnsafeRawPointer(rawHeader).assumingMemoryBound(to: mach_header_64.self)
    printStrings(header, "__TEXT", "__cstring", limit: 5000)
    printStrings(header, "__TEXT", "__swift5_reflstr", limit: 5000)
    printStrings(header, "__TEXT", "__swift5_typeref", limit: 0)
}
