"""Offline tests for the WeChat candidate-group allowlist; no UI or network."""
from __future__ import annotations

import unittest
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import tempfile

from auto_reply_allowlist import (auto_reply_group_allowed,
                                  configured_auto_reply_groups,
                                  configured_swift_proactive_groups,
                                  selected_auto_reply_groups,
                                  runtime_public_question_cooldown,
                                  runtime_proactive_group_allowed,
                                  _MAX_POLICY_BYTES)
from auto_reply_allowlist import swift_proactive_candidate_allowed


class AutoReplyAllowlistTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(prefix="wechat-proactive-policy-")
        self.addCleanup(self.temp_dir.cleanup)
        self.policy_path = Path(self.temp_dir.name) / "config.json"

    def write_policy(self, enabled=True, groups=None):
        document = {"group": {"proactiveJudge": {
            "enabled": enabled,
            "groups": ["别野"] if groups is None else groups,
        }}}
        self.policy_path.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")

    def write_cooldown_policy(self, cooldowns="missing", groups=None):
        proactive = {"enabled": True, "groups": ["别野"] if groups is None else groups}
        if cooldowns != "missing":
            proactive["publicQuestionCooldowns"] = cooldowns
        self.policy_path.write_text(json.dumps({
            "group": {"proactiveJudge": proactive},
        }, ensure_ascii=False), encoding="utf-8")

    def test_public_question_cooldown_uses_exact_group_and_default(self):
        self.write_cooldown_policy()
        self.assertEqual(runtime_public_question_cooldown("别野", self.policy_path), 60)
        self.write_cooldown_policy({"别野": 240, "别野 ": 120}, groups=["别野", "别野 "])
        self.assertEqual(runtime_public_question_cooldown("别野", self.policy_path), 240)
        self.assertEqual(runtime_public_question_cooldown("别野 ", self.policy_path), 120)

        self.write_cooldown_policy({"别野 ": 120}, groups=["别野", "别野 "])
        self.assertEqual(runtime_public_question_cooldown("别野", self.policy_path), 60)
        self.assertEqual(runtime_public_question_cooldown("别野 ", self.policy_path), 120)

    def test_public_question_cooldown_bad_values_or_path_fail_closed(self):
        for value in (29, 1201, 30.0, True, "60", None):
            with self.subTest(value=value):
                self.write_cooldown_policy({"别野": value})
                self.assertIsNone(runtime_public_question_cooldown("别野", self.policy_path))
                # A bad cooldown setting does not rewrite the existing proactive
                # allowlist behavior; only the new settled fast path fails closed.
                self.assertTrue(runtime_proactive_group_allowed("别野", self.policy_path))
        self.write_cooldown_policy([])
        self.assertIsNone(runtime_public_question_cooldown("别野", self.policy_path))
        self.assertIsNone(runtime_public_question_cooldown("别野", self.policy_path / "missing"))

    def test_public_question_cooldown_reloads_atomic_config_replacement(self):
        self.write_cooldown_policy({"别野": 120})
        self.assertEqual(runtime_public_question_cooldown("别野", self.policy_path), 120)
        replacement = Path(self.temp_dir.name) / "config.next"
        replacement.write_text(json.dumps({"group": {"proactiveJudge": {
            "enabled": True, "groups": ["别野"], "publicQuestionCooldowns": {"别野": 600},
        }}}, ensure_ascii=False), encoding="utf-8")
        os.replace(replacement, self.policy_path)
        self.assertEqual(runtime_public_question_cooldown("别野", self.policy_path), 600)

    def test_runtime_policy_hot_reloads_enabled_groups_and_exact_titles(self):
        self.write_policy(enabled=True, groups=["别野"])
        self.assertTrue(runtime_proactive_group_allowed("别野", self.policy_path))
        self.assertFalse(runtime_proactive_group_allowed("别野 ", self.policy_path))
        self.assertFalse(runtime_proactive_group_allowed("别野(2)", self.policy_path))

        self.write_policy(enabled=False, groups=["别野"])
        self.assertFalse(runtime_proactive_group_allowed("别野", self.policy_path))
        self.write_policy(enabled=True, groups=["别野"])
        self.assertTrue(runtime_proactive_group_allowed("别野", self.policy_path))

        self.write_policy(enabled=True, groups=["别野 "])
        self.assertFalse(runtime_proactive_group_allowed("别野", self.policy_path))
        self.assertTrue(runtime_proactive_group_allowed("别野 ", self.policy_path))

    def test_runtime_policy_removal_empty_list_and_bad_update_fail_closed(self):
        self.write_policy(enabled=True, groups=["别野"])
        self.assertTrue(runtime_proactive_group_allowed("别野", self.policy_path))

        self.write_policy(enabled=True, groups=[])
        self.assertFalse(runtime_proactive_group_allowed("别野", self.policy_path))
        self.write_policy(enabled=True, groups=["别野"])
        self.assertTrue(runtime_proactive_group_allowed("别野", self.policy_path))

        self.write_policy(enabled=True, groups=["其他群"])
        self.assertFalse(runtime_proactive_group_allowed("别野", self.policy_path))
        self.policy_path.write_text('{"group":', encoding="utf-8")
        self.assertFalse(runtime_proactive_group_allowed("别野", self.policy_path))
        self.write_policy(enabled=True, groups=["别野"])
        self.assertTrue(runtime_proactive_group_allowed("别野", self.policy_path))

    def test_runtime_policy_atomic_replace_and_missing_file(self):
        self.write_policy(enabled=True, groups=["别野"])
        self.assertTrue(runtime_proactive_group_allowed("别野", self.policy_path))

        replacement = Path(self.temp_dir.name) / "config.next"
        replacement.write_text(json.dumps({"group": {"proactiveJudge": {
            "enabled": True, "groups": ["新群"],
        }}}, ensure_ascii=False), encoding="utf-8")
        os.replace(replacement, self.policy_path)
        self.assertFalse(runtime_proactive_group_allowed("别野", self.policy_path))
        self.assertTrue(runtime_proactive_group_allowed("新群", self.policy_path))

        self.policy_path.unlink()
        self.assertFalse(runtime_proactive_group_allowed("新群", self.policy_path))

    def test_runtime_policy_rejects_oversized_and_malformed_group_lists(self):
        self.policy_path.write_bytes(b" " * (_MAX_POLICY_BYTES + 1))
        self.assertFalse(runtime_proactive_group_allowed("别野", self.policy_path))

        for groups in (["别野", 3], [""]):
            with self.subTest(groups=groups):
                self.write_policy(enabled=True, groups=groups)
                self.assertFalse(runtime_proactive_group_allowed("别野", self.policy_path))
        self.policy_path.write_text(
            json.dumps({"group": {"proactiveJudge": {"enabled": True}}}), encoding="utf-8")
        self.assertFalse(runtime_proactive_group_allowed("别野", self.policy_path))

    def test_runtime_policy_reads_are_thread_safe(self):
        self.write_policy(enabled=True, groups=["别野"])
        with ThreadPoolExecutor(max_workers=12) as pool:
            results = list(pool.map(
                lambda _: runtime_proactive_group_allowed("别野", self.policy_path), range(100)))
        self.assertTrue(all(results))

    def test_swift_proactive_requires_explicit_exact_group_and_current_proof(self):
        self.assertEqual(configured_swift_proactive_groups({}), [])
        groups = configured_swift_proactive_groups({
            "DAFEIYU_WECHAT_SWIFT_PROACTIVE_GROUPS": '["别野"]',
        })
        payload = {
            "group_auto_reply": True, "at_me": False, "chat_type": "group",
            "chat_id": "别野", "chat_title": "别野", "account": "wx", "sender_id": "member",
            "msg_id": "m1", "group_auto_reply_window_ok": True,
            "group_auto_reply_superseded": False, "group_auto_reply_context": ["候选"],
        }
        self.assertTrue(swift_proactive_candidate_allowed(payload, groups))
        settled = {**payload,
                   "group_auto_reply_public_question_settled": True,
                   "group_auto_reply_public_question_cooldown_seconds": 240}
        self.assertTrue(swift_proactive_candidate_allowed(settled, groups))
        self.assertFalse(swift_proactive_candidate_allowed(
            {**settled, "group_auto_reply_public_question_cooldown_seconds": True}, groups))
        self.assertFalse(swift_proactive_candidate_allowed(
            {**settled, "group_auto_reply_public_question_cooldown_seconds": 1201}, groups))
        for change in ({"at_me": True}, {"chat_id": "其他群"},
                       {"group_auto_reply_window_ok": False},
                       {"group_auto_reply_superseded": True}, {"quoted": True},
                       {"sender_id": ""}, {"group_auto_reply_context": []}):
            with self.subTest(change=change):
                self.assertFalse(swift_proactive_candidate_allowed({**payload, **change}, groups))

    def test_default_is_empty(self):
        groups, error = configured_auto_reply_groups({})
        self.assertEqual(groups, [])
        self.assertIsNone(error)
        self.assertFalse(auto_reply_group_allowed("别野", groups, ["别野"]))

    def test_json_allowlist_deduplicates_exact_group_titles(self):
        groups, error = configured_auto_reply_groups({
            "DAFEIYU_WECHAT_AUTO_REPLY_GROUPS": '["别野", "别野"]',
        })
        self.assertIsNone(error)
        self.assertEqual(groups, ["别野"])

    def test_json_explicitly_enables_only_new_group_and_keeps_legacy_cli(self):
        configured, error = configured_auto_reply_groups({
            "DAFEIYU_WECHAT_AUTO_REPLY_GROUPS": '["别野"]',
        })
        self.assertIsNone(error)
        selected, error = selected_auto_reply_groups(
            [], configured, "我推的apex人格", ["我推的apex人格", "别野", "其他群"])
        self.assertIsNone(error)
        self.assertEqual(selected, ["别野"])

        # Existing explicit Apex CLI opt-in remains valid with its env flag off.
        selected, error = selected_auto_reply_groups(
            ["我推的apex人格"], [], "我推的apex人格", ["我推的apex人格"])
        self.assertIsNone(error)
        self.assertEqual(selected, ["我推的apex人格"])

    def test_unmonitored_or_unallowlisted_selection_is_rejected(self):
        selected, error = selected_auto_reply_groups(
            [], ["别野"], "我推的apex人格", ["我推的apex人格"])
        self.assertEqual(selected, [])
        self.assertIsNotNone(error)
        selected, error = selected_auto_reply_groups(
            ["其他群"], [], "我推的apex人格", ["其他群"])
        self.assertEqual(selected, [])
        self.assertIsNotNone(error)

    def test_exact_membership_in_config_and_monitored_groups_is_required(self):
        configured = ["Apex", "别野"]
        monitored = ["Apex", "别野", "其他群"]
        self.assertTrue(auto_reply_group_allowed("别野", configured, monitored))
        self.assertFalse(auto_reply_group_allowed("别野(3)", configured, monitored))
        self.assertFalse(auto_reply_group_allowed("其他群", configured, monitored))
        self.assertFalse(auto_reply_group_allowed("Apex", configured, ["别野"]))
        self.assertFalse(auto_reply_group_allowed("  ", configured, monitored))

    def test_malformed_or_non_string_json_fails_closed(self):
        for raw in ("not-json", "{}", '["别野", 2]', '["  "]'):
            with self.subTest(raw=raw):
                groups, error = configured_auto_reply_groups({
                    "DAFEIYU_WECHAT_AUTO_REPLY_GROUPS": raw,
                })
                self.assertEqual(groups, [])
                self.assertIsNotNone(error)


if __name__ == "__main__":
    unittest.main(verbosity=2)
