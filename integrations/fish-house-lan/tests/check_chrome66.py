#!/usr/bin/env python3
"""Strict Chrome 66 (WebView 66.0.3359.158) Static Syntax Gate for DaFeiYu Home v2.

Scans every JS, CSS, and HTML file under web/fish_house/home/ and home.html.
Any hit fails CI immediately so Archimedes (Android 9 WebView Chrome 66) never
suffers syntax crashes or CSS layout collapse.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent / "web" / "fish_house"

JS_BANNED = {
    "optional-chaining (?.)": r"\?\.",
    "nullish-coalescing (??)": r"\?\?",
    "arrow-function (=>)": r"=>",
    "const": r"\bconst\b",
    "let": r"\blet\b",
    "template-literal": r"`",
    "async/await": r"\b(async|await)\b",
    "spread/rest (...)": r"\.\.\.",
    "for-of": r"\bfor\s*\(\s*(var|let|const)?\s*\w+\s+of\b",
    "class-keyword": r"^\s*class\s+\w+",
}

CSS_BANNED = {
    "aspect-ratio": r"aspect-ratio",
    "inset": r"\binset\s*:",
    "min()": r"\bmin\s*\(",
    "max()": r"\bmax\s*\(",
    "clamp()": r"\bclamp\s*\(",
    "env()": r"\benv\s*\(",
    "backdrop-filter": r"backdrop-filter",
    "display:grid": r"\bdisplay\s*:\s*grid",
    "gap": r"(?<![a-zA-Z0-9_-])gap\s*:",
}


def strip_comments(text: str, kind: str) -> str:
    if kind == "js":
        # Strip block comments & single-line comments outside strings (conservative)
        text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
        text = re.sub(r"(?m)^\s*//[^\n]*$", "", text)
        text = re.sub(r"(?<!:)//[^\n\"']*$", "", text, flags=re.M)
    elif kind == "css":
        text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    else:
        text = re.sub(r"<!--.*?-->", "", text, flags=re.S)
    return text


def main() -> int:
    targets = [(ROOT / "home.html", "html", {})]
    for p in sorted((ROOT / "home" / "js").glob("*.js")):
        targets.append((p, "js", JS_BANNED))
    for p in sorted((ROOT / "home" / "css").glob("*.css")):
        targets.append((p, "css", CSS_BANNED))

    fail = 0
    for path, kind, banned in targets:
        raw = path.read_text(encoding="utf-8")
        cleaned = strip_comments(raw, kind)
        lines = cleaned.splitlines()
        rel = path.relative_to(ROOT)
        print(f"[CHECK] {rel} ({len(raw)} bytes)")
        for label, pat in banned.items():
            hits = []
            for idx, line in enumerate(lines, 1):
                if re.search(pat, line):
                    hits.append((idx, line.strip()[:90]))
            if hits:
                fail += 1
                print(f"  FAIL {label}: {hits[:5]}")

    print("CHROME66_GATE:", "FAIL" if fail else "PASS", f"({fail} violation(s))")
    return 1 if fail else 0


if __name__ == "__main__":
    sys.exit(main())
