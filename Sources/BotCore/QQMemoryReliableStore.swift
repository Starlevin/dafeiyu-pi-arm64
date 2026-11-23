import Foundation
#if canImport(CryptoKit)
import CryptoKit
#else
import Crypto
#endif

public struct QQMemoryReliableOptions: Codable, Equatable, Sendable {
    public var journal = QQMemoryJournalOptions()
    public var thresholds = QQMemoryOptions()
    public var maxActiveItems = 256
    public var maxHistoricalItems = 128
    public var maxEpisodes = 256
    public var episodeRetentionDays = 180
    public var historyRetentionDays = 90
    public var maxItemTextCharacters = 260

    public init() {}

    public func validate() throws {
        try journal.validate()
        try thresholds.validate()
        guard (64...1024).contains(maxActiveItems), (0...512).contains(maxHistoricalItems),
              (16...1024).contains(maxEpisodes), (30...365).contains(episodeRetentionDays),
              (30...365).contains(historyRetentionDays), (80...600).contains(maxItemTextCharacters) else {
            throw QQMemoryReliableError.invalid("可靠记忆容量参数超出安全范围")
        }
    }
}

public enum QQMemoryReliableError: LocalizedError, Equatable {
    case invalid(String)
    case corruptState(String)
    case scopeMismatch(String)
    case importWouldLoseData(String)
    public var errorDescription: String? {
        switch self {
        case .invalid(let m): return "可靠记忆参数无效：\(m)"
        case .corruptState(let m): return "可靠记忆状态损坏：\(m)"
        case .scopeMismatch(let m): return "记忆作用域不匹配：\(m)"
        case .importWouldLoseData(let m): return "旧记忆导入会丢数据：\(m)"
        }
    }
}

public struct QQMemoryEpisode: Codable, Equatable, Sendable, Identifiable {
    public var id: String
    public let subject: String
    public var text: String
    public var sourceIDs: [String]
    public var version: Int
    public var supersedes: String?
    public var supersededBy: String?
    public var createdAt: Date
    public var expiresAt: Date
    public func current(_ now: Date) -> Bool { supersededBy == nil && expiresAt > now }
    public init(id: String = UUID().uuidString, text: String, sourceIDs: [String], version: Int,
                supersedes: String? = nil, createdAt: Date, expiresAt: Date) {
        self.id = id; self.subject = "CONVERSATION"; self.text = text; self.sourceIDs = sourceIDs
        self.version = version; self.supersedes = supersedes; self.supersededBy = nil
        self.createdAt = createdAt; self.expiresAt = expiresAt
    }
}

public struct QQMemoryApplyReport: Codable, Equatable, Sendable {
    public var batchCount = 0
    public var changes = 0
    public var newItems = 0
    public var invalidated = 0
    public var episodeVersion: Int?
    public var committed = 0
}

public struct QQMemoryImportReport: Codable, Equatable, Sendable {
    public var items = 0
    public var historical = 0
    public var pending = 0
    public var episodes = 0
    public var duplicates = 0
    public var redacted = 0
    public var archived = 0
    public var forgotten = 0
    public var droppedEvents = 0
    public var lossless = false
}

public struct QQMemoryReliableStats: Codable, Equatable, Sendable {
    public var scope = ""
    public var queuedCount = 0
    public var queueBytes = 0
    public var refusedEvents = 0
    public var droppedEvents = 0
    public var activeItems = 0
    public var historicalItems = 0
    public var episodeCount = 0
    public var archiveRecords = 0
    public var highWatermark = false
    public var corruptJournalRecords = 0
    public var trimmedItems = 0
    public var trimmedEpisodes = 0
    public var lastCompactedAt: Date?
}

/// Three layers:
/// 1. bounded recent window (24 excerpts);
/// 2. per-subject current facts/plans plus bounded historical versions;
/// 3. versioned conversation episodes and append-only audit archive.
/// Pending excerpts live in a checksummed WAL, not in the state JSON.
public final class QQMemoryReliableStore {
    private struct State: Codable {
        var v = 1
        var scope: String
        var items: [QQMemoryItem] = []
        var recent: [QQMemoryEvent] = []
        var knownIDs: [String] = []
        var appliedIDs: [String] = []
        var legacyImported: Bool?
        var forgottenIDs: [String]?
        var episodes: [QQMemoryEpisode] = []
        var droppedEvents = 0
        var refusedEvents = 0
        var trimmedItems = 0
        var trimmedEpisodes = 0
        var lastCompactedAt: Date?
    }

    private struct ArchivedItem: Codable, Equatable {
        var id: String
        var subject: String
        var kind: String
        var text: String
        var sourceIDs: [String]
        var validFrom: Date
        var validTo: Date
        var reason: String
    }

    private struct ArchiveRecord: Codable {
        var kind: String
        var item: ArchivedItem?
        var episode: QQMemoryEpisode?
        var ids: [String]?
        var at: Date
    }

    public let directoryURL: URL
    public let scope: String
    public let options: QQMemoryReliableOptions
    public let stateURL: URL
    private let backupURL: URL
    private let archiveURL: URL
    private let journal: QQMemoryJournal
    private var state: State
    private var archiveRecords = 0
    private let encoder: JSONEncoder
    private let decoder: JSONDecoder

    public init(directory: URL, scope: String, options: QQMemoryReliableOptions = QQMemoryReliableOptions()) throws {
        try options.validate()
        guard !scope.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty, scope.count <= 160 else {
            throw QQMemoryReliableError.invalid("作用域为空或过长")
        }
        self.directoryURL = directory
        self.scope = scope
        self.options = options
        self.stateURL = directory.appendingPathComponent("state.json")
        self.backupURL = directory.appendingPathComponent("state.json.bak")
        self.archiveURL = directory.appendingPathComponent("archive.jsonl")
        self.encoder = JSONEncoder(); encoder.dateEncodingStrategy = .iso8601; encoder.outputFormatting = [.sortedKeys]
        self.decoder = JSONDecoder(); decoder.dateDecodingStrategy = .iso8601
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        self.state = try Self.loadState(stateURL: stateURL, backupURL: backupURL, scope: scope, decoder: decoder, encoder: encoder)
        self.journal = try QQMemoryJournal(directory: directory.appendingPathComponent("journal"), options: options.journal)
        self.archiveRecords = Self.countArchiveRecords(at: archiveURL, decoder: decoder)
        try mergeArchiveTombstones()
        try recoverFromJournal()
    }

    deinit { journal.close() }

    public var items: [QQMemoryItem] { state.items }
    public var recent: [QQMemoryEvent] { state.recent }
    public var episodes: [QQMemoryEpisode] { state.episodes }
    public var hasImportedLegacy: Bool { state.legacyImported ?? false }
    public var forgottenIDs: [String] { state.forgottenIDs ?? [] }

    public var stats: QQMemoryReliableStats {
        let journalStats = journal.stats()
        var stats = QQMemoryReliableStats()
        stats.scope = scope
        stats.queuedCount = journalStats.pendingCount
        stats.queueBytes = journalStats.pendingBytes
        stats.refusedEvents = state.refusedEvents
        stats.droppedEvents = state.droppedEvents
        stats.activeItems = state.items.filter { $0.kind != "episode" && $0.current(Date()) }.count
        stats.historicalItems = state.items.filter { $0.kind != "episode" && !$0.current(Date()) }.count
        stats.episodeCount = state.episodes.count
        stats.archiveRecords = archiveRecords
        stats.highWatermark = journalStats.highWatermark
        stats.corruptJournalRecords = journalStats.corruptRecords
        stats.trimmedItems = state.trimmedItems
        stats.trimmedEpisodes = state.trimmedEpisodes
        stats.lastCompactedAt = state.lastCompactedAt
        return stats
    }

    @discardableResult
    public func ingest(_ event: QQMemoryEvent, now: Date = Date()) throws -> QQMemoryJournalAppend {
        let queueable = !["BOT", "SELF_UNKNOWN"].contains(event.subject)
        let result: QQMemoryJournalAppend
        if queueable {
            result = try journal.append(event, now: now)
            if case .refused = result { state.refusedEvents += 1 }
        } else {
            result = .storedOnly
        }
        note(event)
        try persistState()
        return result
    }

    public func due(now: Date = Date()) -> Bool {
        let entries = journal.entries(knownIDs: Set(state.appliedIDs))
        guard let oldest = entries.first?.event else { return false }
        let chars = entries.reduce(0) { $0 + $1.event.text.count }
        return entries.count >= options.thresholds.messageThreshold ||
            chars >= options.thresholds.characterThreshold ||
            now.timeIntervalSince(oldest.at) >= Double(options.thresholds.intervalMinutes * 60)
    }

    public func batch(limit: Int = 32, maxCharacters: Int = 7500) -> [QQMemoryJournalEntry] {
        journal.batch(knownIDs: Set(state.appliedIDs), limit: limit, maxCharacters: maxCharacters)
    }

    /// A named group note, with a delivery-proven event as its source.
    /// The caller binds this store to exactly one conversation.
    public func recordConfirmedGroupNote(id: String, label: String, value: String, kind: String,
                                         inferred: Bool = false, at now: Date,
                                         retainingExpiry: Date? = nil) throws {
        let key = label.precomposedStringWithCanonicalMapping.lowercased()
        let text = label + "：" + value
        guard !label.isEmpty, key.count <= 20, !value.isEmpty, value.count <= 140,
              !text.contains(where: \.isNewline), !QQMemoryRedactor.containsSensitive(text),
              ["fact", "preference", "task", "event"].contains(kind) else {
            throw QQMemoryReliableError.invalid("无效的群记忆条目")
        }
        guard !state.appliedIDs.contains(id) else { return }
        let subject = "CONVERSATION"
        let prior = state.items.filter {
            $0.subject == subject && $0.keywords == [key] && $0.current(now)
        }
        if inferred && prior.contains(where: { $0.inferredFromConversation != true }) { return }
        if prior.contains(where: { $0.updatedAt > now }) { return }
        let event = QQMemoryEvent(id: id, subject: subject, text: text, at: now)
        let recorded = try ingest(event, now: now)
        let entry = recorded.entry ?? journal.entries(knownIDs: Set(state.appliedIDs)).first { $0.id == id }
        guard let entry else { throw QQMemoryReliableError.invalid("群记忆日志未接受条目") }
        let candidates = prior.sorted { $0.updatedAt > $1.updatedAt }.prefix(6).map(\.id)
        let change = QQMemoryDelta.Change(operation: "upsert", replaces: candidates, subject: subject,
                                          kind: kind, text: text, keywords: [key], importance: 2,
                                          sourceID: id, evidence: text, days: inferred
                                            ? (kind == "task" ? 14 : 45) : (kind == "task" ? 30 : 365))
        let unchanged = prior.contains {
            $0.kind == kind && ($0.inferredFromConversation == true) == inferred
                && (retainingExpiry != nil
                    ? $0.text == text
                    : QQReplyRepetitionPolicy.normalized($0.text) == QQReplyRepetitionPolicy.normalized(text))
        }
        try apply(QQMemoryDelta(summary: "", changes: unchanged ? [] : [change]), batch: [entry],
                  candidateIDs: Set(candidates), now: now, inferredFromConversation: inferred,
                  retainingExpiry: retainingExpiry)
    }

    @discardableResult
    public func apply(_ delta: QQMemoryDelta, batch: [QQMemoryJournalEntry], candidateIDs: Set<String>,
                      now: Date = Date(), inferredFromConversation: Bool = false,
                      retainingExpiry: Date? = nil) throws -> QQMemoryApplyReport {
        guard !batch.isEmpty else { throw QQMemoryReliableError.invalid("空记忆批次") }
        let pendingIDs = Set(journal.entries(knownIDs: Set(state.appliedIDs)).map(\.event.id))
        guard Set(batch.map(\.event.id)).isSubset(of: pendingIDs) else {
            throw QQMemoryReliableError.invalid("记忆批次已变化，等待下一轮")
        }
        let sources = Dictionary(uniqueKeysWithValues: batch.map { ($0.event.id, $0.event) })
        let allowedKinds = ["preference", "fact", "task", "event"]
        for change in delta.changes {
            guard ["upsert", "forget"].contains(change.operation), allowedKinds.contains(change.kind),
                  let source = sources[change.sourceID], source.subject == change.subject,
                  !change.evidence.isEmpty, change.evidence.count <= 200, source.text.contains(change.evidence),
                  change.replaces.count <= 6, Set(change.replaces).isSubset(of: candidateIDs),
                  change.replaces.allSatisfy({ id in state.items.contains { $0.id == id && $0.subject == change.subject && $0.current(now) } }),
                  change.text.count <= 180, change.keywords.count <= 6,
                  change.keywords.allSatisfy({ !$0.isEmpty && $0.count <= 20 }),
                  (1...3).contains(change.importance), (1...365).contains(change.days),
                  (change.operation == "forget" ? !change.replaces.isEmpty && change.text.isEmpty : !change.text.isEmpty) else {
                throw QQMemoryReliableError.invalid("记忆证据、归属或更新范围未通过校验")
            }
        }

        let snapshot = state
        var archived: [ArchiveRecord] = []
        var report = QQMemoryApplyReport(batchCount: batch.count, changes: delta.changes.count)
        for change in delta.changes {
            let obsoleteSources = Set(state.items.filter { change.replaces.contains($0.id) }.flatMap(\.sourceIDs))
            for index in state.items.indices where change.replaces.contains(state.items[index].id) ||
                (state.items[index].kind == "episode" && !obsoleteSources.isDisjoint(with: state.items[index].sourceIDs)) {
                if state.items[index].invalidatedAt == nil {
                    state.items[index].invalidatedAt = now
                    report.invalidated += 1
                    archived.append(archiveVersion(of: state.items[index], at: now, reason: change.operation == "forget" ? "forget" : "correction"))
                }
            }
            guard change.operation == "upsert" else { continue }
            let text = String(QQMemoryRedactor.redact(change.text).prefix(options.maxItemTextCharacters))
            if state.items.contains(where: {
                $0.subject == change.subject && $0.kind == change.kind &&
                QQReplyRepetitionPolicy.normalized($0.text) == QQReplyRepetitionPolicy.normalized(text) && $0.current(now)
            }) { continue }
            let days = min(change.days, change.kind == "event" ? 90 : change.kind == "task" ? 30 : 365)
            state.items.append(QQMemoryItem(subject: change.subject, kind: change.kind, text: text,
                                            keywords: change.keywords.map(QQMemoryRedactor.redact),
                                            importance: change.importance, sourceIDs: [change.sourceID],
                                            updatedAt: now, expiresAt: retainingExpiry ?? now.addingTimeInterval(Double(days * 86400)),
                                            inferredFromConversation: inferredFromConversation ? true : nil))
            report.newItems += 1
        }

        let summary = QQMemoryDelta.decodeSummaryFallback(delta.summary)
        if !summary.isEmpty {
            let previous = state.episodes.last(where: { $0.current(now) })
            let version = (state.episodes.map(\.version).max() ?? 0) + 1
            let episode = QQMemoryEpisode(text: summary, sourceIDs: batch.map(\.event.id), version: version,
                                          supersedes: previous?.id, createdAt: now,
                                          expiresAt: now.addingTimeInterval(Double(options.episodeRetentionDays * 86400)))
            if let previous, let index = state.episodes.firstIndex(where: { $0.id == previous.id }) {
                state.episodes[index].supersededBy = episode.id
                archived.append(ArchiveRecord(kind: "episode", item: nil, episode: state.episodes[index], ids: nil, at: now))
            }
            state.episodes.append(episode)
            report.episodeVersion = version
            archived.append(ArchiveRecord(kind: "episode", item: nil, episode: episode, ids: nil, at: now))
        }

        trim(now: now)
        state.knownIDs = mergeIDs(state.knownIDs, batch.map(\.event.id))
        state.appliedIDs = mergeIDs(state.appliedIDs, batch.map(\.event.id))
        state.lastCompactedAt = now
        do {
            try appendArchive(archived)
            try persistState()
        } catch {
            state = snapshot
            throw error
        }
        let through = batch.map(\.seq).max() ?? 0
        report.committed = (try? journal.commit(ids: Set(batch.map(\.event.id)), through: through, now: now)) ?? 0
        return report
    }

    public func search(_ query: String, subject: String, limit: Int = 12, now: Date = Date(), history: Bool = false) -> [QQMemoryItem] {
        QQMemoryIndex(state.items).search(query, subject: subject, limit: limit, now: now, history: history)
    }

    public func context(_ query: String, subject: String, characters: Int, now: Date = Date(), includeEpisodes: Bool = true) -> String {
        let primary = QQMemoryIndex(state.items).context(query, subject: subject, characters: characters, now: now)
        guard includeEpisodes, state.episodes.contains(where: { $0.current(now) }) else { return primary }
        let episodeItems = state.episodes.map { episode in
            QQMemoryItem(id: episode.id, subject: "CONVERSATION", kind: "episode", text: episode.text,
                         keywords: [], importance: 1, sourceIDs: episode.sourceIDs,
                         updatedAt: episode.createdAt, expiresAt: episode.expiresAt,
                         invalidatedAt: episode.supersededBy == nil ? nil : now)
        }
        let remaining = max(0, characters - primary.count - 1)
        guard remaining > 80 else { return primary }
        let episodeContext = QQMemoryIndex(episodeItems).context(query, subject: "CONVERSATION", characters: remaining, now: now)
        return [primary, episodeContext].filter { !$0.isEmpty }.joined(separator: "\n")
    }

    @discardableResult
    public func forget(subject: String, ids: Set<String>, now: Date = Date()) throws -> Int {
        guard !ids.isEmpty else { return 0 }
        var removed = 0
        state.items.removeAll { item in
            guard ids.contains(item.id) else { return false }
            guard subject == "*" || item.subject == subject else { return false }
            removed += 1
            return true
        }
        if subject == "CONVERSATION" || subject == "*" {
            state.episodes.removeAll { episode in
                guard ids.contains(episode.id) || !ids.isDisjoint(with: episode.sourceIDs) else { return false }
                removed += 1
                return true
            }
        }
        let tombstone = ArchiveRecord(kind: "tombstone", item: nil, episode: nil, ids: Array(ids), at: now)
        state.forgottenIDs = mergeIDs(state.forgottenIDs ?? [], Array(ids))
        try appendArchive([tombstone])
        try persistState()
        return removed
    }

    @discardableResult
    public func vacuum(now: Date = Date()) throws -> Int {
        let before = state.items.count + state.episodes.count
        state.items.removeAll { item in
            guard item.kind != "episode", !item.current(now) else { return false }
            return now.timeIntervalSince(item.invalidatedAt ?? item.expiresAt) >= Double(options.historyRetentionDays * 86400)
        }
        state.episodes.removeAll { episode in
            guard !episode.current(now) else { return false }
            if episode.supersededBy != nil {
                return now.timeIntervalSince(episode.createdAt) >= Double(options.episodeRetentionDays * 86400)
            }
            return now.timeIntervalSince(episode.expiresAt) >= Double(options.episodeRetentionDays * 86400)
        }
        if state.episodes.count > options.maxEpisodes {
            let overflow = state.episodes.count - options.maxEpisodes
            state.episodes.removeFirst(overflow)
            state.trimmedEpisodes += overflow
        }
        try rewriteArchive(now: now)
        _ = try journal.vacuum(now: now)
        try persistState()
        return before - (state.items.count + state.episodes.count)
    }

    public func eraseAll() throws {
        try journal.erase()
        try? FileManager.default.removeItem(at: stateURL)
        try? FileManager.default.removeItem(at: backupURL)
        try? FileManager.default.removeItem(at: archiveURL)
        state = State(scope: scope)
        archiveRecords = 0
        try persistState()
    }

    public func close() { journal.close() }

    public func reload() throws {
        state = try Self.loadState(stateURL: stateURL, backupURL: backupURL, scope: scope, decoder: decoder, encoder: encoder)
        archiveRecords = Self.countArchiveRecords(at: archiveURL, decoder: decoder)
        try mergeArchiveTombstones()
        try recoverFromJournal()
    }

    public func importLegacy(_ book: QQMemoryBook, now: Date = Date()) throws -> QQMemoryImportReport {
        let legacyPending = book.pending + (book.archivedPending ?? [])
        let need = legacyPending.count
        let currentPending = journal.stats().pendingCount
        guard currentPending + need <= options.journal.maxPendingEvents else {
            throw QQMemoryReliableError.importWouldLoseData("队列剩余容量不足：现有 \(currentPending)，待导入 \(need)")
        }
        var report = QQMemoryImportReport()
        var archiveBatch: [ArchiveRecord] = []
        for item in book.items {
            if (state.forgottenIDs ?? []).contains(item.id) {
                report.forgotten += 1
                continue
            }
            if state.items.contains(where: { $0.id == item.id }) {
                report.duplicates += 1
                continue
            }
            // 旧 state 可能保存了未脱敏文本；导入时重新脱敏，避免新库继续留存。
            let text = QQMemoryRedactor.redact(item.text)
            if text != item.text { report.redacted += 1 }
            let imported = QQMemoryItem(id: item.id, subject: item.subject, kind: item.kind, text: text,
                                        keywords: item.keywords.map { QQMemoryRedactor.redact($0) },
                                        importance: item.importance, sourceIDs: item.sourceIDs,
                                        updatedAt: item.updatedAt, expiresAt: item.expiresAt,
                                        invalidatedAt: item.invalidatedAt)
            state.items.append(imported)
            // 即使后续 trim 把条目移出活动窗口，archive 里仍保留一份可恢复副本。
            archiveBatch.append(archiveVersion(of: imported, at: now, reason: "legacy-import"))
            if imported.kind == "episode" {
                if !state.episodes.contains(where: { $0.id == imported.id }) {
                    state.episodes.append(QQMemoryEpisode(id: imported.id, text: imported.text, sourceIDs: imported.sourceIDs,
                                                          version: 1, createdAt: imported.updatedAt, expiresAt: imported.expiresAt))
                    report.episodes += 1
                }
            } else if imported.current(now) {
                report.items += 1
            } else {
                report.historical += 1
            }
        }
        if !archiveBatch.isEmpty { try appendArchive(archiveBatch) }
        report.archived = archiveBatch.count
        for event in legacyPending {
            let result = try journal.append(event, now: now)
            switch result {
            case .accepted: report.pending += 1
            case .duplicate: report.duplicates += 1
            case .refused(let reason): throw QQMemoryReliableError.importWouldLoseData(reason)
            case .storedOnly: break
            }
            if !state.recent.contains(where: { $0.id == event.id }) { state.recent.append(event) }
            state.knownIDs = mergeIDs(state.knownIDs, [event.id])
        }
        state.recent = Array(state.recent.sorted { $0.at < $1.at }.suffix(24))
        state.knownIDs = mergeIDs(state.knownIDs, book.knownIDs)
        state.droppedEvents += book.droppedEvents
        state.refusedEvents += book.refusedEvents ?? 0
        state.legacyImported = true
        try persistState()
        report.droppedEvents = book.droppedEvents
        report.lossless = report.items + report.historical + report.duplicates + report.forgotten >= book.items.count &&
            report.pending + report.duplicates >= legacyPending.count
        return report
    }

    // MARK: - State

    private static func loadState(stateURL: URL, backupURL: URL, scope: String, decoder: JSONDecoder, encoder: JSONEncoder) throws -> State {
        if let data = try? Data(contentsOf: stateURL), let decoded = try? decoder.decode(State.self, from: data), decoded.v == 1 {
            guard decoded.scope == scope else { throw QQMemoryReliableError.scopeMismatch(decoded.scope) }
            return decoded
        }
        if let data = try? Data(contentsOf: backupURL), let decoded = try? decoder.decode(State.self, from: data), decoded.v == 1 {
            guard decoded.scope == scope else { throw QQMemoryReliableError.scopeMismatch(decoded.scope) }
            // Repair the primary from the verified backup so later opens do not repeat recovery.
            if let repaired = try? encoder.encode(decoded) { try? repaired.write(to: stateURL, options: .atomic) }
            return decoded
        }
        if FileManager.default.fileExists(atPath: stateURL.path) || FileManager.default.fileExists(atPath: backupURL.path) {
            throw QQMemoryReliableError.corruptState("state.json 与备份均无法解析")
        }
        return State(scope: scope)
    }

    private func persistState() throws {
        let data: Data
        do { data = try encoder.encode(state) } catch { throw QQMemoryReliableError.invalid("状态编码失败：\(error.localizedDescription)") }
        do {
            try data.write(to: stateURL, options: [.atomic])
            try data.write(to: backupURL, options: [.atomic])
        }
        catch { throw QQMemoryReliableError.invalid("状态写入失败：\(error.localizedDescription)") }
    }

    private func note(_ event: QQMemoryEvent) {
        if !state.knownIDs.contains(event.id) { state.knownIDs.append(event.id); trim(&state.knownIDs, 4096) }
        state.recent.removeAll { $0.id == event.id }
        state.recent.append(event)
        state.recent = Array(state.recent.sorted { $0.at < $1.at }.suffix(24))
    }

    private func recoverFromJournal() throws {
        let pending = journal.recover(knownIDs: Set(state.appliedIDs))
        for entry in pending where !state.knownIDs.contains(entry.event.id) {
            note(entry.event)
        }
        // Crash window: state was applied but the cursor was not advanced. Applied IDs are proof.
        let applied = journal.entries().filter { state.appliedIDs.contains($0.event.id) }
        if let last = applied.last {
            _ = try? journal.commit(ids: Set(applied.map(\.event.id)), through: last.seq)
        }
        if !pending.isEmpty { try persistState() }
    }

    private func trim(now: Date) {
        let nonEpisodes = state.items.filter { $0.kind != "episode" }
        let active = nonEpisodes.filter { $0.current(now) }
            .sorted { ($0.importance, $0.updatedAt) > ($1.importance, $1.updatedAt) }
            .prefix(options.maxActiveItems)
        let historical = nonEpisodes.filter { !$0.current(now) && now.timeIntervalSince($0.invalidatedAt ?? $0.expiresAt) < Double(options.historyRetentionDays * 86400) }
            .sorted { ($0.invalidatedAt ?? $0.expiresAt) > ($1.invalidatedAt ?? $1.expiresAt) }
            .prefix(options.maxHistoricalItems)
        let combined = Array(active) + Array(historical)
        if state.items.count > combined.count { state.trimmedItems += state.items.count - combined.count }
        state.items = combined
        if state.episodes.count > options.maxEpisodes {
            let overflow = state.episodes.count - options.maxEpisodes
            state.episodes.removeFirst(overflow)
            state.trimmedEpisodes += overflow
        }
    }

    private func archiveVersion(of item: QQMemoryItem, at now: Date, reason: String) -> ArchiveRecord {
        let archived = ArchivedItem(id: item.id, subject: item.subject, kind: item.kind, text: item.text,
                                    sourceIDs: item.sourceIDs, validFrom: item.updatedAt,
                                    validTo: item.invalidatedAt ?? now, reason: reason)
        return ArchiveRecord(kind: "itemVersion", item: archived, episode: nil, ids: nil, at: now)
    }

    /// Tombstones are the durable delete authority even if state.json is lost or
    /// restored from an older backup. Re-reading them on open prevents an old
    /// legacy book/archive from resurrecting a forgotten item.
    private func mergeArchiveTombstones() throws {
        guard FileManager.default.fileExists(atPath: archiveURL.path),
              let data = try? Data(contentsOf: archiveURL) else { return }
        let lines = data.split(separator: 0x0A, omittingEmptySubsequences: true)
        var ids: [String] = []
        for line in lines {
            guard let record = try? decoder.decode(ArchiveRecord.self, from: Data(line)),
                  record.kind == "tombstone" else { continue }
            ids.append(contentsOf: record.ids ?? [])
        }
        guard !ids.isEmpty else { return }
        state.forgottenIDs = mergeIDs(state.forgottenIDs ?? [], ids)
        let before = state.items.count + state.episodes.count
        state.items.removeAll { ids.contains($0.id) }
        state.episodes.removeAll { ids.contains($0.id) || !Set(ids).isDisjoint(with: $0.sourceIDs) }
        if state.items.count + state.episodes.count != before { try persistState() }
    }

    private func appendArchive(_ records: [ArchiveRecord]) throws {
        guard !records.isEmpty else { return }
        var data = Data()
        for record in records {
            let encoded = try encoder.encode(record)
            data.append(encoded); data.append(0x0A)
        }
        if !FileManager.default.fileExists(atPath: archiveURL.path) {
            FileManager.default.createFile(atPath: archiveURL.path, contents: nil)
        }
        let handle = try FileHandle(forWritingTo: archiveURL)
        defer { try? handle.close() }
        try handle.seekToEnd()
        try handle.write(contentsOf: data)
        try handle.synchronize()
        archiveRecords += records.count
    }

    private func rewriteArchive(now: Date) throws {
        guard FileManager.default.fileExists(atPath: archiveURL.path) else { return }
        let data = try Data(contentsOf: archiveURL)
        let lines = data.split(separator: 0x0A, omittingEmptySubsequences: true)
        var kept: [Data] = []
        let cutoff = now.addingTimeInterval(-Double(max(options.episodeRetentionDays, options.historyRetentionDays) * 86400))
        for line in lines {
            guard let record = try? decoder.decode(ArchiveRecord.self, from: Data(line)), record.at >= cutoff else { continue }
            kept.append(Data(line))
        }
        if kept.count > 512 { kept = Array(kept.suffix(512)) }
        var output = Data()
        for line in kept { output.append(line); output.append(0x0A) }
        try output.write(to: archiveURL, options: [.atomic])
        archiveRecords = kept.count
    }

    private static func countArchiveRecords(at url: URL, decoder: JSONDecoder) -> Int {
        guard let data = try? Data(contentsOf: url) else { return 0 }
        return data.split(separator: 0x0A, omittingEmptySubsequences: true).count
    }

    private func mergeIDs(_ existing: [String], _ incoming: [String]) -> [String] {
        var result = existing
        for id in incoming where !result.contains(id) { result.append(id) }
        return Array(result.suffix(4096))
    }

    private func trim(_ ids: inout [String], _ cap: Int) { if ids.count > cap { ids = Array(ids.suffix(cap)) } }
}

private extension QQMemoryDelta {
    static func decodeSummaryFallback(_ raw: String) -> String {
        String(QQMemoryRedactor.redact(raw).trimmingCharacters(in: .whitespacesAndNewlines).prefix(400))
    }
}
