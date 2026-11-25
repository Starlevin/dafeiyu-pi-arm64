import Foundation
import Testing
@testable import WeChatAIBot

struct QQProactiveDecisionTests {
    @Test func onlyAcceptsFiniteBoundedDecimal() {
        #expect(QQProactiveDecision.parse(" 0.70\n") == 0.70)
        #expect(QQProactiveDecision.parse("1.0") == 1)
        #expect(QQProactiveDecision.parse("0") == 0)
        for text in ["NaN", "Inf", "-0.1", "1.01", "0.8 分", "0.7 0.8", "0.2e0", "1.0001"] {
            #expect(QQProactiveDecision.parse(text) == nil)
        }
    }

    @Test func keepsOnlyTheRecentBoundedWindowAndRedactsCommonIdentifiers() {
        let texts = Array(repeating: "早期聊天", count: 12)
            + [String(repeating: "文", count: 500), "测试 @示例成员 123456789 https://example.invalid"]
        let result = QQProactiveDecision.context(texts)
        #expect(result.count == 8)
        #expect(result.joined().count <= 1000)
        #expect(!result.joined().contains("123456789"))
        #expect(!result.joined().contains("example.invalid"))
        #expect(!result.joined().contains("示例成员"))
    }

    @Test func defaultsOffAndValidatesIndividualGroupSettings() {
        #expect(QQDecisionSettings.load([:]).isEmpty)
        let config = QQDecisionSettings.load(["DAFEIYU_DECISION_CONFIG":
            #"{"group:12345":{"mode":"shadow"},"group:67890":{"mode":"enforce","threshold":0.8,"dailyBudget":20},"group:33333":{"mode":"shadow","threshold":1.2}}"#])
        #expect(config["group:12345"]?.mode == .shadow)
        #expect(config["group:12345"]?.dailyBudget == 60)
        #expect(config["group:67890"]?.threshold == 0.8)
        #expect(config["group:33333"] == nil)
    }

    @Test @MainActor func budgetsAndInFlightArePerGroup() {
        let judge = QQProactiveDecision()
        let today = Date(timeIntervalSince1970: 100_000)
        let tomorrow = today.addingTimeInterval(86_400)
        #expect(judge.reserve(group: "a", budget: 1, now: today))
        #expect(!judge.reserve(group: "a", budget: 2, now: today))
        #expect(judge.reserve(group: "b", budget: 1, now: today))
        judge.finish(group: "a")
        #expect(!judge.reserve(group: "a", budget: 1, now: today))
        #expect(judge.reserve(group: "a", budget: 1, now: tomorrow))
        #expect(!judge.reserve(group: "b", budget: 1, now: tomorrow))
    }

    @Test @MainActor func overlappingAttemptDoesNotPersistAnotherBudgetReservation() {
        let judge = QQProactiveDecision()
        let today = Date(timeIntervalSince1970: 100_000)
        #expect(judge.reserve(group: "a", budget: 2, now: today))

        var persistCalls = 0
        let result = judge.reserveAfterPersist(group: "a", budget: 2, now: today) {
            persistCalls += 1
            return .persisted
        }

        #expect(result == .alreadyInFlight)
        #expect(persistCalls == 0)
    }

    @Test @MainActor func successfulAttemptPersistsAndConsumesExactlyOneSlotPerGroup() {
        let judge = QQProactiveDecision()
        let today = Date(timeIntervalSince1970: 100_000)
        var persistCalls = 0
        func persist() -> QQProactiveDecision.PersistenceResult {
            persistCalls += 1
            return .persisted
        }

        #expect(judge.reserveAfterPersist(group: "a", budget: 1, now: today, persist: persist) == .reserved)
        #expect(judge.reserveAfterPersist(group: "b", budget: 1, now: today, persist: persist) == .reserved)
        judge.finish(group: "a")
        #expect(judge.reserveAfterPersist(group: "a", budget: 1, now: today, persist: persist) == .dailyLimitReached)
        #expect(persistCalls == 2)
    }

    @Test @MainActor func persistenceFailureDoesNotConsumeTheDecisionSlot() {
        let judge = QQProactiveDecision()
        let today = Date(timeIntervalSince1970: 100_000)
        let failed = judge.reserveAfterPersist(group: "a", budget: 1, now: today) { .failed }
        #expect(failed == .persistenceFailed)

        let retried = judge.reserveAfterPersist(group: "a", budget: 1, now: today) { .persisted }
        #expect(retried == .reserved)
        judge.finish(group: "a")

        let exhausted = judge.reserveAfterPersist(group: "a", budget: 1, now: today) { .dailyLimitReached }
        #expect(exhausted == .dailyLimitReached)
    }

    @Test @MainActor func durableLimitIsDistinctFromPersistenceFailure() {
        let judge = QQProactiveDecision()
        let now = Date(timeIntervalSince1970: 100_000)
        #expect(judge.reserveAfterPersist(group: "a", budget: 2, now: now) { .dailyLimitReached } == .dailyLimitReached)
        #expect(judge.reserveAfterPersist(group: "a", budget: 2, now: now) { .persisted } == .reserved)
        judge.finish(group: "a")
    }
}

