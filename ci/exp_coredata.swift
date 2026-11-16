// Experiment: add a sticker to the sticker store through Core Data itself,
// using the model Apple ships on disk, instead of writing SQL rows by hand.
//
// Why it matters: the store is mirrored to iCloud from Core Data's persistent
// history. Rows written with plain SQL never enter that history. A save made
// through Core Data does, so this is the route that should sync.
//
//     swift ci/exp_coredata.swift <store> <model.momd> <search text> <rep>...
//
// Each <rep> is  role|uti|width|height|preferred(0 or 1)|index|file

import Foundation
import CoreData

struct RepresentationSpec {
    let role: String
    let uti: String
    let width: Double
    let height: Double
    let preferred: Bool
    let index: Int32
    let data: Data
}

func parseRepresentation(_ text: String) -> RepresentationSpec? {
    let parts = text.components(separatedBy: "|")
    guard parts.count == 7 else { return nil }
    guard let width = Double(parts[2]), let height = Double(parts[3]), let index = Int32(parts[5]) else {
        return nil
    }
    guard let data = FileManager.default.contents(atPath: parts[6]) else { return nil }
    return RepresentationSpec(
        role: parts[0],
        uti: parts[1],
        width: width,
        height: height,
        preferred: parts[4] == "1",
        index: index,
        data: data
    )
}

func fail(_ message: String) -> Never {
    print("coredata: \(message)")
    exit(1)
}

let arguments = CommandLine.arguments
if arguments.count < 5 {
    fail("usage: exp_coredata.swift <store> <model.momd> <search text> <rep>...")
}
let storeURL = URL(fileURLWithPath: arguments[1])
let modelURL = URL(fileURLWithPath: arguments[2])
let searchText = arguments[3]
var representations: [RepresentationSpec] = []
for text in arguments.dropFirst(4) {
    guard let spec = parseRepresentation(text) else {
        fail("cannot read representation \(text)")
    }
    representations.append(spec)
}

guard let model = NSManagedObjectModel(contentsOf: modelURL) else {
    fail("cannot load the model at \(modelURL.path)")
}
let container = NSPersistentContainer(name: "Stickers", managedObjectModel: model)
let storeDescription = NSPersistentStoreDescription(url: storeURL)
storeDescription.type = NSSQLiteStoreType
storeDescription.shouldAddStoreAsynchronously = false
// Never change the store's layout: if Apple's model does not match it, stop.
storeDescription.shouldMigrateStoreAutomatically = false
storeDescription.shouldInferMappingModelAutomatically = false
// The store already records history; opening it without this makes it read-only.
storeDescription.setOption(NSNumber(value: true), forKey: NSPersistentHistoryTrackingKey)
storeDescription.setOption(NSNumber(value: true), forKey: NSPersistentStoreRemoteChangeNotificationPostOptionKey)
container.persistentStoreDescriptions = [storeDescription]

var loadError: Error? = nil
container.loadPersistentStores { _, error in
    loadError = error
}
if let loadError = loadError {
    fail("the store did not open: \(loadError)")
}
print("coredata: opened \(storeURL.path)")

let context = container.newBackgroundContext()
context.transactionAuthor = "imsticker"
var outcome: Int32 = 0
context.performAndWait {
    do {
        let request = NSFetchRequest<NSManagedObject>(entityName: "ManagedSticker")
        request.predicate = NSPredicate(format: "type == 1")
        request.sortDescriptors = [NSSortDescriptor(key: "libraryIndex", ascending: false)]
        request.fetchLimit = 1
        let existing = try context.fetch(request)
        var topIndex = 0.0
        if let first = existing.first, let value = first.value(forKey: "libraryIndex") as? Double {
            topIndex = value
        }

        let identifier = UUID()
        let now = Date().timeIntervalSinceReferenceDate
        var totalBytes = 0
        for spec in representations {
            totalBytes += spec.data.count
        }

        let sticker = NSEntityDescription.insertNewObject(forEntityName: "ManagedSticker", into: context)
        sticker.setValue(identifier, forKey: "identifier")
        sticker.setValue(NSNumber(value: Int32(1)), forKey: "type")
        sticker.setValue(NSNumber(value: Int32(0)), forKey: "effect")
        sticker.setValue(NSNumber(value: Int32(1)), forKey: "version")
        sticker.setValue(NSNumber(value: Int32(totalBytes)), forKey: "byteCount")
        sticker.setValue(NSNumber(value: now), forKey: "creationDate")
        sticker.setValue(NSNumber(value: now), forKey: "lastUsedDate")
        sticker.setValue(NSNumber(value: topIndex + 1024), forKey: "libraryIndex")
        sticker.setValue("sticker:///user/identifier/\(identifier.uuidString)", forKey: "externalURI")
        sticker.setValue("", forKey: "name")
        sticker.setValue(searchText, forKey: "searchText")

        for spec in representations {
            let item = NSEntityDescription.insertNewObject(forEntityName: "ManagedRepresentation", into: context)
            item.setValue(UUID(), forKey: "identifier")
            item.setValue(spec.data, forKey: "data")
            item.setValue(spec.uti, forKey: "uti")
            item.setValue(spec.role, forKey: "role")
            item.setValue(NSNumber(value: spec.width), forKey: "size_w")
            item.setValue(NSNumber(value: spec.height), forKey: "size_h")
            item.setValue(NSNumber(value: spec.preferred), forKey: "isPreferred")
            item.setValue(NSNumber(value: spec.index), forKey: "index")
            item.setValue(NSNumber(value: Int32(spec.data.count)), forKey: "byteCount")
            item.setValue(NSNumber(value: Int32(1)), forKey: "version")
            item.setValue(sticker, forKey: "sticker")
        }

        try context.save()
        print("coredata: saved sticker \(identifier.uuidString) with \(representations.count) representations, libraryIndex \(topIndex + 1024)")
    } catch {
        print("coredata: failed: \(error)")
        outcome = 1
    }
}
exit(outcome)
