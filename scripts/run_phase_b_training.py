#!/usr/bin/env python3
"""
scripts/run_phase_b_training.py
=================================
Governed Phase B training run on REAL backfilled feature_snapshots.

Trains a TCN on a symbol's 2-year 1h history, lets the SHIPPED comparison
harness (compare_against_baseline: OOS mse-ratio AND hit-rate AND
cost-aware net P&L) decide honestly whether it beats the Ridge baseline,
and packages an artefact whose ``deep_beats_baseline`` is set ONLY by that
verdict. The artefact is saved for INSPECTION; it is NOT registered in the
model registry and ``forecast_bridge`` stays disabled — so this run cannot
influence any trade. Promotion remains the governed, soak-gated step.

Feature contract: the trained ``feature_specs`` are the sorted numeric
feature-JSON keys (junk near-constant columns dropped). This matches what
the live loop attaches (``attach_forecast_sequence_history`` →
``df.select_dtypes('number')`` sorted) and what the bridge selects
(``_align_sequence_to_artefact``), so a future registered artefact aligns
at runtime by construction.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

import numpy as np
import pandas as pd
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

load_dotenv()

from models.deep_sequence.artefact import build_sequence_forecast_artefact  # noqa: E402
from models.deep_sequence.dataset import make_sequence_windows  # noqa: E402
from models.deep_sequence.train import (  # noqa: E402
    DeepSequenceConfig,
    train_deep_sequence_model,
)
from models.schemas import FeatureSpec  # noqa: E402

# Near-constant / non-informative columns dropped from the contract.
_DROP = {"dividends", "stock splits", "stock_splits"}


async def _load_symbol(symbol: str, timeframe: str) -> pd.DataFrame:
    from sqlalchemy import select
    from storage.db import init_async_database
    from storage.models import FeatureSnapshot

    engine, sm = await init_async_database()
    if sm is None:
        raise SystemExit("DB unavailable")
    async with sm() as s:
        rows = (
            await s.execute(
                select(
                    FeatureSnapshot.bar_timestamp,
                    FeatureSnapshot.close,
                    FeatureSnapshot.features,
                )
                .where(
                    FeatureSnapshot.symbol == symbol,
                    FeatureSnapshot.timeframe == timeframe,
                )
                .order_by(FeatureSnapshot.bar_timestamp.asc())
            )
        ).all()
    await engine.dispose()
    recs = []
    for ts, close, feats in rows:
        d = {"bar_timestamp": ts, "close": float(close or 0.0)}
        if isinstance(feats, dict):
            for k, v in feats.items():
                if isinstance(v, (int, float)) and k not in _DROP:
                    d[k] = float(v)
        recs.append(d)
    df = pd.DataFrame.from_records(recs)
    if df.empty:
        raise SystemExit(f"no rows for {symbol} {timeframe}")
    df["bar_timestamp"] = pd.to_datetime(df["bar_timestamp"], utc=True)
    return df.set_index("bar_timestamp").sort_index()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="BTC-USD")
    ap.add_argument("--timeframe", default="1h")
    ap.add_argument("--window", type=int, default=64)
    ap.add_argument("--horizon", type=int, default=1)
    ap.add_argument(
        "--out",
        default="artifacts/models/forecast/phase_b_seq_btc-0.1.0.pkl",
    )
    args = ap.parse_args()

    df = asyncio.run(_load_symbol(args.symbol, args.timeframe))
    # contract-aligned sorted numeric feature columns (exclude raw close;
    # close only drives the target).
    feat_cols = sorted(c for c in df.columns if c != "close")
    F = df[feat_cols].astype(float)
    # forward return target: y[t] = close[t+horizon]/close[t] - 1
    close = df["close"].astype(float)
    fwd = close.shift(-args.horizon) / close - 1.0
    fwd = fwd.replace([np.inf, -np.inf], np.nan)

    ds = make_sequence_windows(
        feature_frame=F,
        target=fwd,
        window=args.window,
        horizon=args.horizon,
        drop_na=True,
    )
    print(
        f"phase_b | {args.symbol} {args.timeframe} | rows={len(df)} "
        f"features={len(feat_cols)} window={args.window} horizon={args.horizon}"
    )
    print(
        f"phase_b | sequence dataset: X={ds.X.shape} y={ds.y.shape}"
    )

    res = train_deep_sequence_model(
        dataset=ds,
        config=DeepSequenceConfig(enabled=True, architecture="tcn"),
        holdout_fraction=0.2,
    )
    print(f"phase_b | notes: {res.notes}")
    c = res.comparison
    if c is not None:
        print(
            "phase_b | OOS: n=%d mse_ratio=%.4f hit_deep=%.4f hit_base=%.4f "
            "net_pnl_deep=%.6f net_pnl_base=%.6f"
            % (
                c.n_oos, c.mse_ratio, c.hit_rate_deep, c.hit_rate_baseline,
                c.net_pnl_deep, c.net_pnl_baseline,
            )
        )
        if c.failures:
            print(f"phase_b | comparison failures: {c.failures}")
    print(f"phase_b | promote_eligible (harness verdict): {res.promote_eligible}")

    if res.deep_model is not None:
        fs = [FeatureSpec(name=n, dtype="float64") for n in feat_cols]
        art = build_sequence_forecast_artefact(
            res, feature_specs=fs,
            target_kind="forward_return", horizon=args.horizon,
        )
        outp = Path(args.out)
        outp.parent.mkdir(parents=True, exist_ok=True)
        art.save(outp)
        print(
            f"phase_b | artefact saved (INSPECTION ONLY, not registered): {outp}"
        )
        print(
            f"phase_b | deep_beats_baseline={art.metadata['deep_beats_baseline']} "
            "— NOT activated; forecast_bridge stays disabled until governed "
            "registration + paper soak."
        )
    print(
        "phase_b | VERDICT: "
        + (
            "earned promote-eligibility — still requires manual governed "
            "registration + soak before any live influence."
            if res.promote_eligible
            else "did NOT beat the baseline OOS — stays inert (the honest, "
            "expected outcome on first real training)."
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
