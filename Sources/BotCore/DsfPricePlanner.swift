import Foundation

/// 三角洲行动物价工具的**纯函数**部分：输入用户原话，输出「该不该联网、抓哪个 ID」。
///
/// 这个文件刻意不碰网络、不碰 Engine、不碰发送路径，方便单独评审与合并；
/// 网络与解析在 `DsfPriceTool.swift`，接入点在 `QQEngine` 的既有游戏知识分支之后（见 INTEGRATION.md）。
///
/// 设计边界：
/// * 默认关闭（`DsfPriceConfig.fromEnvironment` 为 false）；
/// * 只在「群消息 + 明确价格意图 + 能唯一确定一件物品」时才返回查询；
/// * 名称精确匹配，歧义/缺失都不联网，交给模型如实说；
/// * 绝不因为「是三角洲话题」就自动联网，也不改变任何发言概率。
public enum DsfPriceIntent: String, Sendable, Equatable {
    case price
    case outOfScope
    case none
}

public struct DsfPriceRequest: Sendable, Equatable {
    public let itemID: String
    /// 命中来源：directID / nameExact；用于在回答里说明「这条价格是怎么对上的」。
    public let matchedBy: String

    public init(itemID: String, matchedBy: String) {
        self.itemID = itemID
        self.matchedBy = matchedBy
    }
}

public struct DsfPriceConfig: Sendable, Equatable {
    public let enabled: Bool
    public let maxAgeMinutes: Int
    public let perChatDaily: Int

    public init(enabled: Bool = false, maxAgeMinutes: Int = 60, perChatDaily: Int = 12) {
        self.enabled = enabled
        self.maxAgeMinutes = min(1440, max(5, maxAgeMinutes))
        self.perChatDaily = min(50, max(1, perChatDaily))
    }

    /// 与 GameKnowledgeV2 同一风格：未设置或非法值一律关闭。
    public static func fromEnvironment(_ env: [String: String] = ProcessInfo.processInfo.environment) -> Self {
        let raw = env["DAFEIYU_DSF_PRICE"]?.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
        let enabled = raw.map { ["1", "true", "on", "yes"].contains($0) } ?? false
        return Self(
            enabled: enabled,
            maxAgeMinutes: Int(env["DAFEIYU_DSF_PRICE_MAX_AGE_MIN"] ?? "") ?? 60,
            perChatDaily: Int(env["DAFEIYU_DSF_PRICE_DAILY"] ?? "") ?? 12
        )
    }
}

public enum DsfPricePlanner {
    /// 三角洲话题词。只用来限定「是不是在聊这个游戏」，不构成联网理由。
    public static let gameWords = ["三角洲行动", "三角洲", "烽火地带", "全面战场", "kk日报", "一图流"]

    /// 价格意图词。
    public static let priceWords = ["价格", "价钱", "多少钱", "卖多少", "值多少", "物价", "交易行", "多少钱一个", "能卖"]

    /// 明确超出本工具的请求：不做预测、不做榜单、不做全套。
    public static let outOfScopeWords = ["预测", "涨跌", "趋势", "收益率", "全部", "所有", "排行榜", "排行"]

    /// 从原话里抽物品 ID：必须 6–12 位、边界干净。
    /// 用逐字符扫描而不是正则，避免不同平台的正则方言差异，也好写测试。
    public static func extractItemID(_ text: String) -> String? {
        var found: String?
        var current = ""
        func flush() {
            if (6...12).contains(current.count), found == nil { found = current }
            current = ""
        }
        for scalar in text.unicodeScalars {
            if scalar.isASCII, CharacterSet.decimalDigits.contains(scalar) {
                current.unicodeScalars.append(scalar)
            } else {
                flush()
            }
        }
        flush()
        return found
    }

    /// 判断意图。`outOfScope` 优先于 `price`，避免「预测」被当成普通询价。
    public static func intent(_ text: String) -> DsfPriceIntent {
        let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty, trimmed.count <= 600 else { return .none }
        guard gameWords.contains(where: { trimmed.localizedCaseInsensitiveContains($0) }) else { return .none }
        if outOfScopeWords.contains(where: { trimmed.contains($0) }) { return .outOfScope }
        guard priceWords.contains(where: { trimmed.contains($0) }) else { return .none }
        return .price
    }

    /// 一次计划：返回 nil 表示「这一轮不要联网」。
    ///
    /// `names` 是官方本地化表里「名称 → ID」的**精确**映射；为空表示还没加载，
    /// 此时只认直接给的 ID（不为了一个可能存在的名字去额外抓 89 KB）。
    public static func plan(message: String, isGroup: Bool, names: [String: String]) -> DsfPriceRequest? {
        guard isGroup, intent(message) == .price else { return nil }
        if let id = extractItemID(message) { return DsfPriceRequest(itemID: id, matchedBy: "directID") }
        // 名称：只接受「与本地化表全名完全相等」的名词出现在原话里。子串只用来定位，
        // 命中后取最长匹配，避免「.300 BLK」吃掉「.300 BLK SUB」。
        let hits = names.keys.filter { $0.count >= 2 && message.contains($0) }
        let maximal = hits.filter { outer in !hits.contains { $0 != outer && $0.contains(outer) && $0.count > outer.count } }
        guard maximal.count == 1, let name = maximal.first, let id = names[name] else { return nil }
        return DsfPriceRequest(itemID: id, matchedBy: "nameExact")
    }
}
