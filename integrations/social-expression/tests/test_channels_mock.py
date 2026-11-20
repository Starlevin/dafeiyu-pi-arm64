from __future__ import annotations

import unittest

from social_expression.channels import (
    OutboundError,
    TargetGuard,
    build_qq_group_message,
    build_wechat_message,
    check_receipt,
)
from social_expression.mock import MockOneBot, MockWeChatBridge

GROUP = 100000001
CHAT = "测试群(5)"


class MockOneBotTests(unittest.TestCase):
    def test_exact_group_and_at_target(self) -> None:
        payload = build_qq_group_message(
            expected_group_id=GROUP, actual_group_id=GROUP, text="在呢，下午好",
            max_chars=34, mentioned_bot=True, at_qq="10001",
        )
        recorder = MockOneBot(expected_group_id=GROUP)
        result = recorder.deliver(payload)
        self.assertTrue(result["ok"], result)
        self.assertEqual(GROUP, payload["params"]["group_id"])
        self.assertEqual("at", payload["params"]["message"][0]["type"])
        self.assertEqual("10001", payload["params"]["message"][0]["data"]["qq"])

    def test_wrong_group_is_rejected_before_send(self) -> None:
        with self.assertRaises(OutboundError) as raised:
            build_qq_group_message(
                expected_group_id=GROUP, actual_group_id=GROUP - 1, text="在呢",
                max_chars=34, mentioned_bot=True, at_qq="10001",
            )
        self.assertEqual("target_mismatch", raised.exception.reason)

    def test_missing_at_target_is_rejected(self) -> None:
        with self.assertRaises(OutboundError) as raised:
            build_qq_group_message(
                expected_group_id=GROUP, actual_group_id=GROUP, text="在呢",
                max_chars=34, mentioned_bot=True, at_qq="",
            )
        self.assertEqual("missing_or_invalid_at_target", raised.exception.reason)

    def test_sticker_uses_opaque_id_and_visible_receipt(self) -> None:
        payload = build_qq_group_message(
            expected_group_id=GROUP, actual_group_id=GROUP, text="恭喜",
            max_chars=34, mentioned_bot=True, at_qq="10001", sticker_id="st_joy_01",
        )
        recorder = MockOneBot(expected_group_id=GROUP, sticker_visible=True)
        result = recorder.deliver(payload)
        self.assertTrue(result["ok"], result)
        image = [segment for segment in payload["params"]["message"] if segment["type"] == "image"][0]
        self.assertEqual("sticker-id:st_joy_01", image["data"]["file"])
        self.assertTrue(result["delivered"])

    def test_unconfirmed_sticker_is_not_delivery(self) -> None:
        payload = build_qq_group_message(
            expected_group_id=GROUP, actual_group_id=GROUP, text="恭喜",
            max_chars=34, mentioned_bot=True, at_qq="10001", sticker_id="st_joy_01",
        )
        recorder = MockOneBot(expected_group_id=GROUP, sticker_visible=False)
        result = recorder.deliver(payload)
        self.assertTrue(result["delivered"])
        self.assertFalse(result["sticker_delivered"])
        self.assertEqual("unconfirmed", result["receipt"])

    def test_url_and_cq_code_are_rejected(self) -> None:
        for text in ("看这个 https://example.com", "发[CQ:at,qq=1] 给我"):
            with self.assertRaises(OutboundError) as raised:
                build_qq_group_message(
                    expected_group_id=GROUP, actual_group_id=GROUP, text=text,
                    max_chars=34, mentioned_bot=True, at_qq="10001",
                )
            self.assertEqual("unsafe_text", raised.exception.reason)

    def test_over_budget_text_is_rejected(self) -> None:
        with self.assertRaises(OutboundError) as raised:
            build_qq_group_message(
                expected_group_id=GROUP, actual_group_id=GROUP, text="这" * 60,
                max_chars=34, mentioned_bot=True, at_qq="10001",
            )
        self.assertEqual("text_over_budget", raised.exception.reason)

    def test_invalid_reply_id_is_rejected(self) -> None:
        with self.assertRaises(OutboundError) as raised:
            build_qq_group_message(
                expected_group_id=GROUP, actual_group_id=GROUP, text="在呢",
                max_chars=34, mentioned_bot=True, at_qq="10001", reply_to="bad id!",
            )
        self.assertEqual("invalid_reply_id", raised.exception.reason)


class MockWeChatBridgeTests(unittest.TestCase):
    def test_exact_chat_and_mention(self) -> None:
        payload = build_wechat_message(
            expected_chat=CHAT, actual_chat="测试群（5）", text="早上好",
            max_chars=34, mentioned=True, at_name="大肥鱼",
        )
        recorder = MockWeChatBridge(expected_chat=CHAT)
        result = recorder.deliver(payload)
        self.assertTrue(result["ok"], result)
        self.assertEqual("测试群(5)", payload["messages"][0]["chat"])
        self.assertEqual(["大肥鱼"], payload["messages"][0]["mentions"])

    def test_wrong_chat_is_rejected(self) -> None:
        with self.assertRaises(OutboundError) as raised:
            build_wechat_message(
                expected_chat=CHAT, actual_chat="测试群(4)", text="早上好",
                max_chars=34, mentioned=False, at_name=None,
            )
        self.assertEqual("target_mismatch", raised.exception.reason)

    def test_sticker_receipt_confirmed(self) -> None:
        payload = build_wechat_message(
            expected_chat=CHAT, actual_chat=CHAT, text="恭喜",
            max_chars=34, mentioned=True, at_name="大肥鱼", sticker_id="st_joy_01",
        )
        recorder = MockWeChatBridge(expected_chat=CHAT, sticker_visible=True)
        result = recorder.deliver(payload)
        self.assertTrue(result["ok"], result)
        self.assertTrue(result["sticker"])
        self.assertTrue(result["delivered"])

    def test_unconfirmed_sticker_is_not_delivery(self) -> None:
        payload = build_wechat_message(
            expected_chat=CHAT, actual_chat=CHAT, text="恭喜",
            max_chars=34, mentioned=True, at_name="大肥鱼", sticker_id="st_joy_01",
        )
        recorder = MockWeChatBridge(expected_chat=CHAT, sticker_visible=False)
        result = recorder.deliver(payload)
        self.assertTrue(result["delivered"])
        self.assertFalse(result["sticker_delivered"])

    def test_missing_at_name_is_rejected(self) -> None:
        with self.assertRaises(OutboundError) as raised:
            build_wechat_message(
                expected_chat=CHAT, actual_chat=CHAT, text="早上好",
                max_chars=34, mentioned=True, at_name="",
            )
        self.assertEqual("missing_or_invalid_at_target", raised.exception.reason)

    def test_unsafe_or_empty_text_is_rejected(self) -> None:
        with self.assertRaises(OutboundError) as raised:
            build_wechat_message(
                expected_chat=CHAT, actual_chat=CHAT, text="file:///etc/passwd",
                max_chars=34, mentioned=False, at_name=None,
            )
        self.assertEqual("unsafe_text", raised.exception.reason)
        with self.assertRaises(OutboundError) as raised:
            build_wechat_message(
                expected_chat=CHAT, actual_chat=CHAT, text="   ",
                max_chars=34, mentioned=False, at_name=None,
            )
        self.assertEqual("empty_text", raised.exception.reason)


class ReceiptAndGuardTests(unittest.TestCase):
    def test_accepted_is_not_delivery(self) -> None:
        self.assertFalse(check_receipt("text", "accepted"))
        self.assertTrue(check_receipt("text", "confirmed"))

    def test_sticker_requires_visible_outgoing(self) -> None:
        self.assertFalse(check_receipt("sticker", "confirmed", visible_outgoing=False))
        self.assertTrue(check_receipt("sticker", "visible_outgoing", visible_outgoing=True))

    def test_target_guard_normalizes_width_only(self) -> None:
        guard = TargetGuard(platform="wechat", scope="group", expected=CHAT)
        self.assertEqual("测试群(5)", guard.assert_match("测试群（5）"))

    def test_target_guard_rejects_other_chat(self) -> None:
        guard = TargetGuard(platform="wechat", scope="group", expected=CHAT)
        with self.assertRaises(OutboundError) as raised:
            guard.assert_match("测试群(4)")
        self.assertEqual("target_mismatch", raised.exception.reason)


if __name__ == "__main__":
    unittest.main()
