"""
system/strategy_pnl_health.py
==============================
Per-strategy rolling P&L lookup, used by ``dynamic_thresholds.base_target_notional``
to shrink sizing for bleeding strategies and grow it for winners.

We cache results for ``cache_ttl_sec`` seconds (YAML-tunable) so the
trading loop doesn't hammer the fills ledger every iteration. A
single query covers all strategies in one round-trip.

Empty / unavailable ledger → empty dict → ``base_target_notional`` falls
back to neutral sizing (no P&L adjustment).
"""

from __future__ import annotations

import time
from decimal import Decimal
from typing import Any

from loguru import logger


_DEFAULT_LOOKBACK = 100   # last N fills per strategy
_DEFAULT_CACHE_TTL = 300  # 5 minutes

_cache: dict[str, tuple[float, dict[str, dict[str, Any]]]] = {}


async def fetch_strategy_pnl_recent(
    session_factory,
    *,
    lookback_fills: int = _DEFAULT_LOOKBACK,
    cache_ttl_sec: float = _DEFAULT_CACHE_TTL,
) -> dict[str, dict[str, Any]]:
    """Return ``{strategy: {"net_pnl": Decimal, "fills": int, "win_rate": float,
    "profit_factor": float | None}}`` for the last ``lookback_fills`` per
    strategy across the live fills ledger.

    D231 (P1.5/P2) — grouped by ``opening_strategy`` (the strategy that
    OPENED the lot), not ``strategy`` (which on a closing fill names the exit
    mechanism — stop_loss_monitor / capital_recycle / ...). This is what lets
    the orchestrator's trust posterior (``TradingLoop._orchestrator_strategy_trust``)
    actually answer "is trend_following live-profitable", instead of silently
    keying on the wrong strategy name and never matching the edge-gate prior.
    Closing fills recorded before the D231 migration have ``opening_strategy
    = NULL`` and are excluded (cannot be attributed).

    The cache key is ``(session_factory id, lookback)`` so different loop
    instances don't share stale results. Returns ``{}`` on any failure so
    the caller can fall back to neutral behaviour.
    """
    if session_factory is None:
        return {}
    cache_key = f"{id(session_factory)}:{lookback_fills}"
    now = time.monotonic()
    entry = _cache.get(cache_key)
    if entry is not None and (now - entry[0]) < cache_ttl_sec:
        return entry[1]

    try:
        from sqlalchemy import select
        from storage.models import FillLog

        async with session_factory() as session:
            # Pull a generous slice of recent fills for any strategy and
            # aggregate in-Python — one query, not N strategy queries.
            stmt = (
                select(
                    FillLog.opening_strategy,
                    FillLog.realised_pnl,
                    FillLog.fee,
                )
                .where(FillLog.realised_pnl != 0, FillLog.opening_strategy.isnot(None))
                .order_by(FillLog.timestamp.desc())
                .limit(lookback_fills * 20)  # cushion for many strategies
            )
            res = await session.execute(stmt)
            rows = list(res.all())
    except Exception as exc:  # noqa: BLE001 — never break the loop on a DB hiccup
        logger.debug("strategy_pnl_health | query failed: {}", exc)
        return {}

    if not rows:
        _cache[cache_key] = (now, {})
        return {}

    # Group by strategy, take only the most recent ``lookback_fills`` per.
    per_strat: dict[str, list[tuple[Decimal, Decimal]]] = {}
    for strat, rpnl, fee in rows:
        if not strat:
            continue
        key = str(strat)
        bucket = per_strat.setdefault(key, [])
        if len(bucket) < lookback_fills:
            bucket.append((Decimal(str(rpnl or 0)), Decimal(str(fee or 0))))

    out: dict[str, dict[str, Any]] = {}
    for strat, items in per_strat.items():
        gross = sum((r for r, _f in items), Decimal("0"))
        fees = sum((f for _r, f in items), Decimal("0"))
        net = gross - fees
        wins = sum(1 for r, _f in items if r > 0)
        win_sum = sum((r for r, _f in items if r > 0), Decimal("0"))
        loss_sum = sum((r for r, _f in items if r < 0), Decimal("0"))
        n = len(items)
        pf: float | None
        if loss_sum < 0:
            pf = float(win_sum / abs(loss_sum))
        elif win_sum > 0:
            pf = float("inf")
        else:
            pf = None
        out[strat] = {
            "net_pnl": net,
            "fills": n,
            "win_rate": float(wins) / float(n) if n > 0 else 0.0,
            "profit_factor": pf,
        }
    _cache[cache_key] = (now, out)
    return out


def reset_cache() -> None:
    """Test helper — drop any cached results."""
    _cache.clear()
