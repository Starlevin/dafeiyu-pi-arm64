#!/usr/bin/env python3
"""记账层验收测试：只用**合成夹具**，不联网、不调用任何真实/付费模型。

覆盖：
  A. usage 归一（OpenAI chat / OpenAI Responses / Anthropic messages / Anthropic 流式）
  B. 流式与非流式取数（SSE 行解析：三种真实形态）
  C. fallback：主通道失败后，主/备两个真正计费的模型各记一次
  D. 缺字段：provider 没给 usage → 只计 missing，不估算
  E. 状态持久化：新进程（新对象）从同一文件读到相同累计
  F. 重复请求 / 重启：同一令牌不会重复计数
  G. 日期切换：今日 / 近 7 日 / 累计 各自口径正确
  H. 记账失败不阻断：坏账本、坏 usage、写盘失败都不抛异常
  I. 补丁脚本：对真实 model-hub.py 的 dry-run 前必须锚点全中（这里用合成样本）

运行：python3 test_usage_ledger.py
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import usage_ledger as L  # noqa: E402


def tmpdir():
    return Path(tempfile.mkdtemp(prefix="usage-test-"))


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tmpdir()
        self.path = self.dir / "ledger.json"
        self.logs = []
        self.ledger = L.UsageLedger(self.path, log=self.logs.append)

    def tearDown(self):
        shutil.rmtree(str(self.dir), ignore_errors=True)

    def rows(self, window="since"):
        data = self.ledger.dashboard()
        return {(r["provider"], r["model"]): r for r in data[window]["providers"]}


class TestNormalize(Base):
    """A. 三种真实 usage 形态的归一。"""

    def test_openai_chat(self):
        self.assertEqual(
            L.normalize_usage({"prompt_tokens": 12, "completion_tokens": 7, "total_tokens": 19}),
            (12, 7, 19))

    def test_openai_responses(self):
        self.assertEqual(
            L.normalize_usage({"input_tokens": 40, "output_tokens": 9, "total_tokens": 49}),
            (40, 9, 49))

    def test_anthropic_no_total(self):
        # Anthropic 不给 total：用 provider 自己的 input+output 补齐（不是估算）。
        self.assertEqual(
            L.normalize_usage({"input_tokens": 30, "output_tokens": 5}),
            (30, 5, 35))

    def test_anthropic_cache_tokens_fold_into_input(self):
        self.assertEqual(
            L.normalize_usage({"input_tokens": 10, "output_tokens": 2,
                               "cache_read_input_tokens": 90,
                               "cache_creation_input_tokens": 5}),
            (105, 2, 107))

    def test_missing_is_none_not_zero(self):
        self.assertEqual(L.normalize_usage({}), (None, None, None))
        self.assertEqual(L.normalize_usage(None), (None, None, None))
        self.assertEqual(L.normalize_usage("nope"), (None, None, None))
        self.assertTrue(L.usage_absent({}))
        self.assertFalse(L.usage_absent({"prompt_tokens": 0, "completion_tokens": 0,
                                         "total_tokens": 0}))

    def test_negative_and_bool_rejected(self):
        # -1 和 True 都不是合法计数，视为缺失。
        self.assertEqual(L.normalize_usage({"prompt_tokens": -1, "completion_tokens": True}),
                         (None, None, None))


class TestStreaming(Base):
    """B. 流式取数：三种真实 SSE 形态。"""

    def test_anthropic_sse_two_frames(self):
        lines = [
            "event: message_start",
            'data: {"type":"message_start","message":{"usage":{"input_tokens":25,"output_tokens":1}}}',
            "",
            'data: {"type":"content_block_delta","delta":{"text":"hi"}}',
            "",
            'data: {"type":"message_delta","usage":{"output_tokens":42}}',
            'data: {"type":"message_stop"}',
        ]
        usage, completed = L.sse_usage(lines)
        self.assertTrue(completed)
        self.assertEqual(L.normalize_usage(usage), (25, 42, 67))

    def test_openai_chat_last_chunk_has_usage(self):
        lines = [
            'data: {"choices":[{"delta":{"content":"a"}}]}',
            'data: {"choices":[],"usage":{"prompt_tokens":8,"completion_tokens":3,"total_tokens":11}}',
            "data: [DONE]",
        ]
        usage, completed = L.sse_usage(lines)
        self.assertTrue(completed)
        self.assertEqual(L.normalize_usage(usage), (8, 3, 11))

    def test_openai_responses_completed(self):
        lines = [
            'data: {"type":"response.output_text.delta","delta":"x"}',
            'data: {"type":"response.completed","response":{"usage":{"input_tokens":15,"output_tokens":4,"total_tokens":19}}}',
        ]
        usage, completed = L.sse_usage(lines)
        self.assertTrue(completed)
        self.assertEqual(L.normalize_usage(usage), (15, 4, 19))

    def test_stream_error_is_not_completed(self):
        lines = ['data: {"type":"error","error":{"message":"boom"}}']
        usage, completed = L.sse_usage(lines)
        self.assertFalse(completed)
        self.assertIsNone(usage)

    def test_broken_stream_has_no_usage(self):
        usage, completed = L.sse_usage(['data: {"type":"message_start"}'])
        self.assertFalse(completed)
        self.assertIsNone(usage)


class TestRecord(Base):
    """C/D/F. 一次调用记一次、fallback 两端各一次、缺字段只计 missing。"""

    def test_single_call(self):
        token = self.ledger.reserve("openai-compat@api.x", "m-1")
        self.assertTrue(self.ledger.record("openai-compat@api.x", "m-1",
                                           {"prompt_tokens": 10, "completion_tokens": 5,
                                            "total_tokens": 15}, token=token))
        row = self.rows()[("openai-compat@api.x", "m-1")]
        self.assertEqual((row["in"], row["out"], row["total"], row["calls"], row["missing"]),
                         (10, 5, 15, 1, 0))

    def test_fallback_counts_both_billable_models(self):
        primary = ("openai-compat@127.0.0.1:8317", "fixture-primary-model")
        backup = ("openai-compat@provider.example.invalid", "fixture-backup-model")
        # 主通道 500：真正计费的一次调用但没 usage → calls+1, missing+1
        t1 = self.ledger.reserve(*primary)
        self.ledger.record(primary[0], primary[1], None, status="http_500", token=t1)
        # 备用成功
        t2 = self.ledger.reserve(*backup)
        self.ledger.record(backup[0], backup[1],
                           {"prompt_tokens": 20, "completion_tokens": 6, "total_tokens": 26},
                           token=t2)
        rows = self.rows()
        self.assertEqual(len(rows), 2)
        self.assertEqual((rows[primary]["calls"], rows[primary]["missing"]), (1, 1))
        self.assertEqual((rows[primary]["total"]), 0)   # 没数字就绝不填 0 以外的数
        self.assertEqual((rows[backup]["calls"], rows[backup]["total"]), (1, 26))

    def test_repeated_same_token_not_double_counted(self):
        token = self.ledger.reserve("p", "m")
        usage = {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}
        self.assertTrue(self.ledger.record("p", "m", usage, token=token))
        self.assertFalse(self.ledger.record("p", "m", usage, token=token))
        self.assertFalse(self.ledger.record("p", "m", usage, token=token))
        row = self.rows()[("p", "m")]
        self.assertEqual((row["calls"], row["total"]), (1, 2))

    def test_restart_does_not_recount(self):
        token = self.ledger.reserve("p", "m")
        usage = {"prompt_tokens": 3, "completion_tokens": 4, "total_tokens": 7}
        self.ledger.record("p", "m", usage, token=token)
        # 模拟重启：全新对象读同一个文件，再用同一令牌重放
        revived = L.UsageLedger(self.path, log=self.logs.append)
        self.assertFalse(revived.record("p", "m", usage, token=token))
        row = {r["model"]: r for r in revived.dashboard()["since"]["providers"]}["m"]
        self.assertEqual((row["calls"], row["total"]), (1, 7))

    def test_missing_usage_counted_separately(self):
        for _ in range(3):
            token = self.ledger.reserve("p", "m")
            self.ledger.record("p", "m", {}, token=token)
        row = self.rows()[("p", "m")]
        self.assertEqual((row["calls"], row["missing"], row["in"], row["out"], row["total"]),
                         (3, 3, 0, 0, 0))

    def test_zero_usage_is_not_missing(self):
        token = self.ledger.reserve("p", "m")
        self.ledger.record("p", "m", {"prompt_tokens": 0, "completion_tokens": 0,
                                      "total_tokens": 0}, token=token)
        row = self.rows()[("p", "m")]
        self.assertEqual((row["calls"], row["missing"]), (1, 0))

    def test_no_token_still_counts_call(self):
        self.assertTrue(self.ledger.record("p", "m", {"input_tokens": 1, "output_tokens": 1}))
        self.assertEqual(self.rows()[("p", "m")]["calls"], 1)

    def test_many_models_are_all_counted(self):
        """模型由受控 profile 决定：不再有单日模型数上限，>5 种都要入账且可见。"""
        total = 12
        for i in range(total):
            token = self.ledger.reserve("p", "model-%02d" % i)
            self.assertTrue(self.ledger.record(
                "p", "model-%02d" % i,
                {"input_tokens": 10 + i, "output_tokens": 1, "total_tokens": 11 + i},
                token=token), "第 %d 个模型应入账" % i)
        data = self.ledger.dashboard()
        self.assertEqual(len(data["today"]["providers"]), total)
        self.assertEqual(len(data["since"]["providers"]), total)
        self.assertEqual(data["today"]["totals"]["calls"], total)
        self.assertEqual(data["today"]["totals"]["in"], sum(10 + i for i in range(total)))
        self.assertEqual(data["today"]["totals"]["total"], sum(11 + i for i in range(total)))
        # 没有任何静默丢弃的痕迹
        self.assertFalse([x for x in self.logs if "上限" in x])


class TestLongTermSince(Base):
    """B2. 累计独立保留：日桶被裁剪后，「累计」仍然完整。"""

    def test_since_survives_prune(self):
        now = datetime(2026, 11, 11, 12, 0, tzinfo=L.CST)
        # 一条很老的记录（超出保留期）与一条今天的记录
        for offset, tokens in ((L.RETENTION_DAYS + 30, 500), (0, 7)):
            moment = now - timedelta(days=offset)
            token = self.ledger.reserve("p", "m")
            self.assertTrue(self.ledger.record(
                "p", "m", {"input_tokens": tokens, "output_tokens": 0,
                           "total_tokens": tokens}, token=token, now=moment))
        data = self.ledger.dashboard(today=now)
        # 日桶：只应有今天那一条（老桶被裁）
        self.assertEqual(data["dayCount"], 1)
        self.assertEqual(data["today"]["totals"]["total"], 7)
        # 累计：两条都在，没被保留期删掉
        self.assertEqual(data["since"]["totals"]["total"], 507)
        self.assertEqual(data["since"]["totals"]["calls"], 2)

    def test_prune_does_not_touch_since(self):
        now = datetime(2026, 11, 11, 12, 0, tzinfo=L.CST)
        token = self.ledger.reserve("p", "m")
        self.ledger.record("p", "m", {"input_tokens": 3, "output_tokens": 0,
                                      "total_tokens": 3}, token=token, now=now)
        before = self.ledger.dashboard(today=now)["since"]["totals"]
        # 直接构造远超保留期的日桶，再触发一次裁剪
        self.ledger.state["buckets"]["2000-01-01"] = {"s-9": [99, 99, 99, 9, 0]}
        self.ledger._prune()
        self.assertNotIn("2000-01-01", self.ledger.state["buckets"])
        after = self.ledger.dashboard(today=now)["since"]["totals"]
        self.assertEqual(before, after)

    def test_since_matches_sum_of_day_buckets_when_not_pruned(self):
        now = datetime(2026, 11, 11, 23, 0, tzinfo=L.CST)
        day_sum = {"in": 0, "out": 0, "total": 0, "calls": 0, "missing": 0}
        for offset, tokens in ((0, 5), (1, 11), (3, 13)):
            token = self.ledger.reserve("p", "m")
            usage = ({"input_tokens": tokens, "output_tokens": 0, "total_tokens": tokens}
                     if tokens != 11 else {})
            self.ledger.record("p", "m", usage, token=token,
                               now=now - timedelta(days=offset))
        data = self.ledger.dashboard(today=now)
        for key in ("in", "out", "total", "calls", "missing"):
            self.assertEqual(data["since"]["totals"][key],
                             data["days7"]["totals"][key],
                             "未裁剪时累计应等于近 7 日之和：%s" % key)


class TestTimezone(Base):
    """B3. 日期固定按 Asia/Shanghai，与主机本地时区无关。"""

    def test_day_boundary_is_shanghai(self):
        # 上海 2026-11-12 00:30 = UTC 2026-11-11 16:30
        # 若按 UTC 或其它时区算，会落到 11 号。
        moment = datetime(2026, 11, 12, 0, 30, tzinfo=L.CST)
        self.assertEqual(L.day_key(moment), "2026-11-12")
        utc_same = moment.astimezone(timezone.utc)
        self.assertEqual(utc_same.strftime("%Y-%m-%d"), "2026-11-11")
        self.assertEqual(L.day_key(utc_same), "2026-11-12")

        token = self.ledger.reserve("p", "m")
        self.ledger.record("p", "m", {"input_tokens": 1, "output_tokens": 0,
                                      "total_tokens": 1}, token=token, now=utc_same)
        data = self.ledger.dashboard(today="2026-11-12")
        self.assertFalse(data["today"]["empty"], "应落在上海 11-12")
        data11 = self.ledger.dashboard(today="2026-11-11")
        self.assertTrue(data11["today"]["empty"], "不应落在 11-11")

    def test_naive_datetime_treated_as_shanghai(self):
        naive = datetime(2026, 11, 12, 0, 5)
        self.assertEqual(L.day_key(naive), "2026-11-12")

    def test_today_uses_shanghai_not_host(self):
        self.assertEqual(L.CST.utcoffset(None), timedelta(hours=8))


class TestRollback(Base):
    """C. 写盘失败必须精确回滚：内存与磁盘一致，恢复后继续正确计数。"""

    def _block_writes(self):
        """让 os.replace 失败（目标路径变成目录），其余写盘逻辑保持原样。"""
        target = self.ledger.path
        saved = target.read_bytes() if target.exists() else None
        if saved is not None:
            backup = target.with_suffix(".saved")
            backup.write_bytes(saved)
            target.unlink()
        target.mkdir()                      # 目录 → os.replace 必然失败
        return saved

    def _unblock_writes(self, saved):
        target = self.ledger.path
        target.rmdir()
        if saved is not None:
            backup = target.with_suffix(".saved")
            target.write_bytes(backup.read_bytes())
            backup.unlink()

    def test_failed_flush_rolls_back_all_counters(self):
        token = self.ledger.reserve("p", "m")
        self.ledger.record("p", "m", {"input_tokens": 5, "output_tokens": 5,
                                      "total_tokens": 10}, token=token)
        good = self.ledger.dashboard()["since"]["totals"].copy()
        saved = self._block_writes()
        try:
            token2 = self.ledger.reserve("p", "m")
            ok = self.ledger.record("p", "m", {"input_tokens": 100, "output_tokens": 100,
                                               "total_tokens": 200}, token=token2)
        finally:
            self._unblock_writes(saved)
        self.assertFalse(ok, "写盘失败必须返回 False")
        after = self.ledger.dashboard()["since"]["totals"]
        self.assertEqual(after, good, "in/out/total/calls/missing 必须全部回滚")
        self.assertTrue(any("写入失败" in line for line in self.logs))

    def test_failed_flush_restores_seen_so_retry_counts_once(self):
        saved = self._block_writes()
        try:
            token = self.ledger.reserve("p", "m")
        finally:
            self._unblock_writes(saved)
        # 预登记失败后：令牌未被幂等表永久收留
        self.assertFalse(bool(getattr(token, "unsaved", False) and token),
                         "unsaved 句柄应为空令牌")

        token = self.ledger.reserve("p", "m")
        usage = {"input_tokens": 7, "output_tokens": 3, "total_tokens": 10}
        saved = self._block_writes()
        try:
            self.assertFalse(self.ledger.record("p", "m", usage, token=token))
            # 写盘失败的那次不能进 seen，否则重试会被误判为重复
            self.assertFalse(self.ledger.already_recorded(token))
        finally:
            self._unblock_writes(saved)

        # 恢复写盘后重试：必须正好计一次
        self.assertTrue(self.ledger.record("p", "m", usage, token=token))
        self.assertFalse(self.ledger.record("p", "m", usage, token=token))
        totals = self.ledger.dashboard()["since"]["totals"]
        self.assertEqual((totals["calls"], totals["in"], totals["out"], totals["total"]),
                         (1, 7, 3, 10))

    def test_recovered_ledger_continues_counting_correctly(self):
        token = self.ledger.reserve("p", "m")
        self.ledger.record("p", "m", {"input_tokens": 4, "output_tokens": 1,
                                      "total_tokens": 5}, token=token)
        saved = self._block_writes()
        try:
            t2 = self.ledger.reserve("p", "m")
            self.assertFalse(self.ledger.record("p", "m", {"input_tokens": 90,
                                                           "output_tokens": 10,
                                                           "total_tokens": 100}, token=t2))
        finally:
            self._unblock_writes(saved)
        # 恢复后正常记账 + 重启读回：磁盘与内存完全一致
        t3 = self.ledger.reserve("p", "m")
        self.assertTrue(self.ledger.record("p", "m", {"input_tokens": 6, "output_tokens": 4,
                                                      "total_tokens": 10}, token=t3))
        mem = self.ledger.dashboard()["since"]["totals"]
        disk = L.UsageLedger(self.ledger.path).dashboard()["since"]["totals"]
        self.assertEqual(mem, disk, "内存与磁盘必须一致")
        self.assertEqual((disk["calls"], disk["total"]), (2, 15))


class TestSchemaMigration(Base):
    """C2. 旧 schema(1) 账本升级：用历史日桶重建累计，之后裁剪不丢。"""

    def test_v1_ledger_gets_since_and_keeps_totals(self):
        old = {
            "schema": 1, "started": 1700000000,
            "buckets": {"2026-11-10": {"s-aaaa": [10, 2, 12, 1, 0]},
                        "2026-11-11": {"s-aaaa": [5, 1, 6, 1, 1]}},
            "slots": {"s-aaaa": {"p": "p", "m": "m"}},
            "seen": {}, "reserved": [], "rev": 3,
        }
        self.path.write_text(json.dumps(old), encoding="utf-8")
        ledger = L.UsageLedger(self.path)
        data = ledger.dashboard(today="2026-11-11")
        self.assertTrue(data["available"])
        self.assertEqual(data["since"]["totals"]["total"], 18)
        self.assertEqual(data["since"]["totals"]["calls"], 2)
        self.assertEqual(data["since"]["totals"]["missing"], 1)
        self.assertEqual(data["startedAt"], 1700000000)
        # 升级后落盘为 schema 2
        on_disk = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(on_disk["schema"], 2)
        self.assertIn("since", on_disk)


class TestPersistence(Base):
    """E. 状态持久化 + 原子性。"""

    def test_reload_keeps_totals_and_start_time(self):
        token = self.ledger.reserve("p", "m")
        self.ledger.record("p", "m", {"input_tokens": 5, "output_tokens": 5}, token=token)
        first = self.ledger.dashboard()
        revived = L.UsageLedger(self.path)
        second = revived.dashboard()
        self.assertEqual(first["since"]["totals"], second["since"]["totals"])
        self.assertEqual(first["startedAt"], second["startedAt"])
        self.assertGreater(first["startedAt"], 0)

    def test_file_is_owner_only(self):
        self.ledger.record("p", "m", {"input_tokens": 1, "output_tokens": 1})
        if os.name != "posix":
            self.skipTest("POSIX 权限语义；Windows 不适用")
        mode = self.path.stat().st_mode & 0o777
        self.assertEqual(mode, 0o600)

    def test_corrupt_ledger_is_quarantined_not_fatal(self):
        self.path.write_text("{not json")
        ledger = L.UsageLedger(self.path, log=self.logs.append)
        self.assertTrue(ledger.record("p", "m", {"input_tokens": 1, "output_tokens": 1}))
        self.assertEqual(len(list(self.dir.glob("ledger.broken-*"))), 1)
        self.assertTrue(ledger.dashboard()["available"])

    def test_unwritable_path_does_not_raise(self):
        """写入失败只记日志、返回 False，绝不抛异常。"""
        # 目标路径本身是个目录 → os.replace 必然失败，等于磁盘不可写。
        target = self.dir / "ledger.json"
        target.mkdir()
        ledger = L.UsageLedger(target, log=self.logs.append)
        self.assertFalse(ledger.record("p", "m", {"input_tokens": 1, "output_tokens": 1},
                                       token="t"))
        self.assertTrue(any("写入失败" in line for line in self.logs))
        self.assertTrue(target.is_dir())          # 原物完好，没有被替换掉


class TestWindows(Base):
    """G. 今日 / 近 7 日 / 累计 的日期口径。"""

    def test_day_boundaries(self):
        now = datetime(2026, 11, 11, 10, 0, 0)
        def put(day, tokens, model="m"):
            token = self.ledger.reserve("p", model)
            self.ledger.record("p", model,
                               {"input_tokens": tokens, "output_tokens": 0,
                                "total_tokens": tokens}, token=token, now=now)

        put(11, 1)                                   # 今天
        self.ledger2 = L.UsageLedger(self.path)
        # 分别写入前 1 天与第 7 天前（含边界）和更早
        for offset, tokens in ((1, 10), (6, 100), (7, 1000)):
            token = self.ledger.reserve("p", "m")
            self.ledger.record("p", "m", {"input_tokens": tokens, "output_tokens": 0,
                                          "total_tokens": tokens},
                               token=token,
                               now=now - timedelta(days=offset))

        data = L.UsageLedger(self.path).dashboard(today=date(2026, 11, 11))
        self.assertEqual(data["today"]["totals"]["total"], 1)
        self.assertEqual(data["days7"]["totals"]["total"], 111)      # 1 + 10 + 100
        self.assertEqual(data["since"]["totals"]["total"], 1111)     # 再含第 7 天前
        self.assertEqual(data["dayCount"], 4)
        self.assertEqual(data["today"]["totals"]["calls"], 1)

    def test_today_rolls_over(self):
        yesterday = datetime.now() - timedelta(days=1)
        token = self.ledger.reserve("p", "m")
        self.ledger.record("p", "m", {"input_tokens": 9, "output_tokens": 0,
                                      "total_tokens": 9}, token=token, now=yesterday)
        data = self.ledger.dashboard()
        self.assertTrue(data["today"]["empty"])
        self.assertFalse(data["days7"]["empty"])
        self.assertEqual(data["days7"]["totals"]["total"], 9)


class TestDegrade(Base):
    """H. 记账失败绝不阻断聊天。"""

    def test_record_swallows_all_errors(self):
        class Boom:
            def __str__(self):
                raise RuntimeError("str boom")
        # 连 __str__ 都抛异常的 provider：仍然只返回 False，不往上抛
        self.assertFalse(self.ledger.record(Boom(), "m", {"input_tokens": 1}))
        # 字段类型不合法时按「缺用量」处理（不抛错）：计一次调用 + 一次缺用量
        self.assertTrue(self.ledger.record("p", "m", {"input_tokens": "x"}))
        row = self.rows()[("p", "m")]
        self.assertEqual((row["calls"], row["missing"]), (1, 1))

    def test_dashboard_never_raises(self):
        self.ledger.state = {"schema": L.SCHEMA, "buckets": {"2026-11-11": {"s-1": "bad"}},
                             "slots": {"s-1": {"p": "p", "m": "m"}}, "seen": {}}
        data = self.ledger.dashboard(today=date(2026, 11, 11))
        self.assertTrue(data["available"])
        self.assertEqual(data["today"]["totals"]["calls"], 0)


class TestProviderInference(Base):
    """provider 名必须来自真实 profile 形态（protocol 取值 chat/responses/oauth）。"""

    def test_protocols(self):
        self.assertEqual(
            L.infer_provider({"protocol": "chat", "base_url": "http://127.0.0.1:8317/v1"}),
            "openai-compat@127.0.0.1")
        self.assertEqual(
            L.infer_provider({"protocol": "responses", "base_url": "https://token.example/v1"}),
            "openai-responses@token.example")
        self.assertEqual(
            L.infer_provider({"protocol": "oauth", "base_url": "https://api.openai.com/v1"}),
            "chatgpt-oauth@api.openai.com")
        # 显式声明优先
        self.assertEqual(
            L.infer_provider({"protocol": "chat", "base_url": "http://127.0.0.1:8317/v1",
                              "provider": "example-provider"}), "example-provider")
        # 未知协议也不炸
        self.assertTrue(L.infer_provider({"protocol": "weird", "base_url": "https://x.y"}))


class TestFixtureRepo(Base):
    """确认仓库里的合成夹具是脱敏的（不含真实地址/密钥特征）。"""

    def test_fixture_has_no_secrets(self):
        fixture = HERE.parent / "server" / "fixtures" / "usage.fixed.json"
        text = fixture.read_text(encoding="utf-8")
        for needle in ("192.168.", "admin.key", "sk-", "Bearer "):
            self.assertNotIn(needle, text)
        data = json.loads(text)
        self.assertEqual(data["available"], True)
        self.assertIn("today", data)


if __name__ == "__main__":
    unittest.main(verbosity=2)
