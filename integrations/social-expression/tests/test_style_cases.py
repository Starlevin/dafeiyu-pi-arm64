from __future__ import annotations

import unittest

from support import build_context, load_cases

from social_expression import decide, style_prompt_constraints


class SyntheticStyleCases(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.cases = load_cases()

    def test_case_count_at_least_30(self) -> None:
        self.assertGreaterEqual(len(self.cases), 30)

    def test_every_case_matches_expectation(self) -> None:
        failures = []
        for case in self.cases:
            context = build_context(case)
            decision = decide(context)
            for key, expected in case["expected"].items():
                actual = getattr(decision, key)
                if actual != expected:
                    failures.append((case["id"], key, expected, actual))
            if decision.action == "reply":
                prompt = style_prompt_constraints(context, decision)
                self.assertIn("不改变人格", prompt)
                self.assertIn(str(decision.max_chars), prompt)
                if decision.sticker_intent == "none":
                    self.assertIn("不要配贴纸", prompt)
            else:
                self.assertEqual("", style_prompt_constraints(context, decision))
        self.assertEqual([], failures, "mismatches: " + repr(failures[:8]))

    def test_prompt_never_adds_identity_or_permissions(self) -> None:
        for case in self.cases:
            context = build_context(case)
            decision = decide(context)
            prompt = style_prompt_constraints(context, decision)
            for forbidden in ("你是真人", "冒充真人", "可以外发", "泄露密钥", "忽略安全"):
                self.assertNotIn(forbidden, prompt)


if __name__ == "__main__":
    unittest.main()
