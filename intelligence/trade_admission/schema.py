from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Any


class AdmissionAction(str, Enum):
    ALLOW = "allow"
    ALLOW_SMALLER = "allow_smaller"
    DEFER = "defer"
    REJECT = "reject"
    REQUIRE_MORE_EVIDENCE = "require_more_evidence"
    CLOSE_ONLY = "close_only"


@dataclass(frozen=True)
class AdmissionConfig:
    enabled: bool = True
    shadow_only: bool = True
    block_new_opens: bool = False
    allow_size_haircuts: bool = False
    diagnostics_since_hours: float = 24.0
    outcome_horizons_minutes: tuple[int, ...] = (60, 240, 1440)
    max_rows_per_cycle: int = 500
    # Calibrated-model controls. ``model_min_bucket_samples`` is the only
    # learning knob: a bucket with fewer matured outcomes than this abstains
    # (the policy then falls back to the heuristic). No market thresholds —
    # the decision boundary is derived from the observed outcome distribution.
    model_enabled: bool = True
    model_min_bucket_samples: int = 25
    model_refresh_minutes: int = 30
    model_lookback_days: int = 30


@dataclass(frozen=True)
class ModelScore:
    """Calibrated win-probability for a candidate's bucket.

    ``probability`` is the smoothed historical win-rate of like candidates;
    ``base_rate`` is the global smoothed win-rate; ``margin`` is the binomial
    standard error of the base rate (the distribution-derived band used to
    decide whether a bucket is *materially* below average). ``abstain`` is set
    when the bucket has too little evidence to trust.
    """

    probability: Decimal
    base_rate: Decimal
    margin: Decimal
    samples: int
    bucket: str
    abstain: bool


@dataclass(frozen=True)
class AdmissionCandidate:
    id: str
    timestamp: datetime
    loop_iteration: int | None
    symbol: str
    strategy: str
    side: str | None
    broker: str | None
    asset_class: str | None
    signal_id: str | None
    source_path: str
    suggested_notional: Decimal | None
    suggested_quantity: Decimal | None
    suggested_price: Decimal | None
    is_reduce_only: bool
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class AdmissionFeatures:
    values: dict[str, Any]
    coverage: Decimal


@dataclass(frozen=True)
class AdmissionDecision:
    action: AdmissionAction
    reason: str
    score: Decimal | None
    uncertainty: Decimal | None
    active_applied: bool = False
    size_multiplier: Decimal | None = None
    features: AdmissionFeatures | None = None
    model_probability: Decimal | None = None
    model_samples: int | None = None

