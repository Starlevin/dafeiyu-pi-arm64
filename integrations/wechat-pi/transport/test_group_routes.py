"""Offline route tests; fixtures never call the bridge or send chat messages."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import group_routes


class GroupRouteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="wechat-route-test-")
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "routes.json"

    def write(self, data):
        self.path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        group_routes._cache.update({"path": None, "mtime": 0.0, "data": None})

    def test_explicit_single_group_uses_only_loopback_swift_endpoint(self):
        self.write({"enabled": True, "default": "old", "groups": {
            "别野": "swift", "Apex": "old",
        }, "testGroups": ["别野"]})
        self.assertEqual(group_routes.route_for(
            "别野", config_path=self.path, swift_url="http://127.0.0.1:41143"),
            ("swift", "http://127.0.0.1:41143"))
        self.assertEqual(group_routes.route_for(
            "其他群", config_path=self.path, swift_url="http://127.0.0.1:41143"),
            ("old", group_routes.OLD_CORE_URL))
        self.assertEqual(group_routes.route_for(
            "Apex", config_path=self.path, swift_url="http://127.0.0.1:41143"),
            ("old", group_routes.OLD_CORE_URL))

    def test_disabled_invalid_and_non_loopback_routes_fail_closed(self):
        cases = [
            ({"enabled": False, "groups": {"别野": "swift"}}, "http://127.0.0.1:41143"),
            ({"enabled": True, "groups": {"别野": "swift"}, "testGroups": []}, "http://127.0.0.1:41143"),
            ({"enabled": True, "groups": {"别野": "swift"}}, "http://example.com:41143"),
        ]
        for config, endpoint in cases:
            with self.subTest(config=config, endpoint=endpoint):
                self.write(config)
                self.assertEqual(group_routes.route_for(
                    "别野", config_path=self.path, swift_url=endpoint),
                    ("old", group_routes.OLD_CORE_URL))

    def test_missing_or_malformed_route_file_stays_on_old_core(self):
        self.assertEqual(group_routes.route_for(
            "别野", config_path=self.path, swift_url="http://127.0.0.1:41143"),
            ("old", group_routes.OLD_CORE_URL))
        self.path.write_text("{bad json", encoding="utf-8")
        group_routes._cache.update({"path": None, "mtime": 0.0, "data": None})
        self.assertEqual(group_routes.route_for(
            "别野", config_path=self.path, swift_url="http://127.0.0.1:41143"),
            ("old", group_routes.OLD_CORE_URL))


if __name__ == "__main__":
    unittest.main(verbosity=2)
