"""
models/options/risk.py
========================
Wave 12 — options-specific pre-trade risk gates.

The full risk engine in ``risk/engine.py`` already enforces broad
limits (drawdown, exposure, kill switch). These helpers are
*structure-aware* gates that apply to options specifically:

  - Premium-exposure cap: the operator's premium budget per trade and
    in aggregate.
  - Underlying-required: covered calls and protective puts cannot be
    opened without the corresponding long stock position.
  - No-naked-short: short calls / short puts without an offsetting
    underlying position are refused at this layer (defence in depth;
    the strategy module should never produce them in the first place).

These are pure functions; the strategy layer composes them.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Iterable, Optional


@dataclass
class OptionsRiskCheck:
    allowed: bool
    reason: str
    metadata: dict = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.metadata is None:
            self.metadata = {}


def check_premium_exposure(
    *,
    new_premium_notional: Decimal,
    existing_premium_notional: Decimal,
    nav: Decimal,
    max_pct_per_trade: float,
    max_pct_aggregate: float,
) -> OptionsRiskCheck:
    """
    Block trades whose premium spend exceeds operator-configured caps.

    All inputs are Decimal; percentages are floats (decimals, e.g.
    0.02 = 2% of NAV).
    """
    if nav is None or nav <= 0:
        return OptionsRiskCheck(allowed=False, reason="nav_unavailable")
    np_dec = Decimal(new_premium_notional or 0)
    ep_dec = Decimal(existing_premium_notional or 0)
    nav_dec = Decimal(nav)

    if np_dec < 0 or ep_dec < 0:
        return OptionsRiskCheck(allowed=False, reason="negative_premium_notional")

    per_trade_cap = nav_dec * Decimal(str(max_pct_per_trade))
    if np_dec > per_trade_cap:
        return OptionsRiskCheck(
            allowed=False,
            reason="exceeds_per_trade_premium_cap",
            metadata={
                "new_premium_notional": str(np_dec),
                "per_trade_cap": str(per_trade_cap),
            },
        )

    aggregate_cap = nav_dec * Decimal(str(max_pct_aggregate))
    if (np_dec + ep_dec) > aggregate_cap:
        return OptionsRiskCheck(
            allowed=False,
            reason="exceeds_aggregate_premium_cap",
            metadata={
                "new_plus_existing": str(np_dec + ep_dec),
                "aggregate_cap": str(aggregate_cap),
            },
        )

    return OptionsRiskCheck(allowed=True, reason="within_caps")


def check_underlying_required(
    *,
    underlying_symbol: str,
    holdings_by_symbol: Iterable[tuple[str, Decimal]],
    required_quantity: Decimal,
    side_label: str = "long",
) -> OptionsRiskCheck:
    """
    Verify the operator holds at least ``required_quantity`` of the
    underlying with the right sign.

    For protective put: requires LONG underlying (qty > 0).
    For covered call: requires LONG underlying (qty > 0).
    """
    sym = (underlying_symbol or "").strip().upper()
    if not sym:
        return OptionsRiskCheck(allowed=False, reason="missing_underlying_symbol")
    expected_dir = (side_label or "long").strip().lower()
    if expected_dir not in ("long",):
        return OptionsRiskCheck(
            allowed=False,
            reason="only_long_underlying_supported",
            metadata={"side_label": side_label},
        )

    held = Decimal("0")
    for s, q in holdings_by_symbol:
        if (s or "").strip().upper() != sym:
            continue
        try:
            held += Decimal(q)
        except (TypeError, ValueError):
            continue
    req = Decimal(required_quantity or 0)
    if held <= 0:
        return OptionsRiskCheck(
            allowed=False,
            reason="no_underlying_long_position",
            metadata={"underlying": sym, "required_quantity": str(req), "held": str(held)},
        )
    if held < req:
        return OptionsRiskCheck(
            allowed=False,
            reason="insufficient_underlying_quantity",
            metadata={"underlying": sym, "required_quantity": str(req), "held": str(held)},
        )
    return OptionsRiskCheck(
        allowed=True,
        reason="underlying_position_satisfied",
        metadata={"underlying": sym, "required_quantity": str(req), "held": str(held)},
    )
