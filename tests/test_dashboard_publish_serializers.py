"""Serializer parity for dashboard UI (global_edge vs D015)."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest

from core.models_runtime import MarketStateComponents, PortfolioState, RegimeState
from portfolio.strategy_opportunity import StrategyOpportunity
from system.dashboard_publish import (
    DASHBOARD_SNAPSHOT_KEY,
    REGIME_TRANSITION_SHADOW_HISTORY_KEY,
    _transition_history_entry,
    append_regime_transition_shadow_history,
    publish_dashboard_snapshot_heartbeat,
    serialize_coordinator_actions,
    serialize_regime_state,
    serialize_strategy_opportunity,
)


class _FakeBus:
    def __init__(self) -> None:
        self.state: dict[str, object] = {}

    async def set_state(self, key: str, value: object) -> None:
        self.state[key] = value

    async def get_state(self, key: str, default: object = None) -> object:
        return self.state.get(key, default)


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


def test_regime_serializer_promotes_transition_shadow_block():
    now = datetime.now(timezone.utc)
    regime = RegimeState(
        timestamp=now,
        regime_label="mixed",
        market_state_score=Decimal("0.1"),
        drawdown_throttle=Decimal("0"),
        execution_quality=Decimal("0.8"),
        breadth_score=Decimal("0.2"),
        components=MarketStateComponents(),
        metadata={
            "regime_transition_used": True,
            "regime_transition_shadow_only": True,
            "regime_transition_probability": 0.53,
            "regime_transition_label": "stress_transition",
            "regime_transition_threshold": 0.45,
            "regime_transition_model_version": "phase_c",
        },
    )

    out = serialize_regime_state(regime)

    assert out["regime_label"] == "mixed"
    assert out["transition"] == {
        "used": True,
        "shadow_only": True,
        "probability": 0.53,
        "label": "stress_transition",
        "threshold": 0.45,
        "model_version": "phase_c",
    }
    assert out["metadata"]["regime_transition_probability"] == 0.53


@pytest.mark.asyncio
async def test_transition_shadow_history_appends_bounded_rows():
    bus = _FakeBus()
    payload = {
        "updated_at": "2026-05-16T22:00:00+00:00",
        "path": "d015",
        "loop_iteration": 11,
        "regime": {
            "regime_label": "mixed",
            "market_state_score": "0.1",
            "breadth_score": "0.2",
            "transition": {
                "used": True,
                "shadow_only": True,
                "probability": 0.53,
                "label": "stress_transition",
                "threshold": 0.45,
                "model_version": "phase_c",
            },
        },
    }

    await append_regime_transition_shadow_history(bus, payload, limit=1)  # type: ignore[arg-type]
    payload["loop_iteration"] = 12
    await append_regime_transition_shadow_history(bus, payload, limit=1)  # type: ignore[arg-type]

    rows = bus.state[REGIME_TRANSITION_SHADOW_HISTORY_KEY]
    assert isinstance(rows, list)
    assert len(rows) == 1
    assert rows[0]["loop_iteration"] == 12
    assert rows[0]["probability"] == 0.53


def test_transition_history_entry_stamps_shadow_policy(monkeypatch):
    monkeypatch.setattr(
        "system.dashboard_publish._phase_c_shadow_policy",
        lambda: {"enabled": True, "trigger_probability": 0.55, "exposure_multiplier": 0.5},
    )
    payload = {
        "updated_at": "2026-05-16T22:00:00+00:00",
        "path": "global_edge",
        "loop_iteration": 1,
        "regime": {
            "regime_label": "mixed",
            "transition": {
                "used": True,
                "probability": 0.56,
                "label": "stress_transition",
                "threshold": 0.45,
            },
        },
    }

    row = _transition_history_entry(payload)

    assert row is not None
    assert row["policy_shadow_enabled"] is True
    assert row["policy_throttle_applied"] is True
    assert row["policy_exposure_multiplier"] == 0.5
