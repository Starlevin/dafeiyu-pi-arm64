import Foundation
import CoreFoundation
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

    func applying(groupThreshold: Double) -> Self {
        Self(mode: mode, thresholdOverride: min(0.95, max(0.30, groupThreshold)),
             dailyLimitOverride: dailyLimitOverride, scoreInShadow: scoreInShadow, personaGuard: personaGuard)
    }
}

/// Per-group operator settings reloaded from a bounded JSON file. Invalid files
/// are ignored by the caller, which retains the last verified snapshot.
struct QQDecisionRuntimeSettings: Sendable {
    let mode: QQDecisionSettings.Mode
    let threshold: Double
    let dailyBudget: Int
    let freshnessSeconds: Double
    let publicQuestionEnabled: Bool
    let publicQuestionCooldownSeconds: Int
    let ordinaryReplyEnabled: Bool?
    private let hasGroupThreshold: Bool

    /// An explicit ordinary-reply switch takes precedence over the old pilot rule.
    var admitsOrdinarySwiftScoring: Bool {
        mode == .enforce && (ordinaryReplyEnabled
            ?? (publicQuestionEnabled && hasGroupThreshold && threshold == 0.45))
    }

    init(mode: QQDecisionSettings.Mode, threshold: Double, dailyBudget: Int, freshnessSeconds: Double,
         publicQuestionEnabled: Bool = false, publicQuestionCooldownSeconds: Int = 60,
         hasGroupThreshold: Bool = false, ordinaryReplyEnabled: Bool? = nil) {
        self.mode = mode
        self.threshold = threshold
        self.dailyBudget = dailyBudget
        self.freshnessSeconds = freshnessSeconds
        self.publicQuestionEnabled = publicQuestionEnabled
        self.publicQuestionCooldownSeconds = publicQuestionCooldownSeconds
        self.ordinaryReplyEnabled = ordinaryReplyEnabled
        self.hasGroupThreshold = hasGroupThreshold
    }

    static func environment(_ env: [String: String] = ProcessInfo.processInfo.environment) -> [String: Self] {
        let global = Double(env["DAFEIYU_REPLY_DECISION_THRESHOLD"] ?? "").flatMap { (0...1).contains($0) ? $0 : nil } ?? 0.70
        guard let raw = env["DAFEIYU_DECISION_CONFIG"], raw.utf8.count <= 4096,
              let data = raw.data(using: .utf8) else { return [:] }
        return parse(data, defaultThreshold: global) ?? [:]
    }

    static func parse(_ data: Data, defaults: [String: Self] = [:], defaultThreshold: Double = 0.70) -> [String: Self]? {
        guard data.count <= 16_384,
              let object = try? JSONSerialization.jsonObject(with: data) as? [String: [String: Any]],
              !object.isEmpty else { return nil }
        var result: [String: Self] = [:]
        for (key, value) in object {
            let id = key.dropFirst(6)
            guard key.hasPrefix("group:"), (1...20).contains(id.utf8.count),
                  id.utf8.allSatisfy({ (48...57).contains($0) }) else { return nil }
            let base = defaults[key] ?? Self(mode: .off, threshold: defaultThreshold, dailyBudget: 60, freshnessSeconds: 3)
            let mode: QQDecisionSettings.Mode
            if let raw = value["mode"] {
                guard let string = raw as? String, let parsed = QQDecisionSettings.Mode(rawValue: string) else { return nil }
                mode = parsed
            } else { mode = base.mode }
            let threshold: Double
            if let raw = value["threshold"] {
                guard let number = raw as? NSNumber, CFGetTypeID(number) != CFBooleanGetTypeID(),
                      (0...1).contains(number.doubleValue) else { return nil }
                threshold = number.doubleValue
            } else { threshold = base.threshold }
            let budget: Int
            if let raw = value["dailyBudget"] {
                guard let number = raw as? NSNumber, CFGetTypeID(number) != CFBooleanGetTypeID(),
                      number.doubleValue.rounded() == number.doubleValue,
                      (1...500).contains(number.intValue) else { return nil }
                budget = number.intValue
            } else { budget = base.dailyBudget }
            let freshness: Double
            if let raw = value["freshnessSeconds"] {
                guard let number = raw as? NSNumber, CFGetTypeID(number) != CFBooleanGetTypeID(),
                      (1...3).contains(number.doubleValue) else { return nil }
                freshness = number.doubleValue
            } else { freshness = base.freshnessSeconds }
            let publicQuestionEnabled: Bool
            if let raw = value["publicQuestionEnabled"] {
                guard let number = raw as? NSNumber, CFGetTypeID(number) == CFBooleanGetTypeID() else { return nil }
                publicQuestionEnabled = number.boolValue
            } else { publicQuestionEnabled = base.publicQuestionEnabled }
            let publicQuestionCooldownSeconds: Int
            if let raw = value["publicQuestionCooldownSeconds"] {
                guard let number = raw as? NSNumber, CFGetTypeID(number) != CFBooleanGetTypeID(),
                      number.doubleValue.rounded() == number.doubleValue,
                      (30...1200).contains(number.intValue) else { return nil }
                publicQuestionCooldownSeconds = number.intValue
            } else { publicQuestionCooldownSeconds = base.publicQuestionCooldownSeconds }
            let ordinaryReplyEnabled: Bool?
            if let raw = value["ordinaryReplyEnabled"] {
                guard let number = raw as? NSNumber, CFGetTypeID(number) == CFBooleanGetTypeID() else { return nil }
                ordinaryReplyEnabled = number.boolValue
            } else { ordinaryReplyEnabled = base.ordinaryReplyEnabled }
            result[key] = Self(mode: mode, threshold: threshold, dailyBudget: budget, freshnessSeconds: freshness,
                               publicQuestionEnabled: publicQuestionEnabled,
                               publicQuestionCooldownSeconds: publicQuestionCooldownSeconds,
                               hasGroupThreshold: value["threshold"] != nil || base.hasGroupThreshold,
                               ordinaryReplyEnabled: ordinaryReplyEnabled)
        }
        return result
    }
}

/// Admission for the QQ public-question fast lane. Every condition is explicit so
/// the ordinary group-participation setting cannot broaden this opt-in.
enum QQPublicQuestionAdmission {
    enum Route: Equatable {
        case existing
        case publicQuestion
        case ordinary
        case ignore(Rejection)
    }

    enum Rejection: String {
        case mentionPresent = "mention_present"
        case quotedReply = "quoted_reply"
        case mediaPresent = "media_present"
        case noUnansweredContext = "no_unanswered_context"
        case notPublicQuestion = "not_public_question"
    }

    static func pilotApplies(profileAuthorized: Bool, settings: QQDecisionRuntimeSettings?,
                             isGroup: Bool, mentionsSelf: Bool) -> Bool {
        profileAuthorized && settings?.mode == .enforce
            && settings?.publicQuestionEnabled == true && isGroup && !mentionsSelf
    }

    static func route(message: QQIncoming, hasUnansweredContext: Bool, settings: QQDecisionRuntimeSettings?,
                      profileAuthorized: Bool, swiftEnforced: Bool) -> Route {
        guard message.group, !message.mentionsSelf, let settings else { return .existing }
        let publicQuestion = pilotApplies(profileAuthorized: profileAuthorized, settings: settings,
                                          isGroup: message.group, mentionsSelf: message.mentionsSelf)
        guard publicQuestion || settings.ordinaryReplyEnabled != nil else { return .existing }
        let matchesQuestion = publicQuestion && PublicGroupQuestion.matchesForQQ(message.text)
        guard let reason = rejection(mentionedUsers: message.mentionedUsers, hasReply: message.replyTo != nil,
                                     hasImages: !message.images.isEmpty, hasUnansweredContext: hasUnansweredContext,
                                     matchesQuestion: matchesQuestion) else { return .publicQuestion }
        if reason == .notPublicQuestion, profileAuthorized, swiftEnforced, settings.admitsOrdinarySwiftScoring {
            return .ordinary
        }
        return .ignore(reason)
    }

    static func allows(profileAuthorized: Bool, settings: QQDecisionRuntimeSettings?, isGroup: Bool,
                       mentionsSelf: Bool, mentionedUsers: [String], hasReply: Bool,
                       hasImages: Bool, hasUnansweredContext: Bool, matchesQuestion: Bool) -> Bool {
        pilotApplies(profileAuthorized: profileAuthorized, settings: settings, isGroup: isGroup,
                     mentionsSelf: mentionsSelf)
            && rejection(mentionedUsers: mentionedUsers, hasReply: hasReply, hasImages: hasImages,
                         hasUnansweredContext: hasUnansweredContext, matchesQuestion: matchesQuestion) == nil
    }

    /// The first content gate that declined an otherwise eligible group message.
    static func rejection(mentionedUsers: [String], hasReply: Bool, hasImages: Bool,
                          hasUnansweredContext: Bool, matchesQuestion: Bool) -> Rejection? {
        if !mentionedUsers.isEmpty { return .mentionPresent }
        if hasReply { return .quotedReply }
        if hasImages { return .mediaPresent }
        if !hasUnansweredContext { return .noUnansweredContext }
        return matchesQuestion ? nil : .notPublicQuestion
    }

    static func candidateIsStillLatest(candidateID: String, latestID: String?) -> Bool {
        latestID == candidateID
    }

    static func candidateWithinLifetime(messageTime: TimeInterval, now: TimeInterval,
                                        maxAge: TimeInterval = 120) -> Bool {
        now - messageTime <= maxAge
    }

    static func candidateIsChronologicallyLatest(messageTime: TimeInterval,
                                                  previousMessageTimes: [TimeInterval]) -> Bool {
        guard let previousLatest = previousMessageTimes.max() else { return true }
        return messageTime >= previousLatest
    }
}

func publicQuestionKnowledgeFacts(from promptFragment: String) -> [String] {
    var facts: [String] = []
    for line in promptFragment.split(separator: "\n") {
        guard line.hasPrefix("- ") else { continue }
        let fact = String(line.dropFirst(2)).trimmingCharacters(in: .whitespacesAndNewlines)
        guard !fact.isEmpty else { continue }
        facts.append(String(fact.prefix(220)))
        if facts.count == 2 { break }
    }
    return facts
}

func publicQuestionPriorContext(from events: [String]) -> [String] {
    Array(events.dropLast().suffix(3))
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
                now: Date = Date(), scorer: (any ReplyScoring)? = nil,
                skipSoftPrefilters: Bool = false) async -> QQReplyDecisionTrace? {
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
        let (result, next) = await decisionEngine.decide(input, state: state, now: Int(now.timeIntervalSince1970),
                                                        scorer: scorer, skipSoftPrefilters: skipSoftPrefilters)
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

/// Post-score freshness guard for proactive group replies. It tolerates a narrow
/// same-speaker suffix only when the update is very recent and non-conflicting,
/// or when repeated words prove it continues the candidate's topic.
enum QQProactiveDecisionFreshness {
    private static let innocuousSuffixes: Set<String> = [
        "嗯", "嗯嗯", "哦", "噢", "好", "好的", "收到", "ok", "哈哈", "哈哈哈",
        "呵呵", "lol", "👍", "👌", "😂", "🙂", "😊", "😅"
    ]

    static func vetoReason(candidate: QQIncoming, timeline: [QQMemoryEvent],
                           messages: [String: QQIncoming], freshnessSeconds: Double = 3) -> String? {
        guard let index = timeline.firstIndex(where: { $0.id == candidate.id }) else {
            return "candidate_missing"
        }
        guard let candidateInput = messages[candidate.id],
              candidateInput.sender == candidate.sender,
              candidateInput.key == candidate.key, candidateInput.group else {
            return "candidate_unverified"
        }
        let suffix = timeline.suffix(from: timeline.index(after: index))
        guard suffix.count <= 4 else { return "suffix_too_many" }
        let speaker = timeline[index].subject
        for event in suffix {
            guard event.at >= timeline[index].at else { return "suffix_time_reversed" }
            let age = event.at.timeIntervalSince(timeline[index].at)
            guard age <= freshnessSeconds else { return "suffix_delayed" }
            guard event.subject == speaker else { return "suffix_speaker_changed" }
            guard let message = messages[event.id] else { return "suffix_unverified" }
            guard message.sender == candidate.sender, message.key == candidate.key, message.group else {
                return "suffix_sender_changed"
            }
            guard !message.mentionsSelf else { return "suffix_self_mention" }
            if message.mentionedUsers.contains("all") { return "suffix_all_mention" }
            if !message.mentionedUsers.isEmpty { return "suffix_other_mention" }
            guard message.replyTo == nil else { return "suffix_reply_target" }
            guard message.images.isEmpty else { return "suffix_media" }
            let text = event.text.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
            if innocuousSuffixes.contains(text) { continue }
            // Do not infer topic continuity from generic short words, questions, or
            // explicit disagreement/withdrawal. For ordinary short follow-ups, a
            // 1–3 second window limits the chance that a new thought superseded us.
            let conflicts = ["不聊", "别说", "算了", "不是", "别了", "停", "不要", "反对", "不同意", "？", "?"]
            guard !conflicts.contains(where: { text.contains($0) }) else { return "suffix_content_changed" }
            let overlap = QQReplyDecisionInputBuilder.bigramOverlap(text, candidate.text)
            let shortFresh = age >= 1 && age <= freshnessSeconds && text.count <= 8
            let sameTopic = overlap >= 2
            guard shortFresh || sameTopic else { return "suffix_content_changed" }
        }
        return nil
    }
}

enum QQProactiveReplyReference {
    static func add(messageID: String, to params: [String: Any]) -> [String: Any]? {
        guard let numeric = Int64(messageID), numeric != 0,
              var segments = params["message"] as? [[String: Any]] else { return nil }
        segments.insert(["type": "reply", "data": ["id": String(numeric)]], at: 0)
        var updated = params
        updated["message"] = segments
        return updated
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
