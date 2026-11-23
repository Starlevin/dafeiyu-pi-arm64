#!/usr/bin/env python3
"""Offline, bounded tests for coalesced WAL wake lifecycle timing."""
import threading
import time
import unittest
import sys
import types

# The production inotify module binds Linux libc symbols during import. This
# scheduler unit test injects a fake watcher and deliberately does not load or
# exercise any OS or database integration.
_linux_watcher_stub = types.ModuleType("wal_inotify_readonly")
_linux_watcher_stub.ReadOnlyWalEvents = object
sys.modules.setdefault("wal_inotify_readonly", _linux_watcher_stub)

from wal_hybrid_wake import WalWake


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

            roles, first_seen = wake.take_pending_with_timing()
            self.assertEqual(roles, {"message"})
            self.assertEqual(set(first_seen), {"message"})
            self.assertLessEqual(first_seen["message"], time.monotonic())

            drained_deadline = time.monotonic() + 0.08
            while time.monotonic() < drained_deadline:
                time.sleep(0.005)
            with notify.lock:
                self.assertEqual(notify.calls, calls_before_drain)
        finally:
            wake.close()


if __name__ == "__main__":
    unittest.main()
