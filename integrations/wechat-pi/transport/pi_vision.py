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
import ctypes as c
import hashlib
import json
import struct
import urllib.error
import urllib.request
import zlib

CORE = "http://127.0.0.1:19301"


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


def crop(x: int, y: int, w: int, h: int) -> bytes | None:
    """从根窗口截取矩形，返回 PNG 字节。越界会被裁到屏幕内。"""
    L = _lib()
    d = L["od"](None)
    if not d:
        return None
    root = L["rr"](d)
    r_ret, x_ret, y_ret = c.c_ulong(), c.c_int(), c.c_int()
    sw, sh, bw, dp = c.c_uint(), c.c_uint(), c.c_uint(), c.c_uint()
    L["geo"](d, root, c.byref(r_ret), c.byref(x_ret), c.byref(y_ret),
             c.byref(sw), c.byref(sh), c.byref(bw), c.byref(dp))
    SW, SH = sw.value, sh.value
    x = max(0, min(x, SW - 1))
    y = max(0, min(y, SH - 1))
    w = max(1, min(w, SW - x))
    h = max(1, min(h, SH - y))
    img = L["gi"](d, root, x, y, w, h, c.c_ulong(0xFFFFFFFF), 2)
    try:
        if not img:
            return None
        rows = []
        gp = L["gp"]
        for yy in range(h):
            row = bytearray([0])
            for xx in range(w):
                px = gp(img, xx, yy) & 0xFFFFFF
                row += bytes(((px >> 16) & 0xFF, (px >> 8) & 0xFF, px & 0xFF))
            rows.append(bytes(row))
        return _png(w, h, rows)
    finally:
        if img:
            L["di"](img)
        L["cd"](d)


def sample_avatar(x, y, w, h):
    """Use pixel variation to distinguish an avatar from the flat chat background."""
    data = crop(x, y, w, h)
    if not data:
        return None
    width, height = struct.unpack_from(">II", data, 16)
    if (width, height) != (w, h):
        return None
    rows = zlib.decompress(data[41:-16])
    pixels = {
        rows[row * (3 * w + 1) + i:row * (3 * w + 1) + i + 3]
        for row in range(h)
        for i in range(1, 3 * w + 1, 3)
    }
    return len(pixels) >= 16, hashlib.sha256(rows).digest()


def describe(png: bytes, prompt: str, timeout: int = 120) -> str | None:
    """把 PNG 交给 core 的 /api/vision，返回文字描述。"""
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


def describe_rect(x, y, w, h, prompt, timeout=120) -> str | None:
    png = crop(x, y, w, h)
    if not png:
        return None
    return describe(png, prompt, timeout)


if __name__ == "__main__":
    import sys
    if len(sys.argv) >= 5:
        x, y, w, h = (int(v) for v in sys.argv[1:5])
        pr = sys.argv[5] if len(sys.argv) > 5 else "这张图里有什么？简洁描述"
        png = crop(x, y, w, h)
        print(f"crop {w}x{h} -> {len(png) if png else 0} bytes")
        if png:
            print("描述:", describe(png, pr))
