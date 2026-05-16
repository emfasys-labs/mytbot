"""Run the standard Phase C shadow status bundle."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


COMMANDS = [
    ["python", "scripts/phase_c_readiness.py"],
    ["python", "scripts/report_phase_c_transition.py"],
    ["python", "scripts/evaluate_phase_c_transition_history.py"],
    ["python", "scripts/sweep_phase_c_allocator_policy.py"],
    ["python", "scripts/export_phase_c_shadow_history.py", "--prefix", "latest_phase_c_shadow_history"],
]


def run_bundle(commands: list[list[str]] = COMMANDS) -> int:
    rc = 0
    for cmd in commands:
        print("\n$ " + " ".join(cmd))
        proc = subprocess.run(cmd, cwd=ROOT, check=False)
        if proc.returncode != 0:
            rc = proc.returncode if rc == 0 else rc
    return rc


def main() -> int:
    return run_bundle()


if __name__ == "__main__":
    raise SystemExit(main())
