import Foundation
import Testing
@testable import ConnectionStore

@Suite("Connection persistence")
struct ConnectionStoreTests {
    @Test func roundTripAndAtomicWrite() async throws {
        let temporary = try TemporaryDirectory()
        let source = try syntheticJump()
        try temporary.writeJump(source)
        let imported = try #require(JumpImporter(directory: temporary.url).plan(existing: []).entries.first).connection
        let store = ConnectionStore(fileURL: temporary.storeURL)
        #expect(try await store.load().isEmpty)
        try await store.insert(imported)
        let firstData = try Data(contentsOf: temporary.storeURL)
        var changed = imported
        changed.notes = "Synthetic notes"
        try await store.update(changed)
        #expect(firstData != (try Data(contentsOf: temporary.storeURL)))
        let reloaded = ConnectionStore(fileURL: temporary.storeURL)
        #expect(try await reloaded.load() == [changed])
        let files = try FileManager.default.contentsOfDirectory(atPath: temporary.url.path)
        #expect(Set(files) == ["test.jump", "connections.json"])
        try await store.save()
        #expect(try await reloaded.load() == [changed])
    }

    @Test func unopenedStoreMutationsPreserveExistingRecords() async throws {
        let temporary = try TemporaryDirectory()
        let original = Connection(name: "Existing", host: "PC0001.example")
        try await ConnectionStore(fileURL: temporary.storeURL).insert(original)
        let newStore = ConnectionStore(fileURL: temporary.storeURL)
        try await newStore.save()
        #expect(await newStore.connections == [original])
        let added = Connection(name: "New", host: "PC0002.example")
        let another = ConnectionStore(fileURL: temporary.storeURL)
        try await another.insert(added)
        #expect(await another.connections == [original, added])
        var change = original
        change.name = "Changed"
        let updating = ConnectionStore(fileURL: temporary.storeURL)
        try await updating.update(change)
        #expect(await updating.connections == [change, added])
    }

    @Test func failedPersistenceDoesNotChangeSnapshot() async throws {
        let temporary = try TemporaryDirectory()
        let store = ConnectionStore(fileURL: temporary.storeURL)
        let connection = Connection(name: "Test", host: "PC0001.example")
        try await store.insert(connection)
        try FileManager.default.removeItem(at: temporary.storeURL)
        try FileManager.default.createDirectory(at: temporary.storeURL, withIntermediateDirectories: false)
        var change = connection
        change.name = "Must not persist"
        await #expect(throws: (any Error).self) { try await store.update(change) }
        #expect(await store.connections == [connection])
    }

    @Test func crudDuplicateSearchAndSort() async throws {
        let temporary = try TemporaryDirectory()
        let store = ConnectionStore(fileURL: temporary.storeURL)
        var alpha = Connection(name: "Alpha", host: "PC0001.example", username: "testuser")
        alpha.tags = ["Testing"]
        alpha.lastConnected = Date(timeIntervalSince1970: 100)
        var beta = Connection(name: "Beta", host: "PC0002.example")
        beta.lastConnected = Date(timeIntervalSince1970: 200)
        beta.importSource = ImportSource(uniqueID: "synthetic-2", fileName: "synthetic.jump")
        try await store.insert(beta)
        try await store.insert(alpha)
        #expect(await store.search().map(\.name) == ["Alpha", "Beta"])
        #expect(await store.search(sortedBy: .recent).map(\.name) == ["Beta", "Alpha"])
        #expect(await store.search("TESTING").map(\.id) == [alpha.id])
        #expect(await store.search("pc0002").map(\.id) == [beta.id])
        #expect(ConnectionStore.filter(await store.connections, matching: "alpha", sortedBy: .name).map(\.id) == [alpha.id])
        let duplicate = try await store.duplicate(id: beta.id)
        #expect(duplicate.name == "Beta copy")
        #expect(duplicate.id != beta.id)
        #expect(duplicate.importSource == nil)
        #expect(duplicate.lastConnected == nil)
        #expect(duplicate.host == beta.host)
        try await store.delete(id: duplicate.id)
        #expect(await store.connections.count == 2)
        await #expect(throws: ConnectionStoreError.duplicateID(alpha.id)) { try await store.insert(alpha) }
        await #expect(throws: ConnectionStoreError.missingConnection(duplicate.id)) { try await store.delete(id: duplicate.id) }
    }

    @Test func modifyChangesTheStoredCopyNotAStaleOne() async throws {
        let temporary = try TemporaryDirectory()
        let store = ConnectionStore(fileURL: temporary.storeURL)
        let original = Connection(name: "Alpha", host: "PC0001.example")
        try await store.insert(original)
        var edited = original
        edited.notes = "Edited meanwhile"
        try await store.update(edited)
        let when = Date(timeIntervalSinceReferenceDate: 1000)
        let result = try await store.modify(id: original.id) { $0.lastConnected = when }
        #expect(result.notes == "Edited meanwhile" && result.lastConnected == when)
        #expect(try await ConnectionStore(fileURL: temporary.storeURL).load() == [result])
        await #expect(throws: ConnectionStoreError.missingConnection(edited.id)) {
            try await ConnectionStore(fileURL: temporary.url.appendingPathComponent("other.json")).modify(id: edited.id) { _ in }
        }
    }

    @Test func builtInLegacyMigrationBacksUpOriginal() async throws {
        let temporary = try TemporaryDirectory()
        let id = UUID()
        let original = try JSONSerialization.data(withJSONObject: [["id": id.uuidString, "name": "Old", "host": "PC0001.example"]])
        try original.write(to: temporary.storeURL)
        let store = ConnectionStore(fileURL: temporary.storeURL)
        let loaded = try await store.load()
        #expect(loaded.count == 1)
        #expect(loaded[0].id == id)
        #expect(loaded[0].schemaVersion == 1)
        #expect(loaded[0].port == 3389)
        let backup = try #require(await store.lastMigrationBackupURL)
        #expect(try Data(contentsOf: backup) == original)
        let next = ConnectionStore(fileURL: temporary.storeURL)
        #expect(try await next.load() == loaded)
        #expect(await next.lastMigrationBackupURL == nil)
    }

    @Test func customMigrationHookAndFailureBackup() async throws {
        let temporary = try TemporaryDirectory()
        let original = Data("{\"schemaVersion\":0,\"oldRecords\":[]}".utf8)
        try original.write(to: temporary.storeURL)
        let migrated = Connection(name: "Migrated", host: "PC0001.example")
        let store = ConnectionStore(fileURL: temporary.storeURL) { bytes, version in
            #expect(bytes == original)
            #expect(version == 0)
            return try ConnectionStore.encoder().encode(ConnectionStoreDocument(connections: [migrated]))
        }
        #expect(try await store.load() == [migrated])
        #expect(try Data(contentsOf: #require(await store.lastMigrationBackupURL)) == original)
        try original.write(to: temporary.storeURL)
        let failing = ConnectionStore(fileURL: temporary.storeURL) { _, _ in throw ConnectionStoreError.invalidStore }
        await #expect(throws: ConnectionStoreError.invalidStore) { try await failing.load() }
        #expect(try Data(contentsOf: temporary.storeURL) == original)
        #expect(try Data(contentsOf: #require(await failing.lastMigrationBackupURL)) == original)
    }

    @Test func rejectsFutureSchemaAndInvalidConnections() async throws {
        let temporary = try TemporaryDirectory()
        let data = Data("{\"schemaVersion\":99,\"connections\":[]}".utf8)
        try data.write(to: temporary.storeURL)
        let store = ConnectionStore(fileURL: temporary.storeURL)
        await #expect(throws: ConnectionStoreError.unsupportedSchema(99)) { try await store.load() }
        #expect(try Data(contentsOf: temporary.storeURL) == data)
        #expect(await store.lastMigrationBackupURL == nil)
        await #expect(throws: ConnectionStoreError.unsupportedSchema(99)) {
            try await store.insert(Connection(name: "Must not overwrite", host: "PC0002.example"))
        }
        #expect(try Data(contentsOf: temporary.storeURL) == data)
        try FileManager.default.removeItem(at: temporary.storeURL)
        var invalid = Connection(name: "Invalid", host: "PC0001.example")
        invalid.port = 0
        await #expect(throws: ConnectionStoreError.invalidConnection(invalid.id)) { try await store.insert(invalid) }
        #expect(await store.connections.isEmpty)
    }

    @Test func agentAccessDefaultsToAskAndRoundTrips() async throws {
        // Security settings written before agent access existed.
        let old = Data(#"{"ignoreCertificateErrors":false,"trustedCertificateFingerprints":["AA:BB"],"disableNLA":true,"consoleSession":false,"authenticationLevel":0,"promptForCredentials":false}"#.utf8)
        let decoded = try JSONDecoder().decode(SecuritySettings.self, from: old)
        #expect(decoded.agentAccess == .ask)
        #expect(decoded.disableNLA && decoded.authenticationLevel == 0 && !decoded.promptForCredentials)
        #expect(decoded.trusts(fingerprint: "aabb"))
        let unknown = Data(#"{"agentAccess":"sometimes"}"#.utf8)
        #expect(try JSONDecoder().decode(SecuritySettings.self, from: unknown).agentAccess == .ask)

        let temporary = try TemporaryDirectory()
        var connection = Connection(name: "Lab", host: "PC0001.example")
        connection.security.agentAccess = .never
        try await ConnectionStore(fileURL: temporary.storeURL).insert(connection)
        #expect(try String(contentsOf: temporary.storeURL, encoding: .utf8).contains(#""agentAccess" : "never""#))
        #expect(try await ConnectionStore(fileURL: temporary.storeURL).load().first?.security.agentAccess == .never)
    }
}
