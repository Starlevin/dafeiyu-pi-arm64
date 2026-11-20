from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Optional, Sequence, Tuple

PLATFORMS = ("qq", "wechat")
SCOPES = ("group", "private")
ROLES = ("owner", "member", "peer", "bot", "unknown")
ACTIONS = ("reply", "silence")
LENGTHS = ("short", "medium", "long")
TONES = ("neutral", "playful", "gentle", "dry", "celebratory", "sleepy")
HUMORS = ("none", "light", "self_deprecating")
QUOTES = ("none", "reply_to_current", "reference_previous")
STICKER_INTENTS = ("none", "optional", "preferred")


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _has_control(text: str) -> bool:
    return any(ord(ch) < 32 and ch not in "\n\t" for ch in text)


@dataclass(frozen=True)
class StyleProfile:
    """Channel and persona caps. Do not infer identity from here."""

    platform: str = "qq"
    max_chars: int = 60
    max_bubbles: int = 1
    allow_stickers: bool = True
    allow_quote: bool = True
    allow_questions: bool = True
    allow_long_form: bool = True
    version: str = "social-style-c1"

    def __post_init__(self) -> None:
        if self.platform not in PLATFORMS:
            raise ValueError("platform must be qq or wechat")
        if not 20 <= self.max_chars <= 300:
            raise ValueError("max_chars must be 20..300")
        if not 1 <= self.max_bubbles <= 3:
            raise ValueError("max_bubbles must be 1..3")
        if len(self.version) > 40 or _has_control(self.version):
            raise ValueError("invalid profile version")

    @property
    def short_budget(self) -> int:
        return max(18, min(self.max_chars, 34))

    @property
    def medium_budget(self) -> int:
        return max(self.short_budget, min(self.max_chars, 58))

    @property
    def long_budget(self) -> int:
        return self.max_chars


@dataclass(frozen=True)
class BotReply:
    """A previous bot message, expressed relative to now for deterministic tests."""

    text: str
    seconds_ago: float
    sticker_id: Optional[str] = None
    template_key: Optional[str] = None

    def __post_init__(self) -> None:
        if len(self.text) > 1000:
            raise ValueError("bot reply too long")
        if not 0 <= self.seconds_ago <= 7 * 86400:
            raise ValueError("bot reply age out of range")
        if _has_control(self.text):
            raise ValueError("bot reply has control characters")


@dataclass(frozen=True)
class ChatMessage:
    message_id: str
    sender_id: str
    sender_role: str
    text: str
    seconds_ago: float
    mentioned_bot: bool = False
    reply_to_bot: bool = False
    has_media: bool = False
    media_readable: bool = True
    is_forward: bool = False

    def __post_init__(self) -> None:
        if self.sender_role not in ROLES:
            raise ValueError("invalid sender_role")
        if len(self.text) > 4000 or len(self.sender_id) > 128:
            raise ValueError("message too large")
        if not 0 <= self.seconds_ago <= 7 * 86400:
            raise ValueError("message age out of range")


@dataclass(frozen=True)
class StickerUse:
    sticker_id: str
    seconds_ago: float
    sender_id: str = ""

    def __post_init__(self) -> None:
        if not self.sticker_id or len(self.sticker_id) > 128:
            raise ValueError("invalid sticker id")
        if not 0 <= self.seconds_ago <= 30 * 86400:
            raise ValueError("sticker use age out of range")


@dataclass(frozen=True)
class StickerHistory:
    uses: Tuple[StickerUse, ...] = ()
    last_sticker_seconds_ago: Optional[float] = None
    replies_since_sticker: int = 99

    def __post_init__(self) -> None:
        if self.last_sticker_seconds_ago is not None and not 0 <= self.last_sticker_seconds_ago <= 30 * 86400:
            raise ValueError("last sticker age out of range")
        if not 0 <= self.replies_since_sticker <= 999:
            raise ValueError("invalid replies_since_sticker")

    def recent_for(self, sticker_id: str, window_seconds: float) -> bool:
        return any(use.sticker_id == sticker_id and use.seconds_ago < window_seconds for use in self.uses)

    def count_in(self, window_seconds: float) -> int:
        return sum(1 for use in self.uses if use.seconds_ago < window_seconds)

    def sender_used_recently(self, sender_id: str, window_seconds: float) -> bool:
        return any(use.sender_id == sender_id and use.seconds_ago < window_seconds for use in self.uses)


@dataclass(frozen=True)
class StickerCandidate:
    """Metadata only. The module never reads or returns a filesystem path."""

    id: str
    title: str
    emotion: str
    tags: Tuple[str, ...] = ()
    license: str = ""
    author: str = ""
    source: str = ""
    origin: str = "bundled"
    group_ok: bool = False
    private_ok: bool = True
    public_sharing: bool = False
    intensity_min: int = 0
    intensity_max: int = 3
    sha256: str = ""
    caption: str = ""

    ALLOWED_ORIGINS = ("bundled", "original", "licensed")
    BLOCKED_ORIGINS = ("member_photo", "member_avatar", "forwarded_unknown", "screenshot", "unverified")

    def __post_init__(self) -> None:
        if not self.id or len(self.id) > 128:
            raise ValueError("invalid sticker id")
        if not self.title or len(self.title) > 80:
            raise ValueError("invalid sticker title")
        if not 0 <= self.intensity_min <= self.intensity_max <= 3:
            raise ValueError("invalid sticker intensity")
        if len(self.tags) > 12 or any(len(tag) > 24 for tag in self.tags):
            raise ValueError("invalid sticker tags")
        if _has_control(self.title + self.caption):
            raise ValueError("sticker metadata has control characters")

    @property
    def authorized_for_use(self) -> bool:
        if self.origin in self.BLOCKED_ORIGINS or self.origin not in self.ALLOWED_ORIGINS:
            return False
        if self.origin == "bundled":
            return True
        return bool(self.license.strip()) and bool(self.author.strip())

    def allowed_in(self, scope: str) -> bool:
        if not self.authorized_for_use:
            return False
        if scope == "group":
            return self.group_ok and self.public_sharing
        return self.private_ok


@dataclass(frozen=True)
class GroupMeme:
    """A whitelisted, per-group term. It is never persisted by this module."""

    term: str
    meaning: str
    group_id: str
    safe: bool = False
    sensitive: bool = False
    expires_in_seconds: Optional[float] = None
    group_key: str = ""

    def __post_init__(self) -> None:
        if not self.term or len(self.term) > 24 or len(self.meaning) > 80:
            raise ValueError("invalid meme term")
        if len(self.group_id) > 128 or len(self.group_key) > 32:
            raise ValueError("invalid meme group")

    def usable(self) -> bool:
        return self.safe and not self.sensitive and (self.expires_in_seconds is None or self.expires_in_seconds > 0)


@dataclass(frozen=True)
class MemoryFact:
    """Already-redacted digest from session B. No raw chat text, no account ids."""

    subject_hash: str
    fact: str
    confidence: float = 0.5
    valid_for_seconds: Optional[float] = None
    citable: bool = False

    def __post_init__(self) -> None:
        if len(self.subject_hash) > 32 or len(self.fact) > 160:
            raise ValueError("invalid memory fact")
        if not 0 <= self.confidence <= 1:
            raise ValueError("invalid confidence")


@dataclass(frozen=True)
class UpstreamDecision:
    """Minimal contract from session A's reply gate; C never re-scores speak/wait."""

    action: str = "speak"
    score: float = 0.0
    reason: str = ""
    addressed: bool = False
    ambient_relevance: float = 0.0
    ttl_seconds: float = 120.0

    def __post_init__(self) -> None:
        if self.action not in ("speak", "wait", "skip"):
            raise ValueError("invalid upstream action")
        if not 0 <= self.score <= 1 or not 0 <= self.ambient_relevance <= 1:
            raise ValueError("invalid upstream scores")


@dataclass(frozen=True)
class ChatContext:
    platform: str
    scope: str
    conversation_id: str
    self_id: str
    sender_id: str
    sender_role: str
    text: str
    profile: StyleProfile
    mentioned_bot: bool = False
    reply_to_bot: bool = False
    quoted_bot: bool = False
    has_media: bool = False
    media_kind: str = ""
    media_readable: bool = True
    is_forward: bool = False
    ambient_relevance: float = 0.0
    upstream: Optional[UpstreamDecision] = None
    recent_messages: Tuple[ChatMessage, ...] = ()
    bot_replies: Tuple[BotReply, ...] = ()
    sticker_history: StickerHistory = field(default_factory=StickerHistory)
    group_memes: Tuple[GroupMeme, ...] = ()
    memory_facts: Tuple[MemoryFact, ...] = ()
    sensitive_flags: Tuple[str, ...] = ()
    topic_tags: Tuple[str, ...] = ()
    group_key: str = ""
    local_hour: int = 12
    voice_requested: bool = False

    def __post_init__(self) -> None:
        if self.platform not in PLATFORMS:
            raise ValueError("invalid platform")
        if self.scope not in SCOPES:
            raise ValueError("invalid scope")
        if self.sender_role not in ROLES:
            raise ValueError("invalid sender_role")
        if not self.conversation_id or len(self.conversation_id) > 128 or _has_control(self.conversation_id):
            raise ValueError("invalid conversation_id")
        if not self.self_id or len(self.self_id) > 128 or not self.sender_id or len(self.sender_id) > 128:
            raise ValueError("invalid participant id")
        if len(self.text) > 4000 or _has_control(self.text):
            raise ValueError("invalid text")
        if not 0 <= self.ambient_relevance <= 1:
            raise ValueError("ambient_relevance must be 0..1")
        if not 0 <= self.local_hour <= 23:
            raise ValueError("local_hour must be 0..23")
        if len(self.recent_messages) > 40 or len(self.bot_replies) > 40:
            raise ValueError("context window too large")
        if len(self.sensitive_flags) > 12 or len(self.topic_tags) > 12:
            raise ValueError("too many flags")
        if len(self.group_key) > 32:
            raise ValueError("invalid group_key")

    @property
    def addressed_to_bot(self) -> bool:
        return self.mentioned_bot or self.reply_to_bot or self.quoted_bot

    @property
    def effective_relevance(self) -> float:
        if self.upstream is not None and self.upstream.ambient_relevance > 0:
            return _clamp(max(self.ambient_relevance, self.upstream.ambient_relevance), 0.0, 1.0)
        return _clamp(self.ambient_relevance, 0.0, 1.0)


@dataclass(frozen=True)
class StyleDecision:
    action: str
    scenario: str
    length: str
    tone: str
    humor: str
    quote: str
    question: bool
    sticker_intent: str
    max_chars: int
    max_bubbles: int
    voice: bool
    visual_grounding_required: bool
    rewrite_required: bool
    memory_allowed: bool
    meme_term: Optional[str]
    avoid_recent_phrases: Tuple[str, ...]
    reason_codes: Tuple[str, ...]
    confidence: float
    profile_version: str

    def __post_init__(self) -> None:
        if self.action not in ACTIONS or self.length not in LENGTHS or self.tone not in TONES:
            raise ValueError("invalid decision enum")
        if self.humor not in HUMORS or self.quote not in QUOTES or self.sticker_intent not in STICKER_INTENTS:
            raise ValueError("invalid decision enum")
        if not 1 <= self.max_chars <= 300 or not 1 <= self.max_bubbles <= 3:
            raise ValueError("invalid budget")
        if not 0 <= self.confidence <= 1:
            raise ValueError("invalid confidence")

    @property
    def should_send(self) -> bool:
        return self.action == "reply"


@dataclass(frozen=True)
class StickerSelection:
    item: StickerCandidate
    score: float
    reason_codes: Tuple[str, ...]
    alternatives: Tuple[StickerCandidate, ...] = ()

    def __post_init__(self) -> None:
        if self.score < 0:
            raise ValueError("score must be non-negative")
        if len(self.alternatives) > 5:
            raise ValueError("too many alternatives")

    @property
    def item_id(self) -> str:
        return self.item.id
