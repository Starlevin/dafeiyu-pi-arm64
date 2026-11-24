"""Focused send-receipt tests; AST-load pure code without importing AT-SPI/GI."""
from __future__ import annotations

import ast
import hashlib
import re
import types
import unittest
from pathlib import Path
from unittest.mock import Mock

import tx_guard as real_tx_guard

SOURCE = Path(__file__).with_name("pi_bridge.py")
_TREE = ast.parse(SOURCE.read_text(encoding="utf-8"))
_NAMES = {"baseline", "new_rows", "_safe_receipt_reason",
          "_classify_visible_outgoing", "_valid_receipt_baseline",
          "_receipt_identity_reason", "_receipt_proxy_live", "send_reply", "record"}
_FUNCTIONS = [node for node in _TREE.body
              if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
              and node.name in _NAMES]


def _helpers(namespace):
    exec(compile(ast.Module(body=[node for node in _FUNCTIONS
                                 if node.name != "send_reply"],
                           type_ignores=[]), str(SOURCE), "exec"), namespace)


class TxVerdict:
    def __init__(self, allow, reason="identity_light_verified"):
        self.allow = allow
        self.reason = reason


class FakeReader:
    def __init__(self, state):
        self.state = state
        self.text = ""
    def get_character_count(self):
        if self.state["reader_throws"] and self.state["returned"]:
            raise RuntimeError("synthetic reader fault")
        return len(self.text)


class FakeEditor:
    def __init__(self, reader):
        self.reader = reader
    def set_text_contents(self, value):
        self.reader.text = value


class FakeNode:
    def __init__(self, reader):
        self.reader = reader
    def get_text_iface(self):
        return self.reader
    def get_editable_text_iface(self):
        return FakeEditor(self.reader)


class FakeStateSet:
    def __init__(self, defunct=False): self.defunct = defunct
    def contains(self, state): return state == "defunct" and self.defunct


class FakeApp:
    def __init__(self, defunct=False): self.defunct = defunct
    def get_state_set(self): return FakeStateSet(self.defunct)


class SendReplyHarness:
    TARGET = "测试群"
    BOUNDS = (0, 0, 600, 400)
    def __init__(self, *, after_return="allow", reader_throws=False,
                 baseline_read_throws=False,
                 state_wait_enabled=False,
                 selected=True, baseline_rows=None, baseline_bounds=None,
                 pre_return_rows=None,
                 post_rows=None, post_read_sequence=None, after_return_rows=None,
                 after_return_delay=0.0, sleep_scale=1.0,
                 post_get_app_sequence=None, defunct_proxy=False,
                 identity_title_sequence=None, identity_selected_sequence=None):
        self.state = {
            "returned": False, "reader_throws": reader_throws,
            "baseline_read_throws": baseline_read_throws,
            "state_wait_enabled": state_wait_enabled,
            "selected": selected, "after_return": after_return,
            "baseline_rows": baseline_rows if baseline_rows is not None else
                [("此前可见消息", (0, 1, 600, 50))],
            "pre_return_rows": pre_return_rows,
            "pre_return_read_count": 0,
            "baseline_bounds": baseline_bounds if baseline_bounds is not None else self.BOUNDS,
            "post_rows": post_rows,
            "post_read_sequence": list(post_read_sequence or []),
            "post_read_index": 0,
            "after_return_rows": after_return_rows,
            "after_return_delay": after_return_delay,
            "after_return_active": False,
            "sleeps_after_guard": 0,
            "sleep_scale": sleep_scale,
            "post_read_after_guard": 0,
            "post_get_app_sequence": list(post_get_app_sequence or []),
            "post_get_app_index": 0,
            "post_get_app_calls": 0,
            "identity_title_sequence": list(identity_title_sequence or []),
            "identity_selected_sequence": list(identity_selected_sequence or []),
            "return_key_count": 0,
            "submit_count": 0,
            "timing_logs": [], "clock": 0.0,
            "after_return_calls": 0, "identity_checks": 0,
            "identity_app_ids": [],
        }
        self.reader = FakeReader(self.state)
        self.node = FakeNode(self.reader)
        self.app = FakeApp(defunct=defunct_proxy)
        self.other_app = FakeApp()
        self.guard = self._guard()
        def sleep(seconds):
            if self.state["after_return_active"]:
                self.state["sleeps_after_guard"] += 1
            self.state["clock"] += seconds * self.state["sleep_scale"]
        self.ns = {
            "hashlib": hashlib, "re": re, "tx_guard": types.SimpleNamespace(
                norm_target=lambda value: value.strip(),
                row_first_line=lambda value: value.split("\n", 1)[0].strip(),
                auto_reply_recheck=real_tx_guard.auto_reply_recheck),
            "UI_STATE_WAIT_ENABLED": state_wait_enabled, "DRAFT_SETTLE_S": 0.25,
            "DRAFT_POLL_S": 0.02, "Atspi": types.SimpleNamespace(Text=
                types.SimpleNamespace(get_text=lambda reader, _a, _b: reader.text),
                StateType=types.SimpleNamespace(DEFUNCT="defunct")),
            "time": types.SimpleNamespace(
                sleep=sleep, monotonic=lambda: self.state["clock"]),
            "log": lambda event, **fields:
                self.state["timing_logs"].append((event, fields)),
            "get_app": self._get_app,
            "chat_title": lambda app: self.TARGET if app is self.app else "other",
            "composer": lambda _app: (self.node, (0, 0, 400, 40)),
            "_tx_composer_fp": lambda _node: "synthetic-fingerprint",
            "_tx_new_guard": self._new_guard,
            "_tx_observe": self._observe_original,
            "read_messages": self._read_messages,
            "_tx_header_info": self._header_info,
            "_tx_chats_info": self._chats_info,
            "avatar_side": lambda row, _bounds: ("outgoing" if row[1] == 1 else "incoming", "fixture"),
            "xi": types.SimpleNamespace(click=lambda *_a, **_k: None, key=self._key),
            "_wait_for_state": self._wait,
            "_safe_receipt_reason": lambda reason: reason if reason in {
                "reader_unavailable", "composer_not_empty", "no_new_rows",
                "header_unreadable",
                "selected_row_mismatch", "ambiguous_same_name",
                "identity_light_verified", "target_title_mismatch",
                "client_unavailable", "title_mismatch", "target_changed",
                "selected_row_unreadable", "no_selected_row",
                "multiple_selected_rows", "target_row_mismatch"} else "other",
            "_receipt_proxy_live": _FUNCTIONS_BY_NAME.get("_receipt_proxy_live"),
        }
        _helpers(self.ns)
        self.ns["_receipt_identity_reason"] = _FUNCTIONS_BY_NAME["_receipt_identity_reason"]
        # Re-evaluate with helpers in the harness namespace.
        exec(compile(ast.Module(body=[_FUNCTIONS_BY_NAME["_receipt_identity_reason"]],
                                type_ignores=[]), str(SOURCE), "exec"), self.ns)
        exec(compile(ast.Module(body=[_FUNCTIONS_BY_NAME["send_reply"]],
                                type_ignores=[]), str(SOURCE), "exec"), self.ns)

    def _new_guard(self, _target, *, observe=None):
        self.state["observe"] = observe
        return self.guard

    def _observe_original(self, _target, original):
        original.update(app=self.app, node=self.node, rect=(0, 0, 400, 40),
                        fp="synthetic-fingerprint", reader=self.reader,
                        editor=self.node.get_editable_text_iface())

    def _guard(self):
        state = self.state
        class Guard:
            def before_send(self):
                state["observe"]()
                return TxVerdict(True)
            def after_click(self, _count): return TxVerdict(True)
            def before_return(self, _text): return TxVerdict(True)
            def clear_proof(self, *_args): return TxVerdict(False)
            def after_return(self, _count):
                state["after_return_calls"] += 1
                state["after_return_active"] = True
                state["clock"] += state["after_return_delay"]
                if state["after_return_rows"] is not None:
                    state["post_rows"] = state["after_return_rows"]
                if state["after_return"] == "raise":
                    raise RuntimeError("synthetic guard fault")
                if state["after_return"] == "deny":
                    return TxVerdict(False, "composer_not_empty")
                return TxVerdict(True)
        return Guard()

    def _read_messages(self, _app):
        if not self.state["returned"]:
            self.state["pre_return_read_count"] += 1
        if self.state["returned"] and self.state["after_return_active"]:
            self.state["post_read_after_guard"] += 1
        if not self.state["returned"] and self.state["baseline_read_throws"]:
            raise RuntimeError("synthetic baseline read fault")
        if self.state["returned"] and self.state["post_read_index"] < len(self.state["post_read_sequence"]):
            rows = self.state["post_read_sequence"][self.state["post_read_index"]]
            self.state["post_read_index"] += 1
        else:
            if self.state["returned"]:
                rows = self.state["post_rows"]
            else:
                rows = (self.state["pre_return_rows"]
                        if self.state["pre_return_rows"] is not None
                        else self.state["baseline_rows"])
        bounds = self.BOUNDS if self.state["returned"] else self.state["baseline_bounds"]
        return bounds, list(rows)

    def _chats_info(self, _app, target):
        checks = self.state["identity_checks"]
        sequence = self.state["identity_selected_sequence"]
        selected = sequence[checks - 1] if checks and checks <= len(sequence) else self.state["selected"]
        if not selected:
            return 1, ["别的群"], 0
        return 1, [target], 1

    def _header_info(self, app, target):
        self.state["identity_checks"] += 1
        self.state["identity_app_ids"].append(id(app))
        sequence = self.state["identity_title_sequence"]
        title = sequence[self.state["identity_checks"] - 1] if self.state["identity_checks"] <= len(sequence) else target
        if title is None:
            return None
        return {"raw_title": title, "showing_target": 1 if title == target else 0}

    def _get_app(self):
        if not self.state["returned"]:
            return self.app
        self.state["post_get_app_calls"] += 1
        index = self.state["post_get_app_index"]
        self.state["post_get_app_index"] += 1
        sequence = self.state["post_get_app_sequence"]
        if index < len(sequence):
            item = sequence[index]
            if item == "same":
                return self.app
            return self.other_app if item == "other" else item
        return self.app

    def _key(self, name, *_args, **_kwargs):
        if name == "Return":
            self.state["returned"] = True
            self.state["return_key_count"] += 1
            self.reader.text = ""

    def _wait(self, predicate, *_args):
        return (bool(predicate()), 0.0)

    def send(self, text="我的回复", auto_ctx=None, *, trace=None, memory_account=None):
        def submitted(): self.state["submit_count"] += 1
        return self.ns["send_reply"](self.TARGET, text, submitted,
                                      auto_ctx=auto_ctx, trace=trace, memory_account=memory_account)


_FUNCTIONS_BY_NAME = {node.name: node for node in _FUNCTIONS}


def side_for(row, _bounds):
    return row


class SendReceiptTests(unittest.TestCase):
    def test_settled_candidate_rejects_message_arriving_after_delivery_recheck(self):
        rows = [("群友提问", (0, 1, 600, 50))]
        ctx = real_tx_guard.make_auto_context(SendReplyHarness.TARGET, rows, 0)
        harness = SendReplyHarness(
            baseline_rows=rows,
            pre_return_rows=rows + [("新补答", (0, 51, 600, 50))],
            post_rows=rows + [("新补答", (0, 51, 600, 50))])

        status = harness.send(auto_ctx=ctx)

        self.assertEqual(status, "auto_reply_stale:new_rows_or_viewport_shift")
        self.assertEqual(harness.state["return_key_count"], 0)
        self.assertEqual(harness.state["submit_count"], 0)
        self.assertEqual(harness.state["pre_return_read_count"], 1)
        # The fake cannot prove it owns the draft, so it must leave it untouched.
        self.assertEqual(harness.reader.text, "我的回复")

    def test_settled_candidate_with_same_snapshot_submits_and_returns_once(self):
        rows = [("群友提问", (0, 1, 600, 50))]
        ctx = real_tx_guard.make_auto_context(SendReplyHarness.TARGET, rows, 0)
        harness = SendReplyHarness(baseline_rows=rows, pre_return_rows=rows,
                                   post_rows=rows + [("我的回复", 1)])

        status = harness.send(auto_ctx=ctx)

        self.assertNotIn("auto_reply_stale", status)
        self.assertEqual(harness.state["return_key_count"], 1)
        self.assertEqual(harness.state["submit_count"], 1)
        self.assertEqual(harness.state["pre_return_read_count"], 1)

    def test_composer_uses_short_click_only_in_state_wait_mode(self):
        for enabled, expected in (
            (False, {"settle": 0.2}),
            (True, {"settle": 0.12, "move_delay": 0.03, "hold_delay": 0.03}),
        ):
            with self.subTest(state_wait_enabled=enabled):
                harness = SendReplyHarness(state_wait_enabled=enabled)
                harness.ns["xi"].click = Mock()
                harness.send()
                harness.ns["xi"].click.assert_called_once_with(150, 20, **expected)
                self.assertEqual(harness.state["return_key_count"], 1)

    def test_settled_candidate_baseline_timing_survives_ledger_reservation(self):
        rows = [("群友提问", (0, 1, 600, 50))]
        ctx = real_tx_guard.make_auto_context(SendReplyHarness.TARGET, rows, 0)
        harness = SendReplyHarness(
            baseline_rows=rows, pre_return_rows=rows,
            post_rows=rows + [("我的回复", (0, 1, 600, 50))])
        read = harness.ns["read_messages"]

        def timed_read(app):
            if not harness.state["returned"]:
                harness.ns["time"].sleep(0.05)
            return read(app)

        harness.ns["read_messages"] = timed_read
        harness.send(auto_ctx=ctx)
        timing = harness.state["timing_logs"][-1][1]
        self.assertGreaterEqual(timing["receipt_baseline_ms"], 49)
        self.assertEqual(harness.state["submit_count"], 1)
        self.assertEqual(harness.state["return_key_count"], 1)

    def setUp(self):
        self.bounds = (0, 0, 600, 400)
        self.previous = [("incoming old", ("incoming", "avatar"))]
        self.previous_hashes = None
        ns = {"hashlib": hashlib, "re": re}
        _helpers(ns)
        self.baseline = ns["baseline"]
        self.classify = ns["_classify_visible_outgoing"]
        self.safe_reason = ns["_safe_receipt_reason"]
        self.previous_hashes = self.baseline(self.previous)

    def classify_rows(self, current, *, target_verified=True, bounds=None):
        return self.classify(target_verified, "我的回复", self.previous_hashes,
                             current, self.bounds if bounds is None else bounds,
                             side_for)

    def test_confirms_only_new_exact_outgoing_row(self):
        current = self.previous + [("我的回复", ("outgoing", "avatar"))]
        self.assertEqual(self.classify_rows(current), (True, "visible_outgoing"))

    def test_rejects_exact_outgoing_text_without_same_target_proof(self):
        current = self.previous + [("我的回复", ("outgoing", "avatar"))]
        self.assertEqual(self.classify_rows(current, target_verified=False),
                         (False, "target_unverified"))

    def test_does_not_confirm_existing_matching_outgoing_row(self):
        current = [("我的回复", ("outgoing", "avatar"))]
        hashes = self.baseline(current)
        self.assertEqual(self.classify(True, "我的回复", hashes, current,
                                        self.bounds, side_for),
                         (False, "no_new_rows"))

    def test_does_not_confirm_incoming_or_different_outgoing_row(self):
        incoming = self.previous + [("我的回复", ("incoming", "avatar"))]
        self.assertEqual(self.classify_rows(incoming),
                         (False, "new_rows_not_outgoing"))
        different = self.previous + [("另一条", ("outgoing", "avatar"))]
        self.assertEqual(self.classify_rows(different),
                         (False, "new_outgoing_text_mismatch"))

    def test_viewport_realignment_failure_is_explicit(self):
        current = [("unrelated viewport", ("outgoing", "avatar")),
                   ("我的回复", ("outgoing", "avatar"))]
        self.assertEqual(self.classify_rows(current), (False, "viewport_unaligned"))

    def test_missing_rows_and_redacted_reason(self):
        self.assertEqual(self.classify(True, "我的回复", self.previous_hashes,
                                        self.previous, None, side_for),
                         (False, "message_list_unavailable"))
        self.assertEqual(self.safe_reason("selected row: PRIVATE CHAT TEXT"), "other")

    def test_send_reply_recovers_after_return_guard_denial(self):
        harness = SendReplyHarness(
            after_return="deny",
            post_rows=[("此前可见消息", (0, 0, 600, 50)),
                       ("我的回复", (0, 1, 600, 50))])
        self.assertEqual(harness.send(), "visible_outgoing")
        self.assertEqual(harness.state["return_key_count"], 1)
        self.assertEqual(harness.state["after_return_calls"], 0)
        self.assertEqual(harness.state["identity_checks"], 2)
        self.assertEqual(len(set(harness.state["identity_app_ids"])), 1)
        timing = harness.state["timing_logs"][-1][1]
        self.assertEqual(timing["candidate_reads"], 2)
        self.assertIn("candidate_confirm_read_ms", timing)

    def test_candidate_disappearing_between_identity_checks_is_not_confirmed(self):
        old = [("此前可见消息", (0, 0, 600, 50))]
        candidate = old + [("我的回复", (0, 1, 600, 50))]
        harness = SendReplyHarness(post_read_sequence=[candidate, old])
        self.assertEqual(harness.send(), "unconfirmed:no_new_rows")
        self.assertEqual(harness.state["return_key_count"], 1)
        self.assertEqual(harness.state["identity_checks"], 2)
        self.assertEqual(harness.state["after_return_calls"], 1)
        timing = harness.state["timing_logs"][-1][1]
        self.assertEqual(timing["candidate_reads"], 2)

    def test_transient_get_app_none_uses_live_prior_proxy_with_bracketed_proof(self):
        old = [("此前可见消息", (0, 0, 600, 50))]
        candidate = old + [("我的回复", (0, 1, 600, 50))]
        harness = SendReplyHarness(post_get_app_sequence=[None], post_rows=candidate)
        self.assertEqual(harness.send(), "visible_outgoing")
        self.assertEqual(harness.state["return_key_count"], 1)
        self.assertEqual(harness.state["identity_checks"], 2)
        self.assertEqual(len(set(harness.state["identity_app_ids"])), 1)
        self.assertEqual(harness.state["post_get_app_calls"], 1)
        timing = harness.state["timing_logs"][-1][1]
        self.assertEqual(timing["prior_proxy_reads"], 1)
        self.assertEqual(timing["retry_wait_ms"], 0)
        self.assertEqual(harness.state["after_return_calls"], 0)

    def test_prior_proxy_wait_is_bounded_when_candidate_appears_on_nth_none(self):
        old = [("此前可见消息", (0, 0, 600, 50))]
        candidate = old + [("我的回复", (0, 1, 600, 50))]
        harness = SendReplyHarness(
            post_get_app_sequence=[None, None],
            post_read_sequence=[old, candidate])
        self.assertEqual(harness.send(), "visible_outgoing")
        self.assertEqual(harness.state["post_get_app_calls"], 2)
        self.assertEqual(harness.state["return_key_count"], 1)
        self.assertLessEqual(harness.state["clock"], 0.31)

    def test_prior_proxy_failure_has_explicit_bounded_timeout(self):
        old = [("此前可见消息", (0, 0, 600, 50))]
        harness = SendReplyHarness(post_get_app_sequence=[None] * 20,
                                   post_rows=old)
        self.assertEqual(harness.send(), "unconfirmed:no_new_rows")
        self.assertEqual(harness.state["return_key_count"], 1)
        fields = harness.state["timing_logs"][-1][1]
        self.assertLessEqual(fields["retry_wait_ms"], 350)
        self.assertLessEqual(harness.state["clock"], 0.61)

    def test_one_final_check_uses_after_return_delay_without_another_wait(self):
        old = [("此前可见消息", (0, 0, 600, 50))]
        candidate = old + [("我的回复", (0, 1, 600, 50))]
        harness = SendReplyHarness(
            post_rows=old, post_read_sequence=[old] * 4,
            after_return_rows=candidate, after_return_delay=4.6,
            sleep_scale=2.0)

        self.assertEqual(harness.send(), "visible_outgoing")
        self.assertEqual(harness.state["return_key_count"], 1)
        self.assertEqual(harness.state["submit_count"], 1)
        self.assertEqual(harness.state["post_read_index"], 4)
        self.assertEqual(harness.state["post_read_after_guard"], 2)
        self.assertEqual(harness.state["identity_checks"], 2)
        self.assertEqual(harness.state["after_return_calls"], 1)
        self.assertEqual(harness.state["sleeps_after_guard"], 0)
        fields = harness.state["timing_logs"][-1][1]
        self.assertTrue(fields["final_check_attempted"])
        self.assertEqual(fields["final_check_ms"], 0)
        self.assertEqual(fields["candidate_reads"], 6)

    def test_final_check_with_no_row_remains_unconfirmed_after_one_read(self):
        old = [("此前可见消息", (0, 0, 600, 50))]
        harness = SendReplyHarness(
            post_rows=old, post_read_sequence=[old] * 4,
            after_return_rows=old, after_return_delay=4.6,
            sleep_scale=2.0)

        self.assertEqual(harness.send(), "unconfirmed:no_new_rows")
        self.assertEqual(harness.state["return_key_count"], 1)
        self.assertEqual(harness.state["post_read_index"], 4)
        self.assertEqual(harness.state["post_read_after_guard"], 1)
        self.assertEqual(harness.state["sleeps_after_guard"], 0)
        fields = harness.state["timing_logs"][-1][1]
        self.assertTrue(fields["final_check_attempted"])
        self.assertEqual(fields["candidate_reads"], 5)

    def test_final_check_recovers_a_client_that_reappears_after_guard(self):
        old = [("此前可见消息", (0, 0, 600, 50))]
        candidate = old + [("我的回复", (0, 1, 600, 50))]
        harness = SendReplyHarness(defunct_proxy=True, post_rows=candidate, sleep_scale=2.0)

        def get_app():
            if not harness.state["returned"]:
                return harness.app
            return harness.other_app if harness.state["after_return_active"] else None

        harness.ns["get_app"] = get_app
        trace = "a" * 64
        self.assertEqual(harness.send(trace=trace), "visible_outgoing")
        self.assertEqual(harness.state["return_key_count"], 1)
        self.assertEqual(harness.state["submit_count"], 1)
        self.assertEqual(harness.state["identity_app_ids"], [id(harness.other_app)] * 2)
        self.assertEqual(harness.state["post_read_after_guard"], 2)
        self.assertEqual(harness.state["sleeps_after_guard"], 0)
        fields = harness.state["timing_logs"][-1][1]
        self.assertEqual(fields["trace"], trace)
        self.assertEqual(fields["final_check_from"], "client_unavailable")

    def test_final_check_recovers_after_transient_message_read_failures(self):
        old = [("此前可见消息", (0, 0, 600, 50))]
        candidate = old + [("我的回复", (0, 1, 600, 50))]
        for reason in ("message_list_unavailable", "observation_error"):
            with self.subTest(reason=reason):
                harness = SendReplyHarness(post_rows=candidate, sleep_scale=2.0)

                def read_messages(app):
                    if harness.state["returned"] and not harness.state["after_return_active"]:
                        if reason == "observation_error":
                            raise RuntimeError("synthetic read failure")
                        return None, []
                    return harness._read_messages(app)

                harness.ns["read_messages"] = read_messages
                self.assertEqual(harness.send(trace="not-a-trace"), "visible_outgoing")
                self.assertEqual(harness.state["return_key_count"], 1)
                self.assertEqual(harness.state["sleeps_after_guard"], 0)
                fields = harness.state["timing_logs"][-1][1]
                self.assertNotIn("trace", fields)
                self.assertEqual(fields["final_check_from"], reason)

    def test_final_check_does_not_accept_reappearing_wrong_target(self):
        old = [("此前可见消息", (0, 0, 600, 50))]
        candidate = old + [("我的回复", (0, 1, 600, 50))]
        harness = SendReplyHarness(defunct_proxy=True, post_rows=candidate, sleep_scale=2.0,
                                   identity_title_sequence=["其他群"])
        harness.ns["get_app"] = lambda: (
            harness.app if not harness.state["returned"] else
            harness.other_app if harness.state["after_return_active"] else None)
        self.assertEqual(harness.send(), "unconfirmed:title_mismatch")
        self.assertEqual(harness.state["return_key_count"], 1)
        self.assertEqual(harness.state["identity_checks"], 1)
        self.assertEqual(harness.state["post_read_after_guard"], 1)

    def test_after_return_guard_denial_blocks_the_final_check(self):
        old = [("此前可见消息", (0, 0, 600, 50))]
        candidate = old + [("我的回复", (0, 1, 600, 50))]
        harness = SendReplyHarness(
            after_return="deny", post_rows=old,
            post_read_sequence=[old] * 4, after_return_rows=candidate,
            sleep_scale=2.0)

        self.assertEqual(harness.send(), "unconfirmed:composer_not_empty")
        self.assertEqual(harness.state["return_key_count"], 1)
        self.assertEqual(harness.state["post_read_index"], 4)
        self.assertEqual(harness.state["post_read_after_guard"], 0)
        self.assertEqual(harness.state["sleeps_after_guard"], 0)
        fields = harness.state["timing_logs"][-1][1]
        self.assertFalse(fields["final_check_attempted"])
        self.assertEqual(fields["candidate_reads"], 4)

    def test_prior_proxy_defunct_never_confirms(self):
        rows = [("此前可见消息", (0, 0, 600, 50)),
                ("我的回复", (0, 1, 600, 50))]
        harness = SendReplyHarness(defunct_proxy=True,
                                   post_get_app_sequence=[None], post_rows=rows)
        self.assertEqual(harness.send(), "unconfirmed:client_unavailable")
        self.assertEqual(harness.state["identity_checks"], 0)
        self.assertEqual(harness.state["return_key_count"], 1)

    def test_prior_proxy_wrong_target_or_snapshot_switch_never_confirms(self):
        rows = [("此前可见消息", (0, 0, 600, 50)),
                ("我的回复", (0, 1, 600, 50))]
        wrong_before = SendReplyHarness(
            post_get_app_sequence=[None], post_rows=rows,
            identity_title_sequence=["其他群"])
        self.assertEqual(wrong_before.send(), "unconfirmed:title_mismatch")
        self.assertEqual(wrong_before.state["identity_checks"], 1)

        switched_after = SendReplyHarness(
            post_get_app_sequence=[None], post_rows=rows,
            identity_title_sequence=["测试群", "其他群"])
        self.assertEqual(switched_after.send(), "unconfirmed:target_changed")
        self.assertEqual(switched_after.state["identity_checks"], 2)
        self.assertEqual(len(set(switched_after.state["identity_app_ids"])), 1)

        wrong_selected = SendReplyHarness(
            post_get_app_sequence=[None], post_rows=rows,
            identity_selected_sequence=[False])
        self.assertEqual(wrong_selected.send(), "unconfirmed:target_row_mismatch")
        self.assertEqual(wrong_selected.state["identity_checks"], 1)

    def test_wrong_enumerated_snapshot_does_not_fall_back_to_prior_proxy(self):
        rows = [("此前可见消息", (0, 0, 600, 50)),
                ("我的回复", (0, 1, 600, 50))]
        harness = SendReplyHarness(post_get_app_sequence=["other"], post_rows=rows,
                                   identity_title_sequence=["其他群"])
        self.assertEqual(harness.send(), "unconfirmed:title_mismatch")
        self.assertEqual(harness.state["identity_checks"], 1)
        self.assertEqual(harness.state["timing_logs"][-1][1]["prior_proxy_reads"], 0)

    def test_send_reply_recovers_when_after_return_reader_raises(self):
        harness = SendReplyHarness(
            reader_throws=True, state_wait_enabled=True,
            post_rows=[("此前可见消息", (0, 0, 600, 50)),
                       ("我的回复", (0, 1, 600, 50))])
        self.assertEqual(harness.send(), "visible_outgoing")
        self.assertEqual(harness.state["return_key_count"], 1)
        self.assertEqual(harness.state["after_return_calls"], 0)

    def test_send_reply_recovers_when_after_return_guard_raises(self):
        old_rows = [("此前可见消息", (0, 1, 600, 50))]
        harness = SendReplyHarness(
            after_return="raise", post_rows=old_rows,
            post_read_sequence=[old_rows] * 4, sleep_scale=2.0)
        self.assertEqual(harness.send(), "unconfirmed:reader_unavailable")
        self.assertEqual(harness.state["after_return_calls"], 1)
        self.assertEqual(harness.state["post_read_after_guard"], 0)
        self.assertFalse(harness.state["timing_logs"][-1][1]["final_check_attempted"])

    def test_send_reply_sends_once_but_unconfirmed_without_baseline_rows(self):
        harness = SendReplyHarness(
            baseline_rows=[],
            post_rows=[("此前可见消息", (0, 0, 600, 50)),
                       ("我的回复", (0, 1, 600, 50))])
        self.assertEqual(harness.send(),
                         "unconfirmed:receipt_prior_viewport_unavailable")
        self.assertEqual(harness.state["submit_count"], 1)
        self.assertEqual(harness.state["return_key_count"], 1)

    def test_send_reply_does_not_reuse_preexisting_matching_bubble(self):
        old_match = [("我的回复", (0, 1, 600, 50))]
        harness = SendReplyHarness(baseline_rows=old_match, post_rows=old_match)
        self.assertEqual(harness.send(), "unconfirmed:no_new_rows")
        self.assertEqual(harness.state["return_key_count"], 1)

    def test_send_reply_sends_once_but_unconfirmed_without_baseline_bounds(self):
        harness = SendReplyHarness(
            baseline_bounds=None,
            post_rows=[("此前可见消息", (0, 0, 600, 50)),
                       ("我的回复", (0, 1, 600, 50))])
        harness.state["baseline_bounds"] = None
        self.assertEqual(harness.send(),
                         "unconfirmed:receipt_prior_viewport_unavailable")
        self.assertEqual(harness.state["submit_count"], 1)
        self.assertEqual(harness.state["return_key_count"], 1)

    def test_send_reply_baseline_exception_preserves_single_send(self):
        harness = SendReplyHarness(
            baseline_read_throws=True,
            post_rows=[("此前可见消息", (0, 0, 600, 50)),
                       ("我的回复", (0, 1, 600, 50))])
        self.assertEqual(harness.send(),
                         "unconfirmed:receipt_prior_viewport_unavailable")
        self.assertEqual(harness.state["submit_count"], 1)
        self.assertEqual(harness.state["return_key_count"], 1)

    def test_pre_return_reader_failure_clears_only_the_proven_bot_draft(self):
        for edited_by_user in (False, True):
            with self.subTest(edited_by_user=edited_by_user):
                harness = SendReplyHarness()
                def fail_validation(reader, *_args):
                    if edited_by_user:
                        reader.text = "synthetic user draft"
                    raise RuntimeError("synthetic draft validation failure")
                harness.ns["Atspi"].Text.get_text = fail_validation
                harness.guard.clear_proof = Mock(side_effect=lambda draft, _fp:
                    TxVerdict(harness.reader.text == draft))
                with self.assertRaisesRegex(RuntimeError, "draft validation failure"):
                    harness.send()
                harness.guard.clear_proof.assert_called_once_with(
                    "我的回复", "synthetic-fingerprint")
                self.assertEqual(harness.reader.text, "synthetic user draft" if edited_by_user else "")
                self.assertEqual(harness.state["return_key_count"], 0)

    def test_new_draft_after_baseline_is_preserved_and_never_sent(self):
        harness = SendReplyHarness()
        read = harness.ns["read_messages"]

        def read_with_operator_change(app):
            view = read(app)
            harness.reader.text = "operator changed draft"
            return view

        harness.ns["read_messages"] = read_with_operator_change
        self.assertEqual(harness.send(), "draft_mismatch")
        self.assertEqual(harness.state["return_key_count"], 0)
        self.assertEqual(harness.reader.text, "operator changed draft")

    def test_login_switch_before_return_prevents_submission(self):
        harness = SendReplyHarness()
        harness.ns["app_memory_account"] = lambda _app: "other-account"
        self.assertEqual(harness.send(memory_account="initial-account"), "memory_account_changed")
        self.assertEqual(harness.state["return_key_count"], 0)

    def test_unknown_return_submission_never_clears_the_draft_or_retries(self):
        harness = SendReplyHarness()
        harness.guard.clear_proof = Mock(return_value=TxVerdict(True))
        harness.ns["xi"].key = Mock(side_effect=RuntimeError("synthetic submit failure"))
        with self.assertRaisesRegex(RuntimeError, "submit failure"):
            harness.send()
        harness.guard.clear_proof.assert_not_called()
        self.assertEqual(harness.ns["xi"].key.call_count, 1)
        self.assertEqual(harness.reader.text, "我的回复")

    def test_send_reply_rejects_visible_row_after_target_changes(self):
        harness = SendReplyHarness(
            selected=False,
            post_rows=[("此前可见消息", (0, 0, 600, 50)),
                       ("我的回复", (0, 1, 600, 50))])
        result = harness.send()
        self.assertTrue(result.startswith("unconfirmed:"), result)
        self.assertNotIn("PRIVATE", result)
        self.assertEqual(harness.state["return_key_count"], 1)
        self.assertFalse(harness.state["timing_logs"][-1][1]["final_check_attempted"])

    def test_receipt_retries_missing_header_on_same_client_without_resending(self):
        rows = [("此前可见消息", (0, 0, 600, 50)), ("我的回复", (0, 1, 600, 50))]
        harness = SendReplyHarness(post_rows=rows, identity_title_sequence=[None])
        self.assertEqual(harness.send(), "visible_outgoing")
        timing = harness.state["timing_logs"][-1][1]
        self.assertTrue(timing["identity_before_retried"])
        self.assertEqual(timing["candidate_reads"], 2)
        self.assertEqual(harness.state["identity_checks"], 3)
        self.assertEqual(harness.state["return_key_count"], 1)

    def test_missing_header_retry_fails_closed_on_rebuild_or_wrong_title(self):
        rows = [("此前可见消息", (0, 0, 600, 50)), ("我的回复", (0, 1, 600, 50))]
        cases = (([None, None], [], "header_unreadable"),
                 ([None, "其他群"], [], "title_mismatch"),
                 ([None], ["same", "other"], "client_unavailable"),
                 ([None], [None, "other"], "header_unreadable"))
        for titles, apps, reason in cases:
            with self.subTest(titles=titles, apps=apps):
                harness = SendReplyHarness(post_rows=rows, identity_title_sequence=titles,
                                           post_get_app_sequence=apps)
                self.assertEqual(harness.send(), "unconfirmed:" + reason)
                self.assertEqual(harness.state["return_key_count"], 1)
                self.assertFalse(harness.state["timing_logs"][-1][1]["final_check_attempted"])

    def test_receipt_keeps_live_proxy_when_header_retry_cannot_enumerate_app(self):
        rows = [("此前可见消息", (0, 0, 600, 50)), ("我的回复", (0, 1, 600, 50))]
        harness = SendReplyHarness(post_rows=rows, identity_title_sequence=[None],
                                   post_get_app_sequence=["same", None])
        self.assertEqual(harness.send(), "visible_outgoing")
        self.assertTrue(harness.state["timing_logs"][-1][1]["identity_before_retried"])
        self.assertEqual(harness.state["identity_checks"], 3)
        self.assertEqual(len(set(harness.state["identity_app_ids"])), 1)
        self.assertEqual(harness.state["return_key_count"], 1)

    def test_unconfirmed_status_is_single_record_compatible_reason(self):
        harness = SendReplyHarness(post_rows=[("此前可见消息", (0, 0, 600, 50))])
        result = harness.send()
        self.assertRegex(result, r"^unconfirmed:[A-Za-z0-9_:-]{1,80}$")
        self.assertEqual(result, "unconfirmed:no_new_rows")
        captured = []
        record_ns = {"re": re, "log": lambda phase, **fields:
                     captured.append((phase, fields))}
        exec(compile(ast.Module(body=[_FUNCTIONS_BY_NAME["record"]],
                                type_ignores=[]), str(SOURCE), "exec"), record_ns)
        record_ns["record"](None, "confirmation", result, "synthetic-trace",
                            unknown=True)
        self.assertEqual(captured[0][1]["reason"], result)

    def test_post_return_timing_is_redacted_and_skips_identity_without_candidate(self):
        harness = SendReplyHarness(post_rows=[("此前可见消息", (0, 0, 600, 50))])
        self.assertEqual(harness.send(), "unconfirmed:no_new_rows")
        event, fields = harness.state["timing_logs"][-1]
        self.assertEqual(event, "send_receipt_timing")
        self.assertGreater(fields["candidate_reads"], 0)
        self.assertEqual(fields["identity_before_ms"], 0)
        self.assertEqual(fields["identity_after_ms"], 0)
        self.assertEqual(fields["candidate_confirm_read_ms"], 0)
        self.assertEqual(harness.state["identity_checks"], 0)
        self.assertEqual(fields["outcome"], "unconfirmed")
        self.assertNotIn("text", fields)
        self.assertNotIn("我的回复", repr(fields))

    def test_pre_return_stages_are_in_existing_single_redacted_receipt_log(self):
        harness = SendReplyHarness(
            post_rows=[("此前可见消息", (0, 0, 600, 50)),
                       ("我的回复", (0, 1, 600, 50))])
        self.assertEqual(harness.send(), "visible_outgoing")
        self.assertEqual(harness.state["return_key_count"], 1)
        self.assertEqual(harness.state["submit_count"], 1)
        self.assertEqual(len(harness.state["timing_logs"]), 1)
        event, fields = harness.state["timing_logs"][0]
        self.assertEqual(event, "send_receipt_timing")
        stage_fields = {
            "before_send_identity_ms", "composer_lookup_ms", "composer_setup_ms",
            "click_ms", "after_click_identity_ms", "post_click_validation_ms",
            "ledger_reservation_ms", "draft_settle_ms", "draft_validation_ms",
            "before_return_identity_ms", "receipt_baseline_ms", "pre_return_ms",
        }
        self.assertTrue(stage_fields.issubset(fields))
        self.assertTrue(all(isinstance(fields[key], int) and fields[key] >= 0
                            for key in stage_fields))
        self.assertGreaterEqual(fields["draft_settle_ms"], 200)
        self.assertGreaterEqual(fields["pre_return_ms"],
                                sum(fields[key] for key in stage_fields if key != "pre_return_ms"))
        self.assertNotIn("text", fields)
        self.assertNotIn("title", fields)
        self.assertNotIn("rect", fields)
        self.assertNotIn("我的回复", repr(fields))
        self.assertNotIn(harness.TARGET, repr(fields))


if __name__ == "__main__":
    unittest.main(verbosity=2)
