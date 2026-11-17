from __future__ import annotations

import os
from typing import Dict

STYLE_ENV = "DAFEIYU_SOCIAL_STYLE"
STICKER_ENV = "DAFEIYU_STICKER_RERANK"
SHADOW_ENV = "DAFEIYU_SOCIAL_STYLE_SHADOW"

_TRUE = ("1", "true", "yes", "on")
_FALSE = ("0", "false", "no", "off", "")


def _flag(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    normalized = value.strip().lower()
    if normalized in _TRUE:
        return True
    if normalized in _FALSE:
        return False
    return default


def style_enabled() -> bool:
    """Default off; the existing persona path stays untouched until explicitly enabled."""
    return _flag(STYLE_ENV, False)


def sticker_rerank_enabled() -> bool:
    """Default off; on failure the caller must fall back to QQStickerLibrary.available()."""
    return _flag(STICKER_ENV, False)


def shadow_enabled() -> bool:
    """Default off: compute/log decisions only when explicitly enabled."""
    return _flag(SHADOW_ENV, False)


def describe() -> Dict[str, bool]:
    return {
        "style_enabled": style_enabled(),
        "sticker_rerank_enabled": sticker_rerank_enabled(),
        "shadow_enabled": shadow_enabled(),
    }
