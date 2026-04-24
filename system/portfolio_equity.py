"""
Sum broker-reported balances as a proxy for total account equity (async).
Used by the API dashboard and the trading loop so both agree on "total balance".
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from control.runtime import get_risk_engine


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


async def live_portfolio_value(broker_manager: Any | None) -> Decimal:
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
        return Decimal(0)
    allow = _nav_allowlist(broker_manager)
    disabled = _disabled_broker_names()
    total = Decimal(0)
    # Snapshot adapters to avoid concurrent mutation during late broker connects.
    for name, adapter in list(broker_manager.adapters.items()):
        n = str(name).strip().lower()
        if allow is not None and n not in allow:
            continue
        if n in disabled:
            continue
        try:
            balances = await adapter.get_balance()
        except Exception:  # noqa: BLE001
            continue
        total += _per_adapter_total(balances)
    return total
