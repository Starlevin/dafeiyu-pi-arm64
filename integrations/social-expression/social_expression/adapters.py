from __future__ import annotations

import hashlib
import math
from datetime import datetime, timezone
from typing import Any, Mapping, Optional, Sequence, Tuple

from .models import MemoryFact, UpstreamDecision
from .text import redact
from .version import A_TO_C_VERSION, B_TO_C_VERSION


class ContractError(ValueError):
    """A wire value that cannot be safely converted; callers must fail closed."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


_ACTIONS = {"SPEAK": "speak", "WAIT": "wait", "SKIP": "skip"}


def normalize_upstream_action(value: Any) -> str:
    """Map A's uppercase wire action to C's lowercase enum.

    Only SPEAK/WAIT/SKIP are accepted. Unknown or non-string values are a
    contract error, never a guessed action.
    """
    if not isinstance(value, str):
        raise ContractError("action_not_string")
    key = value.strip().upper()
    if key not in _ACTIONS:
        raise ContractError("unknown_action")
    return _ACTIONS[key]


def _number(value: Any, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ContractError(field + "_not_number")
    number = float(value)
    if not math.isfinite(number):
        raise ContractError(field + "_not_finite")
    return number


def parse_upstream(payload: Mapping[str, Any]) -> UpstreamDecision:
    """Strict A -> C parser. Raises ContractError; use from_session_a for fallback."""
    if not isinstance(payload, Mapping):
        raise ContractError("payload_not_mapping")
    version = payload.get("contract_version")
    if version is not None and version != A_TO_C_VERSION:
        raise ContractError("version_mismatch")
    action = normalize_upstream_action(payload.get("action"))

    addressed = payload.get("addressed", False)
    if not isinstance(addressed, bool):
        raise ContractError("addressed_not_bool")

    score = _number(payload.get("score", 0.0), "score")
    if not 0.0 <= score <= 1.0:
        raise ContractError("score_out_of_range")

    ambient_raw = payload.get("ambient_relevance", payload.get("ambientRelevance", 0.0))
    ambient = _number(ambient_raw, "ambient_relevance")
    if not 0.0 <= ambient <= 1.0:
        raise ContractError("ambient_relevance_out_of_range")

    ttl_raw = payload.get("ttl_seconds", payload.get("ttlSeconds", 120.0))
    ttl = _number(ttl_raw, "ttl_seconds")
    if not 0.0 <= ttl <= 7 * 86400.0:
        raise ContractError("ttl_seconds_out_of_range")

    reason = payload.get("reason", "")
    if not isinstance(reason, str):
        reason = str(reason)
    reason = reason[:160]

    return UpstreamDecision(
        action=action,
        score=score,
        reason=reason,
        addressed=addressed,
        ambient_relevance=ambient,
        ttl_seconds=ttl,
    )


def from_session_a(payload: Mapping[str, Any]) -> UpstreamDecision:
    """Fail-closed wrapper: malformed A payload becomes WAIT, never SPEAK."""
    try:
        return parse_upstream(payload)
    except (ContractError, TypeError, ValueError) as error:
        code = getattr(error, "reason", type(error).__name__)
        return UpstreamDecision(
            action="wait",
            score=0.0,
            reason="upstream_contract_error:" + str(code),
            addressed=False,
            ambient_relevance=0.0,
            ttl_seconds=0.0,
        )


def from_a_decision(
    result: Mapping[str, Any],
    *,
    scope: str,
    mention: str,
    invited: bool = False,
    topic_relevance: float = 0.0,
    reply_to_bot_verified: bool = False,
    age_seconds: float = 0.0,
) -> UpstreamDecision:
    """Convert one A decision plus its input evidence into C's wire contract.

    ``addressed`` is derived here, never copied from an untrusted A field:
    a bare reply target is not enough unless A's verified bot-message proof is
    present.  SPEAK decisions expire with A's TTL; expired payloads become WAIT.
    """
    try:
        if not isinstance(result, Mapping):
            raise ContractError("payload_not_mapping")
        if scope not in ("group", "private"):
            raise ContractError("invalid_scope")
        if mention not in ("none", "self", "reply_to_bot", "other"):
            raise ContractError("invalid_mention")
        if not isinstance(invited, bool) or not isinstance(reply_to_bot_verified, bool):
            raise ContractError("invalid_addressed_evidence")
        relevance = _number(topic_relevance, "topic_relevance")
        if not 0.0 <= relevance <= 1.0:
            raise ContractError("topic_relevance_out_of_range")
        age = _number(age_seconds, "age_seconds")
        if age < 0.0:
            raise ContractError("age_seconds_out_of_range")

        addressed = (
            scope == "private"
            or mention == "self"
            or invited
            or (mention == "reply_to_bot" and reply_to_bot_verified)
        )
        payload = dict(result)
        payload["addressed"] = addressed
        payload.setdefault("ambient_relevance", relevance)
        decision = parse_upstream(payload)
        if decision.action == "speak" and is_upstream_expired(decision, age):
            return UpstreamDecision(
                action="wait",
                score=0.0,
                reason="upstream_ttl_expired",
                addressed=False,
                ambient_relevance=decision.ambient_relevance,
                ttl_seconds=0.0,
            )
        return decision
    except (ContractError, TypeError, ValueError) as error:
        code = getattr(error, "reason", type(error).__name__)
        return UpstreamDecision(
            action="wait",
            score=0.0,
            reason="upstream_contract_error:" + str(code),
            addressed=False,
            ambient_relevance=0.0,
            ttl_seconds=0.0,
        )


def is_upstream_expired(decision: UpstreamDecision, age_seconds: float) -> bool:
    """TTL is measured from A's decision time; zero/negative TTL is expired."""
    if decision.action != "speak":
        return False
    age = max(0.0, float(age_seconds))
    return decision.ttl_seconds <= 0.0 or age > decision.ttl_seconds


def upstream_allows_send(decision: UpstreamDecision, age_seconds: float) -> bool:
    return decision.action == "speak" and not is_upstream_expired(decision, age_seconds)


def _as_mapping(value: Any, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ContractError(field + "_not_mapping")
    return value


def _required_string(value: Any, field: str, max_len: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ContractError(field + "_missing")
    text = value.strip()
    if len(text) > max_len:
        raise ContractError(field + "_too_long")
    return text


def _epoch(value: Any, field: str) -> float:
    if isinstance(value, bool):
        raise ContractError(field + "_not_time")
    if isinstance(value, (int, float)):
        number = float(value)
        if not math.isfinite(number):
            raise ContractError(field + "_not_time")
        return number
    if isinstance(value, str):
        raw = value.strip().replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(raw)
        except ValueError:
            raise ContractError(field + "_invalid_iso8601")
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp()
    raise ContractError(field + "_not_time")


_CITABLE_KINDS = ("preference", "task")


def qq_memory_fact(
    payload: Mapping[str, Any],
    *,
    current_scope_key: str,
    expected_subject: str,
    now_epoch: float,
) -> MemoryFact:
    """Convert one B envelope (QQMemoryItem + scope) into a bounded MemoryFact.

    The envelope must be:
      {"scope_key": "<hash of account+target>", "item": {QQMemoryItem fields}}
    Missing scope, a different scope, a different person, missing expiry,
    expired items, invalidated items, or empty text all fail closed.
    """
    envelope = _as_mapping(payload, "envelope")
    version = envelope.get("contract_version")
    if version is not None and version != B_TO_C_VERSION:
        raise ContractError("version_mismatch")
    scope_key = _required_string(envelope.get("scope_key"), "scope_key", 64)
    if scope_key != current_scope_key:
        raise ContractError("scope_mismatch")

    item = _as_mapping(envelope.get("item"), "item")
    subject = _required_string(item.get("subject"), "subject", 64)
    if subject != expected_subject:
        raise ContractError("wrong_subject")

    invalidated = item.get("invalidatedAt", item.get("invalidated_at"))
    if invalidated not in (None, ""):
        raise ContractError("invalidated")

    expires_raw = item.get("expiresAt", item.get("expires_at"))
    if expires_raw in (None, ""):
        raise ContractError("missing_expiry")
    expires_at = _epoch(expires_raw, "expiresAt")

    valid_for = expires_at - float(now_epoch)
    if valid_for <= 0:
        raise ContractError("expired")

    kind = _required_string(item.get("kind"), "kind", 32)
    text = _required_string(item.get("text"), "text", 1200)
    fact_text = redact(text)[:160]
    if not fact_text:
        raise ContractError("empty_fact")

    importance_raw = item.get("importance", 1)
    if isinstance(importance_raw, bool) or not isinstance(importance_raw, int):
        raise ContractError("importance_not_int")
    importance = max(1, min(5, importance_raw))

    source_ids = item.get("sourceIDs", item.get("source_ids", []))
    if source_ids is None:
        source_ids = []
    if not isinstance(source_ids, list):
        raise ContractError("source_ids_not_list")
    if any(not isinstance(source_id, str) or not source_id.strip() for source_id in source_ids):
        raise ContractError("source_id_invalid")

    citable = kind in _CITABLE_KINDS and bool(source_ids)
    subject_hash = hashlib.sha256(
        ("dafeiyu-social-style-c1|%s|%s" % (scope_key, subject)).encode("utf-8")
    ).hexdigest()[:16]

    return MemoryFact(
        subject_hash=subject_hash,
        fact=fact_text,
        confidence=round(0.5 + 0.1 * (importance - 1), 2),
        valid_for_seconds=round(valid_for, 3),
        citable=citable,
    )


def adapt_qq_memory_item(
    payload: Mapping[str, Any],
    *,
    current_scope_key: str,
    expected_subject: str,
    now_epoch: float,
) -> Optional[MemoryFact]:
    try:
        return qq_memory_fact(
            payload,
            current_scope_key=current_scope_key,
            expected_subject=expected_subject,
            now_epoch=now_epoch,
        )
    except (ContractError, TypeError, ValueError):
        return None


def qq_memory_reject_reason(
    payload: Mapping[str, Any],
    *,
    current_scope_key: str,
    expected_subject: str,
    now_epoch: float,
) -> Optional[str]:
    try:
        qq_memory_fact(
            payload,
            current_scope_key=current_scope_key,
            expected_subject=expected_subject,
            now_epoch=now_epoch,
        )
        return None
    except (ContractError, TypeError, ValueError) as error:
        return str(getattr(error, "reason", type(error).__name__))


def adapt_qq_memory_items(
    payloads: Sequence[Mapping[str, Any]],
    *,
    current_scope_key: str,
    expected_subject: str,
    now_epoch: float,
) -> Tuple[MemoryFact, ...]:
    facts = []
    for payload in payloads:
        fact = adapt_qq_memory_item(
            payload,
            current_scope_key=current_scope_key,
            expected_subject=expected_subject,
            now_epoch=now_epoch,
        )
        if fact is not None:
            facts.append(fact)
    return tuple(facts)


def memory_facts(
    payloads: Sequence[Mapping[str, Any]],
    *,
    current_scope_key: str,
    expected_subject: str,
    now_epoch: float,
    limit: int = 3,
) -> Tuple[MemoryFact, ...]:
    """Scope/subject-safe batch with a hard C-injection cap of three facts."""
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        return ()
    cap = min(limit, 3)
    facts = adapt_qq_memory_items(
        payloads,
        current_scope_key=current_scope_key,
        expected_subject=expected_subject,
        now_epoch=now_epoch,
    )
    return tuple(sorted(facts, key=lambda fact: (-fact.confidence, fact.subject_hash)))[:cap]
