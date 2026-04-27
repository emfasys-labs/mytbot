"""
scripts/train_deep_sequence.py
================================
Wave 11 — train baseline + (optional) deep sequence model and run the
comparison harness.

Usage:
    python scripts/train_deep_sequence.py \\
        --features data/research/feats.csv \\
        --target   data/research/target.csv \\
        --window 64 --horizon 1 \\
        --architecture none \\
        --out-baseline artefacts/seq/baseline-0.1.0.pkl

When ``--architecture`` is ``tcn`` or ``tft`` and PyTorch is available,
the harness will (in a future build) train the deep model and compare.
For now those code paths surface a clear "not implemented" note and
return ``promote_eligible=False``.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from models.deep_sequence import (  # noqa: E402
    DeepSequenceConfig,
    make_sequence_windows,
    train_deep_sequence_model,
)
from models.schemas import FeatureSpec  # noqa: E402


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Train deep sequence model (Wave 11)")
    p.add_argument("--features", required=True)
    p.add_argument("--target", required=True)
    p.add_argument("--window", type=int, default=64)
    p.add_argument("--horizon", type=int, default=1)
    p.add_argument("--architecture", default="none", choices=["none", "tcn", "tft"])
    p.add_argument("--baseline-alpha", type=float, default=1.0)
    p.add_argument("--mse-ratio-threshold", type=float, default=0.95)
    p.add_argument("--hit-rate-margin", type=float, default=0.01)
    p.add_argument("--round-trip-cost-bps", type=float, default=5.0)
    p.add_argument("--holdout-fraction", type=float, default=0.2)
    p.add_argument("--out-baseline", default=None, help="optional pickle path for the trained baseline")
    return p.parse_args()


def main() -> int:
    args = _parse_args()
    feats = pd.read_csv(args.features, index_col=0, parse_dates=True)
    target = pd.read_csv(args.target, index_col=0, parse_dates=True).iloc[:, 0].astype(float)
    common = feats.index.intersection(target.index)
    feats = feats.loc[common]
    target = target.loc[common]

    ds = make_sequence_windows(
        feature_frame=feats, target=target, window=args.window, horizon=args.horizon
    )
    if len(ds.y) < 50:
        raise SystemExit(f"too few rows after windowing: {len(ds.y)}")

    cfg = DeepSequenceConfig(
        enabled=True,
        architecture=args.architecture,
        baseline_alpha=args.baseline_alpha,
        mse_ratio_threshold=args.mse_ratio_threshold,
        hit_rate_margin=args.hit_rate_margin,
        round_trip_cost_bps=args.round_trip_cost_bps,
    )
    feature_specs = [FeatureSpec(name=c, dtype=str(feats[c].dtype)) for c in feats.columns]

    res = train_deep_sequence_model(
        dataset=ds,
        config=cfg,
        feature_specs=feature_specs,
        holdout_fraction=args.holdout_fraction,
    )
    print(f"baseline: window={ds.window} n_features={len(ds.feature_names)}")
    print(f"  contract_hash={res.feature_contract_hash}")
    print(f"  notes={res.notes}")
    print(f"  metadata={res.metadata}")
    if res.comparison is not None:
        c = res.comparison
        print("comparison:")
        print(f"  n_oos={c.n_oos}")
        print(f"  mse_baseline={c.mse_baseline:.6f}  mse_deep={c.mse_deep:.6f}  ratio={c.mse_ratio:.3f}")
        print(f"  hit_rate_baseline={c.hit_rate_baseline:.3f}  hit_rate_deep={c.hit_rate_deep:.3f}")
        print(f"  net_pnl_baseline={c.net_pnl_baseline:+.6f}  net_pnl_deep={c.net_pnl_deep:+.6f}")
        print(f"  deep_beats_baseline={c.deep_beats_baseline}")
        if c.failures:
            print(f"  failures={c.failures}")
    print(f"\npromote_eligible={res.promote_eligible}")

    if args.out_baseline:
        res.baseline.save(args.out_baseline)
        print(f"baseline written to: {args.out_baseline}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
