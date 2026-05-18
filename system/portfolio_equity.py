"""
Sum broker-reported balances as a proxy for total account equity (async).
Used by the API dashboard and the trading loop so both agree on "total balance".
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
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
    # Per-broker contributed equity (broker -> stringified Decimal). Empty by
    # default so every existing positional constructor stays valid; populated
    # by ``live_portfolio_snapshot`` for the read-only balances breakdown.
    per_broker: dict[str, str] = field(default_factory=dict)


def _cache_ttl_seconds() -> float:
    raw = os.getenv("LIVE_PORTFOLIO_VALUE_CACHE_TTL_SEC", "45")
    try:
        return max(0.0, float(raw))
    except (TypeError, ValueError):
        return 45.0


def _extended_cache_ttl_seconds() -> float:
    """Fallback staleness window for brokers that the manager confirmed are
    ``balance_ready`` but which momentarily return an empty ``get_balance``.

    IBKR's paper account snapshot is intermittent: a single 1-2 minute window
    of empty replies can flip the NAV banner to "WAITING FOR IBKR" even when
    the trading loop is otherwise happily filling orders. As long as the
    broker manager periodically marks the adapter ``balance_ready`` we trust
    the last good value for this window (default 10 min).
    """
    raw = os.getenv("LIVE_PORTFOLIO_VALUE_EXT_CACHE_TTL_SEC", "600")
    try:
        return max(0.0, float(raw))
    except (TypeError, ValueError):
        return 600.0


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

    Two-tier cache fallback for transient empty replies:
      * Standard TTL (``LIVE_PORTFOLIO_VALUE_CACHE_TTL_SEC``, default 45s) covers
        the normal case — last successful snapshot is reused for a short window.
      * Extended TTL (``LIVE_PORTFOLIO_VALUE_EXT_CACHE_TTL_SEC``, default 600s)
        kicks in only when the broker manager has confirmed the adapter is
        ``balance_ready``. This protects the dashboard from IBKR's paper API
        going briefly empty without flipping the whole NAV gate to "missing".
    """
    if broker_manager is None:
        return PortfolioValueSnapshot(Decimal(0), False, tuple(), tuple())
    allow = _nav_allowlist(broker_manager)
    disabled = _disabled_broker_names()
    cache = _cache_for(broker_manager)
    ttl = _cache_ttl_seconds()
    ext_ttl = _extended_cache_ttl_seconds()
    now = time.monotonic()
    total = Decimal(0)
    included: list[str] = []
    missing: list[str] = []
    per_broker: dict[str, str] = {}
    # Brokers the manager has confirmed ``balance_ready`` are eligible for the
    # extended fallback window — we trust the last good value for longer when
    # the broker manager periodically vouches for the adapter.
    healthy = allow if allow is not None else set()
    # Snapshot adapters to avoid concurrent mutation during late broker connects.
    for name, adapter in list(broker_manager.adapters.items()):
        n = str(name).strip().lower()
        if allow is not None and n not in allow:
            continue
        if n in disabled:
            continue
        included.append(n)
        effective_ttl = ext_ttl if n in healthy else ttl
        try:
            balances = await adapter.get_balance()
        except Exception:  # noqa: BLE001
            cached = _cached_value(cache, n, now, effective_ttl)
            if cached > 0:
                total += cached
                per_broker[n] = str(cached)
            else:
                missing.append(n)
            continue
        value = _per_adapter_total(balances)
        if value > 0:
            cache[n] = (value, now)
            total += value
            per_broker[n] = str(value)
        elif _zero_balance_is_complete(n, list(balances)):
            total += Decimal(0)
            per_broker[n] = "0"
        else:
            cached = _cached_value(cache, n, now, effective_ttl)
            if cached > 0:
                total += cached
                per_broker[n] = str(cached)
            else:
                missing.append(n)
    if allow is not None:
        known = {str(name).strip().lower() for name in getattr(broker_manager, "adapters", {})}
        for n in sorted(allow - disabled - known):
            missing.append(n)
    complete = bool(included) and not missing
    return PortfolioValueSnapshot(
        total if complete else Decimal(0),
        complete,
        tuple(included),
        tuple(missing),
        per_broker,
    )


async def live_portfolio_value(broker_manager: Any | None) -> Decimal:
    """Return a complete live NAV, or zero when included broker coverage is incomplete."""
    return (await live_portfolio_snapshot(broker_manager)).value
