from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from social_expression import policy, stickers  # noqa: E402

OUT = ROOT / "contracts" / "style_table_v1.json"


def main() -> int:
    payload = {
        "version": "social-style-c1",
        "note": "语言中立决策表，供会话 A / QQ Swift 端口 / 微信 Python 适配器共用；数值与 policy.py 保持一致。",
        "platforms": ["qq", "wechat"],
        "scopes": ["group", "private"],
        "enums": {
            "action": ["reply", "silence"],
            "length": ["short", "medium", "long"],
            "tone": ["neutral", "playful", "gentle", "dry", "celebratory", "sleepy"],
            "humor": ["none", "light", "self_deprecating"],
            "quote": ["none", "reply_to_current", "reference_previous"],
            "sticker_intent": ["none", "optional", "preferred"],
        },
        "profiles": {
            "qq_group": {"max_chars": 60, "max_bubbles": 1, "short_budget": 34, "medium_budget": 58},
            "qq_private": {"max_chars": 60, "max_bubbles": 1, "short_budget": 34, "medium_budget": 58},
            "wechat_group": {"max_chars": 70, "max_bubbles": 2, "short_budget": 34, "medium_budget": 58},
            "wechat_private": {"max_chars": 140, "max_bubbles": 2, "short_budget": 34, "medium_budget": 58},
        },
        "scenes": policy._BASE_TABLE,
        "thresholds": {
            "ambient_relevance": policy._AMBIENT_RELEVANCE,
            "quiet_start_hour": policy._QUIET_START,
            "quiet_end_hour": policy._QUIET_END,
            "floor_window_seconds": policy._FLOOR_WINDOW,
            "floor_min_gap_seconds": policy._FLOOR_MIN_GAP,
            "floor_rate_window_seconds": policy._FLOOR_RATE_WINDOW,
            "floor_rate_max": policy._FLOOR_RATE_MAX,
            "repeat_similarity": 0.72,
            "repeat_prompt_similarity": 0.80,
        },
        "sticker_policy": {
            "min_gap_seconds": stickers._MIN_GAP_SECONDS,
            "min_replies": stickers._MIN_REPLIES,
            "max_per_hour": stickers._MAX_PER_HOUR,
            "item_repeat_seconds": stickers._ITEM_REPEAT_SECONDS,
            "sender_repeat_seconds": stickers._SENDER_REPEAT_SECONDS,
            "score_threshold": stickers._SCORE_THRESHOLD,
            "allowed_origins": list(stickers.StickerCandidate.ALLOWED_ORIGINS),
            "blocked_origins": list(stickers.StickerCandidate.BLOCKED_ORIGINS),
        },
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print("wrote", OUT)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
