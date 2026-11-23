"""Orchestration layer for the qq-qzone module (``QzoneService``).

Layering (docs/MODULE_CONTRACT.md section 0)::

    qzone_core (pure) -> qzone_store (IO) -> qzone_napcat (OneBot) -> this file

Every public method returns the route envelope::

    {"ok": True,  "data": {...}}
    {"ok": False, "error": {"code": "...", "message": "..."}}

``error.code`` is one of the frozen enum from contract section 8:
``owner_unauth`` / ``disabled`` / ``quiet_hours`` / ``daily_limit`` /
``min_interval`` / ``busy`` / ``uncertain`` / ``bad_request`` /
``upstream_error`` / ``not_found``.

Counting rules (contract section 6.0 — the only standard):

* **before** any write leaves the process: ``bump_usage(state, day, kind)``
  (``posts`` / ``likes`` / ``comments``) is persisted together with a
  ``record_write_intent`` entry = book first;
* settle: ``PUBLISHED`` -> nothing; ``REJECTED`` -> roll that ``+1`` back;
  ``UNCERTAIN`` -> nothing extra except ``bump_usage(..., "uncertain")``;
* invariant: full day == ``posts + uncertain >= max_posts_per_day``, and one
  UNCERTAIN consumes exactly one share (never double-counted).

Other guarantees owned here:

* no write action is ever retried, and ``UNCERTAIN`` is never re-sent;
* ``like`` / ``unlike`` / ``comment`` are gated behind their own switches and
  are simply never called when those switches are off;
* ``confirm_token`` is one-shot: nonces are recorded in the state file and a
  replay is refused;
* nothing here prints to stdout, logs a token, or returns an owner QQ number.

Real sibling-module signatures used (verified against qzone_core / qzone_store):

``day_bucket(state, day)`` / ``count_usage(state, day, kind)`` /
``bump_usage(state, day, kind, n=1) -> new state`` /
``allowance(state, day, cfg, now_ts=None) -> dict`` /
``evaluate_publish(*, now_ts, cfg, state, content, candidate_topics=None,
owner_authenticated, expected_token, provided_token, confirm, images=None)`` /
``evaluate_followup(*, now_ts, cfg, state, kind, owner_authenticated,
expected_token, provided_token, confirm, target_tid)`` /
``classify_publish_result(upstream_ok, tid, error)`` /
``check_publish_consistency(outcome, ugc_right, cfg)`` /
``append_snapshot(state, entry, keep=200) -> new state`` (entry keys: ts, tid,
day, outcome, chars, sig, has_images, ugc_right) /
``owner_confirm_token(owner_uin, day, nonce)`` /
``effective_ugc_right(cfg)`` / ``text_length(s)`` / ``topic_signature(s)`` /
``StateStore.load()`` (``fatal`` means "treat as exhausted") /
``StateStore.save(state)`` / ``StateStore.lock(owner, timeout)`` contextmanager /
``record_write_intent(state, day, kind, intent) -> new state`` /
``clear_write_intent(state, key) -> new state``.
"""

from __future__ import annotations

import importlib
import json
import logging
import os
import time
from typing import Any, Callable, Mapping, Sequence

try:  # the pure-function layer is a hard dependency of everything below
    import qzone_core
except ImportError:  # pragma: no cover - only when the sibling file is absent
    qzone_core = None  # type: ignore[assignment]

logger = logging.getLogger("qq_qzone.service")

# --------------------------------------------------------------------------
# Contract constants (contract sections 3, 4, 5, 6.0, 8)
# --------------------------------------------------------------------------

#: Error codes frozen in contract section 8.
ERROR_CODES = frozenset(
    {
        "owner_unauth",
        "disabled",
        "quiet_hours",
        "daily_limit",
        "min_interval",
        "busy",
        "uncertain",
        "bad_request",
        "upstream_error",
        "not_found",
    }
)

#: ``PublishDecision.reason`` -> route error code.
REASON_TO_CODE: dict[str, str] = {
    "ok": "bad_request",
    "disabled": "disabled",
    "quiet_hours": "quiet_hours",
    "daily_limit": "daily_limit",
    "min_interval": "min_interval",
    "owner_unauth": "owner_unauth",
    "not_confirmed": "owner_unauth",
    "bad_token": "owner_unauth",
    "not_logged_in": "owner_unauth",
    "repeat_topic": "bad_request",
    "text_invalid": "bad_request",
    "images_invalid": "bad_request",
    "too_many_images": "bad_request",
    "bad_target": "bad_request",
    "not_found": "not_found",
    "upstream_error": "upstream_error",
    "uncertain": "uncertain",
    "busy": "busy",
}

#: 好友动态只读刷新（契约 §10）的 reason -> route error code。
#: 保持少量稳定码，控制台与运维按码对号；不把上游原始报错透出去。
_READ_ERROR_TO_CODE: dict[str, str] = {
    "read_disabled": "disabled",
    "read_quiet": "quiet_hours",
    "read_interval": "min_interval",
    "read_daily_limit": "daily_limit",
    "owner_unauth": "owner_unauth",
    "read_rejected": "upstream_error",
    "read_upstream": "upstream_error",
    "read_unavailable": "upstream_error",
    "read_malformed": "upstream_error",
}

_READ_ERROR_MESSAGE: dict[str, str] = {
    "read_disabled": "未配置可用的本机 OneBot 地址，已按 fail closed 拒绝",
    "read_quiet": "00–07 点不刷新好友动态",
    "read_interval": "距上次刷新太近，请稍后再试",
    "read_daily_limit": "今日刷新次数已用完",
    "owner_unauth": "请先在控制台登录后再操作",
    "read_rejected": "上游拒绝了这次读取",
    "read_upstream": "上游返回错误",
    "read_unavailable": "本机 OneBot 暂时不可达",
    "read_malformed": "上游返回结构无法解析",
}

#: Fields an ``update_config`` patch may touch.  ``owner_uin`` is deliberately
#: absent: the identity anchor is not editable through the config route.
CONFIG_EDITABLE_FIELDS = frozenset(
    {
        "enable_like",
        "enable_comment",
        "visibility",
        "quiet_start",
        "quiet_end",
        "min_interval_minutes",
        "max_posts_per_day",
        "max_likes_per_day",
        "max_comments_per_day",
        "min_chars",
        "max_chars",
        "read_retry",
        "upstream_timeout_seconds",
    }
)

#: How long we are willing to block waiting for the cross-process lock.
LOCK_TIMEOUT_SECONDS = 5.0
#: Bounded attempts at re-reading the state when the lock is busy.
LOCK_ATTEMPTS = 2

DEFAULT_HISTORY_LIMIT = 20
MAX_HISTORY_LIMIT = 200
#: How many confirm nonces we remember (oldest dropped first).
MAX_NONCES = 200

#: Keys under which the used-nonce list may live (ours first).
_NONCE_KEYS = ("confirm_nonces", "used_confirm_nonces", "used_nonces", "consumed_tokens")


# --------------------------------------------------------------------------
# Sibling modules — imported lazily so a missing file is reported cleanly
# --------------------------------------------------------------------------


class ServiceUnavailable(Exception):
    """A sibling module this layer builds on is not importable."""

    def __init__(self, module: str, message: str) -> None:
        super().__init__(message)
        self.module = module
        self.message = message

    def to_envelope(self) -> dict[str, Any]:
        return _error(
            "upstream_error", f"qzone module unavailable: {self.message}"
        )


class ServiceError(Exception):
    """Internal failure already carrying a contract error code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code if code in ERROR_CODES else "upstream_error"
        self.message = message

    def to_envelope(self) -> dict[str, Any]:
        return _error(self.code, self.message)


def load_core() -> Any:
    """Import :mod:`qzone_core`, raising :class:`ServiceUnavailable` if absent."""
    try:
        return importlib.import_module("qzone_core")
    except ImportError as exc:  # pragma: no cover - depends on checkout state
        raise ServiceUnavailable(
            "qzone_core", f"cannot import qzone_core ({exc.name or exc})"
        ) from None


def load_store_module() -> Any:
    """Import :mod:`qzone_store`, raising :class:`ServiceUnavailable` if absent."""
    try:
        return importlib.import_module("qzone_store")
    except ImportError as exc:  # pragma: no cover - depends on checkout state
        raise ServiceUnavailable(
            "qzone_store", f"cannot import qzone_store ({exc.name or exc})"
        ) from None


def load_napcat() -> Any:
    """Import :mod:`qzone_napcat` (the OneBot client layer)."""
    try:
        return importlib.import_module("qzone_napcat")
    except ImportError as exc:  # pragma: no cover
        raise ServiceUnavailable(
            "qzone_napcat", f"cannot import qzone_napcat ({exc.name or exc})"
        ) from None


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def field(obj: Any, name: str, default: Any = None) -> Any:
    """Read ``name`` from a mapping or an attribute-style object."""
    if obj is None:
        return default
    if isinstance(obj, Mapping):
        return obj.get(name, default)
    return getattr(obj, name, default)


def is_allowed(decision: Any) -> bool:
    """Interpret a ``PublishDecision``.  Anything without an explicit ``True``
    is treated as **not** allowed (fail closed)."""
    flag = field(decision, "allowed", None)
    return flag is True


def _decision_reason(decision: Any) -> str:
    reason = field(decision, "reason", None)
    return str(reason) if reason is not None else ""


def _decision_detail(decision: Any) -> dict[str, Any]:
    to_dict = getattr(decision, "to_dict", None)
    if callable(to_dict):
        try:
            data = to_dict()
            if isinstance(data, Mapping):
                return dict(data)
        except Exception:  # noqa: BLE001 - diagnostics must never explode
            logger.debug("decision.to_dict() failed", exc_info=True)
    detail = field(decision, "detail", None)
    return dict(detail) if isinstance(detail, Mapping) else {}


def _ok(data: Mapping[str, Any]) -> dict[str, Any]:
    return {"ok": True, "data": dict(data)}


def _err(code: str, message: str, **extra: Any) -> dict[str, Any]:
    block: dict[str, Any] = {
        "code": code if code in ERROR_CODES else "upstream_error",
        "message": message,
    }
    for key, value in extra.items():
        if value is not None:
            block[key] = value
    return block


def _error(code: str, message: str, **extra: Any) -> dict[str, Any]:
    return {"ok": False, "error": _err(code, message, **extra)}


def _unexpected(where: str, exc: BaseException) -> dict[str, Any]:
    """Turn an unexpected exception into a redacted envelope (no traceback)."""
    logger.warning("%s failed: %s", where, type(exc).__name__, exc_info=True)
    try:
        detail = qzone_core.redact(str(exc))
    except Exception:  # noqa: BLE001
        detail = type(exc).__name__
    return _error("upstream_error", f"{where} failed: {detail}")


def _coerce_int(value: Any, default: int = 0) -> int:
    if isinstance(value, bool):
        return int(value)
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _clamp_limit(limit: Any) -> int:
    try:
        value = int(limit)
    except (TypeError, ValueError):
        return DEFAULT_HISTORY_LIMIT
    if value <= 0:
        return DEFAULT_HISTORY_LIMIT
    return min(value, MAX_HISTORY_LIMIT)


def _candidate_hour(now: int) -> int:
    """Beijing wall-clock hour, independent of the host time zone / DST."""
    return int((int(now) + 8 * 3600) % 86400 // 3600)


#: Key under which the masking placeholder does not matter; the value is a
#: syntactically valid but non-routable QQ length so ``QzoneConfig`` accepts it.
UNCONFIGURED_OWNER_UIN = "00000"


def _configured_owner(value: Any) -> str:
    """Normalise ``owner_uin``; falls back to :data:`UNCONFIGURED_OWNER_UIN`."""
    if isinstance(value, str) and value.strip():
        return value.strip()
    return UNCONFIGURED_OWNER_UIN


# --------------------------------------------------------------------------
# QzoneService
# --------------------------------------------------------------------------


class QzoneService:
    """Orchestrates evaluation, accounting, OneBot writes and receipts.

    Parameters
    ----------
    state_dir:
        Directory holding ``state.json`` (and its backups / lock).
    cfg:
        A ``qzone_core.QzoneConfig``.  Defaults to one derived from
        ``owner_uin``/``owner`` when given, otherwise ``QzoneConfig(owner_uin="")``
        which is *not* enough to publish (owner auth will fail closed).
    transport:
        An :class:`qzone_napcat.OneBotTransport`.  ``None`` -> a real
        :class:`qzone_napcat.UrllibTransport` built from ``onebot_base_url`` /
        ``onebot_access_token`` *only when a call is actually attempted*, so
        ``status`` / ``preview`` / ``history`` never touch the network.
    clock:
        ``() -> epoch seconds``.  Injectable for tests.
    owner_authenticated:
        Whether the owner is logged in on the console.  With ``False`` (the
        default) every write is refused locally; nothing is sent upstream.
    """

    def __init__(
        self,
        *,
        state_dir: os.PathLike[str] | str,
        cfg: Any = None,
        transport: Any = None,
        clock: Callable[[], float] = time.time,
        owner_authenticated: bool = False,
        owner_uin: str | None = None,
        onebot_base_url: str | None = None,
        onebot_access_token: str | None = None,
        lock_timeout: float = LOCK_TIMEOUT_SECONDS,
    ) -> None:
        self.core = load_core()
        self.store_module = load_store_module()
        self.cfg = cfg if cfg is not None else self._default_cfg(owner_uin)
        self.transport = transport
        self.clock = clock
        self.owner_authenticated = bool(owner_authenticated)
        self.state_dir = os.fspath(state_dir)
        if not self.state_dir:
            raise ValueError("state_dir must be a non-empty path")
        # The endpoint settings live outside QzoneConfig (that dataclass is
        # frozen to tunables only), so they are carried here.
        self.onebot_base_url = onebot_base_url or ""
        self._token = onebot_access_token or ""
        self.lock_timeout = float(lock_timeout)
        self._store: Any = None
        self._loaded_state: dict[str, Any] | None = None

    def _default_cfg(self, owner_uin: str | None) -> Any:
        """Config for a service built without one.

        ``QzoneConfig`` requires 5..12 digits, so a missing identity falls back
        to :data:`UNCONFIGURED_OWNER_UIN` (``"00000"``).  A QQ number never has
        leading zeros, so this value cannot equal a real account: the expected
        confirm token is computed over the placeholder, so no guessable token
        verifies on the first try and every write stays refused until the owner
        is configured properly.
        """
        value = _configured_owner(owner_uin)
        try:
            return self.core.QzoneConfig(owner_uin=value)
        except ValueError:
            return self.core.QzoneConfig(owner_uin=UNCONFIGURED_OWNER_UIN)

    # -- plumbing ---------------------------------------------------------
    @property
    def store(self) -> Any:
        """The state store (constructed once)."""
        if self._store is None:
            self._store = self.store_module.StateStore(self.state_dir, self.cfg, now=self.clock)
        return self._store

    def _read_retry(self) -> int:
        try:
            return max(0, min(5, int(getattr(self.cfg, "read_retry", 2))))
        except (TypeError, ValueError):
            return 2

    def _timeout(self) -> float:
        try:
            value = float(getattr(self.cfg, "upstream_timeout_seconds", 20.0))
        except (TypeError, ValueError):
            return 20.0
        return value if value > 0 else 20.0

    def _get_transport(self) -> Any:
        """Return the injected transport, or build a real one on first use.

        端点判定见 :func:`qzone_napcat.resolve_endpoint`：http(s) 走 HTTP（默认，
        兼容既有调用），ws(s) 走**只读快照** WS 通道（生产 NapCat 只开这个）。
        """
        if self.transport is not None:
            return self.transport
        if not self.onebot_base_url:
            raise ServiceError(
                "upstream_error", "no OneBot endpoint configured (onebot_base_url)"
            )
        napcat = load_napcat()
        endpoint = napcat.resolve_endpoint(self.onebot_base_url)
        if endpoint["kind"] == "ws":
            self.transport = napcat.WebSocketTransport(
                endpoint["url"], self._token, self._timeout()
            )
        elif endpoint["kind"] == "http":
            self.transport = napcat.UrllibTransport(
                endpoint["url"], self._token, self._timeout()
            )
        else:
            # 非回环 / 非 http(s)/ws(s) 的端点一律 fail closed，不构造 transport。
            raise ServiceError(
                "upstream_error", "configured OneBot endpoint is not a loopback http(s)/ws(s) URL"
            )
        return self.transport

    def has_transport(self) -> bool:
        return self.transport is not None or bool(self.onebot_base_url)

    def read_endpoint_available(self) -> bool:
        """好友动态刷新是否被允许发请求（**默认 fail closed**）。

        只有三种情况放行：
        - 注入了假 transport（``FakeOneBotTransport``，离线测试专用，永不联网）；
        - 配置了合法的 loopback HTTP 地址（``is_loopback_url`` 严格校验）；
        - 配置了合法的 loopback **ws** 地址（只读快照通道，同样严格校验）；
          真 WS transport 以它自己的 base_url 为准。
        注入一个**真的** transport 而没配 base_url 时不会走到这里，因为它自己就带着
        base_url；这里只回答「配置层面是否允许发请求」。
        """
        napcat = load_napcat()
        transport = self.transport
        if transport is not None:
            # 假 transport 离线，放行；真 transport 以它自己的 base_url 为准
            if isinstance(transport, napcat.FakeOneBotTransport):
                return True
            base = getattr(transport, "base_url", None)
            return napcat.resolve_endpoint(base)["kind"] in ("http", "ws")
        return napcat.resolve_endpoint(self.onebot_base_url)["kind"] in ("http", "ws")

    def _transport_info(self) -> dict[str, Any]:
        if self.transport is None:
            return {
                "kind": "none",
                "configured": bool(self.onebot_base_url),
                "endpoint_configured": bool(self.onebot_base_url),
                "has_token": bool(self._token),
            }
        try:
            return load_napcat().describe_transport(self.transport)
        except Exception:  # noqa: BLE001
            return {"kind": "unknown", "configured": True}

    # -- state ------------------------------------------------------------
    def _load(self) -> dict[str, Any]:
        """Read the state file (fresh every call: cheap and avoids staleness).

        ``StateStore.load()`` returns ``fatal=True`` both for a brand-new state
        directory (no ``state.json`` and no backups => the very first run) and
        for genuinely unrecoverable corruption.  Only the first case may be
        initialised; the second must stay conservative (quota exhausted), which
        is signalled to callers through ``state["fatal"]``.
        """
        try:
            state = self.store.load()
        except Exception as exc:  # noqa: BLE001 - busy / IO
            logger.warning(
                "state could not be loaded (%s); using an empty skeleton",
                type(exc).__name__,
            )
            state = self.core.make_empty_state()
            state["fatal"] = True
            self._loaded_state = state
            return state
        if not isinstance(state, dict):
            state = self.core.make_empty_state()
            state["fatal"] = True
            self._loaded_state = state
            return state
        if state.get("fatal") and self._is_pristine():
            logger.info("empty state directory: initialising a fresh state file")
            fresh = self.core.make_empty_state()
            try:
                self.store.save(fresh)
                state = self.store.load()
            except Exception:  # noqa: BLE001 - saving is best-effort here
                logger.warning("could not write the initial state file", exc_info=True)
            state["fatal"] = False
        self._loaded_state = state
        return state

    def _is_pristine(self) -> bool:
        """True when the state directory holds neither a state file nor backups."""
        try:
            paths = getattr(self.store, "paths", None)
            if paths is not None and hasattr(paths, "state"):
                return not any(
                    os.path.exists(os.fspath(candidate))
                    for candidate in [paths.state, *list(getattr(paths, "backups", []) or [])]
                )
            return not os.path.exists(os.path.join(self.state_dir, "state.json"))
        except Exception:  # noqa: BLE001
            return True

    def _save(self, state: Mapping[str, Any]) -> None:
        self.store.save(state)
        self._loaded_state = dict(state)

    def _mutate(self, mutator: Callable[[dict[str, Any]], dict[str, Any]]) -> dict[str, Any]:
        """``load -> mutator -> save``.  Callers that already hold the lock in a
        ``with store.lock(...)`` block must use :meth:`_load` / :meth:`_save`
        directly instead (the lock is not re-entrant)."""
        state = self._load()
        new_state = mutator(state)
        if not isinstance(new_state, dict):
            raise ServiceError("upstream_error", "state mutator must return a dict")
        self._save(new_state)
        return new_state

    # -- config -----------------------------------------------------------
    def _config_for(self, state: Mapping[str, Any]) -> Any:
        """Base cfg + persisted overlay from ``state["config"]`` (if any)."""
        overlay = state.get("config") if isinstance(state, Mapping) else None
        if not isinstance(overlay, Mapping) or not overlay:
            return self.cfg
        try:
            return self.core.QzoneConfig.from_dict(dict(overlay), base=self.cfg)
        except (ValueError, TypeError):
            logger.warning("stored config overlay ignored (invalid)", exc_info=True)
            return self.cfg

    def _public_config(self, cfg: Any) -> dict[str, Any]:
        """Config view with the owner QQ number masked (never plaintext)."""
        to_dict = getattr(cfg, "to_dict", None)
        raw = to_dict() if callable(to_dict) else {}
        out: dict[str, Any] = {}
        for key in sorted(CONFIG_EDITABLE_FIELDS):
            if key in raw:
                out[key] = raw[key]
        owner = str(raw.get("owner_uin") or "")
        out["owner_uin"] = self.core.mask_uin(owner) if owner else ""
        out["owner_configured"] = bool(owner)
        return out

    # -- owner auth -------------------------------------------------------
    def _used_nonces(self, state: Mapping[str, Any]) -> list[str]:
        for key in _NONCE_KEYS:
            value = state.get(key)
            if isinstance(value, list):
                return [str(item) for item in value]
            if isinstance(value, Mapping):
                return [str(item) for item in value]
        return []

    def _expected_token(self, cfg: Any, nonce: str, now: int) -> str | None:
        """Expected confirm token for ``nonce`` on ``now``'s Beijing day."""
        owner = str(getattr(cfg, "owner_uin", "") or "")
        try:
            day = str(self.core.beijing_day(now))
        except Exception:  # noqa: BLE001
            logger.warning("beijing_day failed", exc_info=True)
            return None
        try:
            return str(self.core.owner_confirm_token(owner, day, nonce))
        except (TypeError, ValueError):
            logger.warning("owner_confirm_token rejected its inputs")
            return None

    def _auth_inputs(
        self,
        *,
        cfg: Any,
        state: Mapping[str, Any],
        owner_authenticated: bool | None,
        confirm_token: str | None,
        confirm: bool,
        nonce: str | None,
        now: int,
    ) -> tuple[bool, str | None, str | None, bool, str]:
        """Compute the four inputs ``check_owner_auth`` needs, plus the nonce.

        Returns ``(owner_authenticated, expected_token, provided_token, confirm,
        nonce)``.  A replayed nonce yields ``expected_token=None`` so validation
        can never succeed (and the caller reports ``owner_unauth``).
        """
        authed = self.owner_authenticated if owner_authenticated is None else bool(owner_authenticated)
        provided = confirm_token if isinstance(confirm_token, str) and confirm_token.strip() else None
        clean_nonce = nonce.strip() if isinstance(nonce, str) and nonce.strip() else ""
        if not clean_nonce:
            return authed, None, provided, confirm is True, ""
        if clean_nonce in self._used_nonces(state):
            logger.warning("confirm nonce replay refused")
            return authed, None, provided, confirm is True, clean_nonce
        expected = self._expected_token(cfg, clean_nonce, now)
        return authed, expected, provided, confirm is True, clean_nonce

    def _mark_nonce_used(
        self, state: dict[str, Any], nonce: str, strict: bool
    ) -> dict[str, Any]:
        """Record a consumed nonce (in place).  Raises if it was replayed and
        ``strict`` is True (the lock-holding re-check path)."""
        if not nonce:
            return state
        used = self._used_nonces(state)
        if nonce in used:
            if strict:
                raise ServiceError("owner_unauth", "confirm_token has already been used")
            return state
        used.append(nonce)
        for key in _NONCE_KEYS:
            state.pop(key, None)
        state["confirm_nonces"] = used[-MAX_NONCES:]
        return state

    # =====================================================================
    # status / history / preview
    # =====================================================================

    def status(self) -> dict[str, Any]:
        """Snapshot of counts, allowance, quiet window and switches."""
        try:
            core = self.core
            now = int(self.clock())
            state = self._load()
            cfg = self._config_for(state)
            day = str(core.beijing_day(now))
            counts = {
                "day": day,
                "published_today": int(core.count_usage(state, day, "posts")),
                "likes_today": int(core.count_usage(state, day, "likes")),
                "comments_today": int(core.count_usage(state, day, "comments")),
                "uncertain_today": int(core.count_usage(state, day, "uncertain")),
            }
            quota = core.allowance(state, day, cfg, now_ts=now)
            last_ts = state.get("last_publish_ts")
            last_int = _coerce_int(last_ts, 0) if isinstance(last_ts, (int, float)) else 0
            data = {
                "today": day,
                "counts": counts,
                "allowance": quota,
                "quiet": bool(quota.get("quiet", False)) if isinstance(quota, Mapping) else False,
                "quiet_window": {"start": getattr(cfg, "quiet_start", None), "end": getattr(cfg, "quiet_end", None)},
                "config": self._public_config(cfg),
                "last_publish_ts": last_int or None,
                "last_publish_age_seconds": max(0, now - last_int) if last_int else None,
                "state_recovered": bool(state.get("recovered", False)),
                "state_fatal": bool(state.get("fatal", False)),
                "enabled": {
                    "publish": int(core.count_usage(state, day, "posts")) < int(getattr(cfg, "max_posts_per_day", 0)),
                    "like": bool(getattr(cfg, "enable_like", False)),
                    "comment": bool(getattr(cfg, "enable_comment", False)),
                },
                "visibility": str(getattr(cfg, "visibility", "private")),
                "ugc_right": int(core.effective_ugc_right(cfg)),
                "owner_authenticated": self.owner_authenticated,
                "transport": self._transport_info(),
                "ready": self.has_transport(),
                "pending_writes": sorted(str(key) for key in (state.get("pending") or {})),
            }
            return _ok(data)
        except ServiceUnavailable as exc:
            return exc.to_envelope()
        except ServiceError as exc:
            return exc.to_envelope()
        except Exception as exc:  # noqa: BLE001
            return _unexpected("status", exc)

    def history(self, limit: int = DEFAULT_HISTORY_LIMIT) -> dict[str, Any]:
        """Recent publish receipts, redacted."""
        try:
            count = _clamp_limit(limit)
            state = self._load()
            rows = self.core.build_snapshot(state, limit=count)
            items = [self._public_snapshot(row) for row in rows]
            return _ok({"items": items, "total": len(items), "limit": count})
        except ServiceUnavailable as exc:
            return exc.to_envelope()
        except Exception as exc:  # noqa: BLE001
            return _unexpected("history", exc)

    def _public_snapshot(self, row: Mapping[str, Any]) -> dict[str, Any]:
        out = dict(row)
        for key in ("owner_uin", "uin", "target_uin", "qq"):
            if key in out and out[key]:
                try:
                    out[key] = self.core.mask_uin(str(out[key]))
                except Exception:  # noqa: BLE001
                    out[key] = "***"
        return out

    def latest(self, limit: int = 20) -> dict[str, Any]:
        """最近一次好友动态只读刷新留下的快照。**不联网、不改 state。**

        本地没有刷新过就返回空列表 + ``refreshed_ts=None``，绝不回落到
        「自己的发布记录」冒充好友动态。
        """
        try:
            core = self.core
            state = self._load()
            day = str(core.beijing_day(int(self.clock())))
            stored = state.get("latest_feeds")
            rows = stored if isinstance(stored, list) else []
            cap = max(1, min(int(core.READ_LATEST_LIMIT), int(limit or 20)))
            items = [dict(row) for row in rows[:cap] if isinstance(row, Mapping)]
            refreshed = state.get("latest_refreshed_ts")
            quota = core.read_allowance(state, day, int(self.clock()))
            return _ok(
                {
                    "items": items,
                    "total": len(items),
                    "limit": cap,
                    "refreshed_ts": int(refreshed) if isinstance(refreshed, (int, float)) else None,
                    "last_error": state.get("latest_error") or None,
                    "allowance": quota,
                    "source": "qzone_feeds" if items else None,
                }
            )
        except ServiceUnavailable as exc:
            return exc.to_envelope()
        except ServiceError as exc:
            return exc.to_envelope()
        except Exception as exc:  # noqa: BLE001
            return _unexpected("latest", exc)

    def refresh_latest(self, *, owner_authenticated: bool | None = None, confirm: bool = False) -> dict[str, Any]:
        """一次性、主人认证、只读地拉取好友动态并更新独立 latest 快照。

        边界（契约 §10）：
        - 只读：只调 ``get_qzone_feeds``，绝不触碰任何写动作。
        - fail closed：未配置 loopback OneBot 地址就拒绝，不发请求。
        - 静默 00:00–07:00（北京时间）拒绝。
        - 低频独立限窗：最小间隔 10 分钟 + 每日 24 次，和发布额度互不占用。
        - 先记账后请求：即使请求失败，这一次刷新也已计入限窗（保守）。
        - 失败一律记成**稳定错误码**并落盘，供控制台显示；不重试成「假装成功」。
        """
        try:
            core = self.core
            if confirm is not True:
                return _error("bad_request", "refresh requires confirm: true")
            authed = self.owner_authenticated if owner_authenticated is None else bool(owner_authenticated)
            now = int(self.clock())
            day = str(core.beijing_day(now))
            available = self.read_endpoint_available()

            with self.store.lock("read-refresh", timeout=self.lock_timeout):
                state = self._load()
                decision = core.evaluate_read_refresh(
                    now_ts=now, state=state, owner_authenticated=authed,
                    transport_available=available,
                )
                if not decision.allowed:
                    reason = str(decision.reason)
                    return _error(_READ_ERROR_TO_CODE.get(reason, "upstream_error"),
                                  _READ_ERROR_MESSAGE.get(reason, "好友动态刷新未执行"),
                                  reason=reason, detail=decision.detail)
                # 先记账，再发请求：失败也占一次，绝不因失败而放开额度。
                state = core.bump_read_refresh(state, day, now)
                self._save(state)

            napcat = load_napcat()
            try:
                transport = self._get_transport()
                page = napcat.get_feeds(
                    transport, page_num=1, count=int(core.READ_LATEST_LIMIT),
                    read_retry=int(getattr(self._config_for(state), "read_retry", 0)),
                )
                rows = core.normalize_latest_feeds(
                    page.get("feeds"), limit=int(core.READ_LATEST_LIMIT)
                )
                error_code = None
            except Exception as exc:  # noqa: BLE001 - classified, never retried upward
                rows = []
                error_code = core.read_error_from_kind(getattr(exc, "kind", ""))
                logger.warning("qzone read refresh failed: %s", core.redact(str(exc)))

            with self.store.lock("read-refresh-settle", timeout=self.lock_timeout):
                state = self._load()
                if error_code:
                    state["latest_error"] = error_code
                    self._save(state)
                    return _error(_READ_ERROR_TO_CODE.get(error_code, "upstream_error"),
                                  _READ_ERROR_MESSAGE.get(error_code, "好友动态读取失败"),
                                  reason=error_code)
                state["latest_feeds"] = rows
                state["latest_refreshed_ts"] = now
                state["latest_error"] = None
                self._save(state)
                quota = core.read_allowance(state, day, now)

            return _ok(
                {
                    "items": rows,
                    "total": len(rows),
                    "refreshed_ts": now,
                    "allowance": quota,
                    "source": "qzone_feeds",
                }
            )
        except ServiceUnavailable as exc:
            return exc.to_envelope()
        except ServiceError as exc:
            return exc.to_envelope()
        except Exception as exc:  # noqa: BLE001
            return _unexpected("refresh_latest", exc)

    def preview(self, now: int | None = None) -> dict[str, Any]:
        """Draft a candidate post and its decision.

        Pure with respect to the account: no write, no counter bump, no state
        mutation, no network.
        """
        try:
            core = self.core
            when = int(self.clock() if now is None else now)
            state = self._load()
            cfg = self._config_for(state)
            day = str(core.beijing_day(when))
            content = self._candidate_text(state, cfg, when, day)
            # Preview must not depend on the caller having a token, so the
            # decision reports the *candidate* decision with owner auth neutral.
            decision = core.evaluate_publish(
                now_ts=when,
                cfg=cfg,
                state=state,
                content=content,
                owner_authenticated=True,
                expected_token="preview",
                provided_token="preview",
                confirm=True,
                images=None,
            )
            allowed = is_allowed(decision)
            reason = _decision_reason(decision)
            return _ok(
                {
                    "candidate": {
                        "content": content,
                        "chars": int(core.text_length(content)),
                        "sig": str(core.topic_signature(content)),
                        "images": [],
                        "ugc_right": int(core.effective_ugc_right(cfg)),
                        "visibility": str(getattr(cfg, "visibility", "private")),
                        "day": day,
                    },
                    "decision": {
                        "allowed": allowed,
                        "reason": reason,
                        "code": "ok" if allowed else REASON_TO_CODE.get(reason, "bad_request"),
                        "detail": _decision_detail(decision),
                    },
                    "note": "candidate only: nothing was sent and no quota was used",
                }
            )
        except ServiceUnavailable as exc:
            return exc.to_envelope()
        except Exception as exc:  # noqa: BLE001
            return _unexpected("preview", exc)

    def _candidate_text(self, state: Mapping[str, Any], cfg: Any, now: int, day: str) -> str:
        """Deterministic, human-sounding draft (no model call, no randomness)."""
        core = self.core
        hour = _candidate_hour(now)
        last_ts = state.get("last_publish_ts")
        gap_hours = None
        if isinstance(last_ts, (int, float)) and int(last_ts) > 0:
            gap_hours = int(max(0, now - int(last_ts)) // 3600)
        if hour < 5:
            slot = "这个点还醒着"
        elif hour < 9:
            slot = "早上的风还没起来"
        elif hour < 12:
            slot = "上午过半"
        elif hour < 14:
            slot = "中午的光正好"
        elif hour < 18:
            slot = "下午被人喊走了两次"
        elif hour < 22:
            slot = "天黑了"
        else:
            slot = "夜里安静下来"
        if gap_hours is None:
            gap = "很久没在这里说话了"
        elif gap_hours <= 1:
            gap = "刚才那条还热着"
        elif gap_hours <= 24:
            gap = f"上一回是 {gap_hours} 小时前"
        else:
            gap = f"上一回是 {gap_hours // 24} 天前"
        body = f"{slot}，{gap}。留一句：事情在慢慢推进，我先记下来。"
        try:
            normalized = core.normalize_text(body)
            if isinstance(normalized, str) and normalized:
                body = normalized
        except Exception:  # noqa: BLE001
            logger.debug("normalize_text failed for the candidate", exc_info=True)
        # Keep the draft inside the configured length window.
        limit = int(getattr(cfg, "max_chars", 280) or 280)
        if len(body) > limit:
            body = body[: max(0, limit - 1)] + "…"
        return body

    # =====================================================================
    # publish
    # =====================================================================

    def publish(
        self,
        content: str,
        images: Sequence[str] | None = None,
        owner_authenticated: bool | None = None,
        confirm_token: str | None = None,
        confirm: bool = False,
        *,
        confirm_nonce: str | None = None,
        nonce: str | None = None,
    ) -> dict[str, Any]:
        """Publish exactly one post, at most once, never retried."""
        nonce_value = confirm_nonce if confirm_nonce is not None else nonce
        try:
            core = self.core
            now = int(self.clock())
            day = str(core.beijing_day(now))
            state = self._load()
            cfg = self._config_for(state)

            if state.get("fatal"):
                return _error(
                    "daily_limit",
                    "state is unrecoverable; treated as quota exhausted (conservative)",
                )

            image_list = list(images or [])
            bad = [item for item in image_list if not isinstance(item, str)]
            if bad:
                return _error("bad_request", "images must be a list of source strings")

            authed, expected, provided, confirmed, clean_nonce = self._auth_inputs(
                cfg=cfg,
                state=state,
                owner_authenticated=owner_authenticated,
                confirm_token=confirm_token,
                confirm=confirm,
                nonce=nonce_value,
                now=now,
            )

            decision = core.evaluate_publish(
                now_ts=now,
                cfg=cfg,
                state=state,
                content=content,
                owner_authenticated=authed,
                expected_token=expected,
                provided_token=provided,
                confirm=confirmed,
                images=image_list or None,
            )
            if not is_allowed(decision):
                reason = _decision_reason(decision)
                code = REASON_TO_CODE.get(reason, "bad_request")
                if code == "owner_unauth" and expected is None:
                    message = "owner authentication failed (missing token, nonce replay, or not logged in)"
                else:
                    message = f"publish refused: {reason}"
                logger.info("publish refused locally: %s", reason)
                return _error(code, message, reason=reason, detail=_decision_detail(decision))

            ugc_right = int(core.effective_ugc_right(cfg))
            retry = self._read_retry()
            napcat = load_napcat()

            # ---- book first, under the cross-process lock ----------------
            intent_key = f"posts:{day}:{now}:{os.getpid()}:{abs(hash((content, now))) % 10**6}"
            try:
                with self.store.lock("publish", timeout=self.lock_timeout):
                    fresh = self._load()
                    if fresh.get("fatal"):
                        return _error("daily_limit", "state is unrecoverable; refusing to publish")
                    # Re-validate inside the lock: another process may have used
                    # the token or taken the last quota share meanwhile.
                    authed2, expected2, provided2, confirmed2, _ = self._auth_inputs(
                        cfg=cfg,
                        state=fresh,
                        owner_authenticated=owner_authenticated,
                        confirm_token=confirm_token,
                        confirm=confirm,
                        nonce=nonce_value,
                        now=now,
                    )
                    redo = core.evaluate_publish(
                        now_ts=now,
                        cfg=cfg,
                        state=fresh,
                        content=content,
                        owner_authenticated=authed2,
                        expected_token=expected2,
                        provided_token=provided2,
                        confirm=confirmed2,
                        images=image_list or None,
                    )
                    if not is_allowed(redo):
                        reason = _decision_reason(redo)
                        return _error(
                            REASON_TO_CODE.get(reason, "bad_request"),
                            f"publish refused: {reason}",
                            reason=reason,
                            detail=_decision_detail(redo),
                        )
                    booked = core.bump_usage(fresh, day, "posts")
                    booked = self.store_module.record_write_intent(
                        booked,
                        day,
                        "posts",
                        {
                            "key": intent_key,
                            # Freshness marker for the store's rollover grace
                            # (contract 6.1) must be the *wall clock*: the store
                            # judges staleness with time.time().  Using the
                            # injected ``now`` here would mark every in-flight
                            # intent stale whenever the clock is a fixed test
                            # value, and load() would charge an extra uncertain.
                            "ts": int(time.time()),
                            "event_ts": now,
                            "chars": int(core.text_length(content or "")),
                            "images": len(image_list),
                            "ugc_right": ugc_right,
                        },
                    )
                    self._mark_nonce_used(booked, clean_nonce, strict=True)
                    self._save(booked)
            except ServiceError as exc:
                return exc.to_envelope()
            except self.store_module.QzoneBusy:
                return _error("busy", "state is locked by another process")
            except Exception as exc:  # noqa: BLE001 - nothing was sent yet
                return _unexpected("publish(book)", exc)

            # ---- the one and only write --------------------------------
            published: dict[str, Any] | None = None
            failure: BaseException | None = None
            try:
                published = napcat.send_shuoshuo(
                    self._get_transport(), content or "", ugc_right, image_list or None
                )
            except Exception as exc:  # noqa: BLE001 - classified below
                failure = exc

            if failure is None:
                outcome = core.classify_publish_result(True, (published or {}).get("tid"), None)
            else:
                kind = str(getattr(failure, "kind", ""))
                prefix = "rejected" if kind in load_napcat().REJECTED_KINDS else "uncertain"
                outcome = core.classify_publish_result(False, None, prefix)
            outcome_name = core.outcome_str(outcome)
            tid = str((published or {}).get("tid", "") or "") if failure is None else ""
            post_time = _coerce_int((published or {}).get("time", 0)) if failure is None else 0

            # ---- private visibility re-check (contract 1.2) -------------
            private_verified: bool | None = None
            if outcome_name == "PUBLISHED" and str(getattr(cfg, "visibility", "private")) == "private":
                applied = None
                try:
                    applied = napcat.set_msg_right(self._get_transport(), tid, ugc_right)
                except Exception as exc:  # noqa: BLE001 - never retried
                    logger.warning(
                        "set_qzone_msg_right failed: %s",
                        core.redact(str(exc)),
                    )
                applied_right = _coerce_int(field(applied, "ugc_right", -1), -1)
                try:
                    consistent = core.check_publish_consistency(outcome, applied_right, cfg)
                except (TypeError, ValueError):
                    consistent = applied_right == ugc_right
                private_verified = bool(consistent)
                if not consistent:
                    logger.warning(
                        "private visibility could not be verified for %s; status -> UNCERTAIN",
                        core.redact(tid),
                    )
                    outcome = core.PublishOutcome.UNCERTAIN
                    outcome_name = "UNCERTAIN"

            # ---- settle ------------------------------------------------
            return self._settle_publish(
                core=core,
                now=now,
                day=day,
                outcome=outcome,
                outcome_name=outcome_name,
                tid=tid,
                post_time=post_time,
                content=content or "",
                images=len(image_list),
                ugc_right=ugc_right,
                intent_key=intent_key,
                failure=failure,
                private_verified=private_verified,
            )
        except ServiceUnavailable as exc:
            return exc.to_envelope()
        except ServiceError as exc:
            return exc.to_envelope()
        except Exception as exc:  # noqa: BLE001
            return _unexpected("publish", exc)

    def _settle_publish(
        self,
        *,
        core: Any,
        now: int,
        day: str,
        outcome: Any,
        outcome_name: str,
        tid: str,
        post_time: int,
        content: str,
        images: int,
        ugc_right: int,
        intent_key: str,
        failure: BaseException | None,
        private_verified: bool | None,
    ) -> dict[str, Any]:
        """Apply contract 6.0 settlement and persist the receipt."""
        try:
            with self.store.lock("publish-settle", timeout=self.lock_timeout):
                state = self._load()
                state = self.store_module.clear_write_intent(state, intent_key)
                if outcome_name == "REJECTED":
                    # Roll the booked share back.  day_bucket() refuses negative
                    # values, so a state with no bucket for this day cannot have
                    # been booked (or was attributed elsewhere) and needs no
                    # rollback.
                    if self._bucket_posts(state, core, day) > 0:
                        bucket = core.day_bucket(state, day)
                        bucket["posts"] = max(0, int(bucket["posts"]) - 1)
                    state = self._append_receipt(
                        core, state, now, day, outcome_name, tid, content, images,
                        ugc_right, private_verified, failure,
                    )
                elif outcome_name == "PUBLISHED":
                    if post_time:
                        state["last_publish_ts"] = int(post_time)
                    state = self._append_receipt(
                        core, state, now, day, outcome_name, tid, content, images,
                        ugc_right, private_verified, failure,
                    )
                else:  # UNCERTAIN: keep the booked share AND mark it unknown
                    # Contract 6.0: "UNCERTAIN -> 不动，只额外 bump_usage(day,
                    # 'uncertain')" - the booked posts share stays and one extra
                    # uncertain unit records the unknown result.  Invariant:
                    # posts + uncertain >= max_posts_per_day is what blocks.
                    #
                    # Contract 6.1, why this is the ONLY charge for the attempt:
                    #   * an intent's ``ts`` is the *wall clock*
                    #     (``int(time.time())``), written by this service; the
                    #     business moment goes to ``event_ts``;
                    #   * the store treats a pending as stale only when its real
                    #     age exceeds ROLLOVER_GRACE_SECONDS (180s), so an intent
                    #     aged < grace - including a negative age - is in flight
                    #     and is never rolled into uncertain;
                    #   * the store deliberately does NOT use its injected clock
                    #     (``StateStore.now``) for this: business time can sit
                    #     arbitrarily in the past/future under test, and judging
                    #     freshness by it would measure "business vs wall clock
                    #     skew", not "how long ago was this dispatched".
                    # The entry we settle here is in flight, so the load() above
                    # did not already charge it; this bump is the single charge.
                    state = core.bump_usage(state, day, "uncertain")
                    state = self._append_receipt(
                        core, state, now, day, outcome_name, tid, content, images,
                        ugc_right, private_verified, failure,
                    )
                self._save(state)
        except Exception:  # noqa: BLE001 - the write already happened; never mask it
            logger.warning("failed to persist the publish receipt", exc_info=True)

        if outcome_name == "PUBLISHED":
            return _ok(
                {
                    "outcome": "PUBLISHED",
                    "tid": tid,
                    "time": int(post_time),
                    "day": day,
                    "ugc_right": ugc_right,
                    "private_verified": private_verified,
                }
            )
        if outcome_name == "UNCERTAIN":
            detail = None
            if failure is not None:
                to_dict = getattr(failure, "to_dict", None)
                info = to_dict() if callable(to_dict) else {}
                detail = info.get("message")
            elif private_verified is False:
                detail = "visibility could not be verified after publishing"
            return {
                "ok": False,
                "error": {
                    "code": "uncertain",
                    "message": "发布结果不确定：已记账、不重试、不会自动补发",
                    "tid": tid or None,
                    "counted": True,
                    "detail": detail,
                },
            }
        to_dict = getattr(failure, "to_dict", None)
        info = to_dict() if callable(to_dict) else {}
        return {
            "ok": False,
            "error": {
                "code": "upstream_error",
                "message": "上游明确拒绝了这次发布（该份额度已回滚，不重试）",
                "retcode": info.get("retcode"),
                "detail": info.get("message"),
            },
        }

    def _bucket_posts(self, state: Mapping[str, Any], core: Any, day: str) -> int:
        """Read-only ``posts`` count for ``day`` (never creates a bucket)."""
        try:
            return int(core.count_usage(state, day, "posts"))
        except (TypeError, ValueError):
            return 0

    def _append_receipt(
        self,
        core: Any,
        state: dict[str, Any],
        now: int,
        day: str,
        outcome_name: str,
        tid: str,
        content: str,
        images: int,
        ugc_right: int,
        private_verified: bool | None,
        failure: BaseException | None,
    ) -> dict[str, Any]:
        entry = {
            "ts": int(now),
            "tid": tid or "",
            "day": day,
            "outcome": outcome_name,
            "chars": int(core.text_length(content)),
            "sig": str(core.topic_signature(content)),
            "has_images": bool(images),
            "ugc_right": int(ugc_right),
        }
        try:
            new_state = core.append_snapshot(state, entry, keep=200)
        except (ValueError, TypeError):
            logger.warning("could not append the publish receipt", exc_info=True)
            new_state = dict(state)
        # Extra, human-facing context lives outside the frozen snapshot schema.
        if private_verified is not None:
            new_state["last_private_verified"] = bool(private_verified)
        if failure is not None:
            to_dict = getattr(failure, "to_dict", None)
            new_state["last_error"] = to_dict() if callable(to_dict) else {"message": str(failure)}
        else:
            new_state.pop("last_error", None)
        return new_state

    # =====================================================================
    # followup (like / unlike / comment)
    # =====================================================================

    def followup(
        self,
        kind: str,
        tid: str,
        text: str | None = None,
        owner_authenticated: bool | None = None,
        confirm_token: str | None = None,
        confirm: bool = False,
        *,
        target_uin: Any = None,
        abstime: int = 0,
        confirm_nonce: str | None = None,
        nonce: str | None = None,
    ) -> dict[str, Any]:
        """Like / unlike / comment on one post.  Off by default."""
        nonce_value = confirm_nonce if confirm_nonce is not None else nonce
        try:
            core = self.core
            now = int(self.clock())
            day = str(core.beijing_day(now))
            state = self._load()
            cfg = self._config_for(state)
            kind_norm = str(kind or "").strip().lower()

            # ``unlike`` shares the enable_like switch and the likes quota.
            gate = {"like": "enable_like", "unlike": "enable_like", "comment": "enable_comment"}
            if kind_norm not in gate:
                return _error("bad_request", "kind must be one of like/unlike/comment")
            if not bool(getattr(cfg, gate[kind_norm], False)):
                logger.info("followup %s refused: switch off, no request sent", kind_norm)
                return _error(
                    "disabled",
                    f"{gate[kind_norm]}=false; {kind_norm} is disabled",
                )
            if state.get("fatal"):
                return _error("daily_limit", "state is unrecoverable; refusing to write")

            authed, expected, provided, confirmed, clean_nonce = self._auth_inputs(
                cfg=cfg,
                state=state,
                owner_authenticated=owner_authenticated,
                confirm_token=confirm_token,
                confirm=confirm,
                nonce=nonce_value,
                now=now,
            )
            base_kind = "comment" if kind_norm == "comment" else "like"
            decision = core.evaluate_followup(
                now_ts=now,
                cfg=cfg,
                state=state,
                kind=base_kind,
                owner_authenticated=authed,
                expected_token=expected,
                provided_token=provided,
                confirm=confirmed,
                target_tid=tid,
            )
            if not is_allowed(decision):
                reason = _decision_reason(decision)
                return _error(
                    REASON_TO_CODE.get(reason, "disabled"),
                    f"followup refused: {reason}",
                    reason=reason,
                    detail=_decision_detail(decision),
                )

            resolved_uin = None
            for candidate in (target_uin, getattr(cfg, "owner_uin", None)):
                if isinstance(candidate, (str, int)) and not isinstance(candidate, bool):
                    text_uin = str(candidate).strip()
                    if text_uin.isdigit():
                        resolved_uin = text_uin
                        break
            if resolved_uin is None:
                return _error("bad_request", "target_uin is required for followup")

            napcat = load_napcat()
            intent_key = f"{base_kind}:{day}:{now}:{os.getpid()}:{abs(hash((tid, now))) % 10**6}"
            try:
                with self.store.lock("followup", timeout=self.lock_timeout):
                    fresh = self._load()
                    if fresh.get("fatal"):
                        return _error("daily_limit", "state is unrecoverable; refusing to write")
                    authed2, expected2, provided2, confirmed2, _ = self._auth_inputs(
                        cfg=cfg,
                        state=fresh,
                        owner_authenticated=owner_authenticated,
                        confirm_token=confirm_token,
                        confirm=confirm,
                        nonce=nonce_value,
                        now=now,
                    )
                    redo = core.evaluate_followup(
                        now_ts=now,
                        cfg=cfg,
                        state=fresh,
                        kind=base_kind,
                        owner_authenticated=authed2,
                        expected_token=expected2,
                        provided_token=provided2,
                        confirm=confirmed2,
                        target_tid=tid,
                    )
                    if not is_allowed(redo):
                        reason = _decision_reason(redo)
                        return _error(
                            REASON_TO_CODE.get(reason, "disabled"),
                            f"followup refused: {reason}",
                            reason=reason,
                        )
                    booked = core.bump_usage(fresh, day, "comments" if base_kind == "comment" else "likes")
                    booked = self.store_module.record_write_intent(
                        booked,
                        day,
                        "comments" if base_kind == "comment" else "likes",
                        {
                            "key": intent_key,
                            # wall clock, not the injected clock: see publish().
                            "ts": int(time.time()),
                            "event_ts": now,
                            "kind": kind_norm,
                            "tid": tid,
                        },
                    )
                    self._mark_nonce_used(booked, clean_nonce, strict=True)
                    self._save(booked)
            except ServiceError as exc:
                return exc.to_envelope()
            except self.store_module.QzoneBusy:
                return _error("busy", "state is locked by another process")
            except Exception as exc:  # noqa: BLE001
                return _unexpected("followup(book)", exc)

            result: dict[str, Any] = {}
            failure: BaseException | None = None
            try:
                transport = self._get_transport()
                if kind_norm == "like":
                    napcat.like(transport, tid, resolved_uin, abstime)
                elif kind_norm == "unlike":
                    napcat.unlike(transport, tid, resolved_uin, abstime)
                else:
                    result = napcat.comment(transport, tid, resolved_uin, text or "")
            except Exception as exc:  # noqa: BLE001 - classified below
                failure = exc

            if failure is None:
                outcome_name = "PUBLISHED"
            else:
                kind_mark = str(getattr(failure, "kind", ""))
                prefix = "rejected" if kind_mark in napcat.REJECTED_KINDS else "uncertain"
                outcome_name = core.outcome_str(core.classify_publish_result(False, None, prefix))

            try:
                with self.store.lock("followup-settle", timeout=self.lock_timeout):
                    state = self._load()
                    state = self.store_module.clear_write_intent(state, intent_key)
                    counter = "comments" if base_kind == "comment" else "likes"
                    if outcome_name == "REJECTED":
                        try:
                            used = int(core.count_usage(state, day, counter))
                        except (TypeError, ValueError):
                            used = 0
                        if used > 0:
                            bucket = core.day_bucket(state, day)
                            bucket[counter] = max(0, int(bucket[counter]) - 1)
                    elif outcome_name == "UNCERTAIN":
                        # Contract 6.0: keep the booked likes/comments share and
                        # record the unknown result with one uncertain unit.
                        # Contract 6.1: the in-flight intent (``ts`` = wall clock,
                        # stale only when real age > ROLLOVER_GRACE_SECONDS) is not
                        # rolled by load(), so this is the only charge for it.
                        state = core.bump_usage(state, day, "uncertain")
                    self._save(state)
            except Exception:  # noqa: BLE001
                logger.warning("failed to persist the followup receipt", exc_info=True)

            if outcome_name == "PUBLISHED":
                payload: dict[str, Any] = {
                    "outcome": "PUBLISHED",
                    "kind": kind_norm,
                    "tid": tid,
                    "day": day,
                }
                if base_kind == "comment":
                    payload["comment_id"] = result.get("comment_id")
                return _ok(payload)
            detail = None
            if failure is not None:
                to_dict = getattr(failure, "to_dict", None)
                info = to_dict() if callable(to_dict) else {}
                detail = info.get("message")
            if outcome_name == "UNCERTAIN":
                return {
                    "ok": False,
                    "error": {
                        "code": "uncertain",
                        "message": f"{kind_norm} 结果不确定：已记账、不重试",
                        "kind": kind_norm,
                        "counted": True,
                        "detail": detail,
                    },
                }
            return {
                "ok": False,
                "error": {
                    "code": "upstream_error",
                    "message": f"上游明确拒绝了 {kind_norm}（额度已回滚，不重试）",
                    "kind": kind_norm,
                    "detail": detail,
                },
            }
        except ServiceUnavailable as exc:
            return exc.to_envelope()
        except ServiceError as exc:
            return exc.to_envelope()
        except Exception as exc:  # noqa: BLE001
            return _unexpected("followup", exc)

    # =====================================================================
    # update_config
    # =====================================================================

    def update_config(self, patch: Mapping[str, Any], confirm: bool = False) -> dict[str, Any]:
        """Validate and persist a config overlay in the state file."""
        try:
            if not isinstance(patch, Mapping) or not patch:
                return _error("bad_request", "patch must be a non-empty object")
            if confirm is not True:
                return _error("owner_unauth", "confirm:true is required for config changes")
            core = self.core
            unknown = sorted(set(patch) - CONFIG_EDITABLE_FIELDS)
            if unknown:
                return _error("bad_request", f"unsupported config field(s): {', '.join(unknown)}")
            if "owner_uin" in patch:
                return _error("bad_request", "owner_uin is not editable through the config route")

            state = self._load()
            base = self._config_for(state)
            try:
                candidate = core.QzoneConfig.from_dict(dict(patch), base=base)
            except (ValueError, TypeError) as exc:
                return _error("bad_request", f"invalid config patch: {core.redact(str(exc))}")
            problem = self._validate_extra(cfg=candidate, patch=patch)
            if problem:
                return _error("bad_request", problem)

            overlay = dict(state.get("config") or {})
            overlay.update({key: patch[key] for key in patch})
            try:
                with self.store.lock("config", timeout=self.lock_timeout):
                    fresh = self._load()
                    merged_overlay = dict(fresh.get("config") or {})
                    merged_overlay.update(dict(patch))
                    # Persist only what differs from the base config so the
                    # overlay stays a minimal, auditable delta.
                    base_dict = self.cfg.to_dict()
                    delta = {
                        key: value
                        for key, value in merged_overlay.items()
                        if base_dict.get(key) != value
                    }
                    changed = fresh.get("config") or {}
                    delta_keys = sorted(set(delta) ^ set(changed))
                    fresh["config"] = delta
                    fresh["config_updated_ts"] = int(self.clock())
                    fresh["config_changed"] = delta_keys
                    self._save(fresh)
            except self.store_module.QzoneBusy:
                return _error("busy", "state is locked by another process")
            self.cfg = candidate
            return _ok(
                {
                    "config": self._public_config(candidate),
                    "overlay": {key: _jsonable(value) for key, value in overlay.items()},
                }
            )
        except ServiceUnavailable as exc:
            return exc.to_envelope()
        except ServiceError as exc:
            return exc.to_envelope()
        except Exception as exc:  # noqa: BLE001
            return _unexpected("config", exc)

    def _validate_extra(self, *, cfg: Any, patch: Mapping[str, Any]) -> str | None:
        """Checks QzoneConfig itself cannot express (visibility <-> quota)."""
        visibility = str(getattr(cfg, "visibility", "private"))
        if visibility in ("partial", "exclude") and "target_uins" not in patch:
            return (
                f"visibility={visibility} requires target_uins, which this route "
                "does not carry; refusing instead of guessing recipients"
            )
        return None


# --------------------------------------------------------------------------
# JSON helpers
# --------------------------------------------------------------------------


def _jsonable(value: Any) -> Any:
    try:
        json.dumps(value)
        return value
    except (TypeError, ValueError):
        return str(value)


__all__ = [
    "ERROR_CODES",
    "REASON_TO_CODE",
    "CONFIG_EDITABLE_FIELDS",
    "LOCK_TIMEOUT_SECONDS",
    "ServiceError",
    "ServiceUnavailable",
    "QzoneService",
    "field",
    "is_allowed",
    "load_core",
    "load_napcat",
    "load_store_module",
]
