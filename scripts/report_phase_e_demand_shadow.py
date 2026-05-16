#!/usr/bin/env python3
"""Evaluate Phase E learned demand shadow against future panel returns."""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import math
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd
import yaml
from dotenv import load_dotenv
from sqlalchemy import text

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from storage.db import dispose_engine, init_async_database  # noqa: E402
from system.relational_demand_graph import RelationalDemandGraphArtifact  # noqa: E402

DEFAULT_ARTIFACT = ROOT / "artifacts" / "models" / "demand_graph" / "latest_phase_e_relational_graph.json"
DEFAULT_REPORT_DIR = ROOT / "reports" / "models" / "phase_e_demand_graph"


def _clip(x: float, lo: float = -1.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, float(x)))


def _corr(a: list[float], b: list[float]) -> float:
    if len(a) < 3 or len(a) != len(b):
        return 0.0
    sa = pd.Series(a, dtype="float64")
    sb = pd.Series(b, dtype="float64")
    val = sa.corr(sb)
    if val is None or pd.isna(val):
        return 0.0
    return float(val)


def _safe_mean(vals: list[float]) -> float:
    vals = [float(v) for v in vals if math.isfinite(float(v))]
    return float(sum(vals) / len(vals)) if vals else 0.0


def _load_demand_config(path: Path = ROOT / "config" / "strategies.yaml") -> dict[str, Any]:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception:  # noqa: BLE001
        return {}
    return dict(raw.get("demand_engine") or {})


def _static_cross_asset_score(
    rets: pd.Series,
    *,
    config: dict[str, Any] | None = None,
) -> tuple[float, float]:
    cfg = dict(config or {})
    risk_on = [str(x).strip().upper() for x in cfg.get("risk_on_anchors", ["SPY", "QQQ", "XLE", "BTC-USD"])]
    risk_off = [str(x).strip().upper() for x in cfg.get("risk_off_anchors", ["TLT", "GLD", "DXY"])]
    row = {str(k).strip().upper(): float(v) for k, v in rets.dropna().to_dict().items()}
    on = [row[s] for s in risk_on if s in row and math.isfinite(row[s])]
    off = [row[s] for s in risk_off if s in row and math.isfinite(row[s])]
    covered = len(on) + len(off)
    total = max(1, len(risk_on) + len(risk_off))
    spread = _safe_mean(on) - _safe_mean(off)
    scale = float(cfg.get("cross_asset_scale", 30.0) or 30.0)
    return _clip(spread * scale), covered / total


def _learned_score_for_row(
    rets: pd.Series,
    artifact: RelationalDemandGraphArtifact,
    *,
    scale: float,
) -> tuple[float, int, float]:
    row = {str(k).strip().upper(): float(v) for k, v in rets.dropna().to_dict().items()}
    terms: list[float] = []
    confs: list[float] = []
    for edge in artifact.edges:
        src = edge.source.upper()
        if src not in row or not math.isfinite(row[src]):
            continue
        terms.append(float(edge.weight) * row[src] * float(edge.confidence))
        confs.append(float(edge.confidence))
    raw = _safe_mean(terms)
    return _clip(raw * scale), len(terms), _safe_mean(confs)


def build_shadow_evidence(
    close: pd.DataFrame,
    artifact: RelationalDemandGraphArtifact,
    *,
    horizon: int = 1,
    scale: float = 30.0,
    demand_config: dict[str, Any] | None = None,
    min_edges_used: int = 1,
    signal_threshold: float = 0.01,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Build timestamp-level evidence for learned vs static demand shadows."""
    if close.empty or not artifact.edges:
        return {
            "observations": 0,
            "artifact_edges": len(artifact.edges),
            "recommendation": "do_not_promote",
            "reason": "no_close_history_or_edges",
        }, []

    close = close.sort_index()
    rets = close.pct_change().replace([float("inf"), float("-inf")], pd.NA)
    future = (close.shift(-max(1, int(horizon))) / close - 1.0).replace([float("inf"), float("-inf")], pd.NA)
    target_cols = [s for s in artifact.symbols if s in future.columns]
    if not target_cols:
        target_cols = list(future.columns)
    future_panel = future[target_cols].mean(axis=1, skipna=True)

    rows: list[dict[str, Any]] = []
    for ts, ret_row in rets.iterrows():
        future_ret = future_panel.loc[ts]
        if pd.isna(future_ret):
            continue
        learned, edges_used, avg_conf = _learned_score_for_row(ret_row, artifact, scale=scale)
        if edges_used < min_edges_used:
            continue
        static, static_coverage = _static_cross_asset_score(ret_row, config=demand_config)
        rows.append(
            {
                "timestamp": ts.isoformat() if hasattr(ts, "isoformat") else str(ts),
                "learned_score": float(learned),
                "static_cross_asset_score": float(static),
                "future_panel_return": float(future_ret),
                "edges_used": int(edges_used),
                "avg_edge_confidence": float(avg_conf),
                "static_coverage": float(static_coverage),
            }
        )

    learned_scores = [float(r["learned_score"]) for r in rows]
    static_scores = [float(r["static_cross_asset_score"]) for r in rows]
    future_returns = [float(r["future_panel_return"]) for r in rows]
    actionable = [r for r in rows if abs(float(r["learned_score"])) >= signal_threshold]
    learned_hits = [
        1.0
        for r in actionable
        if float(r["learned_score"]) * float(r["future_panel_return"]) > 0
    ]
    learned_ic = _corr(learned_scores, future_returns)
    static_ic = _corr(static_scores, future_returns)
    hit_rate = len(learned_hits) / len(actionable) if actionable else 0.0
    avg_edges = _safe_mean([float(r["edges_used"]) for r in rows])
    mean_abs_score = _safe_mean([abs(float(r["learned_score"])) for r in rows])

    promote_candidate = (
        len(rows) >= 500
        and len(artifact.edges) >= 10
        and avg_edges >= 3.0
        and abs(learned_ic) >= 0.03
        and hit_rate >= 0.52
        and len(actionable) >= 100
    )
    reason = "shadow_edge_not_yet_proven"
    if promote_candidate:
        reason = "meets_initial_shadow_evidence_thresholds_requires_soak_review"
    elif len(rows) < 500:
        reason = "insufficient_observations"
    elif abs(learned_ic) < 0.03:
        reason = "low_information_coefficient"
    elif hit_rate < 0.52:
        reason = "weak_directional_hit_rate"

    summary = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "artifact_version": artifact.version,
        "timeframe": artifact.timeframe,
        "horizon_bars": int(horizon),
        "observations": int(len(rows)),
        "artifact_edges": int(len(artifact.edges)),
        "avg_edges_used": float(avg_edges),
        "mean_abs_learned_score": float(mean_abs_score),
        "learned_ic": float(learned_ic),
        "static_cross_asset_ic": float(static_ic),
        "learned_minus_static_ic": float(learned_ic - static_ic),
        "actionable_rows": int(len(actionable)),
        "learned_hit_rate": float(hit_rate),
        "promote_candidate": bool(promote_candidate),
        "recommendation": "keep_shadow" if promote_candidate else "do_not_promote",
        "reason": reason,
    }
    return summary, rows


async def _load_close_history(
    session_factory: Any,
    *,
    symbols: list[str],
    timeframe: str,
    max_rows_per_symbol: int,
) -> pd.DataFrame:
    async with session_factory() as session:
        stmt = text(
            """
            WITH ranked AS (
              SELECT symbol, bar_timestamp, close,
                     row_number() OVER (PARTITION BY symbol ORDER BY bar_timestamp DESC) AS rn
              FROM feature_snapshots
              WHERE timeframe = :tf AND symbol = ANY(:symbols)
            )
            SELECT symbol, bar_timestamp, close
            FROM ranked
            WHERE rn <= :max_rows
            ORDER BY bar_timestamp ASC, symbol ASC
            """
        )
        rows = (await session.execute(stmt, {"tf": timeframe, "symbols": symbols, "max_rows": max_rows_per_symbol})).fetchall()
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(
        [{"symbol": str(r.symbol), "bar_timestamp": r.bar_timestamp, "close": float(r.close)} for r in rows]
    )
    return df.pivot_table(index="bar_timestamp", columns="symbol", values="close", aggfunc="last").sort_index()


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Report Phase E learned demand shadow evidence")
    p.add_argument("--artifact", default=str(DEFAULT_ARTIFACT))
    p.add_argument("--horizon", type=int, default=1)
    p.add_argument("--scale", type=float, default=30.0)
    p.add_argument("--timeframe", default=None)
    p.add_argument("--max-rows-per-symbol", type=int, default=5000)
    p.add_argument("--min-edges-used", type=int, default=1)
    p.add_argument("--signal-threshold", type=float, default=0.01)
    p.add_argument("--run-id", default=None)
    return p.parse_args()


async def _run(args: argparse.Namespace) -> int:
    load_dotenv(ROOT / ".env")
    artifact_path = Path(args.artifact)
    artifact = RelationalDemandGraphArtifact.from_dict(json.loads(artifact_path.read_text(encoding="utf-8")))
    timeframe = str(args.timeframe or artifact.timeframe)
    symbols = sorted({str(s).upper() for s in artifact.symbols})

    engine, session_factory = await init_async_database()
    if session_factory is None:
        raise SystemExit("database_unavailable")
    try:
        close = await _load_close_history(
            session_factory,
            symbols=symbols,
            timeframe=timeframe,
            max_rows_per_symbol=max(10, int(args.max_rows_per_symbol)),
        )
    finally:
        await dispose_engine(engine)
    if close.empty:
        raise SystemExit("no_history")

    summary, rows = build_shadow_evidence(
        close,
        artifact,
        horizon=max(1, int(args.horizon)),
        scale=float(args.scale),
        demand_config=_load_demand_config(),
        min_edges_used=max(1, int(args.min_edges_used)),
        signal_threshold=max(0.0, float(args.signal_threshold)),
    )

    run_id = args.run_id or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    DEFAULT_REPORT_DIR.mkdir(parents=True, exist_ok=True)
    json_path = DEFAULT_REPORT_DIR / f"phase_e_demand_shadow-{run_id}.json"
    csv_path = DEFAULT_REPORT_DIR / f"phase_e_demand_shadow-{run_id}.csv"
    latest_json = DEFAULT_REPORT_DIR / "latest_phase_e_demand_shadow.json"
    latest_csv = DEFAULT_REPORT_DIR / "latest_phase_e_demand_shadow.csv"

    payload = {"summary": summary, "rows": rows[-500:]}
    text_out = json.dumps(payload, indent=2, default=str)
    json_path.write_text(text_out, encoding="utf-8")
    latest_json.write_text(text_out, encoding="utf-8")
    if rows:
        with csv_path.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            writer.writeheader()
            writer.writerows(rows)
        latest_csv.write_text(csv_path.read_text(encoding="utf-8"), encoding="utf-8")

    print("Phase E demand shadow evidence:")
    print(f"  artifact={artifact.version}")
    print(f"  observations={summary['observations']}")
    print(f"  learned_ic={summary['learned_ic']:.6f}")
    print(f"  static_cross_asset_ic={summary['static_cross_asset_ic']:.6f}")
    print(f"  hit_rate={summary['learned_hit_rate']:.4f}")
    print(f"  recommendation={summary['recommendation']} ({summary['reason']})")
    print(f"  report={json_path}")
    if rows:
        print(f"  csv={csv_path}")
    return 0


def main() -> int:
    return asyncio.run(_run(_parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
