import Foundation
#if canImport(FoundationNetworking)
import FoundationNetworking
#endif

// FoundationNetworking lacks AsyncBytes in the supported toolchain, and tearing down
// short-lived URLSessions on Linux can abort inside _MultiHandle teardown. Production
// calls reuse long-lived pooled sessions while custom URLProtocol test fixtures stay isolated.
private final class BoundedSessionPool: @unchecked Sendable {
    private struct Key: Hashable {
        let resourceTimeoutSeconds: Int
        let disableProxy: Bool
    }

    private final class PooledRunner: NSObject, URLSessionDataDelegate, @unchecked Sendable {
        private let lock = NSLock()
        private var downloads: [Int: BoundedDownload] = [:]
        private lazy var session: URLSession = URLSession(configuration: configuration, delegate: self, delegateQueue: nil)
        private let configuration: URLSessionConfiguration

        init(resourceTimeoutSeconds: Int, disableProxy: Bool) {
            let config = URLSessionConfiguration.ephemeral
            config.urlCache = nil
            config.httpCookieStorage = nil
            config.requestCachePolicy = .reloadIgnoringLocalCacheData
            if resourceTimeoutSeconds > 0 {
                config.timeoutIntervalForRequest = TimeInterval(resourceTimeoutSeconds)
                config.timeoutIntervalForResource = TimeInterval(resourceTimeoutSeconds)
            }
            if disableProxy {
                config.connectionProxyDictionary = [:]
            }
            self.configuration = config
            super.init()
            _ = self.session
        }

        func start(request: URLRequest, download: BoundedDownload) {
            let task = session.dataTask(with: request)
            lock.lock()
            downloads[task.taskIdentifier] = download
            let attached = download.attach(task: task, session: nil)
            if !attached {
                downloads.removeValue(forKey: task.taskIdentifier)
            }
            lock.unlock()
            if attached {
                task.resume()
            } else {
                download.finishCancelledEarly()
            }
        }

        private func lookup(_ task: URLSessionTask) -> BoundedDownload? {
            lock.lock(); defer { lock.unlock() }
            return downloads[task.taskIdentifier]
        }

        private func pop(_ task: URLSessionTask) -> BoundedDownload? {
            lock.lock(); defer { lock.unlock() }
            return downloads.removeValue(forKey: task.taskIdentifier)
        }

        func urlSession(_ session: URLSession, task: URLSessionTask, willPerformHTTPRedirection response: HTTPURLResponse,
                        newRequest request: URLRequest, completionHandler: @escaping (URLRequest?) -> Void) {
            if let download = lookup(task) {
                download.handleRedirect(newRequest: request, completionHandler: completionHandler)
            } else {
                completionHandler(nil)
            }
        }

        func urlSession(_ session: URLSession, dataTask: URLSessionDataTask, didReceive response: URLResponse,
                        completionHandler: @escaping (URLSession.ResponseDisposition) -> Void) {
            if let download = lookup(dataTask) {
                download.handleResponse(response, completionHandler: completionHandler)
            } else {
                completionHandler(.cancel)
            }
        }

        func urlSession(_ session: URLSession, dataTask: URLSessionDataTask, didReceive bytes: Data) {
            lookup(dataTask)?.handleBytes(bytes, dataTask: dataTask)
        }

        func urlSession(_ session: URLSession, task: URLSessionTask, didCompleteWithError error: Error?) {
            pop(task)?.handleCompletion(session: session, error: error, invalidateSession: false)
        }
    }

    static let shared = BoundedSessionPool()
    private let lock = NSLock()
    private var runners: [Key: PooledRunner] = [:]

    func start(configuration: URLSessionConfiguration, request: URLRequest, download: BoundedDownload) {
        let rawTimeout = configuration.timeoutIntervalForResource
        let resourceSeconds = (rawTimeout > 0 && rawTimeout <= 3600) ? max(1, Int(ceil(rawTimeout))) : 0
        let disableProxy = (configuration.connectionProxyDictionary?.isEmpty == true)
        let key = Key(resourceTimeoutSeconds: resourceSeconds, disableProxy: disableProxy)
        let runner: PooledRunner = {
            lock.lock(); defer { lock.unlock() }
            if let existing = runners[key] { return existing }
            let created = PooledRunner(resourceTimeoutSeconds: resourceSeconds, disableProxy: disableProxy)
            runners[key] = created
            return created
        }()
        runner.start(request: request, download: download)
    }
}

private final class BoundedDownload: NSObject, URLSessionDataDelegate, @unchecked Sendable {
    private static let defaultProtocolIDs: Set<ObjectIdentifier> = Set(
        (URLSessionConfiguration.ephemeral.protocolClasses ?? []).map { ObjectIdentifier($0) }
    )

    private static func hasCustomProtocolClasses(_ configuration: URLSessionConfiguration) -> Bool {
        guard let classes = configuration.protocolClasses, !classes.isEmpty else {
            return false
        }
        return classes.contains { !defaultProtocolIDs.contains(ObjectIdentifier($0)) }
    }

    let limit: Int
    /// 允许跟随的重定向目标主机（由调用方写死的白名单，绝不接受用户输入）。
    private let redirectHosts: Set<String>
    private let lock = NSLock()
    private var continuation: CheckedContinuation<(Data, URLResponse), Error>?
    private var isolatedSession: URLSession?
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
        lock.lock()
        if cancelled {
            lock.unlock()
            continuation.resume(throwing: CancellationError())
            return
        }
        self.continuation = continuation
        lock.unlock()

        if Self.hasCustomProtocolClasses(configuration) {
            let session = URLSession(configuration: configuration, delegate: self, delegateQueue: nil)
            let task = session.dataTask(with: request)
            lock.lock()
            let shouldResume = attachLocked(task: task, session: session)
            lock.unlock()
            if shouldResume {
                task.resume()
            } else {
                session.invalidateAndCancel()
                finishCancelledEarly()
            }
            return
        }

        BoundedSessionPool.shared.start(configuration: configuration, request: request, download: self)
    }

    func attach(task: URLSessionDataTask, session: URLSession?) -> Bool {
        lock.lock(); defer { lock.unlock() }
        return attachLocked(task: task, session: session)
    }

    private func attachLocked(task: URLSessionDataTask, session: URLSession?) -> Bool {
        if cancelled { return false }
        self.isolatedSession = session
        self.task = task
        return true
    }

    func finishCancelledEarly() {
        lock.lock()
        let callback = continuation
        continuation = nil
        isolatedSession = nil
        task = nil
        lock.unlock()
        callback?.resume(throwing: CancellationError())
    }

    func cancel() {
        lock.lock()
        cancelled = true
        let task = task
        lock.unlock()
        task?.cancel()
    }

    func handleRedirect(newRequest request: URLRequest, completionHandler: @escaping (URLRequest?) -> Void) {
        // 默认仍然拒绝一切重定向（防 SSRF）；只有目标主机在调用方写死的白名单里才跟随。
        let host = request.url?.host?.lowercased() ?? ""
        completionHandler(redirectHosts.contains(host) ? request : nil)
    }

    func handleResponse(_ response: URLResponse, completionHandler: @escaping (URLSession.ResponseDisposition) -> Void) {
        lock.lock()
        self.response = response
        oversized = response.expectedContentLength > limit
        let isOversized = oversized
        lock.unlock()
        completionHandler(isOversized ? .cancel : .allow)
    }

    func handleBytes(_ bytes: Data, dataTask: URLSessionDataTask) {
        lock.lock()
        guard !oversized, bytes.count <= limit - data.count else {
            oversized = true
            lock.unlock()
            dataTask.cancel()
            return
        }
        data.append(bytes)
        lock.unlock()
    }

    func handleCompletion(session: URLSession, error: Error?, invalidateSession: Bool) {
        lock.lock()
        let callback = continuation
        continuation = nil
        self.isolatedSession = nil
        self.task = nil
        let isOversized = oversized
        let collected = data
        let capturedResponse = response
        lock.unlock()
        if invalidateSession {
            session.finishTasksAndInvalidate()
        }
        if isOversized { callback?.resume(throwing: URLError(.dataLengthExceedsMaximum)) }
        else if let error { callback?.resume(throwing: error) }
        else if let capturedResponse { callback?.resume(returning: (collected, capturedResponse)) }
        else { callback?.resume(throwing: URLError(.badServerResponse)) }
    }

    func urlSession(_ session: URLSession, task: URLSessionTask, willPerformHTTPRedirection response: HTTPURLResponse,
                    newRequest request: URLRequest, completionHandler: @escaping (URLRequest?) -> Void) {
        handleRedirect(newRequest: request, completionHandler: completionHandler)
    }

    func urlSession(_ session: URLSession, dataTask: URLSessionDataTask, didReceive response: URLResponse,
                    completionHandler: @escaping (URLSession.ResponseDisposition) -> Void) {
        handleResponse(response, completionHandler: completionHandler)
    }

    func urlSession(_ session: URLSession, dataTask: URLSessionDataTask, didReceive bytes: Data) {
        handleBytes(bytes, dataTask: dataTask)
    }

    func urlSession(_ session: URLSession, task: URLSessionTask, didCompleteWithError error: Error?) {
        handleCompletion(session: session, error: error, invalidateSession: true)
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
