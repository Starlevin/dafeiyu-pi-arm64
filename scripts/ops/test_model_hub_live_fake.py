#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""真实 HTTP 层自愈测试：本地假上游返回 400 model_not_available，验证 http() 解析
错误码 -> _invoke 自动查 /models -> 切换同一族模型重试成功；无同族时抛 Fallbackable。

不连接任何真实模型网关，不消耗额度，不写入生产 profiles。
"""
import importlib.util
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import tempfile

HUB_PATH = Path(os.environ.get(
    "HUB_PATH", str(Path.home() / "dafeiyu-test" / "model-hub" / "model-hub.py")))
PORT = 18999
AVAILABLE = ["fake-family-auto", "fake-family-2"]


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
        if body.get("model") == "fake-family-old":
            self._send(400, {"error": {"code": "model_not_available",
                                      "message": "model fake-family-old is not available"}})
            return
        self._send(200, {"choices": [{"message": {"role": "assistant", "content": "pong"}}],
                         "usage": {"total_tokens": 2}})


def load_hub():
    spec = importlib.util.spec_from_file_location("model_hub_live_test", str(HUB_PATH))
    mod = importlib.util.module_from_spec(spec)
    sys.modules["model_hub_live_test"] = mod
    spec.loader.exec_module(mod)
    return mod


def main():
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Upstream)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    hub = load_hub()
    key = Path(tempfile.mkdtemp()) / "fake.key"
    key.write_text("fake")
    body = {"messages": [{"role": "user", "content": "ping"}], "max_tokens": 8}
    try:
        profile = {"id": "fake-web", "model": "fake-family-old",
                   "base_url": "http://127.0.0.1:%d/v1" % PORT, "protocol": "chat",
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
    finally:
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    main()
