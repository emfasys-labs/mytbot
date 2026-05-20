"""
signals/trained_meta_labeler.py
================================
Wave 2 — runtime hook for the trained meta-labeller.

This module is the bridge between the strategy / opportunity layer and
the ``models/meta_label/`` infrastructure. It is *intentionally
optional*: when no approved model exists, ``evaluate`` returns a
"pass-through" decision so the existing heuristic chain in
``signals/meta_labeler.py`` continues to drive behaviour. This honours
the architectural rule "any live trading activation must be config-gated
and off by default".

Operator workflow:

  1. Train an artefact via ``scripts/train_meta_labeler.py``.
  2. Save it to the configured artefact directory.
  3. Add an entry to ``config/model_registry.yaml`` with
     ``approval_status: paper`` (and a matching DB row in ``model_versions``).
  4. Flip ``trained_meta_labeler.enabled: true`` in
     ``config/meta_labeler.yaml``.
  5. Soak in paper for 2-4 weeks; only then promote to ``micro_live``.

Dashboard contract (Wave 13 will read this): for every candidate the
meta-labeller saw, the runtime gets back a ``MetaLabelDecision`` carrying
``probability``, ``threshold``, ``reason``, ``model_name``, and
``model_version``. Strategy code MUST surface these in the
``SignalCandidate.metadata`` (or equivalent) so the funnel renders.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Optional

import yaml

from models.meta_label.calibration import CalibrationTable
from models.meta_label.infer import MetaLabelDecision, score_features
from models.meta_label.thresholds import (
    DynamicThresholdConfig,
    ThresholdConfig,
    ThresholdContext,
    resolve_threshold,
    threshold_for,
)
from models.meta_label.train import TrainedMetaLabel
from models.registry import (
    ModelNotApprovedError,
    ModelNotFoundError,
    ModelRegistry,
    get_default_registry,
)
from models.schemas import Mode, ModelContract

logger = logging.getLogger(__name__)


DEFAULT_CONFIG_PATH = Path("config/meta_labeler.yaml")


@dataclass
class TrainedMetaLabelerConfig:
    enabled: bool = False
    model_name: Optional[str] = None
    model_version: Optional[str] = None
    artifact_path: Optional[Path] = None
    write_predictions: bool = True
    # Accepts both the new ``DynamicThresholdConfig`` (preferred) and the
    # legacy ``ThresholdConfig`` (kept for backward compatibility). The
    # resolver dispatches on type.
    thresholds: Any = field(default_factory=DynamicThresholdConfig)

    @classmethod
    def from_dict(cls, raw: Optional[Mapping[str, Any]]) -> "TrainedMetaLabelerConfig":
        if not raw:
            return cls()
        sect = raw.get("trained_meta_labeler") if "trained_meta_labeler" in raw else raw
        sect = dict(sect or {})
        ap = sect.get("artifact_path")
        return cls(
            enabled=bool(sect.get("enabled", False)),
            model_name=(str(sect["model_name"]) if sect.get("model_name") else None),
            model_version=(str(sect["model_version"]) if sect.get("model_version") else None),
            artifact_path=(Path(ap) if ap else None),
            write_predictions=bool(sect.get("write_predictions", True)),
            thresholds=DynamicThresholdConfig.from_dict(sect.get("thresholds")),
        )

    @classmethod
    def load(cls, path: Path | str = DEFAULT_CONFIG_PATH) -> "TrainedMetaLabelerConfig":
        p = Path(path)
        if not p.exists():
            return cls()
        try:
            raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError as exc:
            raise RuntimeError(f"could not parse {p}: {exc}") from exc
        return cls.from_dict(raw)


# ── core entry point ────────────────────────────────────────────────────────


def evaluate_features(
    *,
    features: Mapping[str, float],
    mode: Mode | str,
    config: TrainedMetaLabelerConfig,
    registry: Optional[ModelRegistry] = None,
    regime: Optional[str] = None,
    portfolio_mode: Optional[str] = None,
    market_state_score: float = 1.0,
    market_volatility_scalar: float = 1.0,
    deployment_pressure: float = 0.0,
    artefact_loader=None,
) -> MetaLabelDecision:
    """
    Score a single candidate's feature dict and produce a decision.

    Behaviour matrix:

    - ``config.enabled is False``                → pass-through (kept=True, reason="disabled").
    - No registered model name in config         → pass-through (reason="no_model_passthrough").
    - Registered name not found / not approved   → in LIVE mode, raise; in PAPER/RESEARCH
                                                   mode, pass-through with ``reason="not_approved"``.
    - Artefact path missing / load failure       → pass-through with reason="artefact_unavailable".
    - All checks pass                            → kept = (probability >= threshold).
    """
    ctx = ThresholdContext(
        mode=portfolio_mode,
        regime=regime,
        market_state_score=float(market_state_score),
        market_volatility_scalar=float(market_volatility_scalar),
        deployment_pressure=float(deployment_pressure),
    )

    def _passthrough_threshold(calibration: Optional[CalibrationTable] = None) -> float:
        # Dispatch on config type — legacy ThresholdConfig uses static
        # lookup; DynamicThresholdConfig uses the dynamic resolver.
        if isinstance(config.thresholds, ThresholdConfig):
            return config.thresholds.lookup(mode=portfolio_mode, regime=regime)
        return resolve_threshold(config.thresholds, context=ctx, calibration=calibration).threshold

    if not config.enabled:
        return MetaLabelDecision(
            kept=True,
            probability=None,
            threshold=_passthrough_threshold(),
            reason="disabled",
        )

    if not config.model_name:
        return MetaLabelDecision(
            kept=True,
            probability=None,
            threshold=_passthrough_threshold(),
            reason="no_model_passthrough",
        )

    reg = registry or get_default_registry()

    contract: Optional[ModelContract] = None
    try:
        contract = reg.require_for_mode(
            config.model_name,
            mode=mode,
            version=config.model_version,
        )
    except ModelNotApprovedError:
        if Mode(mode) is Mode.LIVE if isinstance(mode, str) else mode is Mode.LIVE:
            raise
        return MetaLabelDecision(
            kept=True,
            probability=None,
            threshold=_passthrough_threshold(),
            reason="not_approved",
            model_name=config.model_name,
            model_version=config.model_version,
        )
    except ModelNotFoundError:
        if (Mode(mode) if isinstance(mode, str) else mode) is Mode.LIVE:
            raise ModelNotApprovedError(
                f"model {config.model_name!r} not registered — refusing live use"
            )
        return MetaLabelDecision(
            kept=True,
            probability=None,
            threshold=_passthrough_threshold(),
            reason="not_registered",
            model_name=config.model_name,
            model_version=config.model_version,
        )

    calibration = _load_calibration_for(contract)

    artefact = _load_artefact(config, contract, artefact_loader)
    if artefact is None:
        logger.warning(
            "trained_meta_labeler | artefact unavailable for %s@%s — passing through",
            contract.name,
            contract.version,
        )
        return MetaLabelDecision(
            kept=True,
            probability=None,
            threshold=_passthrough_threshold(calibration),
            reason="artefact_unavailable",
            model_name=contract.name,
            model_version=contract.version,
            feature_hash=contract.feature_contract_hash,
        )

    # Build a feature row in the artefact's contract ordering.
    cols = [s.name for s in artefact.feature_specs]
    row = [float(features.get(c, 0.0) or 0.0) for c in cols]
    probs = score_features(artefact, _atleast_2d(row))
    p = float(probs[0])

    # Threshold dispatch: legacy static config vs dynamic resolver.
    if isinstance(config.thresholds, ThresholdConfig):
        thr = config.thresholds.lookup(mode=portfolio_mode, regime=regime)
        meta_extra: dict[str, Any] = {"threshold_source": "legacy_static"}
    else:
        resolution = resolve_threshold(config.thresholds, context=ctx, calibration=calibration)
        thr = resolution.threshold
        meta_extra = {
            "threshold_source": "dynamic",
            "target_win_rate": resolution.target_win_rate,
            "target_pre_clamp": resolution.target_pre_clamp,
            "calibration_used": resolution.calibration_used,
            "threshold_floor": resolution.floor,
            "threshold_ceiling": resolution.ceiling,
            "regime_caution": resolution.regime_caution,
            "vol_caution": resolution.vol_caution,
            "deployment_relief": resolution.deployment_relief,
        }
    kept = p >= thr
    reason = "approved" if kept else "below_threshold"

    return MetaLabelDecision(
        kept=kept,
        probability=p,
        threshold=thr,
        reason=reason,
        model_name=contract.name,
        model_version=contract.version,
        feature_hash=contract.feature_contract_hash,
        metadata={
            "classifier_kind": getattr(artefact, "classifier_kind", "unknown"),
            "calibration_method": getattr(artefact, "calibration_method", "none"),
            **meta_extra,
        },
    )


# ── helpers ─────────────────────────────────────────────────────────────────


def _load_calibration_for(contract: "ModelContract") -> Optional[CalibrationTable]:
    """Resolve the calibration table for a registered model contract.

    Order of resolution:
      1. ``contract.metadata["calibration_table"]`` — structured list
         in the registry YAML (preferred, explicit, inspectable).
      2. ``contract.metadata["activation_gates"]["validation_report_path"]``
         → parse the markdown file (legacy fallback).
    Returns ``None`` if neither source yields a usable table; the
    threshold resolver falls back to its operator-configured bounds.
    """
    md = contract.metadata or {}
    structured = md.get("calibration_table")
    table = CalibrationTable.from_dict(structured) if structured else None
    if table is not None:
        return table
    gates = md.get("activation_gates") if isinstance(md, dict) else None
    if isinstance(gates, dict):
        report_path = gates.get("validation_report_path")
        if report_path:
            return CalibrationTable.from_validation_markdown(report_path)
    return None


def _atleast_2d(values: list[float]):
    import numpy as np

    arr = np.asarray(values, dtype=float)
    return arr.reshape(1, -1) if arr.ndim == 1 else arr


def _load_artefact(
    config: TrainedMetaLabelerConfig,
    contract: ModelContract,
    artefact_loader,
) -> Optional[TrainedMetaLabel]:
    """Load the trained artefact. Tests can inject a custom loader."""
    if artefact_loader is not None:
        try:
            return artefact_loader(contract)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "trained_meta_labeler | injected loader failed for %s@%s: %s",
                contract.name,
                contract.version,
                exc,
            )
            return None

    path = config.artifact_path
    if path is None:
        # Fall back to convention: <artifact_dir>/<name>-<version>.pkl
        artifact_dir_meta = contract.metadata.get("artifact_dir") if contract.metadata else None
        if artifact_dir_meta:
            path = Path(str(artifact_dir_meta)) / f"{contract.name}-{contract.version}.pkl"

    if path is None or not Path(path).exists():
        return None

    try:
        return TrainedMetaLabel.load(path)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "trained_meta_labeler | could not load artefact %s for %s@%s: %s",
            path,
            contract.name,
            contract.version,
            exc,
        )
        return None
