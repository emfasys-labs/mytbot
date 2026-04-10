"""
Sum broker-reported balances as a proxy for total account equity (async).
Used by the API dashboard and the trading loop so both agree on "total balance".
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any


async def live_portfolio_value(broker_manager: Any | None) -> Decimal:
    """
    Sum one equity figure per connected adapter (avoid double-counting duplicate CCY rows).

    IBKR often reports NetLiquidation on the BASE row; taking ``max`` across all currencies
    can pick a small cash line instead of account NAV, understating live equity.
    """
    if broker_manager is None:
        return Decimal(0)
    total = Decimal(0)
    # Snapshot adapters to avoid concurrent mutation during late broker connects.
    for _name, adapter in list(broker_manager.adapters.items()):
        try:
            balances = await adapter.get_balance()
            if not balances:
                continue
            base_rows = [b for b in balances if str(getattr(b, "currency", "") or "").upper() == "BASE"]
            if base_rows:
                row = max(base_rows, key=lambda b: b.total)
                if row.total > 0:
                    total += row.total
                continue
            best = max(balances, key=lambda b: b.total)
            if best.total > 0:
                total += best.total
        except Exception:  # noqa: BLE001
            continue
    return total
