"""Serializer parity for dashboard UI (global_edge vs D015)."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

from portfolio.strategy_opportunity import StrategyOpportunity
from system.dashboard_publish import serialize_coordinator_actions, serialize_strategy_opportunity


def test_strategy_opportunity_includes_opportunity_score_alias():
    o = StrategyOpportunity(
        strategy_name="momentum",
        symbol="TEST",
        side="buy",
        created_at=datetime.now(timezone.utc),
        expected_edge=Decimal("0.02"),
        confidence=Decimal("0.9"),
        capital_required=Decimal("1000"),
        expected_holding_hours=24,
        liquidity_score=Decimal("0.8"),
        execution_score=Decimal("0.7"),
        regime_fit_score=Decimal("0.6"),
        risk_cost_score=Decimal("0.01"),
        priority_score=Decimal("0.812"),
    )
    d = serialize_strategy_opportunity(o)
    assert d["priority_score"] == d["opportunity_score"] == "0.812"
    assert d["tags"] == ["momentum"]


def test_strategy_opportunity_blank_name_uses_global_edge_tag():
    o = StrategyOpportunity(
        strategy_name="",
        symbol="TEST",
        side="buy",
        created_at=datetime.now(timezone.utc),
        expected_edge=Decimal("0.02"),
        confidence=Decimal("0.9"),
        capital_required=Decimal("1000"),
        expected_holding_hours=24,
        liquidity_score=Decimal("0.8"),
        execution_score=Decimal("0.7"),
        regime_fit_score=Decimal("0.6"),
        risk_cost_score=Decimal("0.01"),
        priority_score=Decimal("0.5"),
    )
    d = serialize_strategy_opportunity(o)
    assert d["tags"] == ["global_edge"]


def test_coordinator_action_includes_action_alias():
    a = SimpleNamespace(
        kind="reduce",
        symbol="X",
        strategy_name="s",
        capital=Decimal("1"),
        priority_score=Decimal("0.5"),
    )
    out = serialize_coordinator_actions([a])
    assert len(out) == 1
    assert out[0]["kind"] == "reduce"
    assert out[0]["action"] == "reduce"
