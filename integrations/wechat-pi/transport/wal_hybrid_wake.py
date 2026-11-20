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
                    if not roles:
                        continue
                    self.events+=1
                    # Preserve the coarse event role across burst coalescing.
                    # It is a hint, not a message or a chat identifier.
                    with self._pending_lock:
                        self._pending_roles.update(r for r in roles if r in ('message', 'session'))
                    now=time.monotonic()
                    if now-self._last_pulse>=self.min_gap_s:
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
        with self._pending_lock:
            result=set(self._pending_roles)
            self._pending_roles.clear()
        return result

    def close(self):
        self.stop.set()
        if self.thread is not None:
            self.thread.join(timeout=1)

def bounded_wait(wake, timeout=3.0):
    """A wake hint only shortens an idle sleep; never creates a message."""
    fired=wake.wait(timeout=max(0.0,timeout))
    wake.clear()
    return fired
