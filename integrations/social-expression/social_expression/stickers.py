from __future__ import annotations

from typing import Dict, List, Optional, Sequence, Tuple

from .models import (
    ChatContext,
    StickerCandidate,
    StickerHistory,
    StickerSelection,
    StyleDecision,
)
from .text import normalize_text

_EMOTION_BY_TONE: Dict[str, Tuple[str, ...]] = {
    "neutral": ("neutral",),
    "playful": ("teasing", "smug", "joy", "neutral"),
    "gentle": ("neutral", "sad", "sleepy"),
    "dry": ("smug", "neutral", "annoyed"),
    "celebratory": ("joy", "smug", "teasing"),
    "sleepy": ("sleepy", "neutral"),
}

_SCENE_TAGS: Dict[str, Tuple[str, ...]] = {
    "celebration": ("celebration", "win", "happy", "joy", "congrats", "庆祝", "赢了"),
    "greeting": ("greeting", "hello", "hi", "问候", "打招呼"),
    "sleep": ("sleep", "goodnight", "晚安", "睡了"),
    "tired": ("tired", "sleep", "comfort", "累了", "休息"),
    "banter": ("banter", "tease", "smug", "laugh", "斗嘴", "调侃"),
    "chitchat": ("chat", "neutral", "日常"),
}

_BLOCKED_WORDS = ("去死", "自杀", "裸", "性行为", "傻逼", "废物", "滚")
_MIN_GAP_SECONDS = {"qq": 180.0, "wechat": 150.0}
_MIN_REPLIES = {"qq": 3, "wechat": 2}
_MAX_PER_HOUR = 2
_ITEM_REPEAT_SECONDS = 24 * 3600
_SENDER_REPEAT_SECONDS = 30 * 60
_SCORE_THRESHOLD = 2.5


def _tone_intensity(tone: str) -> int:
    return {"celebratory": 3, "playful": 2, "sleepy": 1, "gentle": 1, "dry": 1, "neutral": 0}.get(tone, 0)


def _metadata_text(item: StickerCandidate) -> str:
    return normalize_text(" ".join((item.title, item.caption, " ".join(item.tags))))


def _score(item: StickerCandidate, context: ChatContext, decision: StyleDecision, history: StickerHistory,
           now: float = 0.0) -> Tuple[float, Tuple[str, ...]]:
    reasons: List[str] = []
    score = 0.0
    targets = _EMOTION_BY_TONE.get(decision.tone, ("neutral",))
    if item.emotion == targets[0]:
        score += 3.0
        reasons.append("emotion_exact")
    elif item.emotion == "neutral":
        score += 1.0
        reasons.append("emotion_neutral")
    elif item.emotion in targets:
        score += 2.0
        reasons.append("emotion_compatible")
    else:
        score -= 1.5

    scene_tags = _SCENE_TAGS.get(decision.scenario, ())
    tag_hits = [tag for tag in item.tags if tag and any(tag in scene_tag or scene_tag in tag for scene_tag in scene_tags)]
    if tag_hits:
        score += min(3.0, 1.5 * len(tag_hits))
        reasons.append("scene_tags:" + ",".join(tag_hits[:3]))

    normalized_input = normalize_text(context.text)
    keyword_hits = [tag for tag in item.tags if tag and tag in normalized_input]
    if keyword_hits:
        score += min(2.0, 0.5 * len(keyword_hits))
        reasons.append("context_tags:" + ",".join(keyword_hits[:3]))

    target_intensity = _tone_intensity(decision.tone)
    midpoint = (item.intensity_min + item.intensity_max) / 2.0
    score += max(0.0, 1.0 - 0.4 * abs(midpoint - target_intensity))
    if decision.humor == "light" and "laugh" in item.tags:
        score += 0.5
        reasons.append("humor_match")
    if decision.humor == "none" and item.emotion in ("teasing", "annoyed"):
        score -= 1.0

    use_count = sum(1 for use in history.uses if use.sticker_id == item.id and use.seconds_ago <= 7 * 86400)
    if use_count == 0:
        score += 0.8
        reasons.append("novel")
    elif use_count > 1:
        score -= 0.3 * min(3, use_count - 1)
    return round(score, 3), tuple(reasons)


def _hard_blocked(item: StickerCandidate, context: ChatContext, history: StickerHistory) -> Optional[str]:
    if not item.allowed_in(context.scope):
        return "not_authorized_or_scope"
    if any(word in (item.title + item.caption + "".join(item.tags)) for word in _BLOCKED_WORDS):
        return "unsafe_wording"
    if history.recent_for(item.id, _ITEM_REPEAT_SECONDS):
        return "item_cooldown"
    if any(use.sender_id == context.sender_id and use.sticker_id == item.id and use.seconds_ago < _SENDER_REPEAT_SECONDS
           for use in history.uses):
        return "sender_mirror_guard"
    return None


def select_sticker(context: ChatContext, decision: StyleDecision, catalog: Sequence[StickerCandidate]) -> Optional[StickerSelection]:
    """Return a bounded, authorized shortlist entry, or None to abstain.

    No filesystem path, URL, avatar or photo is ever returned. Group sends require
    an explicitly group-approved, public-sharing, licensed/original item.
    """
    if decision.action != "reply" or decision.sticker_intent == "none":
        return None
    if not context.profile.allow_stickers or not catalog:
        return None
    history = context.sticker_history
    min_gap = _MIN_GAP_SECONDS.get(context.platform, 180.0)
    if history.last_sticker_seconds_ago is not None and history.last_sticker_seconds_ago < min_gap:
        return None
    if history.replies_since_sticker < _MIN_REPLIES.get(context.platform, 3) and decision.scenario != "celebration":
        return None
    if history.count_in(3600.0) >= _MAX_PER_HOUR:
        return None

    scored: List[Tuple[float, StickerCandidate, Tuple[str, ...]]] = []
    for item in catalog:
        if _hard_blocked(item, context, history):
            continue
        score, reasons = _score(item, context, decision, history)
        if score >= _SCORE_THRESHOLD:
            scored.append((score, item, reasons))
    if not scored:
        return None
    scored.sort(key=lambda row: (-row[0], row[1].id))
    top = scored[0]
    alternatives = tuple(row[1] for row in scored[1:4])
    return StickerSelection(item=top[1], score=top[0], reason_codes=top[2], alternatives=alternatives)


class StickerSelector:
    """Optional facade; disabled mode always abstains and leaves the legacy path intact."""

    def __init__(self, enabled: bool = False):
        self.enabled = bool(enabled)

    def select(self, context: ChatContext, decision: StyleDecision,
               catalog: Sequence[StickerCandidate]) -> Optional[StickerSelection]:
        if not self.enabled:
            return None
        return select_sticker(context, decision, catalog)
