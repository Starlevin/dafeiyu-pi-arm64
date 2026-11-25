"""Pi integration adapters for 大肥鱼统一控制台 (stdlib only).

Design rules (from the deployment brief):
  * Production integration prefers the Pi's existing QQ Controller/API and the
    loopback WeChat control surface. Actual interfaces are taken from the live Pi.
  * Management keys are read by this process through the existing private-file
    interface only. They are never printed, never returned over the API, never
    copied into the static dir, and never written into any response.
  * No message text, no secrets, no key material, no cookies are ever surfaced.
  * Reads go to the real APIs. Writes go ONLY to a fixture in test mode
    (see `--fixture`); in live mode they call the same private HTTP endpoints
    the operator's own admin console uses. Deployment is left to the operator.
  * Each platform degrades independently: if QQ is down, WeChat still shows and
    vice versa. `connected: null` means "unavailable/unknown", not "false".

Verified live surface (read-only reconnaissance, 2026-11-11):
  QQ (model-hub, 127.0.0.1:18090, x-model-control admin key):
    GET  /api/panel   -> {connected,running,busy,model,targets[],contacts[]}
    GET  /api/status  -> {profiles[],engine:{...},route:{...}}
    GET  /api/limits  -> {default,ceiling,total,calls,used,sessions[]}
    POST /api/scope   {add,drop,enabled}                    (enable/disable)
    POST /api/limits  {target,value}                        (daily limit; 0=none)
    POST /api/profiles/activate {id}                       (model switch)
    QQ admin_core.Controller (private import): STYLES, FREQUENCY,
      proactive(), set_limit(), edit() + panel('setPersona'/'save').
  WeChat (core, 127.0.0.1:19301, loopback-only HTTP):
    GET  /api/status              -> {model,provider,gateways[],group{}}
    GET  /api/settings            -> {model,provider,providers{},group{}}
    GET  /api/persona             -> {persona:{sourcePreset,preset}}  (global style)
    GET  /api/persona/presets     -> {presets:[{id,name,...}]}
    POST /api/settings            {model|provider|group{}}  (model / group rules)
    POST /api/config              {patch:{...}}             (whitelist fields)
    POST /api/persona/preset      {id}                      (style)
    GET  /api/features            -> {features:[{key,path,label,...}]}
    The bridge service runs with `--group ... --admin-chat ...`; those titles
    are the WeChat conversations exposed by the console.
"""

from __future__ import annotations

import http.client
import json
import os
import re
import stat
import threading
import time
import urllib.error
import urllib.request
from copy import deepcopy
from pathlib import Path


def _p(value, default):
    """Resolve an optional config path, expanding a leading ~."""
    if not value:
        return Path(default).expanduser()
    return Path(str(value)).expanduser()


# --------------------------------------------------------------------------- #
# Constants mirrored from the live QQ admin command set (no secrets here).
# --------------------------------------------------------------------------- #

# admin_core.py STYLES — keys are the legal values for the "style" field.
QQ_STYLES = [
    {"id": "teasing", "name": "雌小鬼"},
    {"id": "gentle", "name": "温柔"},
    {"id": "tsundere", "name": "傲娇"},
    {"id": "energetic", "name": "元气"},
    {"id": "calm", "name": "理性"},
    {"id": "butler", "name": "管家"},
    {"id": "catgirl", "name": "猫娘"},
    {"id": "tieba", "name": "贴吧"},
    {"id": "abstract", "name": "抽象"},
    {"id": "drama", "name": "戏精"},
    {"id": "custom", "name": "自定义"},
]

# admin_core.py FREQUENCY — legal values for the "frequency" field.
# "off" is a real command but is not part of the console's frequencyOptions
# (the console only offers quiet/natural/active); it is still accepted on write
# when explicitly supplied so existing configuration is not destroyed.
QQ_FREQUENCY = [
    {"id": "quiet", "name": "最低"},
    {"id": "natural", "name": "自然"},
    {"id": "active", "name": "活跃"},
]

# Console-wide options (union of what either platform can really do).
FREQUENCY_OPTIONS = [
    {"id": "quiet", "name": "最低 / 安静", "platforms": ["qq", "wechat"]},
    {"id": "natural", "name": "自然", "platforms": ["qq", "wechat"]},
    {"id": "active", "name": "活跃", "platforms": ["qq", "wechat"]},
]

LIMIT_OPTIONS = [
    {"id": "0", "name": "不限制（0）", "platforms": ["qq"]},
    {"id": "50", "name": "50 条/天", "platforms": ["qq"]},
    {"id": "100", "name": "100 条/天", "platforms": ["qq"]},
    {"id": "200", "name": "200 条/天", "platforms": ["qq"]},
]


class AdapterError(Exception):
    """User-facing Chinese reason for a failed operation."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status
        self.message = message


# --------------------------------------------------------------------------- #
# Small private HTTP helper. Keys are read from disk, attached to a request,
# and never leave this process. No URL with a key is ever logged.
# --------------------------------------------------------------------------- #

def _http_json(url, method="GET", body=None, headers=None, timeout=6):
    data = json.dumps(body, ensure_ascii=False).encode() if body is not None else None
    hdrs = {"Content-Type": "application/json"}
    if headers:
        hdrs.update(headers)
    req = urllib.request.Request(url, data=data, method=method, headers=hdrs)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read(4_000_000)
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        # Deliberately discard the upstream body: it can contain internal detail.
        raise AdapterError("后台控制接口暂时不可用，请稍后再试", 502) from e
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise AdapterError("后台控制接口暂时不可用，请稍后再试", 502) from e


def _read_secret(path: Path) -> str:
    """Read a private management key. Never logged, never returned."""
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError as e:
        raise AdapterError("后台凭据不可读，请联系管理员", 502) from e


def _now():
    """Epoch second of a status read, or None when the clock is unusable."""
    try:
        return time.time()
    except Exception:  # pragma: no cover - defensive only
        return None


# --------------------------------------------------------------------------- #
# QQ adapter
# --------------------------------------------------------------------------- #

class QQAdapter:
    """Talks to the existing model-hub admin API + qq-admin Controller."""

    # Default QQ client name. This is NOT a probe: the hub's /api/panel does
    # not report which client is attached, so the console only names it when
    # the operator configured it (qqClientName) or a read actually reported it.
    DEFAULT_CLIENT = "NapCat"
    CONTROL_ENTRY = re.compile(
        rb"(?m)^QQ_CONTROL_URL=http://127\.0\.0\.1:([0-9]{1,5})/#([0-9A-Za-z_-]{32,128})\r?$"
    )

    def __init__(self, cfg, fixture=None):
        self.cfg = cfg
        self.fixture = fixture
        self.base = cfg.get("qqHubUrl", "http://127.0.0.1:18090")
        root = cfg.get("qqRoot") or os.environ.get("DAFEIYU_QQ_ROOT")
        self.root = Path(str(root)).expanduser() if root else None
        # Explicit operator setting: "" / null / whitespace all mean "don't label".
        configured = cfg.get("qqClientName")
        self.client_name = str(configured).strip() if configured is not None else ""
        # Each overview shares its reads within its own HTTP handler thread.
        self._request_cache = threading.local()
        self._controller = None

    # ---- shared reads ------------------------------------------------------ #

    @property
    def _cache(self):
        return getattr(self._request_cache, "value", None)

    @_cache.setter
    def _cache(self, value):
        self._request_cache.value = value

    def _read(self, path):
        """One hub read. In fixture mode a shallow copy is returned so a later
        fixture mutation can never alias an object already handed to a caller."""
        data = self._get(path)
        if self.fixture is not None and isinstance(data, dict):
            return deepcopy(data)
        return data

    def _panel(self):
        if self._cache is not None:
            return self._cache["panel"]
        panel, err = None, False
        try:
            panel = self._read("/api/panel")
            if not isinstance(panel, dict):
                panel, err = None, True
        except AdapterError:
            err = True
        self._cache = {"panel": panel, "panelFailed": err, "status": None,
                       "statusFailed": False}
        return panel

    def _status_payload(self):
        """Second of the two same-origin reads. Never raises; None on failure."""
        if self._cache is None:
            self._panel()
        if self._cache["status"] is not None or self._cache["statusFailed"]:
            return self._cache["status"]
        try:
            self._cache["status"] = self._read("/api/status")
            self._cache["statusFailed"] = self._cache["status"] is None
        except AdapterError:
            self._cache["status"] = None
            self._cache["statusFailed"] = True
        return self._cache["status"]

    def _cache_clear(self):
        self._cache = None

    def _key(self):
        if self.fixture is not None:
            return "fixture"
        # Existing private interface: the model-hub admin key. Read only, never printed.
        if self.root is None:
            raise AdapterError("QQ 数据目录未配置", 503)
        return _read_secret(self.root / "secrets/model-hub/admin.key")

    def _get(self, path):
        if self.fixture is not None:
            return self.fixture.get("qq", {}).get(path)
        origin = self.base
        headers = {
            "x-model-control": self._key(),
            "Origin": origin,
            "Host": origin.split("//", 1)[-1],
        }
        return _http_json(origin + path, "GET", headers=headers)

    def _post(self, path, body):
        if self.fixture is not None:
            return self.fixture.get("qq", {}).get("post:" + path, {"ok": True})
        origin = self.base
        headers = {
            "x-model-control": self._key(),
            "Origin": origin,
            "Host": origin.split("//", 1)[-1],
        }
        return _http_json(origin + path, "POST", body=body, headers=headers, timeout=45)

    def _controller_instance(self):
        """Lazily import the existing QQ admin Controller (live mode only)."""
        if self._controller is not None:
            return self._controller
        if self.fixture is not None:
            return None
        if self.root is None:
            raise AdapterError("QQ 数据目录未配置", 503)
        import importlib.util
        import sys
        p = self.root / "qq-admin/admin_core.py"
        if not p.exists():
            raise AdapterError("QQ 管理模块不存在", 502)
        # admin_core.py imports its sibling `limits` module by bare name; the
        # qq-admin dir must be importable. Add it without changing the process
        # cwd and without printing any path (paths here are not secret, but we
        # keep the rule of not leaking internals into responses/logs).
        qdir = str(p.parent)
        if qdir not in sys.path:
            sys.path.insert(0, qdir)
        spec = importlib.util.spec_from_file_location("unified_admin_core", str(p))
        mod = importlib.util.module_from_spec(spec)
        try:
            spec.loader.exec_module(mod)
        except Exception as e:
            raise AdapterError("QQ 管理模块不可加载", 502) from e
        self._controller = mod.Controller(self.root)
        return self._controller

    def group_reply_panel(self):
        """Read local QQ status, including on installations without qq-admin."""
        try:
            controller = self._controller_instance()
            if controller is not None:
                return controller.state()
        except AdapterError:
            pass
        if self.fixture is not None or self.root is None or os.name != "posix":
            return None
        path = self.root / "run/qq-bot.log"
        with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), "rb") as log:
            metadata = os.fstat(log.fileno())
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid() or metadata.st_mode & 0o077:
                return None
            limit = 16_384
            entries = self.CONTROL_ENTRY.findall(log.read(limit))
            if metadata.st_size > limit:
                log.seek(max(limit, metadata.st_size - limit))
                entries.extend(self.CONTROL_ENTRY.findall(log.read(limit)))
        if not entries:
            return None
        raw_port, token = entries[-1]
        port = int(raw_port)
        if not 1 <= port <= 65535:
            return None
        # A direct loopback connection never forwards the control header to a
        # proxy or follows a redirect to another endpoint.
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
        try:
            connection.request("GET", "/api/status", headers={
                "Origin": f"http://127.0.0.1:{port}", "x-qq-control": token.decode("ascii"),
            })
            response = connection.getresponse()
            if response.status != 200:
                return None
            limit = 4_000_000
            body = response.read(limit + 1)
            return json.loads(body) if len(body) <= limit else None
        finally:
            connection.close()

    # ---- overview ---------------------------------------------------------- #

    @staticmethod
    def _pick(obj, *keys):
        """First non-empty string value among `keys`, walking dotted paths."""
        for key in keys:
            cur = obj
            for part in key.split("."):
                cur = cur.get(part) if isinstance(cur, dict) else None
            if isinstance(cur, str) and cur.strip():
                return cur.strip()
        return ""

    def _client(self, panel):
        """Which QQ client is attached.

        The hub's /api/panel reports no client field today, so the value is
        (1) whatever a read actually reports, else (2) the operator's
        `qqClientName` config, else (3) empty — an unknown client is never
        invented. `qqClientName: ""` explicitly means "do not label it".
        """
        reported = self._pick(panel if isinstance(panel, dict) else {},
                              "client", "clientName", "engine.client",
                              "engine.clientName", "adapter", "onebot")
        if reported:
            return reported, "reported"
        if self.client_name:
            return self.client_name, "config"
        return "", "unknown"

    def status(self):
        """Return (qq_dict, diagnostics list). qq.connected may be None.

        Shape: {connected, running, model, client, clientSource, lastRead}
        `lastRead` is the epoch second of a successful read, or null on failure.
        Backwards compatibility: existing fields keep
        their names and types; new fields are additive.
        """
        diags = []
        last_read = _now()
        empty = {"connected": None, "running": None, "model": "",
                 "client": self.client_name,
                 "clientSource": "config" if self.client_name else "unknown",
                 "lastRead": None}
        panel = self._panel()
        if self._cache and self._cache.get("panelFailed"):
            diags.append({"platform": "qq", "level": "warning",
                          "message": "QQ 控制接口暂不可用，QQ 相关设置已隐藏"})
            return empty, diags

        client, source = self._client(panel)
        st = self._status_payload()
        model = ""
        if isinstance(st, dict):
            model = self._pick(st, "engine.model", "route.active")
        if not model and isinstance(panel, dict):
            model = self._pick(panel, "model")

        return {
            "connected": panel.get("connected") if isinstance(panel.get("connected"), bool) else None,
            "running": panel.get("running") if isinstance(panel.get("running"), bool) else None,
            "model": model,
            "client": client,
            "clientSource": source,
            "lastRead": last_read,
        }, diags

    def models(self):
        st = self._status_payload()
        out = []
        for p in (st or {}).get("profiles", []) if isinstance(st, dict) else []:
            if not isinstance(p, dict):
                continue
            pid = str(p.get("id") or "").strip()
            if not pid:
                continue
            out.append({"id": pid, "name": str(p.get("name") or pid),
                        "platforms": ["qq"]})
        return out

    def conversations(self):
        try:
            panel = self._panel()
            limits = self._get("/api/limits")
        except AdapterError:
            return []
        panel = panel or {}
        limits = limits or {}
        # Per-session daily limits (key -> int, 0 = unlimited).
        limit_map = {}
        for row in limits.get("sessions", []) or []:
            if isinstance(row, dict) and row.get("key"):
                limit_map[str(row["key"])] = row.get("limit")
        # Per-group proactive frequency from the same private config the admin
        # command uses. Read-only here; values are validated but never emitted
        # beyond the console's own frequency ids.
        freq = self._frequencies()
        # Per-conversation persona styles live in the QQ engine's config.targets,
        # which the hub's /api/panel does not include. Read it through the same
        # private Controller state() the admin command uses; degrade to null if
        # the engine is busy/unavailable (never block the whole overview).
        styles = self._persona_styles()
        out = []
        for t in panel.get("targets", []) or []:
            if not isinstance(t, dict):
                continue
            key = str(t.get("key") or "")
            is_group = bool(t.get("group"))
            number = str(t.get("number") or (key.split(":", 1)[-1] if ":" in key else key))
            name = str(t.get("name") or "") or number
            frequency = freq.get(number) if is_group else None
            style = styles.get(key)  # may be absent -> null (未配置)
            out.append({
                "platform": "qq",
                "id": key,
                "name": name,
                "kind": "group" if is_group else "private",
                "enabled": bool(t.get("enabled")),
                "style": style if isinstance(style, str) and style else None,
                "frequency": frequency,
                "limit": limit_map.get(key),
                # Only real, writable features are marked capable.
                "capabilities": {
                    "enabled": True,       # /api/scope
                    "style": True,         # panel setPersona / style command
                    "frequency": is_group, # proactive-groups.conf (groups only)
                    "limit": True,         # /api/limits
                },
            })
        return out

    def _persona_styles(self):
        """Read per-conversation personaStyle via the private Controller.state().

        Returns {key: style_id}. Best-effort: an unavailable engine yields {}
        so every conversation still shows a null (未配置) style instead of
        taking the whole overview down. Never emits prompts or secrets.
        """
        if self.fixture is not None:
            out = {}
            for t in (self.fixture.get("qq", {}).get("/api/panel") or {}).get("targets", []):
                if isinstance(t, dict) and t.get("personaStyle"):
                    out[str(t.get("key"))] = str(t["personaStyle"])
            return out
        try:
            ctl = self._controller_instance()
            if ctl is None:
                return {}
            state = ctl.state()
            result = {}
            for t in (state.get("config") or {}).get("targets", []) or []:
                if not isinstance(t, dict):
                    continue
                key = ("group:" if t.get("group") else "private:") + str(t.get("number"))
                style = t.get("personaStyle")
                if isinstance(style, str) and style:
                    result[key] = style
            return result
        except Exception:
            return {}

    def _frequencies(self):
        """Read proactive group modes. Never returns secrets."""
        if self.fixture is not None:
            raw = self.fixture.get("qq", {}).get("proactive", "")
        else:
            configured = (self.cfg.get("qqProactiveFile")
                          or os.environ.get("DAFEIYU_QQ_PROACTIVE_FILE"))
            if not configured:
                return {}
            p = Path(str(configured)).expanduser()
            try:
                raw = p.read_text(encoding="utf-8").strip()
            except OSError:
                return {}
        result = {}
        for item in raw.split(",") if raw else []:
            pair = item.split("=")
            if (len(pair) == 2 and re.fullmatch(r"[1-9][0-9]{0,17}", pair[0])
                    and pair[1] in ("quiet", "natural", "active")):
                result[pair[0]] = pair[1]
        return result

    # ---- writes (fixture-only in tests) ------------------------------------ #

    def set_enabled(self, conv_id, value):
        if not isinstance(value, bool):
            raise AdapterError("开关需要 true 或 false")
        panel = self._get("/api/panel") or {}
        keys = [str(t["key"]) for t in panel.get("targets", []) if isinstance(t, dict) and t.get("key")]
        if conv_id not in keys:
            raise AdapterError("该 QQ 会话不在托管名单中", 404)
        enabled = [str(t["key"]) for t in panel.get("targets", [])
                   if isinstance(t, dict) and t.get("enabled") and t.get("key") != conv_id]
        if value:
            enabled.append(conv_id)
        if not enabled:
            raise AdapterError("至少要保留一个启用的会话")
        self._post("/api/scope", {"add": [], "drop": [], "enabled": enabled})

    def set_limit(self, conv_id, value):
        if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 10000:
            raise AdapterError("限额须为 0 到 10000 的整数；0 表示不限制")
        panel = self._get("/api/panel") or {}
        target = None
        for t in panel.get("targets", []) or []:
            if isinstance(t, dict) and str(t.get("key")) == conv_id:
                target = str(t.get("number") or "")
                break
        if target is None:
            raise AdapterError("该 QQ 会话不在托管名单中", 404)
        # The hub endpoint accepts the bare number as target (same as /limit command).
        self._post("/api/limits", {"target": target, "value": str(value)})

    def set_style(self, conv_id, value):
        if value not in {x["id"] for x in QQ_STYLES}:
            raise AdapterError("未知性格，请从可选性格中选择")
        if self.fixture is not None:
            return
        ctl = self._controller_instance()
        panel = ctl.state()
        keys = {("group:" if t.get("group") else "private:") + str(t["number"])
                for t in panel.get("config", {}).get("targets", [])}
        if conv_id not in keys:
            raise AdapterError("该 QQ 会话不在托管名单中", 404)

        def change(paused):
            ctl.panel("setPersona", target=conv_id, personaStyle=value)
        ctl.edit(change)

    def set_frequency(self, conv_id, value):
        if value not in {x["id"] for x in QQ_FREQUENCY} | {"off"}:
            raise AdapterError("可选频率：最低、自然、活跃")
        if self.fixture is not None:
            return
        ctl = self._controller_instance()
        # controller.proactive expects [group_number, mode_alias]; use the id
        # directly since it is one of the legal engine values.
        number = conv_id.split(":", 1)[-1]
        ctl.proactive([number, value])

    def set_model(self, model_id):
        allowed = {m["id"] for m in self.models()}
        if model_id not in allowed:
            raise AdapterError("模型不在已配置列表中", 404)
        self._post("/api/profiles/activate", {"id": model_id})
        # Make sure the next overview reflects the write, not the pre-switch read.
        self._cache_clear()

    def diagnostics(self):
        diags = []
        panel = self._panel()
        if self._cache and self._cache.get("panelFailed"):
            return [{"platform": "qq", "level": "warning",
                     "message": "QQ 控制接口暂不可用"}]
        if panel and not panel.get("connected"):
            diags.append({"platform": "qq", "level": "warning",
                          "message": "QQ 引擎未连接，自动回复可能不工作"})
        if panel and not panel.get("running"):
            diags.append({"platform": "qq", "level": "info",
                          "message": "QQ 自动回复当前处于暂停状态"})
        return diags

    def verify_model(self, expected):
        """Read the model back after a switch.

        Returns (state, observed, last_read) with state one of:
          "confirmed"   the read reported exactly the requested model
          "mismatch"    the read reported a different model
          "unconfirmed" the read failed or reported no model
        A read that cannot see the value never claims success.
        """
        last_read = _now()
        self._cache_clear()
        st = self._status_payload()
        if not isinstance(st, dict):
            return "unconfirmed", None, last_read
        observed = self._pick(st, "engine.model") or None
        if observed is None:
            return "unconfirmed", None, last_read
        return ("confirmed" if observed == expected else "mismatch"), observed, last_read


# --------------------------------------------------------------------------- #
# WeChat adapter
# --------------------------------------------------------------------------- #

class WeChatAdapter:
    """Talks to the loopback WeChat core control API (no key on loopback)."""

    def __init__(self, cfg, fixture=None):
        self.cfg = cfg
        self.fixture = fixture
        self.base = cfg.get("wechatCoreUrl", "http://127.0.0.1:19301")
        self._proactive_groups_lock = threading.Lock()

    def _get(self, path):
        if self.fixture is not None:
            return self.fixture.get("wechat", {}).get(path)
        return _http_json(self.base + path, "GET")

    def _post(self, path, body):
        if self.fixture is not None:
            return self.fixture.get("wechat", {}).get("post:" + path, {"ok": True})
        return _http_json(self.base + path, "POST", body=body, timeout=20)

    # ---- overview ---------------------------------------------------------- #

    # Reasons the WeChat client window can refuse to work (reported by the local
    # bridge as bridgeDiagnostics.client.reason). "login_confirmation_required"
    # is a phone confirmation, everything else beginning with "login" or a scan
    # prompt means the QR login screen is showing.
    SCAN_REASONS = ("login_required", "login_confirmation_required", "scan_required",
                    "qrcode_required", "logout_required")

    # The client window cannot receive replies in these phases.
    WINDOW_REASONS = ("window_blocked", "window_not_ready",
                      "client_unavailable", "client_runtime_changed")

    def _bridge_gateway(self, st):
        gateways = st.get("gateways", []) or [] if isinstance(st, dict) else []
        for g in gateways:
            if isinstance(g, dict) and g.get("platform") == "wechat":
                return g
        return {}

    def _client_state(self, bridge, diag, enabled=None):
        """Derive the honest reachable state of the WeChat client.

        Returns (state, reason) with state one of:
          qr        the client is showing a login/scan screen (明确待扫码)
          offline   the local bridge is not listening (明确离线)
          disabled  the gateway is turned off (明确停用)
          ok        bridge online, listener up and the account is usable
          unknown   the probe failed, or the answer is contradictory
        `enabled` alone is NEVER treated as "connected": it only ever means the
        gateway is configured to run, not that anything is reachable.
        """
        online = bridge.get("online") if isinstance(bridge, dict) else None
        listener = bridge.get("listener") if isinstance(bridge, dict) else None
        account_ok = bridge.get("accountOk") if isinstance(bridge, dict) else None
        reason = ""
        diag = diag if isinstance(diag, dict) else {}
        if diag.get("fresh") is True and isinstance(diag.get("client"), dict):
            reported = diag["client"].get("reason")
            if isinstance(reported, str):
                reason = reported

        if enabled is False:
            return "disabled", reason
        if reason in self.SCAN_REASONS:
            return "qr", reason
        if online is False:
            return "offline", reason
        if reason in self.WINDOW_REASONS:
            # A live bridge can temporarily lose its native window. This is
            # evidence about observation and delivery, not about login.
            return "unknown", reason
        if online is True:
            if listener is False:
                # Heartbeating but declined to add the message listener.
                return "offline", reason
            if account_ok is False:
                return "offline", reason or "account_unavailable"
            if listener is True and account_ok is True:
                return "ok", reason
            if listener is True and account_ok is None:
                # Listener up, account verdict not reported yet: reachable.
                return "ok", reason
            return "unknown", reason
        return "unknown", reason

    def _bridge_state(self, bridge, state, reason):
        if state == "disabled":
            return "disabled"
        if bridge.get("online") is False:
            return "offline"
        if bridge.get("online") is not True:
            return "unknown"
        if state == "qr":
            return "login_pending"
        if reason in self.WINDOW_REASONS:
            return "window_pending"
        if bridge.get("listener") is False:
            return "listener_paused"
        if bridge.get("accountOk") is False:
            return "account_blocked"
        return "ready" if state == "ok" else "unknown"

    def status(self):
        """Return (wechat_dict, diagnostics list).

        Shape (additive to the previous contract):
          {connected, running, model, client, clientState, clientReason,
           lastRead, enabled, bridgeStatus, bridge:{online,listener,accountOk,lastSeenSec},
           diagnostics:{fresh, phase, reason, time}, capabilities}

        connected/running are None whenever they cannot be read — never false
        by default — and an enabled gateway is not reported as connected.
        """
        diags = []
        last_read = _now()
        empty = {"connected": None, "running": None, "model": "", "client": "",
                 "clientState": "unknown", "clientReason": "", "lastRead": None,
                 "enabled": None, "bridgeStatus": "unknown", "bridge": None, "diagnostics": None,
                 "capabilities": {"model": False}}
        try:
            st = self._get("/api/status")
            if st is None:
                raise AdapterError("微信内核无数据", 502)
        except AdapterError:
            diags.append({"platform": "wechat", "level": "warning",
                          "message": "微信内核控制接口暂不可用，微信相关设置已隐藏"})
            return empty, diags
        if not isinstance(st, dict):
            diags.append({"platform": "wechat", "level": "warning",
                          "message": "微信状态返回异常，连接状态未确认"})
            return empty, diags

        gw = self._bridge_gateway(st)
        bridge = gw.get("bridge", {}) if isinstance(gw.get("bridge"), dict) else {}
        diag = st.get("bridgeDiagnostics") if isinstance(st.get("bridgeDiagnostics"), dict) else {}
        model = str(st.get("model") or "")
        enabled = gw.get("enabled") if isinstance(gw.get("enabled"), bool) else None

        state, reason = self._client_state(bridge, diag, enabled)
        bridge_state = self._bridge_state(bridge, state, reason)
        if state == "disabled":
            client, connected, running = "微信客户端未启用", False, False
        elif state == "qr":
            client = "微信客户端等待扫码登录"
            connected, running = False, False
        elif state == "offline":
            client = {"offline": "微信桥接心跳中断",
                      "listener_paused": "微信桥接监听暂停",
                      "account_blocked": "微信桥接账号校验未通过"}.get(bridge_state, "微信桥接暂不可用")
            connected, running = False, False
        elif state == "ok":
            client = "微信客户端在线" if bridge.get("accountOk") is True else "微信桥接监听正常"
            connected, running = True, True
        elif reason in self.WINDOW_REASONS:
            client = "微信窗口暂不可用"
            connected, running = None, False
        else:
            # Never guess in the failure direction either: say "未确认".
            client = "微信连接状态未确认"
            connected, running = None, None

        diag_view = None
        if diag:
            client_diag = diag.get("client") if isinstance(diag.get("client"), dict) else {}
            diag_view = {
                "fresh": diag.get("fresh") is True,
                "phase": str(client_diag.get("phase") or ""),
                "reason": str(client_diag.get("reason") or ""),
                "time": (float(client_diag["time"])
                         if isinstance(client_diag.get("time"), (int, float))
                         and not isinstance(client_diag.get("time"), bool) else None),
            }

        return {
            "connected": connected,
            "running": running,
            "model": model,
            "client": client,
            "clientState": state,
            "clientReason": reason,
            "lastRead": last_read,
            "enabled": enabled,
            "bridgeStatus": bridge_state,
            "bridge": {
                "online": bridge.get("online") if isinstance(bridge.get("online"), bool) else None,
                "listener": bridge.get("listener") if isinstance(bridge.get("listener"), bool) else None,
                "accountOk": (bridge.get("accountOk")
                              if isinstance(bridge.get("accountOk"), bool) else None),
                "lastSeenSec": (int(bridge["lastSeenSec"])
                                if isinstance(bridge.get("lastSeenSec"), (int, float))
                                and not isinstance(bridge.get("lastSeenSec"), bool) else None),
            },
            "diagnostics": diag_view,
            # A model switch is only offered when the core really reports one.
            "capabilities": {"model": bool(model)},
        }, diags

    def models(self):
        """Enumerate models the WeChat core can actually switch to.

        `GET /api/settings` exposes every configured provider with its `models`
        list. Only names that are already configured are offered — the console
        never invents a model id. The currently active model is included too.
        """
        try:
            s = self._get("/api/settings")
        except AdapterError:
            return []
        if not isinstance(s, dict):
            return []
        seen, out = set(), []

        def add(mid, name=None):
            mid = str(mid).strip() if mid is not None else ""
            if not mid or mid in seen:
                return
            seen.add(mid)
            out.append({"id": mid, "name": str(name or mid), "platforms": ["wechat"]})

        cur = s.get("model")
        if isinstance(cur, str) and cur.strip():
            add(cur.strip(), cur.strip())
        for provider, info in (s.get("providers") or {}).items():
            if not isinstance(info, dict):
                continue
            # Each provider's preset model + its explicit model list.
            add(info.get("model"), info.get("model"))
            for m in info.get("models") or []:
                add(m, m)
        return out

    def conversations(self):
        # The bridge service argv declares the allowlisted chat titles. Those are
        # the conversations this console can manage. Never read message bodies.
        titles = self._bridge_titles()
        if not titles:
            return []
        try:
            cfg = self._get("/api/config") or {}
        except AdapterError:
            cfg = {}
        group = ((cfg.get("config") or {}).get("group") or {}) if isinstance(cfg, dict) else {}
        judge = group.get("proactiveJudge") if isinstance(group, dict) else None
        judge = judge if isinstance(judge, dict) else {}
        allowed_groups = judge.get("groups") if isinstance(judge.get("groups"), list) else None
        allowed_groups = ([x for x in allowed_groups if isinstance(x, str)]
                          if allowed_groups is not None else None)
        chance = group.get("freeChatChance")
        frequency = self._chance_to_frequency(chance)
        style = self.global_style()
        group_names = [t["name"] for t in titles
                       if isinstance(t, dict) and t.get("group") is True
                       and isinstance(t.get("name"), str) and t["name"].strip()]
        out = []
        for title in titles:
            kind = "group" if title.get("group") else "private"
            raw_name = title.get("name")
            valid_group_name = (kind == "group" and isinstance(raw_name, str)
                                and bool(raw_name.strip()))
            name = (raw_name if valid_group_name else str(raw_name or ""))
            unique_group = valid_group_name and group_names.count(name) == 1
            membership = (name in allowed_groups
                          if valid_group_name and allowed_groups is not None else None)
            out.append({
                "platform": "wechat",
                "id": "wechat:" + ("group:" if title.get("group") else "private:") + name,
                "name": name,
                "kind": kind,
                # This checkbox manages only the existing exact-title proactive
                # allowlist. It never toggles the global judge or random freeChat.
                "enabled": membership,
                "enabledLabel": "主动闲聊白名单" if kind == "group" else None,
                "enabledHint": ("只增删该群名的白名单项；全局判别关闭时不会触发自动闲聊。"
                                if kind == "group" else None),
                "style": style,
                "frequency": frequency if kind == "group" else None,
                "limit": None,
                "capabilities": {
                    # The core's existing judge groups list uses exact WeChat
                    # titles. Duplicate titles cannot be safely controlled by
                    # title, so keep those rows read-only.
                    "enabled": bool(unique_group and allowed_groups is not None),
                    # Persona is one GLOBAL preset for the whole WeChat core.
                    # It is shown read-only here; the single writable control
                    # lives in globalSettings (scope wechat:global).
                    "style": False,
                    # freeChatChance applies to the whole WeChat gateway.
                    "frequency": False,
                    # WeChat has no daily-limit concept at all.
                    "limit": False,
                },
            })
        return out

    def set_enabled(self, conv_id, value):
        """Add/remove one uniquely titled group in the core judge allowlist."""
        if not isinstance(value, bool):
            raise AdapterError("开关需要 true 或 false")
        prefix = "wechat:group:"
        if not isinstance(conv_id, str) or not conv_id.startswith(prefix):
            raise AdapterError("主动闲聊白名单只支持微信群", 404)
        name = conv_id[len(prefix):]
        if not name.strip():
            raise AdapterError("微信群标题为空，拒绝修改主动闲聊白名单", 404)
        titles = self._bridge_titles()
        group_names = [t["name"] for t in titles
                       if isinstance(t, dict) and t.get("group") is True
                       and isinstance(t.get("name"), str) and t["name"].strip()]
        if group_names.count(name) == 0:
            raise AdapterError("该微信群不在当前桥接名单中", 404)
        if group_names.count(name) != 1:
            raise AdapterError("存在同名微信群，无法按群名安全区分；未写入", 409)
        # The console uses ThreadingHTTPServer, so serialize the core's
        # read/modify/write/readback sequence per WeChat adapter instance.
        # Re-read within the lock so concurrent changes to other groups survive.
        with self._proactive_groups_lock:
            current = self._get("/api/config")
            config = current.get("config") if isinstance(current, dict) else None
            group = config.get("group") if isinstance(config, dict) else None
            judge = group.get("proactiveJudge") if isinstance(group, dict) else None
            allowed = judge.get("groups") if isinstance(judge, dict) else None
            if not isinstance(allowed, list) or any(not isinstance(x, str) for x in allowed):
                raise AdapterError("微信主动闲聊白名单配置不可读或格式无效；未写入", 409)
            updated = list(allowed)
            if value and name not in updated:
                updated.append(name)
            elif not value:
                updated = [x for x in updated if x != name]
            result = self._post("/api/config", {
                "patch": {"group": {"proactiveJudge": {"groups": updated}}},
            })
            if isinstance(result, dict) and result.get("ok") is False:
                raise AdapterError("微信内核拒绝保存主动闲聊白名单", 409)
            reread = self._get("/api/config")
            config = reread.get("config") if isinstance(reread, dict) else None
            group = config.get("group") if isinstance(config, dict) else None
            judge = group.get("proactiveJudge") if isinstance(group, dict) else None
            observed = judge.get("groups") if isinstance(judge, dict) else None
            if not isinstance(observed, list) or observed != updated:
                raise AdapterError("保存后主动闲聊白名单与请求不一致；未确认成功", 409)
            return None

    def global_style(self):
        """Read the one GLOBAL WeChat persona preset from the real /api/persona.

        This is the exact same read `conversations()` uses for its per-chat
        `style` view, so the two can never disagree. The preset is
        `persona.sourcePreset`, falling back to `persona.preset`. It is read
        directly, never via the conversation list: the bridge-title file being
        empty/unreadable must not turn a known preset into null. A failed or
        unusable read returns None (unknown), never a guessed value.
        """
        try:
            persona = self._get("/api/persona")
        except AdapterError:
            return None
        if not isinstance(persona, dict):
            return None
        p = persona.get("persona")
        if not isinstance(p, dict):
            return None
        style = p.get("sourcePreset") or p.get("preset")
        return style if isinstance(style, str) and style else None

    def _bridge_titles(self):
        if self.fixture is not None:
            return self.fixture.get("wechat", {}).get("titles", [])
        configured = (self.cfg.get("wechatBridgeUnit")
                      or os.environ.get("DAFEIYU_WECHAT_BRIDGE_UNIT"))
        if not configured:
            return []
        unit = Path(str(configured)).expanduser()
        try:
            text = unit.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return []
        line = next((l for l in text.splitlines() if l.startswith("ExecStart")), "")
        groups = re.findall(r"--group\s+(\S+)", line)
        privates = re.findall(r"--admin-chat\s+(\S+)", line)
        return ([{"name": g, "group": True} for g in groups]
                + [{"name": p, "group": False} for p in privates])

    @staticmethod
    def _chance_to_frequency(chance):
        if not isinstance(chance, (int, float)):
            return None
        if chance <= 0.12:
            return "quiet"
        if chance <= 0.5:
            return "natural"
        return "active"

    # ---- writes (fixture-only in tests) ------------------------------------ #

    def set_style(self, conv_id, value):
        allowed = {p["id"] for p in self.style_options()}
        if value not in allowed:
            raise AdapterError("未知人设，请从可选人设中选择")
        self._post("/api/persona/preset", {"id": value})

    def style_options(self):
        try:
            pr = self._get("/api/persona/presets")
        except AdapterError:
            return []
        out = []
        for p in (pr or {}).get("presets", []) if isinstance(pr, dict) else []:
            if isinstance(p, dict) and p.get("id"):
                out.append({"id": str(p["id"]), "name": str(p.get("name") or p["id"])})
        return out

    def set_model(self, model_id):
        allowed = {m["id"] for m in self.models()}
        if model_id not in allowed:
            raise AdapterError("模型不在微信已配置列表中", 404)
        # /api/settings {model} is the real write path; it reloads the LLM
        # client immediately. Keys are never sent or echoed.
        self._post("/api/settings", {"model": model_id})

    def verify_model(self, expected):
        """Read the model back after a switch.

        Returns (state, observed, last_read) — same semantics as the QQ one:
        "confirmed" / "mismatch" / "unconfirmed". A read that cannot see the
        model reports "unconfirmed" rather than assuming the write landed.
        """
        last_read = _now()
        try:
            s = self._get("/api/settings")
        except AdapterError:
            return "unconfirmed", None, last_read
        if not isinstance(s, dict):
            return "unconfirmed", None, last_read
        observed = str(s.get("model") or "").strip() or None
        if not observed:
            return "unconfirmed", None, last_read
        return ("confirmed" if observed == expected else "mismatch"), observed, last_read

    def diagnostics(self):
        diags = []
        try:
            st = self._get("/api/status")
        except AdapterError:
            return [{"platform": "wechat", "level": "warning",
                     "message": "微信内核控制接口暂不可用"}]
        gw = self._bridge_gateway(st)
        bridge = gw.get("bridge", {}) if isinstance(gw.get("bridge"), dict) else {}
        diag = st.get("bridgeDiagnostics") if isinstance(st, dict) else None
        enabled = bool(gw.get("enabled")) if gw else None
        state, reason = self._client_state(bridge, diag, enabled)
        if state == "qr":
            diags.append({"platform": "wechat", "level": "warning",
                          "message": "微信客户端需要扫码登录，扫码前回复发不出去"})
        elif state == "offline":
            diags.append({"platform": "wechat", "level": "warning",
                          "message": "微信本地桥接离线，回复暂时发不出去"})
        elif state == "unknown":
            diags.append({"platform": "wechat", "level": "info",
                          "message": "微信连接状态未确认，请稍后刷新再判断"})
        elif state == "disabled":
            diags.append({"platform": "wechat", "level": "info",
                          "message": "微信网关未启用，控制台不发送微信消息"})
        if gw.get("lastError"):
            diags.append({"platform": "wechat", "level": "warning",
                          "message": "微信网关报告过一次发送失败"})
        return diags


# --------------------------------------------------------------------------- #
# Aggregate facade used by the HTTP server
# --------------------------------------------------------------------------- #

class Console:
    def __init__(self, cfg, fixture=None):
        self.qq = QQAdapter(cfg, fixture)
        self.wechat = WeChatAdapter(cfg, fixture)
        self.fixture = fixture

    def overview(self):
        self.qq._cache_clear()
        try:
            return self._overview()
        finally:
            self.qq._cache_clear()

    def _overview(self):
        qq, qdiag = self.qq.status()
        wx, wdiag = self.wechat.status()
        models = self._merge_models(self.qq.models(), self.wechat.models())
        conversations = self.qq.conversations() + self.wechat.conversations()
        diagnostics = (qdiag + wdiag + self.qq.diagnostics() + self.wechat.diagnostics())
        # Style options: union of QQ styles and WeChat persona presets.
        # Each carries a `platforms` hint (extra field, harmless to the contract)
        # so the UI can offer only the ids that platform really accepts.
        styles = [dict(x, platforms=["qq"]) for x in QQ_STYLES]
        seen = {x["id"] for x in styles}
        for p in self.wechat.style_options():
            if p["id"] not in seen:
                styles.append(dict(p, platforms=["wechat"]))
                seen.add(p["id"])
            else:
                for s in styles:
                    if s["id"] == p["id"] and "wechat" not in s["platforms"]:
                        s["platforms"].append("wechat")
        return {
            "qq": qq,
            "wechat": wx,
            "models": models,
            "conversations": conversations,
            "diagnostics": diagnostics,
            "styleOptions": styles,
            "frequencyOptions": FREQUENCY_OPTIONS,
            "limitOptions": LIMIT_OPTIONS,
            # Explicit GLOBAL settings. WeChat's persona is one preset shared by
            # all conversations; expose it once here instead of faking a
            # per-conversation switch. `capability` reflects the real write path
            # (POST /api/conversation with the reserved id wechat:global).
            "globalSettings": self._wechat_global_style(),
        }

    def _wechat_global_style(self):
        """One global WeChat persona setting, not a per-conversation switch.

        The value comes from the same real `/api/persona` summary that feeds
        `WeChatAdapter.conversations()` (`global_style()`), but it is read
        directly rather than picked out of a conversation row: when the bridge
        declares no conversation titles the list is empty, which must not hide
        a preset that `/api/persona` is still reporting. An unreadable or
        failed read stays None (unknown).
        The UI should render this as a single control; POST /api/conversation
        still accepts the reserved id `wechat:global` for backwards
        compatibility with already-saved configurations (it changes the same
        global preset).
        """
        current = self.wechat.global_style()
        return [{
            "scope": "wechat:global",
            "field": "style",
            "value": current,
            "platform": "wechat",
            "label": "微信全局人设",
            "capabilities": {"style": True},
            "styleOptions": self.wechat.style_options(),
        }]

    @staticmethod
    def _merge_models(a, b):
        merged = {}
        for m in a + b:
            cur = merged.get(m["id"])
            if cur is None:
                merged[m["id"]] = {"id": m["id"], "name": m["name"],
                                   "platforms": list(m["platforms"])}
            else:
                for p in m["platforms"]:
                    if p not in cur["platforms"]:
                        cur["platforms"].append(p)
        return list(merged.values())

    def set_conversation(self, platform, conv_id, field, value):
        adapter = self.qq if platform == "qq" else self.wechat
        # WeChat persona is GLOBAL, not per conversation. Accept only the
        # reserved scope id for a style write; anything else gets a clear
        # Chinese reason instead of silently pretending one chat changed.
        if platform == "wechat" and field == "style":
            if conv_id != "wechat:global":
                raise AdapterError(
                    "微信人设是全局设置，请在「微信全局人设」里修改，不能按会话分别设置", 409)
            return adapter.set_style("wechat:global", value)
        convs = adapter.conversations()
        conv = next((c for c in convs if c["id"] == conv_id), None)
        if conv is None:
            raise AdapterError("该会话不存在或暂不可用", 404)
        caps = conv.get("capabilities") or {}
        if not caps.get(field):
            raise AdapterError("该平台暂不支持修改「%s」" % field)
        handlers = {
            "enabled": lambda: adapter.set_enabled(conv_id, value),
            "style": lambda: adapter.set_style(conv_id, value),
            "frequency": lambda: adapter.set_frequency(conv_id, value),
            "limit": lambda: adapter.set_limit(conv_id, value),
        }
        return handlers[field]()

    def set_model(self, platform, model_id):
        if platform == "qq":
            return self.qq.set_model(model_id)
        if platform == "wechat":
            return self.wechat.set_model(model_id)
        raise AdapterError("未知平台", 404)

    def verify_model(self, platform, model_id):
        """Read a just-switched model back.

        Returns {"platform", "requested", "state", "observed", "lastRead"} where
        state is "confirmed" (read back the requested model), "mismatch" (read
        back a different one) or "unconfirmed" (the read failed or saw nothing).
        The HTTP layer returns this as an additive field of the {ok:true} reply,
        so older clients that only look at `ok` keep working.
        """
        adapter = {"qq": self.qq, "wechat": self.wechat}.get(platform)
        if adapter is None:
            raise AdapterError("未知平台", 404)
        state, observed, last_read = adapter.verify_model(model_id)
        return {
            "platform": platform,
            "requested": model_id,
            "state": state,
            "observed": observed,
            "lastRead": last_read,
        }
