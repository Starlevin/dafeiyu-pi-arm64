"""qq-qzone 纯函数核心层。

职责：承载 QQ 空间模块的全部**无副作用**判定逻辑 —— 配置校验、北京时间换算、
文本归一与长度校验、选题签名判重、敏感信息打码/抹除、主人三层认证、
发布与联动（点赞/评论）的放行判定、上游结果分类、日限计数与快照摘要。

边界（硬约束，见 docs/MODULE_CONTRACT.md 第 0 节）：
- 本模块**不做任何 IO**：不读文件、不发网络、不读时钟、不打印 stdout。
  所有时间一律由调用方以 epoch 秒（int/float）传入。
- 本模块**不得** import qzone_store / qzone_napcat / qzone_service。
- 只使用 Python 3.11 标准库。
- 所有公开函数对非法输入要么返回结构化结果（含 reason），要么显式抛 ValueError /
  TypeError；不静默吞掉异常。
- 北京时间固定为 UTC+8（不读系统时区、不用夏令时）。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import re
from dataclasses import MISSING, dataclass, field, fields
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Iterable, Mapping, Sequence

__all__ = [
    # 常量
    "BEIJING_TZ",
    "DAY_KEY_FMT",
    "USAGE_KINDS",
    "FOLLOWUP_KINDS",
    "TOPIC_SIG_LEN",
    # 配置
    "QzoneConfig",
    # 时间
    "beijing_now",
    "beijing_day",
    "parse_hhmm",
    "in_quiet_hours",
    # 文本
    "normalize_text",
    "text_length",
    "validate_text",
    "topic_signature",
    "is_repeat_topic",
    # 敏感性
    "mask_uin",
    "redact",
    "assert_no_secret",
    # 主人认证
    "owner_confirm_token",
    "check_owner_auth",
    # 发布
    "PublishOutcome",
    "PublishDecision",
    "evaluate_publish",
    "classify_publish_result",
    "evaluate_followup",
    # 计数
    "make_empty_state",
    "day_bucket",
    "count_usage",
    "bump_usage",
    "allowance",
    # 快照
    "SnapshotEntry",
    "SNAPSHOT_KEY_ORDER",
    "build_snapshot",
    "snapshot_digest",
    "append_snapshot",
    # 枚举转换
    "outcome_str",
    "parse_outcome",
    # 一致性校验
    "check_publish_consistency",
    "effective_ugc_right",
    # 好友动态只读刷新（契约 §10）
    "READ_REFRESH_MIN_INTERVAL_SECONDS",
    "READ_REFRESH_MAX_PER_DAY",
    "READ_REFRESH_QUIET_START",
    "READ_REFRESH_QUIET_END",
    "READ_LATEST_LIMIT",
    "READ_FEED_TEXT_MAX_CHARS",
    "READ_ERRORS",
    "in_read_quiet_hours",
    "read_allowance",
    "evaluate_read_refresh",
    "bump_read_refresh",
    "normalize_latest_feeds",
    "read_error_from_kind",
]

logger = logging.getLogger(__name__)

#: 北京时间，固定 UTC+8，不读系统时区、不含夏令时。
BEIJING_TZ: timezone = timezone(timedelta(hours=8))
#: 日期键格式（北京时区）。
DAY_KEY_FMT: str = "%Y-%m-%d"

#: 计数桶里允许的种类（顺序即规范化输出顺序）。
USAGE_KINDS: tuple[str, ...] = ("posts", "likes", "comments", "uncertain")
#: 联动（followup）支持的 kind。
FOLLOWUP_KINDS: tuple[str, ...] = ("like", "comment")

#: 选题签名截取的字符数。
TOPIC_SIG_LEN: int = 12
#: tid 合法性上限（契约 3：tid 必须合法非空字符串）。
TID_MAX_LEN: int = 8192

#: 可见性名称 → 上游 ugc_right 权限码（契约第 1 节）。
#: 16 / 128 需要非空 target_uins，本模块不负责；无法确定目标好友时按拒发处理。
VISIBILITY_UGC_RIGHT: Mapping[str, int] = {
    "public": 1,
    "friend": 4,
    "partial": 16,
    "private": 64,
    "exclude": 128,
}

# ---------------------------------------------------------------------------
# 内部工具
# ---------------------------------------------------------------------------


def _is_int(value: Any) -> bool:
    """严格 int 判定：bool 虽然是 int 的子类，但配置里不接受 True/False 当数字。"""
    return isinstance(value, int) and not isinstance(value, bool)


def _is_number(value: Any) -> bool:
    """int/float 判定（同样排除 bool）。"""
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _coerce_ts(ts: Any) -> int:
    """把 epoch 秒规整成 int；非法输入抛 ValueError。"""
    if not _is_number(ts):
        raise ValueError(f"时间戳必须是数字，实际为 {type(ts).__name__}")
    if ts != ts:  # NaN
        raise ValueError("时间戳不能是 NaN")
    if ts in (float("inf"), float("-inf")):
        raise ValueError("时间戳不能是无穷大")
    return int(ts)


def _coerce_tup(value: Any) -> tuple[int, int]:
    """接受 (int,int) 元组或 int，统一成 (int,int)。"""
    if _is_number(value):
        return (_coerce_ts(value), 0)
    if isinstance(value, (tuple, list)) and len(value) == 2:
        return (_coerce_ts(value[0]), _coerce_ts(value[1]))
    raise ValueError("时间戳必须是数字或 (秒, 纳秒) 二元组")


# ---------------------------------------------------------------------------
# 1) 配置
# ---------------------------------------------------------------------------


def _check_int_range(name: str, value: Any, low: int, high: int) -> int:
    """整数 + 闭区间校验；类型错或越界一律 ValueError。"""
    if not _is_int(value):
        raise ValueError(f"配置项 {name} 必须是 int，实际为 {type(value).__name__}")
    if value < low or value > high:
        raise ValueError(f"配置项 {name} 必须在 [{low}, {high}] 内，实际为 {value}")
    return int(value)


def _check_hhmm(name: str, value: Any) -> str:
    """校验 "HH:MM"：严格两位小时/两位分钟，且 00:00 <= t < 24:00。"""
    if not isinstance(value, str):
        raise ValueError(f"配置项 {name} 必须是 'HH:MM' 字符串")
    parse_hhmm(value)  # 内部已做严格格式与范围校验
    return value


def _check_bool(name: str, value: Any) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"配置项 {name} 必须是 bool，实际为 {type(value).__name__}")
    return value


def _check_float_range(name: str, value: Any, low: float, high: float) -> float:
    if not _is_number(value):
        raise ValueError(f"配置项 {name} 必须是数字，实际为 {type(value).__name__}")
    number = float(value)
    if number != number:
        raise ValueError(f"配置项 {name} 不能是 NaN")
    if number < low or number > high:
        raise ValueError(f"配置项 {name} 必须在 [{low}, {high}] 内，实际为 {number}")
    return number


@dataclass
class QzoneConfig:
    """QQ 空间模块配置。

    所有阈值的合法范围在此集中定义；``QzoneConfig.from_dict`` 是唯一的外部入口，
    遇到未知键直接忽略（前向兼容），遇到类型错误 / 越界一律抛 ``ValueError``。

    边界说明：
    - ``min_interval_minutes`` 取 0 表示不限间隔，1440 表示至多一天一条。
    - ``max_posts_per_day`` 取 0 表示禁止发布（此时任何发布都判 ``daily_limit``）。
    - ``quiet_start == quiet_end`` 视为不启用夜间静默。
    - ``visibility`` 仅接受 "private"/"friend"/"public"/"partial"/"exclude"；
      默认 "private"（ugc_right=64，契约唯一允许当默认的可见性）。
    """

    owner_uin: str
    min_interval_minutes: int = 180
    max_posts_per_day: int = 2
    max_likes_per_day: int = 20
    max_comments_per_day: int = 5
    quiet_start: str = "23:30"
    quiet_end: str = "07:00"
    min_chars: int = 4
    max_chars: int = 280
    enable_like: bool = False
    enable_comment: bool = False
    visibility: str = "private"
    upstream_timeout_seconds: float = 20.0
    read_retry: int = 2
    backup_keep: int = 3
    lock_stale_seconds: int = 30

    def __post_init__(self) -> None:
        """构造后统一校验一次，保证无论走 dataclass 构造还是 from_dict 都合法。"""
        self.owner_uin = _check_owner_uin(self.owner_uin)
        self.min_interval_minutes = _check_int_range(
            "min_interval_minutes", self.min_interval_minutes, 0, 1440
        )
        self.max_posts_per_day = _check_int_range(
            "max_posts_per_day", self.max_posts_per_day, 0, 10
        )
        self.max_likes_per_day = _check_int_range(
            "max_likes_per_day", self.max_likes_per_day, 0, 200
        )
        self.max_comments_per_day = _check_int_range(
            "max_comments_per_day", self.max_comments_per_day, 0, 50
        )
        self.quiet_start = _check_hhmm("quiet_start", self.quiet_start)
        self.quiet_end = _check_hhmm("quiet_end", self.quiet_end)
        self.min_chars = _check_int_range("min_chars", self.min_chars, 0, 5000)
        self.max_chars = _check_int_range("max_chars", self.max_chars, 1, 5000)
        if self.min_chars > self.max_chars:
            raise ValueError(
                f"min_chars({self.min_chars}) 不能大于 max_chars({self.max_chars})"
            )
        self.enable_like = _check_bool("enable_like", self.enable_like)
        self.enable_comment = _check_bool("enable_comment", self.enable_comment)
        if not isinstance(self.visibility, str) or self.visibility not in VISIBILITY_UGC_RIGHT:
            raise ValueError(
                "配置项 visibility 必须是 "
                + "/".join(sorted(VISIBILITY_UGC_RIGHT))
                + f" 之一，实际为 {self.visibility!r}"
            )
        self.upstream_timeout_seconds = _check_float_range(
            "upstream_timeout_seconds", self.upstream_timeout_seconds, 1.0, 300.0
        )
        self.read_retry = _check_int_range("read_retry", self.read_retry, 0, 10)
        self.backup_keep = _check_int_range("backup_keep", self.backup_keep, 0, 50)
        self.lock_stale_seconds = _check_int_range(
            "lock_stale_seconds", self.lock_stale_seconds, 1, 3600
        )

    @classmethod
    def from_dict(
        cls, raw: Mapping[str, Any] | None, base: "QzoneConfig | None" = None
    ) -> "QzoneConfig":
        """从 dict 构造配置。

        - ``base`` 为 None 时从默认值出发（此时 ``owner_uin`` 必须给出）；
          否则以 ``base`` 的字段值为底，用 ``raw`` 覆盖。
        - 未知键忽略；``raw`` 不是 dict 时抛 ``ValueError``。
        - 类型错误 / 越界抛 ``ValueError``（消息里带字段名）。
        """
        if raw is None:
            raw = {}
        if not isinstance(raw, Mapping):
            raise ValueError(f"配置必须是 dict，实际为 {type(raw).__name__}")

        if base is None:
            defaults: dict[str, Any] = {"owner_uin": ""}
            for item in fields(cls):
                if item.default is not MISSING:
                    defaults[item.name] = item.default
            merged: dict[str, Any] = dict(defaults)
        else:
            if not isinstance(base, QzoneConfig):
                raise ValueError("base 必须是 QzoneConfig 或其子类实例")
            merged = base.to_dict()

        for key, value in raw.items():
            if not isinstance(key, str):
                raise ValueError("配置键必须是字符串")
            if key in merged:
                merged[key] = value
            # 未知键：静默忽略（前向兼容）

        return cls(**merged)

    def to_dict(self) -> dict[str, Any]:
        """输出纯 JSON 可序列化的 dict（键序固定为 dataclass 字段序）。"""
        return {
            "owner_uin": self.owner_uin,
            "min_interval_minutes": self.min_interval_minutes,
            "max_posts_per_day": self.max_posts_per_day,
            "max_likes_per_day": self.max_likes_per_day,
            "max_comments_per_day": self.max_comments_per_day,
            "quiet_start": self.quiet_start,
            "quiet_end": self.quiet_end,
            "min_chars": self.min_chars,
            "max_chars": self.max_chars,
            "enable_like": self.enable_like,
            "enable_comment": self.enable_comment,
            "visibility": self.visibility,
            "upstream_timeout_seconds": self.upstream_timeout_seconds,
            "read_retry": self.read_retry,
            "backup_keep": self.backup_keep,
            "lock_stale_seconds": self.lock_stale_seconds,
        }

    def copy_with(self, **changes: Any) -> "QzoneConfig":
        """返回覆盖若干字段后的新配置（不改自身）；覆盖值同样走校验。"""
        known = {item.name for item in fields(self)}
        merged = self.to_dict()
        for key, value in changes.items():
            if key not in known:
                raise ValueError(f"未知配置项：{key}")
            merged[key] = value
        return QzoneConfig(**merged)

    @property
    def quiet_minutes(self) -> tuple[int, int]:
        """(quiet_start 分钟数, quiet_end 分钟数)。"""
        return (_hhmm_to_minutes(self.quiet_start), _hhmm_to_minutes(self.quiet_end))


def _check_owner_uin(value: Any) -> str:
    """主人 QQ 号必须是纯数字字符串（非空、无符号、长度合理）。"""
    if not isinstance(value, str):
        raise ValueError("配置项 owner_uin 必须是字符串")
    if not value:
        raise ValueError("配置项 owner_uin 不能为空")
    if not value.isdigit() or not value.isascii():
        raise ValueError("配置项 owner_uin 必须是纯数字字符串")
    if len(value) < 5 or len(value) > 12:
        raise ValueError("配置项 owner_uin 长度必须在 [5, 12] 之间")
    return value


# ---------------------------------------------------------------------------
# 2) 时间
# ---------------------------------------------------------------------------


def beijing_now(ts: float | int) -> datetime:
    """把 epoch 秒转成北京时间 datetime（带 BEIJING_TZ）。

    只做换算，不读时钟。``ts`` 非法（非数字 / NaN / inf）抛 ``ValueError``。
    """
    return datetime.fromtimestamp(_coerce_ts(ts), tz=BEIJING_TZ)


def beijing_day(ts: float | int) -> str:
    """返回北京时区的日期键 ``"YYYY-MM-DD"``。非数字抛 ``ValueError``。"""
    return beijing_now(ts).strftime(DAY_KEY_FMT)


def parse_hhmm(s: str) -> tuple[int, int]:
    """解析严格 ``"HH:MM"``，返回 (hour, minute)。

    边界：必须恰好 5 个字符、两位数字 + ':' + 两位数字，且 ``0 <= H <= 23``、
    ``0 <= M <= 59``（即 00:00 <= t < 24:00，不接受 "24:00"、不接受 "+8:00"、
    不接受 "8:05"）。任何不满足抛 ``ValueError``。
    """
    if not isinstance(s, str):
        raise ValueError(f"HH:MM 必须是字符串，实际为 {type(s).__name__}")
    if len(s) != 5 or s[2] != ":":
        raise ValueError(f"HH:MM 格式必须是 'HH:MM'（两位补零），实际为 {s!r}")
    head, tail = s[0:2], s[3:5]
    if not (head.isdigit() and tail.isdigit() and head.isascii() and tail.isascii()):
        raise ValueError(f"HH:MM 必须全部是 ASCII 数字，实际为 {s!r}")
    hour, minute = int(head), int(tail)
    if hour > 23 or minute > 59:
        raise ValueError(f"HH:MM 超出范围（00:00 <= t < 24:00），实际为 {s!r}")
    return hour, minute


def _hhmm_to_minutes(s: str) -> int:
    hour, minute = parse_hhmm(s)
    return hour * 60 + minute


def in_quiet_hours(ts: float | int, cfg: QzoneConfig) -> bool:
    """判断该时刻是否落在夜间静默区间 ``[quiet_start, quiet_end)``（按北京时间）。

    - 支持跨零点：``23:30 -> 07:00`` 时 23:30 起静默、07:00 整恢复（半开区间）。
    - ``quiet_start == quiet_end`` 视为不静默，恒返回 False。
    - ``cfg`` 类型不对抛 ``TypeError``；``ts`` 非法抛 ``ValueError``。
    """
    if not isinstance(cfg, QzoneConfig):
        raise TypeError("cfg 必须是 QzoneConfig")
    start, end = cfg.quiet_minutes
    if start == end:
        return False
    now = beijing_now(ts)
    current = now.hour * 60 + now.minute
    if start < end:
        return start <= current < end
    return current >= start or current < end


# ---------------------------------------------------------------------------
# 3) 文本
# ---------------------------------------------------------------------------

#: 归一化时折叠的空白（含全角空格、制表符等 Unicode 空白），不含换行。
_WS_RUN_RE = re.compile(r"[^\S\n]+")

#: 选题签名要去掉的标点：常见中英文标点 + Unicode 标点类别。
_SIG_PUNCT_RE = re.compile(
    r"[\s"
    r"!-/:-@\[-`{-~"  # ASCII 标点（保留字母数字）
    r"\u3000-\u303f"  # CJK 标点
    r"\uff00-\uffef"  # 全角形式
    r"\u2000-\u206f"  # 常用通用标点
    r"]+"
)


def normalize_text(s: str) -> str:
    """文本轻量归一：去首尾空白、``\\r\\n``/``\\r`` 归一为 ``\\n``、
    每行内连续空白折叠为单空格、多余空行折叠为空行。中文标点保持原样。

    非字符串入参抛 ``TypeError``；空串 / 全空白返回 ``""``。
    """
    if not isinstance(s, str):
        raise TypeError(f"文本必须是 str，实际为 {type(s).__name__}")
    text = s.replace("\r\n", "\n").replace("\r", "\n")
    lines = [_WS_RUN_RE.sub(" ", line).strip() for line in text.split("\n")]
    # 去掉首尾空行
    while lines and not lines[0]:
        lines.pop(0)
    while lines and not lines[-1]:
        lines.pop()
    # 连续空行折成一个空行
    folded: list[str] = []
    for line in lines:
        if not line and folded and not folded[-1]:
            continue
        folded.append(line)
    return "\n".join(folded)


def text_length(s: str) -> int:
    """按 ``normalize_text`` 之后的码点个数计数（``len``），emoji 按码点计。"""
    return len(normalize_text(s))


def _has_control_chars(text: str) -> bool:
    """是否存在除 ``\\n`` 外的控制字符（charCode < 32 或 == 127）。"""
    for ch in text:
        code = ord(ch)
        if ch == "\n":
            continue
        if code < 32 or code == 127:
            return True
    return False


def validate_text(s: Any, cfg: QzoneConfig) -> tuple[bool, str]:
    """校验说说正文。

    返回 ``(ok, reason)``，reason ∈ {"", "empty", "too_short", "too_long", "bad_chars"}，
    ok 为 True 时 reason 为 ``""``。检查顺序：类型 → 控制字符 → 空 → 过短 → 过长。

    边界：
    - 非字符串入参视为 ``bad_chars``（不抛异常，便于路由层统一回 bad_request）。
    - 空白折叠后再判长度；``min_chars=0`` 时仅要求非空。
    - 除 ``\\n`` 外的控制字符（含 ``\\t``、``\\x00``、DEL）一律拒绝。
    """
    if not isinstance(cfg, QzoneConfig):
        raise TypeError("cfg 必须是 QzoneConfig")
    if not isinstance(s, str):
        return (False, "bad_chars")
    if _has_control_chars(s):
        return (False, "bad_chars")
    text = normalize_text(s)
    if not text:
        return (False, "empty")
    length = len(text)
    if length < cfg.min_chars:
        return (False, "too_short")
    if length > cfg.max_chars:
        return (False, "too_long")
    return (True, "")


def topic_signature(s: Any) -> str:
    """选题签名：归一后去掉空白与常见中英文标点，取前 ``TOPIC_SIG_LEN`` 个字符。

    非字符串入参抛 ``TypeError``；空串 / 全标点返回 ``""``（调用方应视为无效选题）。
    """
    if not isinstance(s, str):
        raise TypeError(f"选题必须是 str，实际为 {type(s).__name__}")
    stripped = _SIG_PUNCT_RE.sub("", normalize_text(s))
    return stripped[:TOPIC_SIG_LEN].casefold()


def is_repeat_topic(s: str, recent_signatures: Sequence[str] | None) -> bool:
    """判断选题是否与最近的签名重复（命中任一即视为重复）。

    边界：``s`` 签名为空（空白 / 全标点）→ 返回 False（无从比较，交给文本校验兜底）；
    ``recent_signatures`` 为 None 或空 → False；非序列入参抛 ``TypeError``。
    """
    sig = topic_signature(s)
    if not sig:
        return False
    if recent_signatures is None:
        return False
    if isinstance(recent_signatures, (str, bytes)):
        raise TypeError("recent_signatures 必须是签名序列，不能是单个字符串")
    if not isinstance(recent_signatures, Iterable):
        raise TypeError("recent_signatures 必须是可迭代对象")
    for item in recent_signatures:
        if not isinstance(item, str) or not item:
            continue
        if _SIG_PUNCT_RE.sub("", item.casefold())[:TOPIC_SIG_LEN] == sig:
            return True
    return False


# ---------------------------------------------------------------------------
# 4) 敏感性
# ---------------------------------------------------------------------------


def mask_uin(uin: Any) -> str:
    """打码 QQ 号：保留后 4 位，其余替换为 ``*``。

    边界：长度 < 4 时全打码；非字符串按 ``str()`` 处理；空值返回 ``"****"``。
    """
    raw = "" if uin is None else str(uin)
    if not raw:
        return "****"
    if len(raw) < 4:
        return "*" * len(raw)
    return "*" * (len(raw) - 4) + raw[-4:]


#: 需要抹除的凭据模式（按顺序应用，先长的先匹配）。
_SECRET_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(?i)\bbearer\s+[A-Za-z0-9\-._~+/]{8,}=*"), "[redacted]"),
    (
        re.compile(r"(?i)\baccess[_-]?token\b\s*[:=]\s*[\"']?[^\s\"',;&]+"),
        "[redacted]",
    ),
    (
        re.compile(r"(?i)\b(?:token|secret|password|passwd|pwd|api[_-]?key)\b\s*[:=]\s*[\"']?[^\s\"',;&]+"),
        "[redacted]",
    ),
    # Authorization 头：抹掉整段（含 "Bearer <token>" 两个词），避免残留 scheme 词。
    (
        re.compile(
            r"(?i)\bauthorization\s*[:=]\s*(?:[A-Za-z][A-Za-z0-9+.-]*\s+)?[^\s\"',;&]*"
        ),
        "[redacted]",
    ),
    # Cookie 头：连同其后的 "k=v; k2=v2" 序列一起抹掉（值里常带 `=`，不能只取到 `=`）。
    (
        re.compile(
            r"(?i)\b(?:set-)?cookie\s*[:=]\s*[^\s\"';,&]+(?:\s*;\s*[^\s\"';,]+)*"
        ),
        "[redacted]",
    ),
    (re.compile(r"(?i)\bsk-[A-Za-z0-9\-_]{6,}"), "[redacted]"),
    (re.compile(r"(?<!\d)\d{10,}(?!\d)"), "[redacted]"),
)

#: ``assert_no_secret`` 使用的严格探测模式（同一语义，但保留更明确的凭据关键词判定）。
_SECRET_PROBES: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9\-._~+/]{8,}=*"),
    re.compile(r"(?i)\baccess[_-]?token\b\s*[:=]"),
    re.compile(r"(?i)\b(?:token|secret|password|passwd|pwd|api[_-]?key)\b\s*[:=]\s*\S"),
    re.compile(r"(?i)\bauthorization\s*[:=]"),
    re.compile(r"(?i)\b(?:cookie|set-cookie)\s*[:=]"),
    re.compile(r"(?i)\bsk-[A-Za-z0-9\-_]{6,}"),
    re.compile(r"(?<!\d)\d{10,}(?!\d)"),
)


def redact(text: Any) -> str:
    """把文本里的疑似凭据抹成 ``[redacted]``。

    覆盖：``token=<值>`` / ``access_token`` / ``authorization: <值>`` / ``cookie: <值>`` /
    ``sk-<密钥>`` / ``Bearer <令牌>`` / 连续 >=10 位纯数字（视为 QQ 号）。

    边界：非字符串按 ``str()`` 处理（``None`` → ``""``）；不做反复替换的嵌套清理，
    但模式之间互不前提，单次遍历足以覆盖。
    """
    if text is None:
        return ""
    raw = text if isinstance(text, str) else str(text)
    for pattern, repl in _SECRET_PATTERNS:
        raw = pattern.sub(repl, raw)
    return raw


def assert_no_secret(obj: Any, _path: str = "$") -> None:
    """递归检查 dict / list / tuple / str，发现疑似凭据抛 ``ValueError``。

    用于测试与回执出站前的最后一道闸。数字 / 布尔 / None 不当作凭据
    （``-> [redacted]`` 之外的长整数若是 QQ 号，请先转成字符串再传入）。
    键名本身也检查（``{"access_token": "..."}`` 会被抓到）。
    """
    if isinstance(obj, str):
        for pattern in _SECRET_PROBES:
            match = pattern.search(obj)
            if match:
                raise ValueError(
                    f"疑似凭据泄漏于 {_path}：匹配到 {redact(match.group(0))!r}"
                )
        return
    if isinstance(obj, Mapping):
        for key, value in obj.items():
            assert_no_secret(key, f"{_path}.<key:{redact(key)}>")
            assert_no_secret(value, f"{_path}.{redact(str(key))}")
        return
    if isinstance(obj, (list, tuple)):
        for index, value in enumerate(obj):
            assert_no_secret(value, f"{_path}[{index}]")
        return
    if obj is None or isinstance(obj, (bool, int, float)):
        return
    raise ValueError(f"无法检查的凭据容器类型 {_path}：{type(obj).__name__}")


# ---------------------------------------------------------------------------
# 5) 主人认证
# ---------------------------------------------------------------------------


def owner_confirm_token(owner_uin: str, day: str, nonce: str) -> str:
    """计算主人确认票据：``sha256(f"{owner_uin}|{day}|{nonce}")`` 的前 32 位十六进制。

    边界：三个参数都必须是 str，否则抛 ``TypeError``；不做任何归一（区分大小写）。
    """
    for name, value in (("owner_uin", owner_uin), ("day", day), ("nonce", nonce)):
        if not isinstance(value, str):
            raise TypeError(f"{name} 必须是 str，实际为 {type(value).__name__}")
    material = f"{owner_uin}|{day}|{nonce}".encode("utf-8")
    return hashlib.sha256(material).hexdigest()[:32]


def check_owner_auth(
    owner_authenticated: bool,
    expected_token: str | None,
    provided_token: str | None,
    confirm: bool,
) -> tuple[bool, str]:
    """主人三层认证，返回 ``(ok, reason)``。

    reason ∈ {"ok", "not_logged_in", "bad_token", "not_confirmed"}。
    顺序固定：先看会话级登录（``not_logged_in``），再比对票据（``bad_token``），
    最后看控制台 ``confirm``（``not_confirmed``）。票据比较走
    ``hmac.compare_digest``（常量时间）。

    边界：``expected_token`` 为 None / 空串时，任何人都无法通过（``bad_token``）；
    ``provided_token`` 不是 str 直接判 ``bad_token``。
    """
    if owner_authenticated is not True:
        return (False, "not_logged_in")
    if not isinstance(expected_token, str) or not expected_token:
        return (False, "bad_token")
    if not isinstance(provided_token, str) or not provided_token:
        return (False, "bad_token")
    if not hmac.compare_digest(
        expected_token.encode("utf-8"), provided_token.encode("utf-8")
    ):
        return (False, "bad_token")
    if confirm is not True:
        return (False, "not_confirmed")
    return (True, "ok")


# ---------------------------------------------------------------------------
# 6) 发布判定
# ---------------------------------------------------------------------------


class PublishOutcome(str, Enum):
    """一次写动作的最终结论（契约第 3 节）。

    - ``PUBLISHED``：上游返回了合法非空 ``tid``。
    - ``REJECTED``：本地预检拒绝（未发出任何写请求），或上游明确 4xx 语义拒绝。
    - ``UNCERTAIN``：发出后拿到超时 / 连接中断 / 5xx / ``tid`` 缺失或不合法。
      **一律不重试、不改发第二条**，并计入日限。
    """

    PUBLISHED = "PUBLISHED"
    REJECTED = "REJECTED"
    UNCERTAIN = "UNCERTAIN"


@dataclass
class PublishDecision:
    """发布 / 联动的放行结论。

    ``detail`` 里带可排查用的上下文（都是脱敏后的、可 JSON 序列化的值），
    例如 ``reason``、``today``、``posts``、``images`` 等。
    """

    allowed: bool
    reason: str
    detail: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """输出纯 JSON 可序列化结构（供路由层直接回包）。"""
        return {
            "allowed": bool(self.allowed),
            "reason": self.reason,
            "detail": json.loads(json.dumps(self.detail, default=str)),
        }


def _sanitize_images(images: Any) -> tuple[bool, list[str], str]:
    """校验图片列表，返回 ``(ok, urls, reason)``。"""
    if images is None:
        return (True, [], "")
    if not isinstance(images, list):
        return (False, [], "images_invalid")
    cleaned: list[str] = []
    for item in images:
        if not isinstance(item, str):
            return (False, [], "images_invalid")
        # 只接受 http(s) 或裸文件名/路径（不含控制字符）
        if _has_control_chars(item):
            return (False, [], "images_invalid")
        cleaned.append(item)
    if len(cleaned) > 9:
        return (False, [], "too_many_images")
    return (True, cleaned, "")


def _day_bucket_raw(state: Mapping[str, Any], day: str) -> Mapping[str, Any]:
    """从 state 里安全取出某天的计数桶（不做写回，保持纯函数）。"""
    days = state.get("days") if isinstance(state, Mapping) else None
    if not isinstance(days, Mapping):
        return {}
    bucket = days.get(day)
    if not isinstance(bucket, Mapping):
        return {}
    return bucket


def _bucket_int(bucket: Mapping[str, Any], kind: str) -> int:
    """从桶里取非负 int（缺失 / 脏值按 0）。"""
    value = bucket.get(kind)
    if not _is_int(value) or value < 0:
        return 0
    return int(value)


def _state_last_publish_ts(state: Mapping[str, Any]) -> int | None:
    """取 last_publish_ts，非法返回 None。"""
    if not isinstance(state, Mapping):
        return None
    value = state.get("last_publish_ts")
    if value is None:
        return None
    if isinstance(value, (tuple, list)) and len(value) == 2:
        value = value[0]
    if _is_number(value) and value == value and value not in (float("inf"), float("-inf")):
        return int(value)
    return None


def _recent_signatures(state: Mapping[str, Any], count: int = 3) -> list[str]:
    """取最近 ``count`` 条快照的签名（按 ts 降序）。"""
    if not isinstance(state, Mapping):
        return []
    snapshot = state.get("snapshot")
    if not isinstance(snapshot, list):
        return []
    items: list[tuple[int, str]] = []
    for entry in snapshot:
        if not isinstance(entry, Mapping):
            continue
        ts = entry.get("ts")
        sig = entry.get("sig")
        if _is_number(ts) and isinstance(sig, str) and sig:
            items.append((int(ts), sig))
    items.sort(key=lambda pair: pair[0], reverse=True)
    return [sig for _, sig in items[:count]]


def evaluate_publish(
    *,
    now_ts: float | int,
    cfg: QzoneConfig,
    state: Mapping[str, Any],
    content: Any,
    candidate_topics: Sequence[str] | None = None,
    owner_authenticated: bool,
    expected_token: str | None,
    provided_token: str | None,
    confirm: bool,
    images: Any = None,
) -> PublishDecision:
    """发布前的完整放行判定（纯函数，无副作用）。

    检查顺序固定（先便宜后贵），命中即返回：

    1. 主人三层认证 → ``owner_unauth``
    2. 数据完整性：正文 → ``text_invalid``；图片 → ``images_invalid`` /
       ``too_many_images``
    3. 日限（含 uncertain 保守占用）→ ``daily_limit``
    4. 最小间隔 → ``min_interval``
    5. 夜间静默 → ``quiet_hours``
    6. 选题重复 → ``repeat_topic``

    全过 → ``PublishDecision(True, "ok", detail)``。

    边界：
    - ``candidate_topics`` 为 None 时，取 ``state["snapshot"]`` 最近 3 条签名做判重
      （契约第 4 节「最近 3 条」）；显式传入则以传入值为准。
    - 本函数只判定、不记账；调用方必须在**发出请求前**另行 ``bump_usage``。
    - 日限先于间隔判定（契约要求），两者都不产生副作用。
    - ``state`` 为 None / 非 Mapping、``cfg`` 类型不对：抛 ``TypeError``。
    """
    if not isinstance(cfg, QzoneConfig):
        raise TypeError("cfg 必须是 QzoneConfig")
    if state is None or not isinstance(state, Mapping):
        raise TypeError("state 必须是 dict")

    now = _coerce_ts(now_ts)
    day = beijing_day(now)
    bucket = _day_bucket_raw(state, day)
    detail: dict[str, Any] = {
        "today": day,
        "now": now,
        "visibility": cfg.visibility,
        "ugc_right": effective_ugc_right(cfg),
    }

    # 1) 主人认证
    auth_ok, auth_reason = check_owner_auth(
        owner_authenticated, expected_token, provided_token, confirm
    )
    if not auth_ok:
        detail["auth"] = auth_reason
        logger.warning("发布被拒：主人认证未通过（%s）", auth_reason)
        return PublishDecision(False, "owner_unauth", detail)

    # 2) 数据完整性
    text_ok, text_reason = validate_text(content, cfg)
    detail["chars"] = text_length(content) if isinstance(content, str) else 0
    if not text_ok:
        detail["text"] = text_reason
        return PublishDecision(False, "text_invalid", detail)

    images_ok, image_list, images_reason = _sanitize_images(images)
    if not images_ok:
        detail["images"] = images_reason
        return PublishDecision(False, images_reason, detail)
    detail["images"] = len(image_list)

    # 3) 日限（含 uncertain 保守占用）
    posts = _bucket_int(bucket, "posts")
    uncertain = _bucket_int(bucket, "uncertain")
    detail["posts"] = posts
    detail["uncertain"] = uncertain
    if posts + uncertain >= cfg.max_posts_per_day:
        detail["max_posts_per_day"] = cfg.max_posts_per_day
        return PublishDecision(False, "daily_limit", detail)

    # 4) 最小间隔
    last_ts = _state_last_publish_ts(state)
    if last_ts is not None and cfg.min_interval_minutes > 0:
        elapsed = now - last_ts
        gap = cfg.min_interval_minutes * 60
        detail["last_publish_ts"] = last_ts
        detail["elapsed_seconds"] = elapsed
        if elapsed < gap:
            detail["next_allowed_ts"] = last_ts + gap
            return PublishDecision(False, "min_interval", detail)

    # 5) 夜间静默
    if in_quiet_hours(now, cfg):
        detail["quiet"] = True
        detail["quiet_start"] = cfg.quiet_start
        detail["quiet_end"] = cfg.quiet_end
        return PublishDecision(False, "quiet_hours", detail)

    # 6) 选题重复
    sig = topic_signature(content)
    detail["sig"] = sig
    if candidate_topics is None:
        recent = _recent_signatures(state, 3)
    else:
        if isinstance(candidate_topics, (str, bytes)) or not isinstance(
            candidate_topics, Iterable
        ):
            raise TypeError("candidate_topics 必须是字符串序列")
        recent = [item for item in candidate_topics if isinstance(item, str)]
    if is_repeat_topic(content, recent):
        detail["recent_topics"] = list(recent)[:3]
        return PublishDecision(False, "repeat_topic", detail)

    detail["quiet"] = False
    return PublishDecision(True, "ok", detail)


def _valid_tid(tid: Any) -> str | None:
    """校验 tid 合法性：字符串、去空白后非空、长度 <= 8192、无控制字符。"""
    if not isinstance(tid, str):
        return None
    stripped = tid.strip()
    if not stripped:
        return None
    if len(stripped) > TID_MAX_LEN:
        return None
    if _has_control_chars(stripped):
        return None
    return stripped


def classify_publish_result(
    upstream_ok: bool, tid: object, error: str | None
) -> PublishOutcome:
    """按上游返回判定结论（契约第 3 节）。

    - ``upstream_ok is False``：``error`` 以 ``"rejected"`` 开头（明确 4xx 语义）
      → ``REJECTED``；``error`` 为 ``"uncertain"`` 开头 → ``UNCERTAIN``；
      其它（超时 / 连接中断 / 5xx / 未知）→ ``UNCERTAIN``。
    - ``upstream_ok is True``：``tid`` 不合法 → ``UNCERTAIN``；否则 ``PUBLISHED``。

    边界：``tid`` 会先 strip；纯空白 / 非字符串 / 含控制字符 / 超长均视为不合法。
    """
    if upstream_ok is not True:
        head = (error or "").strip().lower()
        if head.startswith("rejected"):
            return PublishOutcome.REJECTED
        return PublishOutcome.UNCERTAIN
    if _valid_tid(tid) is None:
        return PublishOutcome.UNCERTAIN
    return PublishOutcome.PUBLISHED


def check_publish_consistency(outcome: PublishOutcome, ugc_right: Any, cfg: QzoneConfig) -> bool:
    """发布后的一致性复核（契约 1.2）：可见性是否与配置一致。

    ``cfg.visibility == "private"`` 时只有 ``ugc_right == 64`` 算通过；其它可见性
    只要求上游回执的 ``ugc_right`` 与期望权限码相同。``ugc_right`` 缺失/非 int
    视为不通过（调用方据此把状态降为 ``UNCERTAIN``，且**不重试**）。
    """
    if not isinstance(outcome, PublishOutcome):
        raise TypeError("outcome 必须是 PublishOutcome")
    expected = effective_ugc_right(cfg)
    if not _is_int(ugc_right):
        return False
    return int(ugc_right) == expected


def effective_ugc_right(cfg: QzoneConfig) -> int:
    """把 ``cfg.visibility`` 映射成上游 ``ugc_right`` 权限码。"""
    if not isinstance(cfg, QzoneConfig):
        raise TypeError("cfg 必须是 QzoneConfig")
    return VISIBILITY_UGC_RIGHT[cfg.visibility]


# ---------------------------------------------------------------------------
# 8) 联动（点赞 / 评论）
# ---------------------------------------------------------------------------


def evaluate_followup(
    *,
    now_ts: float | int,
    cfg: QzoneConfig,
    state: Mapping[str, Any],
    kind: str,
    owner_authenticated: bool,
    expected_token: str | None,
    provided_token: str | None,
    confirm: bool,
    target_tid: Any,
) -> PublishDecision:
    """点赞 / 评论的放行判定（纯函数，无副作用）。

    检查顺序：主人认证 → 目标合法性 → 开关 → 日限 → 夜间静默。

    reason ∈ {"ok", "owner_unauth", "bad_target", "disabled", "daily_limit",
    "quiet_hours", "not_confirmed"}。

    边界：
    - ``kind`` 不在 {"like","comment"} → 抛 ``ValueError``（编程错误，不吞）。
    - 开关关闭（``enable_like`` / ``enable_comment`` 为 False）→ ``disabled``，
      调用方不得发出任何 OneBot 写请求。
    - ``target_tid`` 非法（非 str / 空白 / 含控制字符 / 超长）→ ``bad_target``。
    - 日限各自独立；夜间静默同样适用。
    """
    if not isinstance(cfg, QzoneConfig):
        raise TypeError("cfg 必须是 QzoneConfig")
    if state is None or not isinstance(state, Mapping):
        raise TypeError("state 必须是 dict")
    if kind not in FOLLOWUP_KINDS:
        raise ValueError(f"kind 必须是 {FOLLOWUP_KINDS} 之一，实际为 {kind!r}")

    now = _coerce_ts(now_ts)
    day = beijing_day(now)
    bucket = _day_bucket_raw(state, day)
    detail: dict[str, Any] = {"today": day, "now": now, "kind": kind}

    auth_ok, auth_reason = check_owner_auth(
        owner_authenticated, expected_token, provided_token, confirm
    )
    if not auth_ok:
        detail["auth"] = auth_reason
        logger.warning("联动被拒：主人认证未通过（%s）", auth_reason)
        return PublishDecision(False, "owner_unauth", detail)

    tid = _valid_tid(target_tid)
    if tid is None:
        return PublishDecision(False, "bad_target", detail)
    detail["tid"] = tid

    if kind == "like":
        enabled = cfg.enable_like
        limit = cfg.max_likes_per_day
    else:
        enabled = cfg.enable_comment
        limit = cfg.max_comments_per_day
    if not enabled:
        return PublishDecision(False, "disabled", detail)

    used = _bucket_int(bucket, "likes" if kind == "like" else "comments")
    detail["used"] = used
    if used >= limit:
        detail["limit"] = limit
        return PublishDecision(False, "daily_limit", detail)

    if in_quiet_hours(now, cfg):
        detail["quiet"] = True
        return PublishDecision(False, "quiet_hours", detail)

    detail["quiet"] = False
    return PublishDecision(True, "ok", detail)


# ---------------------------------------------------------------------------
# 7) 计数
# ---------------------------------------------------------------------------


def make_empty_state() -> dict[str, Any]:
    """返回一个合法的空 state（与 store 层结构一致），便于测试与首次初始化。"""
    return {
        "version": 1,
        "days": {},
        "snapshot": [],
        "last_publish_ts": None,
        "pending": {},
        "recovered": False,
        "fatal": False,
    }


def day_bucket(state: dict[str, Any], day: str) -> dict[str, int]:
    """取得（必要时创建）某日的计数桶，保证四个键都是非负 int。

    边界：``state["days"]`` 缺失 / 非 dict 时**就地补建**（这是本函数唯一的写操作，
    属于「规范化 state」；``state`` 必须是可变 dict，否则抛 ``TypeError``）。
    缺失键、负数、非 int、bool 一律按 0 处理。
    """
    if not isinstance(state, dict):
        raise TypeError("state 必须是可变 dict")
    if not isinstance(day, str) or not day:
        raise ValueError("day 必须是非空字符串")
    days = state.get("days")
    if not isinstance(days, dict):
        days = {}
        state["days"] = days
    bucket = days.get(day)
    if not isinstance(bucket, dict):
        bucket = {}
        days[day] = bucket
    clean: dict[str, int] = {}
    for kind in USAGE_KINDS:
        clean[kind] = _bucket_int(bucket, kind)
    # 就地规范化：保证调用方拿到的一定是四个非负 int 键
    for kind, value in clean.items():
        bucket[kind] = value
    return bucket


def count_usage(state: Mapping[str, Any], day: str, kind: str) -> int:
    """读取某日某类计数（缺失 / 脏值按 0）。``kind`` 非法抛 ``ValueError``。"""
    if kind not in USAGE_KINDS:
        raise ValueError(f"kind 必须是 {USAGE_KINDS} 之一，实际为 {kind!r}")
    if not isinstance(day, str) or not day:
        raise ValueError("day 必须是非空字符串")
    bucket = _day_bucket_raw(state, day)
    return _bucket_int(bucket, kind)


def bump_usage(state: Mapping[str, Any], day: str, kind: str, n: int = 1) -> dict[str, Any]:
    """返回「某日某类计数 +n」后的**新** state（深拷贝，不改入参）。

    边界：``n`` 必须是 >=0 的 int（负增量会削弱保守计数，直接拒）；
    ``kind`` 非法抛 ``ValueError``；``state`` 不可 JSON 序列化时抛 ``TypeError``。
    """
    if kind not in USAGE_KINDS:
        raise ValueError(f"kind 必须是 {USAGE_KINDS} 之一，实际为 {kind!r}")
    if not _is_int(n) or n < 0:
        raise ValueError("n 必须是 >= 0 的 int")
    if not isinstance(day, str) or not day:
        raise ValueError("day 必须是非空字符串")
    if state is None or not isinstance(state, Mapping):
        raise TypeError("state 必须是 dict")
    new_state = json.loads(json.dumps(dict(state), default=str))
    bucket = day_bucket(new_state, day)
    bucket[kind] = bucket[kind] + n
    return new_state


def allowance(
    state: Mapping[str, Any],
    day: str,
    cfg: QzoneConfig,
    now_ts: float | int | None = None,
) -> dict[str, Any]:
    """计算某日的剩余额度与下一个可发布时刻。

    返回：:

        {"posts_left": int, "likes_left": int, "comments_left": int,
         "next_allowed_ts": int | None, "quiet": bool, "today": day}

    边界：
    - ``posts_left = max(0, max_posts - posts - uncertain)``（uncertain 保守占用）。
    - ``next_allowed_ts = last_publish_ts + min_interval*60``（无记录 / 间隔为 0 → None）。
    - ``quiet`` 需要「当前时刻」才能判断。传入 ``now_ts`` 时按 ``in_quiet_hours``
      给真实值；不传时恒为 ``False``（纯函数层不读时钟，绝不猜当前时间）。
    """
    if not isinstance(cfg, QzoneConfig):
        raise TypeError("cfg 必须是 QzoneConfig")
    if not isinstance(day, str) or not day:
        raise ValueError("day 必须是非空字符串")
    posts = count_usage(state, day, "posts")
    uncertain = count_usage(state, day, "uncertain")
    likes = count_usage(state, day, "likes")
    comments = count_usage(state, day, "comments")
    last_ts = _state_last_publish_ts(state)
    next_allowed: int | None = None
    if last_ts is not None and cfg.min_interval_minutes > 0:
        next_allowed = last_ts + cfg.min_interval_minutes * 60
    quiet = in_quiet_hours(now_ts, cfg) if now_ts is not None else False
    return {
        "posts_left": max(0, cfg.max_posts_per_day - posts - uncertain),
        "likes_left": max(0, cfg.max_likes_per_day - likes),
        "comments_left": max(0, cfg.max_comments_per_day - comments),
        "next_allowed_ts": next_allowed,
        "quiet": quiet,
        "today": day,
    }


#: 好友动态「只读刷新」的独立限窗（契约 §10）。
#: 与发布额度完全分开计数，互不占用。
READ_REFRESH_MIN_INTERVAL_SECONDS: int = 600          # 两次刷新至少隔 10 分钟
READ_REFRESH_MAX_PER_DAY: int = 24                    # 每北京日最多 24 次
READ_REFRESH_QUIET_START: str = "00:00"               # 00–07 静默
READ_REFRESH_QUIET_END: str = "07:00"
READ_FEED_TEXT_MAX_CHARS: int = 280                   # 快照里折叠后的正文上限
READ_LATEST_LIMIT: int = 20                           # latest 快照保留条数

#: 「稳定错误」分类（契约 §10.2）。这几类失败重试也没用，需要人去看配置或账号。
READ_ERROR_DISABLED = "read_disabled"          # 未配置可用的 loopback OneBot，fail closed
READ_ERROR_QUIET = "read_quiet"                # 00–07 静默
READ_ERROR_INTERVAL = "read_interval"          # 距上次刷新太近
READ_ERROR_DAILY = "read_daily_limit"          # 今日刷新次数用完
READ_ERROR_UNAUTH = "owner_unauth"             # 主人未认证
READ_ERROR_UPSTREAM = "read_upstream"          # 上游返回明确错误（非超时/断连）
READ_ERROR_REJECTED = "read_rejected"          # 上游参数/权限类拒绝
READ_ERROR_UNAVAILABLE = "read_unavailable"    # 超时/断连/5xx：上游暂不可用
READ_ERROR_MALFORMED = "read_malformed"        # 返回结构无法解析

READ_ERRORS = frozenset(
    {
        READ_ERROR_DISABLED, READ_ERROR_QUIET, READ_ERROR_INTERVAL, READ_ERROR_DAILY,
        READ_ERROR_UNAUTH, READ_ERROR_UPSTREAM, READ_ERROR_REJECTED,
        READ_ERROR_UNAVAILABLE, READ_ERROR_MALFORMED,
    }
)


def in_read_quiet_hours(ts: float | int) -> bool:
    """好友动态刷新是否处于静默（固定 00:00–07:00 北京时间，半开区间）。"""
    dt = beijing_now(ts)
    return 0 <= dt.hour < 7


def read_allowance(state: Mapping[str, Any], day: str, now_ts: float | int | None = None) -> dict:
    """好友动态刷新的剩余额度与下一次可刷新时刻。

    与发布额度**独立**：只看 ``state["read_refresh"][day]``。
    """
    if not isinstance(day, str) or not day:
        raise ValueError("day 必须是非空字符串")
    bucket = (state.get("read_refresh") or {}).get(day) or {}
    used = bucket.get("count") if isinstance(bucket, Mapping) else 0
    try:
        used_int = max(0, int(used))
    except (TypeError, ValueError):
        used_int = 0
    last_ts = bucket.get("last_ts") if isinstance(bucket, Mapping) else None
    try:
        last_int = int(last_ts) if last_ts is not None else None
    except (TypeError, ValueError):
        last_int = None
    next_allowed = None
    if last_int is not None:
        next_allowed = last_int + READ_REFRESH_MIN_INTERVAL_SECONDS
    return {
        "refreshes_left": max(0, READ_REFRESH_MAX_PER_DAY - used_int),
        "refreshes_today": used_int,
        "next_allowed_ts": next_allowed,
        "quiet": in_read_quiet_hours(now_ts) if now_ts is not None else False,
        "today": day,
    }


def evaluate_read_refresh(
    *, now_ts: float | int, state: Mapping[str, Any], owner_authenticated: bool,
    transport_available: bool,
) -> "PublishDecision":
    """是否可以发起一次好友动态只读刷新。**纯函数，无副作用。**

    顺序固定：owner → transport（fail closed）→ 静默 → 日限 → 最小间隔。
    """
    day = beijing_day(now_ts)
    if not owner_authenticated:
        return PublishDecision(False, READ_ERROR_UNAUTH, {"allow": False})
    if not transport_available:
        return PublishDecision(False, READ_ERROR_DISABLED, {"allow": False})
    if in_read_quiet_hours(now_ts):
        return PublishDecision(False, READ_ERROR_QUIET,
                               {"quiet": True, "window": [READ_REFRESH_QUIET_START, READ_REFRESH_QUIET_END]})
    quota = read_allowance(state, day, now_ts)
    if quota["refreshes_left"] <= 0:
        return PublishDecision(False, READ_ERROR_DAILY, quota)
    next_allowed = quota["next_allowed_ts"]
    if next_allowed is not None and int(now_ts) < next_allowed:
        return PublishDecision(False, READ_ERROR_INTERVAL,
                               {**quota, "retry_after_seconds": next_allowed - int(now_ts)})
    return PublishDecision(True, "ok", quota)


def bump_read_refresh(state: Mapping[str, Any], day: str, ts: float | int) -> dict:
    """记一次刷新（先记账后发请求，和发布同口径）。返回新 state。"""
    if not isinstance(day, str) or not day:
        raise ValueError("day 必须是非空字符串")
    new_state = json.loads(json.dumps(dict(state), default=str)) if state else {}
    table = new_state.get("read_refresh")
    if not isinstance(table, dict):
        table = {}
    bucket = table.get(day)
    if not isinstance(bucket, dict):
        bucket = {}
    try:
        count = max(0, int(bucket.get("count", 0)))
    except (TypeError, ValueError):
        count = 0
    bucket["count"] = count + 1
    bucket["last_ts"] = int(ts)
    table[day] = bucket
    new_state["read_refresh"] = table
    return new_state


def _feed_text(feed: Mapping[str, Any]) -> str:
    """从上游条目里取正文并折叠：只截断，不解释、不改写。"""
    for key in ("content", "summary", "text"):
        raw = feed.get(key)
        if isinstance(raw, str) and raw.strip():
            return normalize_text(raw)[:READ_FEED_TEXT_MAX_CHARS]
    return ""


def _feed_author(feed: Mapping[str, Any]) -> str:
    for key in ("nickname", "nick", "name"):
        raw = feed.get(key)
        if isinstance(raw, str) and raw.strip():
            return mask_uin(raw.strip())[:64]
    for key in ("uin", "user_id"):
        raw = feed.get(key)
        if raw is not None and str(raw).strip():
            return mask_uin(str(raw).strip())
    return ""


def normalize_latest_feeds(feeds: Any, *, limit: int = READ_LATEST_LIMIT) -> list[dict]:
    """把上游 ``get_qzone_feeds`` 的条目收敛成快照行。

    **只保留展示需要的字段**，正文折叠截断，作者打码；不保留任何原始对象、
    不保留 like_key / cookie 之类可能含凭据的字段。上游结构不可预期，
    这里逐条尽力而为：解析不了的条目整条丢弃，而不是编造。
    """
    if not isinstance(feeds, (list, tuple)):
        return []
    cap = max(1, min(READ_LATEST_LIMIT, int(limit)))
    rows: list[dict] = []
    for item in feeds:
        if not isinstance(item, Mapping):
            continue
        tid = item.get("tid")
        if not isinstance(tid, str) or not tid.strip():
            continue
        ts_raw = item.get("time")
        try:
            ts = int(ts_raw) if ts_raw is not None else 0
        except (TypeError, ValueError):
            ts = 0
        images = item.get("images")
        image_count = len(images) if isinstance(images, (list, tuple)) else 0
        try:
            likes = int(item.get("like_count", item.get("like_num", 0)) or 0)
        except (TypeError, ValueError):
            likes = 0
        try:
            comments = int(item.get("comment_count", item.get("comment_num", 0)) or 0)
        except (TypeError, ValueError):
            comments = 0
        rows.append(
            {
                "tid": tid.strip()[:8192],
                "author": _feed_author(item),
                "text": _feed_text(item),
                "ts": ts,
                "day": beijing_day(ts) if ts else "",
                "images": max(0, image_count),
                "likes": max(0, likes),
                "comments": max(0, comments),
            }
        )
        if len(rows) >= cap:
            break
    return rows


def read_error_from_kind(kind: str) -> str:
    """把 OneBot 失败 kind 映射成稳定错误码（便于控制台与运维对上号）。"""
    text = str(kind or "").strip().lower()
    if text in ("timeout", "conn", "http", "badjson", "retcode"):
        return READ_ERROR_UNAVAILABLE
    if text in ("rejected", "badcall"):
        return READ_ERROR_REJECTED
    return READ_ERROR_UPSTREAM


# ---------------------------------------------------------------------------
# 9) 快照
# ---------------------------------------------------------------------------


@dataclass
class SnapshotEntry:
    """一条发布快照记录（用于「同一快照」判定与选题判重）。"""

    ts: int
    tid: str
    day: str
    outcome: str
    chars: int
    sig: str
    has_images: bool
    ugc_right: int

    def __post_init__(self) -> None:
        self.ts = _coerce_ts(self.ts)
        if not isinstance(self.tid, str):
            raise ValueError("SnapshotEntry.tid 必须是 str")
        if not isinstance(self.day, str) or not self.day:
            raise ValueError("SnapshotEntry.day 必须是非空 str")
        if not isinstance(self.outcome, str) or not self.outcome:
            raise ValueError("SnapshotEntry.outcome 必须是非空 str")
        self.chars = _check_int_range("chars", self.chars, 0, 1_000_000)
        if not isinstance(self.sig, str):
            raise ValueError("SnapshotEntry.sig 必须是 str")
        self.has_images = _check_bool("has_images", self.has_images)
        if not _is_int(self.ugc_right):
            raise ValueError("SnapshotEntry.ugc_right 必须是 int")

    def to_dict(self) -> dict[str, Any]:
        """输出纯 JSON 可序列化 dict。"""
        return {
            "ts": int(self.ts),
            "tid": self.tid,
            "day": self.day,
            "outcome": self.outcome,
            "chars": int(self.chars),
            "sig": self.sig,
            "has_images": bool(self.has_images),
            "ugc_right": int(self.ugc_right),
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "SnapshotEntry":
        """从 dict 还原；缺字段 / 类型错抛 ``ValueError``。"""
        if not isinstance(raw, Mapping):
            raise ValueError("快照条目必须是 dict")
        try:
            return cls(
                ts=raw["ts"],
                tid=raw["tid"],
                day=raw["day"],
                outcome=raw["outcome"],
                chars=raw["chars"],
                sig=raw["sig"],
                has_images=raw["has_images"],
                ugc_right=raw["ugc_right"],
            )
        except KeyError as exc:
            raise ValueError(f"快照条目缺少字段：{exc.args[0]}") from exc


def build_snapshot(state: Mapping[str, Any], limit: int = 20) -> list[dict[str, Any]]:
    """返回最近 ``limit`` 条快照（按 ts 降序）。

    边界：``limit <= 0`` 返回空列表；脏条目（非 dict / ts 非数字）直接跳过，
    不抛异常（快照只用于展示与判重，不应阻断主流程）。
    """
    if not _is_int(limit):
        raise ValueError("limit 必须是 int")
    if not isinstance(state, Mapping):
        return []
    snapshot = state.get("snapshot")
    if not isinstance(snapshot, list):
        return []
    rows: list[tuple[int, dict[str, Any]]] = []
    for index, entry in enumerate(snapshot):
        if not isinstance(entry, Mapping):
            continue
        ts = entry.get("ts")
        if not _is_number(ts) or ts != ts:
            continue
        rows.append((int(ts), dict(entry)))
    rows.sort(key=lambda pair: pair[0], reverse=True)
    if limit <= 0:
        return []
    return [row for _, row in rows[:limit]]


def snapshot_digest(entries: Sequence[Any]) -> str:
    """对快照条目序列求摘要（sha256 前 16 位），用于判定「同一快照」。

    边界：条目可以是 dict 或 ``SnapshotEntry``；每条的字段按固定顺序取，
    与 key 顺序无关；``entries`` 为空时返回空串（表示「无快照可比」）。
    """
    if entries is None:
        raise TypeError("entries 不能是 None")
    if isinstance(entries, (str, bytes)) or not isinstance(entries, Iterable):
        raise TypeError("entries 必须是快照条目序列")
    lines: list[str] = []
    for entry in entries:
        if isinstance(entry, SnapshotEntry):
            data = entry.to_dict()
        elif isinstance(entry, Mapping):
            data = _entry_for_digest(entry)
        else:
            raise TypeError(f"快照条目必须是 dict 或 SnapshotEntry，实际为 {type(entry).__name__}")
        lines.append("|".join(str(data[key]) for key in SNAPSHOT_KEY_ORDER))
    if not lines:
        return ""
    return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()[:16]


#: ``snapshot_digest`` 的字段顺序（固定，避免 dict 键序影响摘要）。
SNAPSHOT_KEY_ORDER: tuple[str, ...] = (
    "ts",
    "tid",
    "day",
    "outcome",
    "chars",
    "sig",
    "has_images",
    "ugc_right",
)


def _entry_for_digest(entry: Mapping[str, Any]) -> dict[str, Any]:
    """把宽松的快照 dict 兜底成固定字段（脏值按零值），仅用于算摘要。"""
    raw_ts = entry.get("ts", 0)
    ts = int(raw_ts) if _is_number(raw_ts) else 0
    raw_chars = entry.get("chars", 0)
    chars = int(raw_chars) if _is_number(raw_chars) else 0
    raw_right = entry.get("ugc_right", 0)
    right = int(raw_right) if _is_number(raw_right) else 0
    return {
        "ts": ts,
        "tid": str(entry.get("tid", "")),
        "day": str(entry.get("day", "")),
        "outcome": str(entry.get("outcome", "")),
        "chars": chars,
        "sig": str(entry.get("sig", "")),
        "has_images": 1 if entry.get("has_images") else 0,
        "ugc_right": right,
    }


def append_snapshot(
    state: Mapping[str, Any], entry: Any, keep: int = 200
) -> dict[str, Any]:
    """返回「追加一条快照 + 截断到 ``keep``」后的**新** state（深拷贝）。

    边界：``keep`` 必须是 >=1 的 int；``entry`` 可以是 ``SnapshotEntry`` 或 dict
    （dict 会走 ``SnapshotEntry.from_dict`` 校验，失败抛 ``ValueError``）。
    保持「最新的在前」：新条目插到列表头部，并按 ts 降序重新排列后截断。
    """
    if not _is_int(keep) or keep < 1:
        raise ValueError("keep 必须是 >= 1 的 int")
    if not isinstance(state, Mapping):
        raise TypeError("state 必须是 dict")
    if isinstance(entry, SnapshotEntry):
        payload = entry.to_dict()
    elif isinstance(entry, Mapping):
        payload = SnapshotEntry.from_dict(entry).to_dict()
    else:
        raise TypeError(f"entry 必须是 SnapshotEntry 或 dict，实际为 {type(entry).__name__}")
    new_state = json.loads(json.dumps(dict(state), default=str))
    snapshot = new_state.get("snapshot")
    if not isinstance(snapshot, list):
        snapshot = []
    rows: list[tuple[int, int, dict[str, Any]]] = []
    for index, item in enumerate(snapshot):
        if isinstance(item, Mapping) and _is_number(item.get("ts")):
            rows.append((-int(item["ts"]), index, dict(item)))
    rows.append((-int(payload["ts"]), -1, payload))
    rows.sort(key=lambda row: (row[0], row[1]))
    new_state["snapshot"] = [row[2] for row in rows[:keep]]
    return new_state


# ---------------------------------------------------------------------------
# 10) 枚举转换
# ---------------------------------------------------------------------------


def outcome_str(outcome: PublishOutcome) -> str:
    """``PublishOutcome`` → 大写字符串。非枚举入参抛 ``TypeError``。"""
    if not isinstance(outcome, PublishOutcome):
        raise TypeError("outcome 必须是 PublishOutcome")
    return outcome.value


def parse_outcome(s: Any) -> PublishOutcome:
    """字符串 → ``PublishOutcome``（大小写与首尾空白不敏感）。

    非法字符串抛 ``ValueError``；非字符串抛 ``TypeError``。
    """
    if not isinstance(s, str):
        raise TypeError(f"outcome 字符串必须是 str，实际为 {type(s).__name__}")
    try:
        return PublishOutcome(s.strip().upper())
    except KeyError as exc:  # Enum 取值失败抛 KeyError（3.11 行为）
        raise ValueError(f"未知 outcome：{s!r}") from exc
    except ValueError as exc:
        raise ValueError(f"未知 outcome：{s!r}") from exc
