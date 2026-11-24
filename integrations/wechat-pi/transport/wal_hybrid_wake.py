#!/usr/bin/env python3
"""Optional, fail-open-to-UI wake notifier for encrypted WeChat WAL writes.

Input is *filesystem metadata events only*. This never emits chat messages,
never determines chat identity, and never authorizes sending.
"""
import threading
import time
from wal_hint_readonly import find_active_wal_files
from wal_inotify_readonly import ReadOnlyWalEvents

class WalWake:
    def __init__(self, notify, *, min_gap_s=0.4, startup_log=None,
                 paths=None, watcher_factory=ReadOnlyWalEvents):
        self.notify=notify
        self._pending_lock=threading.Lock()
        self._pending_roles=set()
        self._pending_since={}
        self._latest_message_at=None
        self.min_gap_s=max(0.2,float(min_gap_s))
        self.startup_log=startup_log or (lambda *_args:None)
        self.paths=list(paths) if paths is not None else find_active_wal_files()
        self.watcher_factory=watcher_factory
        self.thread=None
        self.stop=threading.Event()
        self.pulses=0
        self.errors=0
        self.events=0
        self._last_pulse=0.
        self._last_err=""

    def start(self):
        if not self.paths:
            self.startup_log("wal_hints_no_active_files")
            return False
        self.thread=threading.Thread(target=self._run,daemon=True,name="wechat-wal-hint")
        self.thread.start()
        return True

    def _run(self):
        try:
            with self.watcher_factory(self.paths) as watcher:
                self.startup_log("wal_hints_listening",len(self.paths))
                while not self.stop.is_set():
                    roles=watcher.poll(timeout=0.25)
                    if roles:
                        self.events+=1
                        now=time.monotonic()
                        # Preserve the coarse event role across burst coalescing.
                        # It is a hint, not a message or a chat identifier.
                        with self._pending_lock:
                            for role in roles:
                                if role in ('message', 'session'):
                                    self._pending_roles.add(role)
                                    self._pending_since.setdefault(role, now)
                                    if role == 'message':
                                        self._latest_message_at=now
                    # The consumer clears its Event after wait(). If another
                    # file event races with that clear, pending roles remain
                    # here; re-pulse them until the main loop drains the roles.
                    with self._pending_lock:
                        pending=bool(self._pending_roles)
                    now=time.monotonic()
                    if pending and now-self._last_pulse>=self.min_gap_s:
                        self._last_pulse=now
                        self.pulses+=1
                        self.notify.set()
        except Exception as exc:
            self.errors+=1
            self._last_err=type(exc).__name__
            # Caller must continue its normal bounded UI polling.
            self.startup_log("wal_hints_disabled",self._last_err)

    def take_pending(self):
        """Atomically drain pending coarse WAL roles; no message access."""
        roles, _ = self.take_pending_with_timing()
        return roles

    def latest_pending_message_at(self):
        """Inspect the newest pending message-role timestamp without draining hints."""
        with self._pending_lock:
            return self._latest_message_at

    def take_pending_with_timing(self):
        """Drain roles and their first monotonic event times; no identifiers."""
        with self._pending_lock:
            result=set(self._pending_roles)
            first_seen=dict(self._pending_since)
            self._pending_roles.clear()
            self._pending_since.clear()
            self._latest_message_at=None
        return result, first_seen

    def close(self):
        self.stop.set()
        if self.thread is not None:
            self.thread.join(timeout=1)

def bounded_wait(wake, timeout=3.0):
    """A wake hint only shortens an idle sleep; never creates a message."""
    fired=wake.wait(timeout=max(0.0,timeout))
    wake.clear()
    return fired
