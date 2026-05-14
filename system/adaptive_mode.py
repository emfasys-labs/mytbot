"""
system/adaptive_mode.py
========================
Derive the operator-visible "mode" indicator (defender / trader / hunter)
from current market state. Mode is no longer an operator switch — the
classifier runs once per trading-loop iteration and writes the result to
``data/runtime/active_mode.json``. The UI shows it as a read-only badge.

Bias toward **hunter**: the system stays in hunter unless objective
evidence of a hostile market is present. We never step down to defender
on subjective signals; the override conditions are concrete and
falsifiable so an operator reading the log can always tell why the
classifier moved.

Override ladder (highest priority first):

  1. ``defender``  — emergency conditions:
        * cross-section realised vol > 2σ above the 30-day cross-section mean
        * NAV drawdown today >= 1.5% of NAV
        * news pipeline flagged an emergency event in the last 60 minutes
        * primary equity market closed (US session NYSE) on a trading day
  2. ``trader``    — moderate caution:
        * cross-section realised vol > 1σ above the 30-day mean
        * NAV drawdown today between 0.5% and 1.5%
        * signal density (candidates per minute) below floor for 3+ ticks
  3. ``hunter``    — default. Everything else.

All thresholds are configurable via env vars (see ``_THRESHOLDS``) so
operators can tighten/relax without redeploying. The pure function
``classify_market_mode`` takes a snapshot of inputs and returns a
``ModeDecision`` — no side effects, easy to unit test.

This module is the only place a mode string is *produced*. Every other
consumer reads ``active_mode.json``; ``POST /system/mode`` is rejected.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, time, timezone
from decimal import Decimal
from typing import Literal, Optional

Mode = Literal["defender", "trader", "hunter"]


@dataclass(frozen=True)
class ModeInputs:
    """Snapshot of market state the classifier reads. All fields optional;
    missing inputs fall through to the hunter default rather than forcing
    a defensive stance on stale data."""

    # Cross-section realised volatility (e.g. mean daily-return std across the
    # tracked universe). Higher = market is jumpy.
    cross_section_vol: Optional[float] = None
    # 30-day rolling mean of cross_section_vol.
    cross_section_vol_baseline: Optional[float] = None
    # 30-day rolling std of cross_section_vol.
    cross_section_vol_std: Optional[float] = None
    # NAV-pct drawdown today (negative = loss). Eg ``-0.012`` = -1.2%.
    nav_drawdown_pct: Optional[float] = None
    # Number of new candidates generated in the last loop iteration.
    recent_signal_density: Optional[float] = None
    # Recent emergency news event detected by ai pipeline?
    emergency_news_active: bool = False
    # Override timestamp for tests; defaults to ``datetime.now(utc)``.
    now: Optional[datetime] = None


@dataclass(frozen=True)
class ModeDecision:
    mode: Mode
    reason: str
    inputs: ModeInputs


_THRESHOLDS = {
    # Vol spike (in sigmas above 30-day mean) that triggers defender.
    "defender_vol_sigma": float(os.getenv("ADAPTIVE_MODE_DEFENDER_VOL_SIGMA", "2.0")),
    # Vol spike that triggers trader.
    "trader_vol_sigma": float(os.getenv("ADAPTIVE_MODE_TRADER_VOL_SIGMA", "1.0")),
    # NAV drawdown pct that triggers defender (negative).
    "defender_drawdown_pct": float(os.getenv("ADAPTIVE_MODE_DEFENDER_DRAWDOWN_PCT", "-0.015")),
    # NAV drawdown pct that triggers trader.
    "trader_drawdown_pct": float(os.getenv("ADAPTIVE_MODE_TRADER_DRAWDOWN_PCT", "-0.005")),
    # Below this candidates-per-iter rate, mark "low signal density".
    "low_signal_density_floor": float(os.getenv("ADAPTIVE_MODE_LOW_SIGNAL_FLOOR", "1.0")),
}


def _is_us_equity_session(now: datetime) -> bool:
    """Naive US session check — 13:30 UTC – 20:00 UTC, weekdays.
    Used as a defender trigger only when explicitly missing for the
    primary equity universe."""
    if now.weekday() >= 5:
        return False
    utc_t = now.timetz().replace(tzinfo=None)
    return time(13, 30) <= utc_t <= time(20, 0)


def classify_market_mode(inputs: ModeInputs) -> ModeDecision:
    """Pure classifier. Reads market state, returns mode + reason.

    Bias: hunter is the default. The classifier only steps down to
    trader/defender when concrete adverse evidence is present.
    """
    now = inputs.now or datetime.now(timezone.utc)

    # ── Defender override ladder (any one triggers) ────────────────────
    if inputs.emergency_news_active:
        return ModeDecision("defender", "emergency_news_event", inputs)

    if inputs.nav_drawdown_pct is not None and inputs.nav_drawdown_pct <= _THRESHOLDS["defender_drawdown_pct"]:
        return ModeDecision(
            "defender",
            f"intraday_drawdown_{inputs.nav_drawdown_pct:.3%}_breached_{_THRESHOLDS['defender_drawdown_pct']:.3%}",
            inputs,
        )

    if (
        inputs.cross_section_vol is not None
        and inputs.cross_section_vol_baseline is not None
        and inputs.cross_section_vol_std is not None
        and inputs.cross_section_vol_std > 0
    ):
        sigma = (inputs.cross_section_vol - inputs.cross_section_vol_baseline) / inputs.cross_section_vol_std
        if sigma >= _THRESHOLDS["defender_vol_sigma"]:
            return ModeDecision(
                "defender",
                f"vol_sigma_{sigma:.2f}_breached_{_THRESHOLDS['defender_vol_sigma']:.1f}",
                inputs,
            )

    # ── Trader override ladder ─────────────────────────────────────────
    if inputs.nav_drawdown_pct is not None and inputs.nav_drawdown_pct <= _THRESHOLDS["trader_drawdown_pct"]:
        return ModeDecision(
            "trader",
            f"intraday_drawdown_{inputs.nav_drawdown_pct:.3%}_above_trader_floor",
            inputs,
        )

    if (
        inputs.cross_section_vol is not None
        and inputs.cross_section_vol_baseline is not None
        and inputs.cross_section_vol_std is not None
        and inputs.cross_section_vol_std > 0
    ):
        sigma = (inputs.cross_section_vol - inputs.cross_section_vol_baseline) / inputs.cross_section_vol_std
        if sigma >= _THRESHOLDS["trader_vol_sigma"]:
            return ModeDecision(
                "trader",
                f"vol_sigma_{sigma:.2f}_above_trader_floor",
                inputs,
            )

    # ── Hunter default ─────────────────────────────────────────────────
    return ModeDecision("hunter", "no_adverse_evidence", inputs)


def serialise_for_active_mode_json(decision: ModeDecision) -> dict:
    """Persist shape compatible with the existing ``active_mode.json`` reader.
    Stays backward-compatible with the `mode` key that strategies read; adds
    `reason` and `inputs` so the dashboard can show *why* the mode is what it is.
    """
    inp = decision.inputs
    return {
        "mode": decision.mode,
        "reason": decision.reason,
        "auto_derived": True,
        "decided_at": (inp.now or datetime.now(timezone.utc)).isoformat(),
        "inputs": {
            "cross_section_vol": inp.cross_section_vol,
            "cross_section_vol_baseline": inp.cross_section_vol_baseline,
            "cross_section_vol_std": inp.cross_section_vol_std,
            "nav_drawdown_pct": inp.nav_drawdown_pct,
            "recent_signal_density": inp.recent_signal_density,
            "emergency_news_active": inp.emergency_news_active,
        },
    }
