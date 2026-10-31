"""Scripted OpenAI-compatible mock server for LLMProviderTests.

Mode is encoded as the first path segment, so one process serves every scenario:

    POST http://127.0.0.1:<port>/<mode>/chat/completions
    GET  http://127.0.0.1:<port>/__stats/<mode>

Modes: ok, unauthorized, forbidden, quota, server_error, flaky, slow
No external network access and no real API key are involved.
"""
import http.server
import json
import threading
import time

STATS = {}
LOCK = threading.Lock()


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def _send(self, code, payload):
        body = json.dumps(payload).encode("utf-8")
        try:
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_GET(self):
        path = self.path.split("?")[0]
        if path.startswith("/__stats/"):
            mode = path[len("/__stats/"):].strip("/")
            with LOCK:
                entry = STATS.get(mode, {"count": 0, "last": None})
            self._send(200, entry)
            return
        if path.endswith("/models"):
            self._send(200, {"data": [{"id": "mock-model"}]})
            return
        self._send(404, {"error": {"message": "not found"}})

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        segments = [item for item in self.path.split("?")[0].split("/") if item]
        mode = segments[0] if segments else "ok"
        try:
            body = json.loads(raw.decode("utf-8") or "{}")
        except ValueError:
            body = {"__unparsed__": raw.decode("utf-8", "replace")}

        with LOCK:
            entry = STATS.setdefault(mode, {"count": 0, "last": None})
            entry["count"] += 1
            entry["last"] = {
                "path": self.path,
                "authorization": self.headers.get("Authorization"),
                "body": body,
                "count": entry["count"],
            }
            count = entry["count"]

        if mode == "unauthorized":
            self._send(401, {"error": {"message": "invalid api key"}})
            return
        if mode == "forbidden":
            self._send(403, {"error": {"message": "forbidden"}})
            return
        if mode == "quota":
            self._send(429, {"error": {"message": "quota exhausted"}})
            return
        if mode == "server_error":
            self._send(503, {"error": {"message": "service unavailable"}})
            return
        if mode == "flaky" and count == 1:
            self._send(500, {"error": {"message": "transient"}})
            return
        if mode == "slow":
            time.sleep(3)
        self._send(200, {
            "choices": [{"message": {"content": "mock reply"}, "finish_reason": "stop"}],
            "usage": {"total_tokens": 7},
        })


server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
print(server.server_port, flush=True)
server.serve_forever()
