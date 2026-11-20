from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from social_expression.feature_flags import (
    SHADOW_ENV,
    STICKER_ENV,
    STYLE_ENV,
    describe,
    shadow_enabled,
    sticker_rerank_enabled,
    style_enabled,
)


class FeatureFlagTests(unittest.TestCase):
    def test_style_defaults_off(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            self.assertFalse(style_enabled())
            self.assertFalse(sticker_rerank_enabled())
            self.assertFalse(shadow_enabled())

    def test_explicit_values(self) -> None:
        with patch.dict(os.environ, {STYLE_ENV: "1", STICKER_ENV: "true", SHADOW_ENV: "1"}, clear=True):
            self.assertTrue(style_enabled())
            self.assertTrue(sticker_rerank_enabled())
            self.assertTrue(shadow_enabled())
            self.assertEqual(
                {"style_enabled": True, "sticker_rerank_enabled": True, "shadow_enabled": True},
                describe(),
            )

    def test_invalid_value_falls_back_to_default(self) -> None:
        with patch.dict(os.environ, {STYLE_ENV: "maybe"}, clear=True):
            self.assertFalse(style_enabled())


if __name__ == "__main__":
    unittest.main()
