"""
scripts/evaluate_forecasts.py
==============================
Wave 6 — evaluate a fitted forecast artefact against a held-out
features+close CSV.

Prints IC, hit-rate-after-costs, and (for classification targets) a
calibration summary.
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
from models.forecasts.evaluate import (  # noqa: E402
    compute_calibration_summary,
    compute_hit_rate_after_costs,
    compute_information_coefficient,
)
from models.forecasts.infer_tabular import score_forecast  # noqa: E402
from models.forecasts.train_tabular import TrainedForecastModel  # noqa: E402


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Evaluate forecast artefact (Wave 6)")
    p.add_argument("--artefact", required=True)
    p.add_argument("--close", required=True)
    p.add_argument("--features", required=True)
    p.add_argument("--cost-bps", type=float, default=5.0,
                   help="Round-trip cost in bps for hit-rate computation")
    p.add_argument("--n-bins", type=int, default=10)
    return p.parse_args()


def main() -> int:
    args = _parse_args()
    art = TrainedForecastModel.load(args.artefact)
    close = pd.read_csv(args.close, index_col=0, parse_dates=True)["close"].astype(float)
    feats = pd.read_csv(args.features, index_col=0, parse_dates=True)
    common = close.index.intersection(feats.index)
    close, feats = close.loc[common], feats.loc[common]

    ds = build_forecast_dataset_from_close(
        close, feature_frame=feats, target_kind=art.target_kind, horizon=art.horizon
    )
    cols = [s.name for s in art.feature_specs]
    yhat = pd.Series(score_forecast(art, ds.X[cols]), index=ds.X.index)
    y = ds.y

    if art.is_classification:
        cal = compute_calibration_summary(y, yhat, n_bins=args.n_bins)
        print(f"calibration | ECE={cal.expected_calibration_error:.4f}")
        for c, mp, mo, n in zip(cal.bin_centers, cal.bin_means_predicted, cal.bin_means_observed, cal.bin_counts):
            print(f"  bin={c:.2f}  predicted={mp:.3f}  observed={mo:.3f}  n={n}")
    else:
        ic = compute_information_coefficient(y, yhat)
        cost = args.cost_bps / 10000.0
        hit = compute_hit_rate_after_costs(y, yhat, round_trip_cost=cost)
        print(f"IC={ic:.4f}  hit_rate@cost={hit:.3f}  cost_bps={args.cost_bps}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
