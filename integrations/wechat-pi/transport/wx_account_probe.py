"""On-demand WeChat login inspection on the dedicated client display."""
import ctypes as c
from collections import deque
from dataclasses import dataclass
import json
import os
from pathlib import Path
import sys
from urllib.parse import urlsplit


UNIT = "dafeiyu-wechat-client.service"
SCAN = {"scan to log in", "scan qr code to log in", "scan the qr code to log in",
        "扫码登录", "请使用微信扫码登录", "请用微信扫码登录", "使用手机微信扫码登录"}
CONFIRM = {"confirm on phone", "confirm login on your phone", "请在手机上确认", "请在手机上确认登录", "手机上确认"}
QR_LABELS = {"qr code", "login qr code", "qrcode", "二维码", "登录二维码", "微信登录二维码"}


@dataclass(frozen=True)
class Login:
    state: str
    pid: int = 0
    window: tuple = ()
    region: tuple = ()


def inspect(app, showing, extents):
    if app is None:
        return Login("unknown")
    windows = [node for index in range(min(app.get_child_count(), 16))
               if (node := app.get_child_at_index(index)) and showing(node)]
    scan, confirm, login = False, False, False
    navigation = set()
    images = []
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
            if name in {"contacts", "通讯录"}:
                navigation.add("contacts")
            if name in {"moments", "朋友圈"}:
                navigation.add("moments")
            if len(navigation) == 2:
                return Login("online")
        if role not in {"list", "list box", "table", "tree table"}:
            scan |= name in SCAN
            confirm |= name in CONFIRM
            login |= role in {"push button", "button"} and name in {"log in", "登录", "switch account", "切换账号"}
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
    if not scan:
        return Login("login" if login else "unknown")
    window = extents(windows[0])
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

    return inspect(candidates[0], lambda node: node.get_state_set().contains(Atspi.StateType.SHOWING), rect)


def _function(library, name, result, *args):
    fn = getattr(library, name)
    fn.restype = result
    fn.argtypes = args
    return fn


def capture_region(state):
    """Read a bounded region of the verified client's X drawable, never the root."""
    xlib = c.CDLL("libX11.so.6")
    P, W, I, U = c.c_void_p, c.c_ulong, c.c_int, c.c_uint
    x = lambda name, result, *args: _function(xlib, "X" + name, result, *args)
    display = x("OpenDisplay", P, c.c_char_p)(None)
    if not display:
        return None
    query = x("QueryTree", I, P, W, c.POINTER(W), c.POINTER(W), c.POINTER(c.POINTER(W)), c.POINTER(U))
    free = x("Free", I, P)
    root = x("DefaultRootWindow", W, P)(display)
    atom_pid = x("InternAtom", W, P, c.c_char_p, I)(display, b"_NET_WM_PID", 1)
    try:
        if not atom_pid:
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
                if (wx.value, wy.value, ww.value, wh.value) == state.window and depth.value in (24, 32):
                    matches.append(window)
        if len(matches) != 1:
            return None
        rx, ry, w, h = state.region
        left, top = state.window[:2]
        image = x("GetImage", P, P, W, I, I, U, U, W, I)(
            display, matches[0], rx - left, ry - top, w, h, W(-1), 2)
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
    finally:
        x("CloseDisplay", I, P)(display)


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
        parsed = urlsplit(uri)
        if (parsed.scheme == "https" and parsed.hostname in {"login.weixin.qq.com", "weixin.qq.com",
                                                               "wx.qq.com", "web.wechat.com"}
                and not parsed.username and not parsed.password and parsed.port is None
                and parsed.path and all(ch.isprintable() and not ch.isspace() for ch in uri)):
            return uri
        return None
    finally:
        if image:
            z("image_destroy", None, P)(image)
        if scanner:
            z("image_scanner_destroy", None, P)(scanner)


def run(action, inspect_fn=native_status, capture=capture_region, decode=decode_login_qr):
    before = inspect_fn()
    payload = {"state": before.state, "canScan": bool(before.region)}
    if action != "qr" or before.state != "scan" or not before.region:
        return payload
    token = decode(capture(before))
    after = inspect_fn()
    if after != before:
        return {"state": after.state, "canScan": bool(after.region)}
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
