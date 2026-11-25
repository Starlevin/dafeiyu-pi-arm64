"""Offline regression tests for the fail-closed blocking-state tree scan."""
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, call, patch

from test_runtime_proactive_bridge import load_bridge


bridge = load_bridge()
SHOWING = "showing"


class FakeStateSet:
    def __init__(self, showing=True):
        self.showing = showing

    def contains(self, state):
        return self.showing if state == SHOWING else False


class FakeNode:
    def __init__(self, role, name="", children=(), *, showing=True, rect=(0, 0, 0, 0)):
        self.role = role
        self.name = name
        self.children = list(children)
        self.rect = rect
        self.state = FakeStateSet(showing)
        self.role_calls = 0
        self.name_calls = 0
        self.child_count_calls = 0
        self.child_index_calls = 0
        self.extent_calls = 0

    def get_role_name(self):
        self.role_calls += 1
        return self.role

    def get_name(self):
        self.name_calls += 1
        return self.name

    def get_state_set(self):
        return self.state

    def get_child_count(self):
        self.child_count_calls += 1
        return len(self.children)

    def get_child_at_index(self, index):
        self.child_index_calls += 1
        return self.children[index]

    def get_component_iface(self):
        return self

    def get_extents(self, _coord_type):
        self.extent_calls += 1
        x, y, width, height = self.rect
        return SimpleNamespace(x=x, y=y, width=width, height=height)


def legacy_blocking_state(root):
    """Baseline implementation before role metadata was reused from the walker."""
    for node, _ in bridge._walk_showing(root, maxd=12, skip_lists=True):
        name = bridge.nm(node).lower()
        if "confirm on phone" in name or "手机上确认" in name:
            return "login_confirmation_required"
        if ("scan to log in" in name or "扫码登录" in name
                or (name in ("log in", "登录", "switch account")
                    and bridge.role(node) == "button")):
            return "login_required"
        if "after logging out" in name or "确定退出登录" in name:
            return "window_blocked"
        if bridge.role(node) == "dialog" and (bridge.ext(node) or (0, 0, 0, 0))[2] > 0:
            return "window_blocked"
    return None


class BlockingStateScanTests(unittest.TestCase):
    def test_idle_window_probe_refreshes_without_reopening_conversations(self):
        app = FakeNode("application", children=(FakeNode("list", "Chats"),))
        health = Mock()
        probe = bridge.ClientWindowProbe(health)
        atspi = SimpleNamespace(StateType=SimpleNamespace(SHOWING=SHOWING))
        with (patch.object(bridge, "Atspi", atspi),
              patch.object(bridge, "_chats_cache", (None, None)),
              patch.object(bridge, "set_heartbeat_state") as heartbeat,
              patch.object(bridge, "open_chat") as open_chat,
              patch.object(bridge, "read_messages") as read_messages,
              patch.object(bridge, "try_saved_login") as saved_login):
            for second in range(85):
                self.assertIsNone(probe.refresh(app, now=second))
            self.assertEqual(health.update.call_args_list, [call("client", "window_available")] * 5)
            self.assertEqual(heartbeat.call_args_list, [call(True, None)] * 5)
            open_chat.assert_not_called()
            read_messages.assert_not_called()
            saved_login.assert_not_called()

    def test_window_probe_recovers_from_missing_app_or_probe_error(self):
        app = object()
        health = Mock()
        probe = bridge.ClientWindowProbe(health)
        with (patch.object(bridge, "blocking_state", side_effect=[
                  RuntimeError("test"), "login_confirmation_required", None]),
              patch.object(bridge, "chats_list", return_value=object()),
              patch.object(bridge, "set_heartbeat_state") as heartbeat):
            self.assertEqual(probe.refresh(None, now=1), "client_unavailable")
            self.assertEqual(probe.refresh(app, now=2), "window_not_ready")
            self.assertEqual(probe.refresh(app, now=3), "window_not_ready")
            self.assertEqual(probe.refresh(app, now=4), "login_confirmation_required")
            self.assertEqual(probe.refresh(None, now=5), "client_unavailable")
            self.assertIsNone(probe.refresh(app, now=6))
        self.assertEqual(health.update.call_args_list, [
            call("client", "client_unavailable"), call("client", "window_not_ready"),
            call("client", "login_confirmation_required"), call("client", "client_unavailable"),
            call("client", "window_available"),
        ])
        self.assertEqual(heartbeat.call_args_list, [call(False, False)] * 4 + [call(True, None)])

    def test_message_viewport_lookup_skips_hidden_pages_and_chats_children(self):
        sidebar_row = FakeNode("list item", "fixture room")
        sidebar = FakeNode("list", "Chats", children=(sidebar_row,))
        hidden = FakeNode("frame", children=(FakeNode("list", "Messages"),), showing=False)
        messages = FakeNode("list", "Messages")
        root = FakeNode("application", children=(sidebar, hidden, messages))
        atspi = SimpleNamespace(StateType=SimpleNamespace(SHOWING=SHOWING))
        with patch.object(bridge, "Atspi", atspi), patch.object(bridge, "_messages_cache", (None, None)):
            self.assertIs(bridge.messages_list(root), messages)
            self.assertIs(bridge.messages_list(root), messages)
        self.assertEqual(sidebar.child_count_calls, 0)
        self.assertEqual(hidden.child_count_calls, 0)
        self.assertEqual(root.child_count_calls, 1)

    def test_message_viewport_lookup_falls_back_on_unreported_ancestor_visibility(self):
        messages = FakeNode("list", "Messages")
        parent = FakeNode("frame", children=(messages,), showing=False)
        root = FakeNode("application", children=(parent,))
        atspi = SimpleNamespace(StateType=SimpleNamespace(SHOWING=SHOWING))
        with patch.object(bridge, "Atspi", atspi), patch.object(bridge, "_messages_cache", (None, None)):
            self.assertIs(bridge.messages_list(root), messages)

    def test_sidebar_lookup_skips_message_rows_and_hidden_qt_pages(self):
        row = FakeNode("list item", "fixture message")
        messages = FakeNode("list", "Messages", children=(row,))
        hidden_sidebar = FakeNode("list", "Chats", showing=False)
        hidden = FakeNode("filler", children=(hidden_sidebar,), showing=False)
        chats = FakeNode("list", "Chats")
        root = FakeNode("application", children=(hidden, messages, chats))
        atspi = SimpleNamespace(StateType=SimpleNamespace(SHOWING=SHOWING))
        with patch.object(bridge, "Atspi", atspi), patch.object(bridge, "_chats_cache", (None, None)):
            self.assertIs(bridge.chats_list(root), chats)
            self.assertIs(bridge.chats_list(root), chats)
        self.assertEqual(messages.child_count_calls, 0)
        self.assertEqual(hidden.child_count_calls, 0)
        self.assertEqual(row.role_calls, 0)
        self.assertEqual(root.child_count_calls, 1)

    def test_sidebar_lookup_preserves_fallback_and_rechecks_hidden_cached_sidebar(self):
        sidebar = FakeNode("list", "Chats")
        parent = FakeNode("frame", children=(sidebar,), showing=False)
        root = FakeNode("application", children=(parent,))
        atspi = SimpleNamespace(StateType=SimpleNamespace(SHOWING=SHOWING))
        with patch.object(bridge, "Atspi", atspi), patch.object(bridge, "_chats_cache", (None, None)):
            self.assertIs(bridge.chats_list(root), sidebar)
            sidebar.state.showing = False
            self.assertIsNone(bridge.chats_list(root))

    def test_overlay_scan_reuses_roles_and_still_finds_a_visible_search_list(self):
        hidden = FakeNode("list", showing=False, rect=(110, 115, 300, 320))
        regular = FakeNode("list", "Chats")
        overlay = FakeNode("list", rect=(110, 115, 300, 320))
        root = FakeNode("frame", children=(hidden, regular, overlay))
        atspi = SimpleNamespace(StateType=SimpleNamespace(SHOWING=SHOWING),
                                CoordType=SimpleNamespace(SCREEN="screen"))
        with patch.object(bridge, "Atspi", atspi):
            self.assertTrue(bridge._bieye_search_overlay_visible(root))
        self.assertEqual(hidden.role_calls, 0)
        self.assertEqual([node.role_calls for node in (root, regular, overlay)], [1, 1, 1])

    def test_depth_boundary_yields_node_without_reading_excluded_children(self):
        leaf = FakeNode("list")
        boundary = FakeNode("filler", children=(leaf,))
        root = FakeNode("frame", children=(boundary,))
        atspi = SimpleNamespace(StateType=SimpleNamespace(SHOWING=SHOWING))
        with patch.object(bridge, "Atspi", atspi):
            self.assertEqual(list(bridge._walk_showing(root, maxd=1)),
                             [(root, 0), (boundary, 1)])
            self.assertEqual(boundary.child_count_calls, 0)
            self.assertEqual(boundary.child_index_calls, 0)
            with self.assertRaisesRegex(RuntimeError, "visible_tree_incomplete"):
                list(bridge._walk_showing(root, maxd=1, strict=True))
        self.assertEqual(boundary.child_count_calls, 1)
        self.assertEqual(boundary.child_index_calls, 0)

    def scan(self, root):
        atspi = SimpleNamespace(
            StateType=SimpleNamespace(SHOWING=SHOWING),
            CoordType=SimpleNamespace(SCREEN="screen"),
        )
        with patch.object(bridge, "Atspi", atspi):
            return bridge.blocking_state(root)

    def test_dialog_reuses_pruning_role_and_preserves_early_exit(self):
        baseline_dialog = FakeNode("dialog", "Preferences", rect=(10, 10, 300, 200))
        baseline_later = FakeNode("button", "Log In")
        baseline_root = FakeNode("frame", children=[baseline_dialog, baseline_later])
        dialog = FakeNode("dialog", "Preferences", rect=(10, 10, 300, 200))
        later_blocker = FakeNode("button", "Log In")
        root = FakeNode("frame", children=[dialog, later_blocker])
        atspi = SimpleNamespace(
            StateType=SimpleNamespace(SHOWING=SHOWING),
            CoordType=SimpleNamespace(SCREEN="screen"),
        )

        with patch.object(bridge, "Atspi", atspi):
            baseline = legacy_blocking_state(baseline_root)
            candidate = bridge.blocking_state(root)
        self.assertEqual((baseline, candidate), ("window_blocked", "window_blocked"))
        self.assertEqual(root.role_calls, 1)
        self.assertEqual(dialog.role_calls, 1)
        self.assertEqual(dialog.extent_calls, 1)
        self.assertEqual(later_blocker.role_calls, 0)
        self.assertEqual(baseline_later.role_calls, 0)

    def test_named_blockers_keep_their_reasons_and_stop_at_first_match(self):
        cases = (
            ("Confirm on phone", "label", "login_confirmation_required"),
            ("Scan to log in", "label", "login_required"),
            ("Log In", "button", "login_required"),
            ("Switch Account", "button", "login_required"),
            ("After logging out", "label", "window_blocked"),
            ("确定退出登录", "label", "window_blocked"),
        )
        for name, role_name, expected in cases:
            with self.subTest(name=name, role=role_name):
                baseline_blocker = FakeNode(role_name, name)
                baseline_later = FakeNode("dialog", "Later", rect=(0, 0, 200, 100))
                baseline_root = FakeNode("frame", children=[baseline_blocker, baseline_later])
                blocker = FakeNode(role_name, name)
                later = FakeNode("dialog", "Later", rect=(0, 0, 200, 100))
                root = FakeNode("frame", children=[blocker, later])
                atspi = SimpleNamespace(
                    StateType=SimpleNamespace(SHOWING=SHOWING),
                    CoordType=SimpleNamespace(SCREEN="screen"),
                )
                with patch.object(bridge, "Atspi", atspi):
                    baseline = legacy_blocking_state(baseline_root)
                    candidate = bridge.blocking_state(root)
                self.assertEqual((baseline, candidate), (expected, expected))
                self.assertEqual(blocker.role_calls, 1)
                self.assertEqual(later.role_calls, 0)
                self.assertEqual(baseline_later.role_calls, 0)

    def test_hidden_nodes_and_chat_lists_remain_pruned(self):
        hidden_child = FakeNode("dialog", "Preferences", rect=(0, 0, 200, 100))
        hidden = FakeNode("panel", children=[hidden_child], showing=False)
        chat_descendant = FakeNode("dialog", "Preferences", rect=(0, 0, 200, 100))
        chats = FakeNode("list", "Chats", [chat_descendant])
        root = FakeNode("frame", children=[hidden, chats])

        self.assertIsNone(self.scan(root))
        self.assertEqual(hidden.role_calls, 0)
        self.assertEqual(hidden_child.role_calls, 0)
        self.assertEqual(chats.role_calls, 1)
        self.assertEqual(chat_descendant.role_calls, 0)

    def test_normal_scan_role_queries_drop_from_twice_per_node_to_once(self):
        # Match the 69 showing proxies in the measured production traversal.
        children = [FakeNode("panel", f"item-{index}") for index in range(68)]
        baseline_root = FakeNode("frame", children=children)
        candidate_children = [FakeNode("panel", f"item-{index}") for index in range(68)]
        candidate_root = FakeNode("frame", children=candidate_children)
        atspi = SimpleNamespace(
            StateType=SimpleNamespace(SHOWING=SHOWING),
            CoordType=SimpleNamespace(SCREEN="screen"),
        )
        with patch.object(bridge, "Atspi", atspi):
            baseline = legacy_blocking_state(baseline_root)
            candidate = bridge.blocking_state(candidate_root)

        baseline_roles = baseline_root.role_calls + sum(n.role_calls for n in children)
        candidate_roles = candidate_root.role_calls + sum(n.role_calls for n in candidate_children)
        self.assertEqual((baseline, candidate), (None, None))
        self.assertEqual(baseline_roles, 138)
        self.assertEqual(candidate_roles, 69)
        self.assertEqual(candidate_roles, baseline_roles // 2)

    def test_default_walker_keeps_two_tuple_api(self):
        child = FakeNode("button", "Help")
        root = FakeNode("frame", children=[child])
        atspi = SimpleNamespace(StateType=SimpleNamespace(SHOWING=SHOWING))
        with patch.object(bridge, "Atspi", atspi):
            rows = list(bridge._walk_showing(root))
        self.assertEqual(rows, [(root, 0), (child, 1)])
        self.assertEqual(root.role_calls, 0)
        self.assertEqual(child.role_calls, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
