from __future__ import annotations

import ast
import hashlib
import unittest
from pathlib import Path

from support import make_context

from social_expression import (
    BotReply,
    ContractError,
    MemoryFact,
    OutboundError,
    UpstreamDecision,
    adapt_qq_memory_item,
    adapt_qq_memory_items,
    build_qq_group_message,
    build_wechat_message,
    decide,
    filter_memory_facts,
    from_session_a,
    is_upstream_expired,
    normalize_upstream_action,
    parse_upstream,
    qq_memory_fact,
    qq_memory_reject_reason,
    upstream_allows_send,
)

ROOT = Path(__file__).resolve().parents[1]


def memory_envelope(**overrides):
    now = 1_700_000_000.0
    item = {
        "subject": "OWNER",
        "kind": "preference",
        "text": "喜欢简短回复",
        "keywords": ["简短"],
        "importance": 4,
        "sourceIDs": ["src-1"],
        "updatedAt": now - 60,
        "expiresAt": now + 3600,
        "invalidatedAt": None,
    }
    item.update(overrides.pop("item", {}))
    payload = {"scope_key": "g_alpha", "item": item}
    payload.update(overrides)
    return payload


class SessionAAdapterTests(unittest.TestCase):
    def test_uppercase_speak_wait_skip_become_lowercase(self) -> None:
        for raw, expected in (("SPEAK", "speak"), ("WAIT", "wait"), ("SKIP", "skip")):
            with self.subTest(raw=raw):
                self.assertEqual(expected, normalize_upstream_action(raw))
                decision = parse_upstream({
                    "action": raw, "score": 0.5, "addressed": False,
                    "ambient_relevance": 0.4, "ttl_seconds": 120,
                })
                self.assertEqual(expected, decision.action)

    def test_camel_case_fields_are_mapped(self) -> None:
        decision = parse_upstream({
            "action": "SPEAK",
            "score": 0.82,
            "reason": "owner_question",
            "addressed": True,
            "ambientRelevance": 0.76,
            "ttlSeconds": 90,
        })
        self.assertEqual("speak", decision.action)
        self.assertEqual(0.76, decision.ambient_relevance)
        self.assertEqual(90.0, decision.ttl_seconds)
        self.assertTrue(decision.addressed)

    def test_snake_case_is_accepted_and_wins_when_both_exist(self) -> None:
        decision = parse_upstream({
            "action": "WAIT",
            "ambient_relevance": 0.2,
            "ambientRelevance": 0.9,
            "ttl_seconds": 30,
            "ttlSeconds": 900,
        })
        self.assertEqual("wait", decision.action)
        self.assertEqual(0.2, decision.ambient_relevance)
        self.assertEqual(30.0, decision.ttl_seconds)

    def test_unknown_or_malformed_action_fails_closed(self) -> None:
        for payload in (
            {"action": "SHOUT"},
            {"action": 1},
            {"action": ""},
            {},
        ):
            with self.subTest(payload=payload):
                with self.assertRaises(ContractError):
                    parse_upstream(payload)
                decision = from_session_a(payload)
                self.assertEqual("wait", decision.action)
                self.assertIn("upstream_contract_error", decision.reason)

    def test_non_boolean_addressed_fails_closed(self) -> None:
        self.assertEqual("wait", from_session_a({"action": "SPEAK", "addressed": "true"}).action)
        self.assertEqual("wait", from_session_a({"action": "SPEAK", "addressed": 1}).action)

    def test_ttl_expiry_is_explicit(self) -> None:
        decision = parse_upstream({"action": "SPEAK", "ttl_seconds": 120})
        self.assertFalse(is_upstream_expired(decision, 60))
        self.assertTrue(is_upstream_expired(decision, 121))
        self.assertTrue(is_upstream_expired(parse_upstream({"action": "SPEAK", "ttl_seconds": 0}), 0))
        self.assertFalse(is_upstream_expired(parse_upstream({"action": "WAIT"}), 999))
        self.assertTrue(upstream_allows_send(decision, 30))
        self.assertFalse(upstream_allows_send(parse_upstream({"action": "WAIT"}), 0))

    def test_ttl_out_of_range_fails_closed(self) -> None:
        self.assertEqual("wait", from_session_a({"action": "SPEAK", "ttlSeconds": -1}).action)
        self.assertEqual("wait", from_session_a({"action": "SPEAK", "ttlSeconds": 8 * 86400}).action)

    def test_speak_from_a_overrides_low_relevance(self) -> None:
        context = make_context(
            "随便聊聊",
            ambient=0.1,
            upstream=UpstreamDecision(action="speak", score=0.8, addressed=False, ambient_relevance=0.1),
        )
        self.assertEqual("reply", decide(context).action)

    def test_wait_from_a_is_authoritative_even_when_addressed(self) -> None:
        context = make_context(
            "大肥鱼在吗",
            mentioned=True,
            upstream=UpstreamDecision(action="wait", score=0.1, addressed=True),
        )
        decision = decide(context)
        self.assertEqual("silence", decision.action)
        self.assertIn("upstream_wait", decision.reason_codes)

    def test_speak_still_obeys_hard_floor_gate(self) -> None:
        context = make_context(
            "随便聊聊",
            ambient=0.9,
            bot_replies=(BotReply(text="刚说过", seconds_ago=10),),
            upstream=UpstreamDecision(action="speak", score=0.8, ambient_relevance=0.9),
        )
        decision = decide(context)
        self.assertEqual("silence", decision.action)
        self.assertIn("floor_guard", decision.reason_codes)

    def test_speak_still_obeys_quiet_hours_and_unreadable_media(self) -> None:
        quiet = make_context(
            "随便聊聊", ambient=0.9, local_hour=3,
            upstream=UpstreamDecision(action="speak", score=0.8, ambient_relevance=0.9),
        )
        self.assertEqual("silence", decide(quiet).action)
        self.assertIn("quiet_hours", decide(quiet).reason_codes)

        media = make_context(
            "看看这图", ambient=0.9, has_media=True, media_readable=False,
            upstream=UpstreamDecision(action="speak", score=0.8, ambient_relevance=0.9),
        )
        decision = decide(media)
        self.assertEqual("silence", decision.action)
        self.assertIn("no_grounded_basis", decision.reason_codes)


class SessionBAdapterTests(unittest.TestCase):
    NOW = 1_700_000_000.0

    def fact(self, **overrides):
        return qq_memory_fact(
            memory_envelope(**overrides),
            current_scope_key="g_alpha",
            expected_subject="OWNER",
            now_epoch=self.NOW,
        )

    def test_qq_memory_item_maps_to_bounded_fact(self) -> None:
        fact = self.fact()
        self.assertIsInstance(fact, MemoryFact)
        self.assertLessEqual(len(fact.fact), 160)
        self.assertEqual(0.8, fact.confidence)
        self.assertEqual(3600.0, fact.valid_for_seconds)
        self.assertTrue(fact.citable)
        self.assertLessEqual(len(fact.subject_hash), 32)
        self.assertNotIn("OWNER", fact.subject_hash)

    def test_owner_hash_is_scope_bound(self) -> None:
        alpha = self.fact()
        beta = qq_memory_fact(
            memory_envelope(scope_key="g_beta"),
            current_scope_key="g_beta",
            expected_subject="OWNER",
            now_epoch=self.NOW,
        )
        self.assertNotEqual(alpha.subject_hash, beta.subject_hash)

    def test_scope_mismatch_is_dropped(self) -> None:
        self.assertIsNone(adapt_qq_memory_item(
            memory_envelope(scope_key="g_beta"),
            current_scope_key="g_alpha",
            expected_subject="OWNER",
            now_epoch=self.NOW,
        ))
        self.assertEqual("scope_mismatch", qq_memory_reject_reason(
            memory_envelope(scope_key="g_beta"),
            current_scope_key="g_alpha",
            expected_subject="OWNER",
            now_epoch=self.NOW,
        ))

    def test_wrong_subject_is_dropped(self) -> None:
        self.assertIsNone(adapt_qq_memory_item(
            memory_envelope(item={"subject": "PEER"}),
            current_scope_key="g_alpha",
            expected_subject="OWNER",
            now_epoch=self.NOW,
        ))
        self.assertEqual("wrong_subject", qq_memory_reject_reason(
            memory_envelope(item={"subject": "PEER"}),
            current_scope_key="g_alpha",
            expected_subject="OWNER",
            now_epoch=self.NOW,
        ))

    def test_missing_expiry_is_dropped(self) -> None:
        self.assertIsNone(adapt_qq_memory_item(
            memory_envelope(item={"expiresAt": None}),
            current_scope_key="g_alpha",
            expected_subject="OWNER",
            now_epoch=self.NOW,
        ))
        self.assertEqual("missing_expiry", qq_memory_reject_reason(
            memory_envelope(item={"expiresAt": None}),
            current_scope_key="g_alpha",
            expected_subject="OWNER",
            now_epoch=self.NOW,
        ))

    def test_expired_is_dropped(self) -> None:
        self.assertIsNone(adapt_qq_memory_item(
            memory_envelope(item={"expiresAt": self.NOW - 1}),
            current_scope_key="g_alpha",
            expected_subject="OWNER",
            now_epoch=self.NOW,
        ))
        self.assertEqual("expired", qq_memory_reject_reason(
            memory_envelope(item={"expiresAt": self.NOW - 1}),
            current_scope_key="g_alpha",
            expected_subject="OWNER",
            now_epoch=self.NOW,
        ))

    def test_invalidated_is_dropped(self) -> None:
        self.assertIsNone(adapt_qq_memory_item(
            memory_envelope(item={"invalidatedAt": self.NOW - 10}),
            current_scope_key="g_alpha",
            expected_subject="OWNER",
            now_epoch=self.NOW,
        ))
        self.assertEqual("invalidated", qq_memory_reject_reason(
            memory_envelope(item={"invalidatedAt": self.NOW - 10}),
            current_scope_key="g_alpha",
            expected_subject="OWNER",
            now_epoch=self.NOW,
        ))

    def test_missing_text_or_kind_is_dropped(self) -> None:
        self.assertIsNone(adapt_qq_memory_item(
            memory_envelope(item={"text": ""}),
            current_scope_key="g_alpha",
            expected_subject="OWNER",
            now_epoch=self.NOW,
        ))
        self.assertIsNone(adapt_qq_memory_item(
            memory_envelope(item={"kind": ""}),
            current_scope_key="g_alpha",
            expected_subject="OWNER",
            now_epoch=self.NOW,
        ))

    def test_episode_is_not_citable(self) -> None:
        fact = self.fact(item={"kind": "episode", "sourceIDs": ["src-1"]})
        self.assertFalse(fact.citable)
        self.assertEqual((), filter_memory_facts((fact,)))

    def test_no_source_ids_is_not_citable(self) -> None:
        fact = self.fact(item={"sourceIDs": []})
        self.assertFalse(fact.citable)
        self.assertEqual((), filter_memory_facts((fact,)))

    def test_missing_importance_defaults_to_low_confidence(self) -> None:
        item = memory_envelope()["item"]
        item.pop("importance")
        fact = qq_memory_fact(
            {"scope_key": "g_alpha", "item": item},
            current_scope_key="g_alpha",
            expected_subject="OWNER",
            now_epoch=self.NOW,
        )
        self.assertEqual(0.5, fact.confidence)

    def test_secret_is_redacted(self) -> None:
        fact = self.fact(item={"text": "我的 token 是 abcdef123456，喜欢简短回复"})
        self.assertNotIn("abcdef123456", fact.fact)
        self.assertLessEqual(len(fact.fact), 160)

    def test_batch_filters_invalid_items_only(self) -> None:
        valid = memory_envelope()
        expired = memory_envelope(item={"expiresAt": self.NOW - 1})
        wrong = memory_envelope(item={"subject": "PEER"})
        facts = adapt_qq_memory_items(
            (valid, expired, wrong),
            current_scope_key="g_alpha",
            expected_subject="OWNER",
            now_epoch=self.NOW,
        )
        self.assertEqual(1, len(facts))
        self.assertNotEqual("OWNER", facts[0].subject_hash)


class IsolationAndTargetTests(unittest.TestCase):
    def test_module_does_not_import_network_or_process_libraries(self) -> None:
        forbidden = {"socket", "requests", "websocket", "subprocess", "urllib", "http", "aiohttp"}
        for path in (ROOT / "social_expression").glob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            imports = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    imports.update(alias.name.split(".")[0] for alias in node.names)
                elif isinstance(node, ast.ImportFrom) and node.module:
                    imports.add(node.module.split(".")[0])
            self.assertEqual(set(), imports & forbidden, "%s imports transport/network" % path.name)

    def test_channel_builders_only_return_drafts(self) -> None:
        qq = build_qq_group_message(
            expected_group_id=100000001, actual_group_id=100000001,
            text="短答", max_chars=60, mentioned_bot=True, at_qq="10001",
        )
        wx = build_wechat_message(
            expected_chat="测试群(4)", actual_chat="测试群(4)",
            text="短答", max_chars=70, mentioned=False, at_name=None,
        )
        self.assertIsInstance(qq, dict)
        self.assertIsInstance(wx, dict)
        self.assertNotIn("send", qq)
        self.assertNotIn("send", wx)

    def test_member_suffix_is_not_collapsed(self) -> None:
        with self.assertRaises(OutboundError):
            build_wechat_message(
                expected_chat="测试群(4)", actual_chat="测试群(5)",
                text="短答", max_chars=70, mentioned=False, at_name=None,
            )

    def test_uppercase_action_hash_is_stable(self) -> None:
        first = hashlib.sha256(b"dafeiyu-social-style-c1|g_alpha|OWNER").hexdigest()[:16]
        fact = qq_memory_fact(
            memory_envelope(),
            current_scope_key="g_alpha",
            expected_subject="OWNER",
            now_epoch=1_700_000_000.0,
        )
        self.assertEqual(first, fact.subject_hash)


if __name__ == "__main__":
    unittest.main()
