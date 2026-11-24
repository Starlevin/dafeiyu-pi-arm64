"""On-demand WeChat login inspection on the dedicated client display."""
import ctypes as c
from collections import deque
from contextlib import contextmanager
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import sys
import time
from urllib.parse import urlsplit


UNIT = "dafeiyu-wechat-client.service"
SCAN = {"scan to log in", "scan qr code to log in", "scan the qr code to log in",
        "扫码登录", "请使用微信扫码登录", "请用微信扫码登录", "使用手机微信扫码登录"}
CONFIRM = {"confirm on phone", "confirm login on your phone", "请在手机上确认", "请在手机上确认登录", "手机上确认"}
QR_LABELS = {"qr code", "login qr code", "qrcode", "二维码", "登录二维码", "微信登录二维码"}
SWITCH_LABELS = {"switch account", "切换账号"}
WINDOW_TOLERANCE = 24


@dataclass(frozen=True)
class Login:
    state: str
    pid: int = 0
    window: tuple = ()
    region: tuple = ()
    switch: tuple = ()


def _contains(window, rect):
    if not window or not rect:
        return False
    x, y, w, h = window
    rx, ry, rw, rh = rect
    return rw > 0 and rh > 0 and x <= rx and y <= ry and rx + rw <= x + w and ry + rh <= y + h


def inspect(app, showing, extents, enabled=lambda _node: True):
    if app is None:
        return Login("unknown")
    windows = [node for index in range(min(app.get_child_count(), 16))
               if (node := app.get_child_at_index(index)) and showing(node)]
    scan, confirm, login = False, False, False
    navigation = set()
    images = []
    switches = []
    queue = deque((node, 0) for node in windows)
    budget = 256
    incomplete = False
    while queue and budget:
        node, depth = queue.popleft()
        budget -= 1
        if not showing(node):
            continue
        role = node.get_role_name()
        if role in {"list item", "entry", "text", "password text"}:
            continue
        name = " ".join((node.get_name() or "").casefold().strip("。.!！ ").split())
        if role in {"list", "list box"} and name in {"chats", "聊天", "会话"}:
            return Login("online")
        if role in {"button", "push button"}:
            if name in SWITCH_LABELS and enabled(node):
                switches.append(extents(node))
            if name in {"contacts", "通讯录"}:
                navigation.add("contacts")
            if name in {"moments", "朋友圈"}:
                navigation.add("moments")
            if len(navigation) == 2:
                return Login("online")
        if role not in {"list", "list box", "table", "tree table"}:
            scan |= name in SCAN
            confirm |= name in CONFIRM
            login |= role in {"push button", "button"} and name in {"log in", "登录", *SWITCH_LABELS}
            if role == "image" and name in QR_LABELS:
                images.append(extents(node))
            children = node.get_child_count()
            if depth < 12 and children <= 48:
                queue.extend((child, depth + 1) for index in range(children)
                             if (child := node.get_child_at_index(index)))
            elif children:
                incomplete = True
    if queue or incomplete or len(windows) != 1 or app.get_child_count() > 16:
        return Login("unknown")
    if confirm:
        return Login("confirm")
    window = extents(windows[0])
    if not scan:
        if (login and window and 190 <= window[2] <= 480 and 210 <= window[3] <= 650
                and len(switches) == 1 and _contains(window, switches[0])):
            return Login("login", app.get_process_id(), window, switch=switches[0])
        return Login("login" if login else "unknown")
    if not window or not (190 <= window[2] <= 480 and 210 <= window[3] <= 650):
        return Login("scan")
    x, y, width, height = window
    # A named accessible image is preferred. Qt can expose only a login
    # instruction; in that compact, positively identified window inspect its inset.
    if len(images) > 1:
        return Login("scan")
    region = images[0] if images else (x + 16, y + 36, width - 32, height - 72)
    if not region:
        return Login("scan")
    rx, ry, rw, rh = region
    if not (80 <= rw <= 448 and 80 <= rh <= 520 and x <= rx and y <= ry
            and rx + rw <= x + width and ry + rh <= y + height):
        return Login("scan")
    return Login("scan", app.get_process_id(), window, region)


def native_status():
    import gi
    gi.require_version("Atspi", "2.0")
    from gi.repository import Atspi

    desktop = Atspi.get_desktop(0)
    candidates = []
    count = desktop.get_child_count()
    if count > 48:
        return Login("unknown")
    for index in range(count):
        app = desktop.get_child_at_index(index)
        if not app or (app.get_name() or "").casefold() not in {"wechat", "weixin", "微信"}:
            continue
        pid = app.get_process_id()
        proc = Path("/proc") / str(pid)
        try:
            if proc.stat().st_uid == os.getuid() and any(
                    UNIT in line.split("/") for line in (proc / "cgroup").read_text().splitlines()
            ):
                candidates.append(app)
        except OSError:
            continue
    if len(candidates) != 1:
        return Login("unknown")

    def rect(node):
        if not node.get_component_iface():
            return None
        bounds = node.get_extents(Atspi.CoordType.SCREEN)
        return bounds.x, bounds.y, bounds.width, bounds.height

    return inspect(candidates[0], lambda node: node.get_state_set().contains(Atspi.StateType.SHOWING),
                   rect, lambda node: node.get_state_set().contains(Atspi.StateType.ENABLED))


def _function(library, name, result, *args):
    fn = getattr(library, name)
    fn.restype = result
    fn.argtypes = args
    return fn


@contextmanager
def _x_display():
    xlib = c.CDLL("libX11.so.6")
    P, I = c.c_void_p, c.c_int
    x = lambda name, result, *args: _function(xlib, "X" + name, result, *args)
    display = x("OpenDisplay", P, c.c_char_p)(None)
    if not display:
        yield None
        return
    try:
        yield display, x
    finally:
        x("CloseDisplay", I, P)(display)


def _matching_geometry(expected, actual):
    # Qt's accessible bounds omit small X11 frame offsets on the Pi session.
    return (len(expected) == len(actual) == 4
            and all(abs(a - b) <= WINDOW_TOLERANCE for a, b in zip(expected, actual)))


def _login_drawable(state, display, x):
    """Resolve a unique top-level drawable owned by the verified login app."""
    P, W, I, U = c.c_void_p, c.c_ulong, c.c_int, c.c_uint
    query = x("QueryTree", I, P, W, c.POINTER(W), c.POINTER(W), c.POINTER(c.POINTER(W)), c.POINTER(U))
    free = x("Free", I, P)
    root = x("DefaultRootWindow", W, P)(display)
    atom_pid = x("InternAtom", W, P, c.c_char_p, I)(display, b"_NET_WM_PID", 1)
    if not atom_pid or state.pid <= 0:
        return None
    root_return, parent = W(), W()
    children, count = c.POINTER(W)(), U()
    if not query(display, root, c.byref(root_return), c.byref(parent), c.byref(children), c.byref(count)):
        return None
    try:
        if count.value > 160:
            return None
        window_ids = tuple(children[:count.value]) if children else ()
    finally:
        if children:
            free(children)
    property_get = x("GetWindowProperty", I, P, W, W, c.c_long, c.c_long, I, W, c.POINTER(W),
                     c.POINTER(I), c.POINTER(W), c.POINTER(W), c.POINTER(c.POINTER(c.c_ubyte)))
    geometry = x("GetGeometry", I, P, W, c.POINTER(W), c.POINTER(I), c.POINTER(I),
                 *(c.POINTER(U),) * 4)
    matches = []
    for window in window_ids:
        kind, length, left, fmt, value = W(), W(), W(), I(), c.POINTER(c.c_ubyte)()
        status = property_get(display, window, atom_pid, 0, 1, 0, 6, c.byref(kind), c.byref(fmt),
                              c.byref(length), c.byref(left), c.byref(value))
        try:
            owner = c.cast(value, c.POINTER(W))[0] if value and length.value == 1 and fmt.value == 32 else 0
        finally:
            if value:
                free(value)
        if status or owner != state.pid:
            continue
        wx, wy, ww, wh, border, depth = I(), I(), U(), U(), U(), U()
        if geometry(display, window, c.byref(root_return), c.byref(wx), c.byref(wy), c.byref(ww),
                    c.byref(wh), c.byref(border), c.byref(depth)):
            bounds = (wx.value, wy.value, ww.value, wh.value)
            if depth.value in (24, 32) and _matching_geometry(state.window, bounds):
                matches.append((window, bounds))
    if len(matches) != 1:
        return None
    return matches[0]


def _drawable_region(state, bounds, rect):
    """Scale Qt's logical coordinates into the verified X11 drawable."""
    if not _matching_geometry(state.window, bounds) or not _contains(state.window, rect):
        return None
    left, top, logical_w, logical_h = state.window
    rx, ry, w, h = rect
    actual_w, actual_h = bounds[2:]
    x = (rx - left) * actual_w // logical_w
    y = (ry - top) * actual_h // logical_h
    right = ((rx + w - left) * actual_w + logical_w - 1) // logical_w
    bottom = ((ry + h - top) * actual_h + logical_h - 1) // logical_h
    return x, y, right - x, bottom - y


def capture_region(state):
    """Read a bounded region of the verified client's X drawable, never the root."""
    if state.state != "scan" or not state.region:
        return None
    P, W, I, U = c.c_void_p, c.c_ulong, c.c_int, c.c_uint
    with _x_display() as connection:
        if connection is None or not (match := _login_drawable(state, *connection)):
            return None
        display, x = connection
        window, bounds = match
        region = _drawable_region(state, bounds, state.region)
        if region is None:
            return None
        dx, dy, w, h = region
        image = x("GetImage", P, P, W, I, I, U, U, W, I)(
            display, window, dx, dy, w, h, W(-1), 2)
        if not image:
            return None
        try:
            pixel = x("GetPixel", W, P, I, I)
            pixels = bytearray()
            for yy in range(h):
                for xx in range(w):
                    color = pixel(image, xx, yy)
                    luminance = ((color >> 16 & 255) * 77 + (color >> 8 & 255) * 150 + (color & 255) * 29) >> 8
                    pixels.append(luminance)
            return w, h, bytes(pixels)
        finally:
            x("DestroyImage", I, P)(image)


def open_scan_page(state):
    """Activate only the verified account chooser on the dedicated display."""
    if state.state != "login" or not state.switch or os.environ.get("DISPLAY") != ":99":
        return False
    import pi_xinput as xi

    with _x_display() as connection:
        if connection is None or not (match := _login_drawable(state, *connection)):
            return False
        window, bounds = match
        region = _drawable_region(state, bounds, state.switch)
        if region is None:
            return False
        dx, dy, w, h = region
        x, y = bounds[0] + dx + w // 2, bounds[1] + dy + h // 2
        xi.move(x, y)
        if xi.pointer() != (x, y, window) or native_status() != state:
            return False
        xi.click(x, y, settle=0.1)
    return True


def _is_login_uri(uri):
    if not uri.isascii() or not all(ch.isprintable() and not ch.isspace() for ch in uri):
        return False
    try:
        parsed = urlsplit(uri)
    except ValueError:
        return False
    host = parsed.netloc.lower()
    if parsed.scheme == "https":
        return host in {"login.weixin.qq.com", "weixin.qq.com", "wx.qq.com", "web.wechat.com"} and bool(parsed.path)
    # The native Linux client encodes this legacy URI. Keep the bytes for the
    # QR image and never perform a network request to it.
    return (parsed.scheme == "http" and host == "weixin.qq.com"
            and not parsed.query and not parsed.fragment
            and re.fullmatch(r"/x/[A-Za-z0-9_-]{8,160}", parsed.path) is not None)


def decode_login_qr(frame):
    if frame is None:
        return None
    zbar = c.CDLL("libzbar.so.0")
    P, U, I = c.c_void_p, c.c_uint, c.c_int
    z = lambda name, result, *args: _function(zbar, "zbar_" + name, result, *args)
    scanner, image = z("image_scanner_create", P)(), z("image_create", P)()
    try:
        if not scanner or not image:
            return None
        z("image_set_format", None, P, c.c_ulong)(image, int.from_bytes(b"Y800", "little"))
        width, height, pixels = frame
        z("image_set_size", None, P, U, U)(image, width, height)
        data = c.create_string_buffer(pixels)
        z("image_set_data", None, P, P, c.c_ulong, P)(image, data, len(pixels), None)
        if z("scan_image", I, P, P)(scanner, image) != 1:
            return None
        symbol = z("image_first_symbol", P, P)(image)
        if not symbol or z("symbol_get_type", I, P)(symbol) != 64:
            return None
        length = z("symbol_get_data_length", U, P)(symbol)
        if not 1 <= length <= 1000:
            return None
        uri = c.string_at(z("symbol_get_data", P, P)(symbol), length).decode("ascii")
        return uri if _is_login_uri(uri) else None
    finally:
        if image:
            z("image_destroy", None, P)(image)
        if scanner:
            z("image_scanner_destroy", None, P)(scanner)


def _status(state):
    payload = {"state": state.state, "canScan": bool(state.region)}
    if state.state == "login" and state.switch:
        payload["canOpenQr"] = True
    return payload


def run(action, inspect_fn=native_status, capture=capture_region, decode=decode_login_qr,
        open_page=open_scan_page, sleep=time.sleep):
    before = inspect_fn()
    if action == "qr" and before.state == "login" and before.switch and open_page(before):
        for _ in range(6):
            sleep(0.2)
            before = inspect_fn()
            if before.state in {"scan", "confirm", "online"}:
                break
    payload = _status(before)
    if action != "qr" or before.state != "scan" or not before.region:
        return payload
    token = decode(capture(before))
    after = inspect_fn()
    if after != before:
        return _status(after)
    if token:
        payload["qr"] = token
    return payload


if __name__ == "__main__":
    try:
        if sys.argv[1:] not in (["status"], ["qr"]):
            raise ValueError
        print(json.dumps(run(sys.argv[1]), separators=(",", ":")))
    except Exception:
        print('{"state":"unknown","canScan":false}')
