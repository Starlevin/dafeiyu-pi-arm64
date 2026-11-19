import Foundation
import Testing
@testable import BotCore
@testable import WeChatAIBot

/// Adapter-level fixtures for the risks that are not visible in the pure protocol tests.
@Suite struct QQReplyDecisionAdapterTests {
    private let groupKey = "group:10001"
    // 09:30 Asia/Shanghai；避免夜间静默时段先于 addressed_to_other 命中。
    private let now = Date(timeIntervalSince1970: 1_700_011_800)

    private func context(quotedSpeaker: String) -> QQGroupContext {
        let quoted = QQMemoryEvent(id: "m1", subject: quotedSpeaker, text: "原消息", at: now.addingTimeInterval(-10))
        let trigger = QQIncoming(id: "m2", sender: "20002", target: "10001", group: true,
                                 text: "引用一下", time: now.timeIntervalSince1970,
                                 replyTo: "m1", images: [], mentionsSelf: false,
                                 isOwner: false, mentionedUsers: [])
        let event = QQMemoryEvent(id: "m2", subject: "OWNER", text: "引用一下", at: now)
        return QQGroupContext(events: [quoted, event], through: "m2", messages: ["m2": trigger], account: "10001")!
    }

    private var profile: QQScopedProactiveProfile {
        QQScopedProactiveProfile(groupKey: groupKey, mode: .natural)
    }

    @Test func replyToOtherIsNotMustAnswer() async {
        let input = QQReplyDecisionInputBuilder.build(
            message: QQIncoming(id: "m2", sender: "20002", target: "10001", group: true,
                                text: "引用一下", time: now.timeIntervalSince1970,
                                replyTo: "m1", images: [], mentionsSelf: false,
                                isOwner: false, mentionedUsers: []),
            context: context(quotedSpeaker: "PEER"),
            profile: profile, now: now, dailyRemaining: 30)
        #expect(input.mention == .other)
        #expect(input.addressedToOther)
        #expect(!input.isMustAnswer)
        let engine = ReplyDecisionEngine()
        let (result, _) = await engine.decide(input, state: ReplyDecisionState(dayKey: ReplyDecisionState.dayKey(for: Int(now.timeIntervalSince1970))),
                                              now: Int(now.timeIntervalSince1970))
        #expect(result.action == .skip)
        #expect(result.reason == .addressedToOther)
    }

    @Test func replyToBotIsMustAnswer() async {
        let input = QQReplyDecisionInputBuilder.build(
            message: QQIncoming(id: "m2", sender: "20002", target: "10001", group: true,
                                text: "引用一下", time: now.timeIntervalSince1970,
                                replyTo: "m1", images: [], mentionsSelf: false,
                                isOwner: false, mentionedUsers: []),
            context: context(quotedSpeaker: "BOT"),
            profile: profile, now: now, dailyRemaining: 30)
        #expect(input.mention == .replyToBot)
        #expect(!input.addressedToOther)
        #expect(input.isMustAnswer)
        let engine = ReplyDecisionEngine()
        let (result, _) = await engine.decide(input, state: ReplyDecisionState(dayKey: ReplyDecisionState.dayKey(for: Int(now.timeIntervalSince1970))),
                                              now: Int(now.timeIntervalSince1970))
        #expect(result.action == .speak)
        #expect(result.reason == .mustAnswer)
    }

    @MainActor
    @Test func gateRollsDailyRemainingBeforeNewDay() async {
        let day1 = Date(timeIntervalSince1970: 1_700_000_000)
        let day2 = day1.addingTimeInterval(86_400)
        let gate = QQReplyDecisionGate()
        gate.recordSpeak(chatKey: groupKey, text: "昨天说过", at: day1)
        let message = QQIncoming(id: "m1", sender: "20001", target: "10001", group: true,
                                 text: "今天继续吗", time: day2.timeIntervalSince1970,
                                 replyTo: nil, images: [], mentionsSelf: false,
                                 isOwner: false, mentionedUsers: [])
        let context = QQGroupContext(events: [QQMemoryEvent(id: "m1", subject: "PEER", text: "今天继续吗", at: day2)],
                                     through: "m1", messages: ["m1": message], account: "10001")!
        let settings = QQReplyDecisionSettings.load(["DAFEIYU_REPLY_DECISION_V2": "shadow"])
        let trace = await gate.decide(message: message, context: context, profile: profile,
                                      settings: settings, now: day2, scorer: nil)
        #expect(trace?.input.dailyRemaining == 30)
    }

    @Test func legacyAndV2EnforceDoNotBothEnqueue() {
        #expect(suppressesLegacyEnqueue(legacyMode: .shadow, reply: .load(["DAFEIYU_REPLY_DECISION_V2": "enforce"])))
        #expect(suppressesLegacyEnqueue(legacyMode: .enforce, reply: .load(["DAFEIYU_REPLY_DECISION_V2": "off"])))
        #expect(!suppressesLegacyEnqueue(legacyMode: .shadow, reply: .load(["DAFEIYU_REPLY_DECISION_V2": "shadow"])))
        #expect(!suppressesLegacyEnqueue(legacyMode: .off, reply: .load(["DAFEIYU_REPLY_DECISION_V2": "shadow"])))
    }

    @Test func runtimeGuardUsesRecallOverlapNotMemoryBookExistence() {
        let message = QQIncoming(id: "m1", sender: "20001", target: "10001", group: true,
                                 text: "你还记得吗", time: now.timeIntervalSince1970,
                                 replyTo: nil, images: [], mentionsSelf: false,
                                 isOwner: false, mentionedUsers: [])
        let persona = QQPersona()
        let supported = QQPersonaRuntimeGuard.check(
            reply: "上次你说的那个阵容，我记下了。", message: message, persona: persona,
            memoryEvidence: "上次你说想试双辅助阵容")
        #expect(!supported.blocking)
        let unsupported = QQPersonaRuntimeGuard.check(
            reply: "我记得我们上次一起打排位。", message: message, persona: persona,
            memoryEvidence: "上次你说想试双辅助阵容")
        #expect(unsupported.blocking)
        #expect(unsupported.hits.contains { $0.code == .fabricatedSharedExperience })
    }
}
