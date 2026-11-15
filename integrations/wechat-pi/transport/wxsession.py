#!/usr/bin/env python3
"""wxsession.py — WeChat Linux session/conversation accessor via AT-SPI.

Observed accessibility-tree conventions (vary by client and desktop environment):
  * The session (conversation) list rows are AT-SPI `list item` nodes whose
    accessible NAME carries a 3-line payload:
        "<display name>\n<last message preview>\n<HH:MM>\n"
    Example (synthetic): 'Test Group\nMember: sample message\n12:25\n'
  * The tree is deep (~20 levels) and riddled with anonymous fillers, so a
    depth-limited scan misses content. We walk to depth 20 and filter by role.
  * Toolbar buttons (Weixin/Contacts/Favorites/Moments/Channels/Search/...)
    are `button` nodes with real extents — usable for navigation.
  * The app root is `application[name='wechat']`, window `frame[name='Weixin']`.

This module only READS. Opening a conversation is a click; typing/sending is in
wxsender.py and is gated.
"""
from __future__ import annotations

import json
import re
import subprocess
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import gi
gi.require_version("Atspi", "2.0")
from gi.repository import Atspi

MAX_DEPTH = 20
TIME_RE = re.compile(r"^(?:\d{1,2}:\d{2}|Yesterday\s+\d{1,2}:\d{2}|昨天.*|\d{4}-\d{1,2}-\d{1,2}.*)$")


# --------------------------------------------------------------------------- #
# Low-level helpers
# --------------------------------------------------------------------------- #
def get_app(name_sub: str = "wechat"):
    d = Atspi.get_desktop(0)
    for i in range(d.get_child_count()):
        a = d.get_child_at_index(i)
        if a and name_sub in (a.get_name() or "").lower():
            return a
    return None


def node_attr(node, which: str) -> str:
    try:
        if which == "name":
            return node.get_name() or ""
        if which == "desc":
            return node.get_description() or ""
        if which == "text":
            t = node.get_text_iface()
            if t is not None:
                n = t.get_character_count()
                if n:
                    return t.get_text(0, n) or ""
    except Exception:
        return ""
    return ""


def node_role(node) -> str:
    try:
        return node.get_role_name() or ""
    except Exception:
        return ""


def node_extents(node) -> Optional[Tuple[int, int, int, int]]:
    try:
        c = node.get_component_iface()
        if c is not None:
            e = c.get_extents(Atspi.CoordType.SCREEN)
            return (e.x, e.y, e.width, e.height)
    except Exception:
        pass
    return None


def node_actions(node) -> List[str]:
    out = []
    try:
        a = node.get_action_iface()
        if a is not None:
            for i in range(a.get_n_actions()):
                try:
                    out.append(a.get_action_name(i))
                except Exception:
                    out.append("?")
    except Exception:
        pass
    return out


def walk(node, depth: int = 0, max_depth: int = MAX_DEPTH):
    """Yield (path, node, depth) depth-first, bounded."""
    stack = [("0", node, 0)]
    visited = 0
    while stack:
        path, cur, dep = stack.pop()
        visited += 1
        if visited > 20000:
            return
        yield path, cur, dep
        if dep >= max_depth:
            continue
        try:
            ch = cur.get_child_count()
        except Exception:
            continue
        if ch and ch < 800:
            for i in range(ch - 1, -1, -1):
                try:
                    c = cur.get_child_at_index(i)
                except Exception:
                    continue
                if c is not None:
                    stack.append((f"{path}.{i}", c, dep + 1))


# --------------------------------------------------------------------------- #
# Data model
# --------------------------------------------------------------------------- #
@dataclass
class SessionRow:
    """One row in WeChat's left-hand session list."""
    room_name: str
    preview: str = ""
    time_text: str = ""
    path: str = ""
    extents: Optional[Tuple[int, int, int, int]] = None
    is_group: bool = False       # heuristic, refined by open()
    raw: str = ""

    def to_dict(self):
        return {"room_name": self.room_name, "preview": self.preview,
                "time": self.time_text, "path": self.path,
                "extents": list(self.extents) if self.extents else None,
                "is_group": self.is_group}


def parse_row_name(name: str) -> Optional[SessionRow]:
    """Parse a `list item` accessible name into a SessionRow.

    Expected shape: "<title>\\n<preview>\\n<HH:MM>\\n" (preview may be empty).
    """
    if not name or "\n" not in name:
        return None
    parts = [p for p in name.split("\n")]
    # drop trailing empties
    while parts and not parts[-1].strip():
        parts.pop()
    if len(parts) < 2:
        return None
    title = parts[0].strip()
    rest = parts[1:]
    time_text = ""
    if rest and TIME_RE.match(rest[-1].strip()):
        time_text = rest[-1].strip()
        rest = rest[:-1]
    preview = "\n".join(rest).strip()
    if not title:
        return None
    return SessionRow(room_name=title, preview=preview, time_text=time_text, raw=name)


# --------------------------------------------------------------------------- #
# The session reader
# --------------------------------------------------------------------------- #
class WeChatSession:
    """Read-only view of WeChat's session list + navigation."""

    GROUP_TIME_LIKE = re.compile(r"^\d{1,2}:\d{2}$")

    def __init__(self):
        self.app = None
        self.refresh()

    # -- discovery ---------------------------------------------------------
    def refresh(self):
        self.app = get_app()
        return self.app is not None

    def available(self) -> bool:
        return self.refresh()

    def is_login_screen(self) -> bool:
        if not self.available():
            return False
        for path, n, dep in walk(self.app, max_depth=10):
            nm = (node_attr(n, "name") or "").lower()
            if "scan to log in" in nm or "qr code" in nm:
                return True
        return False

    def is_locked(self) -> bool:
        if not self.available():
            return False
        for path, n, dep in walk(self.app, max_depth=10):
            if "locked" in (node_attr(n, "name") or "").lower():
                return True
        return False

    # -- session list ------------------------------------------------------
    def sessions(self) -> List[SessionRow]:
        """All session rows currently visible in the list."""
        if not self.available():
            return []
        out: List[SessionRow] = []
        seen = set()
        for path, n, dep in walk(self.app):
            role = node_role(n)
            if role != "list item":
                continue
            nm = node_attr(n, "name")
            row = parse_row_name(nm)
            if row is None:
                continue
            key = (row.room_name, row.time_text, row.preview[:40])
            if key in seen:
                continue
            seen.add(key)
            row.path = path
            row.extents = node_extents(n)
            # a group row almost always has a "Sender: text" preview
            row.is_group = bool(re.match(r"^[^:]{1,40}[:：]", row.preview or ""))
            out.append(row)
        return out

    def find_session(self, room_name: str) -> List[SessionRow]:
        return [s for s in self.sessions() if s.room_name == room_name]

    def same_name_groups(self, room_name: str) -> List[SessionRow]:
        """All rows sharing a display name — the ambiguity source."""
        return self.find_session(room_name)

    # -- navigation --------------------------------------------------------
    def open_session(self, row: SessionRow) -> bool:
        """Open a conversation by clicking its row.

        Delivery notes measured on WSLg:
          * There is NO window manager (`_NET_ACTIVE_WINDOW` is unsupported), so
            `xdotool windowactivate` FAILS and must not be relied on.
          * A plain `mousemove --sync` + `click 1` at absolute coordinates DOES
            reach WeChat. We click, then verify by re-reading the header.
        """
        if not row.extents:
            return False
        x, y, w, h = row.extents
        if w <= 0 or h <= 0:
            return False
        cx, cy = x + w // 2, y + h // 2
        for delay in (0.2, 0.4):
            subprocess.run(["xdotool", "mousemove", "--sync", str(cx), str(cy)], check=False)
            time.sleep(delay)
            subprocess.run(["xdotool", "mousedown", "1"], check=False)
            time.sleep(0.12)
            subprocess.run(["xdotool", "mouseup", "1"], check=False)
            time.sleep(0.9)
            # success = header now names this conversation
            if self.current_title() == row.room_name:
                return True
        return self.current_title() == row.room_name

    def press_nav(self, label: str) -> bool:
        """Click a navigation toolbar button by accessible label."""
        if not self.available():
            return False
        for path, n, dep in walk(self.app, max_depth=10):
            if node_role(n) == "button" and (node_attr(n, "name") or "").strip() == label:
                e = node_extents(n)
                if e and e[2] > 0:
                    cx, cy = e[0] + e[2] // 2, e[1] + e[3] // 2
                    subprocess.run(["xdotool", "mousemove", "--sync", str(cx), str(cy)], check=False)
                    subprocess.run(["xdotool", "click", "1"], check=False)
                    time.sleep(0.8)
                    return True
        return False

    # -- current conversation header ---------------------------------------
    def current_title(self) -> str:
        """Title of the currently open conversation.

        MEASURED: WeChat draws the chat header as an AT-SPI `text` node inside the
        right-hand pane with real extents, e.g. ('测试群', (300,500,500,80)).
        The frame name is always 'Weixin' and is useless for this purpose.
        """
        if not self.available():
            return ""
        best = None
        for path, n, dep in walk(self.app):
            if node_role(n) != "text":
                continue
            nm = node_attr(n, "name")
            e = node_extents(n)
            if not nm or not e:
                continue
            if e[0] > 250 and e[2] > 100:      # right pane, wide enough to be a header
                if best is None or e[1] < best[0]:
                    best = (e[1], nm)
        return best[1] if best else ""

    # -- message text near the conversation (for read-back) ----------------
    def conversation_payloads(self) -> List[str]:
        """All multi-line accessible names that look like message payloads.

        Used for read-back verification after sending: WeChat updates the
        session row preview to '<my name>: <my text>' once the message lands.
        """
        if not self.available():
            return []
        out = []
        for path, n, dep in walk(self.app):
            nm = node_attr(n, "name")
            if nm and "\n" in nm and len(nm) > 4:
                out.append(nm)
        return out


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def main(argv):
    ws = WeChatSession()
    if not ws.available():
        print("WECHAT_APP_NOT_FOUND")
        return 3
    print(f"login_screen={ws.is_login_screen()}  locked={ws.is_locked()}")
    rows = ws.sessions()
    print(f"sessions={len(rows)}")
    for r in rows:
        print(f"  [{('GROUP' if r.is_group else 'chat ')}] {r.room_name!r} "
              f"time={r.time_text!r} ext={r.extents}")
        if r.preview:
            print(f"        preview={r.preview[:80]!r}")
    dupes = {}
    for r in rows:
        dupes.setdefault(r.room_name, []).append(r)
    amb = {k: len(v) for k, v in dupes.items() if len(v) > 1}
    if amb:
        print(f"AMBIGUOUS_NAMES={amb}")
    if argv and argv[0] == "--json":
        print(json.dumps([r.to_dict() for r in rows], ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main(sys.argv[1:]))
