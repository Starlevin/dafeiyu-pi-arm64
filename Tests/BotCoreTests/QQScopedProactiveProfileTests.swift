import Foundation
import Testing
@testable import BotCore

struct QQScopedProactiveProfileTests {
    private func clock(_ hour: Int, _ minute: Int = 0) -> Date {
        var cal = Calendar(identifier: .gregorian)
        cal.timeZone = TimeZone(identifier: "Asia/Shanghai")!
        return cal.date(from: DateComponents(year: 2026, month: 11, day: 9, hour: hour, minute: minute))!
    }

    @Test func noUnapprovedGlobalOrUnknownGroup() {
        #expect(QQScopedProactiveProfile.fromEnvironment([:]) == nil)
        #expect(QQScopedProactiveProfile.fromEnvironment(["DAFEIYU_PROACTIVE_GROUP_ID": "961666830"]) == nil)
        #expect(QQScopedProactiveProfile.fromEnvironment(["DAFEIYU_PROACTIVE_GROUP_ID": "all", "DAFEIYU_PROACTIVE_MODE": "natural"]) == nil)
        #expect(QQScopedProactiveProfile.fromEnvironment(["DAFEIYU_PROACTIVE_GROUP_ID": "961666830", "DAFEIYU_PROACTIVE_MODE": "unknown"]) == nil)
        let p = QQScopedProactiveProfile.fromEnvironment(["DAFEIYU_PROACTIVE_GROUP_ID": "961666830", "DAFEIYU_PROACTIVE_MODE": "natural"])
        #expect(p?.groupKey == "group:961666830")
        #expect(p?.interval == 5)
        #expect(p?.evaluationProbability == 0.25)
        #expect(p?.dailyLimit == 8)
        #expect(p?.cooldown == 480)
    }

    @Test func probabilitiesAndPresetIntervals() {
        for (mode, interval, probability, max) in [("quiet", 10, 0.15, 3), ("natural", 5, 0.25, 8), ("active", 3, 0.35, 15)] {
            let profile = QQScopedProactiveProfile.fromEnvironment(["DAFEIYU_PROACTIVE_GROUP_ID": "961666830", "DAFEIYU_PROACTIVE_MODE": mode])!
            #expect(profile.interval == interval)
            #expect(profile.dailyLimit == max)
            #expect(profile.shouldEvaluate(messageCount: interval, roll: probability - 0.001))
            #expect(!profile.shouldEvaluate(messageCount: interval - 1, roll: 0))
            #expect(!profile.shouldEvaluate(messageCount: interval, roll: probability))
            #expect(!profile.shouldEvaluate(messageCount: interval, roll: -1))
        }
    }

    @Test func quietHoursCooldownAndPersistentDailyBudget() {
        let p = QQScopedProactiveProfile.fromEnvironment(["DAFEIYU_PROACTIVE_GROUP_ID": "961666830", "DAFEIYU_PROACTIVE_MODE": "natural"])!
        #expect(!p.canSpeak(at: clock(2), confirmed: []))
        #expect(!p.canSpeak(at: clock(7, 59), confirmed: []))
        #expect(p.canSpeak(at: clock(8), confirmed: []))
        #expect(!p.canSpeak(at: clock(13, 7), confirmed: [clock(13)]))
        #expect(p.canSpeak(at: clock(13, 8), confirmed: [clock(13)]))
        #expect(!p.canSpeak(at: clock(23), confirmed: (0..<8).map { clock(9 + $0) }))
        #expect(p.canSpeak(at: clock(23), confirmed: [clock(10)]))
    }
}
