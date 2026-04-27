"""
data/fundamental_features.py
=============================
Wave 3 — value / quality / carry features sourced from a per-symbol
fundamentals dict.

Defensive: every function returns ``None`` when the required input is
missing or non-positive (so cross-sectional ranking can simply ignore
that symbol for that factor). No exceptions on missing keys.

The fundamentals dict is a thin abstraction over whatever upstream
pipeline supplies the values (Finnhub, IBKR fundamentals, manual
overrides). Expected keys are documented inline; consumers are not
required to populate every key.
"""

from __future__ import annotations

import math
from typing import Mapping, Optional


def _pos(value: object) -> Optional[float]:
    if value is None:
        return None
    try:
        v = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if not math.isfinite(v) or v <= 0:
        return None
    return v


def _finite(value: object) -> Optional[float]:
    if value is None:
        return None
    try:
        v = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


# ── value family ─────────────────────────────────────────────────────────────


def earnings_yield(fund: Mapping[str, object]) -> Optional[float]:
    """E/P. Inputs: ``eps_ttm``, ``price``."""
    eps = _finite(fund.get("eps_ttm"))
    px = _pos(fund.get("price"))
    if eps is None or px is None:
        return None
    return eps / px


def book_to_market(fund: Mapping[str, object]) -> Optional[float]:
    """B/M. Inputs: ``book_value_per_share``, ``price``."""
    bv = _finite(fund.get("book_value_per_share"))
    px = _pos(fund.get("price"))
    if bv is None or px is None:
        return None
    return bv / px


def fcf_yield(fund: Mapping[str, object]) -> Optional[float]:
    """FCF / EV (preferred) or FCF / market cap as fallback."""
    fcf = _finite(fund.get("free_cash_flow"))
    ev = _pos(fund.get("enterprise_value"))
    if fcf is not None and ev is not None:
        return fcf / ev
    mcap = _pos(fund.get("market_cap"))
    if fcf is not None and mcap is not None:
        return fcf / mcap
    return None


def sales_yield(fund: Mapping[str, object]) -> Optional[float]:
    """Revenue / EV (or / market cap fallback)."""
    rev = _finite(fund.get("revenue_ttm"))
    ev = _pos(fund.get("enterprise_value"))
    if rev is not None and ev is not None:
        return rev / ev
    mcap = _pos(fund.get("market_cap"))
    if rev is not None and mcap is not None:
        return rev / mcap
    return None


# ── quality family ───────────────────────────────────────────────────────────


def profitability(fund: Mapping[str, object]) -> Optional[float]:
    """Operating profit / total assets (gross profitability proxy)."""
    op = _finite(fund.get("operating_income"))
    assets = _pos(fund.get("total_assets"))
    if op is None or assets is None:
        return None
    return op / assets


def margin_stability(fund: Mapping[str, object]) -> Optional[float]:
    """
    1 - stdev(operating margin) over the last N years, expressed as a
    quality score in [0, 1]. Caller supplies ``operating_margin_history``
    as an iterable of floats.
    """
    hist = fund.get("operating_margin_history")
    if not isinstance(hist, (list, tuple)) or len(hist) < 3:
        return None
    vals: list[float] = []
    for x in hist:
        f = _finite(x)
        if f is not None:
            vals.append(f)
    if len(vals) < 3:
        return None
    n = len(vals)
    mean = sum(vals) / n
    var = sum((v - mean) ** 2 for v in vals) / max(1, n - 1)
    sd = math.sqrt(var)
    # Map sd → score: 0 sd ⇒ 1.0, large sd ⇒ → 0.
    return float(1.0 / (1.0 + sd))


def leverage(fund: Mapping[str, object]) -> Optional[float]:
    """
    Debt / equity (lower is better → composite_factor_score flips sign).
    Returns ``None`` when equity is missing/non-positive.
    """
    debt = _finite(fund.get("total_debt"))
    eq = _pos(fund.get("total_equity"))
    if debt is None or eq is None:
        return None
    return debt / eq


def accruals_proxy(fund: Mapping[str, object]) -> Optional[float]:
    """
    Accruals = (NI - CFO) / total assets. Negative accruals are a quality
    signal (operating cash flow exceeds reported earnings).
    """
    ni = _finite(fund.get("net_income"))
    cfo = _finite(fund.get("operating_cash_flow"))
    assets = _pos(fund.get("total_assets"))
    if ni is None or cfo is None or assets is None:
        return None
    return (ni - cfo) / assets


# ── carry family ─────────────────────────────────────────────────────────────


def dividend_yield(fund: Mapping[str, object]) -> Optional[float]:
    div = _finite(fund.get("dividends_per_share_ttm"))
    px = _pos(fund.get("price"))
    if div is None or px is None:
        return None
    return div / px


def fx_carry(fund: Mapping[str, object]) -> Optional[float]:
    """
    FX carry = (foreign rate - domestic rate). Inputs: ``foreign_rate``,
    ``domestic_rate`` (annualised decimals).
    """
    fr = _finite(fund.get("foreign_rate"))
    dr = _finite(fund.get("domestic_rate"))
    if fr is None or dr is None:
        return None
    return fr - dr


def crypto_funding_carry(fund: Mapping[str, object]) -> Optional[float]:
    """
    Average funding rate over a recent window (e.g. 7 days). Negative
    funding ⇒ shorts pay longs ⇒ positive carry for longs.
    """
    return _finite(fund.get("avg_funding_7d"))


def bond_yield_carry(fund: Mapping[str, object]) -> Optional[float]:
    """Yield to maturity minus short-rate proxy (expression of slope)."""
    ytm = _finite(fund.get("yield_to_maturity"))
    short = _finite(fund.get("short_rate"))
    if ytm is None or short is None:
        return ytm  # fall back to absolute yield
    return ytm - short


# ── catch-all builder ────────────────────────────────────────────────────────


def build_fundamental_factors(fund: Mapping[str, object] | None) -> dict[str, Optional[float]]:
    """Compute every fundamental factor we can from ``fund``."""
    f = fund or {}
    return {
        "earnings_yield": earnings_yield(f),
        "book_to_market": book_to_market(f),
        "fcf_yield": fcf_yield(f),
        "sales_yield": sales_yield(f),
        "profitability": profitability(f),
        "margin_stability": margin_stability(f),
        "leverage": leverage(f),
        "accruals_proxy": accruals_proxy(f),
        "dividend_yield": dividend_yield(f),
        "fx_carry": fx_carry(f),
        "crypto_funding_carry": crypto_funding_carry(f),
        "bond_yield_carry": bond_yield_carry(f),
    }
