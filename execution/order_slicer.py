"""
execution/order_slicer.py
===========================
Wave 9 — child-order slicing.

Given a parent order quantity, the daily volume of the symbol, and a
participation cap, produce an ordered list of child orders that
respect the cap. Optionally distribute the children over a time window
for scheduled execution (TWAP-lite).

Rejection rule: if even a single-child order at the participation cap
would still leave a residual that exceeds the rejection threshold, we
return ``SliceResult(rejected=True, ...)`` so the execution layer can
escalate or skip rather than ramming the book.

The module is pure — no IO, no broker calls. The caller (router /
engine) decides whether to translate ``ChildOrder``s into actual
broker orders.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Optional


# ── data ───────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ChildOrder:
    """One slice of a parent order."""

    sequence: int
    quantity: Decimal
    schedule_at: Optional[datetime] = None  # None ⇒ fire immediately
    metadata: dict = field(default_factory=dict)


@dataclass
class SliceResult:
    children: list[ChildOrder]
    rejected: bool = False
    reason: Optional[str] = None
    parent_quantity: Decimal = Decimal("0")
    participation_rate: float = 0.0
    metadata: dict = field(default_factory=dict)

    def total_quantity(self) -> Decimal:
        return sum((c.quantity for c in self.children), start=Decimal("0"))


# ── slicer ──────────────────────────────────────────────────────────────────


def slice_order(
    *,
    parent_quantity: Decimal,
    daily_volume: float,
    participation_rate_cap: float = 0.10,
    rejection_participation: float = 0.30,
    max_child_qty: Optional[Decimal] = None,
    n_slices: Optional[int] = None,
    schedule_window_seconds: int = 0,
    start_at: Optional[datetime] = None,
) -> SliceResult:
    """
    Slice ``parent_quantity`` into children.

    Rules:
      - If ``parent_quantity / daily_volume > rejection_participation``,
        return ``rejected=True``.
      - If ``daily_volume`` is unknown / non-positive, fall back to a
        single immediate child (router will treat it as a market order).
      - Otherwise compute ``max_child_by_pr = daily_volume * participation_rate_cap``
        and emit ``ceil(parent / cap)`` children, optionally further
        capped by ``max_child_qty``. Distribute evenly across
        ``schedule_window_seconds`` if non-zero.
      - ``n_slices`` is an explicit override that bypasses participation
        sizing — useful when the operator wants a fixed-N TWAP.

    All quantities are ``Decimal``.
    """
    parent = Decimal(parent_quantity)
    if parent <= 0:
        return SliceResult(
            children=[],
            rejected=False,
            reason="zero_quantity",
            parent_quantity=parent,
        )

    if daily_volume is None or daily_volume <= 0:
        # No volume info — single immediate child.
        return SliceResult(
            children=[ChildOrder(sequence=0, quantity=parent)],
            rejected=False,
            reason="no_volume_data",
            parent_quantity=parent,
            participation_rate=0.0,
        )

    pr = float(parent) / float(daily_volume)
    if pr > rejection_participation:
        return SliceResult(
            children=[],
            rejected=True,
            reason="exceeds_rejection_threshold",
            parent_quantity=parent,
            participation_rate=pr,
            metadata={"rejection_threshold": rejection_participation},
        )

    cap_qty = Decimal(str(daily_volume * participation_rate_cap))
    if max_child_qty is not None:
        cap_qty = min(cap_qty, Decimal(max_child_qty))
    if cap_qty <= 0:
        cap_qty = parent  # fall back to single child

    if n_slices is not None and n_slices > 0:
        # Operator-fixed N: ignore participation sizing.
        per = (parent / Decimal(n_slices)).quantize(Decimal("0.00000001"))
        # Ensure leftover from rounding lands on the last slice.
        children: list[ChildOrder] = []
        running = Decimal("0")
        for i in range(n_slices):
            qty = per if i < n_slices - 1 else (parent - running)
            running += qty
            children.append(ChildOrder(sequence=i, quantity=qty))
    else:
        # Participation-driven: ceil(parent / cap_qty) children.
        if cap_qty >= parent:
            children = [ChildOrder(sequence=0, quantity=parent)]
        else:
            n = int((parent + cap_qty - Decimal("1e-12")) / cap_qty)  # ceil
            n = max(1, n)
            per = (parent / Decimal(n)).quantize(Decimal("0.00000001"))
            children = []
            running = Decimal("0")
            for i in range(n):
                qty = per if i < n - 1 else (parent - running)
                running += qty
                children.append(ChildOrder(sequence=i, quantity=qty))

    # Time-distribution.
    if schedule_window_seconds and schedule_window_seconds > 0 and len(children) > 1:
        anchor = start_at or datetime.now(timezone.utc)
        gap = float(schedule_window_seconds) / max(1, len(children))
        sched = []
        for c in children:
            sched.append(
                ChildOrder(
                    sequence=c.sequence,
                    quantity=c.quantity,
                    schedule_at=anchor + timedelta(seconds=gap * c.sequence),
                    metadata=c.metadata,
                )
            )
        children = sched

    return SliceResult(
        children=children,
        rejected=False,
        parent_quantity=parent,
        participation_rate=pr,
        metadata={
            "participation_rate_cap": float(participation_rate_cap),
            "rejection_participation": float(rejection_participation),
            "n_children": len(children),
        },
    )
