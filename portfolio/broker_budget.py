"""
portfolio/broker_budget.py
==========================

Per-broker buying-power layer (cross-broker portfolio, capital_pct aware).

PROBLEM:
    The portfolio orchestrator nets every strategy's intent across ALL brokers
    into ONE conviction-ranked book sized against a single global NAV pool
    (``tradable_capital = total_equity × capital_pct``). That is the right
    *decision* layer — pick the best opportunities regardless of venue — but it
    silently assumes capital is fungible across brokers. It is not: you cannot
    spend Binance cash to buy on IBKR. Without a per-broker constraint the
    allocator can target more notional on a venue than that venue actually
    holds, producing rejected/failed live orders.

SOLUTION (this module):
    A pure, deterministic capping layer applied AFTER orchestration and BEFORE
    risk/execution. Each broker gets a deployable budget = its own equity
    contribution × capital_pct. Opening/increasing orders are funded in
    descending order of conviction (the strongest opportunities get the scarce
    cash first — exactly "most profitable opportunity wins"), each broker capped
    by its own room. Reduce/close orders always pass (they FREE capital).

    This NEVER loosens risk (rule 2) — it only constrains. All money is
    ``Decimal`` (rule 3). No I/O, fully unit-testable.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Iterable

D0 = Decimal("0")


def _dec(x: Any, default: str = "0") -> Decimal:
    try:
        d = Decimal(str(x))
        return d if d.is_finite() else Decimal(default)
    except Exception:  # noqa: BLE001
        return Decimal(default)


@dataclass
class BrokerBudget:
    """Per-broker deployable capital and the running room consumed this tick."""

    broker: str
    equity: Decimal
    capital_pct: Decimal
    existing_notional: Decimal = D0

    @property
    def cap(self) -> Decimal:
        """Max notional allowed deployed on this broker (= equity × capital_pct)."""
        return self.equity * self.capital_pct

    @property
    def room(self) -> Decimal:
        """Remaining headroom for NEW opening notional."""
        return max(D0, self.cap - self.existing_notional)


def existing_notional_by_broker(portfolio_state: dict[str, Any] | None) -> dict[str, Decimal]:
    """Sum the absolute current position notional per broker from the book."""
    out: dict[str, Decimal] = {}
    if not isinstance(portfolio_state, dict):
        return out
    positions = portfolio_state.get("positions")
    if not isinstance(positions, dict):
        return out
    for _sym, pos in positions.items():
        if not isinstance(pos, dict):
            continue
        broker = str(pos.get("broker") or "").strip().lower()
        if not broker:
            continue
        qty = _dec(pos.get("quantity"))
        px = _dec(pos.get("current_price") or pos.get("avg_entry_price"))
        notional = abs(qty) * px
        if notional > 0:
            out[broker] = out.get(broker, D0) + notional
    return out


def compute_broker_budgets(
    per_broker_equity: dict[str, Any] | None,
    capital_pct: Any,
    existing_notional: dict[str, Decimal] | None = None,
) -> dict[str, BrokerBudget]:
    """Build the deployable budget per broker from live equity + capital_pct."""
    pct = _dec(capital_pct, "1")
    if pct < 0:
        pct = D0
    existing = existing_notional or {}
    budgets: dict[str, BrokerBudget] = {}
    for broker, equity in (per_broker_equity or {}).items():
        key = str(broker).strip().lower()
        if not key:
            continue
        budgets[key] = BrokerBudget(
            broker=key,
            equity=max(D0, _dec(equity)),
            capital_pct=pct,
            existing_notional=existing.get(key, D0),
        )
    return budgets


def cap_orders_to_broker_budgets(
    orders: Iterable[Any],
    budgets: dict[str, BrokerBudget],
    *,
    min_ticket_notional: Decimal = Decimal("1"),
    conviction_attr: str = "net_conviction",
    notional_attr: str = "delta_notional",
) -> tuple[list[Any], dict[str, Any]]:
    """Cap opening/increasing orders to each broker's deployable room.

    - reduce/close orders pass through untouched (they free capital).
    - opening orders are funded strongest-conviction-first per broker.
    - an order that exceeds remaining room is shrunk to the room (allow_smaller)
      if at least ``min_ticket_notional`` fits, else dropped.

    Mutates the ``delta_notional`` of shrunk orders in place (they are plain
    dataclasses owned by the caller for this tick). Returns
    ``(kept_orders, diagnostics)``.
    """
    order_list = list(orders)
    diag: dict[str, Any] = {
        "orders_in": len(order_list),
        "shrunk": 0,
        "dropped": 0,
        "uncapped_no_budget": 0,
        "broker_room": {},
        "broker_cap": {},
    }
    for b, bud in budgets.items():
        diag["broker_room"][b] = str(bud.room)
        diag["broker_cap"][b] = str(bud.cap)

    # Reduce/close always pass; opens compete for room.
    passthrough: list[Any] = []
    opens: list[Any] = []
    for o in order_list:
        is_reduce = bool(getattr(o, "reduce_only", False) or getattr(o, "close_only", False))
        if is_reduce:
            passthrough.append(o)
        else:
            opens.append(o)

    # Strongest conviction first — the scarce per-broker cash funds the best
    # opportunities before the marginal ones.
    opens.sort(key=lambda o: abs(_dec(getattr(o, conviction_attr, 0))), reverse=True)

    room: dict[str, Decimal] = {b: bud.room for b, bud in budgets.items()}
    kept_opens: list[Any] = []
    for o in opens:
        broker = str(getattr(o, "broker", "") or "").strip().lower()
        want = abs(_dec(getattr(o, notional_attr, 0)))
        if broker not in room:
            # No budget info for this broker (e.g. equity snapshot missing) —
            # don't block trading on missing data; let risk/execution decide.
            diag["uncapped_no_budget"] += 1
            kept_opens.append(o)
            continue
        avail = room[broker]
        if want <= avail:
            room[broker] = avail - want
            kept_opens.append(o)
        elif avail >= min_ticket_notional:
            setattr(o, notional_attr, avail)
            room[broker] = D0
            diag["shrunk"] += 1
            kept_opens.append(o)
        else:
            diag["dropped"] += 1  # no room left on this broker this tick

    diag["broker_room_after"] = {b: str(r) for b, r in room.items()}
    diag["orders_out"] = len(passthrough) + len(kept_opens)
    return passthrough + kept_opens, diag
