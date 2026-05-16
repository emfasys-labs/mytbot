#!/usr/bin/env python3
"""Build Phase E learned cross-asset relational demand graph artifact."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd
from dotenv import load_dotenv
from sqlalchemy import text

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from storage.db import dispose_engine, init_async_database  # noqa: E402
from system.relational_demand_graph import build_relational_artifact  # noqa: E402

DEFAULT_SYMBOLS = [
    "SPY",
    "QQQ",
    "IWM",
    "TLT",
    "HYG",
    "GLD",
    "BTC-USD",
    "ETH-USD",
    "SOL-USD",
    "NVDA",
    "AAPL",
    "MSFT",
    "USDCAD=X",
    "EURUSD=X",
]


def _run_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Build Phase E relational demand graph")
    p.add_argument("--symbols", default=",".join(DEFAULT_SYMBOLS))
    p.add_argument("--timeframe", default="1h")
    p.add_argument("--max-rows-per-symbol", type=int, default=5000)
    p.add_argument("--min-overlap", type=int, default=300)
    p.add_argument("--min-abs-lag-corr", type=float, default=0.08)
    p.add_argument("--max-edges", type=int, default=80)
    p.add_argument("--run-id", default=None)
    return p.parse_args()


async def _load_close_history(session_factory: Any, *, symbols: list[str], timeframe: str, max_rows_per_symbol: int) -> pd.DataFrame:
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


async def _run(args: argparse.Namespace) -> int:
    load_dotenv(ROOT / ".env")
    symbols = [s.strip().upper() for s in str(args.symbols).split(",") if s.strip()]
    run_id = args.run_id or _run_id()
    artifact_dir = ROOT / "artifacts" / "models" / "demand_graph"
    report_dir = ROOT / "reports" / "models" / "phase_e_demand_graph"
    artifact_dir.mkdir(parents=True, exist_ok=True)
    report_dir.mkdir(parents=True, exist_ok=True)

    engine, session_factory = await init_async_database()
    if session_factory is None:
        raise SystemExit("database_unavailable")
    try:
        close = await _load_close_history(
            session_factory,
            symbols=symbols,
            timeframe=args.timeframe,
            max_rows_per_symbol=max(10, int(args.max_rows_per_symbol)),
        )
    finally:
        await dispose_engine(engine)
    if close.empty:
        raise SystemExit("no_history")

    artifact = build_relational_artifact(
        close,
        timeframe=args.timeframe,
        version=run_id,
        min_overlap=max(2, int(args.min_overlap)),
        min_abs_lag_corr=float(args.min_abs_lag_corr),
        max_edges=max(1, int(args.max_edges)),
    )
    payload = artifact.to_dict()
    payload["metadata"]["symbols_requested"] = symbols
    payload["metadata"]["symbols_with_data"] = [str(c) for c in close.columns if close[c].notna().any()]
    payload["metadata"]["built_at"] = datetime.now(timezone.utc).isoformat()

    artifact_path = artifact_dir / f"phase_e_relational_graph-{run_id}.json"
    latest_path = artifact_dir / "latest_phase_e_relational_graph.json"
    report_path = report_dir / f"phase_e_relational_graph-{run_id}.json"
    text_out = json.dumps(payload, indent=2, default=str)
    artifact_path.write_text(text_out, encoding="utf-8")
    latest_path.write_text(text_out, encoding="utf-8")
    report_path.write_text(text_out, encoding="utf-8")

    print("Phase E relational demand graph built:")
    print(f"  symbols_with_data={len(payload['metadata']['symbols_with_data'])}")
    print(f"  bars={len(close)}")
    print(f"  edges={len(payload['edges'])}")
    print(f"  artifact={artifact_path}")
    print(f"  latest={latest_path}")
    return 0


def main() -> int:
    return asyncio.run(_run(_parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
