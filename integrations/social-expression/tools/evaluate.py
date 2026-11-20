from __future__ import annotations

import json
import statistics
import sys
import time
import tracemalloc
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from support import build_context, load_cases  # noqa: E402

from social_expression import StickerCandidate, decide, select_sticker  # noqa: E402

CATALOG_PATH = ROOT / "fixtures" / "sticker_catalog.json"


def main() -> int:
    cases = load_cases()
    catalog_payload = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
    catalog = [StickerCandidate(**row) for row in catalog_payload["items"]]
    contexts = [build_context(case) for case in cases]

    decisions = [decide(context) for context in contexts]
    mismatches = []
    for case, decision in zip(cases, decisions):
        for key, expected in case["expected"].items():
            if getattr(decision, key) != expected:
                mismatches.append((case["id"], key, expected, getattr(decision, key)))

    # Warm up, then measure 200 decisions per case.
    for context in contexts:
        decide(context)
    samples = []
    start_all = time.perf_counter()
    for _ in range(200):
        for context in contexts:
            t0 = time.perf_counter_ns()
            decide(context)
            samples.append((time.perf_counter_ns() - t0) / 1_000_000)
    total_seconds = time.perf_counter() - start_all

    tracemalloc.start()
    for _ in range(50):
        for context in contexts:
            decide(context)
    current, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    sticker_count = 0
    sticker_ids = Counter()
    for context, decision in zip(contexts, decisions):
        if decision.action == "reply" and decision.sticker_intent != "none":
            selection = select_sticker(context, decision, catalog)
            if selection:
                sticker_count += 1
                sticker_ids[selection.item_id] += 1

    samples.sort()
    p50 = samples[len(samples) // 2]
    p95 = samples[int(len(samples) * 0.95)]
    report = {
        "cases": len(cases),
        "decision_mismatches": len(mismatches),
        "reply": sum(1 for decision in decisions if decision.action == "reply"),
        "silence": sum(1 for decision in decisions if decision.action == "silence"),
        "scenario_distribution": dict(Counter(decision.scenario for decision in decisions)),
        "silence_reasons": dict(Counter(
            reason for decision in decisions if decision.action == "silence"
            for reason in decision.reason_codes if reason.endswith("_guard") or reason.startswith("upstream_")
            or reason in ("low_relevance", "quiet_hours", "no_grounded_basis")
        )),
        "sticker_selected": sticker_count,
        "sticker_ids": dict(sticker_ids),
        "decisions_measured": len(samples),
        "wall_seconds": round(total_seconds, 3),
        "latency_ms": {
            "p50": round(p50, 4),
            "p95": round(p95, 4),
            "mean": round(statistics.fmean(samples), 4),
            "max": round(samples[-1], 4),
        },
        "memory_kb": {"current": round(current / 1024, 1), "peak": round(peak / 1024, 1)},
        "note": "Windows 本机测量；Pi 3B 未运行，报告需标注 NOT_RUN，不得当作生产耗时。",
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 1 if mismatches else 0


if __name__ == "__main__":
    raise SystemExit(main())
