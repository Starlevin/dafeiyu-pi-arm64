"""Exercise the real header walk without a running AT-SPI session."""
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from test_blocking_state_scan import FakeNode, SHOWING, bridge
import tx_guard


def observe(*children, target="测试群"):
    header = FakeNode("filler", children=children, rect=(200, 0, 400, 36))
    pane = FakeNode("filler", children=[header])
    messages = FakeNode("list", rect=(200, 36, 400, 400))
    atspi = SimpleNamespace(StateType=SimpleNamespace(SHOWING=SHOWING),
                            CoordType=SimpleNamespace(SCREEN="screen"))
    with patch.object(bridge, "Atspi", atspi), \
         patch.object(bridge, "messages_list", return_value=messages), \
         patch.object(bridge, "ancestor", return_value=pane):
        return bridge._tx_header_info(object(), target)


class HeaderProofTests(unittest.TestCase):
    def test_hidden_pages_are_pruned_and_nested_title_counts_once(self):
        hidden_title = FakeNode("label", "测试群")
        hidden = FakeNode("filler", children=[hidden_title], showing=False)
        nested = FakeNode("label", "测试群(5)", children=[FakeNode("label", "测试群")])
        proof = observe(hidden, FakeNode("label", "  "), nested, FakeNode("label", "消息设置"))
        self.assertEqual(proof, {"raw_title": "测试群(5)", "showing_target": 1})
        self.assertEqual(hidden.child_count_calls, 0)
        self.assertEqual(hidden_title.name_calls, 0)

    def test_independent_visible_titles_still_trigger_ambiguity_guard(self):
        proof = observe(
            FakeNode("label", "测试群(5)"),
            FakeNode("filler", children=[FakeNode("label", "测试群（6）")]),
        )
        self.assertEqual(proof, {"raw_title": "测试群(5)", "showing_target": 2})
        signature = dict.fromkeys(tx_guard._REQUIRED_PROOF)
        signature.update(ok=True, app_ok=True, title=proof["raw_title"], **proof)
        self.assertEqual(tx_guard.identity_verdict(signature, "测试群", "before_return").reason,
                         "header_title_ambiguous")

    def test_peers_are_reread_when_visibility_changes(self):
        first = FakeNode("filler", children=[FakeNode("label", "测试群(5)")])
        second = FakeNode("filler", children=[FakeNode("label", "另一个群(5)")], showing=False)
        self.assertEqual(observe(first, second)["showing_target"], 1)
        first.state.showing = False
        second.state.showing = True
        self.assertEqual(observe(first, second), {"raw_title": "另一个群(5)", "showing_target": 0})

    def test_unreadable_visible_branch_never_passes_as_a_unique_title(self):
        unreadable = FakeNode("filler")
        for operation in ("get_state_set", "get_child_count", "get_role_name"):
            with self.subTest(operation=operation), \
                 patch.object(unreadable, operation, side_effect=RuntimeError("fixture read failure")):
                with self.assertRaisesRegex(RuntimeError, "fixture read failure"):
                    observe(FakeNode("label", "测试群"), unreadable)
        with self.assertRaisesRegex(RuntimeError, "visible_tree_child_missing"):
            observe(FakeNode("label", "测试群"), FakeNode("filler", children=[None]))


if __name__ == "__main__":
    unittest.main()
