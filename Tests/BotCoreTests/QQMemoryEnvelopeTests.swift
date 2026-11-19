import Foundation
import Testing
@testable import BotCore

struct QQMemoryEnvelopeTests {
    private let now = Date(timeIntervalSince1970: 1_790_000_000)

    private func tempDir() throws -> URL {
        let url = FileManager.default.temporaryDirectory.appendingPathComponent("qqmem-envelope-\(UUID().uuidString)", isDirectory: true)
        try FileManager.default.createDirectory(at: url, withIntermediateDirectories: true)
        return url
    }

    private func event(_ id: String, _ text: String, subject: String) -> QQMemoryEvent {
        QQMemoryEvent(id: id, subject: subject, text: text, at: now)
    }

    private func delta(source: QQMemoryEvent) throws -> QQMemoryDelta {
        let change: [String: Any] = [
            "operation": "upsert", "replaces": [], "subject": source.subject,
            "kind": "preference", "text": source.text,
            "keywords": ["合成"], "importance": 3, "sourceID": source.id,
            "evidence": source.text, "days": 365,
        ]
        let data = try JSONSerialization.data(withJSONObject: ["summary": "", "changes": [change]])
        return try QQMemoryDelta.decode(String(decoding: data, as: UTF8.self))
    }

    @Test func envelopeKeepsSubjectAndScopeIsolation() throws {
        let dir = try tempDir()
        defer { try? FileManager.default.removeItem(at: dir) }
        let store = try QQMemoryReliableStore(directory: dir, scope: "acct:group:g1")
        let a = event("a", "我喜欢星空摄影", subject: "MEMBER_A")
        let b = event("b", "另一个人的秘密收藏模型", subject: "MEMBER_B")
        _ = try store.ingest(a)
        _ = try store.apply(try delta(source: a), batch: store.batch(), candidateIDs: [], now: now)
        _ = try store.ingest(b)
        _ = try store.apply(try delta(source: b), batch: store.batch(), candidateIDs: [], now: now)

        let envelope = store.contextEnvelope("摄影", subject: "MEMBER_A", characters: 600, now: now)
        #expect(envelope.scope == "acct:group:g1")
        #expect(envelope.subject == "MEMBER_A")
        #expect(envelope.text.contains("星空摄影"))
        #expect(!envelope.text.contains("秘密收藏模型"))
        #expect(envelope.facts.allSatisfy { $0.subject == "MEMBER_A" })
        #expect(!envelope.facts.contains { $0.text.contains("秘密收藏模型") })
        store.close()
    }

    @Test func envelopeRoundTripsAsVersionedJSON() throws {
        let dir = try tempDir()
        defer { try? FileManager.default.removeItem(at: dir) }
        let store = try QQMemoryReliableStore(directory: dir, scope: "acct:private:42")
        let source = event("p", "用户喜欢清淡的晚饭", subject: "PEER")
        _ = try store.ingest(source)
        _ = try store.apply(try delta(source: source), batch: store.batch(), candidateIDs: [], now: now)

        let envelope = store.contextEnvelope("晚饭", subject: "PEER", characters: 400, now: now)
        let data = try JSONEncoder().encode(envelope)
        let decoded = try JSONDecoder().decode(QQMemoryContextEnvelope.self, from: data)
        #expect(decoded == envelope)
        #expect(decoded.source == "reliable")
        #expect(decoded.facts.first?.current == true)
        store.close()
    }
}
