import Foundation
import BotCore
#if canImport(Glibc)
import Glibc
#elseif canImport(Darwin)
import Darwin
#endif

struct WeChatGroupPersona: Encodable, Sendable {
    let id: String
    let label: String
    let accountCode: String
    let preset: String
}

/// Swift owns writes. The WeChat bridge reads this same file for compatibility replies.
actor WeChatGroupPersonas {
    static let filename = "unified-wechat-group-personas.json"
    static let styles = QQPersonality.allCases.filter { $0 != .custom }
    private static let limit = 32_768
    private struct Document: Codable {
        let version: Int
        var presets: [String: String]
    }

    private let file: URL?
    private var document: Document?

    init(file: URL?) {
        self.file = file
        self.document = Self.load(file)
    }

    func presets() throws -> [String: String] {
        guard let document else { throw WeChatMemoryAdminError.unavailable }
        return document.presets
    }

    func personality(for scope: String) -> QQPersonality? {
        guard let selected = document?.presets[scope] else { return nil }
        return Self.styles.first { $0.name == selected }
    }

    func set(_ preset: String, previous: String, scope: String) throws {
        guard let file, var candidate = document else { throw WeChatMemoryAdminError.unavailable }
        guard preset.isEmpty || Self.styles.contains(where: { $0.name == preset }) else {
            throw WeChatMemoryAdminError.invalid
        }
        let current = candidate.presets[scope] ?? ""
        guard current == previous else { throw WeChatMemoryAdminError.stale }
        guard current != preset else { return }
        candidate.presets[scope] = preset.isEmpty ? nil : preset
        let bytes = try JSONEncoder().encode(candidate)
        guard bytes.count <= Self.limit else { throw WeChatMemoryAdminError.unavailable }
        try UnifiedIdempotencyJournal.writeAtomically(bytes, to: file)
        document = candidate
    }

    private static func load(_ file: URL?) -> Document? {
        guard let file else { return nil }
        let descriptor = file.path.withCString { open($0, O_RDONLY | O_NOFOLLOW | O_NONBLOCK) }
        if descriptor < 0 {
            return errno == ENOENT ? Document(version: 1, presets: [:]) : nil
        }
        let handle = FileHandle(fileDescriptor: descriptor, closeOnDealloc: true)
        var metadata = stat()
        guard fstat(descriptor, &metadata) == 0, metadata.st_uid == getuid(),
              metadata.st_mode & mode_t(S_IFMT | 0o777) == mode_t(S_IFREG | 0o600),
              let data = try? handle.read(upToCount: limit + 1), data.count <= limit,
              let decoded = try? JSONDecoder().decode(Document.self, from: data),
              decoded.version == 1, decoded.presets.allSatisfy({ key, preset in
                  key.utf8.count == 64 &&
                      key.utf8.allSatisfy { (48...57).contains($0) || (97...102).contains($0) } &&
                      styles.contains { $0.name == preset }
              }) else { return nil }
        return decoded
    }
}
