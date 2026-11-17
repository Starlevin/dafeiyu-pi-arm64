import Foundation

/// Date-scoped, per-group telemetry for observing proactive replies without storing message text.
public struct QQProactiveDailyStats: Codable, Sendable {
    public enum Event {
        case ordinary, candidate, gatePassed, queued, blocked, contextRejected, modelSilent, confirmed, directMention
    }

    public var day = ""
    public var ordinary = 0
    public var candidate = 0
    public var gatePassed = 0
    public var queued = 0
    public var blocked = 0
    public var contextRejected = 0
    public var modelSilent = 0
    public var confirmed = 0
    public var directMention = 0

    public init() {}

    public mutating func add(_ event: Event, at date: Date = Date()) {
        var calendar = Calendar(identifier: .gregorian)
        calendar.timeZone = TimeZone(identifier: "Asia/Shanghai")!
        let parts = calendar.dateComponents([.year, .month, .day], from: date)
        let today = "\(parts.year!)-\(parts.month!)-\(parts.day!)"
        if day != today {
            self = QQProactiveDailyStats()
            day = today
        }
        switch event {
        case .ordinary: ordinary += 1
        case .candidate: candidate += 1
        case .gatePassed: gatePassed += 1
        case .queued: queued += 1
        case .blocked: blocked += 1
        case .contextRejected: contextRejected += 1
        case .modelSilent: modelSilent += 1
        case .confirmed: confirmed += 1
        case .directMention: directMention += 1
        }
    }
}
