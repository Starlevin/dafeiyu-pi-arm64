#!/usr/bin/env python3
"""大肥鱼 model-hub 用量记账（TokenSave）。

设计要点（全部来自真实网关 model-hub.py 的现状）：
  * 记账只发生在**真实上游调用**处：每次 `_invoke()` 向 provider 发出的那一次请求，
    成功或失败各记一次；主通道回退到备用时，两个真正计费的模型各记一次。
  * 数字**只取 provider 返回的 usage**。缺 usage 时不估算，只把 `missing` 计数 +1。
  * 幂等：每个请求有一个 request_id，同一 (request_id, attempt) 只会入账一次；
    记账状态持久化（原子写 JSON），进程重启后已入账的请求不会被重复计入。
  * 绝不因为记账失败而影响聊天：所有入口包在 try/except 里，异常只写日志。
  * 不记录任何消息正文、密钥、请求体；只记 provider / 实际 model / 计数 / 时间。

存储模型（schema 2）
  ``state["since"]["s-<slot>"] = [in, out, total, calls, missing]``
      **长期汇总**，独立于日分桶，永不被保留期裁剪掉。累计口径以它为准。
  ``state["buckets"]["YYYY-MM-DD"]["s-<slot>"] = [in, out, total, calls, missing]``
      **日分桶**，只服务「今日 / 近 7 日」，超过保留期即裁剪，不影响累计。
  * 日期统一按 Asia/Shanghai 切分（UTC+8，无夏令时，固定偏移即可精确换算），
    与主机本地时区无关，避免 Pi 上 TZ 变化导致跨日错位。
  * total 用 provider 给的 total_tokens；**只有**在 provider 未给 total 时才用 in+out
    （这不是估算：两者都是 provider 的数字）。若 provider 连 in/out 都缺，total 不计。
  * slot = sha256(provider \\0 model) 的前 16 个十六进制字符；原始字符串写在
    ``state["slots"]`` 里，只有持有文件的人能看到。
  * 模型种类不设上限：实际模型由受控 profile 决定，任何模型都必须入账且可见。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import sys
import tempfile
import threading
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

SCHEMA = 2
RETENTION_DAYS = 400          # **仅**裁剪日分桶；累计存在 since，不受此值影响
DEDUPE_KEEP = 20000           # 幂等表最大条目数
DEDUPE_TTL = 7 * 86400        # 幂等表条目保留秒数
RESERVE_KEEP = 8192           # 只做预登记、从未真正记账的令牌最多留这么多

# 日期口径：一律按 Asia/Shanghai（UTC+8，1991 年后无夏令时）换算，
# 不依赖主机本地时区，避免 Pi 上 TZ 变化把「今日」算错。
CST = timezone(timedelta(hours=8), "Asia/Shanghai")

COUNTER_KEYS = ("in", "out", "total", "calls", "missing")

# provider 标识：优先取 profile 里显式声明的 provider，否则按协议/base_url 推断。
# 键必须与 model-hub 的 profile['protocol'] 取值一致：chat / responses / oauth。
_KIND = {
    "chat": "openai-compat",
    "responses": "openai-responses",
    "oauth": "chatgpt-oauth",
}


def now_cst():
    """当前时间，带 Asia/Shanghai 时区（供记账与测试注入）。"""
    return datetime.now(CST)


def day_key(moment=None):
    """把时刻换算成 Asia/Shanghai 的 YYYY-MM-DD。"""
    moment = moment or now_cst()
    if not isinstance(moment, datetime):
        moment = now_cst()
    if moment.tzinfo is None:
        # 无时区的 datetime 视为 Shanghai 本地时间（记账入口只传带时区的时刻）。
        moment = moment.replace(tzinfo=CST)
    return moment.astimezone(CST).strftime("%Y-%m-%d")


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def secrets_token() -> str:
    """每次上游调用一个随机令牌（不暴露给客户端，只存在于本地账本）。"""
    return secrets.token_hex(16)


class ReserveHandle(str):
    """reserve() 的返回值。

    正常情况下它就是令牌字符串本身（可直接当作 token 用）；
    当预登记**没能落盘**时，它是空令牌 + unsaved=True，让调用方仍能记账但
    不会被幂等表误伤（既不永久收留、也不影响之后的正常入账）。
    """

    unsaved = False

    def __new__(cls, token, unsaved=False):
        obj = super().__new__(cls, token or "")
        obj.unsaved = bool(unsaved) or not token
        return obj


def _plain(token):
    """把 reserve() 的返回值归一成纯令牌字符串（unsaved 句柄视为无令牌）。"""
    if isinstance(token, ReserveHandle):
        return None if token.unsaved else str(token)
    return str(token) if token else None


def _migrate_v1(old):
    """schema 1 → 2：用历史日分桶重建长期汇总，之后再裁剪也不会丢掉累计。"""
    state = {"schema": SCHEMA, "started": old.get("started") or int(time.time()),
             "since": {}, "buckets": {}, "slots": old.get("slots") or {},
             "seen": old.get("seen") or {}, "reserved": old.get("reserved") or [],
             "rev": old.get("rev") or 0}
    for bucket in (old.get("buckets") or {}).values():
        if not isinstance(bucket, dict):
            continue
        for slot, row in bucket.items():
            if not (isinstance(row, list) and len(row) >= len(COUNTER_KEYS)):
                continue
            target = state["since"].setdefault(slot, [0, 0, 0, 0, 0])
            for index in range(len(COUNTER_KEYS)):
                try:
                    target[index] += max(0, int(row[index] or 0))
                except (TypeError, ValueError):
                    pass
    for day, bucket in (old.get("buckets") or {}).items():
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(day)) and isinstance(bucket, dict):
            state["buckets"][day] = bucket
    return state


def infer_provider(profile) -> str:
    """从 profile 推断 provider 名（不含任何密钥，只读协议与主机名）。"""
    explicit = str(profile.get("provider") or "").strip()
    if explicit:
        return explicit[:40]
    kind = _KIND.get(str(profile.get("protocol") or ""), "unknown")
    try:
        from urllib.parse import urlsplit
        host = (urlsplit(str(profile.get("base_url") or "")).hostname or "").lower()
    except Exception:
        host = ""
    if not host:
        return kind
    return "%s@%s" % (kind, host)[:64]


def _pick(obj, *names):
    """从 dict 里读第一个合法非负整数；其余一律视为缺失。"""
    if not isinstance(obj, dict):
        return None
    for name in names:
        if name in obj:
            value = obj.get(name)
            if isinstance(value, bool):
                continue
            if isinstance(value, int) and value >= 0:
                return value
            if isinstance(value, float) and value.is_integer() and value >= 0:
                return int(value)
    return None


def normalize_usage(usage):
    """把 OpenAI / Anthropic / Responses 三种真实形态归一成 (in, out, total)。

    返回 (in|None, out|None, total|None)。缺失就是 None——**绝不填 0 冒充**。
    """
    if not isinstance(usage, dict) or not usage:
        return None, None, None
    # OpenAI chat/completions: prompt_tokens / completion_tokens / total_tokens
    # OpenAI responses: input_tokens / output_tokens / total_tokens
    # Anthropic messages: input_tokens / output_tokens（无 total）
    # Anthropic 流式: message_start.message.usage 只有 input，末帧 message_delta.usage 有 output
    prompt = _pick(usage, "prompt_tokens", "input_tokens")
    output = _pick(usage, "completion_tokens", "output_tokens")
    total = _pick(usage, "total_tokens")
    # Anthropic 把缓存读写单列；缓存读的 token 也计费/计入上下文，故并入输入。
    cache_read = _pick(usage, "cache_read_input_tokens")
    cache_write = _pick(usage, "cache_creation_input_tokens")
    cache_create = _pick(usage, "prompt_cache_miss_tokens")
    if prompt is not None and (cache_read or cache_write):
        prompt = prompt + (cache_read or 0) + (cache_write or 0)
    elif prompt is not None and cache_create:
        prompt = prompt + cache_create
    if total is None and prompt is not None and output is not None:
        total = prompt + output      # 两个数字都来自 provider，不是估算
    return prompt, output, total


def usage_absent(usage):
    """True = provider 没给任何可用数字（要单独计一次缺用量）。"""
    prompt, output, total = normalize_usage(usage)
    return prompt is None and output is None and total is None


class UsageLedger:
    """线程安全、原子持久化的用量账本。所有公开方法都不抛异常。"""

    def __init__(self, path, log=None, retention_days=RETENTION_DAYS):
        self.path = Path(path)
        self.log = log or (lambda msg: None)
        self.retention_days = int(retention_days)
        self.lock = threading.RLock()
        self.state = None

    # ---- 持久化 ---------------------------------------------------------- #
    def _blank(self):
        return {"schema": SCHEMA, "started": int(time.time()),
                "since": {}, "buckets": {}, "slots": {}, "seen": {},
                "reserved": [], "rev": 0}

    def load(self):
        with self.lock:
            if self.state is not None:
                return self.state
            data = None
            migrating = False
            if self.path.is_file():
                try:
                    data = json.loads(self.path.read_text(encoding="utf-8"))
                except Exception:
                    # 损坏的账本不能连带把网关打挂：留档后从空账本继续。
                    try:
                        broken = self.path.with_suffix(".broken-%d" % int(time.time()))
                        self.path.replace(broken)
                    except Exception:
                        pass
                    self.log("usage-ledger: 账本损坏，已另存并重建")
                    data = None
            if isinstance(data, dict) and data.get("schema") == 1:
                data = _migrate_v1(data)     # 旧 schema 一次性升到 2，累计不被裁掉
                migrating = True
            if not isinstance(data, dict) or data.get("schema") != SCHEMA:
                data = self._blank()
            for key, value in self._blank().items():
                data.setdefault(key, value)
            for name in ("buckets", "since", "seen", "slots"):
                if not isinstance(data.get(name), dict):
                    data[name] = {}
            if not isinstance(data.get("reserved"), list):
                data["reserved"] = []
            self.state = data
            if migrating:
                # 迁移后立即落盘；失败也不影响本次读取（下次会再迁一次）。
                self._flush()
            return self.state

    def _flush(self):
        """原子写：同目录 tmp → fsync → rename。返回 True=确实落盘。"""
        state = self.state
        rev = int(state.get("rev") or 0)
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(prefix=".usage-", dir=str(self.path.parent))
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    json.dump(state, handle, ensure_ascii=False, separators=(",", ":"))
                    handle.flush()
                    os.fsync(handle.fileno())
                try:
                    os.chmod(tmp, 0o600)   # 权限收紧失败不应连带丢掉这次记账
                except OSError:
                    pass
                os.replace(tmp, self.path)
            except Exception:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise
            state["rev"] = rev + 1
            return True
        except Exception as error:      # 磁盘满 / 只读：绝不影响聊天
            state["rev"] = rev          # rev 也回到落盘前的值
            self.log("usage-ledger: 写入失败（%s）" % type(error).__name__)
            return False

    @staticmethod
    def _clone(state):
        """落盘前快照；只在需要精确回滚时调用。"""
        return json.loads(json.dumps(state))

    @staticmethod
    def _add(row, cells):
        for index, name in enumerate(COUNTER_KEYS):
            row[index] += cells.get(name, 0)

    @staticmethod
    def _cells(usage):
        prompt, output, total = normalize_usage(usage)
        if prompt is None and output is None and total is None:
            return {"calls": 1, "missing": 1, "in": 0, "out": 0, "total": 0}
        return {"calls": 1, "missing": 0, "in": prompt or 0,
                "out": output or 0, "total": total or 0}

    # ---- 幂等 ------------------------------------------------------------ #
    def _seen_key(self, request_id, attempt):
        return _digest("%s\0%s" % (request_id, attempt))

    def already_recorded(self, request_id, attempt=0):
        if not request_id:
            return False
        try:
            with self.lock:
                return self._seen_key(request_id, attempt) in (self.load().get("seen") or {})
        except Exception:
            return False

    def _mark_seen(self, key, slot, at):
        state = self.load()
        seen = state["seen"]
        if key in seen:
            return False
        if len(seen) >= DEDUPE_KEEP:
            cutoff = int(time.time()) - DEDUPE_TTL
            fresh = {k: v for k, v in seen.items()
                     if isinstance(v, dict) and int(v.get("at") or 0) >= cutoff}
            if len(fresh) > DEDUPE_KEEP // 2:
                # TTL 内仍然过多：丢最早的一半，保证内存与文件都有界。
                ordered = sorted(fresh.items(), key=lambda kv: int(kv[1].get("at") or 0))
                fresh = dict(ordered[len(ordered) // 2:])
            state["seen"] = fresh
        state["seen"][key] = {"at": int(at), "slot": slot}
        return True

    # ---- 记一笔 ---------------------------------------------------------- #
    def reserve(self, provider, model):
        """在**请求发出之前**登记一个幂等令牌。

        这样即便进程在请求过程中被重启（systemd Restart=on-failure），重放的调用
        也不会被计第二次：同一个 token 只能成功 record() 一次。
        预登记**没落盘**时返回不是真实令牌的 `unsaved=True` 句柄：它不会被幂等表
        永久收留，也不影响后面 record() 正常入账。
        """
        try:
            with self.lock:
                before = self._clone(self.load())
                state = self.state
                provider = (str(provider or "unknown").strip() or "unknown")[:64]
                model = (str(model or "unknown").strip() or "unknown")[:128]
                slot = _digest(provider + "\0" + model)
                state["slots"].setdefault("s-" + slot, {"p": provider, "m": model})
                token = secrets_token()
                state["reserved"] = list(state.get("reserved") or [])[-RESERVE_KEEP:] + [token]
                if self._flush():
                    return token
                self.state = before          # 精确回滚：内存里不留没落盘的登记
                return ReserveHandle(None, unsaved=True)
        except Exception as error:
            self.log("usage-ledger: 预登记失败（%s）" % type(error).__name__)
            return ReserveHandle(None, unsaved=True)

    def record(self, provider, model, usage=None, status=200, token=None,
               attempt=0, kind="chat", now=None):
        """记录一次真实上游调用。返回 True=已入账，False=重复/被拒/异常。"""
        try:
            return self._record(provider, model, usage, status, _plain(token),
                                attempt, kind, now)
        except Exception as error:
            self.log("usage-ledger: 记账失败（%s）" % type(error).__name__)
            return False

    def _record(self, provider, model, usage, status, token, attempt, kind, now):
        provider = (str(provider or "unknown").strip() or "unknown")[:64]
        model = (str(model or "unknown").strip() or "unknown")[:128]
        slot = "s-" + _digest(provider + "\0" + model)
        moment = now or now_cst()
        if not isinstance(moment, datetime):
            moment = now_cst()
        day = day_key(moment)
        key = self._seen_key(token, attempt) if token else None
        with self.lock:
            before = self._clone(self.load())
            state = self.state
            if key and key in state["seen"]:
                return False
            if key and not self._mark_seen(key, slot, moment.timestamp()):
                return False
            cells = self._cells(usage)
            # 长期汇总（永不被裁剪）与日分桶同时累加，两者口径一致。
            self._add(state["since"].setdefault(slot, [0, 0, 0, 0, 0]), cells)
            bucket = state["buckets"].setdefault(day, {})
            self._add(bucket.setdefault(slot, [0, 0, 0, 0, 0]), cells)
            state["slots"].setdefault(slot, {"p": provider, "m": model})
            self._prune(days=day)
            if self._flush():
                return True
            # 没落盘：整份内存状态回滚到快照（计数、seen、reserved、rev 全部还原），
            # 保证「内存里看得到的 = 磁盘上有的」，不出现虚账或漏账。
            self.state = before
            return False

    def _prune(self, days=None):
        """只裁剪日分桶；累计在 state['since']，不受保留期影响。"""
        state = self.load()
        keep_from = (date.fromisoformat(day_key(now_cst()))
                     - timedelta(days=self.retention_days)).strftime("%Y-%m-%d")
        buckets = state["buckets"]
        for key in list(buckets):
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", key) or key < keep_from:
                buckets.pop(key, None)
        if days:
            for key in list(buckets):
                if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", key):
                    buckets.pop(key, None)

    # ---- 查询 ------------------------------------------------------------ #
    def empty_dashboard(self, reason=""):
        return {
            "available": False, "reason": reason,
            "startedAt": None, "today": None, "days7": None,
            "since": None, "dayCount": 0, "rev": 0,
            "coverage": coverage(),
        }

    def dashboard(self, today=None):
        """返回与 UI 合同一致的汇总（今日 / 近 7 日 / 累计，按 provider+model）。"""
        try:
            with self.lock:
                state = self.load()
                slots = dict(state.get("slots") or {})
                rows = {k: v for k, v in (state.get("buckets") or {}).items()
                        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", k)}
                since_rows = dict(state.get("since") or {})
                started = state.get("started")
                rev = state.get("rev") or 0
        except Exception:
            return self.empty_dashboard("账本不可读")
        ref = today or now_cst().date()
        if isinstance(ref, datetime):
            ref = day_key(ref)
        if isinstance(ref, str):
            try:
                ref = date.fromisoformat(ref)
            except ValueError:
                ref = now_cst().date()
        if not isinstance(ref, date):
            ref = now_cst().date()
        today_key = ref.strftime("%Y-%m-%d")
        week_start = (ref - timedelta(days=6)).strftime("%Y-%m-%d")
        return {
            "available": True,
            "reason": "",
            "startedAt": started,
            "today": aggregate(rows, slots, lambda d: d == today_key),
            "days7": aggregate(rows, slots, lambda d: week_start <= d <= today_key),
            # 累计读长期汇总，与日分桶保留期完全无关（day=None 不走日期过滤）。
            "since": aggregate({None: since_rows}, slots, lambda d: True),
            "dayCount": len(rows),
            "rev": rev,
            "coverage": coverage(),
        }


def _serial(slot):
    """slot 名 → 数字，仅用于同 provider/model 时的稳定次序。"""
    value = slot[2:] if slot.startswith("s-") else slot
    try:
        return int(value, 16)
    except ValueError:
        return 0


def aggregate(rows, slots, keep):
    """对满足 keep(day) 的分桶求和，返回合同结构。

    rows 为 {day: {slot: row}}；传 {"": since} 这类 day=None 的条目表示长期汇总，
    不做任何日期过滤。
    """
    groups = {}
    totals = {"in": 0, "out": 0, "total": 0, "calls": 0, "missing": 0}
    calls_any = False
    for day, bucket in rows.items():
        # day=None 表示传入的是长期汇总（since），不做日期过滤。
        if day is not None and not keep(day):
            continue
        if not isinstance(bucket, dict):
            continue
        for slot, row in bucket.items():
            meta = slots.get(slot) or {}
            if not isinstance(meta, dict):
                meta = {}
            provider = str(meta.get("p") or "未知 provider")
            model = str(meta.get("m") or "未知模型")
            if not (isinstance(row, list) and len(row) >= 5):
                continue
            cells = {}
            for index, name in enumerate(COUNTER_KEYS):
                try:
                    cells[name] = max(0, int(row[index] or 0))
                except (TypeError, ValueError):
                    cells[name] = 0
            key = (provider, model)
            group = groups.setdefault(key, {"provider": provider, "model": model,
                                            "in": 0, "out": 0, "total": 0,
                                            "calls": 0, "missing": 0})
            for name, value in cells.items():
                group[name] += value
                totals[name] += value
            if cells["calls"]:
                calls_any = True
    # 稳定排序：先 provider（不区分大小写），再 model，最后才是隐藏的 slot 序号。
    items = sorted(groups.values(),
                   key=lambda g: (g["provider"].lower(), g["model"].lower()))
    totals["callers"] = len([g for g in groups.values() if g["calls"]])
    return {"totals": totals, "providers": items, "empty": not calls_any}


def coverage():
    """明确写出覆盖边界，供 UI 原样展示（不含任何凭据）。"""
    return {
        "inScope": [
            "model-hub /v1/chat/completions 的每一次上游调用（含主通道与 fallback 备用模型）",
            "OpenAI 兼容 chat（chat/completions 非流式，prompt_tokens/completion_tokens/total_tokens）",
            "OpenAI Responses（非流式 /responses，input_tokens/output_tokens/total_tokens）",
            "ChatGPT OAuth Responses 流式（末帧 response.completed.response.usage）",
        ],
        "notInScope": [
            "绕过本网关的第三方模型直连接口（不计入本记账范围）",
            "第三方图像生成接口（不计入本记账范围）",
            "ChatGPT OAuth 的 token 刷新与 /v1/models 列举（不产生对话 token）",
            "上游返回 4xx 且未给 usage 的错误请求：计入调用数，同时计入缺用量",
        ],
        "zeroToken": ["/v1/models", "/api/status 等本地只读接口不经过任何上游模型"],
    }


# --------------------------------------------------------------------------- #
# 流式（SSE）用量提取：只在真的拿到 provider 的 usage 时才返回数字
# --------------------------------------------------------------------------- #

def sse_usage(lines):
    """从 SSE 行迭代器里取 usage。

    覆盖：
      * Anthropic messages：message_start -> message.usage（输入），
        message_delta -> usage.output_tokens（输出）
      * OpenAI chat/completions：流的最后一个含 usage 的 chunk（需 stream_options）
      * OpenAI Responses：response.completed -> response.usage
    返回 (usage_dict|None, completed: bool)。completed 表示确实看到了正常结束帧。
    """
    merged = {}
    completed = False
    for raw in lines:
        if isinstance(raw, bytes):
            try:
                raw = raw.decode("utf-8", "replace")
            except Exception:
                continue
        line = raw.strip()
        if not line or not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if payload == "[DONE]":
            completed = True
            continue
        try:
            event = json.loads(payload)
        except Exception:
            continue
        if not isinstance(event, dict):
            continue
        kind = str(event.get("type") or "")
        if kind.startswith("message_") or event.get("usage") or event.get("response"):
            for part in (event.get("usage"), (event.get("message") or {}).get("usage"),
                         (event.get("response") or {}).get("usage")):
                if isinstance(part, dict):
                    for name, value in part.items():
                        if isinstance(value, (int, float)) and not isinstance(value, bool):
                            merged[name] = max(merged.get(name, 0) or 0, int(value))
        if kind in ("message_stop", "response.completed") or event.get("choices"):
            if kind in ("message_stop", "response.completed"):
                completed = True
        if kind in ("error", "response.failed"):
            return (merged or None), False
    return (merged or None), completed


if __name__ == "__main__":       # 便于本地手工核对，不参与服务启动
    demo = UsageLedger(sys.argv[1] if len(sys.argv) > 1 else "/tmp/usage-demo.json")
    demo.record("openai-compat@api.example", "demo-1",
                {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
                request_id="demo-1", attempt=0)
    print(json.dumps(demo.dashboard(), ensure_ascii=False, indent=2))
