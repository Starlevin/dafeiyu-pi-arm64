#!/usr/bin/env python3
"""Linux Whale Girl Moments adapter: conservative, read-only AT-SPI path.

- Does not read encrypted databases, private keys, memory or chat history.
- Does not open UI, scroll, like, comment, publish, or activate controls.
- A visible UI feed with an audited row structure is required.
- Worker integration is opt-in; merely importing this file changes nothing.
"""
from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import re
import urllib.request
from urllib.parse import urlparse

READ_ONLY_ACTIONS = frozenset(("read",))
BLOCKED_ACTIONS = frozenset(("like", "unlike", "comment", "reply", "publish"))
MAX_VISIBLE_ITEMS = 20
MAX_NODES = 5000
FEED_NAMES = frozenset(("朋友圈", "moments", "moments feed"))
ROW_ROLES = frozenset(("list item", "listitem", "table row"))
VIEW_ROLES = frozenset(("frame", "dialog", "panel", "filler", "page tab"))
SENSITIVE_FIELDS = frozenset(("username", "location", "latitude", "longitude", "url", "wxid", "comments", "likes"))


class CapabilityError(RuntimeError):
    """A stable error code; never embed user content or paths."""

    def __init__(self, code):
        super().__init__(code)
        self.code = code


def _text(v, size):
    return re.sub(r"\s+", " ", str(v or "")).strip()[:size]


def verify_local_url(url):
    parsed = urlparse(url)
    if parsed.scheme != "http" or not parsed.hostname or parsed.username or parsed.password:
        raise CapabilityError("loopback_required")
    try:
        host = ipaddress.ip_address(parsed.hostname)
    except ValueError:
        raise CapabilityError("loopback_required")
    if not host.is_loopback or parsed.port is None or parsed.path not in ("", "/"):
        raise CapabilityError("loopback_required")
    return url.rstrip("/")


def _children(node):
    try:
        count = node.get_child_count()
        if count < 0 or count > 800:
            return []
        return [node.get_child_at_index(i) for i in range(count)]
    except Exception:
        return []


def _role(node):
    try:
        return (node.get_role_name() or "").lower()
    except Exception:
        return ""


def _name(node):
    try:
        return str(node.get_name() or "")
    except Exception:
        return ""


def _walk(root):
    stack = [(root, 0)]
    seen = 0
    while stack:
        n, depth = stack.pop()
        if n is None:
            continue
        seen += 1
        if seen > MAX_NODES:
            raise CapabilityError("tree_too_large")
        yield n, depth
        if depth < 20:
            stack.extend((c, depth + 1) for c in reversed(_children(n)))


def _get_wechat_application():
    try:
        import gi
        gi.require_version("Atspi", "2.0")
        from gi.repository import Atspi
        desktop = Atspi.get_desktop(0)
        matches = [a for a in _children(desktop)
                   if "wechat" in _name(a).casefold() or "微信" in _name(a)]
        if len(matches) != 1:
            raise CapabilityError("wechat_session_unverified")
        return matches[0]
    except CapabilityError:
        raise
    except Exception:
        raise CapabilityError("atspi_unavailable")


def feed_probe(app=None):
    """Read-only proof of whether a dedicated Moments view is accessible."""
    app = app if app is not None else _get_wechat_application()
    candidates = []
    for n, depth in _walk(app):
        if _role(n) in VIEW_ROLES and _name(n).strip().casefold() in FEED_NAMES:
            row_count = sum(1 for c, _ in _walk(n) if _role(c) in ROW_ROLES)
            candidates.append((n, row_count))
    if len(candidates) != 1:
        raise CapabilityError("moments_view_not_verified")
    view, nrows = candidates[0]
    if nrows == 0:
        raise CapabilityError("moments_layout_unverified")
    return view, nrows


def _summary_row(row):
    lines = _name(row).strip().splitlines()
    if len(lines) < 2 or not 1 <= len(lines[0].strip()) <= 80:
        return None
    text = "\n".join(lines[1:]).strip()
    suffix = re.search(
        r"(?:^|\s)(?:(\d{1,2}) image\(s\)\s+)?(?:(\d{1,2}) video\(s\)\s+)?"
        r"\d{1,4}\s+(?:minute|hour|day)\(s\)\s+ago$",
        text, re.IGNORECASE,
    )
    if not suffix:
        return None
    return (lines[0].strip(), text[:suffix.start()].strip(),
            int(suffix.group(1) or 0), int(suffix.group(2) or 0))


def read_visible(query, app=None):
    """Conservatively read ALREADY OPEN, visible Moments list items.

    The first two directly exposed text labels per list-item are treated as
    publisher and body only when a unique candidate row structure exists.
    Ambiguity always fails closed. No scrolling or background refresh.
    """
    limit = query.get("limit", 20)
    if type(limit) is not int or not 1 <= limit <= MAX_VISIBLE_ITEMS:
        raise CapabilityError("invalid_limit")
    if query.get("own") or query.get("username"):
        raise CapabilityError("unsupported_filter")
    view, _ = feed_probe(app)
    rows = []
    for node, _ in _walk(view):
        if _role(node) not in ROW_ROLES:
            continue
        labels = [_text(_name(c), 6000) for c in _children(node)
                  if _role(c) in ("label", "text", "static text") and _name(c).strip()]
        if 2 <= len(labels) <= 5 and labels[0] and labels[1]:
            publisher, body, images, videos = labels[0][:120], labels[1][:6000], 0, 0
        else:
            parsed = _summary_row(node)
            if parsed is None:
                continue
            publisher, body, images, videos = parsed
        # Avoid duplicate/self-similar row confusion. No hidden IDs are exposed.
        if publisher == body or len(publisher) > 80:
            raise CapabilityError("moments_layout_unverified")
        keyword = _text(query.get("keyword"), 100)
        if keyword and keyword not in body:
            continue
        identity = hashlib.sha256(f"{publisher}\x1f{body}\x1f{images}\x1f{videos}".encode("utf-8")).hexdigest()
        rows.append({
            "id": "visible:" + identity[:24],
            "nickname": publisher,
            "text": body,
            "create_time": 0,  # UI may not expose a trustworthy timestamp.
            "imageCount": images, "videoCount": videos,
            "own": False, "likedByMe": False,
        })
        if len(rows) >= limit:
            break
    if not rows:
        # An empty view may be loading or filtered; do not overwrite snapshots.
        raise CapabilityError("moments_layout_unverified")
    return rows


def execute_job(job, source=read_visible):
    """Implements upstream /api/moments claim/complete reply shape."""
    action = job.get("action", "read")
    if action in BLOCKED_ACTIONS:
        return {"error": "unsupported"}  # no accidental like/unlike flip
    if action not in READ_ONLY_ACTIONS:
        return {"error": "unsupported"}
    try:
        return {"items": source(job.get("query") or {})}
    except CapabilityError as exc:
        return {"error": exc.code}
    except Exception:
        return {"error": "read_failed"}  # hide content and exception details


def _post(url, token, body):
    request = urllib.request.Request(
        url, data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={"content-type": "application/json", "x-bot-token": token},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=2) as response:
        return json.loads(response.read(1024 * 1024).decode("utf-8"))


def poll_once(base_url, account, token, source=read_visible, sender=_post):
    """Opt-in one-shot worker. The core retains its own existing auth checks."""
    base = verify_local_url(base_url)
    if not account or not token:
        raise CapabilityError("missing_account_or_token")
    claim = sender(base + "/api/moments/claim", token, {"account": account})
    job = claim.get("job") if isinstance(claim, dict) else None
    if not job:
        return "no_job"
    if not isinstance(job, dict) or not job.get("id"):
        raise CapabilityError("invalid_job")
    result = execute_job(job, source)
    result.update({"id": job["id"], "account": account})
    sender(base + "/api/moments/complete", token, result)
    return "completed"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--probe", action="store_true", help="AT-SPI only; no UI actions")
    opts = parser.parse_args()
    if not opts.probe:
        parser.error("Only --probe is available in CLI; live polling requires explicit integration.")
    try:
        _, count = feed_probe()
        print(json.dumps({"state": "visible_feed_detected", "visible_rows": count}))
    except CapabilityError as exc:
        print(json.dumps({"state": "unavailable", "reason": exc.code}))
        raise SystemExit(2)


if __name__ == "__main__":
    main()
