import Foundation

/// 可靠记忆接入模式。默认关闭；环境变量只影响 QQ 本地进程，不进入模型上下文。
///
/// - `DAFEIYU_MEMORY_RELIABLE=off`（默认）：完全不创建新目录，旧 `QQMemoryBook` 行为不变。
/// - `DAFEIYU_MEMORY_RELIABLE=shadow` 或 `DAFEIYU_MEMORY_RELIABLE_SHADOW=1`：
///   新 WAL 旁路写入，回复与整理仍走旧核心；用于对比、观察积压和恢复。
/// - `DAFEIYU_MEMORY_RELIABLE=1|true|yes|on|authoritative`：
///   新 store 成为回复/整理的主路径；旧 book 仍接收原始事件作为回滚镜像。
public enum QQMemoryReliableMode: String, Sendable, Equatable {
    case off
    case shadow
    case authoritative

    public static func resolve(environment: [String: String] = ProcessInfo.processInfo.environment) -> Self {
        let raw = (environment["DAFEIYU_MEMORY_RELIABLE"] ?? "")
            .trimmingCharacters(in: .whitespacesAndNewlines)
            .lowercased()
        let shadow = (environment["DAFEIYU_MEMORY_RELIABLE_SHADOW"] ?? "")
            .trimmingCharacters(in: .whitespacesAndNewlines)
            .lowercased()
        if raw == "shadow" || ["1", "true", "yes", "on"].contains(shadow) {
            return .shadow
        }
        if ["1", "true", "yes", "on", "authoritative"].contains(raw) {
            return .authoritative
        }
        return .off
    }

    public var isEnabled: Bool { self != .off }
    public var isAuthoritative: Bool { self == .authoritative }

    public var label: String {
        switch self {
        case .off: return "关闭"
        case .shadow: return "影子写入"
        case .authoritative: return "主路径"
        }
    }
}
