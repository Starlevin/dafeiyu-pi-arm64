#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""QQ 旧记忆 -> QQMemoryReliableStore 迁移 dry-run 校验器。

只读输入文件，绝不修改生产 state；输出一份 JSON 报告。
默认把任何无法映射/会丢失的当前记录视为 FAIL，历史已丢（droppedEvents）视为 WARN。

退出码：
  0 PASS  当前 items/recent/pending 全部可映射，无阻塞错误
  1 WARN  可映射，但存在历史已丢、敏感文本需重脱敏、超长截断等告警
  2 FAIL  结构错误、重复冲突、队列超容量、活动记录不可映射

用法：
  python memory_migration_check.py --state qq-state.json --out report.json
  python memory_migration_check.py --selftest
  python memory_migration_check.py --make-fixture fixtures/qq-state-synthetic.json
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import tempfile
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any

VERSION = "1.0.0"
ALLOWED_KINDS = {"preference", "fact", "task", "event", "episode", "legacy"}
# 新 store 的 subject 约定：OWNER/PEER/CONVERSATION，群成员用 MEMBER_<hex>。
SUBJECT_RE = re.compile(r"^(OWNER|PEER|CONVERSATION|BOT|MEMBER_[0-9a-fA-F]{6,32})$")
SCOPE_RE = re.compile(r"^[^:\s]{1,80}:[^:\s]{1,120}(?::[^:\s]{1,120})?$")
# 仅用于统计，不把命中内容写进报告。
SENSITIVE_PATTERNS = [
    ("private_key", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----")),
    ("api_key", re.compile(r"(?i)\b(sk-[A-Za-z0-9_-]{12,}|cfut_[A-Za-z0-9_-]{12,}|ghp_[A-Za-z0-9]{20,})\b")),
    ("bearer", re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._-]{16,}")),
    ("email", re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")),
    ("cn_id", re.compile(r"\b\d{17}[0-9Xx]\b")),
    ("phone", re.compile(r"\b1[3-9]\d{9}\b")),
    ("qq_number", re.compile(r"(?i)(?:qq|群号|账号)[：:\s]*\d{5,12}")),
]
MAX_ITEM_TEXT = 2000
MAX_EVENT_TEXT = 4000
DEFAULT_QUEUE_CAPACITY = 1024
DEFAULT_MAX_ACTIVE = 256
DEFAULT_MAX_HISTORICAL = 128
DEFAULT_MAX_EPISODES = 256
DEFAULT_RECENT = 24


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def sensitive_types(text: str) -> list[str]:
    hits = []
    for name, pattern in SENSITIVE_PATTERNS:
        if pattern.search(text):
            hits.append(name)
    return hits


def as_int(value: Any, default: int = 0) -> int:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str) and value.strip().lstrip("-").isdigit():
        return int(value.strip())
    return default


def as_list(value: Any) -> list:
    return value if isinstance(value, list) else []


class ScopeReport:
    def __init__(self, key: str):
        self.key = key
        self.account = ""
        self.target_kind = "unknown"
        self.item_ids: set[str] = set()
        self.item_total = 0
        self.item_active = 0
        self.item_historical = 0
        self.item_by_kind: Counter[str] = Counter()
        self.episodes = 0
        self.pending_hot = 0
        self.pending_cold = 0
        self.recent = 0
        self.known_ids = 0
        self.dropped_events = 0
        self.refused_events = 0
        self.redacted_items = 0
        self.truncated_items = 0
        self.sensitive_kinds: Counter[str] = Counter()
        self.duplicate_item_ids = 0
        self.duplicate_queued_ids = 0
        self.conflicting_ids = 0
        self.mapped_l1 = 0
        self.mapped_l2 = 0
        self.mapped_l3 = 0
        self.mapped_wal = 0
        self.warnings: list[str] = []
        self.errors: list[str] = []

    def warn(self, message: str) -> None:
        if message not in self.warnings:
            self.warnings.append(message)

    def error(self, message: str) -> None:
        if message not in self.errors:
            self.errors.append(message)

    def to_dict(self) -> dict[str, Any]:
        return {
            "scope": self.key,
            "account": self.account,
            "targetKind": self.target_kind,
            "items": {
                "total": self.item_total,
                "active": self.item_active,
                "historical": self.item_historical,
                "byKind": dict(sorted(self.item_by_kind.items())),
            },
            "episodes": self.episodes,
            "pending": {"hot": self.pending_hot, "cold": self.pending_cold, "total": self.pending_hot + self.pending_cold},
            "recent": self.recent,
            "knownIDs": self.known_ids,
            "droppedEvents": self.dropped_events,
            "refusedEvents": self.refused_events,
            "mapped": {
                "l1Recent": self.mapped_l1,
                "l2FactsAndPlans": self.mapped_l2,
                "l3Episodes": self.mapped_l3,
                "walQueued": self.mapped_wal,
            },
            "safety": {
                "redactedItems": self.redacted_items,
                "truncatedItems": self.truncated_items,
                "sensitiveKinds": dict(sorted(self.sensitive_kinds.items())),
            },
            "duplicates": {
                "itemIDs": self.duplicate_item_ids,
                "queuedIDs": self.duplicate_queued_ids,
                "conflictingIDs": self.conflicting_ids,
            },
            "warnings": self.warnings,
            "errors": self.errors,
        }


def validate_item(item: Any, scope: ScopeReport, seen_ids: dict[str, str], now: datetime) -> None:
    if not isinstance(item, dict):
        scope.error("items 中存在非对象条目")
        return
    item_id = item.get("id")
    if not isinstance(item_id, str) or not item_id.strip():
        scope.error("item 缺少非空 id")
        return
    item_id = item_id.strip()
    if item_id in seen_ids:
        scope.duplicate_item_ids += 1
        previous = seen_ids[item_id]
        current = json.dumps(item, ensure_ascii=False, sort_keys=True)
        if previous != current:
            scope.conflicting_ids += 1
            scope.error(f"item id 重复且内容冲突：{item_id[:32]}")
        else:
            scope.warn(f"item id 重复且内容相同：{item_id[:32]}")
        return
    seen_ids[item_id] = json.dumps(item, ensure_ascii=False, sort_keys=True)
    scope.item_total += 1

    subject = item.get("subject")
    if not isinstance(subject, str) or not SUBJECT_RE.match(subject):
        scope.error(f"item {item_id[:24]} 的 subject 不合法：{str(subject)[:40]}")
    kind = item.get("kind")
    if kind not in ALLOWED_KINDS:
        scope.error(f"item {item_id[:24]} 的 kind 不合法：{str(kind)[:24]}")
        kind = "unknown"
    scope.item_by_kind[kind] += 1
    if kind == "episode":
        scope.episodes += 1
        if subject != "CONVERSATION":
            scope.error(f"episode {item_id[:24]} 的 subject 必须是 CONVERSATION")
    elif subject == "CONVERSATION":
        scope.warn(f"非 episode 条目 {item_id[:24]} 使用了 CONVERSATION subject")

    text = item.get("text")
    if not isinstance(text, str) or not text.strip():
        scope.error(f"item {item_id[:24]} 缺少 text")
        text = ""
    if len(text) > MAX_ITEM_TEXT:
        scope.truncated_items += 1
        scope.warn(f"item {item_id[:24]} 文本 {len(text)} 字，超过 {MAX_ITEM_TEXT} 会截断")
    hits = sensitive_types(text)
    if hits:
        scope.redacted_items += 1
        for hit in hits:
            scope.sensitive_kinds[hit] += 1

    updated = parse_time(item.get("updatedAt")) or parse_time(item.get("createdAt"))
    expires = parse_time(item.get("expiresAt"))
    invalidated = parse_time(item.get("invalidatedAt"))
    if updated is None:
        scope.warn(f"item {item_id[:24]} 缺少可解析时间，迁移时按导入时间处理")
    if expires is None:
        scope.warn(f"item {item_id[:24]} 缺少 expiresAt，迁移时按永久处理")
    active = invalidated is None and (expires is None or expires > now)
    if active:
        scope.item_active += 1
    else:
        scope.item_historical += 1
    if kind == "episode" and active:
        scope.mapped_l3 += 1


def validate_event(event: Any, scope: ScopeReport, queue_ids: dict[str, str], item_ids: set[str]) -> None:
    if not isinstance(event, dict):
        scope.error("pending 中存在非对象条目")
        return
    event_id = event.get("id")
    if not isinstance(event_id, str) or not event_id.strip():
        scope.error("pending event 缺少非空 id")
        return
    event_id = event_id.strip()
    if event_id in queue_ids:
        scope.duplicate_queued_ids += 1
        scope.warn(f"pending event id 重复：{event_id[:32]}")
        return
    queue_ids[event_id] = json.dumps(event, ensure_ascii=False, sort_keys=True)
    if event_id in item_ids:
        scope.conflicting_ids += 1
        scope.error(f"pending event 与已知 item 冲突：{event_id[:32]}")
    subject = event.get("subject")
    if not isinstance(subject, str) or not SUBJECT_RE.match(subject):
        scope.error(f"pending event {event_id[:24]} 的 subject 不合法")
    if subject in {"BOT", "CONVERSATION"}:
        scope.warn(f"pending event {event_id[:24]} 的 subject={subject} 不适合入队")
    text = event.get("text")
    if not isinstance(text, str) or not text.strip():
        scope.error(f"pending event {event_id[:24]} 缺少 text")
        text = ""
    if len(text) > MAX_EVENT_TEXT:
        scope.truncated_items += 1
        scope.warn(f"pending event {event_id[:24]} 文本超过 {MAX_EVENT_TEXT} 会截断")
    hits = sensitive_types(text)
    if hits:
        scope.redacted_items += 1
        for hit in hits:
            scope.sensitive_kinds[hit] += 1
    if parse_time(event.get("at")) is None:
        scope.warn(f"pending event {event_id[:24]} 缺少可解析 at")


def analyze_state(data: Any, now: datetime, queue_capacity: int) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise ValueError("state 根节点必须是 JSON 对象")
    books = data.get("memoryBooks")
    if books is None:
        books = {}
    if not isinstance(books, dict):
        raise ValueError("memoryBooks 必须是对象")

    reports: list[ScopeReport] = []
    total_dropped = 0
    for raw_key, raw_book in books.items():
        key = str(raw_key)
        scope = ScopeReport(key)
        if not SCOPE_RE.match(key):
            scope.warn("scope key 不是 account:target 形式")
        else:
            scope.account, target = key.split(":", 1)
            scope.target_kind = "group" if target.lower().startswith("group") or "群" in target else "private-or-group"
        if not isinstance(raw_book, dict):
            scope.error("memoryBook 不是对象")
            reports.append(scope)
            continue

        items = as_list(raw_book.get("items"))
        pending = as_list(raw_book.get("pending"))
        cold = as_list(raw_book.get("archivedPending"))
        recent = as_list(raw_book.get("recent"))
        known = as_list(raw_book.get("knownIDs"))
        scope.pending_hot = len(pending)
        scope.pending_cold = len(cold)
        scope.recent = len(recent)
        scope.known_ids = len(known)
        scope.dropped_events = as_int(raw_book.get("droppedEvents"))
        scope.refused_events = as_int(raw_book.get("refusedEvents"))
        total_dropped += scope.dropped_events
        if scope.dropped_events > 0:
            scope.warn(f"历史 droppedEvents={scope.dropped_events}，这些片段在迁移前已丢失，只能告警不可恢复")
        if scope.refused_events > 0:
            scope.warn(f"历史 refusedEvents={scope.refused_events}，为硬上限拒收，不是新增丢失")
        if scope.pending_hot + scope.pending_cold > queue_capacity:
            scope.error(f"队列 {scope.pending_hot + scope.pending_cold} 条超过迁移容量 {queue_capacity}")
        if scope.recent > DEFAULT_RECENT:
            scope.warn(f"recent={scope.recent} 超过短期上下文 {DEFAULT_RECENT} 条，迁移只保留最近 {DEFAULT_RECENT} 条")

        seen_ids: dict[str, str] = {}
        for item in items:
            validate_item(item, scope, seen_ids, now)
        item_ids = set(seen_ids)
        queue_ids: dict[str, str] = {}
        for event in pending:
            validate_event(event, scope, queue_ids, item_ids)
        for event in cold:
            validate_event(event, scope, queue_ids, item_ids)

        # 映射：所有 items 都会进入 store（并先写 archive 副本）；活动事实进 L2，episode 进 L3，
        # pending+cold 进 WAL，recent 只保留 24 条。
        scope.mapped_l2 = scope.item_total - scope.episodes
        scope.mapped_l3 = scope.episodes
        scope.mapped_wal = len(queue_ids)
        scope.mapped_l1 = min(scope.recent, DEFAULT_RECENT)
        if scope.mapped_l2 > DEFAULT_MAX_ACTIVE + DEFAULT_MAX_HISTORICAL:
            scope.warn(
                f"非 episode items={scope.mapped_l2} 超过活动+历史容量 "
                f"{DEFAULT_MAX_ACTIVE + DEFAULT_MAX_HISTORICAL}，将全部写入 state 并由 archive 兜底"
            )
        if scope.mapped_l3 > DEFAULT_MAX_EPISODES:
            scope.warn(f"episodes={scope.mapped_l3} 超过容量 {DEFAULT_MAX_EPISODES}，archive 兜底")
        if scope.item_active > DEFAULT_MAX_ACTIVE:
            scope.warn(f"活动事实={scope.item_active} 超过 {DEFAULT_MAX_ACTIVE}，下次整理会按重要性裁剪（archive 可恢复）")
        if scope.item_historical > DEFAULT_MAX_HISTORICAL:
            scope.warn(f"历史事实={scope.item_historical} 超过 {DEFAULT_MAX_HISTORICAL}，下次整理会裁剪（archive 可恢复）")
        reports.append(scope)

    errors = sum(len(r.errors) for r in reports)
    warnings = sum(len(r.warnings) for r in reports)
    return {
        "version": VERSION,
        "dryRun": True,
        "generatedAt": now.isoformat(),
        "queueCapacity": queue_capacity,
        "scopes": [r.to_dict() for r in reports],
        "totals": {
            "scopes": len(reports),
            "items": sum(r.item_total for r in reports),
            "active": sum(r.item_active for r in reports),
            "historical": sum(r.item_historical for r in reports),
            "episodes": sum(r.episodes for r in reports),
            "pending": sum(r.pending_hot + r.pending_cold for r in reports),
            "recent": sum(r.recent for r in reports),
            "redactedItems": sum(r.redacted_items for r in reports),
            "truncatedItems": sum(r.truncated_items for r in reports),
            "droppedEvents": total_dropped,
            "errors": errors,
            "warnings": warnings,
        },
        "verdict": "FAIL" if errors else ("WARN" if warnings else "PASS"),
    }


def load_state(path: str) -> Any:
    if not os.path.isfile(path):
        raise ValueError(f"state 文件不存在：{path}")
    size = os.path.getsize(path)
    if size > 64 * 1024 * 1024:
        raise ValueError(f"state 文件 {size} bytes，超过 64MiB 安全上限")
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def write_report(report: dict[str, Any], out_path: str | None) -> None:
    text = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=False)
    if out_path:
        parent = os.path.dirname(os.path.abspath(out_path))
        os.makedirs(parent, exist_ok=True)
        with open(out_path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text + "\n")
    else:
        print(text)


def synthetic_state() -> dict[str, Any]:
    """按现场计数生成合成副本：items 0/36/30/368/65/4，pending 0/0/0/0/0/1，dropped 485/64。"""
    counts = [0, 36, 30, 368, 65, 4]
    pending_counts = [0, 0, 0, 0, 0, 1]
    dropped = [0, 485, 0, 64, 0, 0]
    books: dict[str, Any] = {}
    base = datetime(2026, 11, 17, 8, 0, tzinfo=timezone.utc)
    for i, (items_count, pending_count) in enumerate(zip(counts, pending_counts)):
        key = f"10001:{'group' if i % 2 == 0 else 'private'}{i + 1}"
        items = []
        for n in range(items_count):
            kind = "episode" if n % 17 == 0 else ("preference" if n % 5 == 0 else ("task" if n % 7 == 0 else "fact"))
            subject = "CONVERSATION" if kind == "episode" else ("OWNER" if n % 3 == 0 else "MEMBER_0123456789ab")
            items.append({
                "id": f"item-{i}-{n}",
                "subject": subject,
                "kind": kind,
                "text": f"合成记忆 {i}-{n}：周五晚上一起打副本，偏好清淡口味。" if n % 11 else "合成记忆：电话 13800138000，邮箱 test@example.com",
                "keywords": ["合成", "副本"],
                "importance": 1 + (n % 3),
                "sourceIDs": [f"msg-{i}-{n}"],
                "createdAt": (base + timedelta(minutes=n)).isoformat(),
                "updatedAt": (base + timedelta(minutes=n)).isoformat(),
                "expiresAt": (base + timedelta(days=90 + (n % 30))).isoformat(),
                "invalidatedAt": None if n % 13 else (base + timedelta(days=1)).isoformat(),
            })
        pending = []
        for n in range(pending_count):
            pending.append({
                "id": f"pending-{i}-{n}",
                "subject": "OWNER",
                "text": "合成待整理片段：明天提醒带身份证。",
                "at": (base + timedelta(minutes=n)).isoformat(),
            })
        books[key] = {
            "items": items,
            "pending": pending,
            "archivedPending": [],
            "recent": [],
            "knownIDs": [item["id"] for item in items],
            "droppedEvents": dropped[i],
            "refusedEvents": 0,
        }
    return {"version": 1, "memoryBooks": books}


def run_selftest() -> int:
    state = synthetic_state()
    with tempfile.TemporaryDirectory(prefix="qq-memory-migration-") as tmp:
        path = os.path.join(tmp, "qq-state-synthetic.json")
        with open(path, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(state, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
        report = analyze_state(state, now_utc(), DEFAULT_QUEUE_CAPACITY)
        counts = [s["items"]["total"] for s in report["scopes"]]
        pending = [s["pending"]["total"] for s in report["scopes"]]
        dropped = sum(s["droppedEvents"] for s in report["scopes"])
        if counts != [0, 36, 30, 368, 65, 4]:
            print(f"SELFTEST FAIL items={counts}", file=sys.stderr)
            return 2
        if pending != [0, 0, 0, 0, 0, 1]:
            print(f"SELFTEST FAIL pending={pending}", file=sys.stderr)
            return 2
        if dropped != 549:
            print(f"SELFTEST FAIL dropped={dropped}", file=sys.stderr)
            return 2
        if report["verdict"] == "FAIL":
            print(json.dumps(report["totals"], ensure_ascii=False), file=sys.stderr)
            return 2
        print(json.dumps({
            "selftest": "PASS",
            "items": counts,
            "pending": pending,
            "droppedEvents": dropped,
            "verdict": report["verdict"],
            "redactedItems": report["totals"]["redactedItems"],
        }, ensure_ascii=False))
        return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="QQ 旧记忆迁移 dry-run 校验（只读）")
    parser.add_argument("--state", help="旧 qq-state.json 路径（只读）")
    parser.add_argument("--out", help="报告输出路径；省略则打印到 stdout")
    parser.add_argument("--capacity", type=int, default=DEFAULT_QUEUE_CAPACITY, help="每 scope WAL 容量")
    parser.add_argument("--strict", action="store_true", help="WARN 也按失败退出")
    parser.add_argument("--selftest", action="store_true", help="用合成副本自测，不读任何真实 state")
    parser.add_argument("--make-fixture", help="生成合成 state 副本到指定路径后退出")
    args = parser.parse_args(argv)

    if args.make_fixture:
        state = synthetic_state()
        parent = os.path.dirname(os.path.abspath(args.make_fixture))
        os.makedirs(parent, exist_ok=True)
        with open(args.make_fixture, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(state, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
        print(f"已生成合成副本：{args.make_fixture}")
        return 0

    if args.selftest:
        return run_selftest()

    if not args.state:
        parser.error("需要 --state，或使用 --selftest/--make-fixture")

    now = now_utc()
    try:
        data = load_state(args.state)
        report = analyze_state(data, now, max(1, args.capacity))
    except (ValueError, json.JSONDecodeError, OSError) as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 2

    report["input"] = {
        "path": os.path.abspath(args.state),
        "sha256": sha256_file(args.state),
        "bytes": os.path.getsize(args.state),
    }
    write_report(report, args.out)
    code = 0 if report["verdict"] == "PASS" else (1 if report["verdict"] == "WARN" else 2)
    if args.strict and code == 1:
        code = 2
    print(
        f"{report['verdict']} scopes={report['totals']['scopes']} items={report['totals']['items']} "
        f"pending={report['totals']['pending']} dropped={report['totals']['droppedEvents']} "
        f"warn={report['totals']['warnings']} err={report['totals']['errors']}",
        file=sys.stderr,
    )
    return code


if __name__ == "__main__":
    sys.exit(main())
