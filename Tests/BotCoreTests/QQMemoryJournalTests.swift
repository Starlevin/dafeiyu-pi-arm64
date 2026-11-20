import Foundation
import Testing
@testable import BotCore

struct QQMemoryJournalTests {
    private func tempDir() throws -> URL {
        let url = FileManager.default.temporaryDirectory.appendingPathComponent("qqmem-journal-\(UUID().uuidString)", isDirectory: true)
        try FileManager.default.createDirectory(at: url, withIntermediateDirectories: true)
        return url
    }

    private func event(_ id: String, _ text: String = "合成消息", at offset: TimeInterval = 0, subject: String = "PEER") -> QQMemoryEvent {
        QQMemoryEvent(id: id, subject: subject, text: text, at: Date(timeIntervalSince1970: 1_790_000_000 + offset))
    }

    private func accepted(_ result: QQMemoryJournalAppend) -> Bool {
        if case .accepted = result { return true }
        return false
    }

    @Test func appendAndReopenPersistsPendingEntries() throws {
        let dir = try tempDir()
        let journal = try QQMemoryJournal(directory: dir)
        #expect(accepted(try journal.append(event("a"))))
        #expect(accepted(try journal.append(event("b", at: 1))))
        journal.close()
        let reopened = try QQMemoryJournal(directory: dir)
        let entries = reopened.entries()
        #expect(entries.map(\.event.id) == ["a", "b"])
        #expect(entries.map(\.seq) == [1, 2])
        reopened.close()
    }

    @Test func duplicateIDIsNotQueuedTwice() throws {
        let dir = try tempDir()
        let journal = try QQMemoryJournal(directory: dir)
        #expect(accepted(try journal.append(event("same"))))
        let duplicate = try journal.append(event("same", at: 100))
        #expect(duplicate == .duplicate)
        #expect(journal.entries().count == 1)
        journal.close()
    }

    @Test func tornTailIsTruncatedAndPriorEntriesSurvive() throws {
        let dir = try tempDir()
        let journal = try QQMemoryJournal(directory: dir)
        for id in ["1", "2", "3"] { #expect(accepted(try journal.append(event(id)))) }
        journal.close()
        let segment = try #require(journal.segmentURLs().last)
        let handle = try FileHandle(forWritingTo: segment)
        handle.seekToEndOfFile()
        let torn = "{\"v\":1,\"seq\":99,\"event\":{\"id\":\"torn\""
        handle.write(torn.data(using: .utf8)!)
        try handle.close()
        let reopened = try QQMemoryJournal(directory: dir)
        #expect(reopened.entries().map(\.event.id) == ["1", "2", "3"])
        #expect(reopened.stats().corruptRecords == 0)
        reopened.close()
    }

    @Test func corruptedRecordIsQuarantinedAndOthersRemainReadable() throws {
        let dir = try tempDir()
        let journal = try QQMemoryJournal(directory: dir)
        for id in ["1", "2", "3"] { #expect(accepted(try journal.append(event(id)))) }
        journal.close()
        let segment = try #require(journal.segmentURLs().last)
        let text = try String(contentsOf: segment, encoding: .utf8)
        var lines = text.split(separator: "\n", omittingEmptySubsequences: false)
        lines[1] = Substring(String(lines[1]).replacingOccurrences(of: "\"checksum\":\"", with: "\"checksum\":\"corrupt"))
        try lines.joined(separator: "\n").write(to: segment, atomically: true, encoding: .utf8)
        let reopened = try QQMemoryJournal(directory: dir)
        #expect(reopened.entries().map(\.event.id) == ["1", "3"])
        #expect(reopened.stats().corruptRecords == 1)
        reopened.close()
    }

    @Test func capacityRefusesNewEventsWithoutDeletingQueuedOnes() throws {
        let dir = try tempDir()
        var options = QQMemoryJournalOptions(); options.maxPendingEvents = 8
        let journal = try QQMemoryJournal(directory: dir, options: options)
        for i in 0..<10 {
            let result = try journal.append(event("cap\(i)", at: TimeInterval(i)))
            #expect(accepted(result) == (i < 8))
        }
        #expect(journal.entries().count == 8)
        #expect(journal.stats().refusedEvents == 2)
        #expect(journal.entries().first?.event.id == "cap0")
        #expect(journal.entries().last?.event.id == "cap7")
        journal.close()
    }

    @Test func highWatermarkTurnsOnAtConfiguredRatio() throws {
        let dir = try tempDir()
        var options = QQMemoryJournalOptions(); options.maxPendingEvents = 8; options.highWatermarkRatio = 0.75
        let journal = try QQMemoryJournal(directory: dir, options: options)
        for i in 0..<5 { #expect(accepted(try journal.append(event("w\(i)", at: TimeInterval(i))))) }
        #expect(!journal.stats().highWatermark)
        #expect(accepted(try journal.append(event("w5", at: 5))))
        #expect(journal.stats().highWatermark)
        journal.close()
    }

    @Test func batchIsChronologicalAndHonorsCountAndCharacterBudgets() throws {
        let dir = try tempDir()
        let journal = try QQMemoryJournal(directory: dir)
        for i in 0..<6 {
            #expect(accepted(try journal.append(event("b\(i)", String(repeating: "字", count: 20), at: TimeInterval(6 - i)))))
        }
        let batch = journal.batch(limit: 3, maxCharacters: 70)
        #expect(batch.count <= 3)
        #expect(batch.map(\.seq) == batch.map(\.seq).sorted())
        let single = journal.batch(limit: 1, maxCharacters: 1000)
        #expect(single.count == 1)
        #expect(single[0].seq == 1)
        journal.close()
    }

    @Test func commitAdvancesCursorAndPendingBecomesEmpty() throws {
        let dir = try tempDir()
        let journal = try QQMemoryJournal(directory: dir)
        for id in ["1", "2", "3", "4"] { #expect(accepted(try journal.append(event(id)))) }
        let committed = journal.entries(limit: 2)
        #expect(try journal.commit(ids: Set(committed.map(\.event.id)), through: committed.last!.seq) == 2)
        #expect(journal.entries().map(\.event.id) == ["3", "4"])
        journal.close()
        let reopened = try QQMemoryJournal(directory: dir)
        #expect(reopened.entries().map(\.event.id) == ["3", "4"])
        reopened.close()
    }

    @Test func committedEntriesAreNotResurrectedAfterReopen() throws {
        let dir = try tempDir()
        let journal = try QQMemoryJournal(directory: dir)
        for id in ["1", "2"] { #expect(accepted(try journal.append(event(id)))) }
        let all = journal.entries()
        #expect(try journal.commit(ids: Set(all.map(\.event.id)), through: all.last!.seq) == 2)
        journal.close()
        let reopened = try QQMemoryJournal(directory: dir)
        #expect(reopened.entries().isEmpty)
        #expect(reopened.recover(knownIDs: []).isEmpty)
        reopened.close()
    }

    @Test func recoverySkipsKnownIDsAndKeepsUnknownOnes() throws {
        let dir = try tempDir()
        let journal = try QQMemoryJournal(directory: dir)
        for id in ["1", "2", "3"] { #expect(accepted(try journal.append(event(id)))) }
        journal.close()
        let reopened = try QQMemoryJournal(directory: dir)
        let recovered = reopened.recover(knownIDs: ["1"])
        #expect(recovered.map(\.event.id) == ["2", "3"])
        reopened.close()
    }

    @Test func explicitScopeLockRejectsSecondWriter() throws {
        let dir = try tempDir()
        let first = try QQMemoryJournal(directory: dir)
        do {
            _ = try QQMemoryJournal(directory: dir)
            Issue.record("expected second writer to be rejected")
        } catch {
            #expect(error is QQMemoryJournalError)
        }
        first.close()
        let second = try QQMemoryJournal(directory: dir)
        second.close()
    }

    @Test func eraseRemovesJournalFilesAndRecreatesEmptyScope() throws {
        let dir = try tempDir()
        let journal = try QQMemoryJournal(directory: dir)
        #expect(accepted(try journal.append(event("1"))))
        try journal.erase()
        #expect(journal.entries().isEmpty)
        #expect(journal.stats().pendingCount == 0)
        journal.close()
        let reopened = try QQMemoryJournal(directory: dir)
        #expect(reopened.entries().isEmpty)
        reopened.close()
    }

    @Test func statsReportBytesAndOldestPending() throws {
        let dir = try tempDir()
        let journal = try QQMemoryJournal(directory: dir)
        #expect(accepted(try journal.append(event("1", at: 0))))
        #expect(accepted(try journal.append(event("2", at: 10))))
        let stats = journal.stats()
        #expect(stats.pendingCount == 2)
        #expect(stats.pendingBytes > 0)
        #expect(stats.oldestPendingAt == Date(timeIntervalSince1970: 1_790_000_000))
        #expect(stats.segmentCount >= 1)
        journal.close()
    }

    @Test func segmentRotationAndRetentionKeepDiskBounded() throws {
        let dir = try tempDir()
        var options = QQMemoryJournalOptions()
        options.segmentBytes = 1024
        options.maxPendingEvents = 64
        options.committedSegmentRetention = 2
        let journal = try QQMemoryJournal(directory: dir, options: options)
        for i in 0..<40 {
            #expect(accepted(try journal.append(event("r\(i)", String(repeating: "长", count: 30), at: TimeInterval(i)))))
            let pending = journal.entries(limit: 2)
            if pending.count == 2 {
                _ = try journal.commit(ids: Set(pending.map(\.event.id)), through: pending.last!.seq)
            }
        }
        #expect(journal.segmentURLs().count <= 4)
        #expect(journal.stats().pendingCount <= 2)
        journal.close()
    }

    @Test func vacuumDropsExpiredCommittedSegmentsOnly() throws {
        let dir = try tempDir()
        var options = QQMemoryJournalOptions(); options.committedRetentionDays = 1; options.committedSegmentRetention = 1
        let journal = try QQMemoryJournal(directory: dir, options: options)
        #expect(accepted(try journal.append(event("1", at: -100 * 86400))))
        let entries = journal.entries()
        #expect(try journal.commit(ids: Set(entries.map(\.event.id)), through: entries.last!.seq) == 1)
        #expect(try journal.vacuum(now: Date(timeIntervalSince1970: 1_790_000_000)) >= 0)
        #expect(journal.entries().isEmpty)
        journal.close()
    }
}
