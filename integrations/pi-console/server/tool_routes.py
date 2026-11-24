"""Fixed route table for the origin-tool bridge (stdlib only).

Mount (minimal, purely additive -- see INTEGRATION.md):

    import tool_routes                      # 1) import once at top
    tool_routes.bind_config(app, cfg)       # 2) after App(...) is built

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            ...gate...
            if tool_routes.resolve(self, "GET", path):    # 3) before the 404
                return
            ...existing routes...

        def do_POST(self):
            ...same two lines with "POST"...

The bridge uses only the console Handler's existing surface: `fail/json/ok/
need_auth/read_json/_send`, plus `app`, `app.ui_dir`, `app.fixture`.

Rules enforced here
------------------
* Fixed verb + fixed parameter names, hand-validated. No `{path:.*}`, no URL
  passthrough, no "proxy anything" endpoint.
* Every route is authenticated by the console session (`need_auth`), so there is
  no second login. Sensitive routes additionally need `confirm: true` -- one
  explicit page confirmation per action, never a second password.
* Upstream targets come from `napcat_tools.NAP_ACTIONS`; request data cannot
  choose a path, host, or method.
* Maintenance actions (restart, config write, import, file browse) are bound to
  the human console only: `assert_console_only("console")` and no route that an
  AI/social caller can reach.
* Nothing here spawns a shell except the origin's own recorded launch script,
  which is skipped entirely when the client is already up.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import struct
import subprocess
import threading
import time
import zlib
from pathlib import Path
from urllib.parse import urlsplit

from tool_common import ToolError
from qrcodegen import QrCode
import napcat_tools as nt
from napcat_tools import (CONFIG_KEYS, NEVER_TOUCH_UNITS, OBSERVE_UNITS, BoxReader,
                          _present)

UI_STATIC = {
    "/admin-tools.js": ("admin-tools.js", "application/javascript; charset=utf-8"),
    "/admin-tools.css": ("admin-tools.css", "text/css; charset=utf-8"),
}

# (method, path) -> handler name. This table IS the whole surface.
_ROUTES = {
    # ---- read ------------------------------------------------------------ #
    ("GET", "/api/tools/status"): "get_status",
    ("GET", "/api/tools/qrcode"): "get_qrcode",
    ("GET", "/api/tools/login"): "get_login",
    ("GET", "/api/tools/onebot"): "get_onebot",
    ("GET", "/api/tools/recovery"): "get_recovery",
    ("GET", "/api/tools/logs"): "get_logs",
    ("GET", "/api/tools/config"): "get_config",
    ("GET", "/api/tools/capabilities"): "get_capabilities",
    ("GET", "/api/tools/original"): "get_original",
    ("GET", "/api/tools/plugins"): "get_plugins",
    ("GET", "/api/tools/files"): "get_files",
    ("GET", "/api/tools/export"): "get_export",
    ("GET", "/api/tools/restart/plan"): "get_restart_plan",
    # ---- cache ----------------------------------------------------------- #
    ("POST", "/api/tools/refresh"): "post_refresh",
    # ---- page-confirmed actions ------------------------------------------ #
    ("POST", "/api/tools/restart"): "post_restart",
    ("POST", "/api/tools/qrcode/refresh"): "post_qrcode_refresh",
    ("POST", "/api/tools/config"): "post_config",
    ("POST", "/api/tools/import"): "post_import",
}


def box(app, cfg):
    reader = getattr(app, "_tools_box", None)
    if reader is None:
        reader = BoxReader(cfg, getattr(app, "fixture", None))
        app._tools_box = reader
    return reader


def app_cfg(handler):
    return getattr(handler.app, "_tools_cfg", None) or {}


def bind_config(app, cfg):
    app._tools_cfg = dict(cfg or {})
    app._tools_cfg.setdefault("wechatBridgeLog", str(Path.home() / "dafeiyu-wechat/bridge.log"))
    app._tools_box = None
    app._tools_restart_lock = threading.Lock()
    app._tools_restart_after = 0


# --------------------------------------------------------------------------- #
# Validation
# --------------------------------------------------------------------------- #


def _int_param(value, name, lo, hi, default):
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        raise ToolError("%s 需要整数" % name, 400, "bad_param")
    try:
        n = int(str(value), 10)
    except (TypeError, ValueError):
        raise ToolError("%s 需要整数" % name, 400, "bad_param") from None
    if not lo <= n <= hi:
        raise ToolError("%s 超出范围（%d–%d）" % (name, lo, hi), 400, "bad_param")
    return n


def _choice(value, name, allowed, default):
    if value is None or value == "":
        return default
    if value not in allowed:
        raise ToolError("%s 不在允许值内" % name, 400, "bad_param")
    return value


def _query(handler):
    from urllib.parse import parse_qs, urlsplit
    raw = urlsplit(getattr(handler, "path", "") or "").query
    if len(raw) > 512:
        raise ToolError("查询参数过长", 400, "bad_param")
    return {k: (v[-1] if v else "")
            for k, v in parse_qs(raw, keep_blank_values=False, max_num_fields=8).items()}


def _confirm(body, action_id):
    """One explicit page confirmation per dangerous action."""
    if body.get("confirm") is not True:
        raise ToolError("这个操作需要你确认一次后再执行", 409, "confirm_required")
    if body.get("action") != action_id:
        raise ToolError("动作不匹配", 400, "action_mismatch")


def _require_writable(reader):
    gate = reader.capability_gate()
    if not gate["canWrite"]:
        raise ToolError(gate["message"], 409, "not_adapted")
    return gate


# --------------------------------------------------------------------------- #
# Read handlers
# --------------------------------------------------------------------------- #


def get_status(handler):
    reader = box(handler.app, app_cfg(handler))
    handler.json(200, {"ok": True, "napcat": _nap_status(reader),
                       "boxes": _boxes(reader), "gate": reader.capability_gate()})


def _nap_status(reader):
    info = {
        "reachable": False,
        "version": reader.nap_version(),
        "qqVersion": None,
        "loggedIn": None,
        "offline": None,
        "coreReady": None,
        "loginPhase": None,
        "account": None,
        "nickname": None,
        "tokenConfigured": reader.token_configured(),
        "onebotTokenConfigured": reader.onebot_token_configured(),
        "source": "running qq process on loopback :6099",
        "legacyUnit": {"name": "napcat-qq.service", "used": False,
                       "note": "qq 进程不属于任何 systemd 单元，本控制台不会据此另起客户端"},
    }
    if not info["tokenConfigured"]:
        info["error"] = "后台凭据不可读，请联系管理员"
        return info
    try:
        st = reader.nap_call("QQLogin/CheckLoginStatus")
    except ToolError as e:
        info["error"] = e.message
        return info
    info["reachable"] = True
    if isinstance(st, dict):
        for src, dst in (("isLogin", "loggedIn"), ("isOffline", "offline"),
                         ("coreReady", "coreReady")):
            if src in st:
                info[dst] = bool(st.get(src))
        phase = st.get("loginPhase")
        info["loginPhase"] = str(phase)[:32] if isinstance(phase, str) else None
    if info["loggedIn"]:
        ok, profile = reader.nap_try("QQLogin/GetQQLoginInfo")
        if ok and isinstance(profile, dict):
            info["account"] = _mask(profile.get("uin"))
            info["online"] = bool(profile.get("online")) if "online" in profile else None
            nick = profile.get("nick") or profile.get("nickname")
            info["nickname"] = str(nick)[:40] if nick else None
    ok, qq_ver = reader.nap_try("base/QQVersion")
    if ok and isinstance(qq_ver, str) and qq_ver.strip():
        info["qqVersion"] = qq_ver.strip()[:40]
    return info


def _mask(value):
    if value is None:
        return None
    s = str(value).strip()
    if not s.isdigit() or len(s) < 5:
        return "已登录"
    return s[:2] + "*" * (len(s) - 4) + s[-2:]


def _boxes(reader):
    wx = reader.wechat_status()
    out = [{"id": k, "unit": v, "observed": True} for k, v in OBSERVE_UNITS.items()]
    out += [{"id": "never", "unit": u, "observed": False,
             "note": "刻意不触碰：可能造成同一账号重复登录"} for u in NEVER_TOUCH_UNITS]
    if isinstance(wx, dict):
        gw = [g for g in (wx.get("gateways") or [])
              if isinstance(g, dict) and g.get("platform") == "wechat"]
        bridge = (gw[0].get("bridge") or {}) if gw else {}
        out.append({"id": "wechat", "model": str(wx.get("model") or ""),
                    "provider": str(wx.get("provider") or ""),
                    "enabled": bool(gw[0].get("enabled")) if gw else None,
                    "bridgeOnline": bool(bridge.get("online")) if gw else None})
    else:
        out.append({"id": "wechat", "model": "", "provider": "",
                    "enabled": None, "bridgeOnline": None})
    return out


def get_qrcode(handler):
    """QR for login. When already logged in the origin answers code:-1 and we
    report that verbatim instead of inventing a QR."""
    reader = box(handler.app, app_cfg(handler))
    state = _login_state(reader)
    can_refresh = _can_refresh(reader)
    if state.get("isLogin") is True:
        handler.json(200, {"ok": True, "loggedIn": True, "qr": None,
                           "reason": "当前账号已在线，无需扫码",
                           "canRefresh": False})
        return
    if state.get("isLogin") is not False:
        handler.json(200, {"ok": True, "loggedIn": None, "qr": None,
                           "reason": "暂时无法确认 QQ 登录状态", "canRefresh": False})
        return
    try:
        data = reader.nap_call("QQLogin/GetQQLoginQrcode", allow_http_401=True)
    except ToolError as e:
        handler.json(200, {"ok": True, "loggedIn": False, "qr": None,
                           "reason": e.message, "canRefresh": can_refresh})
        return
    handler.json(200, {"ok": True, "loggedIn": False, "qr": _qr_payload(data),
                       "canRefresh": can_refresh})


def _qr_payload(data):
    if not isinstance(data, dict):
        raise ToolError("二维码接口未返回数据", 502, "bad_qr")
    png = data.get("pngbase64") or data.get("qrcode") or data.get("qrcodeurl")
    if isinstance(png, str) and len(png) <= 200_000 and png.startswith("iVBOR"):
        return {"image": "data:image/png;base64," + png}
    if isinstance(png, str) and png.startswith("data:image/png;base64,"):
        return {"image": png}
    if isinstance(png, str) and len(png) <= 1600:
        try:
            parsed = urlsplit(png)
            host = (parsed.hostname or "").lower()
            if parsed.scheme == "https" and (host == "qq.com" or host.endswith(".qq.com")):
                return {"image": _make_qr_png(png)}
        except ValueError:
            pass
    raise ToolError("二维码格式无法识别", 502, "bad_qr")


def _make_qr_png(value):
    qr = QrCode.encode_text(value, QrCode.Ecc.MEDIUM)
    scale, border = 4, 4
    size = qr.get_size()
    pixels = (size + border * 2) * scale
    rows = bytearray()
    for y in range(-border, size + border):
        row = b"\x00" + b"".join(
            bytes([0 if qr.get_module(x, y) else 255]) * scale
            for x in range(-border, size + border)
        )
        rows.extend(row * scale)

    def chunk(kind, data):
        return (struct.pack(">I", len(data)) + kind + data
                + struct.pack(">I", zlib.crc32(kind + data)))

    data = (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", pixels, pixels, 8, 0, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(rows))
            + chunk(b"IEND", b""))
    return "data:image/png;base64," + base64.b64encode(data).decode("ascii")


def _can_refresh(reader):
    # The installed route is known by its exact version. Do not call it to probe.
    return reader.capability_gate().get("version") == "4.18.42"


def post_qrcode_refresh(handler):
    body = handler.read_json()
    _confirm(body, "qr.refresh")
    reader = box(handler.app, app_cfg(handler))
    if not _can_refresh(reader) or _login_state(reader).get("isLogin") is not False:
        raise ToolError("当前登录状态无法刷新二维码", 409, "qr_unavailable")
    data = reader.nap_call("QQLogin/RefreshQRcode")
    if isinstance(data, dict) and data.get("restarting"):
        handler.json(200, {"ok": True, "loggedIn": False, "qr": None,
                           "reason": "QQ 正在重连，请稍后获取二维码", "canRefresh": True})
        return
    handler.json(200, {"ok": True, "loggedIn": False, "qr": _qr_payload(data),
                       "canRefresh": True})


def _login_state(reader):
    try:
        return reader.nap_call("QQLogin/CheckLoginStatus") or {}
    except ToolError:
        return {}


def get_login(handler):
    reader = box(handler.app, app_cfg(handler))
    st = _login_state(reader)
    handler.json(200, {
        "ok": True,
        "state": {
            "loggedIn": bool(st.get("isLogin")) if "isLogin" in st else None,
            "offline": bool(st.get("isOffline")) if "isOffline" in st else None,
            "coreReady": bool(st.get("coreReady")) if "coreReady" in st else None,
            "loginPhase": str(st.get("loginPhase") or "")[:32] or None,
            "qrAccepted": bool(st.get("qrLoginAccepted")) if "qrLoginAccepted" in st else None,
        } if st else None,
        "canRefresh": _can_refresh(reader),
    })


def get_onebot(handler):
    reader = box(handler.app, app_cfg(handler))
    summary = reader.onebot_summary()
    handler.json(200, {
        "ok": True,
        "tokenConfigured": reader.onebot_token_configured(),
        "configPath": summary["path"],
        "readable": summary["readable"],
        "networks": summary["networks"],
        "endpoints": [
            {"id": "ws", "label": "正向 WebSocket", "addr": "127.0.0.1:3001",
             "note": "由正在运行的 qq 进程持有；引擎自行重连"},
            {"id": "webui", "label": "WebUI", "addr": _hostport(reader.nap_base),
             "note": "登录与扫码的唯一入口（本机回环）"},
        ],
        "writable": False,
        "writableReason": "OneBot 端口/令牌改动需要改原版配置并重启，"
                          "本页只读展示；写入留给维护窗口",
    })


def _hostport(url):
    text = str(url or "")
    if "//" in text:
        text = text.split("//", 1)[1]
    return text.split("/", 1)[0] or "未知"


def get_recovery(handler):
    reader = box(handler.app, app_cfg(handler))
    handler.json(200, {
        "ok": True, "mode": "observe-only",
        "units": [{"id": k, "unit": v} for k, v in OBSERVE_UNITS.items()],
        "neverTouch": [{"unit": u, "reason": "可能造成同一账号重复登录"}
                       for u in NEVER_TOUCH_UNITS],
        "watchdog": _watchdog_view(
            reader, handler.app.state_dir / "qq-recovery.json"),
        "allowed": ["读取恢复判定所需的观测结果", "查看守护状态文件里的计数与时间戳"],
        "denied": ["不会 start/stop 任何 origin 单元",
                   "不会拉起 napcat-qq.service，也不会另起第二个 QQ 客户端"],
    })


def _watchdog_view(reader, default_path=None):
    configured = reader.cfg.get("watchdogState") or default_path
    state_path = Path(str(configured)).expanduser() if configured else None
    view = {"statePath": str(state_path) if state_path else None,
            "readable": False, "last": None}
    if reader.fixture is not None and hasattr(reader.fixture, "watchdog"):
        view.update(readable=True, last=reader.fixture.watchdog())
        return view
    try:
        if state_path is None:
            return view
        data = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return view
    if isinstance(data, dict):
        view["readable"] = True
        view["last"] = {k: v for k, v in data.items()
                        if isinstance(v, (int, float, bool, str)) and len(str(v)) < 64}
    return view


_WECHAT_EVENT = re.compile(
    r"^([0-9]{4}-[0-9]{2}-[0-9]{2} [0-9]{2}:[0-9]{2}:[0-9]{2}) "
    r"(received|mention|queue|generation|submit|confirmation) "
    r"trace='([0-9a-f]{64}|[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12})' "
    r"reason='([a-z0-9_:-]{1,80})'$"
)
_WECHAT_EVENT_LABELS = {
    ("received", "group"): "收到群消息",
    ("received", "private"): "收到私聊消息",
    ("mention", "accepted"): "识别到 @",
    ("mention", "auto_reply"): "符合自动接话条件",
    ("mention", "direction_unverified"): "消息方向未确认",
    ("queue", "accepted"): "已入队",
    ("queue", "queue_full"): "队列已满",
    ("generation", "started"): "模型开始生成",
    ("generation", "ready"): "回复已生成",
    ("generation", "proactive_participation_declined"): "模型选择不接话",
    ("generation", "generation_failed"): "模型生成失败",
    ("submit", "started"): "正在提交发送",
    ("submit", "tx_denied:composer_not_empty"): "输入框有草稿，已停止发送",
    ("submit", "client_unavailable"): "微信客户端暂不可用",
    ("confirmation", "visible_outgoing"): "已确认原会话可见",
}


def _wechat_log_view(data, limit):
    """Project known event states only. No raw log fields reach the console."""
    rows = []
    for line in data["lines"]:
        event = _WECHAT_EVENT.fullmatch(line)
        if event is None:
            continue
        stamp, phase, trace, reason = event.groups()
        label = _WECHAT_EVENT_LABELS.get((phase, reason))
        if label is None and phase == "confirmation" and reason.startswith("unconfirmed:"):
            label = "发送结果未确认"
        if label is None and phase == "submit" and reason.startswith("auto_reply_stale:"):
            label = "对话已变化，取消本条接话"
        if label is not None:
            reference = hashlib.sha256(trace.encode("ascii")).hexdigest()[:10]
            rows.append(f"{stamp} [{reference}] {label}")
    return {"lines": rows[-limit:], "truncated": data["truncated"] or len(rows) > limit,
            "redacted": len(data["lines"]) - len(rows)}


def get_logs(handler):
    reader = box(handler.app, app_cfg(handler))
    q = _query(handler)
    lines = _int_param(q.get("lines"), "lines", 10, 200, 80)
    sources = sorted(reader.log_tails.keys())
    which = _choice(q.get("source"), "source", tuple(sources), "qq-engine")
    if which == "wechat-bridge":
        # Bridge polling is noisy. Read a larger tail within BoxReader's 64 KiB
        # cap before applying the requested limit to visible chat events.
        data = _wechat_log_view(reader.tail(which, max(500, lines)), lines)
    else:
        data = reader.tail(which, lines)
    handler.json(200, {"ok": True, "source": which, "sources": sources,
                       "lines": data["lines"], "truncated": data["truncated"],
                       "redactedLines": data["redacted"], "readOnly": True})


def get_config(handler):
    reader = box(handler.app, app_cfg(handler))
    gate = reader.capability_gate()
    ok, raw = reader.nap_try("WebUIConfig/GetConfig")
    if not ok:
        handler.json(200, {"ok": True, "gate": gate, "config": None,
                           "reason": "配置接口暂不可读"})
        return
    ok_ip, ipdata = reader.nap_try("WebUIConfig/GetClientIP")
    ok_ssl, ssl = reader.nap_try("WebUIConfig/GetSSLStatus")
    handler.json(200, {
        "ok": True, "gate": gate,
        "config": nt._whitelist_config(raw),
        "editable": list(CONFIG_KEYS),
        "clientIp": (ipdata or {}).get("ip") if ok_ip and isinstance(ipdata, dict) else None,
        "ssl": {"certExists": bool((ssl or {}).get("certExists")),
                "keyExists": bool((ssl or {}).get("keyExists")),
                "enabled": bool((ssl or {}).get("enabled"))} if ok_ssl else None,
        "logNames": _log_names(reader),
        "writable": True,
        "writableReason": "可改白名单字段；提交需要页面确认一次",
    })


def _log_names(reader):
    ok, names = reader.nap_try("Log/GetLogList")
    if not ok or not isinstance(names, list):
        return []
    return [n for n in names[:60] if isinstance(n, str) and len(n) <= 80]


def get_plugins(handler):
    reader = box(handler.app, app_cfg(handler))
    probe = nt.probe_capabilities(reader)
    ok, data = reader.nap_try("Plugin/List")
    plugins = []
    manager_missing = None
    if ok and isinstance(data, dict):
        manager_missing = data.get("pluginManagerNotFound")
        for p in data.get("plugins") or []:
            if isinstance(p, dict):
                plugins.append({
                    "id": str(p.get("id") or p.get("name") or "")[:60],
                    "name": str(p.get("name") or p.get("id") or "")[:60],
                    "version": str(p.get("version") or "")[:24],
                    "enabled": bool(p.get("enable", p.get("enabled", False))),
                })
    handler.json(200, {
        "ok": True, "plugins": plugins,
        "pluginManagerNotFound": manager_missing,
        "extensionPages": len((data or {}).get("extensionPages") or []) if ok else 0,
        "available": probe["plugins"],
        "reason": "" if probe["plugins"] else
                  "本版 Plugin/List 返回 pluginManagerNotFound=true 且 plugins 为空："
                  "插件管理器未安装/未启用，插件安装与开关请在原站完成",
        "writable": False,
        "writableReason": "插件安装/启停在原站完成；本页只读",
    })


def get_files(handler):
    reader = box(handler.app, app_cfg(handler))
    nt.assert_console_only("console")
    q = _query(handler)
    root_id = _choice(q.get("root"), "root", ("config", "logs", "runtime"), "config")
    roots = {
        "config": reader.nap_config_dir,
        "logs": reader.nap_log_dir,
        "runtime": reader.nap_runtime,
    }
    root = roots[root_id]
    if root is None:
        handler.json(200, {"ok": True, "root": root_id, "path": "",
                           "available": False, "entries": [],
                           "reason": "尚未配置此目录"})
        return
    rel = str(q.get("path") or "")
    if q.get("file"):
        info = nt.read_file(root, q["file"])
        handler.json(200, {"ok": True, "root": root_id, "file": info})
        return
    handler.json(200, {"ok": True, "root": root_id, "path": rel,
                       "roots": sorted(roots.keys()),
                       "entries": nt.list_dir(root, rel)})


def get_export(handler):
    reader = box(handler.app, app_cfg(handler))
    nt.assert_console_only("console")
    cfg = nt.export_config(reader)
    handler.json(200, {"ok": True, "config": cfg,
                       "filename": "napcat-webui-config-%s.json" % time.strftime("%Y%m%d"),
                       "note": "导出内容只含白名单字段，不含令牌与证书"})


def get_restart_plan(handler):
    """Explain what a restart will do BEFORE the user confirms it."""
    reader = box(handler.app, app_cfg(handler))
    nt.assert_console_only("console")
    gate = reader.capability_gate()
    handler.json(200, {
        "ok": True, "gate": gate,
        "willDo": ["调用原版 Process/Restart，由 NapCat 自己重启 QQ 进程"],
        "willNotDo": ["不会 systemctl start/restart 任何单元",
                      "不会启动 napcat-qq.service（可能造成重复登录）",
                      "不会另起第二个 QQ 客户端"],
        "ownerNote": "当前 qq 进程不属于任何 systemd 单元（用户会话 scope）",
        "confirmRequired": True,
        "action": "restart",
    })


# --------------------------------------------------------------------------- #
# Page-confirmed writes
# --------------------------------------------------------------------------- #


def post_restart(handler):
    body = handler.read_json()
    nt.assert_console_only("console")
    _confirm(body, "restart")
    reader = box(handler.app, app_cfg(handler))
    _require_writable(reader)
    with handler.app._tools_restart_lock:
        now = time.monotonic()
        if now < handler.app._tools_restart_after:
            raise ToolError("QQ 已收到重启请求，请先确认恢复状态", 409, "restart_pending")
        handler.app._tools_restart_after = now + 30
        result = nt.restart_qq(reader)
    handler.json(200, {"ok": True, "restart": {"note": result.get("note")}})


def _panel_config_patch(reader, value):
    patch = nt.build_import_patch(value)
    current = urlsplit(reader.nap_base)
    if ("host" in patch and patch["host"] not in ("127.0.0.1", "localhost", "::1")
            or "port" in patch and patch["port"] != current.port
            or patch.get("disableWebUI") is True):
        raise ToolError("此设置会断开统一控制台的本机维护连接", 409, "control_link")
    return patch


def post_config(handler):
    body = handler.read_json()
    nt.assert_console_only("console")
    _confirm(body, "config.write")
    reader = box(handler.app, app_cfg(handler))
    _require_writable(reader)
    patch = _panel_config_patch(reader, body.get("patch"))
    # Use the origin's own action; it validates and persists the change.
    reader.nap_call("WebUIConfig/UpdateConfig", patch)
    reader.clear_cache()
    handler.json(200, {"ok": True, "patched": sorted(patch)})


def post_import(handler):
    body = handler.read_json()
    nt.assert_console_only("console")
    _confirm(body, "config.import")
    reader = box(handler.app, app_cfg(handler))
    _require_writable(reader)
    patch = _panel_config_patch(reader, body.get("patch"))
    reader.nap_call("WebUIConfig/UpdateConfig", patch)
    reader.clear_cache()
    handler.json(200, {"ok": True, "patched": sorted(patch)})


def post_refresh(handler):
    handler.read_json()
    reader = box(handler.app, app_cfg(handler))
    reader.clear_cache()
    handler.ok()


# --------------------------------------------------------------------------- #
# Capabilities / originals / static
# --------------------------------------------------------------------------- #


def get_capabilities(handler):
    reader = box(handler.app, app_cfg(handler))
    gate = reader.capability_gate()
    probe = nt.probe_capabilities(reader)
    handler.json(200, {
        "ok": True, "gate": gate,
        "items": nt.capability_rows(gate, probe),
        "routes": sorted(k[1] for k in _ROUTES),
        "consoleOnly": True,
    })


def get_original(handler):
    reader = box(handler.app, app_cfg(handler))
    cfg = app_cfg(handler)
    handler.json(200, {"ok": True, "items": [
        {"id": "napcat", "label": "NapCat WebUI", "addr": reader.nap_base,
         "note": "插件安装、终端、更新、证书均在此完成", "loopbackOnly": True},
        {"id": "model-hub", "label": "模型与档案", "addr": reader.hub_url,
         "note": "模型档案与激活沿用既有界面", "loopbackOnly": True},
        {"id": "wechat", "label": "微信后台", "addr": reader.wechat_url,
         "note": "微信核心与桥接沿用既有接口", "loopbackOnly": True},
        {"id": "console", "label": "本控制台", "addr": str(cfg.get("lanUrl") or ""),
         "note": "统一的手机/电脑入口（同一密码登录）", "loopbackOnly": False},
    ]})


def static_response(handler, path):
    name, ctype = UI_STATIC[path]
    ui_dir = getattr(handler.app, "ui_dir", None)
    if ui_dir is None:
        return False
    target = Path(ui_dir) / name
    if not target.is_file():
        return False
    if not handler.need_auth():
        return True
    try:
        body = target.read_bytes()
    except OSError:
        handler.fail(500, "页面读取失败")
        return True
    handler._send(200, body, ctype)
    return True


# --------------------------------------------------------------------------- #
# Dispatch
# --------------------------------------------------------------------------- #


def resolve(handler, method, path):
    handler_name = _ROUTES.get((method, path))
    if handler_name is None:
        if method == "GET" and path in UI_STATIC:
            return static_response(handler, path)
        return False
    fn = globals().get(handler_name)
    if fn is None:                      # pragma: no cover - table typo guard
        return False
    if not handler.need_auth():
        return True
    try:
        fn(handler)
    except ToolError as e:
        handler.fail(e.status, e.message)
    except ValueError as e:
        handler.fail(400, str(e))
    return True


def heartbeat(app):
    reader = getattr(app, "_tools_box", None)
    return {"mounted": True,
            "adapted": bool(reader.capability_gate()["adapted"]) if reader else False}
