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

from ai.news_classifier import NewsClassifier
from ai.pipeline import AIPipeline
from ai.regime import filter_by_allowed_strategies
from control.command_bus import CommandBus
from control.runner_control import apply_control_commands, hydrate_risk_parameters_from_bus, publish_runner_heartbeat
from control.runtime import set_risk_engine
from execution.engine import ExecutionEngine
from execution.router import SmartOrderRouter
from risk.engine import RiskEngine, RiskVerdict
from risk.m8_loader import merge_m8_into_risk_cfg
from run_m3 import (
    _load_portfolio_state,
    _load_recent_features,
    _persist_position_snapshot,
    _persist_signal,
    _pick_best_signal,
    _upsert_daily_pnl,
)
from signals.engine import SignalEngine
from storage.db import dispose_engine, init_async_database
from storage.models import AIOutputLog
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


def _apply_filled_result_to_portfolio_state(portfolio_state: dict[str, Any], signal, result) -> None:
    positions = dict(portfolio_state.get("positions", {}))
    try:
        filled_qty = Decimal(str(result.filled_quantity))
    except Exception:  # noqa: BLE001
        filled_qty = Decimal("0")
    if filled_qty <= 0:
        return
    fill_px = Decimal(str(result.avg_fill_price or signal.suggested_price or "0"))
    if fill_px <= 0:
        return

    symbol = signal.symbol
    side_mult = Decimal("1") if signal.side == "buy" else Decimal("-1")
    qty_delta = filled_qty * side_mult
    row = positions.get(symbol)
    if row is None:
        row = {
            "quantity": Decimal("0"),
            "avg_entry_price": fill_px,
            "current_price": fill_px,
            "asset_class": (signal.asset_class or "").strip().lower(),
            "broker": (signal.broker or "").strip()[:20],
        }
    prev_qty = Decimal(str(row["quantity"]))
    new_qty = prev_qty + qty_delta
    if new_qty == 0:
        positions.pop(symbol, None)
    else:
        row["quantity"] = new_qty
        row["current_price"] = fill_px
        if prev_qty == 0:
            row["avg_entry_price"] = fill_px
        elif (prev_qty > 0 and qty_delta > 0) or (prev_qty < 0 and qty_delta < 0):
            old_notional = abs(prev_qty) * Decimal(str(row["avg_entry_price"]))
            add_notional = abs(qty_delta) * fill_px
            row["avg_entry_price"] = (old_notional + add_notional) / abs(new_qty)
        positions[symbol] = row

    symbol_exposure: dict[str, Decimal] = {}
    asset_class_exposure: dict[str, Decimal] = {}
    gross = Decimal("0")
    for sym, p in positions.items():
        qty = abs(Decimal(str(p["quantity"])))
        px = Decimal(str(p["current_price"]))
        notional = qty * px
        gross += notional
        symbol_exposure[sym] = symbol_exposure.get(sym, Decimal("0")) + notional
        asset = str(p.get("asset_class", "")).strip().lower()
        if asset:
            asset_class_exposure[asset] = asset_class_exposure.get(asset, Decimal("0")) + notional

    portfolio_state["positions"] = positions
    portfolio_state["symbol_exposure"] = symbol_exposure
    portfolio_state["asset_class_exposure"] = asset_class_exposure
    portfolio_state["current_gross_exposure"] = gross
    portfolio_state["trades_today"] = int(portfolio_state.get("trades_today", 0)) + 1


def _estimate_realized_pnl_from_fill(portfolio_state: dict[str, Any], signal, result) -> Decimal:
    positions = portfolio_state.get("positions", {})
    row = positions.get(signal.symbol)
    if row is None:
        return Decimal("0")
    prev_qty = Decimal(str(row.get("quantity", "0")))
    avg_entry = Decimal(str(row.get("avg_entry_price", "0")))
    fill_px = Decimal(str(result.avg_fill_price or signal.suggested_price or "0"))
    if fill_px <= 0 or prev_qty == 0:
        return Decimal("0")
    fill_qty = Decimal(str(result.filled_quantity or "0"))
    if fill_qty <= 0:
        return Decimal("0")
    # Realized PnL only when reducing an existing position.
    if signal.side == "sell" and prev_qty > 0:
        close_qty = min(fill_qty, prev_qty)
        return (fill_px - avg_entry) * close_qty
    if signal.side == "buy" and prev_qty < 0:
        close_qty = min(fill_qty, abs(prev_qty))
        return (avg_entry - fill_px) * close_qty
    return Decimal("0")


async def _run_loop(args: argparse.Namespace) -> int:
    strategies_cfg = _load_yaml(args.strategies_config)
    pipeline_cfg = _load_yaml(args.pipeline_config)
    risk_cfg = _load_yaml(args.risk_config)
    merge_m8_into_risk_cfg(risk_cfg, args.m8_config)
    ai_cfg = _load_yaml(args.ai_config)
    symbols = [s.strip() for s in (args.symbols.split(",") if args.symbols else pipeline_cfg.get("symbols", [])) if s.strip()]
    if not symbols:
        logger.error("run_m5 | no symbols configured")
        return 2

    m8 = risk_cfg.get("m8_micro_live") or {}
    if isinstance(m8, dict) and m8.get("enabled") and os.getenv("APP_ENV", "paper").strip().lower() == "live":
        logger.warning(
            "run_m5 | M8 micro-live gates ACTIVE | symbols={} strategies={} max_notional_usd={}",
            m8.get("symbol_whitelist"),
            m8.get("strategy_whitelist"),
            m8.get("max_notional_usd_per_order"),
        )

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
    set_risk_engine(risk_engine)
    ai_enabled = bool(ai_cfg.get("enabled", True))
    ai_classifier = NewsClassifier() if ai_enabled else None
    ai_pipeline = AIPipeline(ai_cfg.get("pipeline", {}), classifier=ai_classifier) if ai_enabled else None
    strat_cfg = strategies_cfg.get("strategies", {})
    momentum = MomentumBreakoutStrategy(strat_cfg.get("momentum_breakout", {}))
    mean_rev = MeanReversionStrategy(strat_cfg.get("mean_reversion", {}))
    strategies = {
        momentum.name: momentum,
        mean_rev.name: mean_rev,
    }
    bus = CommandBus(session_factory)
    for name, strategy in strategies.items():
        state_v = await bus.get_state(f"strategy.enabled.{name}", None)
        if state_v is not None:
            strategy.enabled = bool(state_v)
    await hydrate_risk_parameters_from_bus(bus, risk_engine)
    if args.clear_pending_kill_commands:
        n = await bus.delete_pending_commands_of_type("kill")
        risk_engine.reset_kill()
        logger.warning(
            "run_m5 | recovery | removed {} pending kill command(s); kill switch reset for this process",
            n,
        )

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
            ok = await execution.reconcile_positions(session_factory=session_factory)
            logger.info("run_m5 | reconcile-only | ok={}", ok)
            return 0 if ok else 3

        control_ctx = {
            "bus": bus,
            "risk_engine": risk_engine,
            "execution": execution,
            "strategies": strategies,
        }
        poll_sec = max(3, int(args.control_poll_interval_sec))

        async def _control_poll_loop() -> None:
            while True:
                await asyncio.sleep(poll_sec)
                try:
                    await apply_control_commands(
                        control_ctx["bus"],
                        risk_engine=control_ctx["risk_engine"],
                        execution_engine=control_ctx["execution"],
                        strategies=control_ctx["strategies"],
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning("run_m5 | control poll failed | {}", exc)

        poll_task = asyncio.create_task(_control_poll_loop())

        try:
            while True:
                try:
                    generated = 0
                    executed = 0
                    await apply_control_commands(
                        bus,
                        risk_engine=risk_engine,
                        execution_engine=execution,
                        strategies=strategies,
                    )
                    ai_result = None
                    if ai_pipeline is not None:
                        ai_result = await ai_pipeline.compute(session_factory, symbols)
                        await ai_pipeline.persist(session_factory, ai_result)
                    for symbol in symbols:
                        df, feature_ts = await _load_recent_features(
                            session_factory,
                            symbol=symbol,
                            timeframe=args.timeframe,
                            lookback_bars=args.lookback_bars,
                        )
                        if df.empty:
                            logger.warning(
                                "run_m5 | no features | {} | timeframe={} | "
                                "no rows in feature_snapshots — run `python run_pipeline.py --once` "
                                "(1h bars) or `--backfill` (1d bars), and use --timeframe matching the DB",
                                symbol,
                                args.timeframe,
                            )
                            continue

                        raw_candidates = []
                        m_sig = momentum.generate_signal(symbol, df)
                        if m_sig is not None:
                            raw_candidates.append(m_sig)
                        r_sig = mean_rev.generate_signal(symbol, df)
                        if r_sig is not None:
                            raw_candidates.append(r_sig)
                        if ai_result is not None and ai_pipeline is not None:
                            allowed = ai_pipeline.allowed_strategy_names(ai_result.macro_regime)
                            filtered = filter_by_allowed_strategies(raw_candidates, allowed)
                            if not filtered and raw_candidates:
                                logger.info(
                                    "run_m5 | regime_gate_block | {} regime={} candidates={}",
                                    symbol,
                                    ai_result.macro_regime,
                                    [r.strategy for r in raw_candidates],
                                )
                            raw_candidates = filtered

                        raw = _pick_best_signal(raw_candidates)
                        if raw is None:
                            logger.info("run_m5 | no strategy signal | {} | {}", symbol, args.timeframe)
                            continue

                        signal = sig_engine.process(
                            raw,
                            portfolio_value=Decimal(str(args.portfolio_value)),
                            news_score=(
                                ai_result.news_scores.get(symbol) if ai_result is not None else None
                            ),
                        )
                        if signal is None:
                            logger.info("run_m5 | signal engine veto | {} | {}", symbol, raw.strategy)
                            continue
                        if ai_result is not None:
                            signal.metadata["ai_macro_regime"] = ai_result.macro_regime
                            signal.metadata["ai_macro_confidence"] = ai_result.macro_confidence
                            signal.metadata["ai_news_detail"] = ai_result.news_details.get(symbol, {})
                            signal.metadata["ai_anomalies"] = [a for a in ai_result.anomalies if a.get("symbol") == symbol]

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
                        risk_engine.restore_runtime_state(portfolio_state)
                        risk_decision = await risk_engine.evaluate_and_persist(
                            session_factory,
                            signal,
                            portfolio_state,
                        )
                        if risk_decision.verdict != RiskVerdict.APPROVED:
                            logger.info(
                                "run_m5 | risk rejected | {} | {} | failed={}",
                                signal.symbol,
                                signal.signal_id,
                                risk_decision.checks_failed,
                            )
                            continue

                        if ai_result is not None and ai_classifier is not None:
                            rationale = await ai_classifier.generate_rationale(
                                {
                                    "symbol": signal.symbol,
                                    "strategy": signal.strategy,
                                    "confidence": signal.confidence,
                                    "news_score": signal.news_score,
                                    "macro_regime": ai_result.macro_regime,
                                    "asset_class": signal.asset_class,
                                }
                            )
                            signal.metadata["ai_rationale"] = rationale
                            async with session_factory() as session:
                                session.add(
                                    AIOutputLog(
                                        symbol=signal.symbol[:32],
                                        context_type="rationale",
                                        score=Decimal(str(signal.news_score)) if signal.news_score is not None else None,
                                        confidence=Decimal(str(signal.confidence)),
                                        event_type=str(
                                            ai_result.news_details.get(symbol, {}).get("event_type", "other")
                                        ),
                                        regime_label=ai_result.macro_regime,
                                        decay_hours=int(
                                            ai_result.news_details.get(symbol, {}).get("decay_hours", 24)
                                        ),
                                        rationale=rationale[:4000],
                                        payload={
                                            "headline": ai_result.news_details.get(symbol, {}).get("headline"),
                                            "sample_count": ai_result.news_details.get(symbol, {}).get("sample_count", 0),
                                            "macro_confidence": ai_result.macro_confidence,
                                        },
                                        source="claude",
                                        signal_id=signal.signal_id,
                                    )
                                )
                                await session.commit()

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

                        realized = _estimate_realized_pnl_from_fill(portfolio_state, signal, result)
                        if realized < 0:
                            risk_engine.record_loss(abs(realized))
                        elif realized > 0:
                            risk_engine.record_win()
                        portfolio_state.update(risk_engine.snapshot_runtime_state())

                        _apply_filled_result_to_portfolio_state(portfolio_state, signal, result)
                        await _persist_position_snapshot(session_factory, portfolio_state)
                        await _upsert_daily_pnl(session_factory, portfolio_state)

                    now_ts = datetime.now(timezone.utc).timestamp()
                    if now_ts >= next_reconcile_at:
                        ok = await execution.reconcile_positions(session_factory=session_factory)
                        logger.info("run_m5 | reconcile | ok={}", ok)
                        next_reconcile_at = now_ts + max(10, int(args.reconcile_interval_sec))

                    await publish_runner_heartbeat(
                        bus,
                        runner_name="run_m5",
                        symbols=symbols,
                        generated=generated,
                        executed=executed,
                        extra={"paper_mode": not args.live},
                    )
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
                    bus = CommandBus(session_factory)
                    control_ctx["bus"] = bus
                    await hydrate_risk_parameters_from_bus(bus, risk_engine)
                await asyncio.sleep(max(1, int(args.loop_interval_sec)))
        finally:
            poll_task.cancel()
            try:
                await poll_task
            except asyncio.CancelledError:
                pass
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
    p.add_argument(
        "--m8-config",
        default="config/m8_micro_live.yaml",
        help="Optional M8 micro-live profile merged into risk config",
    )
    p.add_argument("--ai-config", default="config/ai.yaml")
    p.add_argument("--symbols", default=None, help="Comma-separated symbol override")
    p.add_argument("--available-brokers", default="ibkr,kraken,binance,alpaca")
    p.add_argument(
        "--timeframe",
        default="1h",
        help="Must match rows in feature_snapshots (default 1h = incremental in config/data_pipeline.yaml; use 1d if you only ran run_pipeline.py --backfill)",
    )
    p.add_argument("--lookback-bars", type=int, default=200)
    p.add_argument("--portfolio-value", type=float, default=100000.0)
    _mode = p.add_mutually_exclusive_group()
    _mode.add_argument(
        "--paper",
        action="store_true",
        help="Paper / sandbox trading (default if neither --paper nor --live)",
    )
    _mode.add_argument(
        "--live",
        action="store_true",
        help="Live broker orders (real money when brokers are in live mode)",
    )
    p.add_argument("--loop-interval-sec", type=int, default=120)
    p.add_argument(
        "--control-poll-interval-sec",
        type=int,
        default=5,
        help="How often to apply control commands from the queue (independent of main loop)",
    )
    p.add_argument("--reconcile-interval-sec", type=int, default=300)
    p.add_argument("--place-order-retries", type=int, default=2)
    p.add_argument("--place-order-retry-backoff-sec", type=float, default=1.0)
    p.add_argument("--fill-poll-timeout-sec", type=float, default=10.0)
    p.add_argument("--fill-poll-interval-sec", type=float, default=1.0)
    p.add_argument("--reconcile-only", action="store_true", help="Run one reconciliation cycle then exit")
    p.add_argument(
        "--clear-pending-kill-commands",
        action="store_true",
        help="Delete pending/processing 'kill' rows in control_commands and reset kill switch "
        "(use if a dashboard/API kill was left queued and blocks trading)",
    )
    return p


if __name__ == "__main__":
    main()

