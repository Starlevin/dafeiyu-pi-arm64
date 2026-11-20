import Foundation

// PersonaGuard v1 — pure, Foundation-only.
// Purpose: catch persona drift before a reply is sent. Detection only; it never calls a model.
// Contract: ../../PROTOCOL_REPLY_DECISION_V1.md §6

public enum ReplyRelationship: String, Codable, Sendable {
    case owner
    case peer
    case stranger
}

public enum PersonaScenario: String, Codable, Sendable {
    case greeting
    case casual
    case help
    case banter
    case serious
    case comfort
    case identity
}

public enum PersonaSeverity: String, Codable, Sendable, Comparable {
    case block
    case rewrite
    case warn

    private var rank: Int {
        switch self { case .block: return 0; case .rewrite: return 1; case .warn: return 2 }
    }
    public static func < (lhs: PersonaSeverity, rhs: PersonaSeverity) -> Bool { lhs.rank < rhs.rank }
}

public enum PersonaViolation: String, Codable, Sendable {
    case claimsHuman = "claims_human"
    case deniesAI = "denies_ai"
    case aiDisclaimer = "ai_disclaimer"
    case fabricatedSharedExperience = "fabricated_shared_experience"
    case fabricatedRelationship = "fabricated_relationship"
    case overFamiliarAddress = "over_familiar_address"
    case repeatedTic = "repeated_tic"
    case customerServiceTone = "customer_service_tone"
    case aiSlopConnector = "ai_slop_connector"
    case excessiveLength = "excessive_length"
    case markdownFormatting = "markdown_formatting"
    case stageDirection = "stage_direction"
    case fabricatedCompetence = "fabricated_competence"
    case roleBreak = "role_break"
    case punctuationSpam = "punctuation_spam"
    case emptyText = "empty_text"
    case placeholder = "placeholder"
    case repeatedReply = "repeated_reply"
}

public struct PersonaViolationHit: Codable, Sendable, Equatable {
    public var code: PersonaViolation
    public var severity: PersonaSeverity
    public var detail: String
    public init(code: PersonaViolation, severity: PersonaSeverity, detail: String) {
        self.code = code; self.severity = severity; self.detail = detail
    }
}

public struct PersonaProfile: Codable, Sendable, Equatable {
    public var displayName: String
    public var relationship: ReplyRelationship
    public var maxCharacters: Int
    public var ticBudget: Int
    public var emojiBudget: Int
    public var allowTeasing: Bool

    public init(displayName: String = "大肥鱼", relationship: ReplyRelationship = .peer,
                maxCharacters: Int = 60, ticBudget: Int = 1, emojiBudget: Int = 1,
                allowTeasing: Bool = true) {
        self.displayName = displayName
        self.relationship = relationship
        self.maxCharacters = max(10, min(200, maxCharacters))
        self.ticBudget = max(0, min(3, ticBudget))
        self.emojiBudget = max(0, min(2, emojiBudget))
        self.allowTeasing = allowTeasing
    }
}

public struct PersonaCheckContext: Codable, Sendable, Equatable {
    public var hasMemoryEvidence: Bool
    public var recentReplies: [String]
    public init(hasMemoryEvidence: Bool = false, recentReplies: [String] = []) {
        self.hasMemoryEvidence = hasMemoryEvidence
        self.recentReplies = recentReplies
    }
}

public struct PersonaCheckResult: Codable, Sendable, Equatable {
    public var passed: Bool
    public var hits: [PersonaViolationHit]
    public var blocking: Bool
    public var rewriteRequired: Bool
    public var checkedCharacters: Int

    public init(text: String, hits: [PersonaViolationHit]) {
        self.passed = hits.isEmpty
        self.hits = hits.sorted { lhs, rhs in
            lhs.severity == rhs.severity ? lhs.code.rawValue < rhs.code.rawValue : lhs.severity < rhs.severity
        }
        self.blocking = self.hits.contains { $0.severity == .block }
        self.rewriteRequired = self.hits.contains { $0.severity == .rewrite }
        self.checkedCharacters = text.count
    }

    public var summary: String {
        hits.map { "\($0.code.rawValue):\($0.severity.rawValue)" }.joined(separator: ",")
    }
}

public struct PersonaStylePlan: Codable, Sendable, Equatable {
    public var minCharacters: Int
    public var maxCharacters: Int
    public var minSentences: Int
    public var maxSentences: Int
    public var emojiBudget: Int
    public var ticBudget: Int
    public var allowTeasing: Bool
    public var openerFamily: Int
    public var directive: String

    public static let openerNames = ["直答", "短评", "反问", "接梗", "确认"]
}

public enum PersonaGuard {
    public static func stylePlan(profile: PersonaProfile, scenario: PersonaScenario, seed: Int) -> PersonaStylePlan {
        let base: (Int, Int, Int, Int, Int) // minChars, maxChars, minSent, maxSent, emoji
        switch scenario {
        case .greeting: base = (6, 24, 1, 1, 1)
        case .casual:   base = (8, 36, 1, 2, 1)
        case .help:     base = (20, 80, 2, 4, 0)
        case .banter:   base = (8, 30, 1, 2, 1)
        case .serious:  base = (20, 90, 2, 4, 0)
        case .comfort:  base = (12, 50, 1, 3, 1)
        case .identity: base = (4, 20, 1, 2, 0)
        }
        var rng = LCG(seed: UInt64(bitPattern: Int64(seed)))
        let spread = rng.int(upperBound: 3) // 0..2
        var minChars = base.0 + spread * 2
        var maxChars = min(base.1, profile.maxCharacters) - (2 - spread) * 2
        if maxChars < minChars { minChars = maxChars; maxChars = max(minChars, maxChars) }
        if maxChars < 4 { minChars = 4; maxChars = 4 }
        var minSent = base.2, maxSent = base.3
        if scenario == .casual || scenario == .banter {
            let extra = rng.int(upperBound: 2)
            minSent = 1 + extra
            maxSent = minSent + 1
        }
        let emoji = min(profile.emojiBudget, base.4)
        let tic = scenario == .banter ? min(profile.ticBudget, 1) : 0
        let opener = rng.int(upperBound: PersonaStylePlan.openerNames.count)
        let tease = profile.allowTeasing && (scenario == .banter || scenario == .casual)
        let directive = "本轮约 \(minSent)–\(maxSent) 句、\(minChars)–\(maxChars) 字；最多 \(tic) 个口头禅、\(emoji) 个表情；开头用「\(PersonaStylePlan.openerNames[opener])」；不写括号动作、Markdown、客服腔；不虚构共同经历或现实身份。"
        return PersonaStylePlan(minCharacters: minChars, maxCharacters: maxChars,
                                minSentences: minSent, maxSentences: maxSent,
                                emojiBudget: emoji, ticBudget: tic,
                                allowTeasing: tease, openerFamily: opener, directive: directive)
    }

    public static func check(_ text: String, profile: PersonaProfile, scenario: PersonaScenario,
                             context: PersonaCheckContext = PersonaCheckContext()) -> PersonaCheckResult {
        var hits: [PersonaViolationHit] = []
        let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
        if trimmed.isEmpty {
            return PersonaCheckResult(text: text, hits: [.init(code: .emptyText, severity: .block, detail: "空文本")])
        }
        if trimmed.count > profile.maxCharacters {
            hits.append(.init(code: .excessiveLength, severity: .rewrite,
                              detail: "\(trimmed.count)>\(profile.maxCharacters)字"))
        }
        if let match = firstMatch(#"(?:^|\n)\s*(?:[-*]\s+|\d+\.\s+)"#, in: trimmed) {
            hits.append(.init(code: .markdownFormatting, severity: .rewrite, detail: "列表/分段：\(match)"))
        }
        if trimmed.contains("**") || trimmed.contains("```") || trimmed.contains("|--") {
            hits.append(.init(code: .markdownFormatting, severity: .rewrite, detail: "Markdown 标记"))
        }

        let rules: [(PersonaViolation, PersonaSeverity, String, String)] = [
            (.claimsHuman, .block, #"我是(?:一个)?(?:真人|人类|活人|现实里的人)(?!吗|么|？|\?)|我(?:可|肯|确实)是(?:真人|人类)(?!吗|么|？|\?)|我(?:不是|才不是)(?:AI|机器人|人工智能)|我(?:有|在现实里有)(?:肉身|实体身体)|现实(?:里|中)的我"#, "自称真人"),
            (.deniesAI, .block, #"我不是(?:一个)?(?:AI|人工智能|语言模型|机器人)"#, "否认 AI 身份"),
            (.aiDisclaimer, .block, #"我是(?:一个)?(?:AI|人工智能)(?:助手|语言模型|机器人)|作为(?:一个)?(?:AI|人工智能|语言模型|助手)|我只是(?:一个)?(?:AI|语言模型)"#, "AI 助手腔"),
            (.fabricatedRelationship, .block, #"(?:做|当)我(?:女朋友|男朋友|老婆|老公|对象)|你是我(?:的人|女朋友|男朋友|老婆|老公|对象)|永远(?:在一起|不分开)"#, "虚构恋爱/占有关系"),
            (.fabricatedCompetence, .block, #"我已经(?:帮你|给你|替你)(?:重启|部署|删除|下单|付款|转账|发送|执行|修好)|我(?:帮你|替你)(?:重启|部署|删除|下单|付款|转账)(?:了|好了)|我(?:已经)?(?:关掉|打开)了你的"#, "声称执行了系统操作"),
            (.roleBreak, .block, #"系统提示词|system\s*prompt|开发者模式|越狱模式|jailbreak"#, "泄露内部规则"),
            (.placeholder, .rewrite, #"\{\{[^}]+\}\}|\[你的名字\]|TODO|<\|[^|]+\|>"#, "未替换占位符"),
            (.customerServiceTone, .rewrite, #"有什么可以帮(?:您|你)|请问还有(?:什么|其他)(?:可以|需要)|很高兴为您服务|感谢您的咨询|不客气[，,]?这是我应该做的"#, "客服腔"),
            (.aiSlopConnector, .rewrite, #"首先[，,]|其次[，,]|综上所述|总的来说|希望对你有帮助|如果你有任何问题|不难看出|值得注意的是"#, "AI 连接词"),
            (.stageDirection, .rewrite, #"（[^）]{0,12}(?:笑|叹气|摇头|挥手|歪头|眨眼|摊手)[^）]{0,12}）|\([^)]{0,12}(?:laughs|sighs|smiles)\)"#, "括号动作描写"),
        ]
        for (code, severity, pattern, detail) in rules {
            if firstMatch(pattern, in: trimmed) != nil {
                hits.append(.init(code: code, severity: severity, detail: detail))
            }
        }

        // Address boundaries.
        if profile.relationship != .owner {
            if firstMatch(#"主人"#, in: trimmed) != nil {
                hits.append(.init(code: .overFamiliarAddress, severity: .block, detail: "非 OWNER 称主人"))
            }
        }
        if firstMatch(#"亲爱的|宝贝|宝宝|老婆|老公"#, in: trimmed) != nil {
            let severity: PersonaSeverity = profile.relationship == .owner ? .warn : .rewrite
            hits.append(.init(code: .overFamiliarAddress, severity: severity, detail: "亲密称呼越界"))
        }

        // Tic budget.
        let ticCount = countMatches(#"喵"#, in: trimmed)
        if ticCount > profile.ticBudget {
            hits.append(.init(code: .repeatedTic, severity: .rewrite, detail: "喵 x\(ticCount)"))
        }
        let memeCount = countMatches(#"[绷典孝急赢麻乐]"#, in: trimmed)
        if memeCount > 2 {
            hits.append(.init(code: .repeatedTic, severity: .rewrite, detail: "梗字 x\(memeCount)"))
        }
        for token in ["杂鱼", "本鱼", "哼"] where countMatches(NSRegularExpression.escapedPattern(for: token), in: trimmed) > 1 {
            hits.append(.init(code: .repeatedTic, severity: .rewrite, detail: "\(token) 复读"))
        }

        // Emoji budget (informational; not a hard failure).
        let emojiCount = trimmed.unicodeScalars.filter { scalar in
            (0x1F300...0x1FAFF).contains(scalar.value) || (0x2600...0x27BF).contains(scalar.value)
        }.count
        if emojiCount > profile.emojiBudget {
            hits.append(.init(code: .punctuationSpam, severity: .warn, detail: "表情 \(emojiCount)>\(profile.emojiBudget)"))
        }
        if firstMatch(#"[!！]{3,}|[?？]{3,}|[~～]{3,}"#, in: trimmed) != nil {
            hits.append(.init(code: .punctuationSpam, severity: .warn, detail: "标点连打"))
        }

        // Memory evidence is required for any "I remember us" claim.
        if !context.hasMemoryEvidence {
            if firstMatch(#"记得(?:我们|你)|上次(?:我们|你)|以前(?:我们|你)"#, in: trimmed) != nil &&
                !hits.contains(where: { $0.code == .fabricatedSharedExperience }) {
                hits.append(.init(code: .fabricatedSharedExperience, severity: .block, detail: "无记忆证据的回忆"))
            }
        }

        // Exact duplicate of a recent bot reply is never acceptable.
        let normalized = ReplyDecisionText.normalize(trimmed)
        if !normalized.isEmpty && context.recentReplies.map(ReplyDecisionText.normalize).contains(normalized) {
            hits.append(.init(code: .repeatedReply, severity: .rewrite, detail: "与近期回复完全重复"))
        }

        return PersonaCheckResult(text: text, hits: hits)
    }

    /// 只有回复与「实际召回的记忆文本」有足够多归一化 bigram 重合时，才承认共同经历。
    /// 这样既不会因 `hasMemoryEvidence=false` 误杀有依据的回忆，也不会让无关记忆
    /// 为“我记得我们上次……”这类虚构背书。
    public static func hasSupportingMemoryEvidence(_ reply: String, memory: String,
                                                  minimumBigrams: Int = 2) -> Bool {
        let replyBigrams = normalizedBigrams(reply)
        let memoryBigrams = normalizedBigrams(memory)
        guard !replyBigrams.isEmpty, !memoryBigrams.isEmpty else { return false }
        return replyBigrams.intersection(memoryBigrams).count >= max(1, minimumBigrams)
    }

    /// 只做确定性截断：超出字数时在最后一个句读前收口；不改变其他语义。
    public static func trim(_ text: String, to maxCharacters: Int) -> String {
        let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard trimmed.count > maxCharacters else { return trimmed }
        let cut = String(trimmed.prefix(maxCharacters))
        let terminators: [Character] = ["。", "！", "？", "；", "，", ","]
        if let last = cut.lastIndex(where: { terminators.contains($0) }) {
            let keep = cut[..<last].trimmingCharacters(in: .whitespacesAndNewlines)
            if !keep.isEmpty { return keep + "。" }
        }
        return cut + "…"
    }

    private static func normalizedBigrams(_ text: String) -> Set<String> {
        let chars = Array(ReplyDecisionText.normalize(text))
        guard chars.count >= 2 else { return [] }
        var result = Set<String>()
        for index in 0..<(chars.count - 1) {
            result.insert(String(chars[index...index + 1]))
        }
        return result
    }

    private static func firstMatch(_ pattern: String, in text: String) -> String? {
        guard let regex = try? NSRegularExpression(pattern: pattern, options: [.caseInsensitive]) else { return nil }
        let range = NSRange(text.startIndex..<text.endIndex, in: text)
        guard let match = regex.firstMatch(in: text, options: [], range: range),
              let matchRange = Range(match.range, in: text) else { return nil }
        return String(text[matchRange])
    }

    private static func countMatches(_ pattern: String, in text: String) -> Int {
        guard let regex = try? NSRegularExpression(pattern: pattern, options: [.caseInsensitive]) else { return 0 }
        let range = NSRange(text.startIndex..<text.endIndex, in: text)
        return regex.numberOfMatches(in: text, options: [], range: range)
    }
}

private struct LCG {
    private var state: UInt64
    init(seed: UInt64) { state = seed == 0 ? 0x9E3779B97F4A7C15 : seed }
    mutating func next() -> UInt64 {
        state = state &* 6_364_136_223_846_793_005 &+ 1_442_695_040_888_963_407
        return state
    }
    mutating func int(upperBound: Int) -> Int {
        guard upperBound > 0 else { return 0 }
        return Int(next() % UInt64(upperBound))
    }
}
