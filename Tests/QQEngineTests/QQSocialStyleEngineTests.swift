import Foundation
import Testing
@testable import BotCore
@testable import WeChatAIBot

@Suite struct QQSocialStyleEngineTests {
    private let catalog: [QQStickerLibrary.Item] = [
        .init(id: "s-sad", title: "委屈", emotion: .sad, file: "a.png", sha256: "1", source: "local"),
        .init(id: "s-neutral", title: "点头", emotion: .neutral, file: "b.png", sha256: "2", source: "local"),
        .init(id: "s-joy", title: "开心", emotion: .joy, file: "c.png", sha256: "3", source: "local")
    ]

    @MainActor
    @Test func rerankStickersPrioritizesJoyForCelebrationAndSuppressesSeriousHelp() {
        let celebration = QQSocialStyleDirector.plan(for: "面试通过了太棒", configuredMaxChars: 120)
        let ranked = QQEngine.rerankStickers(catalog, plan: celebration)
        #expect(ranked.first?.id == "s-joy")
        #expect(ranked.count == catalog.count)

        let serious = QQSocialStyleDirector.plan(for: "启动报错怎么解决", configuredMaxChars: 120)
        #expect(QQEngine.rerankStickers(catalog, plan: serious).isEmpty)
    }
}
