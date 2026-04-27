"""
scripts/evaluate_meta_labeler.py
=================================
Wave 2 — load a trained meta-label artefact and an out-of-sample
features+labels CSV, then print calibration and per-regime breakdowns.

Usage:
    python scripts/evaluate_meta_labeler.py \\
        --artefact data/research/meta_label_v0.pkl \\
        --features data/research/oos_features.csv \\
        --labels   data/research/oos_labels.csv \\
        --regimes  data/research/oos_regimes.csv  # optional
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from models.meta_label import score_features  # noqa: E402
from models.meta_label.evaluate import evaluate_calibration, evaluate_per_regime  # noqa: E402
from models.meta_label.train import TrainedMetaLabel  # noqa: E402


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Evaluate a meta-label artefact (Wave 2)")
    p.add_argument("--artefact", required=True)
    p.add_argument("--features", required=True)
    p.add_argument("--labels", required=True)
    p.add_argument("--regimes", default=None, help="optional CSV with single column 'regime'")
    p.add_argument("--threshold", type=float, default=0.55)
    p.add_argument("--n-bins", type=int, default=10)
    return p.parse_args()


def main() -> int:
    args = _parse_args()
    art = TrainedMetaLabel.load(args.artefact)
    X = pd.read_csv(args.features, index_col=0, parse_dates=True)
    y = pd.read_csv(args.labels, index_col=0, parse_dates=True).iloc[:, 0].astype(int)
    common = X.index.intersection(y.index)
    X, y = X.loc[common], y.loc[common]

    p = pd.Series(score_features(art, X), index=X.index)

    cal = evaluate_calibration(y, p, n_bins=args.n_bins)
    print(f"calibration | ECE={cal.expected_calibration_error:.4f}")
    for c, mp, mo, n in zip(cal.bin_centers, cal.bin_means_predicted, cal.bin_means_observed, cal.bin_counts):
        print(f"  bin={c:.2f}  predicted={mp:.3f}  observed={mo:.3f}  n={n}")

    if args.regimes:
        regimes = pd.read_csv(args.regimes, index_col=0, parse_dates=True).iloc[:, 0]
        regimes = regimes.reindex(common)
        per = evaluate_per_regime(
            y_true=y, p_pred=p, group=regimes, threshold=args.threshold
        )
        print("\nper-regime:")
        print(per.to_string(index=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
