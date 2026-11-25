"""Fail-closed allowlist for explicit WeChat proactive candidates."""
from __future__ import annotations

import json
import os
from pathlib import Path
import threading
from dataclasses import dataclass

_MAX_POLICY_BYTES = 1024 * 1024
_POLICY_CACHE_LIMIT = 32
_policy_cache_lock = threading.Lock()
_policy_cache = {}


@dataclass(frozen=True)
class _RuntimeProactivePolicy:
    groups: frozenset[str] | None
    public_question_cooldowns: dict[str, int] | None


def _file_signature(info):
    return (info.st_dev, info.st_ino, info.st_size,
            getattr(info, "st_mtime_ns", int(info.st_mtime * 1_000_000_000)),
            getattr(info, "st_ctime_ns", int(info.st_ctime * 1_000_000_000)))


def _proactive_groups(document):
    if not isinstance(document, dict):
        return None
    group = document.get("group")
    if not isinstance(group, dict):
        return None
    proactive = group.get("proactiveJudge")
    if not isinstance(proactive, dict) or proactive.get("enabled") is not True:
        return None
    groups = proactive.get("groups")
    if not isinstance(groups, list) or any(not isinstance(item, str) or not item.strip()
                                           for item in groups):
        return None
    paused = proactive.get("pausedGroups", [])
    if not isinstance(paused, list) or any(not isinstance(name, str) or not name.strip()
                                           for name in paused):
        return None
    return frozenset(groups).difference(paused)


def _public_question_cooldowns(document):
    group = document.get("group") if isinstance(document, dict) else None
    proactive = group.get("proactiveJudge") if isinstance(group, dict) else None
    if not isinstance(proactive, dict):
        return None
    raw = proactive.get("publicQuestionCooldowns", {})
    if not isinstance(raw, dict):
        return None
    result = {}
    for title, seconds in raw.items():
        if (not isinstance(title, str) or not title.strip()
                or type(seconds) is not int or not 30 <= seconds <= 1200):
            return None
        result[title] = seconds
    return result


def _read_policy(path):
    """Read one stable bounded file version, retrying an atomic replacement once."""
    for _ in range(2):
        try:
            before = path.stat()
            signature = _file_signature(before)
            if not os.path.isfile(path) or before.st_size > _MAX_POLICY_BYTES:
                return signature, None
            with path.open("rb") as stream:
                opened_signature = _file_signature(os.fstat(stream.fileno()))
                if opened_signature != signature:
                    continue
                raw = stream.read(_MAX_POLICY_BYTES + 1)
                after_open_signature = _file_signature(os.fstat(stream.fileno()))
            current_signature = _file_signature(path.stat())
        except (OSError, ValueError):
            return None, None

        if (len(raw) > _MAX_POLICY_BYTES or opened_signature != after_open_signature
                or opened_signature != current_signature):
            if opened_signature != current_signature:
                continue
            return signature, None

        try:
            document = json.loads(raw.decode("utf-8-sig"))
        except (UnicodeDecodeError, ValueError, TypeError, RecursionError):
            return signature, None
        try:
            if _file_signature(path.stat()) != signature:
                continue
        except (OSError, ValueError):
            return None, None
        return signature, _RuntimeProactivePolicy(
            groups=_proactive_groups(document),
            public_question_cooldowns=_public_question_cooldowns(document))
    return None, None


def runtime_proactive_group_allowed(target, policy_path):
    """Read the live Node proactive policy and fail closed on every bad state.

    Cached permissions are tied to the path's device/inode/size/mtime/ctime.
    A missing, oversized, malformed, or unstable update cannot reuse an older
    allowlist. The lock also prevents concurrent threads from parsing duplicate
    versions of the bounded JSON file.
    """
    if not isinstance(target, str) or not target.strip() or not policy_path:
        return False
    try:
        path = Path(policy_path).expanduser().absolute()
    except (OSError, TypeError, ValueError):
        return False

    key = os.fspath(path)
    with _policy_cache_lock:
        try:
            signature = _file_signature(path.stat())
        except (OSError, ValueError):
            _policy_cache.pop(key, None)
            return False

        cached = _policy_cache.get(key)
        if cached is not None and cached[0] == signature:
            policy = cached[1]
        else:
            read_signature, policy = _read_policy(path)
            if read_signature is None:
                _policy_cache.pop(key, None)
                return False
            signature = read_signature
            if key not in _policy_cache and len(_policy_cache) >= _POLICY_CACHE_LIMIT:
                _policy_cache.clear()
            _policy_cache[key] = (signature, policy)

    return policy is not None and policy.groups is not None and target in policy.groups


def runtime_public_question_cooldown(target, policy_path):
    """Return the exact group's public-question cooldown or fail closed.

    A missing map/key means the stable default of 60 seconds. Malformed,
    oversized, missing, or unstable policy files return None.
    """
    if not isinstance(target, str) or not target.strip() or not policy_path:
        return None
    try:
        path = Path(policy_path).expanduser().absolute()
    except (OSError, TypeError, ValueError):
        return None

    key = os.fspath(path)
    with _policy_cache_lock:
        try:
            signature = _file_signature(path.stat())
        except (OSError, ValueError):
            _policy_cache.pop(key, None)
            return None

        cached = _policy_cache.get(key)
        if cached is not None and cached[0] == signature:
            policy = cached[1]
        else:
            read_signature, policy = _read_policy(path)
            if read_signature is None:
                _policy_cache.pop(key, None)
                return None
            signature = read_signature
            if key not in _policy_cache and len(_policy_cache) >= _POLICY_CACHE_LIMIT:
                _policy_cache.clear()
            _policy_cache[key] = (signature, policy)

    if (policy is None or policy.groups is None or target not in policy.groups
            or policy.public_question_cooldowns is None):
        return None
    return policy.public_question_cooldowns.get(target, 60)


def configured_auto_reply_groups(env=None):
    """Return (exact group-title allowlist, error).

    The production integration has a hard-coded legacy Apex group. This new
    variable is additive and never replaces that legacy CLI/flag path.
    Malformed explicit configuration fails closed.
    """
    env = os.environ if env is None else env
    raw = env.get("DAFEIYU_WECHAT_AUTO_REPLY_GROUPS")
    if raw is None:
        return [], None
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return [], "invalid_json"
    if not isinstance(parsed, list) or any(
            not isinstance(item, str) or not item.strip() for item in parsed):
        return [], "invalid_groups"
    return list(dict.fromkeys(item.strip() for item in parsed)), None


def auto_reply_group_allowed(target, configured, monitored):
    """A candidate requires exact title membership in both configured lists."""
    return (isinstance(target, str) and bool(target.strip())
            and target in configured and target in monitored)


def selected_auto_reply_groups(cli_groups, configured, legacy_group, monitored):
    """Validate explicit CLI groups plus JSON opt-ins against monitored chats.

    The legacy built-in group is accepted only when selected through the old
    CLI/flag path. New names must be present in the explicit JSON allowlist.
    """
    selected = list(dict.fromkeys([*cli_groups, *configured]))
    allowed = set(configured)
    if legacy_group:
        allowed.add(legacy_group)
    if any(not isinstance(name, str) or not name.strip()
           or name not in allowed or name not in monitored for name in selected):
        return [], "not_allowlisted_or_not_monitored"
    return selected, None


def configured_swift_proactive_groups(env=None):
    """Independent Swift proactive allowlist; absent/malformed means disabled."""
    env = os.environ if env is None else env
    raw = env.get("DAFEIYU_WECHAT_SWIFT_PROACTIVE_GROUPS")
    if raw is None:
        return []
    try:
        groups = json.loads(raw)
    except (TypeError, ValueError):
        return []
    if not isinstance(groups, list) or any(not isinstance(x, str) or not x.strip() for x in groups):
        return []
    return list(dict.fromkeys(x.strip() for x in groups))


def swift_proactive_candidate_allowed(payload, configured):
    """Only proven plain group candidates may bypass the legacy route."""
    settled = payload.get("group_auto_reply_public_question_settled") if isinstance(payload, dict) else None
    settled_cooldown = payload.get("group_auto_reply_public_question_cooldown_seconds") if isinstance(payload, dict) else None
    return bool(
        isinstance(payload, dict)
        and payload.get("group_auto_reply") is True
        and payload.get("at_me") is False
        and payload.get("chat_type") == "group"
        and payload.get("chat_id") in configured
        and payload.get("chat_title") == payload.get("chat_id")
        and bool(payload.get("account")) and bool(payload.get("sender_id")) and bool(payload.get("msg_id"))
        and payload.get("group_auto_reply_window_ok") is True
        and payload.get("group_auto_reply_superseded") is False
        and isinstance(payload.get("group_auto_reply_context"), list)
        and 1 <= len(payload["group_auto_reply_context"]) <= 8
        and not payload.get("quoted") and not payload.get("image_described")
        and not payload.get("rich_media") and not payload.get("multi_bubble")
        and not payload.get("photo_present")
        and (settled is None or type(settled) is bool)
        and (settled is not True or
             (type(settled_cooldown) is int and 30 <= settled_cooldown <= 1200))
    )
