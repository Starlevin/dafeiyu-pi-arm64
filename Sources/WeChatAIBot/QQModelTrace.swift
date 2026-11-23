import Foundation
#if canImport(FoundationNetworking)
import FoundationNetworking
#endif

/// 本机 model-hub 的 trace 头写入规则。
///
/// 当前 Swift 端只允许一个契约端点：
/// `http://127.0.0.1:18090/v1/chat/completions`
/// （Python 网关的 Host 精确只认 `127.0.0.1:18090`，见 scripts/ops/model-hub.py）。
/// 别名 `localhost`/`::1`、其它 loopback 地址、其它端口/路径、第三方服务一律不加头。
///
/// 判定全部是白名单式等值比较，不做前缀/后缀匹配。percent-encoded 形式也不接受：
/// 原始串出现 `%` 即拒绝，避免 `/v1/%63hat/completions` 这类写法经 URL.path 解码后
/// 伪装成契约路径。
enum QQModelTrace {
    /// 与 model-hub 契约逐字一致的头名，大小写敏感。
    static let headerName = "X-Dafeiyu-Trace-Id"

    /// 当前唯一允许的契约 URL（字面 origin + path）。
    static let contractURLString = "http://127.0.0.1:18090/v1/chat/completions"

    private static let contractScheme = "http"
    private static let contractHost = "127.0.0.1"
    private static let contractPort = 18090
    private static let contractPath = "/v1/chat/completions"

    /// 网关接受 UUID 或 64hex 并自行规范化；Swift 端只发小写规范 UUID 串。
    static func normalized(_ trace: UUID) -> String {
        trace.uuidString.lowercased()
    }

    /// 仅接受契约字面 origin/path：scheme=http、host=127.0.0.1、port=18090、
    /// path 的 percent-encoded 原始形式必须精确等于 /v1/chat/completions，且不带
    /// userinfo/query/fragment。任何一项不满足即 fail closed。
    ///
    /// 这里用 `URLComponents.percentEncodedPath` 而不是 `URL.path`：`URL.path` 会做
    /// percent-decoding，并且会吞掉尾部斜杠（实测 `.../completions/` 的 path 变成
    /// `.../completions`），会把编码别名和尾斜杠误判成契约路径。
    static func isTrustedModelHub(_ url: URL) -> Bool {
        guard url.scheme?.lowercased() == contractScheme else { return false }
        guard url.host?.lowercased() == contractHost else { return false }
        guard url.port == contractPort else { return false }
        guard url.user == nil, url.password == nil else { return false }
        guard url.query == nil, url.fragment == nil else { return false }
        guard let rawPath = URLComponents(url: url, resolvingAgainstBaseURL: false)?.percentEncodedPath,
              rawPath == contractPath else { return false }
        return true
    }

    /// 只有「有 traceID 且目标可信」时才写头，返回是否写入；其余情况一律不改动请求。
    static func apply(to request: inout URLRequest, traceID: UUID?) -> Bool {
        guard let traceID, let url = request.url, isTrustedModelHub(url) else { return false }
        request.setValue(normalized(traceID), forHTTPHeaderField: headerName)
        return true
    }
}
