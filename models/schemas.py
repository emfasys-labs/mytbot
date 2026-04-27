"""
models/schemas.py
==================
Wave 1 dataclasses + enums for model governance. Pure data — no IO, no
sklearn, no DB.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Any, Optional


class Task(str, Enum):
    CLASSIFICATION = "classification"
    REGRESSION = "regression"


class ApprovalStatus(str, Enum):
    """Lifecycle states from ``docs/MODEL_GOVERNANCE.md``."""

    RESEARCH = "research"
    PAPER = "paper"
    MICRO_LIVE = "micro_live"
    LIVE = "live"
    RETIRED = "retired"


class Mode(str, Enum):
    """Mode in which a prediction is produced."""

    RESEARCH = "research"
    PAPER = "paper"
    LIVE = "live"


# Approval statuses that the registry treats as live-eligible. ``LIVE`` and
# ``MICRO_LIVE`` are runnable in real money; ``PAPER`` is the soak window.
LIVE_ELIGIBLE = frozenset({ApprovalStatus.PAPER, ApprovalStatus.MICRO_LIVE, ApprovalStatus.LIVE})


@dataclass(frozen=True)
class FeatureSpec:
    """
    One feature in a model's feature contract.

    ``transform`` is a free-form, deterministic descriptor of any
    pre-processing applied between the raw feature column and the model
    input (e.g. ``"zscore_30d"``, ``"log1p"``, ``"identity"``). It feeds
    into the feature hash; mutating it without a model retrain is a
    contract violation.
    """

    name: str
    dtype: str  # e.g. "float64", "int32", "category"
    transform: str = "identity"

    def to_canonical(self) -> dict[str, str]:
        return {"name": self.name, "dtype": self.dtype, "transform": self.transform}


def canonical_feature_list(features: list[FeatureSpec | dict[str, Any]]) -> list[dict[str, str]]:
    """Normalise a feature list to the canonical form used for hashing."""
    out: list[dict[str, str]] = []
    for f in features:
        if isinstance(f, FeatureSpec):
            out.append(f.to_canonical())
            continue
        if not isinstance(f, dict):
            raise TypeError(f"feature entry must be FeatureSpec or dict, got {type(f).__name__}")
        name = str(f["name"])
        dtype = str(f.get("dtype", "float64"))
        transform = str(f.get("transform", "identity"))
        out.append({"name": name, "dtype": dtype, "transform": transform})
    return out


@dataclass(frozen=True)
class FeatureContract:
    """In-memory representation of a frozen feature contract."""

    hash: str
    features: list[FeatureSpec]
    notes: Optional[str] = None
    created_at: Optional[datetime] = None


@dataclass(frozen=True)
class TrainingDatasetSpec:
    name: str
    version: str
    start_ts: datetime
    end_ts: datetime
    feature_contract_hash: str
    row_count: Optional[int] = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ModelContract:
    """
    Frozen description of a registered model version. Built from the
    ``config/model_registry.yaml`` entry plus the matching DB row in
    ``model_versions``.
    """

    name: str
    version: str
    task: Task
    target: str
    feature_contract_hash: str
    validation_method: str
    calibration_method: str = "none"
    horizon_seconds: Optional[int] = None
    horizon_bars: Optional[int] = None
    min_sample_size: int = 0
    approval_status: ApprovalStatus = ApprovalStatus.RESEARCH
    training_dataset: Optional[TrainingDatasetSpec] = None
    notes: Optional[str] = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def is_live_eligible(self) -> bool:
        return self.approval_status in LIVE_ELIGIBLE


@dataclass
class Prediction:
    """
    One prediction emitted by a registered model.

    ``as_of_ts`` is the latest feature timestamp used. ``prediction_ts``
    is when the model ran. ``as_of_ts <= prediction_ts`` is enforced by
    ``models.prediction_store.write_prediction``. Future-stamped
    predictions are rejected unconditionally.

    Probability / return / volatility / confidence are ``Decimal`` to keep
    money-related downstream math (sizing, expected P&L) Decimal-pure.
    """

    model_name: str
    model_version: str
    symbol: str
    as_of_ts: datetime
    prediction_ts: datetime
    feature_hash: str
    mode: Mode = Mode.RESEARCH
    horizon_seconds: Optional[int] = None
    horizon_bars: Optional[int] = None
    predicted_probability: Optional[Decimal] = None
    expected_return: Optional[Decimal] = None
    expected_volatility: Optional[Decimal] = None
    confidence: Optional[Decimal] = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        # Force timezone-aware UTC. Naive datetimes silently pretending to
        # be UTC are a recurring leakage source.
        if self.as_of_ts.tzinfo is None:
            self.as_of_ts = self.as_of_ts.replace(tzinfo=timezone.utc)
        if self.prediction_ts.tzinfo is None:
            self.prediction_ts = self.prediction_ts.replace(tzinfo=timezone.utc)
