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
