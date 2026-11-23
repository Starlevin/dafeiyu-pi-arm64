"""OneBot 11 transport + QZone action wrappers for the qq-qzone module.

Layering (see docs/MODULE_CONTRACT.md section 0)::

    qzone_core  (pure)  ->  qzone_napcat (this file)  ->  qzone_service

Rules honoured here:
  * Standard library only (urllib / http.client / socket / json / time / logging).
  * Every network call carries an explicit timeout.  A bare ``urlopen(url)``
    without a timeout is never used.
  * Failure kinds are normalised so the service layer can decide
    PUBLISHED / REJECTED / UNCERTAIN (:data:`REJECTED_KINDS` /
    :data:`UNCERTAIN_KINDS`).
  * Nothing here retries.  Retry policy (reads only, bounded by
    ``cfg.read_retry``) belongs to qzone_service.
  * Nothing here prints to stdout and nothing here logs a token: the
    ``Authorization`` header value is never interpolated into a message.

Action names and parameter names mirror the upstream NapCatQQ actions
(``packages/napcat-onebot/action/extends/SendQzoneMsg.ts`` and
``.../extends/qzone/*.ts``)::

    send_qzone_msg         content, images?, ugc_right, target_uins?
                           -> {"tid": str, "time": int}   (time==0 => unknown)
    set_qzone_msg_right    tid, ugc_right, target_uins?
                           -> {"ugc_right": int}
    like_qzone             tid, target_uin, abstime   -> null
    unlike_qzone           tid, target_uin, abstime   -> null
    comment_qzone          tid, target_uin, content   -> {"comment_id": str, ...}
    get_qzone_msg_list     format="snowluma", pos, num
                           -> {"total": int, "msglist": [...]}
    get_qzone_msg_detail   tid (target_uin/pos/num optional)
                           -> {"tid", "content", "comment_num", "comments"}

Two transports implement the same ``post(action, payload) -> data`` protocol:

* :class:`UrllibTransport` —— HTTP ``POST {base_url}/{action}``（默认，向后兼容）；
* :class:`WebSocketTransport` —— OneBot 11 **反向 WS** ``ws://127.0.0.1:<port>``
  的一次性只读快照通道（见 :data:`WS_READ_ACTIONS` 与 docs/MODULE_CONTRACT.md §12）。
"""

from __future__ import annotations

import base64
import hashlib
import http.client
import itertools
import json
import logging
import os
import re
import socket
import struct
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable, Mapping, Sequence

from qzone_core import mask_uin, redact

__all__ = [
    "ACTION_SEND_QZONE_MSG",
    "ACTION_SET_QZONE_MSG_RIGHT",
    "ACTION_LIKE_QZONE",
    "ACTION_UNLIKE_QZONE",
    "ACTION_COMMENT_QZONE",
    "ACTION_GET_QZONE_MSG_LIST",
    "ACTION_GET_QZONE_MSG_DETAIL",
    "ACTION_GET_QZONE_FEEDS",
    "WRITE_ACTIONS",
    "LOOPBACK_HOSTS",
    "LOOPBACK_V4",
    "LOOPBACK_V6",
    "HTTP_SCHEMES",
    "WS_SCHEMES",
    "WS_READ_ACTIONS",
    "WS_TOTAL_DEADLINE_SECONDS",
    "WS_READ_TIMEOUT_SECONDS",
    "WS_CONNECT_TIMEOUT_SECONDS",
    "is_loopback_url",
    "loopback_host_port",
    "is_http_endpoint",
    "is_ws_endpoint",
    "resolve_endpoint",
    "get_feeds",
    "READ_ACTIONS",
    "KNOWN_ACTIONS",
    "VALID_UGC_RIGHTS",
    "UGC_RIGHT_OWNER_ONLY",
    "GATED_UGC_RIGHTS",
    "MAX_IMAGES",
    "MAX_COMMENT_CHARS",
    "DEFAULT_TIMEOUT_SECONDS",
    "REJECTED_KINDS",
    "UNCERTAIN_KINDS",
    "RETCODE_REJECTED",
    "OneBotError",
    "OneBotTransport",
    "UrllibTransport",
    "WebSocketTransport",
    "FakeOneBotTransport",
    "is_write_action",
    "is_read_action",
    "send_shuoshuo",
    "set_msg_right",
    "like",
    "unlike",
    "comment",
    "get_msg_list",
    "get_msg_detail",
]

logger = logging.getLogger("qq_qzone.napcat")

# --------------------------------------------------------------------------
# Action names (upstream ActionName.* values)
# --------------------------------------------------------------------------

ACTION_SEND_QZONE_MSG = "send_qzone_msg"
ACTION_SET_QZONE_MSG_RIGHT = "set_qzone_msg_right"
ACTION_LIKE_QZONE = "like_qzone"
ACTION_UNLIKE_QZONE = "unlike_qzone"
ACTION_COMMENT_QZONE = "comment_qzone"
ACTION_GET_QZONE_MSG_LIST = "get_qzone_msg_list"
ACTION_GET_QZONE_MSG_DETAIL = "get_qzone_msg_detail"
ACTION_GET_QZONE_FEEDS = "get_qzone_feeds"

#: Actions that mutate the QZone account.  Never retried, ever.
WRITE_ACTIONS = frozenset(
    {
        ACTION_SEND_QZONE_MSG,
        ACTION_SET_QZONE_MSG_RIGHT,
        ACTION_LIKE_QZONE,
        ACTION_UNLIKE_QZONE,
        ACTION_COMMENT_QZONE,
    }
)
#: Actions that only read.  These (and only these) may be retried.
READ_ACTIONS = frozenset(
    {ACTION_GET_QZONE_MSG_LIST, ACTION_GET_QZONE_MSG_DETAIL, ACTION_GET_QZONE_FEEDS}
)
KNOWN_ACTIONS = WRITE_ACTIONS | READ_ACTIONS

#: 读动作里**允许**走反向 WS 快照通道的子集（刻意留白，不放进来的就是拒绝）。
#: 目前只有好友动态一读；发说说/点赞/评论/改权限永不走 WS。
WS_READ_ACTIONS = frozenset({ACTION_GET_QZONE_FEEDS})

#: WS 快照的**总期限**（连接 + 发送 + 接收，一次请求共用一个 deadline）。
WS_TOTAL_DEADLINE_SECONDS = 12.0
#: 响应帧单次 socket 读等待上限；每次读取仍由同一总期限封顶。
WS_READ_TIMEOUT_SECONDS = 12.0
#: 连接、握手和发送保持较短的保护上限。
WS_CONNECT_TIMEOUT_SECONDS = 5.0
#: 单帧载荷上限：feeds 较大，放到 2 MiB，超了按畸形处理。
WS_MAX_FRAME_BYTES = 2 * 1024 * 1024
#: 收响应时最多忽略多少条「不是我的」帧（事件推送 / 其他并发调用的响应）。
WS_MAX_STRAY_FRAMES = 32
_WS_MAGIC = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
_ECHO_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")

# --------------------------------------------------------------------------
# Upstream domain constants
# --------------------------------------------------------------------------

#: ``ValidUgcRights`` in SendQzoneMsg.ts.
VALID_UGC_RIGHTS = frozenset({1, 4, 16, 64, 128})
#: The only visibility the module is allowed to use by default.
UGC_RIGHT_OWNER_ONLY = 64
#: Rights that require a non-empty ``target_uins`` (upstream rejects otherwise).
GATED_UGC_RIGHTS = frozenset({16, 128})

MAX_IMAGES = 9
MAX_CONTENT_CHARS = 2000
MAX_COMMENT_CHARS = 2000
MAX_TID_CHARS = 8192
DEFAULT_TIMEOUT_SECONDS = 10.0
#: Prefix of tids minted by :class:`FakeOneBotTransport`.
FAKE_TID_PREFIX = "fake-tid-"

#: Failure kinds that prove the upstream refused the call (safe to call REJECTED).
REJECTED_KINDS = frozenset({"rejected", "badcall"})
#: Failure kinds that leave the outcome unknown (service maps to UNCERTAIN).
UNCERTAIN_KINDS = frozenset({"timeout", "conn", "http", "badjson", "retcode"})

#: OneBot retcodes that mean "bad parameter / no permission / not allowed".
RETCODE_REJECTED = frozenset(
    {
        100,  # bad request / unknown action
        102,  # bad param
        103,  # bad param
        104,  # bad params / permission
        106,  # unsupported action
        1400,  # bad request
        1401,  # unauthorized
        1403,  # forbidden
        1404,  # not found
    }
)

_UIN_RE = re.compile(r"^[1-9][0-9]{0,19}$")
_QUERY_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")

# --------------------------------------------------------------------------
# Errors
# --------------------------------------------------------------------------


class OneBotError(Exception):
    """Normalised failure of one OneBot call.

    ``kind`` is one of the :data:`REJECTED_KINDS` / :data:`UNCERTAIN_KINDS`
    members (or ``"badcall"`` for a purely local validation failure).
    ``retcode`` / ``status_code`` are kept for diagnostics only; neither the
    token nor the raw request headers are ever stored on the error.
    """

    __slots__ = ("action", "kind", "message", "retcode", "status_code", "attempts")

    def __init__(
        self,
        message: str,
        *,
        kind: str = "conn",
        action: str | None = None,
        retcode: int | None = None,
        status_code: int | None = None,
    ) -> None:
        super().__init__(message)
        self.message = str(message)
        self.kind = kind
        self.action = action
        self.retcode = retcode
        self.status_code = status_code
        self.attempts = 1

    # -- convenience predicates -------------------------------------------
    @property
    def rejected(self) -> bool:
        """True when upstream clearly refused (never send that thing again)."""
        return self.kind in REJECTED_KINDS

    @property
    def uncertain(self) -> bool:
        """True when the write may or may not have landed."""
        return self.kind in UNCERTAIN_KINDS

    def to_dict(self) -> dict[str, Any]:
        """Redacted, JSON-ready description (server-error codes strip local paths)."""
        out: dict[str, Any] = {"kind": self.kind, "message": redact(self.message)}
        if self.action:
            out["action"] = self.action
        if self.retcode is not None:
            out["retcode"] = int(self.retcode)
        if self.status_code is not None:
            out["status_code"] = int(self.status_code)
        return out

    def __repr__(self) -> str:  # pragma: no cover - diagnostics
        return (
            f"OneBotError(kind={self.kind!r}, action={self.action!r}, "
            f"retcode={self.retcode!r}, message={self.message!r})"
        )


def _local_error(message: str, action: str | None = None) -> OneBotError:
    """A validation failure that never reached the network."""
    return OneBotError(message, kind="badcall", action=action)


#: 只有本机回环端点才算「已配置可用」——QZone 刷新默认 fail closed。
#: ``localhost`` 也算回环，但**真实连接时会被钉回 127.0.0.1**，不会走 DNS 解析。
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})
#: 回环地址族：固定绑定，不做任何名称解析。
LOOPBACK_V4 = "127.0.0.1"
LOOPBACK_V6 = "::1"
HTTP_SCHEMES = frozenset({"http", "https"})
WS_SCHEMES = frozenset({"ws", "wss"})


def is_loopback_url(url: Any, *, schemes: Sequence[str] | None = None) -> bool:
    """``url`` 是否为带显式端口的本机回环地址。

    缺端口、scheme 不在 ``schemes``（默认 http/https）、非回环一律 False。用于在
    **发请求之前**判定配置是否可用，避免把「注入了一个假 transport」误当成
    「配置了可用端点」。传 ``schemes=WS_SCHEMES`` 即判定 ws 快照端点。
    """
    if not isinstance(url, str) or not url.strip():
        return False
    try:
        parsed = urllib.parse.urlsplit(url.strip())
        port = parsed.port
    except ValueError:
        return False
    allowed = HTTP_SCHEMES if schemes is None else frozenset(schemes)
    if parsed.scheme not in allowed or port is None:
        return False
    # 端口 0 是「让系统挑」，对**连接**毫无意义：显式配置 0 一律视为未配置。
    if not 0 < port <= 65535:
        return False
    return (parsed.hostname or "").lower() in LOOPBACK_HOSTS


def loopback_host_port(url: Any, *, schemes: Sequence[str] | None = None) -> tuple[str, int] | None:
    """把回环 URL 解成 **固定本地绑定**的 ``(host, port)``；不可用返回 None。

    ``localhost`` 映射为 ``127.0.0.1``、``[::1]`` 映射为 ``::1``——结果永远是一个
    字面量地址，后续连接不再经过名称解析（杜绝被 hosts / DNS 指到别处）。
    """
    if not is_loopback_url(url, schemes=schemes):
        return None
    parsed = urllib.parse.urlsplit(str(url).strip())
    try:
        port = parsed.port
    except ValueError:  # pragma: no cover - is_loopback_url already rejects this
        return None
    if port is None:  # pragma: no cover
        return None
    host = (parsed.hostname or "").lower()
    if host == "localhost":
        host = LOOPBACK_V4
    return (host, int(port))


def is_http_endpoint(url: Any) -> bool:
    """是否为可用的 loopback **HTTP** OneBot 端点。"""
    return is_loopback_url(url, schemes=HTTP_SCHEMES)


def is_ws_endpoint(url: Any) -> bool:
    """是否为可用的 loopback **WebSocket** OneBot 端点。"""
    return is_loopback_url(url, schemes=WS_SCHEMES)


def resolve_endpoint(url: Any) -> dict[str, Any]:
    """把端点 URL 归类成 ``{"kind": "http"|"ws"|"none", "url": ...}``。

    路由层用它在不 import transport 的情况下决定该建哪种 transport：
    http(s) -> HTTP；ws(s) -> 只读快照 WS；其它（含非回环）-> none（fail closed）。
    """
    if is_loopback_url(url, schemes=HTTP_SCHEMES):
        return {"kind": "http", "url": str(url).strip()}
    if is_loopback_url(url, schemes=WS_SCHEMES):
        return {"kind": "ws", "url": str(url).strip()}
    return {"kind": "none", "url": None}


# --------------------------------------------------------------------------
# Transport protocol
# --------------------------------------------------------------------------


class OneBotTransport:
    """Protocol for an OneBot 11 client.

    Implementations must either return the ``data`` field of the OneBot
    response envelope, or raise :class:`OneBotError`.  They must not retry.
    """

    #: Human readable description used in status output (never a credential).
    description: str = "abstract"

    def post(self, action: str, payload: dict[str, Any]) -> Any:  # pragma: no cover
        raise NotImplementedError

    # -- helpers usable by every transport --------------------------------
    @staticmethod
    def unwrap_envelope(action: str, raw: Any) -> Any:
        """Validate an OneBot 11 response envelope and return ``data``.

        Accepts the standard ``{"status":"ok","retcode":0,"data":{...}}``.
        ``status == "failed"`` or a non-zero ``retcode`` raises a
        :class:`OneBotError` classified via :data:`RETCODE_REJECTED`.
        """
        if not isinstance(raw, Mapping):
            raise OneBotError(
                f"upstream returned a non-object envelope for {action}",
                kind="badjson",
                action=action,
            )
        status = raw.get("status")
        retcode = raw.get("retcode", 0)
        try:
            retcode_int = int(retcode) if retcode is not None else 0
        except (TypeError, ValueError):
            retcode_int = -1
        failed = (isinstance(status, str) and status.lower() == "failed") or retcode_int != 0
        if failed:
            detail = raw.get("message") or raw.get("wording") or raw.get("msg") or ""
            kind = "rejected" if retcode_int in RETCODE_REJECTED else "retcode"
            text = redact(f"upstream rejected {action}: retcode={retcode_int}")
            if detail:
                text = f"{text}: {redact(str(detail))}"
            raise OneBotError(text, kind=kind, action=action, retcode=retcode_int)
        return raw.get("data")

    def __repr__(self) -> str:  # pragma: no cover - diagnostics
        return f"<{type(self).__name__} {self.description}>"


# --------------------------------------------------------------------------
# Real HTTP transport
# --------------------------------------------------------------------------


class UrllibTransport(OneBotTransport):
    """OneBot 11 over HTTP: ``POST {base_url}/{action}`` with a JSON body.

    ``access_token`` is sent as ``Authorization: Bearer <token>``.  The token
    is held privately, is *never* logged, and never appears in an exception
    message (exception text goes through :func:`qzone_core.redact` anyway, but
    the token is not put there in the first place).
    """

    description = "http"

    def __init__(
        self,
        base_url: str,
        access_token: str | None = None,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        *,
        user_agent: str = "qq-qzone/1.0 (+urllib)",
    ) -> None:
        if not isinstance(base_url, str) or not base_url.strip():
            raise _local_error("UrllibTransport requires a non-empty base_url")
        parsed = urllib.parse.urlsplit(base_url.strip())
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise _local_error("base_url must be an absolute http(s) URL")
        # keep path prefix, drop query/fragment/trailing slash
        prefix = parsed.path.rstrip("/")
        self._base_url = urllib.parse.urlunsplit(
            (parsed.scheme, parsed.netloc, prefix, "", "")
        )
        self._token = access_token or ""
        timeout_value = float(timeout)
        if not timeout_value > 0:
            raise _local_error("timeout must be a positive number of seconds")
        self._timeout = timeout_value
        self._user_agent = user_agent
        #: Wall-clock timestamp of the last successful call (None = never).
        self.last_ok_ts: int | None = None

    # -- public ----------------------------------------------------------
    @property
    def base_url(self) -> str:
        """Base URL without credentials (safe to show in status)."""
        return self._base_url

    @property
    def timeout(self) -> float:
        return self._timeout

    def has_token(self) -> bool:
        """Whether a bearer token is configured (the value itself stays private)."""
        return bool(self._token)

    def post(self, action: str, payload: dict[str, Any]) -> Any:
        _check_action_name(action)
        if not isinstance(payload, Mapping):
            raise _local_error(f"payload for {action} must be an object", action)
        body = json.dumps(dict(payload), ensure_ascii=False).encode("utf-8")
        headers = {
            "Content-Type": "application/json; charset=utf-8",
            "Accept": "application/json",
            "User-Agent": self._user_agent,
        }
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"

        url = f"{self._base_url}/{action}"
        request = urllib.request.Request(url, data=body, headers=headers, method="POST")
        raw: Any
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:
                status_code = getattr(response, "status", None) or response.getcode() or 0
                charset = response.headers.get_content_charset() or "utf-8"
                blob = response.read()
        except urllib.error.HTTPError as exc:  # 4xx / 5xx (urllib's own subclass)
            status_code = int(getattr(exc, "code", 0) or 0)
            # Body may carry the upstream wording; read it but never echo headers.
            detail = ""
            try:
                raw_body = exc.read() or b""
                detail = _short(_decode(raw_body, "utf-8"))
            except Exception:  # noqa: BLE001 - diagnostics only
                detail = ""
            finally:
                try:
                    exc.close()
                except Exception:  # noqa: BLE001
                    pass
            raise OneBotError(
                redact(
                    f"HTTP {status_code} from {action}"
                    + (f": {detail}" if detail else "")
                ),
                kind="http",
                action=action,
                status_code=status_code,
            ) from None
        except urllib.error.URLError as exc:
            reason = getattr(exc, "reason", None)
            if isinstance(reason, (socket.timeout, TimeoutError)):
                raise OneBotError(
                    f"timed out after {self._timeout:g}s calling {action}",
                    kind="timeout",
                    action=action,
                ) from None
            if isinstance(reason, socket.gaierror):
                raise OneBotError(
                    f"cannot resolve OneBot host for {action}",
                    kind="conn",
                    action=action,
                ) from None
            raise OneBotError(
                redact(f"cannot connect for {action}: {reason or 'unknown error'}"),
                kind="conn",
                action=action,
            ) from None
        except (socket.timeout, TimeoutError):
            raise OneBotError(
                f"timed out after {self._timeout:g}s calling {action}",
                kind="timeout",
                action=action,
            ) from None
        except (http.client.HTTPException, ConnectionError, OSError) as exc:
            raise OneBotError(
                redact(f"connection dropped during {action}: {type(exc).__name__}"),
                kind="conn",
                action=action,
            ) from None

        if status_code < 200 or status_code >= 300:
            raise OneBotError(
                f"HTTP {status_code} from {action}",
                kind="http",
                action=action,
                status_code=status_code,
            )
        if not blob:
            raise OneBotError(
                f"empty response body for {action}",
                kind="badjson",
                action=action,
                status_code=status_code,
            )
        try:
            envelope = json.loads(_decode(blob, charset))
        except (ValueError, UnicodeDecodeError) as exc:
            raise OneBotError(
                redact(f"response for {action} is not JSON: {type(exc).__name__}"),
                kind="badjson",
                action=action,
                status_code=status_code,
            ) from None
        data = self.unwrap_envelope(action, envelope)
        self.last_ok_ts = int(time.time())
        return data


def _decode(blob: bytes, charset: str) -> str:
    try:
        return blob.decode(charset or "utf-8", errors="replace")
    except LookupError:
        return blob.decode("utf-8", errors="replace")


# --------------------------------------------------------------------------
# Real WebSocket transport (read-only snapshot path)
# --------------------------------------------------------------------------


def _ws_mask(payload: bytes, mask_key: bytes) -> bytes:
    """XOR ``payload`` with the repeating 4-byte ``mask_key`` (RFC 6455 §5.3)."""
    if not payload:
        return b""
    length = len(payload)
    repeats = (length + 3) // 4
    stream = (mask_key * repeats)[:length]
    return (int.from_bytes(payload, "big") ^ int.from_bytes(stream, "big")).to_bytes(length, "big")


def _ws_frame(opcode: int, payload: bytes) -> bytes:
    """Encode one **masked** client frame (clients must mask, servers must not)."""
    length = len(payload)
    header = bytearray([0x80 | (opcode & 0x0F)])
    if length < 126:
        header.append(0x80 | length)
    elif length < (1 << 16):
        header.append(0x80 | 126)
        header += struct.pack(">H", length)
    else:
        header.append(0x80 | 127)
        header += struct.pack(">Q", length)
    mask_key = os.urandom(4)
    return bytes(header) + mask_key + _ws_mask(payload, mask_key)


class _WSSocket:
    """Bare-minimum RFC 6455 client socket (handshake + frames, no extensions).

    只做本模块需要的最小集合：握手、发一个文本帧、按 deadline 收文本帧、
    回 pong。不实现分片消息的重新组装之外的花活；不读写代理环境变量。
    """

    def __init__(self, sock: Any, deadline: float, read_timeout: float) -> None:
        self._sock = sock
        self._deadline = deadline
        self._read_timeout = read_timeout
        self._buf = bytearray()

    # -- plumbing --------------------------------------------------------
    def _remaining(self) -> float:
        left = self._deadline - time.monotonic()
        if left <= 0:
            raise socket.timeout("ws deadline exceeded")
        return left

    def _recv_exact(self, size: int) -> bytes:
        while len(self._buf) < size:
            self._sock.settimeout(min(self._read_timeout, self._remaining()))
            try:
                chunk = self._sock.recv(max(1, min(65536, size - len(self._buf))))
            except socket.timeout:
                raise
            if not chunk:
                raise ConnectionError("ws peer closed the connection")
            self._buf += chunk
        out = bytes(self._buf[:size])
        del self._buf[:size]
        return out

    # -- frames ----------------------------------------------------------
    def send_text(self, text: str) -> None:
        self._sock.settimeout(min(WS_CONNECT_TIMEOUT_SECONDS, self._remaining()))
        self._sock.sendall(_ws_frame(0x1, text.encode("utf-8")))

    def _send_close(self) -> None:
        try:
            self._sock.settimeout(1.0)
            self._sock.sendall(_ws_frame(0x8, b""))
        except Exception:  # noqa: BLE001 - best effort on teardown
            pass

    def recv_text(self) -> str | None:
        """Return the next text payload, or None for a non-text control frame."""
        head = self._recv_exact(2)
        fin = bool(head[0] & 0x80)
        opcode = head[0] & 0x0F
        masked = bool(head[1] & 0x80)
        length = head[1] & 0x7F
        if length == 126:
            length = struct.unpack(">H", self._recv_exact(2))[0]
        elif length == 127:
            length = struct.unpack(">Q", self._recv_exact(8))[0]
        if length < 0 or length > WS_MAX_FRAME_BYTES:
            raise OneBotError("ws frame too large", kind="badjson")
        mask_key = self._recv_exact(4) if masked else b""
        payload = self._recv_exact(length) if length else b""
        if masked and mask_key:
            payload = _ws_mask(payload, mask_key)
        if opcode == 0x8:
            self._send_close()
            raise ConnectionError("ws peer sent close")
        if opcode == 0x9:  # ping -> pong (same payload, masked)
            self._sock.settimeout(min(WS_CONNECT_TIMEOUT_SECONDS, self._remaining()))
            self._sock.sendall(_ws_frame(0xA, payload))
            return None
        if opcode == 0xA:
            return None
        if opcode not in (0x1, 0x0):  # binary / unknown -> not our answer
            return None
        if not fin:  # intermediate fragment
            return None
        return payload.decode("utf-8", errors="replace")

    def close(self) -> None:
        self._send_close()
        try:
            self._sock.close()
        except Exception:  # noqa: BLE001
            pass


class WebSocketTransport(OneBotTransport):
    """OneBot 11 over a **reverse WebSocket** endpoint, one read per connection.

    生产 NapCat 只开 ``ws://127.0.0.1:<port>``（反向 WS 的服务端），而 HTTP 动作
    端点不可达。本类把一次 ``post(action, payload)`` 实现成「一次只读快照请求」：

    * **只有** :data:`WS_READ_ACTIONS` 里的动作被接受，其余（含一切写动作）在
      **发请求之前**就以 ``kind="badcall"`` 拒绝——WS 通道永远不会发出写请求；
    * **固定本地绑定**：主机只可能是 ``127.0.0.1`` 或 ``::1``（``localhost`` 被映射为
      ``127.0.0.1``），不做名称解析；非回环或非 ws(s) 端点直接拒绝；
    * **总期限**：连接 + 发送 + 接收共用一个 ``deadline``（默认
      :data:`WS_TOTAL_DEADLINE_SECONDS` = 12s），单次读另有 12s 上限；
    * **只认自己的 echo**：每次请求带唯一 ``echo``（不可猜的前缀 + 序号），收到的帧
      必须 ``echo`` 完全匹配才算应答；不匹配的帧（事件推送、别的并发调用的响应）被
      忽略，**不会串包**；长时间等不到自己的应答就按 ``timeout`` 失败，绝不复用别人
      的结果；
    * **只读快照语义**：一次请求一条连接，答复后立即关闭，无重试（重试策略仍归
      service 层，且 service 对只读刷新本就 ``read_retry=0``）；
    * **令牌**：``access_token`` 走 ``Authorization: Bearer <token>`` 头，只在私有属性
      里，不打印、不进异常文本、不进 ``__repr__``。
    """

    description = "ws"

    def __init__(
        self,
        base_url: str,
        access_token: str | None = None,
        timeout: float = WS_TOTAL_DEADLINE_SECONDS,
        *,
        read_timeout: float = WS_READ_TIMEOUT_SECONDS,
        allowed_actions: Sequence[str] | None = None,
    ) -> None:
        endpoint = loopback_host_port(base_url, schemes=WS_SCHEMES)
        if endpoint is None:
            raise _local_error(
                "WebSocketTransport requires a ws(s)://127.0.0.1|::1:<port> endpoint"
            )
        self._host, self._port = endpoint
        self._base_url = str(base_url).strip()
        self._token = access_token or ""
        total = float(timeout)
        if not total > 0:
            raise _local_error("timeout must be a positive number of seconds")
        self._timeout = total
        read_limit = float(read_timeout)
        self._read_timeout = read_limit if read_limit > 0 else WS_READ_TIMEOUT_SECONDS
        allowed = WS_READ_ACTIONS if allowed_actions is None else frozenset(allowed_actions)
        unknown = allowed - READ_ACTIONS
        if unknown:
            raise _local_error(f"cannot allow non-read actions over WS: {sorted(unknown)}")
        self._allowed = frozenset(allowed)
        #: 不可猜的 echo 前缀：别人无法让自己的响应「看起来像我的」。
        self._echo_prefix = "qzv-" + os.urandom(8).hex()
        self._seq = itertools.count(1)
        self._lock = threading.Lock()
        self.last_ok_ts: int | None = None

    # -- public ----------------------------------------------------------
    @property
    def base_url(self) -> str:
        """Endpoint without credentials (safe to show in status)."""
        return self._base_url

    @property
    def timeout(self) -> float:
        return self._timeout

    @property
    def allowed_actions(self) -> frozenset[str]:
        return self._allowed

    def has_token(self) -> bool:
        """Whether a bearer token is configured (the value itself stays private)."""
        return bool(self._token)

    def next_echo(self) -> str:
        """A fresh, unguessable echo token for one request."""
        with self._lock:
            return f"{self._echo_prefix}-{next(self._seq)}"

    def post(self, action: str, payload: dict[str, Any]) -> Any:
        _check_action_name(action)
        # 第一道闸：动作不在只读白名单里就别碰网络（写动作永远到不了这里）。
        if action not in self._allowed:
            raise OneBotError(
                f"{action} is not allowed on the read-only WS snapshot channel",
                kind="badcall",
                action=action,
            )
        if not isinstance(payload, Mapping):
            raise _local_error(f"payload for {action} must be an object", action)

        deadline = time.monotonic() + self._timeout
        echo = self.next_echo()
        body = json.dumps(
            {"action": action, "params": dict(payload), "echo": echo}, ensure_ascii=False
        )
        sock: Any = None
        ws: _WSSocket | None = None
        try:
            sock = self._connect()
            ws = self._handshake(sock, deadline)
            ws.send_text(body)
            envelope = self._await_echo(ws, action, echo)
        except OneBotError:
            raise
        except (socket.timeout, TimeoutError):
            raise OneBotError(
                f"timed out after {self._timeout:g}s calling {action} over ws",
                kind="timeout",
                action=action,
            ) from None
        except (ConnectionError, OSError, http.client.HTTPException) as exc:
            raise OneBotError(
                redact(f"ws connection failed during {action}: {type(exc).__name__}"),
                kind="conn",
                action=action,
            ) from None
        finally:
            if ws is not None:
                ws.close()
            elif sock is not None:
                try:
                    sock.close()
                except Exception:  # noqa: BLE001
                    pass

        data = self.unwrap_envelope(action, envelope)
        self.last_ok_ts = int(time.time())
        return data

    # -- internals -------------------------------------------------------
    def _connect(self) -> Any:
        """Dial the **literal** loopback address of the endpoint."""
        family = socket.AF_INET6 if ":" in self._host else socket.AF_INET
        sock = socket.socket(family, socket.SOCK_STREAM)
        sock.settimeout(min(WS_CONNECT_TIMEOUT_SECONDS, self._timeout))
        try:
            sock.connect((self._host, self._port))
        except (socket.timeout, TimeoutError):
            sock.close()
            raise
        except OSError:
            sock.close()
            raise
        return sock

    def _handshake(self, sock: Any, deadline: float) -> _WSSocket:
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        host_header = f"[{self._host}]" if ":" in self._host else self._host
        scheme = "wss" if self._base_url.lower().startswith("wss:") else "ws"
        path = urllib.parse.urlsplit(self._base_url).path or "/"
        lines = [
            f"GET {path} HTTP/1.1",
            f"Host: {host_header}:{self._port}",
            "Upgrade: websocket",
            "Connection: Upgrade",
            f"Sec-WebSocket-Key: {key}",
            "Sec-WebSocket-Version: 13",
        ]
        if self._token:
            lines.append(f"Authorization: Bearer {self._token}")
        request = ("\r\n".join(lines) + "\r\n\r\n").encode("latin-1")
        if scheme == "wss":  # 只在显式 wss: 时才包 TLS，ws: 走明文本地回环
            import ssl

            sock = ssl.create_default_context().wrap_socket(sock, server_hostname=self._host)
        sock.settimeout(min(WS_CONNECT_TIMEOUT_SECONDS, max(0.001, deadline - time.monotonic())))
        sock.sendall(request)
        raw = bytearray()
        while b"\r\n\r\n" not in raw:
            if len(raw) > 65536:
                raise OneBotError("ws handshake response too large", kind="badjson")
            chunk = sock.recv(4096)
            if not chunk:
                raise ConnectionError("ws peer closed during handshake")
            raw += chunk
        head_blob, rest = bytes(raw).split(b"\r\n\r\n", 1)
        head_lines = head_blob.decode("latin-1", errors="replace").split("\r\n")
        status_line = head_lines[0] if head_lines else ""
        parts = status_line.split(" ", 2)
        try:
            status = int(parts[1]) if len(parts) > 1 else 0
        except ValueError:
            status = 0
        headers: dict[str, str] = {}
        for line in head_lines[1:]:
            if ":" in line:
                name, value = line.split(":", 1)
                headers[name.strip().lower()] = value.strip()
        if status != 101:
            raise OneBotError(
                redact(f"ws upgrade refused: HTTP {status}"),
                kind="http" if status else "conn",
                status_code=status or None,
            )
        expected = base64.b64encode(
            hashlib.sha1((key + _WS_MAGIC).encode("ascii")).digest()
        ).decode("ascii")
        if headers.get("sec-websocket-accept") != expected:
            raise OneBotError("ws handshake accept key mismatch", kind="badjson")
        ws = _WSSocket(sock, deadline, self._read_timeout)
        if rest:
            ws._buf += rest  # type: ignore[attr-defined]
        return ws

    def _await_echo(self, ws: _WSSocket, action: str, echo: str) -> Any:
        """Read frames until the one whose ``echo`` equals ours."""
        stray = 0
        while True:
            text = ws.recv_text()
            if text is None:
                continue
            try:
                frame = json.loads(text)
            except ValueError:
                stray += 1
            else:
                if (
                    isinstance(frame, Mapping)
                    and str(frame.get("echo")) == str(echo)
                    and ("retcode" in frame or "status" in frame or "data" in frame)
                ):
                    return frame
                stray += 1
            if stray > WS_MAX_STRAY_FRAMES:
                raise OneBotError(
                    f"no ws response matched echo for {action} "
                    f"(ignored {stray} unrelated frames)",
                    kind="timeout",
                    action=action,
                )


def _check_action_name(action: Any) -> None:
    if not isinstance(action, str) or not _QUERY_RE.match(action):
        raise _local_error("action name must be a lowercase snake_case identifier")


# --------------------------------------------------------------------------
# In-memory fake transport (tests / dry runs, no sockets at all)
# --------------------------------------------------------------------------


class FakeOneBotTransport(OneBotTransport):
    """Pure in-memory OneBot.  Records everything, touches no network.

    Parameters
    ----------
    responses:
        Per-action override.  Value may be

        * a ``dict`` / ``list`` / scalar  -> returned as the ``data`` field,
        * an ``Exception``               -> raised (must be ``OneBotError`` or
          it is wrapped as ``kind="rejected"``),
        * a callable ``f(action, payload)`` -> its result is unwrapped the same
          way a body would be (dict with ``status``/``retcode`` is treated as a
          full envelope),
        * a ``list`` of the above       -> consumed in order, last one repeats.

    allow_actions:
        Whitelist.  Calling anything outside it raises ``AssertionError`` —
        this is how "switch off => zero requests" is proven.

    forbidden:
        Extra action names that must not be called (same ``AssertionError``).
    """

    description = "fake"

    def __init__(
        self,
        responses: Mapping[str, Any] | None = None,
        allow_actions: Sequence[str] | None = None,
        *,
        forbidden: Sequence[str] | None = None,
        clock: Callable[[], float] = time.time,
        read_failures: Mapping[str, int] | None = None,
        delay: float = 0.0,
    ) -> None:
        self.recorded_actions: list[dict[str, Any]] = []
        self.responses: dict[str, Any] = dict(responses or {})
        self.allow_actions: frozenset[str] | None = (
            frozenset(allow_actions) if allow_actions is not None else None
        )
        self.forbidden: frozenset[str] = frozenset(forbidden or ())
        #: ``{action: n}`` — first *n* calls raise ``kind="timeout"``.
        self.read_failures: dict[str, int] = dict(read_failures or {})
        self.delay = float(delay)
        self.clock = clock
        self.last_ok_ts: int | None = None
        self._lock = threading.RLock()
        self._seq = 0
        self._cursor: dict[str, int] = {}
        self._script: list[tuple[str, str]] = []

    # -- inspection helpers ----------------------------------------------
    def actions_called(self) -> list[str]:
        with self._lock:
            return [item["action"] for item in self.recorded_actions]

    def count(self, action: str | None = None) -> int:
        with self._lock:
            if action is None:
                return len(self.recorded_actions)
            return sum(1 for item in self.recorded_actions if item["action"] == action)

    def payloads(self, action: str) -> list[dict[str, Any]]:
        with self._lock:
            return [
                dict(item["payload"])
                for item in self.recorded_actions
                if item["action"] == action
            ]

    def last(self, action: str | None = None) -> dict[str, Any] | None:
        with self._lock:
            for item in reversed(self.recorded_actions):
                if action is None or item["action"] == action:
                    return dict(item)
            return None

    def assert_no_writes(self) -> None:
        """Assert that not a single mutating action was attempted."""
        writes = [a for a in self.actions_called() if a in WRITE_ACTIONS]
        if writes:
            raise AssertionError(f"expected no write actions, got {writes!r}")

    def assert_only(self, *allowed: str) -> None:
        """Assert every recorded action is one of ``allowed``."""
        extra = sorted(set(self.actions_called()) - set(allowed))
        if extra:
            raise AssertionError(f"unexpected actions {extra!r} (allowed={list(allowed)!r})")

    def reset(self) -> None:
        with self._lock:
            self.recorded_actions.clear()
            self._cursor.clear()
            self._script.clear()
            self._seq = 0

    # -- scripted failures ------------------------------------------------
    def fail_next(self, action: Any, times: int = 1, *, kind: str = "timeout") -> None:
        """Make the next ``times`` calls of ``action`` (or a list) fail.

        ``kind="conn"`` / ``"timeout"`` model an *unknown* outcome; ``"rejected"``
        models an upstream refusal.  Default targets every write action.
        """
        targets = (
            list(action) if isinstance(action, (list, tuple, set, frozenset)) else [action]
        )
        with self._lock:
            for _ in range(max(1, int(times))):
                for name in targets:
                    self._script.append((str(name), kind))

    def _scripted_kind(self, action: str) -> str | None:
        with self._lock:
            for index, (name, kind) in enumerate(self._script):
                if name == action:
                    del self._script[index]
                    return kind
        return None

    # -- the transport API ------------------------------------------------
    def post(self, action: str, payload: dict[str, Any]) -> Any:
        _check_action_name(action)
        if not isinstance(payload, Mapping):
            raise _local_error(f"payload for {action} must be an object", action)
        if self.allow_actions is not None and action not in self.allow_actions:
            raise AssertionError(
                f"FakeOneBotTransport: action {action!r} is not in allow_actions "
                f"{sorted(self.allow_actions)!r}"
            )
        if action in self.forbidden:
            raise AssertionError(
                f"FakeOneBotTransport: action {action!r} is forbidden"
            )
        call_payload = json.loads(json.dumps(dict(payload), ensure_ascii=False))
        with self._lock:
            self._seq += 1
            self.recorded_actions.append(
                {"action": action, "payload": call_payload, "ts": self.clock()}
            )
        if self.delay:
            time.sleep(self.delay)

        remaining = self.read_failures.get(action, 0)
        if remaining > 0:
            self.read_failures[action] = remaining - 1
            raise OneBotError(
                f"{action}: upstream not reachable", kind="conn", action=action
            )
        scripted = self._scripted_kind(action)
        if scripted is not None:
            raise OneBotError(
                f"{action}: {scripted} failure (scripted)", kind=scripted, action=action
            )

        outcome = self._resolve(action, call_payload)
        if isinstance(outcome, BaseException):
            if isinstance(outcome, OneBotError):
                if outcome.action is None:
                    outcome.action = action
                raise outcome
            raise OneBotError(
                redact(f"{action}: {type(outcome).__name__}: {outcome}"),
                kind="rejected",
                action=action,
            ) from outcome
        if isinstance(outcome, Mapping) and "retcode" in outcome:
            data = self.unwrap_envelope(action, outcome)
        else:
            data = outcome
        self.last_ok_ts = int(self.clock())
        return data

    # -- built-in behaviour ----------------------------------------------
    def _resolve(self, action: str, payload: dict[str, Any]) -> Any:
        if action in self.responses:
            entry = self.responses[action]
            if isinstance(entry, (list, tuple)) and not isinstance(entry, str):
                index = self._cursor.get(action, 0)
                self._cursor[action] = index + 1
                if not entry:
                    return self._default(action, payload)
                return entry[min(index, len(entry) - 1)]
            if callable(entry):
                return entry(action, payload)
            return entry
        return self._default(action, payload)

    def _default(self, action: str, payload: dict[str, Any]) -> Any:
        now = int(self.clock())
        if action == ACTION_SEND_QZONE_MSG:
            return {"tid": f"{FAKE_TID_PREFIX}{self._seq}", "time": now}
        if action == ACTION_SET_QZONE_MSG_RIGHT:
            right = payload.get("ugc_right", UGC_RIGHT_OWNER_ONLY)
            try:
                right = int(right)
            except (TypeError, ValueError):
                right = UGC_RIGHT_OWNER_ONLY
            return {"ugc_right": right}
        if action in (ACTION_LIKE_QZONE, ACTION_UNLIKE_QZONE):
            return None
        if action == ACTION_COMMENT_QZONE:
            return {"comment_id": f"fake-c{self._seq}"}
        if action == ACTION_GET_QZONE_MSG_LIST:
            return {
                "total": 2,
                "total_known": True,
                "msglist": [
                    {
                        "tid": f"{FAKE_TID_PREFIX}1",
                        "content": "今天风很大",
                        "time": now - 3600,
                        "comment_num": 0,
                        "is_private": True,
                        "images": [],
                    },
                    {
                        "tid": f"{FAKE_TID_PREFIX}2",
                        "content": "把代码跑通的那一刻很安静",
                        "time": now - 7200,
                        "comment_num": 1,
                        "is_private": True,
                        "images": [],
                    },
                ],
            }
        if action == ACTION_GET_QZONE_MSG_DETAIL:
            return {
                "tid": str(payload.get("tid") or f"{FAKE_TID_PREFIX}1"),
                "content": "今天风很大",
                "comment_num": 0,
                "comments": [],
            }
        raise AssertionError(f"FakeOneBotTransport: no default for {action!r}")

    def __repr__(self) -> str:  # pragma: no cover - diagnostics
        return (
            f"<FakeOneBotTransport calls={len(self.recorded_actions)} "
            f"allow={self.allow_actions}>"
        )


# --------------------------------------------------------------------------
# Read helpers (bounded retry, reads only)
# --------------------------------------------------------------------------


def _call_read(
    transport: OneBotTransport,
    action: str,
    payload: dict[str, Any],
    *,
    read_retry: int,
) -> Any:
    """Run a *read* action, retrying at most ``read_retry`` extra times.

    Only :data:`READ_ACTIONS` may pass through here; a write action is
    refused outright so a future refactor cannot accidentally retry a write.
    """
    if action not in READ_ACTIONS:
        raise _local_error(f"refusing to retry non-read action {action!r}", action)
    attempts = max(0, int(read_retry)) + 1
    last: OneBotError | None = None
    for index in range(attempts):
        try:
            return transport.post(action, dict(payload))
        except OneBotError as exc:
            if exc.kind == "rejected":
                raise
            last = exc
            logger.warning(
                "read %s failed (attempt %d/%d): kind=%s %s",
                action,
                index + 1,
                attempts,
                exc.kind,
                redact(exc.message),
            )
    assert last is not None
    last.attempts = attempts
    raise last


# --------------------------------------------------------------------------
# Local validation helpers
# --------------------------------------------------------------------------


def _as_int(value: Any, name: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool):
        raise _local_error(f"{name} must be an integer, not a boolean")
    if isinstance(value, float) and not value.is_integer():
        raise _local_error(f"{name} must be an integer")
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise _local_error(f"{name} must be an integer") from None
    if number < minimum:
        raise _local_error(f"{name} must be >= {minimum}")
    return number


def _valid_tid(tid: Any) -> str:
    """Mirror upstream ``qzoneIdentity``: 1..8192 chars, no C0/C1 controls."""
    if isinstance(tid, bool) or tid is None:
        raise _local_error("tid must be a non-empty string")
    if isinstance(tid, float) and not tid.is_integer():
        raise _local_error("tid must be a non-empty string")
    if not isinstance(tid, (str, int)):
        raise _local_error("tid must be a non-empty string")
    text = str(tid)
    if not text.strip():
        raise _local_error("tid must be a non-empty string")
    if len(text) > MAX_TID_CHARS:
        raise _local_error(f"tid must be at most {MAX_TID_CHARS} characters")
    for character in text:
        code = ord(character)
        if code < 32 or code == 127:
            raise _local_error("tid must not contain control characters")
    return text


def _uart(value: Any) -> str:
    text = str(value).strip() if isinstance(value, (int, str)) and not isinstance(value, bool) else ""
    if not _UIN_RE.match(text):
        raise _local_error("target_uin must be a positive QQ number")
    return text


def _normalise_ugc_right(value: Any) -> int:
    if isinstance(value, bool) or value is None or value == "":
        raise _local_error(
            f"ugc_right must be one of {sorted(VALID_UGC_RIGHTS)}"
        )
    try:
        right = int(value)
    except (TypeError, ValueError):
        raise _local_error(
            f"ugc_right must be one of {sorted(VALID_UGC_RIGHTS)}"
        ) from None
    if right not in VALID_UGC_RIGHTS:
        raise _local_error(f"ugc_right must be one of {sorted(VALID_UGC_RIGHTS)}")
    return right


def _normalise_target_uins(value: Any) -> list[str] | None:
    if value is None:
        return None
    if isinstance(value, (str, bytes)) or not isinstance(value, (list, tuple)):
        raise _local_error("target_uins must be a list of QQ numbers")
    return [_uart(item) for item in value]


def _text(value: Any, name: str, *, allow_empty: bool, limit: int) -> str:
    if isinstance(value, bool) or value is None:
        raise _local_error(f"{name} must be text")
    if not isinstance(value, (str, int, float)):
        raise _local_error(f"{name} must be text")
    text = str(value)
    if not allow_empty and not text.strip():
        raise _local_error(f"{name} must not be blank")
    if len(text) > limit:
        raise _local_error(f"{name} must be at most {limit} characters")
    return text


def _images(value: Any) -> list[str] | None:
    if value is None:
        return None
    if isinstance(value, (str, bytes)) or not isinstance(value, (list, tuple)):
        raise _local_error("images must be a list of image sources")
    if len(value) > MAX_IMAGES:
        raise _local_error(f"images must contain at most {MAX_IMAGES} entries")
    out: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise _local_error("images must be non-empty source strings")
        out.append(item)
    return out


# --------------------------------------------------------------------------
# Thin action wrappers (parameter names match upstream exactly)
# --------------------------------------------------------------------------


def send_shuoshuo(
    transport: OneBotTransport,
    content: str,
    ugc_right: int = UGC_RIGHT_OWNER_ONLY,
    images: Sequence[str] | None = None,
    target_uins: Sequence[Any] | None = None,
) -> dict[str, Any]:
    """``send_qzone_msg`` — publish one QZone post.  **Write: never retried.**

    Mirrors ``SendQzoneMsg.ts``: ``content`` may be ``""`` only when images are
    present; ``ugc_right`` must be in :data:`VALID_UGC_RIGHTS`; ``target_uins``
    is required non-empty for ``16`` / ``128``.

    Returns ``{"tid": str, "time": int}`` (``time == 0`` means "unknown").
    """
    image_list = _images(images)
    text = _text(content, "content", allow_empty=bool(image_list), limit=MAX_CONTENT_CHARS)
    if not image_list and not text.strip():
        raise _local_error("content must not be blank unless images are given")
    right = _normalise_ugc_right(ugc_right)
    targets = _normalise_target_uins(target_uins)
    if right in GATED_UGC_RIGHTS and not targets:
        raise _local_error(
            f"ugc_right {right} requires a non-empty target_uins list"
        )
    if right not in GATED_UGC_RIGHTS and targets:
        # Upstream silently ignores the list here; drop it instead of sending noise.
        targets = None

    payload: dict[str, Any] = {
        "content": text,
        "ugc_right": right,
        "images": list(image_list or []),
    }
    if targets:
        payload["target_uins"] = list(targets)
    data = transport.post(ACTION_SEND_QZONE_MSG, payload)
    if not isinstance(data, Mapping):
        raise OneBotError(
            "send_qzone_msg returned no object", kind="retcode", action=ACTION_SEND_QZONE_MSG
        )
    tid = data.get("tid")
    if not isinstance(tid, str) or not tid.strip():
        raise OneBotError(
            "send_qzone_msg returned an empty tid", kind="retcode", action=ACTION_SEND_QZONE_MSG
        )
    raw_time = data.get("time", 0)
    try:
        post_time = int(raw_time) if raw_time is not None else 0
    except (TypeError, ValueError):
        post_time = 0
    if post_time < 0:
        post_time = 0
    return {"tid": tid.strip(), "time": post_time}


def set_msg_right(
    transport: OneBotTransport,
    tid: Any,
    ugc_right: int,
    target_uins: Sequence[Any] | None = None,
) -> dict[str, Any]:
    """``set_qzone_msg_right`` — adjust visibility.  **Write: never retried.**

    Returns ``{"ugc_right": int}``.  A response whose ``ugc_right`` differs
    from the request is surfaced as ``kind="rejected"`` so callers can treat
    the post as not-yet-private.
    """
    tid_text = _valid_tid(tid)
    right = _normalise_ugc_right(ugc_right)
    targets = _normalise_target_uins(target_uins)
    if right in GATED_UGC_RIGHTS and not targets:
        raise _local_error(f"ugc_right {right} requires a non-empty target_uins list")

    payload: dict[str, Any] = {"tid": tid_text, "ugc_right": right}
    if targets:
        payload["target_uins"] = list(targets)
    data = transport.post(ACTION_SET_QZONE_MSG_RIGHT, payload)
    if not isinstance(data, Mapping):
        raise OneBotError(
            "set_qzone_msg_right returned no object",
            kind="retcode",
            action=ACTION_SET_QZONE_MSG_RIGHT,
        )
    applied = data.get("ugc_right")
    try:
        applied_right = int(applied)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        raise OneBotError(
            "set_qzone_msg_right returned no ugc_right",
            kind="retcode",
            action=ACTION_SET_QZONE_MSG_RIGHT,
        ) from None
    if applied_right != right:
        raise OneBotError(
            f"set_qzone_msg_right applied {applied_right}, expected {right}",
            kind="rejected",
            action=ACTION_SET_QZONE_MSG_RIGHT,
        )
    return {"ugc_right": applied_right}


def like(
    transport: OneBotTransport,
    tid: Any,
    target_uin: Any,
    abstime: int = 0,
) -> None:
    """``like_qzone`` — like someone's post.  **Write: never retried.**"""
    payload = {
        "tid": _valid_tid(tid),
        "target_uin": _uart(target_uin),
        "abstime": _as_int(abstime, "abstime", minimum=0),
    }
    transport.post(ACTION_LIKE_QZONE, payload)
    return None


def unlike(
    transport: OneBotTransport,
    tid: Any,
    target_uin: Any,
    abstime: int = 0,
) -> None:
    """``unlike_qzone`` — undo a like.  **Write: never retried.**"""
    payload = {
        "tid": _valid_tid(tid),
        "target_uin": _uart(target_uin),
        "abstime": _as_int(abstime, "abstime", minimum=0),
    }
    transport.post(ACTION_UNLIKE_QZONE, payload)
    return None


def comment(
    transport: OneBotTransport,
    tid: Any,
    target_uin: Any,
    content: str,
) -> dict[str, Any]:
    """``comment_qzone`` — comment on someone's post.

    **Write: never retried.**  Returns ``{"comment_id": str}``.
    """
    payload = {
        "tid": _valid_tid(tid),
        "target_uin": _uart(target_uin),
        "content": _text(content, "content", allow_empty=False, limit=MAX_COMMENT_CHARS),
    }
    data = transport.post(ACTION_COMMENT_QZONE, payload)
    comment_id = ""
    if isinstance(data, Mapping):
        raw = data.get("comment_id")
        if isinstance(raw, (str, int)) and str(raw).strip():
            comment_id = str(raw).strip()
    if not comment_id:
        raise OneBotError(
            "comment_qzone returned no comment_id",
            kind="retcode",
            action=ACTION_COMMENT_QZONE,
        )
    return {"comment_id": comment_id}


def get_msg_list(
    transport: OneBotTransport,
    pos: int = 0,
    num: int = 20,
    *,
    read_retry: int = 0,
) -> dict[str, Any]:
    """``get_qzone_msg_list`` (``format="snowluma"``).  **Read: may retry.**

    Returns ``{"total": int, "msglist": [...]}``.
    """
    payload: dict[str, Any] = {
        "format": "snowluma",
        "pos": _as_int(pos, "pos", minimum=0),
        "num": max(1, min(100, _as_int(num, "num", minimum=1))),
    }
    data = _call_read(transport, ACTION_GET_QZONE_MSG_LIST, payload, read_retry=read_retry)
    if not isinstance(data, Mapping):
        raise OneBotError(
            "get_qzone_msg_list returned no object",
            kind="badjson",
            action=ACTION_GET_QZONE_MSG_LIST,
        )
    msglist = data.get("msglist")
    if msglist is None:
        msglist = data.get("list") or []
    if not isinstance(msglist, list):
        raise OneBotError(
            "get_qzone_msg_list returned a malformed msglist",
            kind="badjson",
            action=ACTION_GET_QZONE_MSG_LIST,
        )
    total = data.get("total", len(msglist))
    try:
        total_int = int(total)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        total_int = len(msglist)
    return {"total": total_int, "msglist": msglist}


def get_feeds(
    transport: OneBotTransport,
    page_num: int = 1,
    count: int = 10,
    cursor: str | None = None,
    *,
    read_retry: int = 0,
) -> dict[str, Any]:
    """``get_qzone_feeds`` —— 读好友动态。**Read: may retry.**

    上游 ``GetQzoneFeeds.ts`` 的 payload schema 是 snake_case
    （``format`` / ``page_num`` / ``count`` / ``cursor``）；它内部的
    ``normalizeQzoneFeedsPayload`` 才把 snake_case 转成 camelCase。因此这里
    **必须**发 snake_case 键，发 ``pageNum`` 会被 schema 的 additionalProperties
    放过、却让 ``page_num`` 取默认值，表现为「翻页参数静默失效」。

    Returns ``{"feeds": [...], "has_more": bool, "next_cursor": str|None}``.
    ``feeds`` 原样保留上游条目（不做字段裁剪），由 service 负责挑出快照需要的字段。
    """
    payload: dict[str, Any] = {
        "format": "snowluma",
        "page_num": max(1, _as_int(page_num, "page_num", minimum=1)),
        "count": max(1, min(50, _as_int(count, "count", minimum=1))),
    }
    if cursor is not None:
        if not isinstance(cursor, str) or not cursor or len(cursor) > 65536:
            raise _local_error("cursor must be a non-empty string (<=65536 chars)", ACTION_GET_QZONE_FEEDS)
        payload["cursor"] = cursor

    data = _call_read(transport, ACTION_GET_QZONE_FEEDS, payload, read_retry=read_retry)
    if not isinstance(data, Mapping):
        raise OneBotError(
            "get_qzone_feeds returned no object",
            kind="badjson",
            action=ACTION_GET_QZONE_FEEDS,
        )
    feeds = data.get("feeds")
    if feeds is None:
        feeds = data.get("msglist")
    if not isinstance(feeds, list):
        raise OneBotError(
            "get_qzone_feeds returned a malformed feeds list",
            kind="badjson",
            action=ACTION_GET_QZONE_FEEDS,
        )
    next_cursor = data.get("next_cursor")
    if next_cursor is not None and not isinstance(next_cursor, str):
        next_cursor = None
    return {
        "feeds": feeds,
        "has_more": bool(data.get("has_more", False)),
        "next_cursor": next_cursor,
    }


def get_msg_detail(
    transport: OneBotTransport,
    tid: Any,
    *,
    read_retry: int = 0,
    pos: int = 0,
    num: int = 20,
) -> dict[str, Any]:
    """``get_qzone_msg_detail``.  **Read: may retry.**"""
    payload: dict[str, Any] = {
        "tid": _valid_tid(tid),
        "pos": _as_int(pos, "pos", minimum=0),
        "num": max(1, min(100, _as_int(num, "num", minimum=1))),
    }
    data = _call_read(transport, ACTION_GET_QZONE_MSG_DETAIL, payload, read_retry=read_retry)
    if not isinstance(data, Mapping):
        raise OneBotError(
            "get_qzone_msg_detail returned no object",
            kind="badjson",
            action=ACTION_GET_QZONE_MSG_DETAIL,
        )
    return dict(data)


# --------------------------------------------------------------------------
# Predicates used by the service layer
# --------------------------------------------------------------------------


def is_write_action(action: str) -> bool:
    """Whether ``action`` mutates the account (and therefore must never retry)."""
    return action in WRITE_ACTIONS


def is_read_action(action: str) -> bool:
    """Whether ``action`` is a pure read."""
    return action in READ_ACTIONS


def describe_transport(transport: OneBotTransport | None) -> dict[str, Any]:
    """Credential-free description of a transport for ``status()`` output."""
    if transport is None:
        return {"kind": "none", "configured": False}
    info: dict[str, Any] = {"kind": getattr(transport, "description", "unknown"), "configured": True}
    if isinstance(transport, UrllibTransport):
        info["base_url"] = transport.base_url
        info["has_token"] = transport.has_token()
        info["timeout"] = transport.timeout
    if isinstance(transport, WebSocketTransport):
        # 只报端点与「有没有令牌」，令牌值本身永不出现。
        info["base_url"] = transport.base_url
        info["has_token"] = transport.has_token()
        info["timeout"] = transport.timeout
        info["read_only"] = True
        info["allowed_actions"] = sorted(transport.allowed_actions)
    if isinstance(transport, FakeOneBotTransport):
        info["calls_recorded"] = len(transport.recorded_actions)
    return info
