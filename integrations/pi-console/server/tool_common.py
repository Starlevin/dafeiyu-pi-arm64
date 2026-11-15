"""Shared primitives for the origin-tool bridge (stdlib only).

Deliberately narrow:
  * One HTTP helper that cannot follow redirects, cannot be pointed at an
    arbitrary URL by request data, and never echoes an upstream body.
  * One version comparator used for "同版既有 API / 未知版本显示限制".
  * Capability metadata so the UI can show what is missing and WHY.

Nothing here reads credentials, message text, or user data.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request


class ToolError(Exception):
    """User-facing Chinese reason for a failed tool operation.

    `status` is the HTTP status the console answers with; `code` is a stable
    machine tag. Anything the upstream said (body, headers, tokens, paths) is
    intentionally dropped.
    """

    def __init__(self, message: str, status: int = 400, code: str = ""):
        super().__init__(message)
        self.message = message
        self.status = status
        self.code = code


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Redirects are a contract violation here, never something to follow."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ToolError("后台接口返回了意外的跳转，已中止本次操作", 502, "unexpected_redirect")


_OPENER = urllib.request.build_opener(_NoRedirect)


def http_json(url, method="GET", body=None, headers=None, timeout=6, limit=8_000_000,
              allow_codes=(200,)):
    """Call a loopback original and decode its JSON.

    `allow_codes` lets a caller treat specific HTTP statuses as data (e.g. the
    QR endpoint answers 401 "please login first" with a useful body).
    """
    raw_body = None
    if body is not None:
        raw_body = json.dumps(body, ensure_ascii=False).encode("utf-8")
    hdrs = {"Accept": "application/json"}
    if raw_body is not None:
        hdrs["Content-Type"] = "application/json"
    if headers:
        hdrs.update(headers)
    req = urllib.request.Request(url, data=raw_body, method=method, headers=hdrs)
    try:
        with _OPENER.open(req, timeout=timeout) as resp:
            data = resp.read(limit)
            status = resp.status
    except urllib.error.HTTPError as e:
        if e.code in allow_codes:
            try:
                return json.loads(e.read(limit) or b"{}")
            except (ValueError, OSError):
                return {}
        if e.code in (401, 403):
            raise ToolError("后台拒绝了本次访问，请检查服务状态", 502, "upstream_denied") from e
        if e.code == 404:
            raise ToolError("后台没有这个接口，可能是版本不一致", 502, "upstream_missing") from e
        raise ToolError("后台控制接口暂时不可用，请稍后再试", 502, "upstream_error") from e
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise ToolError("后台控制接口暂时不可用，请稍后再试", 502, "upstream_unreachable") from e
    if not data:
        return {}
    try:
        return json.loads(data)
    except ValueError as e:
        raise ToolError("后台返回的内容无法解析", 502, "upstream_bad_json") from e


# --------------------------------------------------------------------------- #
# Version comparison ("适配同版既有 API、未知版本显示限制")
# --------------------------------------------------------------------------- #


def parse_version(text):
    """Return a comparable tuple, or None when the version is not readable.

    None means UNKNOWN: callers show their limits instead of guessing.
    """
    if not isinstance(text, str):
        return None
    s = text.strip()
    if not s:
        return None
    if s[:1] in ("v", "V"):
        s = s[1:]
    main = s.split("-", 1)[0].split("+", 1)[0]
    parts = main.split(".")
    if not 1 <= len(parts) <= 4:
        return None
    out = []
    for p in parts:
        if not p.isdigit():
            return None
        out.append(int(p))
    while len(out) < 4:
        out.append(0)
    return tuple(out)


def version_at_least(actual, minimum):
    """True / False / None (unknown) for "actual >= minimum"."""
    a = parse_version(actual)
    m = parse_version(minimum)
    if a is None or m is None:
        return None
    return a >= m


# --------------------------------------------------------------------------- #
# Capability rows
# --------------------------------------------------------------------------- #


def capability(area, key, title, level, note, writable=False, confirm=False,
               available=True, missing_reason=""):
    """One row of the capability table.

    level: read | action | denied
    confirm: the page must send an explicit one-off confirmation
    available/missing_reason: set False when the installed version really lacks
    the action -- the UI then says why instead of hiding the row.
    """
    return {
        "area": area,
        "id": key,
        "title": title,
        "level": level,
        "note": note,
        "writable": bool(writable),
        "confirm": bool(confirm),
        "available": bool(available),
        "missingReason": missing_reason if not available else "",
    }
