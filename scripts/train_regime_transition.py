#!/usr/bin/env python3
"""Train Phase C regime-transition detector from historical feature snapshots.

Research/shadow only: writes artefact + report, never enables config.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from dotenv import load_dotenv
from sqlalchemy import text

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from data.regime_metrics import cross_section_from_feature_rows  # noqa: E402
from risk.regime_transition import RegimeTransitionDetector  # noqa: E402
from storage.db import dispose_engine, init_async_database  # noqa: E402


FEATURE_NAMES = (
    "trend_strength",
    "breadth_score",
    "market_state_score",
    "chaos_penalty",
    "volatility_structure",
    "anomaly_breadth",
    "correlation_crowding",
    "liquidity_state",
    "news_conflict_score",
)


def _run_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")


async def _load_history(session_factory: Any, *, symbols: list[str], timeframe: str, max_rows_per_symbol: int) -> pd.DataFrame:
    async with session_factory() as session:
        stmt = text(
            """
            WITH ranked AS (
              SELECT symbol, bar_timestamp, close, features,
                     row_number() OVER (PARTITION BY symbol ORDER BY bar_timestamp DESC) AS rn
              FROM feature_snapshots
              WHERE timeframe = :tf AND symbol = ANY(:symbols)
            )
            SELECT symbol, bar_timestamp, close, features
            FROM ranked
            WHERE rn <= :max_rows
            ORDER BY bar_timestamp ASC, symbol ASC
            """
        )
        rows = (await session.execute(stmt, {"tf": timeframe, "symbols": symbols, "max_rows": max_rows_per_symbol})).fetchall()
    return pd.DataFrame(
        [
            {
                "symbol": str(r.symbol),
                "bar_timestamp": r.bar_timestamp,
                "close": float(r.close),
                "features": dict(r.features or {}),
            }
            for r in rows
        ]
    )


def build_transition_dataset(
    history: pd.DataFrame,
    *,
    horizon: int,
    min_symbols_per_bar: int,
) -> tuple[pd.DataFrame, pd.Series, pd.DataFrame]:
    if history.empty:
        raise ValueError("empty history")
    pivot_close = history.pivot_table(index="bar_timestamp", columns="symbol", values="close", aggfunc="last").sort_index()
    panel_ret = pivot_close.pct_change(horizon).shift(-horizon)
    market_forward = panel_ret.mean(axis=1, skipna=True)
    abs_forward = market_forward.abs()
    down_thr = float(market_forward.quantile(0.20))
    vol_thr = float(abs_forward.quantile(0.80))

    rows: list[dict[str, Any]] = []
    labels: list[int] = []
    audit: list[dict[str, Any]] = []
    for ts, group in history.groupby("bar_timestamp", sort=True):
        if len(group) < min_symbols_per_bar:
            continue
        fwd = market_forward.get(ts)
        if fwd is None or pd.isna(fwd):
            continue
        feature_rows = [
            {"symbol": r.symbol, "features": dict(r.features or {}), "bar_timestamp": ts}
            for r in group.itertuples(index=False)
        ]
        raw = cross_section_from_feature_rows(
            feature_rows,
            anomaly_volume_z_threshold=1.25,
            anomaly_rel_dv_threshold=0.45,
        )
        trend = float(raw["trend_strength"])
        breadth = min(1.0, max(0.0, float(raw["risk_on_breadth"]) + float(raw["anomaly_breadth"]) * 0.5))
        market_state = (
            0.20 * trend
            + 0.15 * float(raw["cross_asset_confirmation"])
            + 0.10 * float(raw["liquidity_state"])
            + 0.12 * float(raw["risk_on_breadth"])
            - 0.08 * float(raw["chaos_penalty"])
            - 0.04 * float(raw["correlation_crowding"])
            + 0.12 * float(raw["volatility_structure"])
            + 0.10 * float(raw["anomaly_breadth"])
        )
        row = {
            "trend_strength": trend,
            "breadth_score": breadth,
            "market_state_score": market_state,
            "chaos_penalty": float(raw["chaos_penalty"]),
            "volatility_structure": float(raw["volatility_structure"]),
            "anomaly_breadth": float(raw["anomaly_breadth"]),
            "correlation_crowding": float(raw["correlation_crowding"]),
            "liquidity_state": float(raw["liquidity_state"]),
            "news_conflict_score": 0.0,
        }
        y = int(float(fwd) <= down_thr or abs(float(fwd)) >= vol_thr)
        rows.append(row)
        labels.append(y)
        audit.append(
            {
                "bar_timestamp": ts,
                "symbol_count": len(group),
                "forward_panel_return": float(fwd),
                "label": y,
            }
        )
    X = pd.DataFrame(rows, columns=FEATURE_NAMES)
    y = pd.Series(labels, name="stress_transition")
    return X, y, pd.DataFrame(audit)


def _metrics(y: np.ndarray, p: np.ndarray, *, threshold: float) -> dict[str, float | int]:
    pred = (p >= threshold).astype(int)
    tp = int(((pred == 1) & (y == 1)).sum())
    fp = int(((pred == 1) & (y == 0)).sum())
    tn = int(((pred == 0) & (y == 0)).sum())
    fn = int(((pred == 0) & (y == 1)).sum())
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    accuracy = (tp + tn) / len(y) if len(y) else 0.0
    out: dict[str, float | int] = {
        "n": int(len(y)),
        "positive_rate": float(y.mean()) if len(y) else 0.0,
        "accuracy": float(accuracy),
        "precision": float(precision),
        "recall": float(recall),
        "tp": tp,
        "fp": fp,
        "tn": tn,
        "fn": fn,
    }
    try:
        from sklearn.metrics import roc_auc_score

        out["roc_auc"] = float(roc_auc_score(y, p)) if len(set(y.tolist())) > 1 else 0.0
    except Exception:  # noqa: BLE001
        out["roc_auc"] = 0.0
    return out


async def _run(args: argparse.Namespace) -> int:
    load_dotenv(ROOT / ".env")
    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    run_id = args.run_id or _run_id()
    report_dir = ROOT / "reports" / "models" / "phase_c_regime_transition"
    artifact_dir = ROOT / "artifacts" / "models" / "regime_transition"
    report_dir.mkdir(parents=True, exist_ok=True)
    artifact_dir.mkdir(parents=True, exist_ok=True)

    engine, session_factory = await init_async_database()
    if session_factory is None:
        raise SystemExit("database_unavailable")
    try:
        hist = await _load_history(
            session_factory,
            symbols=symbols,
            timeframe=args.timeframe,
            max_rows_per_symbol=args.max_rows_per_symbol,
        )
    finally:
        await dispose_engine(engine)

    X, y, audit = build_transition_dataset(
        hist,
        horizon=args.horizon,
        min_symbols_per_bar=args.min_symbols_per_bar,
    )
    if len(X) < args.min_samples:
        raise SystemExit(f"insufficient_samples:{len(X)}<{args.min_samples}")
    cut = max(1, min(len(X) - 1, int(len(X) * (1.0 - args.holdout_fraction))))
    X_train, X_test = X.iloc[:cut], X.iloc[cut:]
    y_train, y_test = y.iloc[:cut], y.iloc[cut:]

    det = RegimeTransitionDetector(
        feature_names=FEATURE_NAMES,
        threshold=args.threshold,
        model_version=run_id,
        metadata={
            "phase": "C",
            "horizon": args.horizon,
            "timeframe": args.timeframe,
            "symbols": symbols,
            "shadow_only": True,
        },
    ).fit(X_train.to_numpy(), y_train.to_numpy(), l2=args.l2)
    probs = np.asarray([det.predict_probability(row) for row in X_test.to_numpy()])
    metrics = _metrics(y_test.to_numpy(dtype=int), probs, threshold=args.threshold)
    artifact = artifact_dir / f"regime_transition-{run_id}.pkl"
    det.metadata["oos_metrics"] = metrics
    det.save(artifact)

    report = {
        "run_id": run_id,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "artifact": str(artifact.relative_to(ROOT)),
        "config": vars(args),
        "dataset": {
            "rows": int(len(X)),
            "train_rows": int(len(X_train)),
            "test_rows": int(len(X_test)),
            "positive_rate": float(y.mean()),
            "feature_names": list(FEATURE_NAMES),
        },
        "oos_metrics": metrics,
        "promotion_note": "research_only_shadow_candidate; config/regime_models.yaml not modified",
    }
    report_path = report_dir / f"{run_id}.json"
    report_path.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    audit.tail(2000).to_csv(report_dir / f"{run_id}_audit_tail.csv", index=False)
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Train Phase C regime transition detector")
    p.add_argument("--run-id", default=None)
    p.add_argument("--symbols", default="SPY,QQQ,IWM,TLT,HYG,GLD,BTC-USD,ETH-USD,SOL-USD,NVDA,AAPL,MSFT,USDCAD=X,EURUSD=X")
    p.add_argument("--timeframe", default="1h")
    p.add_argument("--horizon", type=int, default=4)
    p.add_argument("--max-rows-per-symbol", type=int, default=6000)
    p.add_argument("--min-symbols-per-bar", type=int, default=6)
    p.add_argument("--min-samples", type=int, default=1000)
    p.add_argument("--holdout-fraction", type=float, default=0.25)
    p.add_argument("--threshold", type=float, default=0.60)
    p.add_argument("--l2", type=float, default=1.0)
    return p.parse_args()


def main() -> int:
    return asyncio.run(_run(_parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
