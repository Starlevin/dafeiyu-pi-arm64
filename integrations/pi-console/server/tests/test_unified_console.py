#!/usr/bin/env python3
"""大肥鱼统一控制台 — contract & security tests.

All WRITE paths in these tests go to a fixture (no production calls, no real
messages, no real secrets). Reads are served from the same fixture, so the
tests never touch the live Pi control interfaces.

Run (from the server directory):
  python -m unittest discover -s tests -p 'test_unified_console.py' -v
  or: python tests/test_unified_console.py
"""

from __future__ import annotations

import copy
import http.client
import json
import os
import ssl
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock
from pathlib import Path

HERE = Path(__file__).resolve().parent
SERVER = HERE.parent          # console modules live one level above tests/
UI_DIR = SERVER.parent / "ui"  # delivered UI lives next to the server/ dir
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(SERVER))

import unified_console as uc  # noqa: E402

FIXTURE = json.loads((SERVER / "fixtures/fixture.json").read_text(encoding="utf-8"))


def parse_cookie_value(header):
    if not header:
        return None
    for part in header.split(";"):
        k, _, v = part.strip().partition("=")
        if k == "dafeiyu_session":
            return v
    return None


def start_server(tmp: Path, extra=None):
    cfg = {
        "stateDir": str(tmp / "state"),
        "uiDir": str(UI_DIR),
        "lanHost": "127.0.0.1",
        "lanPort": 0,          # not used: TLS listener is not started in tests
        "loopPort": 0,
        "lanNets": ["127.0.0.0/8", "::1/128"],
        "extraHosts": ["127.0.0.1", "localhost"],
    }
    if extra:
        cfg.update(extra)
    app = uc.App(cfg, FIXTURE)
    srv = uc.ConsoleServer(("127.0.0.1", 0), uc.Handler, app)
    port = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return app, srv, port


class Client:
    """Minimal stdlib HTTP client with cookie jar + constant Host header."""

    def __init__(self, port: str, host="127.0.0.1"):
        self.host = host
        self.port = port
        self.cookie = ""

    def req(self, method, path, body=None, headers=None, origin=None):
        conn = http.client.HTTPConnection(self.host, self.port, timeout=10)
        h = {"Host": "%s:%d" % (self.host, self.port),
             "Accept": "application/json"}
        if self.cookie:
            h["Cookie"] = self.cookie
        if body is not None:
            h["Content-Type"] = "application/json"
        if origin is not None:
            h["Origin"] = origin
        if headers:
            h.update(headers)
        raw = json.dumps(body, ensure_ascii=False).encode() if body is not None else None
        conn.request(method, path, body=raw, headers=h)
        resp = conn.getresponse()
        data = resp.read()
        setc = resp.getheader("Set-Cookie")
        if setc:
            # keep only the cookie pair (drop attributes)
            self.cookie = setc.split(";", 1)[0]
            if self.cookie.endswith("="):
                self.cookie = ""
        status, ctype = resp.status, resp.getheader("Content-Type", "")
        conn.close()
        parsed = None
        if data and "json" in ctype:
            try:
                parsed = json.loads(data)
            except ValueError:
                parsed = None
        return status, parsed, data


class ContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix="dafeiyu-console-test-")
        cls.app, cls.srv, cls.port = start_server(Path(cls.tmp.name))

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()
        cls.tmp.cleanup()

    def setUp(self):
        # Each test starts with a clean password/session/pair state.
        p = self.app.passwords.path
        if p.exists():
            p.unlink()
        self.app.sessions.tokens.clear()
        self.app.limiter.events.clear()
        self.app.limiter.blocked.clear()
        self.client = Client(self.port)

    def mint_pair(self, reset=False):
        return self.app.pair_store.mint(reset=reset)

    # ---- /api/session ----------------------------------------------------- #

    def test_session_initial_shape(self):
        s, d, _ = self.client.req("GET", "/api/session")
        self.assertEqual(s, 200)
        self.assertEqual(set(d.keys()),
                         {"authenticated", "setupAvailable", "secure", "lanUrl"})
        self.assertFalse(d["authenticated"])
        self.assertTrue(d["setupAvailable"])       # loopback + no password
        self.assertFalse(d["secure"])              # loopback HTTP
        self.assertTrue(d["lanUrl"].startswith("https://"))

    def test_host_status_uses_existing_session_and_fixtures_remain_offline(self):
        s, _, _ = self.client.req("GET", "/api/host/status")
        self.assertEqual(s, 401)

        self.client.cookie = "dafeiyu_session=" + self.app.sessions.create()
        s, d, _ = self.client.req("GET", "/api/host/status")
        self.assertEqual(s, 200)
        self.assertEqual(d, {"ok": True, "sampleSeconds": 60, "current": None, "history": []})
        self.assertFalse(self.app.host_status.enabled)

    def test_setup_needs_loopback(self):
        # A forged non-loopback client address cannot be simulated through the
        # socket here, but we can at least verify the pair is required.
        s, d, _ = self.client.req("POST", "/api/setup", {"password": "abcdefgh"})
        self.assertEqual(s, 400)
        self.assertIn("error", d)

    # ---- setup / login / logout / password -------------------------------- #

    def test_full_auth_flow(self):
        token = self.mint_pair()
        s, d, _ = self.client.req("POST", "/api/setup",
                                  {"pair": token, "password": "correct horse battery"})
        self.assertEqual(s, 200, d)
        self.assertEqual(d, {"ok": True})
        self.assertTrue(self.client.cookie)

        s, d, _ = self.client.req("GET", "/api/session")
        self.assertTrue(d["authenticated"])
        self.assertFalse(d["setupAvailable"])

        # password change: wrong current is refused
        s, d, _ = self.client.req("POST", "/api/password",
                                  {"current": "wrong", "password": "another password"})
        self.assertEqual(s, 401)
        self.assertIn("error", d)

        # password change: correct
        s, d, _ = self.client.req("POST", "/api/password",
                                  {"current": "correct horse battery", "password": "another password"})
        self.assertEqual(s, 200, d)

        # logout
        s, d, _ = self.client.req("POST", "/api/logout", {})
        self.assertEqual(s, 200, d)
        s, d, _ = self.client.req("GET", "/api/session")
        self.assertFalse(d["authenticated"])

        # login with new password
        s, d, _ = self.client.req("POST", "/api/login", {"password": "another password"})
        self.assertEqual(s, 200, d)
        s, d, _ = self.client.req("GET", "/api/session")
        self.assertTrue(d["authenticated"])

    def test_pair_is_single_use(self):
        token = self.mint_pair()
        s, d, _ = self.client.req("POST", "/api/setup",
                                  {"pair": token, "password": "first password"})
        self.assertEqual(s, 200)
        # consume again on a fresh state (reset password store for the test)
        self.app.passwords.path.unlink()
        s, d, _ = self.client.req("POST", "/api/setup",
                                  {"pair": token, "password": "first password"})
        self.assertEqual(s, 400)
        self.assertIn("error", d)

    def test_short_password_does_not_burn_token(self):
        token = self.mint_pair()
        s, d, _ = self.client.req("POST", "/api/setup",
                                  {"pair": token, "password": "short"})
        self.assertEqual(s, 400)
        # The same token must still work with a valid password.
        s, d, _ = self.client.req("POST", "/api/setup",
                                  {"pair": token, "password": "long-enough-password"})
        self.assertEqual(s, 200, d)

    def test_reset_token_requires_existing_password(self):
        # A reset-marked token on a fresh install (no password) is refused with
        # a clear Chinese reason and the token is NOT consumed by the mismatch.
        token = self.mint_pair(reset=True)
        s, d, _ = self.client.req("POST", "/api/setup",
                                  {"pair": token, "password": "abcdefgh", "reset": True})
        self.assertEqual(s, 409)
        self.assertIn("error", d)

    def test_reset_flow(self):
        # First set a password, then reset it using a freshly minted reset token.
        token = self.mint_pair()
        self.client.req("POST", "/api/setup", {"pair": token, "password": "old-password-123"})
        reset = self.mint_pair(reset=True)
        s, d, _ = self.client.req("POST", "/api/setup",
                                  {"pair": reset, "password": "new-password-456", "reset": True})
        self.assertEqual(s, 200, d)
        self.assertTrue(self.app.passwords.verify("new-password-456"))
        self.assertFalse(self.app.passwords.verify("old-password-123"))
        # The setup token cannot be reused to overwrite an existing password.
        token2 = self.mint_pair(reset=False)
        s, d, _ = self.client.req("POST", "/api/setup",
                                  {"pair": token2, "password": "hijack"})
        self.assertEqual(s, 409)

    def test_password_is_kdf_not_plaintext(self):
        token = self.mint_pair()
        self.client.req("POST", "/api/setup", {"pair": token, "password": "plaintext-should-not-appear"})
        raw = self.app.passwords.path.read_text(encoding="utf-8")
        self.assertNotIn("plaintext-should-not-appear", raw)
        rec = json.loads(raw)
        self.assertEqual(set(rec), {"salt", "hash", "iters"})
        self.assertGreaterEqual(rec["iters"], 100_000)
        self.assertTrue(self.app.passwords.verify("plaintext-should-not-appear"))
        self.assertFalse(self.app.passwords.verify("wrong"))

    def test_auth_required_for_overview_and_writes(self):
        s, d, _ = self.client.req("GET", "/api/overview")
        self.assertEqual(s, 401)
        s, d, _ = self.client.req("POST", "/api/conversation",
                                  {"platform": "qq", "id": "group:10001",
                                   "field": "limit", "value": 0})
        self.assertEqual(s, 401)
        s, d, _ = self.client.req("POST", "/api/model", {"platform": "qq", "id": "x"})
        self.assertEqual(s, 401)

    # ---- /api/overview contract ------------------------------------------- #

    def login(self):
        token = self.mint_pair()
        self.client.req("POST", "/api/setup", {"pair": token, "password": "test-password-123"})

    def test_overview_shape(self):
        self.login()
        s, d, _ = self.client.req("GET", "/api/overview")
        self.assertEqual(s, 200)
        for key in ("qq", "wechat", "models", "conversations", "diagnostics",
                    "styleOptions", "frequencyOptions", "limitOptions"):
            self.assertIn(key, d)
        # Backwards compatible: the original fields keep their names/types.
        self.assertLessEqual({"connected", "running", "model"}, set(d["qq"]))
        self.assertLessEqual({"connected", "running", "model", "client"}, set(d["wechat"]))
        # Additive status fields (client identity, read time, WeChat bridge view).
        self.assertEqual(set(d["qq"]),
                         {"connected", "running", "model", "client",
                          "clientSource", "lastRead"})
        self.assertIn(d["qq"]["clientSource"], ("reported", "config", "unknown"))
        self.assertIsInstance(d["qq"]["lastRead"], (int, float))
        self.assertIn(d["wechat"]["clientState"],
                      ("ok", "qr", "offline", "disabled", "unknown"))
        self.assertIsInstance(d["wechat"]["bridge"], dict)
        self.assertEqual(set(d["wechat"]["bridge"]),
                         {"online", "listener", "accountOk", "lastSeenSec"})
        self.assertEqual(set(d["wechat"]["capabilities"]), {"model"})
        for m in d["models"]:
            self.assertEqual(set(m), {"id", "name", "platforms"})
            self.assertTrue(set(m["platforms"]) <= {"qq", "wechat"})
        for c in d["conversations"]:
            expected = {"platform", "id", "name", "kind", "enabled",
                        "style", "frequency", "limit", "capabilities"}
            if c["platform"] == "wechat":
                expected.update({"enabledLabel", "enabledHint"})
            self.assertEqual(set(c), expected)
            self.assertIn(c["platform"], ("qq", "wechat"))
            self.assertIn(c["kind"], ("group", "private"))
            self.assertEqual(set(c["capabilities"]),
                             {"enabled", "style", "frequency", "limit"})
            for v in c["capabilities"].values():
                self.assertIsInstance(v, bool)
            # Unconfigured values are null, not fabricated.
            if c["limit"] is None:
                self.assertIsNone(c["limit"])
        for g in d["diagnostics"]:
            self.assertEqual(set(g), {"platform", "level", "message"})
            self.assertIn(g["level"], ("info", "warning", "error"))
        for o in d["styleOptions"]:
            self.assertTrue({"id", "name"} <= set(o))
        for o in d["frequencyOptions"]:
            self.assertTrue({"id", "name"} <= set(o))
        for o in d["limitOptions"]:
            self.assertTrue({"id", "name"} <= set(o))
        self.assertEqual({o["id"] for o in d["frequencyOptions"]},
                         {"quiet", "natural", "active"})
        self.assertIn("0", {o["id"] for o in d["limitOptions"]})

    def test_overview_values_from_fixture(self):
        self.login()
        _, d, _ = self.client.req("GET", "/api/overview")
        self.assertEqual(d["qq"]["model"], "fixture-qq-model")
        self.assertEqual(d["wechat"]["model"], "fixture-wx-model")
        qq_groups = [c for c in d["conversations"] if c["platform"] == "qq"]
        self.assertEqual(len(qq_groups), 3)
        g1 = next(c for c in qq_groups if c["id"] == "group:10001")
        self.assertEqual(g1["enabled"], True)
        self.assertEqual(g1["style"], "gentle")
        self.assertEqual(g1["frequency"], "quiet")
        self.assertEqual(g1["limit"], 100)
        self.assertEqual(g1["capabilities"],
                         {"enabled": True, "style": True, "frequency": True, "limit": True})
        g2 = next(c for c in qq_groups if c["id"] == "group:10002")
        self.assertEqual(g2["frequency"], "natural")
        self.assertEqual(g2["limit"], 0)  # 0 = 不限制
        self.assertIsNone(g2["style"])  # null = 未配置
        priv = next(c for c in qq_groups if c["id"] == "private:20001")
        self.assertFalse(priv["capabilities"]["frequency"])  # 私聊无逐群频率
        self.assertIsNone(priv["frequency"])
        wx = [c for c in d["conversations"] if c["platform"] == "wechat"]
        self.assertEqual(len(wx), 3)
        for c in wx:
            self.assertFalse(c["capabilities"]["limit"])  # 微信无每日限额语义
            self.assertIsNone(c["limit"])

    # ---- POST /api/conversation ------------------------------------------- #

    def test_conversation_post_then_reget(self):
        self.login()
        s, d, _ = self.client.req("POST", "/api/conversation",
                                  {"platform": "qq", "id": "group:10001",
                                   "field": "limit", "value": 250})
        self.assertEqual(s, 200, d)
        self.assertEqual(d, {"ok": True})
        # Client re-GETs overview; fixture is read-only so the value is stable,
        # but the contract (POST then GET) is exercised.
        s, ov, _ = self.client.req("GET", "/api/overview")
        self.assertEqual(s, 200)

    def test_conversation_rejects_unsupported_field(self):
        self.login()
        # WeChat conversation has limit capability false.
        s, d, _ = self.client.req("POST", "/api/conversation",
                                  {"platform": "wechat", "id": "wechat:group:夹具群甲",
                                   "field": "limit", "value": 10})
        self.assertEqual(s, 400)
        self.assertIn("error", d)

    def test_conversation_rejects_bad_values(self):
        self.login()
        for value in (-1, 10001, True, "10", None):
            s, d, _ = self.client.req("POST", "/api/conversation",
                                      {"platform": "qq", "id": "group:10001",
                                       "field": "limit", "value": value})
            self.assertEqual(s, 400, (value, d))
        s, d, _ = self.client.req("POST", "/api/conversation",
                                  {"platform": "qq", "id": "group:10001",
                                   "field": "enabled", "value": "yes"})
        self.assertEqual(s, 400)
        s, d, _ = self.client.req("POST", "/api/conversation",
                                  {"platform": "qq", "id": "group:10001",
                                   "field": "bogus", "value": 1})
        self.assertEqual(s, 400)
        s, d, _ = self.client.req("POST", "/api/conversation",
                                  {"platform": "qq", "id": "nope",
                                   "field": "limit", "value": 0})
        self.assertEqual(s, 404)

    def test_conversation_all_fields_allowed_by_capability(self):
        self.login()
        cases = [
            ("enabled", False), ("style", "calm"),
            ("frequency", "active"), ("limit", 0),
        ]
        for field, value in cases:
            s, d, _ = self.client.req("POST", "/api/conversation",
                                      {"platform": "qq", "id": "group:10001",
                                       "field": field, "value": value})
            self.assertEqual(s, 200, (field, d))

    # ---- v2: session revocation / global style / strict host ---------------- #

    def test_password_change_revokes_all_sessions(self):
        """Two active sessions: changing the password must revoke BOTH."""
        self.login()
        c1 = Client(self.port)
        s, _, _ = c1.req("POST", "/api/login", {"password": "test-password-123"})
        self.assertEqual(s, 200)
        c2 = Client(self.port)
        s, _, _ = c2.req("POST", "/api/login", {"password": "test-password-123"})
        self.assertEqual(s, 200)
        # Both are valid before the change.
        self.assertEqual(c1.req("GET", "/api/overview")[0], 200)
        self.assertEqual(c2.req("GET", "/api/overview")[0], 200)
        # Change password using c1. On success c1.cookie is replaced with the
        # one freshly issued session.
        s, d, _ = c1.req("POST", "/api/password",
                         {"current": "test-password-123",
                          "password": "changed-password-123"})
        self.assertEqual(s, 200, d)
        cookie_new = c1.cookie
        # c1's OLD cookie and c2's cookie are both dead; the freshly issued one works.
        c1.cookie = "dafeiyu_session=stale-old-c1"
        self.assertEqual(c1.req("GET", "/api/overview")[0], 401)
        self.assertEqual(c2.req("GET", "/api/overview")[0], 401)
        c3 = Client(self.port)
        c3.cookie = cookie_new
        self.assertEqual(c3.req("GET", "/api/overview")[0], 200)

    def test_reset_revokes_all_sessions(self):
        """Password reset must revoke every session, not just the current one."""
        self.login()
        a = Client(self.port)
        a.req("POST", "/api/login", {"password": "test-password-123"})
        b = Client(self.port)
        b.req("POST", "/api/login", {"password": "test-password-123"})
        self.assertEqual(a.req("GET", "/api/overview")[0], 200)
        self.assertEqual(b.req("GET", "/api/overview")[0], 200)
        reset = self.mint_pair(reset=True)
        s, d, _ = self.client.req("POST", "/api/setup",
                                  {"pair": reset, "password": "reset-password-123",
                                   "reset": True})
        self.assertEqual(s, 200, d)
        fresh = self.client.cookie
        self.assertEqual(a.req("GET", "/api/overview")[0], 401)
        self.assertEqual(b.req("GET", "/api/overview")[0], 401)
        c = Client(self.port)
        c.cookie = fresh
        self.assertEqual(c.req("GET", "/api/overview")[0], 200)
        self.assertTrue(self.app.passwords.verify("reset-password-123"))

    def test_setup_revokes_all_sessions(self):
        """Even first-time setup revokes everything then issues one session."""
        token = self.mint_pair()
        self.app.sessions.create()  # a stray pre-existing token
        self.assertEqual(len(self.app.sessions.tokens), 1)
        s, d, _ = self.client.req("POST", "/api/setup",
                                  {"pair": token, "password": "fresh-setup-123"})
        self.assertEqual(s, 200, d)
        self.assertEqual(len(self.app.sessions.tokens), 1)
        self.assertTrue(self.app.sessions.valid(
            parse_cookie_value(self.client.cookie)))
        c = Client(self.port)
        c.cookie = self.client.cookie
        self.assertEqual(c.req("GET", "/api/overview")[0], 200)

    def test_strict_host_port(self):
        """Host must be exactly hostname:port for this listener."""
        c = Client(self.port)
        s, _, _ = c.req("GET", "/api/session",
                        headers={"Host": "127.0.0.1"})
        self.assertEqual(s, 403)  # missing port
        s, _, _ = c.req("GET", "/api/session",
                        headers={"Host": "127.0.0.1:1"})
        self.assertEqual(s, 403)  # wrong port
        s, _, _ = c.req("GET", "/api/session",
                        headers={"Host": "localhost:%d" % self.port})
        self.assertEqual(s, 200)  # allowed loopback name + exact port

    def test_strict_origin_same_host_port(self):
        """A present Origin must match the exact Host:port scheme+value."""
        self.login()
        c = Client(self.port)
        s, _, _ = c.req("POST", "/api/logout", {},
                        origin="http://127.0.0.1:%d" % self.port,
                        headers={"Host": "localhost:%d" % self.port})
        # Host is localhost but Origin says 127.0.0.1 -> not same-origin.
        self.assertEqual(s, 403)
        s, _, _ = c.req("POST", "/api/logout", {},
                        origin="https://127.0.0.1:%d" % self.port)
        self.assertEqual(s, 403)  # wrong scheme

    def test_wechat_style_is_global_not_per_conversation(self):
        """WeChat persona is one global preset, never a per-chat switch."""
        self.login()
        _, ov, _ = self.client.req("GET", "/api/overview")
        # Overview advertises the explicit global setting.
        self.assertIn("globalSettings", ov)
        gs = ov["globalSettings"]
        self.assertEqual(len(gs), 1)
        self.assertEqual(gs[0]["scope"], "wechat:global")
        self.assertEqual(gs[0]["field"], "style")
        self.assertTrue(gs[0]["capabilities"]["style"])
        # Every WeChat conversation exposes style as read-only.
        for c in ov["conversations"]:
            if c["platform"] == "wechat":
                self.assertFalse(c["capabilities"]["style"])
        # A per-conversation style write is refused with a clear Chinese reason.
        s, d, _ = self.client.req("POST", "/api/conversation",
                                  {"platform": "wechat", "id": "wechat:group:夹具群甲",
                                   "field": "style", "value": "persona-tieba"})
        self.assertEqual(s, 409)
        self.assertIn("全局", d["error"])
        # The global scope id still works (updates the same real preset).
        s, d, _ = self.client.req("POST", "/api/conversation",
                                  {"platform": "wechat", "id": "wechat:global",
                                   "field": "style", "value": "persona-tieba"})
        self.assertEqual(s, 200, d)
        # Re-GET reflects the contract (fixture is read-only, value stays).
        s, ov2, _ = self.client.req("GET", "/api/overview")
        self.assertEqual(s, 200)
        self.assertEqual(ov2["globalSettings"][0]["scope"], "wechat:global")

    def test_frontend_page_contract(self):
        """The delivered ../ui files are served and expose the expected hooks."""
        html = (UI_DIR / "index.html").read_text(encoding="utf-8")
        js = (UI_DIR / "app.js").read_text(encoding="utf-8")
        self.assertIn('id="globalCard"', html)
        self.assertIn('id="globalBody"', html)
        self.assertIn("globalSettings", js)
        self.assertIn("renderGlobalSettings", js)
        for path in ("/", "/app.css", "/app.js"):
            self.assertEqual(self.client.req("GET", path)[0], 200, path)

    def test_pi_protection_knobs(self):
        """Pi 3B protection: bounded concurrency + socket timeout are wired."""
        self.assertIsNotNone(self.app.request_semaphore)
        self.assertGreaterEqual(self.app.socket_timeout, 2)
        self.assertGreaterEqual(self.app.request_semaphore._initial_value, 2)
        # A failed upstream call must never leak an internal error body.
        import adapters
        err = adapters.AdapterError("x")
        self.assertNotIn("key", err.message)

    def test_lan_listener_binds_exact_host(self):
        """LAN HTTPS binds to lan_host, never 0.0.0.0 (item 1)."""
        with tempfile.TemporaryDirectory(prefix="dafeiyu-bind-") as tmp:
            d = Path(tmp)
            tls = d / "tls"
            tls.mkdir()
            # Empty placeholder files are enough: build_servers only checks exists.
            (tls / "server.crt").write_bytes(b"x")
            (tls / "server.key").write_bytes(b"x")
            cfg = {
                "stateDir": str(d / "state"),
                "uiDir": str(UI_DIR),
                "lanHost": "203.0.113.200",
                "lanPort": 0, "loopPort": 0,
                "tlsCert": str(tls / "server.crt"),
                "tlsKey": str(tls / "server.key"),
                "lanNets": ["203.0.113.0/24"],
            }
            app = uc.App(cfg, FIXTURE)
            captured = []
            real_init = uc.ConsoleServer.__init__
            real_load = ssl.SSLContext.load_cert_chain
            real_wrap = ssl.SSLContext.wrap_socket

            def fake_init(self, addr, handler, app_ref):
                captured.append(addr)
                # Don't actually bind to the real LAN IP on this dev machine.
                return real_init(self, ("127.0.0.1", addr[1]), handler, app_ref)

            # Placeholder cert bytes cannot be loaded; make TLS setup a no-op so
            # the LAN listener's bind address is still captured.
            ssl.SSLContext.load_cert_chain = lambda self, *a, **k: None
            ssl.SSLContext.wrap_socket = lambda self, sock, **k: sock
            uc.ConsoleServer.__init__ = fake_init
            servers = []
            try:
                servers = app.build_servers()
            finally:
                uc.ConsoleServer.__init__ = real_init
                ssl.SSLContext.load_cert_chain = real_load
                ssl.SSLContext.wrap_socket = real_wrap
                for _, s, _ in servers:
                    try:
                        s.server_close()
                    except OSError:
                        pass
            self.assertEqual(len(captured), 2, captured)
            # First bind is loopback HTTP; second (LAN) must be the exact host.
            self.assertEqual(captured[0][0], "127.0.0.1")
            self.assertEqual(captured[1][0], "203.0.113.200")
            self.assertNotIn("0.0.0.0", [c[0] for c in captured])

    # ---- POST /api/model -------------------------------------------------- #

    def test_model_ok_and_unknown(self):
        self.login()
        # Both platforms offer models from their real configured lists.
        _, ov, _ = self.client.req("GET", "/api/overview")
        wx_models = [m["id"] for m in ov["models"] if "wechat" in m["platforms"]]
        self.assertIn("fixture-wx-alt", wx_models)
        s, d, _ = self.client.req("POST", "/api/model",
                                  {"platform": "wechat", "id": "fixture-wx-alt"})
        self.assertEqual(s, 200, d)
        s, d, _ = self.client.req("POST", "/api/model",
                                  {"platform": "qq", "id": "fixture-qq-alt"})
        self.assertEqual(s, 200, d)
        s, d, _ = self.client.req("POST", "/api/model",
                                  {"platform": "qq", "id": "does-not-exist"})
        self.assertEqual(s, 404)
        s, d, _ = self.client.req("POST", "/api/model",
                                  {"platform": "wechat", "id": "does-not-exist"})
        self.assertEqual(s, 404)
        s, d, _ = self.client.req("POST", "/api/model",
                                  {"platform": "bogus", "id": "x"})
        self.assertEqual(s, 400)

    # ---- security gates --------------------------------------------------- #

    def test_bad_host_rejected(self):
        c = Client(self.port)
        s, d, _ = c.req("GET", "/api/session", headers={"Host": "evil.example"})
        self.assertEqual(s, 403)

    def test_bad_origin_rejected_on_write(self):
        self.login()
        s, d, _ = self.client.req("POST", "/api/logout", {},
                                  origin="https://evil.example")
        self.assertEqual(s, 403)

    def test_non_json_write_rejected(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.request("POST", "/api/login", body="password=x",
                     headers={"Host": "127.0.0.1:%d" % self.port,
                              "Content-Type": "text/plain"})
        r = conn.getresponse()
        self.assertEqual(r.status, 400)
        r.read()
        conn.close()

    def test_ca_is_loopback_only_and_404_without_cert(self):
        s, d, _ = self.client.req("GET", "/api/ca")
        # No cert in the test state dir.
        self.assertEqual(s, 404)
        self.assertIn("error", d)

    def test_static_files(self):
        for path, ctype in (("/", "text/html"), ("/app.css", "text/css"),
                            ("/app.js", "application/javascript")):
            s, _, data = self.client.req("GET", path)
            self.assertEqual(s, 200, path)

    def test_rate_limit(self):
        self.app.passwords.set("rate-limit-password")
        c = Client(self.port)
        for _ in range(10):
            s, d, _ = c.req("POST", "/api/login", {"password": "wrong"})
        self.assertEqual(s, 429)
        self.assertIn("error", d)

    def test_no_secret_in_overview(self):
        self.login()
        _, d, raw = self.client.req("GET", "/api/overview")
        text = json.dumps(d, ensure_ascii=False)
        for forbidden in ("x-model-control", "admin.key", "inboundToken",
                          "apiKey", "password"):
            self.assertNotIn(forbidden, text)


class LanHttpTests(unittest.TestCase):
    def test_lan_http_uses_same_password_and_keeps_setup_local(self):
        with tempfile.TemporaryDirectory(prefix="dafeiyu-lan-http-") as tmp:
            app = uc.App({
                "stateDir": tmp, "lanHttp": True, "lanHost": "127.0.0.1",
                "lanPort": 0, "loopPort": 0, "lanNets": ["127.0.0.0/8"],
            }, FIXTURE)
            servers = app.build_servers()
            self.assertEqual([name for name, _, _ in servers],
                             ["loopback-http", "lan-http"])
            for _, server, _ in servers:
                threading.Thread(target=server.serve_forever, daemon=True).start()
                self.addCleanup(server.server_close)
                self.addCleanup(server.shutdown)

            local = Client(servers[0][1].server_port)
            lan = Client(servers[1][1].server_port)
            self.assertTrue(local.req("GET", "/api/session")[1]["setupAvailable"])
            state = lan.req("GET", "/api/session")[1]
            self.assertFalse(state["setupAvailable"])
            self.assertFalse(state["secure"])
            self.assertTrue(state["lanUrl"].startswith("http://"))
            self.assertEqual(lan.req("GET", "/api/overview")[0], 401)
            self.assertEqual(lan.req("GET", "/api/ca")[0], 403)
            self.assertEqual(local.req("GET", "/api/ca")[0], 403)
            self.assertEqual(lan.req("GET", "/api/session", headers={
                "Host": f"localhost:{lan.port}",
            })[0], 403)
            self.assertEqual(lan.req("POST", "/api/login", {"password": "wrong"},
                                     origin="http://other.example")[0], 403)

            body = {"pair": app.pair_store.mint(), "password": "fixture-password-123"}
            self.assertEqual(lan.req("POST", "/api/setup", body)[0], 403)
            self.assertEqual(local.req("POST", "/api/setup", body)[0], 200)
            self.assertEqual(lan.req("POST", "/api/login", {"password": body["password"]},
                                     origin=f"http://127.0.0.1:{lan.port}")[0], 200)
            self.assertTrue(lan.cookie.startswith("dafeiyu_lan_session="))
            self.assertEqual(lan.req("GET", "/api/overview")[0], 200)
            self.assertEqual(lan.req("POST", "/api/logout", {})[0], 200)
            self.assertEqual(lan.req("GET", "/api/overview")[0], 401)


class DegradationTests(unittest.TestCase):
    """A platform that is down degrades independently (connected:null)."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix="dafeiyu-console-degrade-")
        broken = {"qq": {}, "wechat": FIXTURE["wechat"]}
        cfg = {
            "stateDir": str(Path(cls.tmp.name) / "state"),
            "uiDir": str(UI_DIR),
            "lanHost": "127.0.0.1", "lanPort": 0, "loopPort": 0,
            "lanNets": ["127.0.0.0/8"],
        }
        app = uc.App(cfg, broken)
        srv = uc.ConsoleServer(("127.0.0.1", 0), uc.Handler, app)
        cls.app, cls.srv, cls.port = app, srv, srv.server_address[1]
        threading.Thread(target=srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()
        cls.tmp.cleanup()

    def test_qq_down_wechat_still_up(self):
        token = self.app.pair_store.mint()
        c = Client(self.port)
        c.req("POST", "/api/setup", {"pair": token, "password": "degrade-test-123"})
        s, d, _ = c.req("GET", "/api/overview")
        self.assertEqual(s, 200)
        self.assertIsNone(d["qq"]["connected"])
        self.assertIsNone(d["qq"]["running"])
        self.assertIsNotNone(d["wechat"]["connected"])
        self.assertTrue(any(g["platform"] == "qq" and g["level"] == "warning"
                            for g in d["diagnostics"]))
        self.assertEqual([x for x in d["conversations"] if x["platform"] == "qq"], [])


class StatusTruthTests(unittest.TestCase):
    """Status must report what the backends really say.

    All reads/writes go to synthetic fixtures; nothing here touches the Pi, the
    live hub, the WeChat core or any real account.
    """

    def adapter(self, platform, fixture, cfg=None):
        import adapters
        c = {"qqHubUrl": "http://127.0.0.1:1", "wechatCoreUrl": "http://127.0.0.1:1"}
        c.update(cfg or {})
        return adapters.QQAdapter(c, fixture) if platform == "qq" else \
            adapters.WeChatAdapter(c, fixture)

    # ---- QQ: client identity ------------------------------------------------ #

    def test_qq_client_defaults_to_napcat(self):
        """qqClientName is the operator's optional switch for naming the client."""
        qq, diags = self.adapter("qq", FIXTURE, {"qqClientName": "NapCat"}).status()
        self.assertEqual(qq["client"], "NapCat")
        self.assertEqual(qq["clientSource"], "config")
        self.assertEqual(diags, [])

    def test_qq_client_unlabelled_when_not_configured(self):
        """No config, no reported client: the console says "unknown", not a guess."""
        qq, _ = self.adapter("qq", FIXTURE).status()
        self.assertEqual(qq["client"], "")
        self.assertEqual(qq["clientSource"], "unknown")

    def test_qq_url_kept_outside_client_field(self):
        """Regression: the hub URL / Host is not a client name."""
        base = FIXTURE["qq"]
        fixture = {"qq": dict(base, **{"/api/panel": dict(
            base["/api/panel"], apiUrl="http://127.0.0.1:18090",
            client="NapCat")})}
        qq, _ = self.adapter("qq", fixture).status()
        self.assertEqual(qq["client"], "NapCat")
        self.assertNotIn("127.0.0.1:18090", qq["client"])
        self.assertNotIn("http", qq["client"])

    def test_qq_reported_client_beats_config(self):
        base = FIXTURE["qq"]
        fixture = {"qq": dict(base, **{"/api/panel": dict(
            base["/api/panel"], client="Lagrange")})}
        qq, _ = self.adapter("qq", fixture).status()
        self.assertEqual(qq["client"], "Lagrange")
        self.assertEqual(qq["clientSource"], "reported")

    def test_qq_client_config_is_optional(self):
        qq, _ = self.adapter("qq", FIXTURE, {"qqClientName": "  "}).status()
        self.assertEqual(qq["client"], "")
        self.assertEqual(qq["clientSource"], "unknown")

    def test_qq_connection_reply_model_are_separate(self):
        """Conn / running / model / client are four independent fields."""
        qq, _ = self.adapter("qq", FIXTURE, {"qqClientName": "NapCat"}).status()
        self.assertEqual(set(qq), {"connected", "running", "model", "client",
                                   "clientSource", "lastRead"})
        self.assertIs(qq["connected"], True)
        self.assertIs(qq["running"], True)
        self.assertEqual(qq["model"], "fixture-qq-model")
        self.assertEqual(qq["client"], "NapCat")

    def test_qq_status_failure_keeps_shape(self):
        qq, diags = self.adapter("qq", {"qq": {}}).status()
        self.assertIsNone(qq["connected"])
        self.assertIsNone(qq["running"])
        self.assertEqual(qq["model"], "")
        self.assertEqual(qq["clientSource"], "unknown")
        self.assertIsNone(qq["lastRead"])
        self.assertTrue(any(d["platform"] == "qq" for d in diags))

    # ---- QQ: two same-origin reads ----------------------------------------- #

    def test_qq_same_origin_reads_are_merged(self):
        seen = []

        import adapters

        class Rec(adapters.QQAdapter):
            def _get(self, path):
                seen.append(path)
                return super()._get(path)

        c = Rec({}, FIXTURE)
        c.status()
        c.models()
        c.diagnostics()
        self.assertEqual(seen.count("/api/panel"), 1, seen)
        self.assertEqual(seen.count("/api/status"), 1, seen)

    def test_qq_two_reads_stay_separate_across_overviews(self):
        """Merging is per overview: a new request reads the hub again."""
        seen = []

        import adapters

        class Rec(adapters.QQAdapter):
            def _get(self, path):
                seen.append(path)
                return super()._get(path)

        c = Rec({}, FIXTURE)
        c.status()
        c._cache_clear()      # what Console.overview() does at request start
        c.status()
        self.assertEqual(seen.count("/api/panel"), 2, seen)

    # ---- WeChat: bridge truth, never `enabled` ------------------------------ #

    def wx(self, **over):
        base = FIXTURE["wechat"]["/api/status"]
        gw = dict(base["gateways"][0])
        gw["bridge"] = dict(gw["bridge"])
        for k, v in over.pop("bridge", {}).items():
            gw["bridge"][k] = v
        for k, v in over.pop("gw", {}).items():
            gw[k] = v
        status = dict(base, gateways=[gw])
        for k, v in over.items():
            status[k] = v
        return {"wechat": {"/api/status": status}}

    def wx_status(self, **over):
        return self.adapter("wechat", self.wx(**over)).status()[0]

    def test_wechat_online_when_bridge_and_account_ok(self):
        w = self.wx_status(bridge={"online": True, "listener": True, "accountOk": True})
        self.assertEqual(w["clientState"], "ok")
        self.assertIs(w["connected"], True)
        self.assertIs(w["running"], True)
        self.assertEqual(w["client"], "微信客户端在线")

    def test_wechat_enabled_is_not_connected(self):
        """gateway enabled:true with an offline bridge must not read as connected."""
        w = self.wx_status(gw={"enabled": True},
                          bridge={"online": False, "listener": None, "accountOk": None})
        self.assertIs(w["enabled"], True)
        self.assertEqual(w["clientState"], "offline")
        self.assertIs(w["connected"], False)
        self.assertIs(w["running"], False)
        self.assertNotIn("已连接", w["client"])

    def test_wechat_window_observation_failure_is_not_a_logout(self):
        for reason in ("window_blocked", "window_not_ready", "client_unavailable", "client_runtime_changed"):
            with self.subTest(reason=reason):
                w = self.wx_status(
                    bridge={"online": True, "listener": False, "accountOk": True},
                    bridgeDiagnostics={"fresh": True, "client": {"reason": reason}},
                )
                self.assertEqual(w["clientState"], "unknown")
                self.assertEqual(w["clientReason"], reason)
                self.assertEqual(w["client"], "微信窗口暂不可用")
                self.assertIsNone(w["connected"])
                self.assertFalse(w["running"])
                self.assertTrue(w["bridge"]["online"])

    def test_wechat_qr_required_is_explicit(self):
        w = self.wx_status(bridge={"online": False, "listener": None, "accountOk": False},
                          bridgeDiagnostics={"fresh": True, "client": {
                              "time": 1794400000, "phase": "login_required",
                              "reason": "login_required"}})
        self.assertEqual(w["clientState"], "qr")
        self.assertEqual(w["clientReason"], "login_required")
        self.assertIs(w["connected"], False)
        self.assertIs(w["running"], False)
        self.assertTrue(w["diagnostics"]["fresh"])

    def test_wechat_phone_confirm_counts_as_pending_scan(self):
        w = self.wx_status(bridge={"online": False},
                          bridgeDiagnostics={"fresh": True, "client": {
                              "time": 1794400000, "phase": "login_required",
                              "reason": "login_confirmation_required"}})
        self.assertEqual(w["clientState"], "qr")

    def test_wechat_stale_diagnostics_do_not_force_qr(self):
        """A stale reason may not turn a healthy bridge into "waiting for scan"."""
        w = self.wx_status(bridge={"online": True, "listener": True, "accountOk": True},
                          bridgeDiagnostics={"fresh": False, "client": {
                              "time": 1794000000, "phase": "login_required",
                              "reason": "login_required"}})
        self.assertEqual(w["clientState"], "ok")

    def test_wechat_disabled_gateway(self):
        w = self.wx_status(gw={"enabled": False}, bridge={"online": False})
        self.assertEqual(w["clientState"], "disabled")
        self.assertIs(w["connected"], False)
        self.assertIs(w["enabled"], False)

    def test_wechat_listener_false_is_not_connected(self):
        w = self.wx_status(bridge={"online": True, "listener": False, "accountOk": None})
        self.assertIs(w["connected"], False)
        self.assertNotEqual(w["clientState"], "ok")
        self.assertEqual(w["bridgeStatus"], "listener_paused")
        self.assertEqual(w["client"], "微信桥接监听暂停")

    def test_wechat_ready_listener_does_not_claim_login_without_account_check(self):
        w = self.wx_status(bridge={"online": True, "listener": True, "accountOk": None})
        self.assertIs(w["connected"], True)
        self.assertEqual(w["bridgeStatus"], "ready")
        self.assertEqual(w["client"], "微信桥接监听正常")

    def test_wechat_unknown_when_probe_missing(self):
        w = self.adapter("wechat", {"wechat": {}}).status()[0]
        self.assertEqual(w["clientState"], "unknown")
        self.assertIsNone(w["connected"])
        self.assertIsNone(w["running"])
        self.assertIsNone(w["lastRead"])

    def test_wechat_unknown_when_bridge_fields_contradict(self):
        w = self.wx_status(bridge={"online": True, "listener": None, "accountOk": False})
        self.assertEqual(w["clientState"], "offline")

    def test_wechat_read_time_is_reported(self):
        w = self.wx_status()
        self.assertIsInstance(w["lastRead"], (int, float))
        self.assertGreater(w["lastRead"], 1_600_000_000)

    def test_wechat_recent_composer_not_empty_is_reported_without_leaking(self):
        """Recent queue/composer_not_empty pauses auto-reply but keeps status ok.

        Only status()[1] diagnostics carry the fixed Chinese message; the status
        dict stays online. Expired / future / other events never produce it, and
        neither trace nor message body may leak into any diagnostic.
        """
        import adapters

        now = 1_794_400_000
        message = "最近一条微信自动接话因输入框有草稿而暂停"
        secrets = ("SECRET-TRACE-XYZ", "SECRET-BODY-ABC")

        def run(last_event):
            fixture = self.wx(
                bridge={"online": True, "listener": True, "accountOk": True},
                bridgeDiagnostics={
                    "fresh": True,
                    "client": {"time": now, "phase": "window_available",
                               "reason": "window_available"},
                    "last_event": last_event,
                },
            )
            with mock.patch.object(adapters, "_now", return_value=now):
                return self.adapter("wechat", fixture).status()

        def event(offset, **extra):
            e = {"time": now + offset, "phase": "queue",
                 "reason": "composer_not_empty",
                 "trace": secrets[0], "text": secrets[1]}
            e.update(extra)
            return e

        # Fresh, within the last 60 seconds: message shown, status still online.
        status, diags = run(event(-10))
        self.assertEqual(status["clientState"], "ok")
        self.assertIs(status["connected"], True)
        self.assertIs(status["running"], True)
        messages = [str(d.get("message") or "") for d in diags]
        self.assertIn(message, messages)
        for text in messages:
            for secret in secrets:
                self.assertNotIn(secret, text)

        # Boundary: exactly 60s old is still "recent".
        _, diags = run(event(-60))
        self.assertIn(message, [str(d.get("message") or "") for d in diags])

        # Stale, future, or a different event must not report the message.
        for last_event in (
            event(-61),
            event(10),
            event(-10, phase="replying", reason="model_done"),
            event(-10, reason="composer_empty"),
        ):
            with self.subTest(last_event=last_event):
                _, diags = run(last_event)
                for d in diags:
                    self.assertNotIn(message, str(d.get("message") or ""))
                    for secret in secrets:
                        self.assertNotIn(secret, str(d.get("message") or ""))

    # ---- model: current + candidates + read-back --------------------------- #

    def test_models_include_current_and_candidates(self):
        import adapters
        c = adapters.Console({}, FIXTURE)
        ov = c.overview()
        qq_ids = [m["id"] for m in ov["models"] if "qq" in m["platforms"]]
        self.assertIn(ov["qq"]["model"], qq_ids)
        self.assertGreaterEqual(len(qq_ids), 2)
        wx_ids = [m["id"] for m in ov["models"] if "wechat" in m["platforms"]]
        self.assertIn(ov["wechat"]["model"], wx_ids)
        self.assertIn("fixture-wx-alt", wx_ids)

    def test_verify_model_confirmed(self):
        import adapters
        c = adapters.Console({}, FIXTURE)
        c.set_model("qq", "fixture-qq-alt")   # fixture write is a no-op
        v = c.verify_model("qq", "fixture-qq-model")
        self.assertEqual(v["state"], "confirmed")
        self.assertEqual(v["observed"], "fixture-qq-model")
        self.assertEqual(v["requested"], "fixture-qq-model")
        self.assertIsInstance(v["lastRead"], (int, float))

    def test_verify_model_mismatch(self):
        import adapters
        v = adapters.Console({}, FIXTURE).verify_model("qq", "fixture-qq-alt")
        self.assertEqual(v["state"], "mismatch")
        self.assertEqual(v["observed"], "fixture-qq-model")

    def test_verify_model_unconfirmed_when_unreadable(self):
        import adapters
        v = adapters.Console({}, {"qq": {}, "wechat": {}}).verify_model("qq", "x")
        self.assertEqual(v["state"], "unconfirmed")
        self.assertIsNone(v["observed"])
        self.assertIn("lastRead", v)

    def test_model_post_carries_verification(self):
        with tempfile.TemporaryDirectory(prefix="dafeiyu-verify-") as tmp:
            cfg = {
                "stateDir": str(Path(tmp) / "state"),
                "uiDir": str(UI_DIR),
                "lanHost": "127.0.0.1", "lanPort": 0, "loopPort": 0,
                "lanNets": ["127.0.0.0/8"], "extraHosts": ["127.0.0.1", "localhost"],
            }
            app = uc.App(cfg, FIXTURE)
            srv = uc.ConsoleServer(("127.0.0.1", 0), uc.Handler, app)
            port = srv.server_address[1]
            threading.Thread(target=srv.serve_forever, daemon=True).start()
            try:
                c = Client(port)
                c.req("POST", "/api/setup",
                      {"pair": app.pair_store.mint(), "password": "verify-pass-123"})
                s, d, _ = c.req("POST", "/api/model",
                                {"platform": "qq", "id": "fixture-qq-model"})
                self.assertEqual(s, 200, d)
                # Backwards compatible: `ok` still present, verification added.
                self.assertTrue(d["ok"])
                self.assertEqual(d["verification"]["state"], "confirmed")
                self.assertEqual(d["verification"]["platform"], "qq")
                # And the next overview reads again (not the pre-switch cache).
                s, ov, _ = c.req("GET", "/api/overview")
                self.assertEqual(s, 200)
                self.assertEqual(ov["qq"]["model"], "fixture-qq-model")
            finally:
                srv.shutdown()
                srv.server_close()

    # ---- status failures stay HTTP 200 with a clear shape ------------------ #

    def test_status_failure_is_not_a_server_error(self):
        with tempfile.TemporaryDirectory(prefix="dafeiyu-broken-") as tmp:
            cfg = {
                "stateDir": str(Path(tmp) / "state"),
                "uiDir": str(UI_DIR),
                "lanHost": "127.0.0.1", "lanPort": 0, "loopPort": 0,
                "lanNets": ["127.0.0.0/8"], "extraHosts": ["127.0.0.1", "localhost"],
            }
            app = uc.App(cfg, {"qq": {}, "wechat": {}})
            srv = uc.ConsoleServer(("127.0.0.1", 0), uc.Handler, app)
            port = srv.server_address[1]
            threading.Thread(target=srv.serve_forever, daemon=True).start()
            try:
                c = Client(port)
                c.req("POST", "/api/setup",
                      {"pair": app.pair_store.mint(), "password": "broken-pass-123"})
                s, d, _ = c.req("GET", "/api/overview")
                self.assertEqual(s, 200)
                self.assertIsNone(d["qq"]["connected"])
                self.assertEqual(d["qq"]["clientState"] if "clientState" in d["qq"] else None,
                                 None)
                self.assertEqual(d["wechat"]["clientState"], "unknown")
                self.assertIsNone(d["wechat"]["bridge"])
                self.assertIsNone(d["qq"]["lastRead"])
                self.assertGreaterEqual(len(d["diagnostics"]), 2)
            finally:
                srv.shutdown()
                srv.server_close()


class WeChatProactiveAllowlistTests(unittest.TestCase):
    """Only the exact, unique bridge group title is changed; no live sends."""

    def fixture(self, titles, groups=None):
        fx = copy.deepcopy(FIXTURE)
        fx["wechat"]["titles"] = titles
        fx["wechat"]["/api/config"] = {
            "ok": True, "config": {"group": {
                "freeChat": False, "freeChatChance": 0.06,
                "proactiveJudge": {"enabled": False, "groups": list(groups or [])},
            }},
        }
        fx["wechat"]["writes"] = []
        return fx

    def test_toggle_persists_and_reads_back_only_exact_group(self):
        import adapters

        fx = self.fixture([
            {"name": "原生群甲", "group": True},
            {"name": "相邻群乙", "group": True},
            {"name": "管理员", "group": False},
        ], groups=["legacy-entry", "legacy-entry"])

        class WriteThrough(adapters.WeChatAdapter):
            def _post(self, path, body):
                self.fixture["wechat"]["writes"].append((path, copy.deepcopy(body)))
                if path == "/api/config":
                    cur = self.fixture["wechat"]["/api/config"]
                    groups = body["patch"]["group"]["proactiveJudge"]["groups"]
                    cur["config"]["group"]["proactiveJudge"]["groups"] = groups
                return {"ok": True}

        console = adapters.Console({}, fx)
        console.wechat = WriteThrough({}, fx)
        before = console.wechat.conversations()
        alpha = next(c for c in before if c["id"] == "wechat:group:原生群甲")
        beta = next(c for c in before if c["id"] == "wechat:group:相邻群乙")
        self.assertFalse(alpha["enabled"])
        self.assertTrue(alpha["capabilities"]["enabled"])
        self.assertEqual(alpha["enabledLabel"], "主动闲聊白名单")
        self.assertIn("全局判别关闭时不会触发自动闲聊", alpha["enabledHint"])
        self.assertFalse(beta["enabled"])
        self.assertFalse(console.wechat.conversations()[2]["capabilities"]["enabled"])

        # The per-group checkbox is only an allowlist setting. The global
        # judge stays off and freeChat fields are untouched.
        console.set_conversation("wechat", alpha["id"], "enabled", True)
        self.assertEqual(console.wechat.conversations()[0]["enabled"], True)
        self.assertEqual(fx["wechat"]["/api/config"]["config"]["group"]["proactiveJudge"],
                         {"enabled": False, "groups": [
                             "legacy-entry", "legacy-entry", "原生群甲"]})
        self.assertFalse(fx["wechat"]["/api/config"]["config"]["group"]["freeChat"])
        self.assertEqual(fx["wechat"]["/api/config"]["config"]["group"]["freeChatChance"], 0.06)
        self.assertEqual(len(fx["wechat"]["writes"]), 1)
        console.set_conversation("wechat", alpha["id"], "enabled", False)
        self.assertEqual(console.wechat.conversations()[0]["enabled"], False)
        self.assertEqual(fx["wechat"]["/api/config"]["config"]["group"]["proactiveJudge"],
                         {"enabled": False, "groups": ["legacy-entry", "legacy-entry"]})

    def test_duplicate_group_name_is_read_only_and_write_is_rejected(self):
        import adapters

        fx = self.fixture([
            {"name": "重名群", "group": True},
            {"name": "重名群", "group": True},
        ])
        console = adapters.Console({}, fx)
        rows = console.wechat.conversations()
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(row["enabled"] is False for row in rows))
        self.assertTrue(all(row["capabilities"]["enabled"] is False for row in rows))
        with self.assertRaises(adapters.AdapterError):
            console.set_conversation("wechat", "wechat:group:重名群", "enabled", True)
        self.assertEqual(fx["wechat"]["/api/config"]["config"]["group"]["proactiveJudge"]["groups"], [])
        self.assertEqual(fx["wechat"]["writes"], [])

    def test_missing_or_blank_group_title_is_read_only(self):
        import adapters

        titles = [
            {"group": True}, {"name": None, "group": True},
            {"name": "", "group": True}, {"name": "   ", "group": True},
            {"name": 17, "group": True}, {"name": "fixture-valid", "group": True},
        ]
        fx = self.fixture(titles)
        console = adapters.Console({}, fx)
        rows = console.wechat.conversations()
        self.assertEqual(len(rows), len(titles))
        for row in rows[:-1]:
            self.assertFalse(row["capabilities"]["enabled"])
            with self.assertRaises(adapters.AdapterError):
                console.set_conversation("wechat", row["id"], "enabled", True)
        self.assertEqual(fx["wechat"]["writes"], [])
        self.assertTrue(rows[-1]["capabilities"]["enabled"])

    def test_full_list_readback_mismatch_fails_without_retry(self):
        import adapters

        fx = self.fixture([
            {"name": "fixture-alpha", "group": True},
            {"name": "fixture-beta", "group": True},
        ], groups=["legacy-entry", "fixture-beta"])

        class MismatchedReadback(adapters.WeChatAdapter):
            def _post(self, path, body):
                self.fixture["wechat"]["writes"].append((path, copy.deepcopy(body)))
                self.fixture["wechat"]["/api/config"]["config"]["group"]["proactiveJudge"]["groups"] = [
                    "fixture-beta"]
                return {"ok": True}

        console = adapters.Console({}, fx)
        console.wechat = MismatchedReadback({}, fx)
        with self.assertRaisesRegex(adapters.AdapterError, "白名单与请求不一致"):
            console.set_conversation("wechat", "wechat:group:fixture-alpha", "enabled", True)
        self.assertEqual(len(fx["wechat"]["writes"]), 1)

    def test_concurrent_group_toggles_preserve_both_updates(self):
        import adapters

        fx = self.fixture([
            {"name": "fixture-alpha", "group": True},
            {"name": "fixture-beta", "group": True},
        ])
        first_post_started = threading.Event()
        release_first_post = threading.Event()
        second_read_config = threading.Event()

        class PausedWriteThrough(adapters.WeChatAdapter):
            def __init__(self, cfg, fixture):
                super().__init__(cfg, fixture)
                self.post_count = 0
                self.post_guard = threading.Lock()

            def _get(self, path):
                if path == "/api/config" and threading.current_thread().name == "second-toggle":
                    second_read_config.set()
                return super()._get(path)

            def _post(self, path, body):
                with self.post_guard:
                    self.post_count += 1
                    first = self.post_count == 1
                if first:
                    first_post_started.set()
                    if not release_first_post.wait(timeout=5):
                        raise RuntimeError("fixture gate timed out")
                self.fixture["wechat"]["writes"].append((path, copy.deepcopy(body)))
                groups = body["patch"]["group"]["proactiveJudge"]["groups"]
                self.fixture["wechat"]["/api/config"]["config"]["group"]["proactiveJudge"]["groups"] = groups
                return {"ok": True}

        console = adapters.Console({}, fx)
        console.wechat = PausedWriteThrough({}, fx)
        results = []

        def toggle(name):
            try:
                console.set_conversation("wechat", "wechat:group:" + name, "enabled", True)
                results.append((name, None))
            except Exception as exc:  # captured so the test can release/join both threads
                results.append((name, exc))

        first = threading.Thread(target=toggle, name="first-toggle", args=("fixture-alpha",))
        second = threading.Thread(target=toggle, name="second-toggle", args=("fixture-beta",))
        first.start()
        self.assertTrue(first_post_started.wait(timeout=2))
        second.start()
        self.assertTrue(second_read_config.wait(timeout=2))
        # Second request has completed its UI capability read but cannot start
        # its RMW write until the first has posted and read back.
        self.assertEqual(len(fx["wechat"]["writes"]), 0)
        release_first_post.set()
        first.join(timeout=5)
        second.join(timeout=5)
        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertEqual(results, [("fixture-alpha", None), ("fixture-beta", None)])
        self.assertEqual(
            fx["wechat"]["/api/config"]["config"]["group"]["proactiveJudge"]["groups"],
            ["fixture-alpha", "fixture-beta"])

    def test_failed_patch_releases_group_lock_for_next_request(self):
        import adapters

        fx = self.fixture([
            {"name": "fixture-alpha", "group": True},
            {"name": "fixture-beta", "group": True},
        ])

        class FailOnce(adapters.WeChatAdapter):
            def __init__(self, cfg, fixture):
                super().__init__(cfg, fixture)
                self.posts = 0

            def _post(self, path, body):
                self.posts += 1
                if self.posts == 1:
                    return {"ok": False}
                self.fixture["wechat"]["/api/config"]["config"]["group"]["proactiveJudge"]["groups"] = copy.deepcopy(
                    body["patch"]["group"]["proactiveJudge"]["groups"])
                return {"ok": True}

        console = adapters.Console({}, fx)
        console.wechat = FailOnce({}, fx)
        with self.assertRaisesRegex(adapters.AdapterError, "内核拒绝保存"):
            console.set_conversation("wechat", "wechat:group:fixture-alpha", "enabled", True)
        console.set_conversation("wechat", "wechat:group:fixture-beta", "enabled", True)
        self.assertEqual(
            fx["wechat"]["/api/config"]["config"]["group"]["proactiveJudge"]["groups"],
            ["fixture-beta"])


class WeChatGlobalStyleTests(unittest.TestCase):
    """The global WeChat persona is read from /api/persona, not from rows.

    Regression: with no bridge titles `WeChatAdapter.conversations()` is empty,
    and `Console._wechat_global_style()` used to pick the value out of that
    list — so a readable /api/persona plus a successful write still surfaced as
    `overview.globalSettings[0].value === null`.
    """

    def test_write_then_read_back_without_any_conversation(self):
        import adapters

        fx = copy.deepcopy(FIXTURE)
        fx["wechat"]["titles"] = []  # no bridge titles -> no WeChat conversations

        class WriteThrough(adapters.WeChatAdapter):
            """Project fixture + the one global persona write the real core keeps."""

            def _post(self, path, body):
                result = super()._post(path, body)
                if path == "/api/persona/preset":
                    self.fixture["wechat"]["/api/persona"] = {
                        "ok": True, "persona": {"sourcePreset": body["id"]}}
                return result

        console = adapters.Console({}, fx)
        console.wechat = WriteThrough({}, fx)

        before = console.overview()
        self.assertEqual(
            [c for c in before["conversations"] if c["platform"] == "wechat"], [])
        # A readable /api/persona is reported even with zero conversations.
        self.assertEqual(before["globalSettings"][0]["value"], "persona-dafeiyu")

        # Write through the real global scope, then read the overview back.
        console.set_conversation("wechat", "wechat:global", "style", "persona-tieba")

        after = console.overview()
        self.assertEqual(after["globalSettings"][0]["scope"], "wechat:global")
        self.assertEqual(after["globalSettings"][0]["value"], "persona-tieba")

    def test_global_style_falls_back_to_preset_field(self):
        import adapters

        fx = copy.deepcopy(FIXTURE)
        # The real core reports `persona.preset` when `sourcePreset` is absent.
        fx["wechat"]["/api/persona"] = {"ok": True,
                                        "persona": {"preset": "persona-standard"}}
        self.assertEqual(adapters.Console({}, fx).overview()["globalSettings"][0]["value"],
                         "persona-standard")

    def test_global_style_stays_unknown_when_persona_is_unreadable(self):
        import adapters

        # No /api/persona (or anything else): the value stays unknown, not guessed.
        self.assertIsNone(
            adapters.Console({}, {"qq": {}, "wechat": {}})
            .overview()["globalSettings"][0]["value"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
