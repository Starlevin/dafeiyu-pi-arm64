import contextlib
import hashlib
import importlib.util
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[3]
TRANSPORT = ROOT / "integrations" / "wechat-pi" / "transport"

# The production module is Linux-first (fcntl/flock). The unit tests replace only
# the OS lock and atomic writer so its state transitions can run on Windows too.
_fcntl = sys.modules.get("fcntl")
if _fcntl is None:
    sys.modules["fcntl"] = types.SimpleNamespace(flock=lambda *args: None)
try:
    spec = importlib.util.spec_from_file_location(
        "bridge_health_under_test", TRANSPORT / "bridge_health.py")
    bridge_health = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bridge_health)
finally:
    if _fcntl is None:
        sys.modules.pop("fcntl", None)


class BridgeHealthIngressTests(unittest.TestCase):
    def make_health(self, path):
        health = bridge_health.Health(path)

        @contextlib.contextmanager
        def no_disk_lock():
            yield

        def save_state():
            payload = dict(health.state)
            payload["outbound_traces"] = health.outbound
            path.write_text(json.dumps(payload), encoding="utf-8")
            return True

        health._disk_lock = no_disk_lock
        health._save_unlocked = save_state
        return health

    def test_only_guarded_queue_acceptance_persists_a_redacted_short_marker(self):
        with tempfile.TemporaryDirectory(prefix="wechat-health-ingress-") as directory:
            path = Path(directory) / "health.json"
            health = self.make_health(path)
            health._prune = lambda: self.fail(
                "ordinary health updates must not scan the outbound ledger")
            trace = "ab" * 32
            current = [1000.0]
            with patch.object(bridge_health.time, "time", side_effect=lambda: current[0]):
                health.update("queue", "accepted", trace)
                first = json.loads(path.read_text(encoding="utf-8"))["last_ingress"]
                self.assertEqual(first, {
                    "time": 1000.0,
                    "fingerprint": hashlib.sha256(trace.encode("ascii")).hexdigest(),
                })
                self.assertNotIn("trace", first)

                current[0] += 0.1
                health.update("generation", "started", trace)
                continued = json.loads(path.read_text(encoding="utf-8"))["last_ingress"]
                self.assertEqual(continued, first)

                current[0] += 0.1
                health.update("queue", "accepted", trace)
                duplicate = json.loads(path.read_text(encoding="utf-8"))["last_ingress"]
                self.assertEqual(duplicate, first, "a duplicate enqueue must not extend its TTL")

                health.update("queue", "queue_full", "cd" * 32)
                rejected = json.loads(path.read_text(encoding="utf-8"))
                self.assertEqual(rejected["last_ingress"], first)

                current[0] += bridge_health.Health.INGRESS_RETENTION_SECONDS + 0.1
                health.update("generation", "started", trace)
                expired = json.loads(path.read_text(encoding="utf-8"))
                self.assertNotIn("last_ingress", expired)

    def test_invalid_or_non_hex_trace_never_creates_marker(self):
        with tempfile.TemporaryDirectory(prefix="wechat-health-invalid-") as directory:
            path = Path(directory) / "health.json"
            health = self.make_health(path)
            health.update("queue", "accepted", "not-a-trace")
            self.assertNotIn("last_ingress", json.loads(path.read_text(encoding="utf-8")))

    def test_current_worker_clears_generation_without_overwriting_another_event(self):
        with tempfile.TemporaryDirectory(prefix="wechat-generation-health-") as directory:
            path = Path(directory) / "health.json"
            health = self.make_health(path)
            trace = "a" * 64
            owner = {"pid": bridge_health.os.getpid(), "thread": bridge_health.get_native_id(),
                     "start_ticks": "57"}
            with patch.object(bridge_health, "generation_worker", return_value=owner):
                health.update("generation", "started", trace)
            current = json.loads(path.read_bytes())
            self.assertTrue(current["generation_tracking"])
            self.assertEqual(current["active_generation"]["thread"], owner["thread"])
            health.update("queue", "accepted", "b" * 64)
            health.finish_generation("b" * 64)
            self.assertEqual(json.loads(path.read_bytes())["active_generation"]["trace"], trace)
            health.finish_generation(trace)
            finished = json.loads(path.read_bytes())
            self.assertNotIn("active_generation", finished)
            self.assertEqual(finished["last_event"]["phase"], "queue")


if __name__ == "__main__":
    unittest.main()
