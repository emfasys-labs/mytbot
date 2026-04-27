"""
scripts/train_meta_labeler.py
==============================
Wave 2 — operator entry point for training a meta-label artefact.

Minimal CLI: load a feature/label CSV (or build one from an OHLCV CSV
with a "side" column), train, save, print the eval report. Production
flows that pull from `feature_snapshots` will replace the CSV path; the
CSV mode keeps research workflows reproducible without a database.

Usage:
    python scripts/train_meta_labeler.py \\
        --features data/research/meta_features.csv \\
        --labels   data/research/meta_labels.csv \\
        --out      data/research/meta_label_v0.pkl \\
        --classifier logreg --calibration isotonic
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from models.meta_label import train_meta_label_model  # noqa: E402
from models.schemas import FeatureSpec  # noqa: E402


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Train a meta-label artefact (Wave 2)")
    p.add_argument("--features", required=True, help="CSV with feature columns + datetime index")
    p.add_argument("--labels", required=True, help="CSV with one column 'y' + matching index")
    p.add_argument("--out", required=True, help="Output pickle path for TrainedMetaLabel")
    p.add_argument("--classifier", default="logreg", choices=["logreg", "rf", "gbm", "xgb"])
    p.add_argument("--calibration", default="isotonic", choices=["none", "isotonic", "platt"])
    p.add_argument("--n-splits", type=int, default=5)
    p.add_argument("--embargo-bars", type=int, default=5)
    return p.parse_args()


def main() -> int:
    args = _parse_args()
    X = pd.read_csv(args.features, index_col=0, parse_dates=True)
    y = pd.read_csv(args.labels, index_col=0, parse_dates=True).iloc[:, 0].astype(int)
    common = X.index.intersection(y.index)
    X, y = X.loc[common], y.loc[common]

    feature_specs = [FeatureSpec(name=c, dtype=str(X[c].dtype)) for c in X.columns]
    artefact, report = train_meta_label_model(
        X=X,
        y=y,
        feature_specs=feature_specs,
        classifier=args.classifier,
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
