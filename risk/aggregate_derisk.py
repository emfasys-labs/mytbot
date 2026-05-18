"""
risk/aggregate_derisk.py
========================
Aggregate unrealised-loss de-risk — the "death by many small losers"
guard the per-trade stop-loss structurally misses.

The post-open stop-loss only fires when a *single* position's
unrealised loss exceeds ``NAV × max_loss_per_trade_pct`` (≈ $10.7k at a
$1.07M book / 1%). A book that is −$7k spread across 30 positions, none
individually near that bar, trips nothing and just bleeds. This module
computes a **dynamic, NAV- and volatility-scaled** aggregate budget and,
when breached, selects the worst-loss positions to close **reduce-only**
(risk exits — exempt from the anti-churn governor) until the projected
remaining loss is back inside the budget.

Pure / dependency-free so the policy is unit-tested in isolation; the
orchestrator stop-loss monitor calls it and submits the closes through
the normal risk+execution path.

No hardcoded dollar cap: the budget is a fraction of live NAV, widened
when realised volatility is high (give winners room) and tightened when
calm — market-driven, consistent with the project's philosophy.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any


def _d(v: object) -> Decimal:
    try:
        return Decimal(str(v if v is not None else 0))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal("0")


def _f(env: str, default: float) -> float:
    try:
        return float(os.getenv(env, str(default)))
    except (TypeError, ValueError):
        return default


def derisk_enabled() -> bool:
    return os.getenv("AGG_UNREALISED_DERISK", "1").strip().lower() not in (
        "0",
        "false",
        "no",
        "off",
    )


@dataclass(frozen=True)
class PositionLoss:
    broker: str
    symbol: str
    quantity: Decimal
    avg_entry_price: Decimal
    current_price: Decimal
    asset_class: str = "equity"
    metadata: dict[str, Any] | None = None

    @property
    def unrealised(self) -> Decimal:
        if self.current_price <= 0 or self.avg_entry_price <= 0 or self.quantity == 0:
            return Decimal("0")
        return (self.current_price - self.avg_entry_price) * self.quantity


def aggregate_unrealised(positions: list[PositionLoss]) -> Decimal:
    return sum((p.unrealised for p in positions), Decimal("0"))


def derisk_budget(nav: Decimal, *, realised_vol: float | None = None) -> Decimal:
    """Max tolerated *aggregate* unrealised loss before forced de-risk.

    ``budget = NAV × base_pct × vol_scale``
      * ``base_pct``  — AGG_UNREALISED_DERISK_NAV_PCT (default 0.0075).
      * ``vol_scale`` — 1.0 at the reference vol; scales linearly with
        recent realised vol within [min,max] so a volatile tape gets more
        room (avoid knee-jerk liquidation on noise) and a calm tape is
        held tighter. Missing vol → 1.0 (pure NAV fraction).
    """
    if nav <= 0:
        return Decimal("0")
    base_pct = Decimal(str(max(0.0, _f("AGG_UNREALISED_DERISK_NAV_PCT", 0.0075))))
    scale = Decimal("1")
    if realised_vol is not None and realised_vol > 0:
        ref = max(1e-6, _f("AGG_UNREALISED_DERISK_REF_VOL", 0.02))
        lo = _f("AGG_UNREALISED_DERISK_VOL_MIN", 0.6)
        hi = _f("AGG_UNREALISED_DERISK_VOL_MAX", 2.0)
        raw = realised_vol / ref
        scale = Decimal(str(min(hi, max(lo, raw))))
    return nav * base_pct * scale


def select_derisk_closes(
    positions: list[PositionLoss],
    nav: Decimal,
    *,
    realised_vol: float | None = None,
    max_actions: int | None = None,
) -> list[PositionLoss]:
    """Worst-loss positions to close (reduce-only) until projected
    aggregate unrealised loss is back within budget.

    Returns ``[]`` when disabled, NAV unknown, the book is within budget,
    or there are no losing positions. Bounded per call so we bleed the
    book down over several ticks rather than dumping it all at once.
    """
    if not derisk_enabled() or nav <= 0 or not positions:
        return []
    budget = derisk_budget(nav, realised_vol=realised_vol)
    if budget <= 0:
        return []
    total = aggregate_unrealised(positions)
    # Only act on a NET aggregate loss beyond budget (positive total =
    # net gain, nothing to do).
    if total >= -budget:
        return []
    if max_actions is None:
        try:
            max_actions = max(1, int(_f("AGG_UNREALISED_DERISK_MAX_ACTIONS", 3.0)))
        except (TypeError, ValueError):
            max_actions = 3
    # Close the biggest losers first; stop once the projected remaining
    # aggregate loss is inside the budget (closing a loser removes its
    # negative unrealised from the book).
    losers = sorted(
        (p for p in positions if p.unrealised < 0),
        key=lambda p: p.unrealised,  # most negative first
    )
    chosen: list[PositionLoss] = []
    projected = total
    for p in losers:
        if projected >= -budget or len(chosen) >= max_actions:
            break
        chosen.append(p)
        projected -= p.unrealised  # remove this loss from the book
    return chosen
