#if os(Linux)
import Foundation
import FoundationNetworking
import Glibc
import Testing
import BotCore
@testable import WeChatAIBot

/// 真实 loopback 契约端点（127.0.0.1:18090/v1/chat/completions）的端到端门禁：
/// 用真实 socket 验证 Swift 客户端确实把合法 trace 头发到 model-hub 入口、同一轮
/// 真实重试复用同一值、以及中间 302 不会把 trace 头带到第二个目标。
///
/// 不复用生产 model-hub/配置/凭据，只在本测试进程内起一次性 Python fixture；
/// 断言只比较 trace 值与计数，不打印 trace、正文或凭证。
@Suite(.serialized) struct QQTraceHeaderLoopbackTests {
    private static let trace = UUID(uuidString: "AAAAAAAA-BBBB-CCCC-DDDD-EEEEEEEEEEEE")!
    private static let expectedTraceValue = trace.uuidString.lowercased()

    private func makeClient(retries: Int) -> DeepSeekClient {
        var client = DeepSeekClient()
        client.provider = LLMProvider(
            kind: .custom,
            baseURL: URL(string: "http://127.0.0.1:18090/v1")!,
            modelOverride: "synthetic",
            requestTimeout: 10,
            maxRetries: retries)
        client.session = URLSession(configuration: .ephemeral)
        return client
    }

    private func sendTrace(_ client: DeepSeekClient) async throws -> ModelResult {
        try await client.reply(
            key: "synthetic-key",
            config: BotConfig(),
            rule: ChatRule(name: "trace-gate"),
            history: [],
            text: "synthetic loopback trace gate",
            traceID: Self.trace,
            reserve: {})
    }

    private func captureLines(_ path: String) -> [String] {
        (try? String(contentsOfFile: path, encoding: .utf8))?
            .split(separator: "\n")
            .map(String.init) ?? []
    }

    private func withFixture(mode: String, _ body: (String, String) async throws -> Void) async throws {
        let directory = FileManager.default.temporaryDirectory
            .appendingPathComponent("qq-trace-gate-\(UUID().uuidString)", isDirectory: true)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: directory) }

        let firstCapture = directory.appendingPathComponent("first.log").path
        let secondCapture = directory.appendingPathComponent("second.log").path
        let readyFile = directory.appendingPathComponent("ready").path
        let server = Process()
        server.executableURL = URL(fileURLWithPath: "/usr/bin/python3")
        server.arguments = ["-u", "-c", Self.fixture, mode, firstCapture, secondCapture, readyFile,
                            String(ProcessInfo.processInfo.processIdentifier)]
        server.standardOutput = FileHandle.nullDevice
        server.standardError = FileHandle.nullDevice
        try server.run()
        defer {
            if server.isRunning {
                _ = Glibc.kill(server.processIdentifier, SIGKILL)
                server.waitUntilExit()
            }
        }

        for _ in 0..<120 {
            if FileManager.default.fileExists(atPath: readyFile) || !server.isRunning { break }
            try await Task.sleep(nanoseconds: 50_000_000)
        }
        try #require(FileManager.default.fileExists(atPath: readyFile),
                     "loopback fixture 未就绪，请检查独占端口 18090")
        try await body(firstCapture, secondCapture)
    }

    @Test func trustedLoopbackKeepsTraceAcrossRealRetry() async throws {
        try await withFixture(mode: "retry") { firstCapture, secondCapture in
            let result = try await sendTrace(makeClient(retries: 1))
            #expect(result.text == "ok")

            let lines = captureLines(firstCapture)
            #expect(lines.count == 2, "500 后应发生一次真实重试")
            #expect(lines.allSatisfy { $0 == Self.expectedTraceValue })
            #expect(!FileManager.default.fileExists(atPath: secondCapture))
        }
    }

    @Test func redirectNeverDeliversTraceToSecondTarget() async throws {
        try await withFixture(mode: "redirect") { firstCapture, secondCapture in
            do {
                _ = try await sendTrace(makeClient(retries: 0))
                Issue.record("302 不应被当作成功响应")
            } catch {
                #expect((error as? HTTPFailure)?.code == 302)
            }

            let firstLines = captureLines(firstCapture)
            #expect(firstLines.count == 1)
            #expect(firstLines.first == Self.expectedTraceValue)
            #expect(captureLines(secondCapture).isEmpty)
        }
    }

    private static let fixture = #"""
    import http.server
    import os
    from pathlib import Path
    import sys
    import threading
    import time

    mode, first_capture, second_capture, ready_file, parent_pid = sys.argv[1:6]
    parent_pid = int(parent_pid)

    class First(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):
            pass

        def do_POST(self):
            length = int(self.headers.get("Content-Length", "0") or "0")
            self.rfile.read(length)
            with open(first_capture, "a", encoding="utf-8") as handle:
                handle.write((self.headers.get("X-Dafeiyu-Trace-Id") or "") + "\n")
            attempts = sum(1 for _ in open(first_capture, encoding="utf-8"))
            if mode == "retry" and attempts == 1:
                self.send_response(500)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if mode == "redirect":
                self.send_response(302)
                self.send_header("Location", f"http://127.0.0.1:{second.server_port}/v1/chat/completions")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            payload = b'{"choices":[{"message":{"content":"ok"},"finish_reason":"stop"}],"usage":{"total_tokens":1}}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    class Second(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):
            pass

        def do_POST(self):
            length = int(self.headers.get("Content-Length", "0") or "0")
            self.rfile.read(length)
            with open(second_capture, "a", encoding="utf-8") as handle:
                handle.write((self.headers.get("X-Dafeiyu-Trace-Id") or "") + "\n")
            self.send_response(200)
            self.send_header("Content-Length", "0")
            self.end_headers()

    http.server.ThreadingHTTPServer.allow_reuse_address = True
    with http.server.ThreadingHTTPServer(("127.0.0.1", 18090), First) as first:
        second = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Second) if mode == "redirect" else None
        if second is not None:
            threading.Thread(target=second.serve_forever, daemon=True).start()
        Path(ready_file).touch(exist_ok=False)
        first.timeout = 0.2
        deadline = time.monotonic() + 45
        # Test-runner crashes must not leave the next run sharing a stale endpoint.
        while os.getppid() == parent_pid and time.monotonic() < deadline:
            first.handle_request()
        if second is not None:
            second.shutdown()
            second.server_close()
    """#
}
#endif
