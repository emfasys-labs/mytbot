"""D165 — multiplier-aware futures sizing in SignalEngine.

A futures order must be sized in WHOLE contracts. Internally we keep the
quantity in notional-consistent UNITS (contracts * multiplier) so downstream
``notional = qty * price`` accounting stays correct; the IBKR adapter converts
units <-> contracts at the broker boundary.
"""

from __future__ import annotations

from decimal import Decimal

from signals.engine import RawSignal, SignalEngine


def _cfg(**overrides: object) -> dict:
    base = {
        "default_position_pct": 0.05,
        "min_quantity": 0.0001,
        "quantity_decimals": 4,
        "volatility_sizing": {"enabled": True},
    }
    base.update(overrides)
    return base


def test_futures_sized_in_whole_contracts() -> None:
    eng = SignalEngine(_cfg())
    raw = RawSignal(
        strategy="trend_breakout",
        symbol="CL=F",
        side="buy",
        confidence=0.8,
        broker="ibkr",
        asset_class="future",
        metadata={"close": 74.0, "target_notional": "160000.00"},
    )
    out = eng.process(raw, portfolio_value=Decimal("1000000"), news_score=None)
    assert out is not None
    # 160000 / 74 = 2162.16 units → /1000 = 2.16 → floor 2 contracts → 2000 units
    assert out.suggested_quantity == Decimal("2000")
    resolved = Decimal(out.metadata["signal_engine_resolved_notional"])
    # 2 contracts * 74 * 1000 = 148000
    assert resolved == Decimal("148000.00")


def test_futures_sub_contract_budget_is_dropped() -> None:
    eng = SignalEngine(_cfg())
    raw = RawSignal(
        strategy="mean_reversion",
        symbol="CL=F",
        side="buy",
        confidence=0.7,
        broker="ibkr",
        asset_class="future",
        # 30000 / 74 = 405 units → /1000 = 0.4 → 0 contracts → cannot express
        metadata={"close": 74.0, "target_notional": "30000.00"},
    )
    out = eng.process(raw, portfolio_value=Decimal("1000000"), news_score=None)
    assert out is None


def test_futures_high_multiplier_si() -> None:
    eng = SignalEngine(_cfg())
    raw = RawSignal(
        strategy="trend_following",
        symbol="SI=F",
        side="buy",
        confidence=0.75,
        broker="ibkr",
        asset_class="future",
        # SI multiplier 5000, price 30 → 1 contract = 150000 notional
        metadata={"close": 30.0, "target_notional": "320000.00"},
    )
    out = eng.process(raw, portfolio_value=Decimal("2000000"), news_score=None)
    assert out is not None
    # 320000/30 = 10666.6 units → /5000 = 2.13 → 2 contracts → 10000 units
    assert out.suggested_quantity == Decimal("10000")


def test_equity_sizing_unaffected_by_futures_path() -> None:
    """Control: a normal equity (and an equity whose ticker == a futures root)
    must keep fractional notional sizing, never the whole-contract path."""
    eng = SignalEngine(_cfg())
    # 'CL' bare is Colgate-Palmolive, NOT crude oil futures.
    raw = RawSignal(
        strategy="momentum",
        symbol="CL",
        side="buy",
        confidence=0.7,
        broker="alpaca",
        asset_class="equity",
        metadata={"close": 90.0, "target_notional": "4500.00"},
    )
    out = eng.process(raw, portfolio_value=Decimal("100000"), news_score=None)
    assert out is not None
    # 4500 / 90 = 50 (fractional/share sizing, multiplier NOT applied)
    assert out.suggested_quantity == Decimal("50.0000")
