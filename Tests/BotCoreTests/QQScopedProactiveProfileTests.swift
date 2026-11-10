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
        #expect(p?.interval == 3)
        #expect(p?.evaluationProbability == 0.25)
        #expect(p?.dailyLimit == 30)
        #expect(p?.cooldown == 480)
    }

    @Test func threeApprovedGroupsAreIndependentAndFailClosed() {
        let env = [
            "DAFEIYU_PROACTIVE_GROUP_IDS": "1109445632,961666830,648418715",
            "DAFEIYU_PROACTIVE_GROUP_ID": "961666830",
            "DAFEIYU_PROACTIVE_MODE": "natural"
        ]
        let profiles = QQScopedProactiveProfile.profilesFromEnvironment(env)
        #expect(Set(profiles.keys) == Set(["group:1109445632", "group:961666830", "group:648418715"]))
        #expect(profiles.values.allSatisfy { $0.interval == 3 && $0.dailyLimit == 30 && $0.cooldown == 480 })
        #expect(profiles["private:1915946704"] == nil)
        var bad = env
        bad["DAFEIYU_PROACTIVE_GROUP_IDS"] = "1109445632,961666830,abc"
        #expect(QQScopedProactiveProfile.profilesFromEnvironment(bad).isEmpty)
        bad["DAFEIYU_PROACTIVE_GROUP_IDS"] = "1109445632,1109445632"
        #expect(QQScopedProactiveProfile.profilesFromEnvironment(bad).isEmpty)
        bad["DAFEIYU_PROACTIVE_GROUP_IDS"] = ""
        #expect(QQScopedProactiveProfile.profilesFromEnvironment(bad).isEmpty)
        bad.removeValue(forKey: "DAFEIYU_PROACTIVE_GROUP_IDS")
        #expect(QQScopedProactiveProfile.profilesFromEnvironment(bad).count == 1)
    }
    @Test func perGroupMetricsRollover() {
        var stats = QQProactiveDailyStats()
        stats.add(.ordinary, at: clock(9))
        stats.add(.candidate, at: clock(9))
        stats.add(.gatePassed, at: clock(9))
        stats.add(.confirmed, at: clock(9))
        #expect(stats.ordinary == 1 && stats.candidate == 1 && stats.gatePassed == 1 && stats.confirmed == 1)
        var cal = Calendar(identifier: .gregorian); cal.timeZone = TimeZone(identifier: "Asia/Shanghai")!
        let nextDay = cal.date(byAdding: .day, value: 1, to: clock(9))!
        stats.add(.blocked, at: nextDay)
        #expect(stats.ordinary == 0 && stats.confirmed == 0 && stats.blocked == 1)
    }
    @Test func probabilitiesAndPresetIntervals() {
        for (mode, interval, probability, max) in [("quiet", 10, 0.15, 3), ("natural", 3, 0.25, 30), ("active", 3, 0.35, 15)] {
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
        #expect(!p.canSpeak(at: clock(23), confirmed: (0..<30).map { clock(9, $0) }))
        #expect(p.canSpeak(at: clock(23), confirmed: [clock(10)]))
    }
}
