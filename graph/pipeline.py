from __future__ import annotations

from dataclasses import dataclass

from loguru import logger


@dataclass
class DiscoveryCycleItem:
    anomaly: object
    opportunities: list
    thesis: object | None
    signals: list


class DiscoveryPipeline:
    def __init__(self, scanner, graph, thesis_generator, signal_engine):
        self.scanner = scanner
        self.graph = graph
        self.thesis_generator = thesis_generator
        self.signal_engine = signal_engine
        logger.info("DiscoveryPipeline initialised")

    async def run_cycle(self, portfolio_value, market_context=None):
        items = await self.run_cycle_detailed(portfolio_value, market_context=market_context)
        out = []
        for it in items:
            out.extend(it.signals)
        return out

    async def run_cycle_detailed(self, portfolio_value, market_context=None):
        anomalies = await self.scanner.scan()
        if not anomalies:
            return []
        items: list[DiscoveryCycleItem] = []
        for anomaly in anomalies[:10]:
            opportunities = self.graph.get_opportunities(anomaly)
            if not opportunities:
                continue
            thesis = await self.thesis_generator.generate(anomaly, opportunities, market_context)
            signals = []
            for opp in opportunities:
                raw = self._opportunity_to_signal(opp, thesis, anomaly)
                signal = self.signal_engine.process(raw, portfolio_value)
                if signal is not None:
                    signals.append(signal)
            items.append(DiscoveryCycleItem(anomaly=anomaly, opportunities=opportunities, thesis=thesis, signals=signals))
        return items

    def _opportunity_to_signal(self, opportunity, thesis, anomaly):
        from signals.engine import RawSignal

        return RawSignal(
            strategy="dependency_graph",
            symbol=opportunity.symbol,
            side="buy" if opportunity.direction == "up" else "sell",
            confidence=opportunity.blended_confidence,
            broker="ibkr",
            asset_class=opportunity.asset_class,
            metadata={
                "trigger": anomaly.symbol,
                "trigger_move": anomaly.price_move_pct,
                "trigger_z_score": anomaly.price_z_score,
                "mechanism": opportunity.thesis,
                "expected_lag_hours": opportunity.expected_lag_hours,
                "thesis": thesis.trigger_explanation if thesis else None,
                "discovery_method": "dependency_graph",
                "anomaly_score": anomaly.anomaly_score,
            },
        )
