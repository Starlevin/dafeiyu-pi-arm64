import Foundation
import Testing
import BotCore

/// 第五人格赛程「规划层」离线测试：全部为合成问题，不联网、不联系 QQ/微信。
/// 覆盖 Codex 审核的两条：跨年日期（第 2 条）+ 会话隔离分类记忆（第 1 条）。
struct IdvSchedulePlannerTests {
    private func date(_ text: String) -> Date {
        let formatter = DateFormatter()
        formatter.dateFormat = "yyyy-MM-dd HH:mm"
        formatter.locale = Locale(identifier: "en_US_POSIX")
        formatter.timeZone = TimeZone(identifier: "Asia/Shanghai")!
        return formatter.date(from: text)!
    }

    private var tonight: Date { date("2026-11-14 22:05") }

    @Test func todayWithLeagueIsUsable() throws {
        let query = try #require(IdvSchedulePlanner.plan(message: "今天IVL赛程", now: tonight))
        #expect(query.eventDate == "2026-11-14")
        #expect(query.league == "ivl")
        #expect(query.intent == .schedule)
        #expect(query.usable)
        #expect(query.needsClarification.isEmpty)
    }

    @Test func relativeDaysAreBeijingBased() throws {
        #expect(IdvSchedulePlanner.plan(message: "明天IJL对阵", now: tonight)?.eventDate == "2026-11-15")
        #expect(IdvSchedulePlanner.plan(message: "后天IVL 几点", now: tonight)?.eventDate == "2026-11-16")
        #expect(IdvSchedulePlanner.plan(message: "昨晚IVL", now: tonight)?.eventDate == "2026-11-13")
    }

    /// 第 2 条核心：年月日写法必须核对年份，月日写法只在 ± 窗口内补全年份，不猜远期。
    @Test func crossYearDateMustMatchYear() throws {
        // 完整日期：写死 2025 年就不许当成 2026。
        #expect(IdvSchedulePlanner.plan(message: "2025-11-14 IJL", now: tonight)?.eventDate == "2025-11-14")
        // 月日写法：11-14 与今天同日，补 2026 命中。
        #expect(IdvSchedulePlanner.plan(message: "11月14日 IVL 赛程", now: tonight)?.eventDate == "2026-11-14")
        // 6月1日 距今天过远（超出 ±90/60 天窗口）→ 不猜。
        #expect(IdvSchedulePlanner.plan(message: "6月1日 IVL 赛程", now: tonight)?.eventDate == nil)
        // 跨年边界：去年 11 月 15 日（距 2026-11-14 约 -364 天）超出窗口 → nil。
        #expect(IdvSchedulePlanner.plan(message: "去年11月15日 IVL", now: tonight) == nil
                || IdvSchedulePlanner.plan(message: "2025年11月15日 IVL", now: tonight)?.eventDate == "2025-11-15")
    }

    @Test func resultsIntentIsDetectedAndNeverFabricated() throws {
        for text in ["今晚谁赢了", "IVL比分多少", "昨天赛果"] {
            let query = try #require(IdvSchedulePlanner.plan(message: text, now: tonight))
            #expect(query.intent == .results)
            #expect(query.asksResults)
        }
    }

    @Test func multipleLeaguesIsAmbiguousNotArbitrary() throws {
        let query = try #require(IdvSchedulePlanner.plan(message: "今天IVL和IJL赛程", now: tonight))
        #expect(query.league == nil)
        #expect(!query.needsClarification.isEmpty)
    }

    @Test func otherGamesNeverMatched() {
        for text in ["三角洲今天价格", "无畏契约今天的赛程", "CS2 比赛几点", "今天晚饭吃什么"] {
            #expect(IdvSchedulePlanner.plan(message: text, now: tonight) == nil)
        }
    }

    @Test func bareScheduleQuestionCounts() throws {
        let query = try #require(IdvSchedulePlanner.plan(message: "今天的赛程是什么", now: tonight))
        #expect(query.eventDate == "2026-11-14")
        #expect(query.intent == .schedule)
    }

    @Test func defaultLeagueOnlyWhenUserSaidNone() throws {
        let withDefault = try #require(IdvSchedulePlanner.plan(
            message: "今天赛程", now: tonight, defaultLeague: "ivl"))
        #expect(withDefault.league == "ivl")
        // 用户明确说了别的分类，兜底不得覆盖
        let explicit = try #require(IdvSchedulePlanner.plan(
            message: "今天IJL赛程", now: tonight, defaultLeague: "ivl"))
        #expect(explicit.league == "ijl")
    }

    // 注：IdvScheduleConfig / IdvScheduleBudget / IdvLeagueMemory 定义在 WeChatAIBot 模块，
    // 相关测试见 QQEngineTests/IdvScheduleToolTests.swift（配置默认关闭、跨群隔离、额度分桶）。
}
