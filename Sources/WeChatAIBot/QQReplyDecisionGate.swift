import Foundation
import BotCore

/// Opt-in adapter between the existing QQ proactive path and ReplyDecision Protocol v1.
/// Default: off. `off` leaves every existing code path byte-for-byte unchanged.
struct QQReplyDecisionSettings: Sendable {
    enum Mode: String, Sendable { case off, shadow, enforce }

    let mode: Mode
    let thresholdOverride: Double?
    let dailyLimitOverride: Int?
    let scoreInShadow: Bool
    let personaGuard: PersonaGuardMode

    enum PersonaGuardMode: String, Sendable { case off, proactive, all }

    static func load(_ env: [String: String] = ProcessInfo.processInfo.environment) -> Self {
        let mode = Mode(rawValue: (env["DAFEIYU_REPLY_DECISION_V2"] ?? "off").lowercased()) ?? .off
        let threshold = env["DAFEIYU_REPLY_DECISION_THRESHOLD"].flatMap(Double.init)
        let daily = env["DAFEIYU_REPLY_DECISION_DAILY"].flatMap(Int.init)
        let score = ["1", "true", "yes"].contains((env["DAFEIYU_REPLY_DECISION_SCORE"] ?? "").lowercased())
        let guardMode = PersonaGuardMode(rawValue: (env["DAFEIYU_PERSONA_GUARD"] ?? "off").lowercased()) ?? .off
        return Self(mode: mode,
                    thresholdOverride: threshold.map { min(0.95, max(0.30, $0)) },
                    dailyLimitOverride: daily.map { min(200, max(1, $0)) },
                    scoreInShadow: score,
                    personaGuard: guardMode)
    }

    var enabled: Bool { mode != .off }
    var enforces: Bool { mode == .enforce }
}

/// Single source of truth for the receive() return condition.
/// Legacy enforce already owns the enqueue; v2 enforce owns it too, so returning here
/// prevents the legacy shadow path and v2 from enqueueing the same work twice.
func suppressesLegacyEnqueue(legacyMode: QQDecisionSettings.Mode,
                              reply: QQReplyDecisionSettings) -> Bool {
    legacyMode == .enforce || reply.enforces
}

struct QQReplyDecisionTrace: Sendable {
    let input: ReplyDecisionInput
    let result: ReplyDecisionResult
}

/// Only scores, never writes text. A nil bearer or any transport failure returns nil,
/// which the protocol turns into a fail-closed SKIP.
struct QQProactiveScorer: ReplyScoring {
    let context: [String]
    let bearer: String?

    func score(_ input: ReplyDecisionInput) async -> Double? {
        guard let bearer, !bearer.isEmpty else { return nil }
        return await QQProactiveDecision.score(context: context, bearer: bearer)
    }
}

@MainActor
final class QQReplyDecisionGate {
    private let engine = ReplyDecisionEngine()
    private var states: [String: ReplyDecisionState] = [:]
    private var inFlight: Set<String> = []

    func limits(for profile: QQScopedProactiveProfile, settings: QQReplyDecisionSettings) -> ReplyDecisionLimits {
        ReplyDecisionLimits(threshold: settings.thresholdOverride ?? 0.70,
                            dailyLimit: settings.dailyLimitOverride ?? profile.dailyLimit)
    }

    func decide(message: QQIncoming, context: QQGroupContext, profile: QQScopedProactiveProfile,
                settings: QQReplyDecisionSettings,
                now: Date = Date(), scorer: (any ReplyScoring)? = nil) async -> QQReplyDecisionTrace? {
        let key = message.key
        guard !inFlight.contains(key) else { return nil }
        inFlight.insert(key)
        defer { inFlight.remove(key) }

        var state = states[key] ?? ReplyDecisionState(dayKey: ReplyDecisionState.dayKey(for: Int(now.timeIntervalSince1970)))
        // Roll the day before deriving dailyRemaining; otherwise a stale 0 from the
        // previous day would make every candidate skip on the first message of a new day.
        state.rollDay(to: Int(now.timeIntervalSince1970))
        let lim = limits(for: profile, settings: settings)
        let input = QQReplyDecisionInputBuilder.build(message: message, context: context, profile: profile,
                                                      now: now, dailyRemaining: max(0, lim.dailyLimit - state.speaks))
        let decisionEngine = ReplyDecisionEngine(limits: lim)
        let (result, next) = await decisionEngine.decide(input, state: state, now: Int(now.timeIntervalSince1970), scorer: scorer)
        state = next
        states[key] = state
        return QQReplyDecisionTrace(input: input, result: result)
    }

    func recordSpeak(chatKey: String, text: String, at now: Date = Date()) {
        var state = states[chatKey] ?? ReplyDecisionState(dayKey: ReplyDecisionState.dayKey(for: Int(now.timeIntervalSince1970)))
        state.rollDay(to: Int(now.timeIntervalSince1970))
        engine.recordSpeak(text, at: Int(now.timeIntervalSince1970), state: &state)
        states[chatKey] = state
    }
}

enum QQReplyDecisionInputBuilder {
    static func build(message: QQIncoming, context: QQGroupContext, profile: QQScopedProactiveProfile,
                      now: Date, dailyRemaining: Int) -> ReplyDecisionInput {
        let text = message.text.trimmingCharacters(in: .whitespacesAndNewlines)
        let mentionedOther = message.mentionedUsers.contains { $0 != "all" }
        let question = containsQuestion(text)
        let invited = containsInvite(text) && text.contains("大肥鱼")
        let lastBot = context.lastBotEvent
        let secondsSinceBot = lastBot.map { max(0, Int(now.timeIntervalSince($0.at))) } ?? 86_400
        let repeated = lastBot.map { last in
            secondsSinceBot <= 300 && bigramOverlap(text, last.text) >= 2
        } ?? false

        let mention: ReplyMentionRelation
        let replyToBot = message.replyTo.map { context.speaker(for: $0) == "BOT" } ?? false
        if message.mentionsSelf { mention = .selfMention }
        else if replyToBot { mention = .replyToBot }
        else if message.replyTo != nil || mentionedOther { mention = .other }
        else { mention = .none }

        // A raw replyTo only proves "replying to some message"; it does not prove the
        // author is BOT. Unknown references are treated as addressed-to-other (safe).
        let addressedToOther = (mentionedOther || (message.replyTo != nil && !replyToBot)) && !message.mentionsSelf
        let questionToAll = question && !message.mentionsSelf && !mentionedOther
        let interrupt = interruptCost(text: text, addressedToOther: addressedToOther,
                                      hasRecentMedia: context.hasRecentMedia, isQuestion: question)
        let relevance = topicRelevance(text: text, questionToAll: questionToAll,
                                       hasUnanswered: context.hasUnansweredText)

        return ReplyDecisionInput(
            scope: message.group ? .group : .privateChat,
            chatAlias: message.key,
            window: QQProactiveDecision.context(context.eventsText(limit: 8)),
            mention: mention,
            unansweredQuestion: context.hasUnansweredText,
            questionToAll: questionToAll,
            invited: invited,
            addressedToOther: addressedToOther,
            botSpokeRecently: secondsSinceBot < 90,
            secondsSinceBotReply: secondsSinceBot,
            secondsSinceTrigger: max(0, Int(now.timeIntervalSince1970 - message.time)),
            repeatedTopic: repeated,
            interruptCost: interrupt,
            topicRelevance: relevance,
            isOwner: message.isOwner,
            quietHours: profile.inQuietHours(now),
            dailyRemaining: dailyRemaining
        )
    }

    static func containsQuestion(_ text: String) -> Bool {
        let markers = ["?", "？", "吗", "怎么", "为什么", "谁", "哪里", "多少", "能不能", "可不可以", "求", "帮我看", "有没有"]
        return markers.contains { text.contains($0) }
    }

    static func containsInvite(_ text: String) -> Bool {
        ["出来说", "别潜水", "说两句", "聊两句", "出来聊", "别装死"].contains { text.contains($0) }
    }

    static func interruptCost(text: String, addressedToOther: Bool, hasRecentMedia: Bool, isQuestion: Bool) -> Double {
        if addressedToOther { return 0.85 }
        if hasRecentMedia && text.count < 8 { return 0.75 }
        if ["别吵", "闭嘴", "吵死", "生气", "难过", "哭了", "分手", "吵架", "滚"].contains(where: { text.contains($0) }) { return 0.72 }
        if text.contains("你") && !isQuestion { return 0.55 }
        if isQuestion { return 0.15 }
        if text.count <= 4 { return 0.50 }
        return 0.35
    }

    static func topicRelevance(text: String, questionToAll: Bool, hasUnanswered: Bool) -> Double {
        if questionToAll { return 0.85 }
        if hasUnanswered { return 0.60 }
        if text.count >= 10 { return 0.45 }
        return 0.25
    }

    static func bigramOverlap(_ lhs: String, _ rhs: String) -> Int {
        let a = bigrams(lhs), b = bigrams(rhs)
        guard !a.isEmpty, !b.isEmpty else { return 0 }
        return Set(a).intersection(Set(b)).count
    }

    private static func bigrams(_ text: String) -> [String] {
        let compact = text.lowercased().filter { $0.isLetter || $0.isNumber }
        guard compact.count >= 2 else { return [] }
        let chars = Array(compact)
        return (0..<(chars.count - 1)).map { String(chars[$0...$0 + 1]) }
    }
}

enum QQPersonaRuntimeGuard {
    /// Detection only. `memoryEvidence` must be the text actually recalled for this turn;
    /// the guard requires bigram overlap, so unrelated memory cannot license a shared-history claim.
    static func check(reply text: String, message: QQIncoming, persona: QQPersona,
                      memoryEvidence: String = "") -> PersonaCheckResult {
        var profile = PersonaProfile(displayName: persona.customName ?? "大肥鱼",
                                     relationship: message.isOwner ? .owner : .peer,
                                     maxCharacters: persona.maxCharacters,
                                     ticBudget: 1, emojiBudget: 1,
                                     allowTeasing: persona.effectiveStyle == .teasing)
        profile.relationship = message.isOwner ? .owner : .peer

        let scenario: PersonaScenario
        if text.contains("你是谁") || text.contains("是不是人") || text.contains("AI") {
            scenario = .identity
        } else if QQReplyDecisionInputBuilder.containsQuestion(text) {
            scenario = .help
        } else if ["早", "晚安", "睡了"].contains(where: { text.contains($0) }) {
            scenario = .greeting
        } else if ["难过", "哭", "分手", "吵架", "生病"].contains(where: { text.contains($0) }) {
            scenario = .comfort
        } else if ["哈哈", "就这", "典", "绷"].contains(where: { text.contains($0) }) {
            scenario = .banter
        } else {
            scenario = .casual
        }
        let hasEvidence = PersonaGuard.hasSupportingMemoryEvidence(text, memory: memoryEvidence)
        return PersonaGuard.check(text, profile: profile, scenario: scenario,
                                  context: PersonaCheckContext(hasMemoryEvidence: hasEvidence, recentReplies: []))
    }
}
