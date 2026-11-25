"""Isolated tests for the read-only live WebSocket bridge.

Everything here runs in-process: no platform services, no production logs, and
no messages are ever sent. The real-loopback tests bind an ephemeral port on
127.0.0.1 and exercise the actual HTTP upgrade path.
"""

from __future__ import annotations

import importlib.util
import io
import json
import os
import socket
import struct
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend import live_ws  # noqa: E402
from backend.flow_bridge import (  # noqa: E402
    BUBBLE_TTL_MS,
    DEFAULT_THEME_URL,
    PHASE_BUBBLES,
    PHASES,
    FlowRouter,
    ThemeFlow,
    bubble_expired,
    bubble_for,
    build_router,
    config_message,
    resolve_selection,
    sanitize_bubble,
    theme_slug_for_route,
)

NOW = 1_800_000_000

THEME = {
    "schema": 1,
    "meta": {"id": "test", "name": "测试主题"},
    "config": {
        "flow": {
            "nodes": [
                {"id": "w1", "label": "休息", "video": "videos/w1.mp4", "loop": True,
                 "role": "wait", "duration": 12.0, "media_id": "m_w1"},
                {"id": "pick", "label": "拿手机", "video": "videos/pick.mp4", "loop": False,
                 "role": "action", "duration": 4.0, "media_id": "m_pick"},
                {"id": "ph1", "label": "玩手机", "video": "videos/ph1.mp4", "loop": True,
                 "role": "phone", "duration": 8.0, "media_id": "m_ph1"},
                {"id": "put", "label": "放下", "video": "videos/put.mp4", "loop": False,
                 "role": "transition", "duration": 3.0, "media_id": "m_put"},
                {"id": "wk1", "label": "干活", "video": "videos/wk1.mp4", "loop": True,
                 "role": "both", "duration": 15.0, "media_id": "m_wk1"},
            ],
            "edges": [
                {"id": "e1", "from": "w1", "to": "pick"},
                {"id": "e2", "from": "pick", "to": "ph1"},
                {"id": "e3", "from": "ph1", "to": "put"},
                {"id": "e4", "from": "put", "to": "w1"},
                {"id": "e5", "from": "w1", "to": "wk1"},
            ],
        },
        "videos": {"w1": "videos/w1.mp4"},
        "durations": {"w1": 12.0},
    },
}


# ---------------------------------------------------------------- theme graph
class ThemeGraphTests(unittest.TestCase):
    def router(self):
        return FlowRouter(ThemeFlow(THEME))

    def test_waypoints_come_from_roles_not_hardcoded_ids(self):
        flow = ThemeFlow(THEME)
        self.assertTrue(flow.ok)
        self.assertEqual(flow.wait_node()["id"], "w1")
        self.assertEqual(flow.phone_node()["id"], "ph1")
        self.assertEqual(flow.work_node()["id"], "wk1")

    def test_pickup_segment_is_wait_then_phone_walk(self):
        router = self.router()
        orders = router.orders("received", current=None)
        self.assertEqual([o["node_id"] for o in orders], ["pick", "ph1"])
        # pickup 是一次性前段：播一轮就交给手机循环。
        self.assertEqual([o["loop"] for o in orders], [False, False])

    def test_thinking_and_replying_loop_on_phone(self):
        router = self.router()
        for phase in ("thinking", "replying"):
            orders = router.orders(phase, current="ph1")
            self.assertEqual([o["node_id"] for o in orders], ["ph1"])
            self.assertTrue(orders[0]["loop"])

    def test_putdown_segment_walks_back_to_wait(self):
        router = self.router()
        orders = router.orders("sent", current="ph1")
        self.assertEqual([o["node_id"] for o in orders], ["put", "w1"])
        self.assertEqual([o["loop"] for o in orders], [False, False])

    def test_idle_is_wait_loop(self):
        router = self.router()
        orders = router.orders("idle", current="w1")
        self.assertEqual([o["node_id"] for o in orders], ["w1"])
        self.assertTrue(orders[0]["loop"])

    def test_missing_phone_node_falls_back_conservatively(self):
        theme = json.loads(json.dumps(THEME))
        theme["config"]["flow"]["nodes"] = [
            n for n in theme["config"]["flow"]["nodes"] if n["role"] != "phone"]
        theme["config"]["flow"]["edges"] = [e for e in theme["config"]["flow"]["edges"]
                                            if e["from"] not in ("pick", "ph1")
                                            and e["to"] not in ("pick", "ph1")]
        router = FlowRouter(ThemeFlow(theme))
        self.assertIsNone(router.flow.phone_node())
        # 没有手机节点时不能硬编码别的 id，只能退回等待循环。
        orders = router.orders("thinking", current="w1")
        self.assertEqual([o["node_id"] for o in orders], ["w1"])

    def test_unknown_current_node_reanchors_without_pretending_continuity(self):
        router = self.router()
        orders = router.orders("sent", current="gone")
        self.assertEqual([o["node_id"] for o in orders], ["w1"])

    def test_empty_or_broken_theme_reports_not_ok(self):
        self.assertFalse(ThemeFlow(None).ok)
        self.assertFalse(ThemeFlow({"config": {}}).ok)
        self.assertFalse(ThemeFlow({"config": {"flow": {"nodes": "x"}}}).ok)
        self.assertEqual(FlowRouter(ThemeFlow(None)).orders("received"), [])

    def test_theme_ignores_malformed_nodes_and_dangling_edges(self):
        theme = json.loads(json.dumps(THEME))
        theme["config"]["flow"]["nodes"].append({"id": "bad id!", "role": "wait"})
        theme["config"]["flow"]["edges"].append({"from": "w1", "to": "nope"})
        flow = ThemeFlow(theme)
        self.assertNotIn("bad id!", [n["id"] for n in flow.nodes])
        self.assertFalse([e for e in flow.edges if e["to"] == "nope"])

    def test_config_message_exposes_theme_url_and_resolved_ids(self):
        message = config_message(self.router())
        self.assertEqual(message["theme_url"], DEFAULT_THEME_URL)
        self.assertTrue(message["theme_ok"])
        # key 是「忙碌位」实际落点；有 phone 角色的主题里它就等于 phone。
        self.assertEqual(message["nodes"],
                         {"wait": "w1", "phone": "ph1", "work": "wk1", "key": "ph1"})
        self.assertEqual(message["bubble_ms"], BUBBLE_TTL_MS)

    def test_repository_theme_matches_upstream_default(self):
        # 明确按固定 default 目录加载：部署端 live.theme.json 选了别的主题时，
        # 这条上游默认主题的一致性断言也不能跟着变。
        router = build_router(ROOT / "web" / "fish_house", selection={
            "theme_url": DEFAULT_THEME_URL,
            "theme_path": ROOT / "web" / "fish_house" / "themes" / "default" / "theme.json",
            "slug": "default", "selected": False, "fallback": "selection_absent"})
        self.assertTrue(router.ok, "repository default theme must parse")
        self.assertEqual(router.theme_url, DEFAULT_THEME_URL)
        flow = router.flow
        # 上游 a3245661 的 default_theme.zip：wait=rest / phone=phone_loop / both=computer_loop
        self.assertEqual(flow.wait_node()["id"], "rest")
        self.assertEqual(flow.phone_node()["id"], "phone_loop")
        self.assertEqual(flow.work_node()["id"], "computer_loop")
        self.assertEqual(flow.key_node()["id"], "phone_loop")
        self.assertTrue(flow.wait_node()["video"].startswith("videos/"))
        self.assertIn("phone_pickup", [n["id"] for n in flow.segment("rest", "phone_loop")])
        self.assertIn("phone_putdown", [n["id"] for n in flow.segment("phone_loop", "rest")])


# -------------------------------------------------------------------- bubbles
class BubbleTests(unittest.TestCase):
    def test_fixed_chinese_bubbles_per_phase(self):
        for phase in PHASES:
            text, ttl = bubble_for(phase)
            if phase == "idle":
                self.assertEqual((text, ttl), ("", 0))
            else:
                self.assertEqual(text, PHASE_BUBBLES[phase])
                self.assertEqual(ttl, BUBBLE_TTL_MS)
                self.assertLessEqual(len(text), 24)

    def test_unknown_phase_stays_silent(self):
        self.assertEqual(bubble_for("nonsense"), ("", 0))

    def test_ttl_expiry_and_invalid_input(self):
        self.assertFalse(bubble_expired("在", NOW, NOW, BUBBLE_TTL_MS))
        self.assertTrue(bubble_expired("在", NOW, NOW + 3.0, BUBBLE_TTL_MS))
        self.assertFalse(bubble_expired("", NOW, NOW + 999, BUBBLE_TTL_MS))
        self.assertTrue(bubble_expired("在", "bad", NOW, BUBBLE_TTL_MS))

    def test_sanitize_strips_urls_and_paths(self):
        self.assertEqual(sanitize_bubble("  https://x.example/a  回来了 "), "回来了")
        fixture_home = "/home/" + "example"
        self.assertEqual(sanitize_bubble(f"{fixture_home}/secret 好了"), "好了")
        self.assertLessEqual(len(sanitize_bubble("x" * 80)), 24)
        self.assertEqual(sanitize_bubble(None), "")


# ---------------------------------------------------------------- evidence
class EvidenceTests(unittest.TestCase):
    def test_qq_client_ttl_tracks_remaining_verified_generation(self):
        panel = {"runtimeRecords": [{
            "state": "generating", "timestamp": NOW, "detail": "QQ聊天模型请求中",
        }]}
        bridge = live_ws.LiveBridge(router_factory=lambda: None, snapshot_getter=dict,
                                    panel_reader=lambda: panel)
        with patch.object(live_ws.time, "time", return_value=NOW + 21):
            snapshot = bridge.state_snapshot()
            self.assertEqual(snapshot["phase"], "thinking")
            self.assertEqual(snapshot["ttl_ms"], 99000)
            self.assertEqual(bridge.produce()[-1]["phase"], "thinking")
            self.assertEqual(bridge.produce(), [])
            panel["runtimeRecords"].clear()
            self.assertEqual(bridge.produce()[-1]["phase"], "idle")

    def test_qq_tool_requires_current_execution_and_takes_the_computer_path(self):
        row = {"state": "generating", "timestamp": NOW - 1,
               "detail": live_ws.QQ_TOOL_DETAIL}
        for change in ({"state": "sending"}, {"timestamp": NOW - 301},
                       {"detail": live_ws.QQ_TOOL_DETAIL + " (not a marker)"}):
            evidence = live_ws.qq_evidence({}, {"runtimeRecords": [row | change]}, now=NOW)
            self.assertNotEqual(evidence.phase, "tool")
        router = build_router(ROOT / "web/fish_house", selection={
            "theme_url": DEFAULT_THEME_URL, "slug": "default",
            "theme_path": ROOT / "web/fish_house/themes/default/theme.json",
        })
        bridge = live_ws.LiveBridge(router_factory=lambda: router,
                                    snapshot_getter=dict,
                                    panel_reader=lambda: {"runtimeRecords": [row]})
        with patch.object(live_ws.time, "time", return_value=NOW):
            state = bridge.state_snapshot()
        self.assertEqual(state["phase"], "tool")
        self.assertEqual(state["event_at"], NOW - 1)
        self.assertEqual(state["ttl_ms"], 299000)
        self.assertEqual([item["node_id"] for item in state["orders"]],
                         ["computer_go", "computer_loop"])
        self.assertEqual([item["loop"] for item in state["orders"]], [False, True])
        self.assertNotIn("detail", state)

    def live(self, wx=None, qq=None):
        return {"wechat": wx or {}, "qq": qq or {}}

    def test_busy_is_never_chat_activity(self):
        live = self.live(qq={"service_running": True, "busy": True})
        snap = live_ws.qq_evidence(live, None, now=NOW)
        self.assertEqual(snap.phase, "idle")
        self.assertEqual(snap.evidence, live_ws.EVIDENCE_DEGRADED)

    def test_qq_confirmed_record_yields_sent_only_when_recent(self):
        recent = {"runtimeRecords": [{"state": "confirmed", "timestamp": NOW - 2,
                                      "_chat": True}]}
        stale = {"runtimeRecords": [{"state": "confirmed", "timestamp": NOW - 60,
                                     "_chat": True}]}
        live = self.live(qq={"service_running": True})
        self.assertEqual(live_ws.qq_evidence(live, recent, now=NOW).phase, "sent")
        self.assertEqual(live_ws.qq_evidence(live, stale, now=NOW).phase, "idle")

    def test_fallback_format_notice_cannot_replace_or_invent_a_receipt(self):
        live = self.live(qq={"service_running": True})
        warning = {"state": "failed", "timestamp": NOW - 1, "detail": live_ws.QQ_FALLBACK_NOTICE}
        receipt = {"state": "confirmed", "timestamp": NOW - 2, "_chat": True}
        failure = {"state": "failed", "timestamp": NOW - 1.5, "detail": "发送失败"}
        old_ingress = {"state": "queued", "timestamp": NOW - 2, "_ingress": True}
        evidence = live_ws.qq_evidence(live, {"runtimeRecords": [warning, receipt]}, now=NOW)
        self.assertEqual((evidence.phase, evidence.at, evidence.reason), ("sent", NOW - 2, "chat_receipt"))
        for records in ([warning], [warning, old_ingress], [warning, failure, receipt],
                        [warning, dict(receipt, timestamp=NOW - 60)],
                        [dict(warning, timestamp=NOW + 1), receipt]):
            with self.subTest(records=records):
                self.assertEqual(live_ws.qq_evidence(live, {"runtimeRecords": records}, now=NOW).phase, "idle")

    def test_only_fixed_native_chat_queue_marker_yields_recent_received(self):
        live = self.live(qq={"service_running": True})
        marker = live_ws.QQ_NATIVE_CHAT_INGRESS_DETAIL
        recent = {"runtimeRecords": [{"state": "queued", "timestamp": NOW - 3,
                                      "detail": marker}]}
        snap = live_ws.qq_evidence(live, recent, now=NOW)
        self.assertEqual((snap.phase, snap.evidence, snap.reason),
                         ("received", live_ws.EVIDENCE_VERIFIED, "native_chat_queued"))
        for item in (
            {"state": "queued", "timestamp": NOW - 1, "detail": "等待处理"},
            {"state": "queued", "timestamp": NOW - 1, "detail": "插画已入队"},
            {"state": "queued", "timestamp": NOW - 1, "detail": "设置已入队"},
            {"state": "confirmed", "timestamp": NOW - 1, "detail": marker},
            {"state": "queued", "timestamp": NOW - 16, "detail": marker},
        ):
            with self.subTest(item=item):
                result = live_ws.qq_evidence(live, {"runtimeRecords": [item]}, now=NOW)
                self.assertNotEqual(result.phase, "received")

    def test_non_chat_confirm_is_not_sent(self):
        # 插画/性格/设置命令也会 confirmed，但绝不算聊天发送。
        panel = {"runtimeRecords": [{"state": "confirmed", "timestamp": NOW - 1,
                                     "_chat": False}]}
        live = self.live(qq={"service_running": True})
        snap = live_ws.qq_evidence(live, panel, now=NOW)
        self.assertEqual(snap.phase, "idle")
        self.assertEqual(snap.reason, "non_chat_confirm")

    def test_chat_confirm_prefix_comes_from_controller_detail(self):
        # 原始 controller 记录：只有确切以 OneBot 已接受发送 开头才算聊天。
        self.assertTrue(live_ws.qq_confirmed_chat(
            {"state": "confirmed", "detail": "OneBot 已接受发送 message_id=1"}))
        self.assertFalse(live_ws.qq_confirmed_chat(
            {"state": "confirmed", "detail": "插画已生成并发出"}))
        self.assertFalse(live_ws.qq_confirmed_chat(
            {"state": "confirmed", "detail": "性格已切换为温柔"}))
        self.assertFalse(live_ws.qq_confirmed_chat({"state": "sending"}))
        self.assertFalse(live_ws.qq_confirmed_chat(None))

    def test_qq_does_not_invent_received_or_thinking(self):
        panel = {"runtimeRecords": [], "queued": 5}
        live = self.live(qq={"service_running": True})
        snap = live_ws.qq_evidence(live, panel, now=NOW)
        self.assertNotIn(snap.phase, ("received", "thinking"))

    def test_qq_active_phase_requires_matching_marker_and_expires(self):
        live = self.live(qq={"service_running": True})
        for state, (marker, phase, window) in live_ws.QQ_ACTIVE_CHAT.items():
            def evidence(detail, at):
                item = {"state": state, "timestamp": at, "detail": detail}
                return live_ws.qq_evidence(live, {"runtimeRecords": [item]}, now=NOW).phase

            with self.subTest(state=state):
                self.assertEqual(evidence(marker, NOW - window), phase)
                self.assertEqual(evidence("工具或设置操作", NOW - 1), "idle")
                self.assertEqual(evidence(marker, NOW - window - 1), "idle")
                self.assertEqual(evidence(marker, NOW + 1), "idle")

    def test_producer_emits_native_qq_ingress_once_when_model_start_is_in_same_poll(self):
        ingress = {"state": "queued", "timestamp": NOW - 2, "_ingress": True}
        active = {"state": "generating", "timestamp": NOW - 1, "_active": True}
        panel = {"runtimeRecords": [active, ingress]}
        bridge = live_ws.LiveBridge(lambda: FlowRouter(ThemeFlow(THEME)), lambda: self.live(),
                                    lambda: panel)
        with patch.object(live_ws.time, "time", return_value=NOW):
            first = bridge.produce()
            second = bridge.produce()
            self.assertEqual(bridge.produce(), [])
        self.assertEqual((first[-1]["phase"], first[-1]["event_at"]), ("received", NOW - 2))
        self.assertEqual((second[-1]["phase"], second[-1]["event_at"]), ("thinking", NOW - 1))
        self.assertEqual(first[0]["node_id"], "pick")

    def test_producer_does_not_replay_terminated_qq_ingress_or_override_newer_wechat(self):
        ingress = {"state": "queued", "timestamp": NOW - 3, "_ingress": True}
        active = {"state": "generating", "timestamp": NOW - 1, "_active": True}
        panel = {"runtimeRecords": [active, ingress]}
        bridge = live_ws.LiveBridge(lambda: FlowRouter(ThemeFlow(THEME)), lambda: self.live(
            wx={"activity_state": "thinking", "activity_at": NOW}), lambda: panel)
        with patch.object(live_ws.time, "time", return_value=NOW):
            self.assertEqual(bridge.produce()[-1]["source"], "wechat")
        for terminal in ("confirmed", "skipped", "failed", "uncertain", "cancelled"):
            row = {"state": terminal, "timestamp": NOW - 2, "_chat": terminal == "confirmed"}
            with self.subTest(terminal=terminal):
                self.assertEqual(live_ws.pending_qq_ingress(
                    {"runtimeRecords": [active, row, ingress]}, after=0, now=NOW), 0)

    def test_wechat_reuses_normalized_activity_state(self):
        live = self.live(wx={"activity_state": "thinking",
                             "diagnostics": {"fresh": True, "reason": "generation_started"}})
        snap = live_ws.wechat_evidence(live, now=NOW)
        self.assertEqual((snap.phase, snap.evidence), ("thinking", live_ws.EVIDENCE_VERIFIED))
        self.assertEqual(live_ws.wechat_evidence(self.live(wx={}), now=NOW).phase, "idle")

    def test_pick_prefers_verified_stage_over_degraded(self):
        live = self.live(wx={"activity_state": "received", "diagnostics": {}},
                         qq={"service_running": True, "busy": True})
        self.assertEqual(live_ws.pick_evidence(live, None, now=NOW).source, "wechat")
        idle = self.live(qq={"service_running": True, "busy": True})
        self.assertEqual(live_ws.pick_evidence(idle, None, now=NOW).evidence,
                         live_ws.EVIDENCE_DEGRADED)

    def test_two_fresh_receipts_between_ticks_are_each_published_once(self):
        live = self.live(wx={"activity_state": "sent", "activity_at": NOW - 1})
        panel = {"runtimeRecords": [{"state": "confirmed", "timestamp": NOW - 2, "_chat": True}]}
        bridge = live_ws.LiveBridge(lambda: FlowRouter(ThemeFlow(THEME)), lambda: live, lambda: panel)
        with patch.object(live_ws.time, "time", return_value=NOW):
            self.assertEqual(bridge.state_snapshot()["source"], "qq")
            for source, stamp in (("qq", NOW - 2), ("wechat", NOW - 1)):
                frames = bridge.produce()
                self.assertEqual([frame["type"] for frame in frames], ["flow_goto", "state"])
                self.assertEqual([(f["source"], f["phase"], f["event_at"]) for f in frames],
                                 [(source, "sent", stamp)] * 2)
                self.assertEqual([o["node_id"] for o in frames[-1]["orders"]],
                                 ["pick", "ph1", "put", "w1"])
            self.assertEqual(bridge.state_snapshot()["source"], "wechat")
            self.assertEqual(bridge.produce(), [])

    def test_presented_receipt_does_not_resume_after_another_source_interrupts(self):
        live = self.live(wx={"activity_state": "sent", "activity_at": NOW - 2},
                         qq={"service_running": True})
        records = []
        bridge = live_ws.LiveBridge(lambda: FlowRouter(ThemeFlow(THEME)), lambda: live,
                                    lambda: {"runtimeRecords": records})
        with patch.object(live_ws.time, "time", return_value=NOW):
            first = bridge.produce()[-1]
            self.assertEqual((first["source"], first["phase"]), ("wechat", "sent"))
            records.append({"state": "generating", "timestamp": NOW - 1, "_active": True})
            interrupted = bridge.produce()[-1]
            self.assertEqual((interrupted["source"], interrupted["phase"]), ("qq", "thinking"))
            records.clear()
            self.assertEqual(bridge.state_snapshot()["phase"], "idle")
            self.assertEqual(bridge.produce()[-1]["phase"], "idle")
            self.assertEqual(bridge.produce(), [])
            live["wechat"]["activity_at"] = NOW - 0.5
            fresh = bridge.produce()[-1]
            self.assertEqual((fresh["source"], fresh["phase"], fresh["event_at"]),
                             ("wechat", "sent", NOW - 0.5))

    def test_wechat_delivery_retained_after_a_later_diagnostic_is_sent_once(self):
        spec = importlib.util.spec_from_file_location("fh_delivery_projection", ROOT / "server.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        delivered = NOW - 0.75
        event = {"phase": "mention", "reason": "direction_unverified",
                 "time": NOW - 0.5, "trace": "c" * 64}
        diagnostics = {"last_confirmed_delivery_at": delivered, "last_event": event,
                       "last_failure": event}
        panel = {"runtimeRecords": [{"state": "generating", "timestamp": NOW - 0.25,
                                     "_active": True}]}
        bridge = live_ws.LiveBridge(lambda: FlowRouter(ThemeFlow(THEME)), mod.live_snapshot,
                                    lambda: panel)
        with patch.object(mod.bridge_health_file, "read", return_value=diagnostics), \
             patch.object(mod, "LIVE_STARTED_AT", NOW - 1), \
             patch.object(live_ws.time, "time", return_value=NOW):
            self.assertEqual(mod.live_snapshot()["wechat"]["activity_state"], "idle")
            self.assertEqual(bridge.state_snapshot()["event_at"], delivered)
            sent = bridge.produce()[-1]
            self.assertEqual((sent["source"], sent["phase"], sent["event_at"]),
                             ("wechat", "sent", delivered))
            self.assertNotIn(event["trace"], json.dumps(sent))
            self.assertEqual(bridge.produce()[-1]["phase"], "thinking")
            self.assertEqual(bridge.produce(), [])
            with patch.object(mod, "LIVE_STARTED_AT", NOW - 0.6):
                self.assertEqual(mod.live_snapshot()["wechat"]["delivery_at"], 0.0)


# ------------------------------------------------------------------ framing
class FramingTests(unittest.TestCase):
    def test_accept_matches_rfc_example(self):
        self.assertEqual(live_ws.compute_accept("dGhlIHNhbXBsZSBub25jZQ=="),
                         "s3pPLMBiTxaQ9kYGzzhZRbK+xOo=")

    def test_validate_key_rejects_bad_input(self):
        for value in (None, "", "short", "!!!!notbase64!!!!"):
            with self.assertRaises(live_ws.WSError):
                live_ws.validate_key(value)
        self.assertEqual(live_ws.validate_key("dGhlIHNhbXBsZSBub25jZQ=="),
                         "dGhlIHNhbXBsZSBub25jZQ==")

    def test_parse_http_request_normalizes_and_rejects_duplicates(self):
        method, target, version, headers = live_ws.parse_http_request(
            "get", "/ws?x=1", {"Upgrade": "websocket"})
        self.assertEqual((method, target, version), ("GET", "/ws?x=1", "HTTP/1.1"))
        self.assertEqual(headers["upgrade"], "websocket")
        with self.assertRaises(live_ws.WSError):
            live_ws.parse_http_request("GET", "ws", {})
        with self.assertRaises(live_ws.WSError):
            live_ws.parse_http_request("GET", "/ws", [])  # 非映射

    def test_encode_frame_lengths(self):
        for size, prefix in ((5, 1), (200, 3), (70000, 9)):
            frame = live_ws.encode_frame(live_ws.OPCODE_TEXT, b"x" * size)
            self.assertEqual(frame[0], 0x81)
            if size < 126:
                self.assertEqual(frame[1], size)
            elif size < 65536:
                self.assertEqual(frame[1], 126)
                self.assertEqual(struct.unpack("!H", frame[2:4])[0], size)
            else:
                self.assertEqual(frame[1], 127)
                self.assertEqual(struct.unpack("!Q", frame[2:10])[0], size)

    def test_read_frame_requires_masking(self):
        unmasked = bytes([0x81, 3]) + b"abc"

        class Stream:
            def __init__(self, data): self.data = data
            def read(self, n):
                out, self.data = self.data[:n], self.data[n:]
                return out

        with self.assertRaises(live_ws.WSError):
            live_ws.read_frame(Stream(unmasked), expect_mask=True)

    def test_client_queue_dedupes_seq_and_drops_on_overflow(self):
        queue = live_ws.ClientQueue(maxlen=4)
        self.assertTrue(queue.push({"seq": 1, "a": 1}))
        self.assertFalse(queue.push({"seq": 1, "a": 2}))
        for seq in range(2, 6):
            queue.push({"seq": seq})
        # 超过上限：清空并标记溢出，避免慢消费者把内存撑爆。
        self.assertTrue(queue.overflow)
        self.assertEqual(len(queue.drain()), 0)

    def test_origin_gate(self):
        lan = live_ws.ipaddress.ip_network("192.0.2.0/24")

        def allow(host):
            return host in ("127.0.0.1", "192.0.2.9")

        self.assertTrue(live_ws.origin_allowed(None, "127.0.0.1:1", "127.0.0.1", lan, allow))
        self.assertTrue(live_ws.origin_allowed("http://127.0.0.1:1", "127.0.0.1:1",
                                               "127.0.0.1", lan, allow))
        self.assertFalse(live_ws.origin_allowed("http://evil.example", "127.0.0.1:1",
                                                "127.0.0.1", lan, allow))
        self.assertFalse(live_ws.origin_allowed(None, "127.0.0.1:1", "10.9.9.9", lan, allow))


# ------------------------------------------------------- real loopback server
def load_server(web_dir: Path):
    """Import a fresh server module rooted at a temporary web directory."""
    saved = {key: value for key, value in sys.modules.items()
             if key == "backend" or key.startswith("backend.")}
    for key in saved:
        del sys.modules[key]
    spec = importlib.util.spec_from_file_location("fish_house_ws_server", ROOT / "server.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module, saved


class LoopbackWebSocketTests(unittest.TestCase):
    """Real HTTP upgrade on an ephemeral loopback port."""

    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix="fish-house-ws-test-")
        web = Path(cls.temp.name)
        house = web / "fish_house"
        theme_dir = house / "themes" / "default"
        theme_dir.mkdir(parents=True)
        (theme_dir / "theme.json").write_text(json.dumps(THEME), encoding="utf-8")
        (house / "videos" / "h264").mkdir(parents=True)
        (house / "videos" / "h264" / "clip.mp4").write_bytes(b"LEGACYCLIP")
        (theme_dir / "videos").mkdir(parents=True)
        (theme_dir / "videos" / "w1.mp4").write_bytes(b"0123456789")
        (house / "home.html").write_text("<html>home</html>", encoding="utf-8")
        (house / "legacy.json").write_text("{}", encoding="utf-8")

        cls.saved_backend = {key: value for key, value in sys.modules.items()
                             if key == "backend" or key.startswith("backend.")}
        for key in cls.saved_backend:
            del sys.modules[key]
        if str(ROOT) not in sys.path:
            sys.path.insert(0, str(ROOT))
        spec = importlib.util.spec_from_file_location("fish_house_ws_server", ROOT / "server.py")
        cls.server = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.server)
        # 让被服务目录指向临时夹具，避免污染真实 web 树。
        cls.server.WEB = web
        cls.server.HOUSE = house

        cls.state = {"wechat_activity": "idle", "panel": None}

        def getter():
            return {
                "ok": True, "timestamp": int(time.time()),
                "qq": {"service_running": True, "busy": True},
                "wechat": {"activity_state": cls.state["wechat_activity"],
                           "activity_at": cls.state["wechat_activity_at"],
                           "diagnostics": {"fresh": True, "reason": "generation_started"},
                           "last_activity": {"at": NOW, "last_reply_at": None}},
                "activity": {"phase": "idle"},
                "persona": {"character": "whale"},
            }

        cls.server.snapshot = type("S", (), {
            "get": staticmethod(getter),
            "panel_view": staticmethod(lambda: cls.state["panel"]),
        })()
        cls.server.bridge = live_ws.LiveBridge(
            router_factory=lambda: build_router(house),
            snapshot_getter=getter,
            panel_reader=lambda: cls.state["panel"])
        cls.server.bridge.start()

        cls.httpd = cls.server.ThreadingHTTPServer(("127.0.0.1", 0), cls.server.Handler)
        cls.httpd.daemon_threads = True
        cls.thread = threading.Thread(target=cls.httpd.serve_forever,
                                      kwargs={"poll_interval": 0.2}, daemon=True)
        cls.thread.start()
        cls.port = cls.httpd.server_port

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.server.bridge.stop()
        for key in [key for key in sys.modules
                    if key == "backend" or key.startswith("backend.")]:
            del sys.modules[key]
        sys.modules.update(cls.saved_backend)
        cls.temp.cleanup()

    # ------------------------------------------------------------- helpers
    def setUp(self):
        self.state.update(wechat_activity="idle", wechat_activity_at=time.time(), panel=None)

    def connect(self, path="/ws", origin=None, key="dGhlIHNhbXBsZSBub25jZQ==",
                headers=None):
        sock = socket.create_connection(("127.0.0.1", self.port), timeout=6)
        lines = ["GET %s HTTP/1.1" % path, "Host: 127.0.0.1:%d" % self.port,
                 "Upgrade: websocket", "Connection: Upgrade",
                 "Sec-WebSocket-Version: 13", "Sec-WebSocket-Key: " + key]
        if origin:
            lines.append("Origin: " + origin)
        for name, value in (headers or {}).items():
            lines.append("%s: %s" % (name, value))
        sock.sendall(("\r\n".join(lines) + "\r\n\r\n").encode("latin-1"))
        head = b""
        while b"\r\n\r\n" not in head:
            chunk = sock.recv(1)
            if not chunk:
                break
            head += chunk
        return sock, head.decode("latin-1", "replace")

    def reader(self, sock):
        class Reader:
            def read(self, count):
                buf = b""
                while len(buf) < count:
                    chunk = sock.recv(count - len(buf))
                    if not chunk:
                        break
                    buf += chunk
                return buf
        return Reader()

    def next_json(self, reader):
        frame = live_ws.read_frame(reader, expect_mask=False)
        self.assertIsNotNone(frame, "connection closed before a frame arrived")
        return json.loads(frame[1])

    def wait_for(self, reader, predicate, timeout=8.0):
        deadline = time.time() + timeout
        seen = []
        while time.time() < deadline:
            message = self.next_json(reader)
            seen.append(message)
            if predicate(message):
                return message, seen
        self.fail("predicate never satisfied; last frames=%s" % [
            (m.get("type"), m.get("phase")) for m in seen[-3:]])

    # --------------------------------------------------------------- tests
    def test_embedded_sse_reuses_live_frame_contract_and_releases_slot(self):
        self.state["wechat_activity"] = "thinking"
        url = "http://127.0.0.1:%d/api/house/live-events" % self.port
        with urllib.request.urlopen(url, timeout=5) as stream:
            self.assertEqual(stream.headers.get_content_type(), "text/event-stream")

            def next_data():
                for raw in stream:
                    if raw.startswith(b"data: "):
                        return json.loads(raw[6:])
                self.fail("event stream closed before a frame arrived")

            config, state = next_data(), next_data()
            self.assertEqual(config["type"], "config")
            self.assertEqual(config["theme_url"], DEFAULT_THEME_URL)
            self.assertEqual(state["type"], "state")
            self.assertEqual(state["phase"], "thinking")
            self.assertEqual(state["source"], "wechat")
            self.assertGreater(state["seq"], config["seq"])
            self.state["wechat_activity"] = "sent"
            event = next_data()
            while event.get("phase") != "sent":
                event = next_data()
            self.assertGreater(event["seq"], state["seq"])
            self.assertIn(event["type"], ("flow_goto", "state"))
        deadline = time.monotonic() + 3
        while self.server.bridge.client_count and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertEqual(self.server.bridge.client_count, 0)

    def test_embedded_sse_rejects_foreign_browser_origin(self):
        url = "http://127.0.0.1:%d/api/house/live-events" % self.port
        request = urllib.request.Request(url, headers={"Origin": "http://foreign.example"})
        with self.assertRaises(urllib.error.HTTPError) as seen:
            urllib.request.urlopen(request, timeout=4)
        self.assertEqual(seen.exception.code, 403)

    def test_first_connect_snapshot_then_reconnect_snapshot(self):
        self.state["wechat_activity"] = "thinking"
        sock, head = self.connect()
        try:
            self.assertIn("101 Switching Protocols", head)
            self.assertIn("Sec-WebSocket-Accept", head)
            reader = self.reader(sock)
            config, state = self.next_json(reader), self.next_json(reader)
            self.assertEqual(config["type"], "config")
            self.assertEqual(config["theme_url"], DEFAULT_THEME_URL)
            self.assertEqual(config["nodes"]["wait"], "w1")
            self.assertEqual(state["type"], "state")
            self.assertEqual(state["phase"], "thinking")
            self.assertEqual(state["source"], "wechat")
            self.assertEqual(state["node_id"], "ph1")
            self.assertGreater(state["seq"], config["seq"])
            for key in ("seq", "ts", "source", "phase", "node_id", "bubble_text", "ttl_ms"):
                self.assertIn(key, state)
            self.assertIsInstance(state["ts"], int)
            self.assertGreaterEqual(state["ttl_ms"], 0)
        finally:
            sock.close()
        # 重连同样先给快照，而不是等下一轮变更。
        sock2, head2 = self.connect()
        try:
            self.assertIn("101", head2)
            reader2 = self.reader(sock2)
            first, second = self.next_json(reader2), self.next_json(reader2)
            self.assertEqual([first["type"], second["type"]], ["config", "state"])
            self.assertEqual(second["phase"], "thinking")
        finally:
            sock2.close()

    def test_rejects_bad_origin_wrong_path_and_missing_upgrade(self):
        sock, head = self.connect(origin="http://evil.example.com")
        try:
            self.assertIn("403", head.splitlines()[0])
        finally:
            sock.close()

        sock, head = self.connect(path="/wsx")
        try:
            self.assertIn("404", head.splitlines()[0])
        finally:
            sock.close()

        sock, head = self.connect(headers={"Upgrade": ""})
        try:
            self.assertIn("426", head.splitlines()[0])
        finally:
            sock.close()

    def test_duplicate_and_ttl_bubbles_then_expire(self):
        self.state["wechat_activity"] = "received"
        sock, _ = self.connect()
        try:
            reader = self.reader(sock)
            self.next_json(reader)  # config
            state = self.next_json(reader)
            self.assertEqual(state["phase"], "received")
            self.assertEqual(state["bubble_text"], PHASE_BUBBLES["received"])
            self.assertEqual(state["ttl_ms"], live_ws.LIVE_TTL_MS)
            first_seq = state["seq"]

            expired, seen = self.wait_for(
                reader, lambda m: m["bubble_text"] == "" and
                m["type"] in ("state", "bubble"), timeout=8.0)
            self.assertEqual(expired["bubble_text"], "")
            self.assertEqual(expired["ttl_ms"], 0)
            # 清空只发一次，不能每 tick 重复。
            clears = [m for m in seen if m["bubble_text"] == ""]
            self.assertEqual(len(clears), 1)
            seqs = [m["seq"] for m in seen]
            self.assertEqual(len(seqs), len(set(seqs)), "seq must not repeat per client")
            self.assertTrue(all(b > a for a, b in zip([first_seq] + seqs, seqs)))
        finally:
            sock.close()

    def test_phase_transition_carries_theme_node_ids(self):
        self.state["wechat_activity"] = "thinking"
        sock, _ = self.connect()
        try:
            reader = self.reader(sock)
            self.next_json(reader)  # config
            # 先确保进入 thinking（手机循环），再切 sent 才是真正的「放下手机」。
            entered, _ = self.wait_for(reader, lambda m: m.get("phase") == "thinking")
            self.assertEqual(entered["node_id"], "ph1")
            self.state["wechat_activity"] = "sent"
            # flow_goto 先到、state 后到，所以按 type 分别取。
            sent_state, seen = self.wait_for(
                reader, lambda m: m.get("type") == "state" and m.get("phase") == "sent")
            self.assertEqual(sent_state["source"], "wechat")
            # state 的 node_id = 这一段落到哪儿（放下后回到等待），
            # 而换片的起点由 flow_goto 给出，两者语义不同。
            self.assertEqual(sent_state["node_id"], "w1")
            self.assertEqual([o["node_id"] for o in sent_state["orders"]], ["put", "w1"])
            # 换片用独立 flow_goto 承载：它指「这一段先播哪一片」，
            # 而 state.node_id 指「这一段落到哪个节点」。
            gotos = [m for m in seen if m["type"] == "flow_goto"]
            self.assertEqual(len(gotos), 1)
            self.assertEqual(gotos[0]["node_id"], "put")
            self.assertFalse(gotos[0]["loop"])
            self.assertEqual(gotos[0]["phase"], "sent")
            self.assertTrue(gotos[0]["video"])
        finally:
            sock.close()

    def test_qq_busy_never_fabricates_chat_phase(self):
        self.state["wechat_activity"] = "idle"
        self.state["panel"] = {"runtimeRecords": [
            {"state": "confirmed", "timestamp": int(time.time()) - 120, "_chat": True}]}
        sock, _ = self.connect()
        try:
            reader = self.reader(sock)
            self.next_json(reader)  # config
            message = self.next_json(reader)
            self.assertNotIn(message["phase"], ("received", "thinking", "sent"))
            self.assertEqual(message["phase"], "idle")
            self.assertEqual(message["evidence"], live_ws.EVIDENCE_DEGRADED)
            self.assertEqual(message["source"], "qq")
        finally:
            sock.close()

    def test_chat_receipt_reaches_state_frame_without_detail(self):
        self.state["wechat_activity"] = "idle"
        self.state["panel"] = {"runtimeRecords": [
            {"state": "confirmed", "timestamp": int(time.time()), "_chat": True,
             "detail": "OneBot 已接受发送 message_id=9"}]}
        sock, _ = self.connect()
        try:
            reader = self.reader(sock)
            self.next_json(reader)  # config
            message, _ = self.wait_for(reader, lambda m: m.get("phase") == "sent" and m["type"] == "state")
            self.assertEqual(message["source"], "qq")
            self.assertEqual(message["node_id"], "w1")
            self.assertEqual([order["node_id"] for order in message["orders"]],
                             ["pick", "ph1", "put", "w1"])
            blob = json.dumps(message, ensure_ascii=False)
            for needle in ("OneBot", "message_id", "detail", "_chat", "已接受发送"):
                self.assertNotIn(needle, blob)
        finally:
            sock.close()

    def test_tool_state_reaches_live_transports_through_qq_projection(self):
        stamp = time.time()
        self.state["panel"] = {"runtimeRecords": [
            {"state": "generating", "timestamp": stamp, "detail": live_ws.QQ_TOOL_DETAIL,
             "target": "private-fixture-room"},
        ]}
        poller = self.server.QQEvidencePoller(lambda: self.state["panel"], interval=0.05)
        poller.tick()
        base = "http://127.0.0.1:%d" % self.port

        def next_sse(stream):
            for line in stream:
                if line.startswith(b"data: "):
                    return json.loads(line[6:])
            self.fail("event stream closed before a state arrived")

        with patch.object(self.server, "qq_poller", poller), \
             patch.object(self.server.bridge, "_panel_reader", self.server.qq_panel_view):
            sock, head = self.connect()
            self.addCleanup(sock.close)
            self.assertIn("101", head)
            reader = self.reader(sock)
            self.assertEqual(self.next_json(reader)["type"], "config")
            state = self.next_json(reader)
            self.assertEqual((state["source"], state["phase"], state.get("event_at")),
                             ("qq", "tool", stamp))
            self.assertTrue(state["verified"])
            self.assertGreater(state["ttl_ms"], 250000)
            self.assertNotIn("private-fixture-room", json.dumps(state))
            self.assertNotIn("detail", state)

            for path in ("/api/house/live-events", "/api/house/events"):
                with urllib.request.urlopen(base + path, timeout=5) as stream:
                    event = next_sse(stream)
                    if event.get("type") == "config":
                        event = next_sse(stream)
                    self.assertEqual((event["source"], event["phase"]), ("qq", "tool"))
                    self.assertTrue(event["verified"])
            with urllib.request.urlopen(base + "/fish_house/state.json", timeout=5) as response:
                self.assertEqual(json.load(response)["phase"], "tool")
            with patch.object(self.server, "VIDEO", {}), \
                 urllib.request.urlopen(base + "/fish_house/state.json", timeout=5) as response:
                missing_video = json.load(response)
                self.assertEqual(missing_video["phase"], "tool")
                self.assertEqual(missing_video["node_video"], "")

            self.state["panel"] = {"runtimeRecords": [
                {"state": "generating", "timestamp": time.time(),
                 "detail": live_ws.QQ_TOOL_DETAIL + " extra text"},
            ]}
            poller.tick()
            idle, _ = self.wait_for(reader, lambda m: m["type"] == "state" and m["phase"] == "idle")
            self.assertFalse(idle["verified"])

    def test_frames_never_leak_private_material(self):
        self.state["wechat_activity"] = "thinking"
        self.state["panel"] = {"runtimeRecords": [
            {"state": "confirmed", "timestamp": int(time.time()),
             "title": "等待处理", "target": "群 · 秘密群", "detail": "消息正文 1234567890"}]}
        sock, _ = self.connect()
        try:
            reader = self.reader(sock)
            frames = [self.next_json(reader) for _ in range(2)]
            try:
                frames.append(self.next_json(reader))
            except Exception:
                pass
            blob = json.dumps(frames, ensure_ascii=False)
            for needle in ("秘密群", "消息正文", "1234567890", "trace", "credential",
                           "secret", "target"):
                self.assertNotIn(needle, blob)
            self.assertEqual(frames[-1]["source"], "wechat")
        finally:
            sock.close()

    def test_compat_events_and_polling_require_the_same_wallpaper_evidence(self):
        origin = "http://127.0.0.1:%d" % self.port
        self.state["wechat_activity"] = "idle"
        self.state["panel"] = {"runtimeRecords": [
            {"state": "generating", "timestamp": time.time(), "detail": "unrelated work"}]}
        with patch.object(self.server.snapshot, "get", return_value={
                "wechat": {"activity_state": "idle", "last_activity": {"at": int(time.time()) - 6}}}):
            # A recent raw ingress is not proof that generation actually started.
            with urllib.request.urlopen(origin + "/fish_house/state.json", timeout=5) as response:
                self.assertEqual(json.load(response)["phase"], "idle")
        with urllib.request.urlopen(origin + "/api/house/events", timeout=5) as stream:
            self.assertTrue(stream.readline().startswith(b"data: "))
            self.assertEqual(stream.readline(), b"\n")
            stamp = time.time()
            self.state["panel"] = {"runtimeRecords": [
                {"state": "generating", "timestamp": stamp, "detail": "QQ聊天模型请求中"}]}
            for _ in range(8):
                line = stream.readline()
                if line.startswith(b"data: "):
                    received = json.loads(line[6:])
                    if received["phase"] == "thinking":
                        break
            else:
                self.fail("Verified QQ generation did not reach the compatibility stream")
            self.assertEqual(received["source"], "qq")
            self.assertEqual(received["at"], stamp)
            self.assertNotIn("detail", received)
            with urllib.request.urlopen(origin + "/fish_house/state.json", timeout=5) as response:
                polled = json.load(response)
            self.assertEqual(polled["id"], received["id"])
            self.assertGreater(polled["seq"], received["seq"])

    def test_polling_snapshot_does_not_consume_a_wallpaper_change(self):
        router = FlowRouter(ThemeFlow(THEME))
        bridge = live_ws.LiveBridge(router_factory=lambda: router, snapshot_getter=lambda: {
            "wechat": {"activity_state": "received", "activity_at": time.time() - 0.1},
        })
        self.assertEqual(bridge.state_snapshot()["phase"], "received")
        self.assertEqual([f["type"] for f in bridge.produce()], ["flow_goto", "state"])

    def test_sequence_keeps_browser_events_fresh_after_service_restart(self):
        before = live_ws.LiveBridge(router_factory=lambda: None, snapshot_getter=dict)
        with patch.object(live_ws.time, "time_ns", return_value=NOW * 10**9):
            previous = before.next_seq()
            self.assertEqual(before.next_seq(), previous + 1)
        after = live_ws.LiveBridge(router_factory=lambda: None, snapshot_getter=dict)
        with patch.object(live_ws.time, "time_ns", return_value=(NOW + 1) * 10**9):
            self.assertGreater(after.next_seq(), previous + 1)

    def test_original_routes_still_work(self):
        import urllib.request

        def fetch(path):
            with urllib.request.urlopen(
                    "http://127.0.0.1:%d%s" % (self.port, path), timeout=5) as response:
                return response.status, response.read()

        status, body = fetch("/fish_house/themes/default/theme.json")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["meta"]["id"], "test")
        # 原字节直出：与磁盘内容逐字节一致，没有二次序列化。
        on_disk = (Path(self.temp.name) / "fish_house" / "themes" / "default"
                   / "theme.json").read_bytes()
        self.assertEqual(body, on_disk)

        # 旧 /videos/h264/* 保持原样：SimpleHTTPRequestHandler 的普通 200。
        status, body = fetch("/fish_house/videos/h264/clip.mp4")
        self.assertEqual(status, 200)
        self.assertEqual(body, b"LEGACYCLIP")

        # 只有主题包内 videos/ 走新的单段 Range。
        request = urllib.request.Request(
            "http://127.0.0.1:%d/fish_house/themes/default/videos/w1.mp4" % self.port,
            headers={"Range": "bytes=2-5"})
        with urllib.request.urlopen(request, timeout=5) as response:
            self.assertEqual(response.status, 206)
            self.assertEqual(response.read(), b"2345")
            self.assertEqual(response.headers.get("Content-Range"), "bytes 2-5/10")

        # 路径穿越与非法段名都必须拒绝。
        for bad in ("/fish_house/themes/default/videos/..%2F..%2Ftheme.json",
                    "/fish_house/themes/default/videos/evil.sh",
                    "/fish_house/themes/../videos/w1.mp4"):
            try:
                fetch(bad)
            except urllib.error.HTTPError as error:
                self.assertIn(error.code, (403, 404))
            else:
                self.fail("bad video path served: %s" % bad)

        # 原路径：/healthz、/api/status、/api/house/events 仍存在。
        status, body = fetch("/healthz")
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)["ok"])

        status, body = fetch("/api/status")
        self.assertEqual(status, 200)
        self.assertIn("wechat", json.loads(body))

        # /live 带 query 仍能 302 到 live 入口（保留 bubble=0 之类参数）。
        request = urllib.request.Request(
            "http://127.0.0.1:%d/live?bubble=0" % self.port)
        opener = urllib.request.build_opener(NoRedirect)
        try:
            opener.open(request, timeout=5)
        except urllib.error.HTTPError as error:
            self.assertEqual(error.code, 302)
            self.assertTrue(error.headers.get("Location").endswith("bubble=0"))
        else:
            self.fail("/live must redirect")

    def test_frame_pressure_keeps_one_frame_per_client_state(self):
        self.state["wechat_activity"] = "replying"
        sock, _ = self.connect()
        try:
            reader = self.reader(sock)
            self.next_json(reader)  # config
            baseline = self.next_json(reader)
            while baseline.get("type") != "state":
                baseline = self.next_json(reader)
            self.assertEqual(baseline["phase"], "replying")  # baseline established
            # 客户端狂发文本帧，服务端必须只解析不执行、不放大回包。
            payload = json.dumps({"type": "goto", "node_id": "wk1"}).encode()
            frame = live_ws.encode_frame(live_ws.OPCODE_TEXT, payload)
            masked = self.mask_frame(frame)
            for _ in range(200):
                sock.sendall(masked)
            time.sleep(2.0)
            messages = []
            try:
                while True:
                    messages.append(self.next_json(reader))
                    if len(messages) > 40:
                        break
            except Exception:
                pass
            self.assertTrue(messages)
            # 允许气泡到期这类同阶段内的合法帧，但状态绝不能被客户端改写。
            phases = {m["phase"] for m in messages}
            self.assertEqual(phases, {"replying"}, "client frames must not steer the house")
            seqs = [m["seq"] for m in messages]
            self.assertEqual(len(seqs), len(set(seqs)))
            # 基线之后同一阶段不得再推 state（否则前端动画会被打断）。
            states = [m for m in messages if m["type"] == "state"]
            self.assertEqual(states, [], states)
        finally:
            sock.close()

    @staticmethod
    def mask_frame(frame: bytes) -> bytes:
        """Client frames must be masked; wrap a server frame for sending."""
        head, payload = frame[0], frame[2:]
        mask = b"\x01\x02\x03\x04"
        masked = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
        length = len(payload)
        if length < 126:
            header = bytes([head, 0x80 | length])
        else:
            header = bytes([head, 0x80 | 126]) + struct.pack("!H", length)
        return header + mask + masked


class ReviewRegressionTests(unittest.TestCase):
    """One short regression per review finding."""

    # ---- 1. QQ read path: poller wiring, newest-first, old API untouched ----
    def test_poller_reader_receives_exactly_one_argument(self):
        """回归：旧代码 _bounded_read(reader, qa, timeout) 三参调二参，
        异常被吞导致 QQ 永远没数据。现在读取器是零参闭包，单参调用。"""
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "fh_server_proj0", ROOT / "server.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

        seen = {}

        class FakeCtl:
            def state(self):
                seen["called"] = True
                return {"runtimeRecords": [
                    {"state": "confirmed", "timestamp": 300.0,
                     "detail": "OneBot 已接受发送"},
                    {"state": "generating", "timestamp": 200.0, "detail": "x"},
                ], "queued": 1}

        class FakeAdapter:
            def _controller_instance(self):
                return FakeCtl()

        panel = mod._controller_panel(FakeAdapter())
        self.assertTrue(seen.get("called"))
        self.assertEqual(panel["runtimeRecords"][0]["timestamp"], 300.0)

        # 轮询器本身：reader 无参可调用，tick 把结果缓存进 snapshot。
        poller = mod.QQEvidencePoller(lambda: {"runtimeRecords": [], "queued": 0},
                                      interval=0.01, timeout=1.0)
        poller.tick()
        self.assertEqual(poller.snapshot(), {"runtimeRecords": [], "queued": 0})

    def test_poller_reads_immediately_when_viewer_starts(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "fh_server_immediate_poll", ROOT / "server.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

        called = threading.Event()
        def read_panel():
            called.set()
            return {"runtimeRecords": [], "queued": 0}

        poller = mod.QQEvidencePoller(read_panel, interval=10.0, timeout=1.0)
        poller.start()
        try:
            self.assertTrue(called.wait(0.5), "first QQ snapshot waited for poll interval")
            deadline = time.monotonic() + 0.5
            while poller.snapshot() is None and time.monotonic() < deadline:
                time.sleep(0.001)
            self.assertEqual(poller.snapshot(), {"runtimeRecords": [], "queued": 0})
        finally:
            poller.stop()

    def test_poller_speeds_up_only_for_verified_recent_chat(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "fh_server_active_poll", ROOT / "server.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

        poller = mod.QQEvidencePoller(lambda: None, interval=2.0)
        now = 100.0
        cases = [
            ("queued", live_ws.QQ_NATIVE_CHAT_INGRESS_DETAIL, now - 1, 0.5),
            ("generating", live_ws.QQ_ACTIVE_CHAT["generating"][0], now - 1, 0.5),
            ("sending", live_ws.QQ_ACTIVE_CHAT["sending"][0], now - 1, 0.5),
            ("confirmed", live_ws.QQ_CHAT_CONFIRM_PREFIX, now - 1, 2.0),
            ("generating", live_ws.QQ_ACTIVE_CHAT["generating"][0], now - 21, 2.0),
            ("generating", "unverified", now - 1, 2.0),
        ]
        for state, detail, timestamp, interval in cases:
            with self.subTest(state=state, detail=detail, timestamp=timestamp):
                panel = {"runtimeRecords": [{"state": state, "detail": detail,
                                             "timestamp": timestamp}]}
                self.assertEqual(poller._next_interval(panel, now=now), interval)
        self.assertEqual(poller._next_interval(None, now=now), 2.0)

    def test_concurrent_viewers_share_one_poller_until_last_disconnect(self):
        from concurrent.futures import ThreadPoolExecutor
        import importlib.util
        from unittest.mock import Mock
        spec = importlib.util.spec_from_file_location(
            "fh_server_parallel_viewers", ROOT / "server.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        mod.qq_poller = Mock()
        barrier = threading.Barrier(2)

        def connect():
            barrier.wait(timeout=2)
            return mod.register_live_viewer()

        with ThreadPoolExecutor(max_workers=2) as pool:
            pending = [pool.submit(connect) for _ in range(2)]
            clients = [future.result(timeout=3) for future in pending]
        self.assertEqual(mod.bridge.client_count, 2)
        mod.qq_poller.start.assert_called_once()
        mod.unregister_live_viewer(clients[0][0])
        mod.qq_poller.stop.assert_not_called()
        mod.unregister_live_viewer(clients[1][0])
        mod.qq_poller.stop.assert_called_once()

    def test_health_file_cache_reads_atomic_replacements(self):
        import importlib.util
        import json
        import os
        import tempfile
        spec = importlib.util.spec_from_file_location(
            "fh_server_health_cache", ROOT / "server.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bridge-health.json"
            first = {"last_event": {"time": 1, "phase": "receive", "reason": "mention"}}
            path.write_text(json.dumps(first), encoding="utf-8")
            cache = mod.BridgeHealthFileCache(path)
            self.assertEqual(cache.read(), first)
            self.assertEqual(cache.read(), first)

            replacement = path.with_suffix(".tmp")
            second = {"last_event": {"time": 2, "phase": "generation", "reason": "started"}}
            replacement.write_text(json.dumps(second), encoding="utf-8")
            os.replace(replacement, path)
            self.assertEqual(cache.read(), second)

    def test_live_snapshot_uses_health_file_before_http(self):
        import importlib.util
        import json
        import tempfile
        spec = importlib.util.spec_from_file_location(
            "fh_server_health_first", ROOT / "server.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bridge-health.json"
            path.write_text(json.dumps({"last_event": {
                "time": time.time(), "phase": "receive", "reason": "mention"}}),
                encoding="utf-8")
            mod.bridge_health_file = mod.BridgeHealthFileCache(path)
            mod.core_status = lambda: self.fail("HTTP fallback should not run")
            live = mod.live_snapshot()
            # A receive record without the approved queue marker is not shown.
            self.assertEqual(live["wechat"]["activity_state"], "idle")

    def test_live_snapshot_surfaces_one_fresh_same_trace_ingress_before_generation(self):
        import hashlib
        import importlib.util
        import json
        import tempfile

        spec = importlib.util.spec_from_file_location(
            "fh_server_ingress_marker", ROOT / "server.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

        with tempfile.TemporaryDirectory() as directory:
            now = time.time()
            trace = "a" * 64
            marker = {"time": now,
                      "fingerprint": hashlib.sha256(trace.encode("ascii")).hexdigest()}
            path = Path(directory) / "bridge-health.json"
            path.write_text(json.dumps({
                "last_event": {"time": int(now), "phase": "generation",
                               "reason": "started", "trace": trace},
                "last_ingress": marker,
            }), encoding="utf-8")
            mod.bridge_health_file = mod.BridgeHealthFileCache(path)
            mod.core_status = lambda: self.fail("HTTP fallback should not run")

            first = mod.live_snapshot()
            second = mod.live_snapshot()
            self.assertEqual(first["wechat"]["activity_state"], "received")
            self.assertAlmostEqual(first["wechat"]["activity_at"], now, delta=0.1)
            self.assertEqual(second["wechat"]["activity_state"], "received")
            self.assertNotIn(trace, json.dumps(first))
            self.assertNotIn(marker["fingerprint"], json.dumps(first))

            # A forced snapshot for a newly connected viewer sees the same
            # fresh state even if the existing viewer already read this tick.
            bridge = mod.live_ws.LiveBridge(
                router_factory=lambda: None, snapshot_getter=mod.live_snapshot,
                panel_reader=lambda: None, tick=0.25)
            tick_one = bridge.produce()
            tick_two = bridge.produce()
            self.assertTrue(any(frame.get("phase") == "received"
                                for frame in tick_one))
            self.assertFalse(any(frame.get("phase") == "received"
                                 for frame in tick_two))

            reconnect_bridge = mod.live_ws.LiveBridge(
                router_factory=lambda: None, snapshot_getter=mod.live_snapshot,
                panel_reader=lambda: None, tick=0.25)
            reconnect = reconnect_bridge.snapshot_frames()
            self.assertEqual(reconnect[-1]["phase"], "received")

            # Once the pin window expires, a repeated snapshot returns the real
            # generation phase; the normal bridge phase/time dedupe handles it.
            from unittest.mock import patch
            with patch.object(mod.time, "time", return_value=now + 1.0):
                after_pin = mod.live_snapshot()
            self.assertEqual(after_pin["wechat"]["activity_state"], "thinking")

    def test_live_snapshot_does_not_replay_stale_mismatched_or_failed_ingress(self):
        import hashlib
        import importlib.util
        import json
        import tempfile

        cases = (
            ("stale", 2.1, "b" * 64, "generation", "started", False, "thinking"),
            ("pin_expired", 0.91, "a" * 64, "generation", "started", False, "thinking"),
            ("mismatch", 0.1, "b" * 64, "generation", "started", False, "thinking"),
            ("after_restart", 0.1, "a" * 64, "generation", "started", False, "thinking"),
            ("failed", 0.1, "a" * 64, "generation", "started", True, "error"),
            ("other_phase", 0.1, "a" * 64, "confirmation", "send_exception", False, "error"),
            ("direction_unverified", 0.1, "a" * 64, "mention", "direction_unverified", True, "idle"),
            ("raw_receive", 0.1, "a" * 64, "receive", "group", False, "idle"),
        )
        for name, age, trace, phase, reason, failed, expected in cases:
            with self.subTest(case=name), tempfile.TemporaryDirectory() as directory:
                spec = importlib.util.spec_from_file_location(
                    "fh_server_ingress_" + name, ROOT / "server.py")
                mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)
                now = time.time()
                if name == "after_restart":
                    mod.LIVE_STARTED_AT = now - age / 2
                event = {"time": int(now), "phase": phase, "reason": reason,
                         "trace": trace}
                if failed:
                    event["failed"] = True
                marker_trace = "a" * 64
                path = Path(directory) / "bridge-health.json"
                path.write_text(json.dumps({
                    "last_event": event,
                    "last_failure": dict(event) if failed or phase == "confirmation" else {},
                    "last_ingress": {
                        "time": now - age,
                        "fingerprint": hashlib.sha256(marker_trace.encode("ascii")).hexdigest(),
                    },
                }), encoding="utf-8")
                mod.bridge_health_file = mod.BridgeHealthFileCache(path)
                mod.core_status = lambda: self.fail("HTTP fallback should not run")
                self.assertEqual(mod.live_snapshot()["wechat"]["activity_state"], expected)

        # A terminal receipt already recorded for this same trace makes a later
        # submit marker non-unique, even when the receipt timestamp is older.
        with tempfile.TemporaryDirectory() as directory:
            spec = importlib.util.spec_from_file_location(
                "fh_server_submit_same_trace_terminal", ROOT / "server.py")
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            now = int(time.time())
            trace = "f" * 64
            path = Path(directory) / "bridge-health.json"
            path.write_text(json.dumps({
                "last_event": {"time": now - 23, "phase": "submit",
                               "reason": "started", "trace": trace},
                "last_success": {"time": now - 24, "phase": "confirmation",
                                 "reason": "visible_outgoing", "trace": trace},
            }), encoding="utf-8")
            mod.bridge_health_file = mod.BridgeHealthFileCache(path)
            mod.core_status = lambda: self.fail("HTTP fallback should not run")
            self.assertEqual(mod.live_snapshot()["wechat"]["activity_state"], "idle")

    def test_fresh_ingress_pin_does_not_hide_visible_send_terminal(self):
        import hashlib
        import importlib.util
        import json
        import tempfile

        spec = importlib.util.spec_from_file_location(
            "fh_server_ingress_terminal", ROOT / "server.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

        with tempfile.TemporaryDirectory() as directory:
            now = int(time.time())
            trace = "c" * 64
            marker = {"time": float(now),
                      "fingerprint": hashlib.sha256(trace.encode("ascii")).hexdigest()}
            path = Path(directory) / "bridge-health.json"
            path.write_text(json.dumps({
                "last_event": {"time": now - 1, "phase": "generation",
                               "reason": "started", "trace": trace},
                "last_success": {"time": now, "phase": "confirmation",
                                 "reason": "visible_outgoing", "trace": trace},
                "last_ingress": marker,
            }), encoding="utf-8")
            mod.LIVE_STARTED_AT = now - 1
            mod.bridge_health_file = mod.BridgeHealthFileCache(path)
            mod.core_status = lambda: self.fail("HTTP fallback should not run")
            self.assertEqual(mod.live_snapshot()["wechat"]["activity_state"], "sent")

    def test_only_unique_submit_pending_gets_bounded_35_second_projection(self):
        import importlib.util
        import json
        import tempfile

        cases = (
            ("recheck_fresh", 2, "submit", "auto_reply_recheck_ok", True, None, "replying"),
            ("recheck_expired", 23, "submit", "auto_reply_recheck_ok", True, None, "idle"),
            ("cancelled_before_submit", 2, "submit", "auto_policy_disabled", True, None, "idle"),
            ("pending_23s", 23, "submit", "started", True, None, "replying"),
            ("past_grace", 35.1, "submit", "started", True, None, "idle"),
            ("future", -1, "submit", "started", True, None, "idle"),
            ("no_trace", 23, "submit", "started", False, None, "idle"),
            ("other_phase", 23, "generation", "started", True, None, "idle"),
            ("failed_priority", 23, "submit", "started", True, "last_failure", "error"),
            ("unknown_priority", 23, "submit", "started", True, "last_unknown", "error"),
            ("sent_priority", 23, "submit", "started", True, "last_success", "sent"),
        )
        for name, age, phase, reason, include_trace, terminal, expected in cases:
            with self.subTest(case=name), tempfile.TemporaryDirectory() as directory:
                spec = importlib.util.spec_from_file_location(
                    "fh_server_submit_grace_" + name, ROOT / "server.py")
                mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)
                now = int(time.time())
                event = {"time": now - age, "phase": phase, "reason": reason}
                if include_trace:
                    event["trace"] = "d" * 64
                snapshot = {"last_event": event}
                if terminal:
                    marker_reason = {
                        "last_failure": "send_exception",
                        "last_unknown": "visible_outgoing",
                        "last_success": "visible_outgoing",
                    }[terminal]
                    snapshot[terminal] = {
                        "time": now - 1, "phase": "confirmation",
                        "reason": marker_reason, "trace": "d" * 64,
                    }
                path = Path(directory) / "bridge-health.json"
                path.write_text(json.dumps(snapshot), encoding="utf-8")
                mod.bridge_health_file = mod.BridgeHealthFileCache(path)
                mod.core_status = lambda: self.fail("HTTP fallback should not run")
                self.assertEqual(mod.live_snapshot()["wechat"]["activity_state"], expected)

    def test_wechat_long_generation_requires_current_worker_and_ends_with_it(self):
        spec = importlib.util.spec_from_file_location(
            "fh_server_live_generation", ROOT / "server.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        now = float(NOW)
        event = {"time": now, "phase": "generation", "reason": "started", "trace": "a" * 64}
        diagnostics = {"last_event": event, "generation_tracking": True, "active_generation": {
            "time": now, "pid": 123, "thread": 234, "start_ticks": "678", "trace": event["trace"],
        }}
        bridge = mod.live_ws.LiveBridge(router_factory=lambda: None, snapshot_getter=mod.live_snapshot)
        with patch.object(mod.bridge_health_file, "read", return_value=diagnostics), \
             patch.object(mod, "wechat_generation_owner_live", return_value=True, create=True) as owner, \
             patch.object(mod.time, "time", return_value=now + 38) as clock:
            state = bridge.produce()[-1]
            self.assertEqual((state["source"], state["phase"], state.get("event_at")),
                             ("wechat", "thinking", now))
            self.assertEqual(state["ttl_ms"], 202000)
            self.assertEqual(mod.house_event(state)["expires_at"], now + 240)
            self.assertNotIn("trace", state)
            self.assertEqual(bridge.produce(), [])
            owner.return_value = False
            self.assertEqual(bridge.produce()[-1]["phase"], "idle")
            clock.return_value = now + 3
            self.assertEqual(bridge.state_snapshot()["phase"], "idle")
            owner.return_value = True
            clock.return_value = now + 241
            self.assertEqual(bridge.state_snapshot()["phase"], "idle")
            clock.return_value = now + 39
            diagnostics["last_event"] = {"time": now + 39, "phase": "confirmation",
                                         "reason": "visible_outgoing", "trace": event["trace"],
                                         "success": True}
            self.assertEqual(bridge.produce()[-1]["phase"], "sent")
            clock.return_value = now + 60
            self.assertEqual(bridge.produce()[-1]["phase"], "idle")

    def test_extended_submit_phase_is_not_rebroadcast_and_reconnect_is_fresh(self):
        import importlib.util
        import json
        import tempfile

        spec = importlib.util.spec_from_file_location(
            "fh_server_submit_dedupe", ROOT / "server.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

        with tempfile.TemporaryDirectory() as directory:
            now = int(time.time())
            submit_at = now - 23
            trace = "e" * 64
            path = Path(directory) / "bridge-health.json"
            path.write_text(json.dumps({
                "last_event": {"time": submit_at, "phase": "submit",
                               "reason": "started", "trace": trace},
            }), encoding="utf-8")
            mod.bridge_health_file = mod.BridgeHealthFileCache(path)
            mod.core_status = lambda: self.fail("HTTP fallback should not run")
            bridge = mod.live_ws.LiveBridge(
                router_factory=lambda: None, snapshot_getter=mod.live_snapshot,
                panel_reader=lambda: None, tick=0.25)

            first = bridge.produce()
            repeated = bridge.produce()
            self.assertTrue(any(frame.get("phase") == "replying" for frame in first))
            self.assertFalse(any(frame.get("phase") == "replying" for frame in repeated))

            reconnect = mod.live_ws.LiveBridge(
                router_factory=lambda: None, snapshot_getter=mod.live_snapshot,
                panel_reader=lambda: None, tick=0.25).snapshot_frames()
            self.assertEqual(reconnect[-1]["phase"], "replying")
            self.assertGreater(reconnect[-1]["ttl_ms"], 10000)
            self.assertLessEqual(reconnect[-1]["ttl_ms"], 12000)

            # A visible receipt advances the same bridge lifecycle directly to
            # sent. It must not fall through an idle gap and replay received.
            replacement = path.with_suffix(".tmp")
            replacement.write_text(json.dumps({
                "last_event": {"time": submit_at, "phase": "submit",
                               "reason": "started", "trace": trace},
                "last_success": {"time": now, "phase": "confirmation",
                                 "reason": "visible_outgoing", "trace": trace},
            }), encoding="utf-8")
            replacement.replace(path)
            sent = bridge.produce()
            self.assertTrue(any(frame.get("phase") == "sent" for frame in sent))
            self.assertFalse(any(frame.get("phase") == "received" for frame in sent))

    def test_health_file_fallback_keeps_http_poll_at_one_hz(self):
        import importlib.util
        import tempfile
        spec = importlib.util.spec_from_file_location(
            "fh_server_health_fallback", ROOT / "server.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

        with tempfile.TemporaryDirectory() as directory:
            mod.bridge_health_file = mod.BridgeHealthFileCache(Path(directory) / "missing.json")
            calls = []
            mod.core_status = lambda: calls.append(time.monotonic()) or {"bridgeDiagnostics": {}}
            mod.live_snapshot()
            mod.live_snapshot()
            self.assertEqual(len(calls), 1)
            self.assertEqual(mod.bridge._tick, 0.25)

    def test_qq_projection_keeps_newest_and_drops_detail(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "fh_server_proj", ROOT / "server.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

        records = [{"state": "confirmed", "timestamp": float(300 - i),
                    "detail": "OneBot 已接受发送 %d" % i} for i in range(10)]
        panel = mod.qq_panel_evidence({"runtimeRecords": records, "queued": 3})
        kept = panel["runtime_records"]
        # newest-first 保留头部 6 条，而不是尾部。
        self.assertEqual(kept[0]["timestamp"], 300.0)
        self.assertEqual(len(kept), 6)
        self.assertEqual(set(kept[0]), {"state", "timestamp", "chat", "ingress", "active", "tool", "fallback_notice"})
        self.assertTrue(kept[0]["chat"])
        self.assertFalse(kept[0]["ingress"])
        blob = json.dumps(panel, ensure_ascii=False)
        self.assertNotIn("OneBot", blob)
        self.assertNotIn("detail", blob)

    def test_qq_projection_keeps_only_fixed_ingress_boolean(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("fh_server_ingress_projection", ROOT / "server.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        panel = mod.qq_panel_evidence({"runtimeRecords": [
            {"state": "queued", "timestamp": 300.0,
             "detail": live_ws.QQ_NATIVE_CHAT_INGRESS_DETAIL},
            {"state": "queued", "timestamp": 299.0, "detail": "真实消息正文不应外传"},
        ]})
        self.assertTrue(panel["runtime_records"][0]["ingress"])
        self.assertFalse(panel["runtime_records"][1]["ingress"])
        self.assertEqual(set(panel["runtime_records"][0]),
                         {"state", "timestamp", "chat", "ingress", "active", "tool", "fallback_notice"})
        self.assertNotIn("真实消息正文", json.dumps(panel, ensure_ascii=False))

    def test_non_chat_confirm_is_not_a_receipt(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "fh_server_proj2", ROOT / "server.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        panel = mod.qq_panel_evidence({"runtimeRecords": [
            {"state": "confirmed", "timestamp": 1.0, "detail": "插画已生成"}]})
        self.assertFalse(panel["runtime_records"][0]["chat"])
        self.assertFalse(panel["runtime_records"][0]["ingress"])

    def test_stale_controller_snapshot_cannot_keep_pending_animation(self):
        spec = importlib.util.spec_from_file_location("fh_server_active_stage", ROOT / "server.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        record = {"state": "generating", "timestamp": NOW - 10,
                  "detail": "QQ聊天模型请求中", "target": "fixture group"}
        mod.qq_poller = mod.QQEvidencePoller(lambda: {"runtimeRecords": [record]})
        mod.qq_poller.tick()
        view = mod.qq_panel_view()
        self.assertEqual(view["runtimeRecords"][0],
                         {"state": "generating", "timestamp": NOW - 10,
                          "_chat": False, "_ingress": False, "_active": True, "_tool": False,
                          "_fallback_notice": False})
        self.assertEqual(live_ws.qq_evidence({}, view, now=NOW).phase, "thinking")
        with patch.object(mod.time, "monotonic", return_value=mod.qq_poller._read_at + 6):
            self.assertIsNone(mod.qq_panel_view())

    def test_projected_receipts_reach_the_bridge_and_each_play_once(self):
        spec = importlib.util.spec_from_file_location("fh_server_projection", ROOT / "server.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        record = {"state": "confirmed", "timestamp": time.time() - 2,
                  "detail": "OneBot 已接受发送（测试）"}
        warning = {"state": "failed", "timestamp": record["timestamp"] + 0.5,
                   "detail": live_ws.QQ_FALLBACK_NOTICE}
        poller = mod.QQEvidencePoller(lambda: {"runtimeRecords": [warning, record]},
                                     interval=0.01, timeout=1.0)
        mod.qq_poller = poller
        poller.tick()
        projection = mod.qq_panel_view()
        self.assertTrue(projection["runtimeRecords"][0]["_fallback_notice"])
        self.assertFalse(projection["runtimeRecords"][0]["_chat"])
        self.assertNotIn(live_ws.QQ_FALLBACK_NOTICE, json.dumps(projection, ensure_ascii=False))
        bridge = live_ws.LiveBridge(lambda: FlowRouter(ThemeFlow(THEME)), lambda: {}, mod.qq_panel_view)
        for _ in range(2):
            frames = bridge.produce()
            self.assertEqual([m["type"] for m in frames], ["flow_goto", "state"])
            self.assertEqual(frames[0]["orders"], frames[1]["orders"])
            self.assertEqual(frames[0]["event_at"], record["timestamp"])
            self.assertEqual(frames[0]["event_at"], frames[1]["event_at"])
            self.assertEqual([o["node_id"] for o in frames[1]["orders"]], ["pick", "ph1", "put", "w1"])
            self.assertEqual(bridge.produce(), [])
            record["timestamp"] += 0.25
            poller.tick()

    def test_status_payload_is_unchanged_by_qq_evidence(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "fh_server_proj3", ROOT / "server.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        snap = mod.BotSnapshot()
        self.assertFalse(hasattr(snap, "panel_view"))
        # 旧 read() 不再引用 controller 读取路径。
        src = (ROOT / "server.py").read_text(encoding="utf-8")
        bot_snapshot = src.split("class BotSnapshot:", 1)[1]
        read_body = bot_snapshot.split("def read(self):", 1)[1].split("class PersonaSnapshot", 1)[0]
        self.assertNotIn("_controller_panel", read_body)
        self.assertNotIn("runtimeRecords", read_body)

    def test_busy_status_keeps_qq_connection_but_does_not_verify_chat_activity(self):
        spec = importlib.util.spec_from_file_location("fh_busy_status_fixture", ROOT / "server.py")
        mod = importlib.util.module_from_spec(spec)
        adapter = SimpleNamespace(
            status=lambda: ({"connected": True, "running": True}, None),
            _panel=lambda: {"busy": True},
            _cache_clear=lambda: None,
        )
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp)
            (src / "adapters.py").touch()
            (src / "config.json").write_text("{}", encoding="utf-8")
            with patch.dict(os.environ, {"FISH_HOUSE_QQ_SRC": tmp}):
                spec.loader.exec_module(mod)
                with patch.dict(sys.modules, {"adapters": SimpleNamespace(QQAdapter=lambda _: adapter)}), \
                     patch.object(sys, "path", list(sys.path)), \
                     patch.object(mod, "services_active", return_value={}), \
                     patch.object(mod, "core_status", return_value=None), \
                     patch.object(mod.persona_snapshot, "get", return_value={}):
                    data = mod.BotSnapshot().get()
        self.assertTrue(data["qq"]["connected"])
        self.assertTrue(data["qq"]["running"])
        self.assertTrue(data["qq"]["busy"])
        self.assertEqual(data["activity"], {"phase": "idle", "source": None, "verified": False})

    # ------------------------------------- 2. client keys never collide ----
    def test_client_keys_are_unique_for_concurrent_clients(self):
        bridge = live_ws.LiveBridge(router_factory=lambda: None,
                                    snapshot_getter=lambda: {})
        ids = []
        for _ in range(live_ws.MAX_CLIENTS):
            registered = bridge.register()
            self.assertIsNotNone(registered)
            ids.append(registered[0])
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(ids, sorted(ids))
        for key in ids:
            bridge.unregister(key)
        self.assertEqual(bridge.client_count, 0)

    def test_bridge_capacity_is_bounded(self):
        bridge = live_ws.LiveBridge(router_factory=lambda: None,
                                    snapshot_getter=lambda: {})
        for _ in range(live_ws.MAX_CLIENTS):
            self.assertIsNotNone(bridge.register())
        self.assertIsNone(bridge.register())

    # ------------------ 2/3. dedupe: no repeated state, one goto per change --
    def _bridge(self, phase_box, theme=None):
        from backend.flow_bridge import FlowRouter, ThemeFlow
        router = FlowRouter(ThemeFlow(theme or THEME))

        def getter():
            return {"ok": True, "qq": {"service_running": True},
                    "wechat": {"activity_state": phase_box["phase"],
                               "diagnostics": {"fresh": True, "reason": "x"}}}

        return live_ws.LiveBridge(router_factory=lambda: router,
                                  snapshot_getter=getter,
                                  panel_reader=lambda: None), router

    def test_same_phase_emits_nothing_on_repeat_polls(self):
        box = {"phase": "thinking"}
        bridge, _ = self._bridge(box)
        first = bridge.produce()
        self.assertTrue(first)
        self.assertEqual([f["type"] for f in first], ["flow_goto", "state"])
        # 同一个事件时间戳/阶段：后续 poll 一律不重复推。
        self.assertEqual(bridge.produce(), [])
        self.assertEqual(bridge.produce(), [])

    def test_received_then_quick_sent_pushes_each_once(self):
        box = {"phase": "idle"}
        bridge, router = self._bridge(box)
        bridge.produce()  # baseline
        box["phase"] = "received"
        received = bridge.produce()
        gotos = [f for f in received if f["type"] == "flow_goto"]
        states = [f for f in received if f["type"] == "state"]
        self.assertEqual(len(gotos), 1)
        self.assertEqual(len(states), 1)
        self.assertEqual(gotos[0]["node_id"], "pick")       # 段首
        self.assertEqual(states[0]["node_id"], "ph1")       # 段尾
        self.assertEqual(states[0]["phase"], "received")

        box["phase"] = "sent"                                # 立刻回完
        sent = bridge.produce()
        gotos = [f for f in sent if f["type"] == "flow_goto"]
        states = [f for f in sent if f["type"] == "state"]
        self.assertEqual((len(gotos), len(states)), (1, 1))
        self.assertEqual(gotos[0]["node_id"], "put")
        self.assertEqual(states[0]["node_id"], "w1")
        # 各自只推一次。
        self.assertEqual(bridge.produce(), [])

    def test_flow_goto_targets_the_real_theme_graph(self):
        box = {"phase": "idle"}
        bridge, router = self._bridge(box)
        bridge.produce()
        box["phase"] = "received"
        frames = bridge.produce()
        goto = next(f for f in frames if f["type"] == "flow_goto")
        self.assertIn(goto["node_id"], [n["id"] for n in router.flow.nodes])
        self.assertTrue(goto["video"])
        state = next(f for f in frames if f["type"] == "state")
        self.assertEqual(goto["orders"], state["orders"])

    def test_bubble_expiry_fires_once(self):
        box = {"phase": "received"}
        bridge, _ = self._bridge(box)
        bridge.produce()
        # 等 TTL 过去，同一个阶段只能发一次清空帧。
        bridge._bubble_at -= (live_ws.BUBBLE_TTL_MS / 1000.0) + 1.0
        clears = [f for f in bridge.produce() if f["type"] == "bubble"]
        self.assertEqual(len(clears), 1)
        self.assertEqual(clears[0]["bubble_text"], "")
        self.assertEqual([f for f in bridge.produce() if f["type"] == "bubble"], [])

    # --------------------------- 4. idle socket stays readable + ping -------
    def test_idle_connection_can_still_read_after_timeout(self):
        bridge = live_ws.LiveBridge(router_factory=lambda: None,
                                    snapshot_getter=lambda: {})
        server_sock, client_sock = socket.socketpair()
        try:
            queue = live_ws.ClientQueue()
            conn = live_ws.ClientConnection(server_sock, queue, rfile=None)
            # 长时间闲置：select 轮询不能把连接判死。
            for _ in range(5):
                self.assertTrue(conn.wait_readable(0.02))
            payload = b"hello"
            mask = b"\x01\x02\x03\x04"
            masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
            frame = bytes([0x81, 0x80 | len(payload)]) + mask + masked
            client_sock.sendall(frame)
            self.assertTrue(conn.wait_readable(0.5))
            self.assertFalse(conn.closed)
            # 再闲置一轮仍然可读（旧实现在 timeout 后永久读不到）。
            self.assertTrue(conn.wait_readable(0.02))
            client_sock.sendall(bytes([0x89, 0x80]) + mask)  # masked ping
            self.assertTrue(conn.wait_readable(0.5))
        finally:
            server_sock.close()
            client_sock.close()

    def test_dead_peer_closes_cleanly(self):
        bridge = live_ws.LiveBridge(router_factory=lambda: None,
                                    snapshot_getter=lambda: {})
        server_sock, client_sock = socket.socketpair()
        conn = live_ws.ClientConnection(server_sock, live_ws.ClientQueue(), rfile=None)
        client_sock.close()
        self.assertFalse(conn.wait_readable(0.2))
        self.assertTrue(conn.closed)
        server_sock.close()


class NoRedirect(urllib.request.HTTPRedirectHandler):
    """Turn 3xx into exceptions so the Location header can be asserted."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


# ------------------------------------------------- opt-in theme selection
# 「小肥鱼的零食」市场主题的真实图：wait_1(loop) → action → both_done → action → wait_1。
# 这条图没有 phone 角色，正是需要「可往返 both_done 当关键节点」的情形。
SNACKS_THEME = {
    "schema": 1,
    "meta": {"id": "50756926fc", "name": "小肥鱼的零食", "version": "1.0.0",
             "cover": "cover.jpg"},
    "config": {
        "actions": [],
        "videos": {},
        "audio": {},
        "durations": {},
        "library": [
            {"id": "m_deae154c", "name": "休息", "video": "videos/a-rest.mp4", "duration": 4.063},
            {"id": "m_b69a515d", "name": "回消息工作", "video": "videos/b-work.mp4", "duration": 3.542},
            {"id": "m_ce3add8d", "name": "收到消息", "video": "videos/c-got.mp4", "duration": 3.878},
            {"id": "m_01d6b68d", "name": "放下手机", "video": "videos/d-down.mp4", "duration": 4.063},
        ],
        "flow": {
            "nodes": [
                {"id": "wait_1", "label": "吃零食", "role": "wait", "loop": True,
                 "video": "videos/a-rest.mp4", "media_id": "m_deae154c",
                 "duration": 4.063, "x": 420, "y": 220},
                {"id": "node_mwapo6kh", "label": "干活干活", "role": "both_done",
                 "loop": False, "video": "videos/b-work.mp4", "media_id": "m_b69a515d",
                 "duration": 3.542, "x": 496, "y": 120},
                {"id": "node_mwarm5xy", "label": "收到消息", "role": "action",
                 "loop": False, "video": "videos/c-got.mp4", "media_id": "m_ce3add8d",
                 "duration": 3.878, "x": 332, "y": 362},
                {"id": "node_mwarwecp", "label": "放下手机", "role": "action",
                 "loop": False, "video": "videos/d-down.mp4", "media_id": "m_01d6b68d",
                 "duration": 4.063, "x": 759, "y": 237},
            ],
            "edges": [
                {"id": "e1", "from": "wait_1", "to": "node_mwarm5xy"},
                {"id": "e2", "from": "node_mwarm5xy", "to": "node_mwapo6kh"},
                {"id": "e3", "from": "node_mwapo6kh", "to": "node_mwarwecp"},
                {"id": "e4", "from": "node_mwarwecp", "to": "wait_1"},
            ],
        },
        "settings": {"crossfade_ms": 400},
    },
}


def make_theme_root(base: Path, *, selection=None, snacks=True, default=True):
    """Build a served fish_house tree on disk, like a real deployment."""
    house = Path(base) / "fish_house"
    (house / "themes").mkdir(parents=True, exist_ok=True)
    if default:
        theme_dir = house / "themes" / "default"
        (theme_dir / "videos").mkdir(parents=True, exist_ok=True)
        (theme_dir / "theme.json").write_text(json.dumps(THEME), encoding="utf-8")
        (theme_dir / "videos" / "w1.mp4").write_bytes(b"0123456789")
    if snacks:
        theme_dir = house / "themes" / "snacks"
        (theme_dir / "videos").mkdir(parents=True, exist_ok=True)
        (theme_dir / "theme.json").write_text(json.dumps(SNACKS_THEME), encoding="utf-8")
        for name in ("a-rest", "b-work", "c-got", "d-down"):
            (theme_dir / "videos" / ("%s.mp4" % name)).write_bytes(b"SNACKS" + name.encode())
    if selection is not None:
        (house / "live.theme.json").write_text(
            selection if isinstance(selection, str) else json.dumps(selection),
            encoding="utf-8")
    (house / "home.html").write_text("<html>home</html>", encoding="utf-8")
    return house


class SnacksGraphTests(unittest.TestCase):
    """按图找关键节点：没有 phone 角色时用可往返 both_done，绝不写死资产名。"""

    def flow(self):
        return ThemeFlow(SNACKS_THEME, theme_id="snacks")

    def test_roles_and_key_node_come_from_the_graph(self):
        flow = self.flow()
        self.assertTrue(flow.ok)
        self.assertEqual(flow.wait_node()["id"], "wait_1")
        # 这条主题确实没有手机角色，也没有 both/work。
        self.assertIsNone(flow.phone_node())
        self.assertIsNone(flow.work_node())
        # 关键节点 = 唯一能往返 wait 的 both_done，而不是任何硬编码 id。
        self.assertEqual(flow.both_done_node()["id"], "node_mwapo6kh")
        self.assertEqual(flow.key_node()["id"], "node_mwapo6kh")

    def test_received_walks_收消息_then_干活(self):
        router = FlowRouter(self.flow())
        orders = router.orders("received", current="wait_1")
        self.assertEqual([o["node_id"] for o in orders], ["node_mwarm5xy", "node_mwapo6kh"])
        # 每个动作只播一轮，不能在半途循环卡住。
        self.assertEqual([o["loop"] for o in orders], [False, False])

    def test_quick_received_then_sent_plays_every_action_in_order_once(self):
        """快速收到→回复：全套动作有序、一次不重复。"""
        router = FlowRouter(self.flow())
        played: list[str] = []
        current = "wait_1"
        for phase in ("received", "sent"):
            orders = router.orders(phase, current=current)
            self.assertTrue(orders, "phase %s must still send orders" % phase)
            played.extend(o["node_id"] for o in orders)
            current = str(orders[-1]["node_id"])
        # received: 收消息 → 干活；sent: 放下手机 → 等待。整段不跳、不重复。
        self.assertEqual(played, ["node_mwarm5xy", "node_mwapo6kh",
                                  "node_mwarwecp", "wait_1"])
        self.assertEqual(len(played), len(set(played)), "no clip may repeat")
        # 落回等待后 idle 稳定在 wait 循环上，且再 poll 仍是同一段（不重复推）。
        idle = router.orders("idle", current="wait_1")
        self.assertEqual([o["node_id"] for o in idle], ["wait_1"])
        self.assertTrue(idle[0]["loop"])
        self.assertEqual(router.orders("idle", current="node_mwarwecp")[0]["node_id"],
                         "wait_1")

    def test_replying_loops_the_work_node_that_has_its_own_loop(self):
        router = FlowRouter(self.flow())
        # 走到干活位停住；both_done 自身 loop=False，这里如实沿用节点的值。
        orders = router.orders("replying", current="node_mwarm5xy")
        self.assertEqual([o["node_id"] for o in orders], ["node_mwapo6kh"])
        self.assertFalse(orders[0]["loop"])

    def test_videos_resolve_inside_the_selected_theme_folder(self):
        router = FlowRouter(self.flow())
        # root=None 时保持主题里写的相对路径，不凭空改写成别的目录。
        orders = router.orders("received", current="wait_1")
        self.assertEqual([o["video"] for o in orders],
                         ["videos/c-got.mp4", "videos/b-work.mp4"])

    def test_theme_without_any_cycle_degrades_to_wait_instead_of_hardcoding(self):
        theme = json.loads(json.dumps(SNACKS_THEME))
        # 断掉回路：both_done 回不到 wait。
        theme["config"]["flow"]["edges"] = [
            e for e in theme["config"]["flow"]["edges"]
            if not (e["from"] == "node_mwarwecp")]
        flow = ThemeFlow(theme, theme_id="snacks")
        self.assertIsNone(flow.key_node())
        router = FlowRouter(flow)
        orders = router.orders("thinking", current="wait_1")
        self.assertEqual([o["node_id"] for o in orders], ["wait_1"])
        self.assertTrue(orders[0]["loop"])


class ThemeSelectionTests(unittest.TestCase):
    """live.theme.json 的选择规则：严格同源、失败一律回落固定 default。"""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="fish-house-select-")
        self.addCleanup(self.temp.cleanup)

    def root(self, **kwargs):
        return make_theme_root(Path(self.temp.name), **kwargs)

    def test_absent_pointer_falls_back_to_default(self):
        house = self.root()
        choice = resolve_selection(house)
        self.assertEqual(choice["theme_url"], DEFAULT_THEME_URL)
        self.assertFalse(choice["selected"])
        self.assertEqual(choice["fallback"], "selection_absent")

    def test_valid_pointer_selects_the_snacks_theme(self):
        house = self.root(selection={"theme_url": "/fish_house/themes/snacks/theme.json"})
        choice = resolve_selection(house)
        self.assertTrue(choice["selected"])
        self.assertEqual(choice["slug"], "snacks")
        self.assertEqual(choice["theme_url"], "/fish_house/themes/snacks/theme.json")
        router = build_router(house, selection=choice)
        self.assertTrue(router.ok)
        self.assertEqual(router.theme_url, "/fish_house/themes/snacks/theme.json")
        self.assertEqual(router.flow.wait_node()["id"], "wait_1")

    def test_every_invalid_pointer_falls_back_to_default(self):
        bad = [
            {"theme_url": "http://127.0.0.1:1/fish_house/themes/snacks/theme.json"},
            {"theme_url": "//evil.example/fish_house/themes/snacks/theme.json"},
            {"theme_url": "/fish_house/themes/../default/theme.json"},
            {"theme_url": "/fish_house/themes/snacks/theme.json?v=1"},
            {"theme_url": "/fish_house/themes/snacks/theme.json#x"},
            {"theme_url": "/fish_house/themes/a%2e%2e/theme.json"},
            {"theme_url": "/fish_house/themes/snacks/other.json"},
            {"theme_url": ""},
            {"theme_url": 42},
            {"theme_url": "/fish_house/themes/does-not-exist/theme.json"},
            {"other": "/fish_house/themes/snacks/theme.json"},
            "{not json",
            "[]",
        ]
        for item in bad:
            with self.subTest(pointer=item):
                temp = tempfile.TemporaryDirectory(prefix="fish-house-bad-")
                self.addCleanup(temp.cleanup)
                house = make_theme_root(Path(temp.name), selection=item)
                choice = resolve_selection(house)
                self.assertEqual(choice["theme_url"], DEFAULT_THEME_URL)
                self.assertFalse(choice["selected"])
                self.assertEqual(choice["slug"], "default")
                # 仍能正常运行默认主题，不会因为坏指针把家园弄坏。
                self.assertTrue(build_router(house, selection=choice).ok)

    def test_unparseable_theme_behind_a_valid_pointer_falls_back(self):
        house = self.root(selection={"theme_url": "/fish_house/themes/snacks/theme.json"})
        (house / "themes" / "snacks" / "theme.json").write_text("{broken", encoding="utf-8")
        choice = resolve_selection(house)
        self.assertFalse(choice["selected"])
        self.assertEqual(choice["theme_url"], DEFAULT_THEME_URL)

    def test_theme_without_resolvable_flow_falls_back(self):
        house = self.root(selection={"theme_url": "/fish_house/themes/snacks/theme.json"})
        (house / "themes" / "snacks" / "theme.json").write_text(
            json.dumps({"config": {"flow": {"nodes": [], "edges": []}}}), encoding="utf-8")
        self.assertEqual(resolve_selection(house)["theme_url"], DEFAULT_THEME_URL)

    def test_slug_validation_is_strict_and_same_origin(self):
        cases = {
            "/fish_house/themes/snacks/theme.json": "snacks",
            "/fish_house/themes/a-b_C9/theme.json": "a-b_C9",
            "http://evil.example/fish_house/themes/x/theme.json": "",
            "//evil.example/fish_house/themes/x/theme.json": "",
            "/fish_house/themes/x/theme.json?x=1": "",
            "/fish_house/themes/../etc/theme.json": "",
            "/fish_house/themes/a%2e%2e/theme.json": "",
            "/fish_house/themes/x/y/theme.json": "",
            "/fish_house/themes/%s/theme.json" % ("a" * 40): "",
            "/fish_house/themes/x\\y/theme.json": "",
            "": "",
            None: "",
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(theme_slug_for_route(raw), expected)

    def test_oversized_or_unreadable_pointer_is_ignored(self):
        house = self.root(selection="x" * 5000)
        self.assertEqual(resolve_selection(house)["theme_url"], DEFAULT_THEME_URL)


class OptInHttpTests(unittest.TestCase):
    """真实 loopback：选择文件、原字节主题、未选中主题不外露。"""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="fish-house-optin-")
        self.addCleanup(self.temp.cleanup)
        self.house = make_theme_root(
            Path(self.temp.name),
            selection={"theme_url": "/fish_house/themes/snacks/theme.json"})
        self.server, self.httpd, self.port = self.serve(self.house)

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.server.bridge.stop()

    def serve(self, house):
        """每个用例一份新 server 模块：选择缓存不能跨夹具串味。"""
        saved = {key: value for key, value in sys.modules.items()
                 if key == "backend" or key.startswith("backend.")}
        for key in saved:
            del sys.modules[key]
        spec = importlib.util.spec_from_file_location("fh_optin_server", ROOT / "server.py")
        server = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(server)
        web = house.parent
        server.WEB, server.HOUSE = web, house
        server.bridge = live_ws.LiveBridge(
            router_factory=lambda: server.build_router(
                house, selection=server.live_theme_choice()),
            snapshot_getter=lambda: {"wechat": {"activity_state": "idle"}},
            panel_reader=lambda: None)
        httpd = server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        httpd.daemon_threads = True
        thread = threading.Thread(target=httpd.serve_forever,
                                  kwargs={"poll_interval": 0.2}, daemon=True)
        thread.start()

        def restore():
            for key in [key for key in sys.modules
                        if key == "backend" or key.startswith("backend.")]:
                del sys.modules[key]
            sys.modules.update(saved)
        self.addCleanup(restore)
        return server, httpd, httpd.server_port

    def fetch(self, path):
        with urllib.request.urlopen(
                "http://127.0.0.1:%d%s" % (self.port, path), timeout=5) as response:
            return response.status, response.read()

    def raw_fetch(self, path):
        """Send the literal request line.

        ``http.client`` collapses ``..``/``.`` before sending, so a traversal
        path can never be observed through :meth:`fetch`; this keeps the exact
        bytes the server would actually receive.
        """
        sock = socket.create_connection(("127.0.0.1", self.port), timeout=5)
        try:
            sock.sendall(("GET %s HTTP/1.1\r\nHost: 127.0.0.1:%d\r\n"
                          "Connection: close\r\n\r\n" % (path, self.port)).encode("latin-1"))
            buffer = b""
            while True:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                buffer += chunk
        finally:
            sock.close()
        status_line = buffer.split(b"\r\n", 1)[0].decode("latin-1")
        return int(status_line.split(" ")[1]), buffer

    def test_selection_endpoint_only_serves_the_validated_url(self):
        (self.house / "live.theme.json").write_text(json.dumps({
            "theme_url": "/fish_house/themes/snacks/theme.json",
            "ignored": "local fixture metadata",
        }), encoding="utf-8")
        status, body = self.fetch("/fish_house/live.theme.json")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body),
                         {"theme_url": "/fish_house/themes/snacks/theme.json"})

    def test_selected_theme_is_served_byte_identical(self):
        status, body = self.fetch("/fish_house/themes/snacks/theme.json")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["meta"]["name"], "小肥鱼的零食")
        # 原样 theme.json：与磁盘逐字节一致，没有二次序列化。
        self.assertEqual(body, (self.house / "themes" / "snacks" / "theme.json").read_bytes())
        # 固定 default 也能照旧拿到（保持 default 可用）。
        status, body = self.fetch(DEFAULT_THEME_URL)
        self.assertEqual(status, 200)
        self.assertEqual(body, (self.house / "themes" / "default" / "theme.json").read_bytes())

    def test_unselected_or_malformed_theme_routes_are_not_exposed(self):
        """只有选中的主题才由本服务放行；其余一律不额外暴露。"""
        # 本服务的路由层必须自己拒绝这些形态（含 `..` / `//` / 非 theme.json）。
        handler = self.server.Handler.__new__(self.server.Handler)
        for bad in ("/fish_house/themes/default2/theme.json",
                    "/fish_house/themes/..%2Fdefault/theme.json",
                    "/fish_house/themes/snacks/theme.json/../theme.json",
                    "/fish_house/themes/snacks/cover.jpg",
                    "/fish_house/themes/snacks/themes.js"):
            with self.subTest(path=bad):
                self.assertIsNone(handler.theme_route_for(bad))
        # 未在 live.theme.json 里声明的主题包不会被顺带放出去。
        status, _ = self.raw_fetch("/fish_house/themes/notselected/theme.json")
        self.assertEqual(status, 404)
        # 选中的那个主题本身是正常 200，且就是原样 theme.json。
        status, payload = self.raw_fetch("/fish_house/themes/snacks/theme.json")
        self.assertEqual(status, 200)
        body = payload.split(b"\r\n\r\n", 1)[1]
        self.assertEqual(body, (self.house / "themes" / "snacks" / "theme.json").read_bytes())

    def test_dot_dot_paths_never_reach_the_selected_theme_handler(self):
        """`..` 写法必须由本服务的路由自己挡掉，不能靠正则恰好不匹配。

        用原始请求行断言（``http.client`` 会先把 ``..`` 归一化，测不到服务端
        真正收到的那一行）：这些路径都不能落到「选中主题」的处理器上。
        """
        handler = self.server.Handler.__new__(self.server.Handler)
        traverse = ["/fish_house/themes/snacks/../default/theme.json",
                    "/fish_house/themes/snacks/..%2Fdefault/theme.json",
                    "/fish_house/themes/snacks/./theme.json",
                    "/fish_house/themes/../themes/snacks/theme.json",
                    "/fish_house/themes/snacks//theme.json"]
        for path in traverse:
            with self.subTest(path=path):
                self.assertIsNone(handler.theme_route_for(path))
                status, _ = self.raw_fetch(path)
                self.assertEqual(status, 404)
        # 合法路径仍然正常解析到选中的主题文件。
        chosen = handler.theme_route_for("/fish_house/themes/snacks/theme.json")
        self.assertIsNotNone(chosen)
        self.assertEqual(Path(chosen).parent.name, "snacks")

    def test_ws_config_names_the_selected_theme_and_key_node(self):
        sock = socket.create_connection(("127.0.0.1", self.port), timeout=6)
        try:
            sock.sendall(("\r\n".join([
                "GET /ws HTTP/1.1", "Host: 127.0.0.1:%d" % self.port,
                "Upgrade: websocket", "Connection: Upgrade",
                "Sec-WebSocket-Version: 13",
                "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ=="]) + "\r\n\r\n").encode("latin-1"))
            head = b""
            while b"\r\n\r\n" not in head:
                chunk = sock.recv(1)
                if not chunk:
                    break
                head += chunk
            self.assertIn("101", head.decode("latin-1"))

            class Reader:
                def read(self, count):
                    buf = b""
                    while len(buf) < count:
                        piece = sock.recv(count - len(buf))
                        if not piece:
                            break
                        buf += piece
                    return buf

            reader = Reader()
            frame = live_ws.read_frame(reader, expect_mask=False)
            self.assertIsNotNone(frame)
            config = json.loads(frame[1])
            self.assertEqual(config["type"], "config")
            self.assertEqual(config["theme_url"], "/fish_house/themes/snacks/theme.json")
            self.assertTrue(config["theme_ok"])
            self.assertEqual(config["nodes"],
                             {"wait": "wait_1", "phone": "", "work": "",
                              "key": "node_mwapo6kh"})
            self.assertEqual(config["videos"]["wait_1"],
                             "themes/snacks/videos/a-rest.mp4")
        finally:
            sock.close()

    def test_missing_selection_file_is_404_so_frontend_can_fall_back(self):
        # 选择文件缺失：直接 GET 必须 404，前端据此回退默认主题。
        (self.house / "live.theme.json").unlink()
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.fetch("/fish_house/live.theme.json")
        self.assertEqual(caught.exception.code, 404)
        # 服务端的选择有 5s 小缓存（一次刷新内 WS config 与 HTTP 路由必须一致）。
        # 断言前先让缓存过期，确认缺失时解析结果就是固定 default。
        self.server._THEME_CHOICE_CACHE.update({"at": 0.0, "house": None, "value": None})
        choice = self.server.live_theme_choice()
        self.assertEqual(choice["slug"], "default")
        self.assertFalse(choice["selected"])
        status, body = self.fetch(DEFAULT_THEME_URL)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["meta"]["id"], "test")
        # 主题路由这一层只认「当前有效选择」，缺省时不会把 snacks 放出去。
        handler = self.server.Handler.__new__(self.server.Handler)
        self.assertIsNone(handler.theme_route_for("/fish_house/themes/snacks/theme.json"))
        self.assertIsNotNone(handler.theme_route_for(DEFAULT_THEME_URL))

    def test_selection_change_takes_effect_without_restart(self):
        """指针换成 default 后，刷新缓存即可生效（从不重启服务）。"""
        self.assertEqual(self.server.live_theme_choice()["slug"], "snacks")
        (self.house / "live.theme.json").write_text(
            json.dumps({"theme_url": DEFAULT_THEME_URL}), encoding="utf-8")
        self.server._THEME_CHOICE_CACHE.update({"at": 0.0, "house": None, "value": None})
        choice = self.server.live_theme_choice()
        self.assertEqual(choice["theme_url"], DEFAULT_THEME_URL)
        self.assertEqual(choice["slug"], "default")
        # 切换后 WS config 也跟着新的选择走，HTTP 与 WS 用的是同一个结论。
        bridge = live_ws.LiveBridge(
            router_factory=lambda: self.server.build_router(
                self.house, selection=self.server.live_theme_choice()),
            snapshot_getter=lambda: {"wechat": {"activity_state": "idle"}},
            panel_reader=lambda: None)
        frames = bridge.snapshot_frames()
        config = next(frame for frame in frames if frame["type"] == "config")
        self.assertEqual(config["theme_url"], DEFAULT_THEME_URL)
        self.assertEqual(config["nodes"]["wait"], "w1")


if __name__ == "__main__":
    unittest.main()
