#if os(Linux)
import Foundation
import FoundationNetworking
import Glibc
import Testing
import BotCore
@testable import WeChatAIBot

// 与 BoundedDownloadTests 保持同一套夹具风格：起真实 socket + 脚本化 Python mock 服务，
// 不 mock URLProtocol，不访问任何外部服务，不需要任何真实 API Key。
@Suite(.serialized) struct LLMProviderTests {

    // MARK: - 夹具

    private var fixturePath: String {
        URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent()
            .appendingPathComponent("llm_fixture.py").path
    }

    private func withFixture(_ body: (Int) async throws -> Void) async throws {
        let server = Process(), output = Pipe()
        server.executableURL = URL(fileURLWithPath: "/usr/bin/python3")
        server.arguments = ["-u", fixturePath]
        server.standardOutput = output
        server.standardError = FileHandle.nullDevice
        try server.run()
        defer {
            if server.isRunning {
                _ = Glibc.kill(server.processIdentifier, SIGKILL)
                server.waitUntilExit()
            }
        }
        let text = String(decoding: output.fileHandleForReading.availableData, as: UTF8.self)
            .trimmingCharacters(in: .whitespacesAndNewlines)
        let port = try #require(Int(text), "mock 服务未输出端口：\(text)")
        try await body(port)
    }

    private func makeClient(_ port: Int, mode: String, kind: LLMProvider.Kind = .custom,
                            timeout: TimeInterval = 10, retries: Int = 2) -> DeepSeekClient {
        var client = DeepSeekClient()
        client.provider = LLMProvider(
            kind: kind,
            baseURL: URL(string: "http://127.0.0.1:\(port)/\(mode)")!,
            requestTimeout: timeout,
            maxRetries: retries)
        client.session = URLSession(configuration: .ephemeral)
        return client
    }

    private func stats(port: Int, mode: String) async throws -> [String: Any] {
        let session = URLSession(configuration: .ephemeral)
        defer { session.finishTasksAndInvalidate() }
        let url = URL(string: "http://127.0.0.1:\(port)/__stats/\(mode)")!
        let (data, _) = try await session.data(for: URLRequest(url: url, timeoutInterval: 10))
        return (try JSONSerialization.jsonObject(with: data) as? [String: Any]) ?? [:]
    }

    private func lastRequest(port: Int, mode: String) async throws -> [String: Any] {
        let snapshot = try await stats(port: port, mode: mode)
        return snapshot["last"] as? [String: Any] ?? [:]
    }

    private func lastBody(port: Int, mode: String) async throws -> [String: Any] {
        let request = try await lastRequest(port: port, mode: mode)
        return request["body"] as? [String: Any] ?? [:]
    }

    @discardableResult
    private func reply(_ client: DeepSeekClient, thinking: Bool = false, json: Bool = false) async throws -> ModelResult {
        try await client.reply(key: "test-key", config: BotConfig(), rule: ChatRule(name: "test"),
                               history: [], text: "你好", jsonOutput: json,
                               thinkingEnabled: thinking, images: [], reserve: {})
    }

    // MARK: - 配置解析（纯逻辑，无需服务）

    @Test func defaultsStayOnDeepSeek() {
        let provider = LLMProvider.fromEnvironment([:])
        #expect(provider.kind == .deepseek)
        #expect(provider.baseURL.absoluteString == "https://api.deepseek.com")
        #expect(provider.sendsThinkingField)
        #expect(!provider.sendsIdleReasoningEffort)
        #expect(provider.modelOverride == nil)
        #expect(provider.requestTimeout == 45)
        #expect(provider.maxRetries == 2)
        #expect(provider.configurationProblem == nil)
    }

    @Test func senseNovaProfileFromEnvironment() {
        let provider = LLMProvider.fromEnvironment([
            "DAFEIYU_LLM_PROVIDER": "sensenova",
            "DAFEIYU_LLM_MODEL": "sensenova-6.8-flash-lite",
            "DAFEIYU_LLM_TIMEOUT": "30",
            "DAFEIYU_LLM_MAX_RETRIES": "1",
        ])
        #expect(provider.kind == .sensenova)
        #expect(provider.baseURL.absoluteString == "https://token.sensenova.cn/v1")
        #expect(!provider.sendsThinkingField)
        #expect(provider.sendsIdleReasoningEffort)
        #expect(provider.modelOverride == "sensenova-6.8-flash-lite")
        #expect(provider.requestTimeout == 30)
        #expect(provider.maxRetries == 1)
        #expect(provider.configurationProblem == nil)
    }

    @Test func customProviderWithoutBaseURLIsRejected() {
        let provider = LLMProvider.fromEnvironment(["DAFEIYU_LLM_PROVIDER": "custom"])
        #expect(provider.configurationProblem != nil)
    }

    @Test func nonHTTPBaseURLIsRejected() {
        let provider = LLMProvider.fromEnvironment([
            "DAFEIYU_LLM_PROVIDER": "custom",
            "DAFEIYU_LLM_BASE_URL": "ftp://example.com/v1",
        ])
        #expect(provider.configurationProblem != nil)
    }

    @Test func retryCountIsClamped() {
        #expect(LLMProvider.fromEnvironment(["DAFEIYU_LLM_MAX_RETRIES": "-5"]).maxRetries == 0)
        #expect(LLMProvider.fromEnvironment(["DAFEIYU_LLM_MAX_RETRIES": "99"]).maxRetries == 5)
    }

    // MARK: - 密钥解析顺序

    @Test func keyResolutionOrderIsExplicitThenEnvironmentThenFile() throws {
        let directory = FileManager.default.temporaryDirectory
            .appendingPathComponent("dafeiyu-key-\(UUID().uuidString)", isDirectory: true)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: directory) }
        let file = directory.appendingPathComponent("llm.key")
        try Data("file-key\n".utf8).write(to: file)
        try FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: file.path)

        let fromPanel = try LLMSecret.resolve(
            explicit: "panel-key",
            environment: ["DAFEIYU_LLM_API_KEY": "env-key", "DAFEIYU_LLM_KEY_FILE": file.path])
        #expect(fromPanel == "panel-key")

        let fromEnvironment = try LLMSecret.resolve(
            explicit: "   ",
            environment: ["DAFEIYU_LLM_API_KEY": "env-key", "DAFEIYU_LLM_KEY_FILE": file.path])
        #expect(fromEnvironment == "env-key")

        let fromFile = try LLMSecret.resolve(
            explicit: "",
            environment: ["DAFEIYU_LLM_KEY_FILE": file.path])
        #expect(fromFile == "file-key")

        #expect(throws: AppFailure.self) {
            try LLMSecret.resolve(
                explicit: "",
                environment: ["DAFEIYU_LLM_KEY_FILE": directory.appendingPathComponent("absent").path])
        }
    }

    // MARK: - 请求体按服务商裁剪

    @Test func deepSeekKeepsThinkingFieldAndBaseURL() async throws {
        try await withFixture { port in
            let client = makeClient(port, mode: "ok", kind: .deepseek)
            let result = try await reply(client)
            #expect(result.text == "mock reply")
            let body = try await lastBody(port: port, mode: "ok")
            #expect(body["thinking"] != nil)
            #expect(body["reasoning_effort"] == nil)
            #expect(body["model"] as? String == "deepseek-flash")
            #expect(body["stream"] as? Bool == false)
        }
    }

    @Test func senseNovaOmitsThinkingAndDeclaresIdleEffort() async throws {
        try await withFixture { port in
            let client = makeClient(port, mode: "ok", kind: .sensenova)
            try await reply(client)
            let body = try await lastBody(port: port, mode: "ok")
            #expect(body["thinking"] == nil)
            #expect(body["reasoning_effort"] as? String == "none")
        }
    }

    @Test func thinkingRequestAsksForHighEffortWithoutThinkingField() async throws {
        try await withFixture { port in
            let client = makeClient(port, mode: "ok", kind: .sensenova)
            try await reply(client, thinking: true)
            let body = try await lastBody(port: port, mode: "ok")
            #expect(body["reasoning_effort"] as? String == "high")
            #expect(body["thinking"] == nil)
        }
    }

    @Test func requestUsesConfiguredBaseURLAndBearerToken() async throws {
        try await withFixture { port in
            let client = makeClient(port, mode: "ok")
            try await reply(client)
            let request = try await lastRequest(port: port, mode: "ok")
            #expect(request["authorization"] as? String == "Bearer test-key")
            #expect((request["path"] as? String)?.hasSuffix("/chat/completions") == true)
        }
    }

    @Test func modelsEndpointFollowsConfiguredBaseURL() async throws {
        try await withFixture { port in
            let client = makeClient(port, mode: "ok")
            let models = try await client.models(key: "test-key")
            #expect(models == ["mock-model"])
        }
    }

    // MARK: - 错误映射与重试边界

    @Test func unauthorizedFailsImmediatelyWithoutRetry() async throws {
        try await withFixture { port in
            let client = makeClient(port, mode: "unauthorized", retries: 2)
            await #expect(throws: HTTPFailure.self) { try await reply(client) }
            let snapshot = try await stats(port: port, mode: "unauthorized")
            #expect(snapshot["count"] as? Int == 1)
        }
    }

    @Test func forbiddenFailsImmediatelyWithoutRetry() async throws {
        try await withFixture { port in
            let client = makeClient(port, mode: "forbidden", retries: 2)
            await #expect(throws: HTTPFailure.self) { try await reply(client) }
            let snapshot = try await stats(port: port, mode: "forbidden")
            #expect(snapshot["count"] as? Int == 1)
        }
    }

    @Test func quotaExhaustedRetriesExactlyOnceThenStops() async throws {
        try await withFixture { port in
            let client = makeClient(port, mode: "quota", retries: 1)
            await #expect(throws: HTTPFailure.self) { try await reply(client) }
            let snapshot = try await stats(port: port, mode: "quota")
            #expect(snapshot["count"] as? Int == 2)
        }
    }

    @Test func transientServerErrorIsRetriedAndThenSucceeds() async throws {
        try await withFixture { port in
            let client = makeClient(port, mode: "flaky", retries: 2)
            let result = try await reply(client)
            #expect(result.text == "mock reply")
            let snapshot = try await stats(port: port, mode: "flaky")
            #expect(snapshot["count"] as? Int == 2)
        }
    }

    @Test func timeoutIsReportedAsRetryableFailure() async throws {
        try await withFixture { port in
            let client = makeClient(port, mode: "slow", timeout: 0.6, retries: 0)
            do {
                try await reply(client)
                Issue.record("超时请求不应被视为成功")
            } catch let failure as HTTPFailure {
                #expect(failure.code == HTTPFailure.timeoutCode)
                #expect(failure.isRetryable)
            }
        }
    }
}
#endif
