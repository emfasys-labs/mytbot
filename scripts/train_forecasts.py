"""
scripts/train_forecasts.py
=============================
Wave 6 — train a single (target_kind, horizon) forecast artefact from a
features CSV and a close-price CSV.

Usage:
    python scripts/train_forecasts.py \\
        --close      data/research/close.csv \\
        --features   data/research/feats.csv \\
        --target     forward_return \\
        --horizon    4 \\
        --estimator  ridge \\
        --calibration none \\
        --out        artefacts/forecasts/forecast_return_4h-0.1.0.pkl
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from models.forecasts.dataset import build_forecast_dataset_from_close  # noqa: E402
from models.forecasts.train_tabular import train_forecast_model  # noqa: E402
from models.schemas import FeatureSpec  # noqa: E402


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Train a forecast artefact (Wave 6)")
    p.add_argument("--close", required=True, help="CSV with datetime index + 'close' column")
    p.add_argument("--features", required=True)
    p.add_argument("--target", required=True, choices=[
        "forward_return", "breakout_continuation", "mean_reversion_success",
        "realised_vol_forward", "drawdown_probability",
    ])
    p.add_argument("--horizon", type=int, required=True)
    p.add_argument("--estimator", default="ridge")
    p.add_argument("--calibration", default="none", choices=["none", "isotonic", "platt"])
    p.add_argument("--n-splits", type=int, default=5)
    p.add_argument("--embargo-bars", type=int, default=5)
    p.add_argument("--out", required=True)
    return p.parse_args()


def main() -> int:
    args = _parse_args()
    close = pd.read_csv(args.close, index_col=0, parse_dates=True)["close"].astype(float)
    feats = pd.read_csv(args.features, index_col=0, parse_dates=True)
    common = close.index.intersection(feats.index)
    close = close.loc[common]
    feats = feats.loc[common]

    ds = build_forecast_dataset_from_close(
        close, feature_frame=feats, target_kind=args.target, horizon=args.horizon
    )
    feature_specs = [FeatureSpec(name=c, dtype=str(feats[c].dtype)) for c in ds.feature_columns]
    artefact, report = train_forecast_model(
        dataset=ds,
        feature_specs=feature_specs,
        estimator=args.estimator,
        calibration=args.calibration,
        n_splits=args.n_splits,
        embargo_bars=args.embargo_bars,
    )
    artefact.save(args.out)
    print(report.summary())
    print(f"\nartefact written to: {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
