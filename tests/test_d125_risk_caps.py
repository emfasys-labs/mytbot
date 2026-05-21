"""D125 fix #1 + #5 — single-name notional cap and per-day cumulative-add cap.

The 2026-05-21 BF-B audit found one ticker reaching 28.5% of NAV via 38
consecutive volume_flow buys, with every legacy single-stock cap inert
because `enforce_static_exposure_caps=False` (the documented default).
These tests pin down the unconditional hard rails.
"""
from __future__ import annotations

from decimal import Decimal
from datetime import datetime, timezone

import pytest

from risk.engine import RiskEngine, Signal


def _sig(
    *,
    symbol: str = "BF-B",
    side: str = "buy",
    qty: str = "1000",
    price: str = "25.0",
    meta: dict | None = None,
    asset_class: str = "equity",
    strategy: str = "vol",
) -> Signal:
    return Signal(
        signal_id=f"s-{symbol}-{side}-{qty}",
        symbol=symbol,
        side=side,
        strategy=strategy,
        confidence=0.7,
        suggested_quantity=Decimal(qty),
        suggested_price=Decimal(price),
        broker="ibkr",
        asset_class=asset_class,
        timestamp=datetime.now(timezone.utc).isoformat(),
        metadata=meta or {},
    )


def _engine(**overrides) -> RiskEngine:
    cfg = {
        "single_name_notional": {"enabled": True, "max_pct_nav": "0.05"},
        "intraday_symbol_adds": {"enabled": True, "max_pct_nav": "0.10"},
    }
    cfg.update(overrides)
    return RiskEngine(cfg)


def _portfolio(nav: str = "1000000", positions: dict | None = None) -> dict:
    return {
        "portfolio_value": nav,
        "tradable_capital": nav,
        "positions": positions or {},
    }


# ── Fix #1 — single-name notional cap ─────────────────────────────────────────


def test_single_name_open_under_cap_passes():
    e = _engine()
    sig = _sig(qty="1600", price="25.0")  # $40k = 4% of $1M
    ok, label = e._check_single_name_notional(sig, _portfolio())
    assert ok and label == "single_name_notional"


def test_single_name_open_over_cap_rejected():
    e = _engine()
    sig = _sig(qty="2400", price="25.0")  # $60k = 6% of $1M
    ok, _ = e._check_single_name_notional(sig, _portfolio())
    assert not ok


def test_single_name_existing_plus_new_over_cap_rejected():
    """The BF-B pattern: small per-add signals compounding past the cap."""
    e = _engine()
    portfolio = _portfolio(
        positions={"BF-B": {"symbol": "BF-B", "quantity": "1600", "current_price": "25.0"}}
    )
    # 4% existing + 2% new = 6% projected → reject
    sig = _sig(qty="800", price="25.0")
    ok, _ = e._check_single_name_notional(sig, portfolio)
    assert not ok


def test_single_name_reduce_only_exempt():
    """Exits must never be blocked even at 50% notional on an over-cap name."""
    e = _engine()
    sig = _sig(side="sell", qty="20000", meta={"reduce_only": True})
    ok, _ = e._check_single_name_notional(sig, _portfolio())
    assert ok


def test_single_name_cap_unconditional_independent_of_enforce_static():
    """The cap binds even when `enforce_static_exposure_caps=False`."""
    e = _engine()
    e.config["enforce_static_exposure_caps"] = False
    sig = _sig(qty="2400", price="25.0")  # 6% — over cap
    ok, _ = e._check_single_name_notional(sig, _portfolio())
    assert not ok


def test_single_name_disabled_when_config_off():
    e = _engine(single_name_notional={"enabled": False, "max_pct_nav": "0.05"})
    sig = _sig(qty="100000", price="25.0")  # 250% NAV
    ok, _ = e._check_single_name_notional(sig, _portfolio())
    assert ok


# ── Fix #5 — per-UTC-day cumulative-add cap ───────────────────────────────────


def test_intraday_adds_under_cap_passes():
    e = _engine()
    e._intraday_added_notional["XYZ"] = Decimal("80000")  # 8% pre-loaded
    e._intraday_adds_day_key = datetime.now(timezone.utc).date().isoformat()
    sig = _sig(symbol="XYZ", qty="800", price="25.0")  # +2% → 10%
    ok, _ = e._check_intraday_symbol_adds(sig, _portfolio())
    assert ok


def test_intraday_adds_over_cap_rejected():
    e = _engine()
    e._intraday_added_notional["XYZ"] = Decimal("80000")
    e._intraday_adds_day_key = datetime.now(timezone.utc).date().isoformat()
    sig = _sig(symbol="XYZ", qty="1200", price="25.0")  # +3% → 11%
    ok, _ = e._check_intraday_symbol_adds(sig, _portfolio())
    assert not ok


def test_intraday_adds_resets_on_utc_date_change():
    e = _engine()
    e._intraday_added_notional["XYZ"] = Decimal("999999")
    e._intraday_adds_day_key = "2020-01-01"
    e._roll_intraday_adds_day_if_needed()
    assert e._intraday_added_notional == {}
    assert e._intraday_adds_day_key == datetime.now(timezone.utc).date().isoformat()


def test_intraday_adds_reduce_only_exempt():
    e = _engine()
    e._intraday_added_notional["XYZ"] = Decimal("999999")
    e._intraday_adds_day_key = datetime.now(timezone.utc).date().isoformat()
    sig = _sig(symbol="XYZ", side="sell", qty="20000", meta={"reduce_only": True})
    ok, _ = e._check_intraday_symbol_adds(sig, _portfolio())
    assert ok


def test_record_open_signal_notional_accumulates():
    e = _engine()
    e.record_open_signal_notional("AAPL", Decimal("25000"))
    e.record_open_signal_notional("aapl", Decimal("10000"))  # case-insensitive
    e.record_open_signal_notional("AAPL", Decimal("5000"))
    assert e._intraday_added_notional["AAPL"] == Decimal("40000")


def test_record_open_signal_notional_skips_non_positive():
    e = _engine()
    e.record_open_signal_notional("AAPL", Decimal("0"))
    e.record_open_signal_notional("AAPL", Decimal("-100"))
    e.record_open_signal_notional("", Decimal("100"))
    assert "AAPL" not in e._intraday_added_notional


# ── Full evaluate() integration — record on approval, not on rejection ────────


def test_approved_signal_records_to_intraday_tracker():
    e = _engine()
    # Disable noisy unrelated gates that need richer state.
    e.config["max_consecutive_losses"] = 0
    e.config["min_signal_confidence"] = 0.0
    sig = _sig(symbol="MSFT", qty="100", price="100")  # $10k = 1% of NAV
    sig.confidence = 0.99
    portfolio = _portfolio()
    decision = e.evaluate(sig, portfolio)
    assert decision.verdict.name == "APPROVED", f"unexpected reject: {decision.reason}"
    assert e._intraday_added_notional.get("MSFT") == Decimal("10000.00")


def test_rejected_signal_does_not_record_to_intraday_tracker():
    e = _engine()
    sig = _sig(symbol="MSFT", qty="10000", price="100")  # $1M = 100% — over cap
    portfolio = _portfolio()
    decision = e.evaluate(sig, portfolio)
    assert decision.verdict.name == "REJECTED"
    assert "MSFT" not in e._intraday_added_notional
