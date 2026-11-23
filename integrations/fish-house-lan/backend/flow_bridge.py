"""Read-only bridge between the live house phases and the upstream theme flow.

Upstream reference (jiuchai/fish-house, read-only, no code copied):
``assets/default_theme.zip`` ships a ``theme.json`` whose top level is
``schema`` / ``meta`` / ``config``; ``config.flow`` holds ``nodes`` of
``{id, label, video, loop, role, duration, media_id}`` and ``edges`` of
``{from, to}``. In the shipped default theme the roles are ``wait`` (looping
rest), ``phone`` (looping phone use) and ``both`` (looping desk work).

Nothing here hard-codes a node id: the busy segment is discovered by walking
the graph from the ``wait`` node to the *key* node, and the put-down segment by
walking from the key node back to ``wait``. Only ``role`` is treated as shared
vocabulary, and a theme without roles still resolves through a conservative
fallback.

Market themes such as 「小肥鱼的零食」 ship no ``phone`` role at all: their graph
is ``wait_1(loop) → action → both_done → action → wait_1``. When no ``phone``
role exists the key node is therefore the reachable ``both_done`` (or ``work``)
node that loops the shortest round trip back to ``wait``; when even that is
missing a plain cycle through the waiting node is enough. The segments are still
derived from the real edges, so a quick ``received → sent`` walk stays ordered
(收消息 → 干活 → 放下手机 → 等待) instead of jumping.

Deployment-side theme selection is opt-in and static: ``web/fish_house/
live.theme.json`` may name one same-origin theme URL. It is validated here
(single relative path, strict slug, readable theme, resolvable flow) and any
failure falls back to the fixed default theme, so an unset or broken pointer can
never break the house.

This module performs no I/O except reading the theme JSON / selection JSON
passed in by the caller's resolved paths. It never reads logs, message text,
group numbers, diagnostics or credentials.
"""

from __future__ import annotations

import json
import re
from collections import deque
from pathlib import Path
from typing import Any

# ------------------------------------------------------------------ contracts

PHASES = ("idle", "received", "thinking", "replying", "sent", "error")
SOURCES = ("qq", "wechat")

DEFAULT_THEME_URL = "/fish_house/themes/default/theme.json"

# 收到的消息 → 拿手机；在想在回 → 看手机；发出/出错 → 放下/待机。
PHASE_INTENTS = {
    "idle": "wait",
    "received": "pickup",
    "thinking": "phone",
    "replying": "phone",
    "sent": "putdown",
    "error": "wait",
}
# 前端切片的语义：loop=True 的节点一直循环，False 的节点播一轮后交还。
PHASE_LOOP = {
    "idle": True,
    "received": False,
    "thinking": True,
    "replying": True,
    "sent": False,
    "error": True,
}
PHASE_LABELS = {
    "idle": "待机",
    "received": "收到消息，拿起手机",
    "thinking": "正在想怎么回",
    "replying": "正在回复",
    "sent": "回复发出，放下手机",
    "error": "这次没发出去",
}
# 固定中文短气泡，只描述流程本身，绝不带任何正文、群号、trace 或凭据。
PHASE_BUBBLES = {
    "idle": "",
    "received": "来消息啦",
    "thinking": "让我想想怎么回",
    "replying": "正在打字…",
    "sent": "回好啦",
    "error": "这条没发出去",
}
BUBBLE_TTL_MS = 3000
MAX_BUBBLE_CHARS = 24
MAX_BUBBLE_MS = 60_000

NODE_ID_RE = re.compile(r"[A-Za-z0-9_.-]{1,64}")
# 这些角色的片段天然只播一次（「两边都干完了」「把手机放下」），
# 任何阶段提示都不能把它们钉成循环，否则会卡在同一段上。
ONE_SHOT_ROLES = frozenset({"both_done", "work_done", "transition"})


def phase_for_source_snapshot(source: str) -> str:
    """Map a source snapshot phase to the shared phase vocabulary."""
    phase = str(source or "").strip().lower()
    return phase if phase in PHASES else "idle"


def bubble_for(phase: str, ttl_ms: int = BUBBLE_TTL_MS) -> tuple[str, int]:
    """Fixed short Chinese bubble plus its TTL; unknown phases stay silent."""
    if phase not in PHASES:
        return "", 0
    text = PHASE_BUBBLES.get(phase, "")[:MAX_BUBBLE_CHARS]
    if not text:
        return "", 0
    try:
        ttl = int(ttl_ms)
    except (TypeError, ValueError):
        ttl = BUBBLE_TTL_MS
    ttl = max(0, min(ttl, MAX_BUBBLE_MS))
    return text, ttl


def bubble_expired(bubble: str, shown_at: float, now: float, ttl_ms: int) -> bool:
    """A bubble must fall back to empty once its TTL elapses (no stale text)."""
    if not bubble:
        return False
    try:
        ttl = max(0, min(int(ttl_ms), MAX_BUBBLE_MS))
        age_ms = (float(now) - float(shown_at)) * 1000.0
    except (TypeError, ValueError, OverflowError):
        return True
    return age_ms >= ttl


def sanitize_bubble(text: Any) -> str:
    """Keep a caller-supplied bubble short, single line and credential-free."""
    value = " ".join(str(text or "").split())
    if not value:
        return ""
    # 主题自带的提示词可能含路径/URL；统一抹掉，只留一句话。
    value = re.sub(r"(?i)(?:https?|wss?|file)://\S+", "", value)
    value = re.sub(r"(?:/Users/|/home/|[A-Za-z]:\\)\S+", "", value)
    return value.strip()[:MAX_BUBBLE_CHARS]


# ------------------------------------------------------------------ theme load


def resolve_theme_path(root: Path) -> Path | None:
    """Fixed shared default: ``/fish_house/themes/default/theme.json``.

    ``root`` is the served ``fish_house`` directory. Missing or unreadable
    themes return None so callers can keep the conservative fallback.
    """
    candidate = Path(root) / "themes" / "default" / "theme.json"
    try:
        if candidate.is_file():
            return candidate
    except OSError:
        return None
    return None


# --------------------------------------------------------- opt-in selection
# 部署端同目录的静态小配置：只放一个同源主题 URL，不放任何主机/凭据/开关。
SELECTION_FILE = "live.theme.json"
SELECTION_MAX_BYTES = 4096
SLUG_RE = re.compile(r"[A-Za-z0-9_-]{1,32}")
THEME_ROUTE_TEMPLATE = "/fish_house/themes/%s/theme.json"
# 绝对主题路径 -> (slug, 服务端相对路径)。只认这一种形状，别的一律拒绝。
THEME_ROUTE_RE = re.compile(r"/fish_house/themes/([A-Za-z0-9_-]{1,32})/theme\.json")


def theme_slug_for_route(url: Any) -> str:
    """Return the slug of a strictly same-origin theme URL, else ``""``.

    Rejected on purpose: absolute URLs (``http://…``), protocol-relative ones
    (``//host/…``), anything with ``?query`` / ``#fragment``, escaped or
    backslash bytes, ``..`` traversal, and any path shape other than
    ``/fish_house/themes/<slug>/theme.json``.
    """
    raw = str(url or "")
    if not raw or len(raw) > 256:
        return ""
    if raw.startswith("//") or "://" in raw or "\\" in raw or "%" in raw:
        return ""
    if "?" in raw or "#" in raw:
        return ""
    match = THEME_ROUTE_RE.fullmatch(raw)
    if not match:
        return ""
    return match.group(1)


def selection_path(root: Path | None) -> Path | None:
    """Path of the deployment-side selection file, if the root is usable."""
    if root is None:
        return None
    return Path(root) / SELECTION_FILE


def read_theme_selection(root: Path | None) -> str:
    """Read the opt-in theme URL; anything missing or malformed yields ``""``.

    The file is tiny and read as UTF-8 JSON. Only the ``theme_url`` string is
    used: no other key is honoured, so it cannot smuggle behaviour.
    """
    path = selection_path(root)
    if path is None:
        return ""
    try:
        if not path.is_file() or path.stat().st_size > SELECTION_MAX_BYTES:
            return ""
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        return ""
    if not isinstance(doc, dict):
        return ""
    return str(doc.get("theme_url") or "")


def resolve_selection(root: Path | None) -> dict[str, Any]:
    """The one place that decides which theme this deployment serves.

    Returns ``{"theme_url", "theme_path", "slug", "selected", "fallback"}``.
    ``selected`` is True only when the opt-in pointer parsed, checked out and
    produced a *resolvable* flow; every other outcome is the fixed default so
    the house can never be broken by a bad pointer.
    """
    default_url = DEFAULT_THEME_URL
    default_path = resolve_theme_path(root) if root is not None else None
    slug = theme_slug_for_route(read_theme_selection(root))
    if slug:
        path = Path(root) / "themes" / slug / "theme.json" if root is not None else None
        if (path is not None and path.resolve().is_relative_to((Path(root) / "themes").resolve())
                and ThemeFlow(load_theme(path), root=root, theme_id=slug).ok):
            return {"theme_url": THEME_ROUTE_TEMPLATE % slug, "theme_path": path,
                    "slug": slug, "selected": True, "fallback": ""}
    return {"theme_url": default_url, "theme_path": default_path, "slug": "default",
            "selected": False,
            "fallback": "selection_invalid" if slug else "selection_absent"}


def load_theme(path: Path | None) -> dict[str, Any] | None:
    """Read and shape-check a theme document; anything unexpected returns None."""
    if path is None:
        return None
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        return None
    if not isinstance(raw, dict) or not isinstance(raw.get("config"), dict):
        return None
    flow = raw["config"].get("flow")
    if not isinstance(flow, dict):
        return None
    if not isinstance(flow.get("nodes"), list):
        return None
    return raw


def resolve_video_path(root: Path | None, raw: str, theme_id: str = "default") -> str:
    """Map a theme-relative video to the path this server can actually serve.

    Upstream themes reference ``videos/<name>.mp4`` inside the theme package;
    this repo keeps the same segments under ``videos/h264/<name>.mp4``. Only
    the file name is used and ``theme_id`` is a validated slug, so no theme can
    reach outside those three folders.
    """
    name = Path(str(raw or "")).name
    if not name.endswith(".mp4"):
        return ""
    if root is None:
        return str(raw or "")
    slug = str(theme_id or "default")
    if not SLUG_RE.fullmatch(slug):
        slug = "default"
    prefix = ("themes/default/" if slug == "default" else "themes/%s/" % slug)
    for relative in (prefix + "videos/%s" % name, "videos/h264/%s" % name):
        try:
            if (Path(root) / relative).is_file():
                return relative
        except OSError:
            continue
    return str(raw or "")


class ThemeFlow:
    """The minimal view of an upstream flow graph the house actually needs.

    Parsing follows the upstream normalizer's rules closely enough to agree on
    ids and edges, but never mutates or re-writes the theme.
    """

    def __init__(self, theme: dict[str, Any] | None, *, theme_url: str = DEFAULT_THEME_URL,
                 root: Path | None = None, theme_id: str = "default"):
        self.theme_url = str(theme_url or DEFAULT_THEME_URL)
        self.nodes: list[dict[str, Any]] = []
        self.edges: list[dict[str, Any]] = []
        self.ok = False
        self.root = root
        # 主题 slugs 是严格白名单；任何异常值都退化成 default 的素材目录。
        slug = str(theme_id or "default")
        self.theme_id = slug if SLUG_RE.fullmatch(slug) else "default"
        if isinstance(theme, dict):
            self._parse(theme)
        self.ok = self._resolvable()

    # -------------------------------------------------------------- parsing

    def _parse(self, theme: dict[str, Any]) -> None:
        config = theme.get("config") or {}
        flow = config.get("flow") or {}
        videos = config.get("videos") if isinstance(config.get("videos"), dict) else {}
        durations = config.get("durations") if isinstance(config.get("durations"), dict) else {}

        seen: set[str] = set()
        for item in flow.get("nodes") or []:
            if not isinstance(item, dict):
                continue
            nid = str(item.get("id") or "").strip()
            if not NODE_ID_RE.fullmatch(nid) or nid in seen:
                continue
            seen.add(nid)
            role = str(item.get("role") or "action")[:20]
            video = str(item.get("video") or "")
            if not video:
                # 与上游一致：节点没写视频时回落到 config.videos[<id>]。
                video = str(videos.get(nid) or "")
            duration = item.get("duration")
            try:
                duration = max(0.0, float(duration))
            except (TypeError, ValueError):
                duration = 0.0
            if duration <= 0:
                try:
                    duration = max(0.0, float(durations.get(nid) or 0))
                except (TypeError, ValueError):
                    duration = 0.0
            self.nodes.append({
                "id": nid,
                "label": str(item.get("label") or nid)[:40],
                "video": video,
                "loop": False if role in {"both_done", "work_done"} else bool(item.get("loop")),
                "role": role,
                "duration": duration,
                "media_id": str(item.get("media_id") or ""),
            })

        used: set[str] = set()
        for item in flow.get("edges") or []:
            if not isinstance(item, dict):
                continue
            src = str(item.get("from") or "")
            dst = str(item.get("to") or "")
            if src not in seen or dst not in seen:
                continue
            eid = str(item.get("id") or f"{src}->{dst}")
            if eid in used:
                eid = f"{eid}-{len(used)}"
            used.add(eid)
            self.edges.append({"id": eid, "from": src, "to": dst,
                               "label": str(item.get("label") or "")[:120]})

    # --------------------------------------------------------------- lookups

    def node(self, node_id: str) -> dict[str, Any] | None:
        for item in self.nodes:
            if item["id"] == node_id:
                return item
        return None

    def outgoing(self, node_id: str) -> list[dict[str, Any]]:
        return [item for item in self.edges if item["from"] == node_id]

    def _nodes_with_role(self, role: str) -> list[dict[str, Any]]:
        return [item for item in self.nodes if item["role"] == role]

    def _resolvable(self) -> bool:
        return bool(self.nodes) and bool(self._nodes_with_role("wait"))

    # ------------------------------------------------------------ waypoints

    def wait_node(self) -> dict[str, Any] | None:
        """Idle waypoint: first looping wait node, else first wait node."""
        waits = self._nodes_with_role("wait")
        looping = [item for item in waits if item["loop"]]
        if looping:
            return looping[0]
        if waits:
            return waits[0]
        # 没有 wait 角色时的保守回落：第一个循环节点。
        looping = [item for item in self.nodes if item["loop"]]
        return looping[0] if looping else (self.nodes[0] if self.nodes else None)

    def phone_node(self) -> dict[str, Any] | None:
        phones = self._nodes_with_role("phone")
        if phones:
            looping = [item for item in phones if item["loop"]]
            return looping[0] if looping else phones[0]
        return None

    def work_node(self) -> dict[str, Any] | None:
        for item in self.nodes:
            if item["role"] in {"both", "work"}:
                return item
        return None

    def both_done_node(self) -> dict[str, Any] | None:
        """First node the market themes mark as "both sides are done"."""
        nodes = self._nodes_with_role("both_done")
        return nodes[0] if nodes else None

    def key_node(self) -> dict[str, Any] | None:
        """The node the house sits on while it is busy, discovered from the graph.

        Preference order, all derived from ``role`` + edges instead of ids:

        1. a ``phone`` node (the upstream default theme);
        2. a ``phone``/``both_done``/``both``/``work`` action node that is
           reachable from ``wait`` *and* can walk back to ``wait`` — this is the
           only way a market theme such as 「小肥鱼的零食」 becomes playable;
        3. the first non-empty cycle through ``wait`` (a theme of plain
           ``action`` nodes still loops);
        4. nothing — the caller then keeps the conservative wait loop.

        A key node equal to ``wait`` is never returned: it carries no busy state.
        """
        wait = self.wait_node()
        phone = self.phone_node()
        if phone is not None:
            return phone
        if wait is None:
            for role in ("both_done", "both", "work"):
                nodes = self._nodes_with_role(role)
                if nodes:
                    return nodes[0]
            return None

        candidates: list[dict[str, Any]] = []
        for node in self.nodes:
            if node["id"] == wait["id"]:
                continue
            if self.path(wait["id"], node["id"]) and self.path(node["id"], wait["id"]):
                candidates.append(node)

        def rank(node: dict[str, Any]) -> tuple[int, int, int]:
            role_first = {"phone": 0, "both_done": 1, "both": 2, "work": 3}.get(node["role"], 4)
            # 短回路优先：来回都最短的那个节点才是这条图真正的「忙碌位」。
            hops = (len(self.path(wait["id"], node["id"]) or []) - 1
                    + len(self.path(node["id"], wait["id"]) or []) - 1)
            return (role_first, hops, -1 if node["loop"] else 0)

        ranked = sorted(candidates, key=rank)
        if ranked:
            return ranked[0]
        # 兜底：wait 之外第一个可达的节点，只要它还能回到 wait。
        for edge in self.outgoing(wait["id"]):
            node = self.node(edge["to"])
            if node is not None and self.path(node["id"], wait["id"]):
                return node
        return None

    # ----------------------------------------------------------- graph walk

    def path(self, start: str, target: str, depth: int = 12) -> list[str] | None:
        """Shortest edge path from ``start`` to ``target`` (inclusive)."""
        if not start or not target:
            return None
        if start == target:
            return [start]
        prev: dict[str, str | None] = {start: None}
        queue: deque[str] = deque([start])
        while queue:
            if len(prev) > 200:
                return None
            current = queue.popleft()
            for edge in self.outgoing(current):
                nxt = edge["to"]
                if not nxt or nxt in prev:
                    continue
                prev[nxt] = current
                if nxt == target:
                    chain = [nxt]
                    node = nxt
                    while prev[node] is not None:
                        node = prev[node]
                        chain.append(node)
                    return chain[::-1]
                queue.append(nxt)
        return None

    def first_step_toward(self, start: str, target: str, depth: int = 4) -> str:
        """The single edge target to take first when heading for ``target``."""
        for edge in self.outgoing(start):
            first = edge["to"]
            if not first:
                continue
            if first == target:
                return first
            chain = self.path(first, target, depth=depth)
            if chain:
                return first
        return ""

    def segment(self, start: str, target: str, depth: int = 12) -> list[dict[str, Any]]:
        """Nodes to play when moving from ``start`` to ``target``.

        The start node is never replayed. A one-step move yields just the
        target; a multi-hop move yields the intermediate transitions plus the
        target, so the house walks the real cut instead of jumping.
        """
        chain = self.path(start, target, depth=depth)
        if chain is None or len(chain) < 2:
            return []
        return [node for node in (self.node(nid) for nid in chain[1:]) if node is not None]

    # ------------------------------------------------------------- playback

    def clip(self, node: dict[str, Any], intent: str) -> dict[str, Any]:
        """One playback payload for an order item."""
        loop = bool(PHASE_LOOP.get(intent, node["loop"]))
        if node.get("role") in ONE_SHOT_ROLES:
            loop = False
        return {
            "node_id": node["id"],
            "video": resolve_video_path(self.root, node["video"], self.theme_id),
            "loop": loop,
            "duration": round(float(node.get("duration") or 0.0), 3),
        }


class FlowRouter:
    """Turn a verified source phase into house playback orders.

    ``route`` never invents evidence: it only decides *which* existing theme
    node expresses an already-verified phase, and it degrades to an empty order
    list (front end keeps the current clip) whenever the graph cannot prove the
    next step.

    The busy seat is the graph-derived key node (:meth:`ThemeFlow.key_node`),
    never a hard-coded id. Walked segments are returned in *play order* — the
    order the cut must be shown — so a quick ``received → sent`` still plays
    收到消息 → 干活 → 放下手机 → 等待 without repeating a clip. Only the single
    hop onto an already-busy node keeps the legacy ``PHASE_LOOP`` semantics,
    which is what the default theme's looping ``phone``/``both`` clips rely on.
    """

    def __init__(self, flow: ThemeFlow, *, theme_url: str = DEFAULT_THEME_URL):
        self.flow = flow
        self.theme_url = theme_url

    @property
    def ok(self) -> bool:
        return self.flow.ok

    def _walk(self, start: dict[str, Any], target: dict[str, Any],
              intent: str) -> list[dict[str, Any]]:
        """Orders for walking ``start`` → ``target`` and then staying on target.

        The walked nodes are always one-shot (their own ``loop``), because a
        transition clip that loops would strand the house mid-cut. Returning to
        the waiting node therefore resumes the wait loop.
        """
        if start["id"] == target["id"]:
            return [self.flow.clip(target, intent)]
        chain = self.flow.segment(start["id"], target["id"])
        if not chain:
            return []
        return [self.flow.clip(node, intent) for node in chain]

    def _settle(self, target: dict[str, Any], phase: str,
                intent: str) -> list[dict[str, Any]]:
        """Stay on ``target``: honour its own loop instead of a phase guess.

        ``PHASE_LOOP`` is only a hint for the default theme's looping
        phone/computer clips; a one-shot market clip (``both_done``) must not be
        forced into a loop just because the phase is ``thinking``/``replying``,
        or the house would be stranded on a single clip forever.
        """
        clip = self.flow.clip(target, phase)
        keep = bool(target["loop"])
        hint = bool(PHASE_LOOP.get(intent, keep))
        clip["loop"] = keep if target["role"] in ONE_SHOT_ROLES else hint
        return [clip]

    def orders(self, phase: str, *, current: str | None = None) -> list[dict[str, Any]]:
        phase = phase if phase in PHASES else "idle"
        intent = PHASE_INTENTS[phase]
        wait = self.flow.wait_node()
        key = self.flow.key_node()
        if wait is None:
            return []
        here = current if self.flow.node(current or "") else (wait["id"] if current is None else None)
        if here is None:
            # 当前节点已不在新主题里：直接落到目标段起点，不再假装连续。
            here = wait["id"]
        start = self.flow.node(here) or wait

        if intent == "wait":
            if start["id"] == wait["id"]:
                return [self.flow.clip(wait, phase)]
            return self._walk(start, wait, phase) or [self.flow.clip(wait, phase)]

        if key is None:
            # 没有任何可往返的忙碌节点：保守回到等待循环，绝不硬编码别的节点。
            return self.orders("idle", current=here) if here != wait["id"] else [self.flow.clip(wait, "idle")]

        if intent == "phone":
            # 已经在忙碌位上：沿用该节点自己的 loop（default 的手机/电脑循环），
            # 否则按图走过去，链尾就是忙碌位。
            if start["id"] == key["id"]:
                return self._settle(key, phase, intent)
            chain = self.flow.segment(start["id"], key["id"])
            if not chain:
                chain = self.flow.segment(wait["id"], key["id"])
            return [self.flow.clip(node, phase) for node in chain]

        if intent == "pickup":
            chain = self._walk(start, key, phase)
            if not chain and start["id"] != wait["id"]:
                chain = self._walk(wait, key, phase)
            return chain

        if intent == "putdown":
            if current is None or current == wait["id"]:
                # The first visible receipt can arrive between polls without an
                # earlier receive or thinking stage. Play its full round trip.
                pickup = self._walk(wait, key, phase)
                putdown = self._walk(key, wait, phase)
                if pickup and putdown:
                    return pickup + putdown
            chain = self._walk(start, wait, phase)
            if not chain and start["id"] != wait["id"]:
                chain = self._walk(wait, wait, phase)
            return chain

        return [self.flow.clip(wait, "idle")]


class FlowState:
    """Track the node the house is currently playing, without trusting the client."""

    def __init__(self, router: FlowRouter):
        self.router = router
        self.current: str | None = None

    def next_orders(self, phase: str) -> list[dict[str, Any]]:
        orders = self.router.orders(phase, current=self.current)
        if orders:
            self.current = str(orders[-1]["node_id"])
        return orders


def build_router(theme_root: Path | None, *, selection: dict[str, Any] | None = None) -> FlowRouter:
    """Build a router for the theme this deployment serves.

    The theme is the opt-in one named by ``live.theme.json`` when it validates,
    else the fixed default. Pass ``selection`` to reuse one decision (the server
    and the bridge must agree); otherwise it is resolved here.
    """
    choice = selection if isinstance(selection, dict) else resolve_selection(theme_root)
    theme_url = str(choice.get("theme_url") or DEFAULT_THEME_URL)
    slug = str(choice.get("slug") or "default")
    path = choice.get("theme_path")
    if path is None and theme_root is not None:
        path = resolve_theme_path(theme_root)
    return FlowRouter(ThemeFlow(load_theme(Path(path)) if path is not None else None,
                                theme_url=theme_url, root=theme_root, theme_id=slug),
                      theme_url=theme_url)


def config_message(router: FlowRouter, *, crossfade_ms: int = 400,
                   show_bubble: bool = True) -> dict[str, Any]:
    """Config contract: theme URL plus the resolved wait/phone/work ids."""
    flow = router.flow
    wait = flow.wait_node()
    phone = flow.phone_node()
    work = flow.work_node()
    key = flow.key_node()
    return {
        "theme_url": router.theme_url,
        "theme_ok": bool(flow.ok),
        "crossfade_ms": int(crossfade_ms),
        "show_bubble": bool(show_bubble),
        "bubble_ms": BUBBLE_TTL_MS,
        "nodes": {
            "wait": wait["id"] if wait else "",
            "phone": phone["id"] if phone else "",
            "work": work["id"] if work else "",
            # 「忙碌位」在图上的实际落点：市场主题没有 phone 角色时，
            # 它会是可往返 both_done / action 节点的 id。
            "key": key["id"] if key else "",
        },
        "videos": {item["id"]: resolve_video_path(flow.root, item["video"], flow.theme_id)
                   for item in flow.nodes if item["video"]},
        "durations": {item["id"]: round(float(item.get("duration") or 0.0), 3)
                      for item in flow.nodes},
    }
