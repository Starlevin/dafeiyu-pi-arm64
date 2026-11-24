import hashlib
import unittest
import uuid
from contextlib import nullcontext
from pathlib import Path
from queue import Queue
from types import SimpleNamespace
from unittest.mock import Mock, patch

import wal_hint_readonly as hint
from test_runtime_proactive_bridge import bridge


class MemoryScopeTests(unittest.TestCase):
    def test_one_open_account_with_both_database_roles_is_required(self):
        home = Path.cwd().anchor
        home = Path(home)
        account = home / "xwechat_files" / "operator-fixture"
        session = str(account / "db_storage" / "session" / "session.db")
        message = str(account / "db_storage" / "message" / "message_0.db-wal")
        other = str(home / "xwechat_files" / "another" / "db_storage" / "session" / "session.db")
        expected = hashlib.sha256(b"wechat-local-account-v1\0operator-fixture").hexdigest()
        for paths, scope in (([session, message], expected), ([session], None),
                             ([session, message, other], None)):
            with self.subTest(path_count=len(paths)), \
                 patch.object(hint.os, "scandir", return_value=nullcontext([
                     SimpleNamespace(path=str(i)) for i in range(len(paths))
                 ])), \
                 patch.object(hint.os, "readlink", side_effect=paths):
                self.assertEqual(hint.active_account_scope(412, home=home), scope)

    def test_process_reuse_cannot_prove_the_previous_account(self):
        with patch.object(bridge, "active_account_scope", return_value="a" * 64), \
             patch.object(bridge, "_wechat_client_identity", side_effect=[("client", 1), ("client", 2)]):
            self.assertIsNone(bridge.client_memory_account(412))

    def test_unbound_account_uses_compatibility_without_primary_persona(self):
        payload = {"chat_type": "group", "chat_id": "fixture", "text": "offline fixture",
                   "memory_account": "b" * 64, "_memory_account_pid": 412,
                   "_memory_account_bound": False}
        candidate_routes = SimpleNamespace(
            swift_pilot_url=Mock(return_value=("http://127.0.0.1:41143", "fixture")),
            route_for=Mock(return_value=("swift", "http://127.0.0.1:41143")),
        )
        with patch.multiple(bridge, group_routes=candidate_routes,
                            _ask_core_once=Mock(return_value={"ok": True}),
                            _compatibility_group_persona=Mock(side_effect=lambda body, *_: body)):
            with patch.dict(bridge.os.environ, {"DAFEIYU_WECHAT_UNIFIED_ACCOUNT": "wx-fixture"}):
                bridge.ask_core_routed(payload, "fixture")
            forwarded = bridge._ask_core_once.call_args.args[2]
            self.assertEqual(bridge._ask_core_once.call_args.args[0], bridge.CORE_URL)
            self.assertEqual(bridge._compatibility_group_persona.call_args.args[1], "")
            self.assertEqual(forwarded["memory_account"], "b" * 64)
            self.assertNotIn("_memory_account_pid", forwarded)
            candidate_routes.route_for.assert_not_called()

    def test_compatibility_receipt_comes_only_from_the_scoped_compatibility_route(self):
        receipt = str(uuid.uuid4())
        payload = {"memory_account": "a" * 64}
        response = {"ok": True, "action": "reply", "compatMemoryReceipt": receipt,
                    "_compat_memory_receipt": "spoofed"}
        swift = ("http://127.0.0.1:41143", {"x-unified-token": "fixture"})
        compat = (bridge.CORE_URL, {})
        for route, inbound, valid in ((compat, payload, True), (compat, {}, False),
                                      (swift, payload, False)):
            with self.subTest(route=route, valid=valid), \
                 patch.object(bridge, "core_target", return_value=route), \
                 patch.object(bridge, "_ask_core_once", return_value=dict(response)):
                result = bridge.ask_core_routed(inbound, "fixture")
                self.assertNotIn("compatMemoryReceipt", result)
                self.assertEqual(result.get("_compat_memory_receipt"), receipt if valid else None)
        with patch.object(bridge, "core_target", return_value=swift), \
             patch.object(bridge, "_ask_core_once", side_effect=[
                 {"ok": False, "error": "core_connection_refused"}, dict(response)
             ]):
            self.assertEqual(bridge.ask_core_routed(payload, "fixture").get("_compat_memory_receipt"),
                             receipt)

    def test_compatibility_memory_requires_a_visible_submission_on_the_same_account(self):
        account, trace, receipt = "a" * 64, "c" * 64, str(uuid.uuid4())
        args = SimpleNamespace(_memory_account_binding=account, group=["fixture"])
        scenarios = (("visible_outgoing", True, account, True),
                     ("unconfirmed:receipt_timeout", True, account, False),
                     ("visible_outgoing", True, "b" * 64, False),
                     ("visible_outgoing", False, account, False))
        for result, submit, last_account, stored in scenarios:
            with self.subTest(result=result, submit=submit, stored=stored):
                replies = Queue()
                replies.put(("fixture", True, {"ok": True, "action": "reply", "reply": "fixture",
                                              "_compat_memory_receipt": receipt,
                                              "_bridge_memory_account": account}, trace))
                def send(_target, _reply, before_return, **_):
                    if submit:
                        before_return()
                    return result

                with patch.multiple(
                        bridge, get_app=Mock(),
                        app_memory_account=Mock(side_effect=[account, account, last_account]
                                                if submit else [account, last_account]),
                        blocking_state=Mock(return_value=""), group_is_open=Mock(return_value=True),
                        send_reply=Mock(side_effect=send), confirm_compat_memory=Mock()):
                    bridge.deliver_ready(replies, args, token="fixture")
                    if stored:
                        bridge.confirm_compat_memory.assert_called_once_with(receipt, account, trace, "fixture")
                    else:
                        bridge.confirm_compat_memory.assert_not_called()

    def test_account_switch_before_return_preserves_delivery_ledger(self):
        account = "a" * 64
        args = SimpleNamespace(_memory_account_binding=account, group=["fixture"])
        health = Mock(is_terminal=Mock(return_value=False))
        replies = Queue()
        replies.put(("fixture", True, {"ok": True, "action": "reply", "reply": "fixture",
                                      "_bridge_memory_account": account}, "c" * 64))
        with patch.multiple(
                bridge, get_app=Mock(), app_memory_account=Mock(side_effect=[account, "b" * 64]),
                blocking_state=Mock(return_value=""), group_is_open=Mock(return_value=True),
                send_reply=Mock(side_effect=lambda _target, _reply, before_return, **kw: before_return())):
            bridge.deliver_ready(replies, args, health)
        health.reserve_submit.assert_not_called()
        self.assertTrue(any(call.args[:2] == ("submit", "memory_account_changed")
                            for call in health.update.call_args_list))


if __name__ == "__main__":
    unittest.main()
