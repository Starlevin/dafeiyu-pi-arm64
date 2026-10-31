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
         configurationProblem: String? = nil) {
        self.kind = kind
        self.baseURL = baseURL ?? LLMProvider.defaultBaseURL(for: kind)
        self.sendsThinkingField = sendsThinkingField ?? (kind == .deepseek)
        self.sendsIdleReasoningEffort = sendsIdleReasoningEffort || kind == .sensenova
        self.modelOverride = modelOverride
        self.requestTimeout = requestTimeout
        self.maxRetries = maxRetries
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

        return LLMProvider(kind: kind,
                           baseURL: base,
                           sendsThinkingField: thinkingOverride ?? (kind == .deepseek),
                           sendsIdleReasoningEffort: kind == .sensenova,
                           modelOverride: rawModel.isEmpty ? nil : rawModel,
                           requestTimeout: timeout > 0 ? timeout : 45,
                           maxRetries: max(0, min(retries, 5)),
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
