"""Fake-clock contract for the verified-open empty WAL refresh."""
import unittest

from test_runtime_proactive_bridge import bridge
from wal_sweep_queue import VerifiedWalRescanQueue


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
            "source_at": 99.9,
        }
        values.update(overrides)
        return bridge.schedule_verified_wal_empty_refresh(
            self.queue, "授权群", **values)

    def test_valid_empty_scan_wakes_once_after_650ms_then_completes(self):
        self.assertTrue(self.schedule())
        self.assertFalse(self.queue.due("授权群", now=100.649))
        self.assertAlmostEqual(self.queue.wait_timeout(5, now=100), 0.65)
        self.now = 100.65
        self.assertTrue(self.queue.due("授权群"))
        self.assertFalse(self.schedule())
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
                    viewport_gap=False, settled=False, source_at=source_at)
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
                queue.hint("授权群", now=self.now, earliest_at=self.now)
                values = dict(now=self.now, verified_pending=True,
                              scope_open=True, had_previous_snapshot=True,
                              appended=False, viewport_gap=False, settled=False,
                              source_at=99.9)
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
