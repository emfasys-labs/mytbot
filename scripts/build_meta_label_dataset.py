"""
Build a leakage-safe meta-label training dataset from the local Postgres audit log.

The output CSVs are consumed by ``scripts/train_meta_labeler.py``. Features are
strictly as-of the signal timestamp; labels are generated from future bars using
the existing triple-barrier helper.
"""

from __future__ import annotations

import argparse
import asyncio
import bisect
import json
import sys
from collections import Counter
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import pandas as pd
from sqlalchemy import select

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    from dotenv import load_dotenv
except Exception:  # noqa: BLE001
    load_dotenv = None

from backtest.labels import TripleBarrierSpec, triple_barrier_labels  # noqa: E402
from storage.db import dispose_engine, init_async_database  # noqa: E402
from storage.models import FeatureSnapshot, SignalLog  # noqa: E402


FEATURE_COLUMNS = [
    "strategy_confidence",
    "raw_confidence",
    "side_sign",
    # v0.2.0 dedup fix: `news_score` removed from the feature set.
    # Empirically `sig.news_score` and `accumulator_score` are 0.97-correlated
    # in the live signal log because the accumulator's AI news rollup is what
    # populates both columns. Keeping both doubled the logreg's effective
    # weight on a single underlying signal. The accumulator carries the news
    # information; the standalone field can return when an independent
    # point-in-time AI news source is wired in.
    "accumulator_score",
    "accumulator_confidence",
    "atr_pct",
    "volume_z_score",
    "demand_score",
    "demand_confidence",
    "ai_macro_confidence",
    "rsi_14",
    "mom_10",
    "fracdiff_0_4",
    "garch_vol_1d",
    "hurst_dfa_128",
    "vpin_proxy_50",
    "relative_dollar_volume",
    "volume_z",
    "vol_ratio",
]


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Build meta-label CSVs from the audit DB")
    p.add_argument("--out-dir", default="data/research/meta_label")
    p.add_argument("--timeframe", default="1h")
    p.add_argument("--horizon-bars", type=int, default=10)
    p.add_argument("--pt-mult", type=float, default=2.0)
    p.add_argument("--sl-mult", type=float, default=1.5)
    p.add_argument("--vol-window", type=int, default=20)
    p.add_argument("--min-rows", type=int, default=200)
    return p.parse_args()


def _load_env() -> None:
    if load_dotenv is not None:
        load_dotenv(ROOT / ".env")


def _utc(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _as_float(value: Any, default: float = 0.0) -> float:
    if value is None:
        return default
    try:
        if isinstance(value, Decimal):
            return float(value)
        out = float(value)
        if out == float("inf") or out == float("-inf") or out != out:
            return default
        return out
    except (TypeError, ValueError):
        return default


def _side_sign(side: str) -> int:
    return 1 if str(side or "").strip().lower() in {"buy", "long"} else -1


def _feature_value(md: dict[str, Any], bar_features: dict[str, Any], key: str) -> float:
    if key in {"strategy_confidence", "raw_confidence", "side_sign"}:
        return 0.0
    if key in md:
        return _as_float(md.get(key))
    return _as_float(bar_features.get(key))


async def _load_rows(timeframe: str) -> tuple[list[FeatureSnapshot], list[SignalLog]]:
    engine, session_factory = await init_async_database()
    if session_factory is None:
        raise RuntimeError("database unavailable; check POSTGRES_* in .env")
    try:
        async with session_factory() as session:
            feature_rows = list(
                (
                    await session.execute(
                        select(FeatureSnapshot)
                        .where(FeatureSnapshot.timeframe == timeframe)
                        .order_by(FeatureSnapshot.symbol, FeatureSnapshot.bar_timestamp)
                    )
                )
                .scalars()
                .all()
            )
            signal_rows = list(
                (
                    await session.execute(
                        select(SignalLog).order_by(SignalLog.timestamp)
                    )
                )
                .scalars()
                .all()
            )
            return feature_rows, signal_rows
    finally:
        await dispose_engine(engine)


def _build_feature_index(
    feature_rows: list[FeatureSnapshot],
    spec: TripleBarrierSpec,
) -> dict[str, dict[str, Any]]:
    by_symbol: dict[str, list[FeatureSnapshot]] = {}
    for row in feature_rows:
        by_symbol.setdefault(str(row.symbol).upper(), []).append(row)

    out: dict[str, dict[str, Any]] = {}
    for symbol, rows in by_symbol.items():
        rows = sorted(rows, key=lambda r: _utc(r.bar_timestamp))
        idx = pd.DatetimeIndex([_utc(r.bar_timestamp) for r in rows])
        close = pd.Series([_as_float(r.close) for r in rows], index=idx)
        labels = triple_barrier_labels(close, spec)
        out[symbol] = {
            "rows": rows,
            "timestamps": list(idx),
            "labels": labels,
        }
    return out


def _dataset_rows(
    signal_rows: list[SignalLog],
    feature_index: dict[str, dict[str, Any]],
    *,
    horizon_bars: int,
) -> tuple[pd.DataFrame, pd.Series, dict[str, Any]]:
    x_rows: list[dict[str, float]] = []
    y_rows: list[int] = []
    row_index: list[str] = []
    skipped = Counter()
    by_strategy = Counter()
    labels = Counter()

    for sig in signal_rows:
        symbol = str(sig.symbol or "").upper()
        info = feature_index.get(symbol)
        if info is None:
            skipped["missing_symbol_features"] += 1
            continue
        timestamps: list[datetime] = info["timestamps"]
        pos = bisect.bisect_right(timestamps, _utc(sig.timestamp)) - 1
        if pos < 0:
            skipped["no_asof_feature"] += 1
            continue
        if pos + horizon_bars >= len(timestamps):
            skipped["insufficient_future_bars"] += 1
            continue
        bar = info["rows"][pos]
        bar_features = dict(bar.features or {})
        md = dict(sig.metadata_ or {})
        directional_label = int(info["labels"].iloc[pos])
        side = _side_sign(str(sig.side or ""))
        y = 1 if (side == 1 and directional_label == 1) or (side == -1 and directional_label == -1) else 0

        features = {k: _feature_value(md, bar_features, k) for k in FEATURE_COLUMNS}
        conf = _as_float(sig.confidence)
        features["strategy_confidence"] = conf
        features["raw_confidence"] = conf
        features["side_sign"] = float(side)

        x_rows.append(features)
        y_rows.append(y)
        row_index.append(_utc(sig.timestamp).isoformat())
        by_strategy[str(sig.strategy or "unknown")] += 1
        labels[str(y)] += 1

    X = pd.DataFrame(x_rows, index=pd.DatetimeIndex(row_index), columns=FEATURE_COLUMNS)
    y = pd.Series(y_rows, index=X.index, name="y", dtype=int)
    manifest = {
        "rows": int(len(X)),
        "feature_columns": list(FEATURE_COLUMNS),
        "label_counts": dict(labels),
        "strategy_counts": dict(by_strategy),
        "skipped": dict(skipped),
        "built_at": datetime.now(timezone.utc).isoformat(),
    }
    return X, y, manifest


async def _main() -> int:
    args = _parse_args()
    _load_env()
    out_dir = ROOT / args.out_dir
    out_dir.mkdir(parents=True, exist_ok=True)

    spec = TripleBarrierSpec(
        pt_mult=args.pt_mult,
        sl_mult=args.sl_mult,
        max_horizon=args.horizon_bars,
        vol_window=args.vol_window,
    )
    feature_rows, signal_rows = await _load_rows(args.timeframe)
    feature_index = _build_feature_index(feature_rows, spec)
    X, y, manifest = _dataset_rows(
        signal_rows,
        feature_index,
        horizon_bars=args.horizon_bars,
    )
    manifest.update(
        {
            "timeframe": args.timeframe,
            "horizon_bars": args.horizon_bars,
            "pt_mult": args.pt_mult,
            "sl_mult": args.sl_mult,
            "vol_window": args.vol_window,
            "feature_snapshot_rows": len(feature_rows),
            "signal_rows": len(signal_rows),
        }
    )
    if len(X) < args.min_rows:
        manifest["status"] = "insufficient_training_data"
        manifest["required_rows"] = int(args.min_rows)
        (out_dir / "manifest.json").write_text(
            json.dumps(manifest, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        print(
            f"insufficient_training_data: only {len(X)} leakage-safe rows built; "
            f"need at least {args.min_rows}"
        )
        return 2
    X.to_csv(out_dir / "features.csv")
    y.to_frame().to_csv(out_dir / "labels.csv")
    (out_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    print(json.dumps(manifest, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
