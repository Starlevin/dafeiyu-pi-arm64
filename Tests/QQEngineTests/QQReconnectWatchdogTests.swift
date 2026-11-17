import Foundation
import Testing
@testable import WeChatAIBot
#if canImport(Glibc)
import Glibc
#endif

/// 重连守护策略的回归测试。
///
/// 背景：快速退避预算耗尽后如果直接躺平，一次超过预算时长的断网就会让引擎永久失去
/// 自动恢复能力——「扫码后大肥鱼不恢复自动回复」正是这个坑在长断网场景下的复现。
/// 这里锁定「预算耗尽 → 转入低频守护 → 继续探测并可自动接上」这条链路。
///
/// 说明：本套件会临时改写进程环境变量，因此标记 `.serialized` 且在用例结束时恢复原值。
@Suite(.serialized) struct QQReconnectWatchdogTests {

    /// 临时设置一组环境变量并在结束时恢复原值。
    ///
    /// 键的值为 nil 表示删除该变量。
    private func withEnvironment(_ values: [String: String?], _ body: () async throws -> Void) async rethrows {
        var previous: [String: String?] = [:]
        for key in values.keys { previous[key] = ProcessInfo.processInfo.environment[key] }
        defer {
            for (key, value) in previous {
                if let value { setenv(key, value, 1) } else { unsetenv(key) }
            }
        }
        for (key, value) in values {
            if let value { setenv(key, value, 1) } else { unsetenv(key) }
        }
        try await body()
    }

    /// 取一个当前无人监听、随后被释放的本机端口，用来稳定复现「连不上 OneBot」。
    private func closedLoopbackPort() throws -> Int {
        let process = Process(), output = Pipe()
        process.executableURL = URL(fileURLWithPath: "/usr/bin/python3")
        process.arguments = ["-c", "import socket;s=socket.socket();s.bind(('127.0.0.1',0));print(s.getsockname()[1],flush=True);s.close()"]
        process.standardOutput = output
        try process.run()
        let data = output.fileHandleForReading.readDataToEndOfFile()
        process.waitUntilExit()
        let text = String(decoding: data, as: UTF8.self).trimmingCharacters(in: .whitespacesAndNewlines)
        return try #require(Int(text), "无法取得空闲端口：\(text)")
    }

    /// 守护间隔的默认值与可配置性。
    @Test @MainActor func watchdogIntervalDefaultsToTenMinutesAndIsConfigurable() async throws {
        await withEnvironment(["DAFEIYU_RECONNECT_WATCHDOG_INTERVAL": nil]) {
            #expect(QQEngine.reconnectWatchdogInterval == 600)
        }
        await withEnvironment(["DAFEIYU_RECONNECT_WATCHDOG_INTERVAL": "45"]) {
            #expect(QQEngine.reconnectWatchdogInterval == 45)
        }
        // 0 表示显式关闭守护，退回纯有限退避语义。
        await withEnvironment(["DAFEIYU_RECONNECT_WATCHDOG_INTERVAL": "0"]) {
            #expect(QQEngine.reconnectWatchdogInterval == 0)
        }
        // 非法值退回默认，而不是把守护关掉。
        await withEnvironment(["DAFEIYU_RECONNECT_WATCHDOG_INTERVAL": "abc"]) {
            #expect(QQEngine.reconnectWatchdogInterval == 600)
        }
    }

    /// 快速退避预算耗尽后必须转入低频守护并继续探测，而不是彻底停止重连。
    @Test @MainActor func exhaustedBackoffFallsBackToWatchdogAndKeepsProbing() async throws {
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: directory) }

        let port = try closedLoopbackPort()
        try await withEnvironment([
            "DAFEIYU_AUTO_RESUME": "1",
            "DAFEIYU_RECONNECT_MAX_ATTEMPTS": "1",
            "DAFEIYU_RECONNECT_MAX_DELAY": "5",
            "DAFEIYU_RECONNECT_WATCHDOG_INTERVAL": "2"
        ]) {
            let engine = QQEngine(allowAuthenticationUI: false, storageDirectory: directory)
            defer { engine.disconnect() }
            engine.save(endpoint: "ws://127.0.0.1:\(port)", expectedSelfID: "12345")
            #expect(engine.useTemporaryCredentials(token: "synthetic-test-token", key: "synthetic-key"))

            // 等待：首次连接失败 → 快速退避（1 次、5 秒）→ 预算耗尽 → 转入守护 → 守护开始探测。
            var sawWatchdog = false
            for _ in 0..<60 {
                try await Task.sleep(nanoseconds: 500_000_000)
                if engine.status.contains("低频守护第") { sawWatchdog = true; break }
            }
            #expect(sawWatchdog, "快速退避预算耗尽后应转入低频守护并继续探测，实际状态：\(engine.status)")
            #expect(!engine.connected)
            // 关键：不能是「已停止自动重连」——那意味着长断网后永远躺平。
            #expect(!engine.status.contains("已停止自动重连"), "预算耗尽后不应彻底停止重连，实际状态：\(engine.status)")
        }
    }

    /// 显式关闭守护时，保持原版的「有限退避、到上限即停」语义。
    @Test @MainActor func watchdogCanBeDisabledToKeepStrictFiniteBackoff() async throws {
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: directory) }

        let port = try closedLoopbackPort()
        try await withEnvironment([
            "DAFEIYU_AUTO_RESUME": "1",
            "DAFEIYU_RECONNECT_MAX_ATTEMPTS": "1",
            "DAFEIYU_RECONNECT_MAX_DELAY": "5",
            "DAFEIYU_RECONNECT_WATCHDOG_INTERVAL": "0"
        ]) {
            let engine = QQEngine(allowAuthenticationUI: false, storageDirectory: directory)
            defer { engine.disconnect() }
            engine.save(endpoint: "ws://127.0.0.1:\(port)", expectedSelfID: "12345")
            #expect(engine.useTemporaryCredentials(token: "synthetic-test-token", key: "synthetic-key"))

            var stopped = false
            for _ in 0..<60 {
                try await Task.sleep(nanoseconds: 500_000_000)
                if engine.status.contains("已停止自动重连") { stopped = true; break }
            }
            #expect(stopped, "关闭守护后应按有限退避语义停止重连，实际状态：\(engine.status)")
            #expect(!engine.status.contains("低频守护"))
        }
    }
}
