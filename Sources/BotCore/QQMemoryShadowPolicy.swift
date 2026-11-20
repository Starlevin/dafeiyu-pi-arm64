import Foundation

/// Why a shadow session is not (or no longer) observing. Values are stable for UI/日志.
public enum QQMemoryShadowStopReason: String, Codable, Equatable, Sendable {
    case allowlistEmpty
    case notAllowlisted
    case ttlExpired
    case highWatermark
    case corruptSession
    case disabled
}

/// Shadow-only policy. It never changes the authoritative path; it only decides
/// whether a scope may receive a bounded, time-limited side WAL.
///
/// Default is fail-closed: no scopes are enabled. Operators must explicitly set
/// `DAFEIYU_MEMORY_SHADOW_SCOPES` to `*` or a comma/semicolon separated allowlist.
public struct QQMemoryShadowPolicy: Codable, Equatable, Sendable {
    public static let defaultTTL: TimeInterval = 6 * 60 * 60
    public static let minimumTTL: TimeInterval = 60
    public static let maximumTTL: TimeInterval = 7 * 24 * 60 * 60

    /// Lifetime of one shadow session. The start is persisted under
    /// `QQMemoryReliable/shadow-session.json`, so a process restart does not reset it.
    public var ttl: TimeInterval
    /// `nil` means all scopes (explicit `*`), an empty array means none.
    public var scopes: [String]?
    /// Stop before the WAL is full. The WAL itself still refuses at its hard cap.
    public var stopOnHighWatermark: Bool

    public init(ttl: TimeInterval = defaultTTL, scopes: [String]? = [], stopOnHighWatermark: Bool = true) {
        self.ttl = ttl
        self.scopes = scopes
        self.stopOnHighWatermark = stopOnHighWatermark
    }

    public var isUsable: Bool { ttl >= Self.minimumTTL && ttl <= Self.maximumTTL && allowsAnyScope }
    public var allowsAnyScope: Bool {
        guard let scopes else { return true }
        return !scopes.isEmpty
    }

    /// Exact scope, or a suffix such as `group:123` / `private:456`.
    public func allows(scope: String) -> Bool {
        guard let scopes else { return true }
        guard !scopes.isEmpty else { return false }
        let normalized = scope.trimmingCharacters(in: .whitespacesAndNewlines)
        return scopes.contains { entry in
            let value = entry.trimmingCharacters(in: .whitespacesAndNewlines)
            guard !value.isEmpty else { return false }
            if value == "*" || value.lowercased() == "all" { return true }
            if normalized == value { return true }
            return normalized.hasSuffix(":" + value)
        }
    }

    /// Reads only environment variables; no chat text, no network.
    public static func resolve(environment: [String: String] = ProcessInfo.processInfo.environment) -> Self {
        let rawScopes = environment["DAFEIYU_MEMORY_SHADOW_SCOPES"] ?? ""
        let scopes: [String]?
        if rawScopes.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
            scopes = [] // safe default: no group is shadowed until explicitly allowlisted
        } else {
            let tokens = rawScopes
                .split(whereSeparator: { $0 == "," || $0 == ";" || $0.isWhitespace })
                .map { String($0).trimmingCharacters(in: .whitespacesAndNewlines) }
                .filter { !$0.isEmpty }
            let lower = tokens.map { $0.lowercased() }
            if lower.contains("none") || lower.contains("off") || tokens.isEmpty {
                scopes = []
            } else if lower.contains("*") || lower.contains("all") {
                scopes = nil
            } else {
                scopes = tokens
            }
        }

        let rawTTL = (environment["DAFEIYU_MEMORY_SHADOW_TTL_SECONDS"] ?? "")
            .trimmingCharacters(in: .whitespacesAndNewlines)
        let ttl: TimeInterval
        if rawTTL.isEmpty {
            ttl = defaultTTL
        } else if let value = TimeInterval(rawTTL), value >= minimumTTL, value <= maximumTTL {
            ttl = value
        } else {
            ttl = 0 // invalid/unsafe value fails closed, never silently extends
        }

        let rawExit = (environment["DAFEIYU_MEMORY_SHADOW_EXIT_ON_HIGH_WATERMARK"] ?? "")
            .trimmingCharacters(in: .whitespacesAndNewlines)
            .lowercased()
        let stopOnHighWatermark = !["0", "false", "no", "off"].contains(rawExit)

        return Self(ttl: ttl, scopes: scopes, stopOnHighWatermark: stopOnHighWatermark)
    }

    public static func resetRequested(environment: [String: String] = ProcessInfo.processInfo.environment) -> Bool {
        let raw = (environment["DAFEIYU_MEMORY_SHADOW_RESET"] ?? "")
            .trimmingCharacters(in: .whitespacesAndNewlines)
            .lowercased()
        return ["1", "true", "yes", "on"].contains(raw)
    }
}

/// A persisted, process-wide shadow window. The file contains only timing and a
/// stop reason; it contains no chat text and no scope names.
public struct QQMemoryShadowSession: Codable, Equatable, Sendable {
    public var startedAt: Date
    public var expiresAt: Date
    public var stoppedAt: Date?
    public var stopReason: QQMemoryShadowStopReason?

    public init(startedAt: Date, ttl: TimeInterval) {
        self.startedAt = startedAt
        self.expiresAt = startedAt.addingTimeInterval(max(0, ttl))
        self.stoppedAt = nil
        self.stopReason = nil
    }

    public init(startedAt: Date, expiresAt: Date, stoppedAt: Date? = nil, stopReason: QQMemoryShadowStopReason? = nil) {
        self.startedAt = startedAt
        self.expiresAt = expiresAt
        self.stoppedAt = stoppedAt
        self.stopReason = stopReason
    }

    public func isActive(now: Date = Date()) -> Bool {
        stopReason == nil && now < expiresAt
    }

    public func remainingSeconds(now: Date = Date()) -> Int {
        max(0, Int(expiresAt.timeIntervalSince(now)))
    }

    public mutating func stop(_ reason: QQMemoryShadowStopReason, now: Date = Date()) {
        guard stopReason == nil else { return }
        stopReason = reason
        stoppedAt = now
    }

    public static func failed(now: Date = Date(), _ reason: QQMemoryShadowStopReason) -> Self {
        Self(startedAt: now, expiresAt: now, stoppedAt: now, stopReason: reason)
    }

    /// Reads or creates the persisted session. A corrupt/expired file is never
    /// silently replaced with a fresh window; it fails closed until reset.
    public static func loadOrCreate(root: URL, policy: QQMemoryShadowPolicy, now: Date = Date(),
                                    reset: Bool = false) throws -> Self {
        let file = root.appendingPathComponent("shadow-session.json")
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)

        let decoder = JSONDecoder()
        decoder.dateDecodingStrategy = .iso8601
        if !reset, let data = try? Data(contentsOf: file), let decoded = try? decoder.decode(Self.self, from: data) {
            var session = decoded
            if session.stopReason == nil, now >= session.expiresAt {
                session.stop(.ttlExpired, now: now)
                try? session.persist(to: root)
            }
            return session
        }
        if !reset, FileManager.default.fileExists(atPath: file.path) {
            return .failed(now: now, .corruptSession)
        }

        let session = Self(startedAt: now, ttl: policy.ttl)
        try session.persist(to: root)
        return session
    }

    public func persist(to root: URL) throws {
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
        let encoder = JSONEncoder()
        encoder.outputFormatting = [.sortedKeys]
        encoder.dateEncodingStrategy = .iso8601
        let data = try encoder.encode(self)
        try data.write(to: root.appendingPathComponent("shadow-session.json"), options: [.atomic])
    }
}

/// Read-only diagnostic view for the main controller/tests. No chat text, no raw
/// event bodies; only counters and timing.
public struct QQMemoryShadowStatus: Codable, Equatable, Sendable {
    public var scope: String
    public var active: Bool
    public var stopReason: QQMemoryShadowStopReason?
    public var queuedCount: Int
    public var queueBytes: Int
    public var refusedEvents: Int
    public var highWatermark: Bool
    public var startedAt: Date?
    public var expiresAt: Date?
    public var remainingSeconds: Int
    public var alerts: [String]
}

extension QQMemoryShadowPolicy {
    public func status(scope: String, session: QQMemoryShadowSession?, stats: QQMemoryReliableStats?, mode: QQMemoryReliableMode,
                       now: Date = Date()) -> QQMemoryShadowStatus {
        var output = QQMemoryShadowStatus(scope: scope, active: false, stopReason: nil,
                                          queuedCount: stats?.queuedCount ?? 0, queueBytes: stats?.queueBytes ?? 0,
                                          refusedEvents: stats?.refusedEvents ?? 0,
                                          highWatermark: stats?.highWatermark ?? false,
                                          startedAt: session?.startedAt, expiresAt: session?.expiresAt,
                                          remainingSeconds: session?.remainingSeconds(now: now) ?? 0, alerts: [])
        guard mode == .shadow else {
            output.stopReason = .disabled
            return output
        }
        guard allowsAnyScope else {
            output.stopReason = .allowlistEmpty
            output.alerts.append("shadow-allowlist-empty")
            return output
        }
        guard allows(scope: scope) else {
            output.stopReason = .notAllowlisted
            output.alerts.append("shadow-not-allowlisted")
            return output
        }
        guard let session else {
            output.stopReason = .disabled
            return output
        }
        if session.stopReason == .ttlExpired || now >= session.expiresAt {
            output.stopReason = .ttlExpired
        } else if let reason = session.stopReason {
            output.stopReason = reason
        }
        output.active = output.stopReason == nil && session.isActive(now: now)
        if output.highWatermark { output.alerts.append("shadow-high-watermark") }
        if output.refusedEvents > 0 { output.alerts.append("shadow-refused") }
        if output.active, output.remainingSeconds > 0, output.remainingSeconds <= 300 {
            output.alerts.append("shadow-expiring")
        }
        return output
    }
}
