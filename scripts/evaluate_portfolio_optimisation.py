"""
scripts/evaluate_portfolio_optimisation.py
============================================
Wave 8 — apply the configured optimiser to a returns CSV and print
the resulting target weights + diagnostics.

Usage:
    python scripts/evaluate_portfolio_optimisation.py \\
        --returns data/research/returns_matrix.csv \\
        --config config/portfolio_optimisation.yaml \\
        --method hrp
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from portfolio.optimizers import PortfolioOptimisationConfig, optimize_weights  # noqa: E402


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Evaluate portfolio optimiser (Wave 8)")
    p.add_argument("--returns", required=True, help="CSV: rows=obs, cols=assets")
    p.add_argument("--config", default=None)
    p.add_argument(
        "--method",
        default=None,
        choices=["equal", "inverse_variance", "hrp", "cvar", "kelly"],
        help="Override the YAML method.",
    )
    p.add_argument("--expected-returns", default=None, help="optional CSV with one row of mu")
    return p.parse_args()


def main() -> int:
    args = _parse_args()
    cfg = PortfolioOptimisationConfig.load(args.config) if args.config else PortfolioOptimisationConfig()
    if args.method:
        cfg.method = args.method
    cfg.enabled = True  # enabling here is research-only, no live impact

    df = pd.read_csv(args.returns, index_col=0, parse_dates=True)
    R = df.to_numpy(dtype=float)

    mu = None
    if args.expected_returns:
        mu = pd.read_csv(args.expected_returns, index_col=0).iloc[:, 0].to_numpy(dtype=float)

    res = optimize_weights(returns=R, expected_returns=mu, config=cfg)
    print(f"method={res.method}  fallback={res.fallback}  diagnostics={res.diagnostics}")
    print("target weights:")
    for col, w in zip(df.columns, res.weights):
        print(f"  {col:<10} {w:.4f}")
    print(f"\nsum={float(np.sum(res.weights)):.6f}  gross={float(np.sum(np.abs(res.weights))):.6f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
