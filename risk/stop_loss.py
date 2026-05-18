"""
risk/stop_loss.py
=================

**D031E — Stop-loss framework scaffold (NOT YET ENFORCED AT RUNTIME).**

This module is deliberately a lightweight scaffold. It exists so that future
work can wire ``max_loss_per_trade_pct`` (from ``config/risk_limits.yaml``)
into actual runtime enforcement without another round of architectural
discussion. See ``docs/DECISIONS.md`` D031 for the rationale.

Scope today
-----------
* Expose a single evaluator that, given a live position and NAV, decides
  whether the position has breached its per-trade loss budget and should be
  forcibly closed.
* Compute an ATR-based structural stop when strategy metadata carries the
  required fields (``stop_loss_atr``, ``atr_pct`` / ``atr``).
* Return a ``StopLossDecision`` that is pure data — no side effects.

Explicitly NOT in scope yet
---------------------------
* Running a background monitor loop that evaluates every held position every
  N seconds and issues close orders. That is a follow-up task; wiring it
  prematurely risks emitting sell orders from a half-tested code path.
* Trailing stops, time stops, thesis-invalidation stops. Those are per-
  strategy and will live in the strategy exit logic (see D031 follow-up).
* Options / futures specific loss accounting.

To wire this up when the follow-up task is accepted
---------------------------------------------------
1. Call ``evaluate_stop_loss`` from a periodic orchestrator task (similar to
   the D029 NAV heartbeat) once per N seconds.
2. When ``should_close`` is True, submit a close order via the execution
   engine using ``reason=decision.reason`` for auditability.
3. Add a regression test asserting the monitor closes positions exceeding
   ``max_loss_per_trade_pct``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any


def _f(env: str, default: float) -> float:
    try:
        return float(os.getenv(env, str(default)))
    except (TypeError, ValueError):
        return default


@dataclass(frozen=True)
class StopLossDecision:
    should_close: bool
    reason: str
    loss_pct: Decimal
    loss_absolute: Decimal
    structural_stop_price: Decimal | None
    structural_stop_breached: bool
    # Position-relative stop: position is down more than a volatility-scaled
    # fraction of its OWN cost basis (independent of NAV size and of
    # "edge"). Default False keeps every existing caller valid.
    position_stop_breached: bool = False


def evaluate_stop_loss(
    *,
    symbol: str,
    quantity: Decimal,
    avg_entry_price: Decimal,
    current_price: Decimal,
    nav: Decimal,
    max_loss_per_trade_pct: Decimal,
    metadata: dict[str, Any] | None = None,
    position_stop_pct: Decimal | None = None,
) -> StopLossDecision:
    """Evaluate whether a held position has breached its loss budget.

    Returns a pure-data decision; callers are responsible for acting on it.

    Portfolio stop
      If ``|unrealised_loss|`` exceeds ``nav * max_loss_per_trade_pct``, the
      position has consumed its per-trade loss budget and should be closed
      regardless of strategy opinion.

    Structural stop (optional)
      If metadata carries ``stop_loss_atr`` (ATR multiple) and either
      ``atr`` (absolute price) or ``atr_pct``, a structural stop price is
      derived from entry. Breach flags the position as a candidate for
      close even if the portfolio budget has not yet been consumed.
    """
    meta = metadata or {}

    if avg_entry_price <= 0 or current_price <= 0:
        return StopLossDecision(
            should_close=False,
            reason="invalid_prices",
            loss_pct=Decimal("0"),
            loss_absolute=Decimal("0"),
            structural_stop_price=None,
            structural_stop_breached=False,
        )

    direction = Decimal("1") if quantity >= 0 else Decimal("-1")
    unrealised = direction * (current_price - avg_entry_price) * abs(quantity)
    loss_abs = -unrealised if unrealised < 0 else Decimal("0")

    budget = nav * max_loss_per_trade_pct if nav > 0 and max_loss_per_trade_pct > 0 else Decimal("0")
    portfolio_stop_breached = budget > 0 and loss_abs > budget

    # ── Position-relative loss stop ───────────────────────────────────────
    # Cut a single position when it is down more than a volatility-scaled
    # fraction of its OWN cost basis — independent of NAV size and of
    # "edge". This is what actually catches a position down ~20% of itself
    # that is far below the (NAV-relative) per-trade budget and not
    # "dead-edge". Volatile names get more room; calm names are held
    # tighter. ``POSITION_STOP_LOSS_PCT<=0`` disables it (prior behaviour).
    if position_stop_pct is None:
        try:
            position_stop_pct = Decimal(str(_f("POSITION_STOP_LOSS_PCT", 0.08)))
        except (InvalidOperation, TypeError, ValueError):
            position_stop_pct = Decimal("0.08")
    position_notional = abs(quantity) * avg_entry_price
    position_stop_breached = False
    if position_stop_pct > 0 and position_notional > 0 and loss_abs > 0:
        vol_scale = Decimal("1")
        raw_atr_pct = meta.get("atr_pct")
        if raw_atr_pct is not None:
            try:
                ap = float(raw_atr_pct)
                ref = max(1e-6, _f("POSITION_STOP_VOL_REF", 0.02))
                lo = _f("POSITION_STOP_VOL_MIN", 0.7)
                hi = _f("POSITION_STOP_VOL_MAX", 2.0)
                vol_scale = Decimal(str(min(hi, max(lo, ap / ref))))
            except (TypeError, ValueError):
                vol_scale = Decimal("1")
        threshold = position_stop_pct * vol_scale
        loss_frac_of_position = loss_abs / position_notional
        position_stop_breached = loss_frac_of_position >= threshold

    structural_stop_price: Decimal | None = None
    structural_stop_breached = False

    try:
        atr_mult = Decimal(str(meta.get("stop_loss_atr") or "0"))
    except Exception:  # noqa: BLE001
        atr_mult = Decimal("0")
    if atr_mult > 0:
        atr_abs: Decimal | None = None
        raw_atr = meta.get("atr")
        if raw_atr is not None:
            try:
                atr_abs = Decimal(str(raw_atr))
            except Exception:  # noqa: BLE001
                atr_abs = None
        if atr_abs is None:
            raw_pct = meta.get("atr_pct")
            if raw_pct is not None:
                try:
                    atr_abs = Decimal(str(raw_pct)) * avg_entry_price
                except Exception:  # noqa: BLE001
                    atr_abs = None
        if atr_abs is not None and atr_abs > 0:
            if direction > 0:
                structural_stop_price = avg_entry_price - atr_mult * atr_abs
                structural_stop_breached = current_price <= structural_stop_price
            else:
                structural_stop_price = avg_entry_price + atr_mult * atr_abs
                structural_stop_breached = current_price >= structural_stop_price

    should_close = (
        portfolio_stop_breached or structural_stop_breached or position_stop_breached
    )
    if portfolio_stop_breached:
        reason = f"portfolio_loss_budget:{loss_abs} > {budget}"
    elif position_stop_breached:
        reason = (
            f"position_stop:loss={loss_abs} >= "
            f"{position_stop_pct}x_cost_basis({position_notional})"
        )
    elif structural_stop_breached:
        reason = f"structural_stop:{structural_stop_price}"
    else:
        reason = "within_budget"

    loss_pct = (loss_abs / nav) if nav > 0 else Decimal("0")
    return StopLossDecision(
        should_close=should_close,
        reason=reason,
        loss_pct=loss_pct,
        loss_absolute=loss_abs,
        structural_stop_price=structural_stop_price,
        structural_stop_breached=structural_stop_breached,
        position_stop_breached=position_stop_breached,
    )
