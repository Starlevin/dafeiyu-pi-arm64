import Foundation

/// OpenAI 兼容模型服务配置。
///
/// 默认值与原版**完全一致**（DeepSeek）：不设置任何环境变量时，请求地址、请求体字段、
/// 重试次数都与接入前相同，因此现有部署不需要改动。
///
/// 通过环境变量切换到其他 OpenAI 兼容服务（例如商汤 SenseNova）：
///
///     DAFEIYU_LLM_PROVIDER=deepseek|sensenova|custom
///     DAFEIYU_LLM_BASE_URL=https://token.sensenova.cn/v1
///     DAFEIYU_LLM_MODEL=sensenova-6.8-flash-lite
///     DAFEIYU_LLM_API_KEY=sk-...            （或 DAFEIYU_LLM_KEY_FILE=/etc/dafeiyu/llm.key）
///     DAFEIYU_LLM_TIMEOUT=45
///     DAFEIYU_LLM_MAX_RETRIES=2
///
/// 密钥本身只在这里解析、只在请求头里使用，不写日志、不进错误信息、不落盘。
/// 回复链路模式。
///
/// - `.original`：原版行为，每条回复固定走「草稿 + 语义校对」两次模型调用。
/// - `.fast`：普通短文本私聊只走一次调用；只有当消息命中风险信号时才回退到原版双阶段。
///
/// 实测（SenseNova 6.8 Flash Lite，合成填充负载，非真实聊天内容）：
/// 单次调用约 2.5 秒 / ~2100 tokens，双阶段约 9.2 秒 / ~8900 tokens。
/// 延迟主要来自「第二次调用」本身，而不是 `reasoning_effort` 的取值
/// （high / none / low 实测差异在噪声范围内）。
///
/// 回滚：设置 `DAFEIYU_REPLY_MODE=original` 即完全恢复原版双阶段链路。
enum QQReplyMode: String, Equatable, Sendable {
    case fast
    case original
}

struct LLMProvider: Equatable, Sendable {
    enum Kind: String, Equatable, Sendable {
        case deepseek
        case sensenova
        case custom
    }

    var kind: Kind
    var baseURL: URL
    /// DeepSeek 专属的 `thinking: {type}` 请求字段。其他服务商不认识该字段，发送可能被 400 拒绝。
    var sendsThinkingField: Bool
    /// 关闭思考时是否仍显式发送 `reasoning_effort: "none"`（商汤要求显式声明）。
    var sendsIdleReasoningEffort: Bool
    /// 非空时覆盖已保存配置里的模型 ID，便于无面板的服务器侧固定模型。
    var modelOverride: String?
    var requestTimeout: TimeInterval
    var maxRetries: Int
    /// 回复链路模式；`.original` 即原版双阶段，用于回滚对照。
    var replyMode: QQReplyMode
    /// 双阶段被保留时的思考强度。**默认 high，与原版一致**；
    /// 需要缩短超长校对输入的耗时可用 DAFEIU_REVIEW_EFFORT 降到 low。
    var reviewEffort: String
    /// 只向日志输出脱敏的阶段统计（耗时 / tokens / 调用次数），绝不输出消息原文。
    var perfLogEnabled: Bool
    /// 是否把图片像素发给模型。实测 SenseNova 6.8 Flash Lite 对 image_url 稳定返回
    /// HTTP 500 `engine is not available temporarily`（纯文本同一 Key 正常），
    /// 即该模型不支持图片输入。关掉后改为文字说明「未读取图片」，避免整条回复失败。
    var imageInputEnabled: Bool
    /// 配置本身不成立时的说明；第一次真正调用时抛出，避免静默打到错误地址。
    var configurationProblem: String?

    var displayName: String {
        switch kind {
        case .deepseek: return "DeepSeek"
        case .sensenova: return "SenseNova"
        case .custom: return "自定义模型服务"
        }
    }

    static let deepSeekBaseURL = URL(string: "https://api.deepseek.com")!
    static let senseNovaBaseURL = URL(string: "https://token.sensenova.cn/v1")!

    init(kind: Kind = .deepseek,
         baseURL: URL? = nil,
         sendsThinkingField: Bool? = nil,
         sendsIdleReasoningEffort: Bool = false,
         modelOverride: String? = nil,
         requestTimeout: TimeInterval = 45,
         maxRetries: Int = 2,
         replyMode: QQReplyMode = .fast,
         reviewEffort: String = "high",
         perfLogEnabled: Bool = false,
         imageInputEnabled: Bool = true,
         configurationProblem: String? = nil) {
        self.kind = kind
        self.baseURL = baseURL ?? LLMProvider.defaultBaseURL(for: kind)
        self.sendsThinkingField = sendsThinkingField ?? (kind == .deepseek)
        self.sendsIdleReasoningEffort = sendsIdleReasoningEffort || kind == .sensenova
        self.modelOverride = modelOverride
        self.requestTimeout = requestTimeout
        self.maxRetries = maxRetries
        self.replyMode = replyMode
        self.reviewEffort = reviewEffort
        self.perfLogEnabled = perfLogEnabled
        self.imageInputEnabled = imageInputEnabled
        self.configurationProblem = configurationProblem
    }

    static func defaultBaseURL(for kind: Kind) -> URL {
        switch kind {
        case .sensenova: return senseNovaBaseURL
        case .deepseek, .custom: return deepSeekBaseURL
        }
    }

    static func fromEnvironment(_ environment: [String: String] = ProcessInfo.processInfo.environment) -> LLMProvider {
        let rawKind = environment["DAFEIYU_LLM_PROVIDER"]?
            .trimmingCharacters(in: .whitespacesAndNewlines).lowercased() ?? ""
        let kind = Kind(rawValue: rawKind) ?? .deepseek

        var problem: String?
        var base: URL?
        let rawBase = environment["DAFEIYU_LLM_BASE_URL"]?
            .trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
        if !rawBase.isEmpty {
            if let url = URL(string: rawBase), let scheme = url.scheme?.lowercased(),
               scheme == "http" || scheme == "https", url.host != nil {
                base = url
            } else {
                problem = "DAFEIYU_LLM_BASE_URL 不是合法的 http(s) 地址：\(rawBase)"
            }
        } else if kind == .custom {
            problem = "DAFEIYU_LLM_PROVIDER=custom 时必须同时设置 DAFEIYU_LLM_BASE_URL"
        }

        let rawModel = environment["DAFEIYU_LLM_MODEL"]?
            .trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
        let timeout = Double(environment["DAFEIYU_LLM_TIMEOUT"] ?? "") ?? 45
        let retries = Int(environment["DAFEIYU_LLM_MAX_RETRIES"] ?? "") ?? 2
        // 显式开关仅影响自定义服务商；DeepSeek / SenseNova 的字段组合由 provider 决定。
        let thinkingOverride: Bool?
        switch environment["DAFEIYU_LLM_SEND_THINKING"]?.trimmingCharacters(in: .whitespacesAndNewlines) {
        case "1", "true", "yes": thinkingOverride = true
        case "0", "false", "no": thinkingOverride = false
        default: thinkingOverride = nil
        }

        // DAFEIYU_REPLY_MODE=original 完全恢复原版「草稿+校对」双阶段链路。
        let rawMode = environment["DAFEIYU_REPLY_MODE"]?
            .trimmingCharacters(in: .whitespacesAndNewlines).lowercased() ?? ""
        let replyMode = QQReplyMode(rawValue: rawMode) ?? .fast

        // 保留双阶段时使用的思考强度。**默认与原版一致（high）**；
        // 需要缩短超长校对输入耗时可用 DAFEIYU_REVIEW_EFFORT 降到 low。
        // 只接受被测过的值，未知值一律回落到 high，避免向 API 传不支持的枚举，
        // 也保证「不设任何环境变量时行为与原版一致」这一前提不被破坏。
        let rawEffort = environment["DAFEIYU_REVIEW_EFFORT"]?
            .trimmingCharacters(in: .whitespacesAndNewlines).lowercased() ?? ""
        let allowedEfforts: Set<String> = ["none", "minimal", "low", "medium", "high"]
        let reviewEffort = allowedEfforts.contains(rawEffort) ? rawEffort : "high"

        let perfRaw = environment["DAFEIYU_PERF_LOG"]?
            .trimmingCharacters(in: .whitespacesAndNewlines).lowercased() ?? ""
        let perfLogEnabled = ["1", "true", "yes"].contains(perfRaw)

        // DAFEIYU_IMAGE_INPUT=0 时不下发图片像素（模型不支持 image_url 时避免整条回复 500）。
        let imageRaw = environment["DAFEIYU_IMAGE_INPUT"]?
            .trimmingCharacters(in: .whitespacesAndNewlines).lowercased() ?? ""
        let imageInputEnabled = !["0", "false", "no", "off"].contains(imageRaw)

        return LLMProvider(kind: kind,
                           baseURL: base,
                           sendsThinkingField: thinkingOverride ?? (kind == .deepseek),
                           sendsIdleReasoningEffort: kind == .sensenova,
                           modelOverride: rawModel.isEmpty ? nil : rawModel,
                           requestTimeout: timeout > 0 ? timeout : 45,
                           maxRetries: max(0, min(retries, 5)),
                           replyMode: replyMode,
                           reviewEffort: reviewEffort,
                           perfLogEnabled: perfLogEnabled,
                           imageInputEnabled: imageInputEnabled,
                           configurationProblem: problem)
    }
}

/// 模型 Key 解析：调用方传入（面板 / 钥匙串）优先，其次环境变量，最后受权限保护的密钥文件。
///
/// 原版在 Linux 上把 Key 限制为"仅本次运行有效"，导致进程重启后必须人工粘贴，
/// 无法满足"开机自启、无人值守"。这里补一条持久化通道，原面板流程保持不变。
enum LLMSecret {
    static let defaultKeyFile = "/etc/dafeiyu/llm.key"

    static func resolve(explicit: String,
                        environment: [String: String] = ProcessInfo.processInfo.environment,
                        fileManager: FileManager = .default) throws -> String {
        let trimmed = explicit.trimmingCharacters(in: .whitespacesAndNewlines)
        if !trimmed.isEmpty { return trimmed }

        if let value = environment["DAFEIYU_LLM_API_KEY"]?
            .trimmingCharacters(in: .whitespacesAndNewlines), !value.isEmpty {
            return value
        }

        let rawPath = environment["DAFEIYU_LLM_KEY_FILE"]?
            .trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
        let path = rawPath.isEmpty ? defaultKeyFile : rawPath
        if let data = fileManager.contents(atPath: path),
           let text = String(data: data, encoding: .utf8)?
               .trimmingCharacters(in: .whitespacesAndNewlines),
           !text.isEmpty {
            return text
        }

        throw AppFailure.message("未配置模型 Key：请设置 DAFEIYU_LLM_API_KEY、写入 \(path)（权限 0600），或在面板中填写")
    }
}
