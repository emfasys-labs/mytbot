"""
scripts/evaluate_factor_sleeve.py
==================================
Wave 3 — evaluate a factor snapshot CSV (output of
``build_factor_dataset.py``) against the configured blend, print the
top/bottom-N composite scorers and a per-family breakdown.

Read-only — does not register, save, or trade.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from signals.factor_scoring import composite_factor_score  # noqa: E402
from strategies.factor_sleeve import FactorSleeveConfig  # noqa: E402


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Evaluate factor sleeve snapshot")
    p.add_argument("--snapshot", required=True, help="CSV from build_factor_dataset.py")
    p.add_argument("--config", default="config/factor_sleeve.yaml")
    p.add_argument("--top", type=int, default=10)
    p.add_argument("--bottom", type=int, default=0)
    return p.parse_args()


def main() -> int:
    args = _parse_args()
    df = pd.read_csv(args.snapshot)
    cfg = FactorSleeveConfig.load(args.config)

    factor_cols = [c for c in df.columns if c not in ("symbol", "asset_class")]
    per_symbol = {
        row["symbol"]: {c: (None if pd.isna(row[c]) else float(row[c])) for c in factor_cols}
        for _, row in df.iterrows()
    }
    groups = (
        {row["symbol"]: str(row["asset_class"]) for _, row in df.iterrows()}
        if cfg.neutralise_by_asset_class
        else None
    )
    scores = composite_factor_score(
        per_symbol_factors=per_symbol,
        blend=cfg.blend,
        groups=groups,
        treat_missing=cfg.treat_missing,
    )
    print(f"composite scored {len(scores.composite)} symbols\n")
    print(f"TOP {args.top}:")
    for sym, z in scores.top_n(args.top):
        breakdown = {fam: round(scores.by_family[fam].get(sym, 0.0), 3) for fam in scores.by_family}
        print(f"  {sym:<10} z={z:+.3f}  families={breakdown}")
    if args.bottom > 0:
        print(f"\nBOTTOM {args.bottom}:")
        for sym, z in scores.bottom_n(args.bottom):
            breakdown = {fam: round(scores.by_family[fam].get(sym, 0.0), 3) for fam in scores.by_family}
            print(f"  {sym:<10} z={z:+.3f}  families={breakdown}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
