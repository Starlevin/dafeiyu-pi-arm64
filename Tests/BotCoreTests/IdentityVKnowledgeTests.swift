import Foundation
import Testing
@testable import BotCore

/// V1/V2 离线合成单测：全部使用虚构群聊文本与本地夹具，不联网、不发消息、不读取生产数据。
struct IdentityVKnowledgeTests {
    private func indexData() throws -> Data {
        let root = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
        return try Data(contentsOf: root.appendingPathComponent("Resources/IdentityV/identityv_knowledge.json"))
    }

    private func enabled(_ maxFacts: Int = 3) -> IdentityVConfig {
        IdentityVConfig(enabled: true, maxFacts: maxFacts, maxContextMessages: 8)
    }

    private func knowledge(_ config: IdentityVConfig? = nil) throws -> IdentityVKnowledge {
        try IdentityVKnowledge(config: config ?? enabled(), data: indexData())
    }

    // MARK: - 开关 / 降级

    @Test func disabledIsExactNoOpAndNoDataLoad() {
        let disabled = IdentityVKnowledge(config: IdentityVConfig(enabled: false))
        #expect(!disabled.isEnabled)
        let result = disabled.lookup(messages: ["第五人格里压机是什么意思", "飞轮和搏命区别"])
        #expect(result.topic == .noMatch)
        #expect(result.facts.isEmpty)
        #expect(result.matchedIDs.isEmpty)
        #expect(result.noMatch)
    }

    @Test func environmentIsFailClosedByDefault() {
        #expect(!IdentityVConfig.fromEnvironment([:]).enabled)
        #expect(!IdentityVConfig.fromEnvironment(["DAFEIYU_IDV_KNOWLEDGE": "0"]).enabled)
        #expect(!IdentityVConfig.fromEnvironment(["DAFEIYU_IDV_KNOWLEDGE": "maybe"]).enabled)
        #expect(IdentityVConfig.fromEnvironment(["DAFEIYU_IDV_KNOWLEDGE": "1"]).enabled)
        #expect(IdentityVConfig.fromEnvironment(["DAFEIYU_IDV_KNOWLEDGE": "on", "DAFEIYU_IDV_MAX_FACTS": "2"]).maxFacts == 2)
        // 上限受保护，不接受超大值把整篇索引塞进 Prompt
        #expect(IdentityVConfig(enabled: true, maxFacts: 999).maxFacts == 6)
    }

    @Test func corruptDataFallsBackToEmbeddedIndex() {
        let bad = IdentityVKnowledge(config: enabled(), data: Data("{not json".utf8))
        // 内嵌兜底仍可用，不谎称线上同步
        #expect(bad.isEnabled)
        let result = bad.lookup(messages: ["第五人格 压机"])
        #expect(result.topic == .identityV)
    }

    @Test func emptyMessagesReturnNoMatch() throws {
        let result = try knowledge().lookup(messages: [])
        #expect(result.topic == .noMatch)
        #expect(result.facts.isEmpty)
    }

    // MARK: - 主题识别 / unknown / no-match

    @Test func basicTermMatchesWithSourceAndFields() throws {
        let result = try knowledge().lookup(messages: ["这把压机没压好", "开门战直接寄了"])
        #expect(result.topic == .identityV)
        #expect(!result.facts.isEmpty)
        #expect(result.facts.count <= 3)
        #expect(result.facts.allSatisfy { $0.sourceURL.hasPrefix("https://wiki.biligame.com/dwrg/") })
        #expect(result.facts.allSatisfy { !$0.summary.isEmpty && $0.summary.count <= 60 })
        #expect(result.facts.allSatisfy { $0.sourceCheckedAt == "2026-11-10" })
        #expect(result.facts.allSatisfy { $0.gamePatchDate == nil }) // 不自称最新版本
        #expect(result.matchedIDs.contains("term_prime") || result.matchedIDs.contains("term_endgame"))
    }

    @Test func multipleTermsInOneShortSentence() throws {
        let result = try knowledge().lookup(messages: ["飞轮和搏命有什么区别"])
        #expect(result.topic == .identityV)
        #expect(result.matchedIDs.count >= 2)
        #expect(result.facts.count <= 3)
    }

    @Test func nonGameChatIsNoMatch() throws {
        for text in ["今天食堂吃什么", "作业写完了吗", "明天降温记得带伞", "晚安", "借我支笔"] {
            let result = try knowledge().lookup(messages: [text])
            #expect(result.topic == .noMatch, "\(text) 不应激活游戏主题")
            #expect(result.facts.isEmpty)
        }
    }

    @Test func ambiguousDailyWordWithoutGameSignalIsUnknownOrNoMatch() throws {
        // 日常用语单独出现：不得命中角色/技能条目
        for text in ["我手机拿去修机了", "厂长今天开会", "医生说我多休息", "约瑟夫是我同事", "杰克去吃饭了", "律师函收到了", "闪现一下就到"] {
            let result = try knowledge().lookup(messages: [text])
            #expect(result.topic != .identityV, "\(text) 不应识别为游戏主题")
            #expect(result.facts.isEmpty, "\(text) 不应注入事实")
        }
    }

    @Test func ambiguousWordActivatesOnlyWithGameSignalInSameScope() throws {
        let alone = try knowledge().lookup(messages: ["医生说我多休息"])
        #expect(alone.facts.isEmpty)
        let withSignal = try knowledge().lookup(messages: ["第五人格里医生带什么天赋好"])
        #expect(withSignal.topic == .identityV)
        #expect(withSignal.matchedIDs.contains("role_doctor"))
    }

    @Test func followUpKeepsGameFactsUntilConversationNamesAnotherGame() throws {
        let k = try knowledge()
        let opening = "第五人格这把压机没压好"
        let followUp = k.lookup(messages: [opening, "那这时候再开是不是太早了"])
        #expect(followUp.matchedIDs.contains("term_prime"))
        for other in ["三角洲现在大坝人好多", "无畏契约今晚有比赛", "CS2 刚排完一局"] {
            let directSwitch = k.lookup(messages: [opening, other])
            #expect(directSwitch.topic == .noMatch)
            let continuedOther = k.lookup(messages: [opening, other, "那这时候再上有点晚了"])
            #expect(continuedOther.facts.isEmpty)
        }
        let back = k.lookup(messages: [opening, "三角洲刚打完", "第五人格再聊聊飞轮"])
        #expect(back.matchedIDs.contains("skill_flywheel"))
        #expect(!back.matchedIDs.contains("term_prime"))
    }

    @Test func currentTermWinsShortFactBudgetAndNearbySpeakerCannotDisambiguate() throws {
        let k = try knowledge(enabled(1))
        let newest = k.lookup(messages: ["第五人格压机，也叫压最后一台", "飞轮这波交早了"])
        #expect(newest.matchedIDs == ["skill_flywheel"])
        let daily = k.lookup(messages: ["刚看了会第五人格", "医生说我多休息"])
        #expect(daily.facts.isEmpty)
    }

    @Test func unknownTopicWhenSignalButNoFact() throws {
        // 构造：只有泛游戏信号，没有可命中条目
        let result = try knowledge().lookup(messages: ["第五人格 啊这"])
        #expect(result.topic == .unknown || result.topic == .identityV)
        if result.topic == .unknown { #expect(result.facts.isEmpty) }
    }

    // MARK: - 版本 / 置信 / 过期

    @Test func versionSensitiveFactsCarryCautionAndNeverClaimLatest() throws {
        let result = try knowledge().lookup(messages: ["新赛季精华返场了吗", "COA 决赛时间"])
        #expect(result.topic == .identityV)
        let sensitive = result.facts.filter(\.versionSensitive)
        #expect(sensitive.allSatisfy { $0.caution?.contains("版本") == true })
        #expect(result.facts.allSatisfy { !$0.summary.contains("最新") })
    }

    @Test func staleRuleMarksOldSourcesWithoutBreakingLookup() throws {
        let k = IdentityVKnowledge(config: enabled(), data: try indexData())
        let result = k.lookup(messages: ["第五人格 压机"], now: date("2030-01-01"))
        #expect(result.topic == .identityV)
        #expect(result.facts.allSatisfy { $0.stale })
        #expect(result.facts.allSatisfy { $0.caution?.contains("过期") == true })
    }

    // MARK: - 有限上下文 / 上限

    @Test func contextWindowAndFactCapAreBounded() throws {
        let k = try knowledge(enabled(2))
        let many = (0..<40).map { "第\($0)条无关聊天" } + ["第五人格 压机 开门战 飞轮 搏命 震慑"]
        let result = k.lookup(messages: many)
        #expect(result.facts.count <= 2)
        #expect(result.topic == .identityV)
    }

    @Test func indexSchemaAndMinimumCoverage() throws {
        let index = try JSONDecoder().decode(IdentityVKnowledgeIndex.self, from: indexData())
        #expect(index.schemaVersion == 1)
        #expect(index.entries.count >= 60)
        #expect(!index.gameSignals.isEmpty)
        #expect(Set(index.entries.map(\.id)).count == index.entries.count)
        let types = Set(index.entries.map(\.type))
        for expected in ["character", "talent", "map", "match_term", "cosmetic_event", "esports"] {
            #expect(types.contains(expected), "缺少类型 \(expected)")
        }
        #expect(index.entries.filter(\.contextRequired).allSatisfy { $0.ambiguity != nil })
        #expect(index.entries.allSatisfy { $0.summary.count <= 60 })
        #expect(index.entries.allSatisfy { $0.sourceURL.hasPrefix("https://wiki.biligame.com/dwrg/") })
        #expect(index.entries.allSatisfy { $0.gamePatchDate == nil })
    }

    private func date(_ value: String) -> Date {
        let f = DateFormatter(); f.dateFormat = "yyyy-MM-dd"
        f.timeZone = TimeZone(identifier: "Asia/Shanghai"); f.locale = Locale(identifier: "en_US_POSIX")
        return f.date(from: value)!
    }
}
