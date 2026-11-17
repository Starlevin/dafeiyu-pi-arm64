import Foundation
import Testing
@testable import BotCore

private struct FixedScorer: ReplyScoring {
    var value: Double?
    func score(_ input: ReplyDecisionInput) async -> Double? { value }
}

private func state(now: Int) -> ReplyDecisionState {
    ReplyDecisionState(dayKey: ReplyDecisionState.dayKey(for: now))
}

private let now = 1_700_000_000

private func base() -> ReplyDecisionInput {
    ReplyDecisionInput(
        scope: .group, chatAlias: "group:A",
        window: ["今晚组队吗？", "缺一个"],
        mention: .none, unansweredQuestion: true, questionToAll: true,
        invited: false, addressedToOther: false,
        botSpokeRecently: false, secondsSinceBotReply: 86_400,
        repeatedTopic: false, interruptCost: 0.2, topicRelevance: 0.8,
        isOwner: false, quietHours: false, dailyRemaining: 30
    )
}

@Test func directMentionAlwaysSpeaksWithoutScoring() async {
    let engine = ReplyDecisionEngine()
    var input = base(); input.mention = .selfMention; input.window = []
    let (result, _) = await engine.decide(input, state: state(now: now), now: now, scorer: FixedScorer(value: 0.1))
    #expect(result.action == .speak)
    #expect(result.reason == .mustAnswer)
    #expect(result.score == 1)
    #expect(result.source == .mustAnswer)
    #expect(result.scorerCalled == false)
}

@Test func privateMessageAlwaysSpeaks() async {
    let engine = ReplyDecisionEngine()
    var input = base(); input.scope = .privateChat; input.chatAlias = "private:X"
    input.quietHours = true; input.dailyRemaining = 0
    let (result, _) = await engine.decide(input, state: state(now: now), now: now, scorer: nil)
    #expect(result.action == .speak)
    #expect(result.reason == .mustAnswer)
}

@Test func hardGatesShortCircuit() async {
    let engine = ReplyDecisionEngine()
    var quiet = base(); quiet.quietHours = true
    #expect((await engine.decide(quiet, state: state(now: now), now: now)).0.reason == .quietHours)
    var budget = base(); budget.dailyRemaining = 0
    #expect((await engine.decide(budget, state: state(now: now), now: now)).0.reason == .dailyBudget)
    var other = base(); other.addressedToOther = true
    #expect((await engine.decide(other, state: state(now: now), now: now)).0.reason == .addressedToOther)
    var recent = base(); recent.unansweredQuestion = false; recent.botSpokeRecently = true; recent.secondsSinceBotReply = 30
    #expect((await engine.decide(recent, state: state(now: now), now: now)).0.reason == .recentlySpoke)
    var repeatTopic = base(); repeatTopic.repeatedTopic = true; repeatTopic.botSpokeRecently = true
    #expect((await engine.decide(repeatTopic, state: state(now: now), now: now)).0.reason == .repeatSuppressed)
    var costly = base(); costly.interruptCost = 0.8
    #expect((await engine.decide(costly, state: state(now: now), now: now)).0.reason == .highInterruptCost)
}

@Test func lowHeuristicSkipsWithoutModelCost() async {
    let engine = ReplyDecisionEngine()
    var low = base()
    low.unansweredQuestion = false; low.questionToAll = false; low.topicRelevance = 0
    low.interruptCost = 0.6; low.window = ["哈哈"]
    var calls = 0
    let scorer = CountingScorer(value: 0.99) { calls += 1 }
    let (result, _) = await engine.decide(low, state: state(now: now), now: now, scorer: scorer)
    #expect(result.reason == .heuristicLow)
    #expect(calls == 0)
}

@Test func scorerFailureAndOutOfRangeFailClosed() async {
    let engine = ReplyDecisionEngine()
    let (nilResult, _) = await engine.decide(base(), state: state(now: now), now: now, scorer: FixedScorer(value: nil))
    #expect(nilResult.action == .skip)
    #expect(nilResult.reason == .scorerUnavailable)
    #expect(nilResult.source == .failClosed)
    let (rangeResult, _) = await engine.decide(base(), state: state(now: now), now: now, scorer: FixedScorer(value: 1.7))
    #expect(rangeResult.reason == .scorerUnavailable)
}

@Test func scorerPassAndHold() async {
    let engine = ReplyDecisionEngine()
    let (pass, _) = await engine.decide(base(), state: state(now: now), now: now, scorer: FixedScorer(value: 0.82))
    #expect(pass.action == .speak)
    #expect(pass.reason == .modelPass)
    let (hold, _) = await engine.decide(base(), state: state(now: now), now: now, scorer: FixedScorer(value: 0.61))
    #expect(hold.action == .wait)
    #expect(hold.reason == .belowThreshold)
    #expect(hold.ttlSeconds == 45)
}

@Test func heuristicOnlyModeSpeaksAboveThreshold() async {
    let engine = ReplyDecisionEngine()
    var input = base(); input.topicRelevance = 1; input.interruptCost = 0
    let (result, _) = await engine.decide(input, state: state(now: now), now: now, scorer: nil)
    #expect(result.action == .speak)
    #expect(result.reason == .heuristicPass)
}

@Test func dailyStateCapIsEnforced() async {
    let engine = ReplyDecisionEngine()
    let capped = ReplyDecisionState(dayKey: ReplyDecisionState.dayKey(for: now), speaks: 30)
    let (result, _) = await engine.decide(base(), state: capped, now: now)
    #expect(result.reason == .dailyBudget)
}

@Test func recordSpeakCountsConfirmedSendsAndDedupes() {
    let engine = ReplyDecisionEngine()
    var state = ReplyDecisionState(dayKey: ReplyDecisionState.dayKey(for: now))
    engine.recordSpeak("在呢。", at: now + 1, state: &state)
    #expect(state.speaks == 1)
    engine.recordSpeak(" 在呢! ", at: now + 2, state: &state)
    #expect(state.speaks == 2)
    #expect(state.recentSpeakTexts.count == 1)
    #expect(state.recentSpeakTexts[0] == ReplyDecisionText.normalize("在呢。"))
}

@Test func dayRollResetsCounters() {
    var state = ReplyDecisionState(dayKey: ReplyDecisionState.dayKey(for: now), decisions: 40, speaks: 12)
    state.rollDay(to: now + 86_400)
    #expect(state.speaks == 0)
    #expect(state.decisions == 0)
}

@Test func heuristicMatchesDocumentedFormula() {
    let engine = ReplyDecisionEngine()
    let expected = (0.1 + 0.28 + 0.1 + 0.22 * 0.8 - 0.3 * 0.2)
    #expect(engine.heuristicScore(base()) == (expected * 10_000).rounded() / 10_000)
}

private struct CountingScorer: ReplyScoring, @unchecked Sendable {
    var value: Double?
    var onCall: () -> Void
    init(value: Double?, onCall: @escaping () -> Void) { self.value = value; self.onCall = onCall }
    func score(_ input: ReplyDecisionInput) async -> Double? { onCall(); return value }
}
