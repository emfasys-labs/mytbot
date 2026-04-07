#!/usr/bin/env python3
"""
Deterministic dev bootstrap for local/CI parity.

Usage:
  python scripts/setup_dev.py
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def _run(cmd: list[str]) -> None:
    print(">", " ".join(cmd))
    subprocess.run(cmd, check=True)


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    req = root / "requirements-dev.txt"
    if not req.exists():
        print(f"Missing {req}")
        return 1
    _run([sys.executable, "-m", "pip", "install", "--upgrade", "pip"])
    _run([sys.executable, "-m", "pip", "install", "-r", str(req)])
    print("Dev bootstrap complete.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
