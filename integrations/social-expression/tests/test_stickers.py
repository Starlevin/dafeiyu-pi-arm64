from __future__ import annotations

import json
import unittest
from dataclasses import replace
from pathlib import Path

from support import ROOT, default_profile

from social_expression import (
    ChatContext,
    StickerCandidate,
    StickerHistory,
    StickerSelector,
    StickerUse,
    decide,
    select_sticker,
)

CATALOG = Path(__file__).resolve().parents[1] / "fixtures" / "sticker_catalog.json"


def load_catalog() -> list:
    payload = json.loads(CATALOG.read_text(encoding="utf-8"))
    return [StickerCandidate(**row) for row in payload["items"]]


def context(text: str, *, platform: str = "qq", scope: str = "group", sender_role: str = "member",
            mentioned: bool = True, sender_id: str = "u1", history: StickerHistory | None = None,
            ambient: float = 0.0) -> ChatContext:
    profile = default_profile(platform, scope)
    return ChatContext(
        platform=platform, scope=scope, conversation_id="100000001" if platform == "qq" else "测试群(5)",
        self_id="bot", sender_id=sender_id, sender_role=sender_role, text=text, profile=profile,
        mentioned_bot=mentioned, ambient_relevance=ambient,
        sticker_history=history or StickerHistory(),
    )


class StickerSelectionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.catalog = load_catalog()

    def test_celebration_selects_original_celebration_item(self) -> None:
        ctx = context("我面试通过了！")
        decision = decide(ctx)
        self.assertEqual("celebration", decision.scenario)
        selection = select_sticker(ctx, decision, self.catalog)
        self.assertIsNotNone(selection)
        self.assertEqual("st_joy_01", selection.item_id)
        self.assertGreater(selection.score, 2.5)

    def test_game_context_beats_generic_laugh(self) -> None:
        ctx = context("第五人格排位，就这？", ambient=0.9)
        decision = decide(ctx)
        self.assertEqual("game_talk", decision.scenario)
        catalog = [item for item in self.catalog if item.id in ("st_context_game", "st_laugh_01")]
        selection = select_sticker(ctx, decision, catalog)
        self.assertIsNotNone(selection)
        self.assertEqual("st_context_game", selection.item_id)

    def test_member_photo_is_never_selected(self) -> None:
        ctx = context("我通过了！")
        decision = decide(ctx)
        catalog = [item for item in self.catalog if item.id == "st_member_photo"]
        self.assertIsNone(select_sticker(ctx, decision, catalog))

    def test_forwarded_or_unlicensed_is_never_selected(self) -> None:
        ctx = context("我通过了！")
        decision = decide(ctx)
        catalog = [item for item in self.catalog if item.id in ("st_forwarded", "st_unlicensed")]
        self.assertIsNone(select_sticker(ctx, decision, catalog))

    def test_group_requires_group_ok_and_public_sharing(self) -> None:
        ctx = context("我通过了！")
        decision = decide(ctx)
        catalog = [item for item in self.catalog if item.id == "st_private_only"]
        self.assertIsNone(select_sticker(ctx, decision, catalog))
        private = context("好累", scope="private", sender_role="peer", mentioned=False)
        private_decision = decide(private)
        self.assertEqual("tired", private_decision.scenario)
        private_catalog = [item for item in self.catalog if item.id == "st_private_only"]
        self.assertIsNotNone(select_sticker(private, private_decision, private_catalog))

    def test_group_public_sharing_false_is_blocked(self) -> None:
        base = [item for item in self.catalog if item.id == "st_joy_01"][0]
        item = replace(base, public_sharing=False)
        ctx = context("我通过了！")
        decision = decide(ctx)
        self.assertIsNone(select_sticker(ctx, decision, [item]))

    def test_licensed_item_without_author_is_blocked(self) -> None:
        base = [item for item in self.catalog if item.id == "st_laugh_01"][0]
        item = replace(base, author="")
        ctx = context("哈哈哈就这", ambient=0.9)
        decision = decide(ctx)
        self.assertIsNone(select_sticker(ctx, decision, [item]))

    def test_sticker_cooldown_blocks_all(self) -> None:
        ctx = context("我通过了！", history=StickerHistory(last_sticker_seconds_ago=30, replies_since_sticker=9))
        decision = decide(ctx)
        self.assertEqual("none", decision.sticker_intent)
        self.assertIsNone(select_sticker(ctx, decision, self.catalog))

    def test_reply_gap_blocks_banter_but_allows_celebration(self) -> None:
        banter = context("哈哈哈就这", ambient=0.9, history=StickerHistory(replies_since_sticker=1))
        banter_decision = decide(banter)
        self.assertIsNone(select_sticker(banter, banter_decision, self.catalog))
        celebration = context("我通过了！", history=StickerHistory(replies_since_sticker=1))
        celebration_decision = decide(celebration)
        self.assertIsNotNone(select_sticker(celebration, celebration_decision, self.catalog))

    def test_item_cooldown_24h(self) -> None:
        ctx = context("我通过了！", history=StickerHistory(
            uses=(StickerUse(sticker_id="st_joy_01", seconds_ago=3600),), replies_since_sticker=9))
        decision = decide(ctx)
        catalog = [item for item in self.catalog if item.id == "st_joy_01"]
        self.assertIsNone(select_sticker(ctx, decision, catalog))

    def test_per_hour_cap(self) -> None:
        ctx = context("我通过了！", history=StickerHistory(
            uses=(StickerUse(sticker_id="st_joy_02", seconds_ago=1800),
                  StickerUse(sticker_id="st_hello_01", seconds_ago=3000)),
            replies_since_sticker=9))
        decision = decide(ctx)
        self.assertIsNone(select_sticker(ctx, decision, self.catalog))

    def test_sender_mirror_guard(self) -> None:
        ctx = context("我通过了！", sender_id="u1", history=StickerHistory(
            uses=(StickerUse(sticker_id="st_joy_01", seconds_ago=600, sender_id="u1"),),
            replies_since_sticker=9))
        decision = decide(ctx)
        catalog = [item for item in self.catalog if item.id == "st_joy_01"]
        self.assertIsNone(select_sticker(ctx, decision, catalog))

    def test_unsafe_wording_blocked(self) -> None:
        ctx = context("哈哈哈就这", ambient=0.9)
        decision = decide(ctx)
        catalog = [item for item in self.catalog if item.id == "st_unsafe"]
        self.assertIsNone(select_sticker(ctx, decision, catalog))

    def test_sticker_none_decision_abstains(self) -> None:
        ctx = context("好难过", scope="private", sender_role="peer", mentioned=False)
        decision = decide(ctx)
        self.assertEqual("sad", decision.scenario)
        self.assertEqual("none", decision.sticker_intent)
        self.assertIsNone(select_sticker(ctx, decision, self.catalog))

    def test_disabled_selector_abstains(self) -> None:
        ctx = context("我通过了！")
        decision = decide(ctx)
        self.assertIsNone(StickerSelector(enabled=False).select(ctx, decision, self.catalog))
        self.assertIsNotNone(StickerSelector(enabled=True).select(ctx, decision, self.catalog))

    def test_selection_contains_no_paths_or_urls(self) -> None:
        ctx = context("我通过了！")
        decision = decide(ctx)
        selection = select_sticker(ctx, decision, self.catalog)
        rendered = repr(selection)
        for forbidden in ("/", "\\", "http", "C:", "file:"):
            self.assertNotIn(forbidden, rendered)

    def test_alternatives_are_bounded_and_exclude_top(self) -> None:
        ctx = context("我通过了！")
        decision = decide(ctx)
        selection = select_sticker(ctx, decision, self.catalog)
        self.assertLessEqual(len(selection.alternatives), 5)
        self.assertNotIn(selection.item_id, [item.id for item in selection.alternatives])


if __name__ == "__main__":
    unittest.main()
