"""Authenticated console controls for the local WeChat client and bridge."""
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time

from tool_routes import _make_qr_png


BRIDGE = "dafeiyu-wechat-bridge.service"
PROBE_STATES = {"online", "scan", "confirm", "login", "unknown"}
STATE_LABELS = {"online": "微信已登录", "scan": "微信等待扫码", "confirm": "请在手机微信上确认",
                "login": "微信停在登录界面", "unknown": "暂时无法确认微信原生窗口状态"}
NATIVE_LOCK = threading.Lock()
RESTART_LOCK = threading.Lock()
LAST_RESTART = float("-inf")
GENERATION_WINDOW_SECONDS = 240


def _native(root, action):
    if root is None:
        return None
    try:
        session = (root / "client-session").resolve(strict=True)
        runtime = (session / "runtime").resolve(strict=True)
        bus = runtime / "at-spi/bus_99"
        if runtime.parent != session or not runtime.name.startswith(".runtime.") or not bus.is_socket():
            return None
        helper = root / "transport/wx_account_probe.py"
        if not helper.is_file():
            return None
        env = {**os.environ, "DISPLAY": ":99", "XAUTHORITY": str(runtime / "Xauthority"),
               "XDG_RUNTIME_DIR": str(runtime), "AT_SPI_BUS_ADDRESS": "unix:path=" + str(bus),
               "GI_TYPELIB_PATH": str(root / "atspi-typelib/usr/lib/aarch64-linux-gnu/girepository-1.0"),
               "LC_ALL": "C"}
        if not NATIVE_LOCK.acquire(blocking=False):
            return None
        try:
            result = subprocess.run([sys.executable, "-B", str(helper), action], env=env,
                                    timeout=10 if action == "qr" else 5,
                                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=True)
        finally:
            NATIVE_LOCK.release()
        if len(result.stdout) > 1600:
            return None
        payload = json.loads(result.stdout)
        if not isinstance(payload, dict) or payload.get("state") not in PROBE_STATES:
            return None
        return payload
    except (OSError, TypeError, ValueError, subprocess.SubprocessError):
        return None


def _status(root, action):
    native = _native(root, action) or {}
    state = native.get("state", "unknown")
    result = {"ok": True, "state": state, "label": STATE_LABELS[state],
              "canScan": state == "scan" and native.get("canScan") is True,
              "canOpenQr": state == "login" and native.get("canOpenQr") is True,
              "canReconnect": root is not None}
    token = native.get("qr")
    if action == "qr" and result["canScan"] and isinstance(token, str) and 1 <= len(token) <= 1000:
        result["image"] = _make_qr_png(token)
    elif action == "qr" and state == "scan":
        result["label"] = "暂未读到有效二维码，微信登录窗口就绪后可重试"
    elif action == "qr" and state == "login":
        result["label"] = "微信尚未进入扫码页，可刷新窗口状态后重试"
    return result


def _generation_owner_live(pid, thread, start_ticks):
    """Match the live Linux worker, including protection against ID reuse."""
    try:
        stat = Path(f"/proc/{pid}/task/{thread}/stat").read_text(encoding="utf-8")
        fields = stat.rsplit(")", 1)[1].split()
        return fields[0] not in {"Z", "X", "x"} and fields[19] == start_ticks
    except (FileNotFoundError, ProcessLookupError):
        return False
    except (OSError, UnicodeError, IndexError):
        return True  # A current generation with unreadable owner is not idle.


def _idle(health_path, bridge_pid):
    path = Path(health_path) if health_path else Path.home() / ".local/state/dafeiyu-wechat/bridge-health.json"
    try:
        with path.open("rb") as stream:
            raw = stream.read(512_001)
        if len(raw) > 512_000:
            return False
        data = json.loads(raw)
        now = time.time()
        active = data.get("active_generation")
        if isinstance(active, dict) and active.get("pid") == bridge_pid:
            at, thread, ticks = active.get("time"), active.get("thread"), active.get("start_ticks")
            if (type(at) in (int, float) and -2 <= now - at < GENERATION_WINDOW_SECONDS
                    and type(thread) is int and thread > 0 and isinstance(ticks, str)
                    and ticks.isdecimal() and _generation_owner_live(bridge_pid, thread, ticks)):
                return False
        event = data.get("last_event") or {}
        if event.get("phase") in {"queue", "generation", "submit"}:
            if 0 <= now - float(event.get("time", 0)) <= 180:
                return False
        traces = data.get("outbound_traces", {})
        return not any(item.get("status") == "submitting" and
                       0 <= now - float(item.get("time", 0)) <= 180 for item in traces.values())
    except (OSError, ValueError, TypeError, AttributeError):
        return False


def _systemctl(*args, timeout=5):
    output = subprocess.run(["systemctl", "--user", "--no-ask-password", *args], timeout=timeout,
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, check=True)
    return output.stdout.strip()


def _reconnect(root, cfg):
    global LAST_RESTART
    if root is None:
        return 503, {"error": "微信本地桥接配置不可用"}
    if not RESTART_LOCK.acquire(blocking=False):
        return 409, {"error": "微信桥接正在重连"}
    try:
        if time.monotonic() - LAST_RESTART < 60:
            return 429, {"error": "重连请求已提交，请稍后刷新状态"}
        previous = int(_systemctl("show", "-p", "MainPID", "--value", BRIDGE))
        if previous and not _idle(cfg.get("wechatBridgeHealthPath"), previous):
            return 409, {"error": "微信桥接可能正在处理消息，请稍后重连"}
        LAST_RESTART = time.monotonic()
        _systemctl("restart", BRIDGE, timeout=23)
        current = int(_systemctl("show", "-p", "MainPID", "--value", BRIDGE))
        if current > 0 and current != previous and _systemctl("is-active", BRIDGE) == "active":
            return 200, {"ok": True, "restarted": True}
        return 503, {"error": "重连结果未确认，请刷新后核对"}
    except (OSError, ValueError, subprocess.SubprocessError):
        return 503, {"error": "重连结果未确认，请刷新后核对"}
    finally:
        RESTART_LOCK.release()


def resolve(handler, method, path, root, cfg):
    routes = {("GET", "/api/account/wechat"): "status",
              ("POST", "/api/account/wechat/qr"): "qr",
              ("POST", "/api/account/wechat/reconnect"): "reconnect"}
    action = routes.get((method, path))
    if action is None:
        return False
    if not handler.need_auth():
        return True
    if method == "POST":
        if not handler.headers.get("Origin") or not handler.origin_ok():
            handler.fail(403, "请从本控制台页面操作")
            return True
        try:
            body = handler.read_json()
        except ValueError:
            handler.fail(400, "微信账号操作请求无效")
            return True
        expected = {} if action == "qr" else {"confirmed": True}
        if body.keys() != expected.keys() or (expected and body.get("confirmed") is not True):
            handler.fail(400, "微信账号操作尚未确认")
            return True
    if action == "reconnect":
        status, result = _reconnect(root, cfg)
        handler.json(status, result)
    else:
        result = _status(root, action)
        if action == "status":
            status, _ = handler.app.console.wechat.status()
            result["bridgeStatus"] = status.get("bridgeStatus", "unknown")
        handler.json(200, result)
    return True
