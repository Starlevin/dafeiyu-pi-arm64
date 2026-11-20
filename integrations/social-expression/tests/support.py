from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, List, Optional

import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from social_expression import (  # noqa: E402
    BotReply,
    ChatContext,
    ChatMessage,
    GroupMeme,
    StickerHistory,
    StyleProfile,
    UpstreamDecision,
    scan_sensitive,
)


FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "synthetic_style_cases.json"


def default_profile(platform: str, scope: str) -> StyleProfile:
    if platform == "qq":
        return StyleProfile(platform="qq", max_chars=60, max_bubbles=1)
    if scope == "group":
        return StyleProfile(platform="wechat", max_chars=70, max_bubbles=2)
    return StyleProfile(platform="wechat", max_chars=140, max_bubbles=2)


def make_context(text: str, *, platform: str = "qq", scope: str = "group", sender_role: Optional[str] = None,
                 mentioned: bool = False, sender_id: str = "u1", history: Optional[StickerHistory] = None,
                 ambient: float = 0.0, bot_replies: tuple = (), recent_messages: tuple = (),
                 group_memes: tuple = (), upstream: Optional[UpstreamDecision] = None,
                 local_hour: int = 12, voice: bool = False, has_media: bool = False,
                 media_readable: bool = True, sensitive_flags: Optional[tuple] = None,
                 reply_to_bot: bool = False, quoted_bot: bool = False, group_key: str = "") -> ChatContext:
    role = sender_role or ("peer" if scope == "private" else "member")
    flags = tuple(sorted(set(sensitive_flags or ()) | set(scan_sensitive(text))))
    return ChatContext(
        platform=platform, scope=scope,
        conversation_id="100000001" if platform == "qq" else "测试群(5)",
        self_id="bot", sender_id=sender_id, sender_role=role, text=text,
        profile=default_profile(platform, scope), mentioned_bot=mentioned,
        reply_to_bot=reply_to_bot, quoted_bot=quoted_bot,
        ambient_relevance=ambient, bot_replies=bot_replies, recent_messages=recent_messages,
        sticker_history=history or StickerHistory(), group_memes=group_memes, sensitive_flags=flags,
        group_key=group_key,
        upstream=upstream, local_hour=local_hour, voice_requested=voice,
        has_media=has_media, media_readable=media_readable,
    )


def load_cases() -> List[Dict[str, object]]:
    payload = json.loads(FIXTURES.read_text(encoding="utf-8"))
    if payload.get("version") != 1:
        raise ValueError("unsupported fixture version")
    return list(payload["cases"])


def build_context(case: Dict[str, object]) -> ChatContext:
    platform = str(case["platform"])
    scope = str(case["scope"])
    text = str(case["text"])
    flags = tuple(sorted(set(case.get("sensitive_flags", []) or []) | set(scan_sensitive(text))))
    recent = tuple(
        ChatMessage(
            message_id=str(row.get("message_id", "m")),
            sender_id=str(row.get("sender_id", "u")),
            sender_role=str(row.get("sender_role", "member")),
            text=str(row.get("text", "")),
            seconds_ago=float(row.get("seconds_ago", 60)),
            mentioned_bot=bool(row.get("mentioned_bot", False)),
            reply_to_bot=bool(row.get("reply_to_bot", False)),
            has_media=bool(row.get("has_media", False)),
            media_readable=bool(row.get("media_readable", True)),
            is_forward=bool(row.get("is_forward", False)),
        )
        for row in (case.get("recent_messages", []) or [])
    )
    bot_replies = tuple(
        BotReply(
            text=str(row.get("text", "")),
            seconds_ago=float(row.get("seconds_ago", 60)),
            sticker_id=row.get("sticker_id"),
        )
        for row in (case.get("bot_replies", []) or [])
    )
    sticker_row = case.get("sticker_history") or {}
    sticker_history = StickerHistory(
        uses=tuple(),
        last_sticker_seconds_ago=sticker_row.get("last_sticker_seconds_ago"),
        replies_since_sticker=int(sticker_row.get("replies_since_sticker", 99)),
    )
    memes = tuple(
        GroupMeme(
            term=str(row["term"]),
            meaning=str(row["meaning"]),
            group_id=str(row["group_id"]),
            safe=bool(row.get("safe", False)),
            sensitive=bool(row.get("sensitive", False)),
            expires_in_seconds=row.get("expires_in_seconds"),
        )
        for row in (case.get("group_memes", []) or [])
    )
    upstream_row = case.get("upstream")
    upstream = UpstreamDecision(**upstream_row) if upstream_row else None
    return ChatContext(
        platform=platform,
        scope=scope,
        conversation_id=str(case["conversation_id"]),
        self_id="bot",
        sender_id=str(case["sender_id"]),
        sender_role=str(case["sender_role"]),
        text=text,
        profile=default_profile(platform, scope),
        mentioned_bot=bool(case.get("mentioned_bot", False)),
        reply_to_bot=bool(case.get("reply_to_bot", False)),
        quoted_bot=bool(case.get("quoted_bot", False)),
        has_media=bool(case.get("has_media", False)),
        media_kind=str(case.get("media_kind", "")),
        media_readable=bool(case.get("media_readable", True)),
        is_forward=bool(case.get("is_forward", False)),
        ambient_relevance=float(case.get("ambient_relevance", 0.0)),
        upstream=upstream,
        recent_messages=recent,
        bot_replies=bot_replies,
        sticker_history=sticker_history,
        group_memes=memes,
        sensitive_flags=flags,
        local_hour=int(case.get("local_hour", 12)),
        voice_requested=bool(case.get("voice_requested", False)),
    )
