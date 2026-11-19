"""Bridge to the origin NapCat tool surface (stdlib only, loopback only).

VERIFIED contract (read from the live install, 2026-11-11, NapCat 4.18.42)
-----------------------------------------------------------------------------
Two-step session API -- NOT the "Bearer sha256(token)" form some docs show:

    POST /api/auth/login   {"hash": sha256(webuiToken + ".napcat")}
        -> {"code":0,"data":{"Credential":"<session token>"}}
    Authorization: Bearer <Credential>          (session token, NOT the hash)
    GET|POST /api/{Area}/{Action}
        -> {"code":0,"data": …} | HTTP 404 | {"code":-1}

Confirmed support matrix on this install (see CAPABILITIES below):

    QQLogin/CheckLoginStatus      POST   isLogin/isOffline/loginPhase/coreReady/…
    QQLogin/GetQQLoginInfo        POST   uin/nick/online/uid
    QQLogin/GetQQLoginQrcode      POST   code:-1 while logged in (see note)
    QQLogin/GetQuickLoginList     GET    [account strings]
    QQLogin/SetQuickLogin         POST   exists but returns -1 without params
    base/GetNapCatVersion         GET    {version}
    base/QQVersion                GET    str
    WebUIConfig/GetConfig         GET    host/port/ipWhitelist/…
    WebUIConfig/GetClientIP       GET    {ip}
    WebUIConfig/GetSSLStatus      GET    certExists/keyExists/enabled
    WebUIConfig/*  Update/Upload/Delete            -> privileged, page-confirmed
    Log/GetLogList                GET    [23 names, no extension]
    Log/GetLog                    GET    -1 with empty params (needs a name)
    Plugin/List                   GET    {plugins:[], pluginManagerNotFound:true}
    Process/Restart               POST   {code:0} and REALLY restarts QQ

Design rules kept here (per the migration brief)
------------------------------------------------
* Never start a second QQ client. The running `qq` process does NOT belong to a
  systemd unit (its cgroup is a user-scope session), so "native restart" goes
  through the origin's own Process/Restart and its on-disk launch script.
* Credentials are read from the origin's own config by the server process and
  are never printed, returned, or written. Only booleans leave.
* No generic URL proxy: the catalog below is a fixed list; request data can
  never choose an upstream path.
* Maintenance tools (terminal/file/config/restart) are for the human console
  only. `assert_console_only()` guards them so an AI/social caller cannot use
  this module to act on the host.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import subprocess
import threading
import time
from pathlib import Path

from tool_common import ToolError, capability, http_json, parse_version

# --------------------------------------------------------------------------- #
# Fixed action catalog: (Area/Action, HTTP method, timeout)
# Nothing outside this dict is ever reachable.
# --------------------------------------------------------------------------- #

NAP_ACTIONS = {
    # ---- read ------------------------------------------------------------ #
    "base/GetNapCatVersion": ("GET", 6),
    "base/QQVersion": ("GET", 6),
    "WebUIConfig/GetConfig": ("GET", 8),
    "WebUIConfig/GetClientIP": ("GET", 6),
    "WebUIConfig/GetSSLStatus": ("GET", 8),
    "Log/GetLogList": ("GET", 8),
    "Log/GetLog": ("GET", 12),
    "Plugin/List": ("GET", 8),
    "QQLogin/CheckLoginStatus": ("POST", 8),
    "QQLogin/GetQQLoginInfo": ("POST", 8),
    "QQLogin/GetQQLoginQrcode": ("POST", 25),
    "QQLogin/RefreshQRcode": ("POST", 25),
    "QQLogin/GetQuickLoginList": ("GET", 8),
    "QQLogin/SetQuickLogin": ("POST", 8),
    # ---- page-confirmed writes ------------------------------------------- #
    "Process/Restart": ("POST", 10),
    "WebUIConfig/UpdateConfig": ("POST", 10),
    "WebUIConfig/UpdateDisableWebUI": ("POST", 10),
}

# Actions that need an explicit single-use confirmation from the page.
CONFIRM_REQUIRED = {
    "Process/Restart",
    "QQLogin/RefreshQRcode",
    "WebUIConfig/UpdateConfig",
    "WebUIConfig/UpdateDisableWebUI",
}

# Actions this console will never bind to an AI/social caller.
CONSOLE_ONLY = set(NAP_ACTIONS)

# Verified-adapted version window. Outside it (or unknown) the console shows its
# limits and keeps writes closed.
NAP_CATALOG_MIN = "4.18.0"
NAP_CATALOG_MAX = "4.18.99"

# Origin units this console only ever OBSERVES (it does not own them).
OBSERVE_UNITS = {
    "qq_engine": "dafeiyu.service",
    "qq_admin_relay": "qq-admin.service",
    "model_hub": "model-hub.service",
    "wechat_core": "dafeiyu-wechat-core.service",
    "wechat_bridge": "dafeiyu-wechat-bridge.service",
    "wechat_client": "dafeiyu-wechat-client.service",
    "watchdog": "qq-recovery.timer",
}
# Never started by this console: it would risk a duplicate account login.
NEVER_TOUCH_UNITS = ("napcat-qq.service",)

# WebUIConfig display whitelist (no token, no cert content, no path).
CONFIG_KEYS = ("host", "port", "loginRate", "disableWebUI", "enableXForwardedFor",
               "accessControlMode")

SESSION_TTL = 20 * 60


# --------------------------------------------------------------------------- #
# Paths / credentials
# --------------------------------------------------------------------------- #


def _p(value, default=None):
    if not value:
        return Path(str(default)).expanduser() if default else None
    return Path(str(value)).expanduser()


def _secret(path):
    try:
        text = path.read_text(encoding="utf-8").strip()
    except OSError as e:
        raise ToolError("后台凭据不可读，请联系管理员", 502, "credential_unreadable") from e
    if not text:
        raise ToolError("后台凭据为空，请联系管理员", 502, "credential_empty")
    return text


def _present(value):
    return bool(str(value or "").strip())


class BoxReader:
    """Reads/writes the loopback origin through its own fixed API."""

    def __init__(self, cfg, fixture=None):
        self.cfg = cfg
        self.fixture = fixture          # may be a dict (tests) or a FakeBox
        self.nap_base = str(cfg.get("napcatBase", "http://127.0.0.1:6099")).rstrip("/")
        runtime = cfg.get("napcatRuntime") or os.environ.get("DAFEIYU_NAPCAT_ROOT")
        self.nap_runtime = _p(runtime)
        self.nap_secrets = self.nap_runtime / "secrets" if self.nap_runtime else None
        explicit_config = _p(cfg.get("napcatWebUiConfig"))
        self.nap_config_candidates = ([explicit_config] if explicit_config else
            [self.nap_runtime / "qq/opt/QQ/resources/app/config/webui.json"]
            if self.nap_runtime else [])
        # OneBot config lives next to the WebUI config, named per account.
        self.nap_config_dir = self.nap_config_candidates[0].parent if self.nap_config_candidates else None
        self.nap_log_dir = (_p(cfg.get("napcatLogDir")) or
                            (self.nap_config_dir.parent / "logs" if self.nap_config_dir else None))
        self.nap_launch_script = (_p(cfg.get("napcatLaunchScript")
                                     or os.environ.get("DAFEIYU_NAPCAT_LAUNCH_SCRIPT")) or
                                 (self.nap_runtime / "start-qq-persistent.sh"
                                  if self.nap_runtime else None))
        self.hub_url = str(cfg.get("qqHubUrl", "http://127.0.0.1:18090")).rstrip("/")
        qq_root = cfg.get("qqRoot") or os.environ.get("DAFEIYU_QQ_ROOT")
        self.hub_root = _p(qq_root)
        self.wechat_url = str(cfg.get("wechatCoreUrl", "http://127.0.0.1:19301")).rstrip("/")
        self.log_tails = {
            "qq-engine": (_p(cfg.get("qqEngineLog")) or
                          (self.hub_root / "run/dafeiyu.log" if self.hub_root else None)),
            "qq-relay": (_p(cfg.get("qqRelayLog")) or
                         (self.hub_root / "run/qq-admin.log" if self.hub_root else None)),
            "model-hub": (_p(cfg.get("hubLog")) or
                          (self.hub_root / "run/model-hub.log" if self.hub_root else None)),
            "wechat-bridge": _p(cfg.get("wechatBridgeLog")),
            "napcat": (_p(cfg.get("napcatLog")) or
                       (self.nap_runtime / "napcat.log" if self.nap_runtime else None)),
        }
        self._cache = {}
        self._session = None
        self._session_at = 0.0
        self._session_lock = threading.Lock()

    # ---- cache ----------------------------------------------------------- #

    def _cached(self, key, fn):
        if self.fixture is not None and hasattr(self.fixture, "action"):
            return fn()
        if key in self._cache:
            return self._cache[key]
        value = fn()
        self._cache[key] = value
        return value

    def clear_cache(self):
        self._cache.clear()

    # ---- credential ------------------------------------------------------ #

    def webui_token(self):
        if self.fixture is not None and hasattr(self.fixture, "action"):
            return "fixture-webui-token"
        for path in self.nap_config_candidates:
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            token = data.get("token") if isinstance(data, dict) else None
            if isinstance(token, str) and token.strip():
                return token.strip()
        if self.nap_secrets is None:
            raise ToolError("尚未配置 NapCat 私有目录", 503, "credential_unconfigured")
        return _secret(self.nap_secrets / "webui.token")

    def token_configured(self):
        try:
            self.webui_token()
            return True
        except ToolError:
            return False

    def onebot_token_configured(self):
        """Report presence only; the token itself never leaves this method."""
        if self.fixture is not None and hasattr(self.fixture, "action"):
            return bool(getattr(self.fixture, "onebot_token", True))
        if self.nap_secrets is None:
            return False
        try:
            return _present((self.nap_secrets / "onebot.token").read_text(encoding="utf-8"))
        except OSError:
            return False

    # ---- session --------------------------------------------------------- #

    def _login(self):
        if self.fixture is not None and hasattr(self.fixture, "action"):
            return "fixture-session"
        token = self.webui_token()
        digest = hashlib.sha256((token + ".napcat").encode("utf-8")).hexdigest()
        body = http_json(self.nap_base + "/api/auth/login", "POST",
                         body={"hash": digest}, headers={"Origin": self.nap_base},
                         timeout=10)
        if not isinstance(body, dict) or body.get("code") != 0:
            raise ToolError("后台拒绝了本次登录，请检查服务状态", 502, "nap_login_rejected")
        cred = (body.get("data") or {}).get("Credential")
        if not isinstance(cred, str) or not cred:
            raise ToolError("后台没有返回会话凭证", 502, "nap_no_credential")
        return cred

    def _session_cred(self):
        with self._session_lock:
            now = time.time()
            if self._session and now - self._session_at < SESSION_TTL:
                return self._session
            self._session = self._login()
            self._session_at = now
            return self._session

    def drop_session(self):
        with self._session_lock:
            self._session = None
            self._session_at = 0.0

    # ---- action call ----------------------------------------------------- #

    def nap_call(self, action, params=None, timeout=None, retry_on_auth=True,
                 allow_http_401=False):
        """Call one fixed action. `params` are validated by the route layer.

        Returns the unwrapped `data`. Raises ToolError with a stable code so the
        UI can distinguish "not in this version" from "failed".
        """
        if action not in NAP_ACTIONS:
            raise ToolError("这个后台动作不在允许清单内", 403, "action_not_allowed")
        if self.fixture is not None and hasattr(self.fixture, "action"):
            return self.fixture.action(action, params or {})
        method, default_timeout = NAP_ACTIONS[action]
        cred = self._session_cred()
        headers = {"Authorization": "Bearer " + cred, "Origin": self.nap_base}
        kwargs = {}
        if allow_http_401:
            kwargs["allow_codes"] = (200, 401)
        try:
            body = http_json("%s/api/%s" % (self.nap_base, action), method,
                             body=(params if params is not None else {}) if method == "POST" else None,
                             headers=headers, timeout=timeout or default_timeout, **kwargs)
        except ToolError as e:
            if e.code in ("upstream_denied", "upstream_missing") and retry_on_auth:
                # An expired session looks exactly like this; re-login once.
                self.drop_session()
                return self.nap_call(action, params, timeout, retry_on_auth=False,
                                     allow_http_401=allow_http_401)
            raise
        if not isinstance(body, dict):
            raise ToolError("后台返回的内容无法解析", 502, "upstream_bad_json")
        code = body.get("code")
        if code != 0:
            raise ToolError("后台没有完成这个动作，可能是版本不支持该接口",
                            502, "unsupported_action")
        return body.get("data")

    def nap_try(self, action, params=None):
        """Call an action, returning (ok, data_or_None). For capability probing."""
        try:
            return True, self.nap_call(action, params)
        except ToolError:
            return False, None

    # ---- version --------------------------------------------------------- #

    def nap_version(self):
        return self._cached("nap_version", self._nap_version)

    def _nap_version(self):
        ok, data = self.nap_try("base/GetNapCatVersion")
        if not ok:
            return None
        if isinstance(data, dict):
            v = data.get("version")
            if isinstance(v, str) and parse_version(v):
                return v.strip()
        if isinstance(data, str) and parse_version(data):
            return data.strip()
        return None

    def capability_gate(self):
        v = self.nap_version()
        parsed = parse_version(v)
        if parsed is None:
            return {"version": None, "adapted": False, "canWrite": False, "level": "warning",
                    "message": "未读到 NapCat 版本，按受限模式显示：只开放读取，"
                               "重启/配置写入不开放"}
        lo, hi = parse_version(NAP_CATALOG_MIN), parse_version(NAP_CATALOG_MAX)
        if lo <= parsed <= hi:
            return {"version": v, "adapted": True, "canWrite": True, "level": "info",
                    "message": "当前版本 %s 在已适配范围内" % v}
        return {"version": v, "adapted": False, "canWrite": False, "level": "warning",
                "message": "当前版本 %s 不在已适配范围 %s–%s：写入类动作已关闭，"
                           "只读动作照常显示" % (v, NAP_CATALOG_MIN, NAP_CATALOG_MAX)}

    # ---- other originals ------------------------------------------------- #

    def wechat_status(self):
        def run():
            try:
                return http_json(self.wechat_url + "/api/status", timeout=5)
            except ToolError:
                return None
        return self._cached("wechat_status", run)

    # ---- local files ----------------------------------------------------- #

    def read_json_file(self, path):
        try:
            return json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def onebot_config_path(self):
        """The per-account OneBot file, otherwise the single one if unique."""
        if self.nap_config_dir is None:
            return None
        try:
            files = sorted(self.nap_config_dir.glob("onebot11*.json"))
        except OSError:
            return None
        if not files:
            return None
        return files[0] if len(files) == 1 else files[0]

    def onebot_summary(self):
        """OneBot endpoints WITHOUT tokens (booleans only)."""
        path = self.onebot_config_path()
        if path is None:
            return {"path": None, "networks": [], "readable": False}
        data = self.read_json_file(path)
        if not isinstance(data, dict):
            return {"path": str(path), "networks": [], "readable": False}
        nets = []
        for kind, label in (("websocketServers", "正向 WebSocket"),
                            ("websocketClients", "反向 WebSocket 客户端"),
                            ("httpServers", "HTTP 服务端"),
                            ("httpSseServers", "HTTP SSE"),
                            ("httpClients", "HTTP 上报地址")):
            for item in (data.get("network") or {}).get(kind) or []:
                if not isinstance(item, dict):
                    continue
                nets.append({
                    "kind": kind, "label": label,
                    "name": str(item.get("name") or "")[:40],
                    "enable": bool(item.get("enable")),
                    "host": str(item.get("host") or "")[:40],
                    "port": item.get("port") if isinstance(item.get("port"), int) else None,
                    "hasToken": _present(item.get("token")),
                })
        return {"path": str(path), "networks": nets, "readable": True}

    def tail(self, name, lines=80):
        path = self.log_tails.get(name)
        if path is None:
            raise ToolError("日志不存在", 404, "log_unknown")
        if self.fixture is not None and hasattr(self.fixture, "log"):
            text = self.fixture.log(name)
        else:
            try:
                text = _tail_text(path, lines)
            except OSError as e:
                raise ToolError("日志文件暂不可读", 502, "log_unreadable") from e
        return redact_tail(text, limit_lines=lines)

    def nap_log(self, name, lines=200):
        """Tail one of NapCat's own log files by NAME (path cannot be supplied)."""
        clean = str(name or "")
        if not re.fullmatch(r"[0-9A-Za-z_.-]{4,80}", clean):
            raise ToolError("日志名不合法", 400, "bad_param")
        if self.fixture is not None and hasattr(self.fixture, "nap_log"):
            return redact_tail(self.fixture.nap_log(clean), limit_lines=lines)
        if self.nap_log_dir is None:
            raise ToolError("尚未配置 NapCat 日志目录", 503, "path_unconfigured")
        target = self.nap_log_dir / (clean if clean.endswith(".log") else clean + ".log")
        try:
            resolved = target.resolve()
            resolved.relative_to(self.nap_log_dir.resolve())
        except (OSError, ValueError):
            raise ToolError("日志名不合法", 400, "bad_param") from None
        try:
            text = _tail_text(resolved, lines)
        except OSError as e:
            raise ToolError("日志文件暂不可读", 502, "log_unreadable") from e
        return redact_tail(text, limit_lines=lines)


# --------------------------------------------------------------------------- #
# Capability table (matched against the installed version)
# --------------------------------------------------------------------------- #

# Actions verified present on the installed build.
_VERIFIED = {
    "QQLogin/CheckLoginStatus", "QQLogin/GetQQLoginInfo", "QQLogin/GetQQLoginQrcode",
    "QQLogin/GetQuickLoginList", "QQLogin/SetQuickLogin",
    "base/GetNapCatVersion", "base/QQVersion", "WebUIConfig/GetConfig",
    "WebUIConfig/GetClientIP", "WebUIConfig/GetSSLStatus", "Log/GetLogList",
    "Log/GetLog", "Plugin/List", "Process/Restart",
}

CAPABILITIES = [
    capability("status", "status", "QQ 登录状态", "read",
               "只读：登录态/在线态/阶段；账号已掩码"),
    capability("login", "qrcode", "扫码登录", "action",
               "取二维码；已登录时原版会拒绝并返回原因", writable=True),
    capability("login", "login_state", "登录态刷新", "read", "读取扫码/登录阶段"),
    capability("onebot", "onebot", "OneBot 连接", "read",
               "端口与令牌是否已配置（不显示令牌）"),
    capability("plugins", "plugins", "插件列表", "read", "原版插件清单"),
    capability("logs", "logs", "运行日志", "read", "只读尾部，敏感行整体隐去"),
    capability("logs", "napcat_logs", "NapCat 原生日志", "read",
               "按文件名读取，路径不可指定"),
    capability("config", "config", "设备配置", "read",
               "白名单字段；令牌与证书内容不显示"),
    capability("config", "config_write", "WebUI 配置写入", "action",
               "只允许改白名单字段", writable=True, confirm=True),
    capability("recovery", "recovery", "恢复观测", "read", "只观测守护结果"),
    capability("restart", "restart", "原生重启 QQ", "action",
               "调用原版 Process/Restart；会真的重启客户端", writable=True, confirm=True),
    capability("files", "files", "文件浏览", "read",
               "只读浏览 NapCat 配置目录与日志目录"),
    capability("terminal", "terminal", "终端", "denied",
               "当前控制台尚未接入原版终端会话"),
    capability("update", "update", "手动更新", "action",
               "当前控制台尚未接入手动更新"),
    capability("export", "export", "配置导出", "read", "导出为 JSON 文本（不含令牌）"),
    capability("import", "import", "配置导入", "action",
               "仅允许白名单字段 JSON，仍需一次确认", writable=True, confirm=True),
]


def capability_rows(gate, probe=None):
    """Rows adjusted for the installed version + real probe results."""
    rows = []
    for row in CAPABILITIES:
        r = dict(row)
        r["adapted"] = gate["adapted"] if r["writable"] else True
        ident = r["id"]
        if ident == "terminal":
            r.update(available=False,
                     missingReason="当前控制台尚未接入原版终端会话")
        elif ident == "update":
            r.update(available=False,
                     missingReason="当前控制台尚未接入手动更新")
        elif ident == "qrcode" and probe is not None:
            r.update(available=probe.get("qrcode", True),
                     missingReason="" if probe.get("qrcode", True)
                     else "当前账号已登录或登录状态暂不可读")
        elif ident == "plugins" and probe is not None:
            r.update(available=probe.get("plugins", True),
                     missingReason="" if probe.get("plugins", True)
                     else "本版 Plugin/List 返回 pluginManagerNotFound=true，且 plugins 为空")
        elif ident == "import":
            r["note"] = "只接受白名单字段 JSON；结构与令牌一律拒绝"
        rows.append(r)
    return rows


def probe_capabilities(reader):
    """Ask the live origin what it can really do (read-only, cached)."""
    login_ok, login = reader.nap_try("QQLogin/CheckLoginStatus")
    qr_ok = login_ok and isinstance(login, dict) and login.get("isLogin") is False
    pl_ok, pl = reader.nap_try("Plugin/List")
    plugins_ok = bool(pl_ok and isinstance(pl, dict) and (pl.get("plugins")
                                                          or not pl.get("pluginManagerNotFound")))
    log_ok, _ = reader.nap_try("Log/GetLogList")
    return {"qrcode": bool(qr_ok), "plugins": plugins_ok, "logs": bool(log_ok)}


# --------------------------------------------------------------------------- #
# Console-only guard
# --------------------------------------------------------------------------- #


def assert_console_only(scope):
    """Maintenance actions belong to the human console session, never an AI.

    The unified console passes scope="console". Anything else (a future social/
    LLM bridge reusing this module) is refused outright -- the guard is here so
    the boundary does not depend on callers remembering it.
    """
    if scope != "console":
        raise ToolError("维护类动作只允许人在控制台页面执行", 403, "console_only")


# --------------------------------------------------------------------------- #
# Native restart (origin's own action + its own launch script)
# --------------------------------------------------------------------------- #


def restart_qq(reader):
    """Trigger the ORIGIN restart action. Never spawns a second client."""
    assert_console_only("console")
    if reader.fixture is not None and hasattr(reader.fixture, "restart"):
        return reader.fixture.restart()
    data = reader.nap_call("Process/Restart")
    # Process/Restart answers before the process is back; report what we saw.
    return {"ok": True, "data": data if isinstance(data, dict) else {},
            "note": "已请求原版重启；OneBot 端口由原启动脚本恢复，稍后刷新状态"}


# --------------------------------------------------------------------------- #
# Config import/export (whitelist only)
# --------------------------------------------------------------------------- #

_IMPORTABLE = ("host", "port", "loginRate", "disableWebUI", "enableXForwardedFor",
               "accessControlMode")


def export_config(reader):
    raw = reader.nap_call("WebUIConfig/GetConfig")
    return _whitelist_config(raw)


def _whitelist_config(raw):
    out = {}
    src = raw if isinstance(raw, dict) else {}
    for k in CONFIG_KEYS:
        if k in src and isinstance(src[k], (str, int, bool)) and len(str(src[k])) < 120:
            out[k] = src[k]
    return out


def build_import_patch(payload):
    """Turn a user-supplied JSON object into a whitelisted patch, or refuse."""
    if not isinstance(payload, dict):
        raise ToolError("导入内容须为 JSON 对象", 400, "bad_import")
    unknown = [k for k in payload if k not in _IMPORTABLE]
    if unknown:
        raise ToolError("导入内容含不允许的字段：%s" % "、".join(sorted(unknown)[:5]),
                        400, "bad_import")
    patch = {}
    for k, v in payload.items():
        if k == "port":
            if isinstance(v, bool) or not isinstance(v, int) or not 1 <= v <= 65535:
                raise ToolError("port 须为 1–65535 的整数", 400, "bad_import")
            patch[k] = v
        elif k == "loginRate":
            if isinstance(v, bool) or not isinstance(v, int) or not 1 <= v <= 100:
                raise ToolError("loginRate 须为 1–100 的整数", 400, "bad_import")
            patch[k] = v
        elif k in ("disableWebUI", "enableXForwardedFor"):
            if not isinstance(v, bool):
                raise ToolError("%s 须为 true/false" % k, 400, "bad_import")
            patch[k] = v
        elif k == "host":
            if not isinstance(v, str) or not re.fullmatch(r"[0-9a-zA-Z.:_-]{1,64}", v):
                raise ToolError("host 不合法", 400, "bad_import")
            patch[k] = v
        elif k == "accessControlMode":
            if not isinstance(v, str) or len(v) > 32:
                raise ToolError("accessControlMode 不合法", 400, "bad_import")
            patch[k] = v
    if not patch:
        raise ToolError("导入内容没有可用字段", 400, "bad_import")
    return patch


# --------------------------------------------------------------------------- #
# Read-only file browser (fixed roots)
# --------------------------------------------------------------------------- #


def safe_join(root, rel, allow_suffix=(".json", ".log", ".txt", ".conf", ".yaml", ".yml")):
    """Resolve `rel` under `root`, refusing traversal and non-allowlisted files."""
    if not isinstance(rel, (str, bytes, os.PathLike)):
        raise ToolError("路径不合法", 400, "bad_path")
    text = str(rel or "")
    if text.startswith("/") or text.startswith("\\") or ".." in text.split("/"):
        raise ToolError("路径不合法", 400, "bad_path")
    base = Path(root).resolve()
    target = (base / text).resolve() if text else base
    try:
        target.relative_to(base)
    except ValueError:
        raise ToolError("路径不合法", 400, "bad_path") from None
    if target.is_file() and target.suffix.lower() not in allow_suffix:
        raise ToolError("这个类型的文件不开放浏览", 400, "bad_type")
    return target


def list_dir(root, rel):
    target = safe_join(root, rel)
    if not target.is_dir():
        raise ToolError("目录不存在", 404, "not_found")
    entries = []
    for item in sorted(target.iterdir(), key=lambda p: (p.is_file(), p.name))[:200]:
        try:
            stat = item.stat()
        except OSError:
            continue
        is_file = item.is_file()
        entries.append({
            "name": item.name,
            "dir": not is_file,
            "size": int(stat.st_size) if is_file else None,
            "mtime": int(stat.st_mtime),
            "browsable": (not is_file) or item.suffix.lower() in (
                ".json", ".log", ".txt", ".conf", ".yaml", ".yml"),
        })
    return entries


def read_file(root, rel, limit=200_000):
    target = safe_join(root, rel)
    if not target.is_file():
        raise ToolError("文件不存在", 404, "not_found")
    try:
        with target.open("rb") as stream:
            data = stream.read(limit + 1)
    except OSError as e:
        raise ToolError("文件不可读", 502, "unreadable") from e
    text = data[:limit].decode("utf-8", errors="replace")
    if target.suffix.lower() == ".json":
        # JSON is shown redacted: any token-looking key becomes a boolean.
        try:
            obj = json.loads(text)
            text = json.dumps(_redact_obj(obj), ensure_ascii=False, indent=2)
        except ValueError:
            raise ToolError("无法安全显示不完整或无效的 JSON 文件", 400, "bad_json") from None
    else:
        text = "\n".join(redact_tail(text, limit_lines=200)["lines"])
    return {"path": str(rel), "text": text[:limit], "truncated": len(data) > limit}


def _redact_obj(obj, depth=0):
    if depth > 4:
        return "<deep>"
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            low = str(k).lower()
            if any(w in low for w in ("token", "secret", "password", "cookie", "credential")):
                out[k] = "［已隐去］"
            else:
                out[k] = _redact_obj(v, depth + 1)
        return out
    if isinstance(obj, list):
        return [_redact_obj(x, depth + 1) for x in obj[:50]]
    if isinstance(obj, str) and len(obj) > 2000:
        return obj[:2000] + "…"
    return obj


# --------------------------------------------------------------------------- #
# Log tail + redaction
# --------------------------------------------------------------------------- #

_REDACT_MARKERS = (
    "token", "authorization", "bearer", "cookie", "password", "passwd",
    "secret", "apikey", "api_key", "x-model-control", "qrcode", "pngbase64",
    "credential", "sk-", "uin=",
)


def _tail_text(path, lines):
    size = Path(path).stat().st_size
    chunk = min(size, 64 * 1024)
    with open(path, "rb") as fh:
        fh.seek(size - chunk)
        data = fh.read(chunk)
    text = data.decode("utf-8", errors="replace")
    return "\n".join(text.splitlines()[-max(1, int(lines)):])


def redact_tail(text, limit_lines=80):
    """Whole-line redaction: partial masking of a secret is still a leak."""
    if not isinstance(text, str):
        return {"lines": [], "truncated": False, "redacted": 0}
    rows = text.splitlines()
    truncated = len(rows) > limit_lines
    rows = rows[-max(1, int(limit_lines)):]
    out, redacted = [], 0
    for row in rows:
        low = row.lower()
        if any(m in low for m in _REDACT_MARKERS) or len(row) > 2000:
            out.append("［含敏感内容，已隐去］")
            redacted += 1
            continue
        out.append(row[:600])
    return {"lines": out, "truncated": truncated, "redacted": redacted}
