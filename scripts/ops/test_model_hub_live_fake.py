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
QQ_ORDINARY_REQUESTS = []
MALFORMED_RESPONSES = {
    "fixture-null": None,
    "fixture-choices-object": {"choices": {"shape": None}},
    "fixture-choice-null": {"choices": [None]},
    "fixture-trailing-choice-null": {"choices": [{"message": {"content": "valid first choice"}}, None]},
    "fixture-message-array": {"choices": [{"message": ["bad message"]}]},
    "fixture-nontext": {"choices": [{"message": {"content": 123}}]},
    "fixture-invalid-tool-list": {"choices": [{"message": {"tool_calls": {"unexpected": True}}}]},
}
FILTERED_RESPONSE = {"choices": [{"finish_reason": "content_filter", "message": ["ignored"]}]}
TOOL_RESPONSE = {"choices": [{"finish_reason": "tool_calls", "message": {
    "content": None, "tool_calls": [{"id": "fixture-tool-call", "type": "function",
                                    "function": {"name": "lookup", "arguments": "{}"}}]}}]}


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
        if body.get("model") in MALFORMED_RESPONSES:
            self._send(200, MALFORMED_RESPONSES[body["model"]])
            return
        if body.get("model") == "fixture-tool":
            self._send(200, TOOL_RESPONSE)
            return
        if body.get("model") == "fixture-filtered":
            self._send(200, FILTERED_RESPONSE)
            return
        if body.get("model") in ("fixture-qq-slow", "fixture-qq-backup"):
            QQ_ORDINARY_REQUESTS.append((body["model"], body.get("max_tokens"),
                                         body.get("reasoning_effort")))
            if body["model"] == "fixture-qq-slow":
                time.sleep(1.5)
            try:
                content = json.dumps({"text": "海边的风很舒服。", "emotion": "neutral", "intensity": 0})
                self._send(200, {"choices": [{"message": {"content": content}}]})
            except (BrokenPipeError, ConnectionResetError):
                pass
            return
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

        hub.clear_cooldown(selected["id"])
        selected.update(model="fixture-qq-slow", reasoning="high")
        fallback["model"] = "fixture-qq-backup"
        hub.GROUP_CHAT_PRIMARY_SECONDS = .25
        hub.GROUP_CHAT_ROUTE_SECONDS = .95
        ordinary = ('针对当前群聊内容自然接一句，简短贴题，不复述，不把群友的经历或邀请说成自己的。'
                    '只输出 text、emotion、intensity 三字段的 JSON。\n只输出最终 JSON，不展示推理过程。')
        request = {"model": selected["id"], "max_tokens": 500,
                   "response_format": {"type": "json_object"},
                   "messages": [{"role": "system", "content": ordinary},
                                {"role": "user", "content": "晚上海边散步挺舒服的"}]}
        started = time.monotonic()
        result = hub.complete(request)
        elapsed = time.monotonic() - started
        assert .14 <= elapsed < .95, elapsed
        assert QQ_ORDINARY_REQUESTS == [("fixture-qq-slow", 160, "none"),
                                       ("fixture-qq-backup", 160, None)], QQ_ORDINARY_REQUESTS
        assert json.loads(result["choices"][0]["message"]["content"])["text"] == "海边的风很舒服。"
        assert hub._test_routes[-1]["reason"] == "group_primary_timeout"
        assert hub._test_routes[-1]["actual"] == fallback["id"]
        assert hub.cooldown_until(selected["id"]) == 0
        print("QQ_ORDINARY_SLOW_WORKBUDDY_HTTP_FALLBACK_PASS elapsed_ms=%d" % round(elapsed * 1000))

        for bad_model in MALFORMED_RESPONSES:
            selected["model"] = bad_model
            hub.clear_cooldown(selected["id"])
            POSTED_MODELS.clear()
            count = len(hub._test_routes)
            reply = hub.complete(request)
            assert json.loads(reply["choices"][0]["message"]["content"])["text"] == "海边的风很舒服。"
            assert POSTED_MODELS == [bad_model, "fixture-qq-backup"]
            assert len(hub._test_routes) == count + 1
            assert hub._test_routes[-1]["reason"] == "empty_reply"
        print("MALFORMED_PRIMARY_HTTP_FALLBACK_PASS shape_count=%d" % len(MALFORMED_RESPONSES))

        selected["model"] = "fixture-null"
        fallback["model"] = "fixture-choice-null"
        hub.clear_cooldown(selected["id"])
        try:
            hub.complete(request)
        except hub.UpstreamError as error:
            assert error.status == 503
        else:
            raise AssertionError("Two malformed replies must return the controlled upstream error")
        assert hub._test_routes[-1]["reason"] == "backup_empty"
        print("MALFORMED_BACKUP_HTTP_CONTROLLED_FAILURE_PASS")

        selected["model"] = "fixture-filtered"
        hub.clear_cooldown(selected["id"])
        POSTED_MODELS.clear()
        assert hub.complete(request) == FILTERED_RESPONSE
        assert POSTED_MODELS == ["fixture-filtered"]
        assert hub.cooldown_until(selected["id"]) == 0
        print("FILTERED_HTTP_RESULT_NO_FALLBACK_PASS")

        selected["model"] = "fixture-tool"
        POSTED_MODELS.clear()
        assert hub.complete(request) == TOOL_RESPONSE
        assert POSTED_MODELS == ["fixture-tool"]
        print("TOOL_ONLY_HTTP_RESULT_NO_FALLBACK_PASS")
    finally:
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    main()
