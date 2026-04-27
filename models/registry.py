"""
models/registry.py
===================
Wave 1 — model registry. The single gateway between trained models and the
rest of the system.

Behaviour:

- ``ModelRegistry.load`` reads ``config/model_registry.yaml`` (path is
  configurable for tests). Each entry produces a ``ModelContract``.
- ``get(name, version=None)`` returns the latest registered version (or
  the explicit version) without enforcing approval status.
- ``require_for_mode(name, mode, version=None)`` is what callers in
  signal/opportunity/execution code MUST use. It returns the contract
  iff the registry approves the model for that mode. In ``live`` mode,
  unapproved or missing models raise ``ModelNotApprovedError``. In
  ``paper`` mode the same lookup is allowed for ``research`` models so
  paper soak can exercise them; a warning is logged. In ``research``
  mode, anything goes.

The DB row in ``model_versions`` is the source of truth for *what is
recorded*; the YAML is the source of truth for *what the operator
approved this build to use*. Both must agree for a model to run live.
This module deliberately does the YAML half — DB sync is a Wave 1
follow-up wired into ``scripts/model_report.py``.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Mapping, Optional

import yaml

from models.schemas import (
    LIVE_ELIGIBLE,
    ApprovalStatus,
    Mode,
    ModelContract,
    Task,
    TrainingDatasetSpec,
)

logger = logging.getLogger(__name__)


DEFAULT_REGISTRY_PATH = Path("config/model_registry.yaml")


class ModelNotApprovedError(RuntimeError):
    """Raised when a caller asks for a model that is not approved for the current mode."""


class ModelNotFoundError(KeyError):
    """Raised when a model name (or specific version) is not registered."""


@dataclass
class _RegistryEntry:
    contract: ModelContract


class ModelRegistry:
    def __init__(self, entries: Iterable[ModelContract] = ()) -> None:
        # Keyed by (name, version) for explicit lookups; we also keep a
        # name → list mapping for "latest version" resolution.
        self._by_key: dict[tuple[str, str], _RegistryEntry] = {}
        self._by_name: dict[str, list[ModelContract]] = {}
        for c in entries:
            self._add(c)

    # ── construction ────────────────────────────────────────────────────────

    @classmethod
    def load(cls, path: Optional[Path | str] = None) -> "ModelRegistry":
        p = Path(path) if path is not None else DEFAULT_REGISTRY_PATH
        if not p.exists():
            logger.info("model_registry | no file at %s — empty registry", p)
            return cls()
        try:
            raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError as exc:
            raise RuntimeError(f"could not parse {p}: {exc}") from exc
        entries = []
        for item in raw.get("models") or []:
            entries.append(_contract_from_yaml(item))
        return cls(entries)

    # ── public API ──────────────────────────────────────────────────────────

    def register(self, contract: ModelContract) -> None:
        """In-process registration (used by tests and Wave 2+ training scripts)."""
        self._add(contract)

    def names(self) -> list[str]:
        return sorted(self._by_name.keys())

    def versions(self, name: str) -> list[str]:
        return [c.version for c in self._by_name.get(name, [])]

    def get(self, name: str, version: Optional[str] = None) -> ModelContract:
        if name not in self._by_name:
            raise ModelNotFoundError(f"model not registered: {name!r}")
        if version is None:
            # "Latest" = last registered. We do not parse semver here on
            # purpose — registration order is operator-controlled and
            # explicit versions are the safe call site.
            return self._by_name[name][-1]
        key = (name, version)
        if key not in self._by_key:
            raise ModelNotFoundError(f"model not registered: {name!r}@{version}")
        return self._by_key[key].contract

    def require_for_mode(
        self,
        name: str,
        mode: Mode | str,
        version: Optional[str] = None,
    ) -> ModelContract:
        """
        Return the contract iff it is allowed in ``mode``.

        Live mode: must be PAPER, MICRO_LIVE, or LIVE.
        Paper mode: any non-RETIRED status (research is logged as warn).
        Research mode: anything except RETIRED.
        """
        m = Mode(mode) if isinstance(mode, str) else mode
        try:
            contract = self.get(name, version)
        except ModelNotFoundError:
            if m is Mode.LIVE:
                raise ModelNotApprovedError(
                    f"model {name!r} not registered — refusing live use"
                ) from None
            raise

        status = contract.approval_status

        if status is ApprovalStatus.RETIRED:
            raise ModelNotApprovedError(
                f"model {name}@{contract.version} is RETIRED — cannot use"
            )

        if m is Mode.LIVE:
            if status not in LIVE_ELIGIBLE:
                raise ModelNotApprovedError(
                    f"model {name}@{contract.version} status={status.value} "
                    f"is not approved for live mode (need paper/micro_live/live)"
                )
            return contract

        if m is Mode.PAPER:
            if status is ApprovalStatus.RESEARCH:
                logger.warning(
                    "model_registry | paper mode using research-status model "
                    "%s@%s — promote to paper before soak ends",
                    name,
                    contract.version,
                )
            return contract

        # research
        return contract

    # ── internals ───────────────────────────────────────────────────────────

    def _add(self, contract: ModelContract) -> None:
        key = (contract.name, contract.version)
        if key in self._by_key:
            raise ValueError(f"duplicate registration: {contract.name}@{contract.version}")
        self._by_key[key] = _RegistryEntry(contract=contract)
        self._by_name.setdefault(contract.name, []).append(contract)


# ── YAML mapping ────────────────────────────────────────────────────────────


def _contract_from_yaml(item: Mapping[str, object]) -> ModelContract:
    name = str(item["name"])
    version = str(item["version"])
    task = Task(str(item["task"]))
    target = str(item["target"])
    feature_contract_hash = str(item["feature_contract_hash"])
    validation_method = str(item.get("validation_method", "purged_kfold"))
    calibration_method = str(item.get("calibration_method", "none"))
    horizon_seconds = item.get("horizon_seconds")
    horizon_bars = item.get("horizon_bars")
    min_sample_size = int(item.get("min_sample_size", 0) or 0)
    approval_status = ApprovalStatus(str(item.get("approval_status", "research")))
    notes = item.get("notes")
    metadata = dict(item.get("metadata") or {})

    training_dataset = None
    td = item.get("training_dataset")
    if td:
        training_dataset = TrainingDatasetSpec(
            name=str(td["name"]),
            version=str(td["version"]),
            start_ts=_parse_ts(td["start_ts"]),
            end_ts=_parse_ts(td["end_ts"]),
            feature_contract_hash=str(td.get("feature_contract_hash", feature_contract_hash)),
            row_count=(int(td["row_count"]) if td.get("row_count") is not None else None),
            metadata=dict(td.get("metadata") or {}),
        )

    return ModelContract(
        name=name,
        version=version,
        task=task,
        target=target,
        feature_contract_hash=feature_contract_hash,
        validation_method=validation_method,
        calibration_method=calibration_method,
        horizon_seconds=int(horizon_seconds) if horizon_seconds is not None else None,
        horizon_bars=int(horizon_bars) if horizon_bars is not None else None,
        min_sample_size=min_sample_size,
        approval_status=approval_status,
        training_dataset=training_dataset,
        notes=str(notes) if notes is not None else None,
        metadata=metadata,
    )


def _parse_ts(raw: object) -> datetime:
    if isinstance(raw, datetime):
        return raw if raw.tzinfo else raw.replace(tzinfo=timezone.utc)
    return datetime.fromisoformat(str(raw)).astimezone(timezone.utc)


# ── convenience ─────────────────────────────────────────────────────────────


_DEFAULT_REGISTRY: Optional[ModelRegistry] = None


def get_default_registry() -> ModelRegistry:
    """Process-wide registry singleton, loaded lazily from the default YAML."""
    global _DEFAULT_REGISTRY
    if _DEFAULT_REGISTRY is None:
        _DEFAULT_REGISTRY = ModelRegistry.load(
            os.getenv("MYTBOT_MODEL_REGISTRY_YAML") or DEFAULT_REGISTRY_PATH
        )
    return _DEFAULT_REGISTRY


def reset_default_registry() -> None:
    """Test helper. Forces the next ``get_default_registry`` call to reload."""
    global _DEFAULT_REGISTRY
    _DEFAULT_REGISTRY = None
