import pathlib
import unittest

import tool_routes_qzone as routes


class ReadOnlyPanelTests(unittest.TestCase):
    def test_only_snapshot_read_and_manual_refresh_routes_are_exposed(self):
        self.assertEqual(set(routes.routes()), {
            ("GET", "/api/qzone/latest"),
            ("POST", "/api/qzone/refresh"),
        })
        for name in ("post_publish", "post_followup", "post_config",
                     "get_status", "get_history", "get_preview"):
            self.assertFalse(hasattr(routes, name), "%s is not part of the read-only package" % name)
        self.assertFalse(hasattr(routes, "CONFIG_FIELDS"))
        for path in ("/api/qzone/publish", "/api/qzone/followup", "/api/qzone/config"):
            self.assertNotIn(("POST", path), routes.routes())

    def test_panel_uses_text_nodes_and_has_no_mutation_controls(self):
        path = pathlib.Path(__file__).parents[2] / "ui" / "qzone-panel.js"
        source = path.read_text(encoding="utf-8")
        self.assertIn("textContent", source)
        self.assertNotIn("innerHTML", source)
        self.assertNotIn("/api/qzone/publish", source)
        self.assertNotIn("/api/qzone/followup", source)
        self.assertNotIn("/api/qzone/config", source)


if __name__ == "__main__":
    unittest.main()
