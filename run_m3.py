#!/usr/bin/env python3
"""
M3 runner: generate paper-mode signals from feature store and persist to signals table.
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import pandas as pd
import yaml
from dotenv import load_dotenv
from loguru import logger
from sqlalchemy import func, select

from ai.news_classifier import NewsClassifier
from ai.pipeline import AIPipeline
from ai.regime import filter_by_allowed_strategies
from control.command_bus import CommandBus
from control.runner_control import apply_control_commands, publish_runner_heartbeat
from control.runtime import set_risk_engine
from risk.engine import RiskEngine, RiskVerdict
from signals.engine import RawSignal, SignalEngine
from storage.db import dispose_engine, init_async_database
from storage.models import AIOutputLog, DailyPnL, FeatureSnapshot, PositionLog, SignalLog
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


async def _persist_signal_ai_audit(
    session_factory,
    *,
    signal,
    macro_regime: str,
    macro_confidence: float,
    news_detail: dict[str, Any] | None,
    rationale: str | None,
) -> None:
    detail = news_detail or {}
    async with session_factory() as session:
        session.add(
            AIOutputLog(
                symbol=signal.symbol[:32],
                context_type="rationale",
                score=Decimal(str(signal.news_score)) if signal.news_score is not None else None,
                confidence=Decimal(str(signal.confidence)),
                event_type=str(detail.get("event_type", "other")),
                regime_label=macro_regime,
                decay_hours=int(detail.get("decay_hours", 24)),
                rationale=(rationale or "")[:4000],
                payload={
                    "headline": detail.get("headline"),
                    "sample_count": detail.get("sample_count", 0),
                    "macro_confidence": macro_confidence,
                },
                source="claude",
                signal_id=signal.signal_id,
            )
        )
        await session.commit()


async def _load_portfolio_state(
    session_factory,
    *,
    fallback_portfolio_value: Decimal,
    signal_price_fallback: Decimal | None = None,
) -> dict[str, Any]:
    """
    Build portfolio state from DB snapshots (M4 risk checks).
    Falls back to provided portfolio value when history is unavailable.
    """
    portfolio_value = fallback_portfolio_value
    high_watermark_value = fallback_portfolio_value
    daily_realized_pnl = Decimal("0")
    current_gross_exposure = Decimal("0")
    symbol_exposure: dict[str, Decimal] = {}
    asset_class_exposure: dict[str, Decimal] = {}
    positions: dict[str, dict[str, Any]] = {}
    trades_today = 0
    consecutive_losses = 0
    cooldown_until: str | None = None
    daily_loss_accumulated = Decimal("0")

    async with session_factory() as session:
        latest_pnl_q = await session.execute(
            select(DailyPnL).order_by(DailyPnL.id.desc()).limit(1)
        )
        latest_pnl = latest_pnl_q.scalars().first()
        if latest_pnl is not None:
            portfolio_value = Decimal(str(latest_pnl.portfolio_value or fallback_portfolio_value))
            daily_realized_pnl = Decimal(str(latest_pnl.realised_pnl or "0"))
            if isinstance(latest_pnl.strategy_breakdown, dict):
                b = latest_pnl.strategy_breakdown
                try:
                    consecutive_losses = int(b.get("risk_consecutive_losses", 0))
                except Exception:  # noqa: BLE001
                    consecutive_losses = 0
                raw_cu = b.get("risk_cooldown_until")
                if isinstance(raw_cu, str) and raw_cu.strip():
                    cooldown_until = raw_cu
                try:
                    daily_loss_accumulated = Decimal(str(b.get("risk_daily_loss_accumulated", "0")))
                except Exception:  # noqa: BLE001
                    daily_loss_accumulated = Decimal("0")

        hwm_q = await session.execute(select(func.max(DailyPnL.portfolio_value)))
        hwm_raw = hwm_q.scalar_one_or_none()
        if hwm_raw is not None:
            high_watermark_value = Decimal(str(hwm_raw))
        else:
            high_watermark_value = portfolio_value

        latest_pos_ts_q = await session.execute(select(func.max(PositionLog.timestamp)))
        latest_pos_ts = latest_pos_ts_q.scalar_one_or_none()
        if latest_pos_ts is not None:
            rows_q = await session.execute(
                select(PositionLog).where(PositionLog.timestamp == latest_pos_ts)
            )
            rows = list(rows_q.scalars().all())
            for row in rows:
                qty = Decimal(str(row.quantity))
                px = Decimal(str(row.current_price or signal_price_fallback or "0"))
                notional = abs(qty) * px
                current_gross_exposure += notional
                symbol = (row.symbol or "").strip()
                if symbol:
                    symbol_exposure[symbol] = symbol_exposure.get(symbol, Decimal("0")) + notional
                    positions[symbol] = {
                        "quantity": qty,
                        "avg_entry_price": Decimal(str(row.avg_entry_price or px)),
                        "current_price": px,
                        "asset_class": (row.asset_class or "").strip().lower(),
                        "broker": (row.broker or "").strip()[:20],
                    }
                asset = (row.asset_class or "").strip().lower()
                if asset:
                    asset_class_exposure[asset] = asset_class_exposure.get(asset, Decimal("0")) + notional

        now = datetime.now(timezone.utc)
        start = datetime(now.year, now.month, now.day, tzinfo=timezone.utc)
        end = start + timedelta(days=1)
        trades_q = await session.execute(
            select(func.count())
            .select_from(SignalLog)
            .where(SignalLog.timestamp >= start, SignalLog.timestamp < end)
        )
        trades_today = int(trades_q.scalar_one() or 0)

    return {
        "portfolio_value": portfolio_value,
        "high_watermark_value": high_watermark_value,
        "daily_realized_pnl": daily_realized_pnl,
        "current_gross_exposure": current_gross_exposure,
        "symbol_exposure": symbol_exposure,
        "asset_class_exposure": asset_class_exposure,
        "positions": positions,
        "trades_today": trades_today,
        "consecutive_losses": consecutive_losses,
        "cooldown_until": cooldown_until,
        "daily_loss_accumulated": daily_loss_accumulated,
    }


def _resolve_price_from_signal(signal) -> Decimal:
    if signal.suggested_price is not None and signal.suggested_price > 0:
        return signal.suggested_price
    metadata = signal.metadata if isinstance(signal.metadata, dict) else {}
    for key in ("close", "last_price", "price"):
        if key not in metadata:
            continue
        try:
            p = Decimal(str(metadata[key]))
        except Exception:  # noqa: BLE001
            continue
        if p > 0:
            return p
    return Decimal("0")


def _apply_signal_to_portfolio_state(portfolio_state: dict[str, Any], signal) -> None:
    """
    Apply intended signal quantities to local state (M3 simulation path).
    Execution-accurate updates should use the M5 fill-based updater.
    """
    _apply_intended_signal_to_portfolio_state(portfolio_state, signal)


def _apply_intended_signal_to_portfolio_state(portfolio_state: dict[str, Any], signal) -> None:
    positions = dict(portfolio_state.get("positions", {}))
    price = _resolve_price_from_signal(signal)
    if price <= 0:
        return
    symbol = signal.symbol
    side_mult = Decimal("1") if signal.side == "buy" else Decimal("-1")
    qty_delta = Decimal(str(signal.suggested_quantity)) * side_mult

    row = positions.get(symbol)
    if row is None:
        row = {
            "quantity": Decimal("0"),
            "avg_entry_price": price,
            "current_price": price,
            "asset_class": (signal.asset_class or "").strip().lower(),
            "broker": (signal.broker or "").strip()[:20],
        }
    prev_qty = Decimal(str(row["quantity"]))
    new_qty = prev_qty + qty_delta
    if new_qty == 0:
        positions.pop(symbol, None)
    else:
        row["quantity"] = new_qty
        row["current_price"] = price
        if prev_qty == 0:
            row["avg_entry_price"] = price
        elif (prev_qty > 0 and qty_delta > 0) or (prev_qty < 0 and qty_delta < 0):
            # Weighted average on add-to-position in same direction.
            old_notional = abs(prev_qty) * Decimal(str(row["avg_entry_price"]))
            add_notional = abs(qty_delta) * price
            denom = abs(new_qty)
            if denom > 0:
                row["avg_entry_price"] = (old_notional + add_notional) / denom
        positions[symbol] = row

    symbol_exposure: dict[str, Decimal] = {}
    asset_class_exposure: dict[str, Decimal] = {}
    current_gross_exposure = Decimal("0")
    for sym, p in positions.items():
        qty = abs(Decimal(str(p["quantity"])))
        px = Decimal(str(p["current_price"]))
        notional = qty * px
        current_gross_exposure += notional
        symbol_exposure[sym] = symbol_exposure.get(sym, Decimal("0")) + notional
        asset = str(p.get("asset_class", "")).strip().lower()
        if asset:
            asset_class_exposure[asset] = asset_class_exposure.get(asset, Decimal("0")) + notional

    portfolio_state["positions"] = positions
    portfolio_state["symbol_exposure"] = symbol_exposure
    portfolio_state["asset_class_exposure"] = asset_class_exposure
    portfolio_state["current_gross_exposure"] = current_gross_exposure
    portfolio_state["trades_today"] = int(portfolio_state.get("trades_today", 0)) + 1


async def _persist_position_snapshot(session_factory, portfolio_state: dict[str, Any]) -> None:
    positions = portfolio_state.get("positions", {})
    if not positions:
        return
    ts = datetime.now(timezone.utc)
    async with session_factory() as session:
        for symbol, p in positions.items():
            row = PositionLog(
                timestamp=ts,
                symbol=symbol[:20],
                broker=str(p.get("broker", "ibkr"))[:20] or "ibkr",
                quantity=Decimal(str(p.get("quantity", "0"))),
                avg_entry_price=Decimal(str(p.get("avg_entry_price", "0"))),
                current_price=Decimal(str(p.get("current_price", "0"))),
                unrealised_pnl=Decimal("0"),
                asset_class=str(p.get("asset_class", ""))[:20],
            )
            session.add(row)
        await session.commit()


async def _upsert_daily_pnl(session_factory, portfolio_state: dict[str, Any]) -> None:
    now = datetime.now(timezone.utc)
    d = now.date().isoformat()
    async with session_factory() as session:
        q = await session.execute(select(DailyPnL).where(DailyPnL.date == d).limit(1))
        row = q.scalars().first()
        if row is None:
            row = DailyPnL(
                date=d,
                realised_pnl=Decimal(str(portfolio_state.get("daily_realized_pnl", "0"))),
                unrealised_pnl=Decimal("0"),
                total_fees=Decimal("0"),
                trade_count=int(portfolio_state.get("trades_today", 0)),
                portfolio_value=Decimal(str(portfolio_state.get("portfolio_value", "0"))),
                strategy_breakdown={
                    "risk_consecutive_losses": int(portfolio_state.get("consecutive_losses", 0)),
                    "risk_cooldown_until": portfolio_state.get("cooldown_until"),
                    "risk_daily_loss_accumulated": str(portfolio_state.get("daily_loss_accumulated", "0")),
                },
            )
            session.add(row)
        else:
            row.realised_pnl = Decimal(str(portfolio_state.get("daily_realized_pnl", "0")))
            row.trade_count = int(portfolio_state.get("trades_today", 0))
            row.portfolio_value = Decimal(str(portfolio_state.get("portfolio_value", "0")))
            row.strategy_breakdown = {
                "risk_consecutive_losses": int(portfolio_state.get("consecutive_losses", 0)),
                "risk_cooldown_until": portfolio_state.get("cooldown_until"),
                "risk_daily_loss_accumulated": str(portfolio_state.get("daily_loss_accumulated", "0")),
            }
        await session.commit()


async def _run_once(args: argparse.Namespace) -> int:
    strategies_cfg = _load_yaml(args.strategies_config)
    pipeline_cfg = _load_yaml(args.pipeline_config)
    risk_cfg = _load_yaml(args.risk_config)
    ai_cfg = _load_yaml(args.ai_config)

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
        await apply_control_commands(bus, risk_engine=risk_engine, execution_engine=None, strategies=strategies)
        generated = 0
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
                logger.warning("run_m3 | no features | {} {}", symbol, args.timeframe)
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
                        "run_m3 | regime_gate_block | {} regime={} candidates={}",
                        symbol,
                        ai_result.macro_regime,
                        [r.strategy for r in raw_candidates],
                    )
                raw_candidates = filtered

            raw = _pick_best_signal(raw_candidates)
            if raw is None:
                logger.info("run_m3 | no_signal | {} {}", symbol, args.timeframe)
                continue

            signal = sig_engine.process(
                raw,
                portfolio_value=Decimal(str(args.portfolio_value)),
                news_score=(
                    ai_result.news_scores.get(symbol) if ai_result is not None else None
                ),
            )
            if signal is None:
                logger.info("run_m3 | vetoed | {} {}", symbol, raw.strategy)
                continue
            if ai_result is not None:
                signal.metadata["ai_macro_regime"] = ai_result.macro_regime
                signal.metadata["ai_macro_confidence"] = ai_result.macro_confidence
                signal.metadata["ai_news_detail"] = ai_result.news_details.get(symbol, {})
                signal.metadata["ai_anomalies"] = [a for a in ai_result.anomalies if a.get("symbol") == symbol]

            # M4: evaluate against live-ish portfolio state loaded from DB snapshots.
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
                logger.info(
                    "run_m3 | risk_rejected | {} {} | reason={}",
                    signal.symbol,
                    signal.strategy,
                    risk_decision.reason,
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
                await _persist_signal_ai_audit(
                    session_factory,
                    signal=signal,
                    macro_regime=ai_result.macro_regime,
                    macro_confidence=ai_result.macro_confidence,
                    news_detail=ai_result.news_details.get(symbol),
                    rationale=rationale,
                )
            await _persist_signal(
                session_factory,
                signal,
                paper_mode=not args.live,
                timeframe=args.timeframe,
                feature_ts=feature_ts,
            )
            _apply_signal_to_portfolio_state(portfolio_state, signal)
            await _persist_position_snapshot(session_factory, portfolio_state)
            await _upsert_daily_pnl(session_factory, portfolio_state)
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
        await publish_runner_heartbeat(
            bus,
            runner_name="run_m3",
            symbols=symbols,
            generated=generated,
            executed=0,
            extra={"paper_mode": not args.live},
        )
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
    p.add_argument("--ai-config", default="config/ai.yaml")
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

