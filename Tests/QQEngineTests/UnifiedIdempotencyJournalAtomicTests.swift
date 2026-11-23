import Foundation
import Testing
@testable import WeChatAIBot

private enum InjectedJournalFailure: Error {
    case write
}

struct UnifiedIdempotencyJournalAtomicTests {
    private func temporaryDirectory() throws -> URL {
        let directory = FileManager.default.temporaryDirectory
            .appendingPathComponent("unified-journal-atomic-" + UUID().uuidString)
        try FileManager.default.createDirectory(at: directory,
                                                withIntermediateDirectories: true)
        return directory
    }

    @Test func failedReserveDoesNotAdmitEntryOrCallSuccessfulWriter() async throws {
        let directory = try temporaryDirectory()
        defer { try? FileManager.default.removeItem(at: directory) }
        let file = directory.appendingPathComponent("journal.json")
        let journal = UnifiedIdempotencyJournal(fileURL: file, capacity: 8) { _, _ in
            throw InjectedJournalFailure.write
        }

        #expect(await journal.reserve("opaque-key", at: 1) == false)
        #expect(await journal.lookup("opaque-key") == nil)
        #expect(await journal.count() == 0)
        #expect(FileManager.default.fileExists(atPath: file.path) == false)
    }

    @Test func failedReplacementKeepsValidPendingFileAndMemoryEntry() async throws {
        let directory = try temporaryDirectory()
        defer { try? FileManager.default.removeItem(at: directory) }
        let file = directory.appendingPathComponent("journal.json")
        let initial = UnifiedIdempotencyJournal(fileURL: file, capacity: 8)
        #expect(await initial.reserve("existing-pending", at: 1))
        let originalBytes = try Data(contentsOf: file)

        let failing = UnifiedIdempotencyJournal(fileURL: file, capacity: 8) { _, _ in
            throw InjectedJournalFailure.write
        }
        #expect(await failing.lookup("existing-pending")?.state == "pending")
        #expect(await failing.reserve("new-key", at: 2) == false)
        #expect(await failing.lookup("new-key") == nil)
        #expect(await failing.lookup("existing-pending")?.state == "pending")
        #expect(try Data(contentsOf: file) == originalBytes)

        let restarted = UnifiedIdempotencyJournal(fileURL: file, capacity: 8)
        #expect(await restarted.lookup("existing-pending")?.state == "pending")
        #expect(await restarted.lookup("new-key") == nil)
    }

    @Test func corruptV1FileIsNotTreatedAsAnEmptyJournal() async throws {
        let directory = try temporaryDirectory()
        defer { try? FileManager.default.removeItem(at: directory) }
        let file = directory.appendingPathComponent("journal.json")
        let corrupt = Data("{not valid journal".utf8)
        try corrupt.write(to: file)

        let journal = UnifiedIdempotencyJournal(fileURL: file, capacity: 8)
        #expect(await journal.reserve("must-not-generate", at: 1) == false)
        #expect(await journal.lookup("must-not-generate") == nil)
        #expect(try Data(contentsOf: file) == corrupt)
    }

    @Test func failedCreateIsClosedAndNilFixtureRemainsInMemory() async throws {
        let directory = try temporaryDirectory()
        defer { try? FileManager.default.removeItem(at: directory) }
        let blocker = directory.appendingPathComponent("regular-file")
        try Data("blocker".utf8).write(to: blocker)
        let impossibleFile = blocker.appendingPathComponent("journal.json")
        let diskJournal = UnifiedIdempotencyJournal(fileURL: impossibleFile, capacity: 8)
        #expect(await diskJournal.reserve("must-not-generate", at: 1) == false)
        #expect(await diskJournal.lookup("must-not-generate") == nil)

        let memoryJournal = UnifiedIdempotencyJournal(fileURL: nil, capacity: 8)
        #expect(await memoryJournal.reserve("fixture-key", at: 1))
        #expect(await memoryJournal.lookup("fixture-key")?.state == "pending")
        #expect(await memoryJournal.count() == 1)
    }

    @Test func successfulPendingReservationSurvivesRestartWithPrivatePermissions() async throws {
        let directory = try temporaryDirectory()
        defer { try? FileManager.default.removeItem(at: directory) }
        let file = directory.appendingPathComponent("journal.json")
        let first = UnifiedIdempotencyJournal(fileURL: file, capacity: 8)
        #expect(await first.reserve("opaque-restart-key", at: 1))

        let restarted = UnifiedIdempotencyJournal(fileURL: file, capacity: 8)
        #expect(await restarted.lookup("opaque-restart-key")?.state == "pending")
        #expect(await restarted.reserve("opaque-restart-key", at: 2) == false)
        let attributes = try FileManager.default.attributesOfItem(atPath: file.path)
        #expect((attributes[.posixPermissions] as? NSNumber)?.intValue == 0o600)
    }
}
