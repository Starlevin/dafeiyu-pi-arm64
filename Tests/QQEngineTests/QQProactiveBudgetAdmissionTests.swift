import Foundation
import Testing
@testable import BotCore
@testable import WeChatAIBot

private actor CallCounter {
    private(set) var value = 0
    func increment() { value += 1 }
    func read() -> Int { value }
}

private struct CountingReplyScorer: ReplyScoring {
    let calls: CallCounter
    let value: Double?

    func score(_ input: ReplyDecisionInput) async -> Double? {
        await calls.increment()
        return value
    }
}

@Suite struct QQProactiveBudgetAdmissionTests {
    private let now = 1_700_000_000

    private func state() -> ReplyDecisionState {
        ReplyDecisionState(dayKey: ReplyDecisionState.dayKey(for: now))
    }

    private func moderateQuestion() -> ReplyDecisionInput {
        ReplyDecisionInput(scope: .group, chatAlias: "group:10001", window: ["有人问了具体问题", "还有没有建议？"],
                           unansweredQuestion: true, questionToAll: true,
                           interruptCost: 0.2, topicRelevance: 0.8, dailyRemaining: 30)
    }

    @Test func hardGatesAndLowHeuristicsDoNotReserveBudget() async {
        let reservations = CallCounter()
        let modelCalls = CallCounter()
        let budgeted = QQBudgetedReplyScorer(
            base: CountingReplyScorer(calls: modelCalls, value: 0.9),
            reserve: { await reservations.increment(); return .reserved },
            release: {}, state: QQBudgetedScorerState())
        let engine = ReplyDecisionEngine()

        var hardSkip = moderateQuestion()
        hardSkip.addressedToOther = true
        hardSkip.mention = .other
        let (hardResult, _) = await engine.decide(hardSkip, state: state(), now: now, scorer: budgeted)
        #expect(hardResult.reason == .addressedToOther)

        var interruptSkip = moderateQuestion()
        interruptSkip.interruptCost = 0.8
        let (interruptResult, _) = await engine.decide(interruptSkip, state: state(), now: now, scorer: budgeted)
        #expect(interruptResult.reason == .highInterruptCost)

        var low = moderateQuestion()
        low.unansweredQuestion = false
        low.questionToAll = false
        low.topicRelevance = 0
        low.interruptCost = 0.6
        low.window = ["哈哈"]
        let (lowResult, _) = await engine.decide(low, state: state(), now: now, scorer: budgeted)
        #expect(lowResult.reason == .heuristicLow)
        #expect(await reservations.read() == 0)
        #expect(await modelCalls.read() == 0)
    }

    @Test func oneEligibleScorerCallConsumesOneReservation() async {
        let reservations = CallCounter()
        let modelCalls = CallCounter()
        let releases = CallCounter()
        let scorerState = QQBudgetedScorerState()
        let budgeted = QQBudgetedReplyScorer(
            base: CountingReplyScorer(calls: modelCalls, value: 0.82),
            reserve: { await reservations.increment(); return .reserved },
            release: { await releases.increment() }, state: scorerState)
        let engine = ReplyDecisionEngine()
        let (result, next) = await engine.decide(moderateQuestion(), state: state(), now: now, scorer: budgeted)
        #expect(result.reason == .modelPass)
        #expect(result.scorerCalled)
        #expect(next.scorerCalls == 1)
        #expect(await reservations.read() == 1)
        #expect(await modelCalls.read() == 1)
        #expect(await releases.read() == 1)
        #expect(await scorerState.read() == .httpRequestStarted)
    }

    @Test func failedDurableReservationFailsClosedWithoutCallingModel() async {
        let reservations = CallCounter()
        let modelCalls = CallCounter()
        let releases = CallCounter()
        let scorerState = QQBudgetedScorerState()
        let budgeted = QQBudgetedReplyScorer(
            base: CountingReplyScorer(calls: modelCalls, value: 0.99),
            reserve: { await reservations.increment(); return .rejected("score_budget_persist_failed") },
            release: { await releases.increment() }, state: scorerState)
        let engine = ReplyDecisionEngine()
        let (result, _) = await engine.decide(moderateQuestion(), state: state(), now: now, scorer: budgeted)
        #expect(result.action == .skip)
        #expect(result.reason == .scorerUnavailable)
        #expect(await reservations.read() == 1)
        #expect(await modelCalls.read() == 0)
        #expect(await releases.read() == 0)
        #expect(await scorerState.read() == .reservationRejected("score_budget_persist_failed"))
    }

    @Test @MainActor func rejectedParallelReservationDoesNotReleaseCurrentHolder() async {
        let judge = QQProactiveDecision()
        let now = Date(timeIntervalSince1970: Double(self.now))
        #expect(judge.reserveAfterPersist(group: "group:10001", budget: 2, now: now) { .persisted } == .reserved)

        let modelCalls = CallCounter()
        let scorer = QQBudgetedReplyScorer(
            base: CountingReplyScorer(calls: modelCalls, value: 0.9),
            reserve: {
                let result = await MainActor.run {
                    judge.reserveAfterPersist(group: "group:10001", budget: 2, now: now) { .persisted }
                }
                return result == .reserved ? .reserved : .rejected("parallel_in_flight")
            },
            release: { await MainActor.run { judge.finish(group: "group:10001") } },
            state: QQBudgetedScorerState())

        #expect(await scorer.score(moderateQuestion()) == nil)
        #expect(await modelCalls.read() == 0)
        #expect(await MainActor.run { !judge.canReserve(group: "group:10001", budget: 2, now: now) })
        judge.finish(group: "group:10001")
    }
}
