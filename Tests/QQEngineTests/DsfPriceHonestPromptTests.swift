import Foundation
#if canImport(FoundationNetworking)
import FoundationNetworking
#endif
import Testing
@testable import WeChatAIBot
import BotCore

/// 诚实提示契约：名称歧义、数据过期、超时/上游错误都必须给一段提示，
/// 且所有失败片段里**不得出现任何价格数字**、不得承诺实时。
/// 纯函数，不联网、不读真实数据、不涉及任何发送路径。
struct DsfPriceHonestPromptTests {

    /// 片段里除了已声明的时间/阈值/版本字段外，不应夹带「报价：数字」这类价格陈述。
    private func assertNoPriceNumber(_ text: String, _ label: String) {
        #expect(!text.contains("当前快照报价"), "\(label) 不应给出价格")
        #expect(!text.contains("现价是"), "\(label) 不应给出现价")
        #expect(text.contains("未给出价格") || text.contains("不联网、不报价")
                || text.contains("没有给出任何价格数字"), "\(label) 应明确说没报价")
        #expect(!text.contains("实时行情") || text.contains("非实时") || text.contains("不是实时行情"),
                "\(label) 不得把第三方快照说成实时")
    }

    @Test func ambiguousFragmentListsIDsAndDoesNotPickOne() {
        let fragment = DsfPriceTool.Prompt.ambiguous(candidateIDs: ["37280300002", "37280400002"])
        #expect(fragment.contains("37280300002"))
        #expect(fragment.contains("37280400002"))
        #expect(fragment.contains("无法唯一确定"))
        #expect(fragment.contains("请直接给物品 ID"))
        assertNoPriceNumber(fragment, "歧义")
    }

    @Test func staleFragmentHasTimeButNoPrice() {
        let fragment = DsfPriceTool.Prompt.stale(capturedAt: "2026-11-14T13:07:04Z", ageMinutes: 96, limitMinutes: 60)
        #expect(fragment.contains("数据已过期"))
        #expect(fragment.contains("2026-11-14T13:07:04Z"))
        #expect(fragment.contains("96"))
        #expect(fragment.contains("60"))
        #expect(fragment.contains("dfttk.com 自行核对"))
        assertNoPriceNumber(fragment, "过期")
    }

    @Test func unavailableFragmentIsHonestAndNumberless() {
        for reason in ["请求超时", "连接失败", "响应结构不可信", "响应超出字节上限"] {
            let fragment = DsfPriceTool.Prompt.unavailable(reason: reason)
            #expect(fragment.contains(reason))
            #expect(fragment.contains("暂时不可用"))
            assertNoPriceNumber(fragment, "不可用(\(reason))")
        }
    }

    @Test func successFragmentSeparatesThreeTimesAndNeverClaimsRealtime() {
        // 用 Snapshot 直接构造成功路径，验证三种时间与非实时措辞仍然齐全。
        let snapshot = DsfPriceTool.Snapshot(
            priceVersion: "pv1", dataVersion: "dv1",
            capturedFrom: "2026-11-04T17:30:46Z", capturedTo: "2026-11-14T13:06:30Z",
            fetchedAt: "2026-11-14T13:07:04Z",
            prices: ["13020000167": 2562], availability: [:],
            fetchedLocallyAt: Date(), itemNames: ["合成测试币A": "13020000167"], bytes: 100
        )
        let fragment = DsfPriceTool.promptFragment(
            request: DsfPriceRequest(itemID: "13020000167", matchedBy: "nameExact"), snapshot: snapshot)
        #expect(fragment.contains("2562"))
        #expect(fragment.contains("① 价格文件抓取时间"))
        #expect(fragment.contains("② 全源数据采集范围"))
        #expect(fragment.contains("③ 单件真实更新时间"))
        #expect(fragment.contains("非实时"))
        #expect(fragment.contains("不是每件物品的更新时间"))
    }

    @Test func noResultSuccessFragmentDoesNotInventPrice() {
        let snapshot = DsfPriceTool.Snapshot(
            priceVersion: "pv1", dataVersion: "dv1",
            capturedFrom: "2026-11-04T17:30:46Z", capturedTo: "2026-11-14T13:06:30Z",
            fetchedAt: "2026-11-14T13:07:04Z",
            prices: [:], availability: [:],
            fetchedLocallyAt: Date(), itemNames: [:], bytes: 100
        )
        let fragment = DsfPriceTool.promptFragment(
            request: DsfPriceRequest(itemID: "13020009999", matchedBy: "directID"), snapshot: snapshot)
        #expect(fragment.contains("没有这件物品的报价"))
        #expect(fragment.contains("不得推测价格"))
        #expect(!fragment.contains("当前快照报价"))
    }

    @Test func unavailableStatusMarksLastKnownPriceAsNotCurrent() {
        let snapshot = DsfPriceTool.Snapshot(
            priceVersion: "pv1", dataVersion: "dv1",
            capturedFrom: "2026-11-04T17:30:46Z", capturedTo: "2026-11-14T13:06:30Z",
            fetchedAt: "2026-11-14T13:07:04Z",
            prices: [:], availability: ["13020000590": .init(status: "providerCatalogUnavailable",
                                                             reason: "provider-down", lastKnownPrice: 131598,
                                                             observedAt: nil)],
            fetchedLocallyAt: Date(), itemNames: [:], bytes: 100
        )
        let fragment = DsfPriceTool.promptFragment(
            request: DsfPriceRequest(itemID: "13020000590", matchedBy: "directID"), snapshot: snapshot)
        #expect(fragment.contains("不可标价"))
        #expect(fragment.contains("早前价"))
        #expect(fragment.contains("不是现价"))
        #expect(!fragment.contains("当前快照报价"))
    }
}
