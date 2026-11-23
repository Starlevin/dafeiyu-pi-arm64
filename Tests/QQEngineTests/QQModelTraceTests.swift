import Foundation
#if canImport(FoundationNetworking)
import FoundationNetworking
#endif
import Testing
@testable import WeChatAIBot

/// 纯 URL / 请求头判定，不建 socket、不发请求、不读任何凭据，两个平台都跑。
struct QQModelTraceTests {

    // 固定 UUID，便于逐字核对规范化后的头值。
    private static let sampleTrace = "AAAAAAAA-BBBB-CCCC-DDDD-EEEEEEEEEEEE"
    private static let otherTrace = "11111111-2222-3333-4444-555555555555"

    // 唯一契约端点；scheme 大小写按 origin 语义归一（网关 Host 精确只认 127.0.0.1:18090）。
    private static let trustedURLStrings = [
        "http://127.0.0.1:18090/v1/chat/completions",
        "HTTP://127.0.0.1:18090/v1/chat/completions"
    ]

    // 别名主机、其他 loopback 地址、其他端口/路径、第三方、query/fragment/userinfo。
    private static let aliasURLStrings = [
        "http://localhost:18090/v1/chat/completions",
        "http://LOCALHOST:18090/v1/chat/completions",
        "http://[::1]:18090/v1/chat/completions",
        "http://127.0.0.2:18090/v1/chat/completions",
        "http://127.1.1.1:18090/v1/chat/completions",
        "http://0.0.0.0:18090/v1/chat/completions",
        "https://127.0.0.1:18090/v1/chat/completions",       // https 同址不再采纳
        "http://127.0.0.1:18091/v1/chat/completions",        // 端口不符
        "http://127.0.0.1:8080/v1/chat/completions",         // 常见代理端口
        "http://127.0.0.1:18090/v1/models",                  // 路径不是 chat/completions
        "http://127.0.0.1:18090/v1/chat/completions/",       // 尾斜杠（URL.path 会吞掉，必须用原始 path 拒绝）
        "http://127.0.0.1:18090/v1/chat/completions//",      // 双尾斜杠
        "http://127.0.0.1:18090/V1/chat/completions",        // 路径大小写敏感
        "http://127.0.0.1:18090/v1/chat/completions?x=1",    // 带 query
        "http://127.0.0.1:18090/v1/chat/completions#frag",   // 带 fragment
        "http://user:pass@127.0.0.1:18090/v1/chat/completions", // 带 userinfo
        "http://api.deepseek.com:18090/v1/chat/completions", // 外部域名 + 相同路径
        "https://api.deepseek.com/v1/chat/completions",      // 生产模型服务
        "http://127.0.0.1.evil.com:18090/v1/chat/completions", // 后缀伪装
        "http://evil.com:18090/v1/chat/completions",         // 第三方同端口
        "http://10.0.0.1:18090/v1/chat/completions",         // 非 loopback 私网地址
        "http://127.0.0.1:18090"                             // 没有路径
    ]

    // percent-encoded 的等价写法：原始路径不是字面契约，必须拒绝，不能靠 URL.path 解码绕过。
    private static let encodedURLStrings = [
        "http://127.0.0.1:18090/v1/%63hat/completions",
        "http://127.0.0.1:18090/v1/chat/%63ompletions",
        "http://127.0.0.1:18090/v1/chat%2Fcompletions",
        "http://127.0.0.1:18090/v1/chat/completions%2F",
        "http://127.0.0.1:18090/v1/%2563hat/completions",   // double-encoded
        "http://127.0.0.1:18090/v1/chat/completions%00",
        "http://127.0.0.1:18090/v1/chat/completions%20"
    ]

    private static var untrustedURLStrings: [String] { aliasURLStrings + encodedURLStrings }

    private func requireURL(_ raw: String) throws -> URL {
        try #require(URL(string: raw))
    }

    /// 不可信矩阵用：URL 解析失败本身就不可能作为可信请求发出，按不可信处理，
    /// 避免 `%00`/IPv6 字面量在个别 corelibs 版本解析失败把用例变成假红。
    private func isTrusted(_ raw: String) -> Bool {
        URL(string: raw).map(QQModelTrace.isTrustedModelHub) ?? false
    }

    // MARK: - 头名契约

    @Test func traceHeaderNameMatchesGatewayContract() {
        #expect(QQModelTrace.headerName == "X-Dafeiyu-Trace-Id")
    }

    // MARK: - 值的规范化

    @Test func normalizedTraceIsLowerCaseCanonicalUUID() throws {
        let id = try #require(UUID(uuidString: Self.sampleTrace))
        #expect(QQModelTrace.normalized(id) == "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")
        #expect(QQModelTrace.normalized(id) == QQModelTrace.normalized(id).lowercased())
    }

    // MARK: - 契约字面量

    @Test func contractURLStringIsTheOnlyTrustedEndpoint() throws {
        let url = try requireURL(QQModelTrace.contractURLString)
        #expect(QQModelTrace.contractURLString == "http://127.0.0.1:18090/v1/chat/completions")
        #expect(QQModelTrace.isTrustedModelHub(url))
    }

    // MARK: - 可信矩阵

    @Test func trustedModelHubEndpointsAreAccepted() throws {
        for raw in Self.trustedURLStrings {
            let url = try requireURL(raw)
            #expect(QQModelTrace.isTrustedModelHub(url), "\(raw) 应被视为本机 model-hub")
        }
    }

    @Test func untrustedEndpointsAreRejected() throws {
        for raw in Self.untrustedURLStrings {
            #expect(isTrusted(raw) == false, "\(raw) 不应被视为本机 model-hub")
        }
    }

    @Test func aliasHostsAndOtherLoopbackAreRejected() throws {
        for raw in Self.aliasURLStrings {
            #expect(isTrusted(raw) == false, "\(raw) 是别名/非契约 origin，不应放行")
        }
    }

    @Test func percentEncodedPathAliasesAreRejected() throws {
        for raw in Self.encodedURLStrings {
            #expect(isTrusted(raw) == false, "\(raw) 是 percent-encoded 别名，不应放行")
        }
    }

    // MARK: - 头的写入

    @Test func applyAttachesNormalizedHeaderToTrustedEndpoint() throws {
        let id = try #require(UUID(uuidString: Self.sampleTrace))
        var request = URLRequest(url: try requireURL("http://127.0.0.1:18090/v1/chat/completions"))
        request.setValue("Bearer synthetic-key", forHTTPHeaderField: "Authorization")

        let traced = QQModelTrace.apply(to: &request, traceID: id)

        #expect(traced)
        #expect(request.value(forHTTPHeaderField: QQModelTrace.headerName) == "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")
        // 既有头不被 trace 逻辑影响。
        #expect(request.value(forHTTPHeaderField: "Authorization") == "Bearer synthetic-key")
    }

    @Test func applyNeverAttachesHeaderToUntrustedEndpoint() throws {
        let id = try #require(UUID(uuidString: Self.sampleTrace))
        for raw in Self.untrustedURLStrings {
            // 解析失败即不可能发出该请求；能解析的必须被 apply 拒绝且不写头。
            guard let url = URL(string: raw) else { continue }
            var request = URLRequest(url: url)
            let traced = QQModelTrace.apply(to: &request, traceID: id)
            #expect(traced == false, "\(raw) 不应写入 trace 头")
            #expect(request.value(forHTTPHeaderField: QQModelTrace.headerName) == nil, "\(raw) 出现了 trace 头")
        }
    }

    @Test func applyWithoutTraceIDLeavesRequestUntouched() throws {
        var request = URLRequest(url: try requireURL("http://127.0.0.1:18090/v1/chat/completions"))
        let traced = QQModelTrace.apply(to: &request, traceID: nil)
        #expect(traced == false)
        #expect(request.value(forHTTPHeaderField: QQModelTrace.headerName) == nil)
    }

    // MARK: - 同一轮重试共用同一个值

    @Test func sameTraceIDYieldsIdenticalHeaderAcrossAttempts() throws {
        let first = try #require(UUID(uuidString: Self.sampleTrace))
        let second = try #require(UUID(uuidString: Self.otherTrace))
        let url = try requireURL(QQModelTrace.contractURLString)

        var attempt1 = URLRequest(url: url)
        var attempt2 = URLRequest(url: url)
        var other = URLRequest(url: url)
        let traced1 = QQModelTrace.apply(to: &attempt1, traceID: first)
        let traced2 = QQModelTrace.apply(to: &attempt2, traceID: first)
        let tracedOther = QQModelTrace.apply(to: &other, traceID: second)
        #expect(traced1 && traced2 && tracedOther)

        let header1 = try #require(attempt1.value(forHTTPHeaderField: QQModelTrace.headerName))
        let header2 = try #require(attempt2.value(forHTTPHeaderField: QQModelTrace.headerName))
        let otherHeader = try #require(other.value(forHTTPHeaderField: QQModelTrace.headerName))
        #expect(header1 == header2)
        #expect(header1 != otherHeader)
    }

    // MARK: - 重定向约束
    //
    // 带 trace 的请求复用 BoundedDownload 的既有约束（redirectHosts 为空即拒绝一切 3xx），
    // 其「重定向被拒绝、目标收不到任何头」已由 Tests/QQEngineTests/BoundedDownloadTests.swift
    // 的真实 socket redirect 用例在两个平台覆盖，这里不再重复建 socket。
}
