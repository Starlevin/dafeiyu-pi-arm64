from datetime import date
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from daily_digest import make_digest


DAY = date(2026, 11, 23)
SCRIPT = Path(__file__).with_name("daily_digest.py")


class DailyDigestTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.ledger = Path(folder.name, "ledger.json")
        self.cache = Path(folder.name, "summary")

    def seed(self, rows=None):
        rows = rows if rows is not None else {
            "private-slot-name": [10, 5, 15, 1, 0],
            "second-private-slot-name": [4, 6, 10, 2, 1],
        }
        self.ledger.write_text(json.dumps({
            "schema": 2, "buckets": {
                DAY.isoformat(): rows, "2026-11-22": {"old": [20, 5, 25, 1, 0]},
            },
            "slots": {"private-slot-name": ["private-provider", "private-model"]},
            "seen": {"private-request-id": 100}, "reserved": ["private-token"],
            "message": "private-chat-text",
        }), encoding="utf-8")

    def test_daily_cli_produces_only_date_and_aggregates(self):
        self.seed()
        ledger_before = self.ledger.read_bytes()
        run = subprocess.run([
            sys.executable, "-B", str(SCRIPT), "--ledger", str(self.ledger),
            "--cache-dir", str(self.cache), "--date", DAY.isoformat(),
        ], text=True, capture_output=True, check=True, timeout=10)
        expected = {"version": 1, "date": DAY.isoformat(), "status": "fresh",
                    "totals": {"in": 14, "out": 11, "total": 25, "calls": 3, "missing": 1}}
        self.assertEqual(json.loads(run.stdout), expected)
        self.assertEqual(run.stderr, "")
        self.assertEqual(self.ledger.read_bytes(), ledger_before)
        self.assertEqual(json.loads((self.cache / f"{DAY}.json").read_bytes()), expected)

    def test_corrupt_input_falls_back_without_replacing_cache(self):
        self.seed()
        fresh = make_digest(self.ledger, self.cache, DAY)
        path = self.cache / f"{DAY}.json"
        saved = path.read_bytes()
        self.ledger.write_bytes(b"{bad input private payload")
        seen = make_digest(self.ledger, self.cache, DAY)
        self.assertEqual(seen, {**fresh, "status": "cached"})
        self.assertEqual(path.read_bytes(), saved)

    def test_missing_day_does_not_use_other_day_or_invent_zeros(self):
        self.seed()
        make_digest(self.ledger, self.cache, DAY)
        absent = make_digest(self.ledger, self.cache, date(2026, 11, 24))
        self.assertEqual(absent, {"version": 1, "date": "2026-11-24",
                                  "status": "unavailable", "totals": None})

    def test_invalid_deep_json_falls_back(self):
        self.seed()
        make_digest(self.ledger, self.cache, DAY)
        self.ledger.write_bytes(b"[" * 5000)
        self.assertEqual(make_digest(self.ledger, self.cache, DAY)["status"], "cached")

    def test_invalid_counters_are_not_coerced(self):
        self.seed({"some-slot": [0, 0, 0, True, 2]})
        self.assertEqual(make_digest(self.ledger, self.cache, DAY)["status"], "unavailable")

    def test_cache_write_failure_still_returns_fresh_result(self):
        self.seed()
        with patch("daily_digest.os.replace", side_effect=PermissionError):
            seen = make_digest(self.ledger, self.cache, DAY)
        self.assertEqual(seen["status"], "fresh")
        self.assertEqual(seen["cache_write"], "failed")
        self.assertEqual(seen["totals"]["calls"], 3)
        self.assertEqual(list(self.cache.iterdir()), [])

    def test_cache_is_not_allowed_to_overwrite_source(self):
        self.ledger = self.cache / f"{DAY}.json"
        self.cache.mkdir()
        self.seed()
        saved = self.ledger.read_bytes()
        with self.assertRaises(ValueError):
            make_digest(self.ledger, self.cache, DAY)
        self.assertEqual(self.ledger.read_bytes(), saved)


if __name__ == "__main__":
    unittest.main()
