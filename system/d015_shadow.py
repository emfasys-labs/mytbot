"""Optional D015 shadow logging alongside the legacy signal path (no orders)."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, cast

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from config.loaders import load_allocation, load_profile_modes
from core.models_runtime import AssetClass, PortfolioState, SignalCandidate
from portfolio.allocation_engine import build_allocation_decision
from risk.regime_state import compute_regime_state_async
from signals.opportunity_engine import build_opportunities_async


def d015_shadow_enabled() -> bool:
    v = os.getenv("ALLOCATOR_D015_SHADOW", "").strip().lower()
    return v in ("1", "true", "yes", "on")


def d015_allocator_enabled() -> bool:
    v = os.getenv("ALLOCATOR_D015_ENABLED", "").strip().lower()
    return v in ("1", "true", "yes", "on")


async def log_d015_shadow_for_signal(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    symbol: str,
    strategy_name: str,
    asset_class: str,
    side: str,
    confidence: float,
    adjusted_strength: Decimal,
    news_score: float | None,
    metadata: dict[str, Any],
    universe_symbols: list[str],
    nav_estimate: Decimal,
    capital_pct: float,
    mode: str,
    timeframe: str,
    legacy_suggested_qty: Decimal,
) -> None:
    if not d015_shadow_enabled():
        return
    try:
        alloc = load_allocation()
        profile = load_profile_modes()
        async with session_factory() as session:
            regime = await compute_regime_state_async(
                portfolio_state=PortfolioState(
                    timestamp=datetime.now(timezone.utc),
                    mode=mode if mode in profile.modes else profile.defaults.active_mode,
                    nav=nav_estimate,
                    cash=nav_estimate * Decimal("0.2"),
                    available_buying_power=nav_estimate,
                    gross_exposure=nav_estimate * Decimal("0.8"),
                    net_exposure=nav_estimate * Decimal("0.5"),
                    leverage_ratio=Decimal("1"),
                    metadata={"capital_pct": capital_pct},
                ),
                allocation_cfg=alloc,
                session=session,
                universe_symbols=universe_symbols or [symbol],
                timeframe=timeframe,
            )
            ac: AssetClass = cast(
                AssetClass,
                asset_class
                if asset_class
                in ("equity", "etf", "bond", "forex", "crypto", "future", "option", "other")
                else "other",
            )
            cand = SignalCandidate(
                symbol=symbol,
                asset_class=ac,
                side="long" if side.lower() == "buy" else "short",
                timestamp=datetime.now(timezone.utc),
                raw_signal_strength=adjusted_strength,
                adjusted_signal_strength=adjusted_strength,
                confidence=Decimal(str(confidence)),
                strategy_name=strategy_name,
                metadata={**metadata, "news_score": news_score} if news_score is not None else dict(metadata),
            )
            opps = await build_opportunities_async(
                signals=[cand],
                regime_state=regime,
                allocation_cfg=alloc,
                session=session,
                timeframe=timeframe,
                profile_cfg=profile,
                active_profile_mode=mode if mode in profile.modes else profile.defaults.active_mode,  # type: ignore[arg-type]
            )
            ps = PortfolioState(
                timestamp=datetime.now(timezone.utc),
                mode=mode if mode in profile.modes else profile.defaults.active_mode,
                nav=nav_estimate,
                cash=nav_estimate * Decimal("0.2"),
                available_buying_power=nav_estimate,
                gross_exposure=nav_estimate * Decimal("0.8"),
                net_exposure=nav_estimate * Decimal("0.5"),
                leverage_ratio=Decimal("1"),
                metadata={"capital_pct": capital_pct},
            )
            dec = build_allocation_decision(
                opportunities=opps,
                portfolio_state=ps,
                regime_state=regime,
                allocation_cfg=alloc,
                profile_cfg=profile,
            )
        payload = {
            "symbol": symbol,
            "legacy_suggested_qty": str(legacy_suggested_qty),
            "d015_opportunity_score": str(opps[0].opportunity_score) if opps else None,
            "d015_gross_exposure_target": str(dec.gross_exposure_target),
            "d015_replacements": len(dec.replacement_candidates),
            "regime": regime.regime_label,
        }
        logger.info("d015_shadow | {}", json.dumps(payload, default=str))
    except Exception as exc:  # noqa: BLE001
        logger.debug("d015_shadow | skipped | {}", exc)
