import hashlib
import unittest
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
                 patch.object(Path, "iterdir", return_value=iter(range(len(paths)))), \
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
