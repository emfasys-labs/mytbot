from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Optional

from data.capability_registry import BrokerCapabilities, CapabilityRegistry
from strategies.arbitrage.calculator import (
    compute_annualised_gross_yield,
    compute_annualised_net_yield,
    compute_basis_bps,
    compute_break_even_funding_rate,
    compute_gross_funding_per_event,
    compute_total_cost,
)
from strategies.arbitrage.models import FundingArbOpportunity, FundingRateSnapshot


@dataclass
class VenueSelectionResult:
    opportunity: FundingArbOpportunity
    score: Decimal


class VenueSelector:
    """
    Enumerates (spot, perp) capability pairs and picks the best funding-carry opportunity.
    Broker-agnostic: venues come from ``CapabilityRegistry`` + live snapshots from the data provider.
    """

    def __init__(
        self,
        capability_registry: CapabilityRegistry,
        funding_data_provider: Any,
        logger: Any | None,
        funding_cfg: dict,
        *,
        latency_predictor: Any | None = None,
    ) -> None:
        self._registry = capability_registry
        self._data = funding_data_provider
        self._logger = logger
        self._cfg = funding_cfg
        self._latency_predictor = latency_predictor

    async def find_best_funding_arbitrage(
        self,
        symbol: str,
        notional: Decimal,
    ) -> Optional[VenueSelectionResult]:
        spot_candidates = self._registry.get_spot_brokers(symbol)
        perp_candidates = self._registry.get_perp_brokers(symbol)
        min_liq = Decimal(str(self._cfg.get("min_liquidity_score", "0")))
        max_lat = int(self._cfg.get("max_latency_ms_pair_sum", 10_000))
        spot_candidates = CapabilityRegistry.filter_by_liquidity(spot_candidates, min_liq)
        perp_candidates = CapabilityRegistry.filter_by_liquidity(perp_candidates, min_liq)

        if not spot_candidates or not perp_candidates:
            return None

        best_result: VenueSelectionResult | None = None

        for spot in spot_candidates:
            for perp in perp_candidates:
                if spot.name == perp.name:
                    continue
                if spot.latency_ms + perp.latency_ms > max_lat:
                    continue

                snapshot = await self._data.get_snapshot(
                    symbol=symbol,
                    spot_venue=spot.name,
                    perp_venue=perp.name,
                )
                if snapshot is None:
                    continue

                opportunity = self._evaluate(snapshot, notional, spot, perp)
                if opportunity is None:
                    continue

                score = self._score(opportunity, spot, perp)
                if best_result is None or score > best_result.score:
                    best_result = VenueSelectionResult(opportunity=opportunity, score=score)

        return best_result

    def _evaluate(
        self,
        snapshot: FundingRateSnapshot,
        notional: Decimal,
        spot: BrokerCapabilities,
        perp: BrokerCapabilities,
    ) -> FundingArbOpportunity | None:
        cfg = self._cfg
        min_funding = Decimal(str(cfg["min_funding_rate"]))
        min_net_yield = Decimal(str(cfg["min_annualised_net_yield"]))
        max_basis = Decimal(str(cfg["max_basis_bps"]))
        expected_events = int(cfg["expected_funding_events_min"])

        funding_rate = snapshot.funding_rate
        if funding_rate <= min_funding:
            return None

        if getattr(snapshot, "liquidity_unstable", False):
            return None
        min_abs_imb = Decimal(str(cfg.get("min_abs_spot_imbalance", "0")))
        if min_abs_imb > 0 and abs(snapshot.spot_imbalance) < min_abs_imb:
            return None

        basis_bps = compute_basis_bps(snapshot.perp_mark, snapshot.spot_mid)
        if abs(basis_bps) > max_basis:
            return None

        gross = compute_gross_funding_per_event(notional, funding_rate)
        annualised_gross = compute_annualised_gross_yield(funding_rate, snapshot.funding_interval_hours)

        total_cost = compute_total_cost(
            notional,
            fee_buffer_bps=Decimal(str(cfg["fee_buffer_bps"])),
            slippage_buffer_bps=Decimal(str(cfg["slippage_buffer_bps"])),
        )

        break_even = compute_break_even_funding_rate(total_cost, notional, expected_events)
        if funding_rate <= break_even:
            return None

        annualised_net = compute_annualised_net_yield(
            gross,
            total_cost,
            notional,
            expected_events,
            snapshot.funding_interval_hours,
        )
        if annualised_net <= min_net_yield:
            return None

        hold_cap = int(cfg.get("max_hold_hours", 120))
        expected_hold = min(hold_cap, snapshot.funding_interval_hours * expected_events)

        conf = min(Decimal("0.95"), Decimal("0.50") + (annualised_net * Decimal("2")))

        return FundingArbOpportunity(
            symbol=snapshot.symbol,
            direction="long_spot_short_perp",
            spot_venue=snapshot.spot_venue,
            perp_venue=snapshot.perp_venue,
            funding_rate=snapshot.funding_rate,
            gross_funding_per_event=gross,
            estimated_open_fees=Decimal("0"),
            estimated_close_fees=Decimal("0"),
            estimated_slippage=Decimal("0"),
            estimated_total_cost=total_cost,
            basis_bps=basis_bps,
            annualised_gross_yield=annualised_gross,
            annualised_net_yield=annualised_net,
            expected_hold_hours=expected_hold,
            expected_funding_events=expected_events,
            confidence=conf,
            snapshot=snapshot,
        )

    def _score(self, opportunity: FundingArbOpportunity, spot: BrokerCapabilities, perp: BrokerCapabilities) -> Decimal:
        yield_score = opportunity.annualised_net_yield
        liquidity_score = min(spot.liquidity_score, perp.liquidity_score)

        lat_s = float(spot.latency_ms)
        lat_p = float(perp.latency_ms)
        if self._latency_predictor is not None:
            try:
                lat_s = float(self._latency_predictor.get_average(spot.name))
                lat_p = float(self._latency_predictor.get_average(perp.name))
            except Exception:  # noqa: BLE001
                pass

        latency_penalty = Decimal(str(lat_s + lat_p)) / Decimal("1000")
        fee_penalty = spot.fee_bps + perp.fee_bps

        degrades = Decimal("1")
        if self._latency_predictor is not None:
            try:
                if self._latency_predictor.is_degrading(spot.name) or self._latency_predictor.is_degrading(perp.name):
                    degrades = Decimal("0.7")
            except Exception:  # noqa: BLE001
                pass

        imb = getattr(opportunity.snapshot, "spot_imbalance", Decimal("0"))
        micro_mult = (Decimal("1") + imb * Decimal("0.08"))
        if micro_mult < Decimal("0.6"):
            micro_mult = Decimal("0.6")
        if micro_mult > Decimal("1.15"):
            micro_mult = Decimal("1.15")

        score = (
            yield_score * liquidity_score * degrades * micro_mult
            - latency_penalty * Decimal("0.01")
            - fee_penalty / Decimal("10000")
        )
        return score
