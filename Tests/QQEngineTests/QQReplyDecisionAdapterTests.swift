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

    @Test func scoringContextPreservesTrustedQuoteAndParticipantMetadata() throws {
        let payload = try #require(QQProactiveDecision.scoringContext(["原消息", "引用一下"],
                                                                       group: context(quotedSpeaker: "BOT")))
        let lines = payload.split(separator: "\n")
        #expect(lines.count == 2)
        let last = try #require(lines.last)
        let decoded = try JSONSerialization.jsonObject(with: Data(last.utf8))
        let fields = try #require(decoded as? [String: String])
        #expect(fields["message"] == "m2")
        #expect(fields["replyTo"] == "m1")
        #expect(fields["quotedSpeaker"] == "BOT")
        #expect(fields["speaker"]?.hasPrefix("[OWNER]") == true)
        let privateText = "示例地址 https://example.test/private 和号码 123456789，口令：sample-secret"
        let messageID = "843721137"
        let mention = QQIncoming(id: messageID, sender: "20002", target: "10001", group: true,
                                 text: privateText, time: now.timeIntervalSince1970, replyTo: nil,
                                 images: [], mentionsSelf: true, isOwner: false, mentionedUsers: ["30003"])
        let privateGroup = try #require(QQGroupContext(
            events: [QQMemoryEvent(id: messageID, subject: "MEMBER", text: privateText, at: now)],
            through: messageID, messages: [messageID: mention], account: "30003"
        ))
        let scrubbed = try #require(QQProactiveDecision.scoringContext(["简化文本"], group: privateGroup))
        #expect(scrubbed.contains("[省略]"))
        let mentionedJSON = try JSONSerialization.jsonObject(with: Data(scrubbed.utf8))
        let mentionedFields = try #require(mentionedJSON as? [String: String])
        #expect(mentionedFields["addressedTo"] == "BOT（本账号）")
        #expect(mentionedFields["message"] == "m1")
        #expect(!scrubbed.contains(messageID))
        #expect(!scrubbed.contains("example.test"))
        #expect(!scrubbed.contains("123456789"))
        #expect(!scrubbed.contains("sample-secret"))
        #expect(QQProactiveDecision.scoringContext(["原消息"], group: nil) == "[\"原消息\"]")
    }

    @Test func scoringContextBoundsHistoryAndKeepsTheTrigger() throws {
        let events = (1...12).map { index in
            QQMemoryEvent(id: "source-\(index)", subject: index == 2 ? "BOT" : "MEMBER", text: "\(index)号测试队友",
                          at: now.addingTimeInterval(Double(index)))
        }
        let trigger = QQIncoming(id: "source-12", sender: "20002", target: "10001", group: true,
                                 text: "12号测试队友", time: now.addingTimeInterval(12).timeIntervalSince1970,
                                 replyTo: "source-2", images: [], mentionsSelf: false, isOwner: false)
        let group = try #require(QQGroupContext(events: events, through: trigger.id, messages: [trigger.id: trigger]))
        let payload = try #require(QQProactiveDecision.scoringContext(["触发"], group: group))
        let lines = payload.split(separator: "\n")
        #expect(lines.count == 8)
        #expect(payload.count <= 1_400)
        #expect(lines.last?.contains("12号测试队友") == true)
        #expect(lines.last?.contains("\"replyTo\":\"outside_window\"") == true)
        #expect(lines.last?.contains("\"quotedSpeaker\":\"BOT\"") == true)
        #expect(!payload.contains("4号测试队友"))
        #expect(!payload.contains("source-"))
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

    @MainActor
    @Test func gateUsesLiveGroupThresholdForTheNextDecision() async throws {
        let gate = QQReplyDecisionGate()
        let text = "这次编译报错怎么解决？"
        let message = QQIncoming(id: "m2", sender: "20002", target: "10001", group: true,
                                 text: text, time: now.timeIntervalSince1970, replyTo: nil,
                                 images: [], mentionsSelf: false, isOwner: false, mentionedUsers: [])
        let context = try #require(QQGroupContext(events: [
            QQMemoryEvent(id: "m1", subject: "PEER", text: "正在编译示例模块", at: now.addingTimeInterval(-10)),
            QQMemoryEvent(id: "m2", subject: "PEER", text: text, at: now),
        ], through: "m2", messages: ["m2": message], account: "10001"))
        let defaults = QQReplyDecisionSettings.load(["DAFEIYU_REPLY_DECISION_V2": "enforce"])

        let cases: [(Double, ReplyDecisionAction, ReplyDecisionReason)] = [
            (0.70, .wait, .heuristicHold), (0.60, .speak, .heuristicPass), (0.80, .skip, .heuristicLow),
        ]
        for (threshold, action, reason) in cases {
            let trace = try #require(await gate.decide(
                message: message, context: context, profile: profile,
                settings: defaults.applying(groupThreshold: threshold), now: now
            ))
            #expect(trace.result.heuristicScore == 0.622)
            #expect(trace.result.action == action)
            #expect(trace.result.reason == reason)
            #expect(!trace.result.scorerCalled)
        }
    }

    @Test func legacyAndV2EnforceDoNotBothEnqueue() {
        #expect(suppressesLegacyEnqueue(legacyMode: .shadow, reply: .load(["DAFEIYU_REPLY_DECISION_V2": "enforce"])))
        #expect(suppressesLegacyEnqueue(legacyMode: .enforce, reply: .load(["DAFEIYU_REPLY_DECISION_V2": "off"])))
        #expect(!suppressesLegacyEnqueue(legacyMode: .shadow, reply: .load(["DAFEIYU_REPLY_DECISION_V2": "shadow"])))
        #expect(!suppressesLegacyEnqueue(legacyMode: .off, reply: .load(["DAFEIYU_REPLY_DECISION_V2": "shadow"])))
    }

    @Test func publicQuestionRuntimeIsExplicitlyOptInAndCooldownIsBounded() throws {
        let defaultSettings = QQDecisionRuntimeSettings.parse(Data(#"{"group:10001":{"mode":"enforce"}}"#.utf8))
        #expect(defaultSettings?[groupKey]?.publicQuestionEnabled == false)
        #expect(defaultSettings?[groupKey]?.publicQuestionCooldownSeconds == 60)

        let enabled = QQDecisionRuntimeSettings.parse(Data(#"{"group:10001":{"mode":"enforce","publicQuestionEnabled":true,"publicQuestionCooldownSeconds":90}}"#.utf8))
        #expect(enabled?[groupKey]?.publicQuestionEnabled == true)
        #expect(enabled?[groupKey]?.publicQuestionCooldownSeconds == 90)
        for seconds in [29, 1201] {
            let invalid = QQDecisionRuntimeSettings.parse(Data(#"{"group:10001":{"mode":"enforce","publicQuestionCooldownSeconds":\#(seconds)}}"#.utf8))
            #expect(invalid == nil)
        }
        #expect(QQDecisionRuntimeSettings.parse(Data(#"{"group:10001":{"mode":"enforce","publicQuestionEnabled":1}}"#.utf8)) == nil)
    }

    @Test func routeASwiftPrefiltersOnlyRelaxForTheExplicitGroupConfiguration() {
        let parsed = QQDecisionRuntimeSettings.parse(Data(#"""
        {"group:10001":{"mode":"enforce","publicQuestionEnabled":true,"threshold":0.45},
         "group:10002":{"mode":"enforce","publicQuestionEnabled":true,"threshold":0.70},
         "group:10003":{"mode":"enforce","publicQuestionEnabled":false,"threshold":0.45},
         "group:10004":{"mode":"shadow","publicQuestionEnabled":true,"threshold":0.45},
         "group:10005":{"mode":"enforce","publicQuestionEnabled":true}}
        """#.utf8), defaultThreshold: 0.45)
        #expect(parsed?[groupKey]?.admitsOrdinarySwiftScoring == true)
        for other in ["group:10002", "group:10003", "group:10004", "group:10005"] {
            #expect(parsed?[other]?.admitsOrdinarySwiftScoring == false)
        }
    }

    @Test func ordinaryReplySwitchIsIndependentAndKeepsGroupAuthorization() throws {
        let configs = try #require(QQDecisionRuntimeSettings.parse(Data(#"""
        {"group:10001":{"mode":"enforce","ordinaryReplyEnabled":true,"threshold":0.60},
         "group:10002":{"mode":"enforce","ordinaryReplyEnabled":false,"publicQuestionEnabled":true,"threshold":0.45},
         "group:10003":{"mode":"enforce","ordinaryReplyEnabled":true,"publicQuestionEnabled":true,"threshold":0.30},
         "group:10004":{"mode":"enforce","ordinaryReplyEnabled":false,"threshold":0.95},
         "group:10005":{"mode":"enforce","threshold":0.70},
         "group:10006":{"mode":"shadow","ordinaryReplyEnabled":true,"threshold":0.45}}
        """#.utf8)))
        let latest = "厂长开会这梗传得真快"
        let question = "各位，第五人格什么时候该压机？"
        func route(_ group: String, text: String = "厂长开会这梗传得真快",
                   authorized: Bool = true, v2: Bool = true, selfMention: Bool = false,
                   otherMentions: [String] = [], quote: String? = nil,
                   hasUnansweredContext: Bool = true) -> QQPublicQuestionAdmission.Route {
            let input = QQIncoming(id: "m1", sender: "20002", target: String(group.dropFirst(6)),
                                   group: true, text: text, time: now.timeIntervalSince1970,
                                   replyTo: quote, images: [], mentionsSelf: selfMention,
                                   mentionedUsers: otherMentions)
            return QQPublicQuestionAdmission.route(message: input, hasUnansweredContext: hasUnansweredContext,
                                                   settings: configs[group], profileAuthorized: authorized,
                                                   swiftEnforced: v2)
        }
        #expect(route("group:10001", text: latest) == .ordinary)
        #expect(route("group:10001", text: question) == .ordinary)
        #expect(route("group:10001", authorized: false) == .ignore(.notPublicQuestion))
        #expect(route("group:10001", v2: false) == .ignore(.notPublicQuestion))
        #expect(route("group:10001", otherMentions: ["20003"]) == .ignore(.mentionPresent))
        #expect(route("group:10001", quote: "m0") == .ignore(.quotedReply))
        #expect(route("group:10001", hasUnansweredContext: false) == .ignore(.noUnansweredContext))
        #expect(route("group:10002") == .ignore(.notPublicQuestion))
        #expect(route("group:10002", text: question) == .publicQuestion)
        for gameQuestion in ["今晚IVL几点开打？", "三角洲老式钢盔多少钱？"] {
            #expect(route("group:10002", text: gameQuestion) == .publicQuestion)
            #expect(route("group:10001", text: gameQuestion) == .ordinary)
            #expect(route("group:10002", text: gameQuestion, authorized: false) == .ignore(.notPublicQuestion))
            #expect(route("group:10002", text: gameQuestion, otherMentions: ["20003"]) == .ignore(.mentionPresent))
            #expect(route("group:10002", text: gameQuestion, quote: "m0") == .ignore(.quotedReply))
        }
        #expect(route("group:10003", text: question) == .publicQuestion)
        #expect(route("group:10003") == .ordinary)
        #expect(route("group:10004") == .ignore(.notPublicQuestion))
        #expect(route("group:10004", selfMention: true) == .existing)
        #expect(route("group:10005") == .existing)
        #expect(route("group:10006") == .ignore(.notPublicQuestion))
        #expect(QQDecisionRuntimeSettings.parse(Data(#"{"group:10001":{"ordinaryReplyEnabled":1}}"#.utf8)) == nil)
    }

    @MainActor
    @Test func ordinaryReplyThresholdUpdateChangesTheNextScoreDecision() async throws {
        struct Scorer: ReplyScoring {
            func score(_ input: ReplyDecisionInput) async -> Double? { 0.58 }
        }
        let before = try #require(QQDecisionRuntimeSettings.parse(
            Data(#"{"group:10001":{"mode":"enforce","ordinaryReplyEnabled":true,"threshold":0.56}}"#.utf8)))
        let after = try #require(QQDecisionRuntimeSettings.parse(
            Data(#"{"group:10001":{"threshold":0.60}}"#.utf8), defaults: before))
        let text = "厂长开会这梗传得真快"
        let message = QQIncoming(id: "m1", sender: "20002", target: "10001", group: true,
                                 text: text, time: now.timeIntervalSince1970, replyTo: nil, images: [])
        let context = try #require(QQGroupContext(events: [
            QQMemoryEvent(id: "m1", subject: "MEMBER", text: text, at: now)
        ], through: "m1", messages: ["m1": message], account: "10001"))
        let gate = QQReplyDecisionGate()
        let enforcement = QQReplyDecisionSettings.load(["DAFEIYU_REPLY_DECISION_V2": "enforce"])
        for (configs, expected) in [(before, ReplyDecisionReason.modelPass), (after, .belowThreshold)] {
            let group = try #require(configs[groupKey])
            let route = QQPublicQuestionAdmission.route(message: message, hasUnansweredContext: true,
                                                        settings: group, profileAuthorized: true,
                                                        swiftEnforced: enforcement.enforces)
            #expect(route == .ordinary)
            let trace = try #require(await gate.decide(
                message: message, context: context, profile: profile,
                settings: enforcement.applying(groupThreshold: group.threshold), now: now, scorer: Scorer(),
                skipSoftPrefilters: route == .ordinary))
            #expect(trace.result.scorerCalled)
            #expect(trace.result.reason == expected)
        }
    }

    @Test func publicQuestionCooldownOverrideKeepsQuietHoursAndConfirmedDailyCap() {
        let profile = QQScopedProactiveProfile(groupKey: groupKey, mode: .active)
        #expect(!profile.canSpeakPublicQuestion(at: now, confirmed: [now.addingTimeInterval(-45)], cooldownSeconds: 60))
        #expect(profile.canSpeakPublicQuestion(at: now, confirmed: [now.addingTimeInterval(-45)], cooldownSeconds: 30))

        let beforeEight = now.addingTimeInterval(-2 * 3600) // 07:30 Asia/Shanghai
        #expect(!profile.canSpeakPublicQuestion(at: beforeEight, confirmed: [], cooldownSeconds: 30))

        let exhausted = Array(repeating: now, count: profile.dailyLimit)
        #expect(!profile.canSpeakPublicQuestion(at: now, confirmed: exhausted, cooldownSeconds: 30))
    }

    @Test func publicQuestionFastLaneRequiresEveryScopedAdmissionGate() {
        let enabled = QQDecisionRuntimeSettings(mode: .enforce, threshold: 0.70,
                                                 dailyBudget: 30, freshnessSeconds: 3,
                                                 publicQuestionEnabled: true)
        #expect(QQPublicQuestionAdmission.allows(
            profileAuthorized: true, settings: enabled, isGroup: true, mentionsSelf: false,
            mentionedUsers: [], hasReply: false, hasImages: false,
            hasUnansweredContext: true, matchesQuestion: true))
        #expect(!QQPublicQuestionAdmission.allows(
            profileAuthorized: false, settings: enabled, isGroup: true, mentionsSelf: false,
            mentionedUsers: [], hasReply: false, hasImages: false,
            hasUnansweredContext: true, matchesQuestion: true))
        #expect(!QQPublicQuestionAdmission.allows(
            profileAuthorized: true, settings: QQDecisionRuntimeSettings(mode: .shadow, threshold: 0.70,
                dailyBudget: 30, freshnessSeconds: 3, publicQuestionEnabled: true),
            isGroup: true, mentionsSelf: false, mentionedUsers: [], hasReply: false,
            hasImages: false, hasUnansweredContext: true, matchesQuestion: true))
        #expect(!QQPublicQuestionAdmission.allows(
            profileAuthorized: true, settings: enabled, isGroup: true, mentionsSelf: false,
            mentionedUsers: ["20002"], hasReply: false, hasImages: false,
            hasUnansweredContext: true, matchesQuestion: true))
        #expect(!QQPublicQuestionAdmission.allows(
            profileAuthorized: true, settings: enabled, isGroup: true, mentionsSelf: false,
            mentionedUsers: [], hasReply: true, hasImages: false,
            hasUnansweredContext: true, matchesQuestion: true))
        #expect(!QQPublicQuestionAdmission.allows(
            profileAuthorized: true, settings: enabled, isGroup: true, mentionsSelf: false,
            mentionedUsers: [], hasReply: false, hasImages: true,
            hasUnansweredContext: true, matchesQuestion: true))
        #expect(!QQPublicQuestionAdmission.allows(
            profileAuthorized: true, settings: enabled, isGroup: true, mentionsSelf: false,
            mentionedUsers: [], hasReply: false, hasImages: false,
            hasUnansweredContext: false, matchesQuestion: true))
        #expect(!QQPublicQuestionAdmission.allows(
            profileAuthorized: true, settings: enabled, isGroup: true, mentionsSelf: true,
            mentionedUsers: [], hasReply: false, hasImages: false,
            hasUnansweredContext: true, matchesQuestion: true))
    }

    @Test func enabledPilotKeepsOrdinaryNonMentionTrafficOutOfLegacyScorer() {
        let enabled = QQDecisionRuntimeSettings(mode: .enforce, threshold: 0.70,
                                                 dailyBudget: 30, freshnessSeconds: 3,
                                                 publicQuestionEnabled: true)
        #expect(QQPublicQuestionAdmission.pilotApplies(
            profileAuthorized: true, settings: enabled, isGroup: true, mentionsSelf: false))
        #expect(!QQPublicQuestionAdmission.allows(
            profileAuthorized: true, settings: enabled, isGroup: true, mentionsSelf: false,
            mentionedUsers: [], hasReply: false, hasImages: false,
            hasUnansweredContext: true, matchesQuestion: false))
        #expect(!QQPublicQuestionAdmission.pilotApplies(
            profileAuthorized: false, settings: enabled, isGroup: true, mentionsSelf: false))
        #expect(!QQPublicQuestionAdmission.pilotApplies(
            profileAuthorized: true, settings: enabled, isGroup: true, mentionsSelf: true))
    }

    @Test func publicQuestionAdmissionReportsFirstContentGate() {
        let cases: [([String], Bool, Bool, Bool, Bool, QQPublicQuestionAdmission.Rejection?)] = [
            (["other"], true, true, false, false, .mentionPresent),
            ([], true, true, false, false, .quotedReply),
            ([], false, true, false, false, .mediaPresent),
            ([], false, false, false, false, .noUnansweredContext),
            ([], false, false, true, false, .notPublicQuestion),
            ([], false, false, true, true, nil),
        ]
        for (mentions, quoted, media, unanswered, matches, expected) in cases {
            #expect(QQPublicQuestionAdmission.rejection(
                mentionedUsers: mentions, hasReply: quoted, hasImages: media,
                hasUnansweredContext: unanswered, matchesQuestion: matches
            ) == expected)
        }
    }

    @Test func publicQuestionCandidateMustRemainLatestThroughDebounceAndGeneration() {
        #expect(QQPublicQuestionAdmission.candidateIsStillLatest(candidateID: "m1", latestID: "m1"))
        #expect(!QQPublicQuestionAdmission.candidateIsStillLatest(candidateID: "m1", latestID: "m2"))
        #expect(!QQPublicQuestionAdmission.candidateIsStillLatest(candidateID: "m1", latestID: nil))
        #expect(QQPublicQuestionAdmission.candidateWithinLifetime(messageTime: 1_000, now: 1_005))
        #expect(!QQPublicQuestionAdmission.candidateWithinLifetime(messageTime: 1_000, now: 1_121))
        #expect(QQPublicQuestionAdmission.candidateIsChronologicallyLatest(messageTime: 1_005, previousMessageTimes: [1_005]))
        #expect(!QQPublicQuestionAdmission.candidateIsChronologicallyLatest(messageTime: 1_004, previousMessageTimes: [1_005]))
        #expect(!QQPublicQuestionAdmission.candidateIsChronologicallyLatest(
            messageTime: 80, previousMessageTimes: [100, 70])) // A(100), late B(70), candidate C(80)
    }

    @Test func publicQuestionKnowledgeIsLimitedToTwoShortLocalFacts() {
        let long = String(repeating: "事实", count: 200)
        let facts = publicQuestionKnowledgeFacts(from: """
        [第五人格话题提示]
        - 压机：(long)
        - 飞轮：短时冲刺。
        - 第三条：不得进入请求。
        出处：本地索引
        """)
        #expect(facts.count == 2)
        #expect(facts[0].hasPrefix("压机："))
        #expect(facts[0].count <= 220)
        #expect(facts[1] == "飞轮：短时冲刺。")
    }

    @Test func publicQuestionContextUsesAtMostThreePriorRawEvents() {
        #expect(publicQuestionPriorContext(from: ["old", "older", "three", "two", "one", "current question"])
                == ["three", "two", "one"])
        #expect(publicQuestionPriorContext(from: ["current question"]).isEmpty)
    }

    @Test func freshnessKeepsCandidateAfterOnlySameSpeakerInnocuousSuffix() {
        let candidate = QQIncoming(id: "m1", sender: "20002", target: "10001", group: true,
                                   text: "大家觉得这个方案怎么样？", time: now.timeIntervalSince1970,
                                   replyTo: nil, images: [], mentionsSelf: false, isOwner: false,
                                   mentionedUsers: [])
        let suffix = QQIncoming(id: "m2", sender: "20002", target: "10001", group: true,
                                text: "哈哈", time: now.addingTimeInterval(1).timeIntervalSince1970,
                                replyTo: nil, images: [], mentionsSelf: false, isOwner: false,
                                mentionedUsers: [])
        let timeline = [
            QQMemoryEvent(id: "m1", subject: "MEMBER_a", text: candidate.text, at: now),
            QQMemoryEvent(id: "m2", subject: "MEMBER_a", text: suffix.text, at: now.addingTimeInterval(1)),
        ]
        #expect(QQProactiveDecisionFreshness.vetoReason(
            candidate: candidate, timeline: timeline, messages: ["m1": candidate, "m2": suffix]
        ) == nil)

        let changed = QQIncoming(id: "m2", sender: "20002", target: "10001", group: true,
                                 text: "算了先不聊这个", time: now.addingTimeInterval(1).timeIntervalSince1970,
                                 replyTo: nil, images: [], mentionsSelf: false, isOwner: false,
                                 mentionedUsers: [])
        let changedTimeline = [timeline[0], QQMemoryEvent(id: "m2", subject: "MEMBER_a",
                                                           text: changed.text, at: now.addingTimeInterval(1))]
        #expect(QQProactiveDecisionFreshness.vetoReason(
            candidate: candidate, timeline: changedTimeline, messages: ["m1": candidate, "m2": changed]
        ) == "suffix_content_changed")

        let delayed = QQMemoryEvent(id: "m2", subject: "MEMBER_a", text: "哈哈", at: now.addingTimeInterval(11))
        #expect(QQProactiveDecisionFreshness.vetoReason(
            candidate: candidate, timeline: [timeline[0], delayed], messages: ["m1": candidate, "m2": suffix]
        ) == "suffix_delayed")
    }

    @Test func freshnessAllowsOnlyRecentShortOrProvablySameTopicFollowups() {
        let candidate = QQIncoming(id: "m1", sender: "20002", target: "10001", group: true,
                                   text: "大家觉得这个方案怎么样", time: now.timeIntervalSince1970,
                                   replyTo: nil, images: [], mentionsSelf: false, isOwner: false,
                                   mentionedUsers: [])
        func reason(_ text: String, seconds: Double, images: [QQIncomingImage] = []) -> String? {
            let suffix = QQIncoming(id: "m2", sender: "20002", target: "10001", group: true,
                                    text: text, time: now.addingTimeInterval(seconds).timeIntervalSince1970,
                                    replyTo: nil, images: images, mentionsSelf: false, isOwner: false,
                                    mentionedUsers: [])
            let timeline = [
                QQMemoryEvent(id: "m1", subject: "MEMBER_a", text: candidate.text, at: now),
                QQMemoryEvent(id: "m2", subject: "MEMBER_a", text: text, at: now.addingTimeInterval(seconds)),
            ]
            return QQProactiveDecisionFreshness.vetoReason(candidate: candidate, timeline: timeline,
                                                            messages: ["m1": candidate, "m2": suffix])
        }
        #expect(reason("我觉得挺好", seconds: 2) == nil)
        #expect(reason("这个方案可以再优化", seconds: 3) == nil)
        #expect(reason("算了先不聊", seconds: 2) == "suffix_content_changed")
        #expect(reason("我觉得挺好", seconds: 4) == "suffix_delayed")
    }

    @Test func freshnessVetoesOtherSpeakersMentionsAndReplies() {
        let candidate = QQIncoming(id: "m1", sender: "20002", target: "10001", group: true,
                                   text: "有人知道吗？", time: now.timeIntervalSince1970,
                                   replyTo: nil, images: [], mentionsSelf: false, isOwner: false,
                                   mentionedUsers: [])
        func suffix(sender: String = "20002", mentionSelf: Bool = false,
                    mentions: [String] = [], replyTo: String? = nil) -> QQIncoming {
            QQIncoming(id: "m2", sender: sender, target: "10001", group: true,
                       text: "嗯", time: now.addingTimeInterval(1).timeIntervalSince1970,
                       replyTo: replyTo, images: [], mentionsSelf: mentionSelf,
                       isOwner: false, mentionedUsers: mentions)
        }
        func veto(_ message: QQIncoming, subject: String = "MEMBER_a") -> String? {
            let timeline = [
                QQMemoryEvent(id: "m1", subject: "MEMBER_a", text: candidate.text, at: now),
                QQMemoryEvent(id: "m2", subject: subject, text: message.text, at: now.addingTimeInterval(1)),
            ]
            return QQProactiveDecisionFreshness.vetoReason(
                candidate: candidate, timeline: timeline, messages: ["m1": candidate, "m2": message]
            )
        }
        #expect(veto(suffix(sender: "20003"), subject: "MEMBER_b") == "suffix_speaker_changed")
        #expect(veto(suffix(mentionSelf: true)) == "suffix_self_mention")
        #expect(veto(suffix(mentions: ["20003"])) == "suffix_other_mention")
        #expect(veto(suffix(mentions: ["all"])) == "suffix_all_mention")
        #expect(veto(suffix(replyTo: "someone")) == "suffix_reply_target")
        let sameTopicFromOther = QQIncoming(id: "m2", sender: "20003", target: "10001", group: true,
                                            text: "这个方案可以", time: now.addingTimeInterval(2).timeIntervalSince1970,
                                            replyTo: nil, images: [], mentionsSelf: false, isOwner: false,
                                            mentionedUsers: [])
        let otherSpeakerTimeline = [
            QQMemoryEvent(id: "m1", subject: "MEMBER_a", text: candidate.text, at: now),
            QQMemoryEvent(id: "m2", subject: "MEMBER_b", text: sameTopicFromOther.text, at: now.addingTimeInterval(2)),
        ]
        #expect(QQProactiveDecisionFreshness.vetoReason(candidate: candidate, timeline: otherSpeakerTimeline,
                                                         messages: ["m1": candidate, "m2": sameTopicFromOther]) == "suffix_speaker_changed")
    }

    @Test func liveDecisionConfigMergesFieldsAndKeepsStrictRanges() throws {
        let fallback = ["group:10001": QQDecisionRuntimeSettings(mode: .enforce, threshold: 0.71,
                                                                    dailyBudget: 60, freshnessSeconds: 3)]
        let merged = try #require(QQDecisionRuntimeSettings.parse(
            Data(#"{"group:10001":{"threshold":0.82,"freshnessSeconds":2}}"#.utf8), defaults: fallback))
        #expect(merged["group:10001"]?.mode == .enforce)
        #expect(merged["group:10001"]?.threshold == 0.82)
        #expect(merged["group:10001"]?.dailyBudget == 60)
        #expect(merged["group:10001"]?.freshnessSeconds == 2)
        #expect(QQDecisionRuntimeSettings.parse(Data(#"{"group:10001":{"threshold":1.2}}"#.utf8), defaults: fallback) == nil)
        #expect(QQDecisionRuntimeSettings.parse(Data(#"{"group:10001":{"freshnessSeconds":4}}"#.utf8), defaults: fallback) == nil)
        let envExplicit = QQDecisionRuntimeSettings.environment([
            "DAFEIYU_REPLY_DECISION_THRESHOLD": "0.65",
            "DAFEIYU_DECISION_CONFIG": #"{"group:10001":{"mode":"enforce","threshold":0.84}}"#,
        ])
        #expect(envExplicit["group:10001"]?.threshold == 0.84)
        let envFallback = QQDecisionRuntimeSettings.environment([
            "DAFEIYU_REPLY_DECISION_THRESHOLD": "0.65",
            "DAFEIYU_DECISION_CONFIG": #"{"group:10001":{"mode":"enforce"}}"#,
        ])
        #expect(envFallback["group:10001"]?.threshold == 0.65)
    }

    @Test func proactiveRepliesCanAnchorOnlyToValidOriginalOneBotID() throws {
        let (action, base) = QQPolicy.sendAction(target: QQTarget(number: "10001", name: "fixture", group: true), text: "合成回复")
        #expect(action == "send_group_msg")
        let anchored = try #require(QQProactiveReplyReference.add(messageID: "12345", to: base))
        let segments = try #require(anchored["message"] as? [[String: Any]])
        #expect(segments.first?["type"] as? String == "reply")
        #expect((segments.first?["data"] as? [String: String])?["id"] == "12345")
        #expect(QQProactiveReplyReference.add(messageID: "forged", to: base) == nil)
        #expect(QQProactiveReplyReference.add(messageID: "0", to: base) == nil)
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
