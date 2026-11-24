import Foundation
import BotCore

/// 平台 / 账号 / 会话三层隔离身份。
///
/// 这是「身份按平台/账号/会话隔离」的唯一入口：所有可复用状态（去重、低频、防复读、
/// 历史）都以 `sessionKey` 为键；所有对外记录只输出 `scopeDigest` / `sessionDigest`
/// 这样的不可逆摘要，绝不输出消息原文、群名、账号或 chat_id 明文。
public struct UnifiedIdentity: Codable, Equatable, Sendable {
    public var platform: String      // "qq" | "wechat"
    public var account: String       // 客户端账号（wechat.account / QQ expectedSelfID）
    public var chatType: String      // "group" | "private"
    public var chatId: String        // 群名 / 私聊 id（沿用已授权桥契约原值）
    public var senderId: String      // 发送者；群内为桥给出的合成 sender_id

    public init(platform: String, account: String, chatType: String, chatId: String, senderId: String) {
        self.platform = platform
        self.account = account
        self.chatType = chatType
        self.chatId = chatId
        self.senderId = senderId
    }

    /// 隔离主键：不同平台、不同账号、不同会话绝不共享状态。
    public var scopeKey: String { "\(platform)|\(account)|\(chatType)|\(chatId)" }
    /// 私聊把发送者纳入会话键，避免同一 chat_id 下多发送者串话。
    public var sessionKey: String {
        chatType == "private" ? scopeKey + "|" + senderId : scopeKey
    }
    public var scopeDigest: String { Self.digest(scopeKey) }
    public var sessionDigest: String { Self.digest(sessionKey) }

    /// 稳定的伪名摘要，仅用于日志关联，不是安全原语，也不保存原文。
    /// 用 FNV-1a 64 位纯 Swift 实现，避免引入平台相关的 Crypto 依赖。
    public static func digest(_ value: String) -> String {
        var hash: UInt64 = 0xcbf29ce484222325
        for byte in Data(("dafeiyu-unified-v1|" + value).utf8) {
            hash ^= UInt64(byte)
            hash = hash &* 0x100000001b3
        }
        return String(format: "%016x", hash)
    }
}

/// 入站请求：与 `pi_bridge.inbound_payload()` / `gateway.normalizeInbound()` 的既有字段对齐，
/// 只增加 `platform` / `account` 两个隔离维度（由路由侧补齐，不由聊天正文决定）。
public struct UnifiedInbound: Codable, Sendable, Equatable {
    public var platform: String
    public var account: String
    public var chatType: String
    public var chatId: String
    public var chatTitle: String
    public var senderName: String
    public var senderId: String
    public var text: String
    public var atMe: Bool
    public var msgId: String
    /// 桥侧是否识别到原生引用（Quote ...'s message:）。不参与授权，只作低信任背景。
    public var quoted: Bool
    /// 入站图片是否已由既有 /api/vision 转成文字描述（Pi 侧无原图出站能力）。
    public var imageDescribed: Bool
    public var groupAutoReply: Bool
    public var groupAutoReplyContext: [String]
    public var groupAutoReplyWindowOK: Bool
    public var groupAutoReplySuperseded: Bool
    public var groupAutoReplyPublicQuestionSettled: Bool
    public var groupAutoReplyPublicQuestionCooldownSeconds: Int?

    private enum CodingKeys: String, CodingKey {
        case platform, account
        case chatType = "chat_type"
        case chatId = "chat_id"
        case chatTitle = "chat_title"
        case senderName = "sender_name"
        case senderId = "sender_id"
        case text
        case atMe = "at_me"
        case msgId = "msg_id"
        case quoted
        case imageDescribed = "image_described"
        case groupAutoReply = "group_auto_reply"
        case groupAutoReplyContext = "group_auto_reply_context"
        case groupAutoReplyWindowOK = "group_auto_reply_window_ok"
        case groupAutoReplySuperseded = "group_auto_reply_superseded"
        case groupAutoReplyPublicQuestionSettled = "group_auto_reply_public_question_settled"
        case groupAutoReplyPublicQuestionCooldownSeconds = "group_auto_reply_public_question_cooldown_seconds"
    }

    public init(platform: String = "wechat", account: String = "", chatType: String = "group",
                chatId: String = "", chatTitle: String = "", senderName: String = "",
                senderId: String = "", text: String = "", atMe: Bool = false,
                msgId: String = "", quoted: Bool = false, imageDescribed: Bool = false,
                groupAutoReply: Bool = false, groupAutoReplyContext: [String] = [],
                groupAutoReplyWindowOK: Bool = false, groupAutoReplySuperseded: Bool = true,
                groupAutoReplyPublicQuestionSettled: Bool = false,
                groupAutoReplyPublicQuestionCooldownSeconds: Int? = nil) {
        self.platform = platform; self.account = account; self.chatType = chatType
        self.chatId = chatId; self.chatTitle = chatTitle; self.senderName = senderName
        self.senderId = senderId; self.text = text; self.atMe = atMe; self.msgId = msgId
        self.quoted = quoted; self.imageDescribed = imageDescribed
        self.groupAutoReply = groupAutoReply; self.groupAutoReplyContext = groupAutoReplyContext
        self.groupAutoReplyWindowOK = groupAutoReplyWindowOK; self.groupAutoReplySuperseded = groupAutoReplySuperseded
        self.groupAutoReplyPublicQuestionSettled = groupAutoReplyPublicQuestionSettled
        self.groupAutoReplyPublicQuestionCooldownSeconds = groupAutoReplyPublicQuestionCooldownSeconds
    }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        platform = (try? c.decode(String.self, forKey: .platform)) ?? "wechat"
        account = (try? c.decode(String.self, forKey: .account)) ?? ""
        chatType = (try? c.decode(String.self, forKey: .chatType)) ?? "group"
        chatId = (try? c.decode(String.self, forKey: .chatId)) ?? ""
        chatTitle = (try? c.decode(String.self, forKey: .chatTitle)) ?? ""
        senderName = (try? c.decode(String.self, forKey: .senderName)) ?? ""
        senderId = (try? c.decode(String.self, forKey: .senderId)) ?? ""
        text = (try? c.decode(String.self, forKey: .text)) ?? ""
        atMe = (try? c.decode(Bool.self, forKey: .atMe)) ?? false
        msgId = (try? c.decode(String.self, forKey: .msgId)) ?? ""
        quoted = (try? c.decode(Bool.self, forKey: .quoted)) ?? false
        imageDescribed = (try? c.decode(Bool.self, forKey: .imageDescribed)) ?? false
        groupAutoReply = (try? c.decode(Bool.self, forKey: .groupAutoReply)) ?? false
        groupAutoReplyContext = (try? c.decode([String].self, forKey: .groupAutoReplyContext)) ?? []
        groupAutoReplyWindowOK = (try? c.decode(Bool.self, forKey: .groupAutoReplyWindowOK)) ?? false
        groupAutoReplySuperseded = (try? c.decode(Bool.self, forKey: .groupAutoReplySuperseded)) ?? true
        groupAutoReplyPublicQuestionSettled = (try? c.decode(Bool.self, forKey: .groupAutoReplyPublicQuestionSettled)) ?? false
        groupAutoReplyPublicQuestionCooldownSeconds = try c.decodeIfPresent(Int.self, forKey: .groupAutoReplyPublicQuestionCooldownSeconds)
    }

    public var identity: UnifiedIdentity {
        UnifiedIdentity(platform: platform, account: account, chatType: chatType,
                        chatId: chatId, senderId: senderId)
    }
}

/// 统一回复结果：形状与旧 core 的 `/webhook/wechat` 响应兼容（ok / action / reply / reason），
/// 因此 `pi_bridge.deliver_ready()` 一行都不用改。
public struct UnifiedReply: Codable, Sendable, Equatable {
    public var ok: Bool
    public var action: String
    public var reply: String?
    public var reason: String?
    public var trace: String?
    /// 仅摘要，不含原文。
    public var scope: String?
    /// A bearer-authenticated bridge may confirm this turn after observing the outgoing bubble.
    var memoryReceipt: UUID? = nil

    public static func ignore(_ reason: String, scope: String, trace: String) -> UnifiedReply {
        UnifiedReply(ok: true, action: "ignore", reason: reason, trace: trace, scope: scope)
    }
    public static func failure(_ reason: String, scope: String, trace: String) -> UnifiedReply {
        UnifiedReply(ok: false, action: "ignore", reason: reason, trace: trace, scope: scope)
    }
    public static func answer(_ text: String, scope: String, trace: String) -> UnifiedReply {
        UnifiedReply(ok: true, action: "reply", reply: text, trace: trace, scope: scope)
    }
}

/// 可观测记录：只含伪名摘要、离散原因、耗时与 token 计数，**绝不含消息原文**。
public struct UnifiedAuditEntry: Codable, Sendable, Equatable {
    public var at: Double
    public var scope: String
    public var reason: String
    public var tokens: Int
    public var durationMs: Int
    public var outcome: String
    /// Optional opaque model-hub correlation fields; absent for old records and no-model gates.
    public var modelTraceID: String? = nil
    public var inboundTraceDigest: String? = nil
}
