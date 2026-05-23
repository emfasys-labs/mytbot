"""Pydantic schemas for allocation and profile-mode YAML configs.

Formula fields are documentation only; implement logic in Python (see DECISIONS D015).
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

ModeName = Literal["defender", "trader", "hunter"]
ModeSelectionSource = Literal["ui", "api", "config", "adaptive_classifier"]
AllocatorMode = Literal["global_opportunity_replacement", "static_sleeves"]
RebalanceTrigger = Literal["continuous", "interval", "hybrid"]
NormalisationMethod = Literal["bounded_sigmoid", "tanh", "zscore_clip"]
SaturatingFn = Literal["tanh", "bounded_sigmoid"]


class StrictBaseModel(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


# ---------------------------------------------------------------------------
# profile_modes.yaml
# ---------------------------------------------------------------------------


class CoefficientWithDynamic(StrictBaseModel):
    """base + optional dynamic term weights (keys match allocation engine inputs)."""

    base: float = Field(..., description="Base coefficient before dynamic adjustments.")
    dynamic: dict[str, float] = Field(default_factory=dict)


class ModeCoefficientsConfig(StrictBaseModel):
    aggression_multiplier: CoefficientWithDynamic
    concentration_exponent: CoefficientWithDynamic
    replacement_sensitivity: CoefficientWithDynamic
    leverage_base: CoefficientWithDynamic
    volume_anomaly_weight: CoefficientWithDynamic
    liquidity_penalty_weight: CoefficientWithDynamic
    chaos_penalty_weight: CoefficientWithDynamic
    profit_lock_bias: CoefficientWithDynamic
    drawdown_throttle_strength: CoefficientWithDynamic


class ProfileModeConfig(StrictBaseModel):
    description: str
    coefficients: ModeCoefficientsConfig


class ProfileDefaultsConfig(StrictBaseModel):
    enabled: bool = True
    active_mode: ModeName = "trader"
    mode_selection_source: ModeSelectionSource = "ui"
    allow_runtime_switch: bool = True


class ProfileSafetyBoundsConfig(StrictBaseModel):
    absolute_max_gross_exposure: dict[str, float]
    absolute_max_single_position_weight: dict[str, float]
    absolute_max_portfolio_leverage: dict[str, float]
    absolute_max_replacement_turnover_per_cycle: dict[str, float]

    @model_validator(mode="after")
    def _validate_mode_keys(self) -> ProfileSafetyBoundsConfig:
        required = {"defender", "trader", "hunter"}
        for name, d in [
            ("absolute_max_gross_exposure", self.absolute_max_gross_exposure),
            ("absolute_max_single_position_weight", self.absolute_max_single_position_weight),
            ("absolute_max_portfolio_leverage", self.absolute_max_portfolio_leverage),
            (
                "absolute_max_replacement_turnover_per_cycle",
                self.absolute_max_replacement_turnover_per_cycle,
            ),
        ]:
            keys = set(d.keys())
            if keys != required:
                raise ValueError(f"{name} must have exactly keys {sorted(required)}, got {sorted(keys)}")
        return self


class ProfileModesConfig(StrictBaseModel):
    version: int = 1
    defaults: ProfileDefaultsConfig
    modes: dict[str, ProfileModeConfig]
    safety_bounds: ProfileSafetyBoundsConfig
    # Per-mode trading-loop cadence (seconds). Consumed by TradingLoop to
    # speed up/slow down iteration tempo based on the active profile mode.
    loop_cadence_sec: dict[str, int] = Field(default_factory=dict)
    notes: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_modes_present(self) -> ProfileModesConfig:
        required = {"defender", "trader", "hunter"}
        actual = set(self.modes.keys())
        missing = required - actual
        if missing:
            raise ValueError(f"profile modes missing required entries: {sorted(missing)}")
        return self


# ---------------------------------------------------------------------------
# allocation.yaml
# ---------------------------------------------------------------------------


class AllocatorConfig(StrictBaseModel):
    enabled: bool = True
    mode: AllocatorMode = "global_opportunity_replacement"
    rebalance_trigger: RebalanceTrigger = "continuous"
    evaluation_interval_seconds: int = 5
    min_replacement_interval_seconds: int = 2


class MomentumSubcomponentsConfig(StrictBaseModel):
    z_return_5m: float
    z_return_1h: float
    breakout_strength: float
    trend_slope: float
    trend_persistence: float


class VolumeAnomalySubcomponentsConfig(StrictBaseModel):
    volume_z: float
    relative_dollar_volume: float
    trade_count_anomaly: float
    orderbook_imbalance: float


class VolumeAnomalyTransformsConfig(StrictBaseModel):
    saturating_function: SaturatingFn = "tanh"
    persistence_boost_enabled: bool = True
    fake_spike_penalty_enabled: bool = True


class FormulaTermWeightsConfig(StrictBaseModel):
    sentiment: float = 1.0
    credibility: float = 1.0
    materiality: float = 1.0
    freshness: float = 1.0


class LiquidityPenaltyConfig(StrictBaseModel):
    spread: float
    slippage_estimate: float
    depth_fragility: float


class WeightedComponentConfig(StrictBaseModel):
    enabled: bool = True
    weight: float


class MomentumComponentConfig(WeightedComponentConfig):
    subcomponents: MomentumSubcomponentsConfig


class VolumeAnomalyComponentConfig(WeightedComponentConfig):
    subcomponents: VolumeAnomalySubcomponentsConfig
    transforms: VolumeAnomalyTransformsConfig


class NewsImpactComponentConfig(WeightedComponentConfig):
    formula: FormulaTermWeightsConfig


class LiquidityQualityComponentConfig(WeightedComponentConfig):
    penalties: LiquidityPenaltyConfig


class OpportunityComponentsConfig(StrictBaseModel):
    momentum: MomentumComponentConfig
    volume_anomaly: VolumeAnomalyComponentConfig
    news_impact: NewsImpactComponentConfig
    regime_alignment: WeightedComponentConfig
    liquidity_quality: LiquidityQualityComponentConfig
    structure_quality: WeightedComponentConfig
    relative_strength: WeightedComponentConfig


class UrgencyScoringConfig(StrictBaseModel):
    """Weights for opportunity urgency_score (all from config, no literals in code)."""

    base: float = 0.2
    volume_detection: float = 0.6
    confidence: float = 0.15
    escalation_multiplier: float = 1.08


class OpportunityScoringConfig(StrictBaseModel):
    normalisation: NormalisationMethod = "bounded_sigmoid"
    score_range: tuple[float, float] = (-1.0, 1.0)
    volume_escalation_strength_threshold: float = 0.72
    urgency: UrgencyScoringConfig = Field(default_factory=UrgencyScoringConfig)

    @model_validator(mode="after")
    def validate_score_range(self) -> OpportunityScoringConfig:
        lo, hi = self.score_range
        if lo >= hi:
            raise ValueError("score_range lower bound must be < upper bound")
        return self


class OpportunityEngineConfig(StrictBaseModel):
    scoring: OpportunityScoringConfig
    components: OpportunityComponentsConfig


class MarketStateComponentsConfig(StrictBaseModel):
    trend_strength: float
    cross_asset_confirmation: float
    liquidity_state: float
    macro_clarity: float
    risk_on_breadth: float
    chaos_penalty: float
    correlation_crowding: float
    volatility_structure: float
    anomaly_breadth: float
    news_conflict_score: float


class RegimeLiquidityEnrichmentConfig(StrictBaseModel):
    """Blend broker book proxy into cross-section liquidity when available."""

    broker_depth_weight: float = 0.0
    feature_proxy_weight: float = 1.0


class MarketStateConfig(StrictBaseModel):
    enabled: bool = True
    components: MarketStateComponentsConfig
    min_symbols_for_regime: int = 3
    anomaly_volume_z_threshold: float = 1.25
    anomaly_rel_dv_threshold: float = 0.45
    news_lookback_hours: int = 48
    liquidity_enrichment: RegimeLiquidityEnrichmentConfig = Field(
        default_factory=RegimeLiquidityEnrichmentConfig
    )


class GrossExposureControlsConfig(StrictBaseModel):
    use_breadth_score: bool = True
    use_drawdown_throttle: bool = True
    use_execution_quality: bool = True


class GrossExposureShapingConfig(StrictBaseModel):
    """Coefficients for gross exposure sigmoid (see portfolio/allocation_engine.py)."""

    market_state_weight: float = 0.35
    breadth_weight: float = 0.35
    aggregate_scale: float = 1.8
    sigmoid_clip_min: float = -3.0
    sigmoid_clip_max: float = 3.0


class UnleashConfig(StrictBaseModel):
    """Optional deployment-pressure relaxation for opportunity shaping only."""

    enabled: bool = True
    start_capital_pct: float = 0.90
    full_capital_pct: float = 1.00
    max_shape_relaxation: float = 0.50


class GrossExposureConfig(StrictBaseModel):
    formula: str
    controls: GrossExposureControlsConfig
    shaping: GrossExposureShapingConfig = Field(default_factory=GrossExposureShapingConfig)
    unleash: UnleashConfig = Field(default_factory=UnleashConfig)


class PositionWeightPostAdjustmentsConfig(StrictBaseModel):
    liquidity_quality_multiplier: bool = True
    regime_alignment_multiplier: bool = True
    execution_quality_multiplier: bool = True


class PositionWeightsConfig(StrictBaseModel):
    formula: str
    lambda_: float = Field(1.0, alias="lambda")
    post_adjustments: PositionWeightPostAdjustmentsConfig


class SwitchingCostConfig(StrictBaseModel):
    fee_weight: float = 1.0
    spread_weight: float = 1.0
    slippage_weight: float = 1.0
    urgency_discount_weight: float = 0.5


class SwitchingCostNormalisationConfig(StrictBaseModel):
    """Map raw bps to 0..1 penalty in portfolio/d015_hold_switch.py."""

    net_bps_max: float = 2000.0
    penalty_divisor_bps: float = 400.0
    urgency_discount_bps_scale: float = 15.0
    fee_bps_clip_min: float = 1.0
    fee_bps_clip_max: float = 500.0
    spread_slippage_bps_clip_max: float = 500.0


class ReplacementThresholdsConfig(StrictBaseModel):
    minimum_replacement_advantage: float = 0.05
    emergency_override_on_extreme_opportunity: bool = True
    extreme_opportunity_threshold: float = 0.90


class ReplacementActionsConfig(StrictBaseModel):
    allow_reduce_existing: bool = True
    allow_close_existing: bool = True
    allow_close_small_winner: bool = True
    allow_close_flat_position: bool = True
    allow_close_small_loser_for_better_opportunity: bool = True


class ReplacementChurnConfig(StrictBaseModel):
    enabled: bool = True
    penalty_per_recent_event: float = 0.02
    max_recent_events: int = 10


class ReplacementLogicConfig(StrictBaseModel):
    enabled: bool = True
    formula: str
    actions: ReplacementActionsConfig
    switching_cost: SwitchingCostConfig
    switching_cost_normalisation: SwitchingCostNormalisationConfig = Field(
        default_factory=SwitchingCostNormalisationConfig
    )
    thresholds: ReplacementThresholdsConfig
    churn: ReplacementChurnConfig = Field(default_factory=ReplacementChurnConfig)


class HoldScoreComponentsConfig(StrictBaseModel):
    current_opportunity_score: float
    unrealised_pnl_quality: float
    trend_continuation: float
    negative_exit_pressure: float
    negative_opportunity_cost: float


class HoldScoreNeutralDefaultsConfig(StrictBaseModel):
    unrealised_pnl_offset: float = 0.2
    trend_continuation_default: float = 0.5


class HoldScoreConfig(StrictBaseModel):
    enabled: bool = True
    components: HoldScoreComponentsConfig
    neutral_defaults: HoldScoreNeutralDefaultsConfig = Field(default_factory=HoldScoreNeutralDefaultsConfig)


class ExitPressureComponentsConfig(StrictBaseModel):
    trend_reversal: float
    volume_fade: float
    news_deterioration: float
    volatility_shock: float
    relative_weakening: float
    opportunity_cost: float


class ExitPressureConfig(StrictBaseModel):
    enabled: bool = True
    components: ExitPressureComponentsConfig


class LeverageControlsConfig(StrictBaseModel):
    allow_long_leverage: bool = True
    allow_short_leverage: bool = True
    require_borrow_or_venue_support_for_shorts: bool = True


class LeverageConfig(StrictBaseModel):
    enabled: bool = True
    formula: str
    controls: LeverageControlsConfig


class ShortsScoreComponentsConfig(StrictBaseModel):
    down_momentum: float
    negative_volume_anomaly: float
    negative_news_impact: float
    regime_alignment: float
    liquidity_quality: float


class ShortsExtraRequirementsConfig(StrictBaseModel):
    borrow_available: bool = True
    squeeze_risk_check: bool = True
    stronger_confirmation_than_longs: bool = True


class ShortsConfig(StrictBaseModel):
    enabled: bool = True
    score_components: ShortsScoreComponentsConfig
    extra_requirements: ShortsExtraRequirementsConfig


class PositionCountConfig(StrictBaseModel):
    hard_limit_enabled: bool = False
    notes: list[str] = Field(default_factory=list)


class CapitalReuseConfig(StrictBaseModel):
    enabled: bool = True
    require_free_cash_to_open_new_position: bool = False
    notes: list[str] = Field(default_factory=list)


class StrategyAttributionConfig(StrictBaseModel):
    enabled: bool = True
    use_for_reporting_only: bool = True
    hard_strategy_sleeves_enabled: bool = False


class EmergencyKillConfig(StrictBaseModel):
    broker_disconnect: bool = True
    stale_market_data: bool = True
    margin_danger: bool = True
    order_state_incoherence: bool = True
    abnormal_equity_jump: bool = True


class EmergencyBoundsConfig(StrictBaseModel):
    max_daily_drawdown_pct: dict[str, float]
    max_rolling_drawdown_pct: dict[str, float]
    max_execution_slippage_pct: dict[str, float]

    @model_validator(mode="after")
    def _validate_mode_keys(self) -> EmergencyBoundsConfig:
        required = {"defender", "trader", "hunter"}
        for name, d in [
            ("max_daily_drawdown_pct", self.max_daily_drawdown_pct),
            ("max_rolling_drawdown_pct", self.max_rolling_drawdown_pct),
            ("max_execution_slippage_pct", self.max_execution_slippage_pct),
        ]:
            keys = set(d.keys())
            if keys != required:
                raise ValueError(f"{name} must have exactly keys {sorted(required)}, got {sorted(keys)}")
        return self


class SafetyConfig(StrictBaseModel):
    enabled: bool = True
    notes: list[str] = Field(default_factory=list)
    emergency_kill: EmergencyKillConfig
    emergency_bounds: EmergencyBoundsConfig


class ExecutionPlanUrgencyConfig(StrictBaseModel):
    replace_close_old: float = 0.85
    replacement_open: float = 0.9
    allocation_close: float = 0.7
    allocation_reduce: float = 0.5
    allocation_open_or_increase: float = 0.55
    reduce_vs_target_tolerance: float = 1.05


class VolumeEscalationConfig(StrictBaseModel):
    enabled: bool = True
    command_type: str = "d015_volume_refresh"


class AllocationStabilityConfig(StrictBaseModel):
    """Soft damping vs prior cycle (see portfolio/d015_smoothing.py)."""

    enabled: bool = True
    gross_exposure_smoothing_alpha: float = 0.35
    turnover_damping_lambda: float = 0.5
    turnover_weight_sum_threshold: float = 0.4


class AllocationConfig(StrictBaseModel):
    version: int = 1
    allocator: AllocatorConfig
    opportunity_engine: OpportunityEngineConfig
    market_state: MarketStateConfig
    gross_exposure: GrossExposureConfig
    position_weights: PositionWeightsConfig
    replacement_logic: ReplacementLogicConfig
    hold_score: HoldScoreConfig
    exit_pressure: ExitPressureConfig
    leverage: LeverageConfig
    shorts: ShortsConfig
    position_count: PositionCountConfig
    capital_reuse: CapitalReuseConfig
    strategy_attribution: StrategyAttributionConfig
    safety: SafetyConfig
    execution_plan_urgency: ExecutionPlanUrgencyConfig = Field(default_factory=ExecutionPlanUrgencyConfig)
    volume_escalation: VolumeEscalationConfig = Field(default_factory=VolumeEscalationConfig)
    allocation_stability: AllocationStabilityConfig = Field(default_factory=AllocationStabilityConfig)
