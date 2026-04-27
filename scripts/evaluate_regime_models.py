"""
scripts/evaluate_regime_models.py
===================================
Wave 4 — load a fitted regime classifier and a feature CSV; print the
predicted label distribution and (optional) per-day labels.

Usage:
    python scripts/evaluate_regime_models.py \\
        --artefact data/research/regime_classifier_v0.pkl \\
        --features data/research/regime_features.csv \\
        --print-sequence
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from risk.regime_models import HMMRegimeClassifier  # noqa: E402


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Evaluate fitted regime classifier")
    p.add_argument("--artefact", required=True)
    p.add_argument("--features", required=True)
    p.add_argument("--print-sequence", action="store_true")
    return p.parse_args()


def main() -> int:
    args = _parse_args()
    clf = HMMRegimeClassifier.load(args.artefact)
    df = pd.read_csv(args.features, index_col=0, parse_dates=True)
    if clf.feature_names:
        df = df[list(clf.feature_names)]
    labels = clf.predict_sequence(df.to_numpy(dtype=float))
    counts = Counter(labels)
    total = max(1, len(labels))
    print(f"regime distribution over {total} rows:")
    for lbl, n in sorted(counts.items(), key=lambda kv: -kv[1]):
        print(f"  {lbl:<20} {n:>6}  {n / total * 100:5.1f}%")
    if args.print_sequence:
        for ts, lbl in zip(df.index, labels):
            print(f"{ts}  {lbl}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
