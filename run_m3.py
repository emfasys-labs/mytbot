#!/usr/bin/env python3
"""
M3 runner: generate paper-mode signals from feature store and persist to signals table.
"""

from __future__ import annotations

import argparse
import asyncio
import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Awaitable, Callable

import pandas as pd
import yaml
from dotenv import load_dotenv
from loguru import logger
from sqlalchemy import and_, func, select

from ai.news_classifier import NewsClassifier
from ai.pipeline import AIPipeline
from ai.router import AIRouter
from ai.regime import filter_by_allowed_strategies
from control.command_bus import CommandBus
from control.runner_control import apply_control_commands, hydrate_risk_parameters_from_bus, publish_runner_heartbeat
from control.runtime import set_risk_engine
from control.startup_validation import validate_startup_env
from core.instruments import parse_option_contract_from_metadata
from risk.engine import RiskEngine, RiskVerdict
from risk.m8_loader import merge_m8_into_risk_cfg
from risk.options_env import merge_options_env_into_risk_cfg
from signals.accumulator import SignalAccumulator
from signals.engine import RawSignal, SignalEngine
from storage.db import dispose_engine, init_async_database
from storage.models import AIOutputLog, DailyPnL, FeatureSnapshot, OrderLog, PositionLog, SignalLog
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


def _coerce_decimal(value) -> "Decimal | None":
    """Best-effort cast that tolerates floats, strings, and bad inputs."""
    if value is None:
        return None
    try:
        if isinstance(value, Decimal):
            return value
        s = str(value).strip()
        if not s:
            return None
        return Decimal(s)
    except (TypeError, ValueError, InvalidOperation):
        return None


def _resolve_signal_news_score(signal) -> "Decimal | None":
    """Pick the best available news_score for SignalLog persistence.

    Priority:
      1. The ``Signal.news_score`` attribute (legacy ``signals.engine.Signal``
         path always sets this from accumulator / AI).
      2. ``metadata.news_score`` (direct AI passthrough).
      3. ``metadata.accumulator_score`` (decayed multi-source conviction).
      4. ``metadata.ai_news_score`` (point-in-time AI sentiment).

    Returns ``None`` only when no signed score is available anywhere.
    The ``risk.engine.Signal`` dataclass (D015 execution path) has no
    ``news_score`` field, so this fallback is the difference between
    rich audit data and a NULL column.
    """
    direct = _coerce_decimal(getattr(signal, "news_score", None))
    if direct is not None:
        return direct
    md = getattr(signal, "metadata", None) or {}
    for key in ("news_score", "accumulator_score", "ai_news_score"):
        v = _coerce_decimal(md.get(key))
        if v is not None:
            return v
    return None


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
        symbol=signal.symbol[:72],
        side=signal.side[:4],
        strategy=signal.strategy[:50],
        confidence=Decimal(str(signal.confidence)),
        asset_class=signal.asset_class[:20],
        broker=signal.broker[:20],
        news_score=_resolve_signal_news_score(signal),
        news_veto=bool(getattr(signal, "news_veto", False)),
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
                source=str(detail.get("provider", "local")),
                signal_id=signal.signal_id,
            )
        )
        await session.commit()


def _position_log_notional(
    row: PositionLog,
    signal_price_fallback: Decimal | None,
) -> tuple[Decimal, bool]:
    qty = abs(Decimal(str(row.quantity)))
    px = Decimal(str(row.current_price or signal_price_fallback or "0"))
    ac = (row.asset_class or "").strip().lower()
    is_opt = ac == "option"
    mult = Decimal(100)
    meta = getattr(row, "instrument_metadata", None)
    if is_opt and isinstance(meta, dict):
        try:
            mult = Decimal(str(meta.get("multiplier", 100)))
        except Exception:  # noqa: BLE001
            mult = Decimal(100)
    if is_opt:
        return qty * px * mult, True
    return qty * px, False


def _position_dict_notional(p: dict[str, Any]) -> Decimal:
    qty = abs(Decimal(str(p.get("quantity", "0"))))
    px = Decimal(str(p.get("current_price", "0")))
    ac = str(p.get("asset_class", "")).strip().lower()
    if ac == "option":
        meta = p.get("instrument_metadata") if isinstance(p.get("instrument_metadata"), dict) else {}
        try:
            mult = Decimal(str(meta.get("multiplier", 100)))
        except Exception:  # noqa: BLE001
            mult = Decimal(100)
        return qty * px * mult
    return qty * px


def _resolve_portfolio_value_for_state(
    fallback_portfolio_value: Decimal,
    pv_from_db: Decimal,
) -> Decimal:
    """
    Choose ``portfolio_value`` for M3 state, NAV heartbeat, and dashboard snapshots.

    In live mode, broker equity (``fallback_portfolio_value`` from
    :func:`system.portfolio_equity.live_portfolio_value`, post-D031 allowlist)
    must win when it is **> 0**. Taking ``max(live, db)`` with ``daily_pnl``
    reintroduced a self-perpetuating stale higher value after IBKR was excluded.

    In paper mode, the persisted paper ledger is the account of record. Broker
    balances are real venue cash snapshots and can be far below the simulated
    paper NAV; using them creates a fake drawdown that blocks all opens.
    """
    is_live = os.getenv("APP_ENV", "paper").strip().lower() == "live"
    if not is_live and pv_from_db > 0:
        return pv_from_db
    if fallback_portfolio_value > 0:
        return fallback_portfolio_value
    if pv_from_db > 0:
        return pv_from_db
    return fallback_portfolio_value


async def _load_portfolio_state(
    session_factory,
    *,
    fallback_portfolio_value: Decimal,
    signal_price_fallback: Decimal | None = None,
    capital_pct: Decimal | None = None,
) -> dict[str, Any]:
    """
    Build portfolio state from DB snapshots (M4 risk checks).

    ``portfolio_value`` is total account equity (sum of balances / configured NAV).
    ``tradable_capital`` is the slice allowed for trading: portfolio_value × capital_pct
    (when capital_pct is omitted, tradable_capital equals portfolio_value).
    """
    daily_realized_pnl = Decimal("0")
    current_gross_exposure = Decimal("0")
    symbol_exposure: dict[str, Decimal] = {}
    asset_class_exposure: dict[str, Decimal] = {}
    positions: dict[str, dict[str, Any]] = {}
    trades_today = 0
    consecutive_losses = 0
    cooldown_until: str | None = None
    daily_loss_accumulated = Decimal("0")
    pv_from_db = Decimal("0")
    option_premium_exposure = Decimal("0")

    async with session_factory() as session:
        latest_pnl_q = await session.execute(
            select(DailyPnL).order_by(DailyPnL.id.desc()).limit(1)
        )
        latest_pnl = latest_pnl_q.scalars().first()
        if latest_pnl is not None:
            pv_from_db = Decimal(str(latest_pnl.portfolio_value or "0"))
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

        latest_by_key = (
            select(
                PositionLog.broker.label("broker"),
                PositionLog.symbol.label("symbol"),
                func.max(PositionLog.timestamp).label("max_ts"),
            )
            .group_by(PositionLog.broker, PositionLog.symbol)
            .subquery()
        )
        latest_rows_q = await session.execute(
            select(PositionLog).join(
                latest_by_key,
                and_(
                    PositionLog.broker == latest_by_key.c.broker,
                    PositionLog.symbol == latest_by_key.c.symbol,
                    PositionLog.timestamp == latest_by_key.c.max_ts,
                ),
            )
        )
        rows = list(latest_rows_q.scalars().all())
        if rows:
            for row in rows:
                qty = Decimal(str(row.quantity))
                if qty == 0:
                    continue
                px = Decimal(str(row.current_price or signal_price_fallback or "0"))
                notional, is_option_row = _position_log_notional(row, signal_price_fallback)
                current_gross_exposure += notional
                if is_option_row:
                    option_premium_exposure += notional
                symbol = (row.symbol or "").strip()
                if symbol:
                    broker_key = (row.broker or "").strip()[:20]
                    position_key = symbol
                    existing = positions.get(position_key)
                    if existing is not None and str(existing.get("broker", "")).strip().lower() != broker_key.lower():
                        position_key = f"{broker_key}:{symbol}"
                    symbol_exposure[symbol] = symbol_exposure.get(symbol, Decimal("0")) + notional
                    entry: dict[str, Any] = {
                        "symbol": symbol,
                        "quantity": qty,
                        "avg_entry_price": Decimal(str(row.avg_entry_price or px)),
                        "current_price": px,
                        "asset_class": (row.asset_class or "").strip().lower(),
                        "broker": broker_key,
                    }
                    im = getattr(row, "instrument_metadata", None)
                    if isinstance(im, dict):
                        entry["instrument_metadata"] = im
                    positions[position_key] = entry
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

    portfolio_value = _resolve_portfolio_value_for_state(fallback_portfolio_value, pv_from_db)

    if hwm_raw is not None:
        high_watermark_value = Decimal(str(hwm_raw))
    else:
        high_watermark_value = portfolio_value
    if portfolio_value > high_watermark_value:
        high_watermark_value = portfolio_value

    alloc_pct = Decimal("1")
    if capital_pct is not None:
        try:
            alloc_pct = max(Decimal("0"), min(Decimal("1"), Decimal(str(capital_pct))))
        except Exception:  # noqa: BLE001
            alloc_pct = Decimal("1")
    tradable_capital = portfolio_value * alloc_pct

    return {
        "portfolio_value": portfolio_value,
        "tradable_capital": tradable_capital,
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
        "option_premium_exposure": option_premium_exposure,
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
    meta = signal.metadata if isinstance(getattr(signal, "metadata", None), dict) else {}
    spec = parse_option_contract_from_metadata(meta)
    symbol = spec.position_key() if spec is not None else signal.symbol
    position_key = str(meta.get("position_key") or symbol)
    side_mult = Decimal("1") if signal.side == "buy" else Decimal("-1")
    qty_delta = Decimal(str(signal.suggested_quantity)) * side_mult

    row = positions.get(position_key)
    if row is None and bool(meta.get("reduce_only") or meta.get("close_only")):
        broker = str(getattr(signal, "broker", "") or "").strip().lower()
        for k, candidate in positions.items():
            if not isinstance(candidate, dict):
                continue
            candidate_symbol = str(candidate.get("symbol") or k).strip()
            candidate_broker = str(candidate.get("broker", "")).strip().lower()
            if candidate_symbol == symbol and (not broker or candidate_broker == broker):
                position_key = str(k)
                row = candidate
                break
    if row is None:
        row = {
            "symbol": symbol,
            "quantity": Decimal("0"),
            "avg_entry_price": price,
            "current_price": price,
            "asset_class": (signal.asset_class or "").strip().lower(),
            "broker": (signal.broker or "").strip()[:20],
        }
        if spec is not None:
            row["asset_class"] = "option"
            row["instrument_metadata"] = spec.to_dict()
    prev_qty = Decimal(str(row["quantity"]))
    new_qty = prev_qty + qty_delta
    if spec is not None:
        row["instrument_metadata"] = spec.to_dict()
        row["asset_class"] = "option"
    if new_qty == 0:
        closed = list(portfolio_state.get("_closed_position_tombstones") or [])
        closed.append(
            {
                "symbol": symbol,
                "broker": str(row.get("broker", signal.broker or "ibkr"))[:20] or "ibkr",
                "avg_entry_price": Decimal(str(row.get("avg_entry_price", price) or price)),
                "current_price": price,
                "asset_class": str(row.get("asset_class", signal.asset_class or ""))[:20],
                "instrument_metadata": row.get("instrument_metadata") if isinstance(row.get("instrument_metadata"), dict) else None,
            }
        )
        portfolio_state["_closed_position_tombstones"] = closed
        positions.pop(position_key, None)
    else:
        row["quantity"] = new_qty
        row["current_price"] = price
        if prev_qty == 0:
            row["avg_entry_price"] = price
        elif (prev_qty > 0 and qty_delta > 0) or (prev_qty < 0 and qty_delta < 0):
            # Weighted average on add-to-position in same direction.
            old_notional = abs(prev_qty) * Decimal(str(row["avg_entry_price"]))
            add_notional = abs(qty_delta) * price
            if str(row.get("asset_class", "")).strip().lower() == "option":
                om = row.get("instrument_metadata") if isinstance(row.get("instrument_metadata"), dict) else {}
                try:
                    mult = Decimal(str(om.get("multiplier", 100)))
                except Exception:  # noqa: BLE001
                    mult = Decimal(100)
                old_notional *= mult
                add_notional *= mult
            denom = abs(new_qty)
            if denom > 0:
                avg = (old_notional + add_notional) / denom
                if str(row.get("asset_class", "")).strip().lower() == "option":
                    om = row.get("instrument_metadata") if isinstance(row.get("instrument_metadata"), dict) else {}
                    try:
                        mult = Decimal(str(om.get("multiplier", 100)))
                    except Exception:  # noqa: BLE001
                        mult = Decimal(100)
                    if mult > 0:
                        avg /= mult
                row["avg_entry_price"] = avg
        row["symbol"] = symbol
        positions[position_key] = row

    symbol_exposure: dict[str, Decimal] = {}
    asset_class_exposure: dict[str, Decimal] = {}
    current_gross_exposure = Decimal("0")
    option_premium_exposure = Decimal("0")
    for sym, p in positions.items():
        notional = _position_dict_notional(p)
        current_gross_exposure += notional
        symbol_exposure[sym] = symbol_exposure.get(sym, Decimal("0")) + notional
        asset = str(p.get("asset_class", "")).strip().lower()
        if asset:
            asset_class_exposure[asset] = asset_class_exposure.get(asset, Decimal("0")) + notional
        if asset == "option":
            option_premium_exposure += notional

    portfolio_state["positions"] = positions
    portfolio_state["symbol_exposure"] = symbol_exposure
    portfolio_state["asset_class_exposure"] = asset_class_exposure
    portfolio_state["current_gross_exposure"] = current_gross_exposure
    portfolio_state["option_premium_exposure"] = option_premium_exposure
    portfolio_state["trades_today"] = int(portfolio_state.get("trades_today", 0)) + 1


def _compute_unrealised_pnl(qty: Decimal, current_price: Decimal, avg_entry_price: Decimal) -> Decimal:
    """Mark-to-market unrealised P&L for a position row.

    Skipped (returns ``0``) when we don't have a usable current price — the
    column legitimately can't be filled until a mark refreshes. ``qty`` is
    signed: positive for long, negative for short, so the formula handles
    both sides naturally.
    """
    if qty == 0:
        return Decimal("0")
    if current_price <= 0 or avg_entry_price <= 0:
        return Decimal("0")
    return (current_price - avg_entry_price) * qty


async def _persist_position_snapshot(session_factory, portfolio_state: dict[str, Any]) -> None:
    positions = portfolio_state.get("positions", {})
    tombstones = list(portfolio_state.get("_closed_position_tombstones") or [])
    if not positions and not tombstones:
        return
    ts = datetime.now(timezone.utc)
    async with session_factory() as session:
        for symbol, p in positions.items():
            im = p.get("instrument_metadata") if isinstance(p.get("instrument_metadata"), dict) else None
            persisted_symbol = str(p.get("symbol") or symbol)
            qty = Decimal(str(p.get("quantity", "0")))
            avg = Decimal(str(p.get("avg_entry_price", "0")))
            cur = Decimal(str(p.get("current_price", "0")))
            row = PositionLog(
                timestamp=ts,
                symbol=persisted_symbol[:72],
                broker=str(p.get("broker", "ibkr"))[:20] or "ibkr",
                quantity=qty,
                avg_entry_price=avg,
                current_price=cur,
                unrealised_pnl=_compute_unrealised_pnl(qty, cur, avg),
                asset_class=str(p.get("asset_class", ""))[:20],
                instrument_metadata=im,
            )
            session.add(row)
        for p in tombstones:
            im = p.get("instrument_metadata") if isinstance(p.get("instrument_metadata"), dict) else None
            row = PositionLog(
                timestamp=ts,
                symbol=str(p.get("symbol", ""))[:72],
                broker=str(p.get("broker", "ibkr"))[:20] or "ibkr",
                quantity=Decimal("0"),
                avg_entry_price=Decimal(str(p.get("avg_entry_price", "0") or "0")),
                current_price=Decimal(str(p.get("current_price", "0") or "0")),
                unrealised_pnl=Decimal("0"),
                asset_class=str(p.get("asset_class", ""))[:20],
                instrument_metadata=im,
            )
            if row.symbol:
                session.add(row)
        await session.commit()


async def _refresh_position_marks_and_persist(
    session_factory,
    *,
    timeframe: str,
    price_oracle: "Callable[[str], Awaitable[Decimal]] | None" = None,
) -> int:
    """Mark-to-market sweep: refresh every open position's ``unrealised_pnl``.

    Reads the latest persisted row per (broker, symbol), looks up the most
    recent close price (default: ``feature_snapshots``; pass a ``price_oracle``
    for venue-native prices), recomputes mark-to-market, and writes a fresh
    ``PositionLog`` row.

    Without this sweep, inherited positions that haven't been touched since a
    crash carry stale ``current_price`` values from their last fill, and the
    ``daily_pnl_unrealised_differs_from_open_book`` accounting check stays
    flagged forever. Run this once per trading-loop iteration so every open
    position is freshly marked before snapshots publish.

    Returns the number of position rows refreshed.
    """
    ts = datetime.now(timezone.utc)
    refreshed = 0
    async with session_factory() as session:
        latest_by_key = (
            select(
                PositionLog.broker.label("broker"),
                PositionLog.symbol.label("symbol"),
                func.max(PositionLog.timestamp).label("max_ts"),
            )
            .group_by(PositionLog.broker, PositionLog.symbol)
            .subquery()
        )
        latest_q = await session.execute(
            select(PositionLog).join(
                latest_by_key,
                and_(
                    PositionLog.broker == latest_by_key.c.broker,
                    PositionLog.symbol == latest_by_key.c.symbol,
                    PositionLog.timestamp == latest_by_key.c.max_ts,
                ),
            )
        )
        latest_rows = list(latest_q.scalars().all())
        open_rows = [r for r in latest_rows if Decimal(str(r.quantity or 0)) != 0]

        # Fall back to a feature_snapshots close lookup when no oracle is
        # supplied — paper-mode trading loops always have features warm.
        async def _fallback_price(sym: str) -> Decimal:
            df, _ = await _load_recent_features(
                session_factory,
                symbol=sym,
                timeframe=timeframe,
                lookback_bars=2,
            )
            if df is None or not hasattr(df, "empty") or df.empty:
                return Decimal("0")
            if "close" not in df.columns:
                return Decimal("0")
            try:
                return Decimal(str(float(df["close"].iloc[-1])))
            except Exception:  # noqa: BLE001
                return Decimal("0")

        for row in open_rows:
            sym = str(row.symbol or "")
            if not sym:
                continue
            qty = Decimal(str(row.quantity or 0))
            avg = Decimal(str(row.avg_entry_price or 0))
            # Price priority: live oracle (venue-native, all asset classes)
            # → feature-snapshot close (equities only) → last-known mark.
            # The old code used oracle XOR feature; with feature-only (no
            # oracle) the ~40 forex/futures/crypto symbols absent from the
            # M2 feature pipeline NEVER got a real price and were re-stamped
            # at entry forever (current_price == avg → unrealised stuck at
            # $0 for 62% of the book). Chaining fixes that at the source.
            px = Decimal("0")
            if price_oracle is not None:
                try:
                    px = await price_oracle(sym)
                except Exception:  # noqa: BLE001
                    px = Decimal("0")
            if px <= 0:
                try:
                    px = await _fallback_price(sym)
                except Exception:  # noqa: BLE001
                    px = Decimal("0")
            if px <= 0:
                # Still nothing — keep the last-known price (best effort).
                px = Decimal(str(row.current_price or 0))
            unreal = _compute_unrealised_pnl(qty, px, avg)
            new_row = PositionLog(
                timestamp=ts,
                symbol=sym[:72],
                broker=str(row.broker or "ibkr")[:20] or "ibkr",
                quantity=qty,
                avg_entry_price=avg,
                current_price=px,
                unrealised_pnl=unreal,
                asset_class=str(row.asset_class or "")[:20],
                instrument_metadata=row.instrument_metadata if isinstance(row.instrument_metadata, dict) else None,
            )
            session.add(new_row)
            refreshed += 1
        if refreshed > 0:
            await session.commit()
    return refreshed


async def _compute_today_realised_pnl(session) -> Decimal:
    """Round-trip realised P&L from today's filled orders.

    Replays the day's fill sequence against a per-(broker, symbol) position
    state and sums ``(gross - fee)`` whenever a fill closes part of an
    existing position. Mirrors the API's ``_filled_order_net_pnl_for_period``
    so the persisted ``daily_pnl.realised_pnl`` always equals what the API
    computes — previously the column read 0 while the API showed thousands.

    Returns 0 when no fills are found.
    """
    today = datetime.now(timezone.utc).date()
    start_dt = datetime.combine(today, datetime.min.time(), tzinfo=timezone.utc)
    end_dt = start_dt + timedelta(days=1)
    q = await session.execute(
        select(OrderLog)
        .where(
            OrderLog.timestamp >= start_dt,
            OrderLog.timestamp < end_dt,
            OrderLog.status.in_(("filled", "partially_filled")),
        )
        .order_by(OrderLog.timestamp.asc(), OrderLog.id.asc())
    )
    rows = list(q.scalars().all())
    if not rows:
        return Decimal("0")

    state: dict[tuple[str, str], tuple[Decimal, Decimal]] = {}
    total = Decimal("0")
    for r in rows:
        broker = str(r.broker or "").strip().lower()
        symbol = str(r.symbol or "").strip().upper()
        key = (broker, symbol)
        pos_qty, pos_avg = state.get(key, (Decimal("0"), Decimal("0")))
        try:
            fill_qty = Decimal(str(r.filled_quantity if r.filled_quantity is not None else r.quantity or 0))
            fill_price = Decimal(str(r.avg_fill_price if r.avg_fill_price is not None else r.limit_price or 0))
            fee = Decimal(str(r.fee or 0))
        except (TypeError, ValueError, InvalidOperation):
            continue
        side_l = str(r.side or "").strip().lower()
        if side_l not in {"buy", "sell"} or fill_qty <= 0 or fill_price <= 0:
            continue
        signed_fill = fill_qty if side_l == "buy" else -fill_qty
        closing_qty = Decimal("0")
        gross = Decimal("0")
        if pos_qty > 0 and signed_fill < 0:
            closing_qty = min(pos_qty, abs(signed_fill))
            gross = (fill_price - pos_avg) * closing_qty
        elif pos_qty < 0 and signed_fill > 0:
            closing_qty = min(abs(pos_qty), signed_fill)
            gross = (pos_avg - fill_price) * closing_qty
        if closing_qty > 0:
            fee_alloc = fee * (closing_qty / fill_qty)
            total += gross - fee_alloc
        new_qty = pos_qty + signed_fill
        eps = Decimal("0.00000001")
        if abs(new_qty) <= eps:
            new_qty = Decimal("0")
            new_avg = fill_price
        elif pos_qty == 0 or (pos_qty > 0 and signed_fill > 0) or (pos_qty < 0 and signed_fill < 0):
            total_abs = abs(pos_qty) + abs(signed_fill)
            new_avg = (
                ((abs(pos_qty) * pos_avg) + (abs(signed_fill) * fill_price)) / total_abs
                if total_abs > 0
                else fill_price
            )
        elif abs(signed_fill) < abs(pos_qty):
            new_avg = pos_avg
        else:
            new_avg = fill_price
        state[key] = (new_qty, new_avg)
    return total


async def _upsert_daily_pnl(session_factory, portfolio_state: dict[str, Any]) -> None:
    now = datetime.now(timezone.utc)
    d = now.date().isoformat()
    # `fees_today_delta` is set by the trading loop when a fill produces fees;
    # we accumulate into the existing row rather than overwriting, so we don't
    # lose earlier fills' fees when later fills land on the same day.
    try:
        fee_delta = Decimal(str(portfolio_state.get("fees_today_delta", "0")))
    except Exception:  # noqa: BLE001
        fee_delta = Decimal("0")
    # Compute today's unrealised pnl from the live position snapshot so
    # operators have a real number, not a hard-coded 0.
    try:
        positions = portfolio_state.get("positions", {}) or {}
        unreal = Decimal("0")
        for _sym, p in positions.items():
            try:
                qty = Decimal(str(p.get("quantity", "0")))
                avg = Decimal(str(p.get("avg_entry_price", "0")))
                cur = Decimal(str(p.get("current_price", "0")))
                if qty != 0 and avg > 0 and cur > 0:
                    unreal += (cur - avg) * qty
            except Exception:  # noqa: BLE001
                continue
    except Exception:  # noqa: BLE001
        unreal = Decimal("0")

    async with session_factory() as session:
        # Always compute realised P&L from today's orders so the persisted
        # row matches the API's round-trip view. The state-passed
        # ``daily_realized_pnl`` only updates in the legacy execution path;
        # D015 batch closes never accumulate into it, which is why the
        # ``daily_pnl.realised_pnl`` column used to read 0 while
        # ``/pnl`` correctly reported thousands.
        try:
            realised_today = await _compute_today_realised_pnl(session)
        except Exception as exc:  # noqa: BLE001
            logger.debug("daily_pnl | realised compute failed, falling back to state: {}", exc)
            realised_today = Decimal(str(portfolio_state.get("daily_realized_pnl", "0")))
        q = await session.execute(select(DailyPnL).where(DailyPnL.date == d).limit(1))
        row = q.scalars().first()
        if row is None:
            row = DailyPnL(
                date=d,
                realised_pnl=realised_today,
                unrealised_pnl=unreal,
                total_fees=fee_delta,
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
            row.realised_pnl = realised_today
            row.unrealised_pnl = unreal
            if fee_delta and fee_delta != Decimal("0"):
                try:
                    prev_fees = Decimal(str(row.total_fees or "0"))
                except Exception:  # noqa: BLE001
                    prev_fees = Decimal("0")
                row.total_fees = prev_fees + fee_delta
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
    merge_m8_into_risk_cfg(risk_cfg, args.m8_config)
    merge_options_env_into_risk_cfg(risk_cfg)
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
        _se_cfg = strategies_cfg.get("signal_engine", {}) or {}
        _acc = SignalAccumulator() if bool(_se_cfg.get("use_signal_accumulator", False)) else None
        sig_engine = SignalEngine(_se_cfg, accumulator=_acc)
        risk_engine = RiskEngine(risk_cfg)
        set_risk_engine(risk_engine)
        ai_enabled = bool(ai_cfg.get("enabled", True))
        ai_mode = str(ai_cfg.get("mode", "local_first")).strip().lower()
        if ai_enabled and ai_mode != "api_only":
            ai_classifier = AIRouter(ai_cfg)
        elif ai_enabled:
            ai_classifier = NewsClassifier()
        else:
            ai_classifier = None
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
        await apply_control_commands(bus, risk_engine=risk_engine, execution_engine=None, strategies=strategies)
        generated = 0
        ai_result = None
        if ai_pipeline is not None:
            ai_result = await ai_pipeline.compute(session_factory, symbols)
            await ai_pipeline.persist(session_factory, ai_result)

        if sig_engine.accumulator is not None and ai_result is not None:
            sig_engine.accumulator.feed_ai_pipeline_result(
                ai_result,
                symbols,
                now=datetime.now(timezone.utc),
            )

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
    validate_startup_env(component="run_m3.py", require_postgres=True, strict=True)
    p = argparse.ArgumentParser(description="M3 signal generation runner")
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
    p.add_argument(
        "--timeframe",
        default="1h",
        help="Must match feature_snapshots timeframe (incremental pipeline default is 1h)",
    )
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

