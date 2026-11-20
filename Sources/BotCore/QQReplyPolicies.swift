import Foundation

/// Limits the length of an already-authorized reply. It never changes who may be contacted.
public enum QQReplyScenario: String, Sendable {
    case greeting, conversation, explanation
}

public enum QQReplyLengthPolicy {
    public static func scenario(for input: String, group: Bool = false) -> QQReplyScenario {
        let text = input.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
        let compact = text.components(separatedBy: .whitespacesAndNewlines).joined()
        let unpunctuated = compact.trimmingCharacters(in: .punctuationCharacters)
        let greetings: Set<String> = ["嗨", "你好", "早", "早安", "早上好", "晚上好", "晚安", "午安", "在吗", "在嘛", "哈喽",
            "hello", "hi", "hey", "谢谢", "谢啦", "哈哈", "哈哈哈", "嗯", "哦", "好", "好的", "收到", "拜拜", "再见"]
        if compact.isEmpty || greetings.contains(unpunctuated) || (compact.count <= 5 && compact.allSatisfy { "哈嗯哦啊嘿！？!?~～。".contains($0) }) {
            return .greeting
        }
        let detailSignals = [
            "为什么", "怎么做", "怎么解决", "怎么办", "怎么修", "如何", "原理",
            "分析", "详细", "具体", "讲讲", "解释", "步骤", "原因",
            "比较", "区别", "推荐", "建议", "证明", "计算", "教程",
            "配置", "报错", "故障", "失败", "不懂", "不明白", "请问",
            "why", "how to", "explain", "compare", "because"
        ]
        if detailSignals.contains(where: { text.contains($0) }) || (!group && text.count >= 45) {
            return .explanation
        }
        return .conversation
    }

    public static func maximum(for input: String, configured: Int, group: Bool = false, proactive: Bool = false) -> Int {
        let safeCap = min(200, max(20, configured))
        if proactive { return min(50, safeCap) } // Does not enable or alter proactive participation.
        let cap: Int
        switch scenario(for: input, group: group) {
        case .greeting: cap = 50
        case .conversation: cap = 120
        case .explanation: cap = 160
        }
        return min(safeCap, cap)
    }

    public static func instruction(for input: String, group: Bool = false, proactive: Bool = false, maximum: Int) -> String {
        let label: String
        if proactive {
            label = "普通群接话：只回应当下有用的话题，不抢话，不延长闲聊"
        } else {
            switch scenario(for: input, group: group) {
            case .greeting: label = "简短问候或表情：一两句即可，不要无故追问或扩写"
            case .conversation: label = "日常聊天：自然回应并可补充一两个贴题细节，避免机械套话"
            case .explanation: label = "认真提问或复杂话题：优先完整回答问题，必要时说明原因或步骤，不要硬压成一句"
            }
        }
        return "本轮回复长度策略：\(label)。最多 \(maximum) 字（含标点）；这是上限，不是必须凑满的目标。禁止因为想写长而编造事实。"
    }
}

/// A conservative repeat guard. Exact normalized repeats are skipped;
/// semantic near matches are NOT dropped, since a negation can invert the meaning.
public enum QQReplyRepetitionPolicy {
    public static func normalized(_ text: String) -> String {
        let drop = CharacterSet.whitespacesAndNewlines
            .union(.punctuationCharacters)
        return String(String.UnicodeScalarView(text.lowercased().unicodeScalars.filter { !drop.contains($0) }))
    }

    public static func isRepeated(_ text: String, among recent: [String]) -> Bool {
        let candidate = normalized(text)
        guard candidate.count >= 10 else { return false } // Do not silence repeated brief greetings.
        return recent.suffix(8).contains { normalized($0) == candidate }
    }
}

/// Environment switches for session C social expression & sticker reranking.
/// Default off: leaves legacy persona prompts and sticker selection untouched.
public struct QQSocialStyleSettings: Sendable, Equatable {
    public let styleEnabled: Bool
    public let stickerRerankEnabled: Bool
    public let shadowEnabled: Bool

    public init(styleEnabled: Bool, stickerRerankEnabled: Bool, shadowEnabled: Bool) {
        self.styleEnabled = styleEnabled
        self.stickerRerankEnabled = stickerRerankEnabled
        self.shadowEnabled = shadowEnabled
    }

    public static func load(_ env: [String: String] = ProcessInfo.processInfo.environment) -> Self {
        func truthy(_ key: String) -> Bool {
            ["1", "true", "yes", "on"].contains(
                (env[key] ?? "").trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
            )
        }
        return Self(
            styleEnabled: truthy("DAFEIYU_SOCIAL_STYLE"),
            stickerRerankEnabled: truthy("DAFEIYU_STICKER_RERANK"),
            shadowEnabled: truthy("DAFEIYU_SOCIAL_STYLE_SHADOW")
        )
    }

    public var isActive: Bool { styleEnabled && !shadowEnabled }
    public var logsStyle: Bool { styleEnabled || shadowEnabled }
    public var needsPlan: Bool { styleEnabled || shadowEnabled || stickerRerankEnabled }
}

public enum QQSocialScene: String, Sendable, Equatable {
    case crisis
    case credentialAttack = "credential_attack"
    case seriousHelp = "serious_help"
    case sad
    case tired
    case conflict
    case complaint
    case celebration
    case sleep
    case greeting
    case mediaQuestion = "media_question"
    case gameTalk = "game_talk"
    case banter
    case forward
    case detailedRequest = "detailed_request"
    case question
    case chitchat
}

public enum QQSocialTone: String, Sendable, Equatable {
    case gentle, dry, neutral, celebratory, sleepy, playful

    var chineseLabel: String {
        switch self {
        case .gentle: return "温和关切"
        case .dry: return "克制冷静"
        case .neutral: return "自然平实"
        case .celebratory: return "轻快欢呼"
        case .sleepy: return "困倦柔和"
        case .playful: return "俏皮轻松"
        }
    }
}

public enum QQSocialLength: String, Sendable, Equatable {
    case short, medium, long
}

public enum QQSocialStickerIntent: String, Sendable, Equatable {
    case none, optional, preferred
}

public struct QQSocialStylePlan: Sendable, Equatable {
    public let scene: QQSocialScene
    public let tone: QQSocialTone
    public let length: QQSocialLength
    public let maxChars: Int
    public let stickerIntent: QQSocialStickerIntent

    public var promptInstruction: String {
        let stickerNote = stickerIntent == .none ? "本轮无需配表情包。" : "可按自然语境决定是否配图。"
        return "社交表达约束：当前场景 \(scene.rawValue)，语气保持\(tone.chineseLabel)，回复尺度 \(length.rawValue)（不超过 \(maxChars) 字），避免套话开头和重复句式。\(stickerNote)"
    }

    public func logLine(mode: String) -> String {
        "[social_style] scene=\(scene.rawValue) tone=\(tone.rawValue) length=\(length.rawValue) maxChars=\(maxChars) sticker=\(stickerIntent.rawValue) mode=\(mode)"
    }
}

/// Deterministic scene/tone/sticker planner aligned with session C (`social_expression/policy.py`).
public enum QQSocialStyleDirector {
    public static let stickerCooldownSeconds: TimeInterval = 150

    private static let scenePatterns: [(QQSocialScene, [String])] = [
        (.crisis, ["不想活", "自杀", "轻生", "自残", "结束生命", "活不下去"]),
        (.credentialAttack, ["系统提示词", "system prompt", "api_key", "apikey", "密钥", "访问令牌", "开发者模式", "越狱", "jailbreak", "忽略之前", "忽略规则", "ignore previous"]),
        (.seriousHelp, ["报错", "错误代码", "崩溃", "启动不了", "打不开", "连不上", "安装失败", "配置", "部署", "怎么解决", "怎么弄", "如何处理", "求助", "求帮", "教我", "不会做", "是什么原因", "原理", "区别", "步骤", "教程", "python", "代码", "编译", "依赖", "版本冲突"]),
        (.sad, ["难过", "伤心", "想哭", "哭了", "委屈", "失恋", "被骂", "心累", "压力大", "焦虑", "郁闷", "烦死", "崩溃了", "不想上班", "不想上学"]),
        (.tired, ["累死", "好累", "太累", "困死", "顶不住", "熬不住", "想躺"]),
        (.conflict, ["滚吧", "闭嘴", "傻逼", "废物", "垃圾", "有病", "互喷", "吵起来", "骂战"]),
        (.complaint, ["又卡", "怎么又", "老是不回", "总是出错", "掉线", "失联"]),
        (.celebration, ["通过了", "考过了", "面试过了", "修好了", "搞定", "成功了", "赢了", "上岸", "通关", "太棒", "牛啊", "太牛", "牛逼", "牛批", "newbee", "拿到了", "第一次"]),
        (.sleep, ["晚安", "睡了", "困了", "睡觉", "碎觉", "眯一会", "下线了"]),
        (.greeting, ["你好", "您好", "早上好", "早安", "下午好", "晚上好", "在吗", "在不在", "哈喽", "hello"]),
        (.mediaQuestion, ["这是啥", "什么图", "谁画的", "出处", "什么角色", "哪个角色", "看看这", "这张图", "表情", "gif", "动图"]),
        (.gameTalk, ["第五人格", "排位", "上分", "角色", "地图", "赛事", "ivl", "fps", "kkrb", "帧率", "外挂", "皮肤", "版本", "阵容", "对局"]),
        (.banter, ["杂鱼", "就这", "菜就多练", "菜鸡", "太菜", "笨蛋", "笨鱼", "哈哈", "笑死", "逗你", "开玩笑", "反串", "锐评", "你行你上", "急了", "破防", "绷不住"]),
        (.forward, ["转发", "砍一刀", "拼多多", "助力", "小程序", "帮我点", "链接"]),
        (.detailedRequest, ["详细", "展开", "具体", "长一点", "多说", "写一段", "方案", "分析", "步骤", "教程", "原理"]),
        (.question, ["?", "？", "吗", "怎么", "为什么", "多少", "哪个", "哪里", "谁", "能不能", "可不可以", "是不是"])
    ]

    public static func classifyScene(_ text: String, hasMedia: Bool = false) -> QQSocialScene {
        let lower = text.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
        for (scene, keywords) in scenePatterns {
            if scene == .mediaQuestion && !hasMedia { continue }
            if scene == .greeting && (lower == "hi" || lower == "hey") { return .greeting }
            if keywords.contains(where: { lower.contains($0.lowercased()) }) {
                return scene
            }
        }
        return .chitchat
    }

    public static func plan(for text: String, hasMedia: Bool = false, group: Bool = false,
                            proactive: Bool = false, configuredMaxChars: Int = 120,
                            lastStickerAt: Date? = nil, now: Date = Date()) -> QQSocialStylePlan {
        let scene = classifyScene(text, hasMedia: hasMedia)
        let tone: QQSocialTone
        var length: QQSocialLength
        var stickerIntent: QQSocialStickerIntent

        switch scene {
        case .crisis, .sad:
            tone = .gentle; length = .short; stickerIntent = .none
        case .credentialAttack, .forward:
            tone = .dry; length = .short; stickerIntent = .none
        case .seriousHelp, .mediaQuestion, .question:
            tone = .neutral; length = .medium; stickerIntent = .none
        case .tired:
            tone = .gentle; length = .short; stickerIntent = .optional
        case .conflict:
            tone = .gentle; length = .short; stickerIntent = .none
        case .complaint:
            tone = .dry; length = .short; stickerIntent = .none
        case .celebration:
            tone = .celebratory; length = .short; stickerIntent = .preferred
        case .sleep:
            tone = .sleepy; length = .short; stickerIntent = .optional
        case .greeting, .banter, .chitchat:
            tone = .playful; length = .short; stickerIntent = .optional
        case .gameTalk:
            tone = .playful; length = .medium; stickerIntent = .optional
        case .detailedRequest:
            tone = .neutral; length = .long; stickerIntent = .none
        }

        if proactive {
            length = .short
        } else if group && length == .long {
            length = .medium
        }

        if let lastStickerAt, now.timeIntervalSince(lastStickerAt) < stickerCooldownSeconds {
            stickerIntent = .none
        }

        let desiredCap: Int
        switch length {
        case .short: desiredCap = 50
        case .medium: desiredCap = 120
        case .long: desiredCap = 160
        }
        let maxChars = max(20, min(configuredMaxChars, desiredCap))

        return QQSocialStylePlan(
            scene: scene,
            tone: tone,
            length: length,
            maxChars: maxChars,
            stickerIntent: stickerIntent
        )
    }

    /// Preferred `QQEmotion.rawValue` ordering when reranking sticker candidates.
    public static func preferredEmotionRawValues(for scene: QQSocialScene) -> [String] {
        switch scene {
        case .celebration: return ["joy", "smug", "teasing"]
        case .banter: return ["smug", "teasing", "joy", "annoyed"]
        case .sleep: return ["sleepy", "neutral"]
        case .tired: return ["sleepy", "sad", "neutral"]
        case .gameTalk: return ["smug", "joy", "teasing", "flustered"]
        case .greeting, .chitchat: return ["joy", "neutral", "smug", "teasing"]
        default: return ["neutral", "joy"]
        }
    }
}
