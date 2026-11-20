#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Unit tests for the migration dry-run validator (stdlib only)."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))
import memory_migration_check as m  # noqa: E402


def iso(days: int = 90) -> str:
    return (datetime.now(timezone.utc) + timedelta(days=days)).isoformat()


def make_item(item_id: str = "i-1", **overrides):
    item = {
        "id": item_id,
        "subject": "OWNER",
        "kind": "fact",
        "text": "喜欢清淡口味，周五晚上打副本。",
        "keywords": ["口味", "副本"],
        "importance": 2,
        "sourceIDs": ["m-1"],
        "createdAt": iso(-1),
        "updatedAt": iso(-1),
        "expiresAt": iso(90),
        "invalidatedAt": None,
    }
    item.update(overrides)
    return item


def make_state(items=None, pending=None, cold=None, recent=None, known=None, dropped=0, refused=0):
    book = {
        "items": items if items is not None else [make_item()],
        "pending": pending if pending is not None else [],
        "archivedPending": cold if cold is not None else [],
        "recent": recent if recent is not None else [],
        "knownIDs": known if known is not None else ["i-1"],
        "droppedEvents": dropped,
        "refusedEvents": refused,
    }
    return {"version": 1, "memoryBooks": {"10001:group1": book}}


def analyze(state, capacity=1024):
    return m.analyze_state(state, datetime.now(timezone.utc), capacity)


class MigrationCheckTests(unittest.TestCase):
    def test_valid_state_passes(self):
        report = analyze(make_state())
        self.assertEqual("PASS", report["verdict"], report)
        self.assertEqual(1, report["totals"]["items"])
        self.assertEqual(1, report["totals"]["active"])

    def test_duplicate_item_id_fails(self):
        report = analyze(make_state(items=[make_item(), make_item(text="冲突内容")]))
        self.assertEqual("FAIL", report["verdict"])
        self.assertEqual(1, report["scopes"][0]["duplicates"]["itemIDs"])

    def test_conflicting_pending_with_item_fails(self):
        pending = [{"id": "i-1", "subject": "OWNER", "text": "另一条", "at": iso(-1)}]
        report = analyze(make_state(pending=pending))
        self.assertEqual("FAIL", report["verdict"])
        self.assertGreaterEqual(report["scopes"][0]["duplicates"]["conflictingIDs"], 1)

    def test_pending_id_in_known_ids_but_not_item_is_not_conflict(self):
        # Real QQMemoryBook.append records pending IDs in knownIDs. That is normal
        # deduplication state, not a collision with an already-mapped item.
        pending = [{"id": "p-1", "subject": "OWNER", "text": "待整理片段", "at": iso(-1)}]
        report = analyze(make_state(pending=pending, known=["i-1", "p-1"]))
        self.assertEqual("PASS", report["verdict"], report)
        self.assertEqual(1, report["totals"]["pending"])
        self.assertEqual(0, report["scopes"][0]["duplicates"]["conflictingIDs"])

    def test_invalid_subject_fails(self):
        report = analyze(make_state(items=[make_item(subject="random")]))
        self.assertEqual("FAIL", report["verdict"])

    def test_episode_requires_conversation(self):
        report = analyze(make_state(items=[make_item(kind="episode", subject="OWNER")]))
        self.assertEqual("FAIL", report["verdict"])
        report = analyze(make_state(items=[make_item(kind="episode", subject="CONVERSATION")]))
        self.assertEqual("PASS", report["verdict"], report)

    def test_sensitive_text_is_counted_without_echo(self):
        report = analyze(make_state(items=[make_item(text="电话 13800138000，邮箱 test@example.com")]))
        self.assertEqual(1, report["totals"]["redactedItems"])
        self.assertIn("phone", report["scopes"][0]["safety"]["sensitiveKinds"])
        self.assertNotIn("13800138000", json.dumps(report, ensure_ascii=False))

    def test_queue_overflow_fails(self):
        pending = [
            {"id": f"p-{i}", "subject": "OWNER", "text": "待整理", "at": iso(-1)}
            for i in range(5)
        ]
        report = analyze(make_state(pending=pending), capacity=3)
        self.assertEqual("FAIL", report["verdict"])

    def test_malformed_root_fails(self):
        with self.assertRaises(ValueError):
            analyze([])
        with self.assertRaises(ValueError):
            analyze({"memoryBooks": []})

    def test_missing_expiry_warns(self):
        report = analyze(make_state(items=[make_item(expiresAt=None)]))
        self.assertEqual("WARN", report["verdict"])
        self.assertTrue(any("expiresAt" in w for w in report["scopes"][0]["warnings"]))

    def test_long_text_warns_truncation(self):
        report = analyze(make_state(items=[make_item(text="长" * 2500)]))
        self.assertEqual("WARN", report["verdict"])
        self.assertEqual(1, report["totals"]["truncatedItems"])

    def test_scope_key_without_colon_warns(self):
        state = make_state()
        state["memoryBooks"] = {"bad-scope": state["memoryBooks"]["10001:group1"]}
        report = analyze(state)
        self.assertEqual("WARN", report["verdict"])

    def test_duplicate_pending_warns(self):
        event = {"id": "p-1", "subject": "OWNER", "text": "待整理", "at": iso(-1)}
        report = analyze(make_state(pending=[event, dict(event)]))
        self.assertEqual("WARN", report["verdict"])
        self.assertEqual(1, report["scopes"][0]["duplicates"]["queuedIDs"])

    def test_dropped_events_warn_not_fail(self):
        report = analyze(make_state(dropped=485))
        self.assertEqual("WARN", report["verdict"])
        self.assertEqual(485, report["totals"]["droppedEvents"])

    def test_synthetic_scene_counts(self):
        state = m.synthetic_state()
        report = analyze(state)
        counts = [s["items"]["total"] for s in report["scopes"]]
        pending = [s["pending"]["total"] for s in report["scopes"]]
        self.assertEqual([0, 36, 30, 368, 65, 4], counts)
        self.assertEqual([0, 0, 0, 0, 0, 1], pending)
        self.assertEqual(549, report["totals"]["droppedEvents"])
        self.assertNotEqual("FAIL", report["verdict"])

    def test_totals_are_sum_of_scopes(self):
        state = make_state()
        state["memoryBooks"]["10001:private2"] = dict(state["memoryBooks"]["10001:group1"])
        report = analyze(state)
        self.assertEqual(2, report["totals"]["scopes"])
        self.assertEqual(2, report["totals"]["items"])
        self.assertEqual(
            sum(s["items"]["total"] for s in report["scopes"]),
            report["totals"]["items"],
        )

    def test_selftest_cli_returns_zero(self):
        proc = subprocess.run(
            [sys.executable, str(HERE / "memory_migration_check.py"), "--selftest"],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(0, proc.returncode, proc.stderr)
        self.assertIn("PASS", proc.stdout)

    def test_cli_report_and_exit_code(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = os.path.join(tmp, "state.json")
            report_path = os.path.join(tmp, "report.json")
            with open(state_path, "w", encoding="utf-8") as fh:
                json.dump(make_state(), fh, ensure_ascii=False)
            proc = subprocess.run(
                [sys.executable, str(HERE / "memory_migration_check.py"), "--state", state_path, "--out", report_path],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(0, proc.returncode, proc.stderr)
            with open(report_path, "r", encoding="utf-8") as fh:
                report = json.load(fh)
            self.assertTrue(report["dryRun"])
            self.assertEqual("PASS", report["verdict"])
            self.assertEqual(64, len(report["input"]["sha256"]))


if __name__ == "__main__":
    unittest.main()
