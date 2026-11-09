#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""deploy/pi-systemd 的本地合成验收测试（P0）。

运行：
    python3 scripts/test-pi-systemd-p0.py
    python3 deploy/pi-systemd/qq-recovery.py --explain   # 决策矩阵自检
    python3 deploy/pi-systemd/qq-recovery.py --probe-from-json <fixture.json>

约束（与任务书一致）：
  * 完全本地，纯 Python 标准库；不启动第二个 QQ/机器人实例。
  * 不登录、不扫码、不真实发 QQ、不调用模型、不读取任何凭证/账号/聊天。
  * 不接触树莓派、不执行 systemctl start/restart、不部署生产。
  * 合成身份与本机夹具：FakeEnvironment 替换全部系统探针；被测脚本以
    --probe-from-json 模式导入，只测纯决策函数与静态文件内容。

覆盖场景（任务书第六节要求）：
  1. 已有进程（healthy / OneBot 通）
  2. 无进程（observe-only 默认不动作）
  3. 启动超时（activating 宽限内与疑似超时，绝不叠加第二个 start）
  4. PID 文件过期（本设计根本不读 PID 文件；静态断言单元与启动器无 PIDFile）
  5. 网络断开（断网期不做恢复动作）
  6. QQ 仍活着但 OneBot 断开（仅引擎内部重连 WS，脚本不动作、不重启 QQ）
  7. 双重启动竞态（双实例/外来实例 -> 人工确认，绝不 kill；flock 单例静态断言）
  8. 恢复重试上限（动作计数达上限进入冷却）
  9. 无凭据日志（日志只含固定分类码；状态文件仅时间戳与整数）
 10. 网络恢复宽限（连续 N 次 ok 前不动作）
 11. 动作最小间隔（退避）
 12. 显式 --managed 才允许一次受控引擎重启，且只碰引擎
 13. 启动器：无 status 轮询、无 kill、无 fork、exec 前台、退出码 78
 14. 单元：Type=simple、无 PIDFile、RestartPreventExitStatus、StartLimit
 15. 隐私：单元/脚本不引用账号、令牌、密钥、Cookie、私有备份包
 16. CLI --probe-from-json 夹具模式端到端（不接触真实系统）
"""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEPLOY = ROOT / "deploy" / "pi-systemd"
RECOVERY = DEPLOY / "qq-recovery.py"

# 以模块方式加载被测脚本，避免任何 __main__ 副作用。
spec = importlib.util.spec_from_file_location("qq_recovery", RECOVERY)
assert spec and spec.loader
qr = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = qr
spec.loader.exec_module(qr)


class FakeEnvironment(qr.Environment):
    """合成系统探针：绝不接触真实 /proc / systemctl / 网络。"""

    def __init__(self, probe: qr.Probe):
        self.probe = probe
        self.restarts = 0
        self.logs = []

    def engine_processes(self):
        n = self.probe.engine_count
        owned = self.probe.engine_owned_by_unit
        return [(1000 + i, owned) for i in range(n)]

    def unit_state(self):
        return self.probe.unit_state, self.probe.unit_activating_seconds

    def onebot_alive(self): return self.probe.onebot_alive
    def network_ok(self): return self.probe.network_ok
    def qq_alive(self): return self.probe.qq_alive

    def restart_engine(self):
        self.restarts += 1
        return True

    def log(self, level, code, reason):
        self.logs.append((level, code, reason))


def cfg(**kw) -> qr.Config:
    base = dict(
        network_grace_probes=3,
        max_actions_per_window=3,
        window_seconds=900,
        cooldown_seconds=300,
        min_action_interval=60,
        activating_grace_seconds=120,
        managed=False,
    )
    base.update(kw)
    return qr.Config(**base)


def healthy(**overrides) -> qr.Probe:
    base = dict(
        engine_count=1,
        engine_owned_by_unit=True,
        unit_state="active",
        unit_activating_seconds=0.0,
        onebot_alive=True,
        network_ok=True,
        qq_alive=True,
    )
    base.update(overrides)
    return qr.Probe(**base)


class DecideTests(unittest.TestCase):
    """纯决策函数：无 IO、时间由调用方注入，结果确定。"""

    def test_1_existing_process_healthy(self):
        d = qr.decide(healthy(), qr.State(), cfg(), now=1000.0)
        self.assertEqual(d.code, "observe")
        self.assertEqual(d.reason, "healthy")

    def test_2_no_process_observe_only(self):
        p = healthy(engine_count=0, unit_state="inactive")
        d = qr.decide(p, qr.State(consecutive_network_ok=2), cfg(), now=1000.0)
        self.assertEqual(d.code, "observe")
        self.assertEqual(d.reason, "observe_only_mode")

    def test_3_startup_timeout_never_stacks_start(self):
        # 宽限内：观测，不动作。
        d = qr.decide(healthy(engine_count=0, unit_state="activating",
                              unit_activating_seconds=30),
                      qr.State(), cfg(), now=1000.0)
        self.assertEqual(d.reason, "unit_activating_grace")
        self.assertIn(d.code, qr.OBSERVE_DECISIONS)
        # 疑似超时：只告警，仍然不叠加第二个 start（双进程的核心防线）。
        d = qr.decide(healthy(engine_count=0, unit_state="activating",
                              unit_activating_seconds=300),
                      qr.State(), cfg(), now=1000.0)
        self.assertEqual(d.code, "warn")
        self.assertEqual(d.reason, "unit_activating_suspect_timeout")

    def test_3b_migration_failure_regression_sequence(self):
        """复现 2026-11-09 现场序列，证明新设计不再超时、不再双进程。

        现场：dafeiyu.service 保持 activating/start -> 180s 超时 -> auto-restart，
        短暂出现旧 session.scope 与新 cgroup 两个主进程。逐帧验证：
          (a) activating 期间守护绝不叠加第二个 start；
          (b) 即使出现两个实例，只告警交人工，绝不 kill、绝不自动再拉起；
          (c) 全程没有任何 recover_engine 动作。
        """
        p_activating = healthy(engine_count=0, unit_state="activating",
                               unit_activating_seconds=170)
        d = qr.decide(p_activating, qr.State(), cfg(managed=True), now=1000.0)
        self.assertEqual(d.reason, "unit_activating_suspect_timeout")
        self.assertIn(d.code, qr.OBSERVE_DECISIONS)
        self.assertNotEqual(d.code, "recover_engine")

        p_dual = healthy(engine_count=2, engine_owned_by_unit=False)
        d = qr.decide(p_dual, qr.State(), cfg(managed=True), now=1000.0)
        self.assertEqual(d.reason, "dual_instance_manual_required")
        self.assertNotEqual(d.code, "recover_engine")

        # 现场「旧 session.scope 实例 + 新 cgroup 实例」：外来实例同样是人工确认。
        p_foreign = healthy(engine_count=1, engine_owned_by_unit=False)
        d = qr.decide(p_foreign, qr.State(), cfg(managed=True), now=1000.0)
        self.assertEqual(d.reason, "foreign_instance_manual_required")
        self.assertNotEqual(d.code, "recover_engine")

    def test_5_network_disconnected(self):
        p = healthy(engine_count=0, unit_state="inactive", network_ok=False)
        d = qr.decide(p, qr.State(), cfg(), now=1000.0)
        self.assertEqual(d.reason, "network_down")
        self.assertIn(d.code, qr.OBSERVE_DECISIONS)

    def test_6_qq_alive_onebot_down_only_ws_reconnect(self):
        p = healthy(onebot_alive=False)
        d = qr.decide(p, qr.State(), cfg(), now=1000.0)
        self.assertEqual(d.reason, "onebot_down_engine_reconnects_ws")
        self.assertIn(d.code, qr.OBSERVE_DECISIONS)
        # 关键：QQ 活着但 OneBot 断时绝不重启 QQ，也绝不重启引擎。
        self.assertNotEqual(d.code, "recover_engine")

    def test_7_dual_start_race(self):
        # 双实例：人工确认，绝不 kill。
        d = qr.decide(healthy(engine_count=2), qr.State(), cfg(), now=1000.0)
        self.assertEqual(d.reason, "dual_instance_manual_required")
        self.assertEqual(d.code, "human_required")
        # 外来实例（非本单元 cgroup）：同样人工确认，绝不 kill。
        d = qr.decide(healthy(engine_count=1, engine_owned_by_unit=False),
                      qr.State(), cfg(), now=1000.0)
        self.assertEqual(d.reason, "foreign_instance_manual_required")
        self.assertEqual(d.code, "human_required")

    def test_8_retry_limit_cools_down(self):
        p = healthy(engine_count=0, unit_state="inactive")
        st = qr.State(window_start=500.0, actions_in_window=3,
                      last_action_at=900.0, cooldown_until=0.0,
                      consecutive_network_ok=3)
        d = qr.decide(p, st, cfg(), now=1000.0)
        self.assertEqual(d.reason, "retry_limit_reached")
        self.assertGreater(d.state.cooldown_until, 1000.0)
        # 冷却期内即使 managed 也不动作。
        d2 = qr.decide(p, d.state, cfg(managed=True), now=1010.0)
        self.assertEqual(d2.code, "cool_down")
        self.assertEqual(d2.reason, "cool_down_active")

    def test_10_network_grace_after_recovery(self):
        p = healthy(engine_count=0, unit_state="inactive", network_ok=True)
        # 注意：decide() 每轮会把 consecutive_network_ok 加 1，
        # 所以「第 N 次 ok」的夹具要从 N-1 起步（这里 3 次宽限 -> 从 2 起步）。
        for start in (0, 1):
            st = qr.State(window_start=500.0, consecutive_network_ok=start)
            d = qr.decide(p, st, cfg(managed=True), now=1000.0)
            self.assertEqual(d.reason, "network_grace_pending",
                             "start=%d 时应仍在网络恢复宽限内" % start)
        # 第 3 次 ok 后才允许（managed 下）动作。
        st = qr.State(window_start=500.0, consecutive_network_ok=2)
        d = qr.decide(p, st, cfg(managed=True), now=1010.0)
        self.assertEqual(d.code, "recover_engine")

    def test_11_min_action_interval_backoff(self):
        p = healthy(engine_count=0, unit_state="inactive")
        st = qr.State(window_start=500.0, actions_in_window=1,
                      last_action_at=990.0, consecutive_network_ok=3)
        d = qr.decide(p, st, cfg(managed=True, min_action_interval=60), now=1000.0)
        self.assertEqual(d.reason, "action_interval_pending")
        self.assertIn(d.code, qr.OBSERVE_DECISIONS)

    def test_12_managed_restart_is_the_only_action(self):
        p = healthy(engine_count=0, unit_state="inactive")
        st = qr.State(window_start=0.0, consecutive_network_ok=3)
        d = qr.decide(p, st, cfg(managed=True), now=1000.0)
        self.assertEqual(d.code, "recover_engine")
        self.assertEqual(d.state.actions_in_window, 1)
        self.assertEqual(d.state.last_action_at, 1000.0)
        # apply：默认模式绝不重启；managed 模式只调用 restart_engine（只碰引擎）。
        env_default = FakeEnvironment(p)
        self.assertFalse(qr.apply(d, env_default, cfg(managed=False)))
        self.assertEqual(env_default.restarts, 0)
        env_managed = FakeEnvironment(p)
        self.assertTrue(qr.apply(d, env_managed, cfg(managed=True)))
        self.assertEqual(env_managed.restarts, 1)
        self.assertFalse(hasattr(env_managed, "restart_qq"))

    def test_13_qq_missing_never_autostarts_qq(self):
        p = healthy(engine_count=0, unit_state="inactive", qq_alive=False)
        st = qr.State(window_start=0.0, consecutive_network_ok=3)
        d = qr.decide(p, st, cfg(managed=True), now=1000.0)
        self.assertEqual(d.reason, "qq_missing_manual_required")
        self.assertEqual(d.code, "human_required")
        self.assertNotEqual(d.code, "recover_engine")

    def test_9_log_reasons_are_whitelisted(self):
        # 所有决策的分类码都必须在白名单内，保证日志不含外部文本。
        scenarios = [
            healthy(), healthy(onebot_alive=False), healthy(engine_count=2),
            healthy(engine_count=1, engine_owned_by_unit=False),
            healthy(engine_count=0, unit_state="activating", unit_activating_seconds=1),
            healthy(engine_count=0, unit_state="activating", unit_activating_seconds=999),
            healthy(engine_count=0, unit_state="inactive", network_ok=False),
            healthy(engine_count=0, unit_state="inactive", qq_alive=False),
            healthy(engine_count=0, unit_state="inactive"),
        ]
        for p in scenarios:
            for managed in (False, True):
                d = qr.decide(p, qr.State(), cfg(managed=managed), now=1000.0)
                self.assertIn(d.reason, qr.REASONS)
        self.assertTrue(qr.OBSERVE_DECISIONS.issuperset(
            {"observe", "warn", "cool_down", "human_required"}))

    def test_state_serialization_has_no_credentials(self):
        st = qr.State(window_start=1.0, actions_in_window=2, last_action_at=3.0,
                      cooldown_until=0.0, consecutive_network_ok=3)
        data = json.loads(st.to_json())
        self.assertEqual(set(data), {
            "window_start", "actions_in_window", "last_action_at",
            "cooldown_until", "consecutive_network_ok"})
        self.assertTrue(all(isinstance(v, (int, float)) for v in data.values()))
        self.assertEqual(qr.State.from_json(st.to_json()), st)

    def test_probe_roundtrip(self):
        p = healthy(onebot_alive=False)
        self.assertEqual(qr.Probe.from_json(p.to_json()), p)


class StaticFileTests(unittest.TestCase):
    """静态断言：不允许回到旧设计（forking/PIDFile/status 轮询/kill）。"""

    def setUp(self):
        self.unit = (DEPLOY / "dafeiyu.service").read_text(encoding="utf-8")
        self.launch = (DEPLOY / "dafeiyu-launch.sh").read_text(encoding="utf-8")
        self.recovery = RECOVERY.read_text(encoding="utf-8")
        self.preflight = (DEPLOY / "dafeiyu-p0-preflight.sh").read_text(encoding="utf-8")
        self.recovery_service = (DEPLOY / "qq-recovery.service").read_text(encoding="utf-8")
        self.all_files = {
            name: (DEPLOY / name).read_text(encoding="utf-8")
            for name in (
                "dafeiyu.service", "dafeiyu-launch.sh", "qq-recovery.py",
                "qq-recovery.service", "qq-recovery.timer", "dafeiyu-p0-preflight.sh",
            )
        }

    def test_runtime_profile_retains_legacy_environment_and_safe_logging(self):
        self.assertIn('source "$PROFILE"', self.launch)
        self.assertIn('chmod 0600 "$LOG_FILE"', self.launch)
        self.assertIn('dafeiyu-test/dafeiyu-linux', self.unit)
        self.assertIn("DAFEIYU_RUNTIME_PROFILE", self.preflight)
        generator = (DEPLOY / "prepare-runtime-profile.py").read_text(encoding="utf-8")
        for key in ("DAFEIYU_DATA_DIR", "DAFEIYU_LLM_KEY_FILE",
                    "DAFEIYU_ONEBOT_TOKEN_FILE", "DAFEIYU_AUTO_RESUME",
                    "DAFEIYU_ALLOWED_TARGETS"):
            self.assertIn(key, generator)
        self.assertIn("0o600", generator)

    def test_shell_files_use_unix_line_endings(self):
        for name in ("dafeiyu-launch.sh", "dafeiyu-p0-preflight.sh"):
            self.assertNotIn(b"\r", (DEPLOY / name).read_bytes())

    def test_4_no_pidfile_anywhere(self):
        # PID 文件过期场景：新设计完全不依赖 PID 文件，systemd 直接跟踪 MainPID。
        # 只查非注释配置行：单元头部的说明注释会合法提到 PIDFile 的删除原因。
        unit_code = "\n".join(
            line for line in self.unit.splitlines()
            if line.strip() and not line.strip().startswith("#"))
        self.assertNotIn("PIDFile", unit_code)
        self.assertNotIn("dafeiyu.pid", self.launch)
        self.assertIn("Type=simple", self.unit)
        self.assertIn("RestartPreventExitStatus=78", self.unit)
        self.assertIn("StartLimitBurst", self.unit)
        self.assertIn("StartLimitIntervalSec", self.unit)

    def test_13_launcher_has_no_status_poll_no_kill_no_fork(self):
        self.assertNotIn("status", self.launch.split("exec \"$REAL_BIN\"")[-1])
        # 只查真实命令调用，不查注释/说明文字（中文说明里会合法出现这些词）。
        forbidden_commands = [
            "pkill ", "killall ", "kill -9 ", "systemctl ", "Type=forking",
            "dafeiyu-autostart", "&$", "nohup ", "setsid ",
        ]
        for token in forbidden_commands:
            self.assertNotIn(token, self.launch, "启动器不得出现命令：%s" % token)
        self.assertIn('exec "$REAL_BIN"', self.launch)
        self.assertIn("flock -n 9", self.launch)
        self.assertIn("EX_CONFIG=78", self.launch)
        self.assertIn("for proc in /proc/[0-9]*", self.launch)
        self.assertIn('"$proc/exe"', self.launch)

    def test_7_flock_and_race_protection_present(self):
        self.assertIn("flock -n 9", self.launch)
        self.assertIn("fcntl.LOCK_EX | fcntl.LOCK_NB", self.recovery)
        self.assertIn('[[ "$exe" == "$REAL_BIN" ]] || continue', self.launch)
        self.assertIn("dual_instance_manual_required", self.recovery)

    def test_12_managed_is_opt_in(self):
        # 参考 service 单元默认不带 --managed；脚本默认 observe-only。
        self.assertNotIn("--managed", self._code_lines("qq-recovery.service"))
        self.assertIn('action="store_true"', self.recovery)
        self.assertIn("managed: bool = False", self.recovery)
        self.assertIn('default=os.path.join', self.recovery)

    def _code_lines(self, name: str) -> str:
        """只保留代码行（去掉 # 注释行与空行），避免说明文字误伤断言。"""
        lines = []
        for line in self.all_files[name].splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            lines.append(line)
        return "\n".join(lines)

    def test_15_no_credentials_in_any_file(self):
        # 代码里不得读取凭证/账号/聊天文件，或引用含用户名的绝对路径、私有备份包。
        # 只检查代码行；注释里为说明「不读取 Cookie」而出现的词不算。
        # 拼接形式避免把含用户名的绝对路径字面量写进公开仓库文件。
        user_root = "/" + "Users" + "/"
        home_user = "/" + "home" + "/" + "starlevin"
        private_backup = "pre-upgrade" + ".tar.gz"
        forbidden_in_code = [
            "sk-", "api_key", "apikey", "API_KEY",
            "password", "passwd", "webui.json", "onebot11.json",
            "qq-state.json", private_backup, user_root, home_user,
            "DAFEIYU_API_KEY", "DEEPSEEK_API_KEY", "ONEBOT_TOKEN",
        ]
        for name in self.all_files:
            code = self._code_lines(name)
            for token in forbidden_in_code:
                self.assertNotIn(token, code, "%s 代码不得引用：%s" % (name, token))

    def test_15_no_private_backup_or_absolute_user_path_in_comments(self):
        # 这两类即使出现在注释里也不允许（任务书明令不得上传/打印私有备份内容）。
        tokens = ("pre-upgrade" + ".tar.gz", "/" + "Users" + "/",
                  "/" + "home" + "/" + "starlevin")
        for name, text in self.all_files.items():
            for token in tokens:
                self.assertNotIn(token, text, "%s 不得出现：%s" % (name, token))

    def test_15_preflight_is_read_only(self):
        # 只查真实命令；注释里的说明文字不算。
        for token in ("systemctl restart", "systemctl start", "systemctl stop",
                      "systemctl enable", "systemctl disable", "pkill", "killall",
                      "kill -9", "kill $", "kill \"", "kill $((", ">>", "rm ", "mv ", "cp ",
                      " > /", " > \"$", " > ~", "tee ", "sed -i"):
            self.assertNotIn(token, self.preflight, "预检脚本必须是只读的：%s" % token)
        self.assertIn("no changes made", self.preflight)

    def test_unit_ordering_has_no_qq_requires(self):
        # 不得 Wants/Requires/After 到 QQ 单元，避免误启第二个 QQ 登录态。
        for line in self.unit.splitlines():
            if line.startswith(("After=", "Wants=", "Requires=")):
                self.assertNotIn("napcat", line)
                self.assertNotIn("network-online", line)


class CLITests(unittest.TestCase):
    """CLI 端到端：--probe-from-json 夹具模式，绝不接触真实系统。"""

    def run_cli(self, probe: qr.Probe, extra=None, state: qr.State = None):
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            fixture = td_path / "probe.json"
            fixture.write_text(probe.to_json(), encoding="utf-8")
            state_dir = td_path / "state"
            state_dir.mkdir()
            args = [sys.executable, str(RECOVERY),
                    "--probe-from-json", str(fixture), "--now", "1000.0"]
            if state is not None:
                sf = td_path / "initial-state.json"
                sf.write_text(state.to_json(), encoding="utf-8")
                args += ["--state-json", str(sf)]
            if extra:
                args += extra
            env = dict(os.environ)
            env["DAFEIYU_RECOVERY_STATE_DIR"] = str(state_dir)
            result = subprocess.run(args, capture_output=True, text=True,
                                    timeout=30, env=env)
            state_file = state_dir / "qq-recovery-state.json"
            return result, {"exists": state_file.exists(), "text": state_file.read_text(encoding="utf-8") if state_file.exists() else "", "mode": state_file.stat().st_mode if state_file.exists() else 0}

    def test_16_happy_path_no_system_change(self):
        result, state_file = self.run_cli(healthy())
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("code=observe", result.stdout)
        self.assertIn("reason=healthy", result.stdout)
        self.assertTrue(state_file["exists"])
        # 状态文件仅含时间戳与整数；POSIX 上额外校验 0600。
        data = json.loads(state_file["text"])
        self.assertTrue(all(isinstance(v, (int, float)) for v in data.values()))
        if os.name == "posix":
            self.assertEqual(oct(state_file["mode"] & 0o777), "0o600")

    def test_16_qq_alive_onebot_down_cli(self):
        result, _ = self.run_cli(healthy(onebot_alive=False))
        self.assertEqual(result.returncode, 0)
        self.assertIn("reason=onebot_down_engine_reconnects_ws", result.stdout)
        self.assertNotIn("recover_engine", result.stdout)

    def test_16_dual_instance_cli(self):
        result, _ = self.run_cli(healthy(engine_count=2))
        self.assertEqual(result.returncode, 0)
        self.assertIn("reason=dual_instance_manual_required", result.stdout)
        self.assertNotIn("recover_engine", result.stdout)

    def test_16_activating_timeout_cli(self):
        result, _ = self.run_cli(healthy(engine_count=0, unit_state="activating",
                                         unit_activating_seconds=999))
        self.assertEqual(result.returncode, 0)
        self.assertIn("unit_activating_suspect_timeout", result.stdout)

    def test_16_managed_restart_does_not_touch_system_in_fixture_mode(self):
        # 夹具模式下 --managed 也只走 _NullEnvironment，restart_engine 返回 False。
        # 注入「网络宽限已满足」的初始状态，让决策走到 managed_engine_restart。
        p = healthy(engine_count=0, unit_state="inactive")
        st = qr.State(window_start=500.0, consecutive_network_ok=2)
        result, _ = self.run_cli(p, extra=["--managed"], state=st)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("managed_engine_restart", result.stdout)

    def test_explain_lists_decision_matrix(self):
        result = subprocess.run(
            [sys.executable, str(RECOVERY), "--explain"],
            capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0)
        self.assertIn("唯一进程所有者", result.stdout)
        self.assertIn("observe-only", result.stdout)

    def test_bad_fixture_does_not_leak_path(self):
        with tempfile.TemporaryDirectory() as td:
            bad = Path(td) / "bad.json"
            bad.write_text("not-json", encoding="utf-8")
            result = subprocess.run(
                [sys.executable, str(RECOVERY), "--probe-from-json", str(bad)],
                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 2)
        self.assertNotIn(str(bad), result.stdout + result.stderr)
        self.assertIn("code=observe", result.stdout)


class PrivacyTests(unittest.TestCase):
    """隐私：日志与状态不含任何外部文本。"""

    def test_log_output_is_only_fixed_codes(self):
        env = FakeEnvironment(healthy())
        d = qr.decide(env.probe, qr.State(), cfg(), now=1000.0)
        qr.apply(d, env, cfg())
        self.assertEqual(len(env.logs), 1)
        level, code, reason = env.logs[0]
        self.assertIn(level, {"info", "warn"})
        self.assertIn(reason, qr.REASONS)
        self.assertNotIn("/", reason)

    def test_no_credentials_read_in_source(self):
        src = RECOVERY.read_text(encoding="utf-8")
        for token in ("open(.*config", "read_text.*state", "getpass", "input("):
            self.assertNotIn(token, src)


def main() -> int:
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    for cls in (DecideTests, StaticFileTests, CLITests, PrivacyTests):
        suite.addTests(loader.loadTestsFromTestCase(cls))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    total = result.testsRun
    failed = len(result.failures) + len(result.errors)
    print("\nP0 local synthetic tests: total=%d passed=%d failed=%d" %
          (total, total - failed, failed))
    print("Scope: Python stdlib only; no Pi access, no QQ, no model, no credentials, no deployment.")
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
