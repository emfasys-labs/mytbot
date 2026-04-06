#!/usr/bin/env python3
"""
M5 runner: autonomous paper loop
Signal -> Risk -> Execute -> Fill tracking -> Logs/PnL -> Reconciliation.
"""

from __future__ import annotations

import argparse
import asyncio
import os
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv
from loguru import logger

from execution.engine import ExecutionEngine
from execution.router import SmartOrderRouter
from risk.engine import RiskEngine, RiskVerdict
from run_m3 import (
    _apply_signal_to_portfolio_state,
    _load_portfolio_state,
    _load_recent_features,
    _persist_position_snapshot,
    _persist_signal,
    _pick_best_signal,
    _upsert_daily_pnl,
)
from signals.engine import SignalEngine
from storage.db import dispose_engine, init_async_database
from strategies.mean_reversion import MeanReversionStrategy
from strategies.momentum import MomentumBreakoutStrategy


def _load_yaml(path: str | Path) -> dict[str, Any]:
    with Path(path).open(encoding="utf-8") as f:
        return yaml.safe_load(f)


def _build_broker_configs() -> dict[str, dict[str, Any]]:
    return {
        "ibkr": {
            "host": os.getenv("IBKR_HOST", "127.0.0.1"),
            "port": int(os.getenv("IBKR_PORT", "7497")),
            "client_id": int(os.getenv("IBKR_CLIENT_ID", "1")),
            "account_id": os.getenv("IBKR_ACCOUNT_ID", "").strip(),
        },
        "kraken": {
            "api_key": os.getenv("KRAKEN_API_KEY", "").strip(),
            "api_secret": os.getenv("KRAKEN_API_SECRET", "").strip(),
        },
        "binance": {
            "api_key": os.getenv("BINANCE_API_KEY", "").strip(),
            "api_secret": os.getenv("BINANCE_API_SECRET", "").strip(),
            "testnet": os.getenv("BINANCE_TESTNET", "0").strip().lower() in {"1", "true", "yes", "on"},
        },
        "alpaca": {
            "api_key": os.getenv("ALPACA_API_KEY", "").strip(),
            "api_secret": os.getenv("ALPACA_API_SECRET", "").strip(),
            "base_url": os.getenv("ALPACA_BASE_URL", "").strip() or None,
        },
    }


async def _run_loop(args: argparse.Namespace) -> int:
    strategies_cfg = _load_yaml(args.strategies_config)
    pipeline_cfg = _load_yaml(args.pipeline_config)
    risk_cfg = _load_yaml(args.risk_config)
    symbols = [s.strip() for s in (args.symbols.split(",") if args.symbols else pipeline_cfg.get("symbols", [])) if s.strip()]
    if not symbols:
        logger.error("run_m5 | no symbols configured")
        return 2

    engine, session_factory = await init_async_database()
    if session_factory is None:
        logger.error("run_m5 | no database | fix POSTGRES_* and ensure Postgres is up")
        return 1

    broker_configs = _build_broker_configs()
    available_brokers = [b.strip() for b in args.available_brokers.split(",") if b.strip()]
    router = SmartOrderRouter(available_brokers)
    execution = ExecutionEngine(
        broker_configs=broker_configs,
        paper_mode=not args.live,
        place_order_retries=args.place_order_retries,
        place_order_retry_backoff_sec=args.place_order_retry_backoff_sec,
        fill_poll_timeout_sec=args.fill_poll_timeout_sec,
        fill_poll_interval_sec=args.fill_poll_interval_sec,
    )
    sig_engine = SignalEngine(strategies_cfg.get("signal_engine", {}))
    risk_engine = RiskEngine(risk_cfg)
    strat_cfg = strategies_cfg.get("strategies", {})
    momentum = MomentumBreakoutStrategy(strat_cfg.get("momentum_breakout", {}))
    mean_rev = MeanReversionStrategy(strat_cfg.get("mean_reversion", {}))

    next_reconcile_at = datetime.now(timezone.utc).timestamp()

    async def _reconnect_db() -> tuple[Any, Any]:
        logger.warning("run_m5 | db reconnect | attempting")
        new_engine, new_session_factory = await init_async_database()
        if new_session_factory is None:
            logger.error("run_m5 | db reconnect failed")
            return None, None
        logger.info("run_m5 | db reconnect succeeded")
        return new_engine, new_session_factory

    try:
        if args.reconcile_only:
            ok = await execution.reconcile_positions()
            logger.info("run_m5 | reconcile-only | ok={}", ok)
            return 0 if ok else 3

        while True:
            try:
                generated = 0
                executed = 0
                for symbol in symbols:
                    df, feature_ts = await _load_recent_features(
                        session_factory,
                        symbol=symbol,
                        timeframe=args.timeframe,
                        lookback_bars=args.lookback_bars,
                    )
                    if df.empty:
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
                        continue

                    signal = sig_engine.process(
                        raw,
                        portfolio_value=Decimal(str(args.portfolio_value)),
                        news_score=None,
                    )
                    if signal is None:
                        continue

                    routed = router.route(signal.asset_class, signal.symbol)
                    if routed is None:
                        logger.warning("run_m5 | no_route | {} {}", signal.symbol, signal.asset_class)
                        continue
                    signal.broker = routed

                    portfolio_state = await _load_portfolio_state(
                        session_factory,
                        fallback_portfolio_value=Decimal(str(args.portfolio_value)),
                        signal_price_fallback=signal.suggested_price,
                    )
                    risk_engine.update_high_watermark(
                        Decimal(str(portfolio_state.get("high_watermark_value", args.portfolio_value)))
                    )
                    risk_decision = await risk_engine.evaluate_and_persist(
                        session_factory,
                        signal,
                        portfolio_state,
                    )
                    if risk_decision.verdict != RiskVerdict.APPROVED:
                        continue

                    await _persist_signal(
                        session_factory,
                        signal,
                        paper_mode=not args.live,
                        timeframe=args.timeframe,
                        feature_ts=feature_ts,
                    )
                    generated += 1

                    result = await execution.execute(
                        signal,
                        risk_decision,
                        session_factory=session_factory,
                    )
                    if result is None:
                        continue
                    executed += 1

                    _apply_signal_to_portfolio_state(portfolio_state, signal)
                    await _persist_position_snapshot(session_factory, portfolio_state)
                    await _upsert_daily_pnl(session_factory, portfolio_state)

                now_ts = datetime.now(timezone.utc).timestamp()
                if now_ts >= next_reconcile_at:
                    ok = await execution.reconcile_positions()
                    logger.info("run_m5 | reconcile | ok={}", ok)
                    next_reconcile_at = now_ts + max(10, int(args.reconcile_interval_sec))

                logger.info("run_m5 | iteration | generated={} executed={}", generated, executed)
            except Exception as exc:  # noqa: BLE001
                logger.exception("run_m5 | iteration failed | {}", exc)
                await execution._send_critical_alert(f"run_m5 iteration failure: {exc}")
                # Attempt DB recovery to avoid prolonged persistence outages.
                try:
                    await dispose_engine(engine)
                except Exception:  # noqa: BLE001
                    pass
                engine, session_factory = await _reconnect_db()
                if session_factory is None:
                    await asyncio.sleep(max(5, int(args.loop_interval_sec)))
                    continue
            await asyncio.sleep(max(1, int(args.loop_interval_sec)))
    finally:
        await dispose_engine(engine)


def main() -> None:
    load_dotenv()
    p = _build_parser()
    args = p.parse_args()
    raise SystemExit(asyncio.run(_run_loop(args)))


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="M5 autonomous paper runner")
    p.add_argument("--strategies-config", default="config/strategies.yaml")
    p.add_argument("--pipeline-config", default="config/data_pipeline.yaml")
    p.add_argument("--risk-config", default="config/risk_limits.yaml")
    p.add_argument("--symbols", default=None, help="Comma-separated symbol override")
    p.add_argument("--available-brokers", default="ibkr,kraken,binance,alpaca")
    p.add_argument("--timeframe", default="1d")
    p.add_argument("--lookback-bars", type=int, default=200)
    p.add_argument("--portfolio-value", type=float, default=100000.0)
    p.add_argument("--live", action="store_true")
    p.add_argument("--loop-interval-sec", type=int, default=120)
    p.add_argument("--reconcile-interval-sec", type=int, default=300)
    p.add_argument("--place-order-retries", type=int, default=2)
    p.add_argument("--place-order-retry-backoff-sec", type=float, default=1.0)
    p.add_argument("--fill-poll-timeout-sec", type=float, default=10.0)
    p.add_argument("--fill-poll-interval-sec", type=float, default=1.0)
    p.add_argument("--reconcile-only", action="store_true", help="Run one reconciliation cycle then exit")
    return p


if __name__ == "__main__":
    main()

