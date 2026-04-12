#!/usr/bin/env python3
"""
Fail if milestone status in README and docs/BUILD_PLAN.md diverge.
"""

from __future__ import annotations

import re
from pathlib import Path


def _extract_readme_status(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in text.splitlines():
        if not line.strip().startswith("| M"):
            continue
        parts = [p.strip() for p in line.split("|")]
        if len(parts) < 5:
            continue
        milestone = parts[1]
        status = parts[3]
        if re.fullmatch(r"M(?:10|[1-9])", milestone):
            out[milestone] = status
    return out


def _extract_build_status(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in text.splitlines():
        if not line.strip().startswith("| M"):
            continue
        parts = [p.strip() for p in line.split("|")]
        if len(parts) < 5:
            continue
        milestone = parts[1]
        status = parts[4]
        if re.fullmatch(r"M(?:10|[1-9])", milestone):
            out[milestone] = status
    return out


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    readme = (root / "README.md").read_text(encoding="utf-8")
    build = (root / "docs" / "BUILD_PLAN.md").read_text(encoding="utf-8")
    a = _extract_readme_status(readme)
    b = _extract_build_status(build)
    milestones = [f"M{i}" for i in range(1, 11)]  # M1 … M10
    missing = [k for k in milestones if k not in a or k not in b]
    if missing:
        print(f"Missing milestone rows in docs: {missing}")
        return 1
    mismatches: list[str] = []
    for k in milestones:
        ra = "done" if "✅" in a[k] else "not_done"
        rb = "done" if "✅" in b[k] else "not_done"
        if ra != rb:
            mismatches.append(f"{k}: README={a[k]!r} BUILD_PLAN={b[k]!r}")
    if mismatches:
        print("Milestone status mismatch:")
        for x in mismatches:
            print(" -", x)
        return 2
    print("Docs consistency OK (README vs BUILD_PLAN milestones).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
