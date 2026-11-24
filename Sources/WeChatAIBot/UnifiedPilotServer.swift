import Foundation
#if canImport(FoundationNetworking)
import FoundationNetworking
#endif
import BotCore

/// 独立本机监听（试点专用），**与 QQControlServer 完全分离**：
/// - 复用 `LoopbackListener` / `LoopbackConnection` 的既有安全边界：仅 IPv4 127.0.0.1、
///   随机端口、32 连接上限、token 放在 URL fragment 并由请求头携带、Host/Origin 校验。
/// - **不复制 QQControlServer.swift:41 的 15 秒连接生命周期定时器**：长模型请求在
///   独立连接上完成，不会占用 QQ 控制面板的 MainActor `handle`，因此 `/api/status`
///   健康轮询与 15s 新鲜度门禁不受影响。
/// - 认证沿用"安全本地方式"：进程内随机 token + 0600 运行时文件；**不读取任何登录
///   凭证、OneBot token、钥匙串或模型 Key 文件**（模型 Key 由 `LLMSecret` 自己按既有
///   优先级解析，试点不扩大权限）。
/// - 同时提供异步 job 语义：`POST /api/job` 立刻返回 job_id，`GET /api/job/<id>` 取结果；
///   `POST /webhook/wechat` 保持旧 core 的同步响应形状，`pi_bridge` 无需改动。

private struct PilotRuntime: Codable {
    let port: Int
    let token: String
    let controlURL: String
}

private struct PilotMemoryAdminRuntime: Encodable {
    let port: Int
    let token: String
}

private struct PilotMemoryResult: Encodable {
    let ok = true
    var groups: [WeChatMemoryGroup]? = nil
    var entries: [WeChatMemoryEntry]? = nil
    var entry: WeChatMemoryEntry? = nil
}

private struct PilotPersonaList: Encodable {
    let ok = true
    let styles: [String]
    let groups: [WeChatGroupPersona]
}

private struct PilotPersonaChange: Decodable {
    let preset: String
    let previous: String
}

/// 所有控制响应的统一形状（不含消息原文）。可选字段让一个结构体覆盖所有路径。
private struct PilotAck: Encodable {
    var ok: Bool
    var error: String? = nil
    var jobId: String? = nil
    var state: String? = nil
    var generation: UInt64? = nil
    var entries: [UnifiedAuditEntry]? = nil

    private enum CodingKeys: String, CodingKey {
        case ok, error, state, generation, entries
        case jobId = "job_id"
    }
}

/// 单个异步 job 的公开状态（不含消息原文）。
private struct JobStatus: Encodable {
    let state: String            // pending | done | failed
    let result: UnifiedReply?
}

/// HTTP/1.1 请求头解析结果（**不含 body**）。
///
/// 与旧实现的本质区别：头部与 body 允许分任意多次 TCP 分段到达。
/// 这里只解析 `\r\n\r\n` 之前的头部，算出 Content-Length；body 由
/// `UnifiedPilotServer.receive` 继续累加到完整后再交付，绝不在头到达的那一刻
/// 就要求 body 已经齐（Pi 3B 复现的 HTTP 400 根因）。
private struct PilotHead {
    let method: String
    let path: String
    let headers: [String: String]
    let contentLength: Int
    let bodyStart: Int
}

private enum PilotHTTP {
    /// 头部上限：超过即拒绝，避免无界缓冲。
    static let headerLimit = 16384
    /// 单请求 body 上限。
    static let bodyLimit = 65536
    /// 整个连接累计缓冲上限（含头部与 body）。
    static let bufferLimit = 4_220_000

    /// 解析头部；返回 nil 表示「格式错误，应尽早 400」。
    /// 只检查 Content-Length 的合法性，不检查 body 是否到达。
    static func parseHead(_ data: Data) -> PilotHead? {
        guard let sep = data.range(of: Data("\r\n\r\n".utf8)) else { return nil }
        let head = String(decoding: data[..<sep.lowerBound], as: UTF8.self)
        var lines = head.components(separatedBy: "\r\n")
        let request = (lines.removeFirst()).split(separator: " ").map(String.init)
        guard request.count == 3, request[2] == "HTTP/1.1" else { return nil }
        var headers: [String: String] = [:]
        for line in lines {
            guard let colon = line.firstIndex(of: ":") else { return nil }
            let name = line[..<colon].lowercased()
            guard headers[name] == nil else { return nil }
            headers[name] = String(line[line.index(after: colon)...]).trimmingCharacters(in: .whitespaces)
        }
        // 不支持分块传输；有该头一律拒绝，避免长度不确定。
        guard headers["transfer-encoding"] == nil else { return nil }
        let length: Int
        if let raw = headers["content-length"] {
            guard let value = Int(raw), (0...bodyLimit).contains(value) else { return nil }
            length = value
        } else {
            length = 0
        }
        return PilotHead(method: request[0], path: request[1], headers: headers,
                         contentLength: length, bodyStart: sep.upperBound)
    }

    static func response(_ code: Int, _ object: some Encodable) -> Data {
        let encoder = JSONEncoder()
        encoder.outputFormatting = [.sortedKeys]
        let data = (try? encoder.encode(object)) ?? Data("{}".utf8)
        let status = [200: "OK", 202: "Accepted", 403: "Forbidden", 404: "Not Found",
                      409: "Conflict", 503: "Service Unavailable"][code] ?? "Bad Request"
        let header = "HTTP/1.1 \(code) \(status)\r\nContent-Type: application/json; charset=utf-8\r\nContent-Length: \(data.count)\r\nConnection: close\r\nCache-Control: no-store\r\nX-Content-Type-Options: nosniff\r\n\r\n"
        return Data(header.utf8) + data
    }
}

@MainActor
public final class UnifiedPilotServer {
    private let listener: LoopbackListener
    private let token: String
    private let memoryAdminToken = UUID().uuidString + UUID().uuidString
    private let origin: String
    private let core: UnifiedReplyCore
    private let runtimeFile: URL?
    private var memoryAdminRuntimeFile: URL? {
        runtimeFile?.deletingPathExtension().appendingPathExtension("memory-admin.json")
    }
    private var jobs: [String: JobStatus] = [:]
    private var started = false

    public init(core: UnifiedReplyCore, runtimeFile: URL? = nil) throws {
        self.core = core
        self.listener = try LoopbackListener()
        self.token = UUID().uuidString + UUID().uuidString
        self.origin = "http://127.0.0.1:\(listener.port)"
        self.runtimeFile = runtimeFile ?? Self.defaultRuntimeFile()
    }

    private static func defaultRuntimeFile() -> URL? {
        let env = ProcessInfo.processInfo.environment
        if let dir = env["DAFEIYU_DATA_DIR"], !dir.isEmpty {
            return URL(fileURLWithPath: dir, isDirectory: true).appendingPathComponent("unified-pilot.json")
        }
        // 不读取任何登录凭证；仅写自己进程产生的运行时指针。
        return FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask).first?
            .appendingPathComponent("WeChatAIBot", isDirectory: true)
            .appendingPathComponent("unified-pilot.json")
    }

    public var controlURL: String { "\(origin)/#\(token)" }

    public func start(announce: Bool = true) {
        guard !started else { return }
        started = true
        writeRuntime()
        if let file = memoryAdminRuntimeFile {
            do {
                let metadata = PilotMemoryAdminRuntime(port: Int(listener.port), token: memoryAdminToken)
                try UnifiedIdempotencyJournal.writeAtomically(JSONEncoder().encode(metadata), to: file)
            } catch {
                print("unified_memory_admin_runtime_write_failed")
            }
        }
        if announce {
            print("UNIFIED_PILOT_URL=\(controlURL)")
            print("统一回复试点本机监听已就绪；独立端口，不影响 QQ 控制面板 /api/status。")
        }
        listener.start { [weak self] connection in
            guard let self else { connection.cancel(); return }
            self.receive(connection, buffer: Data())
        }
    }

    private func writeRuntime() {
        guard let runtimeFile else { return }
        let payload = PilotRuntime(port: Int(listener.port), token: token, controlURL: controlURL)
        guard let data = try? JSONEncoder().encode(payload) else { return }
        do {
            try FileManager.default.createDirectory(at: runtimeFile.deletingLastPathComponent(),
                                                    withIntermediateDirectories: true)
            try data.write(to: runtimeFile, options: .atomic)
            try FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: runtimeFile.path)
        } catch {
            print("unified_pilot_runtime_write_failed type=\(type(of: error))")
        }
    }

    public func stop() {
        listener.stop()
        if let runtimeFile { try? FileManager.default.removeItem(at: runtimeFile) }
        if let file = memoryAdminRuntimeFile { try? FileManager.default.removeItem(at: file) }
    }

    // MARK: - 连接处理（nonisolated：不占用 MainActor）
    //
    // 有界增量解析：头与 body 允许任意 TCP 分段到达。
    // 状态机：
    //   1. 累加字节直到出现 \r\n\r\n（头部上限 headerLimit，超限/EOF 尽早 400）
    //   2. 只解析一次头部，得到 Content-Length 与 body 起始偏移
    //   3. 继续 read 直到 body 达到 Content-Length（EOF 未齐 / 超限尽早 400）
    //   4. 才把完整请求交给 handle；绝不在头到达瞬间要求 body 已齐。
    private nonisolated func receive(_ connection: LoopbackConnection, buffer: Data) {
        connection.read { [weak self] data, ended, error in
            guard let self else { return }
            var buf = buffer
            if let data { buf.append(data) }
            guard error == nil, buf.count <= PilotHTTP.bufferLimit else {
                self.reject(connection, "bad_request"); return
            }

            let separator = buf.range(of: Data("\r\n\r\n".utf8))
            if separator == nil {
                // 头还没到齐：EOF 或头部超限都是格式错误，尽早拒绝。
                if ended || buf.count > PilotHTTP.headerLimit {
                    self.reject(connection, "incomplete_headers")
                } else {
                    self.receive(connection, buffer: buf)
                }
                return
            }

            guard let head = PilotHTTP.parseHead(buf) else {
                self.reject(connection, "bad_request"); return
            }
            let bodyStart = head.bodyStart
            let needed = bodyStart + head.contentLength
            if buf.count < needed {
                // body 还没到齐：EOF 提前到达 = 截断，尽早拒绝；否则继续读。
                if ended {
                    self.reject(connection, "truncated_body")
                } else {
                    self.receive(connection, buffer: buf)
                }
                return
            }
            guard buf.count == needed else {
                // 声明长度之外的多余字节：不按 pipeline 处理，直接拒绝。
                self.reject(connection, "body_length_mismatch"); return
            }
            let body = Data(buf[bodyStart..<needed])
            let parsed = (method: head.method, path: head.path, headers: head.headers, body: body)
            Task { await self.handle(connection, parsed) }
        }
    }

    private nonisolated func reject(_ connection: LoopbackConnection, _ reason: String) {
        send(connection, PilotHTTP.response(400, PilotAck(ok: false, error: reason)))
    }

    private nonisolated func send(_ connection: LoopbackConnection, _ data: Data) {
        connection.send(data) { connection.cancel() }
    }

    private func authorized(_ headers: [String: String], requireOrigin: Bool) -> Bool {
        guard headers["x-unified-token"] == token,
              headers["host"] == origin.replacingOccurrences(of: "http://", with: "") else { return false }
        if requireOrigin { return headers["origin"] == origin }
        return headers["origin"] == nil || headers["origin"] == origin
    }

    private func handle(_ connection: LoopbackConnection,
                        _ parsed: (method: String, path: String, headers: [String: String], body: Data)) async {
        let (method, path, headers, body) = parsed
        let memoryPath = "/api/admin/wechat-memory"
        let personaPath = "/api/admin/wechat-persona"
        let personaRequest = path == personaPath || path.hasPrefix(personaPath + "/")
        if personaRequest || path == memoryPath || path.hasPrefix(memoryPath + "/") {
            guard headers["x-unified-admin-token"] == memoryAdminToken, headers["origin"] == origin,
                  headers["host"] == origin.replacingOccurrences(of: "http://", with: "") else {
                send(connection, PilotHTTP.response(403, PilotAck(ok: false, error: "forbidden"))); return
            }
            do {
                if personaRequest {
                    if method == "GET", path == personaPath, body.isEmpty {
                        let groups = try await core.wechatGroupPersonas()
                        let styles = WeChatGroupPersonas.styles.map(\.name)
                        send(connection, PilotHTTP.response(200, PilotPersonaList(styles: styles, groups: groups)))
                    } else {
                        guard method == "POST", path.hasPrefix(personaPath + "/"), body.count <= 512,
                              headers["content-type"]?.components(separatedBy: ";").first?.lowercased() == "application/json",
                              let change = try? JSONDecoder().decode(PilotPersonaChange.self, from: body) else {
                            throw WeChatMemoryAdminError.invalid
                        }
                        let scope = String(path.dropFirst(personaPath.count + 1))
                        try await core.setWeChatGroupPersona(scope: scope, preset: change.preset, previous: change.previous)
                        send(connection, PilotHTTP.response(200, PilotAck(ok: true)))
                    }
                } else if method == "GET", path == memoryPath, body.isEmpty {
                    send(connection, PilotHTTP.response(200, PilotMemoryResult(groups: try core.wechatMemoryGroups())))
                } else {
                    guard path.hasPrefix(memoryPath + "/") else { throw WeChatMemoryAdminError.invalid }
                    let scope = String(path.dropFirst(memoryPath.count + 1))
                    if method == "GET", body.isEmpty {
                        let entries = try await core.wechatMemoryEntries(scope: scope)
                        send(connection, PilotHTTP.response(200, PilotMemoryResult(entries: entries)))
                    } else {
                        struct Update: Decodable {
                            let id: UUID
                            let value: String?
                            let paused: Bool?
                            let previousPaused: Bool?
                        }
                        guard method == "POST", body.count <= 2048,
                              headers["content-type"]?.components(separatedBy: ";").first?.lowercased() == "application/json",
                              let update = try? JSONDecoder().decode(Update.self, from: body) else {
                            throw WeChatMemoryAdminError.invalid
                        }
                        let entry: WeChatMemoryEntry
                        if let value = update.value, update.paused == nil, update.previousPaused == nil {
                            entry = try await core.correctWeChatMemory(scope: scope, id: update.id, value: value)
                        } else if update.value == nil, let paused = update.paused, let previous = update.previousPaused {
                            entry = try await core.setWeChatMemoryPause(scope: scope, id: update.id,
                                                                        paused: paused, previous: previous)
                        } else {
                            throw WeChatMemoryAdminError.invalid
                        }
                        send(connection, PilotHTTP.response(200, PilotMemoryResult(entry: entry)))
                    }
                }
            } catch WeChatMemoryAdminError.invalid {
                send(connection, PilotHTTP.response(400, PilotAck(ok: false, error: "invalid_memory_update")))
            } catch WeChatMemoryAdminError.unknownScope {
                send(connection, PilotHTTP.response(404, PilotAck(ok: false, error: "unknown_scope")))
            } catch WeChatMemoryAdminError.stale {
                send(connection, PilotHTTP.response(409, PilotAck(ok: false, error: "stale_memory_entry")))
            } catch {
                send(connection, PilotHTTP.response(503, PilotAck(ok: false, error: "memory_unavailable")))
            }
            return
        }

        // 静态能力声明与审计：只读，不含消息原文。
        if method == "GET", path == "/api/capabilities" {
            guard authorized(headers, requireOrigin: false) else {
                send(connection, PilotHTTP.response(403, PilotAck(ok: false, error: "forbidden"))); return
            }
            send(connection, PilotHTTP.response(200, core.capabilities))
            return
        }
        if method == "GET", path == "/api/audit" {
            guard authorized(headers, requireOrigin: false) else {
                send(connection, PilotHTTP.response(403, PilotAck(ok: false, error: "forbidden"))); return
            }
            send(connection, PilotHTTP.response(200, PilotAck(ok: true, entries: core.auditSnapshot())))
            return
        }

        guard authorized(headers, requireOrigin: false) else {
            send(connection, PilotHTTP.response(403, PilotAck(ok: false, error: "forbidden"))); return
        }
        // 写操作 / 入站：token + Host 必填；Origin 若带则必须等于本机 origin
        // （已授权桥契约会带 Origin；不带 Origin 的纯本机客户端仍可工作，
        //  与 QQControlServer 的 `headers["origin"] == nil || == origin` 同口径）。
        // 这样真实桥不会被 403 静默吞掉，同时不扩大到任何非本机来源。

        // GET /api/job/<id> 是状态读取：只需 token（与 capabilities/audit 同口径），
        // 不强制 Origin，避免非浏览器本地客户端被误拒。
        if method == "GET", path.hasPrefix("/api/job/") {
            let id = String(path.dropFirst("/api/job/".count))
            guard let job = jobs[id] else {
                send(connection, PilotHTTP.response(404, PilotAck(ok: false, error: "unknown_job"))); return
            }
            send(connection, PilotHTTP.response(200, job))
            return
        }

        // 异步 job：立刻返回 job_id，长请求不在本连接上等待，彻底避开任何 15s 生命周期限制。
        if method == "POST", path == "/api/job" {
            guard let inbound = try? JSONDecoder().decode(UnifiedInbound.self, from: body) else {
                send(connection, PilotHTTP.response(400, PilotAck(ok: false, error: "bad_inbound"))); return
            }
            let id = UUID().uuidString
            jobs[id] = JobStatus(state: "pending", result: nil)
            Task.detached { [weak self] in
                guard let self else { return }
                let outcome = await self.core.handle(inbound)
                await self.markJob(id, outcome.reply)
            }
            send(connection, PilotHTTP.response(202, PilotAck(ok: true, jobId: id, state: "pending")))
            return
        }
        // 同步契约：与旧 core 的 /webhook/wechat 响应同形，pi_bridge.deliver_ready 零改动。
        if method == "POST", path == "/webhook/wechat" {
            guard let inbound = try? JSONDecoder().decode(UnifiedInbound.self, from: body) else {
                send(connection, PilotHTTP.response(400, UnifiedReply.failure("bad_inbound", scope: "", trace: "")))
                return
            }
            let outcome = await core.handle(inbound)
            send(connection, PilotHTTP.response(200, outcome.reply))
            return
        }

        if method == "POST", path == "/api/memory/confirmed" {
            struct Confirmation: Decodable { let receipt: UUID }
            guard let evidence = try? JSONDecoder().decode(Confirmation.self, from: body) else {
                send(connection, PilotHTTP.response(400, PilotAck(ok: false, error: "bad_receipt"))); return
            }
            let stored = await core.confirmWeChatMemory(evidence.receipt)
            send(connection, PilotHTTP.response(stored ? 200 : 404,
                                                PilotAck(ok: stored, error: stored ? nil : "unknown_receipt")))
            return
        }

        // 试点撤回：递增代次，在途长请求结果作废；不触碰 QQ 引擎。
        if method == "POST", path == "/api/retire" {
            let generation = await core.retireGeneration()
            send(connection, PilotHTTP.response(200, PilotAck(ok: true, generation: generation)))
            return
        }

        send(connection, PilotHTTP.response(404, PilotAck(ok: false, error: "not_found")))
    }

    private func markJob(_ id: String, _ reply: UnifiedReply) {
        jobs[id] = JobStatus(state: reply.ok ? "done" : "failed", result: reply)
    }
}
