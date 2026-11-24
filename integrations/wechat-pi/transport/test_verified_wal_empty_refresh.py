"""Fake-clock contract for the verified-open empty WAL refresh."""
import unittest
from unittest.mock import patch

from test_runtime_proactive_bridge import bridge
from wal_sweep_queue import VerifiedWalRescanQueue, WalSweepQueue


class VerifiedWalEmptyRefreshTests(unittest.TestCase):
    def test_known_group_change_defers_broad_hint_without_resolving_private_scope(self):
        roles, times, carry = bridge.prioritize_sidebar_hint(
            ["group"], {"group": "after", "private": "same"}, {"group": "before", "private": "same"},
            {"message"}, {"message": 20.0}, carry=None, refresh_held=False)
        self.assertEqual((roles, times), (set(), {}))
        with patch.object(bridge, "chat_title") as title:
            self.assertIsNone(bridge.verified_wal_target(object(), object(), roles))
            title.assert_not_called()

        # Carry remains available even if the targeted read did not update its baseline.
        roles, times, remaining = bridge.prioritize_sidebar_hint(
            ["group"], {"group": "after"}, {"group": "before"},
            {"message", "session"}, {"message": 20.7, "session": 20.6},
            carry=carry, refresh_held=False)
        self.assertEqual(roles, {"message", "session"})
        self.assertEqual(times, {"message": 20.0, "session": 20.6})
        self.assertIsNone(remaining)

    def test_initial_or_private_preview_change_and_pending_refresh_keep_wal_path(self):
        for previous, signatures, refresh_held in (
            ({}, {"group": "first"}, False),
            ({"group": "same", "private": "before"}, {"group": "same", "private": "after"}, False),
            ({"group": "before"}, {"group": "after"}, True),
        ):
            with self.subTest(previous=previous, refresh_held=refresh_held):
                roles, times, carry = bridge.prioritize_sidebar_hint(
                    ["group"], signatures, previous, {"message"}, {"message": 20.0},
                    carry=None, refresh_held=refresh_held)
                self.assertEqual((roles, times, carry), ({"message"}, {"message": 20.0}, None))

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


class WalGroupReadPlanningTests(unittest.TestCase):
    def test_due_groups_are_planned_using_one_visible_title_read(self):
        queue = VerifiedWalRescanQueue(lambda: 102)
        queue.hint("第一个群", now=100)
        queue.hint("第二个群", now=100)
        queue.hint("未启用群", now=100)
        app = object()
        with patch.object(bridge, "chat_title", return_value="第二个群(5)") as title, \
             patch.object(bridge, "group_is_open") as full_proof:
            planned = bridge.plan_verified_wal_targets(
                queue, ["第一个群", "第二个群"], app, None, 102)
        self.assertEqual(planned, {"第二个群"})
        title.assert_called_once_with(app, normalize=False)
        full_proof.assert_not_called()

    def test_matching_private_title_does_not_schedule_a_group_read(self):
        queue = VerifiedWalRescanQueue(lambda: 102)
        queue.hint("同名会话", now=100)
        with patch.object(bridge, "chat_title", return_value="同名会话"):
            self.assertEqual(bridge.plan_verified_wal_targets(
                queue, ["同名会话"], object(), None, 102), set())

    def test_group_proof_short_circuits_the_other_visible_room(self):
        with patch.object(bridge, "chat_title", return_value="另一个群(5)"), \
             patch.object(bridge, "visible_chat_matches") as sidebar, \
             patch.object(bridge, "_bieye_search_overlay_visible") as overlay:
            self.assertFalse(bridge.group_is_open(object(), "夹具群"))
        sidebar.assert_not_called()
        overlay.assert_not_called()

    def test_full_proof_rejects_a_switch_or_duplicate_before_reading(self):
        for titles, matches, expected in [
            (["夹具群(5)", "另一个群(5)"], [object()], False),
            (["夹具群(5)", "夹具群(5)"], [object(), object()], False),
            (["夹具群(5)", "夹具群(5)"], [object()], True),
        ]:
            with self.subTest(expected=expected, matching_rows=len(matches)), \
                 patch.object(bridge, "chat_title", side_effect=titles), \
                 patch.object(bridge, "visible_chat_matches", return_value=matches):
                self.assertEqual(bridge.group_is_open(object(), "夹具群"), expected)

    def test_navigation_returns_on_fresh_scope_proof_without_fixed_delay(self):
        target = "fixture-group"
        app = object()
        elapsed = 0.0

        def sleep(delay):
            nonlocal elapsed
            elapsed += delay

        with patch.object(bridge, "get_app", return_value=app), \
             patch.object(bridge, "visible_chat_matches", return_value=[(110, 24, 280, 48)]), \
             patch.object(bridge, "visible_group_title", side_effect=lambda _app: target if elapsed else "other"), \
             patch.object(bridge.xi, "click", create=True) as click, \
             patch.object(bridge.time, "monotonic", side_effect=lambda: elapsed), \
             patch.object(bridge.time, "sleep", side_effect=sleep):
            self.assertTrue(bridge.open_chat(app, target))
        click.assert_called_once_with(150, 48, settle=0)
        self.assertEqual(elapsed, .125)

    def test_navigation_ambiguity_after_click_fails_without_clicking_again(self):
        target = "fixture-group"
        app = object()
        elapsed = 0.0
        rect = (110, 24, 280, 48)

        def sleep(delay):
            nonlocal elapsed
            elapsed += delay

        with patch.object(bridge, "get_app", return_value=app), \
             patch.object(bridge, "visible_group_title", return_value=target), \
             patch.object(bridge, "visible_chat_matches", side_effect=[[rect]] + [[rect, rect]] * 5), \
             patch.object(bridge.xi, "click", create=True) as click, \
             patch.object(bridge.time, "monotonic", side_effect=lambda: elapsed), \
             patch.object(bridge.time, "sleep", side_effect=sleep):
            self.assertFalse(bridge.open_chat(app, target))
        click.assert_called_once()
        self.assertAlmostEqual(elapsed, .4)


if __name__ == "__main__":
    unittest.main(verbosity=2)
