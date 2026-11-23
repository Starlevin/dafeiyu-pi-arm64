"""Real unified-console HTTP-handler tests for the QZone read-only package.

All QZone data and upstream actions are synthetic. No token, Pi, or account is
loaded. Requests pass through the real Handler, same-origin gate, session store,
static map, QZone route resolver, service, and NapCat adapter contract.
"""
from __future__ import annotations

import http.client
import base64
import hashlib
import json
import os
import socketserver
import sys
import struct
import tempfile
import threading
import unittest
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
SERVER = HERE.parent
INTEGRATIONS = SERVER.parents[1]
QZONE = INTEGRATIONS / "qq-qzone"
FISH_HOUSE = INTEGRATIONS / "fish-house-lan" / "web" / "fish_house"
for path in (SERVER, QZONE):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import unified_console as uc  # noqa: E402
import tool_routes_qzone as routes  # noqa: E402
import qzone_core as core  # noqa: E402
import qzone_napcat as napcat  # noqa: E402
import qzone_service as service_mod  # noqa: E402

FIXED_NOW = int(datetime(2026, 11, 23, 12, 0, tzinfo=timezone(
    timedelta(hours=8))).timestamp())


class FeedTransport(napcat.FakeOneBotTransport):
    def __init__(self):
        super().__init__(allow_actions=set(napcat.READ_ACTIONS))
        self.calls = []

    def post(self, action, payload):
        self.calls.append((action, dict(payload)))
        if action != napcat.ACTION_GET_QZONE_FEEDS:
            raise AssertionError("HTTP QZone path attempted a non-feed action")
        return {"feeds": [{
            "tid": "fixture-feed-1", "time": FIXED_NOW,
            "nickname": "测试好友", "content": "只读动态夹具",
            "images": [], "like_count": 2, "comment_count": 1,
            "cookie": "must-not-escape",
        }], "has_more": False, "next_cursor": None}


class FakeOneBotWebSocket(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self):
        self.actions = []
        super().__init__(("127.0.0.1", 0), FakeOneBotWebSocketHandler)


class FakeOneBotWebSocketHandler(socketserver.BaseRequestHandler):
    @staticmethod
    def read_exact(sock, count):
        chunks = bytearray()
        while len(chunks) < count:
            part = sock.recv(count - len(chunks))
            if not part:
                raise ConnectionError("socket closed")
            chunks.extend(part)
        return bytes(chunks)

    def handle(self):
        sock = self.request
        sock.settimeout(5)
        raw = bytearray()
        while b"\r\n\r\n" not in raw:
            raw.extend(sock.recv(4096))
        headers = bytes(raw).split(b"\r\n\r\n", 1)[0].decode("latin-1")
        key = next(line.split(":", 1)[1].strip() for line in headers.split("\r\n")
                   if line.lower().startswith("sec-websocket-key:"))
        accept = base64.b64encode(hashlib.sha1(
            (key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode("ascii")
        ).digest()).decode("ascii")
        sock.sendall(("HTTP/1.1 101 Switching Protocols\r\n"
                      "Upgrade: websocket\r\nConnection: Upgrade\r\n"
                      f"Sec-WebSocket-Accept: {accept}\r\n\r\n").encode("ascii"))
        first, second = self.read_exact(sock, 2)
        length = second & 0x7F
        if length == 126:
            length = struct.unpack("!H", self.read_exact(sock, 2))[0]
        elif length == 127:
            length = struct.unpack("!Q", self.read_exact(sock, 8))[0]
        mask = self.read_exact(sock, 4) if second & 0x80 else b"\0\0\0\0"
        payload = self.read_exact(sock, length)
        request = json.loads(bytes(value ^ mask[i % 4] for i, value in enumerate(payload)))
        self.server.actions.append(request)
        response = json.dumps({
            "status": "ok", "retcode": 0, "echo": request["echo"],
            "data": {"feeds": [{
                "tid": "real-service-feed", "time": FIXED_NOW,
                "nickname": "service fixture", "content": "真实 service 路径",
                "images": [], "like_count": 0, "comment_count": 0,
            }], "has_more": False, "next_cursor": None},
        }, ensure_ascii=False).encode("utf-8")
        size = len(response)
        if size < 126:
            frame_header = bytes((0x81, size))
        elif size < 65536:
            frame_header = bytes((0x81, 126)) + struct.pack("!H", size)
        else:
            frame_header = bytes((0x81, 127)) + struct.pack("!Q", size)
        sock.sendall(frame_header + response)


class HttpClient:
    def __init__(self, port):
        self.port = port
        self.cookie = ""

    def request(self, method, path, body=None, *, origin=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        headers = {"Host": f"127.0.0.1:{self.port}", "Accept": "application/json"}
        if self.cookie:
            headers["Cookie"] = self.cookie
        if body is not None:
            headers["Content-Type"] = "application/json"
        if origin is not None:
            headers["Origin"] = origin
        raw = json.dumps(body, ensure_ascii=False).encode() if body is not None else None
        conn.request(method, path, body=raw, headers=headers)
        response = conn.getresponse()
        payload = response.read()
        if response.getheader("Set-Cookie"):
            self.cookie = response.getheader("Set-Cookie").split(";", 1)[0]
        result = None
        if "json" in (response.getheader("Content-Type") or ""):
            result = json.loads(payload) if payload else None
        status = response.status
        location = response.getheader("Location")
        conn.close()
        return status, result, payload, location


class QzoneUnifiedHandlerE2ETests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="qzone-http-e2e-")
        self.transport = FeedTransport()

        def service_factory(state_dir):
            return service_mod.QzoneService(
                state_dir=str(state_dir), cfg=core.QzoneConfig(owner_uin="12345"),
                transport=self.transport, clock=lambda: FIXED_NOW,
            )

        routes.reset()
        routes.configure(service_factory=service_factory, clock=lambda: FIXED_NOW)
        config = {
            "stateDir": str(Path(self.tmp.name) / "state"),
            "uiDir": str(SERVER.parent / "ui"),
            "fishHouseWebDir": str(FISH_HOUSE),
            "lanHost": "127.0.0.1", "lanPort": 0, "loopPort": 0,
            "lanNets": ["127.0.0.0/8", "::1/128"],
        }
        fixture = json.loads((SERVER / "fixtures" / "fixture.json").read_text(encoding="utf-8"))
        self.app = uc.App(config, fixture)
        self.server = uc.ConsoleServer(("127.0.0.1", 0), uc.Handler, self.app)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.client = HttpClient(self.port)
        self.origin = f"http://127.0.0.1:{self.port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        routes.reset()
        self.tmp.cleanup()

    def test_same_origin_home_static_owner_gate_read_snapshot_refresh_and_limit(self):
        with patch.dict(os.environ, {"DAFEIYU_QZONE_READ_ENABLED": "0"}):
            status, _, _, _ = self.client.request("GET", "/api/qzone/latest")
            self.assertEqual(status, 404, "deployment switch must default off")

        with patch.dict(os.environ, {"DAFEIYU_QZONE_READ_ENABLED": "1"}):
            status, _, _, location = self.client.request("GET", "/home")
            self.assertEqual((status, location), (302, "/fish_house/home.html"))
            status, _, body, _ = self.client.request("GET", location)
            self.assertEqual(status, 200, body[:300])
            self.assertIn(b"data-tab=\"qq-admin\"", body)
            status, _, body, _ = self.client.request("GET", "/qzone-panel.js")
            self.assertEqual(status, 200)
            self.assertIn(b"/api/qzone/latest", body)
            self.assertNotIn(b"/api/qzone/publish", body)

            status, payload, _, _ = self.client.request("GET", "/api/qzone/latest?limit=20")
            self.assertEqual(status, 401)
            self.assertEqual(payload["error"]["code"], "owner_unauth")
            self.assertEqual(self.transport.calls, [])

            pair = self.app.pair_store.mint()
            status, _, _, _ = self.client.request(
                "POST", "/api/setup", {"pair": pair, "password": "fixture-owner-pass-123"},
                origin=self.origin)
            self.assertEqual(status, 200)
            self.assertTrue(self.client.cookie)

            status, payload, _, _ = self.client.request("GET", "/api/qzone/latest?limit=20")
            self.assertEqual(status, 200)
            self.assertEqual(payload["data"]["items"], [])

            status, payload, _, _ = self.client.request(
                "POST", "/api/qzone/refresh", {"action": "qzone.refresh", "confirm": True},
                origin="http://localhost:%d" % self.port)
            self.assertEqual(status, 403, "cross-origin refresh must be rejected")
            self.assertEqual(self.transport.calls, [])

            status, payload, _, _ = self.client.request(
                "POST", "/api/qzone/refresh", {"action": "qzone.refresh", "confirm": False},
                origin=self.origin)
            self.assertEqual(status, 400)
            self.assertEqual(payload["error"]["code"], "bad_request")
            self.assertEqual(self.transport.calls, [])

            status, payload, _, _ = self.client.request(
                "POST", "/api/qzone/refresh", {"action": "qzone.refresh", "confirm": True},
                origin=self.origin)
            self.assertEqual(status, 200)
            self.assertEqual(payload["data"]["items"][0]["text"], "只读动态夹具")
            self.assertNotIn("cookie", json.dumps(payload).lower())
            self.assertEqual([call[0] for call in self.transport.calls],
                             [napcat.ACTION_GET_QZONE_FEEDS])

            status, payload, _, _ = self.client.request(
                "GET", "/api/qzone/latest?limit=20")
            self.assertEqual(status, 200)
            self.assertEqual(payload["data"]["items"][0]["tid"], "fixture-feed-1")

            status, payload, _, _ = self.client.request(
                "POST", "/api/qzone/refresh", {"action": "qzone.refresh", "confirm": True},
                origin=self.origin)
            self.assertEqual(status, 429)
            self.assertEqual(payload["error"]["code"], "min_interval")
            self.assertEqual(len(self.transport.calls), 1,
                             "rate-limited refresh must not call NapCat")

            for path in ("/api/qzone/publish", "/api/qzone/followup", "/api/qzone/config"):
                status, _, _, _ = self.client.request(
                    "POST", path, {"confirm": True}, origin=self.origin)
                self.assertEqual(status, 404, path)
            self.assertEqual(len(self.transport.calls), 1)

    def test_real_service_factory_clock_state_and_loopback_ws(self):
        routes.reset()
        with FakeOneBotWebSocket() as upstream:
            upstream_thread = threading.Thread(target=upstream.serve_forever, daemon=True)
            upstream_thread.start()
            endpoint = f"ws://127.0.0.1:{upstream.server_address[1]}"
            state_home = str(Path(self.tmp.name) / "real-service-state")
            with patch.dict(os.environ, {
                "DAFEIYU_QZONE_READ_ENABLED": "1",
                "DAFEIYU_QZONE_ONEBOT_URL": endpoint,
                "DAFEIYU_QZONE_STATE_HOME": state_home,
            }), patch.object(routes, "_default_clock", new=lambda: FIXED_NOW):
                status, _, _, _ = self.client.request("POST", "/api/setup",
                    {"pair": self.app.pair_store.mint(), "password": "fixture-owner-pass-123"},
                    origin=self.origin)
                self.assertEqual(status, 200)

                status, payload, _, _ = self.client.request("GET", "/api/qzone/latest")
                self.assertEqual(status, 200)
                self.assertEqual(payload["data"]["items"], [])
                with self.assertRaises(ValueError):
                    core.QzoneConfig.from_dict({})
                svc = routes._service(self.app_handler())
                self.assertTrue(callable(svc.clock))
                self.assertEqual(svc.cfg.owner_uin, "00000")

                status, payload, _, _ = self.client.request("POST", "/api/qzone/refresh",
                    {"action": "qzone.refresh", "confirm": True}, origin=self.origin)
                self.assertEqual(status, 200, payload)
                self.assertEqual(payload["data"]["items"][0]["text"], "真实 service 路径")
                self.assertEqual([action["action"] for action in upstream.actions],
                                 [napcat.ACTION_GET_QZONE_FEEDS])

                routes.reset()  # Force the next request to reload the persisted file.
                status, payload, _, _ = self.client.request("GET", "/api/qzone/latest")
                self.assertEqual(status, 200)
                self.assertEqual(payload["data"]["items"][0]["tid"], "real-service-feed")
                status, payload, _, _ = self.client.request("POST", "/api/qzone/refresh",
                    {"action": "qzone.refresh", "confirm": True}, origin=self.origin)
                self.assertEqual(status, 429)
                self.assertEqual(payload["error"]["code"], "min_interval")
                self.assertEqual(len(upstream.actions), 1)
            upstream.shutdown()
            upstream_thread.join(timeout=2)

    def app_handler(self):
        """Minimal handler object used only to resolve the real service cache key."""
        return type("Handler", (), {"app": self.app, "authed": lambda _self: True})()


if __name__ == "__main__":
    unittest.main(verbosity=2)
