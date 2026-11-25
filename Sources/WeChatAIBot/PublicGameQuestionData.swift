import Foundation
import BotCore

/// The adapter supplies verified facts or a fixed reply when it cannot verify them.
public struct PublicGameQuestionData: Sendable {
    let prompt: String
    let source: String?
    private let unavailableReply: String?

    init(_ prompt: String, source: String? = nil, unavailableReply: String? = nil) {
        self.prompt = prompt
        self.source = source
        self.unavailableReply = unavailableReply
    }

    func localResult() throws -> ModelResult? {
        guard let unavailableReply else { return nil }
        struct ReplyEnvelope: Encodable {
            let text: String
            let emotion = "neutral"
            let intensity = 0
        }
        let encoded = try JSONEncoder().encode(ReplyEnvelope(text: unavailableReply))
        return ModelResult(text: String(decoding: encoded, as: UTF8.self), tokens: 0,
                           seconds: 0, sources: source.map { [$0] } ?? [])
    }

    static func lookup(_ text: String, scope: String, schedule: IdvScheduleTool.Coordinator,
                       prices: DsfPriceTool.Coordinator) async -> Self? {
        if IdvSchedulePlanner.namesIdentityVEvent(text), IdvSchedulePlanner.plan(message: text) != nil {
            return await schedule.gameData(for: text, scope: scope)
                ?? Self("[第五人格赛事·数据不可用] 本轮未取得可核实的资料，不得猜测赛程或赛果。")
        }
        if DsfPricePlanner.intent(text) == .price {
            return await prices.gameData(message: text, isGroup: true, scope: scope)
                ?? DsfPriceTool.unavailableGameData(reason: "没有可核实的物品报价")
        }
        return nil
    }
}
