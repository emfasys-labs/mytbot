"""Broker-agnostic arbitrage strategies (funding carry, cross-venue spot spread).

Detection and scoring live here; execution uses ``execution/arbitrage_executor.py`` and
``execution/arbitrage_spot_executor.py``. Risk remains in ``risk/engine.py`` + ``risk/arbitrage_checks.py``.
"""

from strategies.arbitrage.calculator import (
    BPS_DENOMINATOR,
    HOURS_PER_YEAR,
    compute_annualised_gross_yield,
    compute_annualised_net_yield,
    compute_basis_bps,
    compute_break_even_funding_rate,
    compute_gross_funding_per_event,
    compute_total_cost,
    quantize_money,
)
from strategies.arbitrage.cross_exchange import CrossExchangeArbitrageStrategy, cross_exchange_signal_to_raw
from strategies.arbitrage.funding_rate import FundingRateArbitrageStrategy, funding_arb_signal_to_raw
from strategies.arbitrage.models import (
    CrossExchangeOpportunity,
    FundingArbOpportunity,
    FundingArbSignal,
    FundingRateSnapshot,
    PairedArbPosition,
)

__all__ = [
    "BPS_DENOMINATOR",
    "HOURS_PER_YEAR",
    "CrossExchangeArbitrageStrategy",
    "cross_exchange_signal_to_raw",
    "CrossExchangeOpportunity",
    "FundingArbOpportunity",
    "FundingArbSignal",
    "FundingRateArbitrageStrategy",
    "FundingRateSnapshot",
    "PairedArbPosition",
    "compute_annualised_gross_yield",
    "compute_annualised_net_yield",
    "compute_basis_bps",
    "compute_break_even_funding_rate",
    "compute_gross_funding_per_event",
    "compute_total_cost",
    "funding_arb_signal_to_raw",
    "quantize_money",
]
