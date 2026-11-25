import Foundation
import Testing
@testable import BotCore

struct QQMemoryReliableStoreTests {
    private let now = Date(timeIntervalSince1970: 1_790_000_000)

    private func tempDir() throws -> URL {
        let url = FileManager.default.temporaryDirectory.appendingPathComponent("qqmem-store-\(UUID().uuidString)", isDirectory: true)
        try FileManager.default.createDirectory(at: url, withIntermediateDirectories: true)
        return url
    }

    private func event(_ id: String, _ text: String = "合成消息", subject: String = "PEER", at offset: TimeInterval = 0) -> QQMemoryEvent {
        QQMemoryEvent(id: id, subject: subject, text: text, at: now.addingTimeInterval(offset))
    }

    private func delta(source: QQMemoryEvent, text: String? = nil, replaces: [String] = [], operation: String = "upsert",
                       subject: String? = nil, evidence: String? = nil, kind: String = "preference", days: Int = 365,
                       summary: String = "") throws -> QQMemoryDelta {
        let change: [String: Any] = [
            "operation": operation, "replaces": replaces, "subject": subject ?? source.subject,
            "kind": kind, "text": text ?? source.text, "keywords": ["合成", "测试"],
            "importance": 3, "sourceID": source.id, "evidence": evidence ?? source.text, "days": days,
        ]
        let data = try JSONSerialization.data(withJSONObject: ["summary": summary, "changes": [change]])
        return try QQMemoryDelta.decode(String(decoding: data, as: UTF8.self))
    }

    private func accepted(_ result: QQMemoryJournalAppend) -> Bool {
        if case .accepted = result { return true }
        return false
    }

    @Test func ingestPersistsAcrossReopen() throws {
        let dir = try tempDir()
        let store = try QQMemoryReliableStore(directory: dir, scope: "group-a")
        #expect(accepted(try store.ingest(event("1"))))
        store.close()
        let reopened = try QQMemoryReliableStore(directory: dir, scope: "group-a")
        #expect(reopened.batch().map(\.event.id) == ["1"])
        reopened.close()
    }

    @Test func recentWindowCapsAtTwentyFour() throws {
        let dir = try tempDir()
        let store = try QQMemoryReliableStore(directory: dir, scope: "group-a")
        for i in 0..<30 { _ = try store.ingest(event("r\(i)", at: TimeInterval(i))) }
        #expect(store.recent.count == 24)
        #expect(store.batch().count == 30)
        store.close()
    }

    @Test func botAndSelfUnknownStayOutOfTheQueue() throws {
        let dir = try tempDir()
        let store = try QQMemoryReliableStore(directory: dir, scope: "group-a")
        let bot = try store.ingest(event("bot", "BOT 猜测", subject: "BOT"))
        let unknown = try store.ingest(event("unknown", "未知来源", subject: "SELF_UNKNOWN"))
        #expect(bot == .storedOnly)
        #expect(unknown == .storedOnly)
        #expect(store.batch().isEmpty)
        #expect(store.recent.count == 2)
        store.close()
    }

    @Test func dueHonorsMessageThresholdAndInterval() throws {
        let dir = try tempDir()
        var options = QQMemoryReliableOptions()
        options.thresholds.messageThreshold = 4
        options.thresholds.intervalMinutes = 10
        let store = try QQMemoryReliableStore(directory: dir, scope: "group-a", options: options)
        for i in 0..<3 { _ = try store.ingest(event("d\(i)", at: TimeInterval(i))) }
        #expect(!store.due(now: now))
        _ = try store.ingest(event("d3", at: 3))
        #expect(store.due(now: now))
        let singleDir = try tempDir()
        let single = try QQMemoryReliableStore(directory: singleDir, scope: "group-b", options: options)
        _ = try single.ingest(event("one"))
        #expect(!single.due(now: now))
        #expect(single.due(now: now.addingTimeInterval(11 * 60)))
        single.close()
        store.close()
    }

    @Test func dueHonorsCharacterThreshold() throws {
        let dir = try tempDir()
        var options = QQMemoryReliableOptions()
        options.thresholds.characterThreshold = 1000
        let store = try QQMemoryReliableStore(directory: dir, scope: "group-a", options: options)
        _ = try store.ingest(event("long", String(repeating: "长", count: 1200)))
        #expect(store.due(now: now))
        store.close()
    }

    @Test func applyCreatesFactAndConsumesBatch() throws {
        let dir = try tempDir()
        let store = try QQMemoryReliableStore(directory: dir, scope: "group-a")
        let source = event("fact-1", "我喜欢清淡的晚饭")
        #expect(accepted(try store.ingest(source)))
        let batch = store.batch()
        let report = try store.apply(try delta(source: source), batch: batch, candidateIDs: [], now: now)
        #expect(report.changes == 1)
        #expect(store.items.count == 1)
        #expect(store.items.first?.text.contains("清淡") == true)
        #expect(store.batch().isEmpty)
        store.close()
    }

    @Test func applyRejectsWrongSpeakerAtomically() throws {
        let dir = try tempDir()
        let store = try QQMemoryReliableStore(directory: dir, scope: "group-a")
        let source = event("wrong-1", "我喜欢清淡的晚饭")
        _ = try store.ingest(source)
        let batch = store.batch()
        do {
            _ = try store.apply(try delta(source: source, subject: "OWNER"), batch: batch, candidateIDs: [], now: now)
            Issue.record("expected wrong speaker rejection")
        } catch {
            #expect(store.items.isEmpty)
            #expect(store.batch().count == 1)
        }
        store.close()
    }

    @Test func applyRejectsFabricatedEvidenceAtomically() throws {
        let dir = try tempDir()
        let store = try QQMemoryReliableStore(directory: dir, scope: "group-a")
        let source = event("fake-1", "我喜欢清淡的晚饭")
        _ = try store.ingest(source)
        let batch = store.batch()
        do {
            _ = try store.apply(try delta(source: source, evidence: "没有说过的内容"), batch: batch, candidateIDs: [], now: now)
            Issue.record("expected fabricated evidence rejection")
        } catch {
            #expect(store.items.isEmpty)
            #expect(store.batch().count == 1)
        }
        store.close()
    }

    @Test func applyRejectsReplacementOutsideCandidates() throws {
        let dir = try tempDir()
        let store = try QQMemoryReliableStore(directory: dir, scope: "group-a")
        let first = event("a", "我喜欢清淡的晚饭")
        _ = try store.ingest(first)
        let firstBatch = store.batch()
        _ = try store.apply(try delta(source: first), batch: firstBatch, candidateIDs: [], now: now)
        _ = try #require(store.items.first)
        let second = event("b", "我现在喜欢吃辣", at: 10)
        _ = try store.ingest(second)
        let batch = store.batch()
        do {
            _ = try store.apply(try delta(source: second, replaces: ["not-a-candidate"]), batch: batch, candidateIDs: [], now: now)
            Issue.record("expected candidate validation")
        } catch {
            #expect(store.items.first?.invalidatedAt == nil)
        }
        store.close()
    }

    @Test func correctionKeepsHistoryAndPrefersCurrentFact() throws {
        let dir = try tempDir()
        let store = try QQMemoryReliableStore(directory: dir, scope: "group-a")
        let first = event("old", "我喜欢清淡的晚饭")
        _ = try store.ingest(first)
        _ = try store.apply(try delta(source: first), batch: store.batch(), candidateIDs: [], now: now)
        let old = try #require(store.items.first)
        let correction = event("new", "我现在晚饭喜欢吃辣", at: 10)
        _ = try store.ingest(correction)
        _ = try store.apply(try delta(source: correction, text: "现在晚饭喜欢吃辣", replaces: [old.id]),
                            batch: store.batch(), candidateIDs: [old.id], now: now.addingTimeInterval(10))
        let current = store.search("晚饭", subject: "PEER", now: now.addingTimeInterval(20))
        #expect(current.contains { $0.text.contains("吃辣") })
        #expect(!current.contains { $0.text.contains("清淡") })
        let history = store.context("以前的晚饭口味", subject: "PEER", characters: 600, now: now.addingTimeInterval(20))
        #expect(history.contains("历史/已失效"))
        #expect(history.contains("清淡"))
        store.close()
    }

    @Test func duplicateFactsNormalizePunctuation() throws {
        let dir = try tempDir()
        let store = try QQMemoryReliableStore(directory: dir, scope: "group-a")
        let first = event("one", "我喜欢清淡的晚饭")
        _ = try store.ingest(first)
        _ = try store.apply(try delta(source: first, text: "喜欢清淡的晚饭"), batch: store.batch(), candidateIDs: [], now: now)
        let second = event("two", "我还是喜欢清淡的晚饭。", at: 10)
        _ = try store.ingest(second)
        _ = try store.apply(try delta(source: second, text: "喜欢 清淡的晚饭！"), batch: store.batch(), candidateIDs: [], now: now.addingTimeInterval(10))
        #expect(store.items.filter { $0.kind == "preference" }.count == 1)
        store.close()
    }

    @Test func forgetDeltaSuppressesHistoryAndSummariesAfterRestoringOlderState() throws {
        let dir = try tempDir()
        defer { try? FileManager.default.removeItem(at: dir) }
        let store = try QQMemoryReliableStore(directory: dir, scope: "group-a")
        let source = event("supper", "我喜欢咸味晚饭")
        _ = try store.ingest(source)
        _ = try store.apply(try delta(source: source, summary: "讨论过咸味晚饭"),
                            batch: store.batch(), candidateIDs: [], now: now)
        let item = try #require(store.items.first)
        let oldState = try Data(contentsOf: store.stateURL)
        let request = event("retract", "忘记刚才的晚饭口味", at: 10)
        _ = try store.ingest(request)
        let when = now.addingTimeInterval(10)
        _ = try store.apply(try delta(source: request, text: "", replaces: [item.id], operation: "forget",
                                      summary: "之前说过咸味晚饭"),
                            batch: store.batch(), candidateIDs: [item.id], now: when)
        #expect(store.search("晚饭口味", subject: "PEER", now: when, history: true).isEmpty)
        #expect(store.episodes.isEmpty)
        #expect(store.forgottenIDs.contains(item.id))
        #expect(!store.recent.contains { $0.id == source.id })
        store.close()

        try oldState.write(to: dir.appendingPathComponent("state.json"), options: .atomic)
        let restored = try QQMemoryReliableStore(directory: dir, scope: "group-a")
        #expect(restored.context("以前的晚饭口味", subject: "PEER", characters: 600, now: when).isEmpty)
        #expect(restored.episodes.isEmpty)
        #expect(restored.forgottenIDs.contains(item.id))
        #expect(!restored.recent.contains { $0.id == source.id })
        #expect(!accepted(try restored.ingest(source, now: when)))
        #expect(!restored.batch().contains { $0.event.id == source.id })
        restored.close()
    }

    @Test func oldForgetArchiveVersionIsAppliedAsASuppressionRecord() throws {
        let dir = try tempDir()
        defer { try? FileManager.default.removeItem(at: dir) }
        let store = try QQMemoryReliableStore(directory: dir, scope: "group-a")
        let source = event("legacy-note", "过去的合成会议口令")
        _ = try store.ingest(source)
        _ = try store.apply(try delta(source: source, summary: "讨论过合成会议口令"),
                            batch: store.batch(), candidateIDs: [], now: now)
        let old = try #require(store.items.first)
        store.close()
        let stamp = ISO8601DateFormatter().string(from: now)
        let entry: [String: Any] = [
            "kind": "itemVersion", "at": stamp,
            "item": ["id": old.id, "subject": old.subject, "kind": old.kind, "text": old.text,
                     "sourceIDs": old.sourceIDs, "validFrom": stamp, "validTo": stamp, "reason": "forget"],
        ]
        var line = try JSONSerialization.data(withJSONObject: entry)
        line.append(0x0a)
        let archive = dir.appendingPathComponent("archive.jsonl")
        let handle = try FileHandle(forWritingTo: archive)
        try handle.seekToEnd()
        try handle.write(contentsOf: line)
        try handle.close()

        let reopened = try QQMemoryReliableStore(directory: dir, scope: "group-a")
        #expect(reopened.search("合成会议口令", subject: "PEER", now: now, history: true).isEmpty)
        #expect(reopened.episodes.isEmpty)
        #expect(reopened.forgottenIDs.contains(old.id))
        reopened.close()
    }

    @Test func kindSpecificExpiryIsRespected() throws {
        let dir = try tempDir()
        let store = try QQMemoryReliableStore(directory: dir, scope: "group-a")
        let preference = event("pref", "我喜欢清淡的晚饭")
        _ = try store.ingest(preference)
        _ = try store.apply(try delta(source: preference, days: 365), batch: store.batch(), candidateIDs: [], now: now)
        let task = event("task", "我明天要交作业", at: 1)
        _ = try store.ingest(task)
        _ = try store.apply(try delta(source: task, text: "明天要交作业", kind: "task", days: 30), batch: store.batch(), candidateIDs: [], now: now.addingTimeInterval(1))
        #expect(store.search("晚饭", subject: "PEER", now: now.addingTimeInterval(200 * 86400)).count == 1)
        #expect(!store.search("作业", subject: "PEER", now: now.addingTimeInterval(31 * 86400)).contains { $0.kind == "task" })
        store.close()
    }

    @Test func forgetRemovesItemAndDoesNotResurrectAfterReload() throws {
        let dir = try tempDir()
        let store = try QQMemoryReliableStore(directory: dir, scope: "group-a")
        let source = event("forget", "我喜欢清淡的晚饭")
        _ = try store.ingest(source)
        _ = try store.apply(try delta(source: source), batch: store.batch(), candidateIDs: [], now: now)
        let item = try #require(store.items.first)
        #expect(try store.forget(subject: "PEER", ids: [item.id], now: now.addingTimeInterval(5)) == 1)
        #expect(store.items.isEmpty)
        store.close()
        let reopened = try QQMemoryReliableStore(directory: dir, scope: "group-a")
        #expect(reopened.items.isEmpty)
        #expect(reopened.search("晚饭", subject: "PEER", now: now).isEmpty)
        reopened.close()
    }

    @Test func forgottenItemIsNotResurrectedByLegacyImportOrLostState() throws {
        let dir = try tempDir()
        var book = QQMemoryBook()
        let source = event("forget-legacy", "我喜欢老式甜点")
        book.append(source)
        try book.apply(try delta(source: source), batch: [source], candidateIDs: [], now: now)
        let bookItem = try #require(book.items.first)

        let store = try QQMemoryReliableStore(directory: dir, scope: "group-a")
        let imported = try store.importLegacy(book, now: now)
        #expect(imported.forgotten == 0)
        #expect(imported.items == 1)
        #expect(store.hasImportedLegacy)
        #expect(try store.forget(subject: "PEER", ids: [bookItem.id], now: now.addingTimeInterval(5)) == 1)
        #expect(store.items.isEmpty)
        #expect(store.forgottenIDs.contains(bookItem.id))
        store.close()

        // State loss must not resurrect a forgotten item: the tombstone lives in
        // archive.jsonl and is replayed on open.
        try? FileManager.default.removeItem(at: dir.appendingPathComponent("state.json"))
        try? FileManager.default.removeItem(at: dir.appendingPathComponent("state.json.bak"))
        let reopened = try QQMemoryReliableStore(directory: dir, scope: "group-a")
        #expect(reopened.items.isEmpty)
        #expect(reopened.forgottenIDs.contains(bookItem.id))
        let second = try reopened.importLegacy(book, now: now.addingTimeInterval(10))
        #expect(second.forgotten == 1)
        #expect(reopened.items.isEmpty)
        reopened.close()
    }

    @Test func legacyImportIsMarkedOnceAndPersists() throws {
        let dir = try tempDir()
        var book = QQMemoryBook()
        book.importLegacy(text: "喜欢旧式甜点", subject: "PEER", at: now)
        let store = try QQMemoryReliableStore(directory: dir, scope: "group-a")
        #expect(!store.hasImportedLegacy)
        _ = try store.importLegacy(book, now: now)
        #expect(store.hasImportedLegacy)
        store.close()
        let reopened = try QQMemoryReliableStore(directory: dir, scope: "group-a")
        #expect(reopened.hasImportedLegacy)
        reopened.close()
    }

    @Test func forgettingConversationRemovesItsEpisodeAndArchiveText() throws {
        let dir = try tempDir()
        let store = try QQMemoryReliableStore(directory: dir, scope: "group-a")
        let source = event("ep", "今天讨论了晚饭")
        _ = try store.ingest(source)
        _ = try store.apply(try delta(source: source, text: "讨论了晚饭", summary: "大家在聊晚饭"), batch: store.batch(), candidateIDs: [], now: now)
        let episode = try #require(store.episodes.first)
        #expect(try store.forget(subject: "CONVERSATION", ids: [episode.id], now: now.addingTimeInterval(5)) == 1)
        #expect(store.episodes.allSatisfy { !$0.text.contains("大家在聊晚饭") })
        store.close()
    }

    @Test func perSubjectRetrievalNeverLeaksOtherMembers() throws {
        let dir = try tempDir()
        let store = try QQMemoryReliableStore(directory: dir, scope: "group-a")
        let a = event("a", "我喜欢旅行和星空摄影", subject: "MEMBER_A")
        let b = event("b", "B组才知道的秘密收藏模型", subject: "MEMBER_B")
        _ = try store.ingest(a)
        _ = try store.apply(try delta(source: a), batch: store.batch(), candidateIDs: [], now: now)
        _ = try store.ingest(b)
        _ = try store.apply(try delta(source: b), batch: store.batch(), candidateIDs: [], now: now)
        let result = store.context("旅行", subject: "MEMBER_A", characters: 1200, now: now)
        #expect(result.contains("星空摄影"))
        #expect(!result.contains("秘密收藏模型"))
        store.close()
    }

    @Test func separateScopesArePhysicallyIsolated() throws {
        let dirA = try tempDir(), dirB = try tempDir()
        let a = try QQMemoryReliableStore(directory: dirA, scope: "group-a")
        let b = try QQMemoryReliableStore(directory: dirB, scope: "group-b")
        let sourceA = event("shared-a", "A组才知道的星空摄影", subject: "MEMBER_X")
        let sourceB = event("shared-b", "B组才知道的秘密收藏模型", subject: "MEMBER_X")
        _ = try a.ingest(sourceA)
        _ = try a.apply(try delta(source: sourceA), batch: a.batch(), candidateIDs: [], now: now)
        _ = try b.ingest(sourceB)
        _ = try b.apply(try delta(source: sourceB), batch: b.batch(), candidateIDs: [], now: now)
        let aContext = a.context("旅行 秘密", subject: "MEMBER_X", characters: 800, now: now)
        let bContext = b.context("旅行 秘密", subject: "MEMBER_X", characters: 800, now: now)
        #expect(aContext.contains("星空摄影"))
        #expect(!aContext.contains("秘密收藏模型"))
        #expect(bContext.contains("秘密收藏模型"))
        #expect(!bContext.contains("星空摄影"))
        a.close(); b.close()
    }

    @Test func redactionHidesSecretsPhoneIDAndEmailOnDisk() throws {
        let dir = try tempDir()
        let store = try QQMemoryReliableStore(directory: dir, scope: "group-a")
        let raw = "api key: sk-syntheticsecret 手机 13800138000 身份证 110101199001011234 邮箱 a@b.com"
        let source = QQMemoryEvent(id: "secret", subject: "PEER", text: raw, at: now)
        _ = try store.ingest(source)
        _ = try store.apply(try delta(source: source, text: raw), batch: store.batch(), candidateIDs: [], now: now)
        let state = try String(contentsOf: store.stateURL, encoding: .utf8)
        #expect(!state.contains("syntheticsecret"))
        #expect(!state.contains("13800138000"))
        #expect(!state.contains("110101199001011234"))
        #expect(!state.contains("a@b.com"))
        #expect(store.items.first?.text.contains("敏感信息省略") == true)
        store.close()
    }

    @Test func episodeVersionsSupersedeInsteadOfOverwriting() throws {
        let dir = try tempDir()
        let store = try QQMemoryReliableStore(directory: dir, scope: "group-a")
        let first = event("s1", "讨论晚饭")
        _ = try store.ingest(first)
        _ = try store.apply(try delta(source: first, text: "讨论晚饭", summary: "大家在聊晚饭"), batch: store.batch(), candidateIDs: [], now: now)
        let second = event("s2", "讨论旅行", at: 100)
        _ = try store.ingest(second)
        _ = try store.apply(try delta(source: second, text: "讨论旅行", summary: "话题转到旅行"), batch: store.batch(), candidateIDs: [], now: now.addingTimeInterval(100))
        #expect(store.episodes.count == 2)
        let old = try #require(store.episodes.first { $0.text.contains("晚饭") })
        let new = try #require(store.episodes.first { $0.text.contains("旅行") })
        #expect(old.supersededBy == new.id)
        #expect(new.version == 2)
        #expect(old.current(now.addingTimeInterval(200)) == false)
        store.close()
    }

    @Test func episodeRetentionKeepsCurrentAndDropsExpiredHistory() throws {
        let dir = try tempDir()
        var options = QQMemoryReliableOptions(); options.episodeRetentionDays = 180
        let store = try QQMemoryReliableStore(directory: dir, scope: "group-a", options: options)
        let first = event("s1", "讨论晚饭")
        _ = try store.ingest(first)
        _ = try store.apply(try delta(source: first, summary: "旧话题"), batch: store.batch(), candidateIDs: [], now: now.addingTimeInterval(-300 * 86400))
        let second = event("s2", "讨论旅行", at: 100)
        _ = try store.ingest(second)
        _ = try store.apply(try delta(source: second, summary: "新话题"), batch: store.batch(), candidateIDs: [], now: now.addingTimeInterval(100))
        _ = try store.vacuum(now: now.addingTimeInterval(200 * 86400))
        #expect(store.episodes.count == 1)
        #expect(store.episodes.first?.text.contains("新话题") == true)
        store.close()
    }

    @Test func legacyImportPreservesItemsPendingAndCounters() throws {
        let dir = try tempDir()
        let store = try QQMemoryReliableStore(directory: dir, scope: "group-a")
        let book = try legacyBook(items: 2, pending: 2, dropped: 7)
        let report = try store.importLegacy(book, now: now)
        #expect(report.items == 2)
        #expect(report.pending == 2)
        #expect(report.droppedEvents == 7)
        #expect(report.archived == 2)
        #expect(report.redacted == 0)
        #expect(report.lossless)
        #expect(store.items.count == 2)
        #expect(store.batch().count == 2)
        #expect(store.stats.droppedEvents == 7)
        store.close()
    }

    @Test func legacyImportRedactsSensitiveTextAndArchivesCopy() throws {
        let dir = try tempDir()
        let store = try QQMemoryReliableStore(directory: dir, scope: "group-a")
        let book = try legacyBook(items: 1, pending: 0, dropped: 0)
        let encoded = try JSONEncoder().encode(book)
        var object = try #require(try JSONSerialization.jsonObject(with: encoded) as? [String: Any])
        var items = try #require(object["items"] as? [[String: Any]])
        items[0]["text"] = "联系方式 13800138000，邮箱 old@example.com"
        object["items"] = items
        let dirty = try JSONDecoder().decode(QQMemoryBook.self, from: JSONSerialization.data(withJSONObject: object))
        let report = try store.importLegacy(dirty, now: now)
        #expect(report.redacted == 1)
        #expect(report.archived == 1)
        #expect(store.items.count == 1)
        #expect(store.items[0].text.contains("13800138000") == false)
        #expect(store.items[0].text.contains("old@example.com") == false)
        #expect(store.stats.archiveRecords >= 1)
        store.close()
    }

    @Test func legacyImportIsIdempotent() throws {
        let dir = try tempDir()
        let store = try QQMemoryReliableStore(directory: dir, scope: "group-a")
        let book = try legacyBook(items: 1, pending: 1, dropped: 0)
        let first = try store.importLegacy(book, now: now)
        let second = try store.importLegacy(book, now: now.addingTimeInterval(1))
        #expect(first.items == 1)
        #expect(second.items == 0 || second.duplicates >= 1)
        #expect(store.items.count == 1)
        #expect(store.batch().count == 1)
        store.close()
    }

    @Test func queueOverloadDoesNotThrowOrDeleteRecentWindow() throws {
        let dir = try tempDir()
        var options = QQMemoryReliableOptions(); options.journal.maxPendingEvents = 8
        let store = try QQMemoryReliableStore(directory: dir, scope: "group-a", options: options)
        for i in 0..<10 {
            let result = try store.ingest(event("q\(i)", at: TimeInterval(i)))
            #expect(accepted(result) == (i < 8))
        }
        #expect(store.recent.count == 10)
        #expect(store.batch().count == 8)
        #expect(store.stats.refusedEvents == 2)
        store.close()
    }

    @Test func corruptPrimaryStateFallsBackToBackup() throws {
        let dir = try tempDir()
        let store = try QQMemoryReliableStore(directory: dir, scope: "group-a")
        let source = event("backup", "我喜欢清淡的晚饭")
        _ = try store.ingest(source)
        _ = try store.apply(try delta(source: source), batch: store.batch(), candidateIDs: [], now: now)
        store.close()
        try Data("not-json".utf8).write(to: store.stateURL, options: .atomic)
        let reopened = try QQMemoryReliableStore(directory: dir, scope: "group-a")
        #expect(reopened.items.count == 1)
        #expect(reopened.items.first?.text.contains("清淡") == true)
        reopened.close()
    }

    @Test func scopeMismatchOnReopenIsRejected() throws {
        let dir = try tempDir()
        let store = try QQMemoryReliableStore(directory: dir, scope: "group-a")
        _ = try store.ingest(event("1"))
        store.close()
        do {
            _ = try QQMemoryReliableStore(directory: dir, scope: "group-b")
            Issue.record("expected scope mismatch rejection")
        } catch {
            #expect(error is QQMemoryReliableError)
        }
    }

    @Test func eraseAllRemovesStateJournalAndArchive() throws {
        let dir = try tempDir()
        let store = try QQMemoryReliableStore(directory: dir, scope: "group-a")
        let source = event("erase", "我喜欢清淡的晚饭")
        _ = try store.ingest(source)
        _ = try store.apply(try delta(source: source), batch: store.batch(), candidateIDs: [], now: now)
        try store.eraseAll()
        #expect(store.items.isEmpty)
        #expect(store.episodes.isEmpty)
        #expect(store.batch().isEmpty)
        store.close()
        let reopened = try QQMemoryReliableStore(directory: dir, scope: "group-a")
        #expect(reopened.items.isEmpty)
        reopened.close()
    }

    @Test func statsReflectQueuedAppliedAndRefusedCounts() throws {
        let dir = try tempDir()
        let store = try QQMemoryReliableStore(directory: dir, scope: "group-a")
        let source = event("s", "我喜欢清淡的晚饭")
        _ = try store.ingest(source)
        _ = try store.apply(try delta(source: source), batch: store.batch(), candidateIDs: [], now: now)
        let stats = store.stats
        #expect(stats.activeItems == 1)
        #expect(stats.queuedCount == 0)
        #expect(stats.episodeCount == 0)
        #expect(stats.scope == "group-a")
        store.close()
    }

    @Test func longTextIsTruncatedBeforePersisting() throws {
        let dir = try tempDir()
        let store = try QQMemoryReliableStore(directory: dir, scope: "group-a")
        let source = event("long", String(repeating: "长", count: 2000))
        _ = try store.ingest(source)
        #expect(store.recent.last?.text.count ?? 0 <= 1200)
        let acceptedText = String(repeating: "长", count: 180)
        _ = try store.apply(try delta(source: source, text: acceptedText, evidence: acceptedText), batch: store.batch(), candidateIDs: [], now: now)
        #expect(store.items.first?.text.count ?? 0 <= 260)
        store.close()
    }

    @Test func applyRejectsReplacingAnAlreadyInvalidatedFact() throws {
        let dir = try tempDir()
        let store = try QQMemoryReliableStore(directory: dir, scope: "group-a")
        let first = event("a", "我喜欢清淡的晚饭")
        _ = try store.ingest(first)
        _ = try store.apply(try delta(source: first), batch: store.batch(), candidateIDs: [], now: now)
        let old = try #require(store.items.first)
        let correction = event("b", "我现在喜欢吃辣", at: 10)
        _ = try store.ingest(correction)
        _ = try store.apply(try delta(source: correction, replaces: [old.id]), batch: store.batch(), candidateIDs: [old.id], now: now.addingTimeInterval(10))
        let third = event("c", "我又改回清淡", at: 20)
        _ = try store.ingest(third)
        do {
            _ = try store.apply(try delta(source: third, replaces: [old.id]), batch: store.batch(), candidateIDs: [old.id], now: now.addingTimeInterval(20))
            Issue.record("expected invalidated replacement rejection")
        } catch {
            #expect(store.items.first(where: { $0.text.contains("吃辣") })?.invalidatedAt == nil)
        }
        store.close()
    }


    @Test func crashAfterJournalAppendBeforeStatePersistReplaysQueue() throws {
        let dir = try tempDir()
        let journal = try QQMemoryJournal(directory: dir.appendingPathComponent("journal"))
        #expect(accepted(try journal.append(event("crash-before-state", "崩溃前已写入日志"))))
        journal.close()
        let store = try QQMemoryReliableStore(directory: dir, scope: "group-a")
        #expect(store.batch().map(\.event.id) == ["crash-before-state"])
        #expect(store.recent.contains { $0.id == "crash-before-state" })
        store.close()
    }

    @Test func crashAfterStatePersistBeforeCursorDoesNotDuplicate() throws {
        let dir = try tempDir()
        let store = try QQMemoryReliableStore(directory: dir, scope: "group-a")
        let source = event("crash-after-state", "已经写入状态但游标未推进")
        _ = try store.ingest(source)
        _ = try store.apply(try delta(source: source), batch: store.batch(), candidateIDs: [], now: now)
        store.close()
        let cursor = dir.appendingPathComponent("journal/cursor.json")
        let reset = "{\"v\":1,\"committedThrough\":0,\"committedIDs\":[],\"updatedAt\":\"2026-09-21T00:00:00Z\"}"
        try Data(reset.utf8).write(to: cursor, options: .atomic)
        let reopened = try QQMemoryReliableStore(directory: dir, scope: "group-a")
        #expect(reopened.items.count == 1)
        #expect(reopened.batch().isEmpty)
        #expect(reopened.recent.contains { $0.id == "crash-after-state" })
        reopened.close()
    }

    @Test func byteHighWatermarkTriggersBeforeCountLimit() throws {
        let dir = try tempDir()
        var options = QQMemoryReliableOptions()
        options.journal.maxPendingBytes = 64 * 1024
        options.journal.highWatermarkRatio = 0.25
        let store = try QQMemoryReliableStore(directory: dir, scope: "group-a", options: options)
        let text = String(repeating: "x", count: 260)
        for i in 0..<100 {
            let id = "big-\(i)"
            #expect(accepted(try store.ingest(event(id, text))))
        }
        #expect(store.stats.queuedCount == 100)
        #expect(store.stats.queueBytes >= 20 * 1024)
        #expect(store.stats.highWatermark)
        store.close()
    }

    @Test func sixScopeDiskAndLatencyBudget() throws {
        let root = try tempDir()
        var stores: [QQMemoryReliableStore] = []
        for s in 0..<6 {
            let store = try QQMemoryReliableStore(directory: root.appendingPathComponent("scope-\(s)"), scope: "scope-\(s)")
            for i in 0..<12 {
                let source = event("s\(s)-\(i)", "第\(i)条合成记忆", subject: "MEMBER_\(i % 3)", at: TimeInterval(i))
                _ = try store.ingest(source)
                _ = try store.apply(try delta(source: source), batch: store.batch(), candidateIDs: [], now: now.addingTimeInterval(Double(i)))
            }
            stores.append(store)
        }
        let appendStart = Date()
        for i in 0..<200 {
            let source = event("latency-\(i)", "延迟测试", at: TimeInterval(i))
            _ = try stores[0].ingest(source)
            if let entry = stores[0].batch(limit: 1).first {
                _ = try stores[0].apply(try delta(source: entry.event), batch: [entry], candidateIDs: [], now: now.addingTimeInterval(Double(i)))
            }
        }
        let appendMs = Date().timeIntervalSince(appendStart) * 1000 / 200
        let contextStart = Date()
        for _ in 0..<200 { _ = stores[0].context("合成记忆", subject: "MEMBER_0", characters: 1800, now: now) }
        let contextMs = Date().timeIntervalSince(contextStart) * 1000 / 200
        func directoryBytes(_ url: URL) -> Int {
            let names = (try? FileManager.default.contentsOfDirectory(atPath: url.path)) ?? []
            var total = 0
            for name in names {
                let child = url.appendingPathComponent(name)
                let values = try? child.resourceValues(forKeys: [.isDirectoryKey, .fileSizeKey])
                if values?.isDirectory == true { total += directoryBytes(child) } else { total += values?.fileSize ?? 0 }
            }
            return total
        }
        let bytes = directoryBytes(root)
        print("QQ memory reliable benchmark: append+apply \(String(format: "%.2f", appendMs)) ms/op, context \(String(format: "%.2f", contextMs)) ms/query, 6 scopes disk \(bytes / 1024) KB")
        #expect(appendMs < 100)
        #expect(contextMs < 50)
        for store in stores { store.close() }
    }

    private func legacyBook(items: Int, pending: Int, dropped: Int) throws -> QQMemoryBook {
        var book = QQMemoryBook()
        for i in 0..<items {
            let source = event("legacy\(i)", "第\(i)条旧记忆")
            book.append(source)
            let change: [String: Any] = [
                "operation": "upsert", "replaces": [], "subject": "PEER", "kind": "preference",
                "text": "第\(i)条旧记忆", "keywords": ["旧记忆"], "importance": 3,
                "sourceID": source.id, "evidence": source.text, "days": 365,
            ]
            let data = try JSONSerialization.data(withJSONObject: ["summary": "", "changes": [change]])
            let delta = try QQMemoryDelta.decode(String(decoding: data, as: UTF8.self))
            try book.apply(delta, batch: book.batch(), candidateIDs: [], now: now)
        }
        for i in 0..<pending { book.append(event("pending\(i)", "待整理\(i)", at: TimeInterval(i))) }
        let encoded = try JSONEncoder().encode(book)
        var object = try #require(try JSONSerialization.jsonObject(with: encoded) as? [String: Any])
        object["droppedEvents"] = dropped
        let data = try JSONSerialization.data(withJSONObject: object)
        return try JSONDecoder().decode(QQMemoryBook.self, from: data)
    }
}
