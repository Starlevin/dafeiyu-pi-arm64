#!/usr/bin/env python3
"""QQ 空间（qq-qzone）模块在统一控制台上的路由层 —— 纯标准库、叠加式接入。

挂载方式（由 unified_console.py 的维护者做，本文件不自己挂）：

    import tool_routes_qzone                      # 1) 顶层导入一次
    ...
    if tool_routes_qzone.resolve(self, "GET", path):     # 2) 放在 404 之前
        return

设计约束（与 tool_routes.py 保持同一套语义）
------------------------------------------
* 固定动词 + 固定路径，`_ROUTES` 就是全部表面，绝不透传任意 URL。
* 所有路由先检查控制台 owner 会话；刷新还要求 `confirm: true`。
* GET 只读本地快照；POST 只执行限频的好友动态读取，不提供发布、点赞、评论或配置写入。
* 返回形状固定：`{"ok": true, "data": {...}}` / `{"ok": false, "error": {"code", "message"}}`。
  `message` 一律是短中文说明，绝不携带 traceback / token / QQ 号明文。
* qzone_core / qzone_store / qzone_service 全部**延迟导入**：模块没装好时
  pi-console 仍能正常启动，只是这几条路由回答 501 not_found（"未部署"）。
* `resolve()` 绝不向 ThreadingHTTPServer 抛异常；每条失败路径都落在 JSON 里。
"""

from __future__ import annotations

import logging
import os
import re as _re
import threading
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import tool_routes

log = logging.getLogger("dafeiyu.qzone_routes")

PREFIX = "/api/qzone/"
ENV_STATE_HOME = "DAFEIYU_QZONE_STATE_HOME"

# (method, path) -> 处理函数名。这张表就是全部表面。
_ROUTES = {
    ("GET", "/api/qzone/latest"): "get_latest",
    ("POST", "/api/qzone/refresh"): "post_refresh",
}

# 当前只读接口的稳定错误码。
ERROR_CODES = ("owner_unauth", "disabled", "quiet_hours", "daily_limit",
               "min_interval", "busy", "bad_request", "upstream_error", "not_found")
_CODES = frozenset(ERROR_CODES)

# 路由错误码到默认 HTTP 状态。
_STATUS_BY_CODE = {
    "owner_unauth": 401,
    "disabled": 409,
    "quiet_hours": 409,
    "daily_limit": 429,
    "min_interval": 429,
    "busy": 409,
    "bad_request": 400,
    "upstream_error": 502,
    "not_found": 404,
}

# 回执里的凭据字段一律不出现（即使 core 的 redact 漏了也在这里兜住）。
_SECRET_KEY_PARTS = (
    "token", "secret", "passwd", "password", "cookie", "authorization",
    "access_token", "credential", "skey", "cookie_str", "uin_plain",
)

# 未部署时给的短中文说明（同一句话，避免不同路由口径不一致）。
_NOT_DEPLOYED = "QQ 空间模块尚未部署到本控制台"

# --------------------------------------------------------------------------- #
# 依赖注入
# --------------------------------------------------------------------------- #

_injection = {
    "service_factory": None,   # (state_dir) -> service-like
    "clock": None,             # () -> epoch int
    "owner_state": None,       # dict，测试里放 {"authenticated": True}
}
_cache = {"key": None, "service": None}
_cache_lock = threading.Lock()

def configure(*, service_factory=None, clock=None, owner_state=None):
    """注入替身。不传的关键字保持原状，`None` 表示"回到真实实现"。

    测试用法：
        configure(service_factory=lambda state_dir: FakeService(),
                  owner_state={"authenticated": True})
    收工时 `reset()` 清空全部注入。
    """
    if service_factory is not None:
        _injection["service_factory"] = service_factory
    _injection["clock"] = clock
    if owner_state is not None:
        _injection["owner_state"] = owner_state
    with _cache_lock:
        _cache["key"] = None
        _cache["service"] = None

def reset():
    """清空注入与缓存的 service（测试 tearDown 用）。"""
    _injection["service_factory"] = None
    _injection["clock"] = None
    _injection["owner_state"] = None
    with _cache_lock:
        _cache["key"] = None
        _cache["service"] = None

def routes():
    """可用 (method, path) 列表，给 capabilities 汇总用。"""
    return sorted(_ROUTES)

# --------------------------------------------------------------------------- #
# 状态目录 / 时区
# --------------------------------------------------------------------------- #

def state_home(handler=None):
    """qzone 状态目录：注入 > DAFEIYU_QZONE_STATE_HOME > <默认状态目录>/qzone。

    不硬编码任何生产路径；默认根目录从 App.state_dir 拿（App 自己就来自
    default_state_dir()，可被 DAFEIYU_CONSOLE_STATE_HOME 覆盖）。
    """
    env = str(os.environ.get(ENV_STATE_HOME) or "").strip()
    if env:
        return Path(env).expanduser()
    app = getattr(handler, "app", None) if handler is not None else None
    root = getattr(app, "state_dir", None) if app is not None else None
    if root is None:
        import unified_console as uc                       # 仅兜底，延迟导入
        root = uc.default_state_dir()
    return Path(root).expanduser() / "qzone"

def _default_clock():
    """北京时间 epoch 秒（契约 §2：固定 UTC+8，不读系统时区）。"""
    import time as _time
    return int(_time.time())

# --------------------------------------------------------------------------- #
# 依赖包（qzone_core / qzone_service）—— 延迟、可缺
# --------------------------------------------------------------------------- #

def _import_qzone():
    """返回 (qzone_core, qzone_service)；任一侧缺失都抛 ImportError。"""
    try:
        import qzone_core       # type: ignore
        import qzone_service    # type: ignore
    except ImportError:
        import sys
        extra = Path(__file__).resolve().parents[2] / "qq-qzone"
        if extra.is_dir() and str(extra) not in sys.path:
            sys.path.insert(0, str(extra))
        import qzone_core       # type: ignore
        import qzone_service    # type: ignore
    return qzone_core, qzone_service

def _loopback_endpoint(app=None):
    """解析 OneBot 端点，**默认 fail closed**。

    只接受 loopback：主机必须是 127.0.0.1 / ::1 / localhost。任何非本机地址
    （包括局域网、域名、公网 IP）一律视为未配置——这是「只在明确配置的本机
    OneBot 可用时才发请求」的落地点，避免误连到别处。

    来源优先级（都只认已有的约定，不新建全局配置文件）：
      1. 环境变量 ``DAFEIYU_QZONE_ONEBOT_URL``（本模块专用，最高优先级）
      2. 环境变量 ``DAFEIYU_ONEBOT_URL``（与 pi-console 其它模块一致的通用名）
      3. 稳定本地 ini ``<state_home>/../qzone.ini`` 的 ``[onebot] url = ...``
    返回 ``(base_url|None, source)``；``base_url`` 为 None 表示未配置。
    """
    candidates = [
        ("env:DAFEIYU_QZONE_ONEBOT_URL", os.environ.get("DAFEIYU_QZONE_ONEBOT_URL")),
        ("env:DAFEIYU_ONEBOT_URL", os.environ.get("DAFEIYU_ONEBOT_URL")),
    ]
    for source, raw in candidates:
        text = str(raw or "").strip()
        if text:
            return (_loopback_only(text), source)

    ini = _qzone_ini_path(app)
    if ini is not None and ini.is_file():
        import configparser

        parser = configparser.ConfigParser()
        try:
            parser.read(ini, encoding="utf-8")
            text = str(parser.get("onebot", "url", fallback="") or "").strip()
        except (configparser.Error, OSError, UnicodeDecodeError):
            text = ""
        if text:
            return (_loopback_only(text), "ini:%s" % ini.name)
    return (None, "unconfigured")

def _loopback_only(url):
    """非 loopback 返回 None（fail closed），loopback 规范化后返回。

    同时接受 HTTP（默认，向后兼容）与 **WebSocket** 快照端点：生产 NapCat 只开
    ``ws://127.0.0.1:<port>``，而既有部署用 ``http://127.0.0.1:<port>``。scheme 决定
    之后造哪种 transport（见 ``qzone_napcat.resolve_endpoint``），这里只做地址闸门。
    """
    from urllib.parse import urlsplit

    parsed = urlsplit(url)
    if parsed.scheme not in ("http", "https", "ws", "wss") or not parsed.port:
        return None
    host = (parsed.hostname or "").lower()
    if host not in ("127.0.0.1", "::1", "localhost"):
        return None
    return url

def _qzone_ini_path(app=None):
    """稳定本地 ini 位置：$DAFEIYU_QZONE_STATE_HOME/../qzone.ini。"""
    try:
        home = state_home(_FakeHandler(app) if app is not None else None)
    except Exception:                                       # noqa: BLE001
        return None
    return home.parent / "qzone.ini"

class _FakeHandler:
    """仅用于让 state_home 取到 App 的 state_dir。"""

    def __init__(self, app):
        self.app = app

def _service(handler):
    """取（并缓存）本次使用的 service。注入的 factory 优先。"""
    factory = _injection["service_factory"]
    clock = _injection["clock"]
    if not callable(clock):
        clock = _default_clock
    owner = _injection["owner_state"]
    owner_auth = bool(owner.get("authenticated")) if isinstance(owner, dict) else False
    home = state_home(handler)
    key = (str(home), id(factory), id(clock), owner_auth)
    with _cache_lock:
        if _cache["key"] == key and _cache["service"] is not None:
            return _cache["service"]
    if factory is not None:
        svc = factory(home)
    else:
        core, service_mod = _import_qzone()
        # Reads do not need an account identity. Use the service's documented
        # impossible placeholder so its validated config can be constructed
        # without loading or exposing any owner credential.
        cfg = core.QzoneConfig.from_dict({"owner_uin": "00000"})
        endpoint, _source = _loopback_endpoint(getattr(handler, "app", None))
        access_token = ""
        try:
            # Read the token from the same private NapCat secrets directory used
            # by QQ management. The value stays in process memory and is never
            # returned or logged.
            reader = tool_routes.box(handler.app, tool_routes.app_cfg(handler))
            if reader.nap_secrets is not None:
                access_token = tool_routes.nt._secret(reader.nap_secrets / "onebot.token")
        except Exception:  # missing/unreadable secrets fail closed at transport auth
            access_token = ""
        # endpoint 为 None 时不传 base_url，service 的 has_transport() 即为 False，
        # 好友动态刷新会按 fail closed 直接拒绝，不发任何请求。
        svc = service_mod.QzoneService(
            state_dir=home, cfg=cfg, transport=None, clock=clock,
            owner_authenticated=owner_auth or _handler_owner_authed(handler),
            onebot_base_url=endpoint, onebot_access_token=access_token)
    with _cache_lock:
        _cache["key"] = key
        _cache["service"] = svc
    return svc

def _handler_owner_authed(handler):
    """真实运行时：控制台会话有效即视为主人已登录。

    `handler.authed()` 由 unified_console 提供；单元/集成测试里的假 handler
    可能只有 `owner_authenticated` 属性，按两者之一判定。
    """
    auth = getattr(handler, "authed", None)
    if callable(auth):
        try:
            return bool(auth())
        except Exception:                                   # noqa: BLE001
            return False
    return bool(getattr(handler, "owner_authenticated", False))

# --------------------------------------------------------------------------- #
# 回包小工具
# --------------------------------------------------------------------------- #

class RouteError(Exception):
    """内部异常：统一翻译成 {"ok":false,"error":{code,message}}。"""

    def __init__(self, code, message, status=None):
        super().__init__(message)
        self.code = code if code in _CODES else "upstream_error"
        self.message = message or _DEFAULT_MESSAGE.get(self.code, "操作未完成")
        self.status = int(status or _STATUS_BY_CODE.get(self.code, 400))

_DEFAULT_MESSAGE = {
    "owner_unauth": "请先在控制台登录后再操作",
    "disabled": "好友动态读取当前不可用",
    "quiet_hours": "现在处于夜间静默时段",
    "daily_limit": "今天的读取额度已经用完",
    "min_interval": "距上次读取太近，请稍后再试",
    "busy": "上一次读取还没结束，请稍后再试",
    "bad_request": "请求参数不合法",
    "upstream_error": "QQ 空间接口暂时不可用",
    "not_found": "没有找到对应记录",
}

def _fail(handler, code, message=None, status=None):
    err = RouteError(code, message, status)
    _emit(handler, err.status, {"ok": False,
                                "error": {"code": err.code,
                                          "message": _safe_text(err.message)}})

def _ok(handler, data, status=200):
    payload = {"ok": True, "data": _sanitize(data)}
    _emit(handler, status, payload)

def _emit(handler, status, payload):
    """只走 handler.json()；没有它就退化到 _send()（兼容极简假 handler）。"""
    sender = getattr(handler, "json", None)
    if callable(sender):
        sender(status, payload)
        return
    import json as _json
    raw = _json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler._send(status, raw, "application/json; charset=utf-8")   # noqa: SLF001

def _safe_text(value, limit=160):
    text = str(value or "").strip()
    if len(text) > limit:
        text = text[:limit] + "…"
    return text

class _Secret(str):
    """标记：明知是凭据的文本，`_sanitize` 必须整段丢掉（仅供测试注入）。

    真实代码不会产生它；它的作用是让"凭据不得出现在回执里"这件事可以被
    断言，而不是指望 core 的正则刚好命中。
    """

    __slots__ = ()

def _secret_text(value, limit):
    """内部通道：与 _safe_text 相同，但不做任何取舍（用于先脱敏再截断）。"""
    return (value if isinstance(value, str) else str(value or "")).strip()[:limit]

_CRED_PATTERNS = (
    (_re.compile(r"(?i)(authorization)\s*[:=]\s*\S+"), r"\1=［已隐去］"),
    (_re.compile(r"(?i)(access_token|refresh_token|token|secret|password|passwd)"
                 r"\s*[:=]\s*\S+"), r"\1=［已隐去］"),
    (_re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._\-]{4,}"), "Bearer ［已隐去］"),
    (_re.compile(r"(?i)\bCookie\s*[:=]\s*\S+"), "Cookie=［已隐去］"),
    (_re.compile(r"\bsk-[A-Za-z0-9_\-]{4,}"), "sk-［已隐去］"),
)

def _scrub_credentials(text):
    """本模块自己的兜底：core 缺席或漏掉时，凭据也不能漏出去。"""
    out = text
    for pattern, repl in _CRED_PATTERNS:
        out = pattern.sub(repl, out)
    return out

def _redact(text):
    """文本级脱敏：core.redact 优先，其后必定再过一次本模块的凭据清理。"""
    raw = _secret_text(text, 400)
    if not raw:
        return ""
    try:
        core, _ = _import_qzone()
        out = core.redact(raw)
        if isinstance(out, str):
            raw = out
    except Exception:                                       # noqa: BLE001
        pass
    return _safe_text(_scrub_credentials(raw), 200)

def _sanitize(value, depth=0):
    """递归脱敏：凭据字段丢掉；长数字打码；其余字符串过 _redact。"""
    if isinstance(value, _Secret):
        return "［已隐去］"
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, (bytes, bytearray)):
        return "［二进制已省略］"
    if depth > 8:
        return None
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            name = _secret_text(key, 120)
            if any(part in name.lower() for part in _SECRET_KEY_PARTS):
                continue
            out[name] = _sanitize(item, depth + 1)
        return out
    if isinstance(value, (list, tuple, set)):
        return [_sanitize(v, depth + 1) for v in list(value)[:200]]
    return _mask_long_digits(_redact(_secret_text(value, 2000)))

def _mask_long_digits(text):
    """6 位以上连续数字（QQ 号 / tid）保留后 4 位。"""
    out = []
    run = []

    def flush():
        if not run:
            return
        blob = "".join(run)
        if len(blob) >= 6:
            out.append("*" * (len(blob) - 4) + blob[-4:])
        else:
            out.append(blob)
        run.clear()

    for ch in text:
        if ch.isdigit():
            run.append(ch)
        else:
            flush()
            out.append(ch)
    flush()
    return "".join(out)

def _shape(value):
    """把 service 的返回归一成 dict（允许对象 / None / 裸 dict）。"""
    if value is None:
        return {}
    if isinstance(value, dict):
        return dict(value)
    for attr in ("to_dict", "as_dict"):
        fn = getattr(value, attr, None)
        if callable(fn):
            try:
                out = fn()
                if isinstance(out, dict):
                    return dict(out)
            except Exception:                               # noqa: BLE001
                break
    data = getattr(value, "data", None)
    if isinstance(data, dict):
        return dict(data)
    for attr in ("__dict__",):
        raw = getattr(value, attr, None)
        if isinstance(raw, dict):
            return {k: v for k, v in raw.items() if not k.startswith("_")}
    return {"value": value}


def _raise_service_error(payload):
    """Surface read-refresh refusals as HTTP errors, not false route success."""
    if payload.get("ok") is not False:
        return
    block = payload.get("error") if isinstance(payload.get("error"), dict) else {}
    code = str(block.get("code") or payload.get("code") or "upstream_error")
    if code not in _CODES:
        code = "upstream_error"
    message = block.get("message") or payload.get("message")
    status = block.get("status") or payload.get("status")
    raise RouteError(code, _safe_text(message) if message else None,
                     status if isinstance(status, int) else None)


def _service_data(value):
    """Unwrap the service envelope once and propagate its refusal envelope."""
    payload = _shape(value)
    _raise_service_error(payload)
    data = payload.get("data")
    return dict(data) if payload.get("ok") is True and isinstance(data, dict) else payload

# --------------------------------------------------------------------------- #
# 认证 / 确认
# --------------------------------------------------------------------------- #

def _owner_authed(handler):
    owner = _injection["owner_state"]
    if isinstance(owner, dict) and "authenticated" in owner:
        return bool(owner.get("authenticated"))
    return _handler_owner_authed(handler)

def _require_owner(handler):
    if not _owner_authed(handler):
        raise RouteError("owner_unauth", _DEFAULT_MESSAGE["owner_unauth"])

def _require_confirm(body, action_id):
    """一次页面确认 = body.confirm === true（+ 可选的 action 校验）。"""
    if body.get("confirm") is not True:
        raise RouteError("bad_request", "这个操作需要你确认一次后再执行")
    action = body.get("action")
    if action not in (None, "") and action != action_id:
        raise RouteError("bad_request", "动作与接口不匹配")

def _read_body(handler):
    reader = getattr(handler, "read_json", None)
    if not callable(reader):
        return {}
    try:
        body = reader()
    except ValueError as exc:
        raise RouteError("bad_request", _safe_text(exc) or "请求内容无法解析") from None
    except Exception:                                       # noqa: BLE001
        raise RouteError("bad_request", "请求内容无法解析") from None
    if body is None:
        return {}
    if not isinstance(body, dict):
        raise RouteError("bad_request", "请求内容须为对象")
    return body

def _query(handler, limit_keys=8):
    raw = urlsplit(str(getattr(handler, "path", "") or "")).query
    if len(raw) > 512:
        raise RouteError("bad_request", "查询参数过长")
    try:
        return {k: (v[-1] if v else "")
                for k, v in parse_qs(raw, keep_blank_values=False,
                                     max_num_fields=limit_keys).items()}
    except ValueError:
        raise RouteError("bad_request", "查询参数不合法") from None

def _int_query(q, name, lo, hi, default):
    value = q.get(name)
    if value is None or value == "":
        return default
    try:
        n = int(str(value), 10)
    except (TypeError, ValueError):
        raise RouteError("bad_request", "%s 需要整数" % name) from None
    if not lo <= n <= hi:
        raise RouteError("bad_request", "%s 超出范围（%d–%d）" % (name, lo, hi))
    return n

# --------------------------------------------------------------------------- #
# 只读 GET 与手动刷新 POST
# --------------------------------------------------------------------------- #

def get_latest(handler):
    """最近一次好友动态只读刷新的快照。**不联网**。"""
    _require_owner(handler)
    q = _query(handler)
    limit = _int_query(q, "limit", 1, 20, 20)
    svc = _service(handler)
    if not hasattr(svc, "latest"):
        raise RouteError("not_found", "当前 service 不支持好友动态快照")
    data = _service_data(svc.latest(limit))
    data.setdefault("items", [])
    data.setdefault("total", 0)
    data.setdefault("sent", False)
    data.setdefault("consumesQuota", False)
    _ok(handler, data)

def post_refresh(handler):
    """一次性、主人认证的只读刷新（契约 §10）。"""
    _require_owner(handler)
    body = _read_body(handler)
    _require_confirm(body, "qzone.refresh")
    svc = _service(handler)
    if not hasattr(svc, "refresh_latest"):
        raise RouteError("not_found", "当前 service 不支持好友动态刷新")
    data = _service_data(
        svc.refresh_latest(owner_authenticated=True, confirm=True)
    )
    data.setdefault("sent", False)
    data.setdefault("writes", 0)
    _ok(handler, data)

# --------------------------------------------------------------------------- #
# 分发
# --------------------------------------------------------------------------- #

def resolve(handler, method, path):
    """返回 True 表示本模块已处理（无论成功失败都写完了响应）。"""
    if not isinstance(path, str) or not path.startswith(PREFIX):
        return False
    handler_name = _ROUTES.get((method, path))
    if handler_name is None:
        return False                      # 前缀相同但不是我们的表项 → 交回上游
    try:
        _dispatch(handler, handler_name)
    except RouteError as exc:
        _fail(handler, exc.code, exc.message, exc.status)
    except Exception as exc:                                # noqa: BLE001
        # 唯一允许落日志的位置：只记类型与短消息，且过 redact。
        log.warning("qzone route %s failed: %s", path, type(exc).__name__)
        _fail(handler, "upstream_error",
              _redact(getattr(exc, "message", "") or "") or _DEFAULT_MESSAGE["upstream_error"])
    return True

def _dispatch(handler, handler_name):
    fn = globals().get(handler_name)
    if fn is None:                                          # pragma: no cover
        raise RouteError("not_found", _NOT_DEPLOYED)
    try:
        fn(handler)
    except RouteError:
        raise
    except ImportError:
        raise RouteError("not_found", _NOT_DEPLOYED, 501) from None
    except FileNotFoundError:
        raise RouteError("not_found", _DEFAULT_MESSAGE["not_found"]) from None
