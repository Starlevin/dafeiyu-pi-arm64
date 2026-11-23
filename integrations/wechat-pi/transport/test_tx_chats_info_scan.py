"""Regression tests for the read-only chat-sidebar state scan."""
from __future__ import annotations

from types import SimpleNamespace
import unittest
from unittest.mock import patch

from test_runtime_proactive_bridge import load_bridge


bridge = load_bridge()
SHOWING = "showing"
SELECTED = "selected"
TARGET = "目标群"


class FakeStateSet:
    def __init__(self, showing, selected=False, fail_on=None):
        self.values = {SHOWING: showing, SELECTED: selected}
        self.fail_on = fail_on
        self.calls = []

    def contains(self, state):
        self.calls.append(state)
        if state == self.fail_on:
            raise RuntimeError("fixture state fault")
        return self.values[state]


class FakeRow:
    def __init__(self, name, showing=True, selected=False, fail_on=None,
                 fail_get_state=False):
        self.name = name
        self.states = FakeStateSet(showing, selected, fail_on)
        self.get_state_calls = 0
        self.fail_get_state = fail_get_state

    def get_state_set(self):
        self.get_state_calls += 1
        if self.fail_get_state:
            raise RuntimeError("fixture state-set fault")
        return self.states

    def get_name(self):
        return self.name


class FakeListing:
    def __init__(self, rows):
        self.rows = rows

    def get_child_count(self):
        return len(self.rows)

    def get_child_at_index(self, index):
        return self.rows[index]


class TxChatsInfoScanTests(unittest.TestCase):
    def setUp(self):
        self.guard = SimpleNamespace(
            row_first_line=lambda value: value.split("\n", 1)[0].strip(),
            norm_target=lambda value: value.strip(),
        )

    def scan(self, rows):
        listing = FakeListing(rows)
        with patch.object(bridge, "Atspi", SimpleNamespace(StateType=SimpleNamespace(
                SHOWING=SHOWING, SELECTED=SELECTED))), \
             patch.object(bridge, "tx_guard", self.guard), \
             patch.object(bridge, "chats_list", return_value=listing):
            return bridge._tx_chats_info(object(), TARGET)

    def test_reads_one_state_set_per_row_and_preserves_visible_duplicate_counts(self):
        first = FakeRow(TARGET, showing=True, selected=False)
        selected_duplicate = FakeRow(TARGET + "\n预览", showing=True, selected=True)
        hidden_duplicate = FakeRow(TARGET, showing=False, selected=True)
        other = FakeRow("其他群", showing=True, selected=False)

        visible_matches, selected, selected_matches = self.scan(
            [None, first, selected_duplicate, hidden_duplicate, other])

        self.assertEqual((visible_matches, selected, selected_matches),
                         (2, [TARGET], 1))
        for row in (first, selected_duplicate, hidden_duplicate, other):
            self.assertEqual(row.get_state_calls, 1)
        self.assertEqual(first.states.calls, [SHOWING, SELECTED])
        self.assertEqual(selected_duplicate.states.calls, [SHOWING, SELECTED])
        self.assertEqual(hidden_duplicate.states.calls, [SHOWING])
        self.assertEqual(other.states.calls, [SHOWING, SELECTED])

    def test_state_set_or_state_read_exception_fails_closed(self):
        cases = [
            FakeRow(TARGET, fail_get_state=True),
            FakeRow(TARGET, showing=True, fail_on=SHOWING),
            FakeRow(TARGET, showing=True, selected=True, fail_on=SELECTED),
        ]
        for row in cases:
            with self.subTest(fail_on=row.states.fail_on,
                              fail_get_state=row.fail_get_state):
                self.assertEqual(self.scan([row]), (None, None, None))
                self.assertEqual(row.get_state_calls, 1)

    def test_preserves_multiple_visible_selected_rows_for_guard_to_reject(self):
        first = FakeRow(TARGET, showing=True, selected=True)
        second = FakeRow("另一群", showing=True, selected=True)

        visible_matches, selected, selected_matches = self.scan([first, second])

        self.assertEqual((visible_matches, selected, selected_matches),
                         (1, [TARGET, "另一群"], 1))
        self.assertEqual(first.get_state_calls, 1)
        self.assertEqual(second.get_state_calls, 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
