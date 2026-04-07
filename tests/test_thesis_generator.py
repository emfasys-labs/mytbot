import pytest

from ai.thesis_generator import ThesisGenerator
from data.scanner import AnomalySignal
from graph.engine import DependencyGraphEngine


@pytest.mark.asyncio
async def test_thesis_generator_stub() -> None:
    g = DependencyGraphEngine("graph/data/relationships.yaml")
    anomaly = AnomalySignal(
        symbol="vix",
        asset_class="index",
        timestamp="2026-04-06T10:00:00Z",
        price_move_pct=25.0,
        price_z_score=3.5,
        volume_ratio=4.0,
        volume_z_score=4.0,
        news_velocity=5.0,
        news_sentiment=-0.8,
        anomaly_score=0.92,
        direction="up",
    )
    ops = g.get_opportunities(anomaly)
    gen = ThesisGenerator(api_key="")
    thesis = await gen.generate(anomaly, ops, market_context={})
    assert thesis is not None
    assert thesis.model_used == "stub_dependency_graph"
    assert thesis.priority_opportunities
