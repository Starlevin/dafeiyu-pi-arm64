"""Read-only normalization of authoritative chat activity into house states.

Input contract: a mapping with ``time`` (Unix seconds), ``phase`` and ``reason``
from a structured bridge-health event, plus optional boolean ``success``,
``failed``, ``unknown`` and ``local_handoff`` flags. Never pass log text here.
The caller must supply only the current event for the selected QQ/WeChat source.

States are idle, received, thinking, replying, sent, and error. A core-side
local handoff means replying; only an explicit successful confirmation with a
recognized visible-send receipt means sent. Missing, malformed, or stale input
returns idle. This module does not read files or start a service.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


DEFAULT_MAX_AGE_SECONDS = 60
_VISIBLE_RECEIPTS = frozenset({"visible_outgoing", "confirmed"})


def normalize_activity(
    event: Mapping[str, Any] | None,
    *,
    now: float,
    max_age_seconds: float = DEFAULT_MAX_AGE_SECONDS,
) -> str:
    """Map one fresh structured source event to a house activity state.

    Timestamps must be Unix seconds. Negative age (future data), invalid age,
    unknown phases, and unsupported receipt labels fail closed to ``idle``.
    """
    if not isinstance(event, Mapping):
        return "idle"
    try:
        age = float(now) - float(event.get("time"))
        max_age = float(max_age_seconds)
    except (TypeError, ValueError, OverflowError):
        return "idle"
    if age < 0 or max_age < 0 or age > max_age:
        return "idle"

    phase = event.get("phase")
    reason = event.get("reason")
    if not isinstance(phase, str) or not isinstance(reason, str):
        return "idle"
    phase = phase.strip().lower()
    reason = reason.strip().lower()

    if event.get("failed") is True or event.get("unknown") is True or phase in {"error", "failed"}:
        return "error"
    if phase in {"received", "receive"}:
        return "received"
    if phase == "generation" and reason == "started":
        return "thinking"
    if phase == "generation" and reason == "ready":
        return "replying"
    # The bridge has already prepared a reply when its active-chat recheck passes.
    if phase == "submit" and reason in {"auto_reply_recheck_ok", "started"}:
        return "replying"
    if phase in {"handoff", "local_handoff"} and event.get("local_handoff") is True:
        return "replying"
    if phase == "confirmation":
        if event.get("success") is True and reason in _VISIBLE_RECEIPTS:
            return "sent"
        if event.get("success") is False or reason in {"send_exception", "failed", "error"}:
            return "error"
    return "idle"


def normalize_bridge_health(snapshot: Mapping[str, Any] | None, *, now: float,
                            max_age_seconds: float = DEFAULT_MAX_AGE_SECONDS) -> str:
    """Normalize the latest event in the bridge-health snapshot schema.

    Health stores latest event and latest success/failure/unknown separately.
    At equal timestamps a failure/unknown marker takes precedence; a success
    marker supplies the explicit success flag required for ``sent``.
    """
    if not isinstance(snapshot, Mapping):
        return "idle"
    candidates: list[tuple[float, int, Mapping[str, Any], str]] = []
    for field, rank in (("last_event", 3), ("last_success", 0),
                        ("last_failure", 1), ("last_unknown", 2)):
        event = snapshot.get(field)
        if not isinstance(event, Mapping):
            continue
        try:
            at = float(event.get("time"))
        except (TypeError, ValueError, OverflowError):
            continue
        candidates.append((at, rank, event, field))
    if not candidates:
        return "idle"
    _, _, event, field = max(candidates, key=lambda row: (row[0], row[1]))
    normalized = dict(event)
    if field != "last_event":
        return normalize_activity(
            {**normalized,
             "success": field == "last_success",
             "failed": field == "last_failure",
             "unknown": field == "last_unknown"},
            now=now,
            max_age_seconds=max_age_seconds,
        )

    # The bridge's event timestamp has one-second resolution. Use marker flags
    # only when the marker describes this exact latest event; a different event
    # in the same second (often a newly received message) must win.
    def same_marker(marker: Any) -> bool:
        return (isinstance(marker, Mapping)
                and all(marker.get(key) == event.get(key)
                        for key in ("time", "phase", "reason", "trace")))

    if same_marker(snapshot.get("last_success")):
        normalized["success"] = True
    if same_marker(snapshot.get("last_failure")):
        normalized["failed"] = True
    if same_marker(snapshot.get("last_unknown")):
        normalized["unknown"] = True
    return normalize_activity(normalized, now=now, max_age_seconds=max_age_seconds)
