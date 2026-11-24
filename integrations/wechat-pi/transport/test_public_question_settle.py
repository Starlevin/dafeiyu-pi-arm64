"""Pure scheduling tests: no sleep, UI, network, or candidate text retention."""
import unittest
from unittest.mock import Mock, patch

from test_runtime_proactive_bridge import bridge


PublicQuestionSettleQueue = bridge.PublicQuestionSettleQueue
latest_candidate_matches = bridge.latest_candidate_matches


class PublicQuestionSettleTests(unittest.TestCase):
    def test_one_pending_candidate_per_scope_and_monotonic_expiry(self):
        queue = PublicQuestionSettleQueue()
        queue.stage("目标群", "a" * 64, 10.0)
        self.assertIsNone(queue.due_trace("目标群", 11.49))
        self.assertEqual(queue.wait_timeout(3.0, 10.0), 1.5)
        self.assertEqual(queue.due_trace("目标群", 11.5), "a" * 64)

        # Replacing a candidate in the same scope restarts the quiet window;
        # the old trace cannot be released as settled.
        queue.stage("目标群", "b" * 64, 11.6)
        self.assertIsNone(queue.due_trace("目标群", 13.0))
        self.assertEqual(queue.due_trace("目标群", 13.1), "b" * 64)
        self.assertEqual(queue.pending_targets(), {"目标群"})
        self.assertNotIn("正文", repr(queue.pending("目标群")))

    def test_current_latest_proof_rejects_newer_rows_and_unreadable_state(self):
        rows = [("公开问题", (0, 0, 1, 1))]
        self.assertTrue(latest_candidate_matches(
            rows, "same", lambda index: "same" if index == 0 else "other",
            lambda text: text == "公开问题"))
        self.assertFalse(latest_candidate_matches(
            rows + [("更新消息", (0, 0, 1, 1))], "same",
            lambda index: "same" if index == 0 else "newer",
            lambda text: True))
        self.assertFalse(latest_candidate_matches(
            [], "same", lambda _index: "same", lambda _text: True))
        self.assertFalse(latest_candidate_matches(
            rows, "same", lambda _index: None, lambda _text: True))

    def test_foreign_sidebar_and_wal_activity_do_not_cancel_pending_group(self):
        queue = PublicQuestionSettleQueue()
        queue.stage("群X", "x" * 64, 10.0)
        app = object()
        # Y may be scheduled by a sidebar redraw or urgent WAL hint, but the
        # existing pending scope keeps the bounded poll focused on X.
        foreign_activity_targets = ["群Y", "群X"]
        self.assertEqual(bridge.settle_poll_targets(
            foreign_activity_targets, queue.pending_targets()), ["群X"])
        with patch.object(bridge, "group_is_open", return_value=True) as is_open:
            self.assertTrue(bridge.keep_pending_settle_group_open(app, "群X", queue))
        is_open.assert_called_once_with(app, "群X")
        self.assertEqual(queue.pending_targets(), {"群X"})
        self.assertEqual(queue.pending("群X").trace, "x" * 64)

    def test_switching_away_from_pending_group_cancels_it(self):
        queue = PublicQuestionSettleQueue()
        queue.stage("群X", "x" * 64, 10.0)
        with patch.object(bridge, "group_is_open", return_value=False):
            self.assertFalse(bridge.keep_pending_settle_group_open(object(), "群X", queue))
        self.assertFalse(queue.has_pending)

    def test_settling_reserves_sidebar_and_wal_hints_until_after_verification(self):
        app = object()
        watched = {"群X", "群Y"}
        wake = Mock()
        wake.take_pending_with_timing.return_value = ({"message"}, {"message": 12.5})
        with (patch.object(bridge, "get_app", return_value=app) as get_app,
              patch.object(bridge, "sidebar_signatures", return_value={"群Y": "changed"}) as sidebar):
            self.assertEqual(bridge.read_poll_hints(watched, wake, settling=True), (None, {}, set(), {}))
            get_app.assert_not_called()
            sidebar.assert_not_called()
            wake.take_pending_with_timing.assert_not_called()
            self.assertEqual(bridge.read_poll_hints(watched, wake, settling=False),
                             (app, {"群Y": "changed"}, {"message"}, {"message": 12.5}))
            sidebar.assert_called_once_with(app, watched)
            wake.take_pending_with_timing.assert_called_once_with()

    def test_newer_row_in_same_group_rejects_and_drops_settled_candidate(self):
        queue = PublicQuestionSettleQueue()
        trace = "x" * 64
        queue.stage("群X", trace, 10.0)
        rows = [("原候选", (0, 0, 1, 1)), ("同群后续消息", (0, 1, 1, 1))]
        matches = latest_candidate_matches(
            rows, trace, lambda index: "y" * 64 if index == 1 else trace,
            lambda _text: True)
        self.assertFalse(matches)
        queue.cancel("群X")
        self.assertFalse(queue.has_pending)

    def test_changed_group_cancels_only_its_pending_candidate(self):
        queue = PublicQuestionSettleQueue()
        queue.stage("目标群", "a" * 64, 1.0)
        queue.cancel("目标群")
        self.assertFalse(queue.has_pending)
        self.assertIsNone(queue.pending("目标群"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
