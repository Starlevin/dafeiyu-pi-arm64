import Foundation

/// 第五人格赛事「赛程」接线候选（**不新增常驻进程、不新增端口、不新增第三方依赖**）。
///
/// 定位：
/// * 纯函数规划层：从**当前这条消息**判断是否在问赛程/赛果，解析明确日期与分类。
///   零网络，可在 `BotCore` 里单测。
/// * 取数层在 `Sources/WeChatAIBot/IdvScheduleTool.swift`（受限读取 + prompt 段落 + 额度）。
///
/// 红线（与 `GameKnowledgeV2` 既有措辞保持一致，不与之冲突）：
/// * 只当做**社区赛程**：来源与抓取时间必须随答案展示；`isLiveResult` 恒 false。
/// * 绝不把赛程、`0:0:0` 占位当实时比分；问比分/赛果 → 明确「无法核实」。
/// * 不得覆盖 `GameKnowledgeV2` 的静态快照；它是兜底，本候选是补充。
public enum IdvScheduleIntent: String, Codable, Sendable {
    case schedule, results, none
}

/// 一次自然语言解析结果。任何 nil 都表示「不明确」，不得由本层杜撰。
public struct IdvScheduleQuery: Sendable, Equatable {
    public let eventDate: String?        // YYYY-MM-DD（北京时间口径）
    public let league: String?           // ivl / ijl / coa
    public let intent: IdvScheduleIntent
    public let needsClarification: [String]

    public var usable: Bool { eventDate != nil && league != nil && intent == .schedule }
    public var asksResults: Bool { intent == .results }
}

public enum IdvSchedulePlanner {
    /// 赛事分类枚举（只认这三类；其它一律不介入）。
    static let leagues = ["ivl", "ijl", "coa"]

    static let resultWords = ["比分", "赛果", "谁赢", "谁获胜", "输赢", "胜负", "战绩",
                              "战况", "几比几", "结果如何", "夺冠", "冠军是谁"]
    static let scheduleWords = ["赛程", "对阵", "几点", "什么时候", "哪天", "日程",
                                "开赛", "开打", "比赛安排", "下一场", "打谁", "谁打谁"]

    /// 只对**当前这条消息**判断，避免把别人的话当成提问。
    /// `defaultLeague` 只在用户完全没提分类时兜底；多分类歧义不兜底。
    public static func plan(message: String, now: Date = Date(),
                            defaultLeague: String? = nil) -> IdvScheduleQuery? {
        let text = String(message.prefix(600))
        guard isIdentityVQuestion(text) else { return nil }

        let intent: IdvScheduleIntent
        if contains(resultWords, text) { intent = .results }
        else if contains(scheduleWords, text) { intent = .schedule }
        else if requestedDay(text, now: now) != nil, isIdentityVQuestion(text) { intent = .schedule }
        else { return nil }

        let leagues = detectedLeagues(text)
        let day = requestedDay(text, now: now)

        var missing: [String] = []
        if day == nil { missing.append("日期") }
        if leagues.count > 1 { missing.append("赛事分类（同时提到多个）") }
        else if leagues.isEmpty && defaultLeague == nil { missing.append("赛事分类") }

        let league: String?
        if leagues.count == 1 { league = leagues[0] }
        else if leagues.isEmpty { league = defaultLeague.map { $0.lowercased() } }
        else { league = nil }

        return IdvScheduleQuery(eventDate: day, league: league,
                                intent: intent, needsClarification: missing)
    }

    static func contains(_ words: [String], _ text: String) -> Bool {
        words.contains { text.range(of: $0, options: .caseInsensitive) != nil }
    }

    /// 是否在说第五人格。无游戏线索时，只有带时间词的赛程/赛果提问才算；
    /// 命中其它游戏或无关话题一律返回 false（防误命中）。
    public static func isIdentityVQuestion(_ text: String) -> Bool {
        if contains(["第五人格", "身份V", "identity v", "IVL", "IJL", "COA",
                     "深渊的呼唤", "庄园", "监管者", "求生者"], text) { return true }
        if contains(["三角洲", "kkrb", "无畏契约", "VCT", "CS2", "CSGO", "反恐精英",
                     "王者荣耀", "英雄联盟", "原神", "崩坏", "明日方舟", "和平精英",
                     "永劫无间", "DOTA", "守望先锋", "天气", "股票"], text) { return false }
        let timeWords = ["今天", "今日", "今晚", "明天", "明日", "明晚", "昨天", "昨晚", "后天"]
        guard timeWords.contains(where: { text.contains($0) }) else { return false }
        return contains(scheduleWords + resultWords, text)
    }

    static func detectedLeagues(_ text: String) -> [String] {
        var found = Set<String>()
        for league in ["IVL", "IJL", "COA"] where contains([league], text) {
            found.insert(league.lowercased())
        }
        if contains(["深渊的呼唤", "深渊"], text) { found.insert("coa") }
        if contains(["日本赛区", "日本联赛", "日服"], text) { found.insert("ijl") }
        if contains(["大陆赛区", "国服"], text) { found.insert("ivl") }
        return found.sorted()
    }

    /// 只接受可明确解析的日期；「今天/明天/后天」按北京时间。模糊或越界一律 nil。
    /// 单独暴露给测试与文档核对（与 `GameKnowledgeV2.requestedDay` 同口径）。
    public static func requestedDay(_ text: String, now: Date) -> String? {
        var cal = Calendar(identifier: .gregorian)
        cal.timeZone = TimeZone(identifier: "Asia/Shanghai")!
        let today = cal.startOfDay(for: now)

        for (word, delta) in [("大后天", 3), ("后天", 2), ("明天", 1), ("明日", 1),
                              ("今天", 0), ("今日", 0), ("今晚", 0), ("昨晚", -1), ("昨天", -1)] {
            if text.contains(word), let day = cal.date(byAdding: .day, value: delta, to: today) {
                return format(day, cal)
            }
        }

        // 完整日期：2026年11月14日 / 2026-11-14 / 2026/11/14
        if let regex = try? NSRegularExpression(pattern: "(20\\d{2})\\s*[年/\\-.]\\s*(\\d{1,2})\\s*[月/\\-.]\\s*(\\d{1,2})"),
           let match = regex.firstMatch(in: text, range: NSRange(text.startIndex..., in: text)),
           let date = date(from: match, in: text, cal: cal, y: 1, m: 2, d: 3) {
            return format(date, cal)
        }

        // 只有月日：仅在 ±窗口内补全年份（不猜远期/过去）。
        if let regex = try? NSRegularExpression(pattern: "(\\d{1,2})\\s*月\\s*(\\d{1,2})\\s*日?"),
           let match = regex.firstMatch(in: text, range: NSRange(text.startIndex..., in: text)),
           let month = number(match, in: text, group: 1), let day = number(match, in: text, group: 2) {
            let parts = cal.dateComponents([.year], from: today)
            guard let year = parts.year else { return nil }
            var best: Date?
            for candidateYear in [year - 1, year, year + 1] {
                guard let date = cal.date(from: DateComponents(year: candidateYear, month: month, day: day))
                else { continue }
                let delta = cal.dateComponents([.day], from: today, to: date).day ?? 0
                guard (-90...60).contains(delta) else { continue }
                if best == nil || abs(delta) < abs(cal.dateComponents([.day], from: today, to: best!).day ?? 0) {
                    best = date
                }
            }
            if let best { return format(best, cal) }
        }
        return nil
    }

    private static func number(_ match: NSTextCheckingResult, in text: String, group: Int) -> Int? {
        guard let range = Range(match.range(at: group), in: text) else { return nil }
        return Int(text[range])
    }

    private static func date(from match: NSTextCheckingResult, in text: String,
                             cal: Calendar, y: Int, m: Int, d: Int) -> Date? {
        guard let year = number(match, in: text, group: y),
              let month = number(match, in: text, group: m),
              let day = number(match, in: text, group: d),
              (1...12).contains(month), (1...31).contains(day) else { return nil }
        return cal.date(from: DateComponents(year: year, month: month, day: day))
    }

    static func format(_ date: Date, _ cal: Calendar) -> String {
        let parts = cal.dateComponents([.year, .month, .day], from: date)
        return String(format: "%04d-%02d-%02d", parts.year ?? 0, parts.month ?? 0, parts.day ?? 0)
    }
}
