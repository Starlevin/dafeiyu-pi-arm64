#!/usr/bin/env python3
"""大肥鱼统一控制台 — deployable stdlib-only server.

Two listeners:
  * Optional LAN HTTPS binds only to the explicitly configured host and needs
    operator-provided certificates; it is disabled by default.
  * Loopback HTTP on 127.0.0.1:18444 (only reachable through SSH local forwarding;
    this is the "本机浏览器" path used for first-time setup / password reset)

Security properties (per brief):
  * Password is stored ONLY as a strong salted KDF (PBKDF2-HMAC-SHA256,
    600k iterations, 16-byte random salt). The plaintext is never written.
  * Session cookie: random 32-byte token, server-side store, TTL, HttpOnly,
    Secure on HTTPS, SameSite=Strict, Path=/. No CSRF token needed because
    SameSite=Strict + strict Origin/Host + JSON-only writes.
  * Constant-time comparison for password and cookie tokens (hmac.compare_digest).
  * Rate limiting per client IP with a lockout window (login / setup / password).
  * Strict Host allowlist and Origin check (same-origin for writes).
  * LAN source CIDR check is operator-configured; defaults accept loopback only.
    The loopback HTTP listener accepts only 127.0.0.1 / ::1.
  * First-time setup and password reset are ONLY possible from the loopback
    HTTP path (本机浏览器), never from LAN. `setupAvailable` reflects this.
  * /api/ca is loopback-only; the cert is served without its private key.
  * Management keys (model-hub admin.key etc.) are read by adapters.py in the
    service process through the existing private interface, never printed,
    never returned over the API, never copied to the static dir.
  * No existing internal secret is exposed by any endpoint.

No message text, no secrets, no cookies are ever logged.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import ipaddress
import json
import os
import secrets
import ssl
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import adapters
import usage_adapter
import content
import host_status
import tool_routes
try:
    import social_sidecar
except ImportError:
    social_sidecar = None
try:
    import moments_console
except ImportError:
    moments_console = None
try:
    import house_console_ext
except ImportError:
    house_console_ext = None
try:
    # QQ 空间（qq-qzone 模块）是叠加式可选接入：缺模块时控制台照常启动，
    # 只是没有 /api/qzone/* 这组路由。
    import tool_routes_qzone
except ImportError:
    tool_routes_qzone = None

HERE = Path(__file__).resolve().parent
DEFAULT_UI = HERE.parent / "ui"


def default_state_dir() -> Path:
    """Return a private per-user state location, never a path inside this source tree."""
    configured = os.environ.get("DAFEIYU_CONSOLE_STATE_HOME")
    if configured:
        return Path(configured).expanduser()
    xdg = os.environ.get("XDG_STATE_HOME")
    if xdg:
        return Path(xdg).expanduser() / "dafeiyu-console"
    return Path.home() / ".local" / "state" / "dafeiyu-console"


def configured_qq_root(cfg) -> str:
    """The QQ data/key root is opt-in; never guess a deployment path."""
    return str(cfg.get("qqRoot") or os.environ.get("DAFEIYU_QQ_ROOT") or "").strip()


def qzone_read_enabled() -> bool:
    """QZone read API is an explicit deployment opt-in; default is off."""
    return os.environ.get("DAFEIYU_QZONE_READ_ENABLED", "").strip() == "1"

# --------------------------------------------------------------------------- #
# Password store (strong salted KDF only)
# --------------------------------------------------------------------------- #

KDF_ITERATIONS = 600_000
SALT_BYTES = 16
KEY_BYTES = 32
PASSWORD_MIN = 8
PASSWORD_MAX = 256


class PasswordStore:
    """A tiny file-based store. Format: {"salt":b64,"hash":b64,"iters":int}."""

    def __init__(self, path: Path, iterations: int = KDF_ITERATIONS):
        self.path = path
        # Iterations are tunable for constrained hardware (Pi 3B); the shipped
        # default is strong. Each record stores its own count so a change never
        # invalidates an existing password.
        self.iterations = max(1000, int(iterations))
        self.lock = threading.Lock()

    def exists(self):
        return self.path.exists() and self._load() is not None

    def _load(self):
        try:
            d = json.loads(self.path.read_text(encoding="utf-8"))
            if not all(k in d for k in ("salt", "hash", "iters")):
                return None
            return d
        except (OSError, ValueError):
            return None

    @staticmethod
    def _derive(password: str, salt: bytes, iters: int) -> bytes:
        return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt,
                                   iters, dklen=KEY_BYTES)

    @staticmethod
    def validate(password: str):
        if not isinstance(password, str) or not PASSWORD_MIN <= len(password) <= PASSWORD_MAX:
            raise ValueError("密码长度须为 %d 到 %d 个字符" % (PASSWORD_MIN, PASSWORD_MAX))

    def set(self, password: str):
        self.validate(password)
        salt = secrets.token_bytes(SALT_BYTES)
        digest = self._derive(password, salt, self.iterations)
        record = {
            "salt": base64.b64encode(salt).decode(),
            "hash": base64.b64encode(digest).decode(),
            "iters": self.iterations,
        }
        with self.lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            # Atomic write, owner-only.
            tmp = self.path.with_suffix(".tmp")
            fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(record, f)
            finally:
                try:
                    os.chmod(str(tmp), 0o600)
                except OSError:
                    pass
            os.replace(str(tmp), str(self.path))

    def verify(self, password: str) -> bool:
        with self.lock:
            rec = self._load()
        if rec is None or not isinstance(password, str):
            return False
        try:
            salt = base64.b64decode(rec["salt"])
            expected = base64.b64decode(rec["hash"])
            iters = int(rec["iters"])
        except (ValueError, KeyError, TypeError):
            return False
        actual = self._derive(password, salt, iters)
        # Constant-time. Length mismatch is handled by compare_digest too.
        return hmac.compare_digest(actual, expected)


# --------------------------------------------------------------------------- #
# Pair / reset token store (short-lived, single-use, minted on the Pi via SSH)
# --------------------------------------------------------------------------- #

class PairStore:
    """Reads a token file written by pair_token.py on the Pi.

    TokenSave semantics: the token file is the single source of truth; an
    unexpired, unconsumed token is reused ("尽量先用 TokenSave"), and a new one
    is minted only when the saved one is missing/expired/consumed.
    """

    def __init__(self, path: Path, ttl: int = 180):
        self.path = path
        self.ttl = ttl
        self.lock = threading.Lock()

    def _read(self):
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def _write(self, data):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f)
        os.chmod(str(tmp), 0o600)
        os.replace(str(tmp), str(self.path))

    def consume(self, token: str, expect_reset: bool) -> str | None:
        """Validate + consume atomically. Returns 'setup' or 'reset', or None."""
        with self.lock:
            rec = self._read()
            if not isinstance(rec, dict):
                return None
            if not self._valid(rec, token, expect_reset):
                return None
            rec["consumed"] = True
            rec["consumed_at"] = time.time()
            self._write(rec)
            return "reset" if expect_reset else "setup"

    @staticmethod
    def _valid(rec, token, expect_reset) -> bool:
        saved = rec.get("token") or ""
        if not saved or not hmac.compare_digest(str(saved), str(token or "")):
            return False
        if not isinstance(rec.get("expires"), (int, float)) or time.time() > rec["expires"]:
            return False
        if bool(rec.get("consumed")):
            return False
        return bool(rec.get("reset")) == bool(expect_reset)

    def peek_reset(self, token: str):
        """Read-only: True/False if a valid token is reset/setup, else None."""
        with self.lock:
            rec = self._read()
            if not isinstance(rec, dict) or not rec.get("token"):
                return None
            if not hmac.compare_digest(str(rec["token"]), str(token or "")):
                return None
            if (not isinstance(rec.get("expires"), (int, float))
                    or time.time() > rec["expires"] or bool(rec.get("consumed"))):
                return None
            return bool(rec.get("reset"))

    def mint(self, reset: bool = False) -> str:
        """Mint a fresh token (called only over SSH on the Pi by pair_token.py)."""
        token = secrets.token_urlsafe(24)
        with self.lock:
            self._write({
                "token": token,
                "expires": time.time() + self.ttl,
                "reset": bool(reset),
                "consumed": False,
                "created": time.time(),
            })
        return token


# --------------------------------------------------------------------------- #
# Session store (server-side, TTL)
# --------------------------------------------------------------------------- #

class SessionStore:
    COOKIE = "dafeiyu_session"

    def __init__(self, ttl: int = 86400):
        self.ttl = ttl
        self.lock = threading.Lock()
        self.tokens: dict[str, float] = {}

    def create(self) -> str:
        token = secrets.token_urlsafe(32)
        with self.lock:
            self.tokens[token] = time.time() + self.ttl
        return token

    def valid(self, token: str | None) -> bool:
        if not token:
            return False
        with self.lock:
            expiry = self.tokens.get(token)
            if expiry is None:
                return False
            if time.time() > expiry:
                self.tokens.pop(token, None)
                return False
            return True

    def destroy(self, token: str | None):
        if not token:
            return
        with self.lock:
            self.tokens.pop(token, None)

    def revoke_all(self):
        """Revoke every session atomically (used on password change / reset)."""
        with self.lock:
            self.tokens.clear()

    def revoke_all_and_create(self) -> str:
        """Atomically revoke all sessions and issue exactly one fresh token.

        Serialized by the caller via App.auth_lock, but the clear+insert pair is
        also atomic here so no other thread can slip a session in between.
        """
        token = secrets.token_urlsafe(32)
        with self.lock:
            self.tokens.clear()
            self.tokens[token] = time.time() + self.ttl
        return token

    def sweep(self):
        now = time.time()
        with self.lock:
            for t, exp in list(self.tokens.items()):
                if exp <= now:
                    self.tokens.pop(t, None)

    def cookie_header(self, token: str, secure: bool, name: str | None = None) -> str:
        # Max-Age mirrors the server-side TTL.
        parts = [
            "%s=%s" % (name or self.COOKIE, token),
            "Path=/",
            "HttpOnly",
            "SameSite=Strict",
            "Max-Age=%d" % self.ttl,
        ]
        if secure:
            parts.append("Secure")
        return "; ".join(parts)

    def clear_header(self, secure: bool, name: str | None = None) -> str:
        parts = [
            "%s=" % (name or self.COOKIE),
            "Path=/",
            "HttpOnly",
            "SameSite=Strict",
            "Max-Age=0",
        ]
        if secure:
            parts.append("Secure")
        return "; ".join(parts)


def parse_cookie(header: str, name: str) -> str | None:
    if not header:
        return None
    for part in header.split(";"):
        k, _, v = part.strip().partition("=")
        if k == name:
            return v
    return None


# --------------------------------------------------------------------------- #
# Rate limiter (per client address, sliding lockout)
# --------------------------------------------------------------------------- #

class RateLimiter:
    def __init__(self, max_attempts: int = 8, window: int = 60, lockout: int = 300):
        self.max_attempts = max_attempts
        self.window = window
        self.lockout = lockout
        self.lock = threading.Lock()
        self.events: dict[str, list[float]] = {}
        self.blocked: dict[str, float] = {}

    def check(self, key: str) -> bool:
        now = time.time()
        with self.lock:
            until = self.blocked.get(key, 0)
            if now < until:
                return False
            if until and now >= until:
                self.blocked.pop(key, None)
            arr = [t for t in self.events.get(key, []) if now - t < self.window]
            self.events[key] = arr
            return len(arr) < self.max_attempts

    def fail(self, key: str):
        now = time.time()
        with self.lock:
            arr = [t for t in self.events.get(key, []) if now - t < self.window]
            arr.append(now)
            self.events[key] = arr
            if len(arr) >= self.max_attempts:
                self.blocked[key] = now + self.lockout

    def reset(self, key: str):
        with self.lock:
            self.events.pop(key, None)
            self.blocked.pop(key, None)


# --------------------------------------------------------------------------- #
# Request handler
# --------------------------------------------------------------------------- #

STATIC = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/app.css": ("app.css", "text/css; charset=utf-8"),
    "/app.js": ("app.js", "application/javascript; charset=utf-8"),
    "/avatar.webp": ("avatar.webp", "image/webp"),
    "/usage.js": ("usage.js", "application/javascript; charset=utf-8"),
    "/usage.css": ("usage.css", "text/css; charset=utf-8"),
    "/content.css": ("content.css", "text/css; charset=utf-8"),
    "/content.js": ("content.js", "application/javascript; charset=utf-8"),
    "/social.js": ("social.js", "application/javascript; charset=utf-8"),
    "/admin-tools.css": ("admin-tools.css", "text/css; charset=utf-8"),
    "/admin-tools.js": ("admin-tools.js", "application/javascript; charset=utf-8"),
    "/qzone-panel.js": ("qzone-panel.js", "application/javascript; charset=utf-8"),
}


CONTENT_POST = ("/api/content/read", "/api/content/search",
                "/api/content/subscribe", "/api/content/ask",
                "/api/content/summary", "/api/content/platform-scan",
                "/api/content/moments")


class ConsoleServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 16
    lan_listener = False

    def __init__(self, addr, handler, app):
        super().__init__(addr, handler)
        self.app = app

    def process_request(self, request, client_address):
        # Bound the number of concurrent handler threads on the 1GB Pi. If the
        # semaphore is full, close the connection immediately rather than
        # queueing an unbounded number of TLS handshakes / threads.
        if not self.app.request_semaphore.acquire(blocking=False):
            try:
                request.close()
            except OSError:
                pass
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self.app.request_semaphore.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.app.request_semaphore.release()


class Handler(BaseHTTPRequestHandler):
    server_version = "DaFeiYuConsole/1.0"
    protocol_version = "HTTP/1.1"

    # Never log request URLs: the pair fragment never reaches the server, but
    # query strings and cookies must not be written to any log either.
    def log_message(self, *args):
        pass

    def setup(self):
        super().setup()
        # A stalled LAN/HTTPS client must not hold a handler thread forever.
        try:
            self.connection.settimeout(self.server.app.socket_timeout)
        except (OSError, AttributeError):
            pass

    # ---- context ---------------------------------------------------------- #

    @property
    def app(self):
        return self.server.app

    def client_ip(self) -> str:
        return self.client_address[0]

    def is_loopback(self) -> bool:
        try:
            addr = ipaddress.ip_address(self.client_ip())
        except ValueError:
            return False
        if addr.is_loopback:
            return True
        return bool(getattr(addr, "ipv4_mapped", None)
                    and addr.ipv4_mapped.is_loopback)

    def is_secure(self) -> bool:
        return isinstance(self.connection, ssl.SSLSocket)

    def is_lan(self) -> bool:
        return self.server.lan_listener or self.is_secure()

    def is_local_setup(self) -> bool:
        return not self.is_lan() and self.is_loopback()

    def cookie_name(self) -> str:
        if self.is_lan() and not self.is_secure():
            return "dafeiyu_lan_session"
        return self.app.sessions.COOKIE

    def lan_allowed(self) -> bool:
        if self.is_local_setup():
            return True
        if not self.is_lan():
            return False
        try:
            addr = ipaddress.ip_address(self.client_ip())
        except ValueError:
            return False
        return any(addr in net for net in self.app.lan_nets)

    def host_ok(self) -> bool:
        """Strict Host: exactly the expected hostname AND port for this listener.

        Rejects userinfo, missing/wrong port, and hostname aliases. The loopback
        setup listener accepts loopback names; the LAN listener accepts only
        lan_host on lan_port.
        """
        host = (self.headers.get("Host") or "").strip()
        if not host or "@" in host or " " in host:
            return False
        # Split host and port without treating an IPv6 literal's colons as port.
        if host.startswith("["):
            close = host.find("]")
            if close < 0:
                return False
            hostname = host[1:close].lower()
            rest = host[close + 1:]
            port = rest[1:] if rest.startswith(":") else ""
        else:
            hostname, _, port = host.partition(":")
            hostname = hostname.strip().lower()
        if not port.isdecimal():
            return False
        # Compare against the port this listener ACTUALLY bound (handles an
        # ephemeral/configured port) rather than the configured value.
        try:
            bound_port = self.server.server_address[1]
        except (AttributeError, IndexError):
            bound_port = self.app.loop_port
        if self.is_lan():
            allowed_hosts = {self.app.lan_host.lower()}
        else:
            allowed_hosts = {"127.0.0.1", "localhost"}
            # extraHosts extend the loopback HTTP names only (never the LAN TLS host).
            for h in self.app.extra_hosts:
                allowed_hosts.add(h.lower())
        return hostname in allowed_hosts and int(port) == bound_port

    def origin_ok(self) -> bool:
        """Browser writes must carry a same-origin Origin that exactly matches
        the actual Host:port. Reads may omit Origin (non-browser clients), but
        any Origin that is present must still be valid.
        """
        origin = self.headers.get("Origin")
        if origin is None:
            return True  # non-browser clients; writes still need JSON + cookie
        host = (self.headers.get("Host") or "").strip()
        scheme = "https" if self.is_secure() else "http"
        # Exactly the origin the browser is actually talking to — no aliases,
        # no canonical-LAN fallback. A mismatched Origin is always rejected.
        return origin == "%s://%s" % (scheme, host)

    # ---- low-level response ---------------------------------------------- #

    def _send(self, code: int, body: bytes, ctype: str, extra=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Permissions-Policy", "geolocation=(), microphone=(), camera=()")
        self.send_header("Content-Security-Policy",
                         "default-src 'self'; script-src 'self'; style-src 'self'; "
                         "img-src 'self' data:; connect-src 'self'; object-src 'none'; "
                         "base-uri 'none'; form-action 'self'; frame-ancestors 'none'")
        if extra:
            for k, v in extra:
                self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

    def json(self, code: int, obj, extra=None):
        raw = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self._send(code, raw, "application/json; charset=utf-8", extra)

    def fail(self, code: int, message: str):
        self.json(code, {"error": message})

    def ok(self, extra=None):
        self.json(200, {"ok": True}, extra)

    # ---- request gates ---------------------------------------------------- #

    def gate(self) -> bool:
        """Returns False after it has already sent an error response."""
        if not self.host_ok():
            self.fail(403, "不允许的访问入口")
            return False
        if not self.lan_allowed():
            self.fail(403, "仅允许本机或指定局域网访问")
            return False
        if not self.origin_ok():
            self.fail(403, "请求来源无效，请从本控制台页面操作")
            return False
        return True

    def read_json(self):
        ctype = (self.headers.get("Content-Type") or "").lower()
        if not ctype.startswith("application/json"):
            raise ValueError("请求需要 JSON 格式")
        if self.headers.get("Transfer-Encoding"):
            raise ValueError("不支持分块请求")
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            raise ValueError("请求长度无效")
        if not 0 < length <= 64_000:
            raise ValueError("请求长度无效")
        try:
            data = json.loads(self.rfile.read(length))
        except (ValueError, UnicodeDecodeError):
            raise ValueError("请求内容无法解析")
        if not isinstance(data, dict):
            raise ValueError("请求内容须为对象")
        return data

    def authed(self) -> bool:
        token = parse_cookie(self.headers.get("Cookie", ""), self.cookie_name())
        return self.app.sessions.valid(token)

    def need_auth(self) -> bool:
        if self.authed():
            return True
        self.fail(401, "登录状态已失效，请重新登录")
        return False

    def rate_gate(self) -> bool:
        if self.app.limiter.check(self.client_ip()):
            return True
        self.fail(429, "尝试次数过多，请稍后再试")
        return False

    # ---- GET -------------------------------------------------------------- #

    def do_GET(self):
        try:
            if not self.gate():
                return
            path = urlsplit(self.path).path

            if path == "/api/session":
                return self.api_session()
            if path == "/api/overview":
                if not self.need_auth():
                    return
                return self.api_overview()
            if path == "/api/usage":
                if not self.need_auth():
                    return
                return self.json(200, self.app.usage.section())
            if path == "/api/host/status":
                if not self.need_auth():
                    return
                return self.json(200, self.app.host_status.section())
            if house_console_ext is not None and house_console_ext.resolve(self, "GET", path):
                return
            if path == "/api/ca":
                return self.api_ca()
            if path in ("/api/content/capabilities", "/api/content/feed"):
                if not self.need_auth():
                    return
                return self.api_content("GET", path)
            if path in STATIC:
                return self.static(path)
            if tool_routes.resolve(self, "GET", path):
                return
            if (qzone_read_enabled() and tool_routes_qzone is not None
                    and tool_routes_qzone.resolve(self, "GET", path)):
                return
            self.fail(404, "页面不存在")
        except (adapters.AdapterError, content.ContentError) as e:
            self.fail(e.status, e.message)
        except Exception:
            self.fail(500, "服务器暂时无法完成请求，请稍后再试")

    def api_session(self):
        # `setupAvailable` = a password has never been set AND this is the
        # loopback HTTP path. Password recovery (an existing password) uses a
        # reset token instead; the page learns that from the URL fragment.
        setup_available = not self.app.passwords.exists() and self.is_local_setup()
        self.json(200, {
            "authenticated": self.authed(),
            "setupAvailable": setup_available,
            "secure": self.is_secure(),
            "lanUrl": self.app.lan_url,
        })

    def api_overview(self):
        try:
            data = self.app.console.overview()
        except adapters.AdapterError as e:
            self.fail(e.status, e.message)
            return
        self.json(200, data)

    def api_content(self, method, path, body=None):
        """Read-only social content surface (X / 抖音 / B站 / 小红书).

        Deliberately isolated from `self.app.console`: nothing here can reach a
        platform account, an outbound message, or the QQ/WeChat write paths.
        All upstream network access happens inside content.py, which itself has
        no write capability. `limited` answers additionally carry Retry-After so
        a client backs off instead of retrying in a loop.
        """
        cfg = self.app.content_cfg
        if method == "GET":
            if path == "/api/content/capabilities":
                return self.json(200, content.capabilities(cfg))
            if path == "/api/content/feed":
                query = parse_qs(urlsplit(self.path).query)
                return self.json(200, content.handle_feed(cfg, query))
            self.fail(404, "操作不存在")
            return
        # POST — same-origin + JSON + session already enforced by the caller.
        if path == "/api/content/read":
            data = content.handle_read(cfg, body)
        elif path == "/api/content/search":
            data = content.handle_search(cfg, body)
        elif path == "/api/content/subscribe":
            data = content.handle_subscribe(cfg, body)
        elif path == "/api/content/ask":
            data = content.handle_ask(cfg, body)
        elif path == "/api/content/summary":
            data = content.handle_summary(cfg, body)
        elif path == "/api/content/platform-scan":
            if social_sidecar is None:
                self.fail(503, "社媒巡检模块未部署")
                return
            data = social_sidecar.scan(body)
        elif path == "/api/content/moments":
            if moments_console is None:
                self.fail(503, "朋友圈读取模块未部署")
                return
            data = moments_console.read(body)
        else:
            self.fail(404, "操作不存在")
            return
        extra = None
        if isinstance(data, dict):
            retry = data.get("retryAfter")
            limited = data.get("limited") is True or (
                isinstance(data.get("item"), dict)
                and data["item"].get("coverage") == "limited")
            if limited and isinstance(retry, int) and 0 < retry <= 3600:
                extra = [("Retry-After", str(retry))]
        self.json(200, data, extra)

    def api_ca(self):
        # Loopback only: the CA cert (public) is safe to hand out, but we keep
        # this on the本机 path so a LAN visitor cannot enumerate cert details.
        if not self.is_local_setup() or self.app.lan_http:
            self.fail(403, "CA 证书仅可在本机下载")
            return
        p = self.app.ca_cert
        if not p or not p.exists():
            self.fail(404, "尚未部署 CA 证书")
            return
        data = p.read_bytes()
        self._send(200, data, "application/x-x509-ca-cert",
                   [("Content-Disposition", 'attachment; filename="dafeiyu-ca.crt"')])

    def static(self, path):
        name, ctype = STATIC[path]
        p = self.app.ui_dir / name
        if not p.is_file():
            if name == "avatar.webp":
                self.fail(404, "没有头像文件")
            else:
                self.fail(404, "页面文件缺失")
            return
        try:
            data = p.read_bytes()
        except OSError:
            self.fail(500, "页面读取失败")
            return
        self._send(200, data, ctype)

    # ---- POST ------------------------------------------------------------- #

    def do_POST(self):
        try:
            if not self.gate():
                return
            path = urlsplit(self.path).path
            if tool_routes.resolve(self, "POST", path):
                return
            if house_console_ext is not None and house_console_ext.resolve(self, "POST", path):
                return
            if (qzone_read_enabled() and tool_routes_qzone is not None
                    and tool_routes_qzone.resolve(self, "POST", path)):
                return
            content_write = path in CONTENT_POST
            if path not in ("/api/setup", "/api/login", "/api/password",
                            "/api/logout", "/api/conversation", "/api/model") and not content_write:
                self.fail(404, "操作不存在")
                return
            try:
                body = self.read_json()
            except ValueError as e:
                self.fail(400, str(e))
                return

            if path == "/api/setup":
                return self.api_setup(body)
            if path == "/api/login":
                return self.api_login(body)
            if path == "/api/password":
                return self.api_password(body)
            if path == "/api/logout":
                return self.api_logout(body)
            if not self.need_auth():
                return
            if content_write:
                return self.api_content("POST", path, body)
            if path == "/api/conversation":
                return self.api_conversation(body)
            if path == "/api/model":
                return self.api_model(body)
        except (adapters.AdapterError, content.ContentError) as e:
            self.fail(e.status, e.message)
        except Exception:
            self.fail(500, "服务器暂时无法完成请求，请稍后再试")

    def api_setup(self, body):
        # First-time setup AND password recovery are本机 (loopback HTTP) only.
        if not self.is_local_setup():
            self.fail(403, "首次设置与密码恢复只能在本机浏览器完成")
            return
        if not self.rate_gate():
            return
        pair = body.get("pair")
        password = body.get("password")
        if not isinstance(pair, str) or not pair:
            self.fail(400, "缺少本机配对令牌")
            return
        has_password = self.app.passwords.exists()
        token_kind = self.app.pair_store.peek_reset(pair)  # None / False / True
        if token_kind is None:
            self.app.limiter.fail(self.client_ip())
            self.fail(400, "配对令牌无效、已过期或已使用，请重新生成本机链接")
            return
        # The token's own kind is authoritative. A setup token cannot overwrite
        # an existing password; a reset token requires one. The body's `reset`
        # flag is checked for consistency but never decides authorization.
        if token_kind is False and has_password:
            self.fail(409, "密码已设置；如需恢复请在本机生成重置链接")
            return
        if token_kind is True and not has_password:
            self.fail(409, "尚未设置密码，请改用首次设置链接")
            return
        if "reset" in body and bool(body.get("reset")) != bool(token_kind):
            self.fail(400, "链接类型与当前状态不符，请重新生成本机链接")
            return
        # Validate the new password BEFORE burning the single-use token, so a
        # too-short password does not cost the user their only setup link.
        try:
            self.app.passwords.validate(password)
        except ValueError as e:
            self.fail(400, str(e))
            return
        kind = self.app.pair_store.consume(pair, bool(token_kind))
        if kind is None:
            self.fail(400, "配对令牌无效、已过期或已使用，请重新生成本机链接")
            return
        try:
            self.app.passwords.set(password)
        except ValueError as e:
            self.fail(400, str(e))
            return
        self.app.limiter.reset(self.client_ip())
        # setup AND reset both revoke every existing session atomically, then
        # issue exactly one fresh session. Serialized so no concurrent login can
        # slip in between revoke and issue.
        with self.app.auth_lock:
            token = self.app.sessions.revoke_all_and_create()
        self.ok([("Set-Cookie", self.app.sessions.cookie_header(token, False))])

    def api_login(self, body):
        if not self.rate_gate():
            return
        password = body.get("password")
        # Verify + issue under one lock so a concurrent password change/reset
        # cannot revoke in the middle and leave a stale session alive.
        with self.app.auth_lock:
            if not self.app.passwords.verify(password):
                self.app.limiter.fail(self.client_ip())
                self.fail(401, "密码不正确")
                return
            self.app.limiter.reset(self.client_ip())
            token = self.app.sessions.create()
        self.ok([("Set-Cookie", self.app.sessions.cookie_header(
            token, self.is_secure(), self.cookie_name()))])

    def api_password(self, body):
        if not self.need_auth():
            return
        if not self.rate_gate():
            return
        current = body.get("current")
        password = body.get("password")
        # Verify old password, write new one, revoke ALL sessions and issue one
        # fresh token — all serialized with login/setup/reset.
        with self.app.auth_lock:
            if not self.app.passwords.verify(current):
                self.app.limiter.fail(self.client_ip())
                self.fail(401, "当前密码不正确")
                return
            try:
                self.app.passwords.set(password)
            except ValueError as e:
                self.fail(400, str(e))
                return
            token = self.app.sessions.revoke_all_and_create()
        self.app.limiter.reset(self.client_ip())
        self.ok([("Set-Cookie", self.app.sessions.cookie_header(
            token, self.is_secure(), self.cookie_name()))])

    def api_logout(self, body):
        cookie = parse_cookie(self.headers.get("Cookie", ""), self.cookie_name())
        self.app.sessions.destroy(cookie)
        self.ok([("Set-Cookie", self.app.sessions.clear_header(
            self.is_secure(), self.cookie_name()))])

    def api_conversation(self, body):
        platform = body.get("platform")
        conv_id = body.get("id")
        field = body.get("field")
        if platform not in ("qq", "wechat"):
            self.fail(400, "平台无效")
            return
        if not isinstance(conv_id, str) or not conv_id:
            self.fail(400, "会话标识无效")
            return
        if field not in ("enabled", "style", "frequency", "limit"):
            self.fail(400, "设置项无效")
            return
        if "value" not in body:
            self.fail(400, "缺少设置值")
            return
        # Validate value shapes before handing to the adapter.
        value = body["value"]
        if field == "enabled" and not isinstance(value, bool):
            self.fail(400, "开关需要 true 或 false")
            return
        if field == "limit" and (isinstance(value, bool) or not isinstance(value, int)
                                  or not 0 <= value <= 10000):
            self.fail(400, "限额须为 0 到 10000 的整数；0 表示不限制")
            return
        if field in ("style", "frequency") and not isinstance(value, str):
            self.fail(400, "设置值无效")
            return
        try:
            self.app.console.set_conversation(platform, conv_id, field, value)
        except adapters.AdapterError as e:
            self.fail(e.status, e.message)
            return
        self.ok()

    def api_model(self, body):
        platform = body.get("platform")
        model_id = body.get("id")
        if platform not in ("qq", "wechat") or not isinstance(model_id, str) or not model_id:
            self.fail(400, "平台或模型标识无效")
            return
        try:
            self.app.console.set_model(platform, model_id)
        except adapters.AdapterError as e:
            self.fail(e.status, e.message)
            return
        # Additive: read the model back and report how sure we are. Clients that
        # only look at `ok` keep working; `verification.state` is one of
        # confirmed / mismatch / unconfirmed (never a silent success).
        verification = None
        try:
            verification = self.app.console.verify_model(platform, model_id)
        except adapters.AdapterError:
            verification = {"platform": platform, "requested": model_id,
                            "state": "unconfirmed", "observed": None,
                            "lastRead": None}
        self.json(200, {"ok": True, "verification": verification})

    # HEAD for completeness (no body, no data).
    def do_HEAD(self):
        self.do_GET()


# --------------------------------------------------------------------------- #
# Application wiring
# --------------------------------------------------------------------------- #

class App:
    def __init__(self, cfg, fixture=None):
        self.state_dir = Path(cfg.get("stateDir") or default_state_dir()).expanduser()
        self.state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            os.chmod(self.state_dir, 0o700)
        except OSError:
            pass
        self.passwords = PasswordStore(self.state_dir / "password.json",
                                       iterations=int(cfg.get("kdfIterations", KDF_ITERATIONS)))
        self.pair_store = PairStore(self.state_dir / "pair-token.json",
                                    ttl=int(cfg.get("pairTtl", 180)))
        self.sessions = SessionStore(ttl=int(cfg.get("sessionTtl", 86400)))
        self.limiter = RateLimiter()
        # Serializes password verification + session issuance / revocation so a
        # concurrent login cannot slip in while a password change/reset is
        # revoking every session.
        self.auth_lock = threading.RLock()
        self.console = adapters.Console(cfg, fixture)
        self.usage = usage_adapter.UsageAdapter(cfg, fixture)
        self.host_status = host_status.HostStatus(
            self.state_dir, enabled=fixture is None and sys.platform.startswith("linux"))
        self.fixture = fixture
        tool_routes.bind_config(self, cfg)
        content_cfg = dict(cfg)
        content_cfg.setdefault("contentStateDir",
                               str(self.state_dir / "content"))
        # Model gateway: reuse the existing local model gateway. The config holds
        # only the gateway ADDRESS and a key-file PATH — never the key itself, and
        # never a path taken from a request.
        content_cfg.setdefault("modelBaseUrl",
                               cfg.get("qqHubUrl", "http://127.0.0.1:18090").rstrip("/") + "/v1")
        content_cfg.setdefault("modelName", cfg.get("modelName", ""))
        qq_root = configured_qq_root(cfg)
        content_cfg.setdefault("modelKeyPath",
                               content.DEFAULT_GATEWAY_KEY_REL if qq_root else "")
        content_cfg["qqRoot"] = qq_root
        fixture_content = (fixture or {}).get("content") if isinstance(fixture, dict) else None
        if isinstance(fixture_content, dict):
            # Offline fixture mode: capabilities are granted and every page read
            # is answered from the fixture. Nothing leaves the process. The
            # tester injects its own `fetcher` to exercise limits/failures.
            content_cfg["fixture"] = fixture_content
            content_cfg["fetcher"] = _fixture_fetcher(fixture_content)
        self.content_cfg = content_cfg


        self.ui_dir = Path(cfg.get("uiDir", str(DEFAULT_UI))).resolve()
        self.lan_host = cfg.get("lanHost") or "127.0.0.1"
        self.lan_port = int(cfg.get("lanPort", 18443))
        self.loop_port = int(cfg.get("loopPort", 18444))
        self.lan_http = cfg.get("lanHttp") is True
        scheme = "http" if self.lan_http else "https"
        self.lan_url = "%s://%s:%d" % (scheme, self.lan_host, self.lan_port)
        self.tls_cert = Path(cfg["tlsCert"]) if cfg.get("tlsCert") else self.state_dir / "tls/server.crt"
        self.tls_key = Path(cfg["tlsKey"]) if cfg.get("tlsKey") else self.state_dir / "tls/server.key"
        self.ca_cert = Path(cfg["caCert"]) if cfg.get("caCert") else self.state_dir / "tls/ca.crt"

        # Host allowlist. The LAN HTTPS listener accepts ONLY lan_host:lan_port;
        # the loopback HTTP listener accepts 127.0.0.1/localhost:loop_port plus
        # optional extra loopback names. No wildcard 0.0.0.0 exposure.
        self.extra_hosts = {str(h).strip().lower()
                            for h in (cfg.get("extraHosts") or []) if str(h).strip()}
        self.host_allow = {self.lan_host, "127.0.0.1", "localhost"} | self.extra_hosts

        nets = cfg.get("lanNets") or ["127.0.0.0/8", "::1/128"]
        self.lan_nets = [ipaddress.ip_network(n) for n in nets]

        # Bounded TLS/HTTP request handling for the Pi (1GB RAM): cap concurrent
        # handler threads and give every connection a socket timeout so a slow
        # or stalled LAN client cannot pin a thread forever.
        self.request_semaphore = threading.BoundedSemaphore(
            max(2, int(cfg.get("maxConcurrent", 12))))
        self.socket_timeout = max(2, int(cfg.get("socketTimeout", 20)))

    def build_servers(self):
        tls = self.tls_cert.exists() and self.tls_key.exists()
        servers = []
        # Loopback HTTP — always present; this is the本机 setup / SSH-forward path.
        loop = ConsoleServer(("127.0.0.1", self.loop_port), Handler, self)
        servers.append(("loopback-http", loop, None))
        if self.lan_http:
            lan = ConsoleServer((self.lan_host, self.lan_port), Handler, self)
            lan.lan_listener = True
            servers.append(("lan-http", lan, None))
        elif tls:
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            ctx.minimum_version = ssl.TLSVersion.TLSv1_2
            ctx.load_cert_chain(str(self.tls_cert), str(self.tls_key))
            # Bind to the exact LAN address, NOT 0.0.0.0. wlan0 owns this IP.
            lan = ConsoleServer((self.lan_host, self.lan_port), Handler, self)
            lan.lan_listener = True
            lan.socket = ctx.wrap_socket(
                lan.socket, server_side=True, do_handshake_on_connect=False
            )
            servers.append(("lan-https", lan, ctx))
        return servers


def expand(value):
    """Expand a leading ~ in config paths (stdlib, no shell)."""
    if isinstance(value, str) and value.startswith("~"):
        return str(Path(value).expanduser())
    return value


def _fixture_fetcher(fx):
    """Offline page fetcher backed by `fixture["content"]`.

    Shapes accepted in the fixture:
        pages:   {"<url>": "<html>"}                       exact match
                 {"<url>": {"status":302,"location":"..."}} redirect/denial
                 {"*<suffix>": "<html>"}                    suffix match
        denied:  ["<url>", ...]  -> 403 (used for auth_required / restricted)
        limited: ["<url>", ...]  -> 429
        missing: ["<url>", ...]  -> 404
    Unknown URLs answer 404 rather than reaching the network, so a fixture run
    can never touch a real platform.
    """
    pages = fx.get("pages") if isinstance(fx.get("pages"), dict) else {}
    denied = set(fx.get("denied") or [])
    limited = set(fx.get("limited") or [])
    missing = set(fx.get("missing") or [])

    def lookup(url):
        if url in pages:
            return pages[url]
        for key, value in pages.items():
            if key.startswith("*") and url.endswith(key[1:]):
                return value
        return None

    def fetch(url):
        if url in denied:
            return {"status": 403, "final_url": url, "location": "",
                    "body": "<html><title>需要登录</title></html>", "content_type": "text/html"}
        if url in limited:
            return {"status": 429, "final_url": url, "location": "",
                    "body": "", "content_type": ""}
        if url in missing:
            return {"status": 404, "final_url": url, "location": "",
                    "body": "", "content_type": ""}
        hit = lookup(url)
        if isinstance(hit, dict):
            return {"status": int(hit.get("status", 200)), "final_url":
                    hit.get("final_url") or url,
                    "location": hit.get("location", ""),
                    "body": hit.get("body", ""),
                    "content_type": hit.get("content_type", "text/html")}
        if isinstance(hit, str):
            return {"status": 200, "final_url": url, "location": "",
                    "body": hit, "content_type": "text/html"}
        return {"status": 404, "final_url": url, "location": "",
                "body": "", "content_type": ""}

    return fetch


def load_config(path: Path | None):
    if path and path.exists():
        cfg = json.loads(path.read_text(encoding="utf-8"))
        for k in ("qqRoot", "qqProactiveFile", "wechatBridgeUnit",
                  "tlsCert", "tlsKey", "caCert", "stateDir", "uiDir"):
            if k in cfg:
                cfg[k] = expand(cfg[k])
        return cfg
    return {}


def load_fixture(path: Path | None):
    if path and path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return None


def main(argv=None):
    ap = argparse.ArgumentParser(description="大肥鱼统一控制台服务端")
    ap.add_argument("--config", type=Path, help="非机密配置文件路径")
    ap.add_argument("--fixture", type=Path, help="测试用 fixture JSON（写路径只用 fixture）")
    ap.add_argument("--ui-dir", type=Path, help="静态目录（默认同级 ../ui）")
    ap.add_argument("--state-dir", type=Path, help="状态目录（密码/令牌/证书）")
    ap.add_argument("--mint", action="store_true",
                    help="在 Pi 本机生成新的短时单次配对令牌并打印带 #pair= 的链接")
    ap.add_argument("--reset", action="store_true", help="--mint 时生成重置密码令牌")
    args = ap.parse_args(argv)

    cfg = load_config(args.config)
    if args.ui_dir:
        cfg["uiDir"] = str(args.ui_dir)
    if args.state_dir:
        cfg["stateDir"] = str(args.state_dir)
    if not cfg.get("stateDir"):
        cfg["stateDir"] = str(default_state_dir())
    cfg.setdefault("uiDir", str(DEFAULT_UI))

    app = App(cfg, load_fixture(args.fixture))

    if args.mint:
        # Runs on the Pi itself (the Windows launcher invokes this over SSH).
        token = app.pair_store.mint(reset=args.reset)
        fragment = "#pair=" + token + ("&reset=1" if args.reset else "")
        print("http://127.0.0.1:%d/%s" % (app.loop_port, fragment))
        return 0

    servers = app.build_servers()
    if len(servers) == 1:
        print("提示：未找到 TLS 证书，局域网入口未启动；本机入口仍可用", file=sys.stderr)

    # One thread runs both maintenance tasks at a steady one-minute cadence.
    def sweeper():
        while True:
            started = time.monotonic()
            app.sessions.sweep()
            app.host_status.record()
            time.sleep(max(0, host_status.SAMPLE_SECONDS - (time.monotonic() - started)))
    threading.Thread(target=sweeper, daemon=True).start()

    threads = []
    for name, srv, _ in servers:
        t = threading.Thread(target=srv.serve_forever, name=name, daemon=True)
        t.start()
        threads.append((name, srv, t))
        print("%s ready on %s:%d" % (name, *srv.server_address), flush=True)
    try:
        for _, _, t in threads:
            t.join()
    except KeyboardInterrupt:
        for _, srv, _ in threads:
            srv.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
