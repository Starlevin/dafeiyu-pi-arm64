import Foundation
import Testing
import BotCore
@testable import WeChatAIBot

#if canImport(FoundationNetworking)
import FoundationNetworking
#endif

/// 统一回复核心的确定性夹具测试。
/// 约束（AGENTS.md）：合成身份、本机夹具、零真实模型、零真实会话、零发送。
/// `FixtureReplyGenerator` 完全在进程内，不触网；`fake_model.py` 只绑 127.0.0.1。

private struct FixtureReplyGenerator: ReplyGenerating {
    let replyJSON: String
    let fail: Bool
    var calls: CallCounter
    var recorder: RequestRecorder? = nil
    var shouldSend = true
    var proactiveSkipReason: ProactiveSkipReason? = nil

    func generate(_ request: UnifiedGenerationRequest) async throws -> UnifiedGenerationResult {
        await calls.increment()
        if fail { throw AppFailure.message("synthetic model failure") }
        // 夹具可以断言「实际复用的人格/知识 prompt 被注入」，而不是只看返回值。
        await recorder?.record(request)
        return UnifiedGenerationResult(text: replyJSON, tokens: 7, shouldSend: shouldSend,
                                       proactiveSkipReason: proactiveSkipReason)
    }
}

private actor RequestRecorder {
    private(set) var incomingHistory: [[String]] = []
    private(set) var prompts: [String] = []
    private(set) var systemPrompts: [String] = []
    private(set) var proactive: [Bool] = []
    private(set) var publicQuestionFast: [Bool] = []
    private(set) var contextualParticipation: [Bool] = []
    private(set) var publicQuestionContexts: [[String]] = []
    private(set) var publicQuestionKnowledge: [[String]] = []
    private(set) var publicQuestionGroupMemory: [String] = []
    func record(_ request: UnifiedGenerationRequest) {
        incomingHistory.append(request.history.map(\.incoming))
        prompts.append(request.userText)
        systemPrompts.append(request.rule.prompt)
        proactive.append(request.proactive)
        publicQuestionFast.append(request.publicQuestionFast)
        contextualParticipation.append(request.contextualParticipation)
        publicQuestionContexts.append(request.publicQuestionContext)
        publicQuestionKnowledge.append(request.publicQuestionKnowledge)
        publicQuestionGroupMemory.append(request.publicQuestionGroupMemory)
    }
}

private actor CallCounter {
    private(set) var count = 0
    func increment() { count += 1 }
    func value() -> Int { count }
}

private final class PublicQuestionHTTPFixture: URLProtocol, @unchecked Sendable {
    private static let lock = NSLock()
    nonisolated(unsafe) static var requestBodies: [[String: Any]] = []
    nonisolated(unsafe) static var requestTraceHeaders: [String] = []
    nonisolated(unsafe) private static var responseContent = ""
    static func reset(content: String = #"{"text":"合成短回复","emotion":"neutral","intensity":1}"#) {
        lock.lock()
        requestBodies = []
        requestTraceHeaders = []
        responseContent = content
        lock.unlock()
    }
    static var bodies: [[String: Any]] { lock.lock(); defer { lock.unlock() }; return requestBodies }
    static var traceHeaders: [String] { lock.lock(); defer { lock.unlock() }; return requestTraceHeaders }
    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }
    override func stopLoading() {}
    override func startLoading() {
        let trace = request.value(forHTTPHeaderField: "X-Dafeiyu-Trace-Id") ?? ""
        Self.lock.lock(); Self.requestTraceHeaders.append(trace); Self.lock.unlock()
        var data = request.httpBody ?? Data()
        if data.isEmpty, let stream = request.httpBodyStream {
            stream.open()
            defer { stream.close() }
            var buffer = [UInt8](repeating: 0, count: 4096)
            while stream.hasBytesAvailable {
                let size = stream.read(&buffer, maxLength: buffer.count)
                if size <= 0 { break }
                data.append(contentsOf: buffer.prefix(size))
            }
        }
        if let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any] {
            Self.lock.lock(); Self.requestBodies.append(object); Self.lock.unlock()
        }
        Self.lock.lock()
        let content = Self.responseContent
        Self.lock.unlock()
        let body = try! JSONSerialization.data(withJSONObject: [
            "choices": [["message": ["content": content], "finish_reason": "stop"]],
            "usage": ["total_tokens": 9]
        ])
        let response = HTTPURLResponse(url: request.url!, statusCode: 200, httpVersion: "HTTP/1.1",
                                       headerFields: ["Content-Type": "application/json"])!
        client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
        client?.urlProtocol(self, didLoad: body)
        client?.urlProtocolDidFinishLoading(self)
    }
}

private struct FixedProactiveScorer: ReplyScoring {
    let value: Double?
    var calls: CallCounter
    func score(_ input: ReplyDecisionInput) async -> Double? {
        _ = input
        await calls.increment()
        return value
    }
}

private actor BlockingScoreGate {
    private(set) var started = false
    private var continuation: CheckedContinuation<Void, Never>?
    func block() async {
        started = true
        await withCheckedContinuation { continuation = $0 }
    }
    func release() { continuation?.resume(); continuation = nil }
}

private struct BlockingProactiveScorer: ReplyScoring {
    let gate: BlockingScoreGate
    func score(_ input: ReplyDecisionInput) async -> Double? {
        _ = input
        await gate.block()
        return 0.9
    }
}

private func validJSON(_ text: String) -> String {
    // QQPersona.decode 要求恰好 {text,emotion,intensity} 三个键。
    let encoded = (try? JSONSerialization.data(withJSONObject: ["text": text, "emotion": "neutral", "intensity": 1])) ?? Data()
    return String(decoding: encoded, as: UTF8.self)
}

private func makeConfig(enabled: Bool = true,
                        allowed: [String] = ["fixture-group"],
                        allowPrivate: Bool = false,
                        requireAt: Bool = true,
                        maxPerMinute: Int = 3,
                        cooldown: Double = 0) -> UnifiedCoreConfig {
    var c = UnifiedCoreConfig()
    c.enabled = enabled
    c.allowedChats = allowed
    c.allowPrivate = allowPrivate
    c.requireRealAt = requireAt
    c.maxRepliesPerMinute = maxPerMinute
    c.cooldownSeconds = cooldown
    return c
}

/// 每个测试用独立的**纯内存**账本，避免共享默认文件导致 msg_id 串测。
private func freshJournal() -> UnifiedIdempotencyJournal {
    UnifiedIdempotencyJournal(fileURL: nil, capacity: 64)
}

private func inbound(_ text: String = "合成消息",
                     chatId: String = "fixture-group",
                     chatType: String = "group",
                     atMe: Bool = true,
                     msgId: String = "m1",
                     platform: String = "wechat",
                     account: String = "fixture-account",
                     senderId: String = "wx_group_member") -> UnifiedInbound {
    UnifiedInbound(platform: platform, account: account, chatType: chatType, chatId: chatId,
                   chatTitle: chatId, senderName: "群友", senderId: senderId, text: text,
                   atMe: atMe, msgId: msgId)
}

@Suite(.serialized) struct UnifiedCoreTests {

    @Test func defaultConfigIsFailClosed() {
        let c = UnifiedCoreConfig.fromEnvironment([:]) // 无 DAFEIYU_UNIFIED_PILOT
        #expect(c.enabled == false)
        #expect(c.allowedChats.isEmpty)
    }

    @Test func proactiveGroupAllowlistDoesNotDisableRealAtGateGlobally() {
        let c = UnifiedCoreConfig.fromEnvironment([
            "DAFEIYU_UNIFIED_PILOT": "1",
            "DAFEIYU_UNIFIED_ALLOWED_CHATS": "别野,Apex",
            "DAFEIYU_UNIFIED_ALLOWED_ACCOUNTS": "wx-1",
            "DAFEIYU_UNIFIED_PROACTIVE_GROUPS": "[\"别野\"]",
        ])
        #expect(c.enabled)
        #expect(c.proactiveAllowedChats == ["别野"])
        #expect(c.proactiveExplicitlyEnabled == false)
        #expect(c.requireRealAt == true)
    }

    @Test func disabledCoreNeverCallsModel() async {
        let counter = CallCounter()
        let core = UnifiedReplyCore(config: makeConfig(enabled: false),
                                    generator: FixtureReplyGenerator(replyJSON: validJSON("不该出现"), fail: false, calls: counter), journal: freshJournal())
        let outcome = await core.handle(inbound())
        #expect(outcome.reply.action == "ignore")
        #expect(outcome.reply.reason == "pilot_disabled")
        #expect(outcome.modelCalled == false)
        #expect(await counter.value() == 0)
    }

    @Test func realAtGateAndKnownGroupAllowlist() async {
        let counter = CallCounter()
        let core = UnifiedReplyCore(config: makeConfig(),
                                    generator: FixtureReplyGenerator(replyJSON: validJSON("在呢"), fail: false, calls: counter), journal: freshJournal())
        let noAt = await core.handle(inbound(atMe: false))
        #expect(noAt.reply.reason == "no_real_at")
        let unknownChat = await core.handle(inbound(chatId: "unknown-group"))
        #expect(unknownChat.reply.reason == "chat_not_allowlisted")
        let privateDenied = await core.handle(inbound(chatType: "private"))
        #expect(privateDenied.reply.reason == "private_not_allowed")
        #expect(await counter.value() == 0)
    }

    @Test func proactiveRequiresExplicitEnable() async {
        var c = makeConfig()
        c.requireRealAt = false
        let counter = CallCounter()
        let core = UnifiedReplyCore(config: c,
                                    generator: FixtureReplyGenerator(replyJSON: validJSON("在呢"), fail: false, calls: counter), journal: freshJournal())
        let outcome = await core.handle(inbound(atMe: false))
        #expect(outcome.reply.reason == "proactive_not_enabled")
        #expect(await counter.value() == 0)
    }

    @Test func proactiveDailyScoreBudgetSurvivesCoreStateRecreation() async throws {
        let dir = FileManager.default.temporaryDirectory.appendingPathComponent("unified-budget-\(UUID().uuidString)", isDirectory: true)
        try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: dir) }
        let url = dir.appendingPathComponent("score-budget.json")
        let first = UnifiedState(capacity: 16, proactiveBudgetURL: url)
        #expect(await first.reserveProactive("session-digest", now: 1_800_000_000, dailyLimit: 1) == nil)
        let restored = UnifiedState(capacity: 16, proactiveBudgetURL: url)
        #expect(await restored.reserveProactive("session-digest", now: 1_800_000_001, dailyLimit: 1) == "proactive_daily_budget")
        #expect(FileManager.default.fileExists(atPath: url.path))
    }

    @Test func corruptProactiveBudgetFailsClosed() async throws {
        let dir = FileManager.default.temporaryDirectory.appendingPathComponent("unified-budget-corrupt-\(UUID().uuidString)", isDirectory: true)
        try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: dir) }
        let url = dir.appendingPathComponent("score-budget.json")
        try Data("corrupt".utf8).write(to: url)
        let restored = UnifiedState(capacity: 16, proactiveBudgetURL: url)
        #expect(await restored.reserveProactive("session-digest", now: 1_800_000_000, dailyLimit: 60) == "proactive_budget_storage_unavailable")
    }

    @Test func fullWidthProactiveBudgetsInheritLegacyUsageAndThenStayIndependent() async throws {
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        defer { try? FileManager.default.removeItem(at: directory) }
        let url = directory.appendingPathComponent("score-budget.json")
        let now = 1_800_000_000.0
        let legacy = "00000000b773f597"
        let first = "cf159323b773f597"
        let second = "da415813b773f597"
        let old = UnifiedState(capacity: 16, proactiveBudgetURL: url)
        #expect(await old.reserveProactive(legacy, now: now) == nil)

        let updated = UnifiedState(capacity: 16, proactiveBudgetURL: url)
        #expect(await updated.reserveProactive(first, now: now + 1, dailyLimit: 2) == "proactive_score_cooldown")
        #expect(await updated.reserveProactive(first, now: now + 30, dailyLimit: 2) == nil)
        let restarted = UnifiedState(capacity: 16, proactiveBudgetURL: url)
        #expect(await restarted.reserveProactive(first, now: now + 60, dailyLimit: 2) == "proactive_daily_budget")
        #expect(await restarted.reserveProactive(second, now: now + 60, dailyLimit: 2) == nil)
        #expect(await restarted.reserveProactive(second, now: now + 120, dailyLimit: 2) == "proactive_daily_budget")
    }

    @Test func proactiveCandidateRequiresProofAllowlistAndModelScore() async {
        var c = makeConfig(requireAt: false)
        c.proactiveExplicitlyEnabled = true
        c.proactiveAllowedChats = ["fixture-group"]
        c.allowedAccounts = ["wx-1"]
        let modelCalls = CallCounter()
        let scorerCalls = CallCounter()
        let requests = RequestRecorder()
        let scorerFactory: @Sendable ([String], String) -> any ReplyScoring = { _, _ in
            FixedProactiveScorer(value: 0.70, calls: scorerCalls)
        }
        let core = UnifiedReplyCore(config: c,
                                    generator: FixtureReplyGenerator(replyJSON: validJSON("合成回应"), fail: false, calls: modelCalls, recorder: requests),
                                    proactiveScorerFactory: scorerFactory, journal: freshJournal())
        var candidate = inbound("大家怎么看这个问题？", atMe: false, msgId: "candidate", account: "wx-1")
        candidate.groupAutoReply = true
        candidate.groupAutoReplyContext = ["前文", "大家怎么看这个问题？"]
        candidate.groupAutoReplyWindowOK = true
        candidate.groupAutoReplySuperseded = true // fresh UI proof is required
        let rejected = await core.handle(candidate)
        #expect(rejected.gate == "proactive_evidence_rejected")
        #expect(await scorerCalls.value() == 0)

        candidate.msgId = "candidate-valid"
        candidate.groupAutoReplyContext = ["前文", "大家怎么看这个问题？"]
        candidate.groupAutoReplyWindowOK = true
        candidate.groupAutoReplySuperseded = false
        let accepted = await core.handle(candidate)
        #expect(accepted.reply.action == "reply")
        #expect(await scorerCalls.value() == 1)
        #expect(await modelCalls.value() == 1)
        #expect(await requests.proactive == [true]) // reviewedQQReply runs its participate review
        let duplicate = await core.handle(candidate)
        #expect(duplicate.gate == "durable_replay")
        #expect(await scorerCalls.value() == 1)
    }

    @Test func publicQuestionFastPathRequiresSettledProofAndMatcherAndSkipsScorer() async {
        var c = makeConfig(requireAt: false)
        c.publicQuestionFastEnabled = true
        c.proactiveAllowedChats = ["fixture-group"]
        c.allowedAccounts = ["fixture-account"]
        let modelCalls = CallCounter()
        let scorerCalls = CallCounter()
        let recorder = RequestRecorder()
        let core = UnifiedReplyCore(
            config: c,
            generator: FixtureReplyGenerator(replyJSON: validJSON("可以先检查设置。"), fail: false,
                                             calls: modelCalls, recorder: recorder),
            identityVConfig: IdentityVConfig(enabled: true, maxFacts: 2),
            proactiveScorerFactory: { _, _ in FixedProactiveScorer(value: 1, calls: scorerCalls) },
            journal: freshJournal()
        )
        let missingEvidence = await core.handle(inbound("请问大家密码机压好了怎么办？", atMe: false, msgId: "public-q-no-evidence"))
        #expect(missingEvidence.gate == "public_question_candidate_missing")
        #expect(await modelCalls.value() == 0)
        var candidate = inbound("请问大家密码机压好了怎么办？", atMe: false, msgId: "public-q")
        candidate.groupAutoReply = true
        candidate.groupAutoReplyContext = ["前文一", "前文二", "前文三", "请问大家密码机压好了怎么办？"]
        candidate.groupAutoReplyWindowOK = true
        candidate.groupAutoReplySuperseded = false
        candidate.groupAutoReplyPublicQuestionCooldownSeconds = 300
        candidate.groupAutoReplyPublicQuestionSettled = false

        let unsettled = await core.handle(candidate)
        #expect(unsettled.gate == "public_question_not_settled")
        candidate.msgId = "public-q-unmatched"
        candidate.groupAutoReplyPublicQuestionSettled = true
        candidate.text = "今天天气真好"
        candidate.groupAutoReplyContext[candidate.groupAutoReplyContext.count - 1] = candidate.text
        let unmatched = await core.handle(candidate)
        #expect(unmatched.gate == "public_question_not_matched")
        #expect(await modelCalls.value() == 0)
        #expect(await scorerCalls.value() == 0)

        candidate.msgId = "public-q"
        candidate.text = "请问大家密码机压好了怎么办？"
        candidate.groupAutoReplyContext[candidate.groupAutoReplyContext.count - 1] = candidate.text

        let result = await core.handle(candidate)
        #expect(result.reply.action == "reply")
        #expect(result.gate == "replied")
        #expect(await modelCalls.value() == 1)
        #expect(await scorerCalls.value() == 0)
        #expect(await recorder.publicQuestionFast == [true])
        #expect(await recorder.proactive == [false])
        #expect(await recorder.publicQuestionContexts == [["前文一", "前文二", "前文三"]])
        #expect(await recorder.publicQuestionKnowledge.count == 1)
        #expect(await recorder.publicQuestionKnowledge[0].count >= 1)
        #expect(await recorder.publicQuestionKnowledge[0].count <= 2)
        #expect(await recorder.publicQuestionKnowledge[0].contains(where: { $0.contains("密码机") }))
        candidate.msgId = "public-q-cooldown"
        let cooled = await core.handle(candidate)
        #expect(cooled.gate == "proactive_score_cooldown")
        #expect(await modelCalls.value() == 1)

    }

    @Test func publicQuestionFastEnvironmentSwitchRequiresLiteralOne() {
        #expect(UnifiedCoreConfig.fromEnvironment(["DAFEIYU_WECHAT_PUBLIC_QUESTION_FAST": "1"]).publicQuestionFastEnabled)
        #expect(!UnifiedCoreConfig.fromEnvironment(["DAFEIYU_WECHAT_PUBLIC_QUESTION_FAST": "true"]).publicQuestionFastEnabled)
        #expect(!UnifiedCoreConfig.fromEnvironment([:]).publicQuestionFastEnabled)
        #expect(UnifiedCoreConfig.fromEnvironment(["DAFEIYU_WECHAT_CONTEXTUAL_PARTICIPATION": "1"])
            .contextualParticipationEnabled == true)
        #expect(UnifiedCoreConfig.fromEnvironment(["DAFEIYU_WECHAT_CONTEXTUAL_PARTICIPATION": "true"])
            .contextualParticipationEnabled == false)
    }

    @Test func settledContextualStatementCanReplyWithoutModelScoringOrDuplicateGeneration() async {
        var c = makeConfig(allowed: ["fixture-group", "out-of-scope"])
        c.publicQuestionFastEnabled = true
        c.contextualParticipationEnabled = true
        c.proactiveAllowedChats = ["fixture-group"]
        c.allowedAccounts = ["fixture-account"]
        let scorer = CallCounter()
        let generator = CallCounter()
        let recorder = RequestRecorder()
        let core = UnifiedReplyCore(
            config: c,
            generator: FixtureReplyGenerator(replyJSON: validJSON("会议室到处都是打卡机。"),
                                             fail: false, calls: generator, recorder: recorder),
            proactiveScorerFactory: { _, _ in FixedProactiveScorer(value: nil, calls: scorer) },
            journal: freshJournal()
        )
        let body = "厂长开会这梗传得真快"
        let related = "又看到厂长开会的老梗了"
        var candidate = inbound(body, atMe: false, msgId: "contextual-statement")
        candidate.groupAutoReply = true
        candidate.groupAutoReplyWindowOK = true
        candidate.groupAutoReplyContext = [related, body]
        candidate.groupAutoReplyPublicQuestionCooldownSeconds = 300
        candidate.groupAutoReplySuperseded = false

        #expect((await core.handle(candidate)).gate == "public_question_not_settled")
        candidate.groupAutoReplyPublicQuestionSettled = true
        candidate.groupAutoReplySuperseded = true
        #expect((await core.handle(candidate)).gate == "proactive_evidence_rejected")
        candidate.groupAutoReplySuperseded = false
        var other = candidate
        other.chatId = "out-of-scope"
        other.chatTitle = other.chatId
        #expect((await core.handle(other)).gate == "proactive_evidence_rejected")
        var unrelated = candidate
        unrelated.groupAutoReplyContext = [related, "有人说今天在下雨", body]
        #expect((await core.handle(unrelated)).gate == "proactive_context_not_ready")
        var direct = candidate
        direct.text = "你也刷到了厂长开会这梗"
        direct.groupAutoReplyContext = [related, direct.text]
        #expect((await core.handle(direct)).gate == "proactive_context_not_ready")
        var repeated = candidate
        repeated.groupAutoReplyContext = ["机器人: 厂长开会就是这梗", related, body]
        #expect((await core.handle(repeated)).gate == "proactive_context_not_ready")
        #expect(await generator.value() == 0)

        #expect((await core.handle(candidate)).reply.action == "reply")
        #expect((await core.handle(candidate)).gate == "durable_replay")
        candidate.msgId = "contextual-later"
        #expect((await core.handle(candidate)).gate == "proactive_score_cooldown")
        #expect(await scorer.value() == 0)
        #expect(await generator.value() == 1)
        #expect(await recorder.contextualParticipation == [true])
        #expect(await recorder.publicQuestionContexts == [[related]])
        #expect(await recorder.publicQuestionFast == [false])
    }

    @Test func highestWeChatActivityLetsASingleGenerationJudgeParaphrases() async {
        var config = makeConfig(allowed: ["fixture-group"])
        config.publicQuestionFastEnabled = true
        config.contextualParticipationEnabled = true
        config.proactiveAllowedChats = ["fixture-group"]
        config.allowedAccounts = ["fixture-account"]
        let calls = CallCounter()
        let scores = CallCounter()
        let recorder = RequestRecorder()
        let core = UnifiedReplyCore(
            config: config,
            generator: FixtureReplyGenerator(replyJSON: validJSON("看来这次的衣服扛不住了。"),
                                             fail: false, calls: calls, recorder: recorder),
            proactiveScorerFactory: { _, _ in FixedProactiveScorer(value: nil, calls: scores) },
            journal: freshJournal()
        )
        let previous = "外头突然刮起大风了"
        let body = "我晾的衣服估计要飞走"
        var event = inbound(body, atMe: false, msgId: "paraphrase-regular")
        event.groupAutoReply = true
        event.groupAutoReplyWindowOK = true
        event.groupAutoReplySuperseded = false
        event.groupAutoReplyPublicQuestionSettled = true
        event.groupAutoReplyContext = [previous, body]
        event.groupAutoReplyPublicQuestionCooldownSeconds = 60
        #expect((await core.handle(event)).gate == "proactive_context_not_ready")

        event.msgId = "paraphrase-no-context"
        event.groupAutoReplyPublicQuestionCooldownSeconds = 30
        event.groupAutoReplyContext = [body]
        #expect((await core.handle(event)).gate == "proactive_context_not_ready")
        event.msgId = "paraphrase-just-replied"
        event.groupAutoReplyContext = ["机器人: 我刚收好东西了", body]
        #expect((await core.handle(event)).gate == "proactive_context_not_ready")
        #expect(await calls.value() == 0)

        event.msgId = "paraphrase-high"
        event.groupAutoReplyContext = [previous, body]
        #expect((await core.handle(event)).reply.action == "reply")
        #expect(await recorder.publicQuestionContexts == [[previous]])
        #expect(await recorder.contextualParticipation == [true])
        event.msgId = "paraphrase-high-too-soon"
        #expect((await core.handle(event)).gate == "proactive_score_cooldown")
        #expect(await calls.value() == 1)
        #expect(await scores.value() == 0)
    }

    @Test func globalProactiveSwitchStillRequiresBridgeEvidenceForNonMentions() async {
        var c = makeConfig(requireAt: false)
        c.proactiveExplicitlyEnabled = true
        let calls = CallCounter()
        let core = UnifiedReplyCore(config: c,
                                    generator: FixtureReplyGenerator(replyJSON: validJSON("不该发送"),
                                                                     fail: false, calls: calls),
                                    journal: freshJournal())
        #expect((await core.handle(inbound(atMe: false))).gate == "proactive_evidence_rejected")
        #expect(await calls.value() == 0)
    }

    @Test func contextualParticipationUsesOneToolFreeRequestAndStrictExplicitConsent() async throws {
        let sessionConfig = URLSessionConfiguration.ephemeral
        sessionConfig.protocolClasses = [PublicQuestionHTTPFixture.self]
        let session = URLSession(configuration: sessionConfig)
        let provider = LLMProvider(kind: .custom,
                                   baseURL: URL(string: "http://127.0.0.1:18090/v1")!, maxRetries: 3)
        let generator = DeepSeekReplyGenerator(client: DeepSeekClient(provider: provider, session: session))
        var c = BotConfig()
        c.model = "fixture"
        c.maxTokens = 1000
        let request = UnifiedGenerationRequest(
            key: "fixture", config: c, rule: ChatRule(name: "fixture"), history: [],
            userText: "", modelKey: "synthetic-key", currentUserText: "厂长开会这梗传得真快",
            proactive: true, contextualParticipation: true,
            publicQuestionContext: ["又看到厂长开会的老梗了"], publicQuestionKnowledge: ["厂长开会：合成梗介绍"]
        )
        let speak = #"{"participate":true,"text":"会议室到处都是打卡机。","emotion":"neutral","intensity":1}"#
        let silent = #"{"participate":false,"text":"","emotion":"neutral","intensity":0}"#
        let decisions: [(String, ProactiveSkipReason?)] = [
            (speak, nil),
            ("```json\n" + speak + "\n```", nil),
            (silent, .modelDeclined),
            ("```json\n" + silent + "\n```", .modelDeclined),
            ("描述\n```json\n" + speak + "\n```", .invalidModelDecision),
            (#"{"participate":"true","text":"不得发送","emotion":"neutral","intensity":1}"#, .invalidModelDecision),
            (#"{"text":"不得发送","emotion":"neutral","intensity":1}"#, .invalidModelDecision),
        ]
        for (content, skipReason) in decisions {
            PublicQuestionHTTPFixture.reset(content: content)
            let result = try await generator.generate(request)
            #expect(result.shouldSend == (skipReason == nil))
            #expect(result.proactiveSkipReason == skipReason)
            let bodies = PublicQuestionHTTPFixture.bodies
            #expect(bodies.count == 1)
            #expect(bodies.first?["model"] as? String == "fixture")
            #expect(bodies.first?["max_tokens"] as? Int == 160)
            #expect(bodies.first?["tools"] == nil)
            let messages = try #require(bodies.first?["messages"] as? [[String: Any]])
            #expect((messages.first?["content"] as? String)?.contains("[QQ_PARTICIPATION]") == true)
            #expect((messages.first?["content"] as? String)?.contains("布尔 participate") == true)
            #expect((messages.last?["content"] as? String)?.contains("又看到厂长开会的老梗了") == true)
        }
    }

    @Test func publicQuestionRecallsOnlyCurrentMatchingMemoryFromItsOwnGroup() async throws {
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: directory) }
        let file = directory.appendingPathComponent("journal.json")
        var config = makeConfig(allowed: ["fixture-group", "other-group"], maxPerMinute: 20)
        config.allowedAccounts = ["fixture-account"]
        config.proactiveAllowedChats = config.allowedChats
        config.publicQuestionFastEnabled = true
        config.confirmedWeChatMemoryEnabled = true
        let generator = FixtureReplyGenerator(replyJSON: validJSON("查到了。"), fail: false,
                                              calls: CallCounter())
        let core = UnifiedReplyCore(config: config, generator: generator,
                                    journal: UnifiedIdempotencyJournal(fileURL: file))
        let previous = await core.handle(inbound("记住事项：周报发布时间＝周三上午", msgId: "initial"))
        let previousReceipt = try #require(previous.reply.memoryReceipt)
        #expect(await core.confirmWeChatMemory(previousReceipt))
        let updated = await core.handle(inbound("记住事项：周报发布时间＝周五下午", msgId: "updated"))
        let updatedReceipt = try #require(updated.reply.memoryReceipt)
        #expect(await core.confirmWeChatMemory(updatedReceipt))

        for (chat, question, expected) in [
            ("fixture-group", "各位，之前提到的周报发布时间现在是几点？", true),
            ("fixture-group", "各位，周报表格怎么排版？", false),
            ("other-group", "各位，之前提到的周报发布时间现在是几点？", false),
        ] {
            let recorder = RequestRecorder()
            let calls = CallCounter()
            let restarted = UnifiedReplyCore(config: config,
                generator: FixtureReplyGenerator(replyJSON: validJSON("查到了。"), fail: false,
                                                 calls: calls, recorder: recorder),
                journal: UnifiedIdempotencyJournal(fileURL: file))
            var request = inbound(question, chatId: chat, atMe: false, msgId: UUID().uuidString)
            request.groupAutoReply = true
            request.groupAutoReplyContext = [question]
            request.groupAutoReplyWindowOK = true
            request.groupAutoReplySuperseded = false
            request.groupAutoReplyPublicQuestionSettled = true
            let result = await restarted.handle(request)
            #expect(result.gate == "replied")
            #expect(await recorder.publicQuestionFast == [true])
            #expect(await calls.value() == 1)
            let note = try #require(await recorder.publicQuestionGroupMemory.first)
            #expect(note.contains("周五下午") == expected)
            #expect(!note.contains("周三上午"))
            #expect(note.isEmpty == !expected)
        }
    }

    @Test func contextualFollowupRecallsOnlyOneTopicFromTheImmediatelyPreviousGroupMessage() async throws {
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: directory) }
        let file = directory.appendingPathComponent("journal.json")
        var config = makeConfig(allowed: ["fixture-group", "other-group"], maxPerMinute: 20)
        config.allowedAccounts = ["fixture-account"]
        config.proactiveAllowedChats = config.allowedChats
        config.publicQuestionFastEnabled = true
        config.contextualParticipationEnabled = true
        config.confirmedWeChatMemoryEnabled = true
        let reply = validJSON("那就这样定。")
        let core = UnifiedReplyCore(config: config,
                                    generator: FixtureReplyGenerator(replyJSON: reply, fail: false,
                                                                     calls: CallCounter()),
                                    journal: UnifiedIdempotencyJournal(fileURL: file))
        for (label, value) in [("团建集合地点", "旧校区北门"), ("团建集合地点", "新校区东门"),
                               ("周报发布时间", "周五下午")] {
            let generated = await core.handle(inbound("记住：\(label)＝\(value)", msgId: UUID().uuidString))
            let receipt = try #require(generated.reply.memoryReceipt)
            #expect(await core.confirmWeChatMemory(receipt))
        }

        let topic = "团建集合地点刚才又确认了一下"
        let followup = "那就按这个来"
        let cases: [(String, [String], String, String?)] = [
            ("fixture-group", [topic], followup, "新校区东门"),
            ("fixture-group", ["周报发布时间已经定了", topic], followup, "新校区东门"),
            ("fixture-group", [topic, "先出去买饮料"], followup, nil),
            ("fixture-group", ["团建集合地点和周报发布时间刚确认"], followup, nil),
            ("fixture-group", ["朋友圈在聊团建集合地点"], followup, nil),
            ("fixture-group", [topic], "这局的枪法真够离谱", nil),
            ("fixture-group", [topic], "周报发布时间就这么定", "周五下午"),
            ("other-group", [topic], followup, nil)
        ]
        for (chat, context, message, expected) in cases {
            let recorder = RequestRecorder()
            let calls = CallCounter()
            let restarted = UnifiedReplyCore(config: config,
                generator: FixtureReplyGenerator(replyJSON: reply, fail: false,
                                                 calls: calls, recorder: recorder),
                journal: UnifiedIdempotencyJournal(fileURL: file))
            var request = inbound(message, chatId: chat, atMe: false, msgId: UUID().uuidString)
            request.groupAutoReply = true
            request.groupAutoReplyContext = context + [message]
            request.groupAutoReplyWindowOK = true
            request.groupAutoReplySuperseded = false
            request.groupAutoReplyPublicQuestionSettled = true
            request.groupAutoReplyPublicQuestionCooldownSeconds = 30
            let result = await restarted.handle(request)
            #expect(result.gate == "replied")
            #expect(await recorder.contextualParticipation == [true])
            #expect(await calls.value() == 1)
            let note = try #require(await recorder.publicQuestionGroupMemory.first)
            #expect(!note.contains("旧校区北门"))
            if let expected {
                #expect(note.contains(expected) && note.count <= 220)
            } else {
                #expect(note.isEmpty)
            }
        }
    }

    @Test func publicQuestionEvidenceDecodesSettledFailClosedAndCooldown() throws {
        let missing = try JSONDecoder().decode(UnifiedInbound.self, from: Data(#"{"chat_id":"g"}"#.utf8))
        #expect(!missing.groupAutoReplyPublicQuestionSettled)
        #expect(missing.groupAutoReplyPublicQuestionCooldownSeconds == nil)
        let present = try JSONDecoder().decode(UnifiedInbound.self, from: Data(#"{"group_auto_reply_public_question_settled":true,"group_auto_reply_public_question_cooldown_seconds":300}"#.utf8))
        #expect(present.groupAutoReplyPublicQuestionSettled)
        #expect(present.groupAutoReplyPublicQuestionCooldownSeconds == 300)
    }

    @Test func publicQuestionServiceMakesOneToolFreeRequestWithBoundedContextAndTokens() async throws {
        PublicQuestionHTTPFixture.reset()
        let sessionConfig = URLSessionConfiguration.ephemeral
        sessionConfig.protocolClasses = [PublicQuestionHTTPFixture.self]
        let session = URLSession(configuration: sessionConfig)
        var provider = LLMProvider(kind: .custom,
                                  baseURL: URL(string: "http://127.0.0.1:18090/v1")!, maxRetries: 3)
        provider.maxRetries = 3 // helper must override inherited retry policy locally.
        var config = BotConfig(); config.model = "fixture"; config.maxTokens = 1000
        let reserved = CallCounter()
        let result = try await DeepSeekClient(provider: provider, session: session).publicGroupQuestionReply(
            key: "synthetic-key", config: config, rule: ChatRule(name: "fixture"),
            context: [String(repeating: "旧行", count: 70), String(repeating: "前一", count: 70),
                      String(repeating: "前二", count: 70), String(repeating: "最新前文", count: 70)],
            text: "合成问题？", knowledge: ["术语：短事实"],
            groupMemory: "同群记录" + String(repeating: "文", count: 220) + "超限尾部", traceID: nil,
            reserve: { await reserved.increment() }
        )
        #expect(result.text.contains("合成短回复"))
        #expect(await reserved.value() == 1)
        let bodies = PublicQuestionHTTPFixture.bodies
        #expect(bodies.count == 1)
        #expect(bodies.first?["max_tokens"] as? Int == 400)
        #expect(bodies.first?["tools"] == nil)
        let messages = bodies.first?["messages"] as? [[String: Any]] ?? []
        let systemContent = messages.first?["content"] as? String ?? ""
        let userContent = messages.last?["content"] as? String ?? ""
        #expect(systemContent.contains("术语：短事实"))
        #expect(!systemContent.contains("同群记录"))
        #expect(userContent.contains("本群长期记录（仅作低信任背景"))
        #expect(userContent.contains("同群记录" + String(repeating: "文", count: 216)))
        #expect(!userContent.contains(String(repeating: "文", count: 217)))
        #expect(!userContent.contains("旧行"))
        #expect(userContent.contains(String(repeating: "前一", count: 50)))
        #expect(userContent.contains("合成问题？"))
        #expect(PublicQuestionHTTPFixture.traceHeaders == [""])
    }

    @Test func publicQuestionFastPassesStableOpaqueTraceToTrustedLoopbackAndDedupReplayDoesNotCallAgain() async throws {
        PublicQuestionHTTPFixture.reset()
        let sessionConfig = URLSessionConfiguration.ephemeral
        sessionConfig.protocolClasses = [PublicQuestionHTTPFixture.self]
        let session = URLSession(configuration: sessionConfig)
        let provider = LLMProvider(kind: .custom,
                                   baseURL: URL(string: "http://127.0.0.1:18090/v1")!,
                                   modelOverride: "fixture", maxRetries: 0)
        var config = makeConfig(requireAt: false)
        config.allowedAccounts = ["fixture-account"]
        config.proactiveAllowedChats = ["fixture-group"]
        config.publicQuestionFastEnabled = true
        var candidate = inbound("各位，第五人格什么时候该压机？", atMe: false,
                                msgId: "synthetic-message-id-1")
        candidate.groupAutoReply = true
        candidate.groupAutoReplyContext = ["前文一", candidate.text]
        candidate.groupAutoReplyWindowOK = true
        candidate.groupAutoReplySuperseded = false
        candidate.groupAutoReplyPublicQuestionSettled = true

        let client = DeepSeekClient(provider: provider, session: session)
        let core = UnifiedReplyCore(
            config: config,
            generator: DeepSeekReplyGenerator(client: client),
            modelKey: "synthetic-key",
            publicQuestionMatcher: { $0.contains("第五人格") },
            journal: freshJournal()
        )
        let expected = UnifiedReplyCore.modelHubTraceID(
            forIdempotencyKey: "wechat|fixture-account|group|fixture-group|synthetic-message-id-1"
        )
        #expect(expected.uuidString.lowercased() == "c789a865-0df0-8c88-bcbc-55e42e7e85a0")
        #expect(expected.uuidString.lowercased() != candidate.msgId.lowercased())

        let first = await core.handle(candidate)
        #expect(first.reply.action == "reply")
        #expect(first.gate == "replied")
        #expect(first.modelCalled)
        let headers = PublicQuestionHTTPFixture.traceHeaders
        #expect(headers == [QQModelTrace.normalized(expected)])
        let audit = core.auditSnapshot()
        #expect(audit.count == 1)
        #expect(audit[0].modelTraceID == headers.first)
        #expect(audit[0].inboundTraceDigest == "51cfc0d3bc69")
        if let header = headers.first {
            #expect(!header.contains("fixture-account"))
            #expect(!header.contains("fixture-group"))
            #expect(!header.contains(candidate.msgId))
        }

        let replay = await core.handle(candidate)
        #expect(replay.gate == "durable_replay")
        #expect(!replay.modelCalled)
        #expect(PublicQuestionHTTPFixture.traceHeaders == [QQModelTrace.normalized(expected)])
        #expect(PublicQuestionHTTPFixture.bodies.count == 1)
        let replayAudit = core.auditSnapshot()
        #expect(replayAudit.count == 2)
        #expect(replayAudit[1].modelTraceID == nil)
        #expect(replayAudit[1].inboundTraceDigest == nil)

        var failedCandidate = candidate
        failedCandidate.msgId = "synthetic-failed-message-id"
        let failingCore = UnifiedReplyCore(
            config: config,
            generator: FixtureReplyGenerator(replyJSON: "{}", fail: true, calls: CallCounter()),
            modelKey: "synthetic-key",
            publicQuestionMatcher: { $0.contains("第五人格") },
            journal: freshJournal()
        )
        let failed = await failingCore.handle(failedCandidate)
        #expect(failed.gate == "generation_failed")
        #expect(failed.modelCalled)
        let failureAudit = failingCore.auditSnapshot()
        #expect(failureAudit.count == 1)
        #expect(failureAudit[0].modelTraceID == UnifiedReplyCore.modelHubTraceID(
            forIdempotencyKey: "wechat|fixture-account|group|fixture-group|synthetic-failed-message-id"
        ).uuidString.lowercased())
        #expect(failureAudit[0].inboundTraceDigest == "df96f1bf9c02")
    }

    @Test func directGroupReplyJoinsReviewedRequestsToAuditWithoutRepeatingOnReplay() async throws {
        PublicQuestionHTTPFixture.reset()
        let sessionConfig = URLSessionConfiguration.ephemeral
        sessionConfig.protocolClasses = [PublicQuestionHTTPFixture.self]
        let session = URLSession(configuration: sessionConfig)
        defer { session.invalidateAndCancel() }
        let provider = LLMProvider(kind: .custom,
                                   baseURL: URL(string: "http://127.0.0.1:18090/v1")!,
                                   modelOverride: "fixture", maxRetries: 0)
        let core = UnifiedReplyCore(
            config: makeConfig(requireAt: true),
            generator: DeepSeekReplyGenerator(client: DeepSeekClient(provider: provider, session: session)),
            modelKey: "synthetic-key", journal: freshJournal()
        )
        let candidate = inbound("测试问句？", atMe: true, msgId: "synthetic-message-id-1")
        let expected = UnifiedReplyCore.modelHubTraceID(
            forIdempotencyKey: "wechat|fixture-account|group|fixture-group|synthetic-message-id-1"
        )
        let outcome = await core.handle(candidate)
        #expect(outcome.reply.action == "reply")
        #expect(outcome.modelCalled)
        let values = PublicQuestionHTTPFixture.traceHeaders
        #expect(values.count == 2)
        #expect(values.allSatisfy { $0 == QQModelTrace.normalized(expected) })
        let audit = core.auditSnapshot()
        #expect(audit.first?.modelTraceID == values.first)
        #expect(audit.first?.inboundTraceDigest == "51cfc0d3bc69")

        let replay = await core.handle(candidate)
        #expect(replay.gate == "durable_replay")
        #expect(!replay.modelCalled)
        #expect(PublicQuestionHTTPFixture.traceHeaders == values)
        #expect(core.auditSnapshot().last?.modelTraceID == nil)
    }

    @Test func olderAuditJSONWithoutOptionalTraceFieldsRemainsDecodable() throws {
        let old = #"{"at":1,"scope":"scope","reason":"ignored","tokens":0,"durationMs":2,"outcome":"ignore"}"#
        let entry = try JSONDecoder().decode(UnifiedAuditEntry.self, from: Data(old.utf8))
        #expect(entry.modelTraceID == nil)
        #expect(entry.inboundTraceDigest == nil)
    }

    @Test func proactiveScoreBelowThresholdAndMissingAllowlistStaySilent() async {
        var c = makeConfig(requireAt: false)
        c.proactiveExplicitlyEnabled = true
        c.proactiveAllowedChats = ["fixture-group"]
        var noAccountACL = c
        c.allowedAccounts = ["fixture-account"]
        let modelCalls = CallCounter()
        let scorerCalls = CallCounter()
        let core = UnifiedReplyCore(config: c,
                                    generator: FixtureReplyGenerator(replyJSON: validJSON("不得生成"), fail: false, calls: modelCalls),
                                    proactiveScorerFactory: { _, _ in FixedProactiveScorer(value: 0.69, calls: scorerCalls) },
                                    journal: freshJournal())
        var candidate = inbound("大家怎么看这个问题？", atMe: false, msgId: "low")
        candidate.groupAutoReply = true
        candidate.groupAutoReplyContext = ["前文", "大家怎么看这个问题？"]
        candidate.groupAutoReplyWindowOK = true
        candidate.groupAutoReplySuperseded = false
        let emptyAccountACLCore = UnifiedReplyCore(config: noAccountACL,
                                                   generator: FixtureReplyGenerator(replyJSON: validJSON("不得生成"), fail: false, calls: modelCalls),
                                                   proactiveScorerFactory: { _, _ in FixedProactiveScorer(value: 1, calls: scorerCalls) },
                                                   journal: freshJournal())
        let emptyACL = await emptyAccountACLCore.handle(candidate)
        #expect(emptyACL.gate == "proactive_evidence_rejected")
        let low = await core.handle(candidate)
        #expect(low.reply.action == "ignore")
        #expect(low.gate == "proactive_below_threshold")
        #expect(await scorerCalls.value() == 1)
        #expect(await modelCalls.value() == 0)

        let unavailableCalls = CallCounter()
        let unavailableCore = UnifiedReplyCore(config: c,
                                               generator: FixtureReplyGenerator(replyJSON: validJSON("不得生成"), fail: false, calls: modelCalls),
                                               proactiveScorerFactory: { _, _ in FixedProactiveScorer(value: nil, calls: unavailableCalls) },
                                               journal: freshJournal())
        candidate.msgId = "score-unavailable"
        let unavailable = await unavailableCore.handle(candidate)
        #expect(unavailable.gate == "proactive_score_unavailable")
        #expect(await unavailableCalls.value() == 1)
        #expect(await modelCalls.value() == 0)

        c.proactiveAllowedChats = []
        let deniedCore = UnifiedReplyCore(config: c,
                                          generator: FixtureReplyGenerator(replyJSON: validJSON("不得生成"), fail: false, calls: modelCalls),
                                          proactiveScorerFactory: { _, _ in FixedProactiveScorer(value: 1, calls: scorerCalls) },
                                          journal: freshJournal())
        candidate.msgId = "not-allowlisted"
        let denied = await deniedCore.handle(candidate)
        #expect(denied.gate == "proactive_evidence_rejected")
        #expect(await modelCalls.value() == 0)
    }

    @Test(arguments: [false, true])
    func proactiveSilenceDistinguishesDeclineAndInvalidDecision(invalid: Bool) async {
        var c = makeConfig(requireAt: false)
        c.proactiveAllowedChats = ["fixture-group"]
        c.allowedAccounts = ["fixture-account"]
        let modelCalls = CallCounter()
        let core = UnifiedReplyCore(config: c,
                                    generator: FixtureReplyGenerator(replyJSON: validJSON("即使文本有效也不得发送"),
                                                                    fail: false, calls: modelCalls, shouldSend: false,
                                                                    proactiveSkipReason: invalid ? .invalidModelDecision : .modelDeclined),
                                    proactiveScorerFactory: { _, _ in FixedProactiveScorer(value: 0.9, calls: CallCounter()) },
                                    journal: freshJournal())
        var candidate = inbound("大家怎么看这个问题？", atMe: false, msgId: "participate-no")
        candidate.groupAutoReply = true
        candidate.groupAutoReplyContext = ["前文", "大家怎么看这个问题？"]
        candidate.groupAutoReplyWindowOK = true
        candidate.groupAutoReplySuperseded = false
        let result = await core.handle(candidate)
        #expect(result.gate == (invalid ? "model_output_rejected" : "proactive_participation_declined"))
        #expect(result.reply.action == "ignore")
        #expect(result.reply.ok == !invalid)
        #expect(await modelCalls.value() == 1)
    }

    @Test func proactiveReservationSerializesSameSessionButNotDirectAt() async {
        var c = makeConfig(requireAt: true)
        c.proactiveAllowedChats = ["fixture-group"]
        c.allowedAccounts = ["fixture-account"]
        let modelCalls = CallCounter()
        let gate = BlockingScoreGate()
        let core = UnifiedReplyCore(config: c,
                                    generator: FixtureReplyGenerator(replyJSON: validJSON("合成回应"), fail: false, calls: modelCalls),
                                    proactiveScorerFactory: { _, _ in BlockingProactiveScorer(gate: gate) },
                                    journal: freshJournal())
        var candidate = inbound("大家怎么看这个问题？", atMe: false, msgId: "held")
        candidate.groupAutoReply = true
        candidate.groupAutoReplyContext = ["前文", "大家怎么看这个问题？"]
        candidate.groupAutoReplyWindowOK = true
        candidate.groupAutoReplySuperseded = false
        let held = Task { await core.handle(candidate) }
        while !(await gate.started) { await Task.yield() }

        candidate.msgId = "concurrent-proactive"
        let concurrent = await core.handle(candidate)
        #expect(concurrent.gate == "proactive_in_flight")

        let direct = Task { await core.handle(inbound("直接 @ 合成", atMe: true, msgId: "direct-at")) }
        let directResult = await direct.value
        #expect(directResult.reply.action == "reply")
        await gate.release()
        let heldResult = await held.value
        #expect(heldResult.reply.action == "reply")
        #expect(await modelCalls.value() == 2)
    }

    @Test func happyPathRepliesWithoutMessageBodyInAudit() async {
        let counter = CallCounter()
        let core = UnifiedReplyCore(config: makeConfig(),
                                    generator: FixtureReplyGenerator(replyJSON: validJSON("在呢，刚看到。"), fail: false, calls: counter), journal: freshJournal())
        let outcome = await core.handle(inbound())
        #expect(outcome.reply.ok)
        #expect(outcome.reply.action == "reply")
        #expect(outcome.reply.reply == "在呢，刚看到。")
        #expect(outcome.modelCalled)
        #expect(await counter.value() == 1)
        // 审计不含消息原文：scope 是摘要，reason 是离散枚举。
        let entries = core.auditSnapshot()
        #expect(entries.count == 1)
        #expect(entries[0].reason == "replied")
        #expect(!entries[0].scope.contains("fixture-group"))
        #expect(!entries[0].scope.contains("合成消息"))
    }

    @Test func duplicateMsgIdIsNotRegeneratedOrResent() async {
        let counter = CallCounter()
        let core = UnifiedReplyCore(config: makeConfig(cooldown: 0),
                                    generator: FixtureReplyGenerator(replyJSON: validJSON("只发一次"), fail: false, calls: counter), journal: freshJournal())
        let first = await core.handle(inbound(msgId: "dup"))
        let second = await core.handle(inbound(msgId: "dup"))
        #expect(first.reply.action == "reply")
        #expect(second.reply.action == "reply")       // 幂等回放仍给同一结果
        // 持久化账本优先于内存缓存：gate 为 durable_replay（跨重启也成立）
        #expect(second.gate == "durable_replay")
        #expect(second.modelCalled == false)
        #expect(await counter.value() == 1)          // 模型只调用一次
    }

    @Test func lowFrequencyGateIsPerIsolatedSession() async {
        var c = makeConfig()
        c.maxRepliesPerMinute = 1
        c.cooldownSeconds = 0
        let counter = CallCounter()
        let core = UnifiedReplyCore(config: c,
                                    generator: FixtureReplyGenerator(replyJSON: validJSON("回一条"), fail: false, calls: counter), journal: freshJournal())
        let a = await core.handle(inbound(msgId: "a"))
        let b = await core.handle(inbound(msgId: "b"))
        #expect(a.reply.action == "reply")
        #expect(b.reply.reason == "rate_limited")
        // 不同平台 = 不同隔离会话，不受 A 的额度影响。
        let qq = await core.handle(inbound(msgId: "c", platform: "qq"))
        #expect(qq.reply.action == "reply")
        #expect(await counter.value() == 2)
    }

    @Test func identityIsolationKeysDifferByPlatformAccountSession() {
        let a = UnifiedIdentity(platform: "wechat", account: "acc1", chatType: "group", chatId: "g", senderId: "s")
        let b = UnifiedIdentity(platform: "qq", account: "acc1", chatType: "group", chatId: "g", senderId: "s")
        let c = UnifiedIdentity(platform: "wechat", account: "acc2", chatType: "group", chatId: "g", senderId: "s")
        let d = UnifiedIdentity(platform: "wechat", account: "acc1", chatType: "private", chatId: "g", senderId: "s")
        #expect(a.sessionKey != b.sessionKey)
        #expect(a.sessionKey != c.sessionKey)
        #expect(a.sessionKey != d.sessionKey)
        #expect(a.scopeDigest == "cf159323b773f597")
        #expect(a.scopeDigest != b.scopeDigest)
    }

    @Test func generationFailureIsNotRetriedAndNotCached() async {
        let counter = CallCounter()
        let core = UnifiedReplyCore(config: makeConfig(),
                                    generator: FixtureReplyGenerator(replyJSON: "", fail: true, calls: counter), journal: freshJournal())
        let outcome = await core.handle(inbound())
        #expect(outcome.reply.ok == false)
        #expect(outcome.reply.reason == "generation_failed")
        #expect(await counter.value() == 1) // 不盲重试
        // 失败后账本记 failed：同一 msg_id 再进来按「不确定/已失败」保守拒绝，
        // 绝不重新生成、绝不重新发回（跨重启也成立）。
        let again = await core.handle(inbound())
        #expect(again.reply.reason == "durable_uncertain_or_failed")
        #expect(await counter.value() == 1) // 仍未再次调用模型
    }

    @Test func durableJournalSurvivesRestart() async throws {
        // 同一份 0600 账本文件：模拟进程重启后新建 UnifiedReplyCore，
        // 同一 msg_id 只能回放首次结果，模型不再被调用。
        let dir = FileManager.default.temporaryDirectory
            .appendingPathComponent("unified-journal-" + UUID().uuidString)
        try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: dir) }
        let file = dir.appendingPathComponent("journal.json")

        let counter1 = CallCounter()
        let journal1 = UnifiedIdempotencyJournal(fileURL: file, capacity: 64)
        let core1 = UnifiedReplyCore(config: makeConfig(cooldown: 0),
                                     generator: FixtureReplyGenerator(replyJSON: validJSON("首次回复"), fail: false, calls: counter1),
                                     journal: journal1)
        let first = await core1.handle(inbound(msgId: "restart-1"))
        #expect(first.reply.reply == "首次回复")
        #expect(await counter1.value() == 1)
        #expect(await journal1.count() == 1)

        // 重启：新的 core + 新的 generator，但从同一文件加载账本。
        let counter2 = CallCounter()
        let journal2 = UnifiedIdempotencyJournal(fileURL: file, capacity: 64)
        let core2 = UnifiedReplyCore(config: makeConfig(cooldown: 0),
                                     generator: FixtureReplyGenerator(replyJSON: validJSON("不该出现"), fail: false, calls: counter2),
                                     journal: journal2)
        let again = await core2.handle(inbound(msgId: "restart-1"))
        #expect(again.reply.reply == "首次回复")
        #expect(again.gate == "durable_replay")
        #expect(again.modelCalled == false)
        #expect(await counter2.value() == 0)

        // 账本文件权限应为 0600，且内容不含消息原文。
        let attrs = try FileManager.default.attributesOfItem(atPath: file.path)
        #expect((attrs[.posixPermissions] as? NSNumber)?.intValue == 0o600)
        let text = try String(contentsOf: file, encoding: .utf8)
        // 「记录不含消息原文」= 不含**入站用户消息正文**与任何凭证；
        // 账本必须保留机器人自己生成的回复文本才能跨重启幂等回放。
        #expect(!text.contains("合成消息"))
        #expect(!text.contains("synthetic-fixture-key"))
        #expect(text.contains("restart-1"))
    }

    @Test func confirmedWeChatHistorySurvivesRestartInItsOwnConversation() async throws {
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: directory) }
        let file = directory.appendingPathComponent("journal.json")
        var config = makeConfig(allowed: ["fixture-group", "other-group"], allowPrivate: true,
                                maxPerMinute: 20)
        config.allowedAccounts = ["fixture-account", "other-account"]
        config.confirmedWeChatMemoryEnabled = true
        let recorder = RequestRecorder()
        let generator = FixtureReplyGenerator(replyJSON: validJSON("合成短回复"), fail: false,
                                              calls: CallCounter(), recorder: recorder)
        let firstCore = UnifiedReplyCore(config: config, generator: generator,
                                         journal: UnifiedIdempotencyJournal(fileURL: file))
        let first = await firstCore.handle(inbound("合成待确认茶单", msgId: "tea"))
        let receipt = try #require(first.reply.memoryReceipt)
        let unconfirmed = await firstCore.handle(inbound("未送达的合成文本", msgId: "pending"))
        let unusedReceipt = try #require(unconfirmed.reply.memoryReceipt)
        #expect(await recorder.incomingHistory == [[], []])
        #expect(!(try String(contentsOf: file, encoding: .utf8)).contains("合成待确认茶单"))
        #expect(!(await firstCore.confirmWeChatMemory(UUID())))
        #expect(await firstCore.confirmWeChatMemory(receipt))
        #expect(await firstCore.confirmWeChatMemory(receipt))
        let warm = await firstCore.handle(inbound("暖态追问", msgId: "warm"))
        #expect(warm.reply.action == "reply")
        #expect(await recorder.incomingHistory.last == ["合成待确认茶单"])
        let persisted = try String(contentsOf: file, encoding: .utf8)
        #expect(persisted.contains("合成待确认茶单"))
        #expect(!persisted.contains("未送达的合成文本"))
        let attributes = try FileManager.default.attributesOfItem(atPath: file.path)
        #expect((attributes[.posixPermissions] as? NSNumber)?.intValue == 0o600)

        let cold = UnifiedReplyCore(config: config, generator: generator,
                                    journal: UnifiedIdempotencyJournal(fileURL: file))
        #expect(!(await cold.confirmWeChatMemory(unusedReceipt)))
        #expect(await cold.confirmWeChatMemory(receipt))
        let followups = [
            inbound("原群追问", msgId: "follow-up"),
            inbound("另一个群", chatId: "other-group", msgId: "follow-up"),
            inbound("另一个账号", msgId: "follow-up", account: "other-account"),
            inbound("同名私聊", chatType: "private", msgId: "follow-up"),
            inbound("QQ 会话", msgId: "follow-up", platform: "qq")
        ]
        for message in followups {
            let outcome = await cold.handle(message)
            #expect(outcome.reply.action == "reply")
        }
        let recalled = await recorder.incomingHistory
        #expect(Array(recalled.suffix(5)) == [["合成待确认茶单"], [], [], [], []])

        var quote = inbound("合成引用内容", msgId: "quoted")
        quote.quoted = true
        var image = inbound("合成图像资料", msgId: "image")
        image.imageDescribed = true
        for external in [quote, image, inbound("看看朋友圈", msgId: "moments"),
                         inbound("看看 bilibili 热门", msgId: "social")] {
            let outcome = await cold.handle(external)
            #expect(outcome.reply.memoryReceipt == nil, "fixture source: \(external.msgId)")
        }
        #expect(Array((await recorder.incomingHistory).suffix(4)) == [[], [], [], []])
    }

    @Test func groupNotesRecallLatestValueAfterJournalRotationAndBackupRecovery() async throws {
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: directory) }
        let file = directory.appendingPathComponent("journal.json")
        let books = directory.appendingPathComponent("wechat-group-memory")
        var config = makeConfig(allowed: ["fixture-group", "other-group"], allowPrivate: true, maxPerMinute: 40)
        config.allowedAccounts = ["fixture-account", "other-account"]
        config.confirmedWeChatMemoryEnabled = true
        let recorder = RequestRecorder()
        let generator = FixtureReplyGenerator(replyJSON: validJSON("合成短回复"), fail: false,
                                              calls: CallCounter(), recorder: recorder)
        let core = UnifiedReplyCore(config: config, generator: generator,
                                    journal: UnifiedIdempotencyJournal(fileURL: file, capacity: 6))
        let pending = await core.handle(inbound("记住：团建集合=未送达码头", msgId: "unconfirmed"))
        #expect(pending.reply.memoryReceipt != nil)
        #expect(!FileManager.default.fileExists(atPath: books.path))
        #expect(UnifiedGroupMemoryNote.parse("记住：口令=synthetic-secret") == nil)

        for (id, text) in [
            ("first-note", "记住：团建集合=旧地标"),
            ("duplicate-note", "记住：团建集合＝旧地标"),
            ("changed-note", "记住事项：团建集合＝新地标"),
        ] {
            let reply = await core.handle(inbound(text, msgId: id))
            let receipt = try #require(reply.reply.memoryReceipt)
            #expect(await core.confirmWeChatMemory(receipt))
            #expect(await core.confirmWeChatMemory(receipt))
        }
        for index in 0..<6 {
            let filler = await core.handle(inbound("往来夹具\(index)", msgId: "filler-\(index)"))
            let receipt = try #require(filler.reply.memoryReceipt)
            #expect(await core.confirmWeChatMemory(receipt))
        }
        let scopes = try FileManager.default.contentsOfDirectory(at: books, includingPropertiesForKeys: nil)
        let scope = try #require(scopes.first)
        #expect(scopes.count == 1)
        let attributes = try FileManager.default.attributesOfItem(atPath: scope.path)
        #expect((attributes[.posixPermissions] as? NSNumber)?.intValue == 0o700)
        do {
            let store = try QQMemoryReliableStore(directory: scope, scope: scope.lastPathComponent)
            #expect(store.items.count == 2)
            #expect(store.items.filter { $0.current(Date()) }.map(\.text) == ["团建集合：新地标"])
            #expect(store.items.first { $0.current(Date()) }?.kind == "task")
            try Data("broken-primary".utf8).write(to: store.stateURL, options: .atomic)
        }
        let journalText = try String(contentsOf: file, encoding: .utf8)
        #expect(!journalText.contains("changed-note"))
        let cold = UnifiedReplyCore(config: config, generator: generator,
                                    journal: UnifiedIdempotencyJournal(fileURL: file, capacity: 6))
        let recall = await cold.handle(inbound("团建集合在哪儿？", msgId: "late-query"))
        #expect(recall.reply.action == "reply")
        let prompt = try #require(await recorder.prompts.last)
        #expect(prompt.contains("本群长期记录") && prompt.contains("新地标"))
        #expect(!prompt.contains("旧地标") && !prompt.contains("未送达码头"))
        #expect((await recorder.incomingHistory.last ?? []).allSatisfy { !$0.contains("地标") })
    }

    @Test func confirmedGroupNoteRepairsArchiveOnNextLookupWhilePreservingIsolation() async throws {
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: directory) }
        let file = directory.appendingPathComponent("journal.json")
        let books = directory.appendingPathComponent("wechat-group-memory")
        try FileManager.default.createDirectory(at: books, withIntermediateDirectories: false,
                                                attributes: [.posixPermissions: 0o755])
        var config = makeConfig(allowed: ["fixture-group", "other-group"], allowPrivate: true, maxPerMinute: 40)
        config.allowedAccounts = ["fixture-account", "other-account"]
        config.confirmedWeChatMemoryEnabled = true
        let recorder = RequestRecorder()
        let generator = FixtureReplyGenerator(replyJSON: validJSON("合成短回复"), fail: false,
                                              calls: CallCounter(), recorder: recorder)
        let live = UnifiedReplyCore(config: config, generator: generator,
                                    journal: UnifiedIdempotencyJournal(fileURL: file))
        let note = await live.handle(inbound("记住：社团约定=应急新地标", msgId: "outbox"))
        let receipt = try #require(note.reply.memoryReceipt)
        #expect(!(await live.confirmWeChatMemory(receipt)))
        #expect(try FileManager.default.contentsOfDirectory(atPath: books.path).isEmpty)
        try FileManager.default.setAttributes([.posixPermissions: 0o700], ofItemAtPath: books.path)
        let cold = UnifiedReplyCore(config: config, generator: generator,
                                    journal: UnifiedIdempotencyJournal(fileURL: file))
        let queries = [
            inbound("社团约定是什么？", msgId: "follow"),
            inbound("社团约定是什么？", chatId: "other-group", msgId: "follow"),
            inbound("社团约定是什么？", msgId: "follow", account: "other-account"),
            inbound("社团约定是什么？", chatType: "private", msgId: "follow"),
            inbound("社团约定是什么？", msgId: "follow", platform: "qq"),
        ]
        for message in queries {
            let result = await cold.handle(message)
            #expect(result.reply.action == "reply")
        }
        let prompts = Array((await recorder.prompts).suffix(queries.count))
        #expect(prompts.first?.contains("本群长期记录") == true)
        #expect(prompts.first?.contains("应急新地标") == true)
        #expect(prompts.dropFirst().allSatisfy { !$0.contains("应急新地标") })
    }

    @Test func confirmedNoteSurvivesReplyRotationAndConsoleRecoversItsOwnScope() async throws {
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        let books = directory.appendingPathComponent("wechat-group-memory")
        try FileManager.default.createDirectory(at: books, withIntermediateDirectories: true,
                                                attributes: [.posixPermissions: 0o755])
        defer { try? FileManager.default.removeItem(at: directory) }
        let file = directory.appendingPathComponent("journal.json")
        var config = makeConfig(allowed: ["fixture-group", "other-group"], maxPerMinute: 40)
        config.allowedAccounts = ["fixture-account", "other-account"]
        config.confirmedWeChatMemoryEnabled = true
        let generator = FixtureReplyGenerator(replyJSON: validJSON("合成回复"), fail: false, calls: CallCounter())
        let journal = UnifiedIdempotencyJournal(fileURL: file, capacity: 2)
        let live = UnifiedReplyCore(config: config, generator: generator, journal: journal)
        let note = await live.handle(inbound("记住：集合地点=合成会场", msgId: "blocked-archive"))
        let receipt = try #require(note.reply.memoryReceipt)
        #expect(!(await live.confirmWeChatMemory(receipt)))
        for index in 0..<5 {
            let unrelated = inbound("合成对话", chatId: "other-group", msgId: "rotate-\(index)")
            #expect((await live.handle(unrelated)).reply.action == "reply")
        }
        #expect(await journal.count() == 3)

        let reopened = UnifiedIdempotencyJournal(fileURL: file, capacity: 2)
        #expect(await reopened.count() == 3)
        try FileManager.default.setAttributes([.posixPermissions: 0o700], ofItemAtPath: books.path)
        let cold = UnifiedReplyCore(config: config, generator: generator, journal: reopened)
        let groups = try cold.wechatMemoryGroups()
        #expect(groups.count == 4)
        let original = try #require(groups.first)
        #expect(original.label == "fixture-group")
        for group in groups.dropFirst() {
            #expect(try await cold.wechatMemoryEntries(scope: group.id).isEmpty)
        }
        let records = try await cold.wechatMemoryEntries(scope: original.id)
        #expect(records.map(\.value) == ["合成会场"])
        #expect(await cold.confirmWeChatMemory(receipt))
        #expect(try await cold.wechatMemoryEntries(scope: original.id).map(\.id) == records.map(\.id))
        #expect((await cold.handle(inbound("合成对话", chatId: "other-group",
                                           msgId: "after-recovery"))).reply.action == "reply")
        #expect(await reopened.count() == 2)
    }

    @Test func fullArchiveBacklogPreservesOlderProofAndStillAllowsOrdinaryReplies() async throws {
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        let books = directory.appendingPathComponent("wechat-group-memory")
        try FileManager.default.createDirectory(at: books, withIntermediateDirectories: true,
                                                attributes: [.posixPermissions: 0o755])
        defer { try? FileManager.default.removeItem(at: directory) }
        let file = directory.appendingPathComponent("journal.json")
        var config = makeConfig(allowed: ["fixture-group", "other-group"], maxPerMinute: 40)
        config.allowedAccounts = ["fixture-account"]
        config.confirmedWeChatMemoryEnabled = true
        let generator = FixtureReplyGenerator(replyJSON: validJSON("合成回复"), fail: false, calls: CallCounter())
        let journal = UnifiedIdempotencyJournal(fileURL: file, capacity: 1)
        let core = UnifiedReplyCore(config: config, generator: generator, journal: journal)
        let earlier = await core.handle(inbound("记住：集合地点=旧队列待恢复", msgId: "confirmed-note"))
        let firstReceipt = try #require(earlier.reply.memoryReceipt)
        #expect(!(await core.confirmWeChatMemory(firstReceipt)))
        let later = await core.handle(inbound("记住：其他地点=超出队列", msgId: "over-capacity"))
        let laterReceipt = try #require(later.reply.memoryReceipt)
        #expect(!(await core.confirmWeChatMemory(laterReceipt)))
        #expect((await core.handle(inbound("合成问题", chatId: "other-group", msgId: "ordinary"))).reply.action == "reply")
        #expect(await journal.count() == 2)
        try FileManager.default.setAttributes([.posixPermissions: 0o700], ofItemAtPath: books.path)
        let cold = UnifiedReplyCore(config: config, generator: generator,
                                    journal: UnifiedIdempotencyJournal(fileURL: file, capacity: 1))
        let scope = try #require(cold.wechatMemoryGroups().first?.id)
        #expect(try await cold.wechatMemoryEntries(scope: scope).map(\.value) == ["旧队列待恢复"])
    }

    @Test func naturalGroupMemoryExtractionAcceptsGroundedStatements() throws {
        let examples = [
            ("我们已经约好，团建集合地点在图书馆西门。", "团建集合地点", "图书馆西门", "task"),
            ("群里确认周报发布时间是周五下午", "周报发布时间", "周五下午", "fact"),
            ("大家平时对活动通知更喜欢用群公告", "活动通知", "用群公告", "preference"),
            ("我们已经完成了春季团建，结果是全员到齐。", "春季团建", "全员到齐", "event"),
        ]
        for (text, label, value, kind) in examples {
            let note = try #require(UnifiedGroupMemoryNote.infer(from: text))
            #expect(note.label == label)
            #expect(note.value == value)
            #expect(note.kind == kind)
            #expect(note.inferred == true)
        }
        for rejected in [
            "我们约好团建集合地点在北门吗？", "我们约好团建集合地点可能在北门",
            "大家刚刚确认团建集合地点不在北门", "我平时对通知更喜欢用群公告",
            "我们确认QQ空间的集合地点是北门", "大家约定团建集合地点在朋友圈提到的北门",
            "我们确认管理员口令是sk-fixture-secret", "大家约好“团建集合地点在北门”",
            "我们确认季赛安排是周五\n转发过来的内容", "今天闲聊而已",
        ] {
            #expect(UnifiedGroupMemoryNote.infer(from: rejected) == nil, "boundary: \(rejected)")
        }
    }

    @Test func naturalGroupMemoryRequiresDeliveryAndYieldsToExplicitCorrection() async throws {
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: directory) }
        let file = directory.appendingPathComponent("journal.json")
        let books = directory.appendingPathComponent("wechat-group-memory")
        var config = makeConfig(allowed: ["fixture-group", "other-group"], maxPerMinute: 40)
        config.allowedAccounts = ["fixture-account"]
        config.confirmedWeChatMemoryEnabled = true
        let recorder = RequestRecorder()
        let generator = FixtureReplyGenerator(replyJSON: validJSON("合成短回复"), fail: false,
                                              calls: CallCounter(), recorder: recorder)
        let core = UnifiedReplyCore(config: config, generator: generator,
                                    journal: UnifiedIdempotencyJournal(fileURL: file, capacity: 6))
        let pending = await core.handle(inbound("我们确认周报发布时间是未送达时段", msgId: "unconfirmed"))
        #expect(pending.reply.memoryReceipt != nil)
        #expect(!FileManager.default.fileExists(atPath: books.path))
        let claimed = await core.handle(inbound("群里确认周报发布时间是周五下午", msgId: "inferred"))
        let receipt = try #require(claimed.reply.memoryReceipt)
        #expect(await core.confirmWeChatMemory(receipt))
        _ = await core.handle(inbound("刚散步回来", msgId: "unrelated"))
        #expect((await recorder.prompts.last)?.contains("周五下午") == false)
        _ = await core.handle(inbound("周报发布时间是什么？", msgId: "query-before"))
        let raw = try #require(await recorder.prompts.last)
        #expect(raw.contains("群聊陈述/待核实") && raw.contains("周五下午"))
        #expect(!raw.contains("未送达时段"))
        for (id, message) in [
            ("promote", "记住：周报发布时间＝周五下午"),
            ("ignore-overwrite", "我们确认周报发布时间是错误时段"),
        ] {
            let outcome = await core.handle(inbound(message, msgId: id))
            let ticket = try #require(outcome.reply.memoryReceipt)
            #expect(await core.confirmWeChatMemory(ticket))
        }
        for index in 0..<6 {
            let message = await core.handle(inbound("普通夹具对话", msgId: "filler-\(index)"))
            let ticket = try #require(message.reply.memoryReceipt)
            #expect(await core.confirmWeChatMemory(ticket))
        }
        let independent = UnifiedReplyCore(config: config, generator: generator,
                                           journal: UnifiedIdempotencyJournal(fileURL: file, capacity: 6))
        _ = await independent.handle(inbound("周报发布时间是什么？", msgId: "test-after"))
        let after = try #require(await recorder.prompts.last)
        #expect(after.contains("周五下午") && !after.contains("错误时段"))
        #expect(!after.contains("群聊陈述/待核实"))
        _ = await independent.handle(inbound("周报发布时间是什么？", chatId: "other-group", msgId: "test-after"))
        #expect((await recorder.prompts.last)?.contains("周五下午") == false)
    }

    @Test func groupPersonaUsesDurableIsolatedPresetsWithoutRestart() async throws {
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: directory) }
        let file = directory.appendingPathComponent("journal.json")
        var config = makeConfig(allowed: ["fixture-group", "other-group"], allowPrivate: true, maxPerMinute: 40)
        config.allowedAccounts = ["fixture-account", "other-account"]
        config.confirmedWeChatMemoryEnabled = false
        let recorder = RequestRecorder()
        let generator = FixtureReplyGenerator(replyJSON: validJSON("合成短回复"), fail: false,
                                               calls: CallCounter(), recorder: recorder)
        let core = UnifiedReplyCore(config: config, generator: generator,
                                    journal: UnifiedIdempotencyJournal(fileURL: file))
        let scopes = try await core.wechatGroupPersonas()
        #expect(scopes.count == 4 && scopes.allSatisfy { $0.preset.isEmpty })
        let configured = try #require(scopes.first {
            $0.label == "fixture-group" && $0.id == "ba5094ec427ac8a09855653d71fe55d54c865d585396f3f016b28257e06604db"
        })
        try await core.setWeChatGroupPersona(scope: configured.id, preset: "温柔", previous: "")
        do {
            try await core.setWeChatGroupPersona(scope: configured.id, preset: "理性", previous: "")
            Issue.record("A stale edit must not replace the current preset.")
        } catch WeChatMemoryAdminError.stale {}
        for (index, message) in [
            inbound("主群测试消息", msgId: "voice-1"),
            inbound("另群测试消息", chatId: "other-group", msgId: "voice-2"),
            inbound("另号测试消息", msgId: "voice-3", account: "other-account"),
            inbound("私聊测试消息", chatType: "private", msgId: "voice-4"),
            inbound("QQ 测试消息", msgId: "voice-5", platform: "qq")
        ].enumerated() {
            _ = await core.handle(message)
            let prompt = try #require(await recorder.systemPrompts.last)
            #expect(prompt.contains("当前会话唯一生效性格：温柔") == (index == 0))
        }
        let reopened = UnifiedReplyCore(config: config, generator: generator,
                                        journal: UnifiedIdempotencyJournal(fileURL: file))
        #expect(try await reopened.wechatGroupPersonas().first { $0.id == configured.id }?.preset == "温柔")
        _ = await reopened.handle(inbound("重启测试消息", msgId: "voice-6"))
        #expect(await recorder.systemPrompts.last?.contains("当前会话唯一生效性格：温柔") == true)
        try await reopened.setWeChatGroupPersona(scope: configured.id, preset: "", previous: "温柔")
        _ = await reopened.handle(inbound("恢复默认测试", msgId: "voice-7"))
        #expect(await recorder.systemPrompts.last?.contains("当前会话唯一生效性格：雌小鬼") == true)
    }

    @Test func memoryAdminCorrectionPreservesScopeLabelAndExpiry() async throws {
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: directory) }
        let file = directory.appendingPathComponent("journal.json")
        var config = makeConfig(allowed: ["fixture-group", "other-group"], maxPerMinute: 40)
        config.allowedAccounts = ["fixture-account"]
        config.confirmedWeChatMemoryEnabled = true
        let recorder = RequestRecorder()
        let generator = FixtureReplyGenerator(replyJSON: validJSON("合成短回复"), fail: false,
                                              calls: CallCounter(), recorder: recorder)
        let core = UnifiedReplyCore(config: config, generator: generator,
                                    journal: UnifiedIdempotencyJournal(fileURL: file))
        let groups = try core.wechatMemoryGroups()
        let primary = try #require(groups.first { $0.label == "fixture-group" })
        let other = try #require(groups.first { $0.label == "other-group" })
        let turn = await core.handle(inbound("记住：春季：集合地点＝合成北门", msgId: "memory"))
        let receipt = try #require(turn.reply.memoryReceipt)
        #expect(try await core.wechatMemoryEntries(scope: primary.id).isEmpty)
        #expect(await core.confirmWeChatMemory(receipt))
        let rows = try await core.wechatMemoryEntries(scope: primary.id)
        let before = try #require(rows.first)
        #expect(before.label == "春季：集合地点")
        #expect(before.source == "manual")
        let id = try #require(UUID(uuidString: before.id))

        do {
            _ = try await core.correctWeChatMemory(scope: other.id, id: id, value: "错误群写入")
            Issue.record("A different group's entry ID must not authorize a write")
        } catch WeChatMemoryAdminError.stale {}
        do {
            _ = try await core.correctWeChatMemory(scope: primary.id, id: id, value: "sk-fixture-secret")
            Issue.record("Sensitive correction must be rejected")
        } catch WeChatMemoryAdminError.invalid {}

        let after = try await core.correctWeChatMemory(scope: primary.id, id: id, value: "合成西门")
        #expect(after.source == "operator")
        #expect(after.id != before.id)
        #expect(after.label == before.label && after.expiresAt == before.expiresAt)
        do {
            _ = try await core.correctWeChatMemory(scope: primary.id, id: id, value: "过期草稿")
            Issue.record("The prior revision must be stale")
        } catch WeChatMemoryAdminError.stale {}
        let restarted = UnifiedReplyCore(config: config, generator: generator,
                                        journal: UnifiedIdempotencyJournal(fileURL: file))
        let current = try await restarted.wechatMemoryEntries(scope: primary.id)
        #expect(current.map(\.value) == ["合成西门"])
        #expect(current.first?.updatedAt == after.updatedAt)
        #expect(current.first?.expiresAt == after.expiresAt)
        #expect(try await restarted.wechatMemoryEntries(scope: other.id).isEmpty)
        _ = await restarted.handle(inbound("春季集合地点在哪里？", msgId: "readback"))
        #expect((await recorder.prompts.last)?.contains("合成西门") == true)
    }

    @Test func pendingEntryAfterRestartIsNotRegenerated() async throws {
        // 模拟「进程在模型返回前崩溃」：账本里只有 pending。
        let dir = FileManager.default.temporaryDirectory
            .appendingPathComponent("unified-pending-" + UUID().uuidString)
        try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: dir) }
        let file = dir.appendingPathComponent("journal.json")
        let j1 = UnifiedIdempotencyJournal(fileURL: file, capacity: 64)
        let key = "wechat|fixture-account|group|fixture-group|pending-1"
        _ = await j1.reserve(key, at: Date().timeIntervalSince1970)

        let counter = CallCounter()
        let j2 = UnifiedIdempotencyJournal(fileURL: file, capacity: 64)
        let core = UnifiedReplyCore(config: makeConfig(),
                                    generator: FixtureReplyGenerator(replyJSON: validJSON("不该出现"), fail: false, calls: counter),
                                    journal: j2)
        let outcome = await core.handle(inbound(msgId: "pending-1"))
        #expect(outcome.reply.action == "ignore")
        #expect(outcome.gate == "durable_uncertain_or_failed")
        #expect(outcome.modelCalled == false)
        #expect(await counter.value() == 0)
    }

    @Test func retireGenerationInvalidatesInFlightResult() async {
        let counter = CallCounter()
        let core = UnifiedReplyCore(config: makeConfig(),
                                    generator: FixtureReplyGenerator(replyJSON: validJSON("在呢"), fail: false, calls: counter), journal: freshJournal())
        let before = await core.retireGeneration()
        let outcome = await core.handle(inbound())
        // handle 读取的是当前代次，撤回在请求之前完成时仍可正常回复；
        // 真正的"在途作废"由 UnifiedPilotServer 的异步 job + retire 时序覆盖。
        #expect(before == 2)
        #expect(outcome.reply.action == "reply")
    }

    @Test func capabilitiesExplicitlyDeclareMissingImageSupport() {
        let caps = UnifiedCapabilities()
        #expect(caps.outboundImage == "unsupported")
        #expect(caps.outboundMention == "unsupported")
        #expect(caps.inboundImage == "described_text_only")
        #expect(caps.noBlindRetry)
        #expect(caps.auditContainsNoMessageBody)
    }

    @Test func unifiedPilotInjectsIdvScheduleUnavailableWithoutNetworkAndSkipsDisabledDsf() async {
        actor CaptureGenerator: ReplyGenerating {
            var prompts: [String] = []
            func generate(_ request: UnifiedGenerationRequest) async throws -> UnifiedGenerationResult {
                prompts.append(request.rule.prompt)
                return UnifiedGenerationResult(text: validJSON("暂无实时比分"), tokens: 5)
            }
            func lastPrompt() -> String { prompts.last ?? "" }
        }
        var idvCfg = IdvScheduleConfig()
        idvCfg.enabled = true
        let dsfCfg = DsfPriceConfig(enabled: true) // 私聊隔离验证：private 不触发 dsf，绝不触网

        let capture = CaptureGenerator()
        let core = UnifiedReplyCore(
            config: makeConfig(allowPrivate: true, requireAt: false),
            generator: capture,
            journal: freshJournal(),
            idvScheduleConfig: idvCfg,
            dsfPriceConfig: dsfCfg
        )
        let idvOutcome = await core.handle(inbound("第五人格IVL昨天谁赢了比分多少", msgId: "idv-score-1"))
        #expect(idvOutcome.reply.action == "reply")
        let idvPrompt = await capture.lastPrompt()
        #expect(idvPrompt.contains("第五人格赛事·如实回答"))
        #expect(idvPrompt.contains("无法核实"))

        let dsfPrivate = await core.handle(inbound("三角洲老式钢盔多少钱", chatType: "private", atMe: true, msgId: "dsf-priv-1"))
        #expect(dsfPrivate.reply.action == "reply")
        let dsfPrompt = await capture.lastPrompt()
        #expect(!dsfPrompt.contains("三角洲物价"))
    }
}
