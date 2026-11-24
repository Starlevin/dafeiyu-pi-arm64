#!/usr/bin/env python3
"""pi_xinput.py — Pi 上向 X11 注入输入的极简实现（ctypes + libXtst）

为什么不用 python3-xlib：Pi 上没装（apt 有但需要 sudo 密码）。
libXtst.so.6 系统自带，ctypes 直接调即可。

中文输入：XTest 只能产生 Latin keycode，中文必须走剪贴板。
Pi 上有 xsel，用它把文本放进 CLIPBOARD，再 Ctrl+V 粘贴。

所有函数都不假设窗口位置，坐标由调用方从 AT-SPI extents 取。
"""
from __future__ import annotations

import ctypes as c
import os
import subprocess
import time

_libX11 = c.CDLL("libX11.so.6")
_libXtst = c.CDLL("libXtst.so.6")

_dpy = None


def _display():
    global _dpy
    if _dpy is None:
        _libX11.XOpenDisplay.restype = c.c_void_p
        _libX11.XOpenDisplay.argtypes = [c.c_char_p]
        _dpy = _libX11.XOpenDisplay(None)
        if not _dpy:
            raise RuntimeError("x_display_unavailable")
    return _dpy


def _sync():
    _libX11.XFlush.argtypes = [c.c_void_p]
    _libX11.XFlush(_display())


def screen_size():
    _libX11.XDefaultScreen.restype = c.c_int
    _libX11.XDefaultScreen.argtypes = [c.c_void_p]
    scr = _libX11.XDefaultScreen(_display())
    _libX11.XDisplayWidth.restype = c.c_int
    _libX11.XDisplayWidth.argtypes = [c.c_void_p, c.c_int]
    _libX11.XDisplayHeight.restype = c.c_int
    _libX11.XDisplayHeight.argtypes = [c.c_void_p, c.c_int]
    return (_libX11.XDisplayWidth(_display(), scr),
            _libX11.XDisplayHeight(_display(), scr))


def pointer():
    """返回 (x, y, child_window_id)。"""
    root = c.c_ulong()
    child = c.c_ulong()
    rx = c.c_int()
    ry = c.c_int()
    wx = c.c_int()
    wy = c.c_int()
    mask = c.c_uint()
    _libX11.XDefaultRootWindow.restype = c.c_ulong
    _libX11.XDefaultRootWindow.argtypes = [c.c_void_p]
    rwin = _libX11.XDefaultRootWindow(_display())
    _libX11.XQueryPointer.restype = c.c_int
    _libX11.XQueryPointer.argtypes = [
        c.c_void_p, c.c_ulong, c.POINTER(c.c_ulong), c.POINTER(c.c_ulong),
        c.POINTER(c.c_int), c.POINTER(c.c_int), c.POINTER(c.c_int),
        c.POINTER(c.c_int), c.POINTER(c.c_uint)]
    _libX11.XQueryPointer(_display(), rwin, c.byref(root), c.byref(child),
                          c.byref(rx), c.byref(ry), c.byref(wx), c.byref(wy),
                          c.byref(mask))
    return (rx.value, ry.value, child.value)


def move(x: int, y: int, settle: float = 0.15):
    _libXtst.XTestFakeMotionEvent.argtypes = [
        c.c_void_p, c.c_int, c.c_int, c.c_int, c.c_ulong]
    _libXtst.XTestFakeMotionEvent(_display(), -1, int(x), int(y), 0)
    _sync()
    time.sleep(settle)


def click(x: int, y: int, button: int = 1, settle: float = 0.4, *,
          move_delay: float = 0.18, hold_delay: float = 0.12):
    move(x, y, move_delay)
    _libXtst.XTestFakeButtonEvent.argtypes = [c.c_void_p, c.c_uint, c.c_int, c.c_ulong]
    _libXtst.XTestFakeButtonEvent(_display(), button, 1, 0)
    _sync()
    time.sleep(hold_delay)
    _libXtst.XTestFakeButtonEvent(_display(), button, 0, 0)
    _sync()
    time.sleep(settle)


def _keysym(name: str) -> int:
    _libX11.XStringToKeysym.restype = c.c_ulong
    _libX11.XStringToKeysym.argtypes = [c.c_char_p]
    return _libX11.XStringToKeysym(name.encode())


def _keycode(keysym: int) -> int:
    _libX11.XKeysymToKeycode.restype = c.c_ubyte
    _libX11.XKeysymToKeycode.argtypes = [c.c_void_p, c.c_ulong]
    return _libX11.XKeysymToKeycode(_display(), keysym)


def _key_event(code: int, press: bool):
    _libXtst.XTestFakeKeyEvent.argtypes = [c.c_void_p, c.c_uint, c.c_int, c.c_ulong]
    _libXtst.XTestFakeKeyEvent(_display(), code, 1 if press else 0, 0)


def key(name: str, mods=(), settle: float = 0.08):
    """按一个键，可带修饰键。mods 例如 ('Control_L',)。"""
    mod_codes = [_keycode(_keysym(m)) for m in mods]
    code = _keycode(_keysym(name))
    for mc in mod_codes:
        _key_event(mc, True)
    _key_event(code, True)
    _key_event(code, False)
    for mc in reversed(mod_codes):
        _key_event(mc, False)
    _sync()
    time.sleep(settle)


def type_text(text: str, settle: float = 0.9) -> bool:
    """通过剪贴板粘贴中文/任意文本。返回是否成功设置剪贴板。"""
    try:
        p = subprocess.run(
            ["xsel", "--clipboard", "--input"],
            input=text.encode(), capture_output=True, timeout=10)
        ok = p.returncode == 0
    except Exception:
        ok = False
    if not ok:
        return False
    time.sleep(0.35)
    key("v", ("Control_L",), settle)
    time.sleep(settle)
    return True


def paste_verify(text: str) -> bool:
    """把文本放进剪贴板（不粘贴），供调用方自行 Ctrl+V。"""
    p = subprocess.run(["xsel", "--clipboard", "--input"],
                       input=text.encode(), capture_output=True, timeout=10)
    return p.returncode == 0
