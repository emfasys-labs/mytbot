"""
system/adaptive_sizing.py
==========================
Vol-targeted position sizing.

Replaces the static ``default_position_pct = 0.05`` (5% of NAV on every
trade regardless of name) with a risk-budget-driven sizer:

    notional = NAV × risk_per_trade / atr_pct

So if the operator's risk budget per trade is 0.5% of NAV and the symbol
moves 2% per day (atr_pct = 0.02), the position is sized at
``NAV × 0.005 / 0.02 = 25%`` of NAV. A symbol that moves 0.5% per day
gets 100%. A symbol that moves 5% gets 10%. **Every trade now risks the
same dollar amount in a 1-day adverse move**, regardless of asset class
or symbol idiosyncrasy.

Why this matters for "as much money as possible":
  * Too small on low-vol names → strategies have edge but no size.
  * Too large on high-vol names → one bad fill blows up the day.
  * Static 5% guarantees both at once. Vol targeting fixes both.

The mode bias (Phase 0 classifier output) tilts the risk budget:
  * Hunter — risk_per_trade ≈ 0.5% NAV (the operator's default)
  * Trader — 0.3%
  * Defender — 0.15%
This is the ONLY genuinely mode-dependent number left after Phase 3.

The function is pure and falls back gracefully when atr_pct is missing.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Optional


@dataclass(frozen=True)
class SizingInputs:
    """Snapshot for the sizer. Same robustness as the other adaptive
    components — missing values fall through to the static fallback.
    """

    nav: Decimal
    last_price: Optional[Decimal]
    atr_pct: Optional[float]
    mode: str = "hunter"
    # Static fallback the operator's YAML already provides. Used when
    # atr_pct is missing or zero. Default 5% matches legacy behaviour.
    fallback_position_pct: float = 0.05
    # Confidence (0-1) scales the budget linearly: a low-confidence signal
    # gets a smaller position even within the same mode. Defaults to 1.0
    # so missing data is treated as full conviction (legacy behaviour).
    confidence: float = 1.0


# Operator's single risk knob per mode. These three numbers replace the
# entire ``default_position_pct`` static map across all strategies.
_RISK_BUDGET_PER_MODE = {
    "hunter": float(os.getenv("ADAPTIVE_SIZING_RISK_HUNTER", "0.005")),  # 0.5% of NAV
    "trader": float(os.getenv("ADAPTIVE_SIZING_RISK_TRADER", "0.003")),  # 0.3%
    "defender": float(os.getenv("ADAPTIVE_SIZING_RISK_DEFENDER", "0.0015")),  # 0.15%
}

# Safety rails — never size below this fraction of NAV (so we still trade
# something on very-low-vol names), and never above this (single-trade
# concentration cap regardless of vol target).
_MIN_NOTIONAL_PCT = float(os.getenv("ADAPTIVE_SIZING_MIN_PCT", "0.005"))   # 0.5%
_MAX_NOTIONAL_PCT = float(os.getenv("ADAPTIVE_SIZING_MAX_PCT", "0.30"))    # 30%

# Below this atr_pct, treat the symbol as "low-vol but knowable" — clip
# the inflation rather than falling back to the static path. Helps for
# big megacap ETFs where atr_pct can be < 0.5%.
_ATR_FLOOR = float(os.getenv("ADAPTIVE_SIZING_ATR_FLOOR", "0.005"))  # 0.5% / day


def _risk_budget(mode: str) -> float:
    m = (mode or "hunter").strip().lower()
    return _RISK_BUDGET_PER_MODE.get(m, _RISK_BUDGET_PER_MODE["hunter"])


@dataclass(frozen=True)
class SizingDecision:
    """Result of a sizing computation. The ``notional`` is the dollar
    figure to spend; ``quantity`` is what to send to the broker (when
    ``last_price`` was provided). ``path`` is for audit / logging."""

    notional: Decimal
    quantity: Decimal
    path: str
    inputs: SizingInputs


def compute_position_size(inputs: SizingInputs) -> SizingDecision:
    """Vol-targeted sizer.

    Path selection:
      * If ``atr_pct`` is missing or 0, falls back to NAV × fallback_pct
        (so legacy behaviour is preserved when the strategy didn't fill
        the field).
      * If ``last_price`` is None or non-positive, returns
        ``Decimal(0)`` quantity but still surfaces the notional so the
        caller can decide whether to bail.
    """
    nav = Decimal(str(inputs.nav)) if inputs.nav is not None else Decimal("0")
    if nav <= 0:
        return SizingDecision(Decimal("0"), Decimal("0"), "no_nav", inputs)

    last_price = inputs.last_price if inputs.last_price is not None else None
    try:
        last_price_d = Decimal(str(last_price)) if last_price is not None else None
    except (InvalidOperation, TypeError, ValueError):
        last_price_d = None

    # Decide the notional first; convert to quantity only at the end.
    atr = inputs.atr_pct
    if atr is None or atr <= 0:
        # Fallback path — must remain bit-for-bit identical to the legacy
        # ``nav × default_position_pct`` so missing-feature symbols size
        # exactly as they did before Phase 3. **No confidence scaling**
        # in this branch (legacy didn't apply it) and no min-floor clamp
        # (callers like SignalEngine.fallback_position_pct already encode
        # the operator's intent). Phase 5 will remove this path entirely.
        notional_pct = float(inputs.fallback_position_pct)
        notional = nav * Decimal(str(notional_pct))
        path = "fallback_static_pct"
    else:
        atr_eff = max(_ATR_FLOOR, float(atr))
        risk_budget_pct = _risk_budget(inputs.mode)
        # Position size: notional = NAV × risk / atr → a 1-ATR adverse
        # move costs ``risk × NAV``.
        notional_pct_raw = risk_budget_pct / atr_eff
        notional_pct = max(_MIN_NOTIONAL_PCT, min(_MAX_NOTIONAL_PCT, notional_pct_raw))
        notional = nav * Decimal(str(notional_pct)) * Decimal(str(max(0.0, min(1.0, inputs.confidence))))
        path = "vol_targeted"

    quantity = Decimal("0")
    if last_price_d is not None and last_price_d > 0:
        quantity = (notional / last_price_d)

    return SizingDecision(
        notional=notional.quantize(Decimal("0.01")),
        quantity=quantity,
        path=path,
        inputs=inputs,
    )
