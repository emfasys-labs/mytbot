"""Runtime dataclasses for opportunity scoring, regime state, allocation, and execution planning."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Literal

Side = Literal["long", "short"]
AssetClass = Literal["equity", "etf", "bond", "forex", "crypto", "future", "option", "other"]
RegimeLabel = Literal[
    "trend_up",
    "trend_down",
    "range",
    "volatile",
    "crash",
    "panic",
    "risk_on",
    "risk_off",
    "mixed",
    "unknown",
    "insufficient_data",
]
ProfileMode = Literal["defender", "trader", "hunter"]
ReplacementActionType = Literal["open", "close", "increase", "reduce", "replace", "hold", "skip"]


def clip_decimal(value: Decimal, low: Decimal, high: Decimal) -> Decimal:
    return min(max(value, low), high)


# ---------------------------------------------------------------------------
# Signal-side models
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class OpportunityComponents:
    momentum: Decimal = Decimal("0")
    volume_anomaly: Decimal = Decimal("0")
    news_impact: Decimal = Decimal("0")
    regime_alignment: Decimal = Decimal("0")
    liquidity_quality: Decimal = Decimal("0")
    structure_quality: Decimal = Decimal("0")
    relative_strength: Decimal = Decimal("0")

    def as_dict(self) -> dict[str, Decimal]:
        return {
            "momentum": self.momentum,
            "volume_anomaly": self.volume_anomaly,
            "news_impact": self.news_impact,
            "regime_alignment": self.regime_alignment,
            "liquidity_quality": self.liquidity_quality,
            "structure_quality": self.structure_quality,
            "relative_strength": self.relative_strength,
        }


@dataclass(slots=True)
class VolumeAnomalyFeatures:
    """
    First-class volume / flow inputs for D015. Missing venue data stays at zero;
    callers should not fabricate values.
    """

    volume_z: Decimal = Decimal("0")
    relative_dollar_volume: Decimal = Decimal("0")
    trade_count_anomaly: Decimal = Decimal("0")
    orderbook_imbalance: Decimal = Decimal("0")
    volume_persistence: Decimal = Decimal("0")
    fake_spike_penalty: Decimal = Decimal("0")


@dataclass(slots=True)
class VolumeAnomalyDetectionResult:
    """
    Detection output only: anomaly observed → refresh context / classify / score.
    Does not imply buy/sell; the allocator + risk engine decide reaction.
    """

    features: VolumeAnomalyFeatures
    refresh_context_recommended: bool
    detection_strength: Decimal
    metadata: dict[str, str | int | float | bool] = field(default_factory=dict)


@dataclass(slots=True)
class Opportunity:
    symbol: str
    asset_class: AssetClass
    side: Side
    timestamp: datetime

    opportunity_score: Decimal
    urgency_score: Decimal
    confidence: Decimal

    expected_return: Decimal = Decimal("0")
    expected_holding_minutes: int = 0

    price: Decimal = Decimal("0")
    volatility: Decimal = Decimal("0")
    spread_bps: Decimal = Decimal("0")
    slippage_bps_estimate: Decimal = Decimal("0")

    components: OpportunityComponents = field(default_factory=OpportunityComponents)
    volume_flow: VolumeAnomalyDetectionResult | None = None
    tags: list[str] = field(default_factory=list)
    metadata: dict[str, str | int | float | bool] = field(default_factory=dict)


@dataclass(slots=True)
class SignalCandidate:
    symbol: str
    asset_class: AssetClass
    side: Side
    timestamp: datetime

    raw_signal_strength: Decimal
    adjusted_signal_strength: Decimal
    confidence: Decimal

    strategy_name: str
    strategy_version: str | None = None

    opportunity: Opportunity | None = None
    rationale: str | None = None
    metadata: dict[str, str | int | float | bool] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Regime / market state
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class MarketStateComponents:
    trend_strength: Decimal = Decimal("0")
    cross_asset_confirmation: Decimal = Decimal("0")
    liquidity_state: Decimal = Decimal("0")
    macro_clarity: Decimal = Decimal("0")
    risk_on_breadth: Decimal = Decimal("0")
    chaos_penalty: Decimal = Decimal("0")
    correlation_crowding: Decimal = Decimal("0")
    volatility_structure: Decimal = Decimal("0")
    anomaly_breadth: Decimal = Decimal("0")
    news_conflict_score: Decimal = Decimal("0")

    def as_dict(self) -> dict[str, Decimal]:
        return {
            "trend_strength": self.trend_strength,
            "cross_asset_confirmation": self.cross_asset_confirmation,
            "liquidity_state": self.liquidity_state,
            "macro_clarity": self.macro_clarity,
            "risk_on_breadth": self.risk_on_breadth,
            "chaos_penalty": self.chaos_penalty,
            "correlation_crowding": self.correlation_crowding,
            "volatility_structure": self.volatility_structure,
            "anomaly_breadth": self.anomaly_breadth,
            "news_conflict_score": self.news_conflict_score,
        }


@dataclass(slots=True)
class RegimeState:
    timestamp: datetime
    regime_label: RegimeLabel

    market_state_score: Decimal
    drawdown_throttle: Decimal
    execution_quality: Decimal
    breadth_score: Decimal

    components: MarketStateComponents = field(default_factory=MarketStateComponents)
    metadata: dict[str, str | int | float | bool] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Portfolio / held positions
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class HeldPositionState:
    symbol: str
    asset_class: AssetClass
    side: Side

    quantity: Decimal
    entry_price: Decimal
    current_price: Decimal
    market_value: Decimal
    notional_exposure: Decimal

    unrealised_pnl: Decimal
    unrealised_pnl_pct: Decimal

    leverage_used: Decimal = Decimal("1")
    margin_used: Decimal = Decimal("0")

    opened_at: datetime | None = None
    last_updated_at: datetime | None = None

    strategy_name: str | None = None
    strategy_version: str | None = None

    current_opportunity_score: Decimal = Decimal("0")
    hold_score: Decimal = Decimal("0")
    exit_pressure: Decimal = Decimal("0")
    opportunity_cost: Decimal = Decimal("0")

    tags: list[str] = field(default_factory=list)
    metadata: dict[str, str | int | float | bool] = field(default_factory=dict)


@dataclass(slots=True)
class PortfolioState:
    timestamp: datetime
    mode: ProfileMode

    nav: Decimal
    cash: Decimal
    available_buying_power: Decimal
    gross_exposure: Decimal
    net_exposure: Decimal
    leverage_ratio: Decimal

    realised_pnl_today: Decimal = Decimal("0")
    unrealised_pnl_total: Decimal = Decimal("0")
    drawdown_from_hwm_pct: Decimal = Decimal("0")
    loss_streak: int = 0

    positions: list[HeldPositionState] = field(default_factory=list)
    metadata: dict[str, str | int | float | bool] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Allocation / replacement
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class ReplacementCandidate:
    new_symbol: str
    old_symbol: str

    new_opportunity_score: Decimal
    old_hold_score: Decimal
    switching_cost_score: Decimal
    replacement_advantage: Decimal

    recommended_action: ReplacementActionType
    reason: str
    metadata: dict[str, str | int | float | bool] = field(default_factory=dict)


@dataclass(slots=True)
class AllocationTarget:
    symbol: str
    target_weight: Decimal
    target_notional: Decimal
    target_leverage: Decimal
    side: Side

    source_opportunity_score: Decimal
    priority_rank: int

    strategy_name: str | None = None
    metadata: dict[str, str | int | float | bool] = field(default_factory=dict)


@dataclass(slots=True)
class AllocationDecision:
    timestamp: datetime
    mode: ProfileMode

    gross_exposure_target: Decimal
    net_exposure_target: Decimal
    capital_deployment_target: Decimal

    allocation_targets: list[AllocationTarget] = field(default_factory=list)
    open_symbols: list[str] = field(default_factory=list)
    close_symbols: list[str] = field(default_factory=list)
    increase_symbols: list[str] = field(default_factory=list)
    reduce_symbols: list[str] = field(default_factory=list)
    hold_symbols: list[str] = field(default_factory=list)

    replacement_candidates: list[ReplacementCandidate] = field(default_factory=list)

    rationale: str | None = None
    metadata: dict[str, str | int | float | bool] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Execution planning (allocation → orders)
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class ExecutionInstruction:
    symbol: str
    action: ReplacementActionType
    side: Side

    target_notional: Decimal
    target_quantity: Decimal | None = None
    target_weight: Decimal | None = None
    max_slippage_bps: Decimal = Decimal("0")
    urgency_score: Decimal = Decimal("0")

    reduce_only: bool = False
    close_only: bool = False

    reason: str | None = None
    metadata: dict[str, str | int | float | bool] = field(default_factory=dict)


@dataclass(slots=True)
class ExecutionPlan:
    timestamp: datetime
    mode: ProfileMode

    instructions: list[ExecutionInstruction] = field(default_factory=list)
    estimated_turnover: Decimal = Decimal("0")
    estimated_cost_bps: Decimal = Decimal("0")
    rationale: str | None = None
    metadata: dict[str, str | int | float | bool] = field(default_factory=dict)


@dataclass(slots=True)
class ScoreBundle:
    raw: Decimal
    adjusted: Decimal
    confidence: Decimal

    def clipped(self, low: Decimal = Decimal("-1"), high: Decimal = Decimal("1")) -> ScoreBundle:
        clipped_adjusted = min(max(self.adjusted, low), high)
        return ScoreBundle(raw=self.raw, adjusted=clipped_adjusted, confidence=self.confidence)
