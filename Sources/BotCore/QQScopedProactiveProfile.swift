import Foundation

/// Independent per-group participation profile. An absent or invalid environment profile
/// is fail-closed and never broadens the legacy global group-participation setting.
public struct QQScopedProactiveProfile: Sendable {
    public enum Mode: String, Sendable { case quiet, natural, active }

    public let groupKey: String
    public let mode: Mode

    public static func fromEnvironment(_ env: [String: String] = ProcessInfo.processInfo.environment) -> Self? {
        guard let id = env["DAFEIYU_PROACTIVE_GROUP_ID"], QQConfig.validID(id),
              let modeName = env["DAFEIYU_PROACTIVE_MODE"],
              let mode = Mode(rawValue: modeName) else { return nil }
        return Self(groupKey: "group:" + id, mode: mode)
    }

    public var interval: Int {
        switch mode { case .quiet: return 10; case .natural: return 5; case .active: return 3 }
    }
    public var evaluationProbability: Double {
        switch mode { case .quiet: return 0.15; case .natural: return 0.25; case .active: return 0.35 }
    }
    public var cooldown: TimeInterval {
        switch mode { case .quiet: return 1200; case .natural: return 480; case .active: return 240 }
    }
    public var dailyLimit: Int {
        switch mode { case .quiet: return 3; case .natural: return 8; case .active: return 15 }
    }

    private var localCalendar: Calendar {
        var calendar = Calendar(identifier: .gregorian)
        calendar.timeZone = TimeZone(identifier: "Asia/Shanghai")!
        return calendar
    }

    public func inQuietHours(_ date: Date) -> Bool {
        localCalendar.component(.hour, from: date) < 8
    }

    /// The 25% gate controls only whether to ask the model; it does not guarantee a message.
    public func shouldEvaluate(messageCount: Int, roll: Double) -> Bool {
        messageCount >= interval && roll >= 0 && roll < evaluationProbability
    }

    /// Conservative: count confirmed sends and refuse further sends during cooldown,
    /// once daily budget is exhausted, or during local quiet hours.
    public func canSpeak(at now: Date, confirmed: [Date]) -> Bool {
        guard !inQuietHours(now) else { return false }
        let today = confirmed.filter { localCalendar.isDate($0, inSameDayAs: now) }
        guard today.count < dailyLimit else { return false }
        if let last = confirmed.max(), now.timeIntervalSince(last) < cooldown { return false }
        return true
    }
}
