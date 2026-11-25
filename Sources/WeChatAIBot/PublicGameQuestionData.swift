import BotCore

/// Prompt facts and the source line produced by the data adapter, not by the model.
public struct PublicGameQuestionData: Sendable {
    let prompt: String
    let source: String?

    init(_ prompt: String, source: String? = nil) {
        self.prompt = prompt
        self.source = source
    }

    static func lookup(_ text: String, scope: String, schedule: IdvScheduleTool.Coordinator,
                       prices: DsfPriceTool.Coordinator) async -> Self? {
        if IdvSchedulePlanner.namesIdentityVEvent(text), IdvSchedulePlanner.plan(message: text) != nil {
            return await schedule.gameData(for: text, scope: scope)
                ?? Self("[第五人格赛事·数据不可用] 本轮未取得可核实的资料，不得猜测赛程或赛果。")
        }
        if DsfPricePlanner.intent(text) == .price {
            return await prices.gameData(message: text, isGroup: true, scope: scope)
                ?? Self(DsfPriceTool.Prompt.unavailable(reason: "本轮没有可核实的物品报价"))
        }
        return nil
    }
}
