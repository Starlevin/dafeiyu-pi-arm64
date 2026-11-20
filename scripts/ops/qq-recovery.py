#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""大肥鱼 P0 恢复守护（watchdog）——含 QQ/微信掉线自动重启自愈与冷却防风暴。

安装位置：~/.local/bin/qq-recovery.py（由 qq-recovery.timer 每分钟调用）
隐私与安全边界：
  * 绝不读取、打印或落盘任何账号、QQ 号、Token、API Key、Cookie 或聊天原文。
  * 绝不删除 ~/.config/QQ 或 ~/dafeiyu-wechat/client-session/profile 登录态目录。
  * 自动重启严格受连续失败次数门槛、网络连通前提及冷却窗口保护，避免无限重启风暴。
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import os
import re
import socket
import stat
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value >= 0 else default


@dataclass(frozen=True)
class Config:
    network_grace_probes: int = 3
    max_actions_per_window: int = 3
    window_seconds: int = 900
    cooldown_seconds: int = 300
    min_action_interval: int = 60
    activating_grace_seconds: int = 120
    managed: bool = False

    @classmethod
    def from_env(cls, managed: bool = False) -> "Config":
        return cls(
            network_grace_probes=_env_int("DAFEIYU_RECOVERY_NETWORK_GRACE", 3),
            max_actions_per_window=_env_int("DAFEIYU_RECOVERY_MAX_ACTIONS", 3),
            window_seconds=_env_int("DAFEIYU_RECOVERY_WINDOW", 900),
            cooldown_seconds=_env_int("DAFEIYU_RECOVERY_COOLDOWN", 300),
            min_action_interval=_env_int("DAFEIYU_RECOVERY_MIN_INTERVAL", 60),
            activating_grace_seconds=_env_int("DAFEIYU_RECOVERY_ACTIVATING_GRACE", 120),
            managed=managed,
        )


@dataclass(frozen=True)
class Probe:
    engine_count: int = 0
    engine_owned_by_unit: bool = False
    unit_state: str = "unknown"
    unit_activating_seconds: float = 0.0
    onebot_alive: bool = False
    network_ok: bool = False
    qq_alive: bool = False

    def to_json(self) -> str:
        return json.dumps(dataclasses.asdict(self), sort_keys=True)

    @classmethod
    def from_json(cls, text: str) -> "Probe":
        data = json.loads(text)
        valid = {f.name for f in dataclasses.fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in valid})


@dataclass(frozen=True)
class State:
    window_start: float = 0.0
    actions_in_window: int = 0
    last_action_at: float = -1.0
    cooldown_until: float = 0.0
    consecutive_network_ok: int = 0

    def to_json(self) -> str:
        return json.dumps(dataclasses.asdict(self), sort_keys=True)

    @classmethod
    def from_json(cls, text: str) -> "State":
        data = json.loads(text)
        valid = {f.name for f in dataclasses.fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in valid})


@dataclass(frozen=True)
class Decision:
    code: str
    reason: str
    state: State


REASONS = frozenset({
    "healthy",
    "onebot_down_engine_reconnects_ws",
    "dual_instance_manual_required",
    "foreign_instance_manual_required",
    "unit_activating_grace",
    "unit_activating_suspect_timeout",
    "network_down",
    "network_grace_pending",
    "qq_missing_manual_required",
    "cool_down_active",
    "retry_limit_reached",
    "action_interval_pending",
    "observe_only_mode",
    "managed_engine_restart",
})

OBSERVE_DECISIONS = frozenset({"observe", "warn", "cool_down", "human_required"})


def decide(probe: Probe, state: State, cfg: Config, now: float) -> Decision:
    if now - state.window_start >= cfg.window_seconds:
        state = replace(state, window_start=now, actions_in_window=0)

    net_ok = state.consecutive_network_ok + 1 if probe.network_ok else 0
    state = replace(state, consecutive_network_ok=net_ok)

    def out(code: str, reason: str, st: Optional[State] = None) -> Decision:
        assert reason in REASONS, "日志分类码必须来自白名单"
        return Decision(code=code, reason=reason, state=state if st is None else st)

    if now < state.cooldown_until:
        return out("cool_down", "cool_down_active")

    if probe.engine_count >= 1:
        if probe.engine_count > 1:
            return out("human_required", "dual_instance_manual_required")
        if not probe.engine_owned_by_unit:
            return out("human_required", "foreign_instance_manual_required")
        if probe.onebot_alive:
            return out("observe", "healthy")
        return out("observe", "onebot_down_engine_reconnects_ws")

    if probe.unit_state == "activating":
        if probe.unit_activating_seconds < cfg.activating_grace_seconds:
            return out("observe", "unit_activating_grace")
        return out("warn", "unit_activating_suspect_timeout")

    if not probe.network_ok:
        return out("observe", "network_down")
    if net_ok < cfg.network_grace_probes:
        return out("observe", "network_grace_pending")

    if not probe.qq_alive:
        return out("human_required", "qq_missing_manual_required")

    if state.actions_in_window >= cfg.max_actions_per_window:
        st = replace(state, cooldown_until=now + cfg.cooldown_seconds)
        return out("cool_down", "retry_limit_reached", st)
    if state.last_action_at >= 0 and now - state.last_action_at < cfg.min_action_interval:
        return out("observe", "action_interval_pending")

    if not cfg.managed:
        return out("observe", "observe_only_mode")

    st = replace(
        state,
        actions_in_window=state.actions_in_window + 1,
        last_action_at=now,
    )
    return out("recover_engine", "managed_engine_restart", st)


class Environment:
    engine_bin: str = ""

    def engine_processes(self) -> List[Tuple[int, bool]]:
        raise NotImplementedError

    def unit_state(self) -> Tuple[str, float]:
        raise NotImplementedError

    def onebot_alive(self) -> bool:
        raise NotImplementedError

    def network_ok(self) -> bool:
        raise NotImplementedError

    def qq_alive(self) -> bool:
        raise NotImplementedError

    def restart_engine(self) -> bool:
        raise NotImplementedError

    def log(self, level: str, code: str, reason: str) -> None:
        raise NotImplementedError


def collect_probe(env: Environment) -> Probe:
    processes = env.engine_processes()
    unit, activating = env.unit_state()
    return Probe(
        engine_count=len(processes),
        engine_owned_by_unit=all(owned for _, owned in processes) if processes else False,
        unit_state=unit,
        unit_activating_seconds=activating,
        onebot_alive=env.onebot_alive(),
        network_ok=env.network_ok(),
        qq_alive=env.qq_alive(),
    )


def apply(decision: Decision, env: Environment, cfg: Config) -> bool:
    env.log(
        "warn" if decision.code in {"warn", "human_required"} else "info",
        decision.code,
        decision.reason,
    )
    if decision.code == "recover_engine" and cfg.managed:
        return env.restart_engine()
    return False


class SystemEnvironment(Environment):
    def __init__(self, engine_bin: str):
        self.engine_bin = os.path.realpath(engine_bin)

    def _exe_matches(self, pid: str, needle: str) -> bool:
        try:
            exe = os.path.realpath("/proc/%s/exe" % pid)
        except OSError:
            return False
        return exe == needle

    def _unit_owned(self, pid: str) -> bool:
        try:
            with open("/proc/%s/cgroup" % pid, "r", encoding="utf-8", errors="replace") as fh:
                return "dafeiyu.service" in fh.read()
        except OSError:
            return False

    def engine_processes(self) -> List[Tuple[int, bool]]:
        found: List[Tuple[int, bool]] = []
        try:
            entries = os.listdir("/proc")
        except OSError:
            return found
        for entry in entries:
            if not entry.isdigit():
                continue
            if self._exe_matches(entry, self.engine_bin):
                found.append((int(entry), self._unit_owned(entry)))
        return found

    def show_unit(self, unit: str) -> Tuple[str, float]:
        try:
            out = subprocess.run(
                ["systemctl", "--user", "show", unit,
                 "--property=ActiveState,SubState,ExecMainStartTimestamp", "--no-pager"],
                capture_output=True, text=True, timeout=5, check=False,
            ).stdout
        except (OSError, subprocess.SubprocessError):
            return "unknown", 0.0
        props = {}
        for line in out.splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                props[k] = v
        active = props.get("ActiveState", "unknown")
        sub = props.get("SubState", "")
        if active == "active" and sub in {"running", "exited"}:
            return "active", 0.0
        if active == "activating" or sub == "start":
            raw = props.get("ExecMainStartTimestamp", "")
            elapsed = 0.0
            try:
                import datetime
                dt = datetime.datetime.strptime(raw.strip(), "%a %Y-%m-%d %H:%M:%S %Z")
                elapsed = max(0.0, time.time() - dt.replace(tzinfo=datetime.timezone.utc).timestamp())
            except (ValueError, TypeError):
                elapsed = 0.0
            return "activating", elapsed
        if active == "failed" or sub == "failed":
            return "failed", 0.0
        return "inactive", 0.0

    def unit_state(self) -> Tuple[str, float]:
        return self.show_unit("dafeiyu.service")

    def onebot_alive(self) -> bool:
        try:
            with socket.create_connection(("127.0.0.1", 3001), timeout=2):
                return True
        except OSError:
            return False

    def network_ok(self) -> bool:
        try:
            with open("/proc/net/route", "r", encoding="utf-8", errors="replace") as fh:
                for line in fh.readlines()[1:]:
                    parts = line.split()
                    if len(parts) > 1 and parts[1] == "00000000":
                        return True
        except OSError:
            pass
        return False

    def qq_alive(self) -> bool:
        try:
            entries = os.listdir("/proc")
        except OSError:
            return False
        for entry in entries:
            if not entry.isdigit():
                continue
            try:
                exe = os.path.realpath("/proc/%s/exe" % entry)
            except OSError:
                continue
            if exe.endswith("/qq") or exe.endswith("/QQ"):
                return True
        return False

    def restart_unit(self, unit: str, timeout: int = 30) -> bool:
        try:
            result = subprocess.run(
                ["systemctl", "--user", "restart", unit],
                capture_output=True, text=True, timeout=timeout, check=False,
            )
            return result.returncode == 0
        except (OSError, subprocess.SubprocessError):
            return False

    def restart_engine(self) -> bool:
        return self.restart_unit("dafeiyu.service", timeout=20)

    def qq_control_connected(self) -> Optional[bool]:
        """Read-only loopback check of QQ engine connected state. Never logs tokens."""
        log_path = Path.home() / "dafeiyu-test" / "run" / "qq-bot.log"
        if not log_path.exists():
            return None
        try:
            text = log_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None
        m_tok = re.search(r"QQ_CONTROL_URL=\S*#([A-Za-z0-9_\-]+)", text)
        m_port = re.search(r"http://127\.0\.0\.1:(\d+)", text)
        if not m_tok or not m_port:
            return None
        base = "http://127.0.0.1:" + m_port.group(1)
        req = urllib.request.Request(
            base + "/api/status",
            headers={"x-qq-control": m_tok.group(1), "Origin": base},
            method="GET",
        )
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                if resp.status != 200:
                    return None
                data = json.load(resp)
                return bool(data.get("connected"))
        except (OSError, urllib.error.URLError, ValueError):
            return None

    def wechat_core_healthz(self) -> bool:
        try:
            with urllib.request.urlopen("http://127.0.0.1:19301/healthz", timeout=4) as resp:
                if resp.status != 200:
                    return False
                data = json.load(resp)
                return bool(data.get("ok"))
        except (OSError, urllib.error.URLError, ValueError):
            return False

    def wechat_window_probe(self, click_login: bool = False) -> Optional[Dict[str, Any]]:
        """只读 AT-SPI 探针；失败或超时返回 None（绝不因探测失败而重启客户端）。"""
        script = WECHAT_PROBE_BIN
        if not script.exists():
            return None
        args = [sys.executable, str(script)]
        if click_login:
            args.append("--click-login")
        try:
            result = subprocess.run(args, capture_output=True, text=True,
                                    timeout=40, check=False)
        except (OSError, subprocess.SubprocessError):
            return None
        line = (result.stdout or "").strip().splitlines()
        if not line:
            return None
        try:
            data = json.loads(line[-1])
        except (ValueError, TypeError):
            return None
        return data if isinstance(data, dict) else None

    def send_admin_alert(self, message: str) -> bool:
        """经本机回环 QQ 中继私聊管理员；只回传布尔结果，不打印任何凭据。"""
        script = WECHAT_ALERT_BIN
        if not script.exists():
            return False
        try:
            result = subprocess.run(
                [sys.executable, str(script), message],
                capture_output=True, text=True, timeout=30, check=False)
        except (OSError, subprocess.SubprocessError):
            return False
        return result.returncode == 0 and "ALERT_SENT=true" in (result.stdout or "")

    def log(self, level: str, code: str, reason: str) -> None:
        print("qq-recovery level=%s code=%s reason=%s" % (level, code, reason))


# ---------------------------------------------------------------------------
# 掉线自愈状态管理（/tmp/dafeiyu-qq-recovery-state.json，仅含计数与时间戳）
# ---------------------------------------------------------------------------

AUTO_STATE_PATH = Path("/tmp/dafeiyu-qq-recovery-state.json")

# --- 微信真实登录态（AT-SPI 窗口几何，不把 19301/healthz 当登录态） ---------
WECHAT_PROBE_BIN = Path.home() / ".local" / "bin" / "wechat-window-probe.py"
WECHAT_ALERT_BIN = Path.home() / ".local" / "bin" / "qq-admin-alert.py"
WECHAT_WINDOW_PROBE_INTERVAL = 120.0   # 每 2 分钟最多探测一次，降低 3B 负载
WECHAT_LOGIN_ALERT_STREAK = 2          # 连续 2 次判定掉线才告警，抑制抖动
WECHAT_LOGIN_ALERT_COOLDOWN = 21600.0  # 同一故障 6 小时内最多一次告警
WECHAT_LOGIN_ALERT_TEXT = (
    "【大肥鱼告警】微信客户端已掉线：检测到登录/扫码窗口（约 280×380），"
    "QQ 侧仍在线。请尽快在树莓派上重新扫码；登录态目录未被改动。"
)
WECHAT_LOGIN_RECOVER_TEXT = "【大肥鱼恢复】微信已重新登录，主窗口恢复 880×640。"


def classify_wechat_window(snapshot: Optional[Dict[str, Any]]) -> str:
    """把 AT-SPI 快照归类为 logged_in / login_window / unknown（纯函数，可单测）。"""
    if not isinstance(snapshot, dict) or not snapshot.get("ok"):
        return "unknown"
    windows = snapshot.get("windows") or []
    sizes = []
    for win in windows:
        try:
            sizes.append((int(win.get("w", 0)), int(win.get("h", 0))))
        except (TypeError, ValueError):
            continue
    if any(w >= 600 and h >= 450 for w, h in sizes):
        return "logged_in"
    if sizes and all(w <= 420 and h <= 460 for w, h in sizes):
        return "login_window"
    return "unknown"


def _load_auto_state(path: Path = AUTO_STATE_PATH) -> Dict[str, Any]:
    default: Dict[str, Any] = {
        "onebot_down_streak": 0,
        "dafeiyu_disconnected_streak": 0,
        "wechat_core_down_streak": 0,
        "last_napcat_restart_at": 0.0,
        "last_dafeiyu_restart_at": 0.0,
        "last_wechat_restart_at": {},
        "wechat_window_state": "unknown",
        "wechat_window_probed_at": 0.0,
        "wechat_login_down_streak": 0,
        "last_wechat_login_alert_at": 0.0,
        "wechat_login_alert_active": False,
    }
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            default.update(data)
    except (OSError, ValueError, TypeError):
        pass
    if not isinstance(default.get("last_wechat_restart_at"), dict):
        default["last_wechat_restart_at"] = {}
    return default


def _save_auto_state(state: Dict[str, Any], path: Path = AUTO_STATE_PATH) -> None:
    try:
        tmp = path.with_suffix(".json.tmp")
        fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(state, fh, sort_keys=True)
                fh.write("\n")
        finally:
            os.chmod(str(tmp), stat.S_IRUSR | stat.S_IWUSR)
        os.replace(str(tmp), str(path))
    except OSError:
        pass


def run_service_watchdog(env: SystemEnvironment, probe: Probe, now: float) -> List[str]:
    """QQ + 微信掉线自愈主巡检：允许掉线自动重启，带冷却防风暴，绝不删登录态。"""
    st = _load_auto_state()
    actions: List[str] = []

    if not probe.network_ok:
        st["onebot_down_streak"] = 0
        st["dafeiyu_disconnected_streak"] = 0
        st["wechat_core_down_streak"] = 0
        _save_auto_state(st)
        env.log("info", "watchdog", "network_down_skip_restart")
        return actions

    # 1) QQ / NapCat OneBot 3001 自愈
    napcat_state, _ = env.show_unit("napcat-qq.service")
    if not probe.onebot_alive:
        st["onebot_down_streak"] = int(st.get("onebot_down_streak", 0)) + 1
        need_napcat_restart = (
            st["onebot_down_streak"] >= 3
            or napcat_state in {"failed", "inactive"}
        )
        if need_napcat_restart and napcat_state != "activating":
            if now - float(st.get("last_napcat_restart_at", 0.0)) >= 900.0:
                env.log("warn", "auto_recover", "restart_napcat_onebot_down")
                if env.restart_unit("napcat-qq.service", timeout=60):
                    st["last_napcat_restart_at"] = now
                    st["onebot_down_streak"] = 0
                    actions.append("napcat-qq.service")
            else:
                env.log("info", "cool_down", "napcat_restart_cooldown")
    else:
        st["onebot_down_streak"] = 0
        # OneBot 3001 存活，再检查 dafeiyu.service 状态及 connected 状态
        dfy_state, _ = env.show_unit("dafeiyu.service")
        if dfy_state in {"failed", "inactive"}:
            if now - float(st.get("last_dafeiyu_restart_at", 0.0)) >= 300.0:
                env.log("warn", "auto_recover", "restart_dafeiyu_inactive")
                if env.restart_unit("dafeiyu.service", timeout=25):
                    st["last_dafeiyu_restart_at"] = now
                    st["dafeiyu_disconnected_streak"] = 0
                    actions.append("dafeiyu.service")
        elif dfy_state == "active":
            connected = env.qq_control_connected()
            if connected is False:
                st["dafeiyu_disconnected_streak"] = int(st.get("dafeiyu_disconnected_streak", 0)) + 1
                if st["dafeiyu_disconnected_streak"] >= 3:
                    if now - float(st.get("last_dafeiyu_restart_at", 0.0)) >= 300.0:
                        env.log("warn", "auto_recover", "restart_dafeiyu_disconnected")
                        if env.restart_unit("dafeiyu.service", timeout=25):
                            st["last_dafeiyu_restart_at"] = now
                            st["dafeiyu_disconnected_streak"] = 0
                            actions.append("dafeiyu.service")
            elif connected is True:
                st["dafeiyu_disconnected_streak"] = 0

    # 2) 微信真实登录态（AT-SPI 窗口几何；19301/healthz 200 不代表已登录）
    window_state = str(st.get("wechat_window_state", "unknown"))
    probed_at = float(st.get("wechat_window_probed_at", 0.0))
    auto_click = os.environ.get("DAFEIYU_WECHAT_AUTO_LOGIN_CLICK", "1") == "1"
    client_state, _ = env.show_unit("dafeiyu-wechat-client.service")
    if now - probed_at >= WECHAT_WINDOW_PROBE_INTERVAL and client_state == "active":
        snapshot = env.wechat_window_probe(
            click_login=auto_click and window_state == "login_window")
        new_state = classify_wechat_window(snapshot)
        if new_state == "unknown":
            env.log("info", "wechat_window", "state=unknown_probe_failed")
        else:
            clicked = bool((snapshot or {}).get("login", {}).get("clicked"))
            env.log("info", "wechat_window",
                    "state=%s_login_click=%d" % (new_state, 1 if clicked else 0))
        if new_state == "login_window":
            st["wechat_login_down_streak"] = int(
                st.get("wechat_login_down_streak", 0)) + 1
        elif new_state == "logged_in":
            was_alerted = bool(st.get("wechat_login_alert_active"))
            st["wechat_login_down_streak"] = 0
            if was_alerted:
                if env.send_admin_alert(WECHAT_LOGIN_RECOVER_TEXT):
                    st["wechat_login_alert_active"] = False
                    actions.append("wechat_login_alert")
                    env.log("info", "admin_alert", "wechat_login_recovered")
        st["wechat_window_state"] = new_state
        st["wechat_window_probed_at"] = now
        window_state = new_state
    elif client_state != "active" and now - probed_at >= WECHAT_WINDOW_PROBE_INTERVAL:
        env.log("info", "wechat_window", "skip_client_%s" % client_state)

    if (window_state == "login_window"
            and int(st.get("wechat_login_down_streak", 0)) >= WECHAT_LOGIN_ALERT_STREAK):
        if now - float(st.get("last_wechat_login_alert_at", 0.0)) >= WECHAT_LOGIN_ALERT_COOLDOWN:
            env.log("warn", "admin_alert", "wechat_login_window_detected")
            if env.send_admin_alert(WECHAT_LOGIN_ALERT_TEXT):
                st["last_wechat_login_alert_at"] = now
                st["wechat_login_alert_active"] = True
                actions.append("wechat_login_alert")
                env.log("info", "admin_alert", "wechat_login_alert_sent")

    # 3) 微信 core / client / bridge 自愈
    wx_last: Dict[str, float] = st.get("last_wechat_restart_at", {})
    core_state, _ = env.show_unit("dafeiyu-wechat-core.service")
    core_ok = env.wechat_core_healthz()
    if core_ok:
        st["wechat_core_down_streak"] = 0
    else:
        st["wechat_core_down_streak"] = int(st.get("wechat_core_down_streak", 0)) + 1

    if core_state != "activating" and (
        core_state in {"failed", "inactive"} or st["wechat_core_down_streak"] >= 3
    ):
        if now - float(wx_last.get("dafeiyu-wechat-core.service", 0.0)) >= 300.0:
            env.log("warn", "auto_recover", "restart_wechat_core")
            if env.restart_unit("dafeiyu-wechat-core.service", timeout=30):
                wx_last["dafeiyu-wechat-core.service"] = now
                st["wechat_core_down_streak"] = 0
                actions.append("dafeiyu-wechat-core.service")

    for unit, cooldown, reason in (
        ("dafeiyu-wechat-client.service", 600.0, "restart_wechat_client"),
        ("dafeiyu-wechat-bridge.service", 300.0, "restart_wechat_bridge"),
    ):
        ustate, _ = env.show_unit(unit)
        if ustate in {"failed", "inactive"}:
            # 登录/扫码窗口仍在时不重启 client，避免打断用户扫码。
            if unit == "dafeiyu-wechat-client.service" and window_state == "login_window":
                env.log("info", "wechat_window", "skip_client_restart_login_window")
                continue
            if now - float(wx_last.get(unit, 0.0)) >= cooldown:
                env.log("warn", "auto_recover", reason)
                if env.restart_unit(unit, timeout=45):
                    wx_last[unit] = now
                    actions.append(unit)

    st["last_wechat_restart_at"] = wx_last
    _save_auto_state(st)
    if not actions:
        env.log(
            "info",
            "watchdog_ok",
            "onebot_streak=%d_dfy_streak=%d_wx_core_streak=%d"
            % (
                int(st.get("onebot_down_streak", 0)),
                int(st.get("dafeiyu_disconnected_streak", 0)),
                int(st.get("wechat_core_down_streak", 0)),
            ),
        )
    return actions


def state_directory() -> Path:
    override = os.environ.get("DAFEIYU_RECOVERY_STATE_DIR")
    if override:
        return Path(override)
    return Path.home() / ".local" / "share" / "dafeiyu"


def state_path() -> Path:
    return state_directory() / "qq-recovery-state.json"


def load_state(path: Path) -> State:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return State()
    try:
        return State.from_json(text)
    except (ValueError, TypeError, KeyError):
        return State()


def save_state(path: Path, state: State) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(state.to_json())
                fh.write("\n")
        finally:
            os.chmod(str(tmp), stat.S_IRUSR | stat.S_IWUSR)
        os.replace(str(tmp), str(path))
    except OSError:
        pass


class _NoLock:
    def fileno(self) -> int:
        return -1

    def close(self) -> None:
        pass


def acquire_lock(lock_path: Path):
    try:
        import fcntl
    except ImportError:
        return _NoLock()
    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(lock_path, "a+")
    except OSError:
        return None
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.close()
        return None
    return fh


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--managed", action="store_true",
                   help="允许一次受控的引擎重启")
    p.add_argument("--observe-only", action="store_true",
                   help="仅观测，不执行 QQ/微信自动自愈重启")
    p.add_argument("--probe-from-json", metavar="FILE",
                   help="从 JSON 夹具读取探针，跳过真实 /proc/systemctl 采集（本地验收用）")
    p.add_argument("--explain", action="store_true",
                   help="打印决策矩阵说明后退出（不采集、不动作）")
    p.add_argument("--engine-bin", default=os.path.join(str(Path.home()), "dafeiyu-test", "dafeiyu-linux", "dafeiyu"),
                   help="引擎可执行文件真实路径（用于 /proc/exe 单例判定）")
    p.add_argument("--now", type=float, default=None,
                   help="固定时钟（本地夹具用；仅与 --probe-from-json 配合生效）")
    p.add_argument("--state-json", metavar="FILE", default=None,
                   help="从 JSON 夹具注入初始状态（本地夹具用）")
    p.add_argument("--wechat-status", action="store_true",
                   help="只读探测并输出微信窗口登录态（JSON），然后退出")
    return p


EXPLAIN = """\
唯一进程所有者：dafeiyu.service（Type=simple，前台进程即 MainPID）
恢复守护：持 flock 单例锁 + QQ OneBot 3001/dafeiyu 掉线重启自愈（15m 冷却）+ 微信 core/client/bridge 自愈
保护边界：绝不删除 ~/.config/QQ 或 client-session/profile 登录态数据
"""


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    if args.explain:
        print(EXPLAIN)
        return 0

    if args.wechat_status:
        env = SystemEnvironment(args.engine_bin)
        snapshot = env.wechat_window_probe()
        print(json.dumps({
            "state": classify_wechat_window(snapshot),
            "probe": snapshot,
        }, ensure_ascii=False, sort_keys=True))
        return 0

    cfg = Config.from_env(managed=args.managed)

    if args.probe_from_json:
        try:
            with open(args.probe_from_json, "r", encoding="utf-8") as fh:
                probe = Probe.from_json(fh.read())
        except (OSError, ValueError, TypeError):
            print("qq-recovery level=error code=observe reason=observe_only_mode")
            return 2
        env: Environment = _NullEnvironment()
    else:
        env = SystemEnvironment(args.engine_bin)

    lock_path = state_directory() / "qq-recovery.lock"
    lock = acquire_lock(lock_path)
    if lock is None:
        print("qq-recovery level=info code=observe reason=observe_only_mode")
        return 0
    try:
        if not args.probe_from_json:
            probe = collect_probe(env)
        path = state_path()
        if args.state_json and args.probe_from_json:
            try:
                with open(args.state_json, "r", encoding="utf-8") as fh:
                    state = State.from_json(fh.read())
            except (OSError, ValueError, TypeError, KeyError):
                state = State()
        else:
            state = load_state(path)
        now = args.now if (args.now is not None and args.probe_from_json) else time.time()
        decision = decide(probe, state, cfg, now)
        apply(decision, env, cfg)
        save_state(path, decision.state)
        if isinstance(env, SystemEnvironment) and not args.observe_only:
            run_service_watchdog(env, probe, now)
        return 0
    finally:
        try:
            import fcntl
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
        except (OSError, ImportError):
            pass
        lock.close()


class _NullEnvironment(Environment):
    def engine_processes(self): return []
    def unit_state(self): return "unknown", 0.0
    def onebot_alive(self): return False
    def network_ok(self): return False
    def qq_alive(self): return False
    def restart_engine(self): return False
    def log(self, level, code, reason):
        print("qq-recovery level=%s code=%s reason=%s" % (level, code, reason))


if __name__ == "__main__":
    sys.exit(main())
