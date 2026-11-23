"""Offline regressions for the managed WeChat AT-SPI registry hint."""
from __future__ import annotations

import builtins
import io
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from test_runtime_proactive_bridge import load_bridge


bridge = load_bridge()
UNIT = "dafeiyu-wechat-client.service"


class FakeApp:
    def __init__(self, name, pid, *, pid_error=False, name_errors=0,
                 on_name=None):
        self.name = name
        self.pid = pid
        self.pid_error = pid_error
        self.name_errors = name_errors
        self.on_name = on_name
        self.name_reads = 0
        self.pid_reads = 0

    def get_name(self):
        self.name_reads += 1
        if self.name_errors:
            self.name_errors -= 1
            raise RuntimeError("synthetic name read failure")
        if self.on_name is not None:
            callback, self.on_name = self.on_name, None
            callback()
        return self.name

    def get_process_id(self):
        self.pid_reads += 1
        if self.pid_error:
            raise RuntimeError("synthetic PID read failure")
        return self.pid


class FakeDesktop:
    def __init__(self, children):
        self.children = list(children)
        self.child_count_reads = 0
        self.index_reads = 0

    def get_child_count(self):
        self.child_count_reads += 1
        return len(self.children)

    def get_child_at_index(self, index):
        self.index_reads += 1
        return self.children[index]


class GetAppRegistryLocatorTests(unittest.TestCase):
    def setUp(self):
        bridge.get_app._registry_hint = None
        self.proc = {}
        self.proc_errors = set()

    def add_process(self, pid, *, managed=True, start="100"):
        path = ("/user.slice/user-1000.slice/user@1000.service/app.slice/" + UNIT
                if managed else "/user.slice/user-1000.slice/app.slice/dev-client.service")
        self.proc[pid] = (path, str(start))

    def proc_open(self, path, mode="r", encoding=None, errors=None):
        parts = path.replace("\\", "/").split("/")
        pid = int(parts[2]) if len(parts) > 2 else -1
        if pid in self.proc_errors or pid not in self.proc:
            raise OSError("synthetic procfs read failure")
        cgroup, start = self.proc[pid]
        if path.endswith("/cgroup"):
            return io.StringIO("0::" + cgroup + "\n")
        if path.endswith("/stat"):
            tail = ["S"] + ["0"] * 18 + [start]
            return io.StringIO(str(pid) + " (WeChat app) " + " ".join(tail) + "\n")
        raise FileNotFoundError(path)

    def invoke(self, desktop):
        atspi = SimpleNamespace(get_desktop=lambda index: desktop)
        with patch.object(bridge, "Atspi", atspi), \
             patch.object(builtins, "open", side_effect=self.proc_open):
            return bridge.get_app()

    def test_stable_managed_index_reads_only_candidate_name_after_first_lookup(self):
        before = FakeApp("Other", 101)
        wechat = FakeApp("WeChat", 202)
        after = FakeApp("Unrelated", 303)
        for pid in (101, 303):
            self.add_process(pid, managed=False)
        self.add_process(202)

        self.assertIs(self.invoke(FakeDesktop([before, wechat, after])), wechat)
        second_wechat = FakeApp("WeChat", 202)
        second = FakeDesktop([FakeApp("Other", 101), second_wechat,
                              FakeApp("Unrelated", 303)])
        self.assertIs(self.invoke(second), second_wechat)
        self.assertEqual(second.children[0].name_reads, 0)
        self.assertEqual(second.children[1].name_reads, 1)
        self.assertEqual(second.children[2].name_reads, 0)

    def test_order_and_count_changes_force_legacy_name_scan(self):
        first = FakeApp("Other", 101)
        wechat = FakeApp("wechat", 202)
        last = FakeApp("Another", 303)
        for pid in (101, 303):
            self.add_process(pid, managed=False)
        self.add_process(202)
        self.assertIs(self.invoke(FakeDesktop([first, wechat, last])), wechat)

        reordered_first = FakeApp("Moved to first", 303)
        reordered_wechat = FakeApp("wechat", 202)
        reordered_last = FakeApp("Moved to last", 101)
        reordered = FakeDesktop([reordered_first, reordered_wechat, reordered_last])
        self.assertIs(self.invoke(reordered), reordered_wechat)
        self.assertEqual(reordered_first.name_reads, 1)
        self.assertEqual(reordered_wechat.name_reads, 1)

        added = FakeApp("Added after reorder", 404)
        self.add_process(404, managed=False)
        expanded = FakeDesktop([reordered_first, reordered_wechat, reordered_last, added])
        self.assertIs(self.invoke(expanded), reordered_wechat)
        self.assertEqual(reordered_first.name_reads, 2)
        self.assertEqual(reordered_wechat.name_reads, 2)

    def test_same_pid_reuse_or_restarted_process_invalidates_hint(self):
        before = FakeApp("Other", 101)
        wechat = FakeApp("WeChat", 202)
        self.add_process(101, managed=False)
        self.add_process(202, start="100")
        self.assertIs(self.invoke(FakeDesktop([before, wechat])), wechat)

        self.add_process(202, start="101")
        new_before = FakeApp("Other", 101)
        replacement = FakeApp("WeChat", 202)
        changed = FakeDesktop([new_before, replacement])
        self.assertIs(self.invoke(changed), replacement)
        self.assertEqual(new_before.name_reads, 1)
        self.assertEqual(replacement.name_reads, 1)

        self.add_process(202, managed=False, start="102")
        next_before = FakeApp("Other", 101)
        unmanaged = FakeApp("WeChat", 202)
        self.assertIs(self.invoke(FakeDesktop([next_before, unmanaged])), unmanaged)
        self.assertEqual(next_before.name_reads, 1)

    def test_multiple_matching_clients_preserve_legacy_first_match(self):
        first = FakeApp("WeChat primary", 202)
        other = FakeApp("Other", 303)
        second = FakeApp("WeChat secondary", 404)
        self.add_process(202)
        self.add_process(303, managed=False)
        self.add_process(404)
        initial = FakeDesktop([first, other, second])
        self.assertIs(self.invoke(initial), first)
        self.assertEqual(second.name_reads, 0)

        fresh_first = FakeApp("WeChat primary", 202)
        fresh_other = FakeApp("Other", 303)
        fresh_second = FakeApp("WeChat secondary", 404)
        self.assertIs(self.invoke(FakeDesktop([fresh_first, fresh_other, fresh_second])),
                      fresh_first)
        self.assertEqual(fresh_first.name_reads, 1)
        self.assertEqual(fresh_other.name_reads, 0)
        self.assertEqual(fresh_second.name_reads, 0)

    def test_snapshot_swap_returns_legacy_match_without_learning_wrong_index(self):
        before = FakeApp("Other", 101)
        replacement = FakeApp("WeChat replacement", 202)
        desktop = FakeDesktop([])
        original = FakeApp("WeChat original", 202,
                           on_name=lambda: setattr(desktop, "children",
                                                   [before, replacement]))
        desktop.children = [before, original]
        self.add_process(101, managed=False)
        self.add_process(202)

        self.assertIs(self.invoke(desktop), original)
        self.assertIsNone(bridge.get_app._registry_hint)
        fresh_before = FakeApp("Other", 101)
        fresh_replacement = FakeApp("WeChat replacement", 202)
        self.assertIs(self.invoke(FakeDesktop([fresh_before, fresh_replacement])),
                      fresh_replacement)
        self.assertEqual(fresh_before.name_reads, 1)

    def test_duplicate_pid_snapshot_stays_on_legacy_path(self):
        self.add_process(101, managed=False)
        self.add_process(202)
        first = FakeApp("WeChat first", 202)
        second_same_pid = FakeApp("WeChat duplicate node", 202)
        for _ in range(2):
            other = FakeApp("Other", 101)
            self.assertIs(self.invoke(FakeDesktop([other, first, second_same_pid])), first)
            self.assertEqual(other.name_reads, 1)
        self.assertIsNone(bridge.get_app._registry_hint)
        self.assertEqual(first.name_reads, 2)

    def test_proc_or_pid_read_failure_uses_legacy_search_and_does_not_cache(self):
        before = FakeApp("Other", 101)
        wechat = FakeApp("WeChat", 202)
        self.add_process(101, managed=False)
        self.proc_errors.add(202)
        for _ in range(2):
            current_before = FakeApp("Other", 101)
            current_wechat = FakeApp("WeChat", 202)
            self.assertIs(self.invoke(FakeDesktop([current_before, current_wechat])),
                          current_wechat)
            self.assertEqual(current_before.name_reads, 1)
        self.assertIsNone(bridge.get_app._registry_hint)

        self.proc_errors.clear()
        self.add_process(202)
        broken_pid = FakeApp("WeChat", 202, pid_error=True)
        prior = FakeApp("Other", 101)
        self.assertIs(self.invoke(FakeDesktop([prior, broken_pid])), broken_pid)
        self.assertEqual(prior.name_reads, 1)
        self.assertIsNone(bridge.get_app._registry_hint)

    def test_cached_proc_or_pid_failure_forces_legacy_search(self):
        self.add_process(101, managed=False)
        self.add_process(202)
        self.assertEqual(self.invoke(FakeDesktop([
            FakeApp("Other", 101), FakeApp("WeChat", 202)] )).name, "WeChat")

        self.proc_errors.add(202)
        before_proc_error = FakeApp("Other", 101)
        after_proc_error = FakeApp("WeChat", 202)
        self.assertIs(self.invoke(FakeDesktop([before_proc_error, after_proc_error])),
                      after_proc_error)
        self.assertEqual(before_proc_error.name_reads, 1)
        self.assertIsNone(bridge.get_app._registry_hint)

        self.proc_errors.clear()
        self.assertEqual(self.invoke(FakeDesktop([
            FakeApp("Other", 101), FakeApp("WeChat", 202)] )).name, "WeChat")
        before_pid_error = FakeApp("Other", 101)
        after_pid_error = FakeApp("WeChat", 202, pid_error=True)
        self.assertIs(self.invoke(FakeDesktop([before_pid_error, after_pid_error])),
                      after_pid_error)
        self.assertEqual(before_pid_error.name_reads, 1)
        self.assertIsNone(bridge.get_app._registry_hint)

    def test_name_exception_falls_back_to_legacy_search(self):
        before = FakeApp("Other", 101)
        self.add_process(101, managed=False)
        self.add_process(202)
        first = FakeApp("WeChat", 202)
        self.assertIs(self.invoke(FakeDesktop([before, first])), first)

        before_again = FakeApp("Other", 101)
        transient = FakeApp("WeChat", 202, name_errors=1)
        self.assertIs(self.invoke(FakeDesktop([before_again, transient])), transient)
        self.assertEqual(before_again.name_reads, 1)
        self.assertEqual(transient.name_reads, 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
