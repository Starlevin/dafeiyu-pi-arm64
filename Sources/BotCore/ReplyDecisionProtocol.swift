import Foundation

// ReplyDecisionProtocol v1 — pure, Foundation-only, no app dependencies.
// Contract: ../../PROTOCOL_REPLY_DECISION_V1.md
// Principle: the candidate model only scores; any failure means no proactive speech.

public enum ReplyChatScope: String, Codable, Sendable {
    case group
    case privateChat = "private"
}

public enum ReplyMentionRelation: String, Codable, Sendable {
    case none
    case selfMention = "self"
    case replyToBot = "reply_to_bot"
    case other
}

public enum ReplyDecisionAction: String, Codable, Sendable {
    case speak = "SPEAK"
    case wait = "WAIT"
    case skip = "SKIP"
}

public enum ReplyDecisionReason: String, Codable, Sendable {
    case mustAnswer = "must_answer"
    case quietHours = "quiet_hours"
    case dailyBudget = "daily_budget"
    case addressedToOther = "addressed_to_other"
    case repeatSuppressed = "repeat_suppressed"
    case recentlySpoke = "recently_spoke"
    case highInterruptCost = "high_interrupt_cost"
    case emptyContext = "empty_context"
    case heuristicLow = "heuristic_low"
    case heuristicHold = "heuristic_hold"
    case scorerUnavailable = "scorer_unavailable"
    case belowThreshold = "below_threshold"
    case modelPass = "model_pass"
    case heuristicPass = "heuristic_pass"
}

public enum ReplyDecisionSource: String, Codable, Sendable {
    case mustAnswer = "must_answer"
    case heuristic
    case model
    case failClosed = "fail_closed"
}

public struct ReplyDecisionResult: Codable, Sendable, Equatable {
    public var action: ReplyDecisionAction
    public var score: Double
    public var reason: ReplyDecisionReason
    public var ttlSeconds: Int
    public var source: ReplyDecisionSource
    public var heuristicScore: Double?
    public var scorerCalled: Bool

    public init(action: ReplyDecisionAction, score: Double, reason: ReplyDecisionReason,
                ttlSeconds: Int, source: ReplyDecisionSource,
                heuristicScore: Double? = nil, scorerCalled: Bool = false) {
        self.action = action
        self.score = score
        self.reason = reason
        self.ttlSeconds = ttlSeconds
        self.source = source
        self.heuristicScore = heuristicScore
        self.scorerCalled = scorerCalled
    }
}

public struct ReplyDecisionInput: Codable, Sendable, Equatable {
    public var scope: ReplyChatScope
    public var chatAlias: String
    public var window: [String]
    public var mention: ReplyMentionRelation
    public var unansweredQuestion: Bool
    public var questionToAll: Bool
    public var invited: Bool
    public var addressedToOther: Bool
    public var botSpokeRecently: Bool
    public var secondsSinceBotReply: Int
    public var secondsSinceTrigger: Int
    public var repeatedTopic: Bool
    public var interruptCost: Double
    public var topicRelevance: Double
    public var isOwner: Bool
    public var quietHours: Bool
    public var dailyRemaining: Int

    public init(scope: ReplyChatScope = .group,
                chatAlias: String,
                window: [String] = [],
                mention: ReplyMentionRelation = .none,
                unansweredQuestion: Bool = false,
                questionToAll: Bool = false,
                invited: Bool = false,
                addressedToOther: Bool = false,
                botSpokeRecently: Bool = false,
                secondsSinceBotReply: Int = 86_400,
                secondsSinceTrigger: Int = 0,
                repeatedTopic: Bool = false,
                interruptCost: Double = 0.2,
                topicRelevance: Double = 0.3,
                isOwner: Bool = false,
                quietHours: Bool = false,
                dailyRemaining: Int = 30) {
        self.scope = scope
        self.chatAlias = chatAlias
        self.window = window
        self.mention = mention
        self.unansweredQuestion = unansweredQuestion
        self.questionToAll = questionToAll
        self.invited = invited
        self.addressedToOther = addressedToOther
        self.botSpokeRecently = botSpokeRecently
        self.secondsSinceBotReply = max(0, secondsSinceBotReply)
        self.secondsSinceTrigger = max(0, secondsSinceTrigger)
        self.repeatedTopic = repeatedTopic
        self.interruptCost = Self.clamp01(interruptCost)
        self.topicRelevance = Self.clamp01(topicRelevance)
        self.isOwner = isOwner
        self.quietHours = quietHours
        self.dailyRemaining = max(0, dailyRemaining)
    }

    /// 把窗口规范化为最后 N 条、每条截断、去空行。调用方仍须保证内容已脱敏。
    public mutating func normalizeWindow(maxCount: Int = 8, maxCharacters: Int = 125) {
        window = Array(window.suffix(max(maxCount, 0)).map {
            String($0.trimmingCharacters(in: .whitespacesAndNewlines).prefix(maxCharacters))
        }.filter { !$0.isEmpty })
    }

    public var isMustAnswer: Bool {
        scope == .privateChat || mention == .selfMention || mention == .replyToBot || invited
    }

    static func clamp01(_ value: Double) -> Double {
        guard value.isFinite else { return 0 }
        return min(1, max(0, value))
    }
}

public struct ReplyDecisionLimits: Codable, Sendable, Equatable {
    public var threshold: Double
    public var waitBand: Double
    public var dailyLimit: Int
    public var recentSpeakSuppressSeconds: Int
    public var highInterruptCost: Double
    public var waitTTLSeconds: Int
    public var maxWindow: Int
    public var maxCharactersPerMessage: Int

    public init(threshold: Double = 0.70, waitBand: Double = 0.12, dailyLimit: Int = 30,
                recentSpeakSuppressSeconds: Int = 90, highInterruptCost: Double = 0.70,
                waitTTLSeconds: Int = 45, maxWindow: Int = 8, maxCharactersPerMessage: Int = 125) {
        self.threshold = min(1, max(0, threshold))
        self.waitBand = min(1, max(0, waitBand))
        self.dailyLimit = max(0, dailyLimit)
        self.recentSpeakSuppressSeconds = max(0, recentSpeakSuppressSeconds)
        self.highInterruptCost = min(1, max(0, highInterruptCost))
        self.waitTTLSeconds = max(0, waitTTLSeconds)
        self.maxWindow = max(1, maxWindow)
        self.maxCharactersPerMessage = max(1, maxCharactersPerMessage)
    }

    /// 与现有 QQ 档位对齐的起点；仍由调用方按群档案覆盖。
    public static func natural(threshold: Double = 0.70, dailyLimit: Int = 30) -> ReplyDecisionLimits {
        ReplyDecisionLimits(threshold: threshold, dailyLimit: dailyLimit)
    }
    public static func quiet(threshold: Double = 0.70, dailyLimit: Int = 3) -> ReplyDecisionLimits {
        ReplyDecisionLimits(threshold: threshold, dailyLimit: dailyLimit, highInterruptCost: 0.55)
    }
    public static func active(threshold: Double = 0.70, dailyLimit: Int = 15) -> ReplyDecisionLimits {
        ReplyDecisionLimits(threshold: threshold, waitBand: 0.10, dailyLimit: dailyLimit)
    }
}

public struct ReplyDecisionState: Codable, Sendable, Equatable {
    public var dayKey: Int
    public var decisions: Int
    public var speaks: Int
    public var scorerCalls: Int
    public var lastSpeakAt: Int
    public var lastDecisionAt: Int
    public var recentSpeakTexts: [String]

    public init(dayKey: Int, decisions: Int = 0, speaks: Int = 0, scorerCalls: Int = 0,
                lastSpeakAt: Int = 0, lastDecisionAt: Int = 0, recentSpeakTexts: [String] = []) {
        self.dayKey = dayKey
        self.decisions = decisions
        self.speaks = speaks
        self.scorerCalls = scorerCalls
        self.lastSpeakAt = lastSpeakAt
        self.lastDecisionAt = lastDecisionAt
        self.recentSpeakTexts = recentSpeakTexts
    }

    public static func dayKey(for timestamp: Int) -> Int {
        Int(floor(Double(timestamp + 28_800) / 86_400))
    }

    public mutating func rollDay(to timestamp: Int) {
        let key = Self.dayKey(for: timestamp)
        guard key != dayKey else { return }
        dayKey = key
        decisions = 0
        speaks = 0
        scorerCalls = 0
        lastSpeakAt = 0
        lastDecisionAt = 0
        recentSpeakTexts = []
    }
}

public protocol ReplyScoring: Sendable {
    /// 只评分，不生成正文。返回 nil / NaN / 越界均视为不可用。
    func score(_ input: ReplyDecisionInput) async -> Double?
}

public enum ReplyDecisionText {
    /// 归一化用于「是否刚说过同一句」的完全匹配，不做语义近似。
    public static func normalize(_ text: String) -> String {
        let lowered = text.lowercased()
        // Keep Unicode letters and numbers only; this removes CJK punctuation
        // consistently across Foundation/Darwin and corelibs on Linux.
        let kept = lowered.unicodeScalars.filter { CharacterSet.alphanumerics.contains($0) }
        return String(String.UnicodeScalarView(kept))
    }
}

public struct ReplyDecisionEngine: Sendable {
    public let limits: ReplyDecisionLimits

    public init(limits: ReplyDecisionLimits = ReplyDecisionLimits()) {
        self.limits = limits
    }

    public func decide(_ rawInput: ReplyDecisionInput, state rawState: ReplyDecisionState,
                       now: Int, scorer: (any ReplyScoring)? = nil) async -> (ReplyDecisionResult, ReplyDecisionState) {
        var input = rawInput
        input.normalizeWindow(maxCount: limits.maxWindow, maxCharacters: limits.maxCharactersPerMessage)
        var state = rawState
        state.rollDay(to: now)
        state.decisions += 1
        state.lastDecisionAt = now

        // 1. Must-answer bypass: never consumed by proactive budget, never scored.
        if input.isMustAnswer {
            return (ReplyDecisionResult(action: .speak, score: 1, reason: .mustAnswer,
                                        ttlSeconds: 0, source: .mustAnswer), state)
        }

        // 2. Hard gates.
        if input.quietHours {
            return (skip(.quietHours, state), state)
        }
        if input.dailyRemaining <= 0 || state.speaks >= limits.dailyLimit {
            return (skip(.dailyBudget, state), state)
        }
        if input.addressedToOther {
            return (skip(.addressedToOther, state), state)
        }
        if input.repeatedTopic && input.botSpokeRecently {
            return (skip(.repeatSuppressed, state), state)
        }
        if input.botSpokeRecently && input.secondsSinceBotReply < limits.recentSpeakSuppressSeconds && !input.unansweredQuestion {
            return (skip(.recentlySpoke, state), state)
        }
        if input.interruptCost >= limits.highInterruptCost {
            return (skip(.highInterruptCost, state), state)
        }
        guard !input.window.isEmpty else {
            return (skip(.emptyContext, state), state)
        }

        // 3. Local heuristic (no model call).
        let heuristic = heuristicScore(input)
        let gate = round4(limits.threshold - limits.waitBand)
        if heuristic < gate {
            var result = skip(.heuristicLow, state)
            result.heuristicScore = heuristic
            return (result, state)
        }

        // 4. Optional scorer. Failure is closed.
        if let scorer {
            state.scorerCalls += 1
            let raw = await scorer.score(input)
            guard let raw, raw.isFinite, (0...1).contains(raw) else {
                return (ReplyDecisionResult(action: .skip, score: 0, reason: .scorerUnavailable,
                                            ttlSeconds: 0, source: .failClosed,
                                            heuristicScore: heuristic, scorerCalled: true), state)
            }
            let score = round4(raw)
            if score >= limits.threshold {
                return (ReplyDecisionResult(action: .speak, score: score, reason: .modelPass,
                                            ttlSeconds: 0, source: .model,
                                            heuristicScore: heuristic, scorerCalled: true), state)
            }
            return (ReplyDecisionResult(action: .wait, score: score, reason: .belowThreshold,
                                        ttlSeconds: limits.waitTTLSeconds, source: .model,
                                        heuristicScore: heuristic, scorerCalled: true), state)
        }

        if heuristic >= limits.threshold {
            return (ReplyDecisionResult(action: .speak, score: heuristic, reason: .heuristicPass,
                                        ttlSeconds: 0, source: .heuristic,
                                        heuristicScore: heuristic, scorerCalled: false), state)
        }
        return (ReplyDecisionResult(action: .wait, score: heuristic, reason: .heuristicHold,
                                    ttlSeconds: limits.waitTTLSeconds, source: .heuristic,
                                    heuristicScore: heuristic, scorerCalled: false), state)
    }

    /// 只有确认发送后调用；被拦截、失败、撤回都不占额度。
    public func recordSpeak(_ text: String, at now: Int, state: inout ReplyDecisionState) {
        state.rollDay(to: now)
        state.speaks += 1
        state.lastSpeakAt = now
        let normalized = ReplyDecisionText.normalize(text)
        guard !normalized.isEmpty else { return }
        if let existing = state.recentSpeakTexts.firstIndex(of: normalized) {
            state.recentSpeakTexts.remove(at: existing)
        }
        state.recentSpeakTexts.append(normalized)
        if state.recentSpeakTexts.count > 8 {
            state.recentSpeakTexts.removeFirst(state.recentSpeakTexts.count - 8)
        }
    }

    public func heuristicScore(_ input: ReplyDecisionInput) -> Double {
        var value = 0.10
        if input.unansweredQuestion { value += 0.28 }
        if input.questionToAll { value += 0.10 }
        value += 0.22 * input.topicRelevance
        value -= 0.30 * input.interruptCost
        if input.botSpokeRecently { value -= 0.12 }
        if input.repeatedTopic { value -= 0.30 }
        if input.window.count <= 1 { value -= 0.05 }
        return round4(ReplyDecisionInput.clamp01(value))
    }

    private func skip(_ reason: ReplyDecisionReason, _ state: ReplyDecisionState) -> ReplyDecisionResult {
        ReplyDecisionResult(action: .skip, score: 0, reason: reason, ttlSeconds: 0, source: .heuristic)
    }

    private func round4(_ value: Double) -> Double {
        guard value.isFinite else { return 0 }
        return (value * 10_000).rounded() / 10_000
    }
}
