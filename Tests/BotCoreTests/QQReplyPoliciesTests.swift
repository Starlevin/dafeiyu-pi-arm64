import Foundation
import Testing
@testable import BotCore

struct QQReplyPoliciesTests {
    @Test func scenariosMatchGreetingConversationAndExplanations() {
        #expect(QQReplyLengthPolicy.scenario(for: "晚安") == .greeting)
        #expect(QQReplyLengthPolicy.scenario(for: "哈哈哈") == .greeting)
        #expect(QQReplyLengthPolicy.scenario(for: "今天游戏打得真舒服，队友还挺会配合的") == .conversation)
        #expect(QQReplyLengthPolicy.scenario(for: "帮我分析一下为什么系统无法启动") == .explanation)
        #expect(QQReplyLengthPolicy.scenario(for: "how to fix this") == .explanation)
    }

    @Test func limitsRespectExistingConfigAndNeverExtendPermissions() {
        #expect(QQReplyLengthPolicy.maximum(for: "你好", configured: 160) == 50)
        #expect(QQReplyLengthPolicy.maximum(for: "今天游戏打得挺好", configured: 160) == 120)
        #expect(QQReplyLengthPolicy.maximum(for: "请详细分析错误原因和解决步骤", configured: 160) == 160)
        #expect(QQReplyLengthPolicy.maximum(for: "请详细分析错误原因", configured: 60) == 60)
        #expect(QQReplyLengthPolicy.maximum(for: "你好", configured: 20) == 20)
        #expect(QQReplyLengthPolicy.maximum(for: "需要详细步骤吗", configured: 160, group: true, proactive: true) == 50)
    }

    @Test func scenePromptAvoidsForcingLongAnswers() {
        let short = QQReplyLengthPolicy.instruction(for: "早上好", maximum: 50)
        let detail = QQReplyLengthPolicy.instruction(for: "为什么失败，如何修复", maximum: 160)
        #expect(short.contains("一两句"))
        #expect(detail.contains("原因") || detail.contains("依据"))
        #expect(detail.contains("160"))
        #expect(detail.contains("不是必须凑满") || detail.contains("无需凑"))
    }

    @Test func repeatedMessagesAreConservativelyDetected() {
        let repeated = "这个问题需要先确认配置文件，然后再检查运行日志。"
        #expect(QQReplyRepetitionPolicy.isRepeated("这个问题需要先确认配置文件然后再检查运行日志！", among: [repeated]))
        #expect(!QQReplyRepetitionPolicy.isRepeated("这个问题不需要先确认配置文件，然后再检查运行日志。", among: [repeated]))
        #expect(!QQReplyRepetitionPolicy.isRepeated("你好", among: ["你好"]))
        #expect(!QQReplyRepetitionPolicy.isRepeated("全新内容值得回应", among: [repeated]))
    }

    @Test func personaDecodeRespectsDynamicCap() {
        var p = QQPersona()
        p.maxCharacters = QQReplyLengthPolicy.maximum(for: "早安", configured: 160)
        let long = String(repeating: "早安啦，今天阳光真不错，", count: 12)
        let data = try! JSONSerialization.data(withJSONObject: ["text": long, "emotion": "joy", "intensity": 1])
        let reply = p.decode(String(decoding: data, as: UTF8.self), input: "早安")
        #expect(reply.text.count <= 50)
        #expect(!reply.isFallback)
    }

    @Test func socialStyleSettingsDefaultOffAndParseFlags() {
        let off = QQSocialStyleSettings.load([:])
        #expect(!off.styleEnabled)
        #expect(!off.stickerRerankEnabled)
        #expect(!off.shadowEnabled)
        #expect(!off.isActive)
        #expect(!off.needsPlan)

        let shadow = QQSocialStyleSettings.load([
            "DAFEIYU_SOCIAL_STYLE": "1",
            "DAFEIYU_STICKER_RERANK": "1",
            "DAFEIYU_SOCIAL_STYLE_SHADOW": "1"
        ])
        #expect(shadow.styleEnabled)
        #expect(shadow.stickerRerankEnabled)
        #expect(shadow.shadowEnabled)
        #expect(!shadow.isActive)
        #expect(shadow.logsStyle)

        let active = QQSocialStyleSettings.load([
            "DAFEIYU_SOCIAL_STYLE": "1",
            "DAFEIYU_STICKER_RERANK": "1",
            "DAFEIYU_SOCIAL_STYLE_SHADOW": "0"
        ])
        #expect(active.isActive)
        #expect(active.stickerRerankEnabled)
    }

    @Test func socialStyleDirectorPlansScenesAndStickerCooldown() {
        let now = Date(timeIntervalSince1970: 1_700_000_000)
        let help = QQSocialStyleDirector.plan(for: "启动报错怎么解决", configuredMaxChars: 160, now: now)
        #expect(help.scene == .seriousHelp)
        #expect(help.tone == .neutral)
        #expect(help.stickerIntent == .none)

        let celebration = QQSocialStyleDirector.plan(for: "面试通过了太棒", configuredMaxChars: 160, now: now)
        #expect(celebration.scene == .celebration)
        #expect(celebration.tone == .celebratory)
        #expect(celebration.stickerIntent == .preferred)
        #expect(celebration.maxChars == 50)
        #expect(celebration.logLine(mode: "active").contains("[social_style] scene=celebration"))

        let cooled = QQSocialStyleDirector.plan(for: "面试通过了太棒", configuredMaxChars: 160,
                                                lastStickerAt: now.addingTimeInterval(-60), now: now)
        #expect(cooled.stickerIntent == .none)
    }
}
