import Foundation
import BotCore
#if canImport(Glibc)
import Glibc
#elseif canImport(Darwin)
import Darwin
#endif

/// Persistent idempotency journal. A pending entry is written before model
/// generation so a restart never regenerates an input with an unclear outcome.
public struct UnifiedJournalEntry: Codable, Sendable, Equatable {
    public var key: String
    public var state: String
    public var at: Double
    public var reply: UnifiedReply?
    var memory: UnifiedJournalMemory? = nil
}

enum WeChatMemoryAdminError: Error {
    case unavailable, unknownScope, stale, invalid
}

struct WeChatMemoryEntry: Encodable, Sendable {
    let id: String
    let label: String
    let value: String
    let kind: String
    let source: String
    let updatedAt: Int64
    let expiresAt: Int64

    init?(_ item: QQMemoryItem) {
        guard item.subject == "CONVERSATION", item.keywords.count == 1,
              let key = item.keywords.first,
              let split = item.text.indices.first(where: {
                  item.text[$0] == "：" &&
                      String(item.text[..<$0]).precomposedStringWithCanonicalMapping.lowercased() == key
              }) else { return nil }
        id = item.id
        label = String(item.text[..<split])
        value = String(item.text[item.text.index(after: split)...])
        kind = item.kind
        source = item.sourceIDs.contains { $0.hasPrefix("console:") } ? "operator"
            : item.inferredFromConversation == true ? "conversation" : "manual"
        // The durable archive stores ISO 8601 timestamps at second precision.
        updatedAt = Int64(item.updatedAt.timeIntervalSince1970.rounded(.down))
        expiresAt = Int64(item.expiresAt.timeIntervalSince1970.rounded(.down))
    }
}

struct UnifiedJournalMemory: Codable, Sendable, Equatable {
    let sessionDigest: String
    let incoming: String
    var confirmedAt: Double? = nil
    var groupNote: UnifiedGroupMemoryNote? = nil
}

struct UnifiedGroupMemoryNote: Codable, Sendable, Equatable {
    let label: String
    let value: String
    let kind: String
    var inferred: Bool? = nil

    private static let naturalForms: [(String, NSRegularExpression)] = [
        ("task", #"^(?:我们|大家|群里)(?:已经|刚刚)?(?:约定|约好|决定|定好)(?:了)?[，,:：\s]*(?:把)?([^，,。；;:：]{2,20}?)(?:定在|定于|定为|改到|改为|安排在|在|是|为)([^，,。；;？?！!]{2,116})[。！!]?$"#),
        ("fact", #"^(?:我们|大家|群里)(?:已经|刚刚)?确认(?:了)?[，,:：\s]*([^，,。；;:：]{2,20}?)(?:是在|在|是|为)([^，,。；;？?！!]{2,116})[。！!]?$"#),
        ("preference", #"^(?:我们|大家|群里)(?:平时|一直|通常)(?:都)?(?:对|在)([^，,。；;:：]{2,20}?)(?:更喜欢|更习惯|偏好)([^，,。；;？?！!]{2,116})[。！!]?$"#),
        ("event", #"^(?:我们|大家|群里)(?:刚|刚刚|已经)(?:完成了|参加了|打完了)([^，,。；;:：]{2,20})[，,](?:结果|成绩)(?:是|为)([^，,。；;？?！!]{2,116})[。！!]?$"#)
    ].compactMap { kind, pattern in
        (try? NSRegularExpression(pattern: pattern)).map { (kind, $0) }
    }

    static func parse(_ text: String) -> Self? {
        guard text.count <= 180, !text.contains(where: \.isNewline) else { return nil }
        let fields = text.split(maxSplits: 1, whereSeparator: { $0 == ":" || $0 == "：" })
        guard fields.count == 2 else { return nil }
        let kind: String
        switch fields[0].trimmingCharacters(in: .whitespaces) {
        case "记住": kind = "fact"
        case "记住偏好": kind = "preference"
        case "记住事项": kind = "task"
        default: return nil
        }
        let assignment = fields[1].split(maxSplits: 1, whereSeparator: { $0 == "=" || $0 == "＝" })
        guard assignment.count == 2 else { return nil }
        let label = assignment[0].trimmingCharacters(in: .whitespaces)
        let value = assignment[1].trimmingCharacters(in: .whitespaces)
        return checked(label, value, kind: kind)
    }

    static func infer(from text: String) -> Self? {
        guard text.count <= 180, !text.contains(where: \.isNewline) else { return nil }
        let lower = text.lowercased()
        guard !["？", "?", "“", "”", "如果", "可能", "也许", "听说", "据说", "待定",
                "暂定", "不确定", "未确定", "尚未", "还没", "转发", "朋友圈",
                "qq动态", "qq空间", "qzone"].contains(where: { lower.contains($0) }),
              !QQMemoryRedactor.containsSensitive(text) else { return nil }
        let full = NSRange(text.startIndex..<text.endIndex, in: text)
        for (kind, pattern) in naturalForms {
            guard let match = pattern.firstMatch(in: text, range: full),
                  let topic = Range(match.range(at: 1), in: text),
                  let detail = Range(match.range(at: 2), in: text) else { continue }
            let label = text[topic].trimmingCharacters(in: .whitespaces)
            let value = text[detail].trimmingCharacters(in: .whitespaces)
            guard !label.hasSuffix("不"), !label.hasSuffix("没"),
                  var note = checked(label, value, kind: kind) else { return nil }
            note.inferred = true
            return note
        }
        return nil
    }

    private static func checked(_ label: String, _ value: String, kind: String) -> Self? {
        guard (1...20).contains(label.count), (1...140).contains(value.count),
              !label.lowercased().contains("cookie"),
              !QQMemoryRedactor.containsSensitive(label + "：" + value) else { return nil }
        return Self(label: label, value: value, kind: kind)
    }
}

private struct JournalFile: Codable {
    var version: Int
    var entries: [UnifiedJournalEntry]
}

private struct JournalLoadResult: Sendable {
    var entries: [String: UnifiedJournalEntry]
    var storageUsable: Bool
}

private enum JournalWriteError: Error {
    case operationFailed
}

public actor UnifiedIdempotencyJournal {
    typealias Writer = @Sendable (URL, Data) throws -> Void

    private let fileURL: URL?
    nonisolated var groupPersonasURL: URL? {
        fileURL?.deletingLastPathComponent().appendingPathComponent(WeChatGroupPersonas.filename)
    }
    private let capacity: Int
    private let writer: Writer
    private var entries: [String: UnifiedJournalEntry] = [:]
    private var memoryDrafts: [String: UnifiedJournalMemory] = [:]
    private var archivedNoteReceipts: Set<UUID> = []
    private var storageUsable = true

    /// `nil` selects an in-memory journal for fixtures. A file-backed journal
    /// loads v1 data and fails closed if an existing file cannot be read or decoded.
    public init(fileURL: URL?, capacity: Int = 512) {
        self.fileURL = fileURL
        self.capacity = capacity
        self.writer = { url, data in try Self.writeAtomically(data, to: url) }
        let loaded = Self.loadExistingFile(at: fileURL, capacity: capacity)
        self.entries = loaded.entries
        self.storageUsable = loaded.storageUsable
    }

    /// Internal writer seam keeps disk-failure behavior deterministic in tests.
    init(fileURL: URL?, capacity: Int, writer: @escaping Writer) {
        self.fileURL = fileURL
        self.capacity = capacity
        self.writer = writer
        let loaded = Self.loadExistingFile(at: fileURL, capacity: capacity)
        self.entries = loaded.entries
        self.storageUsable = loaded.storageUsable
    }

    /// Returns nil only for a new key. A corrupt or unreadable persistent file
    /// still permits reads of entries already loaded, but blocks new work.
    public func lookup(_ key: String) -> UnifiedJournalEntry? { entries[key] }

    /// This is called before model invocation. Persistence failure is the same
    /// fail-closed result as an existing key; no candidate entry enters memory.
    @discardableResult public func reserve(_ key: String, at now: Double) -> Bool {
        guard storageUsable, entries[key] == nil else { return false }
        var candidate = entries
        candidate[key] = UnifiedJournalEntry(key: key, state: "pending", at: now, reply: nil)
        guard persist(candidate) else { return false }
        entries = candidate
        return true
    }

    /// Persist completion before making it visible in memory. On failure the
    /// prior pending entry remains, conservatively preventing another attempt.
    public func complete(_ key: String, reply: UnifiedReply, ok: Bool, at now: Double) {
        _ = complete(key, reply: reply, ok: ok, at: now, memory: nil)
    }

    func complete(_ key: String, reply: UnifiedReply, ok: Bool, at now: Double,
                  memory: UnifiedJournalMemory?) -> Bool {
        guard storageUsable else { return false }
        let pendingMemory = fileURL != nil && ok ? memory : nil
        var recordedReply = reply
        if pendingMemory == nil { recordedReply.memoryReceipt = nil }
        var candidate = entries
        candidate[key] = UnifiedJournalEntry(
            key: key, state: ok ? "done" : "failed", at: now, reply: recordedReply)
        Self.trim(&candidate, capacity: capacity)
        guard persist(candidate) else { return false }
        entries = candidate
        if archivedNoteReceipts.count > capacity {
            archivedNoteReceipts.formIntersection(candidate.values.compactMap { $0.reply?.memoryReceipt })
        }
        // The inbound body stays in process memory unless the UI proves the send.
        memoryDrafts = memoryDrafts.filter { key, _ in
            guard let entry = candidate[key] else { return false }
            return (0...600).contains(now - entry.at)
        }
        memoryDrafts[key] = pendingMemory
        return memory == nil || pendingMemory != nil
    }

    func confirmMemory(_ receipt: UUID, at now: Double) -> Bool {
        guard storageUsable, fileURL != nil,
              var entry = entries.values.first(where: {
                  $0.state == "done" && $0.reply?.memoryReceipt == receipt
              }) else { return false }
        if let memory = entry.memory, memory.confirmedAt != nil {
            return saveGroupNote(memory, receipt: receipt)
        }
        guard (0...600).contains(now - entry.at), var draft = memoryDrafts[entry.key] else { return false }
        draft.confirmedAt = now
        entry.memory = draft
        var candidate = entries
        candidate[entry.key] = entry
        guard persist(candidate) else { return false }
        entries = candidate
        memoryDrafts.removeValue(forKey: entry.key)
        return saveGroupNote(draft, receipt: receipt)
    }

    private func saveGroupNote(_ memory: UnifiedJournalMemory, receipt: UUID) -> Bool {
        guard let note = memory.groupNote, let confirmedAt = memory.confirmedAt else { return true }
        if archivedNoteReceipts.contains(receipt) { return true }
        do {
            guard let archive = try groupArchive(memory.sessionDigest, create: true) else { return false }
            try archive.recordConfirmedGroupNote(id: receipt.uuidString, label: note.label,
                                                  value: note.value, kind: note.kind,
                                                  inferred: note.inferred == true,
                                                  at: Date(timeIntervalSince1970: confirmedAt))
            archivedNoteReceipts.insert(receipt)
            return true
        } catch {
            print("unified_group_memory_write_failed")
            return false
        }
    }

    func groupMemoryContext(for scope: String, query: String) -> String {
        guard storageUsable else { return "" }
        // Finish notes whose delivery proof persisted just before a process or storage failure.
        let waiting = entries.values.compactMap { entry -> (Double, UUID, UnifiedJournalMemory)? in
            guard entry.state == "done", let memory = entry.memory, memory.sessionDigest == scope,
                  let at = memory.confirmedAt, memory.groupNote != nil,
                  let receipt = entry.reply?.memoryReceipt, !archivedNoteReceipts.contains(receipt) else { return nil }
            return (at, receipt, memory)
        }.sorted { $0.0 < $1.0 }
        for (_, receipt, memory) in waiting {
            if !saveGroupNote(memory, receipt: receipt) { break }
        }
        do {
            let archive = try groupArchive(scope, create: false)
            return archive?.context(query, subject: "CONVERSATION", characters: 480,
                                    includeEpisodes: false) ?? ""
        } catch {
            print("unified_group_memory_read_failed")
            return ""
        }
    }

    private func groupArchive(_ scope: String, create: Bool) throws -> QQMemoryReliableStore? {
        guard let fileURL, scope.utf8.count == 64,
              scope.utf8.allSatisfy({ (48...57).contains($0) || (97...102).contains($0) }) else { return nil }
        let root = fileURL.deletingLastPathComponent().appendingPathComponent("wechat-group-memory")
        let directory = root.appendingPathComponent(scope)
        let manager = FileManager.default
        if !create && !manager.fileExists(atPath: directory.path) { return nil }
        for folder in [root, directory] {
            if create && !manager.fileExists(atPath: folder.path) {
                try manager.createDirectory(at: folder, withIntermediateDirectories: false,
                                            attributes: [.posixPermissions: 0o700])
            }
            let attributes = try manager.attributesOfItem(atPath: folder.path)
            guard attributes[.type] as? FileAttributeType == .typeDirectory,
                  (attributes[.posixPermissions] as? NSNumber)?.intValue == 0o700 else {
                throw CocoaError(.fileReadNoPermission)
            }
        }
        return try QQMemoryReliableStore(directory: directory, scope: scope)
    }

    func groupMemoryEntries(for scope: String) throws -> [WeChatMemoryEntry] {
        guard storageUsable, fileURL != nil else { throw WeChatMemoryAdminError.unavailable }
        guard let archive = try groupArchive(scope, create: false) else { return [] }
        let now = Date()
        return archive.items.filter { $0.current(now) }
            .sorted { ($0.updatedAt, $0.id) > ($1.updatedAt, $1.id) }
            .compactMap(WeChatMemoryEntry.init)
    }

    func correctGroupMemory(for scope: String, id: UUID, value: String) throws -> WeChatMemoryEntry {
        guard storageUsable, fileURL != nil else { throw WeChatMemoryAdminError.unavailable }
        let correction = value.trimmingCharacters(in: .whitespaces)
        guard (1...140).contains(correction.count), correction.utf8.count <= 960,
              !correction.unicodeScalars.contains(where: CharacterSet.controlCharacters.contains),
              !QQMemoryRedactor.containsSensitive(correction) else { throw WeChatMemoryAdminError.invalid }
        let now = Date()
        guard let archive = try groupArchive(scope, create: false),
              let item = archive.items.first(where: { UUID(uuidString: $0.id) == id && $0.current(now) }),
              let entry = WeChatMemoryEntry(item) else { throw WeChatMemoryAdminError.stale }
        guard !QQMemoryRedactor.containsSensitive(entry.label + "：" + correction) else { throw WeChatMemoryAdminError.invalid }
        let eventID = "console:" + UUID().uuidString
        try archive.recordConfirmedGroupNote(id: eventID, label: entry.label, value: correction, kind: entry.kind,
                                             at: now, retainingExpiry: item.expiresAt)
        if let updated = archive.items.first(where: { $0.current(now) && $0.sourceIDs.contains(eventID) }),
           let result = WeChatMemoryEntry(updated) { return result }
        guard correction == entry.value else { throw WeChatMemoryAdminError.unavailable }
        return entry
    }

    func memoryHistory(for sessionDigest: String, limit: Int) -> [ChatTurn] {
        guard storageUsable, fileURL != nil, limit > 0 else { return [] }
        let confirmed = entries.values.filter {
            $0.state == "done" && $0.memory?.sessionDigest == sessionDigest && $0.memory?.confirmedAt != nil
        }.sorted { ($0.at, $0.key) < ($1.at, $1.key) }
        return confirmed.suffix(min(limit, 8)).compactMap { entry in
            guard let memory = entry.memory, let reply = entry.reply?.reply else { return nil }
            return ChatTurn(incoming: memory.incoming, outgoing: String(reply.prefix(1200)))
        }
    }

    public func count() -> Int { entries.count }

    nonisolated private static func loadExistingFile(
        at fileURL: URL?, capacity: Int
    ) -> JournalLoadResult {
        guard let fileURL else {
            return JournalLoadResult(entries: [:], storageUsable: true)
        }
        var metadata = stat()
        let lookupResult = fileURL.path.withCString { lstat($0, &metadata) }
        if lookupResult != 0 {
            if errno == ENOENT {
                return JournalLoadResult(entries: [:], storageUsable: true)
            }
            print("unified_idempotency_load_failed reason=unreadable_path")
            return JournalLoadResult(entries: [:], storageUsable: false)
        }
        guard metadata.st_mode & mode_t(S_IFMT) == mode_t(S_IFREG) else {
            print("unified_idempotency_load_failed reason=invalid_file_type")
            return JournalLoadResult(entries: [:], storageUsable: false)
        }
        do {
            let data = try Data(contentsOf: fileURL)
            let decoded = try JSONDecoder().decode(JournalFile.self, from: data)
            guard decoded.version == 1 else {
                print("unified_idempotency_load_failed reason=unsupported_version")
                return JournalLoadResult(entries: [:], storageUsable: false)
            }
            guard decoded.entries.allSatisfy({
                !$0.key.isEmpty && ["pending", "done", "failed"].contains($0.state)
            }) else {
                print("unified_idempotency_load_failed reason=invalid_entry")
                return JournalLoadResult(entries: [:], storageUsable: false)
            }
            var loadedEntries: [String: UnifiedJournalEntry] = [:]
            for entry in decoded.entries { loadedEntries[entry.key] = entry }
            trim(&loadedEntries, capacity: capacity)
            return JournalLoadResult(entries: loadedEntries, storageUsable: true)
        } catch {
            // Do not log paths, content, or platform error descriptions.
            print("unified_idempotency_load_failed reason=unreadable_or_corrupt")
            return JournalLoadResult(entries: [:], storageUsable: false)
        }
    }

    @discardableResult
    private func persist(_ candidate: [String: UnifiedJournalEntry]) -> Bool {
        guard storageUsable else { return false }
        guard let fileURL else { return true }
        do {
            let file = JournalFile(version: 1, entries: Array(candidate.values))
            let data = try JSONEncoder().encode(file)
            try writer(fileURL, data)
            return true
        } catch {
            // Keep diagnostics discrete; never include file paths or entry data.
            print("unified_idempotency_persist_failed reason=write_failed")
            return false
        }
    }

    nonisolated private static func trim(
        _ store: inout [String: UnifiedJournalEntry], capacity: Int
    ) {
        guard store.count > capacity else { return }
        let sorted = store.values.sorted { $0.at < $1.at }
        for entry in sorted.prefix(store.count - capacity) {
            store.removeValue(forKey: entry.key)
        }
    }

    /// Same-directory POSIX replacement: the old journal remains present until
    /// `rename(2)` atomically installs the fully written, mode-0600 new file.
    nonisolated static func writeAtomically(_ data: Data, to fileURL: URL) throws {
        let directory = fileURL.deletingLastPathComponent()
        try FileManager.default.createDirectory(at: directory,
                                                withIntermediateDirectories: true)
        var template = Array((fileURL.path + ".tmp.XXXXXX").utf8CString)
        let descriptor = template.withUnsafeMutableBufferPointer { buffer in
            mkstemp(buffer.baseAddress!)
        }
        guard descriptor >= 0 else { throw JournalWriteError.operationFailed }

        let temporaryPath = template.withUnsafeBufferPointer { String(cString: $0.baseAddress!) }
        var descriptorOpen = true
        var renamed = false
        defer {
            if descriptorOpen { _ = close(descriptor) }
            if !renamed { temporaryPath.withCString { _ = unlink($0) } }
        }

        guard fchmod(descriptor, mode_t(0o600)) == 0 else {
            throw JournalWriteError.operationFailed
        }
        let writeResult = data.withUnsafeBytes { raw -> Bool in
            guard let base = raw.baseAddress else { return raw.isEmpty }
            var offset = 0
            while offset < raw.count {
                let written = write(descriptor, base.advanced(by: offset), raw.count - offset)
                if written < 0 {
                    if errno == EINTR { continue }
                    return false
                }
                if written == 0 { return false }
                offset += written
            }
            return true
        }
        guard writeResult, fsync(descriptor) == 0 else {
            throw JournalWriteError.operationFailed
        }
        guard close(descriptor) == 0 else {
            descriptorOpen = false
            throw JournalWriteError.operationFailed
        }
        descriptorOpen = false

        let renameResult = temporaryPath.withCString { source in
            fileURL.path.withCString { destination in rename(source, destination) }
        }
        guard renameResult == 0 else { throw JournalWriteError.operationFailed }
        renamed = true

        let directoryDescriptor = directory.path.withCString { open($0, O_RDONLY) }
        guard directoryDescriptor >= 0 else { throw JournalWriteError.operationFailed }
        defer { _ = close(directoryDescriptor) }
        guard fsync(directoryDescriptor) == 0 else {
            throw JournalWriteError.operationFailed
        }
    }
}
