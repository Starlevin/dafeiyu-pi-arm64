import Foundation
#if canImport(FoundationNetworking)
import FoundationNetworking
#endif
import Testing
@testable import WeChatAIBot
import BotCore
#if canImport(Glibc)
import Glibc
#endif

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
    nonisolated(unsafe) private static var payload = #"{"choices":[{"message":{"content":"{\"text\":\"在的呀。\",\"emotion\":\"joy\",\"intensity\":1}"},"finish_reason":"stop"}]}"#

    static func reset() { lock.lock(); requests = 0; payload = #"{"choices":[{"message":{"content":"{\"text\":\"在的呀。\",\"emotion\":\"joy\",\"intensity\":1}"},"finish_reason":"stop"}]}"#; lock.unlock() }
    static var count: Int { lock.lock(); defer { lock.unlock() }; return requests }
    /// 让下一次响应携带指定的 JSON（例如带 sticker_id 的表情选择结果）。
    static func setPayload(_ value: String) { lock.lock(); payload = value; lock.unlock() }

    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }
    override func stopLoading() {}

    override func startLoading() {
        FastPathFixtureProtocol.lock.lock()
        FastPathFixtureProtocol.requests += 1
        let body = FastPathFixtureProtocol.payload
        FastPathFixtureProtocol.lock.unlock()
        let data = Data(body.utf8)
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

    /// 单轮路径不能因为「拿不到草稿情绪」就把表情候选筛空。
    ///
    /// 2026-11-01 实测发现的缺陷：单轮路径原本按 `.neutral` 预筛候选，
    /// 而双阶段是「先读草稿情绪再筛」。表情库里若没有中性图，
    /// 单轮路径的候选会直接为空 → 生产私聊**静默丢失表情包**。
    /// 这里用「只有 teasing 图」的库复现该场景，并确认修复后仍能选到图。
    @Test @MainActor func fastPathStillOffersStickerCandidatesWithoutNeutralItems() async throws {
        FastPathFixtureProtocol.reset()
        let session = makeSession(); defer { session.invalidateAndCancel() }
        let teasing = QQStickerLibrary.Item(id: "synthetic-teasing", title: "逗你一下", emotion: .teasing,
                                             file: "synthetic.png", sha256: "synthetic", source: "synthetic")
        // 模型选中这张 teasing 图，且自报情绪也是 teasing。
        FastPathFixtureProtocol.setPayload(
            #"{"choices":[{"message":{"content":"{\"text\":\"哼，就知道逗你。\",\"emotion\":\"teasing\",\"intensity\":2,\"sticker_id\":\"synthetic-teasing\"}"},"finish_reason":"stop"}]}"#)
        let result = try await makeClient(session).reviewedQQReply(
            key: "synthetic", config: QQConfig().ai, rule: ChatRule(name: "fixture"),
            history: [], text: "逗我呀", stickerOptions: [teasing],
            userText: "逗我呀", toolsMayRun: false, reserve: {})
        #expect(FastPathFixtureProtocol.count == 1)
        #expect(result.stickerID == "synthetic-teasing")
    }

    /// 候选池必须覆盖各情绪；库里没有中性图时也不能把候选筛成空集。
    @Test func balancedShortlistCoversEveryEmotionAndNeverEmpties() {
        let items = [
            QQStickerLibrary.Item(id: "t1", title: "t1", emotion: .teasing, file: "a.png", sha256: "a", source: "s"),
            QQStickerLibrary.Item(id: "t2", title: "t2", emotion: .teasing, file: "b.png", sha256: "b", source: "s"),
            QQStickerLibrary.Item(id: "j1", title: "j1", emotion: .joy, file: "c.png", sha256: "c", source: "s"),
            QQStickerLibrary.Item(id: "n1", title: "n1", emotion: .neutral, file: "d.png", sha256: "d", source: "s")
        ]
        let shortlist = QQStickerLibrary.balancedShortlist(items)
        #expect(shortlist.count == 4)
        #expect(Set(shortlist.map(\.emotion)) == [.teasing, .joy, .neutral])
        // 关键：没有中性图时，候选不能为空。
        let teasingOnly = items.filter { $0.emotion != .neutral }
        let fallback = QQStickerLibrary.balancedShortlist(teasingOnly)
        #expect(!fallback.isEmpty)
        #expect(fallback.contains { $0.emotion == .teasing })
        // 上限生效。
        #expect(QQStickerLibrary.balancedShortlist(items, limit: 2).count == 2)
        #expect(QQStickerLibrary.balancedShortlist(items, limit: 0).isEmpty)
    }

    /// 默认必须是原版双阶段；单轮只能显式开启。
    ///
    /// 2026-11-01 修正：默认值原本是 fast。单轮路径此前 100% 不可达，默认值取哪个都看不出差别；
    /// 修正可达性后，默认 fast 会让**所有**下游行为（模型调用次数、表情选择时机）都偏离原版，
    /// 与「不设任何环境变量时行为与原版一致」的既有原则相矛盾。
    /// 现在默认回到原版；生产部署脚本显式设置 DAFEIYU_REPLY_MODE=fast，线上行为不受影响。
    @Test func defaultReplyModeIsOriginalAndFastIsOptIn() {
        #expect(LLMProvider().replyMode == .original)
        let key = "DAFEIYU_REPLY_MODE"
        let previous = ProcessInfo.processInfo.environment[key]
        defer { if let previous { setenv(key, previous, 1) } else { unsetenv(key) } }
        unsetenv(key)
        #expect(LLMProvider.fromEnvironment().replyMode == .original)
        setenv(key, "fast", 1)
        #expect(LLMProvider.fromEnvironment().replyMode == .fast)
        setenv(key, "original", 1)
        #expect(LLMProvider.fromEnvironment().replyMode == .original)
        // 未知值一律回落到原版，不得因为拼写错误就悄悄换一套行为。
        setenv(key, "turbo", 1)
        #expect(LLMProvider.fromEnvironment().replyMode == .original)
    }
}
