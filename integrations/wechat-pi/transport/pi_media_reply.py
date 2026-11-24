#!/usr/bin/env python3
"""Fail-closed WeChat media delivery for explicitly approved local attachments.

Only the locally configured special group is permitted.
No network download, DB reads, clipboard image bytes, or arbitrary model paths.
An unknown UI submission is never retried by the bridge.

v2: buttons are located semantically via AT-SPI and mapped through the
AT-SPI-frame -> X-window affine transform (the Pi client resizes its window,
so raw AT-SPI extents are stale). The Qt file dialog receives no WM focus,
so it is raised and focused via Xlib before keyboard-driven file selection.
"""
from __future__ import annotations

import ctypes
import hashlib
import json
import re
import stat
import time
from pathlib import Path
from runtime_paths import configured_group

MAX_BYTES = 10 * 1024 * 1024
NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}$")
KINDS = {"image": {".png", ".jpg", ".jpeg", ".gif"},
         "file": {".txt", ".pdf"}}
ANIMATED_ROWS = {"Animated Stickers", "Animated sticker", "动画表情", "[动画表情]",
                 "[动态表情]", "[GIF]", "[gif]"}


def approved_path(spec, directory):
    if not isinstance(spec, dict) or not directory:
        return None
    kind, name = spec.get("kind"), spec.get("name")
    if kind not in KINDS or not isinstance(name, str) or not NAME.fullmatch(name):
        return None
    root = Path(directory)
    if not root.is_dir() or root.is_symlink():
        return None
    item = root / name
    try:
        if item.is_symlink() or not item.is_file() or item.resolve().parent != root.resolve():
            return None
        info = item.stat()
        if not 0 < info.st_size <= MAX_BYTES or not stat.S_ISREG(info.st_mode):
            return None
        if item.suffix.lower() not in KINDS[kind]:
            return None
        manifest = root / "approved-media.json"
        if manifest.is_symlink() or not manifest.is_file():
            return None
        catalog = json.loads(manifest.read_text(encoding="utf-8"))
        entry = catalog.get("files", {}).get(name)
        if not isinstance(entry, dict) or entry.get("kind") != kind:
            return None
        expected = entry.get("sha256")
        if not isinstance(expected, str) or not re.fullmatch(r"[0-9a-f]{64}", expected):
            return None
        with item.open("rb") as src:
            magic = src.read(16)
        if kind == "image" and item.suffix.lower() == ".png" and not magic.startswith(b"\x89PNG\r\n\x1a\n"):
            return None
        if kind == "image" and item.suffix.lower() in (".jpg", ".jpeg") and not magic.startswith(b"\xff\xd8\xff"):
            return None
        if kind == "image" and item.suffix.lower() == ".gif" and not magic.startswith((b"GIF87a", b"GIF89a")):
            return None
        if item.suffix.lower() == ".pdf" and not magic.startswith(b"%PDF-"):
            return None
        if item.suffix.lower() == ".txt" and b"\x00" in magic:
            return None
        digest = hashlib.sha256()
        with item.open("rb") as src:
            for block in iter(lambda: src.read(65536), b""):
                digest.update(block)
        if digest.hexdigest() != expected:
            return None
        return item
    except (OSError, ValueError, TypeError, KeyError):
        return None


def _mapper(bridge, pf):
    """Affine map from AT-SPI coordinates to real X pixels, or None."""
    app = bridge.get_app()
    if app is None:
        return None
    fr = None
    for n, _ in bridge.walk(app, maxd=3):
        if bridge.role(n) == "frame" and bridge.nm(n) == "Weixin":
            fr = bridge.ext(n)
            break
    if not fr or fr[2] <= 0 or fr[3] <= 0:
        return None
    mw = pf.main_window()
    if not mw:
        return None
    g = pf.windows().get(mw)
    if not g or g[2] <= 0 or g[3] <= 0:
        return None
    sx, sy = g[2] / fr[2], g[3] / fr[3]
    if not (0.8 <= sx <= 1.3 and 0.8 <= sy <= 1.3):
        return None

    def m(rect):
        x, y, w, h = rect
        return (int((x - fr[0]) * sx + g[0]), int((y - fr[1]) * sy + g[1]),
                max(1, int(w * sx)), max(1, int(h * sy)))
    return m, g


def _toolbar_buttons(bridge, app, composer):
    x, y, w, h = composer
    sendfile, send = [], []
    for n, _ in bridge.walk(app, maxd=26):
        if bridge.role(n) != "button":
            continue
        r = bridge.ext(n)
        if not r or r[2] <= 0 or r[3] <= 0:
            continue
        if not (y + h - 10 <= r[1] <= y + h + 90):
            continue
        nm = bridge.nm(n)
        if nm == "Send File" and x - 50 <= r[0] <= x + w:
            sendfile.append(r)
        elif nm in ("Send", "发送") and r[0] >= x + w * 0.5:
            send.append(r)
    return sendfile, send


def _dialog_visible(bridge):
    app = bridge.get_app()
    if app is None:
        return False
    for n, _ in bridge.walk(app, maxd=28):
        if bridge.role(n) == "button" and bridge.nm(n) in ("Open", "Cancel", "打开", "取消"):
            r = bridge.ext(n)
            if r and r[2] > 0 and r[3] > 0 and bridge.showing(n):
                return True
    return False


def _is_media_row(bridge, visible, spec, path):
    if path.name in visible:
        return True
    if spec["kind"] != "image":
        return False
    return bridge.is_image(visible) or visible.strip() in ANIMATED_ROWS


def _clear_draft(bridge, xi, m):
    try:
        app = bridge.get_app()
        node, comp = bridge.composer(app)
        if not comp:
            return
        cx, cy, cw, ch = m(comp)
        xi.click(cx + cw // 2, cy + ch // 2, settle=.25)
        xi.key("a", ("Control_L",), settle=.12)
        xi.key("Delete", settle=.2)
        xi.key("BackSpace", settle=.2)
    except Exception:
        pass


def _same_composer(bridge, node, original):
    original_node, (role, _, geometry) = original
    if node != original_node:
        return False
    current = bridge._tx_composer_fp(node)
    return bool(current and (current[0], current[2]) == (role, geometry))


def _composer_pre_return_state(bridge, target, original):
    """选完路径准备按 Return 前，确认仍停在原目标且原输入框仍完全空白。"""
    app = bridge.get_app()
    if app is None or not bridge.group_is_open(app, target):
        return "target_changed"
    visible, selected, matches = bridge._tx_chats_info(app, target)
    if visible != 1 or matches != 1 or len(selected or ()) != 1:
        return "target_changed"
    node, _ = bridge.composer(app)
    if node is None:
        return "missing"
    if not _same_composer(bridge, node, original):
        return "composer_replaced"
    reader = node.get_text_iface()
    if reader is None:
        return "missing"
    draft = bridge.Atspi.Text.get_text(reader, 0, -1)
    if (draft or "") != "" or reader.get_character_count() != 0:
        return "concurrent_draft"
    return "ok"


PICKER_TITLES = {"open", "open files", "select file", "choose file", "select files",
                 "choose files", "打开", "打开文件", "选择文件"}
PICKER_NAME_FIELDS = {"file name", "file name:", "文件名", "文件名:"}
PICKER_WAIT_S = 8.0


def _visible_file_picker(bridge, app):
    """Only a single native file chooser on the original WeChat app qualifies."""
    dialogs = []
    for node, _, role in bridge._walk_showing(app, maxd=12, skip_lists=True, include_role=True):
        if role == "dialog" and (rect := bridge.ext(node)) and rect[2] > 0 and rect[3] > 0:
            dialogs.append((node, rect))
    if len(dialogs) != 1 or bridge.nm(dialogs[0][0]).strip().casefold() not in PICKER_TITLES:
        return None
    return dialogs[0]


def _owned_picker_window(pf, window, main):
    transient = ctypes.c_ulong()
    fn = pf.X.XGetTransientForHint
    fn.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.POINTER(ctypes.c_ulong)]
    return bool(fn(pf.dpy(), window, ctypes.byref(transient)) and transient.value == main)


def _wait_for_file_picker(bridge, app, pf, main, before, to_x11):
    """Wait for accessibility and the matching X11 window to agree."""
    deadline = time.monotonic() + PICKER_WAIT_S
    observed = None
    while True:
        if bridge.get_app() != app:
            return None, None
        current = _visible_file_picker(bridge, app)
        if observed is not None and (current is None or current[0] != observed):
            return None, None
        if current is not None:
            observed, rectangle = current
            mapped = to_x11(rectangle)
            matches = [wid for wid, rect in pf.find_new(before)
                       if all(abs(real - expected) <= 32 for real, expected in zip(rect, mapped))
                       and _owned_picker_window(pf, wid, main)]
            if len(matches) == 1:
                return matches[0], observed
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None, observed
        time.sleep(min(.25, remaining))


def _cancel_owned_picker(bridge, app, target, original, observed, path, account):
    """Cancel the chooser opened by this attempt, leaving any changed draft alone."""
    if observed is None:
        return False
    try:
        if (bridge.get_app() != app or (account and bridge.app_memory_account(app) != account)
                or _composer_pre_return_state(bridge, target, original) != "ok"):
            return False
        current = _visible_file_picker(bridge, app)
        if current is None or current[0] != observed:
            return False
        fields, buttons = [], []
        for node, _, role in bridge._walk_showing(observed, maxd=9, skip_lists=True, include_role=True):
            if role == "text" and bridge.nm(node).strip().casefold() in PICKER_NAME_FIELDS:
                fields.append(node)
            elif role == "button" and bridge.nm(node).strip().casefold() in {"cancel", "取消"}:
                buttons.append(node)
        if len(fields) != 1 or len(buttons) != 1:
            return False
        reader = fields[0].get_text_iface()
        if reader is None:
            return False
        filename = bridge.Atspi.Text.get_text(reader, 0, -1)
        sticker_directory = (path.stem == "weixin-sticker"
                             and re.fullmatch(r"sticker-[A-Za-z0-9_-]{8}", path.parent.name)
                             and isinstance(filename, str)
                             and re.fullmatch(r"sticker-[A-Za-z0-9_-]{8}", filename))
        if filename not in ("", str(path)) and not sticker_directory:
            return False
        states = buttons[0].get_state_set()
        if not states.contains(bridge.Atspi.StateType.ENABLED):
            return False
        action = buttons[0].get_action_iface()
        if action is None:
            return False
        indices = [i for i in range(action.get_n_actions())
                   if action.get_action_name(i).strip().casefold() in
                   {"press", "click", "activate", "按下", "点击", "激活"}]
        if (len(indices) != 1 or bridge.get_app() != app
                or (account and bridge.app_memory_account(app) != account)):
            return False
        if bridge.Atspi.Text.get_text(reader, 0, -1) != filename:
            return False
        return bool(action.do_action(indices[0]))
    except Exception:
        return False


def _x11_is_same_or_descendant(pf, focus_win, dlg):
    """确认 X11 焦点窗口就是 dlg 本身或其子控件（如 Qt 对话框内部子窗口）。"""
    try:
        cur = int(focus_win)
    except (TypeError, ValueError):
        return False
    if cur in (0, 1):
        return False
    if cur == dlg:
        return True
    xlib = getattr(pf, "X", None)
    dpy_fn = getattr(pf, "dpy", None)
    if xlib is None or not callable(dpy_fn) or not hasattr(xlib, "XQueryTree"):
        return False
    dpy = dpy_fn()
    if not dpy:
        return False
    try:
        xlib.XQueryTree.argtypes = [
            ctypes.c_void_p, ctypes.c_ulong, ctypes.POINTER(ctypes.c_ulong),
            ctypes.POINTER(ctypes.c_ulong), ctypes.POINTER(ctypes.POINTER(ctypes.c_ulong)),
            ctypes.POINTER(ctypes.c_uint),
        ]
    except Exception:
        pass
    seen = set()
    for _ in range(16):
        if cur in seen or cur in (0, 1):
            return False
        seen.add(cur)
        rr = ctypes.c_ulong()
        pr = ctypes.c_ulong()
        kids = ctypes.POINTER(ctypes.c_ulong)()
        n = ctypes.c_uint()
        status = xlib.XQueryTree(
            dpy, cur, ctypes.byref(rr), ctypes.byref(pr),
            ctypes.byref(kids), ctypes.byref(n),
        )
        if kids and hasattr(xlib, "XFree"):
            try:
                xlib.XFree.argtypes = [ctypes.c_void_p]
            except Exception:
                pass
            try:
                xlib.XFree(kids)
            except Exception:
                pass
        if status == 0:
            return False
        parent = int(pr.value)
        if parent == dlg:
            return True
        if parent == int(rr.value) or parent == 0:
            return False
        cur = parent
    return False


def _picker_has_focus(pf, dlg):
    """fail-closed 验证文件对话框当前确实存在且拥有输入焦点。"""
    try:
        wins = pf.windows()
    except Exception:
        return False
    rect = wins.get(dlg) if isinstance(wins, dict) else None
    if not rect or len(rect) < 4 or rect[2] <= 0 or rect[3] <= 0:
        return False
    fw_fn = getattr(pf, "focused_window", None)
    if callable(fw_fn):
        try:
            fw = fw_fn()
        except Exception:
            return False
        if fw is None:
            return False
        return fw == dlg or _x11_is_same_or_descendant(pf, fw, dlg)
    xlib = getattr(pf, "X", None)
    dpy_fn = getattr(pf, "dpy", None)
    if xlib is not None and callable(dpy_fn) and hasattr(xlib, "XGetInputFocus"):
        try:
            dpy = dpy_fn()
            if not dpy:
                return False
            try:
                xlib.XGetInputFocus.argtypes = [
                    ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong),
                    ctypes.POINTER(ctypes.c_int),
                ]
            except Exception:
                pass
            fw = ctypes.c_ulong()
            revert = ctypes.c_int()
            xlib.XGetInputFocus(dpy, ctypes.byref(fw), ctypes.byref(revert))
            return _x11_is_same_or_descendant(pf, fw.value, dlg)
        except Exception:
            return False
    return False


def _picker_window_present(pf, dlg):
    try:
        wins = pf.windows()
    except Exception:
        return False
    rect = wins.get(dlg) if isinstance(wins, dict) else None
    return bool(rect and len(rect) >= 4 and rect[2] > 0 and rect[3] > 0)


def _target_intact(bridge, target):
    app = bridge.get_app()
    return bool(
        app is not None
        and bridge.group_is_open(app, target)
        and len(bridge.visible_chat_matches(app, target)) == 1
    )


def _require_live_focused_picker(bridge, pf, dlg, target, original):
    """每次向文件对话框发键前都必须正面证明：dlg 仍存在、焦点确在 dlg、目标与原输入框空白未变。"""
    if not _picker_window_present(pf, dlg):
        return (
            "media_picker_closed" if _target_intact(bridge, target)
            else "media_draft_target_changed"
        )
    if not _picker_has_focus(pf, dlg):
        return "media_picker_focus_failed"
    state = _composer_pre_return_state(bridge, target, original)
    if state == "ok":
        return None
    if state == "target_changed":
        return "media_draft_target_changed"
    if callable(getattr(bridge, "log", None)):
        bridge.log("media_draft_preserved", stage="pre_return", reason=state)
    return "media_draft_unverified"


def _focus_picker(bridge, pf, dlg, target, original, settle_s):
    """先验证 dlg 尚在且输入框未变，再聚焦并正面核验焦点确实落在 dlg 上。"""
    if not _picker_window_present(pf, dlg):
        return (
            "media_picker_closed" if _target_intact(bridge, target)
            else "media_draft_target_changed"
        )
    state = _composer_pre_return_state(bridge, target, original)
    if state == "target_changed":
        return "media_draft_target_changed"
    if state != "ok":
        if callable(getattr(bridge, "log", None)):
            bridge.log("media_draft_preserved", stage="pre_focus", reason=state)
        return "media_draft_unverified"
    focus_ret = pf.focus(dlg)
    time.sleep(settle_s)
    if focus_ret is False:
        return (
            "media_picker_closed" if not _picker_window_present(pf, dlg)
            else "media_picker_focus_failed"
        )
    return _require_live_focused_picker(bridge, pf, dlg, target, original)


def _drive_picker_input(bridge, pf, dlg, target, original, path, xi, focus_key, settle_s):
    """驱动文件对话框输入并按 Return；任一阶段对话窗关闭或失焦均 fail-closed 中止。"""
    err = _focus_picker(bridge, pf, dlg, target, original, settle_s)
    if err is not None:
        return err
    mods = ("Alt_L",) if focus_key == "n" else ("Control_L",)
    xi.key(focus_key, mods, settle=.25)
    err = _require_live_focused_picker(bridge, pf, dlg, target, original)
    if err is not None:
        return err
    xi.key("a", ("Control_L",), settle=.12)
    err = _require_live_focused_picker(bridge, pf, dlg, target, original)
    if err is not None:
        return err
    if not xi.type_text(str(path), settle=.4):
        return "media_filename_failed"
    err = _require_live_focused_picker(bridge, pf, dlg, target, original)
    if err is not None:
        return err
    xi.key("Return", settle=1.0)
    return None


def send_media(target, spec, media_dir, on_submit, bridge, xi):
    """Submit one verified attachment, returning only a status enum."""
    if target != configured_group("SPECIAL_GROUP"):
        return "media_target_unapproved"
    path = approved_path(spec, media_dir)
    if path is None:
        return "media_not_approved"
    try:
        import picker_focus as pf
    except Exception:
        return "media_focus_unavailable"
    verify_fn = getattr(bridge, "verify_media_target", None)
    clear_fn = getattr(bridge, "verify_media_clear", None)
    if not callable(verify_fn) or not callable(clear_fn):
        return "media_target_guard_missing"
    app = bridge.get_app()
    if (app is None or bridge.blocking_state(app)
            or len(bridge.visible_chat_matches(app, target)) != 1
            or not bridge.group_is_open(app, target)):
        return "media_target_unverified"
    account = bridge.app_memory_account(app)
    ok_before, _ = verify_fn(target, phase="before_media")
    if not ok_before:
        return "media_target_unverified"
    node, bounds = bridge.composer(app)
    if not node or not bounds:
        return "media_composer_missing"
    reader = node.get_text_iface()
    if reader is None or reader.get_character_count() != 0:
        return "media_composer_busy"
    fp_fn = getattr(bridge, "_tx_composer_fp", None)
    own_fp = fp_fn(node) if callable(fp_fn) else None
    if own_fp is None:
        return "media_composer_unverified"
    original = (node, own_fp)
    mapped = _mapper(bridge, pf)
    if mapped is None:
        return "media_layout_unverified"
    m, g = mapped
    sf, sd = _toolbar_buttons(bridge, app, bounds)
    if len(sf) != 1 or len(sd) != 1:
        return "media_layout_unverified"
    sfx, sfy, sfw, sfh = m(sf[0])
    sdx, sdy, sdw, sdh = m(sd[0])
    for cx, cy in ((sfx + sfw // 2, sfy + sfh // 2), (sdx + sdw // 2, sdy + sdh // 2)):
        if not (g[0] <= cx <= g[0] + g[2] and g[1] <= cy <= g[1] + g[3]):
            return "media_layout_unverified"
    # Reserve before any UI mutation. If the ledger is unavailable, do not
    # create an attachment draft that another actor might accidentally send.
    on_submit()
    mw = pf.main_window()
    if not mw:
        return "media_layout_unverified"
    pf.focus(mw)
    time.sleep(.25)
    if (bridge.get_app() != app or (account and bridge.app_memory_account(app) != account)
            or bridge.blocking_state(app) or _composer_pre_return_state(bridge, target, original) != "ok"):
        return "media_target_unverified"
    before = pf.windows()
    xi.click(sfx + sfw // 2, sfy + sfh // 2, settle=.6)
    dlg, accessible = _wait_for_file_picker(bridge, app, pf, mw, before, m)
    if dlg is None:
        if _cancel_owned_picker(bridge, app, target, original, accessible, path, account):
            return "media_picker_cancelled"
        return "media_picker_unverified"
    drive_err = _drive_picker_input(
        bridge, pf, dlg, target, original, path, xi, "n", settle_s=.35)
    if drive_err is not None:
        _cancel_owned_picker(bridge, app, target, original, accessible, path, account)
        return drive_err

    def gone():
        if _picker_window_present(pf, dlg):
            return False
        return not _dialog_visible(bridge)

    closed = False
    for _ in range(10):
        time.sleep(.5)
        if gone():
            closed = True
            break
    if not closed:
        retry_err = _drive_picker_input(
            bridge, pf, dlg, target, original, path, xi, "l", settle_s=.25)
        if retry_err is not None:
            _cancel_owned_picker(bridge, app, target, original, accessible, path, account)
            return retry_err
        for _ in range(6):
            time.sleep(.5)
            if gone():
                closed = True
                break
    if not closed:
        _cancel_owned_picker(bridge, app, target, original, accessible, path, account)
        return "media_picker_failed"
    mw = pf.main_window()
    if mw:
        pf.focus(mw)
        time.sleep(.3)
    current_app = bridge.get_app()
    if current_app != app or (account and bridge.app_memory_account(app) != account):
        return "media_draft_target_changed"
    ok_send, _ = verify_fn(target, phase="before_media_send")
    if (not ok_send or app is None or bridge.blocking_state(app)
            or _dialog_visible(bridge)
            or not bridge.group_is_open(app, target)
            or len(bridge.visible_chat_matches(app, target)) != 1):
        # 切群或目标身份存疑时绝不执行 Ctrl+A/Delete，防止误清用户草稿。
        return "media_draft_target_changed"
    node, _ = bridge.composer(app)
    reader = node.get_text_iface() if node is not None else None
    if node is None or reader is None:
        return "media_draft_missing"
    if not _same_composer(bridge, node, original):
        if callable(getattr(bridge, "log", None)):
            bridge.log("media_draft_preserved", stage="before_media_send", reason="composer_replaced")
        return "media_draft_unverified"
    draft = bridge.Atspi.Text.get_text(reader, 0, -1)
    char_count = reader.get_character_count()
    expected_drafts = ("\ufffc",) if spec["kind"] == "image" else ("\ufffc", path.name)
    if char_count != len(draft) or draft not in expected_drafts:
        if draft:
            ok_clear, clear_reason = clear_fn(
                target, own_fp,
                expected_draft=path.name if (spec["kind"] == "file" and draft == path.name) else None)
            if ok_clear:
                _clear_draft(bridge, xi, m)
            elif callable(getattr(bridge, "log", None)):
                bridge.log("media_draft_preserved", stage="media_draft_unverified", reason=clear_reason)
        return "media_draft_unverified"
    _, before_msgs = bridge.read_messages(app)
    base0 = bridge.baseline(before_msgs)
    xi.click(sdx + sdw // 2, sdy + sdh // 2, settle=.8)
    for _ in range(12):
        app = bridge.get_app()
        if app is None or not bridge.group_is_open(app, target):
            return "media_post_target_changed"
        anchor, after = bridge.read_messages(app)
        appended = bridge.new_rows(base0, bridge.baseline(after))
        if appended:
            for visible, rect in after[-len(appended):]:
                side = bridge.avatar_side(rect, anchor)
                if side and side[0] == "outgoing" and _is_media_row(bridge, visible, spec, path):
                    return "visible_outgoing"
        time.sleep(.6)
    app = bridge.get_app()
    post_node, _ = bridge.composer(app) if app is not None else (None, None)
    post_reader = post_node.get_text_iface() if post_node is not None else None
    post_draft = (bridge.Atspi.Text.get_text(post_reader, 0, -1)
                  if post_reader is not None else None)
    if post_draft:
        ok_clear, clear_reason = clear_fn(
            target, own_fp,
            expected_draft=path.name if (spec["kind"] == "file" and post_draft == path.name) else None)
        if ok_clear:
            _clear_draft(bridge, xi, m)
        elif callable(getattr(bridge, "log", None)):
            bridge.log("media_draft_preserved", stage="media_submission_unknown", reason=clear_reason)
    return "media_submission_unknown"