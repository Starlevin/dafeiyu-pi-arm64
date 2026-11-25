import Foundation
#if canImport(FoundationNetworking)
import FoundationNetworking
#endif
import BotCore

/// 第五人格赛程取数（**复用现有常驻服务**：不新增进程/端口/定时器）。
///
/// 审核修正要点（逐条对应）：
/// * 第 3 条：**不再写死 `revisionISO`**。每次运行时通过 MediaWiki API 核验页面修订时间；
///   核验失败一律返回 `nil` 元数据 → 答案里写「来源内容更新时间未知」，绝不伪造日期。
///   即使拿到修订时间，也只描述为「页面编辑时间」，明确声明它不是赛程内容刷新时间。
/// * 第 4 条：区分「取数成功」与「不可用」。本层只负责诚实的*数据*表达；
///   是否要让模型改写由上层决定（见 `promptFragment` 的 `degraded` 语义）。
/// * 第 5 条：默认上限高于实测页面大小（约 436 KiB），避免必然截断。
public struct IdvScheduleConfig: Sendable, Equatable {
    public var enabled: Bool = false
    /// 实测页面约 436 KiB；默认 1 MiB 留足余量（仍远低于 2 MiB 硬上限）。
    public var maxBytes: Int = 1024 * 1024
    public var timeout: TimeInterval = 6.0
    public var perChatDaily: Int = 6
    /// 会话内「上次确认的分类」的有效期（秒）。过期即不可复用，缺分类时明确追问。
    public var leagueMemorySeconds: TimeInterval = 30 * 60
    /// 页面缓存有效期（秒）。短缓存只为合并连发追问，不掩盖内容陈旧。
    public var pageCacheSeconds: TimeInterval = 300

    public init() {}

    public static func fromEnvironment(_ env: [String: String] = ProcessInfo.processInfo.environment) -> Self {
        var config = Self()
        let raw = env["DAFEIYU_IDV_SCHEDULE"]?.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
        config.enabled = raw.map { ["1", "true", "on", "yes"].contains($0) } ?? false
        if let bytes = Int(env["DAFEIYU_IDV_SCHEDULE_MAX_BYTES"] ?? ""), (64 * 1024...(2 * 1024 * 1024)).contains(bytes) {
            config.maxBytes = bytes
        }
        if let seconds = Double(env["DAFEIYU_IDV_SCHEDULE_TIMEOUT"] ?? ""), (0.5...15).contains(seconds) {
            config.timeout = seconds
        }
        return config
    }

    /// 字节预算是否足以容纳实测页面（防止把上限调到会必然失败的过小值）。
    public var canHoldMeasuredPage: Bool { maxBytes > Self.measuredPageBytes }

    /// 实测页面大小（2026-11-14 多次实测 436,632 字节）。仅作预算校验基准。
    public static let measuredPageBytes = 436_632
}

/// 页面元数据核验结果。`revisionDate == nil` 表示**未能核验**，调用方必须如实说未知。
public struct IdvPageMeta: Sendable, Equatable {
    /// 页面编辑时间（来自 MediaWiki revisions API）。**不是**赛程内容刷新时间。
    public let revisionDate: Date?
    /// 是否成功核验到元数据（与 revisionDate 是否为空区分：核验本身可能失败）。
    public let verified: Bool
    /// 失败/未知原因的简短说明（不含任何敏感内容）。
    public let note: String

    public static let unknown = IdvPageMeta(revisionDate: nil, verified: false,
                                            note: "来源内容更新时间未知")
}

public enum IdvScheduleTool {
    /// 写死的来源；用户输入永远不能改变它。
    public static let sourceURL = URL(string: "https://wiki.biligame.com/dwrg/%E8%B5%9B%E4%BA%8B")!
    /// 同一站点的 MediaWiki API，用于运行时核验页面修订时间（无需凭据）。
    public static let revisionsURL = URL(string:
        "https://wiki.biligame.com/dwrg/api.php?action=query&format=json"
        + "&prop=revisions&rvprop=timestamp&rvlimit=1&titles=%E8%B5%9B%E4%BA%8B")!

    /// 明确不可用：没有任何可核实来源提供当日比分。答案里必须照实说。
    public static let scoreUnavailableText =
        "无法核实：《第五人格》当日比分或赛果。当前接入的社区赛程页只列赛前安排，不提供可核实的实时比分。"

    public static func makeSession(timeout: TimeInterval) -> URLSession {
        let config = URLSessionConfiguration.ephemeral
        config.timeoutIntervalForRequest = timeout
        config.timeoutIntervalForResource = timeout
        config.requestCachePolicy = .reloadIgnoringLocalCacheData
        config.httpCookieStorage = nil
        return URLSession(configuration: config)
    }

    /// 取一次页面（有界）。任何异常都抛错，由调用方转为诚实降级文本。
    public static func load(session: URLSession, config: IdvScheduleConfig) async throws -> String {
        try await fetchText(session: session, url: sourceURL,
                            limit: config.maxBytes, timeout: config.timeout)
    }

    /// 运行时核验页面修订时间。核验失败返回 `.unknown`（不抛错、不伪造）。
    public static func loadMeta(session: URLSession, config: IdvScheduleConfig) async -> IdvPageMeta {
        do {
            let json = try await fetchText(session: session, url: revisionsURL,
                                           limit: 128 * 1024, timeout: config.timeout)
            guard let date = parseRevisionDate(json) else {
                return .unknown
            }
            return IdvPageMeta(revisionDate: date, verified: true, note: "页面编辑时间")
        } catch {
            return .unknown
        }
    }

    static func fetchText(session: URLSession, url: URL,
                          limit: Int, timeout: TimeInterval) async throws -> String {
        guard url.scheme == "https", url.host == "wiki.biligame.com" else {
            throw URLError(.badURL)
        }
        var request = URLRequest(url: url)
        request.httpMethod = "GET"
        request.timeoutInterval = timeout
        request.setValue("DaFeiYuBot/1.0 (personal assistant)", forHTTPHeaderField: "User-Agent")
        let (data, response) = try await session.boundedData(for: request, limit: limit)
        guard let http = response as? HTTPURLResponse, http.statusCode == 200,
              let text = String(data: data, encoding: .utf8) else {
            throw URLError(.badServerResponse)
        }
        return text
    }

    /// 从 revisions API 的 JSON 里取第一个 timestamp。解析不出就是 nil（不猜）。
    public static func parseRevisionDate(_ json: String) -> Date? {
        guard let regex = try? NSRegularExpression(pattern: "\"timestamp\"\\s*:\\s*\"([^\"]+)\""),
              let match = regex.firstMatch(in: json, range: NSRange(json.startIndex..., in: json)),
              let range = Range(match.range(at: 1), in: json) else { return nil }
        let formatter = ISO8601DateFormatter()
        formatter.formatOptions = [.withInternetDateTime]
        return formatter.date(from: String(json[range]))
    }

    /// 从页面内嵌页脚「最后编辑于2026年4月17日」兜底解析（API 不可用时）。
    public static func parseInlineEdited(_ html: String) -> Date? {
        guard let regex = try? NSRegularExpression(
                pattern: "最后编辑于\\s*(20\\d{2})\\s*年\\s*(\\d{1,2})\\s*月\\s*(\\d{1,2})\\s*日"),
              let m = regex.firstMatch(in: html, range: NSRange(html.startIndex..., in: html)),
              let y = Range(m.range(at: 1), in: html), let mo = Range(m.range(at: 2), in: html),
              let d = Range(m.range(at: 3), in: html),
              let year = Int(html[y]), let month = Int(html[mo]), let day = Int(html[d]) else {
            return nil
        }
        var cal = Calendar(identifier: .gregorian)
        cal.timeZone = TimeZone(identifier: "Asia/Shanghai")!
        return cal.date(from: DateComponents(year: year, month: month, day: day))
    }

    /// 按北京时间取「当天」字符串，用于每日额度分桶（与赛事日期口径一致）。
    public static func day(_ date: Date) -> String {
        let formatter = DateFormatter()
        formatter.dateFormat = "yyyy-MM-dd"
        formatter.locale = Locale(identifier: "en_US_POSIX")
        formatter.timeZone = TimeZone(identifier: "Asia/Shanghai")
        return formatter.string(from: date)
    }

    /// 从页面文本里抽出 `<span class="wiki-bot">` 的纯文本（正则级，够用且无依赖）。
    public static func wikiBotText(_ html: String) -> String? {
        let pattern = "<span[^>]*class=\"[^\"]*wiki-bot[^\"]*\"[^>]*>(.*?)</span>"
        guard let regex = try? NSRegularExpression(pattern: pattern, options: [.dotMatchesLineSeparators]) else {
            return nil
        }
        let range = NSRange(html.startIndex..., in: html)
        let matches = regex.matches(in: html, options: [], range: range)
        guard !matches.isEmpty else { return nil }
        let parts = matches.compactMap { match -> String? in
            guard let r = Range(match.range(at: 1), in: html) else { return nil }
            return stripTags(String(html[r]))
        }
        let joined = parts.joined(separator: "\n")
        return joined.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty ? nil : joined
    }

    static func stripTags(_ text: String) -> String {
        text.replacingOccurrences(of: "<[^>]+>", with: " ", options: .regularExpression)
            .replacingOccurrences(of: "&nbsp;", with: " ")
            .replacingOccurrences(of: "&amp;", with: "&")
    }

    /// 生成可注入 `rule.prompt` 的段落。**永不返回 nil**（第 4 条）：
    /// 取不到数据时返回明确「不可用」的指令文本，避免主模型借机编造。
    public static func promptFragment(query: IdvScheduleQuery, pageText: String?,
                                      meta: IdvPageMeta, fetchedAt: Date,
                                      failure: String? = nil) -> String {
        let stamp = formatBeijing(fetchedAt)

        if query.asksResults {
            return "[第五人格赛事·如实回答] " + scoreUnavailableText
                + "必须明确说无法核实、不支持实时比分，不得推测胜负或比分。"
        }

        guard query.usable, let day = query.eventDate, let league = query.league else {
            var ask: [String] = []
            if query.eventDate == nil { ask.append("是今天、明天，还是具体某天（如 11月14日）") }
            if query.league == nil { ask.append("IVL 还是 IJL（或 COA）") }
            if ask.isEmpty { return "[第五人格赛事·如实回答] 没有足够信息，不得猜测赛事安排。" }
            return "[第五人格赛事·需追问] 请先用一句话问清：" + ask.joined(separator: "、")
                + "。在用户说清之前，不得给出任何具体对阵或时间。"
        }

        // 取数失败 / 结构变了：明确「本赛事数据不可用」，禁止编造（第 4 条核心）。
        guard let pageText else {
            let reason = failure ?? "未能取得赛程数据"
            return "[第五人格赛事·数据不可用] \(reason)。本次没有可用的《第五人格》赛程数据；"
                + "必须直接告诉用户「暂时取不到赛程，请以官方渠道为准」，"
                + "严禁凭记忆或推测给出任何对阵、时间或比分。"
        }

        guard let block = lineFor(day: day, league: league, in: pageText) else {
            return "[第五人格赛事·数据不可用] 来源里没有 \(day) \(league.uppercased()) 的赛程条目。"
                + "必须如实说「来源里没有这一天的赛程」，不得用其它日期或其它联赛的内容顶替。"
        }

        var lines = [
            "[第五人格赛事｜低信任资料] 以下是社区赛程页的**赛前安排**，不是实时比分也不是赛果；"
            + "只能按事实转述，不得延伸推测。",
            "【社区赛程·非赛果】" + block,
            "来源：BWIKI 第五人格赛事页（社区维护，非官方公告）；抓取时间：\(stamp)（北京时间）",
        ]
        // 第 3 条：只陈述「页面编辑时间」，核验不到就明说未知，绝不伪造。
        if meta.verified, let revision = meta.revisionDate {
            lines.append("页面编辑时间：\(formatBeijing(revision))（这是页面编辑时间，"
                         + "不等于赛程内容刷新时间，也不等于赛果更新时间）")
        } else {
            lines.append("来源内容更新时间未知（未能核验页面元数据）")
        }
        lines.append("赛程不是赛果；实时比分不支持，不得编造。")
        return lines.joined(separator: "\n")
    }

    static func formatBeijing(_ date: Date) -> String {
        let formatter = DateFormatter()
        formatter.dateFormat = "yyyy-MM-dd HH:mm:ss"
        formatter.locale = Locale(identifier: "en_US_POSIX")
        formatter.timeZone = TimeZone(identifier: "Asia/Shanghai")
        return formatter.string(from: date)
    }

    /// 在页面文本里找「该年该月该日 + 分类 + 时刻」那一行。
    ///
    /// 审核第 2 条：必须**核对年份**，不能只看月/日——页面可能同时含有往年同名日期。
    static func lineFor(day: String, league: String, in pageText: String) -> String? {
        let parts = day.split(separator: "-")
        guard parts.count == 3,
              let year = Int(parts[0]), let month = Int(parts[1]), let dayOfMonth = Int(parts[2]),
              (2000...2099).contains(year) else { return nil }
        let leagueToken = league.uppercased()

        for rawLine in pageText.split(separator: "\n") {
            let line = String(rawLine)
            guard line.contains(leagueToken) else { continue }
            guard lineMentions(date: (year, month, dayOfMonth), in: line) else { continue }
            guard let colon = line.firstIndex(of: "：") ?? line.firstIndex(of: ":") else { continue }
            let tail = String(line[line.index(after: colon)...])
            let times = extractTimes(tail)
            guard !times.isEmpty else { continue }
            let zone = league == "ijl" ? "日本时间/JST(UTC+9)" : "北京时间/UTC+8"
            return "\(day) \(leagueToken)（\(zone)）：" + times.joined(separator: "、")
        }
        return nil
    }

    /// 行内日期是否**完整匹配**（年+月+日）。支持两种真实写法：
    /// `11月14日 2026`（月日在前、年份后置）与 `2026年11月14日`（年份在前）。
    /// 只写「11月14日」而无年份 → 视为不匹配（跨年时不可靠，宁可不给）。
    static func lineMentions(date: (year: Int, month: Int, day: Int), in line: String) -> Bool {
        let (year, month, day) = date
        // 写法 A：11月14日 2026 / 11月14日2026年
        let trailing = "(?<!\\d)\(month)\\s*月\\s*\(day)\\s*日?\\s*[（(]?\\s*\(year)\\s*年?(?!\\d)"
        // 写法 B：2026年11月14日 / 2026-11-14
        let leading = "(?<!\\d)\(year)\\s*[年/\\-.]\\s*\(month)\\s*[月/\\-.]\\s*\(day)\\s*日?(?!\\d)"
        for pattern in [trailing, leading] {
            if let regex = try? NSRegularExpression(pattern: pattern),
               regex.firstMatch(in: line, range: NSRange(line.startIndex..., in: line)) != nil {
                return true
            }
        }
        return false
    }

    static func extractTimes(_ text: String) -> [String] {
        guard let regex = try? NSRegularExpression(pattern: "([01]?\\d|2[0-3])\\s*[:：]\\s*([0-5]\\d)") else {
            return []
        }
        let range = NSRange(text.startIndex..., in: text)
        var found: [String] = []
        for match in regex.matches(in: text, options: [], range: range) {
            guard let hr = Range(match.range(at: 1), in: text),
                  let mn = Range(match.range(at: 2), in: text),
                  let hour = Int(text[hr]), let minute = Int(text[mn]) else { continue }
            let value = String(format: "%02d:%02d", hour, minute)
            if !found.contains(value) { found.append(value) }
        }
        return found
    }
}

/// 进程内每日额度（按会话计）。不写盘，重启即清零，不污染既有持久化。
public actor IdvScheduleBudget {
    private var used: [String: [String: Int]] = [:]
    private let limit: Int
    public init(perChatDaily: Int) { self.limit = max(0, perChatDaily) }

    public func take(_ scope: String, day: String) -> Bool {
        var perDay = used[scope] ?? [:]
        perDay = perDay.filter { $0.key == day }   // 只保留当天，防无限增长
        let current = perDay[day] ?? 0
        guard current < limit else { used[scope] = perDay; return false }
        perDay[day] = current + 1
        used[scope] = perDay
        return true
    }
}

/// 会话隔离的「上次确认分类」记忆（审核第 1 条）。
///
/// * 键是 `scopeKey`（平台|账号|会话），**绝不跨群共享**；
/// * 有条目上限与过期时间，避免无界增长与陈旧串用；
/// * 过期后返回 nil，调用方据此明确追问，而不是沿用旧分类。
public actor IdvLeagueMemory {
    private struct Entry { let league: String; let at: Date }
    private var entries: [String: Entry] = [:]
    private let ttl: TimeInterval
    private let capacity: Int

    public init(ttl: TimeInterval, capacity: Int = 64) {
        self.ttl = ttl
        self.capacity = max(1, capacity)
    }

    public func set(_ league: String, scope: String, now: Date = Date()) {
        prune(now: now)
        if entries.count >= capacity, entries[scope] == nil,
           let oldest = entries.min(by: { $0.value.at < $1.value.at })?.key {
            entries.removeValue(forKey: oldest)
        }
        entries[scope] = Entry(league: league, at: now)
    }

    /// 只在未过期时返回；过期即清掉并返回 nil（让上层明确追问）。
    public func get(_ scope: String, now: Date = Date()) -> String? {
        prune(now: now)
        guard let entry = entries[scope] else { return nil }
        return entry.league
    }

    private func prune(now: Date) {
        entries = entries.filter { now.timeIntervalSince($0.value.at) <= ttl }
    }

    public func count() -> Int { entries.count }
}

extension IdvScheduleTool {
    /// 共享给 QQEngine 与 UnifiedReplyCore 的单一赛程取数协调器：
    /// 按 scope 隔离每日额度与短期分类记忆，不跨群串用。
    public actor Coordinator {
        public let config: IdvScheduleConfig
        private let session: URLSession
        private let budget: IdvScheduleBudget
        private let leagueMemory: IdvLeagueMemory
        private var pageCache: (text: String, meta: IdvPageMeta, fetchedAt: Date)?

        public init(config: IdvScheduleConfig = .fromEnvironment(),
                    session: URLSession? = nil) {
            self.config = config
            self.session = session ?? IdvScheduleTool.makeSession(timeout: config.timeout)
            self.budget = IdvScheduleBudget(perChatDaily: config.perChatDaily)
            self.leagueMemory = IdvLeagueMemory(ttl: config.leagueMemorySeconds)
        }

        public func fragment(for message: String, scope: String, now: Date = Date()) async -> String? {
            guard config.enabled else { return nil }
            let defaultLeague = await leagueMemory.get(scope, now: now)
            guard let query = IdvSchedulePlanner.plan(
                message: message, now: now, defaultLeague: defaultLeague) else {
                return nil
            }
            if !query.usable {
                return IdvScheduleTool.promptFragment(
                    query: query, pageText: nil as String?,
                    meta: IdvPageMeta.unknown, fetchedAt: now)
            }
            guard await budget.take(scope, day: IdvScheduleTool.day(now)) else {
                return "[第五人格赛事·额度用尽] 本会话今日赛程查询次数已达上限，"
                    + "请如实说明稍后再问，不得凭记忆给出对阵或时间。"
            }

            var pageText: String?
            var meta = IdvPageMeta.unknown
            var failure: String?
            if let cached = pageCache,
               now.timeIntervalSince(cached.fetchedAt) < config.pageCacheSeconds {
                pageText = cached.text
                meta = cached.meta
            } else {
                do {
                    let html = try await IdvScheduleTool.load(session: session, config: config)
                    pageText = IdvScheduleTool.wikiBotText(html)
                    if pageText == nil { failure = "来源页面结构可能已更改，未解析到赛程数据" }
                    meta = await IdvScheduleTool.loadMeta(session: session, config: config)
                    if !meta.verified, let fallback = IdvScheduleTool.parseInlineEdited(html) {
                        meta = IdvPageMeta(revisionDate: fallback, verified: true, note: "页面内嵌编辑时间")
                    }
                    if let text = pageText { pageCache = (text, meta, now) }
                } catch {
                    failure = "取数失败（网络、超时或来源不可达）"
                }
            }

            if query.usable, let league = query.league {
                await leagueMemory.set(league, scope: scope, now: now)
            }
            return IdvScheduleTool.promptFragment(
                query: query, pageText: pageText,
                meta: meta, fetchedAt: now, failure: failure)
        }
    }
}
