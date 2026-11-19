from __future__ import annotations

import json
import statistics
import sys
import time
import tracemalloc
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from support import build_context, load_cases  # noqa: E402

from social_expression import StickerCandidate, decide, select_sticker  # noqa: E402
from social_expression.runtime_budget import PI3B_BUDGET, plan_optional_layers  # noqa: E402

CATALOG_PATH = ROOT / "fixtures" / "sticker_catalog.json"


def main() -> int:
    cases = load_cases()
    catalog_payload = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
    catalog = [StickerCandidate(**row) for row in catalog_payload["items"]]
    contexts = [build_context(case) for case in cases]

    # Static gate: within budget and deliberately over-budget fallback.
    within = plan_optional_layers(
        catalog_size=min(len(catalog), PI3B_BUDGET.max_catalog_items),
        gif_candidates=min(4, PI3B_BUDGET.max_gif_items),
        requested_max_chars=70,
        requested_bubbles=2,
    )
    over_catalog = plan_optional_layers(catalog_size=PI3B_BUDGET.max_catalog_items + 1)
    over_gif = plan_optional_layers(catalog_size=8, gif_candidates=PI3B_BUDGET.max_gif_items + 1)

    # Local timing only. This is NOT a Pi 3B measurement.
    for context in contexts:
        decide(context)
    samples = []
    start_all = time.perf_counter()
    tracemalloc.start()
    for _ in range(100):
        for context in contexts:
            t0 = time.perf_counter_ns()
            decision = decide(context)
            if decision.action == "reply" and decision.sticker_intent != "none":
                select_sticker(context, decision, catalog)
            samples.append((time.perf_counter_ns() - t0) / 1_000_000)
    current, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    wall = time.perf_counter() - start_all
    samples.sort()
    p50 = samples[len(samples) // 2]
    p95 = samples[int(len(samples) * 0.95)]

    report = {
        "environment": "Windows local",
        "pi3b": "NOT_RUN",
        "budget": {
            "version": PI3B_BUDGET.version,
            "style_max_ms": PI3B_BUDGET.style_max_ms,
            "sticker_max_ms": PI3B_BUDGET.sticker_max_ms,
            "total_max_ms": PI3B_BUDGET.total_max_ms,
            "max_catalog_items": PI3B_BUDGET.max_catalog_items,
            "max_gif_items": PI3B_BUDGET.max_gif_items,
            "max_style_chars": PI3B_BUDGET.max_style_chars,
            "max_bubbles": PI3B_BUDGET.max_bubbles,
            "module_memory_kb": PI3B_BUDGET.module_memory_kb,
        },
        "plans": {
            "within": within.as_dict(),
            "over_catalog": over_catalog.as_dict(),
            "over_gif": over_gif.as_dict(),
        },
        "local_only": {
            "cases_per_loop": len(contexts),
            "loops": 100,
            "decisions": len(samples),
            "wall_seconds": round(wall, 3),
            "p50_ms": round(p50, 4),
            "p95_ms": round(p95, 4),
            "max_ms": round(samples[-1], 4),
            "peak_kb": round(peak / 1024, 1),
        },
        "note": "Windows 本机仅用于 fallback 逻辑自检；Pi 3B CPU/内存 = NOT_RUN，不得当作生产数据。",
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
