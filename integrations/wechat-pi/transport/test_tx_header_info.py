"""Exercise the real header walk without a running AT-SPI session."""
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

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


class ComposerProofTests(unittest.TestCase):
    def test_each_guard_pass_shares_one_view_and_requires_the_original_composer(self):
        app, message_list = object(), object()
        rect = (0, 0, 600, 100)
        reader = SimpleNamespace(get_character_count=Mock(return_value=0))
        node = SimpleNamespace(get_text_iface=lambda: reader, get_editable_text_iface=lambda: object())
        replacement = SimpleNamespace(get_text_iface=node.get_text_iface,
                                      get_editable_text_iface=node.get_editable_text_iface, replacement=True)
        root = {}
        atspi = SimpleNamespace(Text=SimpleNamespace(get_text=Mock(return_value="")))
        with patch.multiple(bridge, Atspi=atspi, get_app=Mock(return_value=app),
                            messages_list=Mock(return_value=message_list), ext=Mock(return_value=rect),
                            _tx_header_info=Mock(return_value={"raw_title": "测试群", "showing_target": 1}),
                            _tx_chats_info=Mock(return_value=(1, ["测试群"], 1)),
                            composer=Mock(side_effect=[(node, rect), (node, rect), (replacement, rect)]),
                            _tx_composer_fp=Mock(side_effect=[("text", "", rect), ("text", "renamed", rect)]),
                            blocking_state=Mock(return_value=None)):
            proof = bridge._tx_observe("测试群", root)
            self.assertTrue(proof["ok"])
            self.assertIs(root["reader"], reader)
            bridge.messages_list.assert_called_once_with(app)
            bridge.ext.assert_called_once_with(message_list)
            bridge._tx_header_info.assert_called_once_with(app, "测试群", (message_list, rect))
            bridge.composer.assert_called_once_with(app, (message_list, rect))
            atspi.Text.get_text.return_value = "我的草稿"
            reader.get_character_count.return_value = 4
            changed_label = bridge._tx_observe("测试群", root)
            self.assertTrue(tx_guard.may_clear_draft(changed_label, "测试群", "我的草稿", root["fp"]).allow)
            self.assertFalse(tx_guard.may_clear_draft(changed_label, "测试群", "另一份草稿", root["fp"]).allow)
            updated = bridge._tx_observe("测试群", root)
        self.assertEqual(updated["reason"], "composer_replaced")

    def test_only_owned_draft_can_grow_the_same_composer_upward(self):
        app, message_list = object(), object()
        original_rect = (200, 520, 600, 100)
        wrapped_rect = (200, 490, 600, 130)
        current_rect = original_rect
        draft = ""
        reader = SimpleNamespace(get_character_count=lambda: len(draft))
        node = SimpleNamespace(get_text_iface=lambda: reader, get_editable_text_iface=lambda: object())
        root = {}
        atspi = SimpleNamespace(Text=SimpleNamespace(get_text=lambda *_: draft))
        owned = "本轮长回复\n第二行"
        with patch.multiple(bridge, Atspi=atspi, get_app=Mock(return_value=app),
                            messages_list=Mock(return_value=message_list), ext=Mock(return_value=original_rect),
                            _tx_header_info=Mock(return_value={"raw_title": "测试群(5)", "showing_target": 1}),
                            _tx_chats_info=Mock(return_value=(1, ["测试群"], 1)),
                            composer=lambda *_: (node, current_rect),
                            _tx_composer_fp=lambda _: ("text", draft, current_rect),
                            blocking_state=Mock(return_value=None)):
            guard = tx_guard.TxGuard("测试群", lambda: bridge._tx_observe("测试群", root),
                                     samples=2)
            self.assertTrue(guard.before_send().allow)
            current_rect = wrapped_rect
            draft = owned
            self.assertEqual(guard.before_return(owned).reason, "composer_layout_changed")
            root["owned_draft"] = owned
            self.assertTrue(guard.before_return(owned).allow)
            self.assertTrue(guard.clear_proof(owned, root["fp"]).allow)

            draft = "用户自己的草稿"
            self.assertFalse(guard.before_return(owned).allow)
            self.assertFalse(guard.clear_proof(owned, root["fp"]).allow)
            draft = owned
            for current_rect in ((201, 490, 600, 130), (200, 490, 601, 130),
                                 (200, 491, 600, 130), (200, 399, 600, 221)):
                with self.subTest(rect=current_rect):
                    self.assertFalse(guard.before_return(owned).allow)
                    self.assertFalse(guard.clear_proof(owned, root["fp"]).allow)
            current_rect = wrapped_rect
            bridge._tx_chats_info.return_value = (1, ["另一个群"], 0)
            self.assertFalse(guard.before_return(owned).allow)
            self.assertFalse(guard.clear_proof(owned, root["fp"]).allow)


if __name__ == "__main__":
    unittest.main()
