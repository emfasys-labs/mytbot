"""
scripts/evaluate_execution_quality.py
=======================================
Wave 9 — evaluate predicted vs realised execution quality from a fills
CSV.

Expected CSV columns (one row per fill):

    timestamp, broker, symbol, asset_class, side,
    quantity, daily_volume, daily_volatility,
    fee_bps, spread_bps, ref_price, fill_price

Outputs the mean / median / p95 of:
  * predicted impact bps (square-root model)
  * predicted total cost bps (fee + spread + impact)
  * realised slippage bps (|fill_price - ref_price| / ref_price * 10_000)
  * residual (realised - predicted)

The slippage prior model is then EWMA-updated from the same fills, and
the resulting per-(broker, symbol) priors are printed.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from statistics import mean, median

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from execution.impact import total_execution_cost_bps  # noqa: E402
from execution.slippage_model import SlippageModel  # noqa: E402


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Evaluate execution quality (Wave 9)")
    p.add_argument("--fills", required=True, help="CSV of fills (see module docstring)")
    return p.parse_args()


def _quantile(values: list[float], q: float) -> float:
    if not values:
        return float("nan")
    s = sorted(values)
    idx = max(0, min(len(s) - 1, int(round(q * (len(s) - 1)))))
    return float(s[idx])


def main() -> int:
    args = _parse_args()
    df = pd.read_csv(args.fills)

    impact_bps: list[float] = []
    total_bps: list[float] = []
    slippage_bps: list[float] = []
    residual: list[float] = []

    model = SlippageModel()

    for _, row in df.iterrows():
        ref = float(row["ref_price"])
        fill = float(row["fill_price"])
        slip = abs(fill - ref) / ref * 10_000.0 if ref > 0 else 0.0

        cost = total_execution_cost_bps(
            order_qty=float(row["quantity"]),
            daily_volume=float(row.get("daily_volume", 0.0)),
            daily_volatility=float(row.get("daily_volatility", 0.0)),
            asset_class=str(row.get("asset_class", "other")),
            fee_bps=float(row.get("fee_bps", 0.0)),
            spread_bps=float(row.get("spread_bps", 0.0)),
        )
        impact_bps.append(cost.impact_bps)
        total_bps.append(cost.total_bps)
        slippage_bps.append(slip)
        residual.append(slip - cost.total_bps)

        model.update(
            broker=str(row["broker"]),
            symbol=str(row.get("symbol", "")),
            asset_class=str(row.get("asset_class", "other")),
            observed_bps=slip,
        )

    def _summary(name: str, vals: list[float]) -> None:
        if not vals:
            return
        print(
            f"  {name:<14} mean={mean(vals):.2f}  median={median(vals):.2f}  "
            f"p95={_quantile(vals, 0.95):.2f}  p99={_quantile(vals, 0.99):.2f}  n={len(vals)}"
        )

    print("execution quality summary (bps):")
    _summary("predicted_impact", impact_bps)
    _summary("predicted_total", total_bps)
    _summary("realised_slip", slippage_bps)
    _summary("residual", residual)

    print("\nslippage priors snapshot:")
    snap = model.snapshot()
    by_bs = snap.get("by_broker_symbol", {}) or {}
    for k, v in list(by_bs.items())[:20]:
        print(f"  {k:<28}  {v[0]:.2f} bps  n={int(v[1])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
