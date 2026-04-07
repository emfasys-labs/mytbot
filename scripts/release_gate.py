#!/usr/bin/env python3
"""
Local release gate runner (mirrors the minimum CI checks).

Usage:
  python scripts/release_gate.py
  python scripts/release_gate.py --quick
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path


def _run(cmd: list[str], cwd: Path) -> int:
    print(">", " ".join(cmd))
    return subprocess.run(cmd, cwd=str(cwd)).returncode


def main() -> int:
    parser = argparse.ArgumentParser(description="Run release gate checks")
    parser.add_argument("--quick", action="store_true", help="Skip full test suite")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]

    checks: list[list[str]] = [
        [sys.executable, "scripts/check_docs_consistency.py"],
        [sys.executable, "-m", "compileall", "-q", "."],
    ]
    if not args.quick:
        checks.append([sys.executable, "-m", "pytest", "-q"])

    for cmd in checks:
        code = _run(cmd, root)
        if code != 0:
            print(f"Release gate failed on: {' '.join(cmd)}")
            return code
    print("Release gate PASSED.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
