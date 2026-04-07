from data.scanner import AnomalySignal
from graph.engine import DependencyGraphEngine


def test_dependency_graph_oil_up_has_energy_and_airline_paths() -> None:
    g = DependencyGraphEngine("graph/data/relationships.yaml")
    a = AnomalySignal(
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
    ops = g.get_opportunities(a)
    syms = {o.symbol for o in ops}
    assert "XLE" in syms
    assert "UAL" in syms
    assert all(0.0 <= o.blended_confidence <= 0.95 for o in ops)
