#!/usr/bin/env python3
"""pi_vision.py — 屏幕裁剪 + 识图（纯视觉链路，不碰数据库/密钥）

设计：
  * crop(x,y,w,h)  从 :99 根窗口截取指定矩形，返回 PNG bytes
  * describe(png, prompt)  POST core 的 /api/vision，返回模型描述
  * describe_rect()  两者串联，给桥接直接用

为什么走截图而不是读 .dat：
  微信图片落盘是 07085632 头的加密格式，解密等于碰微信数据；
  而 UI 上图片本来就是渲染出来的，截图即可，零风险。
"""
from __future__ import annotations

import base64
from contextlib import contextmanager
import ctypes as c
import hashlib
import json
import os
from pathlib import Path
import re
import struct
import urllib.request
import zlib

CORE = "http://127.0.0.1:19301"
MODEL_HUB = "http://127.0.0.1:18090/v1/chat/completions"
MODEL_HUB_MAX_RESPONSE = 1024 * 1024
MODEL_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
TRACE_ID_RE = re.compile(
    r"^(?:[0-9a-f]{64}|[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12})$", re.I)


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Keep 30x responses local so credentials never follow a Location header."""
    @staticmethod
    def _return_response(_req, response, _code, _msg, _headers):
        return response

    http_error_301 = http_error_302 = http_error_303 = _return_response
    http_error_307 = http_error_308 = _return_response


def _model_hub_config():
    """Resolve the configured local vision route; never infer a text model."""
    model = os.environ.get("DAFEIYU_WECHAT_VISION_HUB_MODEL", "").strip()
    if not MODEL_NAME_RE.fullmatch(model):
        return None
    key_path = (os.environ.get("DAFEIYU_WECHAT_VISION_HUB_KEY_FILE", "").strip()
                or os.environ.get("DAFEIYU_WECHAT_JUDGE_KEY_FILE", "").strip())
    path = Path(key_path).expanduser() if key_path else (
        Path.home() / "dafeiyu-test" / "secrets" / "model-hub" / "gateway.key")
    try:
        key = path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError):
        return None
    return {"model": model, "key": key} if key else None


def _describe_model_hub(png: bytes, prompt: str, timeout: int,
                        trace_id: str | None = None) -> str | None:
    config = _model_hub_config()
    if config is None:
        return None
    body = {
        "model": config["model"],
        "messages": [{"role": "user", "content": [
            {"type": "image_url", "image_url": {
                "url": "data:image/png;base64," + base64.b64encode(png).decode("ascii")}},
            {"type": "text", "text": prompt},
        ]}],
        "temperature": 0.1,
        "max_tokens": 1000,
        "stream": False,
    }
    headers = {"content-type": "application/json",
               "authorization": "Bearer " + config["key"]}
    # This gateway records the selected/fallback profile only when a recognized
    # trace id is supplied. Never forward arbitrary values in this correlation header.
    if isinstance(trace_id, str) and TRACE_ID_RE.fullmatch(trace_id):
        headers["X-Dafeiyu-Trace-Id"] = trace_id
    req = urllib.request.Request(
        MODEL_HUB,
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        method="POST",
        headers=headers,
    )
    try:
        # Bypass ambient HTTP proxies: this adapter has one fixed IPv4 loopback destination.
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}), _NoRedirectHandler())
        with opener.open(req, timeout=max(1, min(int(timeout), 120))) as response:
            if response.status != 200:
                return None
            raw = response.read(MODEL_HUB_MAX_RESPONSE + 1)
        if len(raw) > MODEL_HUB_MAX_RESPONSE:
            return None
        result = json.loads(raw.decode("utf-8"))
        choices = result.get("choices") if isinstance(result, dict) else None
        if not isinstance(choices, list) or len(choices) != 1:
            return None
        choice = choices[0]
        if not isinstance(choice, dict) or choice.get("finish_reason") not in (None, "stop"):
            return None
        message = choice.get("message")
        text = message.get("content") if isinstance(message, dict) else None
        return text.strip() if isinstance(text, str) and text.strip() else None
    except Exception:
        return None


def _lib():
    x11 = c.CDLL("libX11.so.6")

    def f(n, r, *a):
        fn = getattr(x11, n)
        fn.restype = r
        fn.argtypes = a
        return fn

    return {
        "od": f("XOpenDisplay", c.c_void_p, c.c_char_p),
        "cd": f("XCloseDisplay", c.c_int, c.c_void_p),
        "rr": f("XDefaultRootWindow", c.c_ulong, c.c_void_p),
        "gi": f("XGetImage", c.c_void_p, c.c_void_p, c.c_ulong, c.c_int, c.c_int,
                c.c_uint, c.c_uint, c.c_ulong, c.c_int),
        "gp": f("XGetPixel", c.c_ulong, c.c_void_p, c.c_int, c.c_int),
        "di": f("XDestroyImage", c.c_int, c.c_void_p),
        "geo": f("XGetGeometry", c.c_int, c.c_void_p, c.c_ulong, c.POINTER(c.c_ulong),
                 c.POINTER(c.c_int), c.POINTER(c.c_int), *(c.POINTER(c.c_uint),) * 4),
    }


def _png(W: int, H: int, rows: list[bytes]) -> bytes:
    raw = b"".join(rows)

    def chunk(tag, data):
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    out = b"\x89PNG\r\n\x1a\n"
    out += chunk(b"IHDR", struct.pack(">IIBBBBB", W, H, 8, 2, 0, 0, 0))
    out += chunk(b"IDAT", zlib.compress(raw, 6))
    out += chunk(b"IEND", b"")
    return out


@contextmanager
def _screen():
    L = _lib()
    d = L["od"](None)
    if not d:
        yield None
        return
    try:
        root = L["rr"](d)
        r_ret, x_ret, y_ret = c.c_ulong(), c.c_int(), c.c_int()
        sw, sh, bw, dp = c.c_uint(), c.c_uint(), c.c_uint(), c.c_uint()
        found = L["geo"](d, root, c.byref(r_ret), c.byref(x_ret), c.byref(y_ret),
                         c.byref(sw), c.byref(sh), c.byref(bw), c.byref(dp))
        yield (L, d, root, sw.value, sh.value) if found and sw.value and sh.value else None
    finally:
        L["cd"](d)


def _capture_rows(screen, x, y, w, h):
    L, d, root, _, _ = screen
    img = L["gi"](d, root, x, y, w, h, c.c_ulong(0xFFFFFFFF), 2)
    if not img:
        return None
    try:
        rows = []
        gp = L["gp"]
        for yy in range(h):
            row = bytearray([0])
            for xx in range(w):
                px = gp(img, xx, yy) & 0xFFFFFF
                row += bytes(((px >> 16) & 0xFF, (px >> 8) & 0xFF, px & 0xFF))
            rows.append(bytes(row))
        return rows
    finally:
        L["di"](img)


def crop(x: int, y: int, w: int, h: int) -> bytes | None:
    """从根窗口截取矩形，返回 PNG 字节。越界会被裁到屏幕内。"""
    with _screen() as screen:
        if screen is None:
            return None
        _, _, _, sw, sh = screen
        x = max(0, min(x, sw - 1))
        y = max(0, min(y, sh - 1))
        w = max(1, min(w, sw - x))
        h = max(1, min(h, sh - y))
        rows = _capture_rows(screen, x, y, w, h)
    return _png(w, h, rows) if rows else None


def _avatar_from_rows(rows):
    if not rows:
        return None
    pixels = {
        row[i:i + 3]
        for row in rows
        for i in range(1, len(row), 3)
    }
    return len(pixels) >= 16, hashlib.sha256(b"".join(rows)).digest()


def sample_avatars(*rects):
    """Measure small avatar strips locally on one X11 connection."""
    with _screen() as screen:
        if screen is None:
            return (None,) * len(rects)
        _, _, _, sw, sh = screen
        samples = []
        for x, y, w, h in rects:
            if x < 0 or y < 0 or w <= 0 or h <= 0 or x + w > sw or y + h > sh:
                samples.append(None)
                continue
            samples.append(_avatar_from_rows(_capture_rows(screen, x, y, w, h)))
        return tuple(samples)


def describe(png: bytes, prompt: str, timeout: int = 120,
             trace_id: str | None = None) -> str | None:
    """把 PNG 交给当前显式配置的视觉路线；默认兼容 core `/api/vision`。"""
    backend = os.environ.get("DAFEIYU_WECHAT_VISION_BACKEND", "").strip().lower()
    if backend == "model-hub":
        return _describe_model_hub(png, prompt, timeout, trace_id)
    if backend:
        # A typo or unsupported backend is a configuration failure, not permission
        # to send the image through a different upstream.
        return None
    body = {"image": base64.b64encode(png).decode(),
            "mime": "image/png",
            "prompt": prompt}
    req = urllib.request.Request(
        CORE + "/api/vision",
        data=json.dumps(body, ensure_ascii=False).encode(),
        method="POST", headers={"content-type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            d = json.loads(r.read().decode())
        if d.get("ok"):
            return (d.get("text") or "").strip()
        return None
    except Exception:
        return None


def describe_rect(x, y, w, h, prompt, timeout=120, trace_id=None) -> str | None:
    png = crop(x, y, w, h)
    if not png:
        return None
    return describe(png, prompt, timeout, trace_id)


if __name__ == "__main__":
    import sys
    if len(sys.argv) >= 5:
        x, y, w, h = (int(v) for v in sys.argv[1:5])
        pr = sys.argv[5] if len(sys.argv) > 5 else "这张图里有什么？简洁描述"
        png = crop(x, y, w, h)
        print(f"crop {w}x{h} -> {len(png) if png else 0} bytes")
        if png:
            print("描述:", describe(png, pr))
