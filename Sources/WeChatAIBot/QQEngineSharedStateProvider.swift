import Foundation
import BotCore

/// 单进程宿主：把 QQ 当前进程的实时配置/人格/第五人格开关/模型 key 提供给统一核心。
///
/// - 每次 `currentSharedState()` 都从**同一个** `QQEngine` 实例重新取快照，
///   因此 QQ 控制面板 `save` / `setPersona` / `connect`（内存临时凭证）之后，
///   下一条微信请求自然用上新配置（配置切换生效），而 QQ 侧完全不变。
/// - 模型客户端用 `engine.unifiedModelClient()` 的**值拷贝**构造生成器，
///   与 QQ 生产链路是同一个 `DeepSeekClient`（provider/baseURL/session 不分叉）。
/// - 模型 key 走 `engine.unifiedResolvedModelKey()`：与 QQ 完全相同的三级优先级，
///   明文只作为当次请求参数，绝不进快照/日志/落盘（不读任何登录凭证之外的东西）。
/// - QQEngine 是 @MainActor；本类型只在 @MainActor 内构造与调用，不跨 actor 持有它。

@MainActor
final class QQEngineSharedStateProvider: SharedStateProviding, @unchecked Sendable {
    private let engine: QQEngine
    /// 仅 fixture：显式给出的本机假模型 key。为空时走 QQ 的真实三级解析。
    private let fixtureModelKey: String

    @MainActor init(engine: QQEngine, fixtureModelKey: String = "") {
        self.engine = engine
        self.fixtureModelKey = fixtureModelKey
    }

    @MainActor func currentSharedState() throws -> UnifiedSharedState {
        let snapshot = engine.unifiedSharedState()
        // 与 QQEngine.drain:1038 完全同一套人格取法（全局 persona，无 per-target 覆盖，
        // 因为微信侧没有 QQTarget；如需按群风格可在快照里扩展，试点保持全局一致）。
        let persona = snapshot.config.effectivePersona
        let botConfig = snapshot.config.ai
        // 生产：与 QQ 完全相同的三级优先级（内存临时凭证 → 环境变量/0600 文件 → 钥匙串）。
        // fixture：只用显式合成 key，绝不触碰钥匙串或任何登录凭证。
        let key = fixtureModelKey.isEmpty ? (try engine.unifiedResolvedModelKey()) : fixtureModelKey
        return UnifiedSharedState(botConfig: botConfig,
                                   persona: persona,
                                   identityVConfig: snapshot.identityVConfig,
                                   usesTemporaryCredentials: snapshot.usesTemporaryCredentials,
                                   modelKey: key)
    }

    /// 用 QQ 进程实际的模型客户端值拷贝构造生成器（provider/baseURL/session 一致）。
    @MainActor func makeGenerator(fixtureBaseURL: URL? = nil, fixtureModel: String? = nil) -> DeepSeekReplyGenerator {
        let client: DeepSeekClient
        if let base = fixtureBaseURL {
            var provider = LLMProvider(kind: .custom, baseURL: base)
            if let model = fixtureModel, !model.isEmpty { provider.modelOverride = model }
            client = DeepSeekClient(provider: provider)
        } else {
            client = engine.unifiedModelClient()
        }
        return DeepSeekReplyGenerator(client: client)
    }
}
