"""Verify the bridge consults saved controls before scoring and sending."""
import importlib.util
import io
import json
import os
from pathlib import Path
from queue import Queue
import re
import sys
import tempfile
from contextlib import redirect_stdout
from types import ModuleType, SimpleNamespace
import unittest
import uuid
from unittest.mock import Mock, patch


def load_bridge():
    # Offline tests use the production bridge with no real UI or X11 module.
    gi = ModuleType("gi")
    gi.require_version = Mock()
    repository = ModuleType("gi.repository")
    repository.Atspi = SimpleNamespace()
    xinput = ModuleType("pi_xinput")
    health = ModuleType("bridge_health")
    health.Health = Mock()
    spec = importlib.util.spec_from_file_location(
        "wechat_proactive_test_bridge", Path(__file__).with_name("pi_bridge.py"))
    module = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, {
        "gi": gi, "gi.repository": repository, "pi_xinput": xinput,
        "bridge_health": health,
        "wal_hybrid_wake": None,
    }):
        spec.loader.exec_module(module)
    sys.modules[spec.name] = module
    return module


bridge = load_bridge()
GROUP = "授权夹具群"


class RuntimeProactiveBridgeTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "config.json"
        self.args = SimpleNamespace(
            token_file=self.path, group=[GROUP], auto_reply_group=[GROUP],
            auto_limiter=bridge.AutoReplyLimiter(), bots=["大肥鱼"], vision=False,
            dry_run=False,
        )

    def configure(self, enabled):
        self.path.write_text(json.dumps({
            "group": {"proactiveJudge": {"enabled": enabled, "groups": [GROUP]}},
        }), encoding="utf-8")

    def ingest_bieye_text(self, text):
        target = "别野"
        self.path.write_text(json.dumps({
            "group": {"proactiveJudge": {"enabled": True, "groups": [target]}},
        }), encoding="utf-8")
        self.args.group = self.args.auto_reply_group = [target]
        self.args.auto_limiter = Mock(take=Mock(return_value=True))
        jobs = Queue()
        with patch.multiple(bridge, avatar_side=Mock(return_value=("incoming", 0)),
                            chat_title=Mock(return_value=target)):
            bridge.handle_rows(None, [(text, (1, 1, 2, 2))], 0, (0, 0, 500, 500),
                               target, self.args, jobs)
        return jobs

    def test_unaddressed_native_quote_does_not_enter_auto_queue(self):
        for message in (
            "我也这么理解\nQuote 某人's message: 夹具原话",
            "我也这么理解\nQuote 某人's message: @大肥鱼\u2005 夹具原话",
            "@大肥鱼 我也这么理解\nQuote 某人's message: 夹具原话",
        ):
            with self.subTest(message=message):
                self.assertTrue(self.ingest_bieye_text(message).empty())
                self.args.auto_limiter.take.assert_not_called()

    def test_native_mention_preserves_multiline_question_and_separates_quote(self):
        message = (
            "@大肥鱼\u2005 帮我解释这一段\n"
            "尤其是第二句的 @大肥鱼 是谁说的\n"
            "Quote 某人's message: 原话第一句\n原话第二句"
        )
        jobs = self.ingest_bieye_text(message)
        payload = jobs.get_nowait()[2]
        self.assertTrue(payload["at_me"])
        self.assertTrue(payload["quoted"])
        self.assertNotIn("group_auto_reply", payload)
        self.assertTrue(payload["text"].startswith(
            "帮我解释这一段\n尤其是第二句的 @大肥鱼 是谁说的\n"))
        self.assertIn("未验证", payload["text"])
        self.assertIn("引用者标签：某人；引用内容：原话第一句\n原话第二句", payload["text"])
        self.assertTrue(bridge._pilot_force_old(payload))
        self.args.auto_limiter.take.assert_not_called()
        self.assertTrue(jobs.empty())

    def test_qt_mention_only_reply_retains_reference(self):
        jobs = self.ingest_bieye_text("@大肥鱼\nQuote 某人's message: 需要说明的原话")
        payload = jobs.get_nowait()[2]
        self.assertTrue(payload["at_me"])
        self.assertTrue(payload["quoted"])
        self.assertIn("引用内容：需要说明的原话", payload["text"])
        self.assertTrue(jobs.empty())

    def test_ordinary_multiline_message_remains_auto_candidate(self):
        message = "我看了这段剧情\nQuote 是这集的标题"
        payload = self.ingest_bieye_text(message).get_nowait()[2]
        self.assertFalse(payload["at_me"])
        self.assertFalse(payload["quoted"])
        self.assertTrue(payload["group_auto_reply"])
        self.assertEqual(payload["text"], message)

    def ingest(self, direct):
        jobs = Queue(maxsize=2)
        bounds = (0, 0, 500, 500)
        rows = [("夹具消息包含足够的普通文本", (1, 1, 2, 2))]
        with patch.multiple(bridge, mention_body=Mock(return_value="明确提问" if direct else None),
                            avatar_side=Mock(return_value=("incoming", 0)),
                            chat_title=Mock(return_value=GROUP)):
            bridge.handle_rows(None, rows, 0, bounds, GROUP, self.args, jobs)
        return jobs

    def ingest_rows(self, rows, *, queue_size=4, mention=None, limiter=None,
                    proven=None, jobs=None, health=None, public_question_settler=None,
                    settled_trace=None, swift_route=False, monotonic=123.0,
                    cooldown_config=None, rows_observed_at=None, app=None):
        jobs = jobs or Queue(maxsize=queue_size)
        self.args.auto_limiter = limiter or Mock(take=Mock(return_value=True))
        self.configure(True)
        if cooldown_config is not None:
            policy = json.loads(self.path.read_text(encoding="utf-8"))
            policy["group"]["proactiveJudge"]["publicQuestionCooldowns"] = cooldown_config
            self.path.write_text(json.dumps(policy, ensure_ascii=False), encoding="utf-8")
        patches = {
            "mention_body": Mock(return_value=mention),
            "avatar_side": Mock(return_value=("incoming", 0)),
            "chat_title": Mock(return_value=GROUP),
        }
        if proven is not None:
            patches["bpc"] = SimpleNamespace(build_proven=proven)
        if swift_route:
            patches["core_target"] = Mock(return_value=(
                "http://127.0.0.1:41143", {"x-unified-token": "fixture-token"}))
        with patch.multiple(bridge, **patches), \
             patch.object(bridge.time, "monotonic", return_value=monotonic):
            result = bridge.handle_rows(app, rows, 0, (0, 0, 500, 500),
                                        GROUP, self.args, jobs, health,
                                        public_question_settler=public_question_settler,
                                        public_question_settled_trace=settled_trace,
                                        rows_observed_at=rows_observed_at)
        return result, jobs

    def test_compatibility_cooldown_does_not_block_other_authorized_group(self):
        other = "另一授权夹具群"
        self.args.auto_reply_group = self.args.group = [GROUP, other]
        self.path.write_text(json.dumps({
            "group": {"proactiveJudge": {"enabled": True, "groups": [GROUP, other]}},
        }), encoding="utf-8")
        jobs = Queue()

        def ingest(target, now):
            rows = [(f"同一话题的普通接话候选{now}", (1, 1, 2, 2))]
            with patch.multiple(bridge, mention_body=Mock(return_value=None),
                                avatar_side=Mock(return_value=("incoming", 0)),
                                chat_title=Mock(return_value=target)), \
                 patch.object(bridge.time, "monotonic", return_value=now):
                return bridge.handle_rows(None, rows, 0, (0, 0, 500, 500),
                                          target, self.args, jobs)

        self.assertTrue(ingest(GROUP, 100))
        self.assertFalse(ingest(GROUP, 101))
        self.assertTrue(ingest(other, 101))
        self.assertTrue(ingest(GROUP, 115))
        self.assertEqual([jobs.get_nowait()[0] for _ in range(3)], [GROUP, other, GROUP])
        self.assertTrue(jobs.empty())

    def test_compatibility_limiter_keeps_shared_hourly_and_daily_caps(self):
        limiter = bridge.AutoReplyLimiter(hourly_cap=1, daily_cap=2)
        self.assertTrue(limiter.take(GROUP, 100))
        self.assertFalse(limiter.take("第二个夹具群", 101))
        self.assertTrue(limiter.take("第二个夹具群", 3700))
        self.assertFalse(limiter.take("第三个夹具群", 7300))
        self.assertTrue(limiter.take("第三个夹具群", 86500))

    def test_ingress_latency_is_joinable_to_an_admitted_turn_without_text(self):
        output = io.StringIO()
        with redirect_stdout(output):
            accepted, jobs = self.ingest_rows(
                [("fixture-body-omit", (1, 1, 2, 2))], mention="fixture-direct",
                rows_observed_at=122.75)
        self.assertTrue(accepted)
        job = jobs.get_nowait()
        trace = job[2]["msg_id"]
        self.assertIn(f"ingress_timing trace='{trace}' visible_rows_to_queue_ms=250",
                      output.getvalue())
        self.assertNotIn("fixture-body-omit", output.getvalue())
        self.assertNotIn("fixture-direct", output.getvalue())

    def test_batched_ordinary_rows_only_latest_proven_row_consumes_limiter(self):
        limiter = Mock(take=Mock(return_value=True))
        rows = [
            ("第一条普通真实入站消息", (1, 1, 2, 2)),
            ("第二条普通真实入站消息", (3, 3, 2, 2)),
        ]
        with patch.object(bridge.bpc, "build_proven", wraps=bridge.bpc.build_proven) as prove:
            result, jobs = self.ingest_rows(rows, limiter=limiter)
        self.assertTrue(result)
        self.assertEqual(limiter.take.call_args_list, [unittest.mock.call(GROUP, 123.0)])
        prove.assert_called_once()
        queued = jobs.get_nowait()
        self.assertEqual(queued[2]["text"], "第二条普通真实入站消息")
        self.assertEqual(queued[2]["group_auto_reply_context"][-1],
                         "第二条普通真实入站消息")
        self.assertTrue(queued[2]["group_auto_reply_window_ok"])
        self.assertTrue(jobs.empty())

    def test_superseded_or_unproved_window_does_not_consume_limiter(self):
        limiter = Mock(take=Mock(return_value=True))
        health = Mock()
        output = io.StringIO()
        with redirect_stdout(output):
            _, jobs = self.ingest_rows(
                [("夹具私密正文", (1, 1, 2, 2))], limiter=limiter,
                proven=Mock(return_value=([], True, False)),
                health=health,
            )
        self.assertTrue(jobs.empty())
        limiter.take.assert_not_called()
        health.update.assert_not_called()
        diagnostic = output.getvalue()
        self.assertIn("auto_candidate", diagnostic)
        self.assertIn("window_unavailable", diagnostic)
        self.assertNotIn(GROUP, diagnostic)
        self.assertNotIn("夹具私密正文", diagnostic)

        # A superseded earlier row must not consume the one slot reserved for
        # the newest valid row in the same append batch.
        _, jobs = self.ingest_rows(
            [("旧普通入站消息", (1, 1, 2, 2)), ("最新普通入站消息", (3, 3, 2, 2))],
            limiter=limiter,
        )
        self.assertEqual(jobs.get_nowait()[2]["text"], "最新普通入站消息")
        self.assertTrue(jobs.empty())
        self.assertEqual(limiter.take.call_count, 1)

    def test_non_opted_group_does_not_emit_auto_candidate_diagnostics(self):
        self.configure(True)
        self.args.auto_reply_group = []
        health = Mock()
        output = io.StringIO()
        with patch.multiple(bridge,
                            mention_body=Mock(return_value=None),
                            avatar_side=Mock(return_value=("incoming", 0)),
                            chat_title=Mock(return_value=GROUP)), \
             redirect_stdout(output):
            bridge.handle_rows(None, [("普通群消息正文", (1, 1, 2, 2))], 0,
                               (0, 0, 500, 500), GROUP, self.args,
                               Queue(maxsize=2), health)
        self.assertEqual(output.getvalue(), "")
        health.update.assert_not_called()

    def test_full_queue_does_not_consume_limiter_and_explicit_mention_bypasses_it(self):
        limiter = Mock(take=Mock(return_value=True))
        full = Queue(maxsize=1)
        full.put_nowait("sentinel")
        _, jobs = self.ingest_rows(
            [("普通真实入站消息", (1, 1, 2, 2))], queue_size=1, limiter=limiter,
            jobs=full)
        self.assertEqual(jobs.get_nowait(), "sentinel")
        limiter.take.assert_not_called()

        _, jobs = self.ingest_rows(
            [("明确 @ 的真实提问", (1, 1, 2, 2))], mention="明确 @ 的真实提问",
            limiter=limiter)
        queued = jobs.get_nowait()
        self.assertEqual(queued[2]["text"], "明确 @ 的真实提问")
        limiter.take.assert_not_called()

    def test_swift_public_question_settles_before_queue_and_cooldown(self):
        limiter = Mock(take=Mock(return_value=True))
        settler = bridge.PublicQuestionSettleQueue()
        rows = [("夹具公开提问候选", (1, 1, 2, 2))]
        _, pending_jobs = self.ingest_rows(
            rows, limiter=limiter, public_question_settler=settler,
            swift_route=True)
        self.assertTrue(pending_jobs.empty())
        limiter.take.assert_not_called()
        pending = settler.pending(GROUP)
        self.assertIsNotNone(pending)
        self.assertEqual(pending.due_at, 124.5)
        self.assertNotIn("夹具公开提问候选", repr(pending))

        trace = pending.trace
        queued, jobs = self.ingest_rows(
            rows, limiter=limiter, public_question_settler=settler,
            settled_trace=trace, swift_route=True, monotonic=124.5,
            cooldown_config={GROUP: 240})
        self.assertTrue(queued)
        limiter.take.assert_not_called()
        payload = jobs.get_nowait()[2]
        self.assertTrue(payload["group_auto_reply_public_question_settled"])
        self.assertEqual(payload["group_auto_reply_public_question_cooldown_seconds"], 240)
        self.assertEqual(payload["msg_id"], trace)

    def test_media_demo_words_require_a_native_mention(self):
        for command, kind in (("发送测试图片", "image"), ("发送测试文件", "file")):
            with self.subTest(kind=kind):
                limiter = Mock(take=Mock(return_value=True))
                accepted, jobs = self.ingest_rows(
                    [(command, (1, 1, 2, 2))], limiter=limiter,
                    public_question_settler=bridge.PublicQuestionSettleQueue())
                self.assertFalse(accepted)
                self.assertTrue(jobs.empty())
                limiter.take.assert_not_called()

                target = "别野"
                payload = bridge.inbound_payload(command, target, target)
                with patch.object(bridge.pm, "approved_path", return_value=Path("fixture")) as lookup:
                    self.assertIsNone(bridge.approved_media_command(
                        target, payload | {"at_me": False, "group_auto_reply": True}, "fixture"))
                    self.assertIsNone(bridge.approved_media_command(
                        target, payload | {"quoted": True}, "fixture"))
                    self.assertIsNone(bridge.approved_media_command(
                        target, payload | {"photo_present": True}, "fixture"))
                    self.assertIsNone(bridge.approved_media_command("其他群", payload, "fixture"))
                    lookup.assert_not_called()
                    demo = bridge.approved_media_command(target, payload, "fixture")
                    self.assertEqual(demo["media"]["kind"], kind)
                    lookup.assert_called_once()
        self.assertTrue(bridge.eligible_auto_text("发测试图片的时候大家都笑了"))

    def test_ordinary_media_topic_stays_on_the_settled_swift_route(self):
        endpoint = "http://127.0.0.1:41143"
        routes = SimpleNamespace(
            swift_pilot_url=Mock(return_value=(endpoint, "fixture-token")),
            route_for=Mock(return_value=("swift", endpoint)),
        )
        env = {"DAFEIYU_WECHAT_SWIFT_PROACTIVE_GROUPS": json.dumps([GROUP]),
               "DAFEIYU_WECHAT_UNIFIED_ACCOUNT": "wx-fixture"}
        for text in ("队友的语音里大家都在笑", "相册里的照片拍得挺有意思",
                     "她说别用语音回复就好", "那个表情包太贴切了"):
            with self.subTest(text=text), patch.dict(os.environ, env), \
                 patch.object(bridge, "group_routes", routes):
                rows = [(text, (1, 1, 2, 2))]
                settle = bridge.PublicQuestionSettleQueue()
                accepted, jobs = self.ingest_rows(rows, public_question_settler=settle)
                self.assertFalse(accepted)
                self.assertTrue(jobs.empty())
                pending = settle.pending(GROUP)
                self.assertIsNotNone(pending)
                accepted, jobs = self.ingest_rows(
                    rows, public_question_settler=settle, settled_trace=pending.trace,
                    monotonic=pending.due_at, cooldown_config={GROUP: 30})
                self.assertTrue(accepted)
                payload = jobs.get_nowait()[2]
                self.assertTrue(payload["group_auto_reply_public_question_settled"])
                self.assertIn("x-unified-token", bridge.core_target(payload)[1])

    def test_voice_intent_and_real_media_keep_the_compatibility_route(self):
        env = {"DAFEIYU_WECHAT_SWIFT_PROACTIVE_GROUPS": json.dumps([GROUP]),
               "DAFEIYU_WECHAT_UNIFIED_ACCOUNT": "wx-fixture"}
        payload = bridge.inbound_payload("请用语音回复", GROUP, GROUP)
        payload.update(at_me=False, group_auto_reply=True, group_auto_reply_window_ok=True,
                       group_auto_reply_superseded=False,
                       group_auto_reply_context=["请用语音回复"])
        with patch.dict(os.environ, env):
            self.assertTrue(bridge._pilot_force_old(payload))
            for flag in ("photo_present", "quoted", "image_described", "rich_media", "multi_bubble"):
                with self.subTest(flag=flag):
                    self.assertTrue(bridge._pilot_force_old(payload | {"text": "闲聊", flag: True}))
            self.assertTrue(bridge._pilot_force_old(
                payload | {"at_me": True, "group_auto_reply": False, "text": "给我发张照片"}))
            with patch.object(bridge, "same_sender_image", return_value=True):
                accepted, jobs = self.ingest_rows(
                    [("[图片]", (1, 1, 2, 2)), ("这图很眼熟", (1, 1, 2, 2))],
                    public_question_settler=bridge.PublicQuestionSettleQueue())
            self.assertTrue(accepted)
            media_payload = jobs.get_nowait()[2]
            self.assertTrue(media_payload["photo_present"])
            self.assertTrue(bridge._pilot_force_old(media_payload))

    def test_adjacent_picture_needs_a_reference_from_an_automatic_candidate(self):
        group = "别野"
        self.args.group = self.args.auto_reply_group = [group]
        self.args.vision = True
        self.path.write_text(json.dumps({
            "group": {"proactiveJudge": {"enabled": True, "groups": [group]}},
        }), encoding="utf-8")
        env = {"DAFEIYU_WECHAT_SWIFT_PROACTIVE_GROUPS": json.dumps([group]),
               "DAFEIYU_WECHAT_UNIFIED_ACCOUNT": "wx-fixture"}
        direct = f"@大肥鱼{bridge.MENTION_TOKEN}这是什么？"
        cases = [
            ("今天队友配合得真不错", True, False, False),
            ("地图里有两条路可以走", True, False, False),
            ("这张截图里写的是什么", True, True, False),
            ("这张截图里写的是什么", False, False, False),
            (direct, True, True, True),
        ]
        for body, same_sender, capture_expected, addressed in cases:
            with self.subTest(body=body, same_sender=same_sender):
                self.args.auto_limiter = Mock(take=Mock(return_value=True))
                frame = (1, 1, 400, 80)
                rows = [("[图片]", frame), (body, (1, 82, 400, 60))]
                jobs = Queue(maxsize=2)
                with patch.dict(os.environ, env), patch.multiple(
                    bridge, avatar_side=Mock(return_value=("incoming", 0)),
                    chat_title=Mock(return_value=group), psv=None,
                    same_sender_image=Mock(return_value=same_sender),
                    capture_media=Mock(return_value=b"fixture-image"),
                ):
                    self.assertTrue(bridge.handle_rows(
                        None, rows, 1, (0, 0, 500, 500), group, self.args, jobs))
                    _, _, payload, photo, *_ = jobs.get_nowait()
                    self.assertEqual(payload["at_me"], addressed)
                    self.assertEqual(payload.get("photo_present", False), capture_expected)
                    self.assertEqual(bool(photo), capture_expected)
                    self.assertEqual(bridge._pilot_force_old(payload), capture_expected)
                    if capture_expected:
                        bridge.capture_media.assert_called_once_with(frame, (0, 0, 500, 500))
                    else:
                        bridge.capture_media.assert_not_called()

    def test_occupied_composer_skips_both_proactive_routes_without_consuming_limiter(self):
        reader = Mock(get_character_count=Mock(return_value=7))
        composer = Mock(return_value=(Mock(get_text_iface=Mock(return_value=reader)), None))
        limiter = Mock(take=Mock(return_value=True))
        settler = bridge.PublicQuestionSettleQueue()
        rows = [("夹具普通闲聊消息", (1, 1, 2, 2))]
        app = object()
        with patch.object(bridge, "composer", composer):
            accepted, jobs = self.ingest_rows(rows, limiter=limiter, app=app)
            self.assertFalse(accepted)
            self.assertTrue(jobs.empty())
            _, jobs = self.ingest_rows(rows, public_question_settler=settler,
                                       swift_route=True, app=app)
            self.assertTrue(jobs.empty())
            accepted, jobs = self.ingest_rows(
                rows, limiter=limiter, public_question_settler=settler,
                settled_trace=settler.pending(GROUP).trace, swift_route=True,
                monotonic=124.5, app=app)
            self.assertFalse(accepted)
            self.assertTrue(jobs.empty())
            accepted, jobs = self.ingest_rows(rows, mention="明确提问", app=app)
            self.assertTrue(accepted)
            self.assertFalse(jobs.get_nowait()[2].get("group_auto_reply", False))
        self.assertEqual(composer.call_args_list, [unittest.mock.call(app)] * 2)
        reader.get_text.assert_not_called()
        limiter.take.assert_not_called()

    def test_unknown_or_empty_composer_keeps_normal_proactive_admission(self):
        for result in (0, RuntimeError("unreadable")):
            with self.subTest(result=result):
                get_count = (Mock(side_effect=result) if isinstance(result, Exception)
                             else Mock(return_value=result))
                node = SimpleNamespace(get_text_iface=lambda: SimpleNamespace(
                    get_character_count=get_count))
                with patch.object(bridge, "composer", return_value=(node, None)):
                    accepted, jobs = self.ingest_rows(
                        [("夹具普通闲聊消息", (1, 1, 2, 2))], app=object())
                self.assertTrue(accepted)
                self.assertTrue(jobs.get_nowait()[2]["group_auto_reply"])
                get_count.assert_called_once()

    def test_native_mention_bypasses_settlement_and_feature_off_keeps_old_limiter(self):
        limiter = Mock(take=Mock(return_value=True))
        settler = bridge.PublicQuestionSettleQueue()
        _, mention_jobs = self.ingest_rows(
            [("明确 @ 的真实提问", (1, 1, 2, 2))], mention="明确 @ 的真实提问",
            limiter=limiter, public_question_settler=settler, swift_route=True)
        mention_payload = mention_jobs.get_nowait()[2]
        self.assertFalse(mention_payload.get("group_auto_reply_public_question_settled", False))
        self.assertFalse(settler.has_pending)
        limiter.take.assert_not_called()

        _, normal_jobs = self.ingest_rows(
            [("普通真实入站消息", (1, 1, 2, 2))], limiter=limiter)
        normal_payload = normal_jobs.get_nowait()[2]
        self.assertTrue(normal_payload["group_auto_reply"])
        self.assertNotIn("group_auto_reply_public_question_settled", normal_payload)
        limiter.take.assert_called_once()

    def deliver(self, auto_ctx, send, **evidence):
        responses = Queue()
        responses.put((GROUP, True, {
            "ok": True, "action": "reply", "reply": "夹具回复", "_auto_ctx": auto_ctx,
            **evidence,
        }, "fixture-trace"))
        get_app = Mock(return_value=object())
        with patch.multiple(bridge, get_app=get_app, blocking_state=Mock(return_value=None),
                            group_is_open=Mock(return_value=True),
                            read_messages=Mock(return_value=((0, 0, 4, 4), [])),
                            tx_guard=SimpleNamespace(auto_reply_recheck=Mock(
                                return_value=SimpleNamespace(allow=True))),
                            send_reply=send):
            bridge.deliver_ready(responses, self.args)
        return get_app

    def test_current_group_text_checks_blockers_in_the_transaction_before_input(self):
        responses = Queue()
        responses.put((GROUP, True, {"ok": True, "action": "reply", "reply": "夹具回复"}, "fixture-trace"))
        app = object()
        editor = Mock()
        reader = Mock(get_character_count=Mock(return_value=0))
        node = Mock(get_text_iface=Mock(return_value=reader), get_editable_text_iface=Mock(return_value=editor))
        bounds = (0, 0, 600, 400)
        xi = Mock()
        with patch.multiple(
                bridge, get_app=Mock(return_value=app), group_is_open=Mock(return_value=True),
                messages_list=Mock(return_value=object()), ext=Mock(return_value=bounds),
                _tx_header_info=Mock(return_value={"raw_title": GROUP, "showing_target": 1}),
                _tx_chats_info=Mock(return_value=(1, [GROUP], 1)),
                composer=Mock(return_value=(node, bounds)),
                Atspi=SimpleNamespace(Text=SimpleNamespace(get_text=lambda *_: "")),
                blocking_state=Mock(return_value="window_blocked"), open_chat=Mock(),
                xi=xi, record=Mock()):
            bridge.deliver_ready(responses, self.args)
            bridge.blocking_state.assert_called_once_with(app)
            bridge.composer.assert_called_once()
            bridge.open_chat.assert_not_called()
            bridge.record.assert_any_call(
                None, "submit", "tx_denied:blocking_state:window_blocked", "fixture-trace",
                success=False, failed=True, unknown=False)
        xi.click.assert_not_called()
        xi.key.assert_not_called()
        editor.set_text_contents.assert_not_called()

    def test_blockers_still_prevent_navigation_and_sticker_dispatch(self):
        app = object()
        for is_group, open_group, payload in (
                (True, False, {"reply": "夹具回复"}),
                (False, False, {"reply": "夹具回复"}),
                (True, True, {"stickerOnly": True, "sticker": {"id": "fixture"}})):
            with self.subTest(is_group=is_group, open_group=open_group, payload=payload):
                responses = Queue()
                responses.put((GROUP, is_group, {"ok": True, "action": "reply", **payload}, "fixture-trace"))
                with patch.multiple(
                        bridge, get_app=Mock(return_value=app), group_is_open=Mock(return_value=open_group),
                        blocking_state=Mock(return_value="window_blocked"), open_chat=Mock(),
                        open_account=Mock(), open_private_chat=Mock(), dispatch_sticker=Mock(),
                        send_reply=Mock(), record=Mock()):
                    bridge.deliver_ready(responses, self.args)
                    bridge.blocking_state.assert_called_once_with(app)
                    bridge.record.assert_any_call(None, "submit", "window_blocked", "fixture-trace", failed=True)
                    bridge.open_chat.assert_not_called()
                    bridge.open_account.assert_not_called()
                    bridge.open_private_chat.assert_not_called()
                    bridge.dispatch_sticker.assert_not_called()
                    bridge.send_reply.assert_not_called()

    def test_direct_mention_retries_transient_avatar(self):
        self.configure(True)
        bounds = (0, 0, 500, 500)
        app = object()
        direct = (f"@大肥鱼{bridge.MENTION_TOKEN}测试复读", (0, 200, 500, 55))
        for case in ("incoming", "outgoing", "changed", "other_account", "other_app"):
            with self.subTest(case=case):
                jobs = Queue()
                row = direct
                view = [row] if case != "changed" else [("changed fixture", row[1])]
                side = ("outgoing", 5) if case == "outgoing" else ("incoming", 5)
                account = "other-account" if case == "other_account" else "fixture-account"
                with patch.multiple(
                    bridge, get_app=Mock(return_value=object() if case == "other_app" else app),
                    chat_title=Mock(return_value=GROUP), app_memory_account=Mock(return_value=account),
                    read_messages=Mock(return_value=(bounds, view)),
                    avatar_side=Mock(side_effect=[None, side]),
                ), patch.object(bridge.time, "sleep") as delay:
                    bridge.handle_rows(app, [row], 0, bounds, GROUP, self.args, jobs,
                                       memory_account="fixture-account")
                    if case == "incoming":
                        self.assertTrue(jobs.get_nowait()[2]["at_me"])
                        self.assertEqual(bridge.avatar_side.call_count, 2)
                    else:
                        self.assertTrue(jobs.empty())
                    delay.assert_called_once_with(0.14)

    def test_latest_ordinary_message_retries_transient_avatar_without_guessing(self):
        bounds = (0, 0, 500, 500)
        app = object()
        newest = ("这里有一条同话题的普通发言", (0, 260, 500, 55))
        older = ("这条普通消息已经被超越", (0, 200, 500, 55))
        for case in ("incoming", "outgoing", "newer", "changed", "other_account",
                     "other_app", "different_group", "disabled"):
            with self.subTest(case=case):
                self.configure(case != "disabled")
                jobs = Queue()
                limiter = Mock(take=Mock(return_value=True))
                self.args.auto_limiter = limiter
                rows = [older, newest]
                current_rows = rows
                if case == "newer":
                    current_rows = rows + [("又有新消息", (0, 320, 500, 55))]
                if case == "changed":
                    current_rows = [older, ("可见消息已重排", newest[1])]
                after_paint = ("outgoing" if case == "outgoing" else "incoming", 5)
                read_current = Mock(return_value=(bounds, current_rows))
                with patch.multiple(
                        bridge, get_app=Mock(return_value=object() if case == "other_app" else app),
                        chat_title=Mock(side_effect=[GROUP, "另一个群"] if case == "different_group" else None,
                                        return_value=GROUP),
                        app_memory_account=Mock(return_value=(
                            "second-account" if case == "other_account" else "fixture-account")),
                        read_messages=read_current,
                        avatar_side=Mock(side_effect=[None, None, after_paint]),
                        _composer_has_text=Mock(return_value=False)), \
                     patch.object(bridge.time, "sleep") as delay:
                    handled = bridge.handle_rows(app, rows, 0, bounds, GROUP, self.args, jobs,
                                                 memory_account="fixture-account")
                if case == "incoming":
                    self.assertTrue(handled)
                    target, is_group, payload, *_ = jobs.get_nowait()
                    self.assertEqual((target, is_group), (GROUP, True))
                    self.assertEqual(payload["text"], newest[0])
                    self.assertFalse(payload["at_me"])
                    self.assertTrue(payload["group_auto_reply_window_ok"])
                    limiter.take.assert_called_once()
                    read_current.assert_called_once_with(app)
                else:
                    self.assertFalse(handled)
                    self.assertTrue(jobs.empty())
                    limiter.take.assert_not_called()
                if case == "disabled":
                    delay.assert_not_called()
                else:
                    delay.assert_called_once_with(0.14)

    def test_delayed_direction_uses_fresh_rows_without_charging_an_unproven_candidate(self):
        bounds = (0, 0, 500, 500)
        app = object()
        body = "这里是刚出现的同话题普通发言"
        rows = [("前面还在聊同一个话题", (0, 360, 500, 55)), (body, (0, 475, 500, 55))]
        shifted = [(rows[0][0], (0, 300, 500, 55)), (body, (0, 365, 500, 55))]
        for side in ("incoming", "outgoing", None):
            with self.subTest(side=side):
                self.configure(True)
                jobs = Queue()
                settler = bridge.PublicQuestionSettleQueue(delay=1.5)
                self.args.auto_limiter = Mock(take=Mock(return_value=True))
                clock = Mock(return_value=123.0)
                with patch.multiple(
                    bridge, get_app=Mock(return_value=app), chat_title=Mock(return_value=GROUP),
                    app_memory_account=Mock(return_value="fixture-account"),
                    read_messages=Mock(return_value=(bounds, shifted)),
                    avatar_side=Mock(return_value=None), _composer_has_text=Mock(return_value=False),
                    core_target=Mock(return_value=("http://127.0.0.1:41143", {"x-unified-token": "test"})),
                ), patch.object(bridge.time, "sleep"), patch.object(bridge.time, "monotonic", clock):
                    self.assertFalse(bridge.handle_rows(
                        app, rows, 1, bounds, GROUP, self.args, jobs,
                        public_question_settler=settler, memory_account="fixture-account"))
                    self.assertTrue(jobs.empty())
                    self.args.auto_limiter.take.assert_not_called()

                    self.assertIsNone(settler.due_trace(GROUP, 124.0))
                    trace = settler.due_trace(GROUP, 124.5)
                    self.assertIsNotNone(trace)
                    trace_at = lambda i: bridge.stable_message_trace(
                        GROUP, shifted, i, account="fixture-account")
                    self.assertTrue(bridge.latest_candidate_matches(
                        shifted, trace, trace_at, bridge.eligible_auto_text))
                    newer = shifted + [("来了另一条新的普通消息", (0, 430, 500, 55))]
                    self.assertFalse(bridge.latest_candidate_matches(
                        newer, trace, lambda i: bridge.stable_message_trace(
                            GROUP, newer, i, account="fixture-account"), bridge.eligible_auto_text))
                    settler.cancel(GROUP)
                    clock.return_value = 124.5
                    bridge.avatar_side.return_value = (side, 5) if side else None
                    accepted = bridge.handle_rows(
                        app, shifted, 1, bounds, GROUP, self.args, jobs, public_question_settler=settler,
                        public_question_settled_trace=trace, memory_account="fixture-account")
                    self.assertEqual(accepted, side == "incoming")
                    self.assertFalse(settler.has_pending)
                    if side == "incoming":
                        self.assertEqual(jobs.get_nowait()[2]["msg_id"], trace)
                    else:
                        self.assertTrue(jobs.empty())
                    self.args.auto_limiter.take.assert_not_called()

    def test_disabled_ordinary_messages_do_not_enter_queue_but_native_mentions_do(self):
        self.configure(False)
        self.assertTrue(self.ingest(direct=False).empty())
        self.assertIsNone(self.ingest(direct=True).get_nowait()[-1])
        self.configure(True)
        proactive = self.ingest(direct=False).get_nowait()
        self.assertTrue(proactive[2]["group_auto_reply"])
        self.assertIsNotNone(proactive[-1])

    def test_settled_candidate_fails_closed_without_swift_and_never_falls_back(self):
        payload = {
            "group_auto_reply": True, "at_me": False, "chat_type": "group",
            "chat_id": GROUP, "chat_title": GROUP, "account": "wx-1",
            "sender_id": "member", "msg_id": "a" * 64,
            "group_auto_reply_window_ok": True,
            "group_auto_reply_superseded": False,
            "group_auto_reply_context": ["候选"],
            "group_auto_reply_public_question_settled": True,
            "group_auto_reply_public_question_cooldown_seconds": 240,
            "text": "公开提问夹具",
        }
        refused = {"ok": False, "error": "core_connection_refused"}
        with patch.object(bridge, "core_target", return_value=(bridge.CORE_URL, {})), \
             patch.object(bridge, "_ask_core_once") as request:
            result = bridge.ask_core_routed(payload, "fixture-token")
        self.assertEqual(result["error"], "settled_candidate_swift_route_unavailable")
        request.assert_not_called()

        with patch.object(bridge, "core_target", return_value=(
                "http://127.0.0.1:41143", {"x-unified-token": "fixture-token"})), \
             patch.object(bridge, "_ask_core_once", return_value=refused) as request:
            result = bridge.ask_core_routed(payload, "fixture-token")
        self.assertIs(result, refused)
        request.assert_called_once()

    def test_paused_group_blocks_ordinary_entry_and_delivery_but_allows_native_mention(self):
        self.configure(True)
        self.assertFalse(self.ingest(direct=False).empty())
        policy = json.loads(self.path.read_text(encoding="utf-8"))
        policy["group"]["proactiveJudge"]["pausedGroups"] = [GROUP]
        self.path.write_text(json.dumps(policy), encoding="utf-8")
        self.assertTrue(self.ingest(direct=False).empty())
        self.assertTrue(self.ingest(direct=True).get_nowait()[2]["at_me"])
        send = Mock()
        app = self.deliver({"target": GROUP}, send)
        app.assert_not_called()
        send.assert_not_called()

    def test_turning_off_after_generation_drops_reply_before_the_gui(self):
        self.configure(False)
        send = Mock()
        app = self.deliver({"target": GROUP}, send)
        app.assert_not_called()
        send.assert_not_called()

    def test_receipt_comes_only_from_the_successful_swift_route(self):
        receipt = str(uuid.uuid4())
        reply = {"ok": True, "action": "reply", "memoryReceipt": receipt,
                 "_swift_memory_receipt": "untrusted"}
        payload = {"text": "合成入站"}
        target = ("http://127.0.0.1:41143", {"x-unified-token": "fixture"})
        with patch.object(bridge, "core_target", return_value=target), \
             patch.object(bridge, "_ask_core_once", return_value=dict(reply)):
            self.assertEqual(bridge.ask_core_routed(payload, "")["_swift_memory_receipt"], receipt)
        with patch.object(bridge, "core_target", return_value=target), \
             patch.object(bridge, "_ask_core_once", side_effect=[
                 {"ok": False, "error": "core_connection_refused"}, dict(reply)
             ]):
            self.assertNotIn("_swift_memory_receipt", bridge.ask_core_routed(payload, ""))
        with patch.object(bridge, "core_target", return_value=(bridge.CORE_URL, {})), \
             patch.object(bridge, "_ask_core_once", return_value=dict(reply)):
            self.assertNotIn("_swift_memory_receipt", bridge.ask_core_routed(payload, ""))

    def test_only_verified_submission_confirms_memory(self):
        self.configure(True)
        receipt = str(uuid.uuid4())
        for proof in ("visible_outgoing", "unconfirmed:receipt_timeout"):
            with self.subTest(proof=proof):
                def send(_target, _reply, reserve, **_):
                    reserve()
                    return proof

                with patch.object(bridge, "confirm_swift_memory") as confirm:
                    self.deliver(None, send, _swift_memory_receipt=receipt)
                if proof == "visible_outgoing":
                    confirm.assert_called_once_with(receipt, "fixture-trace")
                else:
                    confirm.assert_not_called()

    def test_confirmed_memory_is_queued_without_waiting_for_swift(self):
        self.configure(True)
        self.args._swift_memory_receipts = Queue(maxsize=1)
        receipt = str(uuid.uuid4())

        def send(_target, _reply, reserve, **_):
            reserve()
            return "visible_outgoing"

        with patch.object(bridge, "confirm_swift_memory") as confirm:
            self.deliver(None, send, _swift_memory_receipt=receipt)
        confirm.assert_not_called()
        self.assertEqual(self.args._swift_memory_receipts.get_nowait(), (receipt, "fixture-trace"))

    def test_swift_confirmation_retries_transient_errors_only(self):
        receipt = str(uuid.uuid4())
        trace = "a" * 64
        route = SimpleNamespace(swift_pilot_url=lambda: ("http://127.0.0.1:41143", "fixture"))
        for results, requests, expected in (
            ([{"error": "TimeoutError"}, {"ok": True}], 2, "stored"),
            ([{"error": "core_connection_refused"}, {"error": "http_503"}, {"ok": True}], 3, "stored"),
            ([{"error": "http_404"}, {"ok": True}], 1, "unavailable"),
            ([{"error": "http_403"}, {"ok": True}], 1, "unavailable"),
        ):
            with self.subTest(results=results), patch.object(bridge, "group_routes", route), \
                    patch.object(bridge, "_ask_core_once", side_effect=results) as post, \
                    patch.object(bridge.time, "sleep"), patch.object(bridge, "log") as log:
                bridge.confirm_swift_memory(receipt, trace)
                self.assertEqual(post.call_count, requests)
                self.assertTrue(all(call.args[1:3] == ("/api/memory/confirmed", {"receipt": receipt})
                                    for call in post.call_args_list))
                log.assert_called_once_with("swift_memory", trace=trace, status=expected)

    def test_compatibility_persona_uses_swift_scope_and_reloads_on_next_message(self):
        path = self.path.with_name("unified-wechat-group-personas.json")
        scope = "ba5094ec427ac8a09855653d71fe55d54c865d585396f3f016b28257e06604db"
        env = {"DAFEIYU_DATA_DIR": str(path.parent), "DAFEIYU_WECHAT_UNIFIED_ACCOUNT": "fixture-account"}
        def write(preset):
            path.write_text(json.dumps({"version": 1, "presets": {scope: preset}}), encoding="utf-8")
            path.chmod(0o600)

        payload = {"account": "ui-alias", "chat_type": "group", "chat_id": "fixture-group",
                   "quoted": True, "text": "合成引用", "bridge_style": "伪造"}
        swift = ("http://127.0.0.1:41143", {"x-unified-token": "fixture-token"})
        with patch.dict(os.environ, env), patch.object(bridge, "_ask_core_once", return_value={"ok": True}) as call:
            write("温柔")
            with patch.object(bridge, "core_target", return_value=(bridge.CORE_URL, {})):
                bridge.ask_core_routed(payload, "fixture-token")
                self.assertEqual(call.call_args.args[2]["bridge_style"], "温柔")
                write("理性")
                bridge.ask_core_routed(payload, "fixture-token")
                self.assertEqual(call.call_args.args[2]["bridge_style"], "理性")
                for changes in ({"chat_type": "private"}, {"chat_id": "other-group"}):
                    bridge.ask_core_routed(payload | changes, "fixture-token")
                    self.assertNotIn("bridge_style", call.call_args.args[2])
                path.write_text("invalid", encoding="utf-8")
                bridge.ask_core_routed(payload, "fixture-token")
                self.assertNotIn("bridge_style", call.call_args.args[2])
            write("温柔")
            with patch.object(bridge, "core_target", return_value=swift):
                call.side_effect = [{"ok": False, "error": "core_connection_refused"}, {"ok": True}]
                bridge.ask_core_routed(payload, "fixture-token")
                self.assertEqual(call.call_args.args[:2], (bridge.CORE_URL, "/webhook/wechat"))
                self.assertEqual(call.call_args.args[2]["bridge_style"], "温柔")

    def test_ordinary_automatic_text_carries_its_context_into_the_send_transaction(self):
        self.configure(True)
        context = {"target": GROUP, "rows": ("digest",), "index": 0, "trace": "fixture"}
        send = Mock(return_value="auto_reply_stale:new_rows_or_viewport_shift")
        self.deliver(context, send)
        send.assert_called_once()
        self.assertIs(send.call_args.kwargs["auto_ctx"], context)

    def test_turning_off_before_draft_reservation_prevents_submission(self):
        self.configure(True)
        writes = []

        def send(target, reply, reserve, *, auto_ctx, trace, memory_account):
            self.assertEqual(trace, "fixture-trace")
            self.configure(False)
            reserve()
            writes.append("draft")
            return "visible_outgoing"

        app = self.deliver({"target": GROUP}, send)
        app.assert_called_once()
        self.assertEqual(writes, [])

    def test_direct_reply_can_still_be_submitted_after_disabling_proactive(self):
        self.configure(False)
        writes = []

        def send(target, reply, reserve, *, auto_ctx, trace, memory_account):
            self.assertEqual(trace, "fixture-trace")
            self.assertIsNone(auto_ctx)
            reserve()
            writes.append(target)
            return "visible_outgoing"

        self.deliver(None, send)
        self.assertEqual(writes, [GROUP])

    def test_proactive_sticker_cannot_reserve_after_policy_changes(self):
        self.configure(True)
        self.args._sticker_enabled = True
        self.args._sticker_token = "fixture"
        self.args._sticker_library = self.path.parent
        self.args._sticker_stage = self.path.parent
        self.args._sticker_cooldown = None
        health = Mock()
        health.is_terminal.return_value = False

        def process_sticker(job, target, token, reserve, *args, **kwargs):
            self.configure(False)
            with self.assertRaisesRegex(RuntimeError, "auto_policy_disabled"):
                reserve()
            return "auto_policy_disabled", False

        transport = SimpleNamespace(
            APPROVED_GROUP=GROUP, UUID_RE=re.compile("fixture-id"),
            StickerError=RuntimeError, process_sticker=process_sticker,
        )
        guard = Mock()
        guard.before_media.return_value = SimpleNamespace(allow=True)
        with patch.multiple(bridge, pst=transport, _tx_new_guard=Mock(return_value=guard)):
            status = bridge.dispatch_sticker(
                GROUP, {"id": "fixture-id"}, self.args, health, "fixture", proactive=True)
        self.assertEqual(status, "auto_policy_disabled")
        health.reserve_submit.assert_not_called()


if __name__ == "__main__":
    unittest.main()
