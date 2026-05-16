"""
signals/forecast_bridge.py
============================
Wave 6 — runtime hook for forecast-native ML.

Bridge between the forecast model registry / artefact store and the
strategy / opportunity layer. Mirrors ``signals/trained_meta_labeler.py``:
disabled by default, returns a "pass-through" decision when no
approved model exists, raises in LIVE for unapproved models.

Operator workflow:

  1. Train forecasts with ``scripts/train_forecasts.py`` (one artefact
     per (target, horizon) pair).
  2. Save artefacts to the configured directory.
  3. Register entries in ``config/model_registry.yaml`` with
     ``approval_status: paper`` and a matching ``model_versions`` row.
  4. Set ``forecast_bridge.enabled: true`` in
     ``config/forecast_models.yaml``.
  5. Soak in paper for 2-4 weeks before promoting to micro_live.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Optional

import yaml

from models.forecasts.ensemble import EnsembleMember, EnsembleResult, ForecastEnsemble
from models.forecasts.infer_tabular import score_forecast
from models.forecasts.train_tabular import TrainedForecastModel
from models.registry import (
    ModelNotApprovedError,
    ModelNotFoundError,
    ModelRegistry,
    get_default_registry,
)
from models.schemas import Mode, ModelContract

logger = logging.getLogger(__name__)


DEFAULT_CONFIG_PATH = Path("config/forecast_models.yaml")


@dataclass
class ForecastModelEntry:
    """One trained forecast model that participates in the ensemble."""

    name: str
    version: Optional[str] = None
    target_kind: str = "forward_return"
    horizon: int = 1
    weight: float = 1.0
    artifact_path: Optional[Path] = None
    # "tabular" → scored from the flat feature row (Wave 6, unchanged).
    # "sequence" → scored from a pre-built (window, n_feat) array via
    # models.deep_sequence.infer.score_sequence (Phase B TCN/TFT). A
    # sequence member with no window available skips safely.
    kind: str = "tabular"


@dataclass
class ForecastBridgeConfig:
    enabled: bool = False
    write_predictions: bool = True
    members: list[ForecastModelEntry] = field(default_factory=list)

    @classmethod
    def from_dict(cls, raw: Optional[Mapping[str, Any]]) -> "ForecastBridgeConfig":
        if not raw:
            return cls()
        sect = raw.get("forecast_bridge") if "forecast_bridge" in raw else raw
        sect = dict(sect or {})
        members: list[ForecastModelEntry] = []
        for item in sect.get("members") or []:
            ap = item.get("artifact_path")
            members.append(
                ForecastModelEntry(
                    name=str(item["name"]),
                    version=(str(item["version"]) if item.get("version") else None),
                    target_kind=str(item.get("target_kind", "forward_return")),
                    horizon=int(item.get("horizon", 1)),
                    weight=float(item.get("weight", 1.0)),
                    artifact_path=Path(ap) if ap else None,
                    kind=str(item.get("kind", "tabular")).strip().lower() or "tabular",
                )
            )
        return cls(
            enabled=bool(sect.get("enabled", False)),
            write_predictions=bool(sect.get("write_predictions", True)),
            members=members,
        )

    @classmethod
    def load(cls, path: Path | str = DEFAULT_CONFIG_PATH) -> "ForecastBridgeConfig":
        p = Path(path)
        if not p.exists():
            return cls()
        try:
            raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError as exc:
            raise RuntimeError(f"could not parse {p}: {exc}") from exc
        return cls.from_dict(raw)


@dataclass
class ForecastDecision:
    """Per-symbol forecast verdict for the dashboard funnel."""

    used: bool
    reason: str  # "disabled" | "no_models" | "not_registered" | "not_approved"
                 # | "artefact_unavailable" | "approved" | "error"
    expected_return: Optional[float] = None
    expected_volatility: Optional[float] = None
    confidence: Optional[float] = None
    horizons_used: tuple[int, ...] = ()
    contributions: dict[str, float] = field(default_factory=dict)
    members_used: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


# ── core entry point ────────────────────────────────────────────────────────


def evaluate_features(
    *,
    features: Mapping[str, float],
    mode: Mode | str,
    config: ForecastBridgeConfig,
    registry: Optional[ModelRegistry] = None,
    artefact_loader=None,
    sequence_window: Any = None,
) -> ForecastDecision:
    """
    Score the supplied feature dict through every approved forecast member
    and combine the result via ``ForecastEnsemble``.

    Behaviour matrix (mirrors trained_meta_labeler.evaluate_features):

    - ``config.enabled is False`` → ``reason='disabled'``, ``used=False``.
    - No registered members → ``reason='no_models'``.
    - Any member missing in registry / unapproved:
        * In LIVE → raise ``ModelNotApprovedError``.
        * In PAPER/RESEARCH → log + skip that member; if all members
          skipped, return ``reason='not_approved'``.
    - Artefact load failure for a single member → log + skip member.
    - At least one member produced a value → ``reason='approved'``.
    """
    if not config.enabled:
        return ForecastDecision(used=False, reason="disabled")
    if not config.members:
        return ForecastDecision(used=False, reason="no_models")

    reg = registry or get_default_registry()
    is_live = (Mode(mode) if isinstance(mode, str) else mode) is Mode.LIVE

    members: list[EnsembleMember] = []
    members_used: list[str] = []
    contributions_meta: dict[str, Any] = {}
    skipped_reasons: dict[str, str] = {}

    for entry in config.members:
        try:
            contract = reg.require_for_mode(entry.name, mode=mode, version=entry.version)
        except ModelNotApprovedError:
            if is_live:
                raise
            skipped_reasons[entry.name] = "not_approved"
            continue
        except ModelNotFoundError:
            if is_live:
                raise ModelNotApprovedError(
                    f"forecast model {entry.name!r} not registered — refusing live use"
                )
            skipped_reasons[entry.name] = "not_registered"
            continue

        artefact = _load_artefact(entry, contract, artefact_loader)
        if artefact is None:
            skipped_reasons[entry.name] = "artefact_unavailable"
            continue

        # ── Phase B safety gate ──────────────────────────────────────────
        # A deep/sequence artefact (TCN/TFT) must carry proof it BEAT the
        # Ridge baseline out-of-sample after costs. The Wave-11
        # promote_eligible check is advisory at train time only; without
        # this runtime gate an untrained/unvalidated deep model could be
        # paper-activated by editing config alone. We refuse any artefact
        # that self-identifies as deep/sequence but lacks a truthy
        # ``deep_beats_baseline`` marker (set by the validated training
        # pipeline). Tabular forecast artefacts are unaffected (they have
        # no such kind marker), so the existing contract/tests stand. Fails
        # closed in LIVE, skips-with-reason in PAPER/RESEARCH.
        _kind = str(
            getattr(artefact, "model_kind", "")
            or (getattr(artefact, "metadata", {}) or {}).get("model_kind", "")
        ).lower()
        if any(k in _kind for k in ("tcn", "tft", "sequence", "deep")):
            _meta = getattr(artefact, "metadata", {}) or {}
            if _meta.get("deep_beats_baseline") is not True:
                if is_live:
                    raise ModelNotApprovedError(
                        f"deep forecast model {entry.name!r} lacks a passing "
                        "baseline_comparison (deep_beats_baseline) — refusing live use"
                    )
                logger.warning(
                    "forecast_bridge | deep model %s@%s skipped: not validated "
                    "against baseline (deep_beats_baseline != True)",
                    entry.name, contract.version,
                )
                skipped_reasons[entry.name] = "deep_not_baseline_validated"
                continue

        if entry.kind == "sequence":
            # Phase B: TCN/TFT scored from a pre-built (window, n_feat)
            # array supplied by the caller (the loop already loads the
            # recent feature df — we never do DB I/O in this pure path).
            # No window → skip safely; never raise into the live path.
            if sequence_window is None:
                skipped_reasons[entry.name] = "no_sequence_window"
                continue
            try:
                from models.deep_sequence.infer import score_sequence

                seq_in = _align_sequence_to_artefact(sequence_window, artefact)
                if seq_in is None:
                    skipped_reasons[entry.name] = "seq_window_contract_mismatch"
                    continue
                seq_res = score_sequence(
                    artefact=artefact,
                    sequence=seq_in,
                    architecture=str(entry.kind),
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "forecast_bridge | sequence predict failed for %s@%s: %s",
                    entry.name, contract.version, exc,
                )
                skipped_reasons[entry.name] = f"predict_failed:{exc.__class__.__name__}"
                continue
            if not seq_res.used or seq_res.prediction is None:
                skipped_reasons[entry.name] = f"seq_{seq_res.reason}"
                continue
            value = float(seq_res.prediction)
        else:
            try:
                cols = [s.name for s in artefact.feature_specs]
                row = [float(features.get(c, 0.0) or 0.0) for c in cols]
                value = float(score_forecast(artefact, _atleast_2d(row))[0])
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "forecast_bridge | predict failed for %s@%s: %s",
                    entry.name,
                    contract.version,
                    exc,
                )
                skipped_reasons[entry.name] = f"predict_failed:{exc.__class__.__name__}"
                continue

        members.append(
            EnsembleMember(
                target_kind=artefact.target_kind or entry.target_kind,
                horizon=int(artefact.horizon or entry.horizon),
                value=value,
                weight=float(entry.weight),
                model_name=contract.name,
            )
        )
        members_used.append(f"{contract.name}@{contract.version}")
        contributions_meta[f"{artefact.target_kind}_h{artefact.horizon}"] = value

    if not members:
        # Every configured member was skipped.
        reason = "not_approved" if any(
            r in {"not_approved", "not_registered"} for r in skipped_reasons.values()
        ) else "artefact_unavailable"
        return ForecastDecision(
            used=False,
            reason=reason,
            metadata={"skipped": skipped_reasons},
        )

    result: EnsembleResult = ForecastEnsemble.combine(members)
    return ForecastDecision(
        used=True,
        reason="approved",
        expected_return=result.expected_return,
        expected_volatility=result.expected_volatility,
        confidence=result.confidence,
        horizons_used=result.horizons_used,
        contributions=result.contributions,
        members_used=members_used,
        metadata={"skipped": skipped_reasons} if skipped_reasons else {},
    )


# ── helpers ─────────────────────────────────────────────────────────────────


def _atleast_2d(values: list[float]):
    import numpy as np

    arr = np.asarray(values, dtype=float)
    return arr.reshape(1, -1) if arr.ndim == 1 else arr


def _align_sequence_to_artefact(sequence_window: Any, artefact: Any):
    """Build a contract-aligned ``(window, n_feat)`` array for a sequence
    artefact, or ``None`` (caller skips safely — never raises).

    Accepts either:
      * a ready 2-D array-like ``(window, n_feat)`` — validated against the
        artefact's window/feature count if known, else passed through; OR
      * a recent-history dict ``{"columns": [...], "rows": [[...], ...]}``
        (what the loop attaches) — the artefact's ``feature_specs`` column
        order is selected and the trailing ``window`` rows are taken, so
        the runtime input exactly matches the trained contract regardless
        of how many extra columns/rows the loop provided.
    """
    try:
        import numpy as np

        spec = getattr(artefact, "spec", None)
        spec = spec if isinstance(spec, dict) else {}
        a_window = spec.get("window") or getattr(artefact, "window", None)
        feat_names = [
            getattr(s, "name", None) for s in (getattr(artefact, "feature_specs", []) or [])
        ]
        feat_names = [f for f in feat_names if f]

        # Case 1: history dict → align to the artefact contract.
        if isinstance(sequence_window, dict):
            cols = list(sequence_window.get("columns") or [])
            rows = sequence_window.get("rows") or []
            if not cols or not rows or not feat_names:
                return None
            idx = []
            for fn in feat_names:
                if fn not in cols:
                    return None  # missing a contract feature → skip safely
                idx.append(cols.index(fn))
            mat = np.asarray(rows, dtype=float)
            if mat.ndim != 2:
                return None
            sel = mat[:, idx]  # (n_rows, n_feat) in contract order
            w = int(a_window) if a_window else sel.shape[0]
            if sel.shape[0] < w:
                return None  # not enough history yet
            return sel[-w:, :]

        # Case 2: ready array-like (assume caller pre-aligned).
        arr = np.asarray(sequence_window, dtype=float)
        if arr.ndim == 3 and arr.shape[0] == 1:
            arr = arr[0]
        if arr.ndim != 2:
            return None
        if a_window and arr.shape[0] != int(a_window):
            return None
        if feat_names and arr.shape[1] != len(feat_names):
            return None
        return arr
    except Exception:  # noqa: BLE001 — alignment must never raise live
        return None


def _load_artefact(
    entry: ForecastModelEntry,
    contract: ModelContract,
    artefact_loader,
) -> Optional[TrainedForecastModel]:
    if artefact_loader is not None:
        try:
            return artefact_loader(entry, contract)
        except Exception as exc:  # noqa: BLE001
            logger.warning("forecast_bridge | injected loader failed: %s", exc)
            return None
    path = entry.artifact_path
    if path is None:
        artifact_dir = (contract.metadata or {}).get("artifact_dir")
        if artifact_dir:
            path = Path(str(artifact_dir)) / f"{contract.name}-{contract.version}.pkl"
    if path is None or not Path(path).exists():
        return None
    try:
        return TrainedForecastModel.load(path)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "forecast_bridge | could not load artefact %s for %s@%s: %s",
            path,
            contract.name,
            contract.version,
            exc,
        )
        return None
