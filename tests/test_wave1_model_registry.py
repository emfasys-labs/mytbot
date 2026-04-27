"""
tests/test_wave1_model_registry.py
====================================
Wave 1 acceptance tests:

1. Registry can register, list, and look up a dummy model by name +
   version.
2. ``require_for_mode`` enforces approval status:
   - LIVE mode rejects ``research`` models and raises for unregistered.
   - PAPER mode accepts ``research`` (with a warning) and ``paper``.
3. Feature hashes are deterministic (same input → same hash) and
   order-sensitive (different ordering → different hash).
4. Prediction store roundtrip: write then read back via an in-memory
   SQLite engine using the same ORM ``Base`` metadata that production
   uses, so the test exercises the real schema.
5. Future-stamped predictions are rejected by ``write_prediction``.
6. YAML registry round-trips an entry with a training_dataset block.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from models.calibration import (
    IdentityCalibrator,
    IsotonicCalibrator,
    PlattCalibrator,
    make_calibrator,
)
from models.feature_contracts import AsOfLeakageError, compute_feature_hash, require_as_of_safe
from models.prediction_store import read_predictions, write_prediction
from models.registry import (
    ModelNotApprovedError,
    ModelNotFoundError,
    ModelRegistry,
)
from models.schemas import (
    ApprovalStatus,
    FeatureSpec,
    Mode,
    ModelContract,
    Prediction,
    Task,
)
from storage.models import Base


def _make_dummy_contract(
    *,
    name: str = "dummy_meta",
    version: str = "0.1.0",
    status: ApprovalStatus = ApprovalStatus.RESEARCH,
) -> ModelContract:
    feats = [
        FeatureSpec("strategy_confidence", "float64"),
        FeatureSpec("atr_14", "float64"),
        FeatureSpec("hurst_64", "float64"),
    ]
    return ModelContract(
        name=name,
        version=version,
        task=Task.CLASSIFICATION,
        target="triple_barrier_outcome",
        feature_contract_hash=compute_feature_hash(feats),
        validation_method="purged_kfold",
        calibration_method="isotonic",
        horizon_bars=10,
        min_sample_size=500,
        approval_status=status,
        notes="wave1 dummy",
    )


# ── 1. Registry round-trip ───────────────────────────────────────────────


def test_registry_register_and_lookup() -> None:
    reg = ModelRegistry()
    contract = _make_dummy_contract()
    reg.register(contract)

    assert reg.names() == ["dummy_meta"]
    assert reg.versions("dummy_meta") == ["0.1.0"]
    assert reg.get("dummy_meta") is contract
    assert reg.get("dummy_meta", "0.1.0") is contract


def test_registry_duplicate_registration_rejected() -> None:
    reg = ModelRegistry()
    reg.register(_make_dummy_contract())
    with pytest.raises(ValueError, match="duplicate"):
        reg.register(_make_dummy_contract())


def test_registry_unknown_model_raises() -> None:
    reg = ModelRegistry()
    with pytest.raises(ModelNotFoundError):
        reg.get("nope")
    with pytest.raises(ModelNotFoundError):
        reg.get("dummy_meta", "9.9.9")


# ── 2. Approval gating ───────────────────────────────────────────────────


def test_require_for_mode_live_rejects_unregistered() -> None:
    reg = ModelRegistry()
    with pytest.raises(ModelNotApprovedError):
        reg.require_for_mode("ghost_model", Mode.LIVE)


def test_require_for_mode_live_rejects_research() -> None:
    reg = ModelRegistry()
    reg.register(_make_dummy_contract(status=ApprovalStatus.RESEARCH))
    with pytest.raises(ModelNotApprovedError):
        reg.require_for_mode("dummy_meta", Mode.LIVE)


def test_require_for_mode_live_accepts_paper_micro_live_live() -> None:
    reg = ModelRegistry()
    reg.register(_make_dummy_contract(version="0.1.0", status=ApprovalStatus.PAPER))
    reg.register(_make_dummy_contract(version="0.2.0", status=ApprovalStatus.MICRO_LIVE))
    reg.register(_make_dummy_contract(version="0.3.0", status=ApprovalStatus.LIVE))

    assert reg.require_for_mode("dummy_meta", Mode.LIVE, version="0.1.0").approval_status is ApprovalStatus.PAPER
    assert reg.require_for_mode("dummy_meta", Mode.LIVE, version="0.2.0").approval_status is ApprovalStatus.MICRO_LIVE
    assert reg.require_for_mode("dummy_meta", Mode.LIVE, version="0.3.0").approval_status is ApprovalStatus.LIVE


def test_require_for_mode_paper_warns_on_research(caplog) -> None:
    reg = ModelRegistry()
    reg.register(_make_dummy_contract(status=ApprovalStatus.RESEARCH))

    with caplog.at_level(logging.WARNING, logger="models.registry"):
        c = reg.require_for_mode("dummy_meta", Mode.PAPER)
    assert c.approval_status is ApprovalStatus.RESEARCH
    assert any("research-status" in r.message for r in caplog.records)


def test_require_for_mode_rejects_retired() -> None:
    reg = ModelRegistry()
    reg.register(_make_dummy_contract(status=ApprovalStatus.RETIRED))
    with pytest.raises(ModelNotApprovedError):
        reg.require_for_mode("dummy_meta", Mode.RESEARCH)


# ── 3. Feature hash determinism ──────────────────────────────────────────


def test_feature_hash_is_deterministic() -> None:
    feats = [
        FeatureSpec("a", "float64"),
        FeatureSpec("b", "float64", "zscore_30d"),
    ]
    assert compute_feature_hash(feats) == compute_feature_hash(feats)


def test_feature_hash_is_order_sensitive() -> None:
    a = [FeatureSpec("a", "float64"), FeatureSpec("b", "float64")]
    b = [FeatureSpec("b", "float64"), FeatureSpec("a", "float64")]
    assert compute_feature_hash(a) != compute_feature_hash(b)


def test_feature_hash_detects_transform_change() -> None:
    a = [FeatureSpec("x", "float64", "identity")]
    b = [FeatureSpec("x", "float64", "log1p")]
    assert compute_feature_hash(a) != compute_feature_hash(b)


def test_feature_hash_accepts_dict_form() -> None:
    a = [FeatureSpec("x", "float64", "identity")]
    b = [{"name": "x", "dtype": "float64", "transform": "identity"}]
    assert compute_feature_hash(a) == compute_feature_hash(b)


# ── 4. Prediction store roundtrip on in-memory SQLite ────────────────────


@pytest.mark.asyncio
async def test_prediction_store_roundtrip() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    contract = _make_dummy_contract(status=ApprovalStatus.PAPER)
    now = datetime.now(timezone.utc)
    pred = Prediction(
        model_name=contract.name,
        model_version=contract.version,
        symbol="AAPL",
        as_of_ts=now - timedelta(seconds=30),
        prediction_ts=now,
        feature_hash=contract.feature_contract_hash,
        mode=Mode.PAPER,
        horizon_bars=10,
        predicted_probability=Decimal("0.62"),
        expected_return=Decimal("0.0035"),
        expected_volatility=Decimal("0.012"),
        confidence=Decimal("0.74"),
        metadata={"strategy": "momentum"},
    )

    rid = await write_prediction(factory, pred)
    assert rid > 0

    rows = await read_predictions(factory, model_name=contract.name, limit=10)
    assert len(rows) == 1
    r = rows[0]
    assert r["symbol"] == "AAPL"
    assert r["mode"] == "paper"
    assert r["predicted_probability"] == Decimal("0.62000000")
    assert r["metadata"] == {"strategy": "momentum"}

    await engine.dispose()


# ── 5. Future-stamp leakage ──────────────────────────────────────────────


def test_require_as_of_safe_rejects_future_features() -> None:
    now = datetime.now(timezone.utc)
    with pytest.raises(AsOfLeakageError):
        require_as_of_safe(as_of_ts=now + timedelta(seconds=10), prediction_ts=now)


def test_require_as_of_safe_allows_equal_or_past() -> None:
    now = datetime.now(timezone.utc)
    require_as_of_safe(as_of_ts=now, prediction_ts=now)
    require_as_of_safe(as_of_ts=now - timedelta(seconds=1), prediction_ts=now)


@pytest.mark.asyncio
async def test_write_prediction_rejects_future_features() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    now = datetime.now(timezone.utc)
    pred = Prediction(
        model_name="dummy_meta",
        model_version="0.1.0",
        symbol="AAPL",
        as_of_ts=now + timedelta(minutes=5),  # future-stamped
        prediction_ts=now,
        feature_hash="0" * 64,
    )
    with pytest.raises(AsOfLeakageError):
        await write_prediction(factory, pred)

    await engine.dispose()


# ── 6. YAML loading with training_dataset block ──────────────────────────


def test_registry_loads_yaml_with_training_dataset(tmp_path: Path) -> None:
    yml = tmp_path / "registry.yaml"
    yml.write_text(
        """
models:
  - name: dummy_meta
    version: 0.1.0
    task: classification
    target: triple_barrier_outcome
    feature_contract_hash: abc123
    validation_method: purged_kfold
    calibration_method: isotonic
    horizon_bars: 10
    min_sample_size: 500
    approval_status: paper
    notes: yaml round trip
    metadata:
      family: meta_label
    training_dataset:
      name: ds_meta_v1
      version: '2026-04'
      start_ts: 2026-01-01T00:00:00+00:00
      end_ts: 2026-04-01T00:00:00+00:00
      feature_contract_hash: abc123
      row_count: 12345
      metadata:
        symbols: 50
""",
        encoding="utf-8",
    )

    reg = ModelRegistry.load(yml)
    c = reg.get("dummy_meta")
    assert c.approval_status is ApprovalStatus.PAPER
    assert c.task is Task.CLASSIFICATION
    assert c.training_dataset is not None
    assert c.training_dataset.name == "ds_meta_v1"
    assert c.training_dataset.row_count == 12345
    assert c.training_dataset.metadata == {"symbols": 50}
    assert c.metadata == {"family": "meta_label"}


def test_default_yaml_loads_empty_models_list() -> None:
    # The shipping config/model_registry.yaml MUST be loadable and empty.
    reg = ModelRegistry.load(Path("config/model_registry.yaml"))
    assert reg.names() == []


# ── 7. Calibration smoke ────────────────────────────────────────────────


def test_make_calibrator_factory() -> None:
    assert isinstance(make_calibrator("none"), IdentityCalibrator)
    assert isinstance(make_calibrator("isotonic"), IsotonicCalibrator)
    assert isinstance(make_calibrator("platt"), PlattCalibrator)
    with pytest.raises(ValueError):
        make_calibrator("nonsense")


def test_isotonic_calibrator_monotone_on_synthetic() -> None:
    import numpy as np

    rng = np.random.default_rng(0)
    scores = rng.uniform(0, 1, size=200)
    labels = (scores + rng.normal(0, 0.1, size=200) > 0.5).astype(int)
    cal = IsotonicCalibrator().fit(scores, labels)
    grid = np.linspace(0.0, 1.0, 21)
    out = cal.transform(grid)
    # Monotone non-decreasing.
    assert np.all(np.diff(out) >= -1e-9)
    assert out.min() >= 0.0 and out.max() <= 1.0
