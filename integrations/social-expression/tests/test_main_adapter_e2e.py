from __future__ import annotations

import math
import statistics
import time
import unittest

from social_expression import (
    ChatContext,
    OutboundError,
    StyleProfile,
    UpstreamDecision,
    build_qq_group_message,
    check_receipt,
    decide,
    from_a_decision,
    memory_facts,
)
from social_expression.mock import MockOneBot

GROUP = 1109445632
OTHER_GROUP = 961666830
OTHER_UNKNOWN = 1079430113
NOW = 1_700_000_000.0


def qq_context(index: int, upstream: UpstreamDecision, *, mentioned: bool = True) -> ChatContext:
    return ChatContext(
        platform="qq",
        scope="group",
        conversation_id=str(GROUP),
        self_id="bot",
        sender_id="u%d" % index,
        sender_role="member",
        text="@大肥鱼 第%d条：今天打什么" % index,
        profile=StyleProfile(platform="qq", max_chars=60, max_bubbles=1),
        mentioned_bot=mentioned,
        ambient_relevance=0.1,
        upstream=upstream,
    )


def envelope(scope: str, *, subject: str = "OWNER", kind: str = "fact",
             text: str = "喜欢简短回复", expires_at: float = NOW + 3600,
             invalidated_at: float | None = None, source_ids: list[str] | None = None) -> dict:
    return {
        "scope_key": scope,
        "item": {
            "subject": subject,
            "kind": kind,
            "text": text,
            "keywords": ["简短"],
            "importance": 3,
            "sourceIDs": ["src-1"] if source_ids is None else source_ids,
            "updatedAt": NOW - 60,
            "expiresAt": expires_at,
            "invalidatedAt": invalidated_at,
        },
    }


class MainAdapterAtoCTests(unittest.TestCase):
    def test_self_mention_is_addressed_and_speak_survives_low_relevance(self) -> None:
        decision = from_a_decision(
            {"action": "SPEAK", "score": 0.86, "reason": "self_mention", "ttlSeconds": 120},
            scope="group", mention="self", topic_relevance=0.05,
        )
        self.assertEqual("speak", decision.action)
        self.assertTrue(decision.addressed)
        self.assertEqual(0.05, decision.ambient_relevance)
        self.assertEqual("reply", decide(qq_context(1, decision)).action)

    def test_private_scope_is_addressed_even_without_mention(self) -> None:
        decision = from_a_decision(
            {"action": "SPEAK", "score": 0.7, "ttlSeconds": 60},
            scope="private", mention="none", topic_relevance=0.0,
        )
        self.assertTrue(decision.addressed)

    def test_bare_reply_target_without_verified_bot_proof_is_not_addressed(self) -> None:
        decision = from_a_decision(
            {"action": "SPEAK", "score": 0.7, "ttlSeconds": 60},
            scope="group", mention="reply_to_bot",
            reply_to_bot_verified=False, topic_relevance=0.2,
        )
        self.assertFalse(decision.addressed)

    def test_verified_reply_target_is_addressed(self) -> None:
        decision = from_a_decision(
            {"action": "SPEAK", "score": 0.7, "ttlSeconds": 60},
            scope="group", mention="reply_to_bot",
            reply_to_bot_verified=True, topic_relevance=0.2,
        )
        self.assertTrue(decision.addressed)

    def test_expired_ttl_becomes_wait(self) -> None:
        decision = from_a_decision(
            {"action": "SPEAK", "score": 0.8, "ttlSeconds": 60},
            scope="group", mention="self", age_seconds=61,
        )
        self.assertEqual("wait", decision.action)
        self.assertEqual("upstream_ttl_expired", decision.reason)
        self.assertFalse(decision.addressed)

    def test_unknown_action_and_bad_scores_fail_closed(self) -> None:
        bad = from_a_decision({"action": "SHOUT", "score": 2.0}, scope="group", mention="self")
        self.assertEqual("wait", bad.action)
        self.assertIn("upstream_contract_error", bad.reason)

    def test_wait_and_skip_are_authoritative(self) -> None:
        for action in ("WAIT", "SKIP"):
            with self.subTest(action=action):
                decision = from_a_decision(
                    {"action": action, "score": 0.2, "ttlSeconds": 120},
                    scope="group", mention="self",
                )
                self.assertEqual(action.lower(), decision.action)
                self.assertEqual("silence", decide(qq_context(2, decision)).action)


class MainAdapterBtoCTests(unittest.TestCase):
    def test_preference_is_citable_with_source_ids(self) -> None:
        facts = memory_facts(
            [envelope("g_alpha", kind="preference")],
            current_scope_key="g_alpha", expected_subject="OWNER", now_epoch=NOW,
        )
        self.assertEqual(1, len(facts))
        self.assertTrue(facts[0].citable)
        self.assertLessEqual(len(facts[0].fact), 160)
        self.assertNotIn("OWNER", facts[0].subject_hash)

    def test_fact_kind_is_not_citable(self) -> None:
        facts = memory_facts(
            [envelope("g_alpha", kind="fact")],
            current_scope_key="g_alpha", expected_subject="OWNER", now_epoch=NOW,
        )
        self.assertEqual(1, len(facts))
        self.assertFalse(facts[0].citable)

    def test_wrong_scope_and_subject_are_dropped(self) -> None:
        for payload in (
            envelope("g_beta"),
            envelope("g_alpha", subject="PEER"),
        ):
            with self.subTest(scope=payload["scope_key"]):
                self.assertEqual((), memory_facts(
                    [payload], current_scope_key="g_alpha",
                    expected_subject="OWNER", now_epoch=NOW,
                ))

    def test_expired_and_invalidated_are_dropped(self) -> None:
        for payload in (
            envelope("g_alpha", expires_at=NOW - 1),
            envelope("g_alpha", invalidated_at=NOW - 5),
        ):
            with self.subTest(payload=payload):
                self.assertEqual((), memory_facts(
                    [payload], current_scope_key="g_alpha",
                    expected_subject="OWNER", now_epoch=NOW,
                ))

    def test_injection_is_capped_at_three(self) -> None:
        payloads = [envelope("g_alpha", text="事实%d" % i) for i in range(6)]
        facts = memory_facts(
            payloads, current_scope_key="g_alpha",
            expected_subject="OWNER", now_epoch=NOW, limit=6,
        )
        self.assertEqual(3, len(facts))

    def test_episode_is_not_citable(self) -> None:
        facts = memory_facts(
            [envelope("g_alpha", kind="episode")],
            current_scope_key="g_alpha", expected_subject="OWNER", now_epoch=NOW,
        )
        self.assertEqual(1, len(facts))
        self.assertFalse(facts[0].citable)

    def test_cross_scope_batch_leaks_zero_identifiers(self) -> None:
        payloads = [envelope("scope-%d" % i) for i in range(12)]
        facts = memory_facts(
            payloads, current_scope_key="scope-0",
            expected_subject="OWNER", now_epoch=NOW,
        )
        self.assertEqual(1, len(facts))
        self.assertNotIn("OWNER", facts[0].subject_hash)
        self.assertNotIn("scope-", facts[0].subject_hash)


class MainMockEndToEndTests(unittest.TestCase):
    def test_thirty_speak_cases_send_to_exact_authorized_group(self) -> None:
        latencies = []
        mock = MockOneBot(expected_group_id=GROUP)
        for index in range(30):
            with self.subTest(index=index):
                decision = from_a_decision(
                    {"action": "SPEAK", "score": 0.8, "reason": "authorized_self_mention", "ttlSeconds": 120},
                    scope="group", mention="self", topic_relevance=0.1,
                )
                started = time.perf_counter()
                style = decide(qq_context(index, decision))
                self.assertEqual("reply", style.action)
                payload = build_qq_group_message(
                    expected_group_id=GROUP, actual_group_id=GROUP,
                    text="短答%d" % index, max_chars=style.max_chars,
                    mentioned_bot=True, at_qq="10001",
                )
                receipt = mock.deliver(payload)
                latencies.append((time.perf_counter() - started) * 1000.0)
                self.assertEqual("confirmed", receipt["receipt"])
                self.assertTrue(check_receipt("text", receipt["receipt"]))
        self.assertEqual(30, len(mock.sent))
        self.assertEqual([], mock.rejected)
        self.assertGreaterEqual(min(latencies), 0.0)
        p50 = statistics.median(latencies)
        p95 = sorted(latencies)[max(0, math.ceil(0.95 * len(latencies)) - 1)]
        print("MAIN_ADAPTER_MOCK_METRICS cases=30 sent=%d rejected=%d p50_ms=%.3f p95_ms=%.3f" % (
            len(mock.sent), len(mock.rejected), p50, p95,
        ))

    def test_forged_wrong_group_is_rejected_by_mock_tx_guard(self) -> None:
        for wrong in (OTHER_GROUP, OTHER_UNKNOWN, 648418715):
            with self.subTest(wrong=wrong):
                mock = MockOneBot(expected_group_id=GROUP)
                payload = {
                    "action": "send_group_msg",
                    "params": {
                        "group_id": wrong,
                        "message": [{"type": "text", "data": {"text": "不应发出"}}],
                    },
                }
                receipt = mock.deliver(payload)
                self.assertFalse(receipt["ok"])
                self.assertEqual("wrong_group", receipt["reason"])
                self.assertEqual([], mock.sent)

    def test_channel_builder_blocks_wrong_group_before_mock(self) -> None:
        rejected = 0
        for actual in (OTHER_GROUP, OTHER_UNKNOWN, 648418715):
            with self.subTest(actual=actual):
                with self.assertRaises(OutboundError):
                    build_qq_group_message(
                        expected_group_id=GROUP, actual_group_id=actual,
                        text="不应发出", max_chars=60,
                        mentioned_bot=False, at_qq=None,
                    )
                rejected += 1
        self.assertEqual(3, rejected)


if __name__ == "__main__":
    unittest.main()
