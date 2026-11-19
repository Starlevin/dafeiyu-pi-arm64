import Foundation
import Testing
@testable import BotCore

struct QQMemoryShadowPolicyTests {
    private let now = Date(timeIntervalSince1970: 1_790_000_000)

    private func tempDir() throws -> URL {
        let url = FileManager.default.temporaryDirectory.appendingPathComponent("qqmem-shadow-\(UUID().uuidString)", isDirectory: true)
        try FileManager.default.createDirectory(at: url, withIntermediateDirectories: true)
        return url
    }

    @Test func defaultScopesFailClosed() {
        let policy = QQMemoryShadowPolicy.resolve(environment: [:])
        #expect(policy.scopes?.isEmpty == true)
        #expect(!policy.isUsable)
        #expect(!policy.allows(scope: "acct:group-a"))
        #expect(policy.ttl == QQMemoryShadowPolicy.defaultTTL)
    }

    @Test func wildcardAllowsAllScopes() {
        let policy = QQMemoryShadowPolicy.resolve(environment: ["DAFEIYU_MEMORY_SHADOW_SCOPES": "*"])
        #expect(policy.scopes == nil)
        #expect(policy.isUsable)
        #expect(policy.allows(scope: "acct:group-a"))
        #expect(policy.allows(scope: "acct:private:42"))
    }

    @Test func explicitScopesMatchGroupAndPrivateSuffixesOnly() {
        let policy = QQMemoryShadowPolicy.resolve(environment: [
            "DAFEIYU_MEMORY_SHADOW_SCOPES": "group:g1, private:p1",
        ])
        #expect(policy.allows(scope: "acct:group:g1"))
        #expect(policy.allows(scope: "acct:private:p1"))
        #expect(!policy.allows(scope: "acct:group:g2"))
        #expect(!policy.allows(scope: "acct:private:p2"))
        #expect(!policy.allows(scope: "acct:group:g1:extra"))
    }

    @Test func noneAndInvalidTTLFailClosed() {
        let none = QQMemoryShadowPolicy.resolve(environment: ["DAFEIYU_MEMORY_SHADOW_SCOPES": "none"])
        #expect(!none.isUsable)
        #expect(!none.allows(scope: "acct:group-a"))

        let badTTL = QQMemoryShadowPolicy.resolve(environment: [
            "DAFEIYU_MEMORY_SHADOW_SCOPES": "*",
            "DAFEIYU_MEMORY_SHADOW_TTL_SECONDS": "30",
        ])
        #expect(badTTL.ttl == 0)
        #expect(!badTTL.isUsable)
    }

    @Test func sessionExpiryPersistsAcrossRestart() throws {
        let root = try tempDir()
        defer { try? FileManager.default.removeItem(at: root) }
        let policy = QQMemoryShadowPolicy(ttl: 60, scopes: nil)
        let created = try QQMemoryShadowSession.loadOrCreate(root: root, policy: policy, now: now)
        #expect(created.isActive(now: now.addingTimeInterval(30)))
        var stopped = created
        stopped.stop(.highWatermark, now: now.addingTimeInterval(30))
        try stopped.persist(to: root)

        let reopened = try QQMemoryShadowSession.loadOrCreate(root: root, policy: policy, now: now.addingTimeInterval(40))
        #expect(!reopened.isActive(now: now.addingTimeInterval(40)))
        #expect(reopened.stopReason == .highWatermark)
    }

    @Test func expiredSessionNeverRestartsWithoutReset() throws {
        let root = try tempDir()
        defer { try? FileManager.default.removeItem(at: root) }
        let policy = QQMemoryShadowPolicy(ttl: 60, scopes: nil)
        let created = try QQMemoryShadowSession.loadOrCreate(root: root, policy: policy, now: now)
        _ = try? created.persist(to: root)

        let expired = try QQMemoryShadowSession.loadOrCreate(root: root, policy: policy, now: now.addingTimeInterval(61))
        #expect(expired.stopReason == .ttlExpired)
        #expect(!expired.isActive(now: now.addingTimeInterval(61)))

        let afterRestart = try QQMemoryShadowSession.loadOrCreate(root: root, policy: policy, now: now.addingTimeInterval(62))
        #expect(!afterRestart.isActive(now: now.addingTimeInterval(62)))

        let reset = try QQMemoryShadowSession.loadOrCreate(root: root, policy: policy, now: now.addingTimeInterval(62), reset: true)
        #expect(reset.isActive(now: now.addingTimeInterval(62)))
    }

    @Test func corruptSessionFailsClosed() throws {
        let root = try tempDir()
        defer { try? FileManager.default.removeItem(at: root) }
        try Data("not-json".utf8).write(to: root.appendingPathComponent("shadow-session.json"))
        let policy = QQMemoryShadowPolicy(ttl: 60, scopes: nil)
        let session = try QQMemoryShadowSession.loadOrCreate(root: root, policy: policy, now: now)
        #expect(session.stopReason == .corruptSession)
        #expect(!session.isActive(now: now))
    }

    @Test func statusExposesThresholdsWithoutChatText() {
        let policy = QQMemoryShadowPolicy(ttl: 60, scopes: nil)
        let session = QQMemoryShadowSession(startedAt: now, ttl: 60)
        var stats = QQMemoryReliableStats()
        stats.queuedCount = 1024
        stats.queueBytes = 4 * 1024 * 1024
        stats.refusedEvents = 3
        stats.highWatermark = true
        let status = policy.status(scope: "acct:group-a", session: session, stats: stats, mode: .shadow, now: now)
        #expect(status.active)
        #expect(status.queuedCount == 1024)
        #expect(status.highWatermark)
        #expect(status.alerts.contains("shadow-high-watermark"))
        #expect(status.alerts.contains("shadow-refused"))
        #expect(!status.alerts.contains("text"))
    }

    @Test func nonAllowlistedScopeIsInactive() {
        let policy = QQMemoryShadowPolicy(ttl: 60, scopes: ["group:g1"])
        let session = QQMemoryShadowSession(startedAt: now, ttl: 60)
        let status = policy.status(scope: "acct:group:g2", session: session, stats: nil, mode: .shadow, now: now)
        #expect(!status.active)
        #expect(status.stopReason == .notAllowlisted)
        #expect(status.alerts.contains("shadow-not-allowlisted"))
    }
}
