import Foundation
import Testing
@testable import WeChatAIBot

@Suite(.serialized) @MainActor
struct QQMemoryUsageTests {
    @Test func memoryTokensDoNotTouchChatLedger() {
        let engine = QQEngine(preview: true)
        engine.creditMemoryTokens(321)
        #expect(engine.usage.tokens == 0)
        #expect(engine.usage.calls == 0)
        #expect(engine.memoryUsageSnapshot.tokens == 321)
    }

    @Test func memoryTokensAccumulateAcrossCalls() {
        let engine = QQEngine(preview: true)
        engine.creditMemoryTokens(120)
        engine.creditMemoryTokens(80)
        #expect(engine.memoryUsageSnapshot.tokens == 200)
        #expect(engine.usage.tokens == 0)
    }

    @Test func negativeOrZeroMemoryTokensAreIgnored() {
        let engine = QQEngine(preview: true)
        engine.creditMemoryTokens(0)
        engine.creditMemoryTokens(-9)
        #expect(engine.memoryUsageSnapshot.tokens == 0)
        #expect(engine.usage.tokens == 0)
    }

    @Test func defaultOffDoesNotCreateReliableStoreDirectory() throws {
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: directory) }
        let engine = QQEngine(allowAuthenticationUI: false, storageDirectory: directory)
        #expect(engine.memoryReliableModeSnapshot == .off)
        #expect(!FileManager.default.fileExists(atPath: directory.appendingPathComponent("QQMemoryReliable").path))
    }
}
