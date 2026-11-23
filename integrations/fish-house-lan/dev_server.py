#!/usr/bin/env python3
"""Isolated Local Dev & R5.2 Protocol-Verification Server for DaFeiYu Home.

Runs strictly locally (default 127.0.0.1:18890). Never touches Pi production
services or real QQ/WeChat channels.

Modes:
  - --mode readonly: mirrors live read-only fish-house-lan (/api/status,
    /fish_house/state.json, /api/diag). Returns 404 for /api/session and all
    extended console endpoints so the frontend's honest read-only lock &
    contract placeholder behavior can be verified.
  - --mode fixture: ALSO mounts:
    * Real-time SSE stream GET /api/house/events
    * Codex unified_console.py endpoints (/api/session, /api/login, /api/logout,
      /api/overview, /api/model, /api/conversation, /api/usage)
    * Codex tool_routes.py fixture (/api/tools/status, /api/tools/plugins, /api/tools/logs)
    * R5.2 contracted extension fixtures (/api/memory/status, /api/stickers,
      /api/proactive/config, /api/trace/summary) — ALL explicitly marked mock=true!
"""
from __future__ import annotations

import argparse
import json
import queue
import secrets
import threading
import time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

BASE = Path(__file__).resolve().parent
WEB = BASE / "web"

STATE_LOCK = threading.Lock()
SSE_SUBSCRIBERS: list[queue.Queue] = []

FIXTURE_STATE = {
    "session_tokens": set(),
    "password": "dev-fixture-pass",
    "qq_model": "deepseek-chat",
    "wechat_model": "deepseek-chat",
    "fallback_models": ["gemini-2.5-flash", "workbuddy-deepseek"],
    "conversations": [
        {"platform": "qq", "id": "group-10001", "name": "大肥鱼调试群（桩）",
         "enabled": True, "style": "dafeiyu", "frequency": "natural", "limit": 80},
        {"platform": "wechat", "id": "wx-fixture-group", "name": "微信测试群（桩）",
         "enabled": True, "style": "dafeiyu", "frequency": "quiet", "limit": 60},
    ],
    "house_phase": "idle",
    "house_source": "demo",
    "house_verified": False,
    "house_seq": 1,
    "house_at": int(time.time()),
    "house_bubble": "",
    "diags": [],
}
QZONE_FIXTURE = {"latest_reads": 0, "refreshes": 0, "latest_delay_ms": 0, "asset_failure": False}


def broadcast_sse(payload: dict):
    msg = "data: " + json.dumps(payload, ensure_ascii=False) + "\n\n"
    with STATE_LOCK:
        dead = []
        for q in SSE_SUBSCRIBERS:
            try:
                q.put_nowait(msg)
            except queue.Full:
                dead.append(q)
        for q in dead:
            SSE_SUBSCRIBERS.remove(q)


def parse_cookie(header: str, name: str) -> str | None:
    if not header:
        return None
    for part in header.split(";"):
        k, _, v = part.strip().partition("=")
        if k == name:
            return v
    return None


class DevServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, addr, handler, mode: str):
        super().__init__(addr, handler)
        self.mode = mode


class DevHandler(SimpleHTTPRequestHandler):
    server_version = "DaFeiYuHomeDev/2.2"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(WEB), **kwargs)

    def log_message(self, fmt, *args):
        pass

    def _json(self, code: int, obj, extra_headers=None):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        if extra_headers:
            for k, v in extra_headers:
                self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _read_json(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > 65536:
            return {}
        raw = self.rfile.read(length)
        return json.loads(raw.decode("utf-8", "replace"))

    def _authed(self) -> bool:
        tok = parse_cookie(self.headers.get("Cookie", ""), "dafeiyu_session")
        with STATE_LOCK:
            return bool(tok and tok in FIXTURE_STATE["session_tokens"])

    def end_headers(self):
        self.send_header("X-Content-Type-Options", "nosniff")
        path = (getattr(self, "path", "") or "").split("?")[0]
        if path.endswith((".webp", ".png", ".jpg", ".mp4")):
            self.send_header("Cache-Control", "public, max-age=3600")
        elif path.endswith((".css", ".js", ".html", ".json")):
            self.send_header("Cache-Control", "no-cache")
        super().end_headers()

    def _serve_sse(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "keep-alive")
        self.end_headers()

        q: queue.Queue = queue.Queue(maxsize=32)
        with STATE_LOCK:
            SSE_SUBSCRIBERS.append(q)
            initial = {
                "seq": FIXTURE_STATE["house_seq"],
                "phase": FIXTURE_STATE["house_phase"],
                "source": FIXTURE_STATE["house_source"],
                "verified": FIXTURE_STATE["house_verified"],
                "bubble": FIXTURE_STATE["house_bubble"],
                "at": FIXTURE_STATE["house_at"],
            }
        try:
            self.wfile.write(("data: " + json.dumps(initial, ensure_ascii=False) + "\n\n").encode("utf-8"))
            self.wfile.flush()
            while True:
                try:
                    chunk = q.get(timeout=12.0)
                    self.wfile.write(chunk.encode("utf-8"))
                    self.wfile.flush()
                except queue.Empty:
                    self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            with STATE_LOCK:
                if q in SSE_SUBSCRIBERS:
                    SSE_SUBSCRIBERS.remove(q)

    def do_GET(self):
        path = urlsplit(self.path).path

        if path == "/api/test/qzone_counts":
            with STATE_LOCK:
                return self._json(200, dict(QZONE_FIXTURE))

        if path in ("/admin-tools.js", "/admin-tools.css", "/qzone-panel.js"):
            if path == "/qzone-panel.js" and QZONE_FIXTURE.get("asset_failure"):
                return self._json(404, {"error": "synthetic QZone asset failure"})
            source = BASE.parent / "pi-console" / "ui" / path.lstrip("/")
            try:
                payload = source.read_bytes()
            except OSError:
                return self._json(404, {"error": "test static not found"})
            ctype = "text/css; charset=utf-8" if path.endswith(".css") else "application/javascript; charset=utf-8"
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            return self.wfile.write(payload)
        query = urlsplit(self.path).query

        if path == "/":
            target = "/fish_house/home.html" + (("?" + query) if query else "")
            self.send_response(302)
            self.send_header("Location", target)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        if path == "/healthz":
            return self._json(200, {"ok": True, "mode": self.server.mode, "version": "2.2.0-r5.2"})

        if path == "/api/status":
            with STATE_LOCK:
                return self._json(200, {
                    "ok": True,
                    "timestamp": int(time.time()),
                    "qq": {"connected": True, "running": True,
                           "busy": FIXTURE_STATE["house_phase"] == "thinking",
                           "model": FIXTURE_STATE["qq_model"]},
                    "wechat": {"connected": True, "running": True, "client_state": "ok",
                               "model": FIXTURE_STATE["wechat_model"]},
                    "activity": {"phase": FIXTURE_STATE["house_phase"],
                                 "source": FIXTURE_STATE["house_source"],
                                 "verified": FIXTURE_STATE["house_verified"]},
                    "mode": "local-dev-" + self.server.mode,
                    "persona": {"character": "whale", "name": "大肥鱼"}
                })

        if path == "/fish_house/state.json":
            with STATE_LOCK:
                return self._json(200, {
                    "seq": FIXTURE_STATE["house_seq"],
                    "phase": FIXTURE_STATE["house_phase"],
                    "source": FIXTURE_STATE["house_source"],
                    "verified": FIXTURE_STATE["house_verified"],
                    "bubble": FIXTURE_STATE["house_bubble"],
                    "at": FIXTURE_STATE["house_at"],
                })

        if path == "/api/diag":
            with STATE_LOCK:
                return self._json(200, {
                    "ok": True,
                    "received": len(FIXTURE_STATE["diags"]),
                    "last": FIXTURE_STATE["diags"][-1] if FIXTURE_STATE["diags"] else None,
                    "all": FIXTURE_STATE["diags"][-20:],
                })

        # SSE & Unified Console Fixtures (only active in --mode fixture)
        if self.server.mode in ("fixture", "owner_fixture"):
            if path == "/api/house/events":
                return self._serve_sse()

            if path == "/api/session":
                return self._json(200, {
                    "authenticated": self._authed(),
                    "setupAvailable": False,
                    "secure": False,
                    "lanUrl": "http://127.0.0.1:%d" % self.server.server_address[1],
                    "fixtureDemo": self.server.mode == "fixture",
                })

            if self.server.mode == "owner_fixture" and path == "/api/qzone/latest":
                if not self._authed():
                    return self._json(401, {"error": "登录状态已失效"})
                with STATE_LOCK:
                    QZONE_FIXTURE["latest_reads"] += 1
                    delay = int(QZONE_FIXTURE["latest_delay_ms"])
                if delay:
                    time.sleep(min(delay, 3000) / 1000)
                return self._json(200, {"ok": True, "data": {
                    "items": [{"author": "好友·***1234", "text": "测试动态 <b>安全文本</b>",
                               "ts": int(time.time()), "day": "测试"}],
                    "refreshed_ts": int(time.time()), "sent": False, "consumesQuota": False
                }})

            if path == "/api/overview":
                if not self._authed():
                    return self._json(401, {"error": "登录状态已失效，请重新登录"})
                with STATE_LOCK:
                    return self._json(200, {
                        "qq": {"connected": True, "running": True, "model": FIXTURE_STATE["qq_model"],
                               "client": "NapCatQQ (local fixture)", "lastRead": int(time.time()),
                               "capabilities": {"model": True}},
                        "wechat": {"connected": True, "running": True, "model": FIXTURE_STATE["wechat_model"],
                                   "client": "WeChat Bridge (local fixture)", "clientState": "ok",
                                   "lastRead": int(time.time()), "capabilities": {"model": True}},
                        "fallbackModels": list(FIXTURE_STATE["fallback_models"]),
                        "models": [
                            {"id": "deepseek-chat", "name": "DeepSeek Chat", "platforms": ["qq", "wechat"]},
                            {"id": "deepseek-reasoner", "name": "DeepSeek Reasoner", "platforms": ["qq", "wechat"]},
                            {"id": "gemini-2.5-flash", "name": "Gemini Flash 备用", "platforms": ["qq", "wechat"]},
                        ],
                        "styleOptions": [
                            {"id": "dafeiyu", "name": "大肥鱼·鲸鱼娘"},
                            {"id": "gentle", "name": "温柔陪伴"},
                            {"id": "playful", "name": "活泼吐槽"},
                        ],
                        "frequencyOptions": [
                            {"id": "quiet", "name": "低频"},
                            {"id": "natural", "name": "正常"},
                            {"id": "active", "name": "活跃"},
                        ],
                        "conversations": list(FIXTURE_STATE["conversations"]),
                        "diagnostics": {"fixtureDemo": True},
                    })

            if path in ("/api/tools/status", "/api/tools/plugins", "/api/tools/logs",
                        "/api/usage", "/api/memory/status", "/api/stickers",
                        "/api/proactive/config", "/api/trace/summary"):
                if not self._authed():
                    return self._json(401, {"error": "登录状态已失效，请重新登录"})
                if path == "/api/tools/status":
                    return self._json(200, {"ok": True, "mock": True, "napcat": {"reachable": True, "loggedIn": True}})
                if path == "/api/tools/plugins":
                    return self._json(200, {"ok": True, "mock": True, "plugins": [
                        {"id": "social-expression", "name": "社交表达与黑话热路径", "enabled": True},
                        {"id": "memory-reliable", "name": "按群隔离长期记忆 v3", "enabled": True},
                        {"id": "fish-house-lan", "name": "大肥鱼家园 R5.2", "enabled": True},
                    ]})
                if path == "/api/tools/logs":
                    return self._json(200, {"ok": True, "mock": True, "lines": [
                        "[fixture] model-hub trace route active (X-Dafeiyu-Trace-Id)",
                        "[fixture] wechat-core static slangTerms cache hit p50=0.064ms",
                        "[fixture] fish-house SSE stream healthy",
                    ]})
                if path == "/api/usage":
                    return self._json(200, {"ok": True, "mock": True, "todayCalls": 42})
                if path == "/api/memory/status":
                    return self._json(200, {"ok": True, "mock": True, "isolation": "per-group-v3",
                                            "groups": 4, "entries": 328, "dbSizeMb": 4.2, "journalState": "healthy"})
                if path == "/api/stickers":
                    return self._json(200, {"ok": True, "mock": True, "activePack": "whalegirl_batch1",
                                            "count": 6, "rotation": "anti-repeat-round-robin", "cooldownSec": 45})
                if path == "/api/proactive/config":
                    return self._json(200, {"ok": True, "mock": True, "judgeMode": "enforce-v2", "threshold": 0.68})
                if path == "/api/trace/summary":
                    return self._json(200, {"ok": True, "mock": True, "gatewayTrace": "model-hub active",
                                            "qqP50Ms": 2480, "wechatP50Ms": 3120,
                                            "contextP50Ms": 142, "promptP50Ms": 116.8})

        if path.startswith("/fish_house/"):
            return super().do_GET()

        return self._json(404, {"error": "接口不存在或当前为只读模式"})

    def do_POST(self):
        path = urlsplit(self.path).path
        body = self._read_json()
        if path == "/api/test/qzone_asset_failure":
            with STATE_LOCK:
                QZONE_FIXTURE["asset_failure"] = bool(body.get("enabled"))
            return self._json(200, {"ok": True})

        if path == "/api/test/qzone_delay":
            with STATE_LOCK:
                QZONE_FIXTURE["latest_delay_ms"] = max(0, min(3000, int(body.get("delay_ms", 0))))
            return self._json(200, {"ok": True})
        if path == "/api/test/expire_session":
            tok = parse_cookie(self.headers.get("Cookie", ""), "dafeiyu_session")
            with STATE_LOCK:
                FIXTURE_STATE["session_tokens"].discard(tok)
            return self._json(200, {"ok": True})

        if path == "/api/diag":
            record = dict(body)
            record["at"] = int(time.time())
            record["ip"] = self.client_address[0]
            with STATE_LOCK:
                FIXTURE_STATE["diags"].append(record)
                if len(FIXTURE_STATE["diags"]) > 50:
                    FIXTURE_STATE["diags"].pop(0)
            return self._json(200, {"ok": True})

        if path == "/api/dev/inject_event":
            with STATE_LOCK:
                FIXTURE_STATE["house_seq"] += 1
                FIXTURE_STATE["house_phase"] = str(body.get("phase") or body.get("type") or "idle")
                FIXTURE_STATE["house_source"] = str(body.get("source") or "qq")
                FIXTURE_STATE["house_verified"] = bool(body.get("verified", True))
                FIXTURE_STATE["house_bubble"] = str(body.get("bubble") or "")
                FIXTURE_STATE["house_at"] = int(body.get("at") or time.time())
                ev = {
                    "seq": FIXTURE_STATE["house_seq"],
                    "phase": FIXTURE_STATE["house_phase"],
                    "source": FIXTURE_STATE["house_source"],
                    "verified": FIXTURE_STATE["house_verified"],
                    "bubble": FIXTURE_STATE["house_bubble"],
                    "at": FIXTURE_STATE["house_at"],
                }
            broadcast_sse(ev)
            return self._json(200, {"ok": True, "seq": ev["seq"]})

        if path == "/api/dev/inject_bridge_health":
            broadcast_sse(body)
            return self._json(200, {"ok": True})

        if self.server.mode not in ("fixture", "owner_fixture"):
            return self._json(405, {"error": "当前为只读模式，不接受写入请求"})

        if path == "/api/login":
            pwd = str(body.get("password") or "")
            if pwd != FIXTURE_STATE["password"]:
                return self._json(401, {"error": "密码不正确（本地联调桩密码为 dev-fixture-pass）"})
            tok = secrets.token_urlsafe(24)
            with STATE_LOCK:
                FIXTURE_STATE["session_tokens"].add(tok)
            cookie = "dafeiyu_session=%s; Path=/; HttpOnly; SameSite=Strict" % tok
            return self._json(200, {"ok": True}, [("Set-Cookie", cookie)])

        if path == "/api/logout":
            tok = parse_cookie(self.headers.get("Cookie", ""), "dafeiyu_session")
            with STATE_LOCK:
                FIXTURE_STATE["session_tokens"].discard(tok)
            return self._json(200, {"ok": True}, [("Set-Cookie", "dafeiyu_session=; Path=/; Max-Age=0")])

        if not self._authed():
            return self._json(401, {"error": "登录状态已失效，请重新登录"})

        if self.server.mode == "owner_fixture" and path == "/api/qzone/refresh":
            if body.get("confirm") is not True or body.get("action") != "qzone.refresh":
                return self._json(400, {"error": "bad confirm"})
            with STATE_LOCK:
                QZONE_FIXTURE["refreshes"] += 1
            return self._json(200, {"ok": True, "data": {"sent": False, "writes": 0}})

        if path == "/api/model":
            plat = str(body.get("platform") or "")
            mid = str(body.get("id") or "")
            if plat not in ("qq", "wechat") or not mid:
                return self._json(400, {"error": "平台或模型参数无效"})
            with STATE_LOCK:
                if plat == "qq":
                    FIXTURE_STATE["qq_model"] = mid
                else:
                    FIXTURE_STATE["wechat_model"] = mid
            return self._json(200, {
                "ok": True,
                "verification": {"state": "confirmed", "observed": mid, "lastRead": int(time.time())}
            })

        if path == "/api/conversation":
            plat = str(body.get("platform") or "")
            cid = str(body.get("id") or "")
            field = str(body.get("field") or "")
            val = body.get("value")
            if field not in ("enabled", "style", "frequency", "limit"):
                return self._json(400, {"error": "不支持的会话字段"})
            with STATE_LOCK:
                for c in FIXTURE_STATE["conversations"]:
                    if c["platform"] == plat and c["id"] == cid:
                        c[field] = val
                        return self._json(200, {"ok": True})
            return self._json(404, {"error": "会话不存在"})

        return self._json(404, {"error": "操作不存在"})


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=18890)
    parser.add_argument("--mode", choices=["readonly", "fixture", "owner_fixture"], default="fixture")
    args = parser.parse_args()
    srv = DevServer((args.host, args.port), DevHandler, args.mode)
    print("DaFeiYu Home R5.2 dev server (%s) at http://%s:%d/fish_house/home.html"
          % (args.mode, args.host, args.port), flush=True)
    try:
        srv.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()


if __name__ == "__main__":
    main()
