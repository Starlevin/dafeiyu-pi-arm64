#!/usr/bin/env python3
"""DaFeiYu fish-house LAN viewer (read-only, stdlib only).

Based on jiuchai/fish-house MIT live player and default theme.
Reads existing local adapters / WeChat core persona; never changes bot
configuration or sends messages.
"""
from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import os
import re
import select
import subprocess
import sys
import threading
import time
import urllib.request
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit
from backend import live_ws
from backend.flow_bridge import (DEFAULT_THEME_URL, PHASE_LABELS, SELECTION_FILE, THEME_ROUTE_RE,
                                 build_router, resolve_selection)
from backend.status_adapter import normalize_bridge_health

BASE = Path(__file__).resolve().parent
WEB = BASE / "web"
HOUSE = WEB / "fish_house"
LAN_NET = ipaddress.ip_network(os.environ.get("FISH_HOUSE_LAN_CIDR", "127.0.0.0/8"))
CONSOLE_SOURCE = Path(os.environ.get("FISH_HOUSE_QQ_SRC") or
                      BASE.parent / "pi-console" / "server").expanduser()

# 固定共享默认主题（与上游 jiuchai/fish-house assets/default_theme.zip 的
# theme.json 同构）。主题缺失时保持保守回落，不再 import 期硬崩。
LIVE_THEME_ROUTE = DEFAULT_THEME_URL
LIVE_THEME_FILE = Path("themes/default/theme.json")
# 部署端 opt-in 选择：同目录静态小配置只放一个同源主题 URL。
# 未设置 / 校验失败一律回落固定 default，绝不因此中断服务。
LIVE_SELECTION_ROUTE = "/fish_house/" + SELECTION_FILE
# 只有主题包内的 videos/ 走新的单段 Range；旧 /fish_house/videos/h264/*
# 继续交给 SimpleHTTPRequestHandler，保持原有素材与响应语义。
THEME_VIDEO_RE = re.compile(
    r"^/fish_house/themes/([A-Za-z0-9._-]{1,64})/videos/([A-Za-z0-9._-]{1,128}\.mp4)$")
STOP_EVENT = threading.Event()


def closed_flag():
    return STOP_EVENT.is_set()


def load_theme_doc():
    """Keep the existing mobile asset paths when its theme is installed."""
    for candidate in (HOUSE / "theme.json", HOUSE / LIVE_THEME_FILE):
        try:
            if candidate.is_file():
                doc = json.loads(candidate.read_text(encoding="utf-8"))
                if isinstance(doc, dict) and isinstance(doc.get("config"), dict):
                    return doc, candidate
        except (OSError, ValueError):
            continue
    return None, None


THEME, _ = load_theme_doc()


def theme_videos():
    config = (THEME or {}).get("config") or {}
    videos = config.get("videos")
    return videos if isinstance(videos, dict) else {}


VIDEO = theme_videos()

PERSONA_URL = "http://127.0.0.1:19301/api/persona"

# 人格 → 小屋角色的映射依据。真实预设 id 见 /api/persona/presets：
#   persona-hajimi-*        → 哈基米（猫猫）
#   persona-gemini-dafeiyu  → 大肥鱼·Gemini 形态（猫耳卫衣）→ 猫猫
#   其余 persona-*          → 大肥鱼（鲸鱼娘）
CAT_ID_TOKENS = ("hajimi", "gemini")
CAT_TEXT_TOKENS = ("猫耳", "猫尾", "猫猫", "哈基米", "gemini")
WHALE_TEXT_TOKENS = ("鲸", "鲸鱼", "鲸尾", "大肥鱼", "蓝鲸")


def safe_json(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _bounded_run(argv, timeout):
    """跑一个外部命令并保证硬超时；返回 (stdout, ok)。

    Windows 上没有 systemctl，subprocess.run 会在管道上一直等不到 EOF，
    即使给了 timeout 也可能卡住整条链路（WS 快照就死在这里）。所以：
    1) 非 Linux 直接不调；2) 其余走线程 + join(timeout)，绝不无限等。
    """
    if not sys.platform.startswith("linux"):
        return "", False
    box = {}

    def worker():
        try:
            proc = subprocess.run(argv, capture_output=True, text=True,
                                  timeout=timeout, check=False)
            box["out"] = proc.stdout
        except Exception:
            box["out"] = ""

    thread = threading.Thread(target=worker, name="fish-house-cmd", daemon=True)
    thread.start()
    thread.join(timeout + 0.5)
    if thread.is_alive():
        # 真卡住就放弃这一轮结果，留 None 给调用方表达「未确认」。
        return "", False
    return box.get("out", ""), True


def service_active(name):
    out, ok = _bounded_run(["systemctl", "--user", "is-active", name], 2)
    return (out.strip() == "active") if ok else None


def services_active(*names):
    """一次 systemctl 调用查多个单元，避免 5s 轮询里反复 fork 进程。"""
    out, ok = _bounded_run(["systemctl", "--user", "is-active", *names], 3)
    if not ok:
        return {name: None for name in names}
    lines = out.strip().splitlines()
    return {name: (line.strip() == "active")
            for name, line in zip(names, lines)}


CORE_STATUS_URL = "http://127.0.0.1:19301/api/status"

# 与 adapters.WeChatAdapter 同一口径：扫码/阻断/在线/未知，绝不在缺证据时猜。
WECHAT_SCAN_REASONS = ("login_required", "login_confirmation_required",
                       "scan_required", "qrcode_required", "logout_required")
WECHAT_BLOCKED_REASONS = WECHAT_SCAN_REASONS + (
    "window_blocked", "window_not_ready", "client_unavailable", "client_runtime_changed")


def core_status():
    try:
        with urllib.request.urlopen(CORE_STATUS_URL, timeout=1.5) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
        return data if isinstance(data, dict) else None
    except Exception:
        return None


class BridgeHealthFileCache:
    """Read the WeChat bridge's atomically replaced health file on change only."""

    def __init__(self, path=None):
        if path is None:
            configured = os.environ.get("DAFEIYU_WECHAT_STATE_HOME")
            if configured:
                state_home = Path(configured).expanduser()
            else:
                state_home = Path(os.environ.get("XDG_STATE_HOME") or
                                  (Path.home() / ".local" / "state")) / "dafeiyu-wechat"
            path = state_home / "bridge-health.json"
        self.path = Path(path).expanduser()
        self._lock = threading.Lock()
        self._stamp = None
        self._value = None

    def read(self):
        """Return cached diagnostics; stat each call, parse only after replacement."""
        try:
            stat = self.path.stat()
            stamp = (stat.st_ino, stat.st_mtime_ns, stat.st_size)
        except OSError:
            return None
        with self._lock:
            if stamp == self._stamp:
                return self._value
            try:
                value = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, ValueError, TypeError):
                return None
            if not isinstance(value, dict):
                return None
            self._stamp = stamp
            self._value = value
            return self._value


bridge_health_file = BridgeHealthFileCache()
_CORE_LIVE_CACHE_LOCK = threading.Lock()
_CORE_LIVE_CACHE = {"at": 0.0, "value": None}
CORE_LIVE_CACHE_SECONDS = 1.0
LIVE_STARTED_AT = time.time()
LIVE_INGRESS_PIN_SECONDS = 0.9
PENDING_SUBMIT_GRACE_SECONDS = 35.0
WECHAT_GENERATION_MAX_SECONDS = 240.0


def live_core_fallback():
    """Keep the legacy HTTP fallback at its former 1 Hz maximum cadence."""
    with _CORE_LIVE_CACHE_LOCK:
        now = time.monotonic()
        if (_CORE_LIVE_CACHE["at"]
                and now - _CORE_LIVE_CACHE["at"] < CORE_LIVE_CACHE_SECONDS):
            return _CORE_LIVE_CACHE["value"]
        value = core_status()
        _CORE_LIVE_CACHE.update(at=now, value=value)
        return value


def pending_submit_phase(diagnostics, *, now):
    """Keep one uniquely identifiable submit pending for its bounded receipt wait."""
    if not isinstance(diagnostics, dict):
        return "idle"
    event = diagnostics.get("last_event")
    if (not isinstance(event, dict) or event.get("phase") != "submit"
            or event.get("reason") != "started"):
        return "idle"
    trace = event.get("trace")
    event_time = event.get("time")
    if (not isinstance(trace, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", trace)
            or isinstance(event_time, bool) or not isinstance(event_time, (int, float))):
        return "idle"
    try:
        age = float(now) - float(event_time)
    except (TypeError, ValueError, OverflowError):
        return "idle"
    if not 0 <= age <= PENDING_SUBMIT_GRACE_SECONDS:
        return "idle"

    # Any terminal marker at or after this timestamp makes the lifecycle no
    # longer uniquely pending. Ambiguous same-second outcomes fail closed.
    for key in ("last_success", "last_failure", "last_unknown"):
        marker = diagnostics.get(key)
        if marker is None:
            continue
        if not isinstance(marker, dict):
            return "idle"
        marker_time = marker.get("time")
        if (isinstance(marker_time, bool)
                or not isinstance(marker_time, (int, float))
                or marker.get("trace") == trace
                or float(marker_time) >= float(event_time)):
            return "idle"

    extended = normalize_bridge_health(
        diagnostics, now=now, max_age_seconds=PENDING_SUBMIT_GRACE_SECONDS)
    return "replying" if extended == "replying" else "idle"


def wechat_generation_owner_live(pid, thread, start_ticks):
    try:
        stat = Path(f"/proc/{pid}/task/{thread}/stat").read_text(encoding="utf-8")
        fields = stat.rsplit(")", 1)[1].split()
        return fields[0] not in {"Z", "X", "x"} and fields[19] == start_ticks
    except (OSError, UnicodeError, IndexError):
        return False


def active_wechat_generation(diagnostics, *, now):
    """A persisted start alone cannot prove that a model request is running."""
    marker = diagnostics.get("active_generation")
    if diagnostics.get("generation_tracking") is not True or not isinstance(marker, dict):
        return None
    at, pid, thread = marker.get("time"), marker.get("pid"), marker.get("thread")
    ticks = marker.get("start_ticks")
    trace = marker.get("trace")
    if (type(at) not in (int, float) or not 0 <= now - at < WECHAT_GENERATION_MAX_SECONDS
            or type(pid) is not int or pid <= 0 or type(thread) is not int or thread <= 0
            or not isinstance(ticks, str) or not re.fullmatch(r"[0-9]{1,20}", ticks)
            or not isinstance(trace, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", trace)):
        return None
    for key in ("last_event", "last_success", "last_failure", "last_unknown"):
        event = diagnostics.get(key)
        if not isinstance(event, dict) or event.get("trace") != trace:
            continue
        stamp = event.get("time")
        if (type(stamp) in (int, float) and at <= stamp <= now
                and (event.get("phase") in {"submit", "confirmation"}
                     or (event.get("phase") == "generation" and event.get("reason") != "started"))):
            return None
    return at if wechat_generation_owner_live(pid, thread, ticks) else None


def _bool(value):
    return value if isinstance(value, bool) else None


def derive_wechat(core):
    """把核心 /api/status 摊开成可核验的分层字段（登录 / 传输 / 回复能力 / 最近活动）。"""
    gateways = core.get("gateways") or []
    gateway = next((g for g in gateways
                    if isinstance(g, dict) and g.get("platform") == "wechat"), {})
    bridge = gateway.get("bridge") if isinstance(gateway.get("bridge"), dict) else {}
    diag_root = (core.get("bridgeDiagnostics")
                 if isinstance(core.get("bridgeDiagnostics"), dict) else {})
    client_diag = diag_root.get("client") if isinstance(diag_root.get("client"), dict) else {}

    reason = str(client_diag.get("reason") or "")
    fresh = diag_root.get("fresh") is True
    enabled = _bool(gateway.get("enabled"))
    online = _bool(bridge.get("online"))
    listener = _bool(bridge.get("listener"))
    account_ok = _bool(bridge.get("accountOk"))
    age = bridge.get("lastSeenSec")
    age = int(age) if isinstance(age, (int, float)) and not isinstance(age, bool) else None

    if fresh and reason in WECHAT_SCAN_REASONS:
        state = "qr"
    elif enabled is False:
        state = "disabled"
    elif fresh and reason in WECHAT_BLOCKED_REASONS:
        state = "offline"
    elif online is True:
        if listener is False or account_ok is False:
            state = "offline"
        elif listener is True:
            state = "ok"
        else:
            state = "unknown"
    elif online is False:
        state = "offline"
    else:
        state = "unknown"

    if state in ("qr", "offline", "disabled"):
        connected = running = False
    elif state == "ok":
        connected = running = True
    else:
        connected = running = None

    client_text = {
        "ok": "微信客户端在线",
        "qr": "微信客户端等待扫码登录",
        "offline": "微信本地桥接离线",
        "disabled": "微信客户端未启用",
    }.get(state, "微信连接状态未确认")

    # 登录态只认「桥接刚探过且报 window_available」，否则如实记 None。
    if fresh and reason == "window_available":
        logged_in = True
    elif fresh and reason in WECHAT_BLOCKED_REASONS:
        logged_in = False
    else:
        logged_in = None

    def stat(key):
        value = gateway.get(key)
        return value if isinstance(value, int) and not isinstance(value, bool) else None

    last_ms = gateway.get("lastMessageAt")
    last_at = (int(last_ms) // 1000
               if isinstance(last_ms, (int, float)) and last_ms else None)
    # 核心的 received/replied 计数器不计入「桥接直发」的回复（微信私聊就是这样发的），
    # 所以另外取桥接健康档案里的 last_success 作为真实的最近一次成功发送时间。
    success = (diag_root.get("last_success")
               if isinstance(diag_root.get("last_success"), dict) else {})
    last_reply_at = (int(success["time"]) if isinstance(success.get("time"), (int, float))
                     and not isinstance(success.get("time"), bool) else None)
    last_reply_reason = str(success.get("reason") or "")
    now = int(time.time())
    activity_state = normalize_bridge_health(diag_root, now=time.time())

    return {
        "connected": connected,
        "running": running,
        "bridge_online": online,
        "client_state": state,
        "client_reason": reason,
        "client": client_text,
        "logged_in": logged_in,
        "enabled": enabled,
        "model": str(core.get("model") or ""),
        "reply_capable": (state == "ok" and listener is True and account_ok is not False),
        "transport": {
            "bridge_online": online,
            "listener": listener,
            "uia": _bool(bridge.get("uia")),
            "account_ok": account_ok,
            "heartbeat_age_sec": age,
            "window": str(bridge.get("window") or ""),
        },
        "last_activity": {
            "at": last_at,
            "ago_sec": (now - last_at) if last_at else None,
            "last_reply_at": last_reply_at,
            "last_reply_ago_sec": (now - last_reply_at) if last_reply_at else None,
            "last_reply_reason": last_reply_reason,
            "received": stat("received"),
            "replied": stat("replied"),
            "sent": stat("sent"),
            "failed": stat("failed"),
            "last_error": str(gateway.get("lastError") or ""),
            "counters_note": "core 计数不含桥接直发，回复时间以 last_reply_at 为准",
        },
        # 仅加入由本机 bridge-health 结构化事件归一的短期阶段；
        # 不把 trace、聊天、账号或模式输入传到家园 API。
        "activity_state": activity_state,
        "diagnostics": {
            "fresh": fresh,
            "phase": str(client_diag.get("phase") or ""),
            "reason": reason,
            "at": (int(client_diag["time"])
                   if isinstance(client_diag.get("time"), (int, float))
                   and not isinstance(client_diag.get("time"), bool) else None),
        },
        "capabilities": {"read": state == "ok", "reply": state == "ok",
                         "model": bool(core.get("model"))},
    }



QQ_RECORD_STATES = {"queued", "generating", "sending", "confirmed",
                    "skipped", "failed", "uncertain", "cancelled"}
QQ_MAX_RECORDS = 6
QQ_POLL_INTERVAL = 2.0     # 有观众时的后台轮询节拍
QQ_ACTIVE_POLL_INTERVAL = 0.5
QQ_ACTIVE_POLL_WINDOW = 20.0  # 长时间等待时恢复低频
QQ_POLL_TIMEOUT = 1.2      # 单次读取预算，超时就保留上一轮，不阻塞任何请求
QQ_PANEL_MAX_AGE = 5.0


def _controller_panel(qa):
    """最窄只读取证：既有 Controller.state() 的 runtimeRecords 投影。

    对应 dafeiyu-test/qq-admin/admin_core.py 的 ``Controller``（state =
    self.panel）。runtimeRecords 已是 newest-first，这里按原顺序保留头部，
    不再取尾部。detail 只在进程内用于识别有明确信号的聊天和工具状态。
    """
    ctl = qa._controller_instance()
    if ctl is None:
        return None
    state = ctl.state()
    if not isinstance(state, dict):
        return None
    records = state.get("runtimeRecords")
    if not isinstance(records, list):
        return {"runtimeRecords": [], "queued": state.get("queued")}
    return {"runtimeRecords": records[:QQ_MAX_RECORDS],
            "queued": state.get("queued")}


def qq_panel_evidence(panel):
    """只保留 QQ 阶段、时间和验证过的标记。

    ``chat`` 是布尔：只有确切以 OneBot 已接受发送 开头的 confirmed 记录才
    为 True，插画/性格/设置命令一律 False。
    ``ingress`` 是布尔：只有固定原生聊天入队标记才为 True。
    ``active`` 是布尔：只有核心当前的聊天模型或 OneBot 调用才为 True。
    ``tool`` 是布尔：只有核心当前的聊天工具执行标记才为 True。
    ``fallback_notice`` 是布尔：模型格式警告和 OneBot 发送回执分别取证。
    """
    records = []
    raw = panel.get("runtimeRecords") if isinstance(panel, dict) else None
    if isinstance(raw, list):
        for item in raw[:QQ_MAX_RECORDS]:
            if not isinstance(item, dict):
                continue
            state = str(item.get("state") or "")
            ts = item.get("timestamp")
            if state not in QQ_RECORD_STATES:
                continue
            if isinstance(ts, bool) or not isinstance(ts, (int, float)):
                continue
            detail = live_ws._record_detail(item).strip()
            chat = (state == "confirmed"
                    and detail.startswith(live_ws.QQ_CHAT_CONFIRM_PREFIX))
            ingress = live_ws.qq_native_chat_ingress(item)
            records.append({"state": state, "timestamp": float(ts),
                            "chat": chat, "ingress": ingress, "active": live_ws.qq_active_chat(item),
                            "tool": live_ws.qq_tool_running(item),
                            "fallback_notice": live_ws.qq_fallback_notice(item)})
    queued = panel.get("queued") if isinstance(panel, dict) else None
    return {
        "available": isinstance(panel, dict),
        "runtime_records": records,
        "queued": (int(queued) if isinstance(queued, int)
                   and not isinstance(queued, bool) and queued >= 0 else None),
    }


class QQEvidencePoller:
    """单个后台 worker 轮询 Controller，只在有实时播放器观众时才跑。

    绝不在 ``BotSnapshot.read()`` / ``/api/status`` 请求内做这次读取：旧接口
    的形状与耗时保持原样，QQ 证据只供 WS 使用。
    """

    def __init__(self, reader, *, interval=QQ_POLL_INTERVAL,
                 timeout=QQ_POLL_TIMEOUT):
        self._reader = reader
        self._interval = interval
        self._timeout = timeout
        self._lock = threading.Lock()
        self._panel = None
        self._read_at = None
        self._thread = None
        self._reader_thread = None
        self._stop = threading.Event()

    def snapshot(self):
        with self._lock:
            if self._read_at is None or time.monotonic() - self._read_at > QQ_PANEL_MAX_AGE:
                return None
            return self._panel

    def poll_once(self):
        """读一次；超时就放弃本轮（None 表示不更新，保留上一轮）。"""
        if self._reader_thread is not None and self._reader_thread.is_alive():
            return None
        box = {"value": None}

        def worker():
            try:
                box["value"] = self._reader()
            except Exception:
                box["value"] = None

        thread = threading.Thread(target=worker, name="fish-house-qq-read", daemon=True)
        self._reader_thread = thread
        thread.start()
        thread.join(self._timeout)
        if thread.is_alive():
            return None
        return box["value"]

    def tick(self):
        value = self.poll_once()
        if value is not None:
            with self._lock:
                self._panel = value
                self._read_at = time.monotonic()
        return value

    def _next_interval(self, panel, *, now):
        evidence = live_ws.qq_evidence({}, panel, now=now)
        if (evidence.phase in ("received", "thinking", "tool", "replying")
                and 0 <= now - evidence.at <= QQ_ACTIVE_POLL_WINDOW):
            return min(self._interval, QQ_ACTIVE_POLL_INTERVAL)
        return self._interval

    def _run(self):
        # Fill the first snapshot as soon as a viewer connects. Waiting one full
        # interval here made cold-start QQ evidence invisible for up to 2 s.
        while not self._stop.is_set():
            panel = self.tick()
            if self._stop.wait(self._next_interval(panel, now=time.time())):
                break

    def start(self):
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="fish-house-qq-poll",
                                        daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=2.0)
        self._thread = None


def _make_qq_reader():
    """构造一个只在被调用时读取 Controller 的闭包；无适配层就返回 None。"""
    src = CONSOLE_SOURCE
    if not ((src / "adapters.py").exists() and (src / "config.json").exists()):
        return None

    def read():
        try:
            if str(src) not in sys.path:
                sys.path.insert(0, str(src))
            from adapters import QQAdapter
            config = json.loads((src / "config.json").read_text(encoding="utf-8"))
            qa = QQAdapter(config)
            try:
                return _controller_panel(qa)
            finally:
                qa._cache_clear()
        except Exception:
            return None

    return read


qq_reader = _make_qq_reader()
qq_poller = QQEvidencePoller(qq_reader) if qq_reader is not None else None


def qq_panel_view():
    """WS 侧证据：只来自后台轮询器，绝不在这里发起读取。"""
    poller = qq_poller
    if poller is None:
        return None
    panel = poller.snapshot()
    if panel is None:
        return None
    evidence = qq_panel_evidence(panel)
    return {"runtimeRecords": [
        {"state": item["state"], "timestamp": item["timestamp"],
         "_chat": item["chat"], "_ingress": item["ingress"], "_active": item["active"],
         "_tool": item["tool"], "_fallback_notice": item["fallback_notice"]}
        for item in evidence["runtime_records"]]}


class BotSnapshot:
    def __init__(self):
        self.lock = threading.Lock()
        self.updated = 0.0
        self.status = {}
        self.seq = 1
        self.phase = None

    def get(self):
        now = time.monotonic()
        with self.lock:
            if self.status and now - self.updated < 2.0:
                return self.status
            data = self.read()
            phase = data["activity"]["phase"]
            if self.phase != phase:
                self.seq += 1
                self.phase = phase
            data["seq"] = self.seq
            self.status = data
            self.updated = time.monotonic()
            return data

    def read(self):
        services = services_active(
            "dafeiyu.service", "napcat-qq.service",
            "dafeiyu-wechat-bridge.service", "dafeiyu-wechat-client.service",
            "dafeiyu-wechat-core.service")
        qq = {"service_running": services.get("dafeiyu.service"),
              "napcat_running": services.get("napcat-qq.service"),
              "connected": None, "running": None, "busy": None,
              "last_activity": None}
        wechat = {"connected": None, "running": None, "bridge_online": None,
                  "client_state": "unknown", "client_reason": "", "client": "",
                  "logged_in": None, "reply_capable": None,
                  "service_running": services.get("dafeiyu-wechat-bridge.service"),
                  "client_service_running": services.get("dafeiyu-wechat-client.service"),
                  "core_service_running": services.get("dafeiyu-wechat-core.service"),
                  "transport": None, "last_activity": None, "capabilities": None}
        src = CONSOLE_SOURCE
        if (src / "adapters.py").exists() and (src / "config.json").exists():
            try:
                if str(src) not in sys.path:
                    sys.path.insert(0, str(src))
                from adapters import QQAdapter
                config = json.loads((src / "config.json").read_text(encoding="utf-8"))
                qa = QQAdapter(config)
                try:
                    q_status, _ = qa.status()
                    qq["connected"] = q_status.get("connected")
                    qq["running"] = q_status.get("running")
                    # 旧接口只保留这两个标量；结构化 QQ 证据由 WS 侧后台
                    # QQEvidencePoller 单独低频读取，这里不再读大状态。
                    panel = qa._panel()
                    if isinstance(panel, dict) and isinstance(panel.get("busy"), bool):
                        qq["busy"] = panel["busy"]
                finally:
                    qa._cache_clear()
            except Exception:
                # Safe degradation: show 'unconfirmed', do not guess account state.
                pass

        core = core_status()
        if core:
            wechat.update(derive_wechat(core))

        return {
            "ok": True,
            "timestamp": int(time.time()),
            "qq": qq,
            "wechat": wechat,
            # Connection and busy flags cannot establish an active chat.
            # The verified event feed is served separately by house_state().
            "activity": {"phase": "idle", "source": None, "verified": False},
            "mode": "live-readonly",
            "persona": persona_snapshot.get(),
        }


class PersonaSnapshot:
    """只读的人格快照。只暴露角色映射所需的最小字段，绝不返回人格正文。"""

    def __init__(self):
        self.lock = threading.Lock()
        self.updated = 0.0
        self.value = {"character": "whale", "preset": "", "name": "",
                      "detected_by": "default", "at": 0}

    def get(self):
        now = time.monotonic()
        with self.lock:
            if self.value["at"] and now - self.updated < 8:
                return self.value
            self.value = self.read()
            self.updated = time.monotonic()
            return self.value

    def read(self):
        try:
            with urllib.request.urlopen(PERSONA_URL, timeout=2.5) as resp:
                payload = json.loads(resp.read().decode("utf-8", "replace"))
            persona = payload.get("persona") or {}
        except Exception:
            # 内核未响应时不猜，保持上一次结果（初次则用鲸鱼娘）。
            return dict(self.value, at=int(time.time()))

        preset_id = str(persona.get("sourcePreset") or "")
        preset = str(persona.get("preset") or "")
        name = str(persona.get("name") or "")
        blob = " ".join(str(persona.get(k) or "") for k in
                        ("appearance", "personality", "background", "text", "preset", "name"))

        character, by = classify(preset_id, blob)
        return {"character": character, "preset": preset, "name": name,
                "detected_by": by, "at": int(time.time())}


def classify(preset_id, blob):
    pid = (preset_id or "").strip().lower()
    if pid:
        if any(token in pid for token in CAT_ID_TOKENS):
            return "cat", "preset"
        if pid.startswith("persona-"):
            return "whale", "preset"
    lowered = (blob or "").lower()
    cat = sum(lowered.count(token) for token in CAT_TEXT_TOKENS)
    whale = sum(lowered.count(token) for token in WHALE_TEXT_TOKENS)
    if cat > whale:
        return "cat", "text"
    if whale > 0:
        return "whale", "text"
    return "whale", "default"


snapshot = BotSnapshot()
persona_snapshot = PersonaSnapshot()


class DiagStore:
    """真机渲染自诊断：只收 UA / 视口 / 图片解码 / 元素尺寸 / JS 错误。

    不接收聊天文本、token 或 cookie，只用于回答「手机到底渲染成了什么」，
    避免以后再用「HTTP 200」冒充「画面对了」。
    """

    MAX_ERRORS = 12

    def __init__(self):
        self.lock = threading.Lock()
        self.value = {"ok": True, "received": 0, "last": None}

    def put(self, payload, ip=""):
        if not isinstance(payload, dict):
            return False
        errors = payload.get("errors")
        errors = ([str(item)[:160] for item in errors][:self.MAX_ERRORS]
                  if isinstance(errors, list) else [])
        imgs = payload.get("imgs")
        imgs = ({str(key)[:80]: str(value)[:40] for key, value in imgs.items()}
                if isinstance(imgs, dict) else {})
        frames = payload.get("frames")
        frames = ({str(key)[:24]: str(value)[:64] for key, value in frames.items()}
                  if isinstance(frames, dict) else {})

        def number(value):
            return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None

        record = {
            "at": int(time.time()),
            "ip": str(ip)[:45],
            "page": str(payload.get("page") or "")[:160],
            "ua": str(payload.get("ua") or "")[:180],
            "viewport": [number(payload.get("w")), number(payload.get("h"))],
            "dpr": number(payload.get("dpr")),
            "errors": errors,
            "imgs": imgs,
            "frames": frames,
            "fallback": str(payload.get("fallback") or "")[:40],
            "sound_on": bool(payload.get("sound_on")),
            "muted_now": bool(payload.get("muted_now")),
        }
        cycle = payload.get("cycle")
        if isinstance(cycle, dict):
            log = cycle.get("log")
            record["cycle"] = {
                "runs": number(cycle.get("runs")),
                "transitions": number(cycle.get("transitions")),
                "failed": number(cycle.get("failed")),
                "canplay_ms_max": number(cycle.get("canplay_ms_max")),
                "fade_ms": number(cycle.get("fade_ms")),
                "min_luma": number(cycle.get("min_luma")),
                "black_frames": number(cycle.get("black_frames")),
                "log": ([str(item)[:60] for item in log][-24:]
                        if isinstance(log, list) else []),
            }
        with self.lock:
            self.value["received"] += 1
            self.value["last"] = record
        return True

    def get(self):
        with self.lock:
            return dict(self.value)


diag_snapshot = DiagStore()

LEGACY_NODES = {
    "idle": ("rest", True),
    "received": ("phone_pickup", False),
    "thinking": ("phone_loop", True),
    "tool": ("computer_loop", True),
    "replying": ("phone_loop", True),
    "sent": ("phone_putdown", False),
    "error": ("rest", True),
}


def house_event(frame):
    """Project one verified wallpaper state onto the compatibility feed."""
    stamp = frame.get("event_at") if frame.get("verified") is True else None
    phase = frame["phase"] if stamp is not None else "idle"
    source = frame["source"]
    event = {
        "id": f"{source}:{phase}:{stamp}" if stamp is not None else "",
        "seq": frame["seq"], "phase": phase, "source": source,
        "verified": True, "at": stamp if stamp is not None else frame["ts"],
        "terminal": phase == "error",
    }
    if stamp is not None and "expires_at" in frame:
        event["expires_at"] = frame["expires_at"]
    return event


def live_event_key(frame):
    return (frame["source"], frame["phase"], frame.get("event_at"))


class LiveCursor:
    """Keep a client's snapshot from replaying as a queued transition."""

    def __init__(self, snapshot_frame):
        self.seq = snapshot_frame["seq"]
        self.key = live_event_key(snapshot_frame)

    def accept(self, frame):
        if frame["seq"] <= self.seq:
            return False
        self.seq = frame["seq"]
        kind = frame["type"]
        if kind == "config":
            self.key = None
        if kind in ("state", "flow_goto"):
            key = live_event_key(frame)
            if key == self.key:
                return False
            if kind == "state":
                self.key = key
        return True


def house_state():
    frame = bridge.state_snapshot()
    event = house_event(frame)
    phase = event["phase"]
    node, loop = LEGACY_NODES[phase]
    wx_phase = phase if event["source"] == "wechat" else "idle"
    stamp = event["at"]
    age = max(0, frame["ts"] - stamp)
    return {
        **event,
        "wechat_activity_state": wx_phase,
        "node_id": node,
        "node_label": PHASE_LABELS[phase],
        "node_video": VIDEO.get(node, ""),
        "node_loop": loop,
        "bubble": "",
        "bubble_until": 0,
        "events": {
            "inbound_at": stamp if wx_phase == "received" else None,
            "inbound_ago_sec": age if wx_phase == "received" else None,
            "reply_at": stamp if wx_phase == "sent" else None,
            "reply_ago_sec": age if wx_phase == "sent" else None,
            "reply_reason": "visible_receipt" if wx_phase == "sent" else "",
        },
    }


def live_snapshot():
    """Read verified chat state from the atomic file, with HTTP compatibility fallback."""
    diagnostics = bridge_health_file.read()
    if diagnostics is None:
        core = live_core_fallback() or {}
        diagnostics = core.get("bridgeDiagnostics") or {}
    if not isinstance(diagnostics, dict):
        diagnostics = {}
    now = time.time()
    phase = normalize_bridge_health(diagnostics, now=now, max_age_seconds=15)
    if phase == "thinking" and diagnostics.get("generation_tracking") is True:
        phase = "idle"
    pending = phase in {"idle", "replying"} and pending_submit_phase(diagnostics, now=now) == "replying"
    if pending:
        phase = "replying"
    timestamp = 0.0
    ingress = diagnostics.get("last_ingress")
    event = diagnostics.get("last_event")
    ingress_time = ingress.get("time") if isinstance(ingress, dict) else None
    fingerprint = ingress.get("fingerprint") if isinstance(ingress, dict) else None
    trace = event.get("trace") if isinstance(event, dict) else None
    event_phase = event.get("phase") if isinstance(event, dict) else None
    event_reason = event.get("reason") if isinstance(event, dict) else None
    direction_unverified = (event_phase == "mention"
                            and event_reason == "direction_unverified")
    ingress_age = None
    try:
        ingress_age = now - float(ingress_time)
    except (TypeError, ValueError, OverflowError):
        pass
    valid_ingress = (
        isinstance(ingress_time, (int, float)) and not isinstance(ingress_time, bool)
        and isinstance(fingerprint, str) and re.fullmatch(r"[0-9a-f]{64}", fingerprint)
        and isinstance(trace, str) and re.fullmatch(r"[0-9a-fA-F]{64}", trace)
        and hashlib.sha256(trace.lower().encode("ascii")).hexdigest() == fingerprint
        and isinstance(event_phase, str) and isinstance(event_reason, str)
        and event_phase in {"queue", "generation", "submit"}
        and ((event_phase == "queue" and event_reason == "accepted")
             or (event_phase == "generation" and event_reason in {"started", "ready"})
             or (event_phase == "submit" and event_reason == "started"))
        and isinstance(ingress_age, (int, float))
        and 0 <= ingress_age <= LIVE_INGRESS_PIN_SECONDS
        and float(ingress_time) >= LIVE_STARTED_AT
        and phase not in {"error", "sent"}
        and not direction_unverified
    )
    if direction_unverified:
        # This UI diagnostics event means ingress direction was not proven; it
        # must not animate as an error or as a received chat message.
        phase = "idle"
    elif phase == "received" and not valid_ingress:
        # A raw receive is not yet accepted by the authorized queue guard.
        phase = "idle"
    elif valid_ingress:
        # Keep the accepted ingress visible briefly so both the normal tick and
        # a new subscriber's forced snapshot can observe it. LiveBridge's
        # phase/timestamp dedupe suppresses repeat playback for existing viewers.
        phase = "received"
        timestamp = float(ingress_time)
    if phase != "idle":
        if timestamp == 0.0:
            for key in ("last_event", "last_success", "last_failure", "last_unknown"):
                marker = diagnostics.get(key)
                value = marker.get("time") if isinstance(marker, dict) else None
                if isinstance(value, (int, float)) and not isinstance(value, bool) and timestamp < value <= now:
                    timestamp = value
    activity_until = 0.0
    if phase == "replying" and pending:
        activity_until = timestamp + PENDING_SUBMIT_GRACE_SECONDS
    if not valid_ingress:
        generation_at = active_wechat_generation(diagnostics, now=now)
        if generation_at is not None and (phase == "idle" or generation_at > timestamp):
            phase, timestamp = "thinking", generation_at
            activity_until = timestamp + WECHAT_GENERATION_MAX_SECONDS
    delivery_at = diagnostics.get("last_confirmed_delivery_at")
    if (type(delivery_at) not in (int, float)
            or not LIVE_STARTED_AT <= delivery_at <= now
            or now - delivery_at > live_ws.SENT_WINDOW_SEC):
        delivery_at = 0.0
    return {"wechat": {"activity_state": phase, "activity_at": timestamp,
                        "activity_until": activity_until, "delivery_at": delivery_at}}


_THEME_CHOICE_LOCK = threading.Lock()
_THEME_CHOICE_CACHE = {"at": 0.0, "house": None, "value": None}
THEME_CHOICE_TTL = 5.0


def live_theme_choice():
    """One decision shared by the bridge and the HTTP theme routes.

    Cached briefly so a WS tick and the HTTP routes cannot disagree, and so the
    tiny selector file is not re-read per frame. ``build_router`` re-resolves on
    its own refresh cycle, so a changed pointer still takes effect without a
    restart. Any failure resolves to the fixed default.
    """
    with _THEME_CHOICE_LOCK:
        now = time.monotonic()
        cached = _THEME_CHOICE_CACHE
        if (cached["value"] is not None and cached["house"] == HOUSE
                and now - cached["at"] < THEME_CHOICE_TTL):
            return cached["value"]
        try:
            value = resolve_selection(HOUSE)
        except Exception:
            value = {"theme_url": DEFAULT_THEME_URL, "theme_path": None,
                     "slug": "default", "selected": False,
                     "fallback": "selection_error"}
        _THEME_CHOICE_CACHE.update({"at": now, "house": HOUSE, "value": value})
        return value


bridge = live_ws.LiveBridge(
    router_factory=lambda: build_router(HOUSE, selection=live_theme_choice()),
    snapshot_getter=live_snapshot,
    panel_reader=qq_panel_view,
    tick=0.25,
)
live_viewer_lock = threading.Lock()


def register_live_viewer():
    with live_viewer_lock:
        registered = bridge.register() if bridge is not None else None
        if registered is not None and qq_poller is not None and bridge.client_count == 1:
            qq_poller.start()
        return registered


def unregister_live_viewer(client_id):
    with live_viewer_lock:
        if bridge is None:
            return
        bridge.unregister(client_id)
        if qq_poller is not None and bridge.client_count == 0:
            qq_poller.stop()


class Handler(SimpleHTTPRequestHandler):
    server_version = "DaFeiYuHouseLAN/0.2"
    protocol_version = "HTTP/1.1"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(WEB), **kwargs)

    def allow_host(self, host):
        """同源判定：loopback 或本网段的地址才允许作为 Origin/Host。"""
        try:
            addr = ipaddress.ip_address(host or "")
        except ValueError:
            return str(host or "").lower() == "localhost"
        return addr.is_loopback or addr in LAN_NET

    def allowed(self):
        try:
            addr = ipaddress.ip_address(self.client_address[0])
            return addr.is_loopback or addr in LAN_NET
        except ValueError:
            return False

    def do_GET(self):
        path = urlsplit(self.path).path
        if path == "/ws":
            return self.serve_ws()
        if not self.allowed():
            self.send_error(403, "LAN only")
            return
        path = urlsplit(self.path).path
        query = urlsplit(self.path).query
        if path in ("/", "/live", "/player"):
            if path == "/":
                # 默认直达 R5.2 新版双形态家园；?legacy=1 保留原版零脚本首页回退入口。
                entry = ("/fish_house/index.html" if "legacy=1" in query
                         else "/fish_house/home.html")
            elif path == "/live":
                # 共享契约入口：/live -> /fish_house/live.html，query 原样保留
                # （例如 ?bubble=0）。
                entry = "/fish_house/live.html"
            else:
                entry = "/fish_house/player.html"
            target = entry + (("?" + query) if query else "")
            self.send_response(302)
            self.send_header("Location", target)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if path == "/api/house/events":
            return self.serve_sse()
        if path == "/api/house/live-events":
            return self.serve_live_sse()
        if path == "/fish_house":
            # 手机浏览器常不带结尾斜杠；缺了它 SimpleHTTPRequestHandler 直接 404，
            # 真机就是整屏灰空白（P0 灰屏的第一层原因）。
            query = urlsplit(self.path).query
            self.send_response(301)
            self.send_header("Location", "/fish_house/" + (("?" + query) if query else ""))
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if path == "/healthz":
            return self.reply({"ok": True, "viewer": "running"})
        if path == "/api/status":
            return self.reply(snapshot.get())
        if path == "/api/diag":
            return self.reply(diag_snapshot.get())
        if path == "/api/persona":
            return self.reply({"ok": True, **persona_snapshot.get()})
        if path == "/fish_house/state.json":
            return self.reply(house_state())
        if path == "/fish_house/config.json":
            config = (THEME or {}).get("config") or {}
            return self.reply({"crossfade_ms": 400, "show_bubble": False,
                               "videos": VIDEO,
                               "durations": config.get("durations", {})})
        # 壁纸与 WebSocket 读取同一个已校验的主题选择。
        if path == LIVE_SELECTION_ROUTE:
            return self.serve_selection(head_only=False)
        # 固定共享默认主题 + 已验证的 opt-in 主题：原字节直出，不改 JSON、不改 schema。
        if path == LIVE_THEME_ROUTE:
            return self.serve_theme(head_only=False)
        theme_route = self.theme_route_for(path)
        if theme_route is not None:
            return self.serve_theme_file(theme_route, head_only=False)
        if self.reject_theme_path(path):
            return self.send_error(404, "Theme not found")
        # 只有主题包内的 videos/ 做单段 Range + HEAD。
        segment = THEME_VIDEO_RE.match(path)
        if segment:
            return self.serve_video(segment.group(1), segment.group(2), head_only=False)
        if path.startswith("/fish_house/"):
            return super().do_GET()
        self.send_error(404)

    def do_HEAD(self):
        if not self.allowed():
            self.send_error(403)
            return
        path = urlsplit(self.path).path
        query = urlsplit(self.path).query
        if path in ("/", "/live", "/player"):
            if path == "/":
                entry = ("/fish_house/index.html" if "legacy=1" in query
                         else "/fish_house/home.html")
            elif path == "/live":
                entry = "/fish_house/live.html"
            else:
                entry = "/fish_house/player.html"
            target = entry + (("?" + query) if query else "")
            self.send_response(302)
            self.send_header("Location", target)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if path in ("/healthz", "/api/status", "/api/persona"):
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            return
        if path == LIVE_SELECTION_ROUTE:
            return self.serve_selection(head_only=True)
        if path == LIVE_THEME_ROUTE:
            return self.serve_theme(head_only=True)
        theme_route = self.theme_route_for(path)
        if theme_route is not None:
            return self.serve_theme_file(theme_route, head_only=True)
        if self.reject_theme_path(path):
            return self.send_error(404, "Theme not found")
        segment = THEME_VIDEO_RE.match(path)
        if segment:
            return self.serve_video(segment.group(1), segment.group(2), head_only=True)
        if path.startswith("/fish_house/"):
            return super().do_HEAD()
        self.send_error(405)

    def do_POST(self):
        if urlsplit(self.path).path != "/api/diag":
            self.send_error(405, "Read only")
            return
        if not self.allowed():
            self.send_error(403, "LAN only")
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            length = 0
        if length <= 0 or length > 8192:
            self.send_error(413, "Payload too large")
            return
        raw = self.rfile.read(length)
        try:
            payload = json.loads(raw.decode("utf-8", "replace"))
        except Exception:
            self.send_error(400, "Bad JSON")
            return
        diag_snapshot.put(payload, self.client_address[0])
        print("diag_from=%s keys=%s" % (self.client_address[0], sorted(payload)[:8]), flush=True)
        self.send_response(204)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_PUT(self):
        self.send_error(405, "Read only")

    def do_DELETE(self):
        self.send_error(405, "Read only")

    # ------------------------------------------------------------- 主题资源
    def serve_theme(self, head_only=False):
        """原字节直出 theme.json，不解析、不改写、不改 schema。"""
        try:
            body = (HOUSE / LIVE_THEME_FILE).read_bytes()
        except OSError:
            return self.send_error(404, "Theme not found")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if not head_only:
            self.wfile.write(body)

    def serve_selection(self, head_only=False):
        """Return the same validated theme URL used for WebSocket orders."""
        if not (HOUSE / SELECTION_FILE).is_file():
            return self.send_error(404, "No live theme selection")
        body = json.dumps({"theme_url": live_theme_choice()["theme_url"]}).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if not head_only:
            self.wfile.write(body)

    @staticmethod
    def reject_theme_path(path):
        if not path.startswith("/fish_house/themes/"):
            return False
        return bool(THEME_ROUTE_RE.fullmatch(path) or "%" in path or "//" in path
                    or any(part in {".", ".."} for part in path.split("/")))

    def theme_route_for(self, path):
        """把请求路径解析成当前选中主题的 theme.json，否则 None。

        只有「路径等于当前有效选择」才放行：未选中的主题、``..`` 一类写法、
        非 ``theme.json`` 文件一律不暴露，开启一个主题不会顺手带出别的主题包。
        """
        match = THEME_ROUTE_RE.fullmatch(path or "")
        if not match:
            return None
        slug = match.group(1)
        if slug == "default":
            return HOUSE / LIVE_THEME_FILE
        choice = live_theme_choice()
        if not choice.get("selected") or slug != choice.get("slug"):
            return None
        target = choice.get("theme_path")
        return Path(target) if target is not None else None

    def serve_theme_file(self, target, head_only=False):
        """原字节直出某个已验证主题的 theme.json。"""
        try:
            body = Path(target).read_bytes()
        except OSError:
            return self.send_error(404, "Theme not found")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if not head_only:
            self.wfile.write(body)

    def serve_video(self, theme_id, name, head_only=False):
        """主题包内视频的单段 HTTP Range + HEAD。

        只接受 ``themes/<safe-id>/videos/<safe-name>.mp4``；路径与长度严格
        校验，绝不接受目录穿越，也不影响旧 /fish_house/videos/h264/* 语义。
        """
        if not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", theme_id or ""):
            return self.send_error(403, "Bad theme id")
        if not re.fullmatch(r"[A-Za-z0-9._-]{1,128}\.mp4", name or ""):
            return self.send_error(403, "Bad video name")
        root = (HOUSE / "themes").resolve()
        try:
            target = (root / theme_id / "videos" / name).resolve()
        except OSError:
            return self.send_error(404, "No such video")
        if root not in target.parents:
            return self.send_error(403, "Bad path")
        try:
            if not target.is_file():
                return self.send_error(404, "No such video")
            size = target.stat().st_size
        except OSError:
            return self.send_error(404, "No such video")
        if size <= 0:
            return self.send_error(404, "Empty video")
        start, end = 0, size - 1
        status = 200
        raw_range = (self.headers.get("Range") or "").strip()
        if raw_range:
            match = re.fullmatch(r"bytes=(\d*)-(\d*)", raw_range)
            if not match:
                self.send_response(416)
                self.send_header("Content-Range", "bytes */%d" % size)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            first, last = match.group(1), match.group(2)
            if first:
                start = int(first)
                end = int(last) if last else size - 1
            else:
                if not last or int(last) <= 0:
                    self.send_response(416)
                    self.send_header("Content-Range", "bytes */%d" % size)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                start = max(0, size - int(last))
                end = size - 1
            if start > end or start >= size:
                self.send_response(416)
                self.send_header("Content-Range", "bytes */%d" % size)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            end = min(end, size - 1)
            status = 206
        length = end - start + 1
        self.send_response(status)
        self.send_header("Content-Type", "video/mp4")
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        if status == 206:
            self.send_header("Content-Range", "bytes %d-%d/%d" % (start, end, size))
        self.send_header("Cache-Control", "private, max-age=3600")
        self.end_headers()
        if head_only:
            return
        remain = length
        try:
            with open(target, "rb") as handle:
                handle.seek(start)
                while remain > 0:
                    chunk = handle.read(min(65536, remain))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remain -= len(chunk)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass

    # ------------------------------------------------------------------- WS
    def serve_ws(self):
        """RFC 6455 只读升级：局域网同源才放行，之后只收发 JSON 状态帧。

        升级发生在 do_GET 内部，此时 stdlib 的 parse_request 已经把请求行和
        全部头读完了；所以这里直接用 self.command / self.headers，绝不能再
        从 rfile 重读（重读会永远等不到数据，浏览器表现为握手挂死）。
        """
        try:
            method, target, _version, headers = live_ws.parse_http_request(
                self.command, self.path, dict(self.headers.items()))
        except live_ws.WSError:
            return self._ws_error(400, "Bad request")
        if urlsplit(target).path != live_ws.WS_PATH:
            return self._ws_error(404, "Not found")
        if method != "GET":
            return self._ws_error(405, "Read only")
        if headers.get("transfer-encoding"):
            return self._ws_error(400, "Bad framing")
        upgrade = (headers.get("upgrade") or "").lower()
        connection = (headers.get("connection") or "").lower()
        if (upgrade != "websocket"
                or "upgrade" not in {token.strip() for token in connection.split(",")}):
            return self._ws_error(426, "WebSocket required")
        if (headers.get("sec-websocket-version") or "") != "13":
            return self._ws_error(426, "Unsupported version")
        try:
            origin_ok = live_ws.origin_allowed(
                headers.get("origin"), headers.get("host") or "",
                self.client_address[0], LAN_NET, self.allow_host)
        except Exception:
            origin_ok = False
        if not origin_ok:
            return self._ws_error(403, "Same origin only")
        try:
            key = live_ws.validate_key(headers.get("sec-websocket-key"))
        except live_ws.WSError:
            return self._ws_error(400, "Bad key")
        registered = register_live_viewer()
        if registered is None:
            return self._ws_error(503, "Too many clients")
        cid, queue = registered
        try:
            accept = live_ws.compute_accept(key)
            parts = [
                "HTTP/1.1 101 Switching Protocols",
                "Upgrade: websocket",
                "Connection: Upgrade",
                "Sec-WebSocket-Accept: " + accept,
            ]
            proto = headers.get("sec-websocket-protocol")
            if proto and re.fullmatch(r"[A-Za-z0-9_, .\-]{1,64}", proto):
                parts.append("Sec-WebSocket-Protocol: " + proto.split(",")[0].strip())
            self.close_connection = True
            self.request.sendall(("\r\n".join(parts) + "\r\n\r\n").encode("latin-1"))
            # 从这里开始是裸 WebSocket 帧，绝不能再回到 HTTP 头/正文路径。
            self._ws_upgraded = True
            self._ws_loop(queue)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            unregister_live_viewer(cid)

    def _ws_error(self, status, message):
        body = message.encode("utf-8")
        try:
            self.request.sendall(live_ws.http_response(
                status, [("Content-Type", "text/plain; charset=utf-8")], body))
        except OSError:
            pass
        self.close_connection = True

    def _ws_loop(self, queue):
        """只消费队列：轮询由 bridge 单线程按真实事件变化驱动。

        读取走 select，闲置不会把 rfile 留在读不到的 timeout 状态。
        """
        conn = live_ws.ClientConnection(self.request, queue, rfile=self.rfile)
        snapshots = bridge.snapshot_frames()
        cursor = LiveCursor(snapshots[-1])
        for frame in snapshots:
            if not conn.send(live_ws.encode_frame(live_ws.OPCODE_TEXT, safe_json(frame))):
                return
        while not conn.closed and not closed_flag():
            for payload in queue.drain():
                if not cursor.accept(json.loads(payload)):
                    continue
                if not conn.send(live_ws.encode_frame(live_ws.OPCODE_TEXT, payload)):
                    break
            if conn.closed:
                break
            try:
                conn.wait_readable(0.25)
            except live_ws.WSError:
                break
        try:
            self.request.close()
        except OSError:
            pass

    def serve_sse(self):
        """Keep the compatibility event shape using the wallpaper's producer."""
        return self.serve_live_sse(compatibility=True)

    def serve_live_sse(self, *, compatibility=False):
        """Serve the wallpaper's real flow frames on an HTTP event stream."""
        if not live_ws.origin_allowed(self.headers.get("Origin"), self.headers.get("Host") or "",
                                      self.client_address[0], LAN_NET, self.allow_host):
            return self.send_error(403, "Same origin only")
        registered = register_live_viewer()
        if registered is None:
            return self.send_error(503, "Too many clients")
        client_id, queue = registered
        self.close_connection = True
        try:
            self.request.settimeout(5)
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self.end_headers()
            # The snapshot has fresh sequence numbers. Drain frames queued before
            # registration completed first so they cannot precede the config.
            snapshots = bridge.snapshot_frames()
            cursor = LiveCursor(snapshots[-1])
            if compatibility:
                snapshots = [house_event(snapshots[-1])]
            for frame in snapshots:
                self.wfile.write(b"data: " + safe_json(frame) + b"\n\n")
            self.wfile.flush()
            last_write = time.monotonic()
            while not queue.closed and not queue.overflow and not closed_flag():
                for payload in queue.drain():
                    frame = json.loads(payload)
                    if not cursor.accept(frame):
                        continue
                    if compatibility:
                        if frame["type"] != "state":
                            continue
                        payload = safe_json(house_event(frame))
                    self.wfile.write(b"data: " + payload + b"\n\n")
                    self.wfile.flush()
                    last_write = time.monotonic()
                if time.monotonic() - last_write >= 5:
                    self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
                    last_write = time.monotonic()
                if select.select([self.request], [], [], 0.25)[0]:
                    break
        except OSError:
            pass
        finally:
            unregister_live_viewer(client_id)

    def reply(self, data):
        body = safe_json(data)
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def end_headers(self):
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "SAMEORIGIN")
        self.send_header("Referrer-Policy", "no-referrer")
        # 旧版在 end_headers 里直接读 self.path，个别错误路径下该属性尚未建立，
        # 会抛 AttributeError 刷日志；这里改为安全取值。
        # 已升级成 WebSocket 的连接不能再走 HTTP 头输出，否则会往帧流里
        # 塞进一段 "X-Content-Type-Options..." 文本，客户端握手直接失败。
        if getattr(self, "_ws_upgraded", False):
            return
        path = getattr(self, "path", "") or ""
        if path.split("?")[0].endswith((".mp4", ".jpg", ".png", ".webp")):
            # 视频/图片可以长缓存；页面与脚本必须每次回源，避免真机拿到旧版。
            self.send_header("Cache-Control", "private, max-age=3600")
        elif path.split("?")[0].endswith((".html", ".js", ".css")):
            self.send_header("Cache-Control", "no-store, must-revalidate")
        super().end_headers()

    def log_message(self, fmt, *args):
        # No personal data is served by this read-only process.
        super().log_message(fmt, *args)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=18777)
    args = parser.parse_args()
    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    httpd.daemon_threads = True
    bridge.start()
    print("fish-house LAN viewer listening on http://%s:%d/" % (args.host, args.port),
          flush=True)
    print("live WebSocket (read-only, same origin): ws://%s:%d/ws"
          % (args.host, args.port), flush=True)
    try:
        httpd.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        STOP_EVENT.set()
        bridge.stop()
        if qq_poller is not None:
            qq_poller.stop()
        httpd.server_close()


if __name__ == "__main__":
    main()
