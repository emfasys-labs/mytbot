"""
scripts/build_paper_soak_reports.py
======================================
Wave 14 — emit the six paper-soak markdown reports.

Pulls the Wave-13 dashboard payload (default funnel telemetry +
on-disk YAML state) and renders the per-section reports into
``reports/paper_soak/``.

Usage:
    python scripts/build_paper_soak_reports.py \\
        --model my_meta_labeler --model-version 0.1.0 \\
        --soak-started 2026-04-01T00:00:00+00:00 \\
        --out-dir reports/paper_soak
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from api.wave13_dashboard import build_wave13_payload  # noqa: E402
from system.paper_soak import build_paper_soak_report  # noqa: E402


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Build paper-soak reports (Wave 14)")
    p.add_argument("--model", default=None)
    p.add_argument("--model-version", default=None)
    p.add_argument("--soak-started", default=None, help="ISO timestamp")
    p.add_argument("--out-dir", default="reports/paper_soak")
    p.add_argument(
        "--rejection-breakdown",
        default=None,
        help="Optional JSON file mapping rejection reason → count",
    )
    p.add_argument("--drawdown-metrics", default=None, help="Optional JSON file with drawdown stats")
    return p.parse_args()


def _load_json_or_none(path: str | None):
    if not path:
        return None
    p = Path(path)
    if not p.exists():
        return None
    import json

    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def main() -> int:
    args = _parse_args()
    soak_started = (
        datetime.fromisoformat(args.soak_started)
        if args.soak_started
        else None
    )
    payload = build_wave13_payload()
    report = build_paper_soak_report(
        dashboard_payload=payload,
        model_name=args.model,
        model_version=args.model_version,
        soak_started_at=soak_started,
        risk_rejection_breakdown=_load_json_or_none(args.rejection_breakdown),
        drawdown_metrics=_load_json_or_none(args.drawdown_metrics),
    )

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    for filename, body in report.sections.items():
        (out_dir / filename).write_text(body, encoding="utf-8")
        print(f"wrote {out_dir / filename}")
    summary = (
        f"# Paper soak summary — {report.model_name or '(no model)'} "
        f"{report.model_version or ''}\n\n"
        f"- generated_at: {report.generated_at.isoformat()}\n"
        f"- soak_started_at: "
        f"{report.soak_started_at.isoformat() if report.soak_started_at else '—'}\n"
        f"- soak_days_elapsed: {report.soak_days_elapsed}\n"
    )
    (out_dir / "_summary.md").write_text(summary, encoding="utf-8")
    print(f"wrote {out_dir / '_summary.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
