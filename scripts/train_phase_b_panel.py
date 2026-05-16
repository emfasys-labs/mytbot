#!/usr/bin/env python3
"""Governed Phase B sequence-forecaster training over real feature history.

Research-only by design: this trains TCN candidates against the mandatory
Ridge baseline and writes reports/artefacts, but it never registers or
activates a model.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import sys
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from dotenv import load_dotenv
from sqlalchemy import select

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from models.deep_sequence.artefact import build_sequence_forecast_artefact  # noqa: E402
from models.deep_sequence.dataset import SequenceDataset, make_sequence_windows  # noqa: E402
from models.deep_sequence.train import DeepSequenceConfig, train_deep_sequence_model  # noqa: E402
from models.forecasts.targets import forward_return  # noqa: E402
from models.schemas import FeatureSpec  # noqa: E402
from run_m3 import _rows_to_features_frame  # noqa: E402
from storage.db import dispose_engine, init_async_database  # noqa: E402
from storage.models import FeatureSnapshot  # noqa: E402


DEFAULT_PANEL = (
    "BTC-USD",
    "ETH-USD",
    "SOL-USD",
    "SPY",
    "QQQ",
    "AAPL",
    "NVDA",
    "USDCAD=X",
)

DEFAULT_FEATURES = (
    "rsi_14",
    "MACD_12_26_9",
    "MACDh_12_26_9",
    "MACDs_12_26_9",
    "atr_14",
    "mom_10",
    "vol_ratio",
    "BBL_20_2.0",
    "BBM_20_2.0",
    "BBU_20_2.0",
    "BBB_20_2.0",
    "BBP_20_2.0",
    "vpin_proxy_50",
    "volume_z",
    "relative_dollar_volume",
    "trade_count_anomaly",
    "volume_persistence",
    "fake_spike_penalty",
    "fracdiff_0_4",
    "hurst_dfa_128",
    "garch_vol_1d",
)


def _run_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")


def _parse_symbols(raw: str | None) -> list[str]:
    if not raw:
        return list(DEFAULT_PANEL)
    return [s.strip().upper() for s in raw.split(",") if s.strip()]


async def _load_symbol_frame(
    session_factory: Any,
    *,
    symbol: str,
    timeframe: str,
    max_rows: int,
) -> pd.DataFrame:
    async with session_factory() as session:
        q = await session.execute(
            select(FeatureSnapshot)
            .where(FeatureSnapshot.symbol == symbol)
            .where(FeatureSnapshot.timeframe == timeframe)
            .order_by(FeatureSnapshot.bar_timestamp.desc())
            .limit(int(max_rows))
        )
        rows = list(q.scalars().all())
    rows.reverse()
    return _rows_to_features_frame(rows)


def build_phase_b_dataset(
    frame: pd.DataFrame,
    *,
    window: int,
    horizon: int,
    min_feature_coverage: float,
) -> tuple[SequenceDataset, pd.DataFrame, pd.Series]:
    """Build a production-compatible sequence dataset from feature snapshots."""
    if frame.empty or "close" not in frame.columns:
        raise ValueError("empty frame or missing close")
    raw_features = frame.copy()
    for col in raw_features.columns:
        raw_features[col] = pd.to_numeric(raw_features[col], errors="coerce")
    raw_features = raw_features.replace([np.inf, -np.inf], np.nan)

    available = [c for c in DEFAULT_FEATURES if c in raw_features.columns]
    if not available:
        raise ValueError("no configured numeric feature columns available")
    features = raw_features[available]
    min_non_null = max(1, int(math.ceil(len(features) * float(min_feature_coverage))))
    features = features.dropna(axis=1, thresh=min_non_null)
    if features.empty:
        raise ValueError("no feature columns passed coverage threshold")
    features = features.astype(float)
    target = forward_return(raw_features["close"].astype(float), horizon=horizon)
    common = features.index.intersection(target.index)
    features = features.loc[common]
    target = target.loc[common]
    ds = make_sequence_windows(
        feature_frame=features,
        target=target,
        window=window,
        horizon=horizon,
        drop_na=True,
    )
    return ds, features, target


def _comparison_to_dict(result: Any) -> dict[str, Any] | None:
    c = result.comparison
    if c is None:
        return None
    return {
        "n_oos": c.n_oos,
        "mse_baseline": c.mse_baseline,
        "mse_deep": c.mse_deep,
        "mse_ratio": c.mse_ratio,
        "hit_rate_baseline": c.hit_rate_baseline,
        "hit_rate_deep": c.hit_rate_deep,
        "hit_rate_margin_required": c.hit_rate_margin_required,
        "net_pnl_baseline": c.net_pnl_baseline,
        "net_pnl_deep": c.net_pnl_deep,
        "deep_beats_baseline": c.deep_beats_baseline,
        "failures": list(c.failures),
        "metadata": dict(c.metadata or {}),
    }


async def _run(args: argparse.Namespace) -> int:
    load_dotenv(ROOT / ".env")
    run_id = args.run_id or _run_id()
    symbols = _parse_symbols(args.symbols)
    report_dir = ROOT / "reports" / "models" / "phase_b_forecaster"
    artifact_dir = ROOT / "artifacts" / "models" / "forecast" / "phase_b" / run_id
    report_dir.mkdir(parents=True, exist_ok=True)
    artifact_dir.mkdir(parents=True, exist_ok=True)

    engine, session_factory = await init_async_database()
    if session_factory is None:
        raise SystemExit("database_unavailable")

    cfg = DeepSequenceConfig(
        enabled=True,
        architecture=args.architecture,
        baseline_alpha=args.baseline_alpha,
        mse_ratio_threshold=args.mse_ratio_threshold,
        hit_rate_margin=args.hit_rate_margin,
        round_trip_cost_bps=args.round_trip_cost_bps,
        epochs=args.epochs,
        batch_size=args.batch_size,
        patience=args.patience,
        learning_rate=args.learning_rate,
        seed=args.seed,
    )

    results: list[dict[str, Any]] = []
    try:
        for symbol in symbols:
            started = datetime.now(timezone.utc)
            print(f"TRAIN {symbol} timeframe={args.timeframe} window={args.window} horizon={args.horizon}", flush=True)
            item: dict[str, Any] = {
                "symbol": symbol,
                "timeframe": args.timeframe,
                "window": args.window,
                "horizon": args.horizon,
                "started_at": started.isoformat(),
                "status": "unknown",
            }
            try:
                frame = await _load_symbol_frame(
                    session_factory,
                    symbol=symbol,
                    timeframe=args.timeframe,
                    max_rows=args.max_rows,
                )
                item["rows_loaded"] = int(len(frame))
                if len(frame) < args.min_rows:
                    item.update(status="skipped", reason=f"insufficient_rows:{len(frame)}<{args.min_rows}")
                    print(f"SKIP  {symbol} {item['reason']}", flush=True)
                    results.append(item)
                    continue
                ds, features, _target = build_phase_b_dataset(
                    frame,
                    window=args.window,
                    horizon=args.horizon,
                    min_feature_coverage=args.min_feature_coverage,
                )
                item["samples"] = int(len(ds.y))
                item["feature_count"] = int(len(ds.feature_names))
                item["feature_names"] = list(ds.feature_names)
                if len(ds.y) < args.min_samples:
                    item.update(status="skipped", reason=f"insufficient_samples:{len(ds.y)}<{args.min_samples}")
                    print(f"SKIP  {symbol} {item['reason']}", flush=True)
                    results.append(item)
                    continue
                feature_specs = [FeatureSpec(name=c, dtype=str(features[c].dtype)) for c in ds.feature_names]
                result = train_deep_sequence_model(
                    dataset=ds,
                    config=cfg,
                    feature_specs=feature_specs,
                    holdout_fraction=args.holdout_fraction,
                )
                item["status"] = "trained"
                item["promote_eligible"] = bool(result.promote_eligible)
                item["feature_contract_hash"] = result.feature_contract_hash
                item["notes"] = result.notes
                item["training_metadata"] = dict(result.metadata or {})
                item["comparison"] = _comparison_to_dict(result)

                baseline_path = artifact_dir / f"{symbol.replace('/', '_')}-ridge.pkl"
                result.baseline.save(baseline_path)
                item["baseline_artifact"] = str(baseline_path.relative_to(ROOT))
                if result.deep_model is not None:
                    deep_path = artifact_dir / f"{symbol.replace('/', '_')}-tcn.pkl"
                    art = build_sequence_forecast_artefact(
                        result,
                        feature_specs=feature_specs,
                        target_kind="forward_return",
                        horizon=args.horizon,
                    )
                    art.save(deep_path)
                    item["deep_artifact"] = str(deep_path.relative_to(ROOT))
                verdict = "PASS" if result.promote_eligible else "FAIL"
                cmp_ = item.get("comparison") or {}
                print(
                    f"DONE  {symbol} {verdict} "
                    f"mse_ratio={cmp_.get('mse_ratio')} "
                    f"hit_deep={cmp_.get('hit_rate_deep')} "
                    f"net_deep={cmp_.get('net_pnl_deep')}",
                    flush=True,
                )
            except Exception as exc:  # noqa: BLE001
                item.update(status="error", reason=f"{exc.__class__.__name__}: {exc}")
                print(f"ERROR {symbol} {item['reason']}", flush=True)
            finally:
                item["finished_at"] = datetime.now(timezone.utc).isoformat()
                results.append(item)
    finally:
        await dispose_engine(engine)

    summary = {
        "run_id": run_id,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "config": {
            "symbols": symbols,
            "timeframe": args.timeframe,
            "window": args.window,
            "horizon": args.horizon,
            "architecture": args.architecture,
            "max_rows": args.max_rows,
            "min_rows": args.min_rows,
            "min_samples": args.min_samples,
            "holdout_fraction": args.holdout_fraction,
            "min_feature_coverage": args.min_feature_coverage,
            "training": asdict(cfg),
        },
        "summary": {
            "symbols_total": len(symbols),
            "trained": sum(1 for r in results if r.get("status") == "trained"),
            "skipped": sum(1 for r in results if r.get("status") == "skipped"),
            "errors": sum(1 for r in results if r.get("status") == "error"),
            "promote_eligible": sum(1 for r in results if r.get("promote_eligible") is True),
        },
        "results": results,
    }
    report_path = report_dir / f"{run_id}.json"
    report_path.write_text(json.dumps(summary, indent=2, sort_keys=True), encoding="utf-8")
    rows_path = report_dir / f"{run_id}.csv"
    pd.DataFrame(
        [
            {
                "symbol": r.get("symbol"),
                "status": r.get("status"),
                "rows_loaded": r.get("rows_loaded"),
                "samples": r.get("samples"),
                "feature_count": r.get("feature_count"),
                "promote_eligible": r.get("promote_eligible"),
                "mse_ratio": (r.get("comparison") or {}).get("mse_ratio"),
                "hit_rate_baseline": (r.get("comparison") or {}).get("hit_rate_baseline"),
                "hit_rate_deep": (r.get("comparison") or {}).get("hit_rate_deep"),
                "net_pnl_baseline": (r.get("comparison") or {}).get("net_pnl_baseline"),
                "net_pnl_deep": (r.get("comparison") or {}).get("net_pnl_deep"),
                "reason": r.get("reason"),
            }
            for r in results
        ]
    ).to_csv(rows_path, index=False)
    print(f"REPORT {report_path.relative_to(ROOT)}", flush=True)
    print(f"CSV    {rows_path.relative_to(ROOT)}", flush=True)
    return 0


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Train governed Phase B TCN panel")
    p.add_argument("--run-id", default=None)
    p.add_argument("--symbols", default=None, help="Comma-separated symbols; default curated panel")
    p.add_argument("--timeframe", default="1h")
    p.add_argument("--window", type=int, default=64)
    p.add_argument("--horizon", type=int, default=1)
    p.add_argument("--max-rows", type=int, default=6000)
    p.add_argument("--min-rows", type=int, default=1000)
    p.add_argument("--min-samples", type=int, default=500)
    p.add_argument("--min-feature-coverage", type=float, default=0.85)
    p.add_argument("--holdout-fraction", type=float, default=0.2)
    p.add_argument("--architecture", default="tcn", choices=["none", "tcn", "tft"])
    p.add_argument("--baseline-alpha", type=float, default=1.0)
    p.add_argument("--mse-ratio-threshold", type=float, default=0.95)
    p.add_argument("--hit-rate-margin", type=float, default=0.01)
    p.add_argument("--round-trip-cost-bps", type=float, default=5.0)
    p.add_argument("--epochs", type=int, default=40)
    p.add_argument("--batch-size", type=int, default=128)
    p.add_argument("--patience", type=int, default=6)
    p.add_argument("--learning-rate", type=float, default=1e-3)
    p.add_argument("--seed", type=int, default=17)
    return p.parse_args()


def main() -> int:
    return asyncio.run(_run(_parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
