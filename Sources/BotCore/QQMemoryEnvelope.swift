import Foundation

/// Stable, text-bearing envelope for the B→C handoff. It is intentionally
/// separate from the reply prompt string so C can consume facts without parsing
/// concatenated context. Every item is already subject-scoped and redacted.
public struct QQMemoryFactEnvelope: Codable, Equatable, Sendable, Identifiable {
    public var id: String
    public var subject: String
    public var kind: String
    public var text: String
    public var keywords: [String]
    public var importance: Int
    public var sourceIDs: [String]
    public var updatedAt: Date
    public var expiresAt: Date
    public var invalidatedAt: Date?
    public var current: Bool

    public init(item: QQMemoryItem, now: Date = Date()) {
        self.id = item.id
        self.subject = item.subject
        self.kind = item.kind
        self.text = item.text
        self.keywords = item.keywords
        self.importance = item.importance
        self.sourceIDs = item.sourceIDs
        self.updatedAt = item.updatedAt
        self.expiresAt = item.expiresAt
        self.invalidatedAt = item.invalidatedAt
        self.current = item.current(now)
    }
}

public struct QQMemoryEpisodeEnvelope: Codable, Equatable, Sendable, Identifiable {
    public var id: String
    public var text: String
    public var sourceIDs: [String]
    public var version: Int
    public var supersedes: String?
    public var supersededBy: String?
    public var createdAt: Date
    public var expiresAt: Date
    public var current: Bool

    public init(episode: QQMemoryEpisode, now: Date = Date()) {
        self.id = episode.id
        self.text = episode.text
        self.sourceIDs = episode.sourceIDs
        self.version = episode.version
        self.supersedes = episode.supersedes
        self.supersededBy = episode.supersededBy
        self.createdAt = episode.createdAt
        self.expiresAt = episode.expiresAt
        self.current = episode.current(now)
    }
}

/// Context for one exact subject (one member in one group, or one private peer).
/// `scope` is always the same scope that produced the facts; consumers must not
/// merge envelopes from different scopes.
public struct QQMemoryContextEnvelope: Codable, Equatable, Sendable {
    public var scope: String
    public var subject: String
    public var query: String
    public var text: String
    public var facts: [QQMemoryFactEnvelope]
    public var episodes: [QQMemoryEpisodeEnvelope]
    public var generatedAt: Date
    public var source: String

    public init(scope: String, subject: String, query: String, text: String,
                facts: [QQMemoryFactEnvelope], episodes: [QQMemoryEpisodeEnvelope],
                generatedAt: Date = Date(), source: String = "reliable") {
        self.scope = scope
        self.subject = subject
        self.query = query
        self.text = text
        self.facts = facts
        self.episodes = episodes
        self.generatedAt = generatedAt
        self.source = source
    }
}

public extension QQMemoryReliableStore {
    /// Pure read: no ingest, no apply, no archive mutation.
    func contextEnvelope(_ query: String, subject: String, characters: Int, now: Date = Date(),
                         includeEpisodes: Bool = true) -> QQMemoryContextEnvelope {
        let text = context(query, subject: subject, characters: characters, now: now,
                           includeEpisodes: includeEpisodes)
        let facts = search(query, subject: subject, limit: 12, now: now).map {
            QQMemoryFactEnvelope(item: $0, now: now)
        }
        let episodes = includeEpisodes
            ? self.episodes.filter { $0.current(now) || now.timeIntervalSince($0.createdAt) < 86400 }
                .sorted { $0.version > $1.version }
                .map { QQMemoryEpisodeEnvelope(episode: $0, now: now) }
            : []
        return QQMemoryContextEnvelope(scope: scope, subject: subject, query: query, text: text,
                                       facts: facts, episodes: episodes, generatedAt: now)
    }
}
