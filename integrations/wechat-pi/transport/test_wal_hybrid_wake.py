#!/usr/bin/env python3
"""Offline, bounded tests for coalesced WAL wake lifecycle timing."""
import threading
import time
import unittest
import sys
import types
from queue import Queue
from unittest.mock import Mock

# The production inotify module binds Linux libc symbols during import. This
# scheduler unit test injects a fake watcher and deliberately does not load or
# exercise any OS or database integration.
_linux_watcher_stub = types.ModuleType("wal_inotify_readonly")
_linux_watcher_stub.ReadOnlyWalEvents = object
sys.modules.setdefault("wal_inotify_readonly", _linux_watcher_stub)

from wal_hybrid_wake import WalWake
from test_runtime_proactive_bridge import bridge


class _Notify:
    def __init__(self):
        self.event = threading.Event()
        self.calls = 0
        self.lock = threading.Lock()

    def set(self):
        with self.lock:
            self.calls += 1
        self.event.set()


class _Watcher:
    def __init__(self, paths):
        self.first = True
        self.stopped = threading.Event()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.stopped.set()

    def poll(self, timeout):
        if self.first:
            self.first = False
            return {"message"}
        self.stopped.wait(min(timeout, 0.002))
        return set()


class WalWakeLifecycleTests(unittest.TestCase):
    def test_pending_hint_repulses_and_exposes_only_monotonic_role_time(self):
        notify = _Notify()
        wake = WalWake(notify, min_gap_s=0.03, paths=["approved-wal-path"],
                       watcher_factory=_Watcher)
        self.assertTrue(wake.start())
        deadline = time.monotonic() + 0.8
        try:
            self.assertTrue(notify.event.wait(timeout=deadline - time.monotonic()))
            # Reproduce the main loop's wait-return / clear window while the
            # coarse role is still pending and has not yet been drained.
            notify.event.clear()
            self.assertTrue(notify.event.wait(timeout=deadline - time.monotonic()))
            while time.monotonic() < deadline:
                with notify.lock:
                    if notify.calls >= 2:
                        break
                time.sleep(0.005)
            with notify.lock:
                calls_before_drain = notify.calls
            self.assertGreaterEqual(calls_before_drain, 2)

            latest = wake.latest_pending_message_at()
            self.assertIsNotNone(latest)
            roles, first_seen = wake.take_pending_with_timing()
            self.assertEqual(roles, {"message"})
            self.assertEqual(set(first_seen), {"message"})
            self.assertLessEqual(first_seen["message"], time.monotonic())
            self.assertGreaterEqual(latest, first_seen["message"])
            self.assertIsNone(wake.latest_pending_message_at())

            drained_deadline = time.monotonic() + 0.08
            while time.monotonic() < drained_deadline:
                time.sleep(0.005)
            with notify.lock:
                self.assertEqual(notify.calls, calls_before_drain)
        finally:
            wake.close()


class _ScheduledWake:
    def __init__(self, callbacks):
        self.now = 0.0
        self.callbacks = iter(callbacks)
        self.next = next(self.callbacks, None)

    def clock(self):
        return self.now

    def clear(self):
        pass

    def wait(self, duration):
        if self.next is not None and self.next[0] <= self.now + duration:
            self.now, call = self.next
            self.next = next(self.callbacks, None)
            call()
        else:
            self.now += duration


class ReplyHoldWakeTests(unittest.TestCase):
    def test_live_sidebar_change_interrupts_before_model_finishes_and_keeps_hint(self):
        wal = Mock()
        wal.latest_pending_message_at.return_value = None
        replies = Queue()
        wake = _ScheduledWake([
            (1.0, lambda: setattr(wal.latest_pending_message_at, "return_value", 1.0)),
            (8.0, lambda: replies.put("model result")),
        ])
        preview = Mock(side_effect=lambda: wake.now >= 1.5)
        result = bridge.wait_for_group_reply(replies, wake, wal, preview, clock=wake.clock)
        self.assertEqual(result, "sidebar_changed")
        self.assertAlmostEqual(wake.now, 1.65)
        self.assertEqual(preview.call_count, 2)
        self.assertTrue(replies.empty())
        self.assertEqual(wal.latest_pending_message_at(), 1.0)
        wal.take_pending_with_timing.assert_not_called()

    def test_old_hint_keeps_hold_until_actual_reply(self):
        wal = Mock()
        wal.latest_pending_message_at.return_value = -1.0
        replies = Queue()
        wake = _ScheduledWake([(2.0, lambda: replies.put("model result"))])
        preview = Mock(return_value=True)
        result = bridge.wait_for_group_reply(replies, wake, wal, preview, clock=wake.clock)
        self.assertEqual(result, "reply_ready")
        self.assertEqual(wake.now, 2.0)
        preview.assert_not_called()

    def test_fresh_hint_without_preview_change_waits_with_bounded_probe_rate(self):
        wal = Mock()
        wal.latest_pending_message_at.return_value = 0.0
        wake = _ScheduledWake([])
        preview = Mock(return_value=False)
        result = bridge.wait_for_group_reply(Queue(), wake, wal, preview, timeout=2, clock=wake.clock)
        self.assertEqual(result, "timeout")
        self.assertEqual(wake.now, 2.0)
        self.assertEqual(preview.call_count, 4)


if __name__ == "__main__":
    unittest.main()
