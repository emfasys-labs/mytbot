"""
Sum broker-reported balances as a proxy for total account equity (async).
Used by the API dashboard and the trading loop so both agree on "total balance".
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from control.runtime import get_risk_engine


_NAV_CACHE_ATTR = "_portfolio_equity_value_cache"


@dataclass(frozen=True)
class PortfolioValueSnapshot:
    value: Decimal
    complete: bool
    included: tuple[str, ...]
    missing: tuple[str, ...]


def _cache_ttl_seconds() -> float:
    raw = os.getenv("LIVE_PORTFOLIO_VALUE_CACHE_TTL_SEC", "45")
    try:
        return max(0.0, float(raw))
    except (TypeError, ValueError):
        return 45.0


def _cache_for(broker_manager: Any) -> dict[str, tuple[Decimal, float]]:
    cache = getattr(broker_manager, _NAV_CACHE_ATTR, None)
    if isinstance(cache, dict):
        return cache
    cache = {}
    try:
        setattr(broker_manager, _NAV_CACHE_ATTR, cache)
    except Exception:  # noqa: BLE001
        pass
    return cache


def _cached_value(
    cache: dict[str, tuple[Decimal, float]],
    name: str,
    now: float,
    ttl: float,
) -> Decimal:
    if ttl <= 0:
        return Decimal(0)
    cached = cache.get(name)
    if cached is None:
        return Decimal(0)
    value, at = cached
    if now - at <= ttl and value > 0:
        return value
    return Decimal(0)


def _nav_allowlist(broker_manager: Any) -> set[str] | None:
    """
    Brokers whose equity counts toward the headline NAV.

    None => caller has no :class:`BrokerReport` (e.g. some tests); all adapters
    in ``broker_manager.adapters`` are included for backward compatibility.

    A non-None (possibly empty) set means: only these lowercase names are
    included — matching :attr:`BrokerReport.included_names` (connected and
    balance_ready).
    """
    report = getattr(broker_manager, "report", None)
    if report is None:
        return None
    return {str(n).strip().lower() for n in getattr(report, "included_names", []) or []}


def _disabled_broker_names() -> frozenset[str]:
    re = get_risk_engine()
    if re is None:
        return frozenset()
    d = getattr(re, "disabled_brokers", None)
    if d is None:
        return frozenset()
    return frozenset(str(x).strip().lower() for x in d)


def _per_adapter_total(balances: list[Any]) -> Decimal:
    if not balances:
        return Decimal(0)
    base_rows = [b for b in balances if str(getattr(b, "currency", "") or "").upper() == "BASE"]
    if base_rows:
        row = max(base_rows, key=lambda b: b.total)
        if row.total > 0:
            return row.total
        return Decimal(0)
    best = max(balances, key=lambda b: b.total)
    if best.total > 0:
        return best.total
    return Decimal(0)


def _zero_balance_is_complete(name: str, balances: list[Any]) -> bool:
    # IBKR's BASE row is the authoritative account NAV; an empty/zero IBKR
    # snapshot is not a credible "zero account". Crypto/secondary venues can
    # legitimately have an empty wallet while still being connected and usable.
    return name != "ibkr" and balances is not None


async def live_portfolio_snapshot(broker_manager: Any | None) -> PortfolioValueSnapshot:
    """
    Sum one equity figure per connected adapter (avoid double-counting duplicate CCY rows).

    Only adapters in :attr:`BrokerManager.report` ``included_names`` (i.e. **connected** and
    **balance_ready**) are summed. Adapters in the risk engine's ``disabled_brokers`` set
    are skipped so excluded venues never affect NAV, even if an adapter object remains
    in ``broker_manager.adapters`` with a stale :meth:`get_balance` snapshot.

    IBKR often reports NetLiquidation on the BASE row; taking ``max`` across all currencies
    can pick a small cash line instead of account NAV, understating live equity.
    """
    if broker_manager is None:
        return PortfolioValueSnapshot(Decimal(0), False, tuple(), tuple())
    allow = _nav_allowlist(broker_manager)
    disabled = _disabled_broker_names()
    cache = _cache_for(broker_manager)
    ttl = _cache_ttl_seconds()
    now = time.monotonic()
    total = Decimal(0)
    included: list[str] = []
    missing: list[str] = []
    # Snapshot adapters to avoid concurrent mutation during late broker connects.
    for name, adapter in list(broker_manager.adapters.items()):
        n = str(name).strip().lower()
        if allow is not None and n not in allow:
            continue
        if n in disabled:
            continue
        included.append(n)
        try:
            balances = await adapter.get_balance()
        except Exception:  # noqa: BLE001
            cached = _cached_value(cache, n, now, ttl)
            if cached > 0:
                total += cached
            else:
                missing.append(n)
            continue
        value = _per_adapter_total(balances)
        if value > 0:
            cache[n] = (value, now)
            total += value
        elif _zero_balance_is_complete(n, list(balances)):
            total += Decimal(0)
        else:
            cached = _cached_value(cache, n, now, ttl)
            if cached > 0:
                total += cached
            else:
                missing.append(n)
    if allow is not None:
        known = {str(name).strip().lower() for name in getattr(broker_manager, "adapters", {})}
        for n in sorted(allow - disabled - known):
            missing.append(n)
    complete = bool(included) and not missing
    return PortfolioValueSnapshot(total if complete else Decimal(0), complete, tuple(included), tuple(missing))


async def live_portfolio_value(broker_manager: Any | None) -> Decimal:
    """Return a complete live NAV, or zero when included broker coverage is incomplete."""
    return (await live_portfolio_snapshot(broker_manager)).value
