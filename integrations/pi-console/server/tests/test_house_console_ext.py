#!/usr/bin/env python3
"""Unit tests for house_console_ext.py: same-origin /home, 9-module APIs, Gemini health & switch."""
import http.server
import json
import os
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

SERVER_DIR = Path(__file__).resolve().parents[1]
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

import unified_console as uc
import house_console_ext
import wechat_account


class FakeUpstream(http.server.BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def do_POST(self):
        if self.path not in ("/api/admin/wechat-memory/" + "a" * 64, "/api/admin/wechat-persona/" + "a" * 64):
            self.send_error(404)
            return
        if (self.headers.get("x-unified-admin-token") != "a" * 72
                or self.headers.get("Origin") != f"http://127.0.0.1:{self.server.server_port}"):
            self.send_error(403)
            return
        self.rfile.read(int(self.headers["Content-Length"]))
        self.send_error(409, "private upstream detail")

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        if self.path in ("/api/admin/wechat-memory", "/api/admin/wechat-persona"):
            if self.headers.get("x-unified-admin-token") != "a" * 72:
                self.send_error(403)
                return
            body = json.dumps({"ok": True, "groups": [{"scope": "a" * 64}]}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path == "/fish_house/themes/default/videos/example.mp4":
            content = b"0123456789"
            if self.headers.get("Range") == "bytes=2-4":
                self.send_response(206)
                self.send_header("Content-Range", "bytes 2-4/10")
                content = content[2:5]
            elif self.headers.get("Range"):
                self.send_response(416)
                self.send_header("Content-Range", "bytes */10")
                content = b""
            else:
                self.send_response(200)
            self.send_header("Content-Type", "video/mp4")
            self.send_header("Content-Length", str(len(content)))
            self.send_header("Accept-Ranges", "bytes")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(content)
            return
        if self.path == "/api/house/live-events":
            data = b'data: {"type":"config","seq":1}\n\ndata: {"type":"state","seq":2}\n\n'
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            self.wfile.write(data)
            return
        if self.path == "/status/routing":
            body = json.dumps({
                "client_authenticated": True,
                "models_complete": True,
                "available_models": ["gemini-auto", "gemini-neon", "gemini-carbon"],
            }).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path == "/status/summary":
            body = json.dumps({
                "overall": "degraded",
                "alert_level": "critical",
                "alert_message": "Google session is UNAUTHENTICATED",
                "client_authenticated": False,
                "account_status": "UNAUTHENTICATED",
                "available_models": ["gemini-auto"],
                "models_complete": False,
                "egress": {"status": "ok"},
                "self_heal": {"attempts": 1},
            }).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path == "/api/status":
            body = json.dumps({"ok": True, "wechat": {"activity_state": "idle"}}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_error(404)


class HouseConsoleExtTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="dafeiyu-ext-test-")
        root = Path(self.tmp.name)
        self.house_web = root / "web_house"
        self.house_web.mkdir()
        (self.house_web / "home.html").write_text("<!doctype html><title>Home</title>", encoding="utf-8")
        self.qq_root = root / "qq_root"
        (self.qq_root / "secrets" / "model-hub").mkdir(parents=True)
        (self.qq_root / "secrets" / "model-hub" / "profiles.json").write_text(json.dumps({
            "profiles": [
                {"id": "gemini-web", "name": "Gemini Web", "model": "gemini-neon-low", "fallback_to": "workbuddy-deepseek"},
            ]
        }), encoding="utf-8")
        (self.qq_root / "secrets" / "model-hub" / "route.jsonl").write_text(
            json.dumps({"latency_ms": 1200, "fallback": False}) + "\n" +
            json.dumps({"latency_ms": 2400, "fallback": True}) + "\n",
            encoding="utf-8",
        )
        self.wx_root = root / "wx_root"
        (self.wx_root / "data" / "memory" / "chats" / "g1").mkdir(parents=True)
        (self.wx_root / "data" / "memory" / "chats" / "g1" / "messages.jsonl").write_text("{}\n{}\n", encoding="utf-8")
        (self.wx_root / "data" / "stickers" / "开心").mkdir(parents=True)
        (self.wx_root / "data" / "stickers" / "开心" / "01_开心欢笑.gif").write_bytes(b"GIF89a")
        # Authoritative WeChat core config: two enabled proactive groups. The
        # shadow settings.json intentionally does not exist; writes must go here.
        self.wx_config = self.wx_root / "config.json"
        self.wx_config.write_text(json.dumps({
            "group": {"proactiveJudge": {"enabled": True, "groups": ["试点群A", "试点群B"]}},
            "persona": {"ownerName": "大肥鱼", "untouched": "keep-me"},
            "wechat": {"account": "pilot-account"},
        }, ensure_ascii=False), encoding="utf-8")

        self.upstream = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FakeUpstream)
        self.up_thread = threading.Thread(target=self.upstream.serve_forever, daemon=True)
        self.up_thread.start()
        up_url = f"http://127.0.0.1:{self.upstream.server_port}"

        cfg = {
            "stateDir": str(root / "state"),
            "uiDir": str(SERVER_DIR.parent / "ui"),
            "lanHost": "127.0.0.1",
            "lanPort": 0,
            "loopPort": 0,
            "fishHouseWebDir": str(self.house_web),
            "fishHouseUrl": up_url,
            "geminiGatewayUrl": up_url,
            "qqRoot": str(self.qq_root),
            "wechatCoreRoot": str(self.wx_root),
            "wechatBridgeHealthPath": str(self.wx_root / "bridge-health.json"),
            "kdfIterations": 1000,
        }
        self.app = uc.App(cfg, fixture={"qq": {}, "wechat": {}})
        self.app.passwords.set("test-pass-1234")
        self.srv = uc.ConsoleServer(("127.0.0.1", 0), uc.Handler, self.app)
        self.srv.lan_listener = False
        self.port = self.srv.server_address[1]
        self.app.loop_port = self.port
        self.thread = threading.Thread(target=self.srv.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.port}"
        self.cookie = self._login()

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        self.upstream.shutdown()
        self.upstream.server_close()
        self.tmp.cleanup()

    def _login(self):
        req = urllib.request.Request(
            self.base + "/api/login",
            data=json.dumps({"password": "test-pass-1234"}).encode("utf-8"),
            headers={"Content-Type": "application/json", "Origin": self.base},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=4) as r:
            return r.headers.get("Set-Cookie", "").split(";")[0]

    def _get(self, path, authed=True):
        headers = {"Cookie": self.cookie} if authed else {}
        req = urllib.request.Request(self.base + path, headers=headers, method="GET")
        with urllib.request.urlopen(req, timeout=4) as r:
            return r.status, r.read()

    def _post(self, path, body, authed=True):
        headers = {"Content-Type": "application/json", "Origin": self.base}
        if authed:
            headers["Cookie"] = self.cookie
        req = urllib.request.Request(self.base + path, data=json.dumps(body).encode("utf-8"), headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=4) as r:
            return r.status, json.loads(r.read().decode("utf-8"))

    def test_same_origin_fish_house_and_traversal_blocked(self):
        st, body = self._get("/fish_house/home.html", authed=False)
        self.assertEqual(st, 200)
        self.assertIn(b"<title>Home</title>", body)
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self._get("/fish_house/../../etc/passwd", authed=False)
        self.assertIn(ctx.exception.code, (403, 404))

    def test_wechat_memory_proxy_requires_session_origin_and_private_runtime(self):
        endpoint = "/api/memory/wechat"
        group_path = endpoint + "/" + "a" * 64
        correction = {"id": "00000000-0000-0000-0000-000000000001", "value": "更正"}
        runtime = self.qq_root / "data-qq" / "unified-pilot.memory-admin.json"
        runtime.parent.mkdir()
        runtime.write_text(json.dumps({"port": self.upstream.server_port, "token": "a" * 72}), encoding="utf-8")
        runtime.chmod(0o600)

        with self.assertRaises(urllib.error.HTTPError) as unauthenticated:
            self._get(endpoint, authed=False)
        self.assertEqual(unauthenticated.exception.code, 401)
        status, body = self._get(endpoint)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["groups"], [{"scope": "a" * 64}])
        self.assertNotIn(b"a" * 72, body)

        for origin in (None, "http://example.invalid"):
            headers = {"Content-Type": "application/json", "Cookie": self.cookie}
            if origin:
                headers["Origin"] = origin
            request = urllib.request.Request(self.base + group_path, json.dumps(correction).encode(), headers)
            with self.assertRaises(urllib.error.HTTPError) as invalid_origin:
                urllib.request.urlopen(request, timeout=4)
            self.assertEqual(invalid_origin.exception.code, 403)

        with self.assertRaises(urllib.error.HTTPError) as invalid_body:
            self._post(group_path, dict(correction, scope="b" * 64))
        self.assertEqual(invalid_body.exception.code, 400)
        with self.assertRaises(urllib.error.HTTPError) as conflict:
            self._post(group_path, correction)
        self.assertEqual(conflict.exception.code, 409)
        self.assertEqual(json.load(conflict.exception)["error"], "条目已被更新或到期，请刷新后重试")
        pause = {"id": correction["id"], "paused": True, "previousPaused": False}
        with self.assertRaises(urllib.error.HTTPError) as stale_pause:
            self._post(group_path, pause)
        self.assertEqual(stale_pause.exception.code, 409)
        for invalid in (dict(pause, paused=1), dict(pause, value="correction"),
                        {"id": pause["id"], "paused": True}):
            with self.assertRaises(urllib.error.HTTPError) as malformed_pause:
                self._post(group_path, invalid)
            self.assertEqual(malformed_pause.exception.code, 400)
        request = urllib.request.Request(self.base + group_path, json.dumps(pause).encode(),
                                         {"Content-Type": "application/json", "Cookie": self.cookie})
        with self.assertRaises(urllib.error.HTTPError) as missing_origin:
            urllib.request.urlopen(request, timeout=4)
        self.assertEqual(missing_origin.exception.code, 403)

        if os.name == "posix":
            runtime.chmod(0o644)
            with self.assertRaises(urllib.error.HTTPError) as unsafe_file:
                self._get(endpoint)
            self.assertEqual(unsafe_file.exception.code, 503)

    def test_wechat_group_persona_requires_same_origin_and_masks_conflicts(self):
        endpoint = "/api/persona/wechat"
        path = endpoint + "/" + "a" * 64
        runtime = self.qq_root / "data-qq" / "unified-pilot.memory-admin.json"
        runtime.parent.mkdir()
        runtime.write_text(json.dumps({"port": self.upstream.server_port, "token": "a" * 72}), encoding="utf-8")
        runtime.chmod(0o600)
        with self.assertRaises(urllib.error.HTTPError) as anonymous:
            self._get(endpoint, authed=False)
        self.assertEqual(anonymous.exception.code, 401)
        self.assertEqual(self._get(endpoint)[0], 200)
        change = {"preset": "温柔", "previous": ""}
        request = urllib.request.Request(self.base + path, json.dumps(change).encode(),
                                         {"Content-Type": "application/json", "Cookie": self.cookie})
        with self.assertRaises(urllib.error.HTTPError) as missing_origin:
            urllib.request.urlopen(request, timeout=4)
        self.assertEqual(missing_origin.exception.code, 403)
        with self.assertRaises(urllib.error.HTTPError) as invalid:
            self._post(path, dict(change, scope="b" * 64))
        self.assertEqual(invalid.exception.code, 400)
        with self.assertRaises(urllib.error.HTTPError) as conflict:
            self._post(path, change)
        self.assertEqual(conflict.exception.code, 409)
        self.assertEqual(json.load(conflict.exception)["error"], "该群人设已被其他操作更新，请刷新核对")

    def test_wechat_account_uses_auth_same_origin_and_only_serves_redrawn_code(self):
        url = "/api/account/wechat"
        token = "https://login.weixin.qq.com/l/synthetic-qr"
        with patch.object(wechat_account, "_native") as native:
            with self.assertRaises(urllib.error.HTTPError) as anonymous:
                self._get(url, authed=False)
            self.assertEqual(anonymous.exception.code, 401)
            request = urllib.request.Request(self.base + url + "/qr", b"{}",
                                             {"Content-Type": "application/json", "Cookie": self.cookie})
            with self.assertRaises(urllib.error.HTTPError) as foreign:
                urllib.request.urlopen(request, timeout=4)
            self.assertEqual(foreign.exception.code, 403)
            native.assert_not_called()
            native.return_value = {"state": "online", "canScan": False, "qr": token}
            _, online = self._post(url + "/qr", {})
            self.assertEqual(online["state"], "online")
            self.assertNotIn("image", online)
            native.return_value = {"state": "scan", "canScan": True, "qr": token}
            _, scan = self._post(url + "/qr", {})
            self.assertTrue(scan["image"].startswith("data:image/png;base64,iVBOR"))
            self.assertNotIn(token, json.dumps(scan))

    def test_wechat_reconnect_confirms_only_the_bridge_and_blocks_repeat(self):
        with patch.object(wechat_account, "_idle", return_value=True), \
                patch.object(wechat_account, "LAST_RESTART", float("-inf")), \
                patch.object(wechat_account, "_systemctl", side_effect=["11", "", "12", "active"]) as command:
            code, value = wechat_account._reconnect(self.wx_root, {})
            self.assertEqual((code, value), (200, {"ok": True, "restarted": True}))
            self.assertEqual(command.call_args_list[1].args, ("restart", "dafeiyu-wechat-bridge.service"))
            self.assertEqual(wechat_account._reconnect(self.wx_root, {})[0], 429)
            self.assertEqual(command.call_count, 4)
        with patch.object(wechat_account, "_idle", return_value=False), \
                patch.object(wechat_account, "LAST_RESTART", float("-inf")), \
                patch.object(wechat_account, "_systemctl", return_value="11") as command:
            self.assertEqual(wechat_account._reconnect(self.wx_root, {})[0], 409)
            command.assert_called_once()

    def test_wechat_reconnect_uses_live_generation_even_after_another_event(self):
        health = self.wx_root / "bridge-health.json"
        now = time.time()
        for label, owner, age, worker_alive, expected in (
            ("active", 11, 6, True, 409),
            ("active vision and reply", 11, 205, True, 409),
            ("dead generation thread", 11, 6, False, 200),
            ("old bridge", 10, 6, True, 200),
            ("stale marker", 11, 245, True, 200),
        ):
            with self.subTest(case=label):
                health.write_text(json.dumps({
                    "last_event": {"phase": "received", "time": now},
                    "active_generation": {"pid": owner, "thread": 12, "start_ticks": "123",
                                          "time": now - age, "trace": "a" * 64},
                    "outbound_traces": {},
                }), encoding="utf-8")
                with patch.object(wechat_account, "_generation_owner_live", return_value=worker_alive), \
                        patch.object(wechat_account, "LAST_RESTART", float("-inf")), \
                        patch.object(wechat_account, "_systemctl",
                                     side_effect=["11", "", "13", "active"]) as command:
                    status, _ = wechat_account._reconnect(
                        self.wx_root, {"wechatBridgeHealthPath": str(health)})
                    self.assertEqual(status, expected)
                    self.assertEqual(command.call_count, 1 if expected == 409 else 4)

    def test_wechat_account_separates_native_login_from_the_bridge(self):
        situations = (
            (True, {"online": True, "listener": False}, "window_blocked", "window_pending"),
            (True, {"online": False}, "client_unavailable", "offline"),
            (True, {"online": True, "listener": False}, "", "listener_paused"),
            (True, {"online": True, "listener": True, "accountOk": False}, "", "account_blocked"),
            (True, {"online": True, "listener": False}, "login_required", "login_pending"),
            (True, {"online": True, "listener": True}, "", "ready"),
            (False, {"online": True, "listener": True}, "login_required", "disabled"),
        )
        for enabled, bridge, reason, expected in situations:
            with self.subTest(expected=expected):
                status = {"gateways": [{"platform": "wechat", "enabled": enabled, "bridge": bridge}],
                          "bridgeDiagnostics": {"fresh": True, "client": {"reason": reason}}}
                with patch.object(wechat_account, "_native", return_value={"state": "online"}), \
                        patch.object(uc.adapters.WeChatAdapter, "_get", return_value=status):
                    _, body = self._get("/api/account/wechat")
                    data = json.loads(body)
                    self.assertEqual(data["state"], "online")
                    self.assertEqual(data["bridgeStatus"], expected)
                    self.assertFalse(data["canScan"])
                    self.assertNotIn("image", data)

    def test_live_video_proxies_get_head_and_unsatisfiable_range(self):
        url = self.base + "/fish_house/themes/default/videos/example.mp4"
        request = urllib.request.Request(url, headers={"Range": "bytes=2-4"})
        with urllib.request.urlopen(request, timeout=4) as response:
            self.assertEqual(response.status, 206)
            self.assertEqual(response.headers["Content-Range"], "bytes 2-4/10")
            self.assertEqual(response.headers["Accept-Ranges"], "bytes")
            self.assertEqual(response.read(), b"234")
        with urllib.request.urlopen(urllib.request.Request(url, method="HEAD"), timeout=4) as response:
            self.assertEqual(response.headers["Content-Length"], "10")
            self.assertEqual(response.read(), b"")
        bad = urllib.request.Request(url, headers={"Range": "bytes=20-"})
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(bad, timeout=4)
        with ctx.exception as response:
            self.assertEqual(response.code, 416)
            self.assertEqual(response.headers["Content-Range"], "bytes */10")

    def test_live_sse_is_same_origin_and_head_does_not_open_stream(self):
        url = self.base + "/api/house/live-events"
        with urllib.request.urlopen(url, timeout=4) as response:
            self.assertEqual(response.headers.get_content_type(), "text/event-stream")
            frames = [json.loads(line[6:]) for line in response if line.startswith(b"data: ")]
            self.assertEqual([frame["type"] for frame in frames], ["config", "state"])
        with self.assertRaises(urllib.error.HTTPError) as foreign:
            urllib.request.urlopen(urllib.request.Request(url, headers={"Origin": "http://foreign.example"}), timeout=4)
        self.assertEqual(foreign.exception.code, 403)
        with self.assertRaises(urllib.error.HTTPError) as head:
            urllib.request.urlopen(urllib.request.Request(url, method="HEAD"), timeout=4)
        self.assertEqual(head.exception.code, 405)

    def test_channel_inventory_enforces_auth_and_isolates_adapter_failure(self):
        with self.assertRaises(urllib.error.HTTPError) as unauthenticated:
            self._get("/api/channels", authed=False)
        self.assertEqual(unauthenticated.exception.code, 401)
        qq_status = {"connected": True, "running": True, "client": "OneBot v11",
                     "sessionToken": "fixture-private-value"}
        with patch.object(self.app.console.qq, "status", return_value=(qq_status, [])), \
                patch.object(self.app.console.wechat, "status", side_effect=RuntimeError("fixture-error")):
            _, raw = self._get("/api/channels")
        self.assertNotIn(b"fixture-private-value", raw)
        self.assertNotIn(b"fixture-error", raw)
        qq, wechat = json.loads(raw)["channels"]
        self.assertEqual(qq, {"platform": "qq", "state": "ok", "connected": True,
                              "running": True, "client": "OneBot v11"})
        self.assertEqual(wechat, {"platform": "wechat", "state": "unknown", "connected": None,
                                  "running": None, "client": ""})

    def test_channel_inventory_preserves_wechat_scan_state(self):
        with patch.object(self.app.console.wechat, "status", return_value=({
            "connected": False, "running": False, "clientState": "qr", "client": "微信",
        }, [])):
            _, raw = self._get("/api/channels")
        self.assertEqual(json.loads(raw)["channels"][1]["state"], "qr")

    def test_delivery_summary_enforces_auth_and_exports_only_counters(self):
        path = "/api/delivery/summary"
        now = int(time.time())
        health = self.wx_root / "bridge-health.json"
        health.write_text(json.dumps({"outbound_traces": {
            "fixture-trace-1": {"status": "confirmed", "time": now},
            "fixture-trace-2": {"status": "unknown", "time": now - 14},
            "fixture-trace-3": {"status": "submitting", "time": now - 20},
            "fixture-trace-4": {"status": "confirmed", "time": now - 86420},
            "fixture-trace-5": {"status": "confirmed", "time": now + 600},
        }, "message": "fixture-private-text"}), encoding="utf-8")
        panel = {"queued": 0, "confirmed": 10, "uncertain": 1,
                 "runtimeRecords": [{"detail": "fixture-private-record"}]}
        with patch.object(self.app.console.qq, "_controller_instance",
                          return_value=SimpleNamespace(state=lambda: panel)) as controller:
            with self.assertRaises(urllib.error.HTTPError) as denied:
                self._get(path, authed=False)
            self.assertEqual(denied.exception.code, 401)
            controller.assert_not_called()
            _, raw = self._get(path)
        self.assertNotIn(b"fixture-", raw)
        self.assertEqual(json.loads(raw), {
            "qq": {"status": "available", "queueDepth": 0,
                   "confirmed": 10, "unknown": 1, "pending": None},
            "wechat": {"status": "available", "queueDepth": None,
                       "confirmed": 1, "unknown": 1, "pending": 1},
            "wechatWindowSeconds": 86400,
        })

    def test_delivery_summary_distinguishes_invalid_and_missing_sources(self):
        path = "/api/delivery/summary"
        health = self.wx_root / "bridge-health.json"
        with patch.object(self.app.console.qq, "_controller_instance",
                          side_effect=RuntimeError("fixture-secret-error")):
            _, raw = self._get(path)
            self.assertEqual(json.loads(raw)["qq"]["status"], "unavailable")
            self.assertEqual(json.loads(raw)["wechat"]["status"], "unavailable")
            health.write_text(json.dumps({"outbound_traces": {
                "fixture-trace": {"status": "unknown", "time": int(time.time())},
                "fixture-bool-time": {"status": "confirmed", "time": True},
                "fixture-bad-status": {"status": [], "time": int(time.time())},
            }}), encoding="utf-8")
            _, raw = self._get(path)
            self.assertNotIn(b"fixture-", raw)
            self.assertEqual(json.loads(raw)["wechat"], {
                "status": "partial", "queueDepth": None, "confirmed": 0,
                "unknown": 1, "pending": 0,
            })
        panel = {"queued": 2, "confirmed": False}
        with patch.object(self.app.console.qq, "_controller_instance",
                          return_value=SimpleNamespace(state=lambda: panel)):
            _, raw = self._get(path)
        self.assertEqual(json.loads(raw)["qq"], {
            "status": "partial", "queueDepth": 2, "confirmed": None,
            "unknown": None, "pending": None,
        })

    def test_memory_stickers_trace_proactive_endpoints(self):
        _, mem = self._get("/api/memory/status")
        mem_d = json.loads(mem)
        self.assertEqual(mem_d["groups"], 1)
        self.assertEqual(mem_d["entries"], 2)

        _, stk = self._get("/api/stickers")
        stk_d = json.loads(stk)
        self.assertEqual(stk_d["wechatGifCount"], 1)
        self.assertEqual(stk_d["activePack"], "whalegirl_batch1")

        _, tr = self._get("/api/trace/summary")
        tr_d = json.loads(tr)
        self.assertEqual(tr_d["recentCalls"], 2)
        self.assertEqual(tr_d["fallbackCount"], 1)

        _, pr = self._get("/api/proactive/config")
        self.assertTrue(json.loads(pr)["ok"])

    def test_trace_reads_production_route_and_confirmed_bridge_samples(self):
        route = self.qq_root / "run" / "model-route.log"
        route.parent.mkdir()
        trace = "a" * 64
        route.write_text("\n".join(map(json.dumps, [
            {"latency_ms": True, "fallback": True},
            {"latency_ms": float("inf"), "fallback": True},
            {"latency_ms": -100, "fallback": True},
            {"latency_ms": 1700, "fallback": False, "trace_id": trace, "prompt": "fixture-private"},
            {"latency_ms": 3700, "fallback": True},
        ])), encoding="utf-8")
        bridge = self.wx_root / "bridge.log"
        self.app.content_cfg["wechatBridgeLog"] = str(bridge)
        base = f"2026-11-25 03:20:00 delivery_timing trace='{trace}' kind='group'"
        bridge.write_text("\n".join([
            f"{base} pickup_ms=7 queue_to_send_result_ms=5100 send_ms=1100 confirmed=True",
            f"{base} pickup_ms=7 queue_to_send_result_ms=6600 send_ms=1700 confirmed=True",
            f"{base} pickup_ms=7 queue_to_send_result_ms=100 send_ms=100 confirmed=False",
            f"{base} pickup_ms=7 queue_to_send_result_ms=-1 send_ms=100 confirmed=True",
        ]), encoding="utf-8")
        with self.assertRaises(urllib.error.HTTPError) as anonymous:
            self._get("/api/trace/summary", authed=False)
        self.assertEqual(anonymous.exception.code, 401)
        _, raw = self._get("/api/trace/summary")
        result = json.loads(raw)
        self.assertEqual((result["recentCalls"], result["fallbackCount"], result["gatewayTracedCalls"]), (2, 1, 1))
        self.assertEqual((result["gatewayP50Ms"], result["gatewayP95Ms"]), (1700, 3700))
        self.assertEqual((result["wechatP50Ms"], result["wechatP95Ms"], result["wechatConfirmedSamples"]), (5100, 6600, 2))
        self.assertEqual(result["qqP50Ms"], "--")
        self.assertNotIn(trace, raw.decode())
        self.assertNotIn("fixture-private", raw.decode())

    def test_memory_storage_requires_auth_and_distinguishes_store_paths(self):
        endpoint = "/api/memory/storage"
        with self.assertRaises(urllib.error.HTTPError) as anonymous:
            self._get(endpoint, authed=False)
        self.assertEqual(anonymous.exception.code, 401)
        _, raw = self._get(endpoint)
        self.assertEqual(json.loads(raw)["stores"]["wechatSwift"]["state"], "unavailable")

        data = self.qq_root / "data-qq"
        qq = data / "QQMemoryReliable" / ("scope-" + "1" * 16)
        (qq / "journal").mkdir(parents=True)
        (qq / "state.json").write_bytes(b"fixture-raw")
        (qq / "state.json.bak").write_bytes(b"bak")
        (qq / "journal" / "seg-000001.log").write_bytes(b"log")
        compat = self.wx_root / "data" / "memory" / "chats" / ("2" * 16)
        compat.mkdir()
        (compat / "messages.jsonl").write_bytes(b"fixture-message-body")
        _, raw = self._get(endpoint)
        self.assertNotIn(b"fixture-message-body", raw)
        self.assertNotIn(b"scope-", raw)
        self.assertEqual(json.loads(raw)["stores"], {
            "qqReliable": {"state": "available", "scopes": 1, "bytes": 17, "stateBackups": 1},
            "wechatSwift": {"state": "empty", "scopes": 0, "bytes": 0, "stateBackups": 0},
            "wechatCompat": {"state": "available", "scopes": 1, "bytes": 20, "stateBackups": None},
        })

        swift = data / "wechat-group-memory" / ("3" * 64)
        swift.mkdir(parents=True)
        (swift / "state.json").write_bytes(b"fixture-note")
        with patch.object(Path, "open", side_effect=AssertionError("metadata probe opened a file")):
            metadata = house_console_ext._build_memory_storage({
                "qqRoot": str(self.qq_root), "wechatCoreRoot": str(self.wx_root),
            })
        self.assertEqual(metadata["stores"]["wechatSwift"],
                         {"state": "available", "scopes": 1, "bytes": 12, "stateBackups": 0})

    def test_memory_storage_reports_truncation_and_skips_links(self):
        data = self.qq_root / "data-qq"
        scope = data / "QQMemoryReliable" / ("scope-" + "a" * 16)
        scope.mkdir(parents=True)
        (scope / "state.json").write_bytes(b"abcd")
        with patch.object(house_console_ext, "MEMORY_SCAN_LIMIT", 1):
            _, body = self._get("/api/memory/storage")
        self.assertEqual(json.loads(body)["stores"]["qqReliable"], {
            "state": "partial", "scopes": 1, "bytes": 0, "stateBackups": 0,
        })
        external = self.qq_root / "outside-memory"
        external.write_bytes(b"outside fixture contents")
        try:
            (scope / "state.json.bak").symlink_to(external)
        except OSError:
            self.skipTest("symbolic links unavailable for this environment")
        _, body = self._get("/api/memory/storage")
        self.assertEqual(json.loads(body)["stores"]["qqReliable"], {
            "state": "partial", "scopes": 1, "bytes": 4, "stateBackups": 0,
        })

    def test_gemini_health_degradation_and_safe_switch_readback(self):
        _, raw = self._get("/api/gemini/health")
        gd = json.loads(raw)
        self.assertTrue(gd["degraded"])
        self.assertFalse(gd["client_authenticated"])
        self.assertEqual(gd["active_model"], "gemini-neon-low")

        st, res = self._post("/api/gemini/model", {"model": "gemini-auto"})
        self.assertEqual(st, 200)
        self.assertEqual(res["verification"]["state"], "confirmed")
        self.assertEqual(res["verification"]["observed"], "gemini-auto")

        st, res = self._post("/api/gemini/model", {"model": "gemini-neon"})
        self.assertEqual(st, 200)
        self.assertEqual(res["verification"]["observed"], "gemini-neon")

        # Invalid model rejected fail-closed
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self._post("/api/gemini/model", {"model": "gemini-neon-low"})
        self.assertEqual(ctx.exception.code, 400)

    def test_gemini_disabled_health_is_clean_and_never_contacts_gateway(self):
        profiles_path = self.qq_root / "secrets" / "model-hub" / "profiles.json"
        profiles = json.loads(profiles_path.read_text(encoding="utf-8"))
        profiles["profiles"][0]["enabled"] = False
        profiles_path.write_text(json.dumps(profiles), encoding="utf-8")

        with patch.object(uc.house_console_ext, "_http_get_json",
                          side_effect=AssertionError("disabled Gemini must not contact 8085")) as gateway:
            _, raw = self._get("/api/gemini/health")
        health = json.loads(raw)
        self.assertTrue(health["ok"])
        self.assertFalse(health["reachable"])
        self.assertEqual(health["overall"], "disabled")
        self.assertFalse(health["degraded"])
        self.assertEqual(health["alert_level"], "ok")
        self.assertEqual(health["available_models"], [])
        self.assertEqual(health["allowed_models"], [])
        self.assertEqual(health["active_model"], "gemini-neon-low")
        self.assertEqual(health["profile_name"], "Gemini Web")
        gateway.assert_not_called()

    def test_gemini_model_post_rejects_locked_or_disabled_before_route_or_write(self):
        profiles_path = self.qq_root / "secrets" / "model-hub" / "profiles.json"
        for field, value, model in (("locked", True, "gemini-neon"),
                                    ("enabled", False, "gemini-auto")):
            with self.subTest(field=field):
                profiles = json.loads(profiles_path.read_text(encoding="utf-8"))
                profiles["profiles"][0][field] = value
                profiles_path.write_text(json.dumps(profiles), encoding="utf-8")
                original = profiles_path.read_bytes()
                with patch.object(uc.house_console_ext, "_http_get_json",
                                  side_effect=AssertionError("rejected model switch must not contact gateway")) as gateway:
                    with self.assertRaises(urllib.error.HTTPError) as ctx:
                        self._post("/api/gemini/model", {"model": model})
                self.assertEqual(ctx.exception.code, 409)
                gateway.assert_not_called()
                self.assertEqual(profiles_path.read_bytes(), original)
                self.assertEqual(list(profiles_path.parent.glob("profiles.json.bak-console-*")), [])
                self.assertEqual(list(profiles_path.parent.glob(".gemini-model-*")), [])

    def test_gemini_model_post_rejects_top_level_lock_when_profile_enabled(self):
        profiles_path = self.qq_root / "secrets" / "model-hub" / "profiles.json"
        profiles = json.loads(profiles_path.read_text(encoding="utf-8"))
        profiles["locked"] = True
        profiles["profiles"][0].pop("locked", None)
        profiles["profiles"][0]["enabled"] = True
        profiles_path.write_text(json.dumps(profiles), encoding="utf-8")
        original = profiles_path.read_bytes()

        with patch.object(uc.house_console_ext, "_http_get_json",
                          side_effect=AssertionError("locked model switch must not contact gateway")) as gateway:
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                self._post("/api/gemini/model", {"model": "gemini-auto"})
        self.assertEqual(ctx.exception.code, 409)
        gateway.assert_not_called()
        self.assertEqual(profiles_path.read_bytes(), original)
        self.assertEqual(list(profiles_path.parent.glob("profiles.json.bak-console-*")), [])
        self.assertEqual(list(profiles_path.parent.glob(".gemini-model-*")), [])

    def test_gemini_model_post_rejects_profile_change_after_route_check(self):
        profiles_path = self.qq_root / "secrets" / "model-hub" / "profiles.json"
        original = profiles_path.read_bytes()

        def route_then_change(*_args, **_kwargs):
            changed = json.loads(profiles_path.read_text(encoding="utf-8"))
            changed["profiles"][0]["model"] = "gemini-carbon"
            profiles_path.write_text(json.dumps(changed), encoding="utf-8")
            return {
                "available_models": ["gemini-neon"],
                "client_authenticated": True,
                "models_complete": True,
            }

        with patch.object(uc.house_console_ext, "_http_get_json", side_effect=route_then_change):
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                self._post("/api/gemini/model", {"model": "gemini-neon"})
        self.assertEqual(ctx.exception.code, 409)
        changed = json.loads(profiles_path.read_text(encoding="utf-8"))
        self.assertEqual(changed["profiles"][0]["model"], "gemini-carbon")
        self.assertFalse(list(profiles_path.parent.glob("profiles.json.bak-console-*")))
        self.assertEqual(list(profiles_path.parent.glob(".gemini-model-*")), [])
        self.assertNotEqual(profiles_path.read_bytes(), original)

    def test_proactive_settings_and_rolling_funnel(self):
        cfg = self.qq_root / "data-qq" / "qq-decision-config.json"
        cfg.parent.mkdir()
        group = "group:12345"
        cfg.write_text(json.dumps({group: {"threshold": 0.70, "dailyBudget": 60}}),
                       encoding="utf-8")
        log = self.qq_root / "run" / "qq-bot.log"
        log.parent.mkdir()
        stamp = int(time.time()) - 10
        fractional_stamp = f"{time.time() - 5:.6f}"
        scope = "1234567890abcdef"
        oldest_stamp = stamp - 3600
        log.with_suffix(".log.1").write_text(
            f"[qq_proactive_funnel] at={oldest_stamp} scope={scope} event=ordinary reason=-\n",
            encoding="utf-8",
        )
        log.with_suffix(".log.3").write_text(
            f"[qq_proactive_funnel] at={oldest_stamp} scope={scope} event=ordinary reason=-\n"
            f"[qq_proactive_funnel] at={stamp} scope={scope} event=confirmed reason=-\n",
            encoding="utf-8",
        )
        log.write_text(
            f"[qq_proactive_funnel] at={stamp} scope={scope} event=ordinary reason=-\n"
            f"[qq_proactive_funnel] at={stamp} scope={scope} event=candidate reason=none\n"
            f"[qq_proactive_funnel] at={fractional_stamp} scope={scope} event=candidate reason=none\n"
            f"[qq_proactive_funnel] at={fractional_stamp} scope={scope} event=candidate reason=-\n"
            f"[qq_proactive_funnel] at={fractional_stamp} scope={scope} event=candidate reason=bad.reason\n"
            f"[qq_proactive_funnel] at={stamp} scope={scope} event=score_after_reject reason=new_mention\n"
            f"[qq_proactive_funnel] at={stamp} scope={scope} event=after_enqueue_skip reason=model_declined\n"
            f"[qq_proactive_funnel] at={stamp} scope={scope} event=after_enqueue_skip reason=expired_after_model\n"
            f"[qq_proactive_funnel] at={stamp} scope={scope} event=score_rejected reason=score_below_threshold\n"
            f"[qq_proactive_funnel] at={stamp} scope={scope} event=score_rejected reason=provider-credential-unavailable\n"
            f"[qq_proactive_funnel] at={stamp} scope={scope} event=score_skipped reason=mode_not_enforce\n"
            f"[qq_proactive_funnel] at={stamp-86400} scope={scope} event=confirmed reason=none\n"
            f"[qq_proactive_funnel] at={stamp+86400} scope={scope} event=confirmed reason=none\n"
            "raw chat text must never appear in the result\n", encoding="utf-8",
        )
        body = {"group": group, "threshold": 0.75, "freshnessSeconds": 2}
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self._post("/api/proactive/config", body, authed=False)
        self.assertEqual(ctx.exception.code, 401)
        status, response = self._post("/api/proactive/config", body)
        self.assertEqual(status, 200)
        self.assertEqual(response["verification"], "persisted")
        _, raw = self._get("/api/proactive/config")
        result = json.loads(raw)
        self.assertEqual(result["qqGroups"][group],
                         {"threshold": 0.75, "freshnessSeconds": 2, "dailyBudget": 60})
        self.assertEqual(result["qqFunnel"]["counts"]["candidate"], 3)
        self.assertEqual(result["qqFunnel"]["windowSeconds"], 86400)
        self.assertEqual(result["qqFunnel"]["firstEventAt"], oldest_stamp)
        self.assertEqual(result["qqFunnel"]["lastEventAt"], float(fractional_stamp))
        self.assertEqual(result["qqFunnel"]["counts"]["ordinary"], 2)
        self.assertEqual(result["qqFunnel"]["groups"][scope]["ordinary"], 2)
        self.assertEqual(result["qqFunnel"]["counts"]["confirmed"], 1)
        self.assertEqual(result["qqFunnel"]["scoreAfterRejectReasons"], {"new_mention": 1})
        self.assertEqual(result["qqFunnel"]["counts"]["after_enqueue_skip"], 2)
        self.assertEqual(result["qqFunnel"]["afterEnqueueSkipReasons"], {
            "model_declined": 1, "expired_after_model": 1,
        })
        self.assertEqual(result["qqFunnel"]["counts"]["score_rejected"], 2)
        self.assertEqual(result["qqFunnel"]["scoreRejectedReasons"], {
            "score_below_threshold": 1, "provider-credential-unavailable": 1,
        })
        self.assertEqual(result["qqFunnel"]["counts"]["score_skipped"], 1)
        self.assertEqual(result["qqFunnel"]["scoreSkippedReasons"], {"mode_not_enforce": 1})
        self.assertNotIn(b"raw chat text", raw)

    def _write_qq_cfg(self, data):
        cfg = self.qq_root / "data-qq" / "qq-decision-config.json"
        cfg.parent.mkdir(parents=True, exist_ok=True)
        cfg.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        return cfg

    def _wx_doc(self):
        return json.loads(self.wx_config.read_text(encoding="utf-8"))

    def _install_wechat_settings_adapter(self, *, runtime_readback=True, file_write=True):
        """Synthetic loopback Node adapter: POST writes the authoritative fixture."""
        class NodeSettings:
            def __init__(inner):
                inner.runtime = self._wx_doc()
                inner.posts = []

            def _get(inner, path):
                if path != "/api/settings":
                    return None
                document = inner.runtime if runtime_readback else {}
                return {"group": json.loads(json.dumps(document.get("group", {})))}

            def _post(inner, path, body):
                inner.posts.append((path, body))
                if path != "/api/settings":
                    return {"ok": False}
                patch = body["group"]
                judge = inner.runtime["group"]["proactiveJudge"]
                if "publicQuestionCooldowns" in patch:
                    judge.setdefault("publicQuestionCooldowns", {}).update(patch["publicQuestionCooldowns"])
                if "proactiveGroupEnabled" in patch:
                    paused = set(judge.get("pausedGroups", []))
                    for group, enabled in patch["proactiveGroupEnabled"].items():
                        if enabled:
                            paused.discard(group)
                        else:
                            paused.add(group)
                    judge["pausedGroups"] = sorted(paused)
                if file_write:
                    self.wx_config.write_text(
                        json.dumps(inner.runtime, ensure_ascii=False), encoding="utf-8")
                return {"ok": True}

        adapter = NodeSettings()
        self.app.console.wechat = adapter
        return adapter

    def test_qq_incremental_cooldown_preserves_other_fields_and_readback(self):
        self._write_qq_cfg({
            "group:111": {"threshold": 0.7, "freshnessSeconds": 1, "dailyBudget": 50},
            "group:222": {"threshold": 0.8, "dailyBudget": 30},
        })
        st, res = self._post("/api/proactive/config", {
            "platform": "qq", "group": "group:111", "publicQuestionCooldownSeconds": 300,
        })
        self.assertEqual(st, 200)
        self.assertEqual(res["verification"], "persisted")
        _, raw = self._get("/api/proactive/config")
        result = json.loads(raw)
        self.assertEqual(result["qqGroups"]["group:111"], {
            "threshold": 0.7, "freshnessSeconds": 1, "dailyBudget": 50,
            "publicQuestionCooldownSeconds": 300,
        })
        self.assertEqual(result["qqGroups"]["group:222"],
                         {"threshold": 0.8, "dailyBudget": 30})
        self.assertEqual(result["publicQuestionCooldowns"]["qq"], {"group:111": 300})
        self.assertEqual(result["publicQuestionCooldownDefault"], 60)

    def test_qq_legacy_save_still_works_with_optional_cooldown(self):
        self._write_qq_cfg({"group:333": {"threshold": 0.5, "dailyBudget": 10}})
        st, _ = self._post("/api/proactive/config", {
            "group": "group:333", "threshold": 0.9, "freshnessSeconds": 2,
            "publicQuestionCooldownSeconds": 120,
        })
        self.assertEqual(st, 200)
        _, raw = self._get("/api/proactive/config")
        result = json.loads(raw)
        self.assertEqual(result["qqGroups"]["group:333"], {
            "threshold": 0.9, "dailyBudget": 10, "freshnessSeconds": 2,
            "publicQuestionCooldownSeconds": 120,
        })
        # Legacy without cooldown keeps the previous contract and adds no key.
        st, _ = self._post("/api/proactive/config", {
            "group": "group:333", "threshold": 0.6, "freshnessSeconds": 1,
        })
        self.assertEqual(st, 200)
        _, raw = self._get("/api/proactive/config")
        row = json.loads(raw)["qqGroups"]["group:333"]
        self.assertEqual(row["threshold"], 0.6)
        self.assertEqual(row["freshnessSeconds"], 1)
        self.assertEqual(row["publicQuestionCooldownSeconds"], 120)

    def test_qq_ordinary_toggle_and_threshold_are_incremental_and_readable(self):
        self._write_qq_cfg({
            "group:111": {
                "mode": "enforce", "threshold": 0.45,
                "publicQuestionEnabled": True, "dailyBudget": 50,
            },
            "group:222": {"mode": "enforce", "threshold": 0.70,
                           "publicQuestionEnabled": True, "dailyBudget": 30},
        })
        st, _ = self._post("/api/proactive/config", {
            "platform": "qq", "group": "group:111",
            "ordinaryReplyEnabled": True, "threshold": 0.62,
        })
        self.assertEqual(st, 200)
        row = json.loads(self._get("/api/proactive/config")[1])["qqGroups"]["group:111"]
        self.assertEqual(row["threshold"], 0.62)
        self.assertTrue(row["publicQuestionEnabled"])
        self.assertEqual(row["dailyBudget"], 50)
        state = json.loads(self._get("/api/proactive/config")[1])["qqOrdinary"]["group:111"]
        self.assertEqual(state, {"enabled": True, "source": "explicit",
                                 "explicit": True, "threshold": 0.62})

        st, _ = self._post("/api/proactive/config", {
            "platform": "qq", "group": "group:111", "ordinaryReplyEnabled": False,
        })
        self.assertEqual(st, 200)
        row = json.loads(self._get("/api/proactive/config")[1])["qqGroups"]["group:111"]
        self.assertFalse(row["ordinaryReplyEnabled"])
        self.assertTrue(row["publicQuestionEnabled"])
        state = json.loads(self._get("/api/proactive/config")[1])["qqOrdinary"]["group:111"]
        self.assertEqual(state["enabled"], False)
        self.assertEqual(state["source"], "explicit")
        legacy = json.loads(self._get("/api/proactive/config")[1])["qqOrdinary"]["group:222"]
        self.assertEqual(legacy["enabled"], False)
        self.assertEqual(legacy["source"], "default")

    def test_qq_stale_edit_cannot_overwrite_another_window(self):
        group = "group:111"
        path = self._write_qq_cfg({group: {"mode": "enforce", "threshold": 0.45,
                                          "ordinaryReplyEnabled": True, "dailyBudget": 35}})
        request = {"platform": "qq", "group": group, "threshold": 0.62, "expected": {"threshold": 0.45}}
        self.assertEqual(self._post("/api/proactive/config", request)[0], 200)
        saved = path.read_bytes()
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self._post("/api/proactive/config", {**request, "threshold": 0.70})
        self.assertEqual(caught.exception.code, 409)
        self.assertEqual(json.load(caught.exception)["conflict"], "stale_edit")
        self.assertEqual(path.read_bytes(), saved)
        status, _ = self._post("/api/proactive/config", {
            "platform": "qq", "group": group, "ordinaryReplyEnabled": False,
            "expected": {"ordinaryReplyEnabled": True},
        })
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(path.read_bytes())[group], {
            "mode": "enforce", "threshold": 0.62, "ordinaryReplyEnabled": False, "dailyBudget": 35,
        })

    def test_qq_policy_readback_requires_matching_live_revision(self):
        group = "group:111"
        path = self._write_qq_cfg({group: {"mode": "enforce", "threshold": 0.45,
                                          "ordinaryReplyEnabled": True}})
        _, before = house_console_ext._read_proactive_document(path)
        state = {"running": True, "connected": True, "config": {"not_public": "fixture-private"},
                 "groupReplyPolicy": {"revision": before, "groups": {
                     group: {"ordinary": True, "publicQuestion": False, "threshold": 0.45, "extra": "private"},
                     "not_a_group": {"ordinary": True, "publicQuestion": False, "threshold": 0.45},
                 }}}
        with patch.object(self.app.console.qq, "_controller_instance",
                          return_value=SimpleNamespace(state=lambda: state)):
            initial = json.loads(self._get("/api/proactive/config")[1])["qqRuntime"]
            self.assertEqual(initial, {"verification": "applied", "running": True, "groups": {
                group: {"ordinary": True, "publicQuestion": False, "threshold": 0.45}
            }})
            status, result = self._post("/api/proactive/config", {
                "platform": "qq", "group": group, "ordinaryReplyEnabled": False, "threshold": 0.61,
            })
            self.assertEqual(status, 200)
            self.assertEqual(result["verification"], "persisted")
            current = json.loads(self._get("/api/proactive/config")[1])
            self.assertEqual(current["qqGroups"][group]["threshold"], 0.61)
            self.assertEqual(current["qqRuntime"], {"verification": "pending", "running": None, "groups": {}})

            _, after = house_console_ext._read_proactive_document(path)
            self.assertNotEqual(after, before)
            state["groupReplyPolicy"] = {"revision": after, "groups": {
                group: {"ordinary": False, "publicQuestion": False, "threshold": 0.61}
            }}
            state["running"] = False
            applied = json.loads(self._get("/api/proactive/config")[1])["qqRuntime"]
            self.assertEqual(applied["verification"], "applied")
            self.assertFalse(applied["running"])
            self.assertFalse(applied["groups"][group]["ordinary"])

        with patch.object(self.app.console.qq, "_controller_instance", side_effect=OSError()):
            current = json.loads(self._get("/api/proactive/config")[1])
            self.assertEqual(current["qqRuntime"]["verification"], "unavailable")
            self.assertEqual(current["qqGroups"][group]["threshold"], 0.61)

    def test_wechat_save_persists_and_get_readback(self):
        document = self._wx_doc()
        document["group"]["proactiveJudge"]["publicQuestionCooldowns"] = {"试点群B": 120}
        self.wx_config.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
        adapter = self._install_wechat_settings_adapter()
        st, res = self._post("/api/proactive/config", {
            "platform": "wechat", "group": "试点群A", "publicQuestionCooldownSeconds": 45,
        })
        self.assertEqual(st, 200)
        self.assertEqual(res["verification"], "persisted")
        self.assertEqual(res["observed"], 45)
        self.assertEqual(res["runtimeObserved"], 45)
        self.assertEqual(adapter.posts, [("/api/settings", {
            "group": {"publicQuestionCooldowns": {"试点群A": 45}},
        })])
        doc = self._wx_doc()
        self.assertEqual(doc["group"]["proactiveJudge"]["publicQuestionCooldowns"], {
            "试点群A": 45, "试点群B": 120,
        })
        # Every other config key/value survives the deep merge.
        self.assertEqual(doc["group"]["proactiveJudge"]["enabled"], True)
        self.assertEqual(doc["group"]["proactiveJudge"]["groups"], ["试点群A", "试点群B"])
        self.assertEqual(doc["persona"], {"ownerName": "大肥鱼", "untouched": "keep-me"})
        self.assertEqual(doc["wechat"], {"account": "pilot-account"})
        self.assertFalse((self.wx_root / "data" / "settings.json").exists())
        _, raw = self._get("/api/proactive/config")
        result = json.loads(raw)
        self.assertEqual(result["publicQuestionCooldowns"]["wechat"], {
            "试点群A": 45, "试点群B": 120,
        })
        self.assertEqual(result["wechatEnabledGroups"], ["试点群A", "试点群B"])
        self.assertTrue(result["wechatJudgeEnabled"])
        self.assertEqual(result["wechatRuntime"], {
            "verification": "matched", "matchedGroups": ["试点群A", "试点群B"],
        })

    def test_wechat_get_only_confirms_exact_runtime_matches(self):
        adapter = self._install_wechat_settings_adapter()
        judge = adapter.runtime["group"]["proactiveJudge"]
        judge["groups"].append("只在运行配置里的群")
        judge["publicQuestionCooldowns"] = {"试点群A": 90}
        current = json.loads(self._get("/api/proactive/config")[1])
        self.assertEqual(current["wechatConfigState"], "available")
        self.assertEqual(current["wechatRuntime"], {
            "verification": "pending", "matchedGroups": ["试点群B"],
        })
        self.assertNotIn("只在运行配置里的群", json.dumps(current, ensure_ascii=False))
        self.assertEqual(adapter.posts, [])
        with patch.object(adapter, "_get", side_effect=OSError()):
            current = json.loads(self._get("/api/proactive/config")[1])
        self.assertEqual(current["wechatAuthorizedGroups"], ["试点群A", "试点群B"])
        self.assertEqual(current["wechatRuntime"], {
            "verification": "unavailable", "matchedGroups": [],
        })

    def test_unreadable_wechat_policy_is_distinct_from_disabled_policy(self):
        document = self._wx_doc()
        document["group"]["proactiveJudge"]["enabled"] = False
        self.wx_config.write_text(json.dumps(document), encoding="utf-8")
        self._install_wechat_settings_adapter()
        current = json.loads(self._get("/api/proactive/config")[1])
        self.assertEqual(current["wechatConfigState"], "disabled")
        self.assertEqual(current["wechatAuthorizedGroups"], [])
        self.wx_config.write_text("{not valid json", encoding="utf-8")
        current = json.loads(self._get("/api/proactive/config")[1])
        self.assertEqual(current["wechatConfigState"], "unreadable")
        self.assertEqual(current["wechatRuntime"]["verification"], "unavailable")

    def test_wechat_stale_edit_does_not_call_the_core_writer(self):
        adapter = self._install_wechat_settings_adapter()
        change = {"platform": "wechat", "group": "试点群A", "publicQuestionCooldownSeconds": 90,
                  "expected": {"publicQuestionCooldownSeconds": 60}}
        self.assertEqual(self._post("/api/proactive/config", change)[0], 200)
        before = self.wx_config.read_bytes()
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self._post("/api/proactive/config", {**change, "publicQuestionCooldownSeconds": 120})
        self.assertEqual(caught.exception.code, 409)
        self.assertEqual(json.load(caught.exception)["conflict"], "stale_edit")
        self.assertEqual(self.wx_config.read_bytes(), before)
        self.assertEqual(len(adapter.posts), 1)
        self.assertEqual(self._post("/api/proactive/config", {
            "platform": "wechat", "group": "试点群A", "ordinaryReplyEnabled": False,
            "expected": {"ordinaryReplyEnabled": True},
        })[0], 200)
        pj = self._wx_doc()["group"]["proactiveJudge"]
        self.assertEqual(pj["publicQuestionCooldowns"], {"试点群A": 90})
        self.assertEqual(pj["pausedGroups"], ["试点群A"])

    def test_wechat_paused_group_remains_selectable_and_can_be_restored(self):
        self._install_wechat_settings_adapter()
        body = {"platform": "wechat", "group": "试点群A", "ordinaryReplyEnabled": False,
                "publicQuestionCooldownSeconds": 75}
        status, result = self._post("/api/proactive/config", body)
        self.assertEqual(status, 200)
        self.assertIs(result["ordinaryReplyEnabled"], False)
        self.assertEqual(result["runtimeObserved"], 75)
        current = json.loads(self._get("/api/proactive/config")[1])
        self.assertEqual(current["wechatAuthorizedGroups"], ["试点群A", "试点群B"])
        self.assertEqual(current["wechatEnabledGroups"], ["试点群B"])
        self.assertEqual(self._wx_doc()["group"]["proactiveJudge"]["pausedGroups"], ["试点群A"])
        del body["publicQuestionCooldownSeconds"]
        body["ordinaryReplyEnabled"] = True
        self.assertIs(self._post("/api/proactive/config", body)[1]["ordinaryReplyEnabled"], True)
        current = json.loads(self._get("/api/proactive/config")[1])
        self.assertEqual(current["wechatEnabledGroups"], ["试点群A", "试点群B"])
        self.assertEqual(current["publicQuestionCooldowns"]["wechat"]["试点群A"], 75)

    def test_wechat_group_toggle_requires_strict_boolean_and_authorized_group(self):
        adapter = self._install_wechat_settings_adapter()
        for group, enabled in (("试点群A", "false"), ("未授权群", True)):
            with self.subTest(group=group, enabled=enabled), self.assertRaises(urllib.error.HTTPError) as ctx:
                self._post("/api/proactive/config", {
                    "platform": "wechat", "group": group, "ordinaryReplyEnabled": enabled,
                })
            self.assertEqual(ctx.exception.code, 400)
        self.assertEqual(adapter.posts, [])

    def test_wechat_save_fails_closed_when_node_runtime_readback_is_unavailable(self):
        adapter = self._install_wechat_settings_adapter(runtime_readback=False)
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self._post("/api/proactive/config", {
                "platform": "wechat", "group": "试点群A", "publicQuestionCooldownSeconds": 75,
            })
        self.assertEqual(ctx.exception.code, 409)
        self.assertEqual(adapter.posts, [])
        self.assertNotIn("publicQuestionCooldowns", self._wx_doc()["group"]["proactiveJudge"])

    def test_wechat_save_fails_closed_if_bridge_config_does_not_match_runtime(self):
        adapter = self._install_wechat_settings_adapter(file_write=False)
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self._post("/api/proactive/config", {
                "platform": "wechat", "group": "试点群A", "publicQuestionCooldownSeconds": 80,
            })
        self.assertEqual(ctx.exception.code, 409)
        self.assertEqual(adapter.posts, [("/api/settings", {
            "group": {"publicQuestionCooldowns": {"试点群A": 80}},
        })])
        self.assertNotIn("publicQuestionCooldowns", self._wx_doc()["group"]["proactiveJudge"])

    def test_wechat_not_allowed_group_rejected_without_expanding_whitelist(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self._post("/api/proactive/config", {
                "platform": "wechat", "group": "未授权群", "publicQuestionCooldownSeconds": 60,
            })
        self.assertEqual(ctx.exception.code, 400)
        doc = self._wx_doc()
        self.assertEqual(doc["group"]["proactiveJudge"]["groups"], ["试点群A", "试点群B"])
        self.assertNotIn("publicQuestionCooldowns", doc["group"]["proactiveJudge"])

    def test_wechat_disabled_judge_rejected(self):
        doc = self._wx_doc()
        doc["group"]["proactiveJudge"]["enabled"] = False
        self.wx_config.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self._post("/api/proactive/config", {
                "platform": "wechat", "group": "试点群A", "publicQuestionCooldownSeconds": 60,
            })
        self.assertEqual(ctx.exception.code, 400)
        self.assertNotIn("publicQuestionCooldowns",
                         self._wx_doc()["group"]["proactiveJudge"])

    def test_unauthenticated_incremental_post_rejected(self):
        for body in (
            {"platform": "qq", "group": "group:111", "publicQuestionCooldownSeconds": 60},
            {"platform": "wechat", "group": "试点群A", "publicQuestionCooldownSeconds": 60},
        ):
            with self.subTest(body=body):
                with self.assertRaises(urllib.error.HTTPError) as ctx:
                    self._post("/api/proactive/config", body, authed=False)
                self.assertEqual(ctx.exception.code, 401)

    def test_invalid_cooldown_values_rejected(self):
        self._write_qq_cfg({"group:111": {"threshold": 0.7, "dailyBudget": 50}})
        cases = (
            {"platform": "wechat", "group": "试点群A", "publicQuestionCooldownSeconds": 29},
            {"platform": "wechat", "group": "试点群A", "publicQuestionCooldownSeconds": 1201},
            {"platform": "wechat", "group": "试点群A", "publicQuestionCooldownSeconds": 0},
            {"platform": "wechat", "group": "试点群A", "publicQuestionCooldownSeconds": "60"},
            {"platform": "wechat", "group": "试点群A", "publicQuestionCooldownSeconds": 60.0},
            {"platform": "wechat", "group": "试点群A", "publicQuestionCooldownSeconds": True},
            {"platform": "wechat", "publicQuestionCooldownSeconds": 60},
            {"platform": "qq", "group": "group:111", "publicQuestionCooldownSeconds": 29},
            {"platform": "qq", "group": "group:111", "publicQuestionCooldownSeconds": "60"},
        )
        for body in cases:
            with self.subTest(body=body):
                with self.assertRaises(urllib.error.HTTPError) as ctx:
                    self._post("/api/proactive/config", body)
                self.assertEqual(ctx.exception.code, 400)
        self.assertNotIn("publicQuestionCooldowns", self._wx_doc()["group"]["proactiveJudge"])
        row = json.loads((self.qq_root / "data-qq" / "qq-decision-config.json").read_text(encoding="utf-8"))["group:111"]
        self.assertNotIn("publicQuestionCooldownSeconds", row)

    def test_default_constant_and_empty_maps_surfaced(self):
        _, raw = self._get("/api/proactive/config")
        result = json.loads(raw)
        self.assertEqual(result["publicQuestionCooldownDefault"], 60)
        self.assertEqual(result["publicQuestionCooldowns"], {"qq": {}, "wechat": {}})

    def test_cross_group_isolation_on_both_platforms(self):
        self._install_wechat_settings_adapter()
        self._write_qq_cfg({
            "group:111": {"threshold": 0.7, "dailyBudget": 50},
            "group:222": {"threshold": 0.8, "dailyBudget": 30},
        })
        self._post("/api/proactive/config", {
            "platform": "wechat", "group": "试点群A", "publicQuestionCooldownSeconds": 90,
        })
        self._post("/api/proactive/config", {
            "platform": "qq", "group": "group:111", "publicQuestionCooldownSeconds": 600,
        })
        _, raw = self._get("/api/proactive/config")
        result = json.loads(raw)
        self.assertEqual(result["publicQuestionCooldowns"], {
            "qq": {"group:111": 600}, "wechat": {"试点群A": 90},
        })
        doc = self._wx_doc()
        self.assertEqual(doc["group"]["proactiveJudge"]["publicQuestionCooldowns"], {"试点群A": 90})
        qq = json.loads((self.qq_root / "data-qq" / "qq-decision-config.json").read_text(encoding="utf-8"))
        self.assertNotIn("publicQuestionCooldownSeconds", qq["group:222"])


if __name__ == "__main__":
    unittest.main()
