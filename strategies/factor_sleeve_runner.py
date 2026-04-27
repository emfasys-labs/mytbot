"""
strategies/factor_sleeve_runner.py
====================================
Wave 3 (wiring) — async glue that fetches close-price history from
``feature_snapshots`` and invokes ``strategies.factor_sleeve.FactorSleeve``.

This module is the only place that touches the database for the factor
sleeve. The sleeve itself stays pure (it operates on
``FactorUniverseInput`` rows that the runner constructs).

Boundary discipline:

- Disabled by default (gated by ``FactorSleeveConfig.enabled``).
- Reads only — never writes.
- Returns ``SignalCandidate``s ready for ``build_opportunities``; risk
  and execution decide what (if anything) trades.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Iterable, Mapping, Optional

import pandas as pd
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.models_runtime import SignalCandidate
from storage.models import FeatureSnapshot
from strategies.factor_sleeve import (
    FactorSleeve,
    FactorSleeveConfig,
    FactorUniverseInput,
)

logger = logging.getLogger(__name__)


# ── price loader ────────────────────────────────────────────────────────────


async def load_close_series(
    session: AsyncSession,
    symbol: str,
    *,
    timeframe: str,
    lookback_bars: int,
) -> Optional[pd.Series]:
    """
    Load the most recent ``lookback_bars`` close prices for ``symbol``
    at ``timeframe`` from ``feature_snapshots``. Returns ``None`` when
    no rows exist.
    """
    sym = symbol[:32]
    tf = timeframe[:8]
    stmt = (
        select(FeatureSnapshot.bar_timestamp, FeatureSnapshot.close)
        .where(FeatureSnapshot.symbol == sym, FeatureSnapshot.timeframe == tf)
        .order_by(FeatureSnapshot.bar_timestamp.desc())
        .limit(int(lookback_bars))
    )
    rows = (await session.execute(stmt)).all()
    if not rows:
        return None
    rows = sorted(rows, key=lambda r: r[0])
    idx = pd.DatetimeIndex([r[0] for r in rows], tz="UTC")
    vals = [float(r[1]) for r in rows]
    return pd.Series(vals, index=idx, name="close")


async def collect_factor_sleeve_candidates(
    session: AsyncSession,
    symbols: Iterable[str],
    *,
    timeframe: str,
    lookback_bars: int,
    config: FactorSleeveConfig,
    asset_class_for_symbol: Optional[Mapping[str, str]] = None,
    benchmark_symbol: Optional[str] = None,
    fundamentals_for_symbol: Optional[Mapping[str, Mapping[str, object]]] = None,
    as_of: Optional[datetime] = None,
) -> list[SignalCandidate]:
    """
    Collect factor-sleeve candidates for ``symbols``.

    Returns an empty list when:
      - the sleeve is disabled,
      - no symbols are provided, or
      - none of the symbols have enough close history to score.

    Any DB error on a single symbol is logged and skipped — the sleeve
    must never crash the trading loop. Other strategies and the
    allocator continue regardless.
    """
    if not config.enabled:
        return []

    syms = [s for s in symbols if s]
    if not syms:
        return []

    # Optional benchmark.
    bench: Optional[pd.Series] = None
    if benchmark_symbol:
        try:
            bench = await load_close_series(
                session, benchmark_symbol, timeframe=timeframe, lookback_bars=lookback_bars
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "factor_sleeve_runner | benchmark fetch failed (%s) — proceeding without",
                exc,
            )
            bench = None

    universe: list[FactorUniverseInput] = []
    for sym in syms:
        try:
            close = await load_close_series(
                session, sym, timeframe=timeframe, lookback_bars=lookback_bars
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("factor_sleeve_runner | %s close-fetch failed: %s", sym, exc)
            continue
        if close is None or len(close) < 30:
            continue
        ac = "other"
        if asset_class_for_symbol:
            ac = (asset_class_for_symbol.get(sym) or asset_class_for_symbol.get(sym.upper()) or "other")
        fund = None
        if fundamentals_for_symbol:
            fund = fundamentals_for_symbol.get(sym) or fundamentals_for_symbol.get(sym.upper())
        universe.append(
            FactorUniverseInput(
                symbol=sym,
                asset_class=str(ac),
                close=close,
                benchmark_close=bench,
                fundamentals=fund,
            )
        )

    if not universe:
        return []

    sleeve = FactorSleeve(config)
    candidates, _scores = sleeve.evaluate(universe, as_of=as_of)
    if candidates:
        logger.info(
            "factor_sleeve_runner | universe=%d candidates=%d", len(universe), len(candidates)
        )
    return candidates
