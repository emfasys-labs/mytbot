"""Serializer parity for dashboard UI (global_edge vs D015)."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest

from core.models_runtime import PortfolioState
from portfolio.strategy_opportunity import StrategyOpportunity
from system.dashboard_publish import (
    DASHBOARD_SNAPSHOT_KEY,
    publish_dashboard_snapshot_heartbeat,
    serialize_coordinator_actions,
    serialize_strategy_opportunity,
)


class _FakeBus:
    def __init__(self) -> None:
        self.state: dict[str, object] = {}

    async def set_state(self, key: str, value: object) -> None:
        self.state[key] = value


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


@pytest.mark.asyncio
async def test_heartbeat_snapshot_writes_bus_and_shape():
    bus = _FakeBus()
    now = datetime.now(timezone.utc)
    ps = PortfolioState(
        timestamp=now,
        mode="trader",
        nav=Decimal("100000"),
        cash=Decimal("100000"),
        available_buying_power=Decimal("100000"),
        gross_exposure=Decimal("0"),
        net_exposure=Decimal("0"),
        leverage_ratio=Decimal("0"),
    )
    await publish_dashboard_snapshot_heartbeat(
        bus,  # type: ignore[arg-type]
        path="d015",
        loop_iteration=7,
        portfolio_state=ps,
        accumulator=None,
        batch_candidate_count=0,
        universe_symbol_count=12,
        symbols_with_features=0,
        symbols_feature_empty=12,
        reason="no_features",
        message="No rows in feature_snapshots for scanned symbols.",
    )
    raw = bus.state[DASHBOARD_SNAPSHOT_KEY]
    assert isinstance(raw, dict)
    assert raw.get("heartbeat_only") is True
    assert raw.get("path") == "d015"
    assert raw.get("loop_iteration") == 7
    feed = raw.get("dashboard_feed")
    assert isinstance(feed, dict)
    assert feed.get("reason") == "no_features"
    assert feed.get("universe_symbol_count") == 12
    assert raw.get("opportunities") == []
    assert isinstance(raw.get("execution_plan"), dict)
    assert raw["execution_plan"].get("instructions") == []
    assert isinstance(raw.get("fingerprint"), str) and len(raw["fingerprint"]) >= 8


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
