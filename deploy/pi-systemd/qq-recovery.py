#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""大肥鱼 P0 恢复守护（watchdog）——默认纯观测，唯一进程所有者是 dafeiyu.service。

安装位置：~/.local/bin/qq-recovery.py（由 qq-recovery.timer 周期性调用）
仓库路径：deploy/pi-systemd/qq-recovery.py

设计结论（对应 2026-11-09 迁移失败的根因）
------------------------------------------------------------------
旧现场里 dafeiyu.service 与 qq-recovery 同时具备「启动/重启引擎」的能力，
且旧启动脚本 Type=forking + 启动后循环等 status，导致：
  * 180s TimeoutStartSec 超时后 auto-restart；
  * 旧 session.scope 实例与新 dafeiyu.service cgroup 实例短暂并存（双进程）。

本修复把职责切成互不竞争的两层：

  唯一进程所有者  = dafeiyu.service（Type=simple，前台进程即 MainPID）
  引擎自动重启    = 仅 systemd 的 Restart=on-failure + StartLimitBurst/IntervalSec
  启动器          = dafeiyu-launch.sh：flock 单例 + 真实路径检查，绝不 kill
  恢复守护(本脚本) = 只观测、分类、计数、告警；默认不执行任何 start/stop/restart/kill

「仅重连 WebSocket」与「才重启 QQ」的判定
------------------------------------------------------------------
  * QQ 进程存活、OneBot(127.0.0.1:3001)断开：
      仅由引擎内置的有限退避重连 + 低频守护重连 WebSocket。
      本脚本只观测，不重连、不重启引擎、绝不重启 QQ。
  * QQ 进程也缺失：
      只产出 human_required 告警。绝不自动拉起 napcat-qq.service——
      现场该单元 enabled/inactive，盲目启动极可能造成同一账号重复登录。
      是否处理由维护窗口人工按命令清单确认。
  * 引擎进程缺失：
      交给 systemd 自己按 Restart=on-failure 处理（唯一所有者）。
      本脚本默认 observe-only；仅当显式 --managed 且全部门槛通过时，
      才允许「一次受控的 systemctl --user restart dafeiyu.service」，
      且受重试上限/冷却/网络宽限约束，并仍是只碰引擎、不碰 QQ。
      ⚠ --managed 默认关闭；本轮交付不启用，需 ChatGPT 验收后另行决定。

隐私
------------------------------------------------------------------
  不读取、不打印、不持久化任何账号、QQ 号、Token、API Key、Cookie、
  Session、聊天正文或含用户名的绝对路径。日志只输出固定分类码与计数。
  状态文件仅含时间戳与整数计数，路径由 $HOME 派生，权限 0600。
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import os
import socket
import stat
import sys
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Callable, List, Optional, Tuple

# ---------------------------------------------------------------------------
# 配置：只从 DAFEIYU_RECOVERY_* 环境变量读整数阈值，不读任何私密配置文件。
# ---------------------------------------------------------------------------


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
    network_grace_probes: int = 3     # 网络恢复后连续 N 次 ok 才允许恢复动作
    max_actions_per_window: int = 3   # 一个窗口内最多 N 次恢复动作
    window_seconds: int = 900         # 重试窗口长度
    cooldown_seconds: int = 300       # 达到上限后的冷却时长
    min_action_interval: int = 60     # 两次动作之间的最小间隔
    activating_grace_seconds: int = 120  # unit 处于 activating 的宽限，绝不叠加第二个 start
    managed: bool = False             # 默认 observe-only，绝不自动重启

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


# ---------------------------------------------------------------------------
# 探针快照：所有外部事实先采集成一个不可变 dataclass，再交给纯函数 decide()。
# 这样测试无需真实 systemd / Pi / 网络，用合成夹具即可覆盖全部场景。
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Probe:
    engine_count: int = 0             # 真实可执行路径匹配的引擎进程数
    engine_owned_by_unit: bool = False  # 是否归属 dafeiyu.service cgroup
    unit_state: str = "unknown"       # active / activating / failed / inactive / unknown
    unit_activating_seconds: float = 0.0
    onebot_alive: bool = False        # 127.0.0.1:3001 可连
    network_ok: bool = False          # 存在默认路由 / 网关可达
    qq_alive: bool = False            # QQ 进程存在（按可执行路径，绝不读 cmdline）

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
    code: str        # observe / warn / cool_down / human_required / recover_engine
    reason: str      # 固定分类码，见 REASONS；绝不含路径或账号
    state: State


# 只允许这些固定分类码进入日志，杜绝把路径/命令行/账号带进日志。
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


# ---------------------------------------------------------------------------
# 纯决策函数：无 IO、无时间隐式依赖（now 由调用方注入），便于确定性单测。
# ---------------------------------------------------------------------------


def decide(probe: Probe, state: State, cfg: Config, now: float) -> Decision:
    """根据探针与状态返回决策 + 新状态。绝不产生副作用。

    判定优先级（越靠前越保守）：
      1. 冷却中           -> 不动作
      2. 引擎进程存在     -> 只观测；双实例/外来实例 -> 人工确认，绝不 kill
      3. unit activating  -> 给宽限，绝不叠加第二个 start（超时/双进程的关键防线）
      4. 网络未恢复       -> 宽限，不在断网期反复重启
      5. QQ 缺失          -> 人工确认，绝不自动拉起 QQ
      6. 重试上限/间隔    -> 冷却
      7. 其余             -> 默认 observe-only；显式 managed 才允许一次受控引擎重启
    """
    # 窗口滚动：超过 window_seconds 重新计数。
    if now - state.window_start >= cfg.window_seconds:
        state = replace(state, window_start=now, actions_in_window=0)

    # 网络连续 ok 计数（用于「网络恢复宽限」）。
    net_ok = state.consecutive_network_ok + 1 if probe.network_ok else 0
    state = replace(state, consecutive_network_ok=net_ok)

    def out(code: str, reason: str, st: Optional[State] = None) -> Decision:
        """构造决策；不传 st 时使用当前 state（含窗口滚动与网络计数）。"""
        assert reason in REASONS, "日志分类码必须来自白名单"
        return Decision(code=code, reason=reason, state=state if st is None else st)

    # 1) 冷却：任何动作都停止，等待冷却结束。
    if now < state.cooldown_until:
        return out("cool_down", "cool_down_active")

    # 2) 引擎进程存在：唯一所有者是 systemd，绝不重复启动、绝不 kill。
    if probe.engine_count >= 1:
        if probe.engine_count > 1:
            return out("human_required", "dual_instance_manual_required")
        if not probe.engine_owned_by_unit:
            return out("human_required", "foreign_instance_manual_required")
        if probe.onebot_alive:
            return out("observe", "healthy")
        # QQ 活、OneBot 断：仅由引擎内部重连 WebSocket；脚本不动作、不重启 QQ。
        return out("observe", "onebot_down_engine_reconnects_ws")

    # 3) unit 正在启动：绝不叠加第二个 start。这直接避免旧现场的
    #    「activating 卡住 -> 超时 -> auto-restart -> 双进程」循环。
    if probe.unit_state == "activating":
        if probe.unit_activating_seconds < cfg.activating_grace_seconds:
            return out("observe", "unit_activating_grace")
        return out("warn", "unit_activating_suspect_timeout")

    # 4) 网络恢复宽限：断网期与刚恢复的一段时间内不做恢复动作。
    if not probe.network_ok:
        return out("observe", "network_down")
    if net_ok < cfg.network_grace_probes:
        return out("observe", "network_grace_pending")

    # 5) QQ 进程缺失：绝不自动拉起，避免重复登录；只告警交人工。
    if not probe.qq_alive:
        return out("human_required", "qq_missing_manual_required")

    # 6) 重试上限与间隔（退避/冷却策略）。
    if state.actions_in_window >= cfg.max_actions_per_window:
        st = replace(state, cooldown_until=now + cfg.cooldown_seconds)
        return out("cool_down", "retry_limit_reached", st)
    if state.last_action_at >= 0 and now - state.last_action_at < cfg.min_action_interval:
        return out("observe", "action_interval_pending")

    # 7) 引擎缺失且所有门槛通过：默认仍只观测。
    if not cfg.managed:
        return out("observe", "observe_only_mode")

    st = replace(
        state,
        actions_in_window=state.actions_in_window + 1,
        last_action_at=now,
    )
    return out("recover_engine", "managed_engine_restart", st)


# ---------------------------------------------------------------------------
# 环境抽象：真实环境只读 /proc / systemctl / socket；测试用合成夹具替换。
# ---------------------------------------------------------------------------


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
    """执行决策。返回是否真的改动了系统状态。

    默认（managed=False）下永远返回 False：脚本绝不 start/stop/restart/kill。
    """
    env.log(
        "warn" if decision.code in {"warn", "human_required"} else "info",
        decision.code,
        decision.reason,
    )
    if decision.code == "recover_engine" and cfg.managed:
        return env.restart_engine()
    return False


# ---------------------------------------------------------------------------
# 真实环境实现（部署在 Pi 上时使用；本轮 DSH 不运行、不部署）
# ---------------------------------------------------------------------------


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

    def unit_state(self) -> Tuple[str, float]:
        # 只读查询，绝不 start/stop/restart。
        import subprocess
        try:
            out = subprocess.run(
                ["systemctl", "--user", "show", "dafeiyu.service",
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
            # ExecMainStartTimestamp 是微秒级 UTC 时间戳；无法解析时按 0 处理（给足宽限）。
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

    def onebot_alive(self) -> bool:
        try:
            with socket.create_connection(("127.0.0.1", 3001), timeout=2):
                return True
        except OSError:
            return False

    def network_ok(self) -> bool:
        # 只检查内核是否存在默认路由，不解析任何地址内容、不打印。
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
        # 按可执行路径关键字识别 QQ 主进程；绝不读 cmdline（可能含凭证/账号）。
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

    def restart_engine(self) -> bool:
        # 仅在显式 --managed 且所有门槛通过时才会到达这里。
        # ⚠ 本轮交付不启用 --managed；此分支默认不可达。
        import subprocess
        try:
            result = subprocess.run(
                ["systemctl", "--user", "restart", "dafeiyu.service"],
                capture_output=True, text=True, timeout=20, check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return False
        return result.returncode == 0

    def log(self, level: str, code: str, reason: str) -> None:
        # 只打印固定分类码，绝不打印路径、账号、令牌或计数之外的任何外部文本。
        print("qq-recovery level=%s code=%s reason=%s" % (level, code, reason))


# ---------------------------------------------------------------------------
# 状态持久化：仅时间戳与整数，0600；不含任何凭据。
# ---------------------------------------------------------------------------


def state_directory() -> Path:
    """状态目录：默认从 $HOME 派生；可由环境变量覆盖以便本地夹具隔离。

    覆盖变量只接受一个目录路径，不含任何账号/令牌内容。
    """
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
        # 原子写，避免 timer 并发时写坏状态文件。
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
        # 状态写不进去也要能安全观测：不抛异常，不阻塞 timer。
        pass


class _NoLock:
    """无 fcntl 的平台（如 Windows 本地夹具）退化为无锁串行执行，不影响纯观测语义。"""

    def fileno(self) -> int:
        return -1

    def close(self) -> None:
        pass


def acquire_lock(lock_path: Path):
    """非阻塞 flock 单例；拿不到锁立即返回 None（timer 重叠时直接跳过本轮）。"""
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


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--managed", action="store_true",
                   help="允许一次受控的引擎重启（默认关闭，仅观测；需 ChatGPT 验收后决定）")
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
    return p


EXPLAIN = """\
唯一进程所有者：dafeiyu.service（Type=simple，前台进程即 MainPID）
引擎自动重启：仅 systemd Restart=on-failure + StartLimitBurst/IntervalSec
启动器：dafeiyu-launch.sh（flock 单例 + 真实路径预检，绝不 kill）
恢复守护：默认纯观测，持 flock 锁 + 网络恢复宽限 + 退避/冷却计数

场景                                  决策                     是否改动系统
QQ 活 + OneBot 断                     引擎内置重连 WS          否
QQ 活 + OneBot 通 + 引擎活            healthy 观测             否
引擎活但不止一个                      人工确认（绝不 kill）     否
引擎活但不归属 dafeiyu.service        人工确认（绝不 kill）     否
unit activating（宽限内）             观测，绝不叠加 start      否
unit activating（疑似超时）           告警，绝不叠加 start      否
网络断开 / 刚恢复未达宽限             观测，不重启              否
QQ 进程缺失                          人工确认，绝不自动拉起 QQ  否
引擎缺失、门槛通过、默认模式          观测（observe-only）      否
引擎缺失、门槛通过、显式 --managed    一次受控 restart 引擎     是（只碰引擎）
重试上限到达                          冷却                      否
"""


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    if args.explain:
        print(EXPLAIN)
        return 0

    cfg = Config.from_env(managed=args.managed)

    if args.probe_from_json:
        try:
            with open(args.probe_from_json, "r", encoding="utf-8") as fh:
                probe = Probe.from_json(fh.read())
        except (OSError, ValueError, TypeError) as exc:
            # 不回显异常里的路径细节。
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
        return 0
    finally:
        try:
            import fcntl
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
        except (OSError, ImportError):
            pass
        lock.close()


class _NullEnvironment(Environment):
    """--probe-from-json 夹具模式：绝不接触真实系统。"""

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
