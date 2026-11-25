import Foundation
import BotCore

/// Only scores; it never produces response text.
struct QQProactiveScorer: ReplyScoring {
    let context: [String]
    let bearer: String?
    var ordinaryConversation = false
    var group: QQGroupContext? = nil

    func score(_ input: ReplyDecisionInput) async -> Double? {
        guard let bearer, !bearer.isEmpty else { return nil }
        return await QQProactiveDecision.score(context: context, bearer: bearer,
                                              ordinaryConversation: ordinaryConversation, group: group)
    }
}

enum QQScoreBudgetAdmission: Sendable, Equatable {
    case reserved
    case rejected(String)
}

actor QQBudgetedScorerState {
    enum Status: Sendable, Equatable {
        case notCalled
        case reservationRejected(String)
        case httpRequestStarted
    }

    private var value: Status = .notCalled
    func set(_ status: Status) { value = status }
    func read() -> Status { value }
}

/// Reserves the durable score allowance only after the protocol's hard gates and
/// local heuristic have selected the optional model scorer.
struct QQBudgetedReplyScorer: ReplyScoring {
    let base: any ReplyScoring
    let reserve: @Sendable () async -> QQScoreBudgetAdmission
    let release: @Sendable () async -> Void
    let state: QQBudgetedScorerState

    func score(_ input: ReplyDecisionInput) async -> Double? {
        switch await reserve() {
        case .reserved:
            break
        case let .rejected(reason):
            await state.set(.reservationRejected(reason))
            return nil
        }
        await state.set(.httpRequestStarted)
        let result = await base.score(input)
        await release()
        return result
    }
}
