"""Read-only report for Phase E learned relational demand graph."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

DEFAULT_ARTIFACT = Path("artifacts/models/demand_graph/latest_phase_e_relational_graph.json")


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Phase E relational demand graph report")
    p.add_argument("--artifact", default=str(DEFAULT_ARTIFACT))
    p.add_argument("--top", type=int, default=20)
    return p.parse_args()


def summarize_graph(raw: dict[str, Any]) -> dict[str, Any]:
    edges = [e for e in (raw.get("edges") or []) if isinstance(e, dict)]
    weights = [abs(float(e.get("weight") or 0.0)) for e in edges]
    return {
        "version": raw.get("version"),
        "timeframe": raw.get("timeframe"),
        "symbols": len(raw.get("symbols") or []),
        "edges": len(edges),
        "max_abs_weight": max(weights) if weights else 0.0,
        "avg_abs_weight": sum(weights) / len(weights) if weights else 0.0,
        "metadata": dict(raw.get("metadata") or {}),
    }


def main() -> int:
    args = _parse_args()
    path = Path(args.artifact)
    if not path.exists():
        print(f"Phase E graph missing: {path}")
        return 1
    raw = json.loads(path.read_text(encoding="utf-8"))
    summary = summarize_graph(raw)
    print("Phase E relational demand graph:")
    print(f"  version={summary['version']}")
    print(f"  timeframe={summary['timeframe']}")
    print(f"  symbols={summary['symbols']}")
    print(f"  edges={summary['edges']}")
    print(f"  max_abs_weight={summary['max_abs_weight']:.4f}")
    print(f"  avg_abs_weight={summary['avg_abs_weight']:.4f}")
    md = summary["metadata"]
    print(f"  bars={md.get('bar_count')}")
    print(f"  min_overlap={md.get('min_overlap')}")
    print(f"  min_abs_lag_corr={md.get('min_abs_lag_corr')}")
    print("\nTop edges:")
    edges = sorted(
        [e for e in (raw.get("edges") or []) if isinstance(e, dict)],
        key=lambda e: abs(float(e.get("weight") or 0.0)) * float(e.get("confidence") or 0.0),
        reverse=True,
    )
    for e in edges[: max(1, int(args.top))]:
        print(
            "  "
            f"{e.get('source')} -> {e.get('target')} "
            f"weight={float(e.get('weight') or 0.0):.4f} "
            f"lag_corr={float(e.get('lag_correlation') or 0.0):.4f} "
            f"same_corr={float(e.get('same_bar_correlation') or 0.0):.4f} "
            f"conf={float(e.get('confidence') or 0.0):.4f} "
            f"obs={e.get('observations')}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
