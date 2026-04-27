"""
scripts/evaluate_pairs.py
===========================
Wave 5 — evaluate a single pair against a CSV of two close-price series.

Inputs:
    --leg-a <csv>  --leg-b <csv>  with datetime index + 'close' column.

Outputs Engle-Granger result, current Kalman β, OU half-life, latest
spread z-score, and the cost-aware entry threshold given the configured
``round_trip_cost_bps``.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd
import yaml

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from models.pairs.johansen import engle_granger_test  # noqa: E402
from models.pairs.kalman import KalmanHedgeRatio  # noqa: E402
from models.pairs.risk import (  # noqa: E402
    detect_correlation_decay,
    detect_spread_break,
    transaction_cost_aware_thresholds,
)
from models.pairs.spread import (  # noqa: E402
    compute_spread,
    half_life_ou,
    spread_zscore,
)


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Evaluate a pair (Wave 5)")
    p.add_argument("--leg-a", required=True)
    p.add_argument("--leg-b", required=True)
    p.add_argument("--config", default="config/pairs_trading.yaml")
    return p.parse_args()


def main() -> int:
    args = _parse_args()
    cfg_raw = yaml.safe_load(Path(args.config).read_text(encoding="utf-8")) or {}
    sa = cfg_raw.get("stat_arb_pairs") or {}

    a = pd.read_csv(args.leg_a, index_col=0, parse_dates=True)["close"].astype(float)
    b = pd.read_csv(args.leg_b, index_col=0, parse_dates=True)["close"].astype(float)

    eg = engle_granger_test(a, b)
    print(f"engle_granger | beta={eg.beta:.4f}  intercept={eg.intercept:.4f}")
    print(f"               adf_stat={eg.adf_stat:+.3f}  p~{eg.p_value_estimate:.3f}  "
          f"cointegrated_5pct={eg.is_cointegrated_5pct}")

    kf = KalmanHedgeRatio()
    params = kf.run(a, b)
    print(f"kalman        | beta_latest={params['beta'].iloc[-1]:.4f}  "
          f"intercept_latest={params['intercept'].iloc[-1]:.4f}")

    spread = compute_spread(a, b, beta=params["beta"], intercept=params["intercept"])
    z = spread_zscore(spread, window=int(sa.get("z_window", 60)))
    hl = half_life_ou(spread)
    print(f"spread        | sigma={float(spread.dropna().std(ddof=1)):.4f}  "
          f"latest_z={float(z.iloc[-1]) if len(z) else float('nan'):+.3f}  "
          f"half_life_bars={hl}")

    decayed, latest_corr = detect_correlation_decay(
        a, b, window=int(sa.get("correlation_window", 60)),
        floor=float(sa.get("correlation_floor", 0.5)),
    )
    print(f"correlation   | latest={latest_corr:+.3f}  decayed={decayed}")

    bk = detect_spread_break(
        z, spread,
        z_threshold=float(sa.get("z_threshold_break", 4.0)),
        half_life_ceiling_bars=float(sa.get("half_life_ceiling_bars", 200.0)),
        lookback=int(sa.get("z_window", 60)),
    )
    print(f"break_detect  | broken={bk.is_broken}  reason={bk.reason}")

    sigma = float(spread.dropna().std(ddof=1))
    entry, exit_ = transaction_cost_aware_thresholds(
        spread_sigma=sigma,
        round_trip_cost_bps=float(sa.get("round_trip_cost_bps", 10.0)),
        min_entry_z=float(sa.get("min_entry_z", 1.5)),
        safety_multiplier=float(sa.get("safety_multiplier", 1.2)),
    )
    print(f"thresholds    | entry_z={entry:.3f}  exit_z={exit_:.3f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
