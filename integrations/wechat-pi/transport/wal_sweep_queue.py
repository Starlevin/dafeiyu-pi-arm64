#!/usr/bin/env python3
"""Bounded, durable-across-slow-UI in-memory WAL recheck queue.

A WAL file change is only a hint to check *approved* conversations.
No account IDs, message payloads, credentials, databases, or sending here.
"""

def should_enqueue_wal_sweep(roles, watched, signatures, last_signatures,
                             urgent_target):
    """Only sweep when a message WAL hint has no safer known chat target.

    Sidebar-preview changes already trigger original per-chat scans. Verified
    open chats already get the existing 8 s priority rule.
    """
    if "message" not in roles or urgent_target is not None:
        return False
    for target in watched:
        if (target in signatures
                and signatures[target] != last_signatures.get(target)):
            return False
    return True


class WalSweepQueue:
    def __init__(self, clock, *, min_cycle_gap_s=30.0, retry_s=25.0,
                 max_attempts=2, max_age_s=180.0):
        self.clock = clock
        self.min_cycle_gap_s = min_cycle_gap_s
        self.retry_s = retry_s
        self.max_attempts = max_attempts
        self.max_age_s = max_age_s
        self.pending = {}
        self.last_cycle_at = float("-inf")
        self.stats = {"cycles": 0, "complete": 0, "failed": 0,
                      "expired": 0, "coalesced": 0}

    def _expire(self, now):
        for target, item in tuple(self.pending.items()):
            if now - item["created"] >= self.max_age_s:
                self.pending.pop(target, None)
                self.stats["expired"] += 1

    def hint(self, roles, targets, *, now=None):
        """Enqueue a cycle only on a message WAL change; sessions alone never enqueue."""
        now = self.clock() if now is None else now
        self._expire(now)
        if "message" not in roles:
            return False
        if now - self.last_cycle_at < self.min_cycle_gap_s:
            self.stats["coalesced"] += 1
            return False
        approved = set(targets)
        if not approved:
            return False
        self.last_cycle_at = now
        for target in approved:
            # Never shorten the queue by replacing a pending entry.
            self.pending.setdefault(target, {"created": now, "attempts": 0,
                                             "retry_at": now})
        self.stats["cycles"] += 1
        return True

    def due(self, target, *, now=None):
        """Whether to attempt a supplemental read; normal periodic reads stay enabled."""
        now = self.clock() if now is None else now
        self._expire(now)
        item = self.pending.get(target)
        return item is not None and now >= item["retry_at"]

    def completed(self, target):
        """Called ONLY after a verified UI read + baseline update."""
        if target in self.pending:
            del self.pending[target]
            self.stats["complete"] += 1

    def failed(self, target, *, now=None):
        """Bound retries. On failure, normal UI polling remains the fallback."""
        now = self.clock() if now is None else now
        item = self.pending.get(target)
        if item is None:
            return
        self.stats["failed"] += 1
        item["attempts"] += 1
        if item["attempts"] >= self.max_attempts:
            self.pending.pop(target, None)
        else:
            item["retry_at"] = now + self.retry_s

    def pending_count(self):
        return len(self.pending)


class VerifiedWalRescanQueue:
    """Short-lived per-chat rechecks for already verified open groups only.

    The caller must supply a configured group that was independently verified
    open when the WAL hint arrived, and verify it again before reading. Entries
    contain only the configured target key and monotonic timestamps; no WAL
    payload or message text is retained.
    """

    def __init__(self, clock, *, ttl_s=45.0, retry_s=8.0, max_pending=64):
        self.clock = clock
        self.ttl_s = ttl_s
        self.retry_s = retry_s
        self.max_pending = max_pending
        self.pending = {}
        self.stats = {"queued": 0, "coalesced": 0, "completed": 0,
                      "failed": 0, "expired": 0, "full": 0,
                      "empty_refresh_queued": 0, "empty_refresh_cancelled": 0}

    def _expire(self, now):
        for target, item in tuple(self.pending.items()):
            if now - item["created"] >= self.ttl_s:
                self.pending.pop(target, None)
                self.stats["expired"] += 1

    def hint(self, target, *, now=None, earliest_at=None, source_at=None):
        now = self.clock() if now is None else now
        self._expire(now)
        if not isinstance(target, str) or not target:
            return False
        due_at = max(now, now if earliest_at is None else earliest_at)
        item = self.pending.get(target)
        if item is not None:
            # Merge duplicate file notifications without extending TTL or
            # postponing an already scheduled recheck. Once a short empty-view
            # refresh is armed, noise must not accelerate its one-shot deadline.
            if not item.get("empty_refresh_pending", False):
                item["due_at"] = min(item["due_at"], due_at)
            self.stats["coalesced"] += 1
            return False
        if len(self.pending) >= self.max_pending:
            self.stats["full"] += 1
            return False
        self.pending[target] = {"created": now, "due_at": due_at, "attempts": 0,
                                "source_at": source_at, "empty_refresh_count": 0,
                                "empty_refresh_pending": False}
        self.stats["queued"] += 1
        return True

    def source_at(self, target):
        """Return a queued hint's monotonic source time for redacted timing."""
        item = self.pending.get(target)
        value = item.get("source_at") if item else None
        return value if isinstance(value, (int, float)) else None

    def due(self, target, *, now=None):
        now = self.clock() if now is None else now
        self._expire(now)
        item = self.pending.get(target)
        return item is not None and now >= item["due_at"]

    def retry_empty_once(self, target, *, now=None, delay_s=0.65):
        """Schedule one short refresh after a valid empty scan; never extend it."""
        now = self.clock() if now is None else now
        self._expire(now)
        item = self.pending.get(target)
        if (item is None or item.get("attempts", 0) != 0
                or item.get("empty_refresh_count", 0) >= 1
                or item.get("empty_refresh_pending", False)):
            return False
        delay_s = float(delay_s)
        if not 0 < delay_s <= 0.8:
            raise ValueError("empty refresh delay must be in (0, 0.8] seconds")
        item["empty_refresh_count"] = 1
        item["empty_refresh_pending"] = True
        item["due_at"] = now + delay_s
        self.stats["empty_refresh_queued"] += 1
        return True

    def empty_refresh_pending(self, target):
        item = self.pending.get(target)
        return bool(item and item.get("empty_refresh_pending", False))

    def cancel_empty_refresh(self, target):
        item = self.pending.get(target)
        if item is None or not item.get("empty_refresh_pending", False):
            return False
        self.pending.pop(target, None)
        self.stats["empty_refresh_cancelled"] += 1
        return True

    def wait_timeout(self, interval, now=None):
        """Shorten idle wake only for the bounded empty-view refresh."""
        now = self.clock() if now is None else now
        self._expire(now)
        deadlines = [item["due_at"] for item in self.pending.values()
                     if item.get("empty_refresh_pending", False)]
        if not deadlines:
            return interval
        return min(interval, max(0.01, min(deadlines) - now))

    def completed(self, target):
        if self.pending.pop(target, None) is not None:
            self.stats["completed"] += 1

    def failed(self, target, *, now=None, max_attempts=3):
        now = self.clock() if now is None else now
        item = self.pending.get(target)
        if item is None:
            return
        self.stats["failed"] += 1
        item["attempts"] += 1
        item["empty_refresh_pending"] = False
        if item["attempts"] >= max_attempts or now - item["created"] + self.retry_s >= self.ttl_s:
            self.pending.pop(target, None)
        else:
            item["due_at"] = now + self.retry_s

    def pending_count(self):
        return len(self.pending)
