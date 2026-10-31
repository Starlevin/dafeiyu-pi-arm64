import Foundation
#if canImport(FoundationNetworking)
import FoundationNetworking
#endif

// FoundationNetworking lacks AsyncBytes in the supported toolchain. The delegate
// enforces the same limit while receiving, not after allocating an unbounded body.
private final class BoundedDownload: NSObject, URLSessionDataDelegate, @unchecked Sendable {
    let limit: Int
    /// 允许跟随的重定向目标主机（由调用方写死的白名单，绝不接受用户输入）。
    private let redirectHosts: Set<String>
    private let lock = NSLock()
    private var continuation: CheckedContinuation<(Data, URLResponse), Error>?
    private var session: URLSession?
    private var task: URLSessionDataTask?
    private var cancelled = false
    private var data = Data()
    private var response: URLResponse?
    private var oversized = false
    init(limit: Int, redirectHosts: Set<String> = []) {
        self.limit = limit
        self.redirectHosts = redirectHosts
    }
    func start(configuration: URLSessionConfiguration, request: URLRequest, continuation: CheckedContinuation<(Data, URLResponse), Error>) {
        lock.lock(); defer { lock.unlock() }
        if cancelled { continuation.resume(throwing: CancellationError()); return }
        self.continuation = continuation
        let session = URLSession(configuration: configuration, delegate: self, delegateQueue: nil)
        self.session = session; let task = session.dataTask(with: request); self.task = task; task.resume()
    }
    func cancel() {
        lock.lock(); cancelled = true; let task = task; lock.unlock(); task?.cancel()
    }
    func urlSession(_ session: URLSession, task: URLSessionTask, willPerformHTTPRedirection response: HTTPURLResponse,
                    newRequest request: URLRequest, completionHandler: @escaping (URLRequest?) -> Void) {
        // 默认仍然拒绝一切重定向（防 SSRF）；只有目标主机在调用方写死的白名单里才跟随。
        let host = request.url?.host?.lowercased() ?? ""
        completionHandler(redirectHosts.contains(host) ? request : nil)
    }
    func urlSession(_ session: URLSession, dataTask: URLSessionDataTask, didReceive response: URLResponse,
                    completionHandler: @escaping (URLSession.ResponseDisposition) -> Void) {
        self.response = response
        oversized = response.expectedContentLength > limit
        completionHandler(oversized ? .cancel : .allow)
    }
    func urlSession(_ session: URLSession, dataTask: URLSessionDataTask, didReceive bytes: Data) {
        guard !oversized, bytes.count <= limit - data.count else { oversized = true; dataTask.cancel(); return }
        data.append(bytes)
    }
    func urlSession(_ session: URLSession, task: URLSessionTask, didCompleteWithError error: Error?) {
        lock.lock(); let callback = continuation; continuation = nil; self.session = nil; self.task = nil; lock.unlock()
        session.finishTasksAndInvalidate()
        if oversized { callback?.resume(throwing: URLError(.dataLengthExceedsMaximum)) }
        else if let error { callback?.resume(throwing: error) }
        else if let response { callback?.resume(returning: (data, response)) }
        else { callback?.resume(throwing: URLError(.badServerResponse)) }
    }
}

extension URLSession {
    /// `redirectHosts` 为空时行为与原来一致：拒绝一切重定向。
    func boundedData(for request: URLRequest, limit: Int, redirectHosts: Set<String> = []) async throws -> (Data, URLResponse) {
        let download = BoundedDownload(limit: limit, redirectHosts: redirectHosts)
        return try await withTaskCancellationHandler {
            try await withCheckedThrowingContinuation { continuation in
                download.start(configuration: configuration, request: request, continuation: continuation)
            }
        } onCancel: { download.cancel() }
    }
}
