"""
models/options/greeks.py
=========================
Wave 12 — Black-Scholes pricing + greeks.

Pure-NumPy implementation using ``math.erf`` for the standard normal
CDF (no scipy dependency). Suitable for European-style options on
non-dividend-paying underlyings; ``q`` (continuous dividend yield) is
exposed for index / FX use cases.

All inputs are plain floats. Decimal-typed money math lives at the
strategy boundary; greeks are always float (the BS model itself is a
float-only construct).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional


# ── helpers ────────────────────────────────────────────────────────────────


def _norm_cdf(x: float) -> float:
    """Standard normal CDF via erf — stdlib only."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _norm_pdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


# ── data classes ───────────────────────────────────────────────────────────


@dataclass(frozen=True)
class OptionInputs:
    spot: float
    strike: float
    time_to_expiry_years: float
    volatility: float
    risk_free_rate: float = 0.05
    dividend_yield: float = 0.0
    is_call: bool = True


@dataclass(frozen=True)
class OptionGreeks:
    delta: float
    gamma: float
    vega: float        # per 1.0 vol-pt (caller divides by 100 for "per 1%")
    theta: float       # per year (caller divides by 365 for "per day")
    rho: float


@dataclass(frozen=True)
class BlackScholesResult:
    price: float
    greeks: OptionGreeks


# ── public API ─────────────────────────────────────────────────────────────


def _d1_d2(inp: OptionInputs) -> tuple[Optional[float], Optional[float]]:
    s = float(inp.spot)
    k = float(inp.strike)
    t = float(inp.time_to_expiry_years)
    sigma = float(inp.volatility)
    r = float(inp.risk_free_rate)
    q = float(inp.dividend_yield)
    if s <= 0 or k <= 0 or t <= 0 or sigma <= 0:
        return None, None
    sqrt_t = math.sqrt(t)
    d1 = (math.log(s / k) + (r - q + 0.5 * sigma * sigma) * t) / (sigma * sqrt_t)
    d2 = d1 - sigma * sqrt_t
    return d1, d2


def black_scholes_price(inp: OptionInputs) -> Optional[float]:
    """European Black-Scholes price; returns ``None`` on degenerate input."""
    d1, d2 = _d1_d2(inp)
    if d1 is None or d2 is None:
        return None
    s = float(inp.spot)
    k = float(inp.strike)
    t = float(inp.time_to_expiry_years)
    r = float(inp.risk_free_rate)
    q = float(inp.dividend_yield)
    df_r = math.exp(-r * t)
    df_q = math.exp(-q * t)
    if inp.is_call:
        price = s * df_q * _norm_cdf(d1) - k * df_r * _norm_cdf(d2)
    else:
        price = k * df_r * _norm_cdf(-d2) - s * df_q * _norm_cdf(-d1)
    return float(max(0.0, price))


def black_scholes_greeks(inp: OptionInputs) -> Optional[BlackScholesResult]:
    """
    Returns price + (delta, gamma, vega, theta, rho).

    Conventions:
      - ``vega`` is per 1.0 volatility unit (so 0.01 means "per 1%").
      - ``theta`` is per year (negative for long options near expiry).
    """
    d1, d2 = _d1_d2(inp)
    if d1 is None or d2 is None:
        return None
    s = float(inp.spot)
    k = float(inp.strike)
    t = float(inp.time_to_expiry_years)
    sigma = float(inp.volatility)
    r = float(inp.risk_free_rate)
    q = float(inp.dividend_yield)
    df_r = math.exp(-r * t)
    df_q = math.exp(-q * t)

    pdf_d1 = _norm_pdf(d1)
    cdf_d1 = _norm_cdf(d1)
    cdf_d2 = _norm_cdf(d2)

    if inp.is_call:
        price = s * df_q * cdf_d1 - k * df_r * cdf_d2
        delta = df_q * cdf_d1
        rho = k * t * df_r * cdf_d2 / 100.0  # per 1% rate move
        theta = (
            -s * df_q * pdf_d1 * sigma / (2.0 * math.sqrt(t))
            - r * k * df_r * cdf_d2
            + q * s * df_q * cdf_d1
        )
    else:
        price = k * df_r * _norm_cdf(-d2) - s * df_q * _norm_cdf(-d1)
        delta = -df_q * _norm_cdf(-d1)
        rho = -k * t * df_r * _norm_cdf(-d2) / 100.0
        theta = (
            -s * df_q * pdf_d1 * sigma / (2.0 * math.sqrt(t))
            + r * k * df_r * _norm_cdf(-d2)
            - q * s * df_q * _norm_cdf(-d1)
        )

    gamma = df_q * pdf_d1 / (s * sigma * math.sqrt(t))
    vega = s * df_q * pdf_d1 * math.sqrt(t)

    return BlackScholesResult(
        price=float(max(0.0, price)),
        greeks=OptionGreeks(
            delta=float(delta),
            gamma=float(gamma),
            vega=float(vega),
            theta=float(theta),
            rho=float(rho),
        ),
    )
