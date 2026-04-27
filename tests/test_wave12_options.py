"""
tests/test_wave12_options.py
==============================
Wave 12 acceptance tests.

Coverage:

- Black-Scholes ATM call price ≈ classical reference (Hull table style).
- Put-call parity holds: C - P = S * exp(-q t) - K * exp(-r t).
- Greeks have correct signs (call delta in [0,1], put delta in [-1,0],
  gamma > 0, vega > 0, theta < 0 for ATM long options).
- Degenerate inputs (zero T, zero vol) → None.
- IV surface bilinear interpolation; calendar-arbitrage screen flagged in notes.
- ``check_premium_exposure`` rejects per-trade and aggregate breaches.
- ``check_underlying_required`` enforces long-stock-required policy.
- ``LongCallStrategy`` / ``LongPutStrategy`` disabled by default emit None.
- Directional strategies enforce DTE band and delta band.
- Protective put refuses without underlying held (naked-call safeguard
  for hedging).
- Covered call refuses naked-call attempt — never produces a SELL CALL
  candidate without held stock.
- Options yaml ships disabled.
"""

from __future__ import annotations

import math
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest
import yaml

from models.options import (
    IVPoint,
    IVSurface,
    OptionInputs,
    black_scholes_greeks,
    black_scholes_price,
    build_iv_surface,
    check_premium_exposure,
    check_underlying_required,
)
from strategies.options_directional import (
    LongCallStrategy,
    LongPutStrategy,
    OptionsDirectionalConfig,
)
from strategies.options_hedging import (
    CoveredCallStrategy,
    OptionsHedgingConfig,
    ProtectivePutStrategy,
)


# ── Black-Scholes ──────────────────────────────────────────────────────────


def test_black_scholes_atm_call_price_matches_reference() -> None:
    # ATM, S=100, K=100, T=1, sigma=0.20, r=0.05, q=0.0
    # Reference (Hull): C ≈ 10.45
    inp = OptionInputs(spot=100.0, strike=100.0, time_to_expiry_years=1.0,
                       volatility=0.20, risk_free_rate=0.05, is_call=True)
    p = black_scholes_price(inp)
    assert p is not None
    assert abs(p - 10.45) < 0.10


def test_put_call_parity_holds() -> None:
    s, k, t, sigma, r, q = 100.0, 100.0, 0.5, 0.25, 0.04, 0.01
    c = black_scholes_price(OptionInputs(s, k, t, sigma, r, q, True))
    p = black_scholes_price(OptionInputs(s, k, t, sigma, r, q, False))
    lhs = c - p
    rhs = s * math.exp(-q * t) - k * math.exp(-r * t)
    assert abs(lhs - rhs) < 1e-6


def test_greek_signs() -> None:
    inp_call = OptionInputs(100, 100, 0.5, 0.25, 0.04, 0.0, True)
    inp_put = OptionInputs(100, 100, 0.5, 0.25, 0.04, 0.0, False)
    rc = black_scholes_greeks(inp_call)
    rp = black_scholes_greeks(inp_put)
    assert rc is not None and rp is not None
    # Delta: call in [0,1], put in [-1, 0].
    assert 0.0 < rc.greeks.delta < 1.0
    assert -1.0 < rp.greeks.delta < 0.0
    # Gamma > 0 for both.
    assert rc.greeks.gamma > 0
    assert rp.greeks.gamma > 0
    # Vega > 0 for both.
    assert rc.greeks.vega > 0
    assert rp.greeks.vega > 0
    # Theta < 0 for ATM long options (time-decay against the holder).
    assert rc.greeks.theta < 0
    assert rp.greeks.theta < 0


def test_zero_inputs_return_none() -> None:
    assert black_scholes_price(OptionInputs(0, 100, 1.0, 0.2, 0.05, 0.0, True)) is None
    assert black_scholes_price(OptionInputs(100, 100, 0.0, 0.2, 0.05, 0.0, True)) is None
    assert black_scholes_price(OptionInputs(100, 100, 1.0, 0.0, 0.05, 0.0, True)) is None


# ── IV surface ────────────────────────────────────────────────────────────


def test_iv_surface_basic_interpolation() -> None:
    pts = [
        IVPoint(strike=90, time_to_expiry_years=0.25, iv=0.30),
        IVPoint(strike=110, time_to_expiry_years=0.25, iv=0.20),
        IVPoint(strike=90, time_to_expiry_years=0.50, iv=0.32),
        IVPoint(strike=110, time_to_expiry_years=0.50, iv=0.22),
    ]
    surf = build_iv_surface(pts)
    # Exact corner.
    assert surf.lookup(strike=90, time_to_expiry_years=0.25) == pytest.approx(0.30)
    # Interpolated midpoint.
    mid = surf.lookup(strike=100, time_to_expiry_years=0.375)
    # Midpoint of all four corners ≈ 0.26.
    assert 0.24 < mid < 0.28


def test_iv_surface_clamps_outside_grid() -> None:
    pts = [
        IVPoint(strike=90, time_to_expiry_years=0.25, iv=0.30),
        IVPoint(strike=110, time_to_expiry_years=0.25, iv=0.20),
    ]
    surf = build_iv_surface(pts)
    # Way outside grid — should clamp to nearest corner.
    val = surf.lookup(strike=200, time_to_expiry_years=10.0)
    assert val in {0.20, 0.30}


def test_iv_surface_rejects_negative_and_nonfinite() -> None:
    pts = [
        IVPoint(strike=100, time_to_expiry_years=0.5, iv=-0.1),
        IVPoint(strike=100, time_to_expiry_years=0.5, iv=float("inf")),
        IVPoint(strike=100, time_to_expiry_years=0.5, iv=0.20),  # only this survives
    ]
    surf = build_iv_surface(pts)
    assert len(surf.points) == 1


def test_iv_surface_calendar_arbitrage_flagged_in_notes() -> None:
    # Same strike, IV crashes from 0.30 to 0.05 between adjacent expiries
    # — calendar-arbitrage screen should flag it.
    pts = [
        IVPoint(strike=100, time_to_expiry_years=0.25, iv=0.30),
        IVPoint(strike=100, time_to_expiry_years=0.50, iv=0.05),
    ]
    surf = build_iv_surface(pts, calendar_decrease_tolerance=0.5)
    assert "calendar_arbitrage" in surf.notes


# ── risk gates ────────────────────────────────────────────────────────────


def test_premium_exposure_per_trade_cap() -> None:
    res = check_premium_exposure(
        new_premium_notional=Decimal("700"),
        existing_premium_notional=Decimal("0"),
        nav=Decimal("100000"),
        max_pct_per_trade=0.005,  # 0.5% NAV = 500
        max_pct_aggregate=0.05,
    )
    assert res.allowed is False
    assert res.reason == "exceeds_per_trade_premium_cap"


def test_premium_exposure_aggregate_cap() -> None:
    res = check_premium_exposure(
        new_premium_notional=Decimal("400"),
        existing_premium_notional=Decimal("1700"),
        nav=Decimal("100000"),
        max_pct_per_trade=0.005,
        max_pct_aggregate=0.02,  # 2% NAV = 2000 < 400+1700=2100
    )
    assert res.allowed is False
    assert res.reason == "exceeds_aggregate_premium_cap"


def test_premium_exposure_allows_within_caps() -> None:
    res = check_premium_exposure(
        new_premium_notional=Decimal("300"),
        existing_premium_notional=Decimal("500"),
        nav=Decimal("100000"),
        max_pct_per_trade=0.005,
        max_pct_aggregate=0.02,
    )
    assert res.allowed is True


def test_underlying_required_refuses_when_no_position() -> None:
    res = check_underlying_required(
        underlying_symbol="SPY",
        holdings_by_symbol=[],
        required_quantity=Decimal("100"),
    )
    assert res.allowed is False
    assert res.reason == "no_underlying_long_position"


def test_underlying_required_refuses_when_short() -> None:
    res = check_underlying_required(
        underlying_symbol="SPY",
        holdings_by_symbol=[("SPY", Decimal("-100"))],   # short stock
        required_quantity=Decimal("100"),
    )
    assert res.allowed is False
    assert res.reason == "no_underlying_long_position"


def test_underlying_required_allows_when_long_enough() -> None:
    res = check_underlying_required(
        underlying_symbol="SPY",
        holdings_by_symbol=[("SPY", Decimal("200"))],
        required_quantity=Decimal("100"),
    )
    assert res.allowed is True


# ── directional strategies ────────────────────────────────────────────────


def _now() -> datetime:
    return datetime(2026, 1, 1, tzinfo=timezone.utc)


def test_long_call_disabled_by_default_emits_none() -> None:
    s = LongCallStrategy()  # default: disabled
    out = s.evaluate(
        underlying_symbol="SPY", spot=100, strike=Decimal("100"),
        expiry_yyyymmdd="20260201", time_to_expiry_years=30 / 365.0,
        volatility=0.30, contracts=1, nav=Decimal("100000"), as_of=_now(),
    )
    assert out is None


def test_long_call_enabled_emits_candidate_with_option_metadata() -> None:
    cfg = OptionsDirectionalConfig(enabled=True, paper_only=True)
    s = LongCallStrategy(cfg)
    out = s.evaluate(
        underlying_symbol="SPY", spot=100, strike=Decimal("100"),
        expiry_yyyymmdd="20260201", time_to_expiry_years=30 / 365.0,
        volatility=0.30, contracts=1, nav=Decimal("1000000"), as_of=_now(),
    )
    assert out is not None
    md = out.metadata
    assert md["instrument_type"] == "option"
    assert md["option_contract"]["right"] == "C"
    assert md["options_paper_only"] is True
    assert out.side == "long"
    # Strategy name reflects the family.
    assert out.strategy_name == "options_long_call"


def test_long_put_enabled_emits_candidate_with_buy_to_open_flag() -> None:
    cfg = OptionsDirectionalConfig(enabled=True, paper_only=True)
    s = LongPutStrategy(cfg)
    out = s.evaluate(
        underlying_symbol="SPY", spot=100, strike=Decimal("100"),
        expiry_yyyymmdd="20260201", time_to_expiry_years=30 / 365.0,
        volatility=0.30, contracts=1, nav=Decimal("1000000"), as_of=_now(),
    )
    assert out is not None
    assert out.metadata["option_contract"]["right"] == "P"
    assert out.metadata["options_buy_to_open"] is True
    assert out.side == "short"  # bearish view on underlying


def test_long_call_premium_cap_rejects_oversized_trade() -> None:
    cfg = OptionsDirectionalConfig(
        enabled=True, paper_only=True,
        max_premium_pct_per_trade=0.0001,  # absurdly tight cap
        max_premium_pct_aggregate=0.0001,
    )
    s = LongCallStrategy(cfg)
    out = s.evaluate(
        underlying_symbol="SPY", spot=100, strike=Decimal("100"),
        expiry_yyyymmdd="20260201", time_to_expiry_years=30 / 365.0,
        volatility=0.30, contracts=1, nav=Decimal("100000"), as_of=_now(),
    )
    assert out is None  # premium exceeds 0.01% NAV cap


def test_long_call_dte_band_rejected_below_min() -> None:
    cfg = OptionsDirectionalConfig(enabled=True, min_dte_days=10, max_dte_days=60)
    s = LongCallStrategy(cfg)
    out = s.evaluate(
        underlying_symbol="SPY", spot=100, strike=Decimal("100"),
        expiry_yyyymmdd="20260108", time_to_expiry_years=2 / 365.0,  # 2 DTE < 10
        volatility=0.30, contracts=1, nav=Decimal("1000000"), as_of=_now(),
    )
    assert out is None


def test_long_call_delta_band_rejected_when_deep_otm() -> None:
    cfg = OptionsDirectionalConfig(enabled=True, min_delta_call=0.30, max_delta_call=0.70)
    s = LongCallStrategy(cfg)
    out = s.evaluate(
        underlying_symbol="SPY", spot=100, strike=Decimal("200"),  # very far OTM ⇒ tiny delta
        expiry_yyyymmdd="20260201", time_to_expiry_years=30 / 365.0,
        volatility=0.20, contracts=1, nav=Decimal("1000000"), as_of=_now(),
    )
    assert out is None


# ── hedging strategies ───────────────────────────────────────────────────


def test_protective_put_refuses_without_underlying() -> None:
    cfg = OptionsHedgingConfig(enabled=True)
    s = ProtectivePutStrategy(cfg)
    out = s.evaluate(
        underlying_symbol="SPY", spot=100, strike=Decimal("95"),
        expiry_yyyymmdd="20260201", time_to_expiry_years=30 / 365.0,
        volatility=0.30, contracts=1, nav=Decimal("1000000"),
        holdings_by_symbol=[],
        as_of=_now(),
    )
    assert out is None


def test_protective_put_emits_when_underlying_held() -> None:
    cfg = OptionsHedgingConfig(enabled=True)
    s = ProtectivePutStrategy(cfg)
    out = s.evaluate(
        underlying_symbol="SPY", spot=100, strike=Decimal("95"),
        expiry_yyyymmdd="20260201", time_to_expiry_years=30 / 365.0,
        volatility=0.30, contracts=1, nav=Decimal("1000000"),
        holdings_by_symbol=[("SPY", Decimal("200"))],
        as_of=_now(),
    )
    assert out is not None
    assert out.metadata["options_hedge_role"] == "protective_put"
    assert out.metadata["options_buy_to_open"] is True


def test_covered_call_refuses_naked_attempt() -> None:
    """The Wave-12 invariant: never emit a SELL CALL without underlying."""
    cfg = OptionsHedgingConfig(enabled=True)
    s = CoveredCallStrategy(cfg)
    out = s.evaluate(
        underlying_symbol="SPY", spot=100, strike=Decimal("105"),
        expiry_yyyymmdd="20260201", time_to_expiry_years=30 / 365.0,
        volatility=0.30, contracts=1,
        holdings_by_symbol=[],   # no underlying
        as_of=_now(),
    )
    assert out is None


def test_covered_call_emits_when_underlying_held() -> None:
    cfg = OptionsHedgingConfig(enabled=True)
    s = CoveredCallStrategy(cfg)
    out = s.evaluate(
        underlying_symbol="SPY", spot=100, strike=Decimal("105"),
        expiry_yyyymmdd="20260201", time_to_expiry_years=30 / 365.0,
        volatility=0.30, contracts=1,
        holdings_by_symbol=[("SPY", Decimal("200"))],
        as_of=_now(),
    )
    assert out is not None
    assert out.metadata["options_hedge_role"] == "covered_call"
    assert out.metadata["options_sell_to_open"] is True


# ── config ────────────────────────────────────────────────────────────────


def test_default_yaml_ships_disabled() -> None:
    raw = yaml.safe_load(Path("config/options_strategies.yaml").read_text(encoding="utf-8"))
    assert (raw.get("options_directional") or {}).get("enabled") is False
    assert (raw.get("options_hedging") or {}).get("enabled") is False
    assert (raw.get("options_directional") or {}).get("paper_only") is True
    assert (raw.get("options_hedging") or {}).get("paper_only") is True
