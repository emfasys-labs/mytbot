"""D031E — stop-loss scaffold regression tests.

These exercise the pure ``evaluate_stop_loss`` helper. Runtime enforcement is
not yet wired (by design); these tests freeze the decision logic so the
follow-up wiring task cannot silently regress it.
"""

from __future__ import annotations

from decimal import Decimal

from risk.stop_loss import StopLossDecision, evaluate_stop_loss


def test_portfolio_stop_triggers_when_loss_exceeds_budget() -> None:
    decision = evaluate_stop_loss(
        symbol="COHR",
        quantity=Decimal("335"),
        avg_entry_price=Decimal("354.11"),
        current_price=Decimal("310.00"),  # -12.5% drawdown
        nav=Decimal("1000000"),
        max_loss_per_trade_pct=Decimal("0.01"),  # 10k budget
    )
    assert isinstance(decision, StopLossDecision)
    assert decision.should_close is True
    assert "portfolio_loss_budget" in decision.reason
    assert decision.loss_absolute > Decimal("14000")


def test_portfolio_stop_quiet_within_budget() -> None:
    decision = evaluate_stop_loss(
        symbol="FCOM",
        quantity=Decimal("135"),
        avg_entry_price=Decimal("74.18"),
        current_price=Decimal("73.74"),  # -0.6%, £59 loss
        nav=Decimal("1000000"),
        max_loss_per_trade_pct=Decimal("0.01"),  # 10k budget
    )
    assert decision.should_close is False
    assert decision.reason == "within_budget"


def test_structural_stop_from_atr_pct() -> None:
    decision = evaluate_stop_loss(
        symbol="COHR",
        quantity=Decimal("22"),
        avg_entry_price=Decimal("355"),
        current_price=Decimal("340"),
        nav=Decimal("1000000"),
        max_loss_per_trade_pct=Decimal("0.01"),
        metadata={
            "stop_loss_atr": "1.5",
            "atr_pct": "0.02",  # 2% ATR → stop at 355 - 1.5*7.10 = 344.35
        },
    )
    assert decision.structural_stop_price is not None
    assert decision.structural_stop_breached is True
    assert decision.should_close is True
    assert "structural_stop" in decision.reason


def test_short_position_structural_stop() -> None:
    decision = evaluate_stop_loss(
        symbol="XYZ",
        quantity=Decimal("-50"),
        avg_entry_price=Decimal("100"),
        current_price=Decimal("103"),
        nav=Decimal("1000000"),
        max_loss_per_trade_pct=Decimal("0.01"),
        metadata={"stop_loss_atr": "1.0", "atr": "2.0"},  # stop at 102
    )
    assert decision.structural_stop_price == Decimal("102")
    assert decision.structural_stop_breached is True


def test_invalid_prices_return_safe_default() -> None:
    decision = evaluate_stop_loss(
        symbol="ZZZ",
        quantity=Decimal("10"),
        avg_entry_price=Decimal("0"),
        current_price=Decimal("50"),
        nav=Decimal("1000000"),
        max_loss_per_trade_pct=Decimal("0.01"),
    )
    assert decision.should_close is False
    assert decision.reason == "invalid_prices"
