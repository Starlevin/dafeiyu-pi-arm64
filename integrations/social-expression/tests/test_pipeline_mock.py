from __future__ import annotations

import unittest

from support import build_context, load_cases

from social_expression import (
    SendThrottle,
    StickerCandidate,
    build_qq_group_message,
    build_qq_private_message,
    build_wechat_message,
    decide,
    select_sticker,
    style_prompt_constraints,
)
from social_expression.mock import MockOneBot, MockWeChatBridge

import json
from pathlib import Path

CATALOG = Path(__file__).resolve().parents[1] / "fixtures" / "sticker_catalog.json"

_STUB = {
    "crisis": "我在，先别一个人扛着，能联系信任的人吗",
    "credential_attack": "这个不能发，咱们聊点别的",
    "serious_help": "先看报错最后一行，把关键信息发我",
    "sad": "抱抱，先缓一缓，我在",
    "tired": "累了就先歇会儿",
    "conflict": "先别气，慢慢说",
    "complaint": "这锅我背，刚卡了一下",
    "celebration": "恭喜！这个真不错",
    "sleep": "晚安，做个好梦",
    "greeting": "在呢，下午好",
    "media_question": "图上是个角色，我看看细节",
    "game_talk": "这版本我还是先观望",
    "banter": "就这？我还没出力呢",
    "forward": "这个我不太方便点",
    "detailed_request": "我分两步说，先看环境再看报错",
    "question": "这个得看具体情况",
    "media_unreadable": "图我没看清，你再说下是哪张",
    "sensitive": "这个我不方便记，咱们聊点别的",
    "chitchat": "收到，先记下了",
}


def stub_for(decision) -> str:
    return _STUB.get(decision.scenario, "收到")


def catalog() -> list:
    payload = json.loads(CATALOG.read_text(encoding="utf-8"))
    return [StickerCandidate(**row) for row in payload["items"]]


class FullPipelineMockTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.cases = load_cases()
        cls.catalog = catalog()

    def test_all_41_cases_route_exactly_or_stay_silent(self) -> None:
        sent = 0
        silent = 0
        for case in self.cases:
            context = build_context(case)
            decision = decide(context)
            if decision.action == "silence":
                silent += 1
                continue
            self.assertIn(decision.scenario, _STUB, case["id"])
            text = stub_for(decision)
            self.assertLessEqual(len(text), decision.max_chars, case["id"])
            self.assertTrue(style_prompt_constraints(context, decision))
            sticker = None
            if decision.sticker_intent != "none":
                sticker = select_sticker(context, decision, self.catalog)
            sticker_id = sticker.item_id if sticker else None
            if context.platform == "qq" and context.scope == "group":
                payload = build_qq_group_message(
                    expected_group_id=int(context.conversation_id),
                    actual_group_id=int(context.conversation_id),
                    text=text, max_chars=decision.max_chars,
                    mentioned_bot=context.mentioned_bot,
                    at_qq=context.sender_id if context.mentioned_bot else None,
                    sticker_id=sticker_id,
                )
                recorder = MockOneBot(expected_group_id=int(context.conversation_id), sticker_visible=True)
            elif context.platform == "qq":
                payload = build_qq_private_message(
                    expected_user_id=int(context.conversation_id),
                    actual_user_id=int(context.conversation_id),
                    text=text, max_chars=decision.max_chars, sticker_id=sticker_id,
                )
                recorder = MockOneBot(expected_user_id=int(context.conversation_id), sticker_visible=True)
            else:
                payload = build_wechat_message(
                    expected_chat=context.conversation_id, actual_chat=context.conversation_id,
                    text=text, max_chars=decision.max_chars,
                    mentioned=context.mentioned_bot,
                    at_name=context.sender_id if context.mentioned_bot else None,
                    sticker_id=sticker_id,
                )
                recorder = MockWeChatBridge(expected_chat=context.conversation_id, sticker_visible=True)
            result = recorder.deliver(payload)
            self.assertTrue(result["ok"], (case["id"], result))
            self.assertTrue(result["delivered"], case["id"])
            if sticker_id:
                self.assertTrue(result["sticker_delivered"], case["id"])
            if decision.scenario == "media_unreadable":
                self.assertIn("没看清", text)
                self.assertNotIn("猫", text)
            if decision.scenario == "sensitive":
                self.assertIsNone(sticker_id)
                for digit in ("13800138000", "3栋"):
                    self.assertNotIn(digit, text)
            sent += 1
        self.assertGreaterEqual(sent, 30)
        self.assertGreaterEqual(silent, 5)

    def test_wrong_group_and_wrong_chat_are_blocked_at_delivery(self) -> None:
        qq_payload = build_qq_group_message(
            expected_group_id=100000001, actual_group_id=100000001, text="在呢",
            max_chars=34, mentioned_bot=True, at_qq="10001",
        )
        qq = MockOneBot(expected_group_id=1109445631)
        self.assertFalse(qq.deliver(qq_payload)["ok"])
        wx_payload = build_wechat_message(
            expected_chat="测试群(5)", actual_chat="测试群(5)", text="在呢",
            max_chars=34, mentioned=False, at_name=None,
        )
        wx = MockWeChatBridge(expected_chat="测试群(4)")
        self.assertFalse(wx.deliver(wx_payload)["ok"])

    def test_send_throttle_caps_burst_and_gap(self) -> None:
        throttle = SendThrottle(max_per_window=3, window_seconds=60, min_gap_seconds=1)
        self.assertEqual((True, "ok"), throttle.allow(0.0))
        self.assertEqual((False, "min_gap"), throttle.allow(0.5))
        self.assertEqual((True, "ok"), throttle.allow(5.0))
        self.assertEqual((True, "ok"), throttle.allow(10.0))
        self.assertEqual((False, "window_cap"), throttle.allow(20.0))
        self.assertEqual((True, "ok"), throttle.allow(70.0))


if __name__ == "__main__":
    unittest.main()
