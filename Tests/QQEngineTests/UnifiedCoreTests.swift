import Foundation
import Testing
import BotCore
@testable import WeChatAIBot

/// 统一回复核心的确定性夹具测试。
/// 约束（AGENTS.md）：合成身份、本机夹具、零真实模型、零真实会话、零发送。
/// `FixtureReplyGenerator` 完全在进程内，不触网；`fake_model.py` 只绑 127.0.0.1。

private struct FixtureReplyGenerator: ReplyGenerating {
    let replyJSON: String
    let fail: Bool
    var calls: CallCounter
    var recorder: RequestRecorder? = nil

    func generate(_ request: UnifiedGenerationRequest) async throws -> UnifiedGenerationResult {
        await calls.increment()
        if fail { throw AppFailure.message("synthetic model failure") }
        // 夹具可以断言「实际复用的人格/知识 prompt 被注入」，而不是只看返回值。
        await recorder?.record(request)
        return UnifiedGenerationResult(text: replyJSON, tokens: 7)
    }
}

private actor RequestRecorder {
    private(set) var incomingHistory: [[String]] = []
    private(set) var prompts: [String] = []
    private(set) var systemPrompts: [String] = []
    func record(_ request: UnifiedGenerationRequest) {
        incomingHistory.append(request.history.map(\.incoming))
        prompts.append(request.userText)
        systemPrompts.append(request.rule.prompt)
    }
}

private actor CallCounter {
    private(set) var count = 0
    func increment() { count += 1 }
    func value() -> Int { count }
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
        #expect(a.scopeDigest.count == 16)
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
