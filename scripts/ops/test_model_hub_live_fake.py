#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""真实 HTTP 层自愈测试：本地假上游返回 400 model_not_available，验证 http() 解析
错误码 -> _invoke 自动查 /models -> 切换同一族模型重试成功；无同族时抛 Fallbackable。

不连接任何真实模型网关，不消耗额度，不写入生产 profiles。
"""
import importlib.util
import json
import os
import socket
import types
import urllib.error
import urllib.request
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import tempfile

HUB_PATH = Path(os.environ.get(
    "HUB_PATH", str(Path(__file__).resolve().with_name("model-hub.py"))))
AVAILABLE = ["fake-family-auto", "fake-family-2"]
POSTED_MODELS = []


class Upstream(BaseHTTPRequestHandler):
    def log_message(self, *args):
        return

    def _send(self, code, payload):
        data = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path.endswith("/models"):
            self._send(200, {"data": [{"id": m} for m in AVAILABLE]})
        else:
            self._send(404, {"error": {"code": "not_found"}})

    def do_POST(self):
        length = int(self.headers.get("content-length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        POSTED_MODELS.append(body.get("model"))
        if body.get("model") == "fixture-workbuddy-unavailable":
            self._send(503, {"error": {"code": "fixture_unavailable"}})
            return
        if body.get("model") == "fixture-sensenova-stalled":
            time.sleep(1.5)
            try:
                self._send(200, {"choices": [{"message": {"content": "late"}}]})
            except (BrokenPipeError, ConnectionResetError):
                pass
            return
        if body.get("model") == "fake-family-old":
            self._send(400, {"error": {"code": "model_not_available",
                                      "message": "model fake-family-old is not available"}})
            return
        self._send(200, {"choices": [{"message": {"role": "assistant", "content": "pong"}}],
                         "usage": {"total_tokens": 2}})


def load_hub():
    # Keep this integration test dependency-free and loopback-only. The adapter
    # below implements the tiny requests surface model-hub.http uses.
    if 'jwt' not in sys.modules:
        jwt = types.ModuleType('jwt')
        jwt.encode = lambda *a, **k: ''
        jwt.decode = lambda *a, **k: {}
        sys.modules['jwt'] = jwt
    if 'requests' not in sys.modules:
        requests = types.ModuleType('requests')
        class Timeout(Exception): pass
        class ConnectionError(Exception): pass
        class Response:
            def __init__(self, status, content): self.status_code, self.content = status, content
            def iter_content(self, size):
                for start in range(0, len(self.content), size): yield self.content[start:start + size]
            def close(self): pass
        class Session:
            trust_env = False
            proxies = {}
            def request(self, method, url, headers=None, json=None, data=None, timeout=None,
                        allow_redirects=False, stream=True):
                if not url.startswith('http://127.0.0.1:'):
                    raise AssertionError('live fake may only call loopback')
                encoded = __import__('json').dumps(json).encode() if json is not None else data
                request = urllib.request.Request(url, data=encoded, headers=headers or {}, method=method)
                try:
                    with urllib.request.urlopen(request, timeout=sum(timeout or (5, 5))) as response:
                        return Response(response.status, response.read())
                except urllib.error.HTTPError as error:
                    return Response(error.code, error.read())
                except socket.timeout as error:
                    raise Timeout() from error
                except urllib.error.URLError as error:
                    if isinstance(error.reason, socket.timeout): raise Timeout() from error
                    raise ConnectionError() from error
            def close(self): pass
        requests.Session = Session
        requests.exceptions = types.SimpleNamespace(Timeout=Timeout, ConnectionError=ConnectionError)
        sys.modules['requests'] = requests
    spec = importlib.util.spec_from_file_location("model_hub_live_test", str(HUB_PATH))
    mod = importlib.util.module_from_spec(spec)
    sys.modules["model_hub_live_test"] = mod
    spec.loader.exec_module(mod)
    return mod


def main():
    server = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
    port = server.server_port
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    hub = load_hub()
    hub._USAGE = None
    key = Path(tempfile.mkdtemp()) / "fake.key"
    key.write_text("fake")
    body = {"messages": [{"role": "user", "content": "ping"}], "max_tokens": 8}
    try:
        profile = {"id": "fake-web", "model": "fake-family-old",
                   "base_url": "http://127.0.0.1:%d/v1" % port, "protocol": "chat",
                   "key_file": str(key), "proxy": False}
        result = hub._invoke(profile, body)
        assert result["choices"][0]["message"]["content"] == "pong"
        assert hub.MODEL_OVERRIDES["fake-web"] == "fake-family-auto"
        print("AUTO_MODEL_HTTP_PASS override=%s" % hub.MODEL_OVERRIDES["fake-web"])

        # 无同族可选：只剩无关模型 -> Fallbackable
        AVAILABLE[:] = ["other-vendor-model"]
        hub.MODEL_OVERRIDES.clear()
        hub.MODEL_CACHE.clear()
        profile2 = dict(profile, id="fake-web-2")
        try:
            hub._invoke(profile2, body)
            raise AssertionError("expected Fallbackable")
        except hub.Fallbackable as exc:
            assert exc.reason == "model_not_available"
        print("NO_ALTERNATIVE_FALLBACK_PASS")

        scratch = key.parent
        hub.COOLDOWN_FILE = scratch / "cooldown.json"
        hub._test_routes = []
        hub.route_log = hub._test_routes.append
        selected = dict(profile, id="workbuddy-deepseek", model="fixture-workbuddy-unavailable", name="WorkBuddy fixture",
                        fallback_to="sensenova-6.8-flash-lite", key_file=str(key))
        fallback = dict(profile, id="sensenova-6.8-flash-lite", model="fixture-sensenova-stalled", name="SenseNova fixture",
                        fallback_to=None)
        hub.GATEWAY_URL = selected["base_url"]
        hub.WORKBUDDY_BACKUP_SECONDS = 0.4
        hub.WORKBUDDY_STATUS_FILE = scratch / "nonexistent-workbuddy-status.json"
        hub.profiles = lambda: [selected, fallback]
        POSTED_MODELS.clear()
        started = time.monotonic()
        try:
            hub.complete({"model": selected["id"], "messages": [{"role": "user", "content": "fixture"}]})
        except hub.UpstreamError as error:
            assert error.status == 503
        else:
            raise AssertionError("The backup must time out before returning its late response")
        elapsed = time.monotonic() - started
        # The HTTP client may return before the full budget if the read phase times out.
        assert 0.2 <= elapsed < 1.25, elapsed
        assert POSTED_MODELS == ["fixture-workbuddy-unavailable", "fixture-sensenova-stalled"]
        assert hub._test_routes[-1]["reason"] == "backup_timeout"
        assert hub._test_routes[-1]["actual"] == ""
        print("WORKBUDDY_BACKUP_DEADLINE_HTTP_PASS elapsed_ms=%d" % round(elapsed * 1000))
    finally:
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    main()
