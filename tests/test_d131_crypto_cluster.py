"""
tests/test_d131_crypto_cluster.py
==================================
D131 — Crypto cluster cap.

The 2026-05-23 audit showed $251k of one-direction long crypto stacked
across kraken / binance / bybit ($1.21M NAV ≈ 21 %): five "single
names" each under the 5 % per-name cap, but one correlated crypto-beta
bet. Crypto dropped 3 – 5 % overnight and they all lost together
(-$9 324). This cap bounds the aggregate signed crypto notional across
every venue.
"""
from __future__ import annotations

from decimal import Decimal

from risk.engine import RiskEngine
from signals.engine import Signal


def _engine(**overrides) -> RiskEngine:
    cfg = {
        "min_signal_confidence": 0.0,
        "min_trade_quality_score": 0.0,
        "theme_uniqueness_check": False,
        "require_catalyst": False,
        "enforce_static_exposure_caps": False,
        "enforce_static_order_caps": False,
        "max_consecutive_losses": 999,
        "max_daily_loss_pct": 1.0,
        "max_drawdown_pct": 1.0,
        "max_loss_per_trade_pct": 1.0,
        "crypto_cluster": {"enabled": True, "max_net_exposure_pct": "0.10"},
        # D125 hard rails would otherwise trip 5%+ single-name notionals.
        "single_name_notional": {"enabled": False},
        "intraday_symbol_adds": {"enabled": False},
    }
    cfg.update(overrides)
    return RiskEngine(cfg)


def _sig(*, symbol, side, qty, price, asset_class="crypto", broker="binance") -> Signal:
    return Signal(
        signal_id=f"sig_{symbol}_{side}",
        symbol=symbol,
        side=side,
        strategy="mean_reversion",
        confidence=0.8,
        suggested_quantity=Decimal(qty),
        suggested_price=Decimal(price),
        broker=broker,
        asset_class=asset_class,
        timestamp="2026-05-23T08:00:00Z",
        metadata={},
    )


def _port(nav: str, positions: dict | None = None) -> dict:
    return {
        "portfolio_value": Decimal(nav),
        "tradable_capital": Decimal(nav),
        "exposure_total": Decimal("0"),
        "asset_class_exposure": {},
        "positions": positions or {},
    }


def test_non_crypto_signal_skipped():
    eng = _engine()
    decision = eng.evaluate(
        _sig(symbol="AAPL", side="buy", qty="100", price="300", asset_class="equity"),
        _port(nav="1000000"),
    )
    assert decision.verdict.value == "approved"


def test_first_crypto_within_cap_allowed():
    eng = _engine()
    # 0.5 BTC @ 75000 = $37,500 < 10% of $1M = $100k.
    decision = eng.evaluate(
        _sig(symbol="BTC-USD", side="buy", qty="0.5", price="75000"),
        _port(nav="1000000"),
    )
    assert decision.verdict.value == "approved"


def test_cluster_aggregates_across_venues():
    """The bug we are fixing: BTC long on kraken + ETH long on binance +
    SOL long on bybit each look small per-name but together exceed the
    aggregate cluster cap. The cap is a capacity rail: if room remains,
    the order is clamped to that room instead of rejected outright."""
    eng = _engine()
    positions = {
        # asset_class on the position is what production reconciliation writes.
        "kraken:BTC-USD":   {"symbol": "BTC-USD",  "asset_class": "crypto",
                             "quantity": "0.8",  "current_price": "75000"},  # +60,000
        "binance:ETH-USD":  {"symbol": "ETH-USD", "asset_class": "crypto",
                             "quantity": "15",   "current_price": "2000"},   # +30,000
    }
    # Already +90k. Add SOL-USD long $20k on bybit → projected +110k > $100k cap.
    sig = _sig(symbol="SOL-USD", side="buy", qty="250", price="80", broker="bybit")
    decision = eng.evaluate(sig, _port(nav="1000000", positions=positions))
    assert decision.verdict.value == "approved"
    assert sig.suggested_quantity == Decimal("125")
    assert sig.metadata["risk_crypto_cluster_clamped"] is True
    assert sig.metadata["risk_crypto_cluster_effective_notional"] == "10000.00"


def test_neutralising_leg_reduces_cluster_and_is_allowed():
    """A short that REDUCES the absolute cluster exposure must always
    pass — it is risk-reducing even when total |cluster| is already over
    the cap."""
    eng = _engine()
    positions = {
        "binance:BTC-USD": {"symbol": "BTC-USD", "asset_class": "crypto",
                            "quantity": "2.0", "current_price": "75000"},   # +150,000 (over cap)
    }
    # ETH short of $30k REDUCES net cluster magnitude from 150k → 120k.
    sig = _sig(symbol="ETH-USD", side="sell", qty="15", price="2000")
    decision = eng.evaluate(sig, _port(nav="1000000", positions=positions))
    assert decision.verdict.value == "approved"


def test_reduce_only_never_blocked():
    eng = _engine()
    positions = {
        "binance:BTC-USD": {"symbol": "BTC-USD", "asset_class": "crypto",
                            "quantity": "3.0", "current_price": "75000"},   # +225,000 over cap
    }
    sig = _sig(symbol="BTC-USD", side="sell", qty="3", price="75000")
    sig.metadata = {"reduce_only": True}
    decision = eng.evaluate(sig, _port(nav="1000000", positions=positions))
    assert decision.verdict.value == "approved"


def test_disabled_check_allows_anything():
    eng = _engine(crypto_cluster={"enabled": False, "max_net_exposure_pct": "0.10"})
    positions = {
        "kraken:BTC-USD": {"symbol": "BTC-USD", "asset_class": "crypto",
                           "quantity": "5.0", "current_price": "75000"},    # +375,000
    }
    sig = _sig(symbol="ETH-USD", side="buy", qty="100", price="2000")       # +200,000
    decision = eng.evaluate(sig, _port(nav="1000000", positions=positions))
    assert decision.verdict.value == "approved"


def test_dash_usd_symbol_with_missing_asset_class_still_detected():
    """Some signals do not carry asset_class; fall back to the canonical
    "-USD" suffix used for spot crypto pairs."""
    eng = _engine()
    positions = {
        "binance:BTC-USD": {"symbol": "BTC-USD",  # no asset_class field
                            "quantity": "1.5", "current_price": "75000"},   # +112,500 over cap
    }
    sig = _sig(symbol="ETH-USD", side="buy", qty="10", price="2000",
               asset_class="")  # blank asset_class
    decision = eng.evaluate(sig, _port(nav="1000000", positions=positions))
    assert decision.verdict.value == "rejected"
    assert "crypto_cluster" in (decision.reason or "")


def test_crypto_cluster_cap_scales_down_with_market_state():
    eng = _engine()
    positions = {
        "binance:BTC-USD": {
            "symbol": "BTC-USD",
            "asset_class": "crypto",
            "quantity": "0.5",
            "current_price": "50000",
        },
    }
    sig = _sig(symbol="ETH-USD", side="buy", qty="1", price="5000", asset_class="crypto")
    port = _port(nav="1000000", positions=positions)
    port["metadata"] = {"market_state_score": 0.20}

    decision = eng.evaluate(sig, port)

    assert decision.verdict.value == "rejected"
    assert "crypto_cluster" in (decision.reason or "")


def test_crypto_cluster_rejects_when_already_over_cap_and_increasing():
    eng = _engine()
    positions = {
        "binance:BTC-USD": {"symbol": "BTC-USD", "asset_class": "crypto",
                            "quantity": "3.0", "current_price": "75000"},
    }
    sig = _sig(symbol="ETH-USD", side="buy", qty="1", price="2000")

    decision = eng.evaluate(sig, _port(nav="1000000", positions=positions))

    assert decision.verdict.value == "rejected"
    assert "crypto_cluster" in (decision.reason or "")
