from __future__ import annotations

from dataclasses import dataclass
from typing import Tuple


@dataclass(frozen=True)
class OptionalLayerBudget:
    """Conservative caps used before enabling optional layers on Pi 3B.

    These are gating limits, not measured production timings. The actual Pi
    runtime measurement remains NOT_RUN until the main session executes it.
    """

    style_max_ms: float = 10.0
    sticker_max_ms: float = 15.0
    total_max_ms: float = 25.0
    max_catalog_items: int = 64
    max_gif_items: int = 16
    max_style_chars: int = 140
    max_bubbles: int = 2
    module_memory_kb: int = 4096
    version: str = "pi3b-budget-v1"

    def __post_init__(self) -> None:
        for name in ("style_max_ms", "sticker_max_ms", "total_max_ms"):
            value = getattr(self, name)
            if value <= 0:
                raise ValueError(name + " must be positive")
        for name in ("max_catalog_items", "max_gif_items", "max_style_chars",
                     "max_bubbles", "module_memory_kb"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(name + " must be a positive int")


PI3B_BUDGET = OptionalLayerBudget()


@dataclass(frozen=True)
class LayerPlan:
    """Which optional layers may run; false means use the existing core path."""

    style: bool
    sticker: bool
    gif: bool
    max_catalog_items: int
    max_gif_items: int
    reason_codes: Tuple[str, ...]

    def as_dict(self) -> dict:
        return {
            "style": self.style,
            "sticker": self.sticker,
            "gif": self.gif,
            "max_catalog_items": self.max_catalog_items,
            "max_gif_items": self.max_gif_items,
            "reason_codes": list(self.reason_codes),
        }


def plan_optional_layers(
    *,
    catalog_size: int,
    gif_candidates: int = 0,
    requested_max_chars: int = 140,
    requested_bubbles: int = 2,
    budget: OptionalLayerBudget = PI3B_BUDGET,
) -> LayerPlan:
    """Choose optional layers statically, before any expensive work.

    - Style is enabled only when the request fits the Pi persona caps.
    - Sticker rerank is enabled only for a non-empty catalog within the cap.
    - GIF selection is enabled only for a small candidate set.
    False results are fallbacks, not errors: the caller keeps its old path.
    """
    for name, value in (
        ("catalog_size", catalog_size),
        ("gif_candidates", gif_candidates),
        ("requested_max_chars", requested_max_chars),
        ("requested_bubbles", requested_bubbles),
    ):
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError(name + " must be a non-negative int")

    reasons = []
    style = requested_max_chars <= budget.max_style_chars and requested_bubbles <= budget.max_bubbles
    if not style:
        reasons.append("style_over_budget")

    sticker = 0 < catalog_size <= budget.max_catalog_items
    if catalog_size > budget.max_catalog_items:
        reasons.append("sticker_catalog_over_budget")

    gif = gif_candidates <= budget.max_gif_items
    if not gif:
        reasons.append("gif_candidates_over_budget")

    return LayerPlan(
        style=style,
        sticker=sticker,
        gif=gif,
        max_catalog_items=budget.max_catalog_items,
        max_gif_items=budget.max_gif_items,
        reason_codes=tuple(reasons),
    )
