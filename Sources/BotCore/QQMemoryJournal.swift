import Foundation
#if canImport(CryptoKit)
import CryptoKit
#else
import Crypto
#endif
#if canImport(Glibc)
import Glibc
#elseif canImport(Darwin)
import Darwin
#endif

public struct QQMemoryJournalOptions: Codable, Equatable, Sendable {
    /// Bounded queue: at the cap new captures are refused; queued entries are never evicted.
    public var maxPendingEvents = 1024
    public var maxPendingBytes = 4 * 1024 * 1024
    public var segmentBytes = 256 * 1024
    public var highWatermarkRatio = 0.75
    public var committedSegmentRetention = 4
    public var committedRetentionDays = 7
    public init() {}
    public func validate() throws {
        guard (8...8192).contains(maxPendingEvents), (64 * 1024...64 * 1024 * 1024).contains(maxPendingBytes),
              (1024...8 * 1024 * 1024).contains(segmentBytes),
              highWatermarkRatio > 0, highWatermarkRatio < 1,
              (0...64).contains(committedSegmentRetention), (0...365).contains(committedRetentionDays) else {
            throw QQMemoryJournalError.invalid("记忆日志参数超出安全范围")
        }
    }
}

public enum QQMemoryJournalError: LocalizedError, Equatable {
    case locked(String)
    case invalid(String)
    case io(String)
    public var errorDescription: String? {
        switch self {
        case .locked(let p): return "记忆日志已被另一个进程占用：\(p)"
        case .invalid(let m): return "记忆日志参数无效：\(m)"
        case .io(let m): return "记忆日志读写失败：\(m)"
        }
    }
}

public struct QQMemoryJournalEntry: Codable, Equatable, Sendable, Identifiable {
    public let seq: UInt64
    public let event: QQMemoryEvent
    public let receivedAt: Date
    public var id: String { event.id }
    public init(seq: UInt64, event: QQMemoryEvent, receivedAt: Date) {
        self.seq = seq; self.event = event; self.receivedAt = receivedAt
    }
}

public enum QQMemoryJournalAppend: Equatable, Sendable {
    case accepted(QQMemoryJournalEntry)
    /// The event was kept in the short recent window but is not a queueable memory candidate.
    case storedOnly
    case duplicate
    case refused(String)
    public var entry: QQMemoryJournalEntry? {
        if case .accepted(let entry) = self { return entry }
        return nil
    }
}

public struct QQMemoryJournalStats: Codable, Equatable, Sendable {
    public var pendingCount = 0
    public var pendingBytes = 0
    public var committedThrough: UInt64 = 0
    public var refusedEvents = 0
    public var corruptRecords = 0
    public var segmentCount = 0
    public var highWatermark = false
    public var oldestPendingAt: Date?
    public var prunedSegments = 0
}

/// Append-only, checksummed, fsync-per-record write-ahead log.
/// A commit cursor is written only after the caller has durably applied the batch.
public final class QQMemoryJournal {
    private struct Record: Codable {
        var v = 1
        var seq: UInt64
        var event: QQMemoryEvent
        var receivedAt: Date
        var checksum: String
    }

    private struct Cursor: Codable {
        var v = 1
        var committedThrough: UInt64 = 0
        var committedIDs: [String] = []
        var updatedAt: Date = Date()
    }

    private struct Segment {
        var index: Int
        var url: URL
        var maxSeq: UInt64
        var bytes: Int
        var createdAt: Date
    }

    public let directoryURL: URL
    public let options: QQMemoryJournalOptions
    private let lockURL: URL
    private let cursorURL: URL
    private let cursorBackupURL: URL
    private let corruptURL: URL
    private var lockFD: Int32 = -1
    private var cursor = Cursor()
    private var segments: [Segment] = []
    private var entries: [QQMemoryJournalEntry] = []
    private var seen: Set<String> = []
    private var seenOrder: [String] = []
    private var seqCounter: UInt64 = 0
    private var currentIndex = 1
    private var currentHandle: FileHandle?
    private var currentBytes = 0
    private var refusedEvents = 0
    private var corruptRecords = 0
    private var prunedSegments = 0
    private let encoder: JSONEncoder
    private let decoder: JSONDecoder

    public init(directory: URL, options: QQMemoryJournalOptions = QQMemoryJournalOptions()) throws {
        try options.validate()
        self.directoryURL = directory
        self.options = options
        self.lockURL = directory.appendingPathComponent(".lock")
        self.cursorURL = directory.appendingPathComponent("cursor.json")
        self.cursorBackupURL = directory.appendingPathComponent("cursor.json.bak")
        self.corruptURL = directory.appendingPathComponent("corrupt.log")
        self.encoder = JSONEncoder(); encoder.dateEncodingStrategy = .iso8601; encoder.outputFormatting = [.sortedKeys]
        self.decoder = JSONDecoder(); decoder.dateDecodingStrategy = .iso8601
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        try acquireLock()
        do {
            try loadCursor()
            try loadSegments()
            try openCurrentSegment()
        } catch {
            releaseLock()
            throw error
        }
    }

    deinit { close() }

    public func segmentURLs() -> [URL] { segments.sorted { $0.index < $1.index }.map(\.url) }

    public func append(_ event: QQMemoryEvent, now: Date = Date()) throws -> QQMemoryJournalAppend {
        guard !event.text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else {
            return .refused("空记忆片段")
        }
        if seen.contains(event.id) || cursor.committedIDs.contains(event.id) {
            return .duplicate
        }
        guard entries.count < options.maxPendingEvents else {
            refusedEvents += 1
            return .refused("记忆队列达到条目上限 \(options.maxPendingEvents)，已保留原有片段")
        }
        let record = Record(seq: seqCounter + 1, event: event, receivedAt: now, checksum: "")
        let line = try encodeLine(recordWithChecksum(record))
        guard entries.reduce(0, { $0 + $1.event.text.utf8.count }) + event.text.utf8.count <= options.maxPendingBytes else {
            refusedEvents += 1
            return .refused("记忆队列达到字节上限 \(options.maxPendingBytes)，已保留原有片段")
        }
        guard let handle = currentHandle else { throw QQMemoryJournalError.io("日志句柄未打开") }
        do {
            try handle.write(contentsOf: Data(line.utf8))
            try handle.synchronize()
        } catch {
            throw QQMemoryJournalError.io("追加记忆日志失败：\(error.localizedDescription)")
        }
        seqCounter = record.seq
        currentBytes += line.utf8.count
        seen.insert(event.id); seenOrder.append(event.id); trimSeen()
        let entry = QQMemoryJournalEntry(seq: record.seq, event: event, receivedAt: now)
        entries.append(entry); entries.sort { $0.seq < $1.seq }
        if currentBytes >= options.segmentBytes { try rotate() }
        return .accepted(entry)
    }

    public func entries(knownIDs: Set<String> = [], limit: Int = Int.max) -> [QQMemoryJournalEntry] {
        var result: [QQMemoryJournalEntry] = []
        for entry in entries where !knownIDs.contains(entry.event.id) {
            if result.count >= limit { break }
            result.append(entry)
        }
        return result
    }

    public func batch(knownIDs: Set<String> = [], limit: Int = 32, maxCharacters: Int = 7500) -> [QQMemoryJournalEntry] {
        var result: [QQMemoryJournalEntry] = [], size = 0
        for entry in entries(knownIDs: knownIDs) {
            if result.count >= limit { break }
            let cost = entry.event.text.count + entry.event.id.count + 96
            if !result.isEmpty, size + cost > maxCharacters { break }
            result.append(entry); size += cost
        }
        return result
    }

    @discardableResult
    public func commit(ids: Set<String>, through: UInt64? = nil, now: Date = Date()) throws -> Int {
        guard !ids.isEmpty else { return 0 }
        let present = entries.filter { ids.contains($0.event.id) }
        guard present.count == ids.count else { throw QQMemoryJournalError.invalid("提交了不存在的记忆片段") }
        let target = through ?? present.map(\.seq).max()!
        guard target <= seqCounter, target >= cursor.committedThrough else { throw QQMemoryJournalError.invalid("提交游标倒退") }
        cursor.committedThrough = target
        cursor.committedIDs.append(contentsOf: ids)
        cursor.committedIDs = Array(cursor.committedIDs.suffix(2048))
        cursor.updatedAt = now
        try persistCursor()
        entries.removeAll { $0.seq <= target }
        try vacuum(now: now)
        return present.count
    }

    /// Startup recovery: entries still above the durable cursor and not already in the caller's state.
    public func recover(knownIDs: Set<String>) -> [QQMemoryJournalEntry] {
        entries(knownIDs: knownIDs)
    }

    public func stats(now: Date = Date()) -> QQMemoryJournalStats {
        var stats = QQMemoryJournalStats()
        stats.pendingCount = entries.count
        stats.pendingBytes = entries.reduce(0) { $0 + $1.event.text.utf8.count + 96 }
        stats.committedThrough = cursor.committedThrough
        stats.refusedEvents = refusedEvents
        stats.corruptRecords = corruptRecords
        stats.segmentCount = segments.count
        stats.oldestPendingAt = entries.first?.event.at
        stats.prunedSegments = prunedSegments
        let countLimit = Double(options.maxPendingEvents) * options.highWatermarkRatio
        let byteLimit = Double(options.maxPendingBytes) * options.highWatermarkRatio
        stats.highWatermark = Double(entries.count) >= countLimit || Double(stats.pendingBytes) >= byteLimit
        return stats
    }

    @discardableResult
    public func vacuum(now: Date = Date()) throws -> Int {
        let committed = segments.filter { $0.maxSeq <= cursor.committedThrough }.sorted { $0.index > $1.index }
        var removed = 0
        for (offset, segment) in committed.enumerated() where offset >= options.committedSegmentRetention {
            guard segment.index != currentIndex else { continue }
            try? FileManager.default.removeItem(at: segment.url)
            segments.removeAll { $0.index == segment.index }
            removed += 1
            prunedSegments += 1
        }
        return removed
    }

    public func erase() throws {
        closeCurrentHandle()
        let names = (try? FileManager.default.contentsOfDirectory(atPath: directoryURL.path)) ?? []
        for name in names where name != ".lock" {
            try? FileManager.default.removeItem(at: directoryURL.appendingPathComponent(name))
        }
        cursor = Cursor(); entries = []; seen = []; seenOrder = []; seqCounter = 0
        segments = []; currentIndex = 1; currentBytes = 0; refusedEvents = 0; corruptRecords = 0; prunedSegments = 0
        try openCurrentSegment()
    }

    public func close() {
        closeCurrentHandle()
        releaseLock()
    }

    // MARK: - Persistence

    private func encodeLine(_ record: Record) throws -> String {
        let data = try encoder.encode(record)
        return String(decoding: data, as: UTF8.self) + "\n"
    }

    private func recordWithChecksum(_ record: Record) -> Record {
        var copy = record
        copy.checksum = checksum(seq: record.seq, event: record.event)
        return copy
    }

    private func checksum(seq: UInt64, event: QQMemoryEvent) -> String {
        let material = "\(seq)|\(event.id)|\(event.subject)|\(Int(event.at.timeIntervalSince1970))|\(event.text)"
        let digest = SHA256.hash(data: Data(material.utf8))
        return digest.map { String(format: "%02x", $0) }.joined()
    }

    private func acquireLock() throws {
        let fd = open(lockURL.path, O_CREAT | O_RDWR, 0o600)
        guard fd >= 0 else { throw QQMemoryJournalError.io("无法打开记忆日志锁") }
        if flock(fd, LOCK_EX | LOCK_NB) != 0 {
            systemClose(fd)
            throw QQMemoryJournalError.locked(directoryURL.path)
        }
        lockFD = fd
    }

    private func releaseLock() {
        if lockFD >= 0 { flock(lockFD, LOCK_UN); systemClose(lockFD); lockFD = -1 }
    }

    private func systemClose(_ fd: Int32) {
        #if canImport(Glibc)
        Glibc.close(fd)
        #elseif canImport(Darwin)
        Darwin.close(fd)
        #endif
    }

    private func loadCursor() throws {
        if let data = try? Data(contentsOf: cursorURL), let decoded = try? decoder.decode(Cursor.self, from: data), decoded.v == 1 {
            cursor = decoded
            return
        }
        if let data = try? Data(contentsOf: cursorBackupURL), let decoded = try? decoder.decode(Cursor.self, from: data), decoded.v == 1 {
            cursor = decoded
            if let repaired = try? encoder.encode(decoded) { try? repaired.write(to: cursorURL, options: .atomic) }
            return
        }
        guard !FileManager.default.fileExists(atPath: cursorURL.path) else {
            throw QQMemoryJournalError.invalid("提交游标损坏且无可用备份")
        }
    }

    private func persistCursor() throws {
        let data = try encoder.encode(cursor)
        do {
            try data.write(to: cursorURL, options: [.atomic])
            try data.write(to: cursorBackupURL, options: [.atomic])
        } catch {
            throw QQMemoryJournalError.io("写入提交游标失败：\(error.localizedDescription)")
        }
    }

    private func loadSegments() throws {
        let names = try FileManager.default.contentsOfDirectory(atPath: directoryURL.path)
        let files = names.filter { $0.hasPrefix("seg-") && $0.hasSuffix(".log") }.sorted()
        for name in files {
            let url = directoryURL.appendingPathComponent(name)
            let index = Int(name.dropFirst(4).dropLast(4)) ?? 0
            let (records, corrupt, bytes) = try readSegment(url)
            if !corrupt.isEmpty { try quarantine(corrupt, in: url) }
            var maxSeq: UInt64 = 0
            for record in records {
                maxSeq = max(maxSeq, record.seq)
                seqCounter = max(seqCounter, record.seq)
                if seen.insert(record.event.id).inserted {
                    seenOrder.append(record.event.id); trimSeen()
                }
                if record.seq > cursor.committedThrough && !cursor.committedIDs.contains(record.event.id) {
                    entries.append(QQMemoryJournalEntry(seq: record.seq, event: record.event, receivedAt: record.receivedAt))
                }
            }
            let created = (try? FileManager.default.attributesOfItem(atPath: url.path)[.creationDate] as? Date) ?? Date.distantPast
            let size = (try? FileManager.default.attributesOfItem(atPath: url.path)[.size] as? Int) ?? bytes
            segments.append(Segment(index: index, url: url, maxSeq: maxSeq, bytes: size, createdAt: created))
        }
        entries.sort { $0.seq < $1.seq }
        if let maxIndex = segments.map(\.index).max() { currentIndex = maxIndex }
    }

    /// Returns decoded records, raw corrupt lines (quarantined), and byte size.
    /// A trailing partial line (power loss during append) is truncated, not counted as corruption.
    private func readSegment(_ url: URL) throws -> ([Record], [Data], Int) {
        var data = try Data(contentsOf: url)
        if let last = data.last, last != 0x0A {
            let cut = data.lastIndex(of: 0x0A).map { data.distance(from: data.startIndex, to: $0) + 1 } ?? 0
            let prefix = data.prefix(cut)
            let handle = try FileHandle(forWritingTo: url)
            try handle.truncate(atOffset: UInt64(cut))
            try handle.synchronize()
            try handle.close()
            data = Data(prefix)
        }
        let lines = data.split(separator: 0x0A, omittingEmptySubsequences: true)
        var records: [Record] = [], corrupt: [Data] = []
        for line in lines {
            let raw = Data(line)
            if let record = try? decoder.decode(Record.self, from: raw), record.v == 1,
               record.checksum == checksum(seq: record.seq, event: record.event) {
                records.append(record)
            } else {
                corrupt.append(raw)
            }
        }
        if !corrupt.isEmpty { rewriteSegment(url, with: records) }
        return (records, corrupt, data.count)
    }

    private func rewriteSegment(_ url: URL, with records: [Record]) {
        guard let handle = try? FileHandle(forWritingTo: url) else { return }
        defer { try? handle.close() }
        try? handle.truncate(atOffset: 0)
        guard let data = try? records.map { try encodeLine(recordWithChecksum($0)) }.joined().data(using: .utf8) else { return }
        try? handle.write(contentsOf: data)
        try? handle.synchronize()
    }

    private func quarantine(_ lines: [Data], in url: URL) throws {
        corruptRecords += lines.count
        guard let handle = try? FileHandle(forWritingTo: corruptURL) else { return }
        defer { try? handle.close() }
        for line in lines {
            try? handle.write(contentsOf: line)
            try? handle.write(contentsOf: Data("\n".utf8))
        }
    }

    private func openCurrentSegment() throws {
        let url = segmentURL(index: currentIndex)
        if !FileManager.default.fileExists(atPath: url.path) {
            FileManager.default.createFile(atPath: url.path, contents: nil)
        }
        let handle = try FileHandle(forWritingTo: url)
        try handle.seekToEnd()
        currentHandle = handle
        currentBytes = (try? FileManager.default.attributesOfItem(atPath: url.path)[.size] as? Int) ?? 0
        if !segments.contains(where: { $0.index == currentIndex }) {
            segments.append(Segment(index: currentIndex, url: url, maxSeq: 0, bytes: currentBytes, createdAt: Date()))
        }
    }

    private func rotate() throws {
        closeCurrentHandle()
        currentIndex += 1
        currentBytes = 0
        try openCurrentSegment()
    }

    private func closeCurrentHandle() {
        if let handle = currentHandle { try? handle.synchronize(); try? handle.close() }
        currentHandle = nil
    }

    private func segmentURL(index: Int) -> URL {
        directoryURL.appendingPathComponent(String(format: "seg-%06d.log", index))
    }

    private func trimSeen() {
        if seenOrder.count > 4096 {
            let overflow = seenOrder.count - 4096
            for id in seenOrder.prefix(overflow) { seen.remove(id) }
            seenOrder.removeFirst(overflow)
        }
    }
}
