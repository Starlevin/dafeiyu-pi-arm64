import Foundation

/// 第五人格轻量本地知识索引（V1）。
///
/// 设计约束（对应任务书 §2/§3）：
/// - 只读本地 JSON / 内嵌兜底，**绝不发起网络请求**；禁用时行为与旧版完全一致。
/// - 只保存原创短事实元数据、别名、类型与可核验页面 URL，不镜像 BWIKI 正文、图片或攻略。
/// - 返回「主题识别 + 最多 N 条相关事实 + 出处」，明确区分 identityv / unknown / no-match。
/// - 歧义词必须在同一短句里出现游戏信号才激活；无依据时不编造机制数值。
public struct IdentityVKnowledgeEntry: Codable, Sendable, Equatable {
    public let id: String
    public let canonical: String
    public let aliases: [String]
    public let type: String
    public let summary: String
    public let sourceURL: String
    public let sourceCheckedAt: String
    public let gamePatchDate: String?
    public let confidence: String
    public let versionSensitive: Bool
    public let contextRequired: Bool
    public let ambiguity: String?
    public let tags: [String]

    private enum CodingKeys: String, CodingKey {
        case id, canonical, aliases, type, summary
        case sourceURL = "source_url"
        case sourceCheckedAt = "source_checked_at"
        case gamePatchDate = "game_patch_date"
        case confidence, versionSensitive = "version_sensitive"
        case contextRequired = "context_required"
        case ambiguity, tags
    }
}

public struct IdentityVKnowledgeIndex: Codable, Sendable {
    public let schemaVersion: Int
    public let compiledAt: String
    public let licenseNote: String
    public let staleAfterDays: Int
    public let versionSensitiveNote: String
    public let gameSignals: [String]
    public let entries: [IdentityVKnowledgeEntry]

    private enum CodingKeys: String, CodingKey {
        case schemaVersion = "schema_version"
        case compiledAt = "compiled_at"
        case licenseNote = "license_note"
        case staleAfterDays = "stale_after_days"
        case versionSensitiveNote = "version_sensitive_note"
        case gameSignals = "game_signals"
        case entries
    }
}

/// 主题识别结果。unknown = 出现疑似游戏词但语境不足/歧义未消；no-match = 无游戏主题。
public enum IdentityVTopic: String, Codable, Sendable, Equatable {
    case identityV = "identityv"
    case unknown
    case noMatch = "no_match"
}

/// 单条事实命中（供 Prompt 注入，字段短小，不含用户原文）。
public struct IdentityVFact: Sendable, Equatable {
    public let id: String
    public let canonical: String
    public let summary: String
    public let type: String
    public let confidence: String
    public let sourceURL: String
    public let sourceCheckedAt: String
    public let gamePatchDate: String?
    public let versionSensitive: Bool
    public let stale: Bool
    public let caution: String?

    public var sourceLine: String {
        "出处：\(sourceURL)（核查 \(sourceCheckedAt)）"
    }
}

public struct IdentityVLookup: Sendable, Equatable {
    public let topic: IdentityVTopic
    public let facts: [IdentityVFact]
    public let matchedIDs: [String]
    public let topicReason: String
    public var noMatch: Bool { topic == .noMatch }
    public var unknown: Bool { topic == .unknown }
}

/// 可独立开关的模块配置。默认关闭，关闭时不加载数据、不调用网络、不改变任何既有行为。
public struct IdentityVConfig: Sendable, Equatable {
    public var enabled: Bool
    public var maxFacts: Int
    public var maxContextMessages: Int

    public init(enabled: Bool = false, maxFacts: Int = 3, maxContextMessages: Int = 8) {
        self.enabled = enabled
        self.maxFacts = min(6, max(1, maxFacts))
        self.maxContextMessages = min(32, max(1, maxContextMessages))
    }

    /// 环境变量显式开启才生效；非法或缺失一律 fail-closed 返回关闭。
    /// DAFEIYU_IDV_KNOWLEDGE=1 开启；DAFEIYU_IDV_MAX_FACTS 可限定注入条数。
    public static func fromEnvironment(_ env: [String: String] = ProcessInfo.processInfo.environment) -> IdentityVConfig {
        let raw = env["DAFEIYU_IDV_KNOWLEDGE"]?.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
        guard let raw, ["1", "true", "on", "yes"].contains(raw) else { return IdentityVConfig(enabled: false) }
        let maxFacts = Int(env["DAFEIYU_IDV_MAX_FACTS"] ?? "") ?? 3
        return IdentityVConfig(enabled: true, maxFacts: maxFacts)
    }
}

/// 纯本地、无网络的知识查询器。线程安全（值类型 + 不可变索引）。
public struct IdentityVKnowledge: Sendable {
    private let index: IdentityVKnowledgeIndex?
    private let config: IdentityVConfig
    private let calendar: Calendar

    /// 极小的内嵌兜底：资源文件缺失/损坏时仍能提供基本术语，不谎称线上同步。
    private static let fallbackJSON = """
    {"schema_version":1,"compiled_at":"2026-11-10","license_note":"内嵌兜底：原创短小事实元数据，不含站点正文。","stale_after_days":180,"version_sensitive_note":"版本相关条目不得声称当前数值。","game_signals":["第五人格","第五","监管者","求生者","密码机","压机","开门战","BP","COA","IVL","精华","限定"],"entries":[
    {"id":"camp_hunter","canonical":"监管者","aliases":["监管","屠夫"],"type":"character","summary":"对局中追击求生者的一方，与求生者阵营相对。","source_url":"https://wiki.biligame.com/dwrg/首页","source_checked_at":"2026-11-10","game_patch_date":null,"confidence":"fact","version_sensitive":false,"context_required":false,"ambiguity":null,"tags":["阵营"]},
    {"id":"camp_survivor","canonical":"求生者","aliases":["求生","人类"],"type":"character","summary":"对局中破译密码机并逃离的一方，与监管者阵营相对。","source_url":"https://wiki.biligame.com/dwrg/首页","source_checked_at":"2026-11-10","game_patch_date":null,"confidence":"fact","version_sensitive":false,"context_required":false,"ambiguity":null,"tags":["阵营"]},
    {"id":"term_cipher","canonical":"密码机","aliases":["机子","电机"],"type":"match_term","summary":"求生者需要破译的局内装置，破译进度决定逃离条件。","source_url":"https://wiki.biligame.com/dwrg/首页","source_checked_at":"2026-11-10","game_patch_date":null,"confidence":"fact","version_sensitive":false,"context_required":false,"ambiguity":null,"tags":["对局"]},
    {"id":"term_prime","canonical":"压机","aliases":["压好","压密码机"],"type":"match_term","summary":"把最后一台密码机留到临界进度，配合时机一次性破译的战术。","source_url":"https://wiki.biligame.com/dwrg/首页","source_checked_at":"2026-11-10","game_patch_date":null,"confidence":"community","version_sensitive":false,"context_required":false,"ambiguity":null,"tags":["对局","战术"]},
    {"id":"term_endgame","canonical":"开门战","aliases":["开门"],"type":"match_term","summary":"密码机全部破译后围绕大门开启与逃离展开的收尾阶段。","source_url":"https://wiki.biligame.com/dwrg/首页","source_checked_at":"2026-11-10","game_patch_date":null,"confidence":"community","version_sensitive":false,"context_required":false,"ambiguity":null,"tags":["对局"]},
    {"id":"term_terror_shock","canonical":"恐惧震慑","aliases":["震慑"],"type":"match_term","summary":"求生者在特定交互动作中被监管者命中时承受的加倍打击。","source_url":"https://wiki.biligame.com/dwrg/首页","source_checked_at":"2026-11-10","game_patch_date":null,"confidence":"community","version_sensitive":false,"context_required":false,"ambiguity":null,"tags":["对局"]},
    {"id":"skill_flywheel","canonical":"飞轮效应","aliases":["飞轮"],"type":"talent","summary":"求生者天赋，短时冲刺并获得极短的免伤窗口。","source_url":"https://wiki.biligame.com/dwrg/首页","source_checked_at":"2026-11-10","game_patch_date":null,"confidence":"community","version_sensitive":true,"context_required":false,"ambiguity":null,"tags":["天赋"]},
    {"id":"skill_borrowed_time","canonical":"化险为夷","aliases":["搏命"],"type":"talent","summary":"救人后为被救者与自身提供短时容错的天赋，俗称搏命。","source_url":"https://wiki.biligame.com/dwrg/首页","source_checked_at":"2026-11-10","game_patch_date":null,"confidence":"community","version_sensitive":true,"context_required":false,"ambiguity":null,"tags":["天赋"]},
    {"id":"esports_coa","canonical":"深渊的呼唤","aliases":["COA"],"type":"esports","summary":"《第五人格》大型赛事系列，社区常以缩写 COA 称呼。","source_url":"https://wiki.biligame.com/dwrg/首页","source_checked_at":"2026-11-10","game_patch_date":null,"confidence":"fact","version_sensitive":true,"context_required":false,"ambiguity":null,"tags":["赛事"]},
    {"id":"esports_ivl","canonical":"IVL","aliases":["IVL"],"type":"esports","summary":"《第五人格》职业联赛的常用缩写。","source_url":"https://wiki.biligame.com/dwrg/首页","source_checked_at":"2026-11-10","game_patch_date":null,"confidence":"fact","version_sensitive":true,"context_required":false,"ambiguity":null,"tags":["赛事"]}]}
    """

    public init(config: IdentityVConfig = .fromEnvironment(), data: Data? = nil) {
        self.config = config
        var cal = Calendar(identifier: .gregorian)
        cal.timeZone = TimeZone(identifier: "Asia/Shanghai")!
        self.calendar = cal
        guard config.enabled else { self.index = nil; return }
        let payload = data ?? Self.defaultData() ?? Data(Self.fallbackJSON.utf8)
        let decoded = try? JSONDecoder().decode(IdentityVKnowledgeIndex.self, from: payload)
        // 显式传入或资源文件损坏时回退到内嵌兜底，不谎称线上同步。
        self.index = decoded ?? (try? JSONDecoder().decode(IdentityVKnowledgeIndex.self,
                                                          from: Data(Self.fallbackJSON.utf8)))
    }

    /// 只读仓库内 Resources/IdentityV/identityv_knowledge.json；找不到就走内嵌兜底，不联网。
    private static func defaultData() -> Data? {
        #if os(Linux)
        let candidates = [
            ProcessInfo.processInfo.environment["DAFEIYU_IDV_INDEX"].map { URL(fileURLWithPath: $0) },
            ProcessInfo.processInfo.environment["DAFEIYU_RESOURCES"].map { URL(fileURLWithPath: $0, isDirectory: true).appendingPathComponent("IdentityV/identityv_knowledge.json") },
            URL(fileURLWithPath: CommandLine.arguments[0]).standardizedFileURL.deletingLastPathComponent()
                .appendingPathComponent("Resources/IdentityV/identityv_knowledge.json")
        ].compactMap { $0 }
        #else
        let candidates = [
            ProcessInfo.processInfo.environment["DAFEIYU_IDV_INDEX"].map { URL(fileURLWithPath: $0) },
            Bundle.main.resourceURL?.appendingPathComponent("IdentityV/identityv_knowledge.json")
        ].compactMap { $0 }
        #endif
        for url in candidates {
            if let data = try? Data(contentsOf: url), !data.isEmpty { return data }
        }
        return nil
    }

    /// 禁用时直接返回 no-match，不读取任何上下文、不做任何计算。
    public var isEnabled: Bool { config.enabled && index != nil }

    /// 给最近群消息（有限上下文，内存里即可）返回主题识别、最多 N 条相关事实和来源。
    /// - Parameters:
    ///   - messages: 最近有限条纯文本消息（调用方保证已脱敏/限长）。
    ///   - maxFacts: 最多返回条数，受配置上限约束；传 nil 用配置值。
    public func lookup(messages: [String], maxFacts: Int? = nil, now: Date = Date()) -> IdentityVLookup {
        guard isEnabled, let index else {
            return IdentityVLookup(topic: .noMatch, facts: [], matchedIDs: [], topicReason: "模块禁用")
        }
        let window = Array(messages.suffix(config.maxContextMessages))
        let context = window.joined(separator: "\n")
        // 匹配用整个有限窗口（连续对局对话里前一句的术语也算本话题）；
        // 歧义消解仍只看最后 1-2 个短句，避免跨消息把「厂长开会」误接成角色。
        let ambiguityScope = Self.lastSentenceScope(context)
        let gameSignal = index.gameSignals.contains { context.contains($0) }
        let scopedSignal = index.gameSignals.contains { ambiguityScope.contains($0) }

        var hits: [(entry: IdentityVKnowledgeEntry, score: Int)] = []
        for entry in index.entries {
            guard Self.matches(entry, in: context, ambiguityScope: ambiguityScope,
                               gameSignal: gameSignal, scopedSignal: scopedSignal) else { continue }
            hits.append((entry, Self.score(entry, in: context, gameSignal: gameSignal)))
        }
        hits.sort { lhs, rhs in
            if lhs.score != rhs.score { return lhs.score > rhs.score }
            return lhs.entry.id < rhs.entry.id
        }
        let limit = min(config.maxFacts, maxFacts.map { min(6, max(1, $0)) } ?? config.maxFacts)
        let selected = Array(hits.prefix(limit))
        let facts = selected.map { Self.fact($0.entry, index: index, now: now, calendar: calendar) }

        if !facts.isEmpty {
            return IdentityVLookup(topic: .identityV, facts: facts,
                                   matchedIDs: selected.map(\.entry.id), topicReason: "命中游戏术语/别名")
        }
        if gameSignal {
            return IdentityVLookup(topic: .unknown, facts: [], matchedIDs: [],
                                   topicReason: "出现游戏信号但无可用事实或歧义未消，不做机制推断")
        }
        return IdentityVLookup(topic: .noMatch, facts: [], matchedIDs: [], topicReason: "未检测到游戏主题")
    }

    /// 歧义词：必须在同一短句作用域内出现游戏信号才激活，避免「修手机」「厂长开会」误命中。
    /// 非歧义的纯游戏黑话可在整个有限窗口内命中（队局连续对话）。
    private static func matches(_ entry: IdentityVKnowledgeEntry, in context: String,
                                ambiguityScope: String, gameSignal: Bool, scopedSignal: Bool) -> Bool {
        let forms = [entry.canonical] + entry.aliases
        if entry.contextRequired {
            guard forms.contains(where: { ambiguityScope.contains($0) }) else { return false }
            return scopedSignal
        }
        return forms.contains(where: { context.contains($0) })
    }

    private static func score(_ entry: IdentityVKnowledgeEntry, in scope: String, gameSignal: Bool) -> Int {
        let forms = [entry.canonical] + entry.aliases
        let hitCount = forms.filter { scope.contains($0) }.count
        var score = hitCount * 10
        if forms.contains(where: { $0 == entry.canonical }) && scope.contains(entry.canonical) { score += 5 }
        if gameSignal { score += 2 }
        if entry.confidence == "fact" { score += 1 }
        if entry.contextRequired { score -= 1 }
        return score
    }

    /// 只取最后 1-2 句作为「同一短句」作用域；换行/句末标点切分，避免跨消息误激活。
    private static func lastSentenceScope(_ text: String) -> String {
        let pieces = text.components(separatedBy: CharacterSet(charactersIn: "\n。！？!?；;，,"))
            .map { $0.trimmingCharacters(in: .whitespacesAndNewlines) }
            .filter { !$0.isEmpty }
        return pieces.suffix(2).joined(separator: " ")
    }

    private static func fact(_ entry: IdentityVKnowledgeEntry, index: IdentityVKnowledgeIndex,
                             now: Date, calendar: Calendar) -> IdentityVFact {
        var stale = false
        if let checked = Self.date(entry.sourceCheckedAt),
           let expiry = calendar.date(byAdding: .day, value: index.staleAfterDays, to: checked) {
            stale = now > expiry
        }
        var cautions: [String] = []
        if entry.confidence == "uncertain" { cautions.append("该条未核实，回答须明示不确定") }
        if entry.confidence == "community" { cautions.append("社区口语/俗称，非官方术语") }
        if entry.versionSensitive { cautions.append("涉及版本或档期，不声称当前数值/最新状态") }
        if entry.gamePatchDate == nil { cautions.append("版本日期未标注，不自称最新") }
        if stale { cautions.append("来源核查已过期，仅作术语索引") }
        return IdentityVFact(id: entry.id, canonical: entry.canonical, summary: entry.summary,
                             type: entry.type, confidence: entry.confidence, sourceURL: entry.sourceURL,
                             sourceCheckedAt: entry.sourceCheckedAt, gamePatchDate: entry.gamePatchDate,
                             versionSensitive: entry.versionSensitive, stale: stale,
                             caution: cautions.isEmpty ? nil : cautions.joined(separator: "；"))
    }

    private static func date(_ value: String) -> Date? {
        let formatter = DateFormatter()
        formatter.dateFormat = "yyyy-MM-dd"
        formatter.timeZone = TimeZone(identifier: "Asia/Shanghai")
        formatter.locale = Locale(identifier: "en_US_POSIX")
        return formatter.date(from: value)
    }
}
