"""On-demand, read-only Moments jobs executed by the existing WeChat UI loop."""

import ctypes
import time

import linux_moments_compat as compat


def verified_window(bridge, app, focus):
    frames = [node for node in compat._children(app) if bridge.role(node) == "frame"
              and bridge.nm(node).strip().casefold() in ("moments", "朋友圈")]
    if len(frames) != 1:
        raise compat.CapabilityError("moments_view_not_verified")
    named = []
    focus.X.XFetchName.argtypes = [ctypes.c_void_p, ctypes.c_ulong,
                                  ctypes.POINTER(ctypes.c_char_p)]
    focus.X.XFree.argtypes = [ctypes.c_void_p]
    for window, geometry in focus.windows().items():
        name = ctypes.c_char_p()
        focus.X.XFetchName(focus.dpy(), window, ctypes.byref(name))
        try:
            if name.value in (b"Moments", "朋友圈".encode()) and min(geometry[2:]) > 150:
                named.append((window, geometry))
        finally:
            if name:
                focus.X.XFree(name)
    if len(named) != 1:
        raise compat.CapabilityError("moments_view_not_verified")
    return frames[0], named[0]


def close_feed(bridge, app, focus):
    frame, (window, geom) = verified_window(bridge, app, focus)
    frame_rect = bridge.ext(frame)
    if not frame_rect or min(frame_rect[2:]) <= 0:
        raise compat.CapabilityError("moments_layout_unverified")
    buttons = [n for n, _ in bridge.walk(frame, maxd=7) if bridge.role(n) == "button"
               and bridge.nm(n).strip().casefold() in ("close", "关闭")]
    if len(buttons) != 1:
        raise compat.CapabilityError("moments_layout_unverified")
    rect = bridge.ext(buttons[0])
    if not rect or min(rect[2:]) <= 0:
        raise compat.CapabilityError("moments_layout_unverified")
    base_x, base_y, base_w, base_h = frame_rect
    x, y, w, h = rect
    if not (base_x + base_w * 0.75 < x < base_x + base_w and base_y <= y < base_y + 60):
        raise compat.CapabilityError("moments_layout_unverified")
    wx, wy, ww, wh = geom
    focus.focus(window)
    bridge.xi.click(round(wx + (x + w/2 - base_x) * ww/base_w),
                    round(wy + (y + h/2 - base_y) * wh/base_h), settle=0.2)
    main_window = focus.main_window()
    if main_window:
        focus.focus(main_window)


def visible_feed(bridge, query):
    import picker_focus as focus
    from gi.repository import Atspi

    app = bridge.get_app()
    if app is None or bridge.blocking_state(app) is not None:
        raise compat.CapabilityError("wechat_session_unverified")
    try:
        verified_window(bridge, app, focus)
    except compat.CapabilityError:
        pass
    else:
        return compat.read_visible(query, app)

    controls = [node for node, _ in bridge.walk(app, maxd=8)
                if bridge.role(node) == "button"
                and bridge.nm(node).strip().casefold() in ("moments", "朋友圈")
                and node.get_state_set().contains(Atspi.StateType.SHOWING)]
    mapped = bridge.pm._mapper(bridge, focus)
    if len(controls) != 1 or not mapped:
        raise compat.CapabilityError("moments_view_not_verified")
    rect = bridge.ext(controls[0])
    window = focus.main_window()
    if not rect or min(rect[2:]) <= 0 or window is None:
        raise compat.CapabilityError("moments_view_not_verified")
    x, y, w, h = mapped[0](rect)
    rx, ry, rw, rh = mapped[1]
    if not (rx <= x <= rx + 100 and ry <= y < ry + rh and 10 <= w <= 120 and 10 <= h <= 120):
        raise compat.CapabilityError("moments_view_not_verified")

    focus.focus(window)
    bridge.xi.click(x + w//2, y + h//2, settle=0.25)
    result = None
    opened = False
    try:
        for _ in range(5):
            try:
                verified_window(bridge, app, focus)
                opened = True
                result = compat.read_visible(query, app)
                break
            except compat.CapabilityError:
                time.sleep(0.2)
        if result is None:
            raise compat.CapabilityError("moments_layout_unverified")
        return result
    finally:
        if opened:
            close_feed(bridge, app, focus)


def poll(bridge, token, account):
    try:
        return compat.poll_once("http://127.0.0.1:19301", account, token,
                                source=lambda query: visible_feed(bridge, query))
    except (OSError, ValueError):
        return "unavailable"
