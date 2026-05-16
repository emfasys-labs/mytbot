"""Run the standard Phase D microstructure shadow status bundle."""

from __future__ import annotations

import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

COMMANDS = [
    ["python", "scripts/report_phase_d_microstructure_shadow.py", "--limit", "100"],
    ["python", "scripts/report_phase_d_execution_outcomes.py", "--limit", "250"],
]


def run_bundle(commands: list[list[str]] = COMMANDS) -> int:
    rc = 0
    for cmd in commands:
        print("\n$ " + " ".join(cmd))
        proc = subprocess.run(cmd, cwd=ROOT, check=False)
        if proc.returncode != 0 and rc == 0:
            rc = proc.returncode
    return rc


def main() -> int:
    return run_bundle()


if __name__ == "__main__":
    raise SystemExit(main())
