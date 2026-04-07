from __future__ import annotations

import asyncio

from ai.thesis_generator import ThesisGenerator
from data.scanner import AnomalySignal
from data.universe import UniverseManager
from graph.engine import DependencyGraphEngine


def main() -> None:
    universe = UniverseManager()
    print(f"Universe size: {len(universe)} instruments")
    print(f"Triggers: {len(universe.get_triggers())}")
    print(f"Crypto: {len(universe.get_by_asset_class('crypto'))}")
    print(f"ETFs: {len(universe.get_by_asset_class('etf'))}")

    graph = DependencyGraphEngine()
    oil_anomaly = AnomalySignal(
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
        sector="commodities",
    )

    print("\n" + "=" * 60)
    print("TEST: Oil spike +3.2% (z=2.8)")
    print("=" * 60)
    opportunities = graph.get_opportunities(oil_anomaly)
    for o in opportunities:
        print(
            f"  {o.symbol:10} | {o.direction:4} | confidence={o.blended_confidence:.2f} | "
            f"lag={o.expected_lag_hours:2}hr | {o.thesis[:50]}"
        )

    print("\n" + "=" * 60)
    print("TEST: Full dependency chain — VIX spike")
    print("=" * 60)
    vix_anomaly = AnomalySignal(
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
    chain = graph.get_full_chain(vix_anomaly, depth=2)
    print(f"Level 1 impacts: {len(chain['level_1'])}")
    for o in chain["level_1"]:
        print(f"  L1: {o.symbol:10} {o.direction} | {o.blended_confidence:.2f}")
    print(f"Level 2 impacts: {len(chain['level_2'])}")
    for o in chain["level_2"][:5]:
        print(f"  L2: {o.symbol:10} {o.direction} | {o.blended_confidence:.2f}")

    print("\n" + "=" * 60)
    print("TEST: Thesis generation (stub mode)")
    print("=" * 60)
    generator = ThesisGenerator()

    async def test_thesis():
        thesis = await generator.generate(oil_anomaly, opportunities)
        if thesis:
            print(f"Trigger: {thesis.trigger_explanation}")
            print(f"Confidence: {thesis.overall_confidence:.2f}")
            print(f"Horizon: {thesis.time_horizon_hours}hr")
            print("Top opportunities:")
            for opp in thesis.priority_opportunities[:3]:
                print(f"  {opp['symbol']:10} {opp['direction']} | {opp['confidence']:.2f} | {opp['rationale'][:50]}")
            print(f"Invalidation: {thesis.invalidation_conditions[0]}")

    asyncio.run(test_thesis())

    print("\n" + "=" * 60)
    print("TEST: Geopolitical escalation")
    print("=" * 60)
    geo_anomaly = AnomalySignal(
        symbol="geopolitical_conflict",
        asset_class="macro_event",
        timestamp="2026-04-06T06:00:00Z",
        price_move_pct=0.0,
        price_z_score=2.2,
        volume_ratio=1.0,
        volume_z_score=0.0,
        news_velocity=8.0,
        news_sentiment=-0.9,
        anomaly_score=0.85,
        direction="escalation",
    )
    opps = graph.get_opportunities(geo_anomaly)
    for o in opps:
        print(f"  {o.symbol:10} | {o.direction:4} | confidence={o.blended_confidence:.2f} | {o.thesis[:50]}")

    print("\nAll discovery tests complete.")


if __name__ == "__main__":
    main()
