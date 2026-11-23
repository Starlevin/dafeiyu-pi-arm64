"""qq-qzone 状态持久化层（原子写 + 备份轮转 + 跨进程锁 + 先记账后发出）。

职责：
- 把 QQ 空间模块的日限计数 / 快照 / 待确认写入日志落到**单个** ``state.json``，
  采用「临时文件 → fsync → os.replace → 目录 fsync」的原子替换；
- 替换前把旧文件轮转为 ``state.json.bak.1``，依次后移，只保留 ``backup_keep`` 份；
- 解析失败时按 ``state.json`` → ``bak.1`` → 其余 ``bak.*``（按新到旧）恢复，
  全部失败则返回 ``fatal=True`` 的骨架状态，**由上层按「已达上限」处理，绝不超发**；
- 用同目录 ``lock`` 文件（``O_CREAT|O_EXCL`` + mtime 过期）做跨进程互斥；
- 提供 ``pending_write`` 日志：写请求发出**之前**先记账，收到确定结果后清除；
  残留记录在 ``load()`` 时按 ``UNCERTAIN`` 保守计入。

边界（硬约束）：
- 只依赖 ``qzone_core`` 与标准库；不 import napcat / service 层。
- 不打印 stdout，日志走 ``logging``。
- 所有可能失败的路径要么抛 ``QzoneStoreError`` 家族异常，要么返回带 ``fatal`` 的状态，
  不静默吞掉错误。
- 本模块不含时钟决策逻辑：``now`` 可注入（默认 ``time.time``），便于测试。
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping

from qzone_core import (
    USAGE_KINDS,
    QzoneConfig,
    allowance,
    beijing_day,
    bump_usage,
    count_usage,
    day_bucket,
    mask_uin,
    make_empty_state,
    redact,
)

logger = logging.getLogger(__name__)

__all__ = [
    "QzoneStoreError",
    "QzoneBusy",
    "QzoneCorrupt",
    "StatePaths",
    "StateStore",
    "make_state",
    "sanitize_state",
    "record_write_intent",
    "clear_write_intent",
    "settle_write_intent",
    "pending_intents",
    "PENDING_KINDS",
]

#: pending 日志里允许的 kind（发布 / 点赞 / 评论）。
PENDING_KINDS: tuple[str, ...] = ("posts", "likes", "comments")

#: pending 条目「残留」判定宽限期（秒）。登记时间新于 now - 该值 → 视为**在途**，
#: ``load()`` 不结算它；只有超期未清的才按 UNCERTAIN 保守计入（契约 6.1）。
#: 必须大于一次正常写请求的最长耗时（默认上游超时 20s），故取 180s。
ROLLOVER_GRACE_SECONDS: int = 180

STATE_VERSION = 1


# ---------------------------------------------------------------------------
# 异常
# ---------------------------------------------------------------------------


class QzoneStoreError(RuntimeError):
    """状态层所有异常的基类（IO 失败 / 结构非法 / 锁问题）。"""


class QzoneBusy(QzoneStoreError):
    """拿不到跨进程锁（已有其它进程持有且未过期）。"""


class QzoneCorrupt(QzoneStoreError):
    """状态文件内容非法且无法修复（保存前校验或显式加载时报出）。"""


# ---------------------------------------------------------------------------
# 状态结构与清洗
# ---------------------------------------------------------------------------


def make_state() -> dict[str, Any]:
    """返回一份全新的合法 state（与契约第 6 节结构一致）。"""
    return make_empty_state()


def _as_int(value: Any, default: int = 0) -> int:
    """宽松取 int：bool / 非数字 / 负数一律回落 ``default``。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    number = int(value)
    if number < 0:
        return default
    return number


def _as_int_or_none(value: Any) -> int | None:
    """取 int 或 None（用于 last_publish_ts）。"""
    if value is None or isinstance(value, bool):
        return None
    if not isinstance(value, (int, float)):
        return None
    return int(value)


def _sanitize_bucket(raw: Any) -> dict[str, int]:
    """把一天计数桶清洗成四个非负 int。"""
    if not isinstance(raw, Mapping):
        return {kind: 0 for kind in USAGE_KINDS}
    return {kind: _as_int(raw.get(kind), 0) for kind in USAGE_KINDS}


def _sanitize_snapshot(raw: Any, keep: int) -> list[dict[str, Any]]:
    """清洗快照列表：丢弃非 dict / ts 缺失的条目，按 ts 降序截断到 ``keep``。"""
    if not isinstance(raw, list):
        return []
    rows: list[tuple[int, dict[str, Any]]] = []
    for item in raw:
        if not isinstance(item, Mapping):
            continue
        if not isinstance(item.get("ts"), (int, float)) or isinstance(item.get("ts"), bool):
            continue
        entry = {
            "ts": int(item["ts"]),
            "tid": str(item.get("tid", "")),
            "day": str(item.get("day", "")),
            "outcome": str(item.get("outcome", "")),
            "chars": _as_int(item.get("chars"), 0),
            "sig": str(item.get("sig", "")),
            "has_images": bool(item.get("has_images", False)),
            "ugc_right": _as_int(item.get("ugc_right"), 0),
        }
        rows.append((entry["ts"], entry))
    rows.sort(key=lambda pair: pair[0], reverse=True)
    return [row for _, row in rows[:keep]]


def _sanitize_pending(raw: Any) -> dict[str, dict[str, Any]]:
    """清洗 pending 日志：值必须是 dict，键统一成 str。"""
    if not isinstance(raw, Mapping):
        return {}
    clean: dict[str, dict[str, Any]] = {}
    for key, value in raw.items():
        if not isinstance(value, Mapping):
            logger.warning("pending 日志条目非法，已丢弃：%r", redact(str(key)))
            continue
        entry = dict(value)
        entry["kind"] = str(entry.get("kind", ""))
        entry["day"] = str(entry.get("day", ""))
        entry["intent"] = {str(k): v for k, v in dict(entry.get("intent") or {}).items()}
        entry["ts"] = _as_int(entry.get("ts"), 0)
        clean[str(key)] = entry
    return clean


def sanitize_state(raw: Any, *, snapshot_keep: int = 200) -> dict[str, Any]:
    """把任意来源的 state 清洗成契约结构（缺键补默认、脏值归零）。

    边界：``raw`` 不是 dict 时抛 ``QzoneCorrupt``（调用方应走备份恢复）。
    额外键原样保留（前向兼容），但契约要求的键一定存在且类型正确。
    """
    if not isinstance(raw, Mapping):
        raise QzoneCorrupt(f"state 必须是 dict，实际为 {type(raw).__name__}")
    clean: dict[str, Any] = dict(raw)
    clean["version"] = _as_int(raw.get("version"), STATE_VERSION) or STATE_VERSION

    days_raw = raw.get("days")
    days: dict[str, dict[str, int]] = {}
    if isinstance(days_raw, Mapping):
        for key, value in days_raw.items():
            if not isinstance(key, str) or not key:
                logger.warning("days 里的非法日期键已忽略：%r", redact(str(key)))
                continue
            days[key] = _sanitize_bucket(value)
    elif days_raw is not None:
        logger.warning("days 字段类型非法（%s），已重置为空", type(days_raw).__name__)
    clean["days"] = days

    clean["snapshot"] = _sanitize_snapshot(raw.get("snapshot"), max(1, snapshot_keep))
    clean["last_publish_ts"] = _as_int_or_none(raw.get("last_publish_ts"))
    clean["pending"] = _sanitize_pending(raw.get("pending"))
    clean["recovered"] = bool(raw.get("recovered", False))
    clean["fatal"] = bool(raw.get("fatal", False))
    return clean


def _roll_pending_into_uncertain(
    state: dict[str, Any], now: float | int | None = None
) -> dict[str, Any]:
    """把 pending 里**残留**的写意图按 UNCERTAIN 保守计入当日桶，并移除该条目。

    「残留」意味着：记账完成但进程在收到确定结果前后崩溃 → 上游可能已经发出。
    契约要求一律按 ``UNCERTAIN`` 计入日限（宁可少发，不可超发）。

    残留判定（契约 6.1）：条目 ``ts`` 的真实年龄（墙钟 now - ts）超过
    ``ROLLOVER_GRACE_SECONDS`` 才算。刚登记的条目是**本轮正在处理**的那条，必须留在
    pending 里 —— 否则「发前记账 → 发完结算再 load」会让一次成功发布也白吃一份额度。

    ``now`` 参数仅为兼容显式传入而保留；**新鲜度一律按墙钟**判定，不受注入的业务时钟影响
    （调用方 qzone_service 正是用注入时钟写业务时刻的，混用会把宽限期判成时钟差）。

    缺 ``ts`` 的条目无法判断新鲜度，按残留处理（宁保守）。

    **幂等**：结算过的条目会被移除，不会重复累加。
    计数口径（契约 6.0）：posts/likes/comments 已在发出前记过，这里只补 uncertain。
    """
    pending = state.get("pending")
    if not isinstance(pending, dict) or not pending:
        return state
    now = int(time.time()) if now is None else int(now)
    # 条目 ts 一律由 service 用**墙钟**写入（int(time.time())）——它是「这条写下多久了」的
    # 唯一依据。不能用注入的业务时钟，因为测试会把业务时刻设到任意位置（过去或未来），
    # 那样宽限期判定的就是「业务时间和墙钟差多少」，毫无意义。
    # 于是这里刻意不受 self.now 影响：残留 = 真实年龄 > 宽限期。
    grace = ROLLOVER_GRACE_SECONDS
    wall_now = int(time.time())
    kept: dict[str, Any] = {}
    for key, entry in pending.items():
        kind = entry.get("kind") if isinstance(entry, Mapping) else ""
        day = entry.get("day") if isinstance(entry, Mapping) else ""
        ts_raw = entry.get("ts") if isinstance(entry, Mapping) else None
        try:
            ts = int(ts_raw)
        except (TypeError, ValueError):
            ts = 0  # 没有时间戳 → 无法证明新鲜，按残留处理
        age = wall_now - ts
        if age < grace:
            # 在途：本轮还在处理，保留，绝不结算。
            # 注意 age 可能为负（条目 ts 略快于本机墙钟/时钟漂移）——那更是「刚写下」，照样保留。
            kept[str(key)] = entry
            continue
        if not isinstance(kind, str) or not kind:
            logger.warning("pending 条目缺少 kind，按 posts 保守计入：%s", redact(key))
            kind = "posts"
        if not isinstance(day, str) or not day:
            logger.warning("pending 条目缺少 day，跳过：%s", redact(key))
            continue
        bucket = day_bucket(state, day)
        # 计数口径（契约 6.0）：posts/likes/comments 已在「请求发出前」加过，
        # 这里只补 uncertain，否则一次 UNCERTAIN 会被双扣额度。
        bucket["uncertain"] = bucket["uncertain"] + 1
        logger.warning(
            "发现残留写入意图（%s / day=%s / kind=%s），已按 UNCERTAIN 保守计入日限",
            redact(str(key)),
            day,
            kind,
        )
    state["pending"] = kept
    return state


# ---------------------------------------------------------------------------
# pending 日志（纯 state 变换，便于单测）
# ---------------------------------------------------------------------------


def record_write_intent(
    state: Mapping[str, Any], day: str, kind: str, intent: Mapping[str, Any]
) -> dict[str, Any]:
    """返回「登记一条写意图」后的**新** state（不改入参）。

    - ``kind`` ∈ {"posts","likes","comments"}，非法抛 ``ValueError``。
    - ``intent`` 须是 JSON 可序列化的 dict（调用方应自带 ``key``/``nonce``）；
      本函数会补上 ``day`` / ``kind`` / ``ts``。
    - 登记本身**不增加**日限计数；计数由调用方在发出请求前用 ``bump_usage`` 完成。
    - ``state`` 里的 ``pending`` 若结构损坏会先规范化，不抛异常。
    """
    if kind not in PENDING_KINDS:
        raise ValueError(f"kind 必须是 {PENDING_KINDS} 之一，实际为 {kind!r}")
    if not isinstance(day, str) or not day:
        raise ValueError("day 必须是非空字符串")
    if not isinstance(intent, Mapping):
        raise ValueError("intent 必须是 dict")
    if not isinstance(state, Mapping):
        raise TypeError("state 必须是 dict")

    key = str(intent.get("key") or "").strip()
    if not key:
        raise ValueError("intent 必须带非空字符串 key")
    payload = json.loads(json.dumps(dict(intent), default=str))
    payload["key"] = key
    payload["day"] = day
    payload["kind"] = kind
    payload.setdefault("ts", int(time.time()))

    new_state = json.loads(json.dumps(dict(state), default=str))
    pending = new_state.get("pending")
    if not isinstance(pending, dict):
        pending = {}
    pending[key] = payload
    new_state["pending"] = pending
    return new_state


def clear_write_intent(state: Mapping[str, Any], key: str) -> dict[str, Any]:
    """返回「清除一条写意图」后的**新** state（不存在则原样返回副本）。

    ``key`` 不是非空 str 抛 ``ValueError``。
    """
    if not isinstance(key, str) or not key.strip():
        raise ValueError("key 必须是非空字符串")
    if not isinstance(state, Mapping):
        raise TypeError("state 必须是 dict")
    new_state = json.loads(json.dumps(dict(state), default=str))
    pending = new_state.get("pending")
    if not isinstance(pending, dict):
        new_state["pending"] = {}
        return new_state
    pending.pop(key.strip(), None)
    new_state["pending"] = pending
    return new_state


def settle_write_intent(
    state: Mapping[str, Any], key: str, outcome: str, uncertain: bool = False
) -> dict[str, Any]:
    """收到**确定结果**后结算一条写意图：清除 pending，必要时补记 ``uncertain``。

    ``uncertain=True`` 表示上游结论不明（超时 / 5xx / tid 不合法）：
    pending 清除的同时给该条目所在日的 ``uncertain`` 桶 +1，避免后续误判额度。
    其它情况（PUBLISHED / REJECTED）只需清 pending —— 正常计数已在前置阶段落账。
    """
    if not isinstance(outcome, str) or not outcome:
        raise ValueError("outcome 必须是非空字符串")
    if not isinstance(state, Mapping):
        raise TypeError("state 必须是 dict")
    new_state = json.loads(json.dumps(dict(state), default=str))
    pending = new_state.get("pending")
    entry = pending.get(key) if isinstance(pending, dict) else None
    result = clear_write_intent(new_state, key)
    if uncertain:
        day = entry.get("day") if isinstance(entry, Mapping) else None
        if isinstance(day, str) and day:
            result = bump_usage(result, day, "uncertain", 1)
        else:
            logger.warning("结算 UNCERTAIN 时找不到所属日期（key=%s），已跳过补记", redact(key))
    return result


def pending_intents(state: Mapping[str, Any]) -> list[dict[str, Any]]:
    """列出当前残留的写意图（按 ts 升序），供控制台展示与排查。"""
    if not isinstance(state, Mapping):
        return []
    pending = state.get("pending")
    if not isinstance(pending, dict):
        return []
    rows = [dict(value) for value in pending.values() if isinstance(value, Mapping)]
    rows.sort(key=lambda item: _as_int(item.get("ts"), 0))
    return rows


# ---------------------------------------------------------------------------
# 路径
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StatePaths:
    """状态目录内的固定文件布局。"""

    state_dir: Path
    state: Path
    lock: Path
    backups: tuple[Path, ...] = field(default=())

    @classmethod
    def build(cls, state_dir: Path | str, backup_keep: int) -> "StatePaths":
        """按 ``backup_keep`` 生成 ``state.json`` / ``state.json.bak.1..N`` / ``lock``。"""
        base = Path(state_dir)
        state = base / "state.json"
        backups = tuple(
            base / f"state.json.bak.{index}" for index in range(1, max(0, backup_keep) + 1)
        )
        return cls(state_dir=base, state=state, lock=base / "lock", backups=backups)


# ---------------------------------------------------------------------------
# StateStore
# ---------------------------------------------------------------------------


class StateStore:
    """``state.json`` 的原子读写 + 备份轮转 + 跨进程锁。

    使用方式::

        store = StateStore(state_dir, cfg)
        state = store.load()
        if state["fatal"]:
            ...  # 上层按已达上限处理
        with store.lock("publish"):
            store.update(lambda s: bump_usage(s, day, "posts"))

    线程/进程安全：``os.replace`` 保证读者永远看到完整文件；跨进程互斥靠 ``lock``。
    本类自身不加线程锁，同一进程内需要串行时应复用同一个 ``lock()``。
    """

    def __init__(
        self,
        state_dir: Path | str,
        cfg: QzoneConfig,
        *,
        now: Callable[[], float] | None = None,
        snapshot_keep: int = 200,
    ) -> None:
        if not isinstance(cfg, QzoneConfig):
            raise TypeError("cfg 必须是 QzoneConfig")
        if snapshot_keep < 1:
            raise ValueError("snapshot_keep 必须是 >= 1 的 int")
        self.cfg = cfg
        self.state_dir = Path(state_dir)
        self.snapshot_keep = int(snapshot_keep)
        self._now: Callable[[], float] = now or time.time
        self.paths = StatePaths.build(self.state_dir, cfg.backup_keep)

    # -- 基本信息 ---------------------------------------------------------

    @property
    def now(self) -> float:
        """注入时钟的当前 epoch 秒（默认 ``time.time``）。"""
        return float(self._now())

    def ensure_dir(self) -> None:
        """确保状态目录存在（幂等）。失败抛 ``QzoneStoreError``。"""
        try:
            self.state_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise QzoneStoreError(f"无法创建状态目录：{exc}") from exc

    # -- 读 --------------------------------------------------------------

    def load(self) -> dict[str, Any]:
        """原子读取 state，必要时按备份恢复。

        顺序：``state.json`` → ``state.json.bak.1`` → ``bak.2..N``（新到旧）。
        恢复成功时返回的 state 带 ``recovered=True``。

        全部失败 → 返回 ``fatal=True`` 的骨架状态（``days`` 为空、``snapshot`` 为空），
        **必须由上层按「已达上限」处理**，本层绝不擅自初始化成「今日零次」。

        任何成功读到的状态都会：清洗结构 → 把**残留** pending 按 UNCERTAIN 计入日限。

        「残留」的判定（契约 6.1，`ROLLOVER_GRACE_SECONDS`）：只有登记时间早于
        ``now - ROLLOVER_GRACE_SECONDS`` 的 pending 才算残留。刚登记、进程正活着
        处理的那条不能被 load 顺手结算，否则「发前记账 → 结算再 load」会平白多扣一份额度。
        """
        candidates: list[Path] = [self.paths.state, *self.paths.backups]
        errors: list[str] = []
        for index, path in enumerate(candidates):
            if not path.exists():
                continue
            try:
                state = self._read_one(path)
            except QzoneCorrupt as exc:
                errors.append(f"{path.name}: {exc}")
                continue
            except OSError as exc:
                errors.append(f"{path.name}: {exc}")
                continue
            if index > 0:
                logger.error(
                    "状态文件已从备份恢复（%s），原始文件解析失败：%s",
                    path.name,
                    "; ".join(errors) or "(无)",
                )
                state["recovered"] = True
            elif state.get("recovered"):
                logger.warning("状态文件本身标记了 recovered=True")
            _roll_pending_into_uncertain(state, self.now)
            return state

        if not errors:
            # 目录里干净得一个状态文件都没有（也备份不了）= 全新安装，不是损坏。
            # 这种情况返回干净的初始 state，让上层按「今日零次、满额度」启动；
            # 若在这里报 CRITICAL，首跑日志会吓人，也会诱导上层误判成「额度耗尽」。
            logger.info("状态目录内没有任何 state.json / 备份：按首次运行初始化")
            fresh = make_state()
            fresh["fatal"] = False
            fresh["recovered"] = False
            fresh["first_run"] = True
            return fresh

        reason = "; ".join(errors)
        logger.critical("状态不可恢复，按已达上限处理：%s", redact(reason))
        fatal_state = make_state()
        fatal_state["fatal"] = True
        fatal_state["recovered"] = False
        # 保守口径：让所有额度看起来已经用尽，避免超发。
        fatal_state["days"] = {}
        return fatal_state

    def _read_one(self, path: Path) -> dict[str, Any]:
        """读取并校验单个文件；结构非法抛 ``QzoneCorrupt``。"""
        try:
            raw_text = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            raise QzoneCorrupt("文件不存在") from None
        except UnicodeDecodeError as exc:
            raise QzoneCorrupt(f"不是合法 UTF-8：{exc}") from exc
        if not raw_text.strip():
            raise QzoneCorrupt("文件为空")
        try:
            raw = json.loads(raw_text)
        except json.JSONDecodeError as exc:
            raise QzoneCorrupt(f"JSON 解析失败：{exc}") from exc
        state = sanitize_state(raw, snapshot_keep=self.snapshot_keep)
        return state

    # -- 写 --------------------------------------------------------------

    def save(self, state: Mapping[str, Any]) -> None:
        """原子写入 state。

        步骤：清洗 → 同目录临时文件写入 → ``flush`` → ``os.fsync`` →
        轮转备份（旧 state.json → bak.1，bak.i → bak.i+1，删除超出 keep 的）→
        ``os.replace`` → 目录 ``fsync``。

        失败抛 ``QzoneStoreError``；``keep=0`` 表示不保留备份（无 bak 文件）。
        写入前会校验结构（``sanitize_state`` 抛 ``QzoneCorrupt``）。
        """
        if not isinstance(state, Mapping):
            raise TypeError("state 必须是 dict")
        try:
            clean = sanitize_state(state, snapshot_keep=self.snapshot_keep)
        except QzoneCorrupt:
            raise
        self.ensure_dir()

        payload = json.dumps(clean, ensure_ascii=False, indent=2, sort_keys=False)
        tmp_fd = -1
        tmp_path: str | None = None
        try:
            tmp_fd, tmp_path = tempfile.mkstemp(
                prefix=".state.", suffix=".tmp", dir=str(self.state_dir)
            )
            with os.fdopen(tmp_fd, "w", encoding="utf-8", newline="\n") as handle:
                tmp_fd = -1  # 已交给 handle 管理
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            self._rotate_backups()
            os.replace(tmp_path, self.paths.state)
            tmp_path = None
            self._fsync_dir()
        except OSError as exc:
            raise QzoneStoreError(f"写入状态失败：{exc}") from exc
        finally:
            if tmp_fd >= 0:
                try:
                    os.close(tmp_fd)
                except OSError:  # pragma: no cover - 关闭失败不影响主流程
                    pass
            if tmp_path is not None:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    logger.warning("临时文件清理失败：%s", Path(tmp_path).name)

    def _rotate_backups(self) -> None:
        """轮转备份：删除最旧，依次后移，最后把当前 state.json 变成 bak.1。"""
        keep = self.cfg.backup_keep
        if keep <= 0:
            return
        backups = list(self.paths.backups)
        oldest = backups[-1]
        try:
            oldest.unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:
            logger.warning("删除最旧备份失败（%s）：%s", oldest.name, exc)
        for index in range(len(backups) - 1, 0, -1):
            src = backups[index - 1]
            dst = backups[index]
            if not src.exists():
                continue
            try:
                os.replace(src, dst)
            except OSError as exc:
                logger.warning("备份轮转失败（%s → %s）：%s", src.name, dst.name, exc)
        if self.paths.state.exists() and len(backups) >= 1:
            try:
                os.replace(self.paths.state, backups[0])
            except OSError as exc:
                logger.warning("备份当前状态失败：%s", exc)

    def _fsync_dir(self) -> None:
        """对状态目录 fsync（Windows 不支持目录句柄，失败只记 debug）。"""
        try:
            dir_fd = os.open(str(self.state_dir), os.O_RDONLY)
        except OSError as exc:
            logger.debug("目录 fsync 跳过（打开失败）：%s", exc)
            return
        try:
            os.fsync(dir_fd)
        except OSError as exc:
            logger.debug("目录 fsync 跳过（不支持）：%s", exc)
        finally:
            try:
                os.close(dir_fd)
            except OSError:  # pragma: no cover
                pass

    def update(self, mutator: Callable[[dict[str, Any]], dict[str, Any]]) -> dict[str, Any]:
        """在锁内执行 ``load → mutator → save``，返回新 state。

        - ``mutator`` 必须返回 dict；返回非 dict 抛 ``QzoneStoreError``（且不落盘）。
        - 拿不到锁 → ``QzoneBusy``；状态 fatal 时**仍然允许**读改写（上层自己决定
          是否拒绝业务动作），但日志会告警。
        - ``mutator`` 抛出的异常原样向上抛，且不会写入任何东西（不会污染 state）。
        """
        if not callable(mutator):
            raise TypeError("mutator 必须可调用")
        with self.lock("update"):
            state = self.load()
            if state.get("fatal"):
                logger.warning("状态处于 fatal，仍继续执行 update（请上层确认额度策略）")
            new_state = mutator(dict(state))
            if not isinstance(new_state, dict):
                raise QzoneStoreError(
                    f"mutator 必须返回 dict，实际返回 {type(new_state).__name__}"
                )
            self.save(new_state)
            return sanitize_state(new_state, snapshot_keep=self.snapshot_keep)

    # -- 锁 --------------------------------------------------------------

    @contextmanager
    def lock(self, owner: str = "qzone", timeout: float = 5.0) -> Iterator[Path]:
        """跨进程互斥上下文管理器（``O_CREAT|O_EXCL`` + mtime 过期抢占）。

        - 锁文件内写 ``pid`` 与时间戳（便于排查），内容不是权威依据，mtime 才是。
        - 已存在且 mtime 超过 ``cfg.lock_stale_seconds`` → 视为 stale，删除并抢占，
          同时记 warning。
        - ``timeout`` 秒内拿不到 → ``QzoneBusy``。``timeout <= 0`` 表示只试一次。
        - 退出时尽力删除锁（只删自己创建的）；删除失败只记 debug。
        """
        self.ensure_dir()
        lock_path = self.paths.lock
        deadline = self.now + max(0.0, float(timeout))
        sleep_step = 0.02
        while True:
            if self._try_acquire(locked_owner=owner, lock_path=lock_path):
                break
            if self.now >= deadline:
                raise QzoneBusy(
                    f"状态被其它进程锁定（{lock_path.name}），{timeout}s 内未取得：owner={owner}"
                )
            time.sleep(sleep_step)
            sleep_step = min(0.2, sleep_step * 2)
        try:
            yield lock_path
        finally:
            try:
                lock_path.unlink()
            except FileNotFoundError:
                pass
            except OSError as exc:
                logger.debug("锁文件删除失败（%s）：%s", lock_path.name, exc)

    def _try_acquire(self, *, locked_owner: str, lock_path: Path) -> bool:
        """尝试创建锁文件；返回是否成功。"""
        payload = json.dumps(
            {"owner": locked_owner, "pid": os.getpid(), "ts": int(self.now)},
            ensure_ascii=False,
        ).encode("utf-8")
        try:
            fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            self._break_if_stale(lock_path)
            return False
        except OSError as exc:
            raise QzoneStoreError(f"无法创建锁文件：{exc}") from exc
        try:
            os.write(fd, payload)
            os.fsync(fd)
        except OSError as exc:
            logger.warning("锁内容写入失败（不影响互斥）：%s", exc)
        finally:
            try:
                os.close(fd)
            except OSError:  # pragma: no cover
                pass
        return True

    def _break_if_stale(self, lock_path: Path) -> None:
        """锁文件 mtime 过期则删除抢占（记 warning）。"""
        try:
            stat = lock_path.stat()
        except FileNotFoundError:
            return
        except OSError as exc:
            logger.warning("读取锁 mtime 失败：%s", exc)
            return
        age = self.now - stat.st_mtime
        if age <= self.cfg.lock_stale_seconds:
            return
        try:
            lock_path.unlink()
        except FileNotFoundError:
            return
        except OSError as exc:
            logger.warning("过期锁删除失败：%s", exc)
            return
        logger.warning(
            "抢占过期锁（mtime 已 %d 秒 > %d 秒，owner=%s）",
            int(age),
            self.cfg.lock_stale_seconds,
            lock_path.name,
        )

    # -- 便捷查询（只读，不含写） -----------------------------------------

    def usage(self, state: Mapping[str, Any], day: str) -> dict[str, int]:
        """读取某日四类计数（不改 state）。"""
        bucket = state.get("days") if isinstance(state, Mapping) else None
        raw = bucket.get(day) if isinstance(bucket, Mapping) else None
        return _sanitize_bucket(raw)

    def quota(self, state: Mapping[str, Any], day: str) -> dict[str, Any]:
        """委托 core 计算剩余额度（保持单一事实来源）。"""
        return allowance(state, day, self.cfg)

    def today(self) -> str:
        """当前（注入时钟）北京日期键。"""
        return beijing_day(int(self.now))

    def masked_owner(self) -> str:
        """打码后的主人 QQ 号（用于日志/状态展示）。"""
        return mask_uin(self.cfg.owner_uin)

    def count(self, state: Mapping[str, Any], day: str, kind: str) -> int:
        """读取某日某类计数（委托 core）。"""
        return count_usage(state, day, kind)
