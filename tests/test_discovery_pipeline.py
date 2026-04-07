from decimal import Decimal

import pytest

from data.scanner import AnomalySignal
from graph.engine import DependencyOpportunity
from graph.pipeline import DiscoveryPipeline
from signals.engine import SignalEngine


class _FakeScanner:
    async def scan(self):
        return [
            AnomalySignal(
                symbol="crude_oil_price",
                asset_class="commodity",
                timestamp="2026-04-06T09:14:00Z",
                price_move_pct=3.2,
                price_z_score=2.8,
                volume_ratio=3.1,
                volume_z_score=3.1,
                news_velocity=4.2,
                news_sentiment=-0.3,
                anomaly_score=0.78,
                direction="up",
            )
        ]


class _FakeGraph:
    def get_opportunities(self, _anomaly):
        return [
            DependencyOpportunity(
                symbol="XLE",
                asset_class="etf",
                direction="up",
                trigger_symbol="crude_oil_price",
                trigger_move_pct=3.2,
                static_confidence=0.8,
                live_correlation=1.0,
                blended_confidence=0.9,
                expected_lag_hours=0,
                thesis="Energy up when oil spikes",
            )
        ]


class _FakeThesis:
    trigger_explanation = "Oil supply shock"


class _FakeThesisGenerator:
    async def generate(self, *args, **kwargs):
        return _FakeThesis()


@pytest.mark.asyncio
async def test_discovery_pipeline_emits_signal():
    signal_engine = SignalEngine({"default_position_pct": 0.05, "min_quantity": "0.0001", "quantity_decimals": 8})
    p = DiscoveryPipeline(_FakeScanner(), _FakeGraph(), _FakeThesisGenerator(), signal_engine)
    out = await p.run_cycle(portfolio_value=Decimal("100000"), market_context={})
    assert out
    assert out[0].strategy == "dependency_graph"
    assert out[0].symbol == "XLE"
