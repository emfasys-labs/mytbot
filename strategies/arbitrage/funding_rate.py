from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Optional

from signals.engine import RawSignal
from strategies.arbitrage.models import FundingArbOpportunity, FundingArbSignal


def funding_arb_signal_to_raw(sig: FundingArbSignal) -> RawSignal:
    """Map structured funding arb output into the unified signal engine input."""
    md = {
        **sig.metadata,
        "perp_venue": sig.perp_venue,
        "spot_venue": sig.spot_venue,
        "funding_rate": str(sig.funding_rate),
        "basis_bps": str(sig.basis_bps),
        "annualised_net_yield": str(sig.annualised_net_yield),
        "expected_hold_hours": sig.expected_hold_hours,
        "expected_funding_events": sig.expected_funding_events,
        "arbitrage_kind": "funding_rate",
    }
    return RawSignal(
        strategy=sig.strategy_name,
        symbol=sig.symbol,
        side=sig.side,
        confidence=float(sig.confidence),
        broker=sig.spot_venue,
        asset_class="crypto",
        metadata=md,
    )


class FundingRateArbitrageStrategy:
    STRATEGY_NAME = "funding_rate_arbitrage"

    def __init__(self, config: dict, venue_selector: Any, logger: Any | None = None) -> None:
        self._config = config
        self._selector = venue_selector
        self._logger = logger

    @property
    def enabled(self) -> bool:
        return bool(self._config.get("enabled", False))

    async def evaluate_symbol(self, symbol: str, target_notional: Decimal) -> Optional[FundingArbSignal]:
        if not self.enabled:
            return None
        result = await self._selector.find_best_funding_arbitrage(symbol, target_notional)
        if result is None:
            return None
        return self._to_signal(result.opportunity)

    def _to_signal(self, opp: FundingArbOpportunity) -> FundingArbSignal:
        return FundingArbSignal(
            strategy_name=self.STRATEGY_NAME,
            symbol=opp.symbol,
            side="ARBITRAGE_LONG_SPOT_SHORT_PERP",
            confidence=opp.confidence,
            created_at=datetime.now(timezone.utc),
            spot_venue=opp.spot_venue,
            perp_venue=opp.perp_venue,
            funding_rate=opp.funding_rate,
            basis_bps=opp.basis_bps,
            annualised_net_yield=opp.annualised_net_yield,
            expected_hold_hours=opp.expected_hold_hours,
            expected_funding_events=opp.expected_funding_events,
            metadata={
                "direction": opp.direction,
                "gross_funding_per_event": str(opp.gross_funding_per_event),
                "estimated_total_cost": str(opp.estimated_total_cost),
                "annualised_gross_yield": str(opp.annualised_gross_yield),
                "spot_mid": str(opp.snapshot.spot_mid),
                "perp_mark": str(opp.snapshot.perp_mark),
            },
        )
