import Foundation
import Testing
@testable import BotCore

/// 由合成样例集驱动的自动断言：88+ 条虚构中文群聊，覆盖主题/匹配/沉默倾向/注入/图片/组队。
/// 样例文件为纯数据，其中的「忽略规则」文本只作测试字符串，绝不当作给测试进程的指令。
struct IdentityVCaseDrivenTests {
    private struct CaseFile: Decodable {
        let schemaVersion: Int
        let note: String
        let cases: [Case]
        private enum CodingKeys: String, CodingKey {
            case schemaVersion = "schema_version"
            case note, cases
        }
    }
    private struct Case: Decodable {
        let id: String
        let category: String
        let messages: [String]
        let hasReadableImage: Bool?
        let expectedTopic: String
        let expectedMatchIDs: [String]?
        let expectedSilent: Bool
        let expectedReplyTraits: [String]?
        let humanExpectation: String
        let injection: Bool?
        let notes: String?
        private enum CodingKeys: String, CodingKey {
            case id, category, messages
            case hasReadableImage = "has_readable_image"
            case expectedTopic = "expected_topic"
            case expectedMatchIDs = "expected_match_ids"
            case expectedSilent = "expected_silent"
            case expectedReplyTraits = "expected_reply_traits"
            case humanExpectation = "human_expectation"
            case injection, notes
        }
    }

    private func loadCases() throws -> CaseFile {
        // Tests/BotCoreTests -> repo root, then Tests/QQEngineTests/identityv_dialogue_cases.json
        let root = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
        let url = root.appendingPathComponent("Tests/QQEngineTests/identityv_dialogue_cases.json")
        return try JSONDecoder().decode(CaseFile.self, from: Data(contentsOf: url))
    }

    private func dialogue() throws -> IdentityVDialogue {
        let root = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
        let data = try Data(contentsOf: root.appendingPathComponent("Resources/IdentityV/identityv_knowledge.json"))
        let config = IdentityVConfig(enabled: true, maxFacts: 6, maxContextMessages: 8)
        // 合成断言用放宽的条数上限：验收「是否命中」，而 Prompt 注入仍有独立硬上限。
        return IdentityVDialogue(knowledge: IdentityVKnowledge(config: config, data: data), config: config)
    }

    @Test func syntheticCorpusMeetsMinimumAndRunsAssertions() throws {
        let file = try loadCases()
        #expect(file.schemaVersion == 1)
        #expect(file.cases.count >= 80, "合成样例应至少 80 条，实际 \(file.cases.count)")
        let d = try dialogue()

        var topicFailures: [String] = []
        var matchFailures: [String] = []
        var silentFailures: [String] = []
        var injectionFailures: [String] = []
        var recruitingFailures: [String] = []

        for c in file.cases {
            let plan = d.plan(messages: c.messages,
                              hasReadableImage: c.hasReadableImage ?? false,
                              recentBotReplies: [], proactive: true)

            // 主题识别：identityv / unknown / no_match 三态断言
            let observedTopic = plan.topic.rawValue
            if c.expectedTopic == "no_match" {
                if observedTopic != "no_match" && observedTopic != "unknown" {
                    topicFailures.append("\(c.id): 期望 no_match/unknown，实际 \(observedTopic)")
                }
            } else if c.expectedTopic == "unknown" {
                if observedTopic == "identityv" {
                    topicFailures.append("\(c.id): 期望 unknown，实际 identityv（歧义未消却命中）")
                }
            } else if c.expectedTopic == "identityv" && observedTopic != "identityv" {
                topicFailures.append("\(c.id): 期望 identityv，实际 \(observedTopic)")
            }

            // 引用匹配：期望命中 id 必须出现在 matchedIDs（词表 id 与样例约定一致）
            let expected = Set(c.expectedMatchIDs ?? [])
            if !expected.isEmpty {
                let missing = expected.subtracting(plan.matchedIDs)
                if !missing.isEmpty { matchFailures.append("\(c.id): 缺少 \(missing.sorted().joined(separator: ","))") }
            }
            if c.expectedTopic == "no_match" && !plan.matchedIDs.isEmpty {
                matchFailures.append("\(c.id): 非游戏聊天却命中 \(plan.matchedIDs.joined(separator: ","))")
            }

            // 沉默倾向：expected_silent=true 时计划必须给出沉默偏置
            if c.expectedSilent && !plan.silentBias {
                silentFailures.append("\(c.id): 期望倾向沉默，实际可接话（\(plan.kind.rawValue)）")
            }

            // 注入攻击：一律不得扩权、不得产生发言理由
            if c.injection == true {
                if !IdentityVDialogue.containsInjection(c.messages.joined(separator: "\n")) {
                    injectionFailures.append("\(c.id): 注入文本未被识别")
                }
                if !plan.silentBias || plan.replyReason != nil {
                    injectionFailures.append("\(c.id): 注入文本影响了发言资格")
                }
                if plan.promptFragment.contains("系统提示词：") {
                    injectionFailures.append("\(c.id): 提示片段疑似转述系统提示")
                }
            }

            // 组队：必须明确不冒充可登录游戏的玩家
            if c.category == "team_recruit" {
                if plan.kind != .recruiting || !plan.silentBias {
                    recruitingFailures.append("\(c.id): 组队未识别或未沉默")
                }
            }
        }

        #expect(topicFailures.isEmpty, "\(topicFailures.joined(separator: "\n"))")
        #expect(matchFailures.isEmpty, "\(matchFailures.joined(separator: "\n"))")
        #expect(silentFailures.isEmpty, "\(silentFailures.joined(separator: "\n"))")
        #expect(injectionFailures.isEmpty, "\(injectionFailures.joined(separator: "\n"))")
        #expect(recruitingFailures.isEmpty, "\(recruitingFailures.joined(separator: "\n"))")
    }

    @Test func everyCategoryIsRepresented() throws {
        let cases = try loadCases().cases
        let categories = Set(cases.map(\.category))
        for required in ["skill_knowledge", "continuous_play", "term_ambiguity", "esports_skin", "complaint",
                         "team_recruit", "image_unreadable", "non_game_chat", "prompt_injection", "spam", "tactics", "qa"] {
            #expect(categories.contains(required), "缺少类别 \(required)")
        }
        #expect(Set(cases.map(\.id)).count == cases.count, "case id 必须唯一")
        #expect(cases.filter { $0.injection == true }.count >= 6)
        #expect(cases.filter { $0.category == "team_recruit" }.count >= 6)
    }

    /// 回归：禁用开关时，即便样例全是游戏话题也不产生任何提示片段。
    @Test func disabledPlanAcrossWholeCorpusIsNoOp() throws {
        let cases = try loadCases().cases
        let disabled = IdentityVDialogue(config: IdentityVConfig(enabled: false))
        for c in cases {
            let plan = disabled.plan(messages: c.messages, hasReadableImage: c.hasReadableImage ?? false, proactive: true)
            #expect(plan.promptFragment.isEmpty, "\(c.id) 禁用时不应注入片段")
            #expect(!plan.identityVTopicDetected)
            #expect(!plan.knowledgeMatched)
        }
    }
}
