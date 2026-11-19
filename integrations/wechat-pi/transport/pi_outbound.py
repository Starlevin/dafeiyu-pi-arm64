#!/usr/bin/env python3
"""pi_outbound.py — 接收 core 主动推送（care / moments），写进微信输入框

为什么需要它：
  core 的 gateway.js 会把回复 POST 到 gateway.outboundUrl；
  该地址为空时，主动消息只能进日志（「这条回复不会到微信」）。
  本模块在 127.0.0.1:19302 起一个接收端，把内容交给桥接的发送链路。

安全闸：
  * 只接受当前已打开的目标群/会话（chat_id 必须匹配），否则丢弃
  * 每小时最多 MAX_PER_HOUR 条，防止风控（账号已有「被限制与对方聊天」提示）
  * 多气泡逐条发，间隔 1.5s
"""
from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable, Optional

MAX_PER_HOUR = 3
GAP_SEC = 1.5


class RateLimiter:
    """滑动窗口限流。"""

    def __init__(self, limit=MAX_PER_HOUR, window=3600):
        self.limit = limit
        self.window = window
        self.hits: list[float] = []
        self._lock = threading.Lock()

    def allow(self) -> bool:
        now = time.time()
        with self._lock:
            self.hits = [t for t in self.hits if now - t < self.window]
            if len(self.hits) >= self.limit:
                return False
            self.hits.append(now)
            return True

    def remaining(self) -> int:
        now = time.time()
        with self._lock:
            self.hits = [t for t in self.hits if now - t < self.window]
            return max(0, self.limit - len(self.hits))


class _Handler(BaseHTTPRequestHandler):
    server_version = "pi-outbound/1"

    def log_message(self, fmt, *args):  # 静音，避免污染 bridge.log
        pass

    def do_POST(self):
        srv = self.server
        if self.path.rstrip("/") != "/outbound":
            self._reply(404, {"ok": False, "error": "not_found"})
            return
        try:
            n = int(self.headers.get("content-length") or 0)
            if n <= 0 or n > 256 * 1024:
                self._reply(400, {"ok": False, "error": "bad_length"})
                return
            body = json.loads(self.rfile.read(n).decode("utf-8", "replace"))
        except Exception as exc:
            self._reply(400, {"ok": False, "error": "bad_json", "detail": repr(exc)[:120]})
            return

        chat_id = str(body.get("chat_id") or body.get("room_id") or "").strip()
        bubbles = body.get("bubbles")
        if not isinstance(bubbles, list) or not bubbles:
            text = str(body.get("text") or "").strip()
            bubbles = [text] if text else []
        bubbles = [str(b).strip() for b in bubbles if str(b).strip()]

        if not bubbles:
            self._reply(400, {"ok": False, "error": "empty_text"})
            return

        if not srv.is_target_open(chat_id):
            srv.log(f"  [outbound] 目标会话未打开，丢弃 chat_id={chat_id!r}")
            self._reply(409, {"ok": False, "error": "target_not_open"})
            return

        if not srv.limiter.allow():
            srv.log(f"  [outbound] 超出每小时 {MAX_PER_HOUR} 条上限，丢弃")
            self._reply(429, {"ok": False, "error": "rate_limited"})
            return

        sent = 0
        for i, b in enumerate(bubbles):
            ok = srv.send_fn(b)
            srv.log(f"  [outbound] 第 {i + 1}/{len(bubbles)} 条 -> {'OK' if ok else 'FAIL'}: {b[:50]!r}")
            if ok:
                sent += 1
            if i < len(bubbles) - 1:
                time.sleep(GAP_SEC)
        self._reply(200, {"ok": sent > 0, "sent": sent, "total": len(bubbles)})

    def _reply(self, code: int, obj: dict):
        raw = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("content-type", "application/json; charset=utf-8")
        self.send_header("content-length", str(len(raw)))
        self.end_headers()
        try:
            self.wfile.write(raw)
        except Exception:
            pass


def start_server(send_fn: Callable[[str], bool],
                 is_target_open: Callable[[str], bool],
                 log: Callable[[str], None],
                 host: str = "127.0.0.1",
                 port: int = 19302) -> Optional[ThreadingHTTPServer]:
    """在后台线程起接收端，返回 server（失败返回 None，不阻塞主流程）。"""
    try:
        httpd = ThreadingHTTPServer((host, port), _Handler)
    except OSError as exc:
        log(f"  [outbound] 端口 {port} 绑定失败：{exc!r}（主动消息将不可用）")
        return None
    httpd.send_fn = send_fn
    httpd.is_target_open = is_target_open
    httpd.limiter = RateLimiter()
    httpd.log = log
    t = threading.Thread(target=httpd.serve_forever, name="outbound", daemon=True)
    t.start()
    log(f"  [outbound] 监听 http://{host}:{port}/outbound  限流={MAX_PER_HOUR}/小时")
    return httpd
