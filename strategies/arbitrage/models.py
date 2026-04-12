from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any, Optional


@dataclass(frozen=True)
class FundingRateSnapshot:
    symbol: str
    perp_venue: str
    spot_venue: str
    funding_rate: Decimal
    funding_interval_hours: int
    next_funding_time: datetime

    perp_bid: Decimal
    perp_ask: Decimal
    perp_mark: Decimal

    spot_bid: Decimal
    spot_ask: Decimal
    spot_mid: Decimal

    timestamp: datetime

    # Microstructure (optional; default neutral for backward compatibility)
    spot_imbalance: Decimal = Decimal("0")
    liquidity_unstable: bool = False


@dataclass(frozen=True)
class FundingArbOpportunity:
    symbol: str
    direction: str
    spot_venue: str
    perp_venue: str

    funding_rate: Decimal
    gross_funding_per_event: Decimal
    estimated_open_fees: Decimal
    estimated_close_fees: Decimal
    estimated_slippage: Decimal
    estimated_total_cost: Decimal

    basis_bps: Decimal
    annualised_gross_yield: Decimal
    annualised_net_yield: Decimal

    expected_hold_hours: int
    expected_funding_events: int
    confidence: Decimal

    snapshot: FundingRateSnapshot


@dataclass(frozen=True)
class FundingArbSignal:
    strategy_name: str
    symbol: str
    side: str
    confidence: Decimal
    created_at: datetime

    spot_venue: str
    perp_venue: str

    funding_rate: Decimal
    basis_bps: Decimal
    annualised_net_yield: Decimal
    expected_hold_hours: int
    expected_funding_events: int

    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class PairedArbPosition:
    pair_id: str
    symbol: str
    spot_venue: str
    perp_venue: str

    spot_order_id: Optional[str] = None
    perp_order_id: Optional[str] = None

    spot_quantity: Decimal = Decimal("0")
    perp_quantity: Decimal = Decimal("0")

    opened_at: Optional[datetime] = None
    status: str = "pending"

    entry_spot_price: Optional[Decimal] = None
    entry_perp_price: Optional[Decimal] = None

    latest_funding_rate: Optional[Decimal] = None
    cumulative_funding_pnl: Decimal = Decimal("0")
    realised_pnl: Decimal = Decimal("0")


@dataclass(frozen=True)
class CrossExchangeOpportunity:
    symbol: str

    buy_venue: str
    sell_venue: str

    buy_price: Decimal
    sell_price: Decimal

    spread_bps: Decimal
    gross_spread: Decimal

    estimated_fees: Decimal
    estimated_slippage: Decimal
    net_spread: Decimal

    notional: Decimal
    confidence: Decimal
