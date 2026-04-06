#!/usr/bin/env python3
"""
M3 runner: generate paper-mode signals from feature store and persist to signals table.
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import pandas as pd
import yaml
from dotenv import load_dotenv
from loguru import logger
from sqlalchemy import select

from risk.engine import RiskEngine, RiskVerdict
from signals.engine import RawSignal, SignalEngine
from storage.db import dispose_engine, init_async_database
from storage.models import FeatureSnapshot, SignalLog
from strategies.mean_reversion import MeanReversionStrategy
from strategies.momentum import MomentumBreakoutStrategy


def _load_yaml(path: str | Path) -> dict[str, Any]:
    p = Path(path)
    with p.open(encoding="utf-8") as f:
        return yaml.safe_load(f)


def _rows_to_features_frame(rows: list[FeatureSnapshot]) -> pd.DataFrame:
    payload: list[dict[str, Any]] = []
    for r in rows:
        row: dict[str, Any] = {
            "timestamp": r.bar_timestamp,
            "open": float(r.open),
            "high": float(r.high),
            "low": float(r.low),
            "close": float(r.close),
            "volume": float(r.volume),
        }
        if isinstance(r.features, dict):
            row.update(r.features)
        payload.append(row)
    if not payload:
        return pd.DataFrame()
    return pd.DataFrame(payload).set_index("timestamp").sort_index()


def _pick_best_signal(signals: list[RawSignal]) -> RawSignal | None:
    if not signals:
        return None
    return max(signals, key=lambda s: s.confidence)


async def _load_recent_features(
    session_factory,
    *,
    symbol: str,
    timeframe: str,
    lookback_bars: int,
) -> tuple[pd.DataFrame, datetime | None]:
    async with session_factory() as session:
        q = await session.execute(
            select(FeatureSnapshot)
            .where(
                FeatureSnapshot.symbol == symbol,
                FeatureSnapshot.timeframe == timeframe,
            )
            .order_by(FeatureSnapshot.bar_timestamp.desc())
            .limit(lookback_bars)
        )
        rows = list(q.scalars().all())
    rows.reverse()
    if not rows:
        return pd.DataFrame(), None
    return _rows_to_features_frame(rows), rows[-1].bar_timestamp


async def _persist_signal(session_factory, signal, *, paper_mode: bool, timeframe: str, feature_ts: datetime | None) -> None:
    ts = datetime.now(timezone.utc)
    metadata = dict(signal.metadata or {})
    metadata.update(
        {
            "paper_mode": paper_mode,
            "timeframe": timeframe,
            "feature_bar_timestamp": feature_ts.isoformat() if feature_ts else None,
        }
    )
    row = SignalLog(
        id=signal.signal_id,
        timestamp=ts,
        symbol=signal.symbol[:20],
        side=signal.side[:4],
        strategy=signal.strategy[:50],
        confidence=Decimal(str(signal.confidence)),
        asset_class=signal.asset_class[:20],
        broker=signal.broker[:20],
        news_score=Decimal(str(signal.news_score)) if signal.news_score is not None else None,
        news_veto=signal.news_veto,
        metadata_=metadata,
    )
    async with session_factory() as session:
        session.add(row)
        await session.commit()


async def _run_once(args: argparse.Namespace) -> int:
    strategies_cfg = _load_yaml(args.strategies_config)
    pipeline_cfg = _load_yaml(args.pipeline_config)
    risk_cfg = _load_yaml(args.risk_config)

    symbols = [s.strip() for s in (args.symbols.split(",") if args.symbols else pipeline_cfg.get("symbols", [])) if s.strip()]
    if not symbols:
        logger.error("run_m3 | no symbols configured")
        return 2

    engine, session_factory = await init_async_database()
    if session_factory is None:
        logger.error("run_m3 | no database | fix POSTGRES_* and ensure Postgres is up")
        return 1

    try:
        sig_engine = SignalEngine(strategies_cfg.get("signal_engine", {}))
        risk_engine = RiskEngine(risk_cfg)
        strat_cfg = strategies_cfg.get("strategies", {})
        momentum = MomentumBreakoutStrategy(strat_cfg.get("momentum_breakout", {}))
        mean_rev = MeanReversionStrategy(strat_cfg.get("mean_reversion", {}))
        generated = 0

        for symbol in symbols:
            df, feature_ts = await _load_recent_features(
                session_factory,
                symbol=symbol,
                timeframe=args.timeframe,
                lookback_bars=args.lookback_bars,
            )
            if df.empty:
                logger.warning("run_m3 | no features | {} {}", symbol, args.timeframe)
                continue

            raw_candidates = []
            m_sig = momentum.generate_signal(symbol, df)
            if m_sig is not None:
                raw_candidates.append(m_sig)
            r_sig = mean_rev.generate_signal(symbol, df)
            if r_sig is not None:
                raw_candidates.append(r_sig)

            raw = _pick_best_signal(raw_candidates)
            if raw is None:
                logger.info("run_m3 | no_signal | {} {}", symbol, args.timeframe)
                continue

            signal = sig_engine.process(
                raw,
                portfolio_value=Decimal(str(args.portfolio_value)),
                news_score=None,
            )
            if signal is None:
                logger.info("run_m3 | vetoed | {} {}", symbol, raw.strategy)
                continue

            # M3/M4 bridge: evaluate and audit every risk decision before persistence.
            portfolio_state = {
                "portfolio_value": Decimal(str(args.portfolio_value)),
                "daily_realized_pnl": Decimal("0"),
                "current_gross_exposure": Decimal("0"),
                "symbol_exposure": {},
            }
            risk_decision = await risk_engine.evaluate_and_persist(
                session_factory,
                signal,
                portfolio_state,
            )
            if risk_decision.verdict != RiskVerdict.APPROVED:
                logger.info(
                    "run_m3 | risk_rejected | {} {} | reason={}",
                    signal.symbol,
                    signal.strategy,
                    risk_decision.reason,
                )
                continue

            await _persist_signal(
                session_factory,
                signal,
                paper_mode=not args.live,
                timeframe=args.timeframe,
                feature_ts=feature_ts,
            )
            generated += 1
            logger.info(
                "run_m3 | signal | {} {} | strategy={} confidence={:.2f} qty={}",
                signal.symbol,
                signal.side,
                signal.strategy,
                signal.confidence,
                signal.suggested_quantity,
            )

        logger.info("run_m3 | done | generated_signals={}", generated)
        return 0
    finally:
        await dispose_engine(engine)


async def _run_loop(args: argparse.Namespace) -> int:
    interval_sec = int(args.loop_interval_sec)
    while True:
        code = await _run_once(args)
        if code != 0:
            logger.warning("run_m3 | iteration failed | code={}", code)
        logger.info("run_m3 | sleep | {}s", interval_sec)
        await asyncio.sleep(interval_sec)


def main() -> None:
    load_dotenv()
    p = argparse.ArgumentParser(description="M3 signal generation runner")
    p.add_argument("--strategies-config", default="config/strategies.yaml")
    p.add_argument("--pipeline-config", default="config/data_pipeline.yaml")
    p.add_argument("--risk-config", default="config/risk_limits.yaml")
    p.add_argument("--symbols", default=None, help="Comma-separated symbol override")
    p.add_argument("--timeframe", default="1d")
    p.add_argument("--lookback-bars", type=int, default=200)
    p.add_argument("--portfolio-value", type=float, default=100000.0)
    p.add_argument("--live", action="store_true", help="Mark generated signals as live-mode context")
    p.add_argument("--loop", action="store_true", help="Run periodic signal generation loop")
    p.add_argument("--loop-interval-sec", type=int, default=300, help="Loop sleep interval seconds")
    args = p.parse_args()
    if args.loop:
        raise SystemExit(asyncio.run(_run_loop(args)))
    raise SystemExit(asyncio.run(_run_once(args)))


if __name__ == "__main__":
    main()

