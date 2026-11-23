import unittest
import importlib.util
import json
import shutil
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend.status_adapter import normalize_activity, normalize_bridge_health


class ActivityStatusTests(unittest.TestCase):
    NOW = 1_800_000_000

    def event(self, phase, reason, **extra):
        return {"time": self.NOW, "phase": phase, "reason": reason, **extra}

    def test_authoritative_input_stages(self):
        self.assertEqual(normalize_activity(self.event("received", "group"), now=self.NOW), "received")
        self.assertEqual(normalize_activity(self.event("generation", "started"), now=self.NOW), "thinking")
        self.assertEqual(normalize_activity(self.event("generation", "ready"), now=self.NOW), "replying")
        self.assertEqual(normalize_activity(self.event("submit", "started"), now=self.NOW), "replying")

    def test_local_handoff_is_not_a_send_receipt(self):
        event = self.event("local_handoff", "accepted", local_handoff=True)
        self.assertEqual(normalize_activity(event, now=self.NOW), "replying")
        self.assertEqual(normalize_activity(self.event("confirmation", "accepted", success=True), now=self.NOW), "idle")
        self.assertEqual(normalize_activity(self.event("confirmation", "visible_outgoing", success=False), now=self.NOW), "error")
        self.assertEqual(normalize_activity(self.event("confirmation", "visible_outgoing", success=True), now=self.NOW), "sent")

    def test_errors_and_expired_or_invalid_events_fail_closed(self):
        self.assertEqual(normalize_activity(self.event("submit", "failed", failed=True), now=self.NOW), "error")
        self.assertEqual(normalize_activity(self.event("generation", "started"), now=self.NOW + 61), "idle")
        self.assertEqual(normalize_activity(self.event("generation", "started"), now=self.NOW - 1), "idle")
        self.assertEqual(normalize_activity({"time": "bad", "phase": "received", "reason": "group"}, now=self.NOW), "idle")
        self.assertEqual(normalize_activity(self.event("unknown", "received"), now=self.NOW), "idle")
        self.assertEqual(normalize_activity(None, now=self.NOW), "idle")

    def test_bridge_health_requires_success_marker_and_uses_latest_event(self):
        event = self.event("confirmation", "visible_outgoing")
        self.assertEqual(normalize_bridge_health({"last_event": event}, now=self.NOW), "idle")
        self.assertEqual(normalize_bridge_health({"last_event": event, "last_success": event}, now=self.NOW), "sent")
        received = self.event("received", "group")
        self.assertEqual(normalize_bridge_health({"last_success": event, "last_event": received}, now=self.NOW), "received")
        failed = self.event("confirmation", "send_exception")
        prior_success = self.event("confirmation", "visible_outgoing")
        prior_success["time"] -= 1
        self.assertEqual(normalize_bridge_health({"last_event": failed, "last_success": prior_success, "last_failure": failed}, now=self.NOW), "error")
        self.assertEqual(normalize_bridge_health({}, now=self.NOW), "idle")

    def test_house_status_http_adds_only_normalized_state_and_preserves_legacy_shape(self):
        source_root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory(prefix="fish-house-status-test-") as temp:
            root = Path(temp)
            shutil.copy2(source_root / "server.py", root / "server.py")
            shutil.copytree(source_root / "backend", root / "backend")
            web = root / "web" / "fish_house"
            web.mkdir(parents=True)
            (web / "theme.json").write_text(
                json.dumps({"config": {"videos": {}}}), encoding="utf-8")

            saved_backend = {key: value for key, value in sys.modules.items()
                             if key == "backend" or key.startswith("backend.")}
            for key in saved_backend:
                del sys.modules[key]
            sys.path.insert(0, str(root))
            try:
                spec = importlib.util.spec_from_file_location("fish_house_fixture_server", root / "server.py")
                server = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(server)
                now = int(time.time())
                trace = "a" * 36
                current = {"diagnostics": {"fresh": True}, "gateways": [{
                    "platform": "wechat", "enabled": True,
                    "bridge": {"online": True, "listener": True, "accountOk": True},
                    "received": 4, "replied": 3, "sent": 3, "failed": 0,
                    "lastMessageAt": now * 1000,
                }], "model": "fixture-model"}
                current["bridgeDiagnostics"] = {
                    "last_event": {"time": now, "phase": "generation", "reason": "started", "trace": trace},
                    "fresh": True,
                }

                class Snapshot:
                    def get(self):
                        return {"ok": True, "timestamp": int(time.time()),
                                "wechat": server.derive_wechat(current),
                                "qq": {"busy": False},
                                "activity": {"phase": "idle", "source": None, "verified": False},
                                "mode": "live-readonly", "persona": {"character": "whale"}}

                server.snapshot = Snapshot()
                httpd = server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
                thread = threading.Thread(target=httpd.serve_forever, daemon=True)
                thread.start()
                try:
                    url = f"http://127.0.0.1:{httpd.server_port}/api/status"

                    def get_status():
                        with urllib.request.urlopen(url, timeout=2) as response:
                            return json.loads(response.read().decode("utf-8"))

                    payload = get_status()
                    wechat = payload["wechat"]
                    self.assertEqual(wechat["activity_state"], "thinking")
                    self.assertTrue(wechat["connected"])
                    self.assertTrue(wechat["running"])
                    self.assertIn("transport", wechat)
                    self.assertIn("last_activity", wechat)
                    self.assertNotIn(trace, json.dumps(payload))

                    receipt = {"time": int(time.time()), "phase": "confirmation",
                               "reason": "visible_outgoing", "trace": trace}
                    current["bridgeDiagnostics"] = {
                        "last_event": receipt, "last_success": dict(receipt), "fresh": True,
                    }
                    self.assertEqual(get_status()["wechat"]["activity_state"], "sent")

                    current["bridgeDiagnostics"] = {"last_event": {
                        "time": int(time.time()) - 61, "phase": "generation", "reason": "ready",
                    }, "fresh": False}
                    expired = get_status()
                    self.assertEqual(expired["wechat"]["activity_state"], "idle")
                    self.assertIn("last_activity", expired["wechat"])
                finally:
                    httpd.shutdown()
                    httpd.server_close()
                    thread.join(timeout=2)
            finally:
                sys.path.remove(str(root))
                for key in [key for key in sys.modules
                            if key == "backend" or key.startswith("backend.")]:
                    del sys.modules[key]
                sys.modules.update(saved_backend)


if __name__ == "__main__":
    unittest.main()
