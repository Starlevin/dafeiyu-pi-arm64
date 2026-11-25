"""The visible context keeps its old order and bot boundary with fewer reads."""
import unittest
from unittest.mock import Mock

import bridge_proactive_context as context


class ProactiveWindowTests(unittest.TestCase):
    def test_dense_window_only_reads_directions_for_seven_prior_rows(self):
        rows = [(f"合成消息 {i}", i) for i in range(30)]
        side = Mock(return_value="incoming")
        window, over, ok = context.build_proven(rows, 29, rows[29][0], side)
        self.assertEqual(window, [text for text, _ in rows[-8:]])
        self.assertEqual([c.args[0][1] for c in side.call_args_list], list(range(28, 21, -1)))
        self.assertFalse(over)
        self.assertTrue(ok)

    def test_outgoing_anchor_keeps_newer_context_but_does_not_read_older_rows(self):
        rows = [
            ("旧话题", "old"), ("不用急着开门", "bot"),
            ("[图片]", "media"), ("可以先等队友 https://example.test", "recent"),
            ("先在这里集合", "current"),
        ]
        side = Mock(side_effect=lambda row: "outgoing" if row[1] == "bot" else "incoming")
        window, _, ok = context.build_proven(rows, 4, rows[4][0], side)
        self.assertTrue(ok)
        self.assertEqual(window, ["机器人: 不用急着开门", "可以先等队友 [省略]", "先在这里集合"])
        self.assertEqual([c.args[0][1] for c in side.call_args_list], ["recent", "media", "bot"])

    def test_outgoing_placeholder_or_empty_line_still_cuts_off_prior_topic(self):
        for out, expected in [("[表情]", ["机器人: [表情]"]), ("", [])]:
            with self.subTest(out=out):
                rows = [("旧话题", "old"), (out, "bot"), ("新的消息", "current")]
                window, over, ok = context.build_proven(
                    rows, 2, rows[2][0], lambda row: "outgoing" if row[1] == "bot" else None)
                self.assertEqual(window, [*expected, "新的消息"])
                self.assertFalse(over)
                self.assertTrue(ok)

    def test_sparse_window_preserves_scan_bound_and_later_message_guard(self):
        rows = [("超出回看边界", 0), *[("[图片]", i) for i in range(1, 29)],
                ("当前候选", 29), ("[表情]", 30)]
        side = Mock(side_effect=RuntimeError("synthetic direction read failure"))
        window, over, ok = context.build_proven(rows, 29, rows[29][0], side)
        self.assertEqual(window, ["当前候选"])
        self.assertEqual(side.call_count, 28)
        self.assertTrue(over)
        self.assertTrue(ok)


if __name__ == "__main__":
    unittest.main()
