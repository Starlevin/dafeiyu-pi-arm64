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
