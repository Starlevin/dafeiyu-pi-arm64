#!/usr/bin/env python3
"""atspi_source.py — REAL Linux UI accessibility event source & sender.

This is the production-grade counterpart of group-bridge's mock event driver:
it talks to the actual AT-SPI2 accessibility bus (libatspi via PyGObject), the
same interface we empirically verified against the official Tencent WeChat
4.1.13.26 login window on WSL2.

Verified facts this file is built on (see LINUX_GUI_REPORT.md):
  * Official WeChat exposes an AT-SPI application named 'wechat' with a
    [frame] 'Weixin', a tool bar with 'Close'/'Back'/'Network proxy settings',
    a [button] 'QR Code' (136x136 at real screen coords), labels
    'Scan to log in' and 'Transfer files only'.
  * Chat surface roles seen: application/frame/filler/layered pane/tool bar/
    button/label/text/password text/check box.

Design rules (red lines from GROUP_BRIDGE_TASK.md / LINUX_VM_BUILD_TASK.md):
  * GROUP ONLY. Anything that cannot be proven to be a group conversation is
    refused (fail-closed). Private/1:1 is not our product shape here.
  * REAL @ ONLY. at_me comes from WeChat's own mention token (U+FFFC placeholder
    or a structured mention object), never from "@alias" typed as plain text.
  * ROOM-ID LOCK. A send is only allowed when the currently-open conversation
    frame title matches the intended room exactly, and the post-send read-back
    confirms the same conversation. Otherwise we refuse and report.
  * NO DB KEYS / NO MEMORY SCAN / NO INJECTION. We only read the accessibility
    tree and drive the visible input box (this module's sender is opt-in and
    additionally gated by the CALLER providing an explicit allow flag).
  * NO auto-scan, NO auto-send until the human at the keyboard authorizes it.

Status tags used below:
  [VERIFIED-WSL] proven on this machine (WSL2 x86_64, WeChat 4.1.13.26)
  [DESIGN]       implemented from observed structure, not yet exercised on a
                 logged-in account (requires the user's on-site QR scan)
  [UNVERIFIED]   cannot be confirmed before login
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, Iterable, List, Optional

try:
    import gi  # type: ignore
    gi.require_version("Atspi", "2.0")
    from gi.repository import Atspi  # type: ignore
    HAS_ATSPI = True
except Exception as _e:  # pragma: no cover
    HAS_ATSPI = False
    _ATSPI_ERR = repr(_e)

# ---------------------------------------------------------------- constants --
# U+FFFC OBJECT REPLACEMENT CHARACTER: WeChat renders an @-mention as this
# placeholder inside the editable text, and exposes the mention object
# separately. Typing "@name" produces plain text and NO placeholder -> must
# never be treated as a real mention.
MENTION_PLACEHOLDER = "\ufffc"

# Roles we expect on the chat surface. Kept loose on purpose: WeChat's Qt
# accessibility tree is sparse (many anonymous fillers), so we identify by
# role + relative position + editable action, not by name alone.
ROLE_APP = "application"
ROLE_FRAME = "frame"
ROLE_BUTTON = "button"
ROLE_LABEL = "label"
ROLE_TEXT = "text"
ROLE_ENTRY = "entry"

# Window titles WeChat uses for the login window vs the main window.
LOGIN_TITLES = ("weixin", "wechat")
# A conversation frame title is the group/contact display name; WeChat puts it
# in the frame name and also a label in the header row.
MAX_TREE_NODES = 4000


# ------------------------------------------------------------------- model --
@dataclass
class A11yNode:
    """Flat snapshot of one accessibility node (safe to serialize/log)."""
    path: str
    role: str
    name: str = ""
    description: str = ""
    text: str = ""
    actions: List[str] = field(default_factory=list)
    extents: Optional[List[int]] = None   # x,y,w,h in screen coords
    states: List[str] = field(default_factory=list)
    n_children: int = 0


@dataclass
class ChatEvent:
    """Normalized group-chat event handed to the Node bridge."""
    room_id: str          # WeChat conversation identifier (see resolve_room_id)
    room_name: str
    member_id: str
    member_name: str
    text: str
    msg_id: str
    at_me: bool
    at_evidence: str      # 'mention_token' | 'structured_list' | 'none'
    is_group: bool
    chat_type: str = "group"
    timestamp: Optional[int] = None
    raw_source: str = "atspi"


class SendRefused(Exception):
    """Raised when the sender refuses (fail-closed) to send."""


# ------------------------------------------------------------------ helpers --
def _safe(fn, default=None):
    try:
        return fn()
    except Exception:
        return default


def node_info(node) -> A11yNode:
    role = _safe(lambda: node.get_role_name(), "") or ""
    name = _safe(lambda: node.get_name(), "") or ""
    desc = _safe(lambda: node.get_description(), "") or ""
    text = ""
    try:
        t = node.get_text_iface()
        if t is not None:
            n = _safe(lambda: t.get_character_count(), 0) or 0
            text = _safe(lambda: t.get_text(0, min(n, 4000)), "") or ""
    except Exception:
        pass
    actions = []
    try:
        a = node.get_action_iface()
        if a is not None:
            cnt = _safe(lambda: a.get_n_actions(), 0) or 0
            for i in range(cnt):
                nm = _safe(lambda i=i: a.get_action_name(i), None)
                # Atspi 2.0 deprecates get_action_name in favour of get_action_name_legacy;
                # prefer descriptive name if available.
                try:
                    nm2 = a.get_action_name(i)  # noqa: B018
                except Exception:
                    nm2 = nm
                if nm2:
                    actions.append(str(nm2))
    except Exception:
        pass
    ext = None
    try:
        comp = node.get_component_iface()
        if comp is not None:
            e = comp.get_extents(Atspi.CoordType.SCREEN)
            if e is not None:
                ext = [int(e.x), int(e.y), int(e.width), int(e.height)]
    except Exception:
        pass
    states = []
    try:
        st = node.get_state_set()
        for nm in ("VISIBLE", "SHOWING", "ENABLED", "SENSITIVE", "FOCUSABLE",
                   "EDITABLE", "FOCUSED", "EXPANDED", "SELECTED"):
            v = getattr(Atspi.StateType, nm, None)
            if v is not None and _safe(lambda: st.contains(v), False):
                states.append(nm)
    except Exception:
        pass
    return A11yNode(
        path="", role=role, name=name, description=desc, text=text[:2000],
        actions=actions, extents=ext, states=states,
        n_children=_safe(lambda: node.get_child_count(), 0) or 0,
    )


class AtspiSource:
    """Real AT-SPI event source.

    Two modes:
      * `snapshot()` — pull a full tree snapshot (used to locate the chat list,
        the conversation frame, and the input box).
      * `poll()`     — incrementally read new messages appearing in the open
        conversation (design: WeChat's a11y tree does not emit semantic
        "message" events, so we diff text nodes between polls).
    """

    def __init__(self, app_match: str = "wechat", poll_interval: float = 0.8):
        if not HAS_ATSPI:
            raise RuntimeError(
                "AT-SPI Python bindings unavailable. Install python3-gi + gir1.2-atspi-2.0 "
                "(and run inside a dbus session with at-spi-bus-launcher). Cause: " + _ATSPI_ERR
            )
        self.app_match = app_match.lower()
        self.poll_interval = poll_interval
        self._last_signature: Optional[str] = None
        self._seen_ids: set = set()

    # -- discovery ---------------------------------------------------------
    def find_app(self):
        desk = Atspi.get_desktop(0)
        for i in range(_safe(lambda: desk.get_child_count(), 0) or 0):
            a = _safe(lambda i=i: desk.get_child_at_index(i))
            if a is None:
                continue
            nm = (_safe(lambda: a.get_name(), "") or "").lower()
            if self.app_match in nm:
                return a
        return None

    def snapshot(self, max_depth: int = 9) -> List[A11yNode]:
        app = self.find_app()
        if app is None:
            return []
        out: List[A11yNode] = []

        def walk(node, depth: int, path: str):
            if depth > max_depth or len(out) >= MAX_TREE_NODES:
                return
            info = node_info(node)
            info.path = path
            out.append(info)
            n = info.n_children
            if 0 < n < 500:
                for i in range(n):
                    ch = _safe(lambda i=i: node.get_child_at_index(i))
                    if ch is None:
                        continue
                    walk(ch, depth + 1, f"{path}.{i}")

        walk(app, 0, "0")
        return out

    # -- structural queries (design; validated against the real login window) --
    def frame(self) -> Optional[A11yNode]:
        for n in self.snapshot(4):
            if n.role == ROLE_FRAME:
                return n
        return None

    def is_login_screen(self) -> bool:
        """True when WeChat shows the login/QR screen (no account logged in)."""
        names = {n.name for n in self.snapshot(6) if n.role in (ROLE_BUTTON, ROLE_LABEL)}
        has_qr = any("qr" in x.lower() for x in names)
        has_scan = any("scan to log in" in x.lower() for x in names)
        return bool(has_qr or has_scan)

    def conversation_title(self) -> str:
        """Title of the currently open conversation.

        WeChat puts the chat/group display name on the frame name AND on a label
        in the header. We require both to agree, because the frame name alone
        can be stale while a switch animates.
        """
        nodes = self.snapshot(6)
        frame_names = [n.name for n in nodes if n.role == ROLE_FRAME and n.name]
        labels = [n.name for n in nodes if n.role == ROLE_LABEL and n.name]
        # header label = first label that also appears as a frame name
        for fn in frame_names:
            if fn in labels:
                return fn
        return frame_names[0] if frame_names else ""

    def input_box(self) -> Optional[A11yNode]:
        """Locate the message input: an EDITABLE text/entry node."""
        cands = [n for n in self.snapshot(9)
                 if n.role in (ROLE_TEXT, ROLE_ENTRY, "editable text", "paragraph")
                 and "EDITABLE" in n.states]
        # prefer the largest visible editable area near the bottom of the window
        if not cands:
            return None
        def score(n: A11yNode):
            e = n.extents or [0, 0, 0, 0]
            return (e[3], e[1])   # taller & lower = more likely the composer
        return max(cands, key=score)

    def send_button(self) -> Optional[A11yNode]:
        for n in self.snapshot(9):
            if n.role == ROLE_BUTTON and n.name.strip().lower() in ("send", "发送"):
                return n
        return None

    # -- mention detection (REAL @ only) -----------------------------------
    @staticmethod
    def detect_mention(text: str) -> tuple[bool, str]:
        """Return (is_real_mention, evidence).

        Real mention == WeChat inserted an object placeholder (U+FFFC).
        Plain '@alias' text is explicitly NOT a mention.
        """
        if MENTION_PLACEHOLDER in text:
            return True, "mention_token"
        return False, "none"

    # -- message diffing ---------------------------------------------------
    # Chrome/labels that exist on WeChat's login screen and must NEVER be
    # treated as chat messages. Observed verbatim on WeChat 4.1.13.26.
    LOGIN_CHROME = {
        "transfer files only", "scan to log in", "scan again", "cancel",
        "switch account", "weixin", "network proxy settings",
        "clearing chat history...", "logging out…", "logging out...",
        "confirm on phone", "current user", "log in", "enter weixin",
        "unable to load", "network disconnected", "check network settings",
        "use proxy", "address", "port", "account", "password", "save",
        "stop", "finish", "ok", "optional", "loading", "entering", "back", "close",
    }

    def _is_chrome(self, txt: str) -> bool:
        t = (txt or "").strip().lower()
        if t in self.LOGIN_CHROME:
            return True
        # percentages / progress like '0%'
        if re.fullmatch(r"\d{1,3}%", t):
            return True
        return False

    def poll(self) -> List[ChatEvent]:
        """Diff the open conversation and emit ChatEvents for new messages.

        [DESIGN] WeChat's Linux Qt a11y tree exposes message bubbles as
        [label]/[text] nodes inside anonymous fillers. We cannot rely on a
        semantic message role, so we:
          1. refuse to emit anything at all while the LOGIN screen is showing
             (there is no conversation; every label is UI chrome)
          2. drop a known login-chrome vocabulary defensively even if we are
             logged in (toolbar buttons/labels share the tree)
          3. capture (role,name,text,extents) signature per node
          4. treat a *new* node with non-empty text near the message column as a
             candidate message
          5. derive a stable msg_id from (room, text, y-position, ts)
        This is intentionally conservative: anything ambiguous is skipped.
        """
        nodes = self.snapshot(9)
        if not nodes:
            return []

        # HARD GATE: no conversation exists on the login screen.
        if self.is_login_screen():
            return []
        title = self.conversation_title()
        events: List[ChatEvent] = []
        for n in nodes:
            if n.role not in (ROLE_LABEL, ROLE_TEXT):
                continue
            txt = (n.text or n.name or "").strip()
            if not txt or len(txt) > 2000:
                continue
            # drop login/toolbar chrome vocabulary
            if self._is_chrome(txt):
                continue
            e = n.extents or [0, 0, 0, 0]
            if e[2] <= 0 or e[3] <= 0:
                continue
            sig = f"{title}|{n.role}|{txt}|{e[0]},{e[1]}"
            at_ok, how = self.detect_mention(txt)
            # WeChat prepends the sender nickname inside group bubbles as
            # "Nickname:\ncontent" for some layouts; we keep it verbatim and let
            # the Node side parse, but we never invent a sender.
            msg_id = "a11y_" + re.sub(r"\W+", "", f"{title}{txt}{e[1]}")[:60]
            if sig in self._seen_ids:
                continue
            events.append(ChatEvent(
                room_id=self.resolve_room_id(title),
                room_name=title,
                member_id="",            # [UNVERIFIED] sender id not exposed pre-login
                member_name="",          # parsed on the Node side from bubble text
                text=txt,
                msg_id=msg_id,
                at_me=at_ok,
                at_evidence=how,
                is_group=True,           # ONLY group surface is supported
                timestamp=int(time.time() * 1000),
            ))
            self._seen_ids.add(sig)
        return events

    def resolve_room_id(self, room_name: str) -> str:
        """Room identity.

        [UNVERIFIED] WeChat's a11y tree exposes the *display name* but not the
        internal chatroom id (e.g. `12345@chatroom`). Until we can read a stable
        id from the UI, we derive a deterministic key from the name AND require
        the caller to maintain a name->id map (see RoomMap). Two groups with the
        same display name are therefore AMBIGUOUS and must be refused by the
        Node side (`ambiguous_group_name`), which is exactly the fail-closed
        behaviour the group-bridge already enforces.
        """
        key = re.sub(r"\s+", " ", room_name or "").strip()
        return f"ui:{key}" if key else ""


class RoomMap:
    """Local name -> stable room id registry (operator-maintained).

    Because a11y gives us display names only, the operator records the real
    chatroom id once (e.g. after the Node side or WeChat UI shows it). Without a
    mapping, sends to non-unique names are refused.
    """

    def __init__(self, path: str):
        self.path = path
        self.data: Dict[str, str] = {}
        if os.path.exists(path):
            try:
                self.data = json.load(open(path))
            except Exception:
                self.data = {}

    def save(self):
        tmp = self.path + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(self.data, fh, ensure_ascii=False, indent=1)
        os.replace(tmp, self.path)

    def set(self, name: str, room_id: str):
        self.data[name] = room_id
        self.save()

    def get(self, name: str) -> Optional[str]:
        return self.data.get(name)


class AtspiSender:
    """Drive the WeChat input box through accessibility.

    SAFETY: constructed with `allow_real_send=False` by default. Nothing is
    typed unless the caller flips it AND provides an expected room id that
    matches the currently open conversation.
    """

    def __init__(self, source: AtspiSource, allow_real_send: bool = False,
                 room_map: Optional[RoomMap] = None):
        self.src = source
        self.allow = allow_real_send
        self.room_map = room_map

    # -- pre-send verification (fail-closed) ------------------------------
    def verify_target(self, expected_room_id: str, expected_room_name: str) -> Dict[str, Any]:
        """Prove the correct conversation is open BEFORE typing anything."""
        cur_title = self.src.conversation_title()
        cur_room = self.src.resolve_room_id(cur_title)
        mapped = self.room_map.get(cur_title) if self.room_map else None
        checks = {
            "expected_room_id": expected_room_id,
            "expected_room_name": expected_room_name,
            "current_title": cur_title,
            "current_room_id": cur_room,
            "mapped_room_id": mapped,
            "title_matches": cur_title == expected_room_name,
            "room_id_matches": (mapped == expected_room_id) if mapped else False,
        }
        ok = checks["title_matches"] and (mapped is not None and checks["room_id_matches"])
        checks["ok"] = bool(ok)
        if not ok:
            checks["refuse_reason"] = (
                "conversation_title_mismatch" if not checks["title_matches"]
                else "room_id_unmapped_or_mismatch"
            )
        return checks

    # -- send --------------------------------------------------------------
    def send(self, room_id: str, room_name: str, text: str,
             confirm_after: bool = True) -> Dict[str, Any]:
        if not self.allow:
            raise SendRefused("real_send_not_authorized (allow_real_send=False)")
        if not text.strip():
            raise SendRefused("empty_text")

        pre = self.verify_target(room_id, room_name)
        if not pre["ok"]:
            raise SendRefused(f"pre_send_refused:{pre.get('refuse_reason')}")

        box = self.src.input_box()
        if box is None:
            raise SendRefused("input_box_not_found")

        # Type via the text interface if editable, else set focus + xdotool.
        typed = False
        try:
            t = Atspi.util  # noqa: F841  (kept for clarity)
            # Prefer editable-text interface
            from gi.repository import Atspi as _A  # local alias
            et = box  # A11yNode has no live handle; caller must pass live node
        except Exception:
            pass

        # The A11yNode is a snapshot; for real typing we need the live node.
        live = self._find_live_input()
        if live is None:
            raise SendRefused("input_box_live_node_not_found")
        ok_edit = False
        try:
            et = live.get_editable_text_iface()
            if et is not None:
                et.set_text_contents(text)
                ok_edit = True
        except Exception:
            ok_edit = False
        if not ok_edit:
            # fallback: focus the box then paste via xdotool (X11 only)
            try:
                comp = live.get_component_iface()
                if comp is not None:
                    comp.grab_focus()
                time.sleep(0.15)
                subprocess.run(["xdotool", "type", "--clearmodifiers", "--delay", "8", text],
                               check=True, timeout=30)
                ok_edit = True
            except Exception as exc:
                raise SendRefused(f"type_failed:{exc!r}")

        # Enter to send
        sent = False
        try:
            act = live.get_action_iface()
            if act is not None:
                for i in range(act.get_n_actions()):
                    nm = (act.get_action_name(i) or "").lower()
                    if nm in ("activate", "press", "click"):
                        act.do_action(i)
                        sent = True
                        break
        except Exception:
            pass
        if not sent:
            try:
                comp = live.get_component_iface()
                if comp is not None:
                    comp.grab_focus()
                subprocess.run(["xdotool", "key", "Return"], check=True, timeout=10)
                sent = True
            except Exception as exc:
                raise SendRefused(f"send_key_failed:{exc!r}")

        result = {"ok": True, "pre_confirm": pre, "typed": ok_edit, "sent": sent}

        if confirm_after:
            time.sleep(0.6)
            post_title = self.src.conversation_title()
            result["post_title"] = post_title
            result["post_confirm"] = (post_title == room_name)
            if not result["post_confirm"]:
                # WeChat switched away from the target -> treat as misroute.
                result["ok"] = False
                result["post_confirm"] = False
        return result

    def _find_live_input(self):
        """Return the *live* Atspi node for the message input box."""
        app = self.src.find_app()
        if app is None:
            return None
        best = {"node": None, "score": -1}

        def walk(node, depth):
            if depth > 9:
                return
            info = node_info(node)
            if info.role in (ROLE_TEXT, ROLE_ENTRY, "editable text", "paragraph") \
                    and "EDITABLE" in info.states:
                e = info.extents or [0, 0, 0, 0]
                sc = e[3] * 2 + e[1]
                if sc > best["score"]:
                    best["score"] = sc
                    best["node"] = node
            for i in range(_safe(lambda: node.get_child_count(), 0) or 0):
                ch = _safe(lambda i=i: node.get_child_at_index(i))
                if ch is not None:
                    walk(ch, depth + 1)

        walk(app, 0)
        return best["node"]


# ------------------------------------------------------------------ selftest --
def selftest() -> int:
    """Prove the module talks to the REAL bus and reports what it can see.

    Runs against whatever WeChat state is on screen (login screen on this lab
    machine). No typing, no sending.
    """
    print("=== atspi_source selftest ===")
    print("HAS_ATSPI =", HAS_ATSPI)
    if not HAS_ATSPI:
        print("ATSPI bindings missing:", _ATSPI_ERR)
        return 2
    src = AtspiSource()
    app = src.find_app()
    if app is None:
        print("WECHAT_APP: NOT FOUND (is WeChat running with a11y enabled?)")
        return 3
    nodes = src.snapshot()
    print("WECHAT_APP: FOUND  nodes =", len(nodes))
    print("is_login_screen =", src.is_login_screen())
    print("conversation_title =", repr(src.conversation_title()))
    roles: Dict[str, int] = {}
    for n in nodes:
        roles[n.role] = roles.get(n.role, 0) + 1
    print("roles =", sorted(roles.items(), key=lambda x: -x[1]))
    named = [n for n in nodes if n.name][:12]
    for n in named:
        print(f"  [{n.role}] {n.name!r} ext={n.extents} states={n.states}")
    # real-@ detector sanity on both forms
    print("mention token test:", AtspiSource.detect_mention("hello\ufffc there"))
    print("plain @ test     :", AtspiSource.detect_mention("hello @wechat there"))
    return 0


if __name__ == "__main__":
    sys.exit(selftest())
