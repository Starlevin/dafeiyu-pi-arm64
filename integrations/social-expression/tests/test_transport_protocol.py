from __future__ import annotations

import unittest

from support import make_context

from social_expression import (
    UpstreamDecision,
    adapt_qq_memory_item,
    decide,
    from_session_a,
    parse_upstream,
    qq_memory_fact,
    upstream_allows_send,
)
from social_expression.channels import (
    OutboundDeduplicator,
    OutboundError,
    build_qq_group_message,
    build_qq_private_message,
    build_qq_recall,
    build_wechat_message,
    build_wechat_recall,
    normalize_receipt,
    retry_plan,
)
from social_expression.mock import MockOneBot, MockWeChatBridge
from social_expression.runtime_budget import PI3B_BUDGET, plan_optional_layers
from social_expression.version import (
    A_TO_C_VERSION,
    B_TO_C_VERSION,
    CHANNEL_PROTOCOL_VERSION,
    CONTRACT_FROZEN_AT,
    STYLE_CONTRACT_VERSION,
)

GROUP = 100000001
PRIVATE_USER = 10002
CHAT = "测试群(5)"
MESSAGE_ID = "m-42"


class ContractFreezeTests(unittest.TestCase):
    def test_versions_are_explicit_and_stable(self) -> None:
        self.assertEqual("social-style-c1", STYLE_CONTRACT_VERSION)
        self.assertEqual("a2c-wire-v1", A_TO_C_VERSION)
        self.assertEqual("b2c-memory-v1", B_TO_C_VERSION)
        self.assertEqual("c-transport-v1", CHANNEL_PROTOCOL_VERSION)
        self.assertEqual("2026-11-17", CONTRACT_FROZEN_AT)

    def test_wait_and_skip_are_never_upgraded_to_speak(self) -> None:
        for action in ("wait", "skip"):
            with self.subTest(action=action):
                private = make_context(
                    "大肥鱼在吗",
                    scope="private",
                    mentioned=True,
                    upstream=UpstreamDecision(action=action, score=0.99, addressed=True),
                )
                self.assertEqual("silence", decide(private).action)
        group = make_context(
            "大肥鱼在吗",
            mentioned=True,
            upstream=UpstreamDecision(action="skip", score=0.99, addressed=True),
        )
        self.assertEqual("silence", decide(group).action)

    def test_expired_speak_never_allows_send(self) -> None:
        decision = UpstreamDecision(action="speak", score=0.9, ttl_seconds=120)
        self.assertFalse(upstream_allows_send(decision, 121))
        zero_ttl = UpstreamDecision(action="speak", score=0.9, ttl_seconds=0)
        self.assertFalse(upstream_allows_send(zero_ttl, 0))
        self.assertTrue(upstream_allows_send(decision, 60))

    def test_versioned_wire_payloads_parse(self) -> None:
        decision = parse_upstream({
            "action": "SPEAK",
            "contract_version": A_TO_C_VERSION,
            "score": 0.7,
            "ttlSeconds": 90,
        })
        self.assertEqual("speak", decision.action)
        fact = qq_memory_fact(
            {
                "contract_version": B_TO_C_VERSION,
                "scope_key": "g_alpha",
                "item": {
                    "subject": "OWNER",
                    "kind": "preference",
                    "text": "喜欢简短回复",
                    "importance": 4,
                    "sourceIDs": ["src-1"],
                    "expiresAt": 1_700_003_600.0,
                    "invalidatedAt": None,
                },
            },
            current_scope_key="g_alpha",
            expected_subject="OWNER",
            now_epoch=1_700_000_000.0,
        )
        self.assertTrue(fact.citable)

    def test_version_mismatch_fails_closed(self) -> None:
        self.assertEqual("wait", from_session_a({
            "action": "SPEAK",
            "contract_version": "a2c-wire-v2",
        }).action)
        self.assertIsNone(adapt_qq_memory_item(
            {
                "contract_version": "b2c-memory-v2",
                "scope_key": "g_alpha",
                "item": {
                    "subject": "OWNER",
                    "kind": "preference",
                    "text": "喜欢简短回复",
                    "importance": 4,
                    "sourceIDs": ["src-1"],
                    "expiresAt": 1_700_003_600.0,
                    "invalidatedAt": None,
                },
            },
            current_scope_key="g_alpha",
            expected_subject="OWNER",
            now_epoch=1_700_000_000.0,
        ))

    def test_cross_scope_fact_is_not_injected(self) -> None:
        now = 1_700_000_000.0
        envelope = {
            "scope_key": "g_beta",
            "item": {
                "subject": "OWNER",
                "kind": "preference",
                "text": "喜欢简短回复",
                "importance": 4,
                "sourceIDs": ["src-1"],
                "expiresAt": now + 3600,
                "invalidatedAt": None,
            },
        }
        self.assertIsNone(adapt_qq_memory_item(
            envelope,
            current_scope_key="g_alpha",
            expected_subject="OWNER",
            now_epoch=now,
        ))


class QQProtocolTests(unittest.TestCase):
    def payload(self, *, reply_to: str | None = None, text: str = "在呢，下午好") -> dict:
        return build_qq_group_message(
            expected_group_id=GROUP, actual_group_id=GROUP, text=text,
            max_chars=34, mentioned_bot=True, at_qq="10001", reply_to=reply_to,
        )

    def test_reply_and_at_are_ordered_before_text(self) -> None:
        payload = self.payload(reply_to=MESSAGE_ID)
        segments = payload["params"]["message"]
        self.assertEqual(["reply", "at", "text"], [segment["type"] for segment in segments])
        self.assertEqual({"id": MESSAGE_ID}, segments[0]["data"])
        self.assertEqual({"qq": "10001"}, segments[1]["data"])
        self.assertEqual(GROUP, payload["params"]["group_id"])

    def test_private_reply_object_and_exact_user_id(self) -> None:
        payload = build_qq_private_message(
            expected_user_id=PRIVATE_USER, actual_user_id=PRIVATE_USER,
            text="收到", max_chars=34, reply_to=MESSAGE_ID,
        )
        self.assertEqual("send_private_msg", payload["action"])
        self.assertEqual(PRIVATE_USER, payload["params"]["user_id"])
        self.assertEqual("reply", payload["params"]["message"][0]["type"])

    def test_duplicate_payload_is_suppressed_only_inside_window(self) -> None:
        payload = self.payload()
        dedup = OutboundDeduplicator(window_seconds=30)
        self.assertEqual((True, "ok"), dedup.allow(payload, now=100.0))
        self.assertEqual((False, "duplicate_suppressed"), dedup.allow(payload, now=101.0))
        self.assertEqual((True, "ok"), dedup.allow(payload, now=200.0))

    def test_same_text_to_other_target_is_not_a_duplicate(self) -> None:
        dedup = OutboundDeduplicator(window_seconds=30)
        first = self.payload()
        second = build_qq_group_message(
            expected_group_id=GROUP + 1, actual_group_id=GROUP + 1, text="在呢，下午好",
            max_chars=34, mentioned_bot=True, at_qq="10001",
        )
        self.assertEqual((True, "ok"), dedup.allow(first, now=100.0))
        self.assertEqual((True, "ok"), dedup.allow(second, now=101.0))

    def test_recall_payload_is_standard_onebot_shape(self) -> None:
        recall = build_qq_recall(
            expected_group_id=GROUP, actual_group_id=GROUP, message_id=MESSAGE_ID,
        )
        self.assertEqual("delete_msg", recall["action"])
        self.assertEqual({"message_id": MESSAGE_ID}, recall["params"])
        recorder = MockOneBot(expected_group_id=GROUP)
        result = recorder.deliver(recall)
        self.assertTrue(result["ok"], result)
        self.assertEqual(MESSAGE_ID, recorder.recalled[0]["params"]["message_id"])

    def test_recall_rejects_wrong_group_and_bad_message_id(self) -> None:
        with self.assertRaises(OutboundError) as raised:
            build_qq_recall(expected_group_id=GROUP, actual_group_id=GROUP - 1, message_id=MESSAGE_ID)
        self.assertEqual("target_mismatch", raised.exception.reason)
        with self.assertRaises(OutboundError) as raised:
            build_qq_recall(expected_group_id=GROUP, actual_group_id=GROUP, message_id="bad id!")
        self.assertEqual("invalid_message_id", raised.exception.reason)

    def test_receipt_distinguishes_accepted_from_delivered(self) -> None:
        accepted_only = normalize_receipt(
            {"ok": True, "reason": "accepted", "receipt": "accepted", "delivered": False},
            platform="qq", scope="group", target=str(GROUP), kind="text", attempt=1,
        )
        self.assertTrue(accepted_only.accepted)
        self.assertFalse(accepted_only.delivered)
        self.assertFalse(accepted_only.confirmed)

        confirmed = normalize_receipt(
            {"ok": True, "reason": "accepted", "receipt": "confirmed", "delivered": True},
            platform="qq", scope="group", target=str(GROUP), kind="text", attempt=1,
        )
        self.assertTrue(confirmed.confirmed)

    def test_sticker_receipt_requires_visible_outgoing(self) -> None:
        unconfirmed = normalize_receipt(
            {"ok": True, "reason": "accepted", "receipt": "unconfirmed", "delivered": True,
             "sticker_delivered": False},
            platform="qq", scope="group", target=str(GROUP), kind="sticker", attempt=1,
        )
        self.assertFalse(unconfirmed.delivered)
        self.assertFalse(unconfirmed.confirmed)
        self.assertFalse(unconfirmed.visible_outgoing)

    def test_transient_failure_can_retry_but_target_mismatch_cannot(self) -> None:
        transient = normalize_receipt(
            {"ok": False, "reason": "transient_failure", "receipt": "failed", "delivered": False},
            platform="qq", scope="group", target=str(GROUP), kind="text", attempt=1,
        )
        plan = retry_plan(transient, max_attempts=3)
        self.assertTrue(plan["retry"])
        self.assertEqual(2, plan["next_attempt"])

        permanent = normalize_receipt(
            {"ok": False, "reason": "target_mismatch", "receipt": "failed", "delivered": False},
            platform="qq", scope="group", target=str(GROUP), kind="text", attempt=1,
        )
        self.assertFalse(retry_plan(permanent, max_attempts=3)["retry"])

    def test_token_bearing_text_is_rejected_before_payload(self) -> None:
        for text in (
            "Authorization: Bearer abcdefghijklmnop",
            "api_key=abcdef1234567890",
            "password: hunter2hunter2",
            "sk-abcdefghijkl",
        ):
            with self.subTest(text=text):
                with self.assertRaises(OutboundError) as raised:
                    self.payload(text=text)
                self.assertEqual("unsafe_text", raised.exception.reason)

    def test_mock_transient_failure_then_retry_succeeds(self) -> None:
        payload = self.payload()
        recorder = MockOneBot(expected_group_id=GROUP, fail_next=1)
        first = recorder.deliver(payload)
        self.assertFalse(first["ok"])
        first_receipt = normalize_receipt(
            first, platform="qq", scope="group", target=str(GROUP), kind="text", attempt=1,
        )
        self.assertTrue(retry_plan(first_receipt, max_attempts=3)["retry"])

        second = recorder.deliver(payload)
        self.assertTrue(second["ok"], second)
        second_receipt = normalize_receipt(
            second, platform="qq", scope="group", target=str(GROUP), kind="text", attempt=2,
        )
        self.assertTrue(second_receipt.confirmed)
        self.assertFalse(retry_plan(second_receipt, max_attempts=3)["retry"])

    def test_builders_return_drafts_not_transport_calls(self) -> None:
        payload = self.payload()
        self.assertEqual({"action", "params"}, set(payload.keys()))
        self.assertNotIn("send", payload)
        self.assertNotIn("url", payload)


class WeChatProtocolTests(unittest.TestCase):
    def payload(self, *, reply_to: str | None = None, text: str = "早上好") -> dict:
        return build_wechat_message(
            expected_chat=CHAT, actual_chat=CHAT, text=text, max_chars=34,
            mentioned=True, at_name="大肥鱼", reply_to=reply_to,
        )

    def test_mention_and_reply_object_are_in_one_text_operation(self) -> None:
        payload = self.payload(reply_to=MESSAGE_ID)
        operation = payload["messages"][0]
        self.assertEqual(["大肥鱼"], operation["mentions"])
        self.assertEqual(MESSAGE_ID, operation["reply_to"])
        self.assertEqual(CHAT, operation["chat"])

    def test_duplicate_payload_is_suppressed_only_inside_window(self) -> None:
        payload = self.payload()
        dedup = OutboundDeduplicator(window_seconds=20)
        self.assertEqual((True, "ok"), dedup.allow(payload, now=10.0))
        self.assertEqual((False, "duplicate_suppressed"), dedup.allow(payload, now=15.0))
        self.assertEqual((True, "ok"), dedup.allow(payload, now=40.0))

    def test_recall_payload_and_mock(self) -> None:
        recall = build_wechat_recall(
            expected_chat=CHAT, actual_chat="测试群（5）", message_id=MESSAGE_ID,
        )
        operation = recall["messages"][0]
        self.assertEqual("recall_message", operation["action"])
        self.assertEqual(CHAT, operation["chat"])
        self.assertEqual(MESSAGE_ID, operation["message_id"])
        recorder = MockWeChatBridge(expected_chat=CHAT)
        result = recorder.deliver(recall)
        self.assertTrue(result["ok"], result)
        self.assertEqual(MESSAGE_ID, recorder.recalled[0]["messages"][0]["message_id"])

    def test_recall_rejects_wrong_chat(self) -> None:
        with self.assertRaises(OutboundError) as raised:
            build_wechat_recall(expected_chat=CHAT, actual_chat="测试群(4)", message_id=MESSAGE_ID)
        self.assertEqual("target_mismatch", raised.exception.reason)

    def test_sticker_visible_receipt_is_confirmed(self) -> None:
        receipt = normalize_receipt(
            {"ok": True, "reason": "accepted", "receipt": "visible_outgoing",
             "delivered": True, "sticker_delivered": True},
            platform="wechat", scope="group", target=CHAT, kind="sticker", attempt=1,
        )
        self.assertTrue(receipt.delivered)
        self.assertTrue(receipt.confirmed)
        self.assertTrue(receipt.visible_outgoing)

    def test_unconfirmed_sticker_retries_sticker_only(self) -> None:
        receipt = normalize_receipt(
            {"ok": True, "reason": "accepted", "receipt": "unconfirmed",
             "delivered": True, "sticker_delivered": False},
            platform="wechat", scope="group", target=CHAT, kind="sticker", attempt=1,
        )
        plan = retry_plan(receipt, max_attempts=3)
        self.assertTrue(plan["retry"])
        self.assertEqual("retry_sticker_only", plan["reason"])

    def test_token_bearing_text_is_rejected_before_payload(self) -> None:
        with self.assertRaises(OutboundError) as raised:
            self.payload(text="Authorization: Bearer abcdefghijklmnop")
        self.assertEqual("unsafe_text", raised.exception.reason)

    def test_builders_return_drafts_not_transport_calls(self) -> None:
        payload = self.payload()
        self.assertEqual(["messages"], list(payload.keys()))
        self.assertNotIn("send", payload)
        self.assertNotIn("url", payload)


class BudgetFallbackTests(unittest.TestCase):
    def test_within_budget_keeps_optional_layers(self) -> None:
        plan = plan_optional_layers(
            catalog_size=32, gif_candidates=4,
            requested_max_chars=70, requested_bubbles=2,
        )
        self.assertTrue(plan.style)
        self.assertTrue(plan.sticker)
        self.assertTrue(plan.gif)
        self.assertEqual((), plan.reason_codes)

    def test_oversized_catalog_falls_back_to_legacy_sticker(self) -> None:
        plan = plan_optional_layers(catalog_size=PI3B_BUDGET.max_catalog_items + 1)
        self.assertTrue(plan.style)
        self.assertFalse(plan.sticker)
        self.assertIn("sticker_catalog_over_budget", plan.reason_codes)

    def test_oversized_gif_set_falls_back_to_static(self) -> None:
        plan = plan_optional_layers(catalog_size=8, gif_candidates=PI3B_BUDGET.max_gif_items + 1)
        self.assertFalse(plan.gif)
        self.assertIn("gif_candidates_over_budget", plan.reason_codes)

    def test_overlong_style_request_falls_back_to_existing_persona(self) -> None:
        plan = plan_optional_layers(
            catalog_size=8, gif_candidates=0,
            requested_max_chars=PI3B_BUDGET.max_style_chars + 1,
            requested_bubbles=2,
        )
        self.assertFalse(plan.style)
        self.assertIn("style_over_budget", plan.reason_codes)

    def test_negative_counts_are_rejected(self) -> None:
        with self.assertRaises(ValueError):
            plan_optional_layers(catalog_size=-1)
        with self.assertRaises(ValueError):
            plan_optional_layers(catalog_size=0, gif_candidates=-1)


if __name__ == "__main__":
    unittest.main()
