"""Fake-clock contract for the verified-open empty WAL refresh."""
import unittest
from unittest.mock import patch

from test_runtime_proactive_bridge import bridge
from wal_sweep_queue import VerifiedWalRescanQueue, WalSweepQueue


class VerifiedWalEmptyRefreshTests(unittest.TestCase):
    def setUp(self):
        self.now = 100.0
        self.queue = VerifiedWalRescanQueue(lambda: self.now)
        self.queue.hint("授权群", now=self.now, earliest_at=self.now, source_at=99.9)

    def schedule(self, **overrides):
        values = {
            "now": self.now,
            "verified_pending": True,
            "scope_open": True,
            "had_previous_snapshot": True,
            "appended": False,
            "viewport_gap": False,
            "settled": False,
        }
        values.update(overrides)
        return bridge.schedule_verified_wal_empty_refresh(
            self.queue, "授权群", **values)

    def test_two_empty_rechecks_then_completes_without_extending_the_window(self):
        first, delay = self.schedule()
        self.assertEqual(first, 1)
        self.assertAlmostEqual(delay, 0.65)
        self.assertFalse(self.queue.due("授权群", now=100.649))
        self.assertAlmostEqual(self.queue.wait_timeout(5, now=100), 0.65)
        self.now = 100.65
        self.assertTrue(self.queue.due("授权群"))
        second, delay = self.schedule()
        self.assertEqual(second, 2)
        self.assertAlmostEqual(delay, 4.45)
        self.assertFalse(self.queue.due("授权群", now=105.09))
        self.now = 105.11
        self.assertTrue(self.queue.due("授权群"))
        self.assertIsNone(self.schedule())
        self.queue.completed("授权群")
        self.assertEqual(self.queue.pending_count(), 0)

    def test_actual_append_completes_without_short_retry(self):
        self.assertFalse(self.schedule(appended=True))
        self.queue.completed("授权群")
        self.assertEqual(self.queue.stats["empty_refresh_queued"], 0)
        self.assertEqual(self.queue.stats["completed"], 1)

    def test_ordinary_scan_before_queue_due_does_not_ack_verified_hint(self):
        self.assertFalse(bridge.verified_wal_scan_may_ack(
            forced_due=False, verified_pending=True, viewport_gap=False))
        self.assertIn("授权群", self.queue.pending)
        self.assertTrue(bridge.verified_wal_scan_may_ack(
            forced_due=True, verified_pending=True, viewport_gap=False))
        self.assertFalse(bridge.verified_wal_scan_may_ack(
            forced_due=True, verified_pending=True, viewport_gap=True))

    def test_generic_forced_sweep_acknowledges_without_short_refresh(self):
        forced_due, verified_due = True, False
        self.assertTrue(bridge.verified_wal_scan_may_ack(
            forced_due=forced_due, verified_pending=True, viewport_gap=False))
        self.assertFalse(bridge.verified_wal_scan_may_short_refresh(
            verified_due=verified_due, verified_pending=True, viewport_gap=False))
        self.queue.completed("授权群")
        self.assertFalse(self.queue.empty_refresh_pending("授权群"))

    def test_verified_due_forced_read_can_schedule_short_refresh(self):
        forced_due, verified_due = True, True
        self.assertTrue(bridge.verified_wal_scan_may_ack(
            forced_due=forced_due, verified_pending=True, viewport_gap=False))
        self.assertTrue(bridge.verified_wal_scan_may_short_refresh(
            verified_due=verified_due, verified_pending=True, viewport_gap=False))
        self.assertTrue(self.schedule())

    def test_short_refresh_requires_real_recent_monotonic_message_time(self):
        for source_at in (None, 0, 94.99, 100.01, True, float("nan"), float("inf")):
            with self.subTest(source_at=source_at):
                queue = VerifiedWalRescanQueue(lambda: self.now)
                queue.hint("授权群", now=self.now, earliest_at=self.now,
                           source_at=source_at)
                result = bridge.schedule_verified_wal_empty_refresh(
                    queue, "授权群", now=100.0, verified_pending=True,
                    scope_open=True, had_previous_snapshot=True, appended=False,
                    viewport_gap=False, settled=False)
                self.assertFalse(result)
                self.assertFalse(queue.empty_refresh_pending("授权群"))

    def test_filtered_short_refresh_cancels_without_expired_deadline_hot_loop(self):
        self.assertTrue(self.schedule())
        cancelled = bridge.cancel_filtered_empty_refreshes(
            self.queue, {"授权群"}, {"另一个群"})
        self.assertEqual(cancelled, ["授权群"])
        self.assertEqual(self.queue.pending_count(), 0)
        self.assertEqual(self.queue.wait_timeout(10, now=101), 10)

    def test_unverified_scope_gap_first_baseline_or_settle_never_gets_short_retry(self):
        for overrides in (
            {"scope_open": False},
            {"viewport_gap": True},
            {"had_previous_snapshot": False},
            {"settled": True},
            {"verified_pending": False},
        ):
            with self.subTest(**overrides):
                queue = VerifiedWalRescanQueue(lambda: self.now)
                queue.hint("授权群", now=self.now, earliest_at=self.now, source_at=99.9)
                values = dict(now=self.now, verified_pending=True,
                              scope_open=True, had_previous_snapshot=True,
                              appended=False, viewport_gap=False, settled=False)
                values.update(overrides)
                result = bridge.schedule_verified_wal_empty_refresh(
                    queue, "授权群", **values)
                self.assertFalse(result)
                self.assertFalse(queue.empty_refresh_pending("授权群"))

    def test_wal_noise_does_not_accelerate_and_switch_cancels_short_window(self):
        self.assertTrue(self.schedule())
        deadline = self.queue.pending["授权群"]["due_at"]
        self.assertFalse(self.queue.hint("授权群", now=100.1, earliest_at=100.1))
        self.assertEqual(self.queue.pending["授权群"]["due_at"], deadline)
        self.assertTrue(self.queue.cancel_empty_refresh("授权群"))
        self.assertEqual(self.queue.pending_count(), 0)

    def test_scheduled_empty_refresh_holds_only_the_open_scope_until_completion(self):
        self.assertTrue(self.schedule())
        app = object()
        with patch.object(bridge, "chat_title", return_value="授权群(5)") as opened:
            self.assertEqual(bridge.keep_short_wal_refresh(
                self.queue, "授权群", app, ["授权群", "其他轮询"]), "授权群")
            opened.assert_called_once_with(app, normalize=False)
            self.now = 100.65
            self.assertTrue(self.queue.due("授权群"))
            self.queue.completed("授权群")
            self.assertIsNone(bridge.keep_short_wal_refresh(self.queue, "授权群", app, ["授权群"]))
            opened.assert_called_once()

    def test_switch_or_higher_priority_settle_releases_navigation_hold(self):
        for open_group, targets in ((False, ["授权群"]), (True, ["其他轮询"])):
            with self.subTest(open_group=open_group):
                self.queue.hint("授权群", now=self.now, source_at=99.9)
                self.assertTrue(self.schedule())
                title = "授权群(5)" if open_group else "其他群(4)"
                with patch.object(bridge, "chat_title", return_value=title):
                    self.assertIsNone(bridge.keep_short_wal_refresh(
                        self.queue, "授权群", object(), targets))
                self.assertFalse(self.queue.empty_refresh_pending("授权群"))


    def test_competing_sidebar_activity_releases_delayed_hold_before_navigation(self):
        self.assertTrue(self.schedule())
        with patch.object(bridge, "chat_title", return_value="授权群(5)") as opened:
            self.assertEqual(bridge.keep_short_wal_refresh(
                self.queue, "授权群", object(), ["授权群"], competing_activity=True), "授权群")
            self.now = 100.66
            self.assertEqual(self.schedule()[0], 2)
            self.assertIsNone(bridge.keep_short_wal_refresh(
                self.queue, "授权群", object(), ["授权群"], competing_activity=True))
            opened.assert_called_once()
        self.assertEqual(self.queue.pending_count(), 0)


class WalPollPriorityTests(unittest.TestCase):
    def test_preview_change_precedes_an_unrelated_private_wal_hint(self):
        ordered = bridge.poll_order(
            ["private", "group-a", "group-b"], {"group-b"}, "private",
            set(), None, 100, {"group-a"})
        self.assertEqual(ordered, ["group-b", "private", "group-a"])

    def test_private_with_actual_preview_change_retains_priority(self):
        ordered = bridge.poll_order(
            ["group", "private"], {"group", "private"}, "private",
            set(), None, 100, {"group"})
        self.assertEqual(ordered, ["private", "group"])

    def test_empty_private_probe_queues_one_visible_group_and_obeys_cooldown(self):
        sweep = WalSweepQueue(lambda: 100)
        groups = ["recent", "cold", "oldest", "older", "hidden", "no_scan"]
        signatures = dict.fromkeys(["recent", "cold", "oldest", "older", "no_scan", "private"], "hash")
        snapshots = {name: ["hash"] for name in ("oldest", "older", "recent", "hidden", "private", "no_scan")}
        scanned = {"recent": 94, "cold": 10, "oldest": 20, "older": 35, "hidden": 3, "private": 1}
        selected = bridge.queue_group_after_empty_private_wal(
            sweep, groups, signatures, snapshots, scanned, now=100)
        self.assertEqual(selected, "oldest")
        self.assertEqual(set(sweep.pending), {"oldest"})
        self.assertTrue(sweep.due("oldest", now=100))
        sweep.completed("oldest")
        scanned["oldest"] = 100
        self.assertIsNone(bridge.queue_group_after_empty_private_wal(
            sweep, groups, signatures, snapshots, scanned, now=129.99))
        self.assertEqual(bridge.queue_group_after_empty_private_wal(
            sweep, groups, signatures, snapshots, scanned, now=130), "older")

    def test_no_usable_group_leaves_existing_periodic_scan_in_charge(self):
        sweep = WalSweepQueue(lambda: 100)
        self.assertIsNone(bridge.queue_group_after_empty_private_wal(
            sweep, ["hidden", "recent"], {"recent": "hash"},
            {"hidden": ["hash"], "recent": ["hash"]},
            {"hidden": 80, "recent": 99}, now=100))
        self.assertEqual(sweep.pending_count(), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
