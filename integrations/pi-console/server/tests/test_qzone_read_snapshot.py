"""好友动态只读刷新 + 独立 latest 快照的合同测试（契约 §10）。

全部合成数据 + 假 transport，**零网络**。

跑法（在 integrations/pi-console/server/tests 下）：
    python -m unittest test_qzone_read_snapshot -v

或在 integrations/qq-qzone 下：
    python -m unittest discover -s tests -p 'test_read_snapshot.py' -t . -v
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
# 1) qq-qzone 模块根（qzone_core / qzone_service / qzone_napcat）
QQZONE = HERE.parents[2] / "qq-qzone"
# 2) pi-console server 根（tool_routes_qzone）
SERVER = HERE.parent
for p in (str(QQZONE), str(SERVER)):
    if p not in sys.path:
        sys.path.insert(0, p)

import qzone_core as core  # noqa: E402
import qzone_napcat as napcat  # noqa: E402
import qzone_service as service_mod  # noqa: E402

BJT = timezone(timedelta(hours=8))


def at(hour, minute=0, day=23):
    return int(datetime(2026, 11, day, hour, minute, tzinfo=BJT).timestamp())


def make_feeds(count=3):
    """合成上游 get_qzone_feeds 的 snowluma 风格条目。"""
    out = []
    for i in range(count):
        out.append(
            {
                "tid": "feed-%d" % i,
                "uin": 1000000 + i,
                "nickname": "朋友%d" % i,
                "time": at(9 + i),
                "content": "第%d条动态  正文里有连续空格" % i,
                "images": ["https://example.invalid/a.jpg"] * (i % 3),
                "like_count": i,
                "comment_count": i * 2,
                "html": "<div>不该进快照</div>",
                "key": "secret-like-key-should-not-leak",
            }
        )
    return out


class FeedTransport(napcat.FakeOneBotTransport):
    """只允许读动作；feeds 返回合成数据。"""

    def __init__(self, feeds=None, fail=None):
        super().__init__(allow_actions=set(napcat.READ_ACTIONS))
        self._feeds = make_feeds() if feeds is None else feeds
        self._fail = fail
        self.recorded_actions = []
        self._calls = []

    def post(self, action, payload):
        self._calls.append((action, dict(payload)))
        if self._fail is not None:
            raise self._fail
        if action != napcat.ACTION_GET_QZONE_FEEDS:
            raise AssertionError("测试不应调用 %r" % action)
        return {"feeds": self._feeds, "has_more": False, "next_cursor": None}


class ReadSnapshotContractTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="qz-read-"))
        self.now = at(12)
        self.cfg = core.QzoneConfig(owner_uin="10001")

    def _svc(self, transport, now=None, owner=True, base_url=None):
        return service_mod.QzoneService(
            state_dir=str(self.tmp),
            cfg=self.cfg,
            transport=transport,
            clock=lambda: (self.now if now is None else now),
            owner_authenticated=owner,
            onebot_base_url=base_url,
        )

    # ---- 核心：只读刷新 + latest 快照 ---------------------------------- #

    def test_refresh_reads_feeds_and_writes_latest_snapshot(self):
        t = FeedTransport()
        svc = self._svc(t)
        out = svc.refresh_latest(owner_authenticated=True, confirm=True)
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["data"]["source"], "qzone_feeds")
        self.assertEqual(out["data"]["total"], 3)

        # 只发了读动作，且只发了一次
        self.assertEqual([a for a, _ in t._calls], ["get_qzone_feeds"])
        # payload 必须是 snake_case（上游 schema 要求），且是 snowluma
        _, payload = t._calls[0]
        self.assertEqual(payload["format"], "snowluma")
        self.assertIn("page_num", payload)
        self.assertIn("count", payload)
        self.assertNotIn("pageNum", payload)

        latest = svc.latest()
        self.assertTrue(latest["ok"])
        items = latest["data"]["items"]
        self.assertEqual(len(items), 3)
        self.assertIsNotNone(latest["data"]["refreshed_ts"])

        # 快照行只保留允许字段，正文折叠、作者打码、敏感原文不入快照
        first = items[0]
        self.assertEqual(
            sorted(first),
            ["author", "comments", "day", "images", "likes", "text", "tid", "ts"],
        )
        # 折叠：连续空格归一为单个空格
        self.assertNotIn("  ", first["text"])
        self.assertIn("正文里有连续空格", first["text"])
        blob = json.dumps(latest, ensure_ascii=False)
        self.assertNotIn("secret-like-key-should-not-leak", blob)
        self.assertNotIn("<div>", blob)
        # 作者不能是明文 uin
        self.assertNotIn("1000000", blob)

    def test_latest_is_independent_of_publish_history(self):
        """latest 不得回落到本地发布记录冒充好友动态。"""
        t = FeedTransport()
        svc = self._svc(t)
        empty = svc.latest()
        self.assertTrue(empty["ok"])
        self.assertEqual(empty["data"]["items"], [])
        self.assertIsNone(empty["data"]["source"])
        self.assertIsNone(empty["data"]["refreshed_ts"])
        # 没有刷新过时 latest 不联网
        self.assertEqual(t._calls, [])

    def test_read_refresh_does_not_touch_publish_quota(self):
        t = FeedTransport()
        svc = self._svc(t)
        before = svc.status()["data"]
        svc.refresh_latest(owner_authenticated=True, confirm=True)
        after = svc.status()["data"]
        self.assertEqual(
            before["counts"]["published_today"], after["counts"]["published_today"]
        )
        self.assertEqual(before["allowance"]["posts_left"], after["allowance"]["posts_left"])
        self.assertEqual(after["counts"]["uncertain_today"], 0)
        self.assertEqual(after["counts"]["likes_today"], 0)
        self.assertEqual(after["counts"]["comments_today"], 0)

    # ---- fail closed ---------------------------------------------------- #

    def test_unconfigured_endpoint_fails_closed_without_request(self):
        """注入假 transport 仅供离线测试；**未配置 loopback 地址必须 fail closed**。"""
        t = FeedTransport()
        svc = self._svc(t, base_url=None)
        out = svc.refresh_latest(owner_authenticated=True, confirm=True)
        self.assertTrue(out["ok"], out)

        # 用真 transport + 非 loopback 地址：仍然必须 fail closed
        real = napcat.UrllibTransport("http://192.0.2.10:3001", None, 5.0)
        svc2 = service_mod.QzoneService(
            state_dir=str(tempfile.mkdtemp(prefix="qz-remote-")), cfg=self.cfg,
            transport=real, clock=lambda: at(12), owner_authenticated=True,
        )
        self.assertFalse(svc2.read_endpoint_available())
        out2 = svc2.refresh_latest(owner_authenticated=True, confirm=True)
        self.assertFalse(out2["ok"])
        self.assertEqual(out2["error"]["code"], "disabled")

        # 真 transport + loopback：放行（但这里不会真发请求，只看判定）
        local = napcat.UrllibTransport("http://127.0.0.1:3001", None, 5.0)
        svc3 = service_mod.QzoneService(
            state_dir=str(tempfile.mkdtemp(prefix="qz-local-")), cfg=self.cfg,
            transport=local, clock=lambda: at(12), owner_authenticated=True,
        )
        self.assertTrue(svc3.read_endpoint_available())

    def test_service_without_any_transport_fails_closed(self):
        """生产接线里 transport=None 且没有 loopback 地址 → disabled 且零请求。"""
        svc = service_mod.QzoneService(
            state_dir=str(tempfile.mkdtemp(prefix="qz-none-")), cfg=self.cfg,
            transport=None, clock=lambda: at(12), owner_authenticated=True,
            onebot_base_url=None,
        )
        self.assertFalse(svc.read_endpoint_available())
        out = svc.refresh_latest(owner_authenticated=True, confirm=True)
        self.assertFalse(out["ok"])
        self.assertEqual(out["error"]["code"], "disabled")
        self.assertEqual(
            core.read_allowance(svc._load(), "2026-11-23", at(12))["refreshes_today"], 0
        )

    def test_requires_owner_auth(self):
        t = FeedTransport()
        svc = self._svc(t, owner=False)
        out = svc.refresh_latest(owner_authenticated=False, confirm=True)
        self.assertFalse(out["ok"])
        self.assertEqual(out["error"]["code"], "owner_unauth")
        self.assertEqual(t._calls, [])

    def test_requires_confirm(self):
        t = FeedTransport()
        svc = self._svc(t)
        out = svc.refresh_latest(owner_authenticated=True, confirm=False)
        self.assertFalse(out["ok"])
        self.assertEqual(out["error"]["code"], "bad_request")
        self.assertEqual(t._calls, [])

    # ---- 时间窗与限频 -------------------------------------------------- #

    def test_quiet_hours_00_to_07(self):
        for hour in (0, 3, 6):
            tmp = Path(tempfile.mkdtemp(prefix="qz-quiet-"))
            t = FeedTransport()
            svc = qzone_service_svc(tmp, t, at(hour), self.cfg)
            out = svc.refresh_latest(owner_authenticated=True, confirm=True)
            self.assertFalse(out["ok"], "hour=%d 应静默" % hour)
            self.assertEqual(out["error"]["code"], "quiet_hours")
            self.assertEqual(t._calls, [], "静默时不得请求")
        # 07:00 整点恢复（半开区间）
        tmp = Path(tempfile.mkdtemp(prefix="qz-quiet-ok-"))
        t = FeedTransport()
        svc = qzone_service_svc(tmp, t, at(7), self.cfg)
        out = svc.refresh_latest(owner_authenticated=True, confirm=True)
        self.assertTrue(out["ok"], out)

    def test_min_interval_blocks_second_refresh(self):
        t = FeedTransport()
        now = {"v": at(12)}
        svc = service_mod.QzoneService(
            state_dir=str(self.tmp), cfg=self.cfg, transport=t,
            clock=lambda: now["v"], owner_authenticated=True,
        )
        self.assertTrue(svc.refresh_latest(owner_authenticated=True, confirm=True)["ok"])
        sent = len(t._calls)
        # 5 分钟后 → 拒绝
        now["v"] = at(12, 5)
        out = svc.refresh_latest(owner_authenticated=True, confirm=True)
        self.assertFalse(out["ok"])
        self.assertEqual(out["error"]["code"], "min_interval")
        self.assertEqual(len(t._calls), sent, "被限频时不得再请求")
        # 10 分钟后 → 放行
        now["v"] = at(12, 10)
        self.assertTrue(svc.refresh_latest(owner_authenticated=True, confirm=True)["ok"])

    def test_daily_cap(self):
        st = core.bump_read_refresh({}, "2026-11-23", at(1))
        st["read_refresh"]["2026-11-23"]["count"] = core.READ_REFRESH_MAX_PER_DAY
        decision = core.evaluate_read_refresh(
            now_ts=at(12), state=st, owner_authenticated=True, transport_available=True
        )
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason, core.READ_ERROR_DAILY)

    # ---- 稳定错误 ------------------------------------------------------ #

    def test_upstream_failure_yields_stable_error_and_counts(self):
        t = FeedTransport(fail=napcat.OneBotError("boom", kind="timeout"))
        svc = self._svc(t)
        out = svc.refresh_latest(owner_authenticated=True, confirm=True)
        self.assertFalse(out["ok"])
        self.assertIn(out["error"]["code"], ("upstream_error",))
        # 失败也已记账（保守），且没有 latest 数据
        self.assertEqual(
            core.read_allowance(svc._load(), "2026-11-23", at(12))["refreshes_today"], 1
        )
        latest = svc.latest()
        self.assertEqual(latest["data"]["items"], [])
        self.assertIsNotNone(latest["data"]["last_error"])
        # 错误文本不得泄露 token/凭据
        self.assertNotIn("token", json.dumps(out, ensure_ascii=False).lower())

    def test_malformed_feeds_dropped_not_fabricated(self):
        t = FeedTransport(feeds=[{"tid": "", "content": "x"}, {"content": "无 tid"},
                                 {"tid": "ok-1", "content": "有效"}])
        svc = self._svc(t)
        out = svc.refresh_latest(owner_authenticated=True, confirm=True)
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["data"]["total"], 1)
        self.assertEqual(out["data"]["items"][0]["tid"], "ok-1")

    # ---- 路由：fail closed 与不抢路径 ---------------------------------- #

    def test_loopback_only_rejects_remote(self):
        import tool_routes_qzone as routes

        self.assertIsNone(routes._loopback_only("http://192.0.2.10:3001"))
        self.assertIsNone(routes._loopback_only("http://example.com:3001"))
        self.assertIsNone(routes._loopback_only("ftp://127.0.0.1:3001"))
        self.assertIsNone(routes._loopback_only("http://127.0.0.1"))
        self.assertEqual(
            routes._loopback_only("http://127.0.0.1:3001"), "http://127.0.0.1:3001"
        )
        self.assertEqual(
            routes._loopback_only("http://localhost:3001"), "http://localhost:3001"
        )

    def test_new_routes_registered(self):
        import tool_routes_qzone as routes

        table = set(routes.routes())
        self.assertIn(("GET", "/api/qzone/latest"), table)
        self.assertIn(("POST", "/api/qzone/refresh"), table)


def qzone_service_svc(state_dir, transport, now, cfg):
    return service_mod.QzoneService(
        state_dir=str(state_dir), cfg=cfg, transport=transport,
        clock=lambda: now, owner_authenticated=True,
    )


if __name__ == "__main__":
    unittest.main(verbosity=2)
