// PublicGroupQuestion is a text-only admission matcher.
//
// Evidence that the caller owns and must prove (whitelist membership, author,
// recency and an exact time window) is deliberately NOT inferred here. This
// type only decides whether a single message body reads like a concrete,
// group-addressed help question. It performs no I/O, uses no model, has no
// configuration and adds no dependencies.
//
// The decision is compositional and conservative: a message must survive all
// exclusions and simultaneously carry (1) a collective address, (2) a question
// form and (3) concrete content left over after removing the question
// scaffolding. There is no numeric score and no broad keyword recall.

public enum PublicGroupQuestion {
    public static func matches(_ text: String) -> Bool {
        let body = normalize(text)
        guard !body.isEmpty else { return false }
        guard !isExcluded(body) else { return false }
        guard hasGroupAddressee(body) else { return false }
        guard hasQuestionForm(body) else { return false }
        guard hasConcreteContent(body) else { return false }
        return true
    }

    // MARK: - Normalization

    /// Trim, map fullwidth ASCII/ideographic space to ASCII, then lowercase.
    /// No Foundation, locale or locale-dependent casing is involved.
    private static func normalize(_ text: String) -> String {
        let characters = Array(text)
        var start = 0
        var end = characters.count
        while start < end && characters[start].isWhitespace { start += 1 }
        while end > start && characters[end - 1].isWhitespace { end -= 1 }
        let trimmed = String(characters[start..<end])

        var scalars = String.UnicodeScalarView()
        for unit in trimmed.unicodeScalars {
            let value = unit.value
            if value == 0x3000 { // IDEOGRAPHIC SPACE
                scalars.append(Unicode.Scalar(0x20)!)
            } else if value >= 0xFF01 && value <= 0xFF5E { // FULLWIDTH ASCII block
                scalars.append(Unicode.Scalar(value - 0xFEE0)!)
            } else {
                scalars.append(unit)
            }
        }
        return String(scalars).lowercased()
    }

    // MARK: - Structure

    private static func containsAny(_ body: String, _ markers: [String]) -> Bool {
        for marker in markers where !marker.isEmpty {
            if body.contains(marker) { return true }
        }
        return false
    }

    /// Remove known markers, longest first so "有没有人" is not partially eaten
    /// by "有人" and "does anyone" is not partially eaten by "anyone know".
    /// Implemented on a character buffer so no Foundation is required.
    private static func removing(_ body: String, _ markers: [String]) -> String {
        var result = Array(body)
        for marker in markers.sorted(by: { $0.count > $1.count }) where !marker.isEmpty {
            let needle = Array(marker)
            guard needle.count <= result.count else { continue }
            var cursor = 0
            while cursor + needle.count <= result.count {
                if Array(result[cursor..<(cursor + needle.count)]) == needle {
                    result.replaceSubrange(cursor..<(cursor + needle.count), with: [" "])
                    cursor += 1
                } else {
                    cursor += 1
                }
            }
        }
        return String(result)
    }

    // MARK: - Exclusions

    private static let hardExclusions: [String] = [
        // Direct mention / replying to a specific person / forwarding / quoting.
        "@", "[cq:at", "转发", "合并转发", "转发消息", "引用消息", "引用:",
        "fwd:", "forwarded", "original message", "quoted", ">>>",
        "聊天记录", "消息记录", "【转发", "[转发", "回复:", "答:", "re:",

        // Links and media placeholders carry no text-based help request.
        "http", "www.", "://", ".com/", ".cn/", ".net/", ".org/", ".io/",
        "t.me/", "[图片]", "[表情]", "[视频]", "[语音]", "[文件]",
        "[动画表情]", "[群图片]", "[闪照]", "[链接]", "[分享]", "[音乐]",
        "[名片]", "<image", "{image}", "$image", "[media]",

        // Explicitly resolved, self-answered or closing remarks.
        "已解决", "已经解决", "解决了", "解决啦", "搞定", "弄好", "修好",
        "已修复", "已找到", "找到了", "自己查到", "我自己搞定", "答案是",
        "解决方法", "破案了", "结案了", "原来如此", "其实就是", "简而言之",
        "正确做法", "方法如下", "不用了", "不需要了", "算了", "自问自答",
        "solved", "resolved", "figured it out", "figure it out", "nevermind",
        "never mind", "nvm", "found it", "fixed it", "got it working",
        "no longer need",

        // Rhetorical questions and opinion probes are not concrete help requests.
        "难道", "还不是", "不就是", "何必", "何苦", "谁不知道", "这还用问", "还用说",
        "是不是很", "大家觉得", "你们觉得", "大家看看", "不愧是",

        // Obvious command text.
        "执行命令", "运行命令", "请执行", "请运行"
    ]

    /// Short alive-probes and greetings that would otherwise look composed.
    private static let exactRejections: Set<String> = [
        "", "有人吗", "有人在吗", "有人在不", "在吗", "在不在", "在线吗",
        "有人在线吗", "大家在线吗", "各位在吗", "大家好吗?", "有没有人", "有人",
        "大家", "各位", "大家好", "hello", "hi", "hey", "yo", "test", "ping",
        "help", "anyone?", "isanyonethere?", "anybodythere?", "howareyou?"
    ]

    private static func isExcluded(_ body: String) -> Bool {
        if containsAny(body, hardExclusions) { return true }
        if body.hasPrefix("/") || body.hasPrefix("!") || body.hasPrefix(">") { return true }
        if body.hasPrefix("回复") || body.hasPrefix("回:") || body.hasPrefix("答:") { return true }
        let compact = String(body.filter { !$0.isWhitespace })
        let bare = String(compact.reversed().drop(while: { Self.isTerminalPunctuation($0) }).reversed())
        if exactRejections.contains(compact) || exactRejections.contains(bare) { return true }
        return false
    }

    private static func isTerminalPunctuation(_ character: Character) -> Bool {
        return "?？!！。.、~～".contains(character)
    }

    // MARK: - Positive structure

    /// A collective addressee, never a single named person.
    private static let groupAddressMarkers: [String] = [
        "请问大家", "请问各位", "请教各位", "问下大家", "问一下大家",
        "想问下大家", "想问一下大家", "求助各位", "求各位", "求大家",
        "各位大佬", "大佬们", "大神们", "兄弟们", "朋友们", "姐妹们",
        "小伙伴们", "各位朋友", "各位同学", "诸位",
        "大家", "各位", "群里", "群友", "有木有", "有没有人", "有人",
        "有谁", "谁有空", "谁会", "谁能", "谁可以", "哪位",
        "小助手", "小帮手", "机器人", "bot", "帮忙看看", "帮个忙",
        "does anyone", "do any of you", "can any of you", "could any of you",
        "can someone", "could someone", "does someone", "can anybody",
        "could anybody", "does anybody", "is anyone", "are there any",
        "is there anyone", "anyone know", "anybody know", "someone know",
        "anyone here know", "who knows", "who here", "anyone else",
        "any ideas", "any suggestions", "any help", "any thoughts",
        "guys", "everyone", "folks"
    ]

    private static let clauseBreakers: [String] = [
        "说", "告诉", "聊", "提", "问过", "说过", "聊过", "提过", "刚才", "刚刚",
        "之前", "先前", "我问", "我跟", "我向"
    ]
    private static let directedPersonMarkers: [String] = [
        "你知道", "你看", "你说", "你觉得", "你怎么看", "你看看", "请问你", "请教你",
        "回复你", "给你", "问你", "告诉你", "麻烦你"
    ]

    private static func hasGroupAddressee(_ body: String) -> Bool {
        guard containsAny(body, groupAddressMarkers) else { return false }
        var clauseStart = body.startIndex
        var i = body.startIndex
        while i < body.endIndex {
            let c = body[i]
            if c == "，" || c == "," || c == ":" || c == "；" || c == ";" {
                let clause = String(body[clauseStart..<i])
                if containsAny(clause, groupAddressMarkers) && containsAny(clause, clauseBreakers) { return false }
                if containsAny(clause, directedPersonMarkers) { return false }
                clauseStart = body.index(after: i)
            }
            i = body.index(after: i)
        }
        return !containsAny(String(body[clauseStart...]), directedPersonMarkers)
    }

    private static let questionStarts: [String] = [
        "how ", "why ", "what ", "where ", "when ", "which ", "who ",
        "can ", "could ", "would ", "should ", "does ", "do ",
        "is there ", "are there ", "anyone know", "anybody know", "any ideas"
    ]

    /// No-punctuation explanation questions such as "知道X是什么意思" /
    /// "X哪来的" are still questions. Kept as a closed, explanation-focused
    /// set; an isolated statement like "X又开会了" matches none of these.
    private static let implicitQuestionMarkers: [String] = [
        "什么意思", "什么意思啊", "哪来的", "哪儿来的", "哪里来的",
        "怎么回事", "出处", "来源", "是什么梗", "什么梗", "梗哪来"
    ]

    private static func hasQuestionForm(_ body: String) -> Bool {
        if body.contains("?") || body.contains("吗") || body.contains("呢") { return true }
        if let last = body.last, last == "不" || last == "没" { return true }
        for prefix in questionStarts where body.hasPrefix(prefix) { return true }
        return containsAny(body, implicitQuestionMarkers)
    }

    /// Function words and question scaffolding are removed before the remaining
    /// text is checked for any actual topic content. This is a structural
    /// emptiness test, not a score and not a keyword recall list.
    private static let scaffolding: [String] = [
        "怎么样", "怎么办", "能不能", "可不可以", "是什么原因", "知道吗",
        "请问", "请教", "问下", "问一下", "帮我", "帮忙", "帮看看", "看看", "看一下",
        "有没有", "是否", "为什么", "怎么", "如何", "怎样", "咋",
        "什么", "哪个", "哪里", "哪儿", "谁", "多少",
        "帮忙看看", "知道", "觉得", "认为", "可以", "能", "会", "给", "把", "被", "让",
        "我", "你", "他", "她", "它", "我们", "你们", "他们", "咱们",
        "吗", "呢", "吧", "啊", "呀", "哦", "嘛", "咯", "哈", "好", "好不好",
        "在线", "一下", "请", "谢谢", "感谢", "做", "弄", "搞", "办",
        "这个", "那个", "这些", "那些", "事情", "东西", "问题", "情况",
        "does anyone", "can someone", "could someone", "is anyone", "are there",
        "anyone know", "anybody know", "any ideas", "do you", "does it",
        "how", "why", "what", "where", "when", "which", "who", "can", "could",
        "would", "should", "does", "do", "is", "are", "there", "anyone",
        "someone", "i", "you", "my", "me", "we", "our", "us", "it", "this",
        "that", "these", "those", "a", "an", "the", "to", "of", "for", "with",
        "on", "in", "at", "please", "help", "any", "idea", "ideas", "know",
        "think", "about", "right", "guys", "everyone", "folks"
    ]

    private static func isTopicCharacter(_ character: Character) -> Bool {
        if character.isASCII {
            return character.isLetter || character.isNumber
        }
        guard character.unicodeScalars.count == 1, let scalar = character.unicodeScalars.first else {
            return false
        }
        let value = scalar.value
        return (0x3400...0x4DBF).contains(value)
            || (0x4E00...0x9FFF).contains(value)
            || (0xF900...0xFAFF).contains(value)
            || (0x20000...0x2FA1F).contains(value)
    }

    private static func hasConcreteContent(_ body: String) -> Bool {
        let withoutAddress = removing(body, groupAddressMarkers)
        let remainder = removing(withoutAddress, scaffolding)
        return remainder.contains(where: isTopicCharacter)
    }
}
