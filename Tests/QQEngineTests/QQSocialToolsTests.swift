import Foundation
#if canImport(FoundationNetworking)
import FoundationNetworking
#endif
import Testing
import BotCore
@testable import WeChatAIBot

private final class SocialModelFixtureProtocol: URLProtocol, @unchecked Sendable {
    private static let lock = NSLock()
    nonisolated(unsafe) private static var requests = 0
    static func reset() { lock.lock(); requests = 0; lock.unlock() }
    static var count: Int { lock.lock(); defer { lock.unlock() }; return requests }
    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }
    override func stopLoading() {}
    override func startLoading() {
        Self.lock.lock(); Self.requests += 1; let index = Self.requests; Self.lock.unlock()
        let result: [String: Any]
        if index == 1 {
            var payload = request.httpBody ?? Data()
            if payload.isEmpty, let stream = request.httpBodyStream {
                stream.open()
                defer { stream.close() }
                var buffer = [UInt8](repeating: 0, count: 8192)
                while stream.hasBytesAvailable {
                    let count = stream.read(&buffer, maxLength: buffer.count)
                    if count <= 0 { break }
                    payload.append(contentsOf: buffer.prefix(count))
                }
            }
            let body = (try? JSONSerialization.jsonObject(with: payload)) as? [String: Any] ?? [:]
            let tools = body["tools"] as? [[String: Any]] ?? []
            #expect(tools.contains { ($0["function"] as? [String: Any])?["name"] as? String == "read_social" })
            result = ["choices": [["message": ["content": NSNull(), "tool_calls": [["id": "fixture", "type": "function", "function": ["name": "read_social", "arguments": #"{"platform":"bilibili","mode":"popular","value":"","limit":2}"#]]]], "finish_reason": "tool_calls"]]]
        } else {
            result = ["choices": [["message": ["content": #"{"text":"这是公开标题摘要。","emotion":"neutral","intensity":0}"#], "finish_reason": "stop"]]]
        }
        let data = try! JSONSerialization.data(withJSONObject: result)
        let response = HTTPURLResponse(url: request.url!, statusCode: 200, httpVersion: "HTTP/1.1", headerFields: ["Content-Type": "application/json"])!
        client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
        client?.urlProtocol(self, didLoad: data)
        client?.urlProtocolDidFinishLoading(self)
    }
}

private actor SocialFixtureCounter {
    private var requests: [String] = []
    func record(_ value: String) { requests.append(value) }
    func snapshot() -> [String] { requests }
}

private struct SocialFixtureGenerator: ReplyGenerating {
    let counter: SocialFixtureCounter
    func generate(_ request: UnifiedGenerationRequest) async throws -> UnifiedGenerationResult {
        await counter.record(request.currentUserText)
        return UnifiedGenerationResult(text: #"{"text":"这是公开标题。","emotion":"neutral","intensity":0}"#,
            tokens: 1, sources: ["https://www.bilibili.com/video/BV17x411w7KC"])
    }
}

struct QQSocialToolsTests {
    @Test func explicitCurrentTurnAndValueAreRequired() {
        let popular = #"{"platform":"bilibili","mode":"popular","value":"","limit":2}"#
        #expect(QQSocialTools.validated(arguments: popular, userText: "看看 B站热门") != nil)
        #expect(QQSocialTools.validated(arguments: popular, userText: "你喜欢 B站吗") == nil)
        #expect(QQSocialTools.validated(arguments: popular, userText: "看看抖音热门") == nil)
        let search = #"{"platform":"bilibili","mode":"search","value":"Minecraft","limit":3}"#
        #expect(QQSocialTools.validated(arguments: search, userText: "B站搜 Minecraft") != nil)
        #expect(QQSocialTools.validated(arguments: search, userText: "B站搜游戏") == nil)
        #expect(QQSocialTools.validated(arguments: #"{"platform":"x","mode":"user","value":"NASA","limit":4}"#, userText: "查看 X 的 NASA 动态") == nil)
        #expect(QQSocialTools.validated(arguments: #"{"platform":"x","mode":"user","value":"NASA","limit":1,"url":"http://127.0.0.1"}"#, userText: "查看 X 的 NASA 动态") == nil)
    }

    @Test func onlyCanonicalPlatformLinksBecomeSources() {
        #expect(QQSocialTools.approvedSource("https://www.bilibili.com/video/BV17x411w7KC?secret=no", platform: "bilibili") == "https://www.bilibili.com/video/BV17x411w7KC")
        #expect(QQSocialTools.approvedSource("https://x.com/NASA/status/2121280846183137564", platform: "x") != nil)
        #expect(QQSocialTools.approvedSource("https://x.com.evil.test/NASA/status/2121280846183137564", platform: "x") == nil)
        #expect(QQSocialTools.approvedSource("http://127.0.0.1/admin", platform: "douyin") == nil)
        #expect(QQSocialTools.approvedSource("https://x.com/NASA/status/2121280846183137564", platform: "douyin") == nil)
    }

    @Test func loopbackReaderAndSourceAttribution() async throws {
        let program = #"""
import http.server, json, signal
signal.signal(signal.SIGTERM, signal.SIG_DFL)
signal.pthread_sigmask(signal.SIG_UNBLOCK, [signal.SIGTERM])
class Handler(http.server.BaseHTTPRequestHandler):
 def log_message(self,*args): pass
 def do_POST(self):
  body=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
  ok=self.path=='/v1/scan' and self.headers.get('X-Dafeiyu-Social-Token')=='fixture-'+'x'*48
  urls={'bilibili':'https://www.bilibili.com/video/BV17x411w7KC?track=1','douyin':'https://www.douyin.com/search/example','x':'https://x.com/NASA/status/2121280846183137564'}
  item={'platform':body.get('platform'),'title':'合成标题','excerpt':'合成摘要','url':urls.get(body.get('platform'),'')}
  raw=json.dumps({'ok':ok,'items':[item]}).encode()
  self.send_response(200 if ok else 403); self.send_header('Content-Length',str(len(raw))); self.end_headers(); self.wfile.write(raw)
server=http.server.HTTPServer(('127.0.0.1',0),Handler)
print(server.server_port,flush=True)
server.serve_forever()
"""#
        let process = Process(), output = Pipe()
        process.executableURL = URL(fileURLWithPath: "/usr/bin/env")
        process.arguments = ["python3", "-u", "-c", program]
        process.standardOutput = output
        process.standardError = FileHandle.nullDevice
        try process.run()
        defer { if process.isRunning { process.terminate(); process.waitUntilExit() } }
        let portText = String(decoding: output.fileHandleForReading.availableData, as: UTF8.self).trimmingCharacters(in: .whitespacesAndNewlines)
        let port = try #require(Int(portText))
        let token = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        defer { try? FileManager.default.removeItem(at: token) }
        try ("fixture-" + String(repeating: "x", count: 48)).write(to: token, atomically: true, encoding: .utf8)
        try FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: token.path)
        let tool = QQSocialTools(tokenURL: token, port: port)
        let cases = [
            (#"{"platform":"bilibili","mode":"popular","value":"","limit":2}"#, "看看B站热门", "www.bilibili.com"),
            (#"{"platform":"douyin","mode":"hot","value":"","limit":2}"#, "看看抖音热搜", "www.douyin.com"),
            (#"{"platform":"x","mode":"user","value":"NASA","limit":2}"#, "查一下 X 的 NASA 动态", "x.com")
        ]
        for (arguments, question, host) in cases {
            let result = try await tool.execute(arguments: arguments, userText: question)
            #expect(result.sources.count == 1)
            #expect(URL(string: result.sources.first ?? "")?.host == host)
            #expect(!(result.sources.first ?? "").contains("track="))
            #expect(result.content.contains("合成标题"))
            #expect(result.content.contains("低信任"))
        }
        let rejected = try await tool.execute(arguments: cases[0].0, userText: "闲聊")
        #expect(rejected.sources.isEmpty)
        #expect(rejected.content.contains("未联网"))
    }

    @Test @MainActor func modelCanCallSocialToolAndKeepVerifiedSources() async throws {
        SocialModelFixtureProtocol.reset()
        let settings = URLSessionConfiguration.ephemeral
        settings.protocolClasses = [SocialModelFixtureProtocol.self]
        let session = URLSession(configuration: settings)
        defer { session.invalidateAndCancel() }
        var model = DeepSeekClient(session: session)
        model.provider.replyMode = .fast
        var calls = 0
        let result = try await model.reviewedQQReply(key: "fixture-key", config: QQConfig().ai,
            rule: ChatRule(name: "fixture"), history: [], text: "看看B站热门",
            toolHandler: { name, arguments in
                calls += 1
                #expect(name == "read_social")
                #expect(QQSocialTools.validated(arguments: arguments, userText: "看看B站热门") != nil)
                return ModelToolResult(content: "低信任的合成标题", sources: ["https://www.bilibili.com/video/BV17x411w7KC"])
            }, onlineToolsEnabled: false, socialToolsEnabled: true,
            userText: "看看B站热门", toolsMayRun: true, reserve: {})
        #expect(calls == 1)
        #expect(result.usedTools)
        #expect(result.sources == ["https://www.bilibili.com/video/BV17x411w7KC"])
        #expect(SocialModelFixtureProtocol.count == 3)
    }

    @Test func wechatGatesAndIdempotentReplyKeepSources() async {
        var config = UnifiedCoreConfig()
        config.enabled = true
        config.allowedChats = ["fixture-group"]
        config.cooldownSeconds = 0
        let counter = SocialFixtureCounter()
        let core = UnifiedReplyCore(config: config, generator: SocialFixtureGenerator(counter: counter),
                                    journal: UnifiedIdempotencyJournal(fileURL: nil, capacity: 64))
        func message(_ group: String, _ at: Bool, _ id: String) -> UnifiedInbound {
            UnifiedInbound(platform: "wechat", account: "fixture-account", chatType: "group", chatId: group,
                           chatTitle: group, senderName: "fixture", senderId: "fixture-sender", text: "看看B站热门",
                           atMe: at, msgId: id)
        }
        let noAt = await core.handle(message("fixture-group", false, "no-at"))
        let other = await core.handle(message("other-group", true, "other"))
        #expect(noAt.reply.action == "ignore")
        #expect(other.reply.action == "ignore")
        #expect(await counter.snapshot().isEmpty)
        let accepted = await core.handle(message("fixture-group", true, "approved"))
        let replay = await core.handle(message("fixture-group", true, "approved"))
        #expect(accepted.reply.action == "reply")
        #expect(accepted.reply.reply?.contains("https://www.bilibili.com/video/BV17x411w7KC") == true)
        #expect(replay.gate == "durable_replay")
        #expect(await counter.snapshot() == ["看看B站热门"])
    }

    @Test func freshnessMetadataIncludedAndStaleCacheRejected() async throws {
        let program = #"""
import http.server, json, signal
signal.signal(signal.SIGTERM, signal.SIG_DFL)
signal.pthread_sigmask(signal.SIG_UNBLOCK, [signal.SIGTERM])
class Handler(http.server.BaseHTTPRequestHandler):
 def log_message(self,*args): pass
 def do_POST(self):
  body=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
  ok=self.path=='/v1/scan' and self.headers.get('X-Dafeiyu-Social-Token')=='fixture-'+'x'*48
  items=[
   {'platform':'bilibili','title':'新鲜条','excerpt':'实时摘要','url':'https://www.bilibili.com/video/BV17x411w7KC','published':'2026-11-15 14:30:00','fetched_at':'2026-11-15 15:00:00 +08:00','age_sec':15},
   {'platform':'bilibili','title':'陈旧条','excerpt':'过期摘要','url':'https://www.bilibili.com/video/BV1xx411c7mD','published':'2026-11-14 08:00:00','fetched_at':'2026-11-14 08:05:00 +08:00','age_sec':7200},
  ]
  if body.get('mode')=='search': items=[items[1]]
  raw=json.dumps({'ok':ok,'items':items}).encode()
  self.send_response(200 if ok else 403); self.send_header('Content-Length',str(len(raw))); self.end_headers(); self.wfile.write(raw)
server=http.server.HTTPServer(('127.0.0.1',0),Handler)
print(server.server_port,flush=True)
server.serve_forever()
"""#
        let process = Process(), output = Pipe()
        process.executableURL = URL(fileURLWithPath: "/usr/bin/env")
        process.arguments = ["python3", "-u", "-c", program]
        process.standardOutput = output
        process.standardError = FileHandle.nullDevice
        try process.run()
        defer { if process.isRunning { process.terminate(); process.waitUntilExit() } }
        let port = try #require(Int(String(decoding: output.fileHandleForReading.availableData, as: UTF8.self).trimmingCharacters(in: .whitespacesAndNewlines)))
        let token = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        defer { try? FileManager.default.removeItem(at: token) }
        try ("fixture-" + String(repeating: "x", count: 48)).write(to: token, atomically: true, encoding: .utf8)
        try FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: token.path)
        let tool = QQSocialTools(tokenURL: token, port: port, maxAgeSeconds: 3600)
        let mixed = try await tool.execute(arguments: #"{"platform":"bilibili","mode":"popular","value":"","limit":2}"#, userText: "看看B站热门")
        #expect(mixed.sources == ["https://www.bilibili.com/video/BV17x411w7KC"])
        #expect(mixed.content.contains("新鲜条"))
        #expect(!mixed.content.contains("陈旧条"))
        #expect(mixed.content.contains("2026-11-15 14:30:00"))
        #expect(mixed.content.contains("2026-11-15 15:00:00 +08:00"))
        #expect(mixed.content.contains("缓存秒数"))
        #expect(mixed.content.contains("发布或抓取时间"))
        let onlyStale = try await tool.execute(arguments: #"{"platform":"bilibili","mode":"search","value":"旧闻","limit":2}"#, userText: "搜索B站旧闻")
        #expect(onlyStale.sources.isEmpty)
        #expect(onlyStale.content.contains("过期缓存"))
    }
}
