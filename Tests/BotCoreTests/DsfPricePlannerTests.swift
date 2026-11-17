import Foundation
import Testing
@testable import BotCore

/// 离线测试：不联网、不读真实数据、不涉及任何会话与发送路径。
struct DsfPricePlannerTests {
    private func names() -> [String: String] {
        [
            "合成测试币A": "13020000167",
            "合成挂件A": "13020000590",
            ".300 BLK": "37280300001",
            ".300 BLK SUB": "37280300002",
            "老式钢盔": "11010001001"
        ]
    }

    @Test func defaultOffAndInvalidValuesFailClosed() {
        #expect(!DsfPriceConfig.fromEnvironment([:]).enabled)
        #expect(!DsfPriceConfig.fromEnvironment(["DAFEIYU_DSF_PRICE": "maybe"]).enabled)
        #expect(DsfPriceConfig.fromEnvironment(["DAFEIYU_DSF_PRICE": "on"]).enabled)
        #expect(DsfPriceConfig(enabled: true, maxAgeMinutes: 0).maxAgeMinutes == 5)
        #expect(DsfPriceConfig(enabled: true, maxAgeMinutes: 99999).maxAgeMinutes == 1440)
        #expect(DsfPriceConfig(enabled: true, perChatDaily: 0).perChatDaily == 1)
    }

    @Test func idExtractionRequiresCleanBoundaries() {
        #expect(DsfPricePlanner.extractItemID("13020000167 多少钱") == "13020000167")
        #expect(DsfPricePlanner.extractItemID("ID:13020000167") == "13020000167")
        #expect(DsfPricePlanner.extractItemID("12345") == nil)                 // 太短
        #expect(DsfPricePlanner.extractItemID("130200001671234") == nil)       // 太长
        #expect(DsfPricePlanner.extractItemID("１３０２００００１６７") == nil)   // 全角
        #expect(DsfPricePlanner.extractItemID("没有数字") == nil)
    }

    @Test func intentNeedsGameAndPriceAndRejectsPrediction() {
        #expect(DsfPricePlanner.intent("三角洲 13020000167 多少钱") == .price)
        #expect(DsfPricePlanner.intent("13020000167 多少钱") == .none)          // 没有游戏语境
        #expect(DsfPricePlanner.intent("三角洲好玩吗") == .none)                // 没有价格意图
        #expect(DsfPricePlanner.intent("三角洲物价预测") == .outOfScope)
        #expect(DsfPricePlanner.intent("三角洲物价排行榜") == .outOfScope)
        #expect(DsfPricePlanner.intent("") == .none)
    }

    @Test func onlyGroupAndOnlyWithClearTarget() {
        #expect(DsfPricePlanner.plan(message: "三角洲 13020000167 多少钱", isGroup: false, names: names()) == nil)
        #expect(DsfPricePlanner.plan(message: "晚安", isGroup: true, names: names()) == nil)
        #expect(DsfPricePlanner.plan(message: "三角洲物价预测", isGroup: true, names: names()) == nil)
        // 名称没加载时不为了一个可能的名字去多抓 89 KB
        #expect(DsfPricePlanner.plan(message: "三角洲 老式钢盔 多少钱", isGroup: true, names: [:]) == nil)
    }

    @Test func directIDWinsAndIsLabelled() {
        let plan = DsfPricePlanner.plan(message: "三角洲 13020000167 多少钱", isGroup: true, names: names())
        #expect(plan?.itemID == "13020000167")
        #expect(plan?.matchedBy == "directID")
    }

    @Test func exactNameMatchOnly() {
        let plan = DsfPricePlanner.plan(message: "三角洲 合成测试币A 多少钱", isGroup: true, names: names())
        #expect(plan?.itemID == "13020000167")
        #expect(plan?.matchedBy == "nameExact")
        // 少一个字：表里没有这个全名 → 不联网
        #expect(DsfPricePlanner.plan(message: "三角洲 合成测试币 多少钱", isGroup: true, names: names()) == nil)
        // 多一个字：已知局限（表名仍作为子串出现会被命中）。这里如实断言当前行为。
        #expect(DsfPricePlanner.plan(message: "三角洲 合成测试币AB 多少钱", isGroup: true, names: names())?
            .itemID == "13020000167")
        // 没有价格意图词时，即使名字出现也不联网
        #expect(DsfPricePlanner.plan(message: "三角洲 合成测试币A", isGroup: true, names: names()) == nil)
    }

    @Test func ambiguousNameDoesNotPickOne() {
        // 两条同名记录在快照里 → 本地化映射里是 AMBIGUOUS: 前缀，不能当 ID 用
        var table = names()
        table["同名弹"] = "AMBIGUOUS:37280300002,37280400002"
        let plan = DsfPricePlanner.plan(message: "三角洲 同名弹 多少钱", isGroup: true, names: table)
        // 规划层不报价：返回的 ID 带 AMBIGUOUS 前缀，交给上层说「无法唯一确定」
        #expect(plan?.itemID.hasPrefix("AMBIGUOUS") == true)
    }

    @Test func longestMatchWinsForOverlappingNames() {
        let plan = DsfPricePlanner.plan(message: "三角洲 .300 BLK SUB 多少钱", isGroup: true, names: names())
        #expect(plan?.itemID == "37280300002")
    }

    @Test func messageLengthIsBounded() {
        let long = "三角洲 " + String(repeating: "啊", count: 700) + " 多少钱"
        #expect(DsfPricePlanner.intent(long) == .none)
    }
}
