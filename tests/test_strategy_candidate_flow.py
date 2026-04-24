"""D033 — multi-strategy candidates, coordinator dedupe, pre-execution log."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from portfolio.global_edge_coordinator import dedupe_opportunities_by_symbol
from portfolio.strategy_opportunity import StrategyOpportunity


def _opp(
    symbol: str,
    strategy: str,
    ps: str,
) -> StrategyOpportunity:
    return StrategyOpportunity(
        strategy_name=strategy,
        symbol=symbol,
        side="long",
        created_at=datetime.now(timezone.utc),
        expected_edge=Decimal("0.1"),
        confidence=Decimal("0.5"),
        capital_required=Decimal("1000"),
        expected_holding_hours=24,
        liquidity_score=Decimal("0.7"),
        execution_score=Decimal("0.75"),
        regime_fit_score=Decimal("0.8"),
        risk_cost_score=Decimal("0.05"),
        priority_score=Decimal(ps),
        metadata={},
    )


def test_dedupe_keeps_highest_priority_per_symbol():
    a = _opp("SPY", "momentum_breakout", "0.50")
    b = _opp("SPY", "mean_reversion", "0.80")
    c = _opp("QQQ", "momentum_breakout", "0.40")
    out, lost = dedupe_opportunities_by_symbol([a, b, c])
    assert len(out) == 2
    by_sym = {o.symbol: o for o in out}
    assert by_sym["SPY"].strategy_name == "mean_reversion"
    assert by_sym["QQQ"].strategy_name == "momentum_breakout"
    assert len(lost) == 1
    assert lost[0][0].strategy_name == "momentum_breakout"
    assert lost[0][1].strategy_name == "mean_reversion"


def test_dedupe_leaves_arbitrage_untouched():
    f = _opp("BTC-USD", "funding_rate_arbitrage", "0.33")
    d = _opp("BTC-USD", "momentum_breakout", "0.90")
    out, lost = dedupe_opportunities_by_symbol([f, d])
    assert len(out) == 2
    assert lost == []
