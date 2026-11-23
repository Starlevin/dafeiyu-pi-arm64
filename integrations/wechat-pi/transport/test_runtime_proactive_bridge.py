"""Verify the bridge consults saved controls before scoring and sending."""
import importlib.util
import io
import json
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
                    cooldown_config=None):
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
            result = bridge.handle_rows(None, rows, 0, (0, 0, 500, 500),
                                        GROUP, self.args, jobs, health,
                                        public_question_settler=public_question_settler,
                                        public_question_settled_trace=settled_trace)
        return result, jobs

    def test_batched_ordinary_rows_only_latest_proven_row_consumes_limiter(self):
        limiter = Mock(take=Mock(return_value=True))
        rows = [
            ("第一条普通真实入站消息", (1, 1, 2, 2)),
            ("第二条普通真实入站消息", (3, 3, 2, 2)),
        ]
        with patch.object(bridge.bpc, "build_proven", wraps=bridge.bpc.build_proven) as prove:
            result, jobs = self.ingest_rows(rows, limiter=limiter)
        self.assertTrue(result)
        self.assertEqual(limiter.take.call_args_list, [unittest.mock.call(123.0)])
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

    def test_turning_off_before_draft_reservation_prevents_submission(self):
        self.configure(True)
        writes = []

        def send(target, reply, reserve, *, auto_ctx, trace):
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

        def send(target, reply, reserve, *, auto_ctx, trace):
            self.assertEqual(trace, "fixture-trace")
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
