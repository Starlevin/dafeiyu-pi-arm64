import unittest
from unittest.mock import patch

import tool_routes_qzone as routes
import unified_console


class Handler:
    owner_authenticated = False
    path = "/api/qzone/latest"
    app = None
    def __init__(self):
        self.response = None
    def json(self, code, payload):
        self.response = (code, payload)


class ReadOnlyRoutesTests(unittest.TestCase):
    def tearDown(self):
        routes.reset()

    def test_owner_required_for_qzone_reads(self):
        routes.configure(owner_state={"authenticated": False})
        handler = Handler()
        self.assertTrue(routes.resolve(handler, "GET", "/api/qzone/latest"))
        self.assertEqual(handler.response[0], 401)
        self.assertEqual(handler.response[1]["error"]["code"], "owner_unauth")

    def test_manual_refresh_requires_confirmation_and_owner(self):
        routes.configure(owner_state={"authenticated": True})
        handler = Handler()
        handler.path = "/api/qzone/refresh"
        handler.read_json = lambda: {"confirm": False}
        self.assertTrue(routes.resolve(handler, "POST", handler.path))
        self.assertEqual(handler.response[0], 400)
        self.assertEqual(handler.response[1]["error"]["code"], "bad_request")

    def test_mutation_endpoints_are_not_routed(self):
        for path in ("/api/qzone/publish", "/api/qzone/followup", "/api/qzone/config"):
            self.assertFalse(routes.resolve(Handler(), "POST", path))

    def test_deployment_switch_is_opt_in(self):
        with patch.dict("os.environ", {}, clear=True):
            self.assertFalse(unified_console.qzone_read_enabled())
        with patch.dict("os.environ", {"DAFEIYU_QZONE_READ_ENABLED": "1"}, clear=True):
            self.assertTrue(unified_console.qzone_read_enabled())


if __name__ == "__main__":
    unittest.main()
