#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""只读微信窗口探针（AT-SPI）。

用途：区分「已登录主窗口 880×640」与「登录/扫码窗口 280×380」，并在登录窗口
内查找 "Log In" 按钮或 "Scan to log in" 提示。绝不读取聊天正文、账号、Cookie。

输出：单行 JSON（stdout）。
退出码：0 表示探测完成（ok 字段说明结果），2 表示 AT-SPI 不可用。
"""
from __future__ import annotations

import json
import os
import sys

BASE = os.path.expanduser("~/dafeiyu-wechat")
LOG_IN_NAMES = {"log in", "login", "sign in", "登录", "登入", "进入微信"}
SCAN_HINTS = (
    "scan to log in", "scan to login", "scan qr", "scan the qr",
    "扫码登录", "扫描二维码", "扫一扫登录",
)
MAX_DEPTH = 9
MAX_NODES = 260


def child_env() -> dict:
    runtime = os.path.realpath(os.path.join(BASE, "client-session", "runtime"))
    env = dict(os.environ)
    env["GI_TYPELIB_PATH"] = os.path.join(
        BASE, "atspi-typelib/usr/lib/aarch64-linux-gnu/girepository-1.0")
    env["DISPLAY"] = ":99"
    env["XAUTHORITY"] = os.path.join(runtime, "Xauthority")
    env["AT_SPI_BUS_ADDRESS"] = "unix:path=%s/at-spi/bus_99" % runtime
    return env


def norm(text: str) -> str:
    return " ".join((text or "").split()).lower()


def emit(payload: dict) -> int:
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return 0 if payload.get("ok") else 2


def probe(click_login: bool) -> dict:
    try:
        import gi
        gi.require_version("Atspi", "2.0")
        from gi.repository import Atspi
    except Exception:
        return {"ok": False, "error": "gi_import_failed"}

    desktop = Atspi.get_desktop(0)
    apps = []
    for i in range(desktop.get_child_count()):
        app = desktop.get_child_at_index(i)
        name = (app.get_name() or "") if app else ""
        low = name.lower()
        if "wechat" not in low and "weixin" not in low:
            continue
        apps.append((name, app))

    if not apps:
        return {"ok": False, "error": "no_wechat_app"}

    windows = []
    login = {"log_in_button": False, "scan_hint": False,
             "button_name": None, "clicked": False}
    scanned = 0

    for app_name, app in apps[:1]:
        for j in range(app.get_child_count()):
            window = app.get_child_at_index(j)
            if not window:
                continue
            ext = window.get_extents(Atspi.CoordType.SCREEN)
            windows.append({"name": window.get_name() or "",
                            "w": int(ext.width), "h": int(ext.height)})
            if ext.width > 520 or ext.height > 520:
                continue  # 主窗口不深扫，避免触碰聊天内容
            stack = [(window, 0)]
            while stack and scanned < MAX_NODES:
                node, depth = stack.pop()
                scanned += 1
                try:
                    role = node.get_role_name() or ""
                    label = norm(node.get_name())
                    if label:
                        if role == "push button" and label in LOG_IN_NAMES:
                            if not login["log_in_button"]:
                                login["log_in_button"] = True
                                login["button_name"] = node.get_name()
                                if click_login:
                                    iface = node.get_action_iface()
                                    if iface is not None and iface.get_n_actions() > 0:
                                        login["clicked"] = bool(iface.do_action(0))
                        if any(h in label for h in SCAN_HINTS):
                            login["scan_hint"] = True
                    if depth < MAX_DEPTH:
                        for k in range(node.get_child_count()):
                            child = node.get_child_at_index(k)
                            if child is not None:
                                stack.append((child, depth + 1))
                except Exception:
                    continue

    return {"ok": True, "app": apps[0][0], "windows": windows,
            "login": login, "nodes_scanned": scanned}


def main() -> int:
    os.environ.update(child_env())
    click = "--click-login" in sys.argv[1:]
    try:
        payload = probe(click)
    except Exception as exc:  # noqa: BLE001 - 探针必须 fail-open
        payload = {"ok": False, "error": type(exc).__name__}
    return emit(payload)


if __name__ == "__main__":
    sys.exit(main())
