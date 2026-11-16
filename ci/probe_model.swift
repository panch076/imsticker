// Print a compiled Core Data model (.momd or .mom) in readable form: every
// entity, attribute and relationship, with the flags that decide how the
// SQLite store is laid out.
//
//     swift ci/probe_model.swift /path/to/Model.momd

import Foundation
import CoreData

func hex(_ data: Data) -> String {
    return data.map { String(format: "%02x", $0) }.joined()
}

func describe(_ url: URL) {
    guard let model = NSManagedObjectModel(contentsOf: url) else {
        print("MODEL could not be loaded: \(url.path)")
        return
    }
    print("MODEL \(url.path)")
    print("  versionIdentifiers: \(model.versionIdentifiers)")
    print("  configurations: \(model.configurations)")
    let entities = model.entities.sorted { ($0.name ?? "") < ($1.name ?? "") }
    for entity in entities {
        let className: String = entity.managedObjectClassName ?? "?"
        let parent: String = entity.superentity?.name ?? "-"
        print("ENTITY \(entity.name ?? "?") class=\(className) abstract=\(entity.isAbstract) super=\(parent) versionHash=\(hex(entity.versionHash))")
        if let info = entity.userInfo, !info.isEmpty {
            print("  userInfo: \(info)")
        }
        let attributes = entity.attributesByName.sorted { $0.key < $1.key }
        for (name, attribute) in attributes {
            let defaultValue = String(describing: attribute.defaultValue)
            let valueClass: String = attribute.attributeValueClassName ?? "-"
            print("  ATTR \(name) type=\(attribute.attributeType.rawValue) optional=\(attribute.isOptional) transient=\(attribute.isTransient) external=\(attribute.allowsExternalBinaryDataStorage) default=\(defaultValue) valueClass=\(valueClass)")
            if let info = attribute.userInfo, !info.isEmpty {
                print("    userInfo: \(info)")
            }
        }
        let relationships = entity.relationshipsByName.sorted { $0.key < $1.key }
        for (name, relationship) in relationships {
            let destination: String = relationship.destinationEntity?.name ?? "?"
            let inverse: String = relationship.inverseRelationship?.name ?? "-"
            print("  REL \(name) -> \(destination) toMany=\(relationship.isToMany) optional=\(relationship.isOptional) deleteRule=\(relationship.deleteRule.rawValue) inverse=\(inverse)")
        }
        for index in entity.indexes {
            let columns = index.elements.map { $0.propertyName ?? "?" }
            print("  INDEX \(index.name) \(columns)")
        }
        if !entity.uniquenessConstraints.isEmpty {
            print("  UNIQUE \(entity.uniquenessConstraints)")
        }
    }
}

for argument in CommandLine.arguments.dropFirst() {
    describe(URL(fileURLWithPath: argument))
}
