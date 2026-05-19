from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from core.session_exit_policy import evaluate_session_exit
from portfolio.global_edge_coordinator import GlobalEdgeCoordinator, HeldPositionEdge


def _utc(y: int, m: int, d: int, hh: int, mm: int = 0) -> datetime:
    return datetime(y, m, d, hh, mm, tzinfo=timezone.utc)


def test_intraday_position_closes_near_market_close(monkeypatch) -> None:
    monkeypatch.setenv("MARKET_SESSION_GATE", "1")
    d = evaluate_session_exit(
        broker="ibkr",
        asset_class="equity",
        symbol="AAPL",
        quantity=Decimal("10"),
        avg_entry_price=Decimal("100"),
        current_price=Decimal("101"),
        strategy_name="intraday_breakout",
        metadata={"holding_horizon": "intraday"},
        profile_mode="trader",
        now=_utc(2026, 5, 13, 19, 45),
    )
    assert d.action == "close_before_close"
    assert d.should_submit_order is True
    assert d.reduce_fraction == Decimal("1")


def test_swing_position_holds_through_close_by_default(monkeypatch) -> None:
    monkeypatch.setenv("MARKET_SESSION_GATE", "1")
    d = evaluate_session_exit(
        broker="ibkr",
        asset_class="equity",
        symbol="MSFT",
        quantity=Decimal("5"),
        avg_entry_price=Decimal("100"),
        current_price=Decimal("100.5"),
        strategy_name="momentum_breakout",
        metadata={"holding_horizon": "swing"},
        profile_mode="trader",
        now=_utc(2026, 5, 13, 19, 45),
    )
    assert d.action == "hold_through_close"
    assert d.should_submit_order is False


def test_defender_trims_profitable_swing_before_close(monkeypatch) -> None:
    monkeypatch.setenv("MARKET_SESSION_GATE", "1")
    d = evaluate_session_exit(
        broker="ibkr",
        asset_class="equity",
        symbol="NVDA",
        quantity=Decimal("2"),
        avg_entry_price=Decimal("100"),
        current_price=Decimal("102"),
        strategy_name="momentum_breakout",
        metadata={"holding_horizon": "swing"},
        profile_mode="defender",
        now=_utc(2026, 5, 13, 19, 45),
    )
    assert d.action == "trim_before_close"
    assert d.should_submit_order is True
    assert d.reduce_fraction == Decimal("0.50")


def test_crypto_position_has_no_pre_close_action(monkeypatch) -> None:
    monkeypatch.setenv("MARKET_SESSION_GATE", "1")
    d = evaluate_session_exit(
        broker="kraken",
        asset_class="crypto",
        symbol="BTC-USD",
        quantity=Decimal("1"),
        avg_entry_price=Decimal("100"),
        current_price=Decimal("101"),
        strategy_name="intraday_crypto",
        metadata={"holding_horizon": "intraday"},
        profile_mode="trader",
        now=_utc(2026, 5, 17, 19, 45),
    )
    assert d.action == "hold_through_close"
    assert d.reason == "no_finite_session_close"
    assert d.should_submit_order is False


def test_coordinator_emits_session_exit_reduce_action(monkeypatch) -> None:
    monkeypatch.setenv("MARKET_SESSION_GATE", "1")
    coord = GlobalEdgeCoordinator({})
    held = [
        HeldPositionEdge(
            symbol="AAPL",
            notional=Decimal("1010"),
            expected_remaining_edge=Decimal("0.04"),
            strategy_name="intraday_breakout",
            broker="ibkr",
            metadata={
                "quantity": "10",
                "avg_entry_price": "100",
                "close": "101",
                "side": "long",
                "asset_class": "equity",
                "holding_horizon": "intraday",
            },
        )
    ]
    actions = coord.propose_session_exit_actions(
        held,
        active_mode="trader",
        now=_utc(2026, 5, 13, 19, 45),
    )
    assert len(actions) == 1
    action = actions[0]
    assert action.kind == "trim_symbol"
    assert action.strategy_name == "session_exit_policy"
    assert action.capital == Decimal("1010.00000000")
    assert action.metadata["close_only"] is True
    assert action.metadata["session_exit_action"] == "close_before_close"
