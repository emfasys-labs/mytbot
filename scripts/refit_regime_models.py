"""
scripts/refit_regime_models.py
================================
Wave 4 — operator entry point for refitting the HMM regime classifier
from a CSV of features (one row per bar, columns matching
``regime_models.classifier.feature_names`` in the YAML).

Usage:
    python scripts/refit_regime_models.py \\
        --features data/research/regime_features.csv \\
        --out data/research/regime_classifier_v0.pkl
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

from risk.regime_models import HMMRegimeClassifier  # noqa: E402


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Refit regime classifier (Wave 4)")
    p.add_argument("--features", required=True)
    p.add_argument("--config", default="config/regime_models.yaml")
    p.add_argument("--out", required=True)
    return p.parse_args()


def main() -> int:
    args = _parse_args()
    cfg_raw = yaml.safe_load(Path(args.config).read_text(encoding="utf-8")) or {}
    section = (cfg_raw.get("regime_models") or {}).get("classifier", {})
    feature_names = tuple(section.get("feature_names") or ())
    n_states = int(section.get("n_states", 4))
    min_samples = int(section.get("min_samples", 60))
    seed = int(section.get("seed", 7))

    df = pd.read_csv(args.features, index_col=0, parse_dates=True)
    if feature_names:
        missing = [c for c in feature_names if c not in df.columns]
        if missing:
            raise SystemExit(f"feature columns missing from CSV: {missing}")
        X = df[list(feature_names)].to_numpy(dtype=float)
    else:
        feature_names = tuple(df.columns)
        X = df.to_numpy(dtype=float)

    clf = HMMRegimeClassifier(
        n_states=n_states,
        feature_names=feature_names,
        min_samples=min_samples,
        seed=seed,
    )
    clf.fit(X)
    if not clf.fitted_:
        print(f"refit | not enough rows ({len(X)} < {min_samples}) — no artefact written")
        return 1
    clf.save(args.out)
    print(f"refit | states={n_states} backend={clf.backend_} -> {args.out}")
    print(f"refit | label vocabulary: {sorted(set((clf.state_to_label_ or {}).values()))}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
