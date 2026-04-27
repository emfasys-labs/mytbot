"""
tests/test_wave10_microstructure.py
=====================================
Wave 10 acceptance tests for order-book features + the imbalance forecaster.

Coverage:

- ``build_orderbook_features`` returns expected keys and handles
  malformed / crossed / empty books gracefully.
- ``top_of_book_imbalance`` sign and magnitude match a hand-crafted
  example.
- ``stack_lob_features`` drops unusable rows and aligns X / y.
- ``train_imbalance_forecaster`` produces an artefact whose probability
  is monotone in the planted relationship.
- ``score_orderbook`` honours the freshness gate and returns
  ``reason="missing_artefact"`` when no artefact is provided.
- save/load round-trip preserves predictions; tampering with the
  feature_contract_hash is detected on load.
- Default YAML loads disabled.
"""

from __future__ import annotations

import pickle
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
import yaml

from data.orderbook_features import (
    OrderbookLevel,
    OrderbookSnapshot,
    build_orderbook_features,
    is_book_well_formed,
    spread_bps,
    top_of_book_imbalance,
)
from models.microstructure import (
    LOBFeatureSet,
    TrainedLOBForecaster,
    score_orderbook,
    stack_lob_features,
    train_imbalance_forecaster,
)


# ── helpers ────────────────────────────────────────────────────────────────


def _snap(
    *,
    bid_qty: float = 1.0,
    ask_qty: float = 1.0,
    spread: float = 0.10,
    timestamp: datetime | None = None,
) -> OrderbookSnapshot:
    ts = timestamp or datetime.now(timezone.utc)
    return OrderbookSnapshot(
        symbol="BTC-USD",
        bids=(
            OrderbookLevel(price=Decimal("100"), quantity=Decimal(str(bid_qty))),
            OrderbookLevel(price=Decimal("99.95"), quantity=Decimal("2")),
            OrderbookLevel(price=Decimal("99.90"), quantity=Decimal("3")),
            OrderbookLevel(price=Decimal("99.85"), quantity=Decimal("4")),
            OrderbookLevel(price=Decimal("99.80"), quantity=Decimal("5")),
        ),
        asks=(
            OrderbookLevel(price=Decimal(str(100 + spread)), quantity=Decimal(str(ask_qty))),
            OrderbookLevel(price=Decimal(str(100 + spread + 0.05)), quantity=Decimal("2")),
            OrderbookLevel(price=Decimal(str(100 + spread + 0.10)), quantity=Decimal("3")),
            OrderbookLevel(price=Decimal(str(100 + spread + 0.15)), quantity=Decimal("4")),
            OrderbookLevel(price=Decimal(str(100 + spread + 0.20)), quantity=Decimal("5")),
        ),
        timestamp=ts,
        asset_class="crypto",
    )


# ── feature correctness ───────────────────────────────────────────────────


def test_top_of_book_imbalance_sign_and_magnitude() -> None:
    snap = _snap(bid_qty=3.0, ask_qty=1.0)
    imb = top_of_book_imbalance(snap)
    # (3 - 1) / 4 = 0.5
    assert imb == pytest.approx(0.5)
    snap2 = _snap(bid_qty=1.0, ask_qty=3.0)
    assert top_of_book_imbalance(snap2) == pytest.approx(-0.5)


def test_spread_bps_positive_and_finite() -> None:
    snap = _snap(spread=0.10)
    bps = spread_bps(snap)
    assert bps is not None and bps > 0


def test_build_orderbook_features_emits_full_block() -> None:
    snap = _snap(bid_qty=2.0, ask_qty=1.0, spread=0.20)
    feats = build_orderbook_features(snap, depth=5)
    assert feats["well_formed"] == 1.0
    expected_keys = {
        "spread_bps", "top_of_book_imbalance", "depth_imbalance",
        "book_slope", "liquidity_fragility", "vpin_proxy", "quote_staleness",
        "well_formed",
    }
    assert set(feats.keys()) == expected_keys
    assert feats["top_of_book_imbalance"] > 0


def test_malformed_book_is_flagged() -> None:
    crossed = OrderbookSnapshot(
        symbol="X",
        bids=(OrderbookLevel(price=Decimal("100"), quantity=Decimal("1")),),
        asks=(OrderbookLevel(price=Decimal("99"), quantity=Decimal("1")),),  # ask below bid
        timestamp=datetime.now(timezone.utc),
    )
    assert is_book_well_formed(crossed) is False
    feats = build_orderbook_features(crossed)
    assert feats["well_formed"] == 0.0
    assert feats["spread_bps"] is None


def test_empty_book_returns_zero_imbalance() -> None:
    empty = OrderbookSnapshot(
        symbol="X", bids=(), asks=(), timestamp=datetime.now(timezone.utc)
    )
    assert top_of_book_imbalance(empty) == 0.0


# ── stacker ───────────────────────────────────────────────────────────────


def test_stack_lob_features_drops_malformed_rows() -> None:
    good = _snap(bid_qty=2.0, ask_qty=1.0)
    crossed = OrderbookSnapshot(
        symbol="X",
        bids=(OrderbookLevel(price=Decimal("100"), quantity=Decimal("1")),),
        asks=(OrderbookLevel(price=Decimal("99"), quantity=Decimal("1")),),
        timestamp=datetime.now(timezone.utc),
    )
    ds = stack_lob_features([good, crossed], [0.001, -0.001])
    # Only the good snapshot survives.
    assert len(ds.X) == 1
    assert ds.feature_names is not None


# ── training + inference ──────────────────────────────────────────────────


def test_train_imbalance_forecaster_recovers_planted_relationship() -> None:
    rng = np.random.default_rng(0)
    snaps: list[OrderbookSnapshot] = []
    rets: list[float] = []
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    # Build 400 snapshots where the realised return is *strongly*
    # sign-aligned with top-of-book imbalance. Noise is small relative
    # to signal so the logistic regression learns a non-trivial weight.
    for i in range(400):
        bid_q = float(rng.uniform(0.5, 5.0))
        ask_q = float(rng.uniform(0.5, 5.0))
        imb = (bid_q - ask_q) / (bid_q + ask_q)
        ret = 0.01 * imb + rng.normal(0, 0.0005)  # signal:noise ~20:1
        snaps.append(
            _snap(
                bid_qty=bid_q,
                ask_qty=ask_q,
                spread=float(rng.uniform(0.05, 0.30)),
                timestamp=base + timedelta(seconds=i),
            )
        )
        rets.append(ret)
    ds = stack_lob_features(snaps, rets)
    assert len(ds.X) > 100
    art, report = train_imbalance_forecaster(dataset=ds, calibration="none")

    # Compare two snapshots that lie *within* the training distribution
    # (extreme out-of-distribution rows would saturate the logistic and
    # tie at ~1.0 / ~0.0).
    bull = _snap(bid_qty=4.5, ask_qty=0.6)
    bear = _snap(bid_qty=0.6, ask_qty=4.5)
    feats_bull = build_orderbook_features(bull)
    feats_bear = build_orderbook_features(bear)
    cols = [s.name for s in art.feature_specs]
    bull_row = np.asarray([float(feats_bull[c]) for c in cols], dtype=float)
    bear_row = np.asarray([float(feats_bear[c]) for c in cols], dtype=float)
    p_bull = float(np.asarray(art.predict(bull_row)).ravel()[0])
    p_bear = float(np.asarray(art.predict(bear_row)).ravel()[0])
    assert p_bull > p_bear


def test_score_orderbook_freshness_gate() -> None:
    art = None  # not needed for this test
    stale = _snap(timestamp=datetime.now(timezone.utc) - timedelta(seconds=30))
    res = score_orderbook(stale, art, max_staleness_seconds=5.0)
    assert res.used is False
    assert res.reason == "stale"


def test_score_orderbook_missing_artefact_returns_imbalance_signal() -> None:
    snap = _snap(bid_qty=3.0, ask_qty=1.0)
    res = score_orderbook(snap, None, max_staleness_seconds=60.0)
    assert res.used is False
    assert res.reason == "missing_artefact"
    # Imbalance signal is still surfaced for the dashboard.
    assert res.imbalance_signal == pytest.approx(0.5)


def test_score_orderbook_uses_artefact_when_fresh(tmp_path: Path) -> None:
    rng = np.random.default_rng(1)
    snaps: list[OrderbookSnapshot] = []
    rets: list[float] = []
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    for i in range(200):
        bid_q = float(rng.uniform(0.5, 5.0))
        ask_q = float(rng.uniform(0.5, 5.0))
        imb = (bid_q - ask_q) / (bid_q + ask_q)
        ret = 0.001 * imb + rng.normal(0, 0.0005)
        snaps.append(_snap(bid_qty=bid_q, ask_qty=ask_q, spread=0.10, timestamp=base + timedelta(seconds=i)))
        rets.append(ret)
    ds = stack_lob_features(snaps, rets)
    art, _ = train_imbalance_forecaster(dataset=ds)
    fresh = _snap(bid_qty=4.0, ask_qty=0.8, timestamp=datetime.now(timezone.utc))
    res = score_orderbook(fresh, art, max_staleness_seconds=60.0)
    assert res.used is True
    assert 0.0 <= (res.probability_up or 0.0) <= 1.0


def test_artefact_save_load_roundtrip(tmp_path: Path) -> None:
    rng = np.random.default_rng(2)
    snaps = [
        _snap(
            bid_qty=float(rng.uniform(0.5, 5.0)),
            ask_qty=float(rng.uniform(0.5, 5.0)),
            timestamp=datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=i),
        )
        for i in range(120)
    ]
    rets = [0.001 if rng.random() > 0.5 else -0.001 for _ in range(120)]
    ds = stack_lob_features(snaps, rets)
    art, _ = train_imbalance_forecaster(dataset=ds)
    out = tmp_path / "lob.pkl"
    art.save(out)
    loaded = TrainedLOBForecaster.load(out)
    np.testing.assert_allclose(
        loaded.predict(ds.X.head(3)),
        art.predict(ds.X.head(3)),
    )


def test_artefact_load_detects_feature_hash_tampering(tmp_path: Path) -> None:
    rng = np.random.default_rng(3)
    snaps = [
        _snap(
            bid_qty=float(rng.uniform(0.5, 5.0)),
            ask_qty=float(rng.uniform(0.5, 5.0)),
            timestamp=datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=i),
        )
        for i in range(120)
    ]
    rets = [0.001 if rng.random() > 0.5 else -0.001 for _ in range(120)]
    ds = stack_lob_features(snaps, rets)
    art, _ = train_imbalance_forecaster(dataset=ds)
    out = tmp_path / "tampered.pkl"
    art.feature_contract_hash = "0" * 64
    with open(out, "wb") as f:
        pickle.dump(art, f)
    with pytest.raises(ValueError, match="hash mismatch"):
        TrainedLOBForecaster.load(out)


# ── config ─────────────────────────────────────────────────────────────────


def test_default_microstructure_yaml_loads_disabled() -> None:
    raw = yaml.safe_load(Path("config/microstructure.yaml").read_text(encoding="utf-8"))
    assert (raw.get("microstructure") or {}).get("enabled") is False
    assert "crypto" in (raw.get("microstructure") or {}).get("asset_classes", [])
