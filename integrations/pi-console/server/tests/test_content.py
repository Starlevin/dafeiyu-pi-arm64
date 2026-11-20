#!/usr/bin/env python3
"""大肥鱼内容页 — 合成测试（只读、离线、零网络）。

覆盖用户明确要求的四类：
  1. 四种地址（X / 抖音 / B站 / 小红书）+ 四个官方短链 + 伪装域越界；
  2. 读失败（403 需要登录 / 429 / 404 / 网络异常 / 壳页面）不假装读到；
  3. 限流（阅读间隔、每分钟上限、不自动重试、Retry-After 头）；
  4. 任何错误路径都**不触发任何写操作**（不发评论、不发消息、不碰 adapters 写接口）。

所有出网都被替换成假 fetcher：一旦真的有人发起网络请求，测试会立刻失败。

Run:  python -m unittest test_content -v
  or: python test_content.py
"""

from __future__ import annotations

import http.client
import json
import os
import re
import sys
import tempfile
import threading
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import content  # noqa: E402
import unified_console as uc  # noqa: E402

FIXTURE = json.loads((HERE / "fixtures/fixture.json").read_text(encoding="utf-8"))

# Four real-shaped URLs, one per platform, plus the four official short links.
URLS = {
    "x": "https://x.com/someone/status/1234567890123456789",
    "douyin": "https://www.douyin.com/video/7412345678901234567",
    "bilibili": "https://www.bilibili.com/video/BV1xx411c7mD",
    "xiaohongshu": "https://www.xiaohongshu.com/explore/6512ab34cd0000001f00abcd",
}
SHORTS = {
    "x": "https://t.co/AbCdEf1234",
    "douyin": "https://v.douyin.com/iAbCdEfG/",
    "bilibili": "https://b23.tv/AbCdEfG",
    "xiaohongshu": "https://xhslink.com/a/AbCdEfG",
}
PAGE_HTML = (
    "<html><head><title>标题：%s</title>"
    '<meta property="og:title" content="正文标题"></head>'
    "<body><article><p>这是一段公开可见的正文，用于验证只读读取链路。</p>"
    "<p>第二段内容。</p></article></body></html>"
)


class Page(object):
    """Records every page fetch; can be scripted to fail."""

    def __init__(self, pages=None, deny=(), limited=(), missing=(), boom=()):
        self.pages = dict(pages or {})
        self.deny = set(deny)
        self.limited = set(limited)
        self.missing = set(missing)
        self.boom = set(boom)
        self.calls = []

    def __call__(self, url):
        self.calls.append(url)
        if url in self.boom:
            raise content.ContentError("网络不可用，请稍后重试", 502)
        if url in self.deny:
            return {"status": 403, "final_url": url, "location": "",
                    "body": "<html><title>登录后查看</title></html>",
                    "content_type": "text/html"}
        if url in self.limited:
            return {"status": 429, "final_url": url, "location": "",
                    "body": "", "content_type": ""}
        if url in self.missing:
            return {"status": 404, "final_url": url, "location": "",
                    "body": "", "content_type": ""}
        if url in self.pages:
            return {"status": 200, "final_url": url, "location": "",
                    "body": self.pages[url], "content_type": "text/html"}
        return {"status": 404, "final_url": url, "location": "",
                "body": "", "content_type": ""}


def reader_cfg(tmp, fetcher, **extra):
    cfg = {
        "contentStateDir": str(Path(tmp) / "content"),
        "platforms": {pid: True for pid in content.PLATFORM_IDS},
        "readIntervalSeconds": 0,
        "maxReadsPerMinute": 60,
        "fetcher": fetcher,
    }
    cfg.update(extra)
    return cfg


# --------------------------------------------------------------------------- #
# 1. 四种地址 + 短链 + 伪装域
# --------------------------------------------------------------------------- #

class AddressTests(unittest.TestCase):
    """四类地址分类正确；越界一律拒绝且从不发请求。"""

    def test_four_platforms_classified(self):
        for pid, url in URLS.items():
            self.assertEqual(content.classify(url), pid, url)
            t = content.parse_target(url)
            self.assertEqual(t.kind, "url")
            self.assertEqual(t.platform, pid)

    def test_four_content_paths_recognised(self):
        for pid, url in URLS.items():
            got_pid, kind, slug = content.classify_path(url)
            self.assertEqual(got_pid, pid, url)
            self.assertEqual(kind, "content", url)
            self.assertTrue(slug, url)

    def test_four_short_hosts_allowed(self):
        for pid, url in SHORTS.items():
            self.assertEqual(content.classify(url), pid, url)

    def test_plain_text_is_paste_not_url(self):
        t = content.parse_target("这是一段直接粘贴过来的正文，不是链接。")
        self.assertEqual(t.kind, "text")

    def test_impostor_hosts_rejected(self):
        bad = [
            "https://x.com.evil.example/someone/status/1",
            "https://www.bilibili.com.evil.example/video/BV1xx411c7mD",
            "https://evil.example/?u=https://x.com/a/status/1",
            "https://www.douyin.com@evil.example/video/1",
            "javascript:alert(1)",
        ]
        for url in bad:
            self.assertIsNone(content.classify(url), url)

    def test_out_of_scope_hop_is_never_fetched(self):
        """A redirect leaving the four platforms must be rejected BEFORE the fetch."""
        seen = []

        def evil_fetcher(url):
            seen.append(url)
            return {"status": 302, "final_url": url,
                    "location": "https://evil.example/steal",
                    "body": "", "content_type": ""}

        res = content.resolve_hops(URLS["x"], evil_fetcher)
        self.assertFalse(res["ok"])
        self.assertEqual(seen, [URLS["x"]],
                         "the out-of-scope target must never be requested")
        self.assertFalse(any(h["url"].startswith("https://evil.example") and h.get("allowed")
                             for h in res["trace"]))
        # And a reader refuses to read through it at all.
        tmp = tempfile.mkdtemp(prefix="dfy-c-")
        reader_page = Page()
        out = content.Reader(reader_cfg(tmp, reader_page)).read(URLS["x"])
        self.assertTrue(out["degraded"])
        self.assertFalse(any("evil.example" in u for u in reader_page.calls))

    def test_hop_limit_enforced(self):
        n = {"i": 0}

        def loop_fetch(url):
            n["i"] += 1
            return {"status": 302, "final_url": url, "location": URLS["bilibili"],
                    "body": "", "content_type": ""}

        res = content.resolve_hops(URLS["bilibili"], loop_fetch)
        self.assertLessEqual(n["i"], content.MAX_HOPS + 1)
        self.assertFalse(res.get("ok") and n["i"] > content.MAX_HOPS)


# --------------------------------------------------------------------------- #
# 2. 读失败 —— 绝不假装读到
# --------------------------------------------------------------------------- #

class ReadFailureTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="dfy-read-")

    def _read(self, page, url, **extra):
        r = content.Reader(reader_cfg(self.tmp, page, **extra))
        return r.read(url)

    def test_bilibili_metadata_does_not_claim_video_coverage(self):
        body = """<html><head><meta property="og:title" content="视频标题">
        <meta property="og:description" content="视频简介"></head>
        <body>网页导航文字</body></html>"""
        out = self._read(Page(pages={URLS["bilibili"]: body}), URLS["bilibili"])
        self.assertEqual(out["coverage"], "partial")
        self.assertEqual(out["item"]["text"], "视频简介")
        self.assertIn("字幕", out["reason"])

    def test_auth_required_is_not_faked(self):
        page = Page(deny=[URLS["xiaohongshu"]])
        out = self._read(page, URLS["xiaohongshu"])
        self.assertIn(out["coverage"],
                      ("auth_required", "restricted", "excerpt", "unavailable"))
        # No body may be invented for a denied read, and the link is preserved.
        self.assertNotIn("登录后查看", out["item"].get("text", ""))
        self.assertEqual(out["item"].get("url"), URLS["xiaohongshu"])
        self.assertTrue(out["degraded"])

    def test_429_maps_to_limited_or_restricted(self):
        page = Page(limited=[URLS["douyin"]])
        out = self._read(page, URLS["douyin"])
        self.assertIn(out["coverage"], ("limited", "restricted", "unavailable"))

    def test_404_maps_to_expired_or_unavailable(self):
        page = Page(missing=[URLS["bilibili"]])
        out = self._read(page, URLS["bilibili"])
        self.assertIn(out["coverage"],
                      ("expired", "unavailable", "restricted", "excerpt"))
        self.assertIn("coverage", out)

    def test_network_error_degrades_cleanly(self):
        page = Page(boom=[URLS["x"]])
        with self.assertRaises(content.ContentError) as ctx:
            self._read(page, URLS["x"])
        self.assertEqual(ctx.exception.status, 502)
        self.assertIn("网络", ctx.exception.message)

    def test_shell_page_downgrades_to_excerpt(self):
        """SPA shell with no readable text must not claim `full`."""
        page = Page(pages={URLS["x"]: "<html><body><div id=app></div></body></html>"})
        out = self._read(page, URLS["x"])
        self.assertNotEqual(out["coverage"], "full")

    def test_unconfigured_platform_degrades_to_save_link(self):
        page = Page(pages={URLS["x"]: PAGE_HTML % "x"})
        cfg = reader_cfg(self.tmp, page, platforms={})
        out = content.Reader(cfg).read(URLS["x"])
        self.assertTrue(out["degraded"])
        self.assertEqual(page.calls, [], "unconfigured platform must not fetch")

    def test_paste_text_works_without_any_platform(self):
        cfg = reader_cfg(self.tmp, Page(), platforms={})
        out = content.Reader(cfg).read("我随手粘贴的一段正文，想让它帮我总结。")
        self.assertEqual(out["coverage"], "full")
        self.assertEqual(out["item"]["mode"], "paste")

    def test_denial_phrases_are_chinese_and_actionable(self):
        page = Page(deny=[URLS["x"]])
        out = self._read(page, URLS["x"])
        self.assertTrue(out["reason"])
        self.assertRegex(out["reason"], r"[\u4e00-\u9fff]")


# --------------------------------------------------------------------------- #
# 3. 限流
# --------------------------------------------------------------------------- #

class RateLimitTests(unittest.TestCase):
    def test_read_interval_blocks_second_read(self):
        tmp = tempfile.mkdtemp(prefix="dfy-rate-")
        page = Page(pages={URLS["bilibili"]: PAGE_HTML % "b"})
        cfg = reader_cfg(tmp, page, readIntervalSeconds=60)
        first = content.Reader(cfg).read(URLS["bilibili"])
        self.assertIn(first["coverage"], content.READABLE)
        calls_after_first = len(page.calls)
        second = content.Reader(cfg).read(URLS["bilibili"])
        self.assertEqual(second["coverage"], "limited")
        self.assertGreater(second.get("retryAfter", 0), 0)
        self.assertEqual(len(page.calls), calls_after_first,
                         "a limited read must NOT retry the network")

    def test_per_minute_cap(self):
        """Same host, interval 0: the per-minute window must still cap reads."""
        tmp = tempfile.mkdtemp(prefix="dfy-cap-")
        url2 = "https://www.bilibili.com/video/BV1yy411c7mE"
        page = Page(pages={URLS["bilibili"]: PAGE_HTML % "b",
                           url2: PAGE_HTML % "b2"})
        cfg = reader_cfg(tmp, page, readIntervalSeconds=0, maxReadsPerMinute=1)
        content.Reader(cfg).read(URLS["bilibili"])
        out = content.Reader(cfg).read(url2)
        self.assertEqual(out["coverage"], "limited")
        self.assertGreater(out.get("retryAfter", 0), 0)

    def test_limited_read_does_not_hit_network_again(self):
        tmp = tempfile.mkdtemp(prefix="dfy-cap2-")
        page = Page(pages={URLS["bilibili"]: PAGE_HTML % "b"})
        cfg = reader_cfg(tmp, page, readIntervalSeconds=60)
        content.Reader(cfg).read(URLS["bilibili"])
        calls = len(page.calls)
        content.Reader(cfg).read(URLS["bilibili"])
        self.assertEqual(len(page.calls), calls)

    def test_limiter_unit_caps_per_minute(self):
        lim = content.RateLimiter(interval=0, per_minute=2)
        self.assertIsNone(lim.admit("www.bilibili.com"))
        self.assertIsNone(lim.admit("www.bilibili.com"))
        blocked = lim.admit("www.bilibili.com")
        self.assertIsNotNone(blocked)
        self.assertEqual(blocked["status"], "limited")
        self.assertGreater(blocked["retryAfter"], 0)
        # A different host has its own budget.
        self.assertIsNone(lim.admit("www.x.com"))

    def test_limiter_is_shared_across_reader_instances(self):
        tmp = tempfile.mkdtemp(prefix="dfy-shared-")
        page = Page(pages={URLS["douyin"]: PAGE_HTML % "d"})
        cfg = reader_cfg(tmp, page, readIntervalSeconds=60)
        content.Reader(cfg).read(URLS["douyin"])
        calls = len(page.calls)
        # A brand-new Reader (as each HTTP request does) still honours cooling.
        again = content.Reader(cfg).read(URLS["douyin"])
        self.assertEqual(again["coverage"], "limited")
        self.assertEqual(len(page.calls), calls)


# --------------------------------------------------------------------------- #
# 4. 错误绝不触发写操作
# --------------------------------------------------------------------------- #

class NoWriteOnErrorTests(unittest.TestCase):
    """Every failure path must leave the QQ/WeChat write surface untouched."""

    def test_content_module_has_no_write_surface(self):
        src = (HERE / "content.py").read_text(encoding="utf-8")
        code = "\n".join(
            line for line in src.splitlines()
            if not line.strip().startswith("#"))
        # Match whole words inside a def name: `_shared_limiter` must not count
        # as a `share` function.
        names = re.findall(r"(?m)^\s*def (\w+)\s*\(", code)
        for fn in names:
            words = set(re.findall(r"[a-z]+", fn.lower()))
            for bad in ("comment", "publish", "reply", "like", "follow",
                        "send", "submit", "repost", "share", "post"):
                self.assertNotIn(bad, words, "content.py must not define %s (%s)" % (bad, fn))
        # The only allowed network identity is our own model-gateway key.
        self.assertNotRegex(code, r"(?m)^\s*import adapters")
        self.assertNotRegex(code, r"(?m)^\s*from adapters")

    def test_no_browser_credentials_read(self):
        src = (HERE / "content.py").read_text(encoding="utf-8")
        # Any Authorization header must carry OUR model-gateway key — never a
        # browser credential. Exactly one request header is built, in ModelGateway.
        auth_lines = [l for l in src.splitlines()
                      if re.search(r'["\']Authorization["\']\s*:', l)]
        self.assertEqual(len(auth_lines), 1, auth_lines)
        self.assertRegex(auth_lines[0], r"_current_key\(\)|_key")
        self.assertNotIn("CookieJar", src)
        self.assertNotRegex(src, r"(?i)\bcookies?\s*=\s*\{")


# --------------------------------------------------------------------------- #
# HTTP 合同（与 ui/content.js 一致）
# --------------------------------------------------------------------------- #

class HttpClient(object):
    def __init__(self, port):
        self.port = port
        self.cookie = ""

    def req(self, method, path, body=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=15)
        try:
            h = {"Host": "127.0.0.1:%d" % self.port, "Accept": "application/json"}
            if self.cookie:
                h["Cookie"] = self.cookie
            if body is not None:
                h["Content-Type"] = "application/json"
            raw = json.dumps(body, ensure_ascii=False).encode() if body is not None else None
            conn.request(method, path, body=raw, headers=h)
            resp = conn.getresponse()
            data = resp.read()
            status = resp.status
            setc = resp.getheader("Set-Cookie")
            if setc:
                self.cookie = setc.split(";", 1)[0]
            hdrs = {k: v for k, v in resp.getheaders()}
        finally:
            conn.close()
        try:
            parsed = json.loads(data)
        except ValueError:
            parsed = data
        return status, parsed, hdrs


class Server(object):
    """A console server on an ephemeral port, cleaned up on exit."""

    def __init__(self, fixture=None, fetcher=None, content_overrides=None,
                 grant_all=False):
        self.tmp = tempfile.TemporaryDirectory(prefix="dfy-content-http-")
        cfg = {
            "stateDir": str(Path(self.tmp.name) / "state"),
            "uiDir": str(HERE.parent / "ui"),
            "lanHost": "127.0.0.1", "lanPort": 0, "loopPort": 0,
            "lanNets": ["127.0.0.0/8", "::1/128"],
            "extraHosts": ["127.0.0.1", "localhost"],
        }
        if grant_all:
            # Explicit operator authorization ("this must not pretend to read"
            # is the default; a test opts in to real reading).
            cfg["contentPlatforms"] = {pid: True for pid in content.PLATFORM_IDS}
            cfg["readIntervalSeconds"] = 0
            cfg["maxReadsPerMinute"] = 60
        self.app = uc.App(cfg, fixture if fixture is not None else FIXTURE)
        if fetcher is not None:
            self.app.content_cfg["fetcher"] = fetcher
        if content_overrides:
            self.app.content_cfg.update(content_overrides)
        self.srv = uc.ConsoleServer(("127.0.0.1", 0), uc.Handler, self.app)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.client = HttpClient(self.srv.server_address[1])

    def login(self, password="content-test-123"):
        token = self.app.pair_store.mint()
        s, d, _ = self.client.req("POST", "/api/setup",
                                  {"pair": token, "password": password})
        assert s == 200, (s, d)

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()
        self.tmp.cleanup()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


class RateLimitHttpTests(unittest.TestCase):
    def test_retry_after_header_on_http_surface(self):
        page = Page(pages={URLS["bilibili"]: PAGE_HTML % "b"})
        with Server(fetcher=page, grant_all=True,
                    content_overrides={"readIntervalSeconds": 60}) as s:
            s.login()
            st, d, _ = s.client.req("POST", "/api/content/read",
                                    {"url": URLS["bilibili"]})
            self.assertEqual(st, 200)
            st, d, hdr = s.client.req("POST", "/api/content/read",
                                      {"url": URLS["bilibili"]})
            self.assertEqual(st, 200)
            self.assertEqual(d["coverage"], "limited")
            self.assertIn("Retry-After", hdr)
            self.assertGreater(int(hdr["Retry-After"]), 0)


class NoWriteHttpTests(unittest.TestCase):
    def test_http_error_paths_never_call_adapters_writes(self):
        calls = []
        page = Page(deny=[URLS["x"]], limited=[URLS["douyin"]],
                    missing=[URLS["bilibili"]], boom=[URLS["xiaohongshu"]])
        with Server(fetcher=page, grant_all=True) as s:
            for name in ("set_conversation", "set_model"):
                orig = getattr(s.app.console, name)

                def trap(*a, _o=orig, _n=name, **k):
                    calls.append(_n)
                    return _o(*a, **k)

                setattr(s.app.console, name, trap)
            s.login()
            for url in URLS.values():
                s.client.req("POST", "/api/content/read", {"url": url})
            self.assertEqual(calls, [],
                             "content errors must not reach adapter writes")

    def test_ask_without_model_is_refused_not_queued(self):
        with Server(fetcher=Page()) as s:
            s.login()
            st, d, _ = s.client.req("POST", "/api/content/read",
                                    {"text": "一段拿来总结的正文。"})
            self.assertEqual(st, 200)
            item_id = d["item"]["id"]
            st, d, _ = s.client.req("POST", "/api/content/ask",
                                    {"id": item_id, "question": "讲了什么？"})
            self.assertIn(st, (409, 400, 503))
            self.assertIn("error", d)
            self.assertIn("模型", d["error"])


class ApiContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.page = Page(pages={URLS["x"]: PAGE_HTML % "x",
                               URLS["bilibili"]: PAGE_HTML % "b"})
        cls.s = Server(fetcher=cls.page, grant_all=True)
        cls.s.login()
        cls.client = cls.s.client

    @classmethod
    def tearDownClass(cls):
        cls.s.close()

    def test_capabilities_shape(self):
        st, d, _ = self.client.req("GET", "/api/content/capabilities")
        self.assertEqual(st, 200)
        ids = [p["id"] for p in d["platforms"]]
        self.assertEqual(sorted(ids),
                         sorted(["x", "douyin", "bilibili", "xiaohongshu"]))
        for p in d["platforms"]:
            for key in ("name", "read", "search", "subscribe", "reason", "mode"):
                self.assertIn(key, p)
            self.assertIsInstance(p["read"], bool)
            self.assertIn(p["mode"], ("read", "paste", "save-link"))
        self.assertIn("model", d)
        self.assertIn("configured", d["model"])
        self.assertEqual(d["redirectMaxHops"], content.MAX_HOPS)

    def test_capabilities_never_leaks_key(self):
        st, d, _ = self.client.req("GET", "/api/content/capabilities")
        raw = json.dumps(d, ensure_ascii=False)
        self.assertNotIn("modelKey", raw)
        self.assertNotIn("sk-", raw)

    def test_feed_shape_and_platform_filter(self):
        self.client.req("POST", "/api/content/read", {"url": URLS["x"]})
        st, d, _ = self.client.req("GET", "/api/content/feed?platform=all")
        self.assertEqual(st, 200)
        self.assertIn("items", d)
        self.assertIn("diagnostics", d)
        for it in d["items"]:
            for key in ("id", "platform", "title", "author", "publishedAt",
                        "url", "coverage", "mode", "excerpt", "hasOriginal"):
                self.assertIn(key, it)
        st, d2, _ = self.client.req("GET", "/api/content/feed?platform=douyin")
        self.assertTrue(all(i["platform"] == "douyin" for i in d2["items"]))

    def test_read_shape_with_trace(self):
        st, d, _ = self.client.req("POST", "/api/content/read",
                                   {"url": URLS["bilibili"]})
        self.assertEqual(st, 200)
        for key in ("item", "coverage", "reason", "trace", "degraded"):
            self.assertIn(key, d)
        self.assertIn(d["coverage"], content.COVERAGE)
        self.assertIsInstance(d["trace"], list)
        self.assertIn("original", d["item"])

    def test_search_and_subscribe(self):
        st, d, _ = self.client.req("POST", "/api/content/search",
                                   {"platform": "bilibili", "keyword": "独立游戏"})
        self.assertEqual(st, 200)
        self.assertIn("items", d)
        self.assertIn("limited", d)
        st, d, _ = self.client.req("POST", "/api/content/subscribe",
                                   {"platform": "bilibili", "keyword": "独立游戏",
                                    "enabled": True})
        self.assertEqual(st, 200)
        self.assertTrue(d.get("ok"))

    def test_requires_auth(self):
        anon = HttpClient(self.s.srv.server_address[1])
        for path in ("/api/content/capabilities", "/api/content/feed"):
            st, d, _ = anon.req("GET", path)
            self.assertEqual(st, 401, path)
        st, d, _ = anon.req("POST", "/api/content/read", {"text": "x"})
        self.assertEqual(st, 401)

    def test_bad_body_rejected(self):
        st, d, _ = self.client.req("POST", "/api/content/read", {})
        self.assertEqual(st, 400)
        st, d, _ = self.client.req("POST", "/api/content/read", {"url": 12345})
        self.assertEqual(st, 400)
        st, d, _ = self.client.req("POST", "/api/content/ask",
                                   {"id": "nope", "question": "?"})
        self.assertIn(st, (400, 404, 409))

    def test_static_assets_served(self):
        for path in ("/content.js", "/content.css"):
            st, d, _ = self.client.req("GET", path)
            self.assertEqual(st, 200, path)


class UnconfiguredDefaultTests(unittest.TestCase):
    """With nothing configured the console must NOT pretend it can read."""

    def test_all_platforms_off_by_default(self):
        with Server() as s:          # no contentPlatforms, no fetcher
            s.login()
            st, d, _ = s.client.req("GET", "/api/content/capabilities")
            self.assertEqual(st, 200)
            for p in d["platforms"]:
                self.assertFalse(p["read"], p["id"])
                self.assertFalse(p["search"], p["id"])
                self.assertIn(p["mode"], ("paste", "save-link"))
                self.assertTrue(p["reason"], p["id"])
                self.assertTrue(p["subscribe"], p["id"])

    def test_url_read_degrades_to_saved_link(self):
        with Server() as s:
            s.login()
            for url in URLS.values():
                st, d, _ = s.client.req("POST", "/api/content/read", {"url": url})
                self.assertEqual(st, 200, url)
                self.assertTrue(d["degraded"], url)
                self.assertIn(d["coverage"], ("excerpt", "limited", "unavailable"), url)
                self.assertTrue(d["item"]["url"], url)

    def test_paste_still_works_when_nothing_configured(self):
        with Server() as s:
            s.login()
            st, d, _ = s.client.req("POST", "/api/content/read",
                                    {"text": "平台都没配置时，贴正文仍然应该能读能存。"})
            self.assertEqual(st, 200)
            self.assertEqual(d["coverage"], "full")
            self.assertEqual(d["item"]["mode"], "paste")

    def test_search_refuses_with_reason_when_unconfigured(self):
        with Server() as s:
            s.login()
            st, d, _ = s.client.req("POST", "/api/content/search",
                                    {"platform": "x", "keyword": "测试"})
            self.assertEqual(st, 200)
            self.assertEqual(d["items"], [])
            self.assertFalse(d["limited"])
            self.assertTrue(d["reason"])


# --------------------------------------------------------------------------- #
# 前端静态审计（只读界面、无写入口、无 innerHTML）
# --------------------------------------------------------------------------- #

def _js_code(src):
    """Strip block/line comments so audits only see executable JS."""
    out = []
    in_block = False
    for line in src.splitlines():
        s = line
        if in_block:
            end = s.find("*/")
            if end < 0:
                continue
            s = s[end + 2:]
            in_block = False
        while "/*" in s:
            start = s.find("/*")
            end = s.find("*/", start + 2)
            if end < 0:
                s = s[:start]
                in_block = True
                break
            s = s[:start] + " " + s[end + 2:]
        if s.strip().startswith("//") or s.strip().startswith("*"):
            continue
        out.append(s)
    return "\n".join(out)


# --------------------------------------------------------------------------- #
# 并发写入：共享路径锁 + 锁内重新读盘（不丢别人的收藏/订阅）
# --------------------------------------------------------------------------- #

class ConcurrencyTests(unittest.TestCase):
    """Every request builds its own Reader/Store; the lock must still be shared."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="dfy-conc-")
        self.path = Path(self.tmp) / "content.json"

    def _store(self):
        return content.Store(self.path)

    def test_lock_is_shared_across_instances(self):
        a, b = self._store(), self._store()
        self.assertIs(a.lock, b.lock)
        self.assertIsNot(content.Store(Path(self.tmp) / "other.json").lock, a.lock)

    def test_concurrent_subscribes_lose_nothing(self):
        store = self._store()
        store.save(content._default_state())  # materialise the file
        n = 24

        def worker(i):
            # contentStateDir must be a DIRECTORY (a .json path is used verbatim).
            content.Reader({
                "contentStateDir": self.tmp,
                "platforms": {},
                "fetcher": None,
            }).subscribe(content.PLATFORM_IDS[i % 4], "关键词%d" % i, True)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        subs = content.Store(self.path).load()["subscriptions"]
        self.assertEqual(len(subs), n, "concurrent subscribes must all survive")

    def test_stale_snapshot_cannot_roll_back_other_writes(self):
        """Simulates a slow request: it read state early, then writes late."""
        store = self._store()
        store.save(content._default_state())
        # A "slow" request grabs a snapshot BEFORE other work lands.
        stale = store.load()
        self.assertEqual(len(stale["subscriptions"]), 0)
        # Meanwhile a fast request stores a bookmark + a subscription.
        content.Reader({"contentStateDir": self.tmp, "platforms": {},
                        "fetcher": None}).subscribe("bilibili", "独立游戏", True)
        store.upsert_item({"id": "slow", "platform": "bilibili", "title": "慢请求",
                           "url": URLS["bilibili"], "coverage": "excerpt", "mode": "save-link"})
        # The late write must merge onto the CURRENT file, not the stale snapshot.
        final = store.load()
        self.assertEqual(len(final["subscriptions"]), 1,
                         "slow request must not erase a saved subscription")
        self.assertTrue(any(i["id"] == "slow" for i in final["items"]))

    def test_upsert_does_not_drop_links(self):
        store = self._store()
        store.save(content._default_state())
        store.update(lambda st: st["links"].append({"id": "keepme", "url": "u"}))
        store.upsert_item({"id": "x1", "platform": "x", "url": URLS["x"]})
        self.assertTrue(any(l["id"] == "keepme" for l in store.load()["links"]))

    def test_update_holds_lock_only_briefly(self):
        """The fetcher/model must run OUTSIDE the lock (no long write-lock hold)."""
        page = Page(pages={URLS["bilibili"]: PAGE_HTML % "b"})
        seen = {"held": None}

        def slow_fetch(url):
            # If the store lock were held during the network call, another
            # thread could not acquire it here while we are inside the fetch.
            got = content.Store(self.path).lock.acquire(timeout=0.5)
            seen["held"] = not got
            if got:
                content.Store(self.path).lock.release()
            return page(url)

        content.Reader(reader_cfg(self.tmp, slow_fetch)).read(URLS["bilibili"])
        self.assertFalse(seen["held"], "network call must not hold the store write lock")


# --------------------------------------------------------------------------- #
# 能力拆分：读公开链接 ≠ 平台内搜索
# --------------------------------------------------------------------------- #

class CapabilitySplitTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="dfy-cap-split-")

    def _caps(self, **cfg_extra):
        cfg = {"contentStateDir": str(Path(self.tmp) / "content"),
               "fetcher": Page()}
        cfg.update(cfg_extra)
        return {p["id"]: p for p in content.Reader(cfg).capabilities()["platforms"]}

    def test_read_does_not_imply_search(self):
        caps = self._caps(platforms={"bilibili": True})
        self.assertTrue(caps["bilibili"]["read"])
        self.assertFalse(caps["bilibili"]["search"],
                         "granting link reading must not grant web search")

    def test_search_requires_its_own_grant(self):
        caps = self._caps(platforms={"bilibili": True},
                          contentSearchPlatforms={"bilibili": True})
        self.assertTrue(caps["bilibili"]["search"])

    def test_x_web_search_never_enabled(self):
        caps = self._caps(platforms={"x": True},
                          contentSearchPlatforms={"x": True})
        self.assertFalse(caps["x"]["search"], "X must not be scraped for search")

    def test_search_refusal_reason_is_explicit(self):
        r = content.Reader({"contentStateDir": str(Path(self.tmp) / "c2"),
                            "platforms": {"x": True}, "fetcher": Page()})
        out = r.search("x", "独立游戏")
        self.assertEqual(out["items"], [])
        self.assertFalse(out["limited"])
        self.assertTrue(out["reason"])
        self.assertNotIn("fetch", out.get("reason", "").lower())


# --------------------------------------------------------------------------- #
# 模型网关 key：只读配置里的路径，不返回、不打印、不接受任意路径
# --------------------------------------------------------------------------- #

class GatewayKeyTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="dfy-key-")
        self.rel = os.path.join("secrets", "model-hub", "gateway.key")
        p = Path(self.root) / self.rel
        p.parent.mkdir(parents=True)
        p.write_text("sk-test-0123456789abcdef\n", encoding="utf-8")
        self.outside = Path(self.root) / "outside.key"
        self.outside.write_text("sk-outside-0123456789\n", encoding="utf-8")

    def _gw(self, **kw):
        base = {"base_url": "http://127.0.0.1:18090/v1",
                "model": "test-model", "key_root": self.root}
        base.update(kw)
        return content.ModelGateway(**base)

    def test_key_loaded_from_config_path(self):
        gw = self._gw(key_path=self.rel)
        self.assertTrue(gw.configured())

    def test_status_never_exposes_key_or_path(self):
        gw = self._gw(key_path=self.rel)
        raw = json.dumps(gw.status(), ensure_ascii=False)
        self.assertNotIn("sk-test", raw)
        self.assertNotIn("gateway.key", raw)
        self.assertNotIn("secrets", raw)

    def test_missing_key_file_is_reported_but_not_fatal(self):
        gw = self._gw(key_path="secrets/model-hub/nope.key")
        self.assertFalse(gw.configured())
        self.assertIn("模型 Key", gw.status()["reason"])
        self.assertNotIn("nope.key", gw.status()["reason"])

    def test_relative_path_cannot_escape_root(self):
        gw = self._gw(key_path="../../outside.key")
        self.assertFalse(gw.configured(),
                         "a relative path must not escape qqRoot")

    def test_caller_cannot_supply_arbitrary_path_via_request(self):
        """The gateway path comes from CONFIG only, never from a request body."""
        page = Page()
        with Server(fetcher=page) as s:
            s.login()
            st, d, _ = s.client.req("POST", "/api/content/ask",
                                    {"id": "x", "question": "?",
                                     "keyPath": str(self.outside),
                                     "modelKey": "sk-injected"})
            self.assertIn(st, (400, 404, 409))
            self.assertNotIn("sk-injected", json.dumps(d))

    def test_default_gateway_points_at_local_hub(self):
        with Server() as s:
            cfg = s.app.content_cfg
            self.assertTrue(cfg["modelBaseUrl"].endswith("/v1"))
            self.assertIn("18090", cfg["modelBaseUrl"])
            self.assertEqual(cfg["modelName"], "test-model")
            self.assertEqual(cfg["modelKeyPath"], content.DEFAULT_GATEWAY_KEY_REL)

    def test_no_real_model_request_during_tests(self):
        """Every content test runs with model unconfigured or a fake opener."""
        with Server(fetcher=Page()) as s:
            s.login()
            st, d, _ = s.client.req("POST", "/api/content/read",
                                    {"text": "一段没有配置模型时也该能保存的正文。"})
            self.assertEqual(st, 200)
            self.assertFalse(d.get("summary"), "no model call when unconfigured")


class RealWorldReadTests(unittest.TestCase):
    """Shapes real platforms actually return (still fully offline)."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="dfy-real-")

    def test_relative_location_is_followed(self):
        """B站 answers `Location: /video/BV...` — must resolve, not be rejected."""
        url = URLS["bilibili"]
        resolved = content._absolutize("/video/BV1xx411c7mD/", url)
        self.assertEqual(resolved, "https://www.bilibili.com/video/BV1xx411c7mD/")

        def fetch(u):
            if u == url:
                return {"status": 301, "final_url": u,
                        "location": "/video/BV1xx411c7mD/", "body": "",
                        "content_type": ""}
            return {"status": 200, "final_url": u, "location": "",
                    "body": PAGE_HTML % "迁移后", "content_type": "text/html"}

        out = content.Reader(reader_cfg(self.tmp, fetch)).read(url)
        self.assertIn(out["coverage"], content.READABLE)
        self.assertTrue(out["item"]["url"].endswith("/"))
        # The out-of-scope guard still applies to a RELATIVE->absolute hop.
        bad = content._absolutize("/x", "https://evil.example/y")
        self.assertTrue(bad.startswith("https://evil.example"))

    def test_relative_location_cannot_escape_allowlist(self):
        def fetch(u):
            return {"status": 301, "final_url": u, "location": "//evil.example/x",
                    "body": "", "content_type": ""}

        res = content.resolve_hops(URLS["bilibili"], fetch)
        self.assertFalse(res["ok"])

    def test_gzip_body_is_decoded(self):
        import gzip
        html = (PAGE_HTML % "压缩").encode("utf-8")
        self.assertEqual(content._decompress(gzip.compress(html), "gzip"), html)
        self.assertEqual(content._decompress(html, "identity"), html)
        self.assertEqual(content._decompress(b"not-gzip", "gzip"), b"")
        self.assertEqual(content._decompress(b"\x00\x01", "deflate"), b"")

    def test_metadata_extracted_like_bilibili(self):
        """Title/author/date from the real field layout, nothing invented."""
        html = (
            '<html><head><title>T</title>'
            '<meta property="og:title" content="真实标题">'
            '<meta property="og:description" content="-, 视频播放量 100, 视频作者 某某UP, 作者简介 x">'
            '<meta property="article:published_time" content="2020-01-01T07:43:23.000Z">'
            "</head><body><p>" + ("正文" * 200) + "</p></body></html>"
        )
        out = content.Reader(reader_cfg(
            self.tmp, Page(pages={URLS["bilibili"]: html}))).read(URLS["bilibili"])
        it = out["item"]
        self.assertEqual(it["title"], "真实标题")
        self.assertEqual(it["author"], "某某UP")
        self.assertEqual(it["publishedAt"], "2020-01-01T07:43:23.000Z")

    def test_author_is_not_the_title(self):
        """A JSON-LD `name` at top level is the title, never the author."""
        html = ('<html><head><title>x</title>'
                '<script type="application/ld+json">'
                '{"name":"其实是标题","author":{"name":"真作者"}}</script>'
                "</head><body><p>" + ("正文" * 200) + "</p></body></html>")
        out = content.Reader(reader_cfg(
            self.tmp, Page(pages={URLS["bilibili"]: html}))).read(URLS["bilibili"])
        self.assertEqual(out["item"]["author"], "真作者")

    def test_page_is_fetched_once_per_read(self):
        page = Page(pages={URLS["bilibili"]: PAGE_HTML % "b"})
        content.Reader(reader_cfg(self.tmp, page)).read(URLS["bilibili"])
        self.assertEqual(len(page.calls), 1,
                         "resolve() must reuse its response, not refetch")


class FrontendAuditTests(unittest.TestCase):
    """Static audit of the read-only content page."""

    @classmethod
    def setUpClass(cls):
        ui = HERE.parent / "ui"
        cls.js_path = ui / "content.js"
        cls.css_path = ui / "content.css"
        cls.js = cls.js_path.read_text(encoding="utf-8") if cls.js_path.exists() else ""
        cls.css = cls.css_path.read_text(encoding="utf-8") if cls.css_path.exists() else ""

    def test_files_exist(self):
        self.assertTrue(self.js_path.exists())
        self.assertTrue(self.css_path.exists())

    def test_no_innerhtml(self):
        """No HTML-string injection anywhere in the runtime code."""
        code = _js_code(self.js)
        self.assertNotRegex(code, r"\binnerHTML\b")
        self.assertNotRegex(code, r"\bouterHTML\b")
        self.assertNotRegex(code, r"insertAdjacentHTML")
        self.assertNotRegex(code, r"\bdocument\.write\b")
        self.assertNotRegex(code, r"\beval\s*\(")
        self.assertNotRegex(code, r"new\s+Function\b")

    def test_no_write_actions_in_ui(self):
        """Only read verbs may reach the screen — no write affordances."""
        code = _js_code(self.js)
        for word in ("发布", "评论", "转发", "点赞", "私信", "分享到"):
            self.assertNotRegex(code, re.escape(word), word)

    def test_no_inline_handlers_or_remote_assets(self):
        code = _js_code(self.js)
        self.assertNotRegex(code, r"""\son[a-z]+\s*=\s*['"]""")
        # No remote assets: `data:` URIs are fine (the only one is an inline SVG
        # arrow), but nothing may be loaded from another origin.
        for u in re.findall(r"url\(\s*['\"]?([^'\")\s]+)", self.css):
            self.assertTrue(u.startswith("data:"), u)
        self.assertNotRegex(self.css, r"@import\s+(?:url\()?['\"]?https?:")
        # No inline style writes (keeps CSP free of style-src 'unsafe-inline').
        self.assertNotRegex(code, r"\.style\.[a-zA-Z]+\s*=")

    def test_exports_mount(self):
        self.assertIn("DaFeiYuContent", self.js)
        self.assertIn("data-content-root", self.js)

    def test_css_vars_and_dark_theme(self):
        self.assertIn("var(--ink)", self.css)
        self.assertIn("var(--line)", self.css)

    def test_index_integration_hook_present(self):
        """Content page is wired into index.html with a stable hook."""
        html = (HERE.parent / "ui/index.html").read_text(encoding="utf-8")
        self.assertIn("data-content-root", html)
        self.assertIn("content.js", html)
        self.assertIn("content.css", html)

    def test_does_not_prefetch_before_login(self):
        """Mounting must not call the API; loading is gated on #console visible."""
        code = _js_code(self.js)
        mount_src = code[code.index("function mount("):code.index("function refresh(")]
        self.assertNotRegex(mount_src, r"^\s*refresh\(\);",
                            "mount() must not fire requests unconditionally")
        self.assertIn("opts.load", mount_src)
        # refresh() refuses unless the (post-login) console is visible.
        self.assertIn("#console:not([hidden])", code)
        self.assertIn("function activate(", code)
        self.assertIn("MutationObserver", code)


if __name__ == "__main__":
    unittest.main(verbosity=2)
