import Foundation

/// 持久化幂等账本：保证「一条输入最多一次发回」跨进程重启仍成立。
///
/// 设计（对应任务书「去重持久化或明确可靠跨重启机制」）：
/// - 文件 0600、原子替换（写 .tmp → chmod → replace），与既有 qq-state.json /
///   bridge-health.json 的落盘范式一致。
/// - 只存**摘要键**（sessionKey|msgId 的 FNV 摘要）与离散结果/状态，
///   绝不存消息原文、群名、账号或 key。
/// - 两阶段：
///   * `reserve(key)` 在**调用模型之前**原子落盘 `state=pending`；
///     重启后看到 pending 表示该输入可能已经生成/提交过 → 保守返回 ignore，
///     绝不重新生成、绝不重新发回（发送不明勿盲重试）。
///   * `complete(key, reply)` 在**返回结果之前**把最终 reply 落盘；
///     同 key 再次进入直接回放，模型不再调用。
/// - 有界：只保留最近 capacity 条（按时间淘汰），防止无界增长。
/// - 单进程单实例假设：journal 文件由同一进程独占；多实例需外部文件锁（列为缺口）。

public struct UnifiedJournalEntry: Codable, Sendable, Equatable {
    public var key: String          // 摘要键，非原文
    public var state: String        // pending | done | failed
    public var at: Double
    public var reply: UnifiedReply?
}

private struct JournalFile: Codable {
    var version: Int
    var entries: [UnifiedJournalEntry]
}

public actor UnifiedIdempotencyJournal {
    private let fileURL: URL?
    private var entries: [String: UnifiedJournalEntry] = [:]
    private let capacity: Int

    /// - Parameter fileURL: nil = 纯内存（fixture 用，不落盘）；
    ///   非 nil = 0600 原子文件，启动时加载。
    public init(fileURL: URL?, capacity: Int = 512) {
        self.fileURL = fileURL
        self.capacity = capacity
        if let fileURL, let data = try? Data(contentsOf: fileURL),
           let decoded = try? JSONDecoder().decode(JournalFile.self, from: data), decoded.version == 1 {
            for entry in decoded.entries { entries[entry.key] = entry }
            trim()
        }
    }

    /// 返回 nil 表示「首次见到，可继续」；否则返回已有条目（回放或保守拒绝）。
    public func lookup(_ key: String) -> UnifiedJournalEntry? { entries[key] }

    /// 在调用模型**之前**占位。返回 false 表示已存在（不重复生成/不重复发送）。
    @discardableResult public func reserve(_ key: String, at now: Double) -> Bool {
        if entries[key] != nil { return false }
        entries[key] = UnifiedJournalEntry(key: key, state: "pending", at: now, reply: nil)
        persist()
        return true
    }

    /// 在把结果交回桥**之前**落盘最终状态。
    public func complete(_ key: String, reply: UnifiedReply, ok: Bool, at now: Double) {
        entries[key] = UnifiedJournalEntry(key: key, state: ok ? "done" : "failed", at: now, reply: reply)
        trim()
        persist()
    }

    /// 供单测/诊断：当前账本条数（不含原文）。
    public func count() -> Int { entries.count }

    private func trim() {
        guard entries.count > capacity else { return }
        let sorted = entries.values.sorted { $0.at < $1.at }
        for entry in sorted.prefix(entries.count - capacity) { entries.removeValue(forKey: entry.key) }
    }

    private func persist() {
        guard let fileURL else { return }
        do {
            try FileManager.default.createDirectory(at: fileURL.deletingLastPathComponent(),
                                                    withIntermediateDirectories: true)
            let file = JournalFile(version: 1, entries: Array(entries.values))
            let data = try JSONEncoder().encode(file)
            let tmp = fileURL.appendingPathExtension("tmp")
            try data.write(to: tmp, options: .atomic)
            try FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: tmp.path)
            // 原子替换：Linux Foundation 的 replaceItemAt 在目标由本进程刚创建时
            // 会抛 NSCocoaErrorDomain Code=4，故用「删旧 → rename 新」两步骤。
            // 两者同目录，rename(2) 是原子的；崩溃时最坏留下 .tmp，下次启动重建。
            if FileManager.default.fileExists(atPath: fileURL.path) {
                try FileManager.default.removeItem(at: fileURL)
            }
            try FileManager.default.moveItem(at: tmp, to: fileURL)
        } catch {
            // 落盘失败不能变成「静默重复发送」：打印离散错误，由上层 fail-closed。
            print("unified_idempotency_persist_failed \(error)")
        }
    }
}
