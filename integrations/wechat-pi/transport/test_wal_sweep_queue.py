#!/usr/bin/env python3
"""Offline tests for scoped verified-open WAL rechecks."""
import unittest

from wal_sweep_queue import VerifiedWalRescanQueue


class VerifiedWalRescanQueueTests(unittest.TestCase):
    def setUp(self):
        self.now = 100.0
        self.queue = VerifiedWalRescanQueue(lambda: self.now, ttl_s=45, retry_s=8)

    def test_scoped_hint_waits_until_scan_cooldown_and_coalesces_without_postponing(self):
        self.assertTrue(self.queue.hint("approved-group", now=100, earliest_at=106))
        self.assertFalse(self.queue.due("approved-group", now=105.99))
        self.assertFalse(self.queue.hint("approved-group", now=104, earliest_at=112))
        self.assertTrue(self.queue.due("approved-group", now=106))
        self.assertEqual(self.queue.pending_count(), 1)
        self.assertEqual(self.queue.stats["coalesced"], 1)

    def test_success_completes_and_failure_retries_within_ttl(self):
        self.queue.hint("group-a", now=100, earliest_at=108)
        self.queue.failed("group-a", now=108)
        self.assertFalse(self.queue.due("group-a", now=115.99))
        self.assertTrue(self.queue.due("group-a", now=116))
        self.queue.completed("group-a")
        self.assertFalse(self.queue.due("group-a", now=200))
        self.assertEqual(self.queue.stats["completed"], 1)

    def test_entries_expire_and_queue_is_bounded(self):
        small = VerifiedWalRescanQueue(lambda: self.now, ttl_s=10, max_pending=1)
        self.assertTrue(small.hint("group-a", now=100, earliest_at=105))
        self.assertFalse(small.hint("group-b", now=100, earliest_at=105))
        self.assertEqual(small.stats["full"], 1)
        self.assertFalse(small.due("group-a", now=110))
        self.assertEqual(small.pending_count(), 0)
        self.assertEqual(small.stats["expired"], 1)

    def test_noise_cannot_accelerate_or_extend_empty_refresh(self):
        self.assertTrue(self.queue.hint("group-a", now=100, earliest_at=100, source_at=100))
        self.assertEqual(self.queue.retry_empty("group-a", now=100)[0], 1)
        self.assertFalse(self.queue.due("group-a", now=100.649))
        self.assertAlmostEqual(self.queue.wait_timeout(10, now=100), 0.65)

        item = self.queue.pending["group-a"]
        deadline = item["due_at"]
        self.assertFalse(self.queue.hint("group-a", now=100.1, earliest_at=100.1))
        self.assertEqual(item["due_at"], deadline)
        self.assertFalse(self.queue.due("group-a", now=100.649))
        self.assertTrue(self.queue.due("group-a", now=100.65))

        self.assertEqual(self.queue.retry_empty("group-a", now=100.65)[0], 2)
        self.assertFalse(self.queue.hint("group-a", now=103, earliest_at=103, source_at=103))
        self.assertFalse(self.queue.due("group-a", now=105.199))
        self.assertTrue(self.queue.due("group-a", now=105.2))
        self.assertIsNone(self.queue.retry_empty("group-a", now=105.2))
        self.assertFalse(self.queue.due("group-a", now=106.5))
        self.assertEqual(self.queue.stats["empty_refresh_queued"], 2)
        self.assertEqual(self.queue.pending_count(), 0)

    def test_scope_switch_cancels_only_short_refresh_and_failure_returns_to_normal_retry(self):
        self.queue.hint("group-a", now=100, earliest_at=100, source_at=100)
        self.queue.retry_empty("group-a", now=100)
        self.assertTrue(self.queue.cancel_empty_refresh("group-a"))
        self.assertFalse(self.queue.due("group-a", now=101))
        self.assertFalse(self.queue.cancel_empty_refresh("group-a"))

        self.queue.hint("group-b", now=110, earliest_at=110, source_at=110)
        self.queue.retry_empty("group-b", now=110)
        self.queue.failed("group-b", now=110.2)
        self.assertFalse(self.queue.empty_refresh_pending("group-b"))
        self.assertFalse(self.queue.due("group-b", now=118.19))
        self.assertTrue(self.queue.due("group-b", now=118.2))

    def test_source_time_survives_until_verified_recheck_completes(self):
        source_at = 97.25
        self.assertTrue(self.queue.hint("group-a", now=100,
                                        earliest_at=108, source_at=source_at))
        self.assertEqual(self.queue.source_at("group-a"), source_at)
        self.assertIsNone(self.queue.source_at("unlisted-group"))
        self.queue.completed("group-a")
        self.assertIsNone(self.queue.source_at("group-a"))


if __name__ == "__main__":
    unittest.main()
