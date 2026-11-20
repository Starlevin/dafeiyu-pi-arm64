import Foundation
import BotCore

/// 单进程：每轮请求从 QQ 当前进程取的「实时共享状态」（纯值、Sendable）。
///
/// 故意**不含**：modelClient（含 URLSession，非 Sendable）、QQEngine 对象、
/// identityVDialogue（lazy）、identityVMetrics（可变统计）、scopedProactiveProfiles（门禁）、
/// 以及任何 key 明文（key 只作为当次请求参数 `modelKey`，不进日志/快照/落盘）。
/// 所有字段来自 QQEngine 的 @Published config / 补丁开关 → 面板 save / setPersona 后
/// 下一轮自然用上新值（实现方必须每次重新读取，不缓存）。
public struct UnifiedSharedState: Sendable {
    public var botConfig: BotConfig
    public var persona: QQPersona
    public var identityVConfig: IdentityVConfig
    public var usesTemporaryCredentials: Bool
    /// 当次请求解析出的模型 key（不进快照之外、不进日志、不落盘）。
    public var modelKey: String

    public init(botConfig: BotConfig, persona: QQPersona, identityVConfig: IdentityVConfig,
                usesTemporaryCredentials: Bool, modelKey: String) {
        self.botConfig = botConfig
        self.persona = persona
        self.identityVConfig = identityVConfig
        self.usesTemporaryCredentials = usesTemporaryCredentials
        self.modelKey = modelKey
    }
}

/// 单进程宿主提供的「取实时共享状态」能力。
/// 实现方必须每次都读 QQEngine 的当前值（不缓存 config），否则面板 save 后不生效。
public protocol SharedStateProviding: Sendable {
    /// 在 @MainActor 内调用，返回当轮请求的实时快照。
    @MainActor func currentSharedState() throws -> UnifiedSharedState
}

/// 两进程 / fixture 用的静态提供者（值固定，保留旧行为）。
public struct StaticSharedState: SharedStateProviding {
    private let value: UnifiedSharedState
    public init(_ value: UnifiedSharedState) { self.value = value }
    @MainActor public func currentSharedState() throws -> UnifiedSharedState { value }
}
