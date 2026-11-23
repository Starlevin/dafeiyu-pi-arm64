"""Tests for the origin-tool bridge -- synthetic backends only.

Every upstream is `fixtures/fake_box.py`, so write actions (restart, config
update, import) are exercised WITHOUT touching the Pi. Reads of real files use
temp directories.

Run:  python -m unittest test_tool_routes -v
"""

from __future__ import annotations

import http.client
import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.parse import urlsplit

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE / "fixtures"))

import tool_routes  # noqa: E402
import unified_console as uc  # noqa: E402
import napcat_tools as nt  # noqa: E402
from fake_box import FakeBox  # noqa: E402
from tool_common import ToolError, parse_version, version_at_least  # noqa: E402


class MountedHandler(uc.Handler):
    """The entire integration: bridge first, then the console."""

    def do_GET(self):
        if not self.gate():
            return
        if tool_routes.resolve(self, "GET", urlsplit(self.path).path):
            return
        return super().do_GET()

    def do_POST(self):
        if not self.gate():
            return
        if tool_routes.resolve(self, "POST", urlsplit(self.path).path):
            return
        return super().do_POST()


class Client:
    def __init__(self, port):
        self.port = port
        self.cookie = ""

    def req(self, method, path, body=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=25)
        h = {"Host": "127.0.0.1:%d" % self.port, "Accept": "application/json"}
        if self.cookie:
            h["Cookie"] = self.cookie
        if body is not None:
            h["Content-Type"] = "application/json"
        if method == "POST":
            h["Origin"] = "http://127.0.0.1:%d" % self.port
        raw = json.dumps(body, ensure_ascii=False).encode() if body is not None else None
        conn.request(method, path, body=raw, headers=h)
        resp = conn.getresponse()
        data = resp.read()
        setc = resp.getheader("Set-Cookie")
        if setc:
            self.cookie = setc.split(";", 1)[0]
        status = resp.status
        ctype = resp.getheader("Content-Type", "")
        conn.close()
        parsed = None
        if data and "json" in ctype:
            try:
                parsed = json.loads(data)
            except ValueError:
                parsed = None
        return status, parsed, data


class BridgeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix="dafeiyu-bridge-")
        cls.root = Path(cls.tmp.name)
        # Fake NapCat config/log dirs so the file browser has real files.
        cls.conf_dir = cls.root / "napcat/qq/opt/QQ/resources/app/config"
        cls.log_dir = cls.root / "napcat/qq/opt/QQ/resources/app/logs"
        cls.conf_dir.mkdir(parents=True)
        cls.log_dir.mkdir(parents=True)
        (cls.conf_dir / "webui.json").write_text(
            json.dumps({"host": "127.0.0.1", "port": 6099, "token": "synthetic-token"}),
            encoding="utf-8")
        (cls.conf_dir / "onebot11_1.json").write_text(json.dumps({
            "network": {"websocketServers": [
                {"name": "engine", "enable": True, "host": "127.0.0.1",
                 "port": 3001, "token": "synthetic-onebot-token"}]}}), encoding="utf-8")
        cls.secret_dir = cls.root / "napcat/secrets"
        cls.secret_dir.mkdir(parents=True)
        (cls.secret_dir / "onebot.token").write_text("synthetic-onebot-token",
                                                     encoding="utf-8")
        (cls.log_dir / "2026-11-11_23-00-00.000.log").write_text(
            "start ok\ntoken=leak\n", encoding="utf-8")

        cls.cfg = {
            "stateDir": str(cls.root / "state"),
            "uiDir": str(HERE.parent / "ui"),
            "lanHost": "127.0.0.1", "lanPort": 0, "loopPort": 0,
            "lanNets": ["127.0.0.0/8", "::1/128"],
            "extraHosts": ["127.0.0.1", "localhost"],
            "napcatBase": "http://127.0.0.1:6099",
            "napcatRuntime": str(cls.root / "napcat"),
            "napcatWebUiConfig": str(cls.conf_dir / "webui.json"),
            "qqEngineLog": str(cls.root / "fixture-engine.log"),
            "watchdogState": str(cls.root / "absent.json"),
        }
        cls.srv_app = uc.App(dict(cls.cfg), None)
        cls.srv = uc.ConsoleServer(("127.0.0.1", 0), MountedHandler, cls.srv_app)
        cls.port = cls.srv.server_address[1]
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()
        cls.tmp.cleanup()

    def setUp(self):
        self.box = FakeBox()
        self.app = uc.App(dict(self.cfg), self.box)
        tool_routes.bind_config(self.app, self.cfg)
        self.srv.app = self.app
        p = self.app.passwords.path
        if p.exists():
            p.unlink()
        self.c = Client(self.port)

    def login(self):
        token = self.app.pair_store.mint()
        s, d, _ = self.c.req("POST", "/api/setup",
                             {"pair": token, "password": "bridge-password-123"})
        self.assertEqual(s, 200, d)

    # ---- authentication -------------------------------------------------- #

    def test_every_route_requires_the_console_session(self):
        for method, path in tool_routes._ROUTES:
            if method == "GET":
                s, d, _ = self.c.req("GET", path)
                self.assertEqual(s, 401, path)
            else:
                s, d, _ = self.c.req("POST", path, {"confirm": True})
                self.assertEqual(s, 401, path)

    def test_no_second_login_surface(self):
        for method, path in tool_routes._ROUTES:
            for word in ("session", "password", "pair", "setup"):
                self.assertNotIn(word, path)

    # ---- status / qrcode ------------------------------------------------- #

    def test_status_masks_account_and_hides_secrets(self):
        self.login()
        s, d, _ = self.c.req("GET", "/api/tools/status")
        self.assertEqual(s, 200, d)
        nap = d["napcat"]
        self.assertTrue(nap["loggedIn"])
        self.assertEqual(nap["account"], "12******90")
        blob = json.dumps(d, ensure_ascii=False).lower()
        for bad in ("1234567890", "synthetic-token", "bearer", "credential"):
            self.assertNotIn(bad, blob)

    def test_qrcode_reports_logged_in_instead_of_faking(self):
        self.login()
        s, d, _ = self.c.req("GET", "/api/tools/qrcode")
        self.assertEqual(s, 200, d)
        self.assertTrue(d["loggedIn"])
        self.assertIsNone(d["qr"])
        self.assertIn("已在线", d["reason"])
        # Refresh is disabled while the account is logged in.
        self.assertFalse(d["canRefresh"])

    def test_qrcode_returns_png_when_logged_out(self):
        self.box.logged_in = False
        self.box.qr_error = False
        self.login()
        s, d, _ = self.c.req("GET", "/api/tools/qrcode")
        self.assertEqual(s, 200, d)
        self.assertFalse(d["loggedIn"])
        self.assertTrue(d["qr"]["image"].startswith("data:image/png;base64,"))
        # The opaque origin token must never be surfaced.
        self.assertNotIn("opaque-token", json.dumps(d))

    # ---- onebot / plugins / config --------------------------------------- #

    def test_onebot_lists_networks_without_tokens(self):
        self.login()
        s, d, _ = self.c.req("GET", "/api/tools/onebot")
        self.assertEqual(s, 200, d)
        self.assertTrue(d["tokenConfigured"])
        self.assertEqual(len(d["networks"]), 1)
        net = d["networks"][0]
        self.assertEqual(net["port"], 3001)
        self.assertTrue(net["hasToken"])          # presence only
        self.assertNotIn("synthetic-onebot-token", json.dumps(d))

    def test_plugins_reports_missing_manager_with_reason(self):
        self.login()
        s, d, _ = self.c.req("GET", "/api/tools/plugins")
        self.assertEqual(s, 200, d)
        self.assertFalse(d["available"])
        self.assertIn("pluginManagerNotFound", d["reason"])
        self.assertFalse(d["writable"])

    def test_config_whitelists_fields_and_exposes_edit_list(self):
        self.login()
        s, d, _ = self.c.req("GET", "/api/tools/config")
        self.assertEqual(s, 200, d)
        self.assertEqual(set(d["config"]), set(nt.CONFIG_KEYS))
        self.assertNotIn("token", d["config"])
        self.assertNotIn("sslCert", d["config"])
        self.assertEqual(d["clientIp"], "203.0.113.9")
        self.assertTrue(d["writable"])

    # ---- page-confirmed writes (all against the fake box) ---------------- #

    def test_restart_requires_one_confirmation(self):
        self.login()
        s, d, _ = self.c.req("POST", "/api/tools/restart", {"action": "restart"})
        self.assertEqual(s, 409, d)
        self.assertEqual(self.box.restarts, 0)
        s, d, _ = self.c.req("POST", "/api/tools/restart",
                             {"action": "restart", "confirm": "yes"})
        self.assertEqual(s, 409)
        self.assertEqual(self.box.restarts, 0)
        s, d, _ = self.c.req("POST", "/api/tools/restart",
                             {"action": "restart", "confirm": True})
        self.assertEqual(s, 200, d)
        self.assertEqual(self.box.restarts, 1)

    def test_restart_plan_states_what_it_will_not_do(self):
        self.login()
        s, d, _ = self.c.req("GET", "/api/tools/restart/plan")
        self.assertEqual(s, 200, d)
        joined = " ".join(d["willNotDo"])
        self.assertIn("napcat-qq.service", joined)
        self.assertIn("第二个", joined)
        self.assertTrue(d["confirmRequired"])

    def test_config_write_is_whitelisted_and_confirmed(self):
        self.login()
        s, d, _ = self.c.req("POST", "/api/tools/config",
                             {"action": "config.write", "confirm": True,
                              "patch": {"loginRate": 7}})
        self.assertEqual(s, 200, d)
        self.assertEqual(self.box.config["loginRate"], 7)
        # A write that tries to touch the token is refused outright.
        s, d, _ = self.c.req("POST", "/api/tools/config",
                             {"action": "config.write", "confirm": True,
                              "patch": {"token": "hijack"}})
        self.assertEqual(s, 400, d)
        self.assertIn("不允许的字段", d["error"])
        self.assertEqual(self.box.config["token"], "synthetic-token-should-never-leave")

    def test_import_accepts_only_whitelisted_shapes(self):
        self.login()
        for bad in ({"port": 0}, {"port": "abc"}, {"host": "a b"},
                    {"loginRate": 999}, {"disableWebUI": "yes"}):
            s, d, _ = self.c.req("POST", "/api/tools/import",
                                 {"action": "config.import", "confirm": True,
                                  "patch": bad})
            self.assertEqual(s, 400, (bad, d))
        s, d, _ = self.c.req("POST", "/api/tools/import",
                             {"action": "config.import", "confirm": True,
                              "patch": {"loginRate": 25, "disableWebUI": False}})
        self.assertEqual(s, 200, d)
        self.assertEqual(self.box.config["loginRate"], 25)

    def test_export_never_contains_secrets(self):
        self.login()
        s, d, _ = self.c.req("GET", "/api/tools/export")
        self.assertEqual(s, 200, d)
        self.assertNotIn("token", json.dumps(d))
        self.assertNotIn("sslCert", json.dumps(d))

    def test_version_gate_closes_writes(self):
        self.box.version = "4.99.9"
        self.login()
        s, d, _ = self.c.req("POST", "/api/tools/restart",
                             {"action": "restart", "confirm": True})
        self.assertEqual(s, 409, d)
        self.assertIn("已适配范围", d["error"])
        self.assertEqual(self.box.restarts, 0)
        # Reads keep working.
        self.assertEqual(self.c.req("GET", "/api/tools/status")[0], 200)

    # ---- logs / files ---------------------------------------------------- #

    def test_logs_redact_and_bound(self):
        self.login()
        s, d, _ = self.c.req("GET", "/api/tools/logs?source=qq-engine&lines=20")
        self.assertEqual(s, 200, d)
        self.assertIn("［含敏感内容，已隐去］", "\n".join(d["lines"]))
        self.assertGreaterEqual(d["redactedLines"], 1)

    def test_napcat_log_path_cannot_be_escaped(self):
        self.login()
        for bad in ("../../etc/passwd", "/etc/passwd", "..%2f..%2fshadow"):
            s, d, _ = self.c.req("GET", "/api/tools/logs?source=" + bad)
            self.assertEqual(s, 400, (bad, d))

    def test_file_browser_stays_inside_fixed_roots(self):
        self.login()
        s, d, _ = self.c.req("GET", "/api/tools/files?root=config")
        self.assertEqual(s, 200, d)
        names = [e["name"] for e in d["entries"]]
        self.assertIn("webui.json", names)
        # Reading the config redacts its token.
        s, d, _ = self.c.req("GET", "/api/tools/files?root=config&file=webui.json")
        self.assertEqual(s, 200, d)
        self.assertNotIn("synthetic-token", d["file"]["text"])
        self.assertIn("已隐去", d["file"]["text"])
        # Traversal and unknown roots are refused.
        self.assertEqual(self.c.req("GET",
                                    "/api/tools/files?root=config&file=../../../etc/passwd")[0], 400)
        self.assertEqual(self.c.req("GET", "/api/tools/files?root=/etc")[0], 400)

    def test_only_whitelisted_file_types_are_browsable(self):
        (self.conf_dir / "secret.bin").write_bytes(b"\x00\x01")
        self.login()
        s, d, _ = self.c.req("GET", "/api/tools/files?root=config&file=secret.bin")
        self.assertEqual(s, 400, d)

    # ---- capabilities ---------------------------------------------------- #

    def test_capabilities_explain_what_is_missing_and_why(self):
        self.login()
        s, d, _ = self.c.req("GET", "/api/tools/capabilities")
        self.assertEqual(s, 200, d)
        rows = {r["id"]: r for r in d["items"]}
        self.assertFalse(rows["terminal"]["available"])
        self.assertIn("尚未接入", rows["terminal"]["missingReason"])
        self.assertFalse(rows["qrcode"]["available"])       # logged-in fake -> -1
        self.assertIn("已登录", rows["qrcode"]["missingReason"])
        self.assertFalse(rows["plugins"]["available"])
        self.assertTrue(rows["restart"]["confirm"])
        self.assertTrue(rows["config_write"]["confirm"])
        actions = {action for action, _ in self.box.calls}
        self.assertNotIn("Process/Restart", actions)
        self.assertNotIn("QQLogin/GetQQLoginQrcode", actions)
        self.assertNotIn("QQLogin/RefreshQRcode", actions)
        # This console does not mount a terminal route.
        routes = " ".join(d["routes"])
        self.assertNotIn("terminal", routes)

    def test_console_only_guard_blocks_non_console_scope(self):
        with self.assertRaises(ToolError) as ctx:
            nt.assert_console_only("social")
        self.assertEqual(ctx.exception.status, 403)
        nt.assert_console_only("console")      # allowed

    def test_no_shell_is_spawned_by_any_route(self):
        """Restart/config go through the origin API only -- no subprocess."""
        import subprocess
        calls = []
        real = subprocess.run

        def spy(*a, **k):
            calls.append(a)
            raise AssertionError("bridge must not spawn: %r" % (a,))

        subprocess.run = spy
        try:
            self.login()
            self.c.req("GET", "/api/tools/status")
            self.c.req("GET", "/api/tools/files?root=config")
            self.c.req("POST", "/api/tools/restart",
                       {"action": "restart", "confirm": True})
        finally:
            subprocess.run = real
        self.assertEqual(calls, [])
        self.assertEqual(self.box.restarts, 1)

    def test_denied_tools_have_no_route_at_all(self):
        self.login()
        for path in ("/api/tools/terminal", "/api/tools/exec", "/api/tools/shell",
                     "/api/tools/update", "/api/tools/debug", "/api/tools/proxy"):
            self.assertEqual(self.c.req("POST", path, {"confirm": True})[0], 404, path)
            self.assertEqual(self.c.req("GET", path)[0], 404, path)

    def test_page_hooks_present(self):
        html = (HERE.parent / "ui/index.html").read_text(encoding="utf-8")
        js = (HERE.parent / "ui/admin-tools.js").read_text(encoding="utf-8")
        self.assertIn('id="toolsMount"', html)
        self.assertIn("admin-tools.js", html)
        for api in ("/api/tools/status", "/api/tools/qrcode", "/api/tools/restart",
                    "/api/tools/config", "/api/tools/files", "/api/tools/capabilities"):
            self.assertIn(api, js)
        for bad in ("innerHTML", "document.write", "eval(", "insertAdjacentHTML"):
            self.assertNotIn(bad, js)


class UnitTests(unittest.TestCase):
    def test_version_parsing(self):
        self.assertEqual(parse_version("4.18.42"), (4, 18, 42, 0))
        self.assertEqual(parse_version("v4.18.41-2"), (4, 18, 41, 0))
        for bad in ("", "abc", "4.x", None, "4.18.41.2.3"):
            self.assertIsNone(parse_version(bad), bad)
        self.assertTrue(version_at_least("4.18.42", "4.18.0"))
        self.assertFalse(version_at_least("4.17.9", "4.18.0"))
        self.assertIsNone(version_at_least("unknown", "4.18.0"))

    def test_redaction_is_line_level_and_bounded(self):
        out = nt.redact_tail("ok\ntoken=abc\nok2\n", limit_lines=10)
        self.assertEqual(out["lines"][1], "［含敏感内容，已隐去］")
        self.assertEqual(out["redacted"], 1)
        long_text = "\n".join("l%d" % i for i in range(50))
        out = nt.redact_tail(long_text, limit_lines=5)
        self.assertTrue(out["truncated"])
        self.assertEqual(out["lines"][-1], "l49")

    def test_catalog_rejects_unknown_actions(self):
        reader = nt.BoxReader({}, FakeBox())
        for action in ("UpdateNapCat/update", "auth/update_token", "../File/Read",
                       "File/Read", "Terminal/Exec"):
            with self.assertRaises(ToolError):
                reader.nap_call(action)

    def test_safe_join_blocks_traversal(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "ok.json").write_text("{}", encoding="utf-8")
            self.assertTrue(nt.safe_join(root, "ok.json").is_file())
            for bad in ("../x", "/etc/passwd", "a/../../b"):
                with self.assertRaises(ToolError):
                    nt.safe_join(root, bad)

    def test_import_patch_rejects_unknown_keys(self):
        with self.assertRaises(ToolError):
            nt.build_import_patch({"token": "x"})
        self.assertEqual(nt.build_import_patch({"port": 6099}), {"port": 6099})


if __name__ == "__main__":
    unittest.main(verbosity=2)
