import Foundation
import Testing
@testable import BotCore

struct RuntimePolicyTests {
    @Test func idleProbeBacksOffAndStillDetectsMissingEvents() {
        var schedule = ReadSchedule()
        func due(now: TimeInterval, event: Bool) -> Bool { schedule.isDue(now: now, event: event) }
        #expect((due(now: 0, event: false)))
        schedule.didRead(now: 0, changed: true)
        #expect((!due(now: 0.9, event: false)))
        #expect((due(now: 1, event: false)))
        schedule.didRead(now: 1, changed: false)
        #expect((!due(now: 2.9, event: false)))
        schedule.didRead(now: 3, changed: false)
        #expect((!due(now: 6.9, event: false)))
        schedule.didRead(now: 7, changed: false)
        #expect((!due(now: 14.9, event: false)))
        #expect((due(now: 15, event: false)))
    }
    @Test func eventBurstCoalescesAndWakesAnIdleReader() {
        var schedule = ReadSchedule()
        func due(now: TimeInterval, event: Bool) -> Bool { schedule.isDue(now: now, event: event) }
        schedule.didRead(now: 0, changed: false)
        for tick in 1...9 { #expect((!due(now: Double(tick) / 10, event: true))) }
        #expect((due(now: 1, event: false)))
        schedule.didRead(now: 1, changed: false)
        #expect((!due(now: 1.1, event: false)))
        #expect((due(now: 2.2, event: true)))
    }
    @Test func globalAndPerChatSendLimitsAreIndependent() throws {
        var limits = SendLimits(); limits.daily = 3; limits.perChatDaily = 2
        var usage = SendUsage(); let chat = UUID(), other = UUID(), now = Date()
        let first = try usage.reserve(chat: chat, limits: limits, chatCooldown: 5, now: now)
        usage.finish(first, confirmed: false, now: now)
        #expect(usage.attempts == 1 && usage.uncertain == 1)
        #expect((usage.allowance(chat: other, limits: limits, chatCooldown: 5, now: now.addingTimeInterval(1)) == .cooldown))
        #expect((usage.allowance(chat: chat, limits: limits, chatCooldown: 5, now: now.addingTimeInterval(4)) == .cooldown))
        let second = try usage.reserve(chat: chat, limits: limits, chatCooldown: 5, now: now.addingTimeInterval(6))
        usage.finish(second, confirmed: true, now: now.addingTimeInterval(6))
        #expect((usage.allowance(chat: chat, limits: limits, chatCooldown: 5, now: now.addingTimeInterval(12)) == .chatLimit))
        _ = try usage.reserve(chat: other, limits: limits, chatCooldown: 5, now: now.addingTimeInterval(12))
        #expect((usage.allowance(chat: UUID(), limits: limits, chatCooldown: 5, now: now.addingTimeInterval(20)) == .globalLimit))
        #expect(usage.attempts == 3 && usage.confirmed == 1 && usage.uncertain == 1)
    }
    @Test func crashRecoveryCountsUnknownOnceWithoutRefunding() throws {
        var usage = SendUsage(); let chat = UUID(), now = Date()
        let ticket = try usage.reserve(chat: chat, limits: SendLimits(), chatCooldown: 5, now: now)
        var restored = try JSONDecoder().decode(SendUsage.self, from: JSONEncoder().encode(usage))
        restored.recoverInterrupted(now: now)
        restored.recoverInterrupted(now: now)
        restored.finish(ticket, confirmed: true, now: now)
        #expect(restored.attempts == 1 && restored.uncertain == 1 && restored.confirmed == 0)
        #expect(restored.byChat[chat.uuidString] == 1)
    }
    @Test func completionIsIdempotentAndDailyRolloverKeepsCooldown() throws {
        var usage = SendUsage(), limits = SendLimits(); limits.globalIntervalSeconds = 10
        let calendar = Calendar.current, chat = UUID()
        let midnight = calendar.date(byAdding: .day, value: 1, to: calendar.startOfDay(for: Date()))!
        let ticket = try usage.reserve(chat: chat, limits: limits, chatCooldown: 20, now: midnight.addingTimeInterval(-2))
        usage.finish(ticket, confirmed: true, now: midnight.addingTimeInterval(-1))
        usage.finish(ticket, confirmed: true, now: midnight.addingTimeInterval(-1))
        #expect(usage.confirmed == 1)
        #expect((usage.allowance(chat: chat, limits: limits, chatCooldown: 20, now: midnight) == .cooldown))
        #expect(usage.attempts == 0 && usage.confirmed == 0)
        #expect((usage.allowance(chat: chat, limits: limits, chatCooldown: 20, now: midnight.addingTimeInterval(21)) == .allowed))
    }
    @Test func legacyConfigDecodesAndInvalidLimitsAreRejected() throws {
        var dictionary = try JSONSerialization.jsonObject(with: JSONEncoder().encode(BotConfig())) as! [String: Any]
        dictionary.removeValue(forKey: "sendLimits")
        var config = try JSONDecoder().decode(BotConfig.self, from: JSONSerialization.data(withJSONObject: dictionary))
        #expect(config.effectiveSendLimits.daily == 100)
        config.effectiveSendLimits.daily = -1
        #expect(throws: (any Error).self) { try config.validate() }
    }
    @Test func unlimitedAndPerTargetOverridesPreserveCountersAndCooldown() throws {
        var limits = SendLimits(); limits.daily = 0; limits.perChatDaily = 0
        let chat = UUID(), other = UUID(), now = Date()
        var usage = SendUsage()
        for i in 0..<110 {
            let t = try usage.reserve(chat: chat, limits: limits, chatCooldown: 1, now: now.addingTimeInterval(Double(i * 4)))
            usage.finish(t, confirmed: true, now: now.addingTimeInterval(Double(i * 4)))
        }
        #expect(usage.attempts == 110 && usage.confirmed == 110)
        let later = now.addingTimeInterval(450)
        limits.perChatDailyOverrides = [chat.uuidString: 100]
        let blocked = usage.allowance(chat: chat, limits: limits, chatCooldown: 1, now: later)
        let otherAllowed = usage.allowance(chat: other, limits: limits, chatCooldown: 1, now: later)
        #expect(blocked == .chatLimit)
        #expect(otherAllowed == .allowed)
        limits.perChatDailyOverrides = [chat.uuidString: 0]
        let unlimited = usage.allowance(chat: chat, limits: limits, chatCooldown: 1, now: later)
        let cooldown = usage.allowance(chat: chat, limits: limits, chatCooldown: 1, now: now.addingTimeInterval(437))
        #expect(unlimited == .allowed)
        #expect(cooldown == .cooldown)
        try limits.validate()
        let roundTrip = try JSONDecoder().decode(SendLimits.self, from: JSONEncoder().encode(limits))
        #expect(roundTrip == limits)
        limits.perChatDailyOverrides = ["not-a-uuid": 1]
        #expect(throws: (any Error).self) { try limits.validate() }
    }
    @Test func unlimitedModelCallsStillCountAndPositiveCapStillWorks() {
        var usage = Usage(); let now = Date()
        for _ in 0..<250 { let reserved = usage.reserve(limit: 0, now: now); #expect(reserved) }
        #expect(usage.calls == 250)
        let rejected = usage.reserve(limit: 250, now: now)
        let reserved = usage.reserve(limit: 251, now: now)
        #expect(!rejected)
        #expect(reserved)
    }
}
