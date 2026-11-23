"""Test the title and ambiguity proof without a running AT-SPI session."""
import ast
from pathlib import Path
import unittest

import tx_guard


def observe(labels, target="测试群"):
    path = Path(__file__).with_name("pi_bridge.py")
    source = ast.parse(path.read_text(encoding="utf-8"))
    fn = next(node for node in source.body
              if isinstance(node, ast.FunctionDef) and node.name == "_tx_header_info")
    showing_reads = []

    def showing(node):
        showing_reads.append(node[0])
        if isinstance(node[2], Exception):
            raise node[2]
        return node[2]

    class Pane:
        def get_child_count(self):
            return 1

        def get_child_at_index(self, _):
            return "header"

    namespace = {
        "messages_list": lambda _: "messages",
        "ext": lambda node: (200, 0, 400, 36) if node == "header" else (200, 36, 400, 400),
        "ancestor": lambda *_: Pane(),
        "walk": lambda _: ((node, node[3]) for node in labels),
        "role": lambda node: node[1],
        "nm": lambda node: node[0],
        "showing": showing,
        "tx_guard": tx_guard,
    }
    exec(compile(ast.Module(body=[fn], type_ignores=[]), str(path), "exec"), namespace)
    return namespace["_tx_header_info"]("app", target), showing_reads


class HeaderProofTests(unittest.TestCase):
    def test_only_target_labels_need_state_after_title(self):
        proof, reads = observe([
            ("搜索", "label", False, 1),
            ("测试群(5)", "label", True, 1),
            ("测试群", "label", True, 2),
            ("(5)", "label", False, 2),
            ("消息设置", "label", False, 1),
            ("菜单", "button", True, 1),
        ])
        self.assertEqual(proof, {"raw_title": "测试群(5)", "showing_target": 1,
                                 "ghost_labels": 0})
        self.assertEqual(reads, ["搜索", "测试群(5)", "测试群"])

    def test_keeps_first_visible_title_and_independent_target_counts(self):
        proof, reads = observe([
            ("  ", "label", True, 1),
            ("另一个会话", "label", True, 1),
            ("测试群", "label", False, 1),
            ("测试群（5）", "label", False, 2),
            ("测试群(4)", "label", True, 1),
            ("设置", "label", True, 1),
            ("测试群(6)", "label", True, 1),
        ])
        self.assertEqual(proof, {"raw_title": "另一个会话", "showing_target": 2,
                                 "ghost_labels": 1})
        self.assertNotIn("设置", reads)

    def test_unreadable_matching_state_is_not_treated_as_safe(self):
        with self.assertRaisesRegex(RuntimeError, "AT-SPI state"):
            observe([
                ("测试群", "label", True, 1),
                ("测试群(5)", "label", RuntimeError("AT-SPI state"), 1),
            ])


if __name__ == "__main__":
    unittest.main()
