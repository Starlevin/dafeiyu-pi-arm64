import Foundation
import Testing
import BotCore
@testable import WeChatAIBot
#if canImport(Glibc)
import Glibc
#endif

@Suite(.serialized) @MainActor
struct QQMemoryShadowEngineTests {
    private let modeKey = "DAFEIYU_MEMORY_RELIABLE"
    private let scopesKey = "DAFEIYU_MEMORY_SHADOW_SCOPES"
    private let ttlKey = "DAFEIYU_MEMORY_SHADOW_TTL_SECONDS"

    private func withEnvironment(_ values: [String: String?], _ body: () throws -> Void) rethrows {
        var previous: [String: String?] = [:]
        for (key, value) in values {
            previous[key] = ProcessInfo.processInfo.environment[key]
            if let value { setenv(key, value, 1) } else { unsetenv(key) }
        }
        defer {
            for (key, value) in previous {
                if let value { setenv(key, value, 1) } else { unsetenv(key) }
            }
        }
        try body()
    }

    @Test func shadowWithoutAllowlistCreatesNoDirectoryAndIsInactive() throws {
        try withEnvironment([modeKey: "shadow", scopesKey: nil, ttlKey: nil]) {
            let directory = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
            try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
            defer { try? FileManager.default.removeItem(at: directory) }

            let engine = QQEngine(allowAuthenticationUI: false, storageDirectory: directory)
            #expect(engine.memoryReliableModeSnapshot == .shadow)
            #expect(!FileManager.default.fileExists(atPath: directory.appendingPathComponent("QQMemoryReliable").path))
            let status = engine.memoryShadowSnapshot(for: "acct:group-a")
            #expect(!status.active)
            #expect(status.stopReason == .allowlistEmpty)
        }
    }

    @Test func shadowDoesNotAutoImportLegacyBook() throws {
        try withEnvironment([modeKey: "shadow", scopesKey: "*", ttlKey: nil]) {
            let directory = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
            try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
            defer { try? FileManager.default.removeItem(at: directory) }

            var book = QQMemoryBook()
            book.importLegacy(text: "旧核心里的合成事实", subject: "PEER", at: Date())
            let engine = QQEngine(allowAuthenticationUI: false, storageDirectory: directory)
            let store = try #require(engine.memoryReliableStoreForTesting("acct:group-a", book: book))
            #expect(store.items.isEmpty)
            #expect(!store.hasImportedLegacy)
            let status = engine.memoryShadowSnapshot(for: "acct:group-a")
            #expect(status.active)
            #expect(status.queuedCount == 0)
            #expect(status.expiresAt != nil)
            store.close()
        }
    }

    @Test func authoritativeStillImportsLegacyOnce() throws {
        try withEnvironment([modeKey: "authoritative", scopesKey: nil, ttlKey: nil]) {
            let directory = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
            try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
            defer { try? FileManager.default.removeItem(at: directory) }

            var book = QQMemoryBook()
            book.importLegacy(text: "旧核心里的合成事实", subject: "PEER", at: Date())
            let engine = QQEngine(allowAuthenticationUI: false, storageDirectory: directory)
            let store = try #require(engine.memoryReliableStoreForTesting("acct:group-a", book: book))
            #expect(store.items.count == 1)
            #expect(store.hasImportedLegacy)
            store.close()
        }
    }
}
