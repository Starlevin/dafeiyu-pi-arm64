"""Read-only RFC 6455 WebSocket bridge for the fish-house live page.

Scope and guarantees
--------------------
* Same-origin only. The HTTP upgrade is accepted from this server's own pages:
  no ``Origin`` (non-browser client) or an ``Origin`` whose host matches the
  request ``Host`` and resolves to a loopback / LAN address. Everything else is
  refused before a single frame is read.
* Read-only. Frames sent by a client are parsed for protocol compliance
  (masking, fragmentation, control frames, UTF-8) and then discarded. There is
  no command surface, so this cannot send a platform message.
* Evidence-only phases. ``received`` / ``thinking`` are only produced from a
  structured inbound event; ``sent`` only from a confirmed send receipt. QQ's
  panel ``busy`` flag is a connection/pipeline indicator and is never turned
  into chat activity. When the only live evidence is a service being up, the
  QQ side reports ``idle`` with ``evidence="degraded"`` instead of inventing a
  stage.
* Nothing private leaves the process: no message bodies, group numbers,
  credentials, trace ids or log text. Bubbles are fixed Chinese short lines.
* Bounded resources: at most ``MAX_CLIENTS`` sockets, one frame read per client
  per tick, ring-buffer dedupe of ``seq``, and a per-client outbound queue whose
  overflow drops the client instead of growing without limit.

Public contract of a pushed frame::

    {"type": "state" | "flow_goto" | "bubble" | "config",
     "seq": <int, strictly increasing>, "ts": <epoch seconds>,
     "source": "qq" | "wechat", "phase": <idle|received|thinking|tool|replying|sent|error>,
     "node_id": <real theme node id or "">, "bubble_text": <short text or "">,
     "ttl_ms": <non-negative int>}
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import ipaddress
import json
import os
import select
import socket
import struct
import threading
import time
from collections import deque
from typing import Any, Callable
from urllib.parse import urlsplit

from backend.flow_bridge import (
    BUBBLE_TTL_MS,
    PHASES,
    FlowRouter,
    PHASE_BUBBLES,
    PHASE_LABELS,
    PHASE_LOOP,
    bubble_expired,
    bubble_for,
    phase_for_source_snapshot,
)
from backend.status_adapter import normalize_bridge_health

WS_GUID = b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
WS_PATH = "/ws"

MAX_CLIENTS = 16
MAX_FRAME_BYTES = 4096
MAX_MESSAGE_BYTES = 8192
MAX_HEADER_BYTES = 4096
MAX_PER_CLIENT_QUEUE = 64
MAX_SEQ_MEMORY = 4096
MAX_THEME_BYTES = 512 * 1024
TICK_SECONDS = 1.0
PING_INTERVAL = 25.0
PONG_TIMEOUT = 10.0
MIN_SEND_INTERVAL = 0.05

OPCODE_CONT, OPCODE_TEXT, OPCODE_BINARY = 0x0, 0x1, 0x2
OPCODE_CLOSE, OPCODE_PING, OPCODE_PONG = 0x8, 0x9, 0xA

EVIDENCE_VERIFIED = "verified"
EVIDENCE_DEGRADED = "degraded"

# 鍥哄畾鏉ユ簮鏃堕棿绐楋細inbound 鐢ㄦ椿鍔ㄧ獥鍙ｏ紝sent 鐢ㄥ洖鎵х獥鍙ｃ€?INBOUND_WINDOW_SEC = 40.0
SENT_WINDOW_SEC = 8.0
QQ_RECEIVED_WINDOW_SEC = 15.0
LIVE_TTL_MS = 15000


# --------------------------------------------------------------- http plumbing
class WSError(Exception):
    """Upgrade rejected; the caller renders an HTTP status and closes."""


def http_date(now: float | None = None) -> str:
    return time.strftime("%a, %d %b %Y %H:%M:%S GMT",
                         time.gmtime(time.time() if now is None else now))


def http_response(status: int, headers: list[tuple[str, str]] | None = None,
                  body: bytes = b"") -> bytes:
    reasons = {204: "No Content", 409: "Conflict", 426: "Upgrade Required",
               403: "Forbidden", 400: "Bad Request", 200: "OK", 426: "Upgrade Required"}
    lines = ["HTTP/1.1 %d %s" % (status, reasons.get(status, "Error"))]
    head = list(headers or [])
    low = {name.lower() for name, _ in head}
    if "content-length" not in low:
        head.append(("Content-Length", str(len(body))))
    if "date" not in low:
        head.append(("Date", http_date()))
    if "connection" not in low:
        head.append(("Connection", "close"))
    for name, value in head:
        lines.append("%s: %s" % (name, value))
    return ("\r\n".join(lines) + "\r\n\r\n").encode("latin-1") + body


def parse_http_request(method: str, target: str,
                       headers: dict[str, str]) -> tuple[str, str, str, dict[str, str]]:
    """Validate the upgrade request from already-parsed stdlib fields.

    The HTTP handler runs ``parse_request`` before calling ``do_GET``, so the
    socket no longer holds the request head. Reusing those fields is both the
    correct and the only non-blocking option.
    """
    if not isinstance(method, str) or not method:
        raise WSError("bad method")
    if not isinstance(target, str) or not target.startswith("/"):
        raise WSError("bad target")
    if len(target) > MAX_HEADER_BYTES:
        raise WSError("target too long")
    if not isinstance(headers, dict):
        raise WSError("bad headers")
    normalized: dict[str, str] = {}
    for name, value in (headers or {}).items():
        key = str(name).strip().lower()
        if not key:
            continue
        if key in normalized:
            # Duplicate headers are rejected to avoid smuggling.
            raise WSError("duplicate header")
        normalized[key] = str(value).strip()
    return method.upper(), target, "HTTP/1.1", normalized


def parse_request(head: bytes) -> tuple[str, str, str, dict[str, str], list[str]]:
    """Parse a request line plus headers without trusting any of it."""
    if len(head) > MAX_HEADER_BYTES:
        raise WSError("header too large")
    text = head.decode("latin-1")
    lines = text.split("\r\n")
    if not lines or lines[0].count(" ") != 2:
        raise WSError("bad request line")
    method, target, version = lines[0].split(" ")
    if version != "HTTP/1.1" or not target.startswith("/"):
        raise WSError("unsupported request")
    headers: dict[str, str] = {}
    order: list[str] = []
    for line in lines[1:]:
        if not line:
            continue
        if ":" not in line or line.startswith(" "):
            raise WSError("bad header")
        name, _, value = line.partition(":")
        name = name.strip().lower()
        if name in headers:  # duplicate header -> reject
            raise WSError("duplicate header")
        headers[name] = value.strip()
        order.append(name)
    return method, target, version, headers, order


def origin_allowed(origin: str | None, host_header: str, peer: str,
                   lan_net: ipaddress.IPv4Network | ipaddress.IPv6Network,
                   is_lan: Callable[[str], bool]) -> bool:
    """Same-origin gate: no Origin (non-browser) or Origin host == Host."""
    if not is_lan(peer):
        return False
    if origin is None:
        return True
    parts = urlsplit(origin)
    if parts.scheme not in ("http", "https"):
        return False
    if parts.netloc.lower() != (host_header or "").lower():
        return False
    host = urlsplit("//" + parts.netloc).hostname or ""
    return bool(host) and bool(is_lan(host) or host.lower() == "localhost")


def encode_frame(opcode: int, payload: bytes) -> bytes:
    header = bytearray([0x80 | (opcode & 0x0F)])
    length = len(payload)
    if length < 126:
        header.append(length)
    elif length < 65536:
        header.append(126)
        header += struct.pack("!H", length)
    else:
        header.append(127)
        header += struct.pack("!Q", length)
    return bytes(header) + payload


def read_frame(stream, *, expect_mask: bool = True) -> tuple[int, bytes] | None:
    """Read exactly one frame; return None on clean EOF.

    Raises WSError on anything that violates the spec, so a broken peer is
    dropped instead of being tolerated into an unbounded buffer.
    """
    start = stream.read(2)
    if not start:
        return None
    if len(start) < 2:
        raise WSError("short frame header")
    fin = bool(start[0] & 0x80)
    opcode = start[0] & 0x0F
    if start[0] & 0x70:
        raise WSError("reserved bits set")
    masked = bool(start[1] & 0x80)
    length = start[1] & 0x7F
    if length == 126:
        raw = stream.read(2)
        if len(raw) < 2:
            raise WSError("short length")
        length = struct.unpack("!H", raw)[0]
    elif length == 127:
        raw = stream.read(8)
        if len(raw) < 8:
            raise WSError("short length")
        length = struct.unpack("!Q", raw)[0]
    if opcode >= 0x8:
        if not fin:
            raise WSError("fragmented control frame")
        if length > 125:
            raise WSError("control frame too large")
    if length > MAX_MESSAGE_BYTES:
        raise WSError("frame too large")
    if expect_mask and not masked:
        raise WSError("client frames must be masked")
    mask = b""
    if masked:
        mask = stream.read(4)
        if len(mask) < 4:
            raise WSError("short mask")
    payload = b""
    remaining = length
    while remaining > 0:
        chunk = stream.read(remaining)
        if not chunk:
            raise WSError("truncated payload")
        payload += chunk
        remaining -= len(chunk)
    if masked:
        payload = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
    return (opcode | (0x80 if fin else 0), payload)


class ClientQueue:
    """Bounded outbound queue with seq dedupe (ring buffer per client)."""

    def __init__(self, maxlen: int = MAX_PER_CLIENT_QUEUE):
        self.lock = threading.Lock()
        self.items: deque[bytes] = deque()
        self.maxlen = maxlen
        self.seen: dict[int, None] = {}
        self.overflow = False
        self.closed = False

    def push(self, frame: dict[str, Any]) -> bool:
        seq = frame.get("seq")
        with self.lock:
            if self.closed:
                return False
            if isinstance(seq, int):
                if seq in self.seen:
                    return False
                self.seen[seq] = None
                while len(self.seen) > MAX_SEQ_MEMORY:
                    self.seen.pop(next(iter(self.seen)))
            if len(self.items) >= self.maxlen:
                # Slow consumer: drop it rather than grow unbounded.
                self.overflow = True
                self.items.clear()
                return False
            self.items.append(json.dumps(frame, ensure_ascii=False,
                                         separators=(",", ":")).encode("utf-8"))
            return True

    def drain(self) -> list[bytes]:
        with self.lock:
            items = list(self.items)
            self.items.clear()
            return items


# ----------------------------------------------------------------- evidence
class EvidenceSnapshot:
    """Normalized, privacy-free view of both sources at one instant."""

    def __init__(self, source: str, phase: str, evidence: str,
                 node_hint: str = "", reason: str = "", at: float = 0.0,
                 expires_at: float = 0.0):
        self.source = source
        self.phase = phase
        self.evidence = evidence
        self.node_hint = node_hint
        self.reason = reason
        self.at = at
        self.expires_at = expires_at

    def as_dict(self) -> dict[str, Any]:
        return {"source": self.source, "phase": self.phase,
                "evidence": self.evidence, "reason": self.reason, "at": self.at}


def _age(event: Any, now: float) -> float | None:
    if not isinstance(event, dict):
        return None
    value = event.get("time")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    age = float(now) - float(value)
    return age if age >= 0 else None


def wechat_evidence(live: dict[str, Any], *, now: float) -> EvidenceSnapshot:
    """WeChat is authorized: reuse the bridge-health adapter as-is.

    ``status_adapter.normalize_bridge_health`` already fails closed on stale
    events, so a non-idle ``activity_state`` is itself the verified stage; the
    diagnostics block is only consulted when the state is idle.
    """
    wx = (live.get("wechat") or {}) if isinstance(live, dict) else {}
    activity_state = str(wx.get("activity_state") or "idle")
    diag = wx.get("diagnostics") if isinstance(wx.get("diagnostics"), dict) else {}
    phase = phase_for_source_snapshot(activity_state)
    if phase != "idle":
        # Only stages already normalized by the adapter are trusted.
        at = wx.get("activity_at")
        expires = wx.get("activity_until")
        return EvidenceSnapshot("wechat", phase, EVIDENCE_VERIFIED,
                                reason=str(diag.get("reason") or ""),
                                at=at if isinstance(at, (int, float)) and 0 < at <= now else 0.0,
                                expires_at=expires if type(expires) in (int, float)
                                and now < expires <= now + 300 else 0.0)
    return EvidenceSnapshot("wechat", "idle", EVIDENCE_VERIFIED,
                            reason="no_recent_event" if not diag else
                            str(diag.get("reason") or "idle"), at=now)


QQ_RECORD_STATES = {"queued", "generating", "sending", "confirmed",
                    "skipped", "failed", "uncertain", "cancelled"}
# Only this exact OneBot acceptance line counts as a chat receipt.
QQ_CHAT_CONFIRM_PREFIX = "OneBot 已接受发送"
QQ_FALLBACK_NOTICE = "模型输出格式无效，已发送一次短句兜底；未写入上下文或记忆"
QQ_NATIVE_CHAT_INGRESS_DETAIL = "QQ原生聊天已入队"
QQ_TOOL_DETAIL = "QQ聊天工具执行中"
# Image generation may take 150 s before trying its fallback. The Core removes
# this in-memory marker immediately when the operation returns or is cancelled.
QQ_TOOL_WINDOW = 300.0
QQ_ACTIVE_CHAT = {
    "generating": ("QQ聊天模型请求中", "thinking", 120.0),
    "sending": ("QQ聊天提交 OneBot", "replying", 30.0),
}
QQ_MAX_RECORDS = 6


def _record_detail(item: dict[str, Any]) -> str:
    """Read a display detail defensively; it is never forwarded upstream."""
    for key in ("detail", "message", "text"):
        value = item.get(key)
        if isinstance(value, str):
            return value
    return ""


def qq_confirmed_chat(item: Any) -> bool:
    """True only for a *chat* send receipt.

    Two shapes are accepted, and only these:

    * the raw controller projection, whose ``detail`` starts with the exact
      OneBot acceptance line (illustration / persona / settings commands also
      end as ``confirmed`` but never carry that prefix);
    * the pre-reduced projection, where the caller already replaced ``detail``
      with the boolean ``_chat`` flag (the original text never leaves the
      process that produced it).
    """
    if not isinstance(item, dict):
        return False
    if str(item.get("state") or "") != "confirmed":
        return False
    if item.get("_chat") is True:
        return True
    return _record_detail(item).strip().startswith(QQ_CHAT_CONFIRM_PREFIX)


def qq_fallback_notice(item: Any) -> bool:
    """The core logs this format warning after an accepted fallback send."""
    return (isinstance(item, dict) and item.get("state") == "failed"
            and (item.get("_fallback_notice") is True
                 or _record_detail(item).strip() == QQ_FALLBACK_NOTICE))


def qq_native_chat_ingress(item: Any) -> bool:
    """True only for the fixed marker emitted after scoped native @ enqueue."""
    if not isinstance(item, dict) or str(item.get("state") or "") != "queued":
        return False
    return item.get("_ingress") is True or _record_detail(item).strip() == QQ_NATIVE_CHAT_INGRESS_DETAIL


def qq_active_chat(item: dict[str, Any]) -> bool:
    expected = QQ_ACTIVE_CHAT.get(str(item.get("state") or ""))
    return expected is not None and (
        item.get("_active") is True or _record_detail(item).strip() == expected[0])


def qq_tool_running(item: dict[str, Any]) -> bool:
    return (item.get("state") == "generating"
            and (item.get("_tool") is True or _record_detail(item).strip() == QQ_TOOL_DETAIL))


def qq_evidence(live: dict[str, Any], panel: dict[str, Any] | None, *,
                now: float) -> EvidenceSnapshot:
    """Project QQ stages from recent, specific controller evidence.

    A scoped native mention carries a fixed enqueue marker. Generic queued
    work and process busy flags do not prove chat activity.
    """
    qq = (live.get("qq") or {}) if isinstance(live, dict) else {}
    degraded = qq.get("service_running") is True

    records = (panel or {}).get("runtimeRecords")
    latest: dict[str, Any] | None = None
    latest_notice: dict[str, Any] | None = None
    if isinstance(records, list):
        for item in records:
            if not isinstance(item, dict):
                continue
            ts = item.get("timestamp")
            if isinstance(ts, bool) or not isinstance(ts, (int, float)):
                continue
            if qq_fallback_notice(item):
                if latest_notice is None or ts >= latest_notice["timestamp"]:
                    latest_notice = item
                continue
            if latest is None or float(ts) >= float(latest.get("timestamp") or 0):
                latest = item
    if latest_notice is not None and (latest is None or latest_notice["timestamp"] >= latest["timestamp"]):
        notice_age = _age({"time": latest_notice["timestamp"]}, now)
        # Only the separate OneBot receipt can prove delivery. A notice
        # without it still ends the previous received/thinking animation.
        if notice_age is None or notice_age > SENT_WINDOW_SEC or not qq_confirmed_chat(latest):
            latest = latest_notice
    if latest is not None:
        age = _age({"time": latest.get("timestamp")}, now)
        state = str(latest.get("state") or "")
        if (age is not None and state == "queued" and qq_native_chat_ingress(latest)
                and age <= QQ_RECEIVED_WINDOW_SEC):
            return EvidenceSnapshot("qq", "received", EVIDENCE_VERIFIED,
                                    reason="native_chat_queued", at=float(latest["timestamp"]))
        if age is not None and age <= QQ_TOOL_WINDOW and qq_tool_running(latest):
            return EvidenceSnapshot("qq", "tool", EVIDENCE_VERIFIED,
                                    reason="active_tool", at=float(latest["timestamp"]),
                                    expires_at=float(latest["timestamp"]) + QQ_TOOL_WINDOW)
        if age is not None and qq_active_chat(latest):
            _, phase, window = QQ_ACTIVE_CHAT[state]
            if age <= window:
                return EvidenceSnapshot("qq", phase, EVIDENCE_VERIFIED,
                                        reason="active_chat", at=float(latest["timestamp"]),
                                        expires_at=float(latest["timestamp"]) + window)
        if age is not None and age <= SENT_WINDOW_SEC:
            if state == "confirmed":
                # confirmed splits in two: only a chat receipt counts as sent.
                if qq_confirmed_chat(latest):
                    return EvidenceSnapshot("qq", "sent", EVIDENCE_VERIFIED,
                                            reason="chat_receipt", at=float(latest["timestamp"]))
                return EvidenceSnapshot("qq", "idle", EVIDENCE_DEGRADED,
                                        reason="non_chat_confirm", at=now)
        if degraded:
            return EvidenceSnapshot("qq", "idle", EVIDENCE_DEGRADED,
                                    reason="record_not_recent", at=now)

    diag = qq.get("bridge_diagnostics")
    if isinstance(diag, dict):
        normalized = normalize_bridge_health(diag, now=now)
        if normalized in PHASES and normalized != "idle":
            return EvidenceSnapshot("qq", normalized, EVIDENCE_VERIFIED,
                                    reason="bridge_health", at=now)

    if degraded:
        return EvidenceSnapshot("qq", "idle", EVIDENCE_DEGRADED, reason="busy_is_not_chat", at=now)
    return EvidenceSnapshot("qq", "idle", EVIDENCE_VERIFIED, reason="no_source", at=now)


def pick_evidence(live: dict[str, Any], panel: dict[str, Any] | None, *,
                  now: float) -> EvidenceSnapshot:
    """Prefer a verified non-idle stage; the most recent source wins ties."""
    wx = wechat_evidence(live, now=now)
    qq = qq_evidence(live, panel, now=now)
    active = [candidate for candidate in (wx, qq)
              if candidate.evidence == EVIDENCE_VERIFIED and candidate.phase != "idle"]
    if active:
        return max(active, key=lambda candidate: candidate.at)
    return qq if qq.evidence == EVIDENCE_DEGRADED else wx


def pending_qq_ingress(panel: dict[str, Any] | None, *, after: float, now: float) -> float:
    """Recover a fresh native queue edge from the same bounded panel sample."""
    records = (panel or {}).get("runtimeRecords")
    if not isinstance(records, list):
        return 0.0
    newest_ingress = 0.0
    for row in records[:QQ_MAX_RECORDS]:
        if not isinstance(row, dict):
            continue
        stamp = row.get("timestamp")
        age = _age({"time": stamp}, now)
        if age is None or age > QQ_RECEIVED_WINDOW_SEC:
            continue
        if qq_native_chat_ingress(row):
            newest_ingress = max(newest_ingress, stamp)
        elif qq_confirmed_chat(row) or row.get("state") in ("failed", "uncertain", "cancelled", "skipped"):
            after = max(after, stamp)
    return newest_ingress if newest_ingress > after else 0.0


# --------------------------------------------------------------- the bridge
class LiveBridge:
    """Owns the client registry, seq counter and the 1 s evidence tick."""

    def __init__(self, router_factory: Callable[[], FlowRouter],
                 snapshot_getter: Callable[[], dict[str, Any]],
                 panel_reader: Callable[[], dict[str, Any] | None] | None = None,
                 *, tick: float = TICK_SECONDS):
        self._router_factory = router_factory
        self._snapshot_getter = snapshot_getter
        self._panel_reader = panel_reader
        self._tick = tick
        self._lock = threading.Lock()
        self._clients: dict[int, ClientQueue] = {}
        self._next_client_id = 0
        self._seq = 0
        self._router: FlowRouter | None = None
        self._router_checked = 0.0
        self._phase = "idle"
        self._source = "wechat"
        self._node_id = ""
        self._bubble = ""
        self._bubble_at = 0.0
        self._evidence = EVIDENCE_VERIFIED
        self._reason = ""
        # Dedupe key = (source, phase, real event timestamp).
        self._last_key: tuple[str, str, float] | None = None
        self._last_qq_ingress_at = 0.0
        self._last_ts = 0.0
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._wake = threading.Event()
        self.stats = {"accepted": 0, "rejected": 0, "frames_out": 0, "overflow": 0}

    # ------------------------------------------------------------- clients
    @property
    def client_count(self) -> int:
        with self._lock:
            return len(self._clients)

    def register(self) -> tuple[int, ClientQueue] | None:
        with self._lock:
            if len(self._clients) >= MAX_CLIENTS:
                self.stats["rejected"] += 1
                return None
            # Monotonic counter: id(object()) can be reused and collide,
            # so two concurrent clients could evict each other.
            self._next_client_id += 1
            key = self._next_client_id
            queue = ClientQueue()
            self._clients[key] = queue
            self.stats["accepted"] += 1
            self._wake.set()
            return key, queue

    def unregister(self, key: int) -> None:
        with self._lock:
            queue = self._clients.pop(key, None)
        if queue is not None:
            queue.closed = True

    # --------------------------------------------------------------- state
    def _router_now(self) -> FlowRouter | None:
        now = time.monotonic()
        if self._router is None or now - self._router_checked > 30.0:
            try:
                self._router = self._router_factory()
            except Exception:
                self._router = self._router
            self._router_checked = now
        return self._router

    def next_seq(self) -> int:
        with self._lock:
            # Browsers also compare sequences across a disconnected/restarted
            # house. Use wall-clock milliseconds as a floor, still JS-safe.
            self._seq = max(self._seq + 1, time.time_ns() // 1_000_000)
            return self._seq

    def current_frame_kind(self) -> str:
        return "state"

    def snapshot_frames(self) -> list[dict[str, Any]]:
        """First-connect / reconnect snapshot: config then state, both current."""
        router = self._router_now()
        frames: list[dict[str, Any]] = []
        if router is not None:
            try:
                from backend.flow_bridge import config_message
                frames.append(self._wrap("config", None, config_message(router)))
            except Exception:
                pass
        frames.append(self.state_snapshot())
        return frames

    def state_snapshot(self) -> dict[str, Any]:
        """Read current evidence without consuming the next broadcast transition."""
        evidence, payload = self.evaluation(force=True)
        return self._wrap("state", evidence, payload)

    def _wrap(self, kind: str, evidence: EvidenceSnapshot | None,
              payload: dict[str, Any]) -> dict[str, Any]:
        source = evidence.source if evidence is not None else self._source
        phase = evidence.phase if evidence is not None else self._phase
        frame = {
            "type": kind,
            "seq": self.next_seq(),
            "ts": int(time.time()),
            "source": source,
            "phase": phase,
            "node_id": str(payload.get("node_id") or ""),
            "bubble_text": str(payload.get("bubble_text") or ""),
            "ttl_ms": int(payload.get("ttl_ms") or 0),
        }
        if (evidence is not None and evidence.evidence == EVIDENCE_VERIFIED
                and phase != "idle" and 0 < evidence.at <= time.time()):
            frame["event_at"] = evidence.at
            if evidence.expires_at > evidence.at:
                frame["expires_at"] = evidence.expires_at
        for key in ("orders", "theme_url", "theme_ok", "nodes", "videos",
                    "durations", "crossfade_ms", "show_bubble", "bubble_ms",
                    "evidence", "verified", "video", "loop", "label"):
            if key in payload:
                frame[key] = payload[key]
        return frame

    def _current_orders(self, router: FlowRouter, phase: str,
                        node_id: str) -> list[dict[str, Any]]:
        if router is None:
            return []
        return router.orders(phase, current=node_id or None)

    def publish(self, frames: list[dict[str, Any]]) -> None:
        with self._lock:
            targets = list(self._clients.items())
        for key, queue in targets:
            for frame in frames:
                if not queue.push(frame):
                    if queue.overflow:
                        with self._lock:
                            self._clients.pop(key, None)
                        queue.closed = True
                        self.stats["overflow"] += 1
                    break

    def evaluation(self, *, force: bool = False, preserve_qq_ingress: bool = False) -> tuple[EvidenceSnapshot, dict[str, Any]]:
        """Read evidence once and turn it into the current state payload."""
        router = self._router_now()
        now = time.time()
        live: dict[str, Any] = {}
        try:
            live = self._snapshot_getter() or {}
        except Exception:
            live = {}
        panel: dict[str, Any] | None = None
        if self._panel_reader is not None:
            try:
                panel = self._panel_reader()
            except Exception:
                panel = None

        evidence = pick_evidence(live, panel, now=now)
        if preserve_qq_ingress and evidence.source == "qq" and evidence.reason in ("active_chat", "active_tool"):
            ingress_at = pending_qq_ingress(panel, after=self._last_qq_ingress_at, now=now)
            if ingress_at:
                evidence = EvidenceSnapshot("qq", "received", EVIDENCE_VERIFIED,
                                            reason="native_chat_queued", at=ingress_at)
        phase = phase_for_source_snapshot(evidence.phase)

        if evidence.evidence == EVIDENCE_VERIFIED and phase != "idle":
            orders = self._current_orders(router, phase, self._node_id)
            node_id = str(orders[-1]["node_id"]) if orders else self._node_id
        elif phase == "idle":
            orders = self._current_orders(router, "idle", self._node_id)
            node_id = (str(orders[0]["node_id"]) if orders else
                       (router.flow.wait_node() or {}).get("id", "") if router else "")
        else:
            orders = []
            node_id = self._node_id

        text, _ = bubble_for(phase)
        if ((phase, evidence.source) == (self._phase, self._source) and not force
                and bubble_expired(self._bubble, self._bubble_at, now, BUBBLE_TTL_MS)):
            text = ""

        ttl_ms = LIVE_TTL_MS if phase != "idle" else 0
        if phase != "idle" and evidence.expires_at:
            ttl_ms = max(1, round((evidence.expires_at - now) * 1000))
        payload = {
            "node_id": node_id,
            "bubble_text": text,
            "ttl_ms": ttl_ms,
            "orders": orders,
            "evidence": evidence.evidence,
            "verified": evidence.evidence == EVIDENCE_VERIFIED and phase != "idle",
            "label": PHASE_LABELS.get(phase, ""),
            "loop": PHASE_LOOP.get(phase, True),
        }
        if router is not None:
            payload["theme_url"] = router.theme_url
            payload["theme_ok"] = bool(router.ok)
        return evidence, payload

    def frame(self, *, force: bool = False) -> dict[str, Any]:
        """Compute the current state frame; ``force`` bypasses Bubble TTL logic."""
        evidence, payload = self.evaluation(force=force)
        now = time.time()
        phase = payload and phase_for_source_snapshot(evidence.phase)
        if phase != self._phase or evidence.source != self._source:
            self._bubble_at = now
        self._phase = phase
        self._source = evidence.source
        self._node_id = str(payload["node_id"])
        self._bubble = str(payload["bubble_text"])
        self._evidence = evidence.evidence
        self._reason = evidence.reason
        return self._wrap("state", evidence, payload)

    def dedupe_key(self, evidence: EvidenceSnapshot) -> tuple[str, str, float]:
        """Dedupe on the real event identity, not on the poll cadence."""
        phase = phase_for_source_snapshot(evidence.phase)
        stamp = float(evidence.at or 0.0)
        if phase == "idle" and not self._bubble:
            stamp = 0.0
        return (evidence.source, phase, stamp)

    def produce(self) -> list[dict[str, Any]]:
        """One poll result -> only the frames that are genuinely new.

        A ``state`` frame carries a real phase change and is always paired with
        an independent ``flow_goto`` naming the exact theme node, so the client
        crossfades once instead of being re-triggered every tick. A bubble that
        expires is its own single ``bubble`` frame. Nothing repeats.
        """
        evidence, payload = self.evaluation(preserve_qq_ingress=True)
        if evidence.source == "qq" and evidence.reason == "native_chat_queued":
            self._last_qq_ingress_at = max(self._last_qq_ingress_at, evidence.at)
        key = self.dedupe_key(evidence)
        frames: list[dict[str, Any]] = []
        bubble_now = str(payload.get("bubble_text") or "")
        phase = phase_for_source_snapshot(evidence.phase)

        # 1) 阶段真的变了：先 flow_goto（换片），再 state（权威快照）。
        if key != self._last_key:
            new_receipt = (phase == "sent" and key[2] > 0 and self._last_key is not None
                           and self._last_key[:2] == key[:2])
            self._last_key = key
            self._last_ts = evidence.at or 0.0
            if phase != self._phase or evidence.source != self._source or new_receipt:
                self._phase = phase
                self._source = evidence.source
                self._node_id = str(payload["node_id"])
                self._bubble = bubble_now
                self._bubble_at = time.time()
                self._evidence = evidence.evidence
                self._reason = evidence.reason
                goto = payload.get("orders") or []
                first = goto[0] if goto else None
                if first and first.get("video"):
                    # flow_goto 指「这一步要播的那一段」：收到消息先播拿手机
                    # 前段，回完消息先播放下手机段；state.node_id 仍是落点。
                    frames.append(self._wrap("flow_goto", evidence, {
                        "node_id": first["node_id"],
                        "video": first["video"],
                        "loop": bool(first.get("loop")),
                        "bubble_text": bubble_now,
                        "ttl_ms": int(payload["ttl_ms"]),
                        "orders": goto,
                    }))
                frames.append(self._wrap("state", evidence, payload))
                return frames

        # 2) 同一阶段内气泡到期：只发一次清空，不重复。
        if self._bubble and not bubble_now:
            self._bubble = ""
            frames.append(self._wrap("bubble", evidence,
                                     {"node_id": payload["node_id"],
                                      "bubble_text": "", "ttl_ms": 0}))
        return frames

    def tick_once(self) -> None:
        """One poll tick: only when viewers exist, only real changes fan out."""
        if self.client_count == 0:
            return
        try:
            frames = self.produce()
        except Exception:
            return
        if frames:
            self.publish(frames)

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="fish-house-ws", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        while not self._stop.is_set():
            self._wake.clear()
            if self.client_count == 0:
                # Retain the low-rate idle sleep; a new viewer wakes this loop.
                self._wake.wait(1.0)
                continue
            if self._stop.wait(self._tick):
                break
            try:
                self.tick_once()
            except Exception:
                continue

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=2.0)
        self._thread = None
        with self._lock:
            clients = list(self._clients.values())
            self._clients.clear()
        for queue in clients:
            queue.closed = True


# ------------------------------------------------------------- ws connection
class ClientConnection:
    """Per-socket loop: read bounded client frames, write queued server frames.

    Reads never block the tick: the socket is polled non-blocking and a frame is
    only assembled once every byte of it is already buffered.
    """

    def __init__(self, sock: socket.socket, queue: ClientQueue, *, rfile,
                 timeout: float = TICK_SECONDS):
        self.sock = sock
        self.queue = queue
        self.rfile = rfile
        self.timeout = timeout
        self.last_ping = time.monotonic()
        self.awaiting_pong = False
        self.pending = 0
        self.buffer = bytearray()
        self.closed = False

    def send(self, payload: bytes) -> bool:
        try:
            self.sock.sendall(payload)
            return True
        except OSError:
            self.closed = True
            return False

    def flush(self) -> None:
        for payload in self.queue.drain():
            if not self.send(encode_frame(OPCODE_TEXT, payload)):
                return

    # ------------------------------------------------------- framing input
    def _take(self, count: int) -> bytes | None:
        if len(self.buffer) < count:
            return None
        head = bytes(self.buffer[:count])
        del self.buffer[:count]
        return head

    def try_parse(self) -> list[tuple[int, bytes]]:
        """Extract every complete client frame already buffered."""
        frames: list[tuple[int, bytes]] = []
        while True:
            if len(self.buffer) < 2:
                return frames
            start = bytes(self.buffer[:2])
            fin = bool(start[0] & 0x80)
            opcode = start[0] & 0x0F
            if start[0] & 0x70:
                raise WSError("reserved bits set")
            masked = bool(start[1] & 0x80)
            length = start[1] & 0x7F
            offset = 2
            if length == 126:
                if len(self.buffer) < 4:
                    return frames
                length = struct.unpack("!H", bytes(self.buffer[2:4]))[0]
                offset = 4
            elif length == 127:
                if len(self.buffer) < 10:
                    return frames
                length = struct.unpack("!Q", bytes(self.buffer[2:10]))[0]
                offset = 10
            if opcode >= 0x8:
                if not fin:
                    raise WSError("fragmented control frame")
                if length > 125:
                    raise WSError("control frame too large")
            if not masked:
                raise WSError("client frames must be masked")
            if length > MAX_MESSAGE_BYTES:
                raise WSError("frame too large")
            if len(self.buffer) < offset + 4 + length:
                return frames
            mask = bytes(self.buffer[offset:offset + 4])
            body = bytes(self.buffer[offset + 4:offset + 4 + length])
            del self.buffer[:offset + 4 + length]
            frames.append((opcode | (0x80 if fin else 0),
                           bytes(byte ^ mask[index % 4] for index, byte in enumerate(body))))

    def handle_frame(self, frame: tuple[int, bytes]) -> None:
        head, payload = frame
        opcode = head & 0x0F
        fin = bool(head & 0x80)
        if opcode == OPCODE_CLOSE:
            self.send(encode_frame(OPCODE_CLOSE, payload[:125]))
            self.closed = True
            return
        if opcode == OPCODE_PING:
            self.send(encode_frame(OPCODE_PONG, payload[:125]))
            return
        if opcode == OPCODE_PONG:
            self.awaiting_pong = False
            return
        if opcode in (OPCODE_TEXT, OPCODE_BINARY, OPCODE_CONT):
            if fin:
                self.pending = 0
            else:
                self.pending += len(payload)
                if self.pending > MAX_MESSAGE_BYTES:
                    raise WSError("message too large")
            return
        raise WSError("unknown opcode")

    def wait_readable(self, timeout: float = 0.25) -> bool:
        """select-based wait, then drain whatever is already available.

        A previous blocking read can leave ``rfile`` in a stale timeout state
        where a later read never returns. ``select`` on the raw socket cannot,
        and the socket is left non-blocking so a read only returns real bytes.
        """
        if self.closed:
            return False
        try:
            self.sock.setblocking(False)
        except OSError:
            self.closed = True
            return False
        try:
            ready, _, _ = select.select([self.sock], [], [], timeout)
        except (OSError, ValueError):
            self.closed = True
            return False
        if ready:
            self.drain_available()
        self.check_heartbeat()
        return not self.closed

    def drain_available(self) -> None:
        """Read at most one bounded chunk; never blocks, never loops forever."""
        try:
            chunk = self.sock.recv(4096)
        except (BlockingIOError, InterruptedError):
            return
        except OSError:
            self.closed = True
            return
        if not chunk:
            self.closed = True
            return
        if len(self.buffer) + len(chunk) > MAX_MESSAGE_BYTES + 14:
            self.closed = True
            return
        self.buffer.extend(chunk)
        for frame in self.try_parse():
            self.handle_frame(frame)
            if self.closed:
                return

    def check_heartbeat(self) -> None:
        now = time.monotonic()
        if self.awaiting_pong and now - self.last_ping > PONG_TIMEOUT:
            self.closed = True
            return
        if now - self.last_ping >= PING_INTERVAL:
            self.awaiting_pong = True
            self.last_ping = now
            self.send(encode_frame(OPCODE_PING, b""))

    def step_read(self) -> None:
        """Compatibility shim: one bounded, non-blocking read pass."""
        self.wait_readable(0.05)


def compute_accept(key: str) -> str:
    return base64.b64encode(hashlib.sha1(key.encode("latin-1") + WS_GUID).digest()).decode("ascii")


def validate_key(value: str | None) -> str:
    if not value:
        raise WSError("missing key")
    try:
        decoded = base64.b64decode(value.encode("latin-1"), validate=True)
    except (binascii.Error, ValueError, UnicodeEncodeError):
        raise WSError("bad key")
    if len(decoded) != 16:
        raise WSError("bad key length")
    return value


def is_loopback(host: str) -> bool:
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return host.lower() == "localhost"


__all__ = [
    "LiveBridge", "EvidenceSnapshot", "WSError", "WS_PATH", "MAX_CLIENTS",
    "MAX_FRAME_BYTES", "MAX_MESSAGE_BYTES", "encode_frame", "read_frame",
    "compute_accept", "validate_key", "parse_request", "origin_allowed",
    "http_response", "wechat_evidence", "qq_evidence", "pick_evidence",
    "EVIDENCE_VERIFIED", "EVIDENCE_DEGRADED",
]
