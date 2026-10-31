import Foundation
#if canImport(FoundationNetworking)
import FoundationNetworking
#endif
import Testing
@testable import WeChatAIBot
import BotCore

/// 单轮快速路径的可达性测试。
///
/// 背景（2026-11-01 实测发现）：原来的风险判定检查的是**拼装后的模型输入**，
/// 而模型输入必然带程序自己加的 [OWNER]/[BOT]/[PEER]/[MEMBER] 说话人标记，
/// 于是「含归属/转述信号」这条在真实私聊里恒命中；同时 `toolHandler != nil`
/// 在联网/生图/插画任一开启时也恒成立。两者叠加，单轮快速路径 **100% 不可达**，
/// `DAFEIYU_REPLY_MODE=fast` 形同虚设。
///
/// 这里锁住修复后的行为：普通简短私聊走**一次**模型调用（快），
/// 只有真正的口语歧义或本轮确实要用工具时才回退双阶段（两次调用）。
private final class FastPathFixtureProtocol: URLProtocol, @unchecked Sendable {
    private static let lock = NSLock()
    nonisolated(unsafe) private static var requests = 0

    static func reset() { lock.lock(); requests = 0; lock.unlock() }
    static var count: Int { lock.lock(); defer { lock.unlock() }; return requests }

    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }
    override func stopLoading() {}

    override func startLoading() {
        FastPathFixtureProtocol.lock.lock()
        FastPathFixtureProtocol.requests += 1
        FastPathFixtureProtocol.lock.unlock()
        let payload = #"{"choices":[{"message":{"content":"{\"text\":\"在的呀。\",\"emotion\":\"joy\",\"intensity\":1}"},"finish_reason":"stop"}]}"#
        let data = Data(payload.utf8)
        let url = request.url!
        let response = HTTPURLResponse(url: url, statusCode: 200, httpVersion: "HTTP/1.1",
                                       headerFields: ["Content-Type": "application/json"])!
        client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
        client?.urlProtocol(self, didLoad: data)
        client?.urlProtocolDidFinishLoading(self)
    }
}

@Suite("单轮快速路径可达性")
struct QQFastReplyPathTests {

    private func makeSession() -> URLSession {
        let config = URLSessionConfiguration.ephemeral
        config.protocolClasses = [FastPathFixtureProtocol.self]
        return URLSession(configuration: config)
    }

    private func makeClient(_ session: URLSession) -> DeepSeekClient {
        var client = DeepSeekClient(session: session)
        client.provider.replyMode = .fast
        return client
    }

    /// 普通简短私聊：只应调用模型一次。
    @Test @MainActor func plainShortMessageUsesSingleModelCall() async throws {
        FastPathFixtureProtocol.reset()
        let session = makeSession(); defer { session.invalidateAndCancel() }
        let result = try await makeClient(session).reviewedQQReply(
            key: "synthetic", config: QQConfig().ai, rule: ChatRule(name: "fixture"),
            history: [], text: "在吗", userText: "在吗", toolsMayRun: false, reserve: {})
        #expect(FastPathFixtureProtocol.count == 1)
        #expect(QQPersona().decode(result.text, input: "在吗").text == "在的呀。")
    }

    /// 核心回归：模型输入里带程序加的说话人标记，**不应**把快速路径判死。
    /// 这是修复前 100% 不可达的根因。
    @Test @MainActor func speakerMarkersInModelInputDoNotForceTwoStages() async throws {
        FastPathFixtureProtocol.reset()
        let session = makeSession(); defer { session.invalidateAndCancel() }
        let built = "本轮之前的双向私聊记录：\n[PEER] 你好\n[BOT] 在的\n现在需要回复的对方消息：\n在吗"
        let result = try await makeClient(session).reviewedQQReply(
            key: "synthetic", config: QQConfig().ai, rule: ChatRule(name: "fixture"),
            history: [], text: built, userText: "在吗", toolsMayRun: false, reserve: {})
        #expect(FastPathFixtureProtocol.count == 1)
        #expect(QQPersona().decode(result.text, input: "在吗").text == "在的呀。")
    }

    /// 用户本轮发言含口语归属信号：必须回退双阶段（两次调用）。
    @Test @MainActor func ambiguousUserSpeechFallsBackToTwoStages() async throws {
        FastPathFixtureProtocol.reset()
        let session = makeSession(); defer { session.invalidateAndCancel() }
        _ = try await makeClient(session).reviewedQQReply(
            key: "synthetic", config: QQConfig().ai, rule: ChatRule(name: "fixture"),
            history: [], text: "帮我告诉主人今天修", userText: "帮我告诉主人今天修",
            toolsMayRun: false, reserve: {})
        #expect(FastPathFixtureProtocol.count == 2)
    }

    /// 本轮确实要用工具：必须回退双阶段，工具结果仍要经过语义校对。
    @Test @MainActor func toolUsingTurnFallsBackToTwoStages() async throws {
        FastPathFixtureProtocol.reset()
        let session = makeSession(); defer { session.invalidateAndCancel() }
        _ = try await makeClient(session).reviewedQQReply(
            key: "synthetic", config: QQConfig().ai, rule: ChatRule(name: "fixture"),
            history: [], text: "帮我搜一下 swift", userText: "帮我搜一下 swift",
            toolsMayRun: true, reserve: {})
        #expect(FastPathFixtureProtocol.count == 2)
    }

    /// 多轮上下文里出现指代词：保守起见仍走双阶段。
    @Test @MainActor func multiTurnPronounFallsBackToTwoStages() async throws {
        FastPathFixtureProtocol.reset()
        let session = makeSession(); defer { session.invalidateAndCancel() }
        let turn = ChatTurn(incoming: "在吗", outgoing: "在的")
        _ = try await makeClient(session).reviewedQQReply(
            key: "synthetic", config: QQConfig().ai, rule: ChatRule(name: "fixture"),
            history: [turn], text: "那个呢", userText: "那个呢", toolsMayRun: false, reserve: {})
        #expect(FastPathFixtureProtocol.count == 2)
    }
}
