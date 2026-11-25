import Foundation
import Testing
@testable import WeChatAIBot
@testable import BotCore

/// 第五人格赛程「取数/表达层」离线测试：合成页面文本，不联网、不联系 QQ/微信。
/// 重点覆盖 Codex 审核的：第 3 条（不写死修订时间）、第 4 条（取数失败/问比分永不返回 nil、禁止编造）、
/// 第 1 条（跨群隔离预算与记忆）、第 5 条（字节预算高于实测页面）。
struct IdvScheduleToolTests {
    private func date(_ text: String) -> Date {
        let formatter = DateFormatter()
        formatter.dateFormat = "yyyy-MM-dd HH:mm"
        formatter.locale = Locale(identifier: "en_US_POSIX")
        formatter.timeZone = TimeZone(identifier: "Asia/Shanghai")!
        return formatter.date(from: text)!
    }

    private var tonight: Date { date("2026-11-14 22:05") }

    private var usableQuery: IdvScheduleQuery {
        IdvScheduleQuery(eventDate: "2026-11-14", league: "ivl",
                         intent: .schedule, needsClarification: [])
    }

    // MARK: 第 4 条：问比分/赛果 —— 永不编造，明确「无法核实」

    @Test func resultsIntentReturnsNonNilUnavailableText() throws {
        let query = IdvScheduleQuery(eventDate: nil, league: nil,
                                     intent: .results, needsClarification: [])
        let fragment = IdvScheduleTool.promptFragment(
            query: query, pageText: nil as String?,
            meta: .unknown, fetchedAt: tonight)
        #expect(fragment.contains("无法核实"))
        #expect(fragment.contains("不支持"))
        #expect(fragment.contains("不得推测胜负或比分"))
    }

    // MARK: 第 4 条：取数失败（pageText == nil）—— 返回诚实降级，绝不返回 nil

    @Test func fetchFailureReturnsNonNilDegradedText() throws {
        let fragment = IdvScheduleTool.promptFragment(
            query: usableQuery, pageText: nil as String?,
            meta: .unknown, fetchedAt: tonight,
            failure: "网络超时")
        #expect(fragment.contains("数据不可用"))
        #expect(fragment.contains("网络超时"))
        #expect(fragment.contains("严禁"))
    }

    // MARK: 第 3 条：页面编辑时间动态核验，绝不写死；核验不到就明说未知

    @Test func verifiedRevisionIsLabeledAsEditTimeNotFreshness() throws {
        let page = "11月14日 2026 IVL 秋季赛-近期赛程：14:00：MRC VS GR；16:00：Wolves VS FPX.ZQ\n"
        let meta = IdvPageMeta(revisionDate: date("2026-04-17 08:52"), verified: true, note: "页面编辑时间")
        let fragment = IdvScheduleTool.promptFragment(
            query: usableQuery, pageText: page,
            meta: meta, fetchedAt: tonight)
        #expect(fragment.contains("2026-04-17 08:52"))
        #expect(fragment.contains("页面编辑时间"))
        // 必须声明：编辑时间不是赛程刷新时间，更不是赛果时间
        #expect(fragment.contains("不等于赛程内容刷新时间"))
        #expect(fragment.contains("抓取时间"))
    }

    @Test func unverifiedMetaSaysUnknownNotFabricatedDate() throws {
        let page = "11月14日 2026 IVL 秋季赛-近期赛程：14:00：MRC VS GR\n"
        let fragment = IdvScheduleTool.promptFragment(
            query: usableQuery, pageText: page,
            meta: .unknown, fetchedAt: tonight)
        #expect(fragment.contains("来源内容更新时间未知"))
        // 绝不得出现任何具体修订日期（未核验就不得编造）
        #expect(!fragment.contains("2026-04-17"))
    }

    @Test func parseInlineEditedIsDynamicNotHardcoded() throws {
        // 解析「最后编辑于」动态兜底，不依赖写死的 2026-04-17
        let html = "<footer>最后编辑于2025年8月3日</footer>"
        let d = try #require(IdvScheduleTool.parseInlineEdited(html))
        // 取数层按 Asia/Shanghai 构造日期，回读必须用同一时区（否则 UTC 会差一天）。
        var cal = Calendar(identifier: .gregorian)
        cal.timeZone = TimeZone(identifier: "Asia/Shanghai")!
        let parts = cal.dateComponents([.year, .month, .day], from: d)
        #expect(parts.year == 2025)
        #expect(parts.month == 8)
        #expect(parts.day == 3)
    }

    // MARK: 来源与抓取时间随答案展示（红线）

    @Test func scheduleFragmentCarriesSourceAndFetchTime() throws {
        let page = "11月14日 2026 IVL 秋季赛-近期赛程：14:00：MRC VS GR；16:00：Wolves VS FPX.ZQ\n"
        let fragment = IdvScheduleTool.promptFragment(
            query: usableQuery, pageText: page,
            meta: .unknown, fetchedAt: tonight)
        #expect(fragment.contains("BWIKI"))
        #expect(fragment.contains("抓取时间"))
        #expect(fragment.contains("14:00 MRC vs GR"))
        #expect(fragment.contains("16:00 Wolves vs FPX.ZQ"))
        #expect(fragment.contains("非赛果"))
        // 赛程段里不得出现「比分」字样（赛程 ≠ 赛果）
        #expect(!fragment.contains("比分："))
    }

    // MARK: 第 2 条：行内日期必须核对年份，跨年不串

    @Test func lineForMatchesYearNotJustMonthDay() throws {
        let page = "2025年11月14日 IVL：14:00：A VS B\n"
        // 问的是 2026-11-14，页面只有 2025 同名日期 → 不应命中
        #expect(IdvScheduleTool.lineFor(day: "2026-11-14", league: "ivl", in: page) == nil)
        // 问 2025 才命中
        #expect(IdvScheduleTool.lineFor(day: "2025-11-14", league: "ivl", in: page) != nil)
    }

    @Test func scheduleKeepsNamedOpponentsAndDoesNotMixLeaguesOrScores() throws {
        let page = """
        11月14日 2026 IJL 秋季赛-近期赛程：14:00：ZETA VS FL；16:00：AXIZ vs QTD；18:00
        11月14日 2026 IJL 秋季赛-近期赛程：14:00：ZETA VS FL
        11月14日 2026 IJL 秋季赛-近期赛程：20:00：RC VS SZ（比分 2:0）
        11月14日 2026 IVL 秋季赛-近期赛程：19:00：TE VS GR
        """
        let line = try #require(IdvScheduleTool.lineFor(day: "2026-11-14", league: "ijl", in: page))
        #expect(line == "2026-11-14 IJL（日本时间/JST(UTC+9)）：14:00 ZETA vs FL；16:00 AXIZ vs QTD；18:00；20:00")
        #expect(!line.contains("比分"))
        #expect(!line.contains("TE"))
    }

    // MARK: 缺日期 / 缺分类 → 追问，不臆造

    @Test func missingLeagueReturnsClarifyPrompt() throws {
        let query = IdvScheduleQuery(eventDate: "2026-11-14", league: nil,
                                     intent: .schedule, needsClarification: ["赛事分类"])
        let fragment = IdvScheduleTool.promptFragment(
            query: query, pageText: "11月14日 2026 IVL",
            meta: .unknown, fetchedAt: tonight)
        #expect(fragment.contains("请先用一句话问清"))
        #expect(fragment.contains("不得"))
    }

    // MARK: 第 1 条：跨群隔离的预算与分类记忆

    @Test func budgetIsPerScopeAndPerDay() async {
        let budget = IdvScheduleBudget(perChatDaily: 2)
        #expect(await budget.take("qq:group:A", day: "2026-11-14"))
        #expect(await budget.take("qq:group:A", day: "2026-11-14"))
        #expect(await budget.take("qq:group:A", day: "2026-11-14") == false)  // 同群超额
        #expect(await budget.take("qq:group:B", day: "2026-11-14"))           // 别群不受影响
        #expect(await budget.take("qq:group:A", day: "2026-11-15"))           // 次日恢复
    }

    @Test func leagueMemoryNeverCrossesScopes() async {
        let memory = IdvLeagueMemory(ttl: 1800)
        await memory.set("ivl", scope: "qq:group:A", now: tonight)
        await memory.set("ijl", scope: "qq:group:B", now: tonight)
        #expect(await memory.get("qq:group:A", now: tonight) == "ivl")
        #expect(await memory.get("qq:group:B", now: tonight) == "ijl")
        #expect(await memory.get("qq:group:C", now: tonight) == nil)
        #expect(await memory.get("wx:room:X", now: tonight) == nil)
    }

    // MARK: 第 5 条：默认字节预算高于近期赛程页

    @Test func defaultBudgetExceedsMeasuredPage() {
        #expect(IdvScheduleConfig.measuredPageBytes == 258_523)
        #expect(IdvScheduleConfig().maxBytes > IdvScheduleConfig.measuredPageBytes)
        #expect(IdvScheduleConfig().canHoldMeasuredPage)
    }

    // MARK: 解析修订时间（动态，非写死）

    @Test func parseRevisionDateFromJson() throws {
        let json = #"{"query":{"pages":{"1":{"revisions":[{"timestamp":"2026-04-17T08:52:53Z"}]}}}}"#
        let d = try #require(IdvScheduleTool.parseRevisionDate(json))
        let fmt = ISO8601DateFormatter()
        fmt.formatOptions = [.withInternetDateTime]
        #expect(fmt.string(from: d) == "2026-04-17T08:52:53Z")
    }

    @Test func wikiBotExtractionRequiresSpan() {
        #expect(IdvScheduleTool.wikiBotText("<div>赛程</div>") == nil)
        let text = IdvScheduleTool.wikiBotText("<span class=\"wiki-bot\">11月14日 2026 IVL</span>")
        #expect(text?.contains("IVL") == true)
    }

    // MARK: 配置默认关闭、越界值 fail-closed（第 5 条相关的预算安全）

    @Test func configurationDefaultsOffAndFailClosed() {
        #expect(!IdvScheduleConfig.fromEnvironment([:]).enabled)
        #expect(!IdvScheduleConfig.fromEnvironment(["DAFEIYU_IDV_SCHEDULE": "maybe"]).enabled)
        #expect(IdvScheduleConfig.fromEnvironment(["DAFEIYU_IDV_SCHEDULE": "true"]).enabled)
        // 越界值不生效，保持默认 1 MiB（实测页面约 436 KiB，默认留足余量）。
        #expect(IdvScheduleConfig.fromEnvironment([:]).maxBytes == 1024 * 1024)
        #expect(IdvScheduleConfig.fromEnvironment(["DAFEIYU_IDV_SCHEDULE_MAX_BYTES": "999999999"]).maxBytes == 1024 * 1024)
        #expect(IdvScheduleConfig.fromEnvironment([:]).canHoldMeasuredPage)
    }

    /// 第 5 条核心：默认上限须高于实测页面（约 436 KiB），避免必然截断。
    @Test func defaultByteBudgetExceedsMeasuredPage() {
        #expect(IdvScheduleConfig.measuredPageBytes == 258_523)
        #expect(IdvScheduleConfig().maxBytes > IdvScheduleConfig.measuredPageBytes)
    }
}
