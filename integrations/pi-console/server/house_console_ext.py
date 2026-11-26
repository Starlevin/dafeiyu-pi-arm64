#!/usr/bin/env python3
"""Unified Fish House + Extended Module Routes for DaFeiYu Unified Console.

Provides:
  1. Same-origin Fish House hosting on the authenticated Console listener:
       GET /home                   -> 302 /fish_house/home.html
       GET /fish_house/<subpath>   -> static R5.2 Fish House assets (path-traversal guarded)
       GET /api/status             -> read-only proxy to local fish-house-lan /api/status
       GET /fish_house/state.json  -> read-only proxy to local fish-house-lan /fish_house/state.json
       GET /api/house/events       -> read-only SSE proxy to local fish-house-lan /api/house/events
       POST /api/diag              -> non-sensitive frontend rendering telemetry
  2. Real, honest, zero-secret backend adapters for Unified Console Modules 4, 6, 7, 9 + Gemini:
       GET  /api/memory/status     -> per-group memory isolation count & storage metadata (never reads chat text)
       GET  /api/memory/storage    -> bounded file metadata for QQ, Swift, and compatibility stores
       GET/POST /api/memory/wechat -> scoped Swift group memory management
       GET  /api/stickers          -> live QQ & WeChat sticker packs inventory (including 6 original GIFs)
       GET  /api/proactive/config  -> live WeChat proactiveJudge & QQ ReplyDecision V2 state
       GET  /api/trace/summary     -> aggregated model-hub route latency & fallback summary (zero UUID/content leak)
       GET  /api/delivery/summary  -> platform delivery counters with separate observation scopes
       GET  /api/gemini/health     -> live 127.0.0.1:8085/status/summary + active gemini-web profile model
       POST /api/gemini/model      -> switch the gemini-web profile to a model in the gateway catalogue
"""
from __future__ import annotations

import http.client
import json
import os
import re
import shutil
import stat
import statistics
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
WECHAT_MEMORY_ROUTE = re.compile(r"/api/memory/wechat(?:/([0-9a-f]{64}))?")
WECHAT_PERSONA_ROUTE = re.compile(r"/api/persona/wechat(?:/([0-9a-f]{64}))?")

MIME_MAP = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".txt": "text/plain; charset=utf-8",
    ".webp": "image/webp",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".svg": "image/svg+xml",
    ".mp4": "video/mp4",
}

GEMINI_SAFE_DEFAULT = "gemini-auto"
GEMINI_MODEL_NAME = re.compile(r"gemini-[a-z0-9]+(?:[.-][a-z0-9]+)*")
HOUSE_THEME_VIDEO = re.compile(
    r"/fish_house/themes/[A-Za-z0-9._-]{1,64}/videos/[A-Za-z0-9._-]{1,128}\.mp4")
PROACTIVE_EVENTS = (
    "ordinary", "candidate", "gate_passed", "score_passed", "score_rejected", "score_skipped", "score_after_reject",
    "enqueued", "after_enqueue_skip", "confirmed",
)
PROACTIVE_LINE = re.compile(
    r"\[qq_proactive_funnel\] at=([0-9]{1,12}(?:\.[0-9]{1,9})?) scope=([0-9a-f]{16}) "
    r"event=(" + "|".join(PROACTIVE_EVENTS) + r") reason=([a-z0-9_-]{1,48}|-)\s*$"
)
PROACTIVE_GROUP = re.compile(r"group:[1-9][0-9]{0,17}")
PROACTIVE_CONFIG_LOCK = threading.Lock()

# Public-question auto-chat interval (seconds). Shared by QQ decision rows and
# the WeChat core authoritative config. The default is a constant exposed to
# the UI via GET; maps below contain only explicitly configured values so the
# UI can distinguish "set by the operator" from "using the default".
PUBLIC_QUESTION_COOLDOWN_DEFAULT = 60
PUBLIC_QUESTION_COOLDOWN_MIN = 30
PUBLIC_QUESTION_COOLDOWN_MAX = 1200
COOLDOWN_KEY = "publicQuestionCooldownSeconds"
COOLDOWN_MAP_KEY = "publicQuestionCooldowns"
ORDINARY_REPLY_KEY = "ordinaryReplyEnabled"
ORDINARY_THRESHOLD_MIN = 0.30
ORDINARY_THRESHOLD_MAX = 0.95


def _fish_house_web_dir(cfg: dict) -> Path | None:
    configured = str(cfg.get("fishHouseWebDir") or os.environ.get("DAFEIYU_FISH_HOUSE_WEB_DIR") or "").strip()
    candidates = []
    if configured:
        candidates.append(Path(configured).expanduser())
    candidates.append(Path.home() / "fish-house-lan" / "web" / "fish_house")
    candidates.append(HERE.parent.parent / "fish-house-lan" / "web" / "fish_house")
    for c in candidates:
        try:
            if c.is_dir() and (c / "home.html").is_file():
                return c.resolve()
        except OSError:
            continue
    return None


def _fish_house_base_url(cfg: dict) -> str:
    explicit = str(cfg.get("fishHouseUrl") or os.environ.get("DAFEIYU_FISH_HOUSE_URL") or "").strip()
    if explicit:
        return explicit.rstrip("/")
    lan_host = str(cfg.get("lanHost") or "127.0.0.1").strip()
    return f"http://{lan_host}:18777"


def _qq_root(cfg: dict) -> Path | None:
    raw = str(cfg.get("qqRoot") or os.environ.get("DAFEIYU_QQ_ROOT") or "").strip()
    if not raw:
        return None
    p = Path(raw).expanduser()
    return p if p.exists() else None


def _wechat_memory_admin(cfg, method, scope, body):
    path = "/api/admin/wechat-memory" + ("/" + scope if scope else "")
    return _wechat_admin(cfg, method, path, body, {
        400: "记忆修改请求无效，请检查条目和内容",
        404: "该群记忆当前不可管理",
        409: "条目已被更新或到期，请刷新后重试",
        503: "微信记忆服务暂不可用，请刷新核对",
    })


def _wechat_admin(cfg, method, upstream_path, body, errors):
    try:
        root = _qq_root(cfg)
        if root is None:
            raise OSError
        path = root / "data-qq" / "unified-pilot.memory-admin.json"
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        with os.fdopen(os.open(path, flags), "rb") as runtime:
            mode = os.fstat(runtime.fileno())
            if not stat.S_ISREG(mode.st_mode) or (os.name == "posix" and
                    (mode.st_uid != os.getuid() or stat.S_IMODE(mode.st_mode) != 0o600)):
                raise OSError
            metadata = json.loads(runtime.read(8192))
        port, token = metadata["port"], metadata["token"]
        if (type(port) is not int or not 1 <= port <= 65535 or not isinstance(token, str)
                or re.fullmatch(r"[0-9A-Fa-f-]{72}", token) is None):
            raise ValueError
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=4)
        try:
            headers = {"Origin": f"http://127.0.0.1:{port}", "x-unified-admin-token": token,
                       "Content-Type": "application/json"}
            payload = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
            connection.request(method, upstream_path, payload, headers)
            upstream = connection.getresponse()
            if upstream.status != 200:
                if upstream.status in errors:
                    return upstream.status, {"error": errors[upstream.status]}
                raise OSError
            raw = upstream.read(512_001)
            if len(raw) > 512_000:
                raise ValueError
            result = json.loads(raw)
            if not isinstance(result, dict) or result.get("ok") is not True:
                raise ValueError
            return 200, result
        finally:
            connection.close()
    except (OSError, KeyError, TypeError, ValueError, http.client.HTTPException):
        return 503, {"error": errors[503]}


def _wechat_root(cfg: dict) -> Path | None:
    raw = str(cfg.get("wechatCoreRoot") or os.environ.get("DAFEIYU_WECHAT_CORE_ROOT") or "").strip()
    candidates = []
    if raw:
        candidates.append(Path(raw).expanduser())
    candidates.append(Path.home() / "dafeiyu-wechat" / "core")
    candidates.append(HERE.parent.parent / "wechat-pi" / "core")
    for c in candidates:
        if c.is_dir():
            return c
    return None


def _http_get_json(url: str, timeout: float = 3.5):
    req = urllib.request.Request(url, method="GET")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def _build_memory_status(cfg: dict) -> dict:
    wroot = _wechat_root(cfg)
    mem_dir = (wroot / "data" / "memory") if wroot else None
    groups = 0
    entries = 0
    total_bytes = 0
    journal_state = "unknown"
    if mem_dir and mem_dir.is_dir():
        journal_state = "healthy"
        chats_dir = mem_dir / "chats"
        scan_root = chats_dir if chats_dir.is_dir() else mem_dir
        try:
            for child in scan_root.iterdir():
                if child.is_dir():
                    groups += 1
                    for f in child.rglob("*"):
                        if f.is_file():
                            total_bytes += f.stat().st_size
                            if f.suffix == ".jsonl":
                                # Count lines without retaining or outputting any text
                                with f.open("rb") as fp:
                                    entries += sum(1 for _ in fp)
                elif child.is_file():
                    total_bytes += child.stat().st_size
        except OSError:
            journal_state = "partial"
    return {
        "ok": True,
        "isolation": "per-group-v3",
        "groups": groups,
        "entries": entries,
        "dbSizeMb": round(total_bytes / (1024.0 * 1024.0), 2),
        "journalState": journal_state,
    }


MEMORY_SCAN_LIMIT = 2048


def _memory_store_metadata(root, parts, scope_pattern, *, state_backups):
    """Inventory scoped files without opening them or following symbolic links."""
    summary = {"state": "unavailable", "scopes": None, "bytes": None, "stateBackups": None}
    if root is None:
        return summary
    try:
        store = root
        if not stat.S_ISDIR(store.lstat().st_mode):
            return summary
        for part in parts:
            store /= part
            if not stat.S_ISDIR(store.lstat().st_mode):
                return summary
    except FileNotFoundError:
        if store == root:
            return summary
        store = None
    except OSError:
        return summary

    summary.update(state="empty", scopes=0, bytes=0, stateBackups=0 if state_backups else None)
    if store is None:
        return summary
    directories = [(store, 0)]
    checked = 0
    while directories:
        folder, depth = directories.pop()
        try:
            with os.scandir(folder) as entries:
                for entry in entries:
                    checked += 1
                    if checked > MEMORY_SCAN_LIMIT:
                        summary["state"] = "partial"
                        return summary
                    if depth == 0 and scope_pattern.fullmatch(entry.name) is None:
                        continue
                    metadata = entry.stat(follow_symlinks=False)
                    if stat.S_ISDIR(metadata.st_mode) and depth < 2:
                        directories.append((Path(entry.path), depth + 1))
                        if depth == 0:
                            summary["scopes"] += 1
                    elif depth and stat.S_ISREG(metadata.st_mode):
                        summary["bytes"] += metadata.st_size
                        if state_backups and depth == 1 and entry.name == "state.json.bak":
                            summary["stateBackups"] += 1
                    else:
                        summary["state"] = "partial"
        except OSError:
            summary["state"] = "partial"
    if summary["state"] == "empty" and summary["scopes"]:
        summary["state"] = "available"
    return summary


def _build_memory_storage(cfg):
    qq_root = _qq_root(cfg)
    wx_root = _wechat_root(cfg)
    qq_data = qq_root / "data-qq" if qq_root is not None else None
    wx_data = wx_root / "data" if wx_root is not None else None
    return {"ok": True, "stores": {
        "qqReliable": _memory_store_metadata(qq_data, ("QQMemoryReliable",),
                                            re.compile(r"scope-[0-9a-f]{16}"), state_backups=True),
        "wechatSwift": _memory_store_metadata(qq_data, ("wechat-group-memory",),
                                             re.compile(r"[0-9a-f]{64}"), state_backups=True),
        "wechatCompat": _memory_store_metadata(wx_data, ("memory", "chats"),
                                              re.compile(r"[0-9a-f]{16}"), state_backups=False),
    }}


def _build_stickers_status(cfg: dict) -> dict:
    wroot = _wechat_root(cfg)
    qroot = _qq_root(cfg)
    wx_gifs = []
    if wroot:
        sdir = wroot / "data" / "stickers"
        if sdir.is_dir():
            for f in sorted(sdir.rglob("*")):
                if f.is_file() and f.suffix.lower() in (".gif", ".webp", ".png", ".jpg"):
                    wx_gifs.append(f.name)
    qq_count = 0
    qq_whalegirl = 0
    if qroot:
        qman = qroot / "dafeiyu-linux" / "Resources" / "QQStickers" / "manifest.json"
        if qman.is_file():
            try:
                items = json.loads(qman.read_text(encoding="utf-8"))
                if isinstance(items, list):
                    qq_count = len(items)
                    qq_whalegirl = sum(1 for x in items if str(x.get("id") or "").startswith("whalegirl-"))
            except (OSError, ValueError):
                pass
    return {
        "ok": True,
        "activePack": "whalegirl_batch1",
        "count": len(wx_gifs) if wx_gifs else qq_count,
        "wechatGifCount": len(wx_gifs),
        "qqStickerCount": qq_count,
        "qqWhalegirlCount": qq_whalegirl,
        "rotation": "anti-repeat-round-robin",
        "cooldownSec": 180,
        "sampleNames": wx_gifs[:6],
    }


def _proactive_path(cfg: dict) -> Path | None:
    explicit = cfg.get("qqDecisionConfigFile") or os.environ.get("DAFEIYU_DECISION_CONFIG_FILE")
    if explicit:
        return Path(str(explicit)).expanduser()
    root = _qq_root(cfg)
    return root / "data-qq" / "qq-decision-config.json" if root else None


def _read_proactive_document(path: Path | None) -> tuple[dict, str]:
    if path is None or not path.is_file():
        return {}, ""
    with path.open("rb") as source:
        raw = source.read(16_385)
    if len(raw) > 16_384:
        raise ValueError("QQ 决策策略文件超过 16 KiB")
    data = json.loads(raw.decode("utf-8"))
    if not isinstance(data, dict):
        raise ValueError("策略文件格式无效")
    groups = {key: row for key, row in data.items()
              if PROACTIVE_GROUP.fullmatch(key) and isinstance(row, dict)}
    # The Swift control endpoint reports this revision of the exact bytes it
    # loaded. A retained last-good policy must never confirm a different file.
    revision = 0xcbf29ce484222325
    for byte in raw:
        revision = ((revision ^ byte) * 0x100000001b3) & 0xffffffffffffffff
    return groups, f"{revision:016x}"


# --- WeChat authoritative config helpers ---------------------------------
# The WeChat core's authoritative runtime config is <wechatRoot>/config.json.
# The bridge (integrations/wechat-pi/transport/auto_reply_allowlist.py) reads
# this same file. We never touch data/settings.json (the shadow settings file)
# and never expand the proactiveJudge allowlist here.
_WECHAT_CONFIG_MAX_BYTES = 1024 * 1024


def _wechat_config_path(cfg: dict) -> Path | None:
    root = _wechat_root(cfg)
    if root is None:
        return None
    path = root / "config.json"
    return path if path.is_file() else None


def _read_wechat_config(path: Path | None) -> dict:
    if path is None or not path.is_file():
        raise FileNotFoundError
    if path.stat().st_size > _WECHAT_CONFIG_MAX_BYTES:
        raise ValueError("微信核心配置超过 1 MiB")
    with path.open("rb") as source:
        raw = source.read(_WECHAT_CONFIG_MAX_BYTES + 1)
    if len(raw) > _WECHAT_CONFIG_MAX_BYTES:
        raise ValueError("微信核心配置超过 1 MiB")
    data = json.loads(raw.decode("utf-8-sig"))
    if not isinstance(data, dict):
        raise ValueError("微信核心配置格式无效")
    return data


def _wechat_proactive_pj(document: dict) -> dict | None:
    """Return the proactiveJudge object only when it is structurally usable."""
    if not isinstance(document, dict):
        return None
    group = document.get("group")
    if not isinstance(group, dict):
        return None
    pj = group.get("proactiveJudge")
    if not isinstance(pj, dict):
        return None
    return pj


def _wechat_enabled_groups(pj: dict | None) -> list[str]:
    """Return exact group names only when the judge is enabled and the list is valid."""
    if not isinstance(pj, dict) or pj.get("enabled") is not True:
        return []
    groups = pj.get("groups")
    if not isinstance(groups, list) or any(
            not isinstance(name, str) or not name.strip() for name in groups):
        return []
    return list(groups)


def _wechat_cooldown_map(pj: dict | None, enabled_groups: list[str]) -> dict:
    """Expose only explicitly configured cooldowns for currently enabled groups."""
    if not isinstance(pj, dict):
        return {}
    stored = pj.get(COOLDOWN_MAP_KEY)
    if not isinstance(stored, dict):
        return {}
    allowed = set(enabled_groups)
    result = {}
    for name, seconds in stored.items():
        if name in allowed and type(seconds) is int:
            result[name] = seconds
    return result


def _wechat_paused_groups(pj: dict | None) -> set[str] | None:
    raw = pj.get("pausedGroups", []) if isinstance(pj, dict) else None
    if not isinstance(raw, list) or any(not isinstance(name, str) or not name.strip()
                                        for name in raw):
        return None
    return set(raw)


def _wechat_activity(document: dict) -> tuple[str, dict, list[str], list[str]] | None:
    pj = _wechat_proactive_pj(document)
    if pj is None or type(pj.get("enabled")) is not bool:
        return None
    if not pj["enabled"]:
        return "disabled", {}, [], []
    raw_groups = pj.get("groups")
    groups = _wechat_enabled_groups(pj)
    paused = _wechat_paused_groups(pj)
    cooldowns = pj.get(COOLDOWN_MAP_KEY, {})
    if (not isinstance(raw_groups, list) or (raw_groups and not groups)
            or paused is None or not isinstance(cooldowns, dict)):
        return None
    if any(not isinstance(name, str) or not name.strip() or not _validate_cooldown(seconds)[0]
           for name, seconds in cooldowns.items()):
        return None
    groups = list(dict.fromkeys(groups))
    return "available", _wechat_cooldown_map(pj, groups), groups, [name for name in groups if name not in paused]


def _read_wechat_activity(cfg: dict) -> tuple[str, dict, list[str], list[str]] | None:
    try:
        return _wechat_activity(_read_wechat_config(_wechat_config_path(cfg)))
    except (OSError, ValueError):
        return None


def _wechat_proactive_runtime(wechat_adapter, saved) -> dict:
    unavailable = {"verification": "unavailable", "matchedGroups": []}
    if wechat_adapter is None or saved is None:
        return unavailable
    try:
        runtime = _wechat_activity(wechat_adapter._get("/api/settings"))
    except Exception:
        return unavailable
    if runtime is None:
        return unavailable
    saved_state, saved_cooldowns, saved_groups, saved_enabled = saved
    running_state, running_cooldowns, running_groups, running_enabled = runtime
    running_group_set = set(running_groups)
    saved_enabled, running_enabled = set(saved_enabled), set(running_enabled)
    default = PUBLIC_QUESTION_COOLDOWN_DEFAULT
    matched = [name for name in saved_groups
               if name in running_group_set and (name in saved_enabled) == (name in running_enabled)
               and saved_cooldowns.get(name, default) == running_cooldowns.get(name, default)]
    complete = (saved_state == running_state and len(matched) == len(saved_groups)
                and running_group_set == set(saved_groups))
    return {"verification": "matched" if complete else "pending", "matchedGroups": matched}


def _check_previous_proactive_values(body: dict, current: dict) -> tuple[int, dict] | None:
    if "expected" not in body:  # Existing clients can still send their original incremental requests.
        return None
    expected = body["expected"]
    if not isinstance(expected, dict) or set(expected) != set(current):
        return 400, {"error": "原值须与本次修改项一一对应"}
    for key, before in expected.items():
        if key == "threshold":
            valid = type(before) in (int, float) and 0 <= before <= 1
        elif key == ORDINARY_REPLY_KEY:
            valid = type(before) is bool
        else:
            valid = _validate_cooldown(before)[0]
        if not valid:
            return 400, {"error": "本次提交的设置原值无效"}
    if expected != current:
        return 409, {"error": "这项设置已被修改。请核对最新值后再保存。", "conflict": "stale_edit"}
    return None


def _update_wechat_activity(cfg: dict, body: dict, wechat_adapter) -> tuple[int, dict]:
    group = body.get("group")
    if not isinstance(group, str) or not group.strip():
        return 400, {"error": "请提供有效的微信群名"}
    patch = {}
    if COOLDOWN_KEY in body:
        value = body[COOLDOWN_KEY]
        valid, message = _validate_cooldown(value)
        if not valid:
            return 400, {"error": message}
        patch[COOLDOWN_MAP_KEY] = {group: value}
    if ORDINARY_REPLY_KEY in body:
        value = body[ORDINARY_REPLY_KEY]
        if type(value) is not bool:
            return 400, {"error": "微信群接话开关须为布尔值"}
        patch["proactiveGroupEnabled"] = {group: value}
    if not patch:
        return 400, {"error": "请提供微信群接话设置"}

    path = _wechat_config_path(cfg)
    if path is None:
        return 404, {"error": "微信核心配置文件不可用"}
    if wechat_adapter is None:
        return 503, {"error": "微信核心设置写入通道不可用"}
    try:
        with PROACTIVE_CONFIG_LOCK:
            saved = _wechat_proactive_pj(_read_wechat_config(path))
            if group not in _wechat_enabled_groups(saved):
                return 400, {"error": "该群不在微信主动辨别白名单内"}
            runtime = _wechat_proactive_pj(wechat_adapter._get("/api/settings"))
            if group not in _wechat_enabled_groups(runtime):
                return 409, {"error": "微信运行配置与桥白名单不一致"}

            if "expected" in body:
                for document in (saved, runtime):
                    before = {}
                    if COOLDOWN_KEY in body:
                        before[COOLDOWN_KEY] = _wechat_cooldown_map(document, [group]).get(
                            group, PUBLIC_QUESTION_COOLDOWN_DEFAULT)
                    if ORDINARY_REPLY_KEY in body:
                        paused = _wechat_paused_groups(document)
                        if paused is None:
                            return 409, {"error": "微信运行配置或桥配置回读不一致"}
                        before[ORDINARY_REPLY_KEY] = group not in paused
                    conflict = _check_previous_proactive_values(body, before)
                    if conflict is not None:
                        return conflict

            result = wechat_adapter._post("/api/settings", {"group": patch})
            if not isinstance(result, dict) or result.get("ok") is False:
                return 503, {"error": "微信核心设置接口未确认写入"}

            runtime = _wechat_proactive_pj(wechat_adapter._get("/api/settings"))
            saved = _wechat_proactive_pj(_read_wechat_config(path))
            runtime_groups = _wechat_enabled_groups(runtime)
            saved_groups = _wechat_enabled_groups(saved)
            if group not in runtime_groups or group not in saved_groups:
                return 409, {"error": "微信运行配置与桥白名单不一致"}
            observed = {}
            if COOLDOWN_KEY in body:
                seconds = body[COOLDOWN_KEY]
                if (_wechat_cooldown_map(runtime, runtime_groups).get(group) != seconds
                        or _wechat_cooldown_map(saved, saved_groups).get(group) != seconds):
                    return 409, {"error": "微信运行配置或桥配置回读不一致"}
                observed.update(observed=seconds, runtimeObserved=seconds)
            if ORDINARY_REPLY_KEY in body:
                enabled = body[ORDINARY_REPLY_KEY]
                for paused in (_wechat_paused_groups(runtime), _wechat_paused_groups(saved)):
                    if paused is None or (group not in paused) != enabled:
                        return 409, {"error": "微信运行配置或桥配置回读不一致"}
                observed["ordinaryReplyEnabled"] = enabled
        return 200, {"ok": True, "verification": "persisted", "platform": "wechat",
                     "group": group, **observed}
    except FileNotFoundError:
        return 404, {"error": "微信核心配置文件不可用"}
    except (OSError, ValueError, UnicodeDecodeError):
        return 503, {"error": "微信核心配置读取或写入失败"}
    except Exception:
        return 503, {"error": "微信核心设置接口不可用"}


def _validate_cooldown(value) -> tuple[bool, str]:
    """Strict validation: int (never bool) in [30, 1200]."""
    if type(value) is not int:
        return False, "公开提问自动聊天间隔须为 30 到 1200 的整数秒"
    if not PUBLIC_QUESTION_COOLDOWN_MIN <= value <= PUBLIC_QUESTION_COOLDOWN_MAX:
        return False, "公开提问自动聊天间隔须为 30 到 1200 的整数秒"
    return True, ""


def _ordinary_reply_state(row: dict) -> tuple[bool, str]:
    """Return effective ordinary-reply state and its compatibility source."""
    explicit = row.get(ORDINARY_REPLY_KEY)
    if type(explicit) is bool:
        return explicit, "explicit"
    legacy = (row.get("mode") == "enforce"
              and row.get("publicQuestionEnabled") is True
              and row.get("threshold") == 0.45)
    return legacy, "legacy" if legacy else "default"


def _proactive_funnel(root: Path | None) -> dict:
    now = time.time()
    counts = dict.fromkeys(PROACTIVE_EVENTS, 0)
    reasons = {}
    rejection_reasons = {}
    skipped_reasons = {}
    after_enqueue_skips = {}
    groups = {}
    readable = 0
    truncated = False
    first_event_at = None
    last_event_at = None
    seen_records = set()
    if root:
        for suffix in ("", ".1", ".2", ".3"):
            path = root / "run" / ("qq-bot.log" + suffix)
            try:
                with path.open("rb") as log:
                    offset = max(0, os.fstat(log.fileno()).st_size - 2_000_000)
                    log.seek(offset)
                    if offset:
                        truncated = True
                        log.readline()
                    data = log.read(2_000_001)
            except OSError:
                continue
            readable += 1
            truncated |= len(data) > 2_000_000
            for line in data[:2_000_000].splitlines():
                if b"[qq_proactive_funnel]" not in line:
                    continue
                match = PROACTIVE_LINE.search(line.decode("utf-8", "replace"))
                if not match:
                    continue
                stamp, scope, event, reason = match.groups()
                event_at = float(stamp)
                if not now - 86400 <= event_at <= now:
                    continue
                # The archive is written before rotating the source so a
                # failed rotation cannot lose its records.
                record = (stamp, scope, event, reason)
                if record in seen_records:
                    continue
                seen_records.add(record)
                if first_event_at is None or event_at < first_event_at:
                    first_event_at = event_at
                if last_event_at is None or event_at > last_event_at:
                    last_event_at = event_at
                counts[event] += 1
                scope_counts = groups.setdefault(scope, dict.fromkeys(PROACTIVE_EVENTS, 0))
                scope_counts[event] += 1
                if event == "score_after_reject":
                    reasons[reason] = reasons.get(reason, 0) + 1
                elif event == "score_rejected":
                    rejection_reasons[reason] = rejection_reasons.get(reason, 0) + 1
                elif event == "score_skipped":
                    skipped_reasons[reason] = skipped_reasons.get(reason, 0) + 1
                elif event == "after_enqueue_skip":
                    after_enqueue_skips[reason] = after_enqueue_skips.get(reason, 0) + 1
    return {"windowSeconds": 86400, "firstEventAt": first_event_at, "lastEventAt": last_event_at,
            "counts": counts, "scoreAfterRejectReasons": reasons,
            "scoreRejectedReasons": rejection_reasons,
            "scoreSkippedReasons": skipped_reasons,
            "afterEnqueueSkipReasons": after_enqueue_skips,
            "groups": groups, "readableLogs": readable, "truncated": truncated}


def _update_proactive_config(cfg: dict, body: dict,
                             wechat_adapter=None) -> tuple[int, dict]:
    """Handle POST /api/proactive/config for both QQ legacy and incremental saves.

    Incremental (platform='qq'): group + publicQuestionCooldownSeconds only;
    threshold/freshnessSeconds/dailyBudget/other groups are untouched.
    Legacy (platform absent/empty): threshold + freshnessSeconds as before, with
    an optional publicQuestionCooldownSeconds set on the same row.
    """
    if not isinstance(body, dict):
        return 400, {"error": "请求体格式无效"}
    platform = body.get("platform")
    platform = str(platform).strip().lower() if isinstance(platform, str) else ""
    if platform == "wechat":
        return _update_wechat_activity(cfg, body, wechat_adapter)
    if platform not in ("", "qq"):
        return 400, {"error": "不支持的平台"}

    path = _proactive_path(cfg)
    group = body.get("group")
    cooldown_given = COOLDOWN_KEY in body
    cooldown = body.get(COOLDOWN_KEY, None)
    if cooldown_given:
        valid, message = _validate_cooldown(cooldown)
        if not valid:
            return 400, {"error": message}
    if not isinstance(group, str) or not PROACTIVE_GROUP.fullmatch(group):
        return 400, {"error": "请选择有效的 QQ 群"}
    ordinary_given = ORDINARY_REPLY_KEY in body
    ordinary_enabled = body.get(ORDINARY_REPLY_KEY)
    threshold_given = "threshold" in body
    threshold = body.get("threshold")
    if platform == "qq":
        # Incremental save: each supplied field is written independently; all
        # existing per-group limits and unrelated fields remain untouched.
        if not cooldown_given and not ordinary_given and not threshold_given:
            return 400, {"error": "请提供 QQ 普通聊天设置"}
        if ordinary_given and type(ordinary_enabled) is not bool:
            return 400, {"error": "普通聊天开关须为布尔值"}
        if threshold_given and (type(threshold) not in (int, float)
                                or isinstance(threshold, bool)
                                or not ORDINARY_THRESHOLD_MIN <= threshold <= ORDINARY_THRESHOLD_MAX):
            return 400, {"error": "QQ 普通聊天阈值须在 0.30 到 0.95 之间"}
    else:
        # Legacy save keeps its original validation contract.
        threshold = body.get("threshold")
        freshness = body.get("freshnessSeconds")
        if type(threshold) not in (int, float) or not 0.30 <= threshold <= 0.95:
            return 400, {"error": "QQ 主动评分阈值须在 0.30 到 0.95 之间"}
        if type(freshness) is not int or not 1 <= freshness <= 3:
            return 400, {"error": "新鲜度窗口须为 1 到 3 秒的整数"}
    if path is None:
        return 503, {"error": "QQ 决策策略文件不可用"}
    try:
        with PROACTIVE_CONFIG_LOCK:
            groups, _ = _read_proactive_document(path)
            if group not in groups:
                return 404, {"error": "该群没有已登记的主动发言策略"}
            if platform == "qq":
                current = groups[group]
                before = {}
                if cooldown_given:
                    before[COOLDOWN_KEY] = current.get(COOLDOWN_KEY, PUBLIC_QUESTION_COOLDOWN_DEFAULT)
                if ordinary_given:
                    before[ORDINARY_REPLY_KEY] = _ordinary_reply_state(current)[0]
                if threshold_given:
                    configured = current.get("threshold")
                    before["threshold"] = configured if type(configured) in (int, float) else 0.70
                conflict = _check_previous_proactive_values(body, before)
                if conflict is not None:
                    return conflict
                # Write only supplied keys; never replace the row or mutate the
                # legacy public-question switch when ordinary chat is toggled.
                if cooldown_given:
                    groups[group][COOLDOWN_KEY] = cooldown
                if ordinary_given:
                    groups[group][ORDINARY_REPLY_KEY] = ordinary_enabled
                if threshold_given:
                    groups[group]["threshold"] = threshold
            else:
                groups[group].update(threshold=threshold, freshnessSeconds=freshness)
                if cooldown_given:
                    groups[group][COOLDOWN_KEY] = cooldown
            with tempfile.NamedTemporaryFile(dir=path.parent, encoding="utf-8", mode="w",
                                             prefix=".qq-decision-", delete=False) as output:
                temporary = Path(output.name)
                try:
                    content = json.dumps(groups, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
                    if len(content.encode("utf-8")) > 16_384:
                        return 400, {"error": "QQ 决策策略文件超过 16 KiB"}
                    output.write(content)
                    output.flush()
                    os.fsync(output.fileno())
                    output.close()
                    os.replace(temporary, path)
                finally:
                    temporary.unlink(missing_ok=True)
            observed_groups, _ = _read_proactive_document(path)
            observed = observed_groups.get(group)
        if observed != groups[group]:
            return 409, {"error": "QQ 决策策略回读不一致"}
        return 200, {"ok": True, "verification": "persisted", "group": group, "observed": observed}
    except (OSError, ValueError):
        return 503, {"error": "QQ 决策策略读取或写入失败"}


def _qq_proactive_runtime(qq_adapter, revision: str) -> dict:
    unavailable = {"verification": "unavailable", "running": None, "groups": {}}
    if qq_adapter is None or not revision:
        return unavailable
    try:
        state = qq_adapter.group_reply_panel()
    except Exception:
        return unavailable
    policy = state.get("groupReplyPolicy") if isinstance(state, dict) else None
    if not isinstance(policy, dict) or not isinstance(policy.get("groups"), dict):
        return unavailable
    if policy.get("revision") != revision:
        return {**unavailable, "verification": "pending"}
    groups = {}
    for group, row in policy["groups"].items():
        if (not isinstance(group, str) or not PROACTIVE_GROUP.fullmatch(group)
                or not isinstance(row, dict) or type(row.get("ordinary")) is not bool
                or type(row.get("publicQuestion")) is not bool):
            continue
        threshold = row.get("threshold")
        if type(threshold) not in (int, float) or not 0 <= threshold <= 1:
            continue
        groups[group] = {"ordinary": row["ordinary"], "publicQuestion": row["publicQuestion"],
                         "threshold": threshold}
    return {"verification": "applied", "running": state.get("running") is True
            and state.get("connected") is True, "groups": groups}


def _build_proactive_config(cfg: dict, qq_adapter=None, wechat_adapter=None) -> dict:
    # The file remains authoritative for what was saved; the core view only verifies it.
    wx_saved = _read_wechat_activity(cfg)
    wx_state, wx_cooldowns, wx_groups, wx_enabled_groups = wx_saved or ("unreadable", {}, [], [])
    wx_enabled = bool(wx_enabled_groups)
    qq_cooldowns = {}
    qq_ordinary = {}
    qq_revision = ""
    try:
        groups, qq_revision = _read_proactive_document(_proactive_path(cfg))
        for name, row in groups.items():
            if type(row.get(COOLDOWN_KEY)) is int:
                qq_cooldowns[name] = row[COOLDOWN_KEY]
            enabled, source = _ordinary_reply_state(row)
            qq_ordinary[name] = {"enabled": enabled, "source": source,
                                 "explicit": type(row.get(ORDINARY_REPLY_KEY)) is bool,
                                 "threshold": row.get("threshold")}
        config_state = "available" if groups else "pending"
    except (OSError, ValueError):
        groups = {}
        config_state = "unreadable"
    return {
        "ok": True,
        "judgeMode": "QQ:enforce-v2 / WeChat:" + ("enabled" if wx_enabled else "gate-off"),
        "wechatJudgeEnabled": wx_enabled,
        "wechatAllowlistCount": len(wx_enabled_groups),
        "wechatAuthorizedGroups": wx_groups,
        "wechatEnabledGroups": wx_enabled_groups,
        "wechatConfigState": wx_state,
        "wechatRuntime": _wechat_proactive_runtime(wechat_adapter, wx_saved),
        "qqConfigState": config_state,
        "qqGroups": groups,
        "qqRuntime": _qq_proactive_runtime(qq_adapter, qq_revision),
        "qqFunnel": _proactive_funnel(_qq_root(cfg)),
        "publicQuestionCooldownDefault": PUBLIC_QUESTION_COOLDOWN_DEFAULT,
        COOLDOWN_MAP_KEY: {
            "qq": qq_cooldowns,
            "wechat": wx_cooldowns,
        },
        "qqOrdinary": qq_ordinary,
    }


def _percentile(values: list[float], p: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    idx = max(0, min(len(ordered) - 1, int(round((len(ordered) - 1) * p))))
    return round(ordered[idx], 1)


DELIVERY_WINDOW_SECONDS = 86400
BRIDGE_HEALTH_MAX_BYTES = 1024 * 1024


def _qq_delivery_summary(adapter) -> dict:
    keys = {"queueDepth": "queued", "confirmed": "confirmed", "unknown": "uncertain"}
    try:
        controller = adapter._controller_instance()
        panel = controller.state() if controller is not None else None
    except Exception:
        panel = None
    panel = panel if isinstance(panel, dict) else {}
    counters = {}
    for key, field in keys.items():
        value = panel.get(field)
        counters[key] = value if type(value) is int and value >= 0 else None
    values = list(counters.values())
    status = "unavailable" if all(n is None for n in values) else (
        "partial" if None in values else "available")
    return {"status": status, **counters, "pending": None}


def _wechat_delivery_summary(path: Path, now: float) -> dict:
    counts = dict.fromkeys(("confirmed", "unknown", "submitting"), 0)
    try:
        with path.open("rb") as stream:
            data = stream.read(BRIDGE_HEALTH_MAX_BYTES + 1)
        if len(data) > BRIDGE_HEALTH_MAX_BYTES:
            raise ValueError("oversized bridge snapshot")
        snapshot = json.loads(data)
        rows = snapshot.get("outbound_traces") if isinstance(snapshot, dict) else None
        if not isinstance(rows, dict):
            raise ValueError("missing bridge ledger")
    except (OSError, ValueError, RecursionError):
        return {"status": "unavailable", "queueDepth": None, "confirmed": None,
                "unknown": None, "pending": None}

    partial = False
    for entry in rows.values():
        if not isinstance(entry, dict):
            partial = True
            continue
        stamp, state = entry.get("time"), entry.get("status")
        if type(stamp) is not int or state not in ("confirmed", "unknown", "submitting"):
            partial = True
            continue
        if now - DELIVERY_WINDOW_SECONDS <= stamp <= now:
            counts[state] += 1
    return {"status": "partial" if partial else "available",
            "queueDepth": None, "confirmed": counts["confirmed"],
            "unknown": counts["unknown"], "pending": counts["submitting"]}


def _build_delivery_summary(cfg, qq_adapter) -> dict:
    health = cfg.get("wechatBridgeHealthPath") or (
        Path.home() / ".local/state/dafeiyu-wechat/bridge-health.json")
    return {
        "qq": _qq_delivery_summary(qq_adapter),
        "wechat": _wechat_delivery_summary(Path(health).expanduser(), time.time()),
        "wechatWindowSeconds": DELIVERY_WINDOW_SECONDS,
    }


TRACE_TAIL_BYTES = 64 * 1024
TRACE_SAMPLE_LIMIT = 200
TRACE_ID = re.compile(r"[0-9a-f]{64}|[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", re.I)
WECHAT_DELIVERY_LINE = re.compile(
    rb"^[0-9 :-]{19} delivery_timing trace='[0-9a-f-]{36,64}' kind='(?:group|private)' "
    rb"[^\r\n]*\bqueue_to_send_result_ms=([0-9]{1,8}) [^\r\n]*\bconfirmed=True$"
)


def _trace_tail(path: Path) -> list[bytes]:
    with path.open("rb") as stream:
        size = stream.seek(0, os.SEEK_END)
        offset = max(0, size - TRACE_TAIL_BYTES)
        stream.seek(offset)
        data = stream.read(min(size, TRACE_TAIL_BYTES))
    if offset:
        data = data.partition(b"\n")[2]
    return data.splitlines()


def _gateway_trace_samples(root: Path | None) -> list[tuple[float, bool, bool]]:
    if root is None:
        return []
    for rel in ("run/model-route.log", "secrets/model-hub/route.jsonl",
                "model-hub/route.jsonl", "logs/model-hub-route.jsonl"):
        try:
            lines = _trace_tail(root / rel)
        except FileNotFoundError:
            continue
        except OSError:
            return []
        samples = []
        for line in reversed(lines):
            try:
                record = json.loads(line)
            except (UnicodeError, ValueError, RecursionError):
                continue
            if not isinstance(record, dict):
                continue
            latency = record.get("latency_ms")
            if type(latency) not in (int, float) or not 0 <= latency <= 86_400_000:
                continue
            trace = record.get("trace_id")
            samples.append((latency, record.get("fallback") is True,
                            isinstance(trace, str) and TRACE_ID.fullmatch(trace) is not None))
            if len(samples) == TRACE_SAMPLE_LIMIT:
                break
        return samples
    return []


def _wechat_delivery_samples(cfg: dict) -> list[float]:
    log = Path(cfg.get("wechatBridgeLog") or (Path.home() / "dafeiyu-wechat/bridge.log")).expanduser()
    try:
        lines = _trace_tail(log)
    except OSError:
        return []
    samples = [int(found[1]) for line in lines if (found := WECHAT_DELIVERY_LINE.fullmatch(line))]
    return samples[-TRACE_SAMPLE_LIMIT:]


def _build_trace_summary(cfg: dict) -> dict:
    gateway = _gateway_trace_samples(_qq_root(cfg))
    latencies = [item[0] for item in gateway]
    delivery = _wechat_delivery_samples(cfg)
    return {
        "ok": True,
        "gatewayTrace": "已读取共享网关路由记录" if gateway else "暂无共享网关路由样本",
        "gatewayP50Ms": _percentile(latencies, 0.50),
        "gatewayP95Ms": _percentile(latencies, 0.95),
        "gatewayTracedCalls": sum(item[2] for item in gateway),
        "qqP50Ms": "--",
        "qqP95Ms": "--",
        "wechatP50Ms": _percentile(delivery, 0.50),
        "wechatP95Ms": _percentile(delivery, 0.95),
        "wechatConfirmedSamples": len(delivery),
        "contextP50Ms": "--",
        "promptP50Ms": "--",
        "recentCalls": len(gateway),
        "fallbackCount": sum(item[1] for item in gateway),
    }


def _profiles_path(cfg: dict) -> Path | None:
    qroot = _qq_root(cfg)
    if not qroot:
        return None
    for rel in ("secrets/model-hub/profiles.json", "model-hub/profiles.json"):
        p = qroot / rel
        if p.is_file():
            return p
    return None


def _load_gemini_profile(
    cfg: dict,
) -> tuple[Path | None, dict | None, dict | None, bool, bytes | None]:
    p = _profiles_path(cfg)
    if not p:
        return None, None, None, False, None
    has_bak = any(p.parent.glob("profiles.json.bak*"))
    try:
        snapshot = p.read_bytes()
        raw = json.loads(snapshot)
        items = raw if isinstance(raw, list) else (raw.get("profiles") or [])
        for it in items:
            if isinstance(it, dict) and it.get("id") == "gemini-web":
                return p, raw, it, has_bak, snapshot
    except (OSError, ValueError):
        pass
    return p, None, None, has_bak, None


def _active_gemini_profile_model(cfg: dict) -> tuple[str, bool]:
    _, _, profile, has_bak, _ = _load_gemini_profile(cfg)
    return str((profile or {}).get("model") or ""), has_bak


def _gemini_model_options(summary: dict) -> list[str]:
    options = [GEMINI_SAFE_DEFAULT]
    models = summary.get("available_models")
    if (summary.get("client_authenticated") is True and summary.get("models_complete") is True
            and isinstance(models, list)):
        options.extend(dict.fromkeys(
            model for model in models
            if isinstance(model, str) and model != GEMINI_SAFE_DEFAULT
            and GEMINI_MODEL_NAME.fullmatch(model)
        ))
    return options


def _build_gemini_health(cfg: dict) -> dict:
    gw_url = str(cfg.get("geminiGatewayUrl") or "http://127.0.0.1:8085").rstrip("/")
    _, _, profile, has_bak, _ = _load_gemini_profile(cfg)
    active_model = str((profile or {}).get("model") or "")
    profile_name = str((profile or {}).get("name") or "gemini-web")
    if profile and profile.get("enabled") is False:
        return {
            "ok": True,
            "reachable": False,
            "overall": "disabled",
            "alert_level": "ok",
            "alert_message": "",
            "client_authenticated": False,
            "account_status": "DISABLED",
            "available_models": [],
            "models_complete": False,
            "degraded": False,
            "active_model": active_model,
            "profile_name": profile_name,
            "allowed_models": [],
            "rollback_backup_present": has_bak,
        }
    try:
        summary = _http_get_json(gw_url + "/status/summary", timeout=3.5)
    except Exception as exc:
        return {
            "ok": False,
            "reachable": False,
            "overall": "unreachable",
            "alert_level": "critical",
            "alert_message": f"无法连接本地 Gemini 网关 ({type(exc).__name__})",
            "client_authenticated": False,
            "account_status": "UNREACHABLE",
            "available_models": [],
            "models_complete": False,
            "degraded": True,
            "active_model": active_model,
            "profile_name": profile_name,
            "allowed_models": [GEMINI_SAFE_DEFAULT],
            "rollback_backup_present": has_bak,
        }
    models = summary.get("available_models") or []
    degraded = (
        summary.get("overall") != "ok"
        or summary.get("client_authenticated") is not True
        or summary.get("models_complete") is not True
        or (len(models) <= 1 and (not models or models[0] == "gemini-auto"))
    )
    return {
        "ok": not degraded,
        "reachable": True,
        "overall": str(summary.get("overall") or "unknown"),
        "alert_level": str(summary.get("alert_level") or ("warning" if degraded else "ok")),
        "alert_message": str(summary.get("alert_message") or ""),
        "client_authenticated": summary.get("client_authenticated") is True,
        "account_status": str(summary.get("account_status") or "UNKNOWN"),
        "available_models": models,
        "models_complete": summary.get("models_complete") is True,
        "degraded": degraded,
        "egress": summary.get("egress") or {},
        "self_heal": summary.get("self_heal") or {},
        "active_model": active_model,
        "profile_name": profile_name,
        "allowed_models": _gemini_model_options(summary),
        "rollback_backup_present": has_bak,
    }


def _switch_gemini_model(cfg: dict, target_model: str) -> tuple[int, dict]:
    p, raw, found, _, snapshot = _load_gemini_profile(cfg)
    if not p:
        return 503, {"error": "未定位到 model-hub profiles.json"}
    if not found:
        return 404, {"error": "profiles.json 中未找到 gemini-web 配置项"}
    if ((isinstance(raw, dict) and raw.get("locked") is True)
            or found.get("locked") is True):
        return 409, {"error": "Gemini 配置目录锁定，拒绝修改"}
    if found.get("enabled") is False:
        return 409, {"error": "Gemini 配置已停用，拒绝修改"}
    if not GEMINI_MODEL_NAME.fullmatch(target_model):
        return 400, {"error": "Gemini 型号无效"}
    if target_model != GEMINI_SAFE_DEFAULT:
        gw_url = str(cfg.get("geminiGatewayUrl") or "http://127.0.0.1:8085").rstrip("/")
        try:
            routing = _http_get_json(gw_url + "/status/routing", timeout=1.5)
        except (OSError, ValueError):
            return 503, {"error": "当前无法核对 Gemini 可用型号"}
        if not isinstance(routing, dict) or target_model not in _gemini_model_options(routing):
            return 400, {"error": "Gemini 网关当前未提供此型号"}
    try:
        previous = p.read_bytes()
        if snapshot is None or previous != snapshot:
            return 409, {"error": "模型配置刚刚变更，请刷新后重试"}
        old_model = str(found.get("model") or "")
        bak = p.with_name(f"profiles.json.bak-console-{time.time_ns()}")
        shutil.copy2(p, bak)
        found["model"] = target_model
        with tempfile.NamedTemporaryFile(dir=p.parent, encoding="utf-8", mode="w",
                                         prefix=".gemini-model-", delete=False) as output:
            tmp = Path(output.name)
            try:
                output.write(json.dumps(raw, ensure_ascii=False, indent=2) + "\n")
                output.flush()
                os.fsync(output.fileno())
                output.close()
                if p.read_bytes() != previous:
                    return 409, {"error": "模型配置刚刚变更，请刷新后重试"}
                os.replace(tmp, p)
            finally:
                tmp.unlink(missing_ok=True)
        observed, _ = _active_gemini_profile_model(cfg)
        return 200, {
            "ok": True,
            "previous": old_model,
            "model": target_model,
            "verification": {"state": "confirmed" if observed == target_model else "mismatch", "observed": observed},
        }
    except Exception as exc:
        return 500, {"error": f"切换 Gemini 模型失败 ({type(exc).__name__})"}


def _send_house_static(handler, file_path: Path, ctype: str):
    try:
        data = file_path.read_bytes()
    except OSError:
        handler.fail(500, "家园资源读取失败")
        return
    handler.send_response(200)
    handler.send_header("Content-Type", ctype)
    handler.send_header("Content-Length", str(len(data)))
    if file_path.suffix.lower() in (".webp", ".png", ".jpg", ".jpeg", ".gif", ".mp4"):
        handler.send_header("Cache-Control", "private, max-age=3600")
    else:
        handler.send_header("Cache-Control", "no-cache")
    handler.send_header("Referrer-Policy", "no-referrer")
    handler.send_header("X-Content-Type-Options", "nosniff")
    handler.send_header("X-Frame-Options", "SAMEORIGIN")
    handler.send_header(
        "Content-Security-Policy",
        "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; media-src 'self'; connect-src 'self'; object-src 'none'; "
        "base-uri 'self'; form-action 'self'; frame-ancestors 'self'",
    )
    handler.end_headers()
    if handler.command != "HEAD":
        try:
            handler.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass


def _app_cfg(handler) -> dict:
    return getattr(handler.app, "content_cfg", None) or getattr(handler.app, "cfg", None) or {}


def _channel_inventory(console):
    """Return the same small, read-only channel contract for both adapters."""
    result = []
    for platform in ("qq", "wechat"):
        try:
            status, _ = getattr(console, platform).status()
        except Exception:
            # One unavailable transport must not hide the other channel.
            status = {}
        if not isinstance(status, dict):
            status = {}
        connected = status.get("connected")
        running = status.get("running")
        client_state = status.get("clientState")
        if client_state not in ("qr", "disabled", "offline", "ok", "unknown"):
            client_state = "ok" if connected is True else "offline" if connected is False else "unknown"
        client = status.get("client")
        result.append({
            "platform": platform,
            "state": client_state,
            "connected": connected if isinstance(connected, bool) else None,
            "running": running if isinstance(running, bool) else None,
            "client": client[:64] if isinstance(client, str) else "",
        })
    return {"channels": result}


def _proxy_fish_house_json(handler, rel_path: str):
    base = _fish_house_base_url(_app_cfg(handler))
    try:
        data = _http_get_json(base + rel_path, timeout=3.5)
        handler.json(200, data)
    except Exception as exc:
        handler.fail(502, f"家园状态服务暂不可达 ({type(exc).__name__})")


def _proxy_fish_house_sse(handler, path="/api/house/events"):
    base = _fish_house_base_url(_app_cfg(handler))
    started = False
    try:
        req = urllib.request.Request(base + path, method="GET")
        with urllib.request.urlopen(req, timeout=25) as upstream:
            handler.close_connection = True
            handler.send_response(200)
            handler.send_header("Content-Type", "text/event-stream; charset=utf-8")
            handler.send_header("Cache-Control", "no-store")
            handler.send_header("Connection", "close")
            handler.end_headers()
            started = True
            while True:
                line = upstream.readline(65537)
                if not line or len(line) > 65536:
                    break
                handler.wfile.write(line)
                handler.wfile.flush()
    except (OSError, http.client.HTTPException):
        if not started:
            handler.fail(502, "家园 SSE 通道暂不可达")


def _proxy_house_video(handler, path):
    base = _fish_house_base_url(_app_cfg(handler))
    request = urllib.request.Request(base + path, method=handler.command)
    byte_range = handler.headers.get("Range")
    if byte_range:
        if not re.fullmatch(r"bytes=(?:[0-9]{1,20}-[0-9]{0,20}|-[0-9]{1,20})", byte_range):
            return handler.fail(400, "只支持单段视频 Range")
        request.add_header("Range", byte_range)
    try:
        upstream = urllib.request.urlopen(request, timeout=8)
    except urllib.error.HTTPError as error:
        with error:
            if error.code == 416 and error.headers.get("Content-Range"):
                handler.send_response(416)
                handler.send_header("Content-Range", error.headers["Content-Range"])
                handler.send_header("Content-Length", "0")
                handler.end_headers()
                return
        return handler.fail(404 if error.code == 404 else 502, "家园视频暂不可读")
    except OSError:
        return handler.fail(502, "家园视频暂不可读")
    with upstream:
        size = upstream.headers.get("Content-Length", "")
        if upstream.status not in (200, 206) or not re.fullmatch(r"[0-9]{1,20}", size):
            return handler.fail(502, "家园视频响应无效")
        handler.send_response(upstream.status)
        handler.send_header("Content-Type", "video/mp4")
        handler.send_header("Content-Length", size)
        handler.send_header("Cache-Control", "private, max-age=3600")
        for name in ("Content-Range", "Accept-Ranges"):
            if upstream.headers.get(name):
                handler.send_header(name, upstream.headers[name])
        handler.end_headers()
        if handler.command != "HEAD":
            try:
                shutil.copyfileobj(upstream, handler.wfile, length=65536)
            except (OSError, http.client.HTTPException):
                handler.close_connection = True


def resolve(handler, method: str, path: str) -> bool:
    """Handle unified Fish House + extended Console endpoints. Returns True if handled."""
    cfg = _app_cfg(handler)
    if path == "/api/account/wechat" or path.startswith("/api/account/wechat/"):
        import wechat_account
        core = _wechat_root(cfg) if handler.app.fixture is None else None
        return wechat_account.resolve(handler, method, path, core.parent if core else None, cfg)

    persona = WECHAT_PERSONA_ROUTE.fullmatch(path)
    if persona and method in ("GET", "POST"):
        if not handler.need_auth():
            return True
        scope = persona.group(1)
        body = None
        if method == "POST":
            if not handler.headers.get("Origin") or not handler.origin_ok():
                handler.fail(403, "请从本控制台页面修改微信群人设")
                return True
            try:
                body = handler.read_json()
                if (not scope or set(body) != {"preset", "previous"}
                        or any(not isinstance(value, str) or len(value) > 12 for value in body.values())):
                    raise ValueError("人设更新请求无效")
            except ValueError:
                handler.fail(400, "人设更新请求无效")
                return True
        elif scope:
            handler.fail(404, "该群人设接口不存在")
            return True
        upstream = "/api/admin/wechat-persona" + ("/" + scope if scope else "")
        code, result = _wechat_admin(cfg, method, upstream, body, {
            400: "请选择列表中的群人设",
            404: "该微信群当前不可管理",
            409: "该群人设已被其他操作更新，请刷新核对",
            503: "微信群人设服务暂不可用，请刷新核对",
        })
        handler.json(code, result)
        return True

    memory = WECHAT_MEMORY_ROUTE.fullmatch(path)
    if memory and method in ("GET", "POST"):
        if not handler.need_auth():
            return True
        scope = memory.group(1)
        body = None
        if method == "POST":
            if not handler.headers.get("Origin") or not handler.origin_ok():
                handler.fail(403, "请从本控制台页面管理记忆")
                return True
            try:
                body = handler.read_json()
                if (not scope or not isinstance(body, dict) or not isinstance(body.get("id"), str)
                        or len(body["id"]) != 36):
                    raise ValueError("记忆修改请求无效")
                correction = (set(body) == {"id", "value"} and isinstance(body["value"], str)
                              and len(body["value"]) <= 500)
                pause = (set(body) == {"id", "paused", "previousPaused"}
                         and type(body["paused"]) is bool and type(body["previousPaused"]) is bool)
                if not (correction or pause):
                    raise ValueError("记忆修改请求无效")
            except ValueError:
                handler.fail(400, "记忆修改请求无效")
                return True
        code, result = _wechat_memory_admin(cfg, method, scope, body)
        handler.json(code, result)
        return True

    if method == "GET":
        if path == "/home":
            query = urllib.parse.urlsplit(handler.path).query
            handler.send_response(302)
            handler.send_header("Location", "/fish_house/home.html" + ("?" + query if query else ""))
            handler.send_header("Content-Length", "0")
            handler.end_headers()
            return True

        if path == "/fish_house/state.json":
            _proxy_fish_house_json(handler, "/fish_house/state.json")
            return True

        if path == "/api/status":
            _proxy_fish_house_json(handler, "/api/status")
            return True

        if path == "/api/channels":
            if not handler.need_auth():
                return True
            handler.json(200, _channel_inventory(handler.app.console))
            return True

        if path == "/api/house/events":
            _proxy_fish_house_sse(handler)
            return True

        if path == "/api/house/live-events":
            if handler.command == "HEAD":
                handler.fail(405, "事件流只支持 GET")
            else:
                _proxy_fish_house_sse(handler, path)
            return True

        if path == "/fish_house/live.theme.json":
            _proxy_fish_house_json(handler, path)
            return True

        if HOUSE_THEME_VIDEO.fullmatch(path):
            _proxy_house_video(handler, path)
            return True

        if path.startswith("/fish_house/"):
            web_dir = _fish_house_web_dir(cfg)
            if not web_dir:
                handler.fail(404, "未找到家园前端资源目录")
                return True
            rel = path[len("/fish_house/"):].lstrip("/") or "home.html"
            try:
                target = (web_dir / rel).resolve()
                target.relative_to(web_dir)
            except (ValueError, OSError):
                handler.fail(403, "路径无效")
                return True
            if not target.is_file():
                handler.fail(404, "家园资源不存在")
                return True
            ctype = MIME_MAP.get(target.suffix.lower(), "application/octet-stream")
            _send_house_static(handler, target, ctype)
            return True

        if path == "/api/memory/status":
            if not handler.need_auth():
                return True
            handler.json(200, _build_memory_status(cfg))
            return True

        if path == "/api/memory/storage":
            if not handler.need_auth():
                return True
            handler.json(200, _build_memory_storage(cfg))
            return True

        if path == "/api/stickers":
            if not handler.need_auth():
                return True
            handler.json(200, _build_stickers_status(cfg))
            return True

        if path == "/api/proactive/config":
            if not handler.need_auth():
                return True
            adapters = handler.app.console
            handler.json(200, _build_proactive_config(cfg, adapters.qq, adapters.wechat))
            return True

        if path == "/api/trace/summary":
            if not handler.need_auth():
                return True
            handler.json(200, _build_trace_summary(cfg))
            return True

        if path == "/api/delivery/summary":
            if not handler.need_auth():
                return True
            handler.json(200, _build_delivery_summary(cfg, handler.app.console.qq))
            return True

        if path == "/api/gemini/health":
            if not handler.need_auth():
                return True
            handler.json(200, _build_gemini_health(cfg))
            return True

    if method == "POST":
        if path == "/api/proactive/config":
            if not handler.need_auth():
                return True
            try:
                body = handler.read_json()
            except ValueError as exc:
                handler.fail(400, str(exc))
                return True
            code, payload = _update_proactive_config(
                cfg, body, getattr(getattr(handler.app, "console", None), "wechat", None))
            handler.json(code, payload)
            return True

        if path == "/api/diag":
            # Non-sensitive frontend rendering telemetry; always acknowledge cleanly
            try:
                _ = handler.read_json()
            except ValueError:
                pass
            handler.json(200, {"ok": True})
            return True

        if path == "/api/gemini/model":
            if not handler.need_auth():
                return True
            try:
                body = handler.read_json()
            except ValueError as e:
                handler.fail(400, str(e))
                return True
            code, payload = _switch_gemini_model(cfg, str(body.get("model") or "").strip())
            handler.json(code, payload)
            return True

    return False
