from __future__ import annotations

import re
import unicodedata
from collections import deque
from dataclasses import dataclass, field
from typing import Deque, Dict, List, Optional, Sequence, Tuple

_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")
_FORBIDDEN_TEXT = re.compile(r"(?i)(\[CQ:|file://|base64://|sk-[a-z0-9]{12,}|<\|im_start\|>|system prompt)")
_URL = re.compile(r"(?i)https?://")


class OutboundError(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def normalize_target(text: str) -> str:
    """NFKC + whitespace normalization only.

    Deliberately stricter than pi_bridge's norm_title: we do not strip a trailing
    member count, because two different groups can share the same base name
    (e.g. 测试群(4) vs 测试群(5)). The adapter may pass a suffix-less title only when
    tx_guard's UI identity proof already pins the target.
    """
    return unicodedata.normalize("NFKC", (text or "")).strip()


def validate_text(text: str, max_chars: int) -> str:
    value = (text or "").strip()
    if not value:
        raise OutboundError("empty_text")
    if len(value) > max_chars + 8:
        raise OutboundError("text_over_budget")
    if _CONTROL.search(value) or _FORBIDDEN_TEXT.search(value) or _URL.search(value):
        raise OutboundError("unsafe_text")
    return value


def verify_target(expected: str, actual: str) -> str:
    if not expected or not actual:
        raise OutboundError("empty_target")
    if normalize_target(expected) != normalize_target(actual):
        raise OutboundError("target_mismatch")
    return normalize_target(actual)


def verify_qq_group(expected_group_id: int, actual_group_id: int) -> int:
    if isinstance(expected_group_id, bool) or isinstance(actual_group_id, bool):
        raise OutboundError("invalid_group_id")
    try:
        expected = int(expected_group_id)
        actual = int(actual_group_id)
    except (TypeError, ValueError):
        raise OutboundError("invalid_group_id")
    if expected <= 0 or actual <= 0:
        raise OutboundError("invalid_group_id")
    if expected != actual:
        raise OutboundError("target_mismatch")
    return actual


def verify_at_target(mentioned: bool, at_qq: Optional[str]) -> Optional[str]:
    if not mentioned:
        return None
    value = str(at_qq or "").strip()
    if not value.isdigit() or not 5 <= len(value) <= 12:
        raise OutboundError("missing_or_invalid_at_target")
    return value


def check_receipt(kind: str, status: str, *, visible_outgoing: bool = False) -> bool:
    """Only an explicit confirmed/visible outgoing counts; accepted is not delivery."""
    if kind not in ("text", "sticker"):
        raise OutboundError("invalid_receipt_kind")
    status = (status or "").lower()
    if kind == "sticker":
        return visible_outgoing and status in ("confirmed", "visible_outgoing")
    return status in ("confirmed", "visible_outgoing")


def build_qq_group_message(*, expected_group_id: int, actual_group_id: int, text: str, max_chars: int,
                           mentioned_bot: bool, at_qq: Optional[str], reply_to: Optional[str] = None,
                           sticker_id: Optional[str] = None) -> Dict[str, object]:
    group_id = verify_qq_group(expected_group_id, actual_group_id)
    at_target = verify_at_target(mentioned_bot, at_qq)
    body = validate_text(text, max_chars)
    segments: List[Dict[str, object]] = []
    if reply_to:
        if not re.fullmatch(r"[A-Za-z0-9:_-]{1,64}", str(reply_to)):
            raise OutboundError("invalid_reply_id")
        segments.append({"type": "reply", "data": {"id": str(reply_to)}})
    if at_target:
        segments.append({"type": "at", "data": {"qq": at_target}})
    segments.append({"type": "text", "data": {"text": body}})
    if sticker_id:
        if not re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", str(sticker_id)):
            raise OutboundError("invalid_sticker_id")
        # Adapter resolves this opaque id to verified local bytes; never a path in the decision.
        segments.append({"type": "image", "data": {"file": "sticker-id:" + str(sticker_id), "sub_type": 1}})
    return {"action": "send_group_msg", "params": {"group_id": group_id, "message": segments}}


def build_qq_private_message(*, expected_user_id: int, actual_user_id: int, text: str, max_chars: int,
                             reply_to: Optional[str] = None, sticker_id: Optional[str] = None) -> Dict[str, object]:
    user_id = verify_qq_group(expected_user_id, actual_user_id)  # same positive-int exact rule
    body = validate_text(text, max_chars)
    segments: List[Dict[str, object]] = []
    if reply_to:
        if not re.fullmatch(r"[A-Za-z0-9:_-]{1,64}", str(reply_to)):
            raise OutboundError("invalid_reply_id")
        segments.append({"type": "reply", "data": {"id": str(reply_to)}})
    segments.append({"type": "text", "data": {"text": body}})
    if sticker_id:
        if not re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", str(sticker_id)):
            raise OutboundError("invalid_sticker_id")
        segments.append({"type": "image", "data": {"file": "sticker-id:" + str(sticker_id), "sub_type": 1}})
    return {"action": "send_private_msg", "params": {"user_id": user_id, "message": segments}}


def build_wechat_message(*, expected_chat: str, actual_chat: str, text: str, max_chars: int,
                         mentioned: bool, at_name: Optional[str], sticker_id: Optional[str] = None) -> Dict[str, object]:
    chat = verify_target(expected_chat, actual_chat)
    at_target = None
    if mentioned:
        at_target = (at_name or "").strip()
        if not at_target or len(at_target) > 64 or _CONTROL.search(at_target):
            raise OutboundError("missing_or_invalid_at_target")
    body = validate_text(text, max_chars)
    operations: List[Dict[str, object]] = [{
        "action": "send_text",
        "chat": chat,
        "text": body,
        "mentions": [at_target] if at_target else [],
    }]
    if sticker_id:
        if not re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", str(sticker_id)):
            raise OutboundError("invalid_sticker_id")
        operations.append({"action": "send_sticker", "chat": chat, "sticker_id": str(sticker_id)})
    return {"messages": operations}


@dataclass
class TargetGuard:
    """Last-line identity guard used before any UI or socket write."""

    platform: str
    scope: str
    expected: str

    def assert_match(self, actual: str) -> str:
        if self.platform not in ("qq", "wechat"):
            raise OutboundError("invalid_platform")
        if self.scope not in ("group", "private"):
            raise OutboundError("invalid_scope")
        return verify_target(self.expected, actual)


@dataclass
class SendThrottle:
    """Small outbound throttle for bubbles/replies; caller passes a monotonic clock."""

    max_per_window: int = 3
    window_seconds: float = 60.0
    min_gap_seconds: float = 1.0
    _sent: Deque[float] = field(default_factory=deque)

    def __post_init__(self) -> None:
        if self.max_per_window < 1 or self.window_seconds <= 0 or self.min_gap_seconds < 0:
            raise OutboundError("invalid_throttle_config")

    def allow(self, now: float) -> Tuple[bool, str]:
        while self._sent and self._sent[0] <= now - self.window_seconds:
            self._sent.popleft()
        if self._sent and now - self._sent[-1] < self.min_gap_seconds:
            return False, "min_gap"
        if len(self._sent) >= self.max_per_window:
            return False, "window_cap"
        self._sent.append(now)
        return True, "ok"

