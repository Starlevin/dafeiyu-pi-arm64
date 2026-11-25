import BotCore
import Foundation

enum OneBotRelayIdentity {
    case qqAdminLoopback
    case fixture // Explicit synthetic identity used only by loopback transport tests.
}

struct OneBotExplicitRejection: LocalizedError {
    let reason: String
    var errorDescription: String? { reason }
}

struct OneBotActionTimeout: LocalizedError {
    var errorDescription: String? { "OneBot 响应超时；如已提交发送，结果未知且不重发" }
}
#if canImport(FoundationNetworking)
import FoundationNetworking
#endif

/// One local, authenticated connection. A broken connection cancels all outstanding actions;
/// sends are never retried because a missing acknowledgement is an unknown outcome.
@MainActor final class OneBotConnection {
    #if os(Linux)
    private var socket: LinuxOneBotTransport?
    #else
    private var socket: URLSessionWebSocketTask?
    #endif
    private var receiver: Task<Void, Never>?
    private var pending: [String: CheckedContinuation<[String: Any], Error>] = [:]
    private var deadlines: [String: Task<Void, Never>] = [:]
    private var relayIdentity: OneBotRelayIdentity?
    var event: (([String: Any]) -> Void)?
    var disconnected: ((Error) -> Void)?
    func connect(endpoint: String, token: String, relayIdentity: OneBotRelayIdentity? = nil) async throws {
        close()
        guard let url = URL(string: endpoint), !token.isEmpty else { throw AppFailure.message("请先配置 OneBot 令牌") }
        if case .some(.qqAdminLoopback) = relayIdentity {
            guard let parts = URLComponents(url: url, resolvingAgainstBaseURL: false), parts.scheme == "ws",
                  parts.host == "127.0.0.1", parts.port == 13001,
                  parts.user == nil, parts.password == nil,
                  parts.path.isEmpty || parts.path == "/", parts.query == nil, parts.fragment == nil else {
                throw AppFailure.message("qq-admin 回执兼容只允许 ws://127.0.0.1:13001")
            }
        }
        #if os(Linux)
        let socket = LinuxOneBotTransport()
        self.socket = socket
        do { try await socket.connect(url: url, token: token) }
        catch { if self.socket === socket { close(error: error) }; throw error }
        guard self.socket === socket else { socket.close(); throw CancellationError() }
        #else
        var request = URLRequest(url: url)
        request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        let socket = URLSession.shared.webSocketTask(with: request)
        socket.maximumMessageSize = 1_048_576
        self.socket = socket; socket.resume()
        #endif
        self.relayIdentity = relayIdentity
        receiver = Task { [weak self] in
            do {
                #if os(Linux)
                for try await data in socket.messages {
                    guard let self, self.socket === socket else { return }
                    guard !socket.closed else { throw URLError(.networkConnectionLost) }
                    try self.receive(data)
                }
                #else
                while !Task.isCancelled {
                    let message = try await socket.receive()
                    guard let self, self.socket === socket else { return }
                    let data: Data
                    switch message { case .data(let d): data = d; case .string(let s): data = Data(s.utf8); @unknown default: continue }
                    try self.receive(data)
                }
                #endif
            } catch {
                guard let self, self.socket === socket else { return }
                self.close(error: error); self.disconnected?(error)
            }
        }
    }
    private func receive(_ data: Data) throws {
        guard let object = try JSONSerialization.jsonObject(with: data) as? [String: Any] else { return }
        if let echo = object["echo"] as? String, let continuation = pending.removeValue(forKey: echo) {
            deadlines.removeValue(forKey: echo)?.cancel()
            if object["status"] as? String == "ok", (object["retcode"] as? Int) == 0 {
                // Lists and objects retain their original shape inside the data field.
                continuation.resume(returning: object)
            } else {
                let retcode = object["retcode"] as? Int ?? -1
                continuation.resume(throwing: AppFailure.message("OneBot 操作失败，返回码 \(retcode)"))
            }
        } else { event?(object) }
    }
    func action(_ name: String, params: [String: Any] = [:], timeout: Duration = .seconds(15)) async throws -> [String: Any] {
        guard let socket else { throw AppFailure.message("QQ 未连接") }
        try Task.checkCancellation()
        let acceptsQuotaSentinel = isKnownQuotaSentinelRelay
        let echo = UUID().uuidString
        let data = try JSONSerialization.data(withJSONObject: ["action": name, "params": params, "echo": echo])
        let response = try await withTaskCancellationHandler {
            try await withCheckedThrowingContinuation { continuation in
                pending[echo] = continuation
                deadlines[echo] = Task { [weak self] in
                    do { try await Task.sleep(for: timeout) } catch { return }
                    self?.fail(echo, error: OneBotActionTimeout())
                }
                let completion: (Error?) -> Void = { [weak self] error in
                    if let error { Task { @MainActor in self?.fail(echo, error: error) } }
                }
                #if os(Linux)
                socket.send(data, completion: completion)
                #else
                socket.send(.data(data), completionHandler: completion)
                #endif
            }
        } onCancel: {
            Task { @MainActor [weak self] in self?.fail(echo, error: CancellationError()) }
        }
        let isSend = name == "send_group_msg" || name == "send_private_msg"
        let messageID = (response["data"] as? [String: Any])?["message_id"]
        if isSend, QQPolicy.identifier(messageID) == "-1", acceptsQuotaSentinel {
            throw OneBotExplicitRejection(reason: "本机 qq-admin relay 已在发送前因配额拒绝（message_id=-1）；未转发至 NapCat，未重试")
        }
        return response
    }
    private var isKnownQuotaSentinelRelay: Bool {
        switch relayIdentity {
        case .some(.fixture): return true
        case .some(.qqAdminLoopback): return true // connect() validates the exact scheme, host, and port.
        case .none: return false
        }
    }
    private func fail(_ echo: String, error: Error) {
        deadlines.removeValue(forKey: echo)?.cancel()
        pending.removeValue(forKey: echo)?.resume(throwing: error)
    }
    func close(error: Error? = nil) {
        #if os(Linux)
        socket?.close()
        #else
        socket?.cancel(with: .goingAway, reason: nil)
        #endif
        socket = nil
        relayIdentity = nil
        receiver?.cancel(); receiver = nil
        let callbacks = pending.values; pending.removeAll()
        deadlines.values.forEach { $0.cancel() }; deadlines.removeAll()
        callbacks.forEach { $0.resume(throwing: error ?? AppFailure.message("QQ 连接已关闭；未确认的发送不重试")) }
    }
}
