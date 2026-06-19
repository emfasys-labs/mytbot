"""D118 — Tests for priority-based candidate selection in UniverseBuilder."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from data.universe_builder import BuildTelemetry, UniverseBuilder
from data.universe_prefilter import COMPONENT_NAMES, PriorityBreakdown


def _bd(symbol: str, score: float, components: dict[str, float] | None = None) -> PriorityBreakdown:
    return PriorityBreakdown(
        symbol=symbol,
        priority_score=score,
        components=components or {name: 0.0 for name in COMPONENT_NAMES},
    )


class _FakeAdapter:
    def __init__(self, symbols: list[str]) -> None:
        self._symbols = symbols

    async def get_supported_symbols(self) -> list[str]:
        return list(self._symbols)


class _FakeBrokerManager:
    def __init__(self, by_broker: dict[str, list[str]]) -> None:
        self.adapters = {name: _FakeAdapter(syms) for name, syms in by_broker.items()}


@pytest.fixture
def fake_brokers() -> _FakeBrokerManager:
    return _FakeBrokerManager(
        {
            "ibkr": ["SPY", "AAPL", "MSFT"],
            "kraken": ["BTC-USD", "ETH-USD"],
        }
    )


@pytest.fixture(autouse=True)
def _no_yf(monkeypatch):
    """Replace the synchronous yfinance scorer with a deterministic stub.

    Tests must NOT hit the network; the stub returns a per-symbol value
    derived from the alphabetical order of the symbol so we can predict
    the resulting tier ordering exactly.
    """
    from data import universe_builder as ub

    def fake_score(sym: str) -> float:
        # SPY -> high, BTC-USD -> high-mid, AAPL -> mid, ETH-USD -> low-mid, MSFT -> low
        order = ["SPY", "BTC-USD", "AAPL", "ETH-USD", "MSFT"]
        if sym in order:
            return float(100 - order.index(sym) * 10)
        return 0.0

    monkeypatch.setattr(ub, "liquidity_score_for_symbol", fake_score)
    yield


# ---------------------------------------------------------------------------
# 1. Priority-driven selection wins over the legacy sampler
# ---------------------------------------------------------------------------


def test_priority_scores_drive_candidate_selection(tmp_path, fake_brokers):
    """When priority_scores is supplied, the top-N picks must be scored
    (and others must NOT be scored)."""

    # AAPL has the highest priority despite SPY's higher liquidity score.
    priority = {
        "AAPL": _bd("AAPL", 0.95),
        "SPY": _bd("SPY", 0.20),
        "MSFT": _bd("MSFT", 0.15),
        "BTC-USD": _bd("BTC-USD", 0.10),
        "ETH-USD": _bd("ETH-USD", 0.05),
    }
    builder = UniverseBuilder(
        max_symbols=300,
        ranking={
            "enabled": True,
            "core_max": 2,
            "scan_max": 1,
            "max_candidates_to_score": 5,
            "yf_concurrency": 4,
            "score_timeout_sec": 2.0,
            "tiers_path": str(tmp_path / "tiers.json"),
        },
    )
    telemetry = BuildTelemetry()
    tiers = asyncio.run(
        builder.build_tiered_universe(
            fake_brokers,
            priority_scores=priority,
            target_budget=2,  # only score top-2 by priority
            telemetry=telemetry,
        )
    )
    # Only AAPL and SPY should appear in tiers.scores (budget=2 hit those two).
    assert set(tiers.scores.keys()) == {"AAPL", "SPY"}
    # MSFT/BTC-USD/ETH-USD did not get scored this cycle.
    assert telemetry.picked == 2
    assert telemetry.scored == 2
    assert telemetry.candidates_considered == 5
    # picks_breakdowns must include the two picked symbols' priorities.
    assert set(telemetry.picks_breakdowns.keys()) == {"AAPL", "SPY"}


def test_priority_anchors_pinned_into_budget(tmp_path, fake_brokers):
    """Anchors must consume slots even when their priority is low."""
    priority = {
        "AAPL": _bd("AAPL", 0.95),
        "MSFT": _bd("MSFT", 0.80),
        "SPY": _bd("SPY", 0.05),  # very low priority
    }
    builder = UniverseBuilder(
        max_symbols=300,
        ranking={
            "enabled": True,
            "core_max": 1,
            "scan_max": 1,
            "max_candidates_to_score": 10,
            "yf_concurrency": 4,
            "tiers_path": str(tmp_path / "tiers.json"),
        },
    )
    telemetry = BuildTelemetry()
    tiers = asyncio.run(
        builder.build_tiered_universe(
            fake_brokers,
            priority_scores=priority,
            target_budget=2,
            anchors=["SPY"],
            telemetry=telemetry,
        )
    )
    # SPY is anchor-pinned even with priority 0.05.
    assert "SPY" in tiers.scores
    # The second slot goes to AAPL (highest non-anchor priority).
    assert "AAPL" in tiers.scores
    # MSFT did not make the cut.
    assert "MSFT" not in tiers.scores


def test_priority_anchors_pinned_into_active_tiers_after_liquidity_sort(tmp_path):
    """Pinned anchors that score poorly on liquidity must not fall to light."""
    brokers = _FakeBrokerManager(
        {
            "ibkr": ["SPY", "AAPL", "MSFT"],
            "kraken": ["BTC-USD", "ETH-USD"],
        }
    )
    priority = {
        "AAPL": _bd("AAPL", 0.95),
        "MSFT": _bd("MSFT", 0.90),
        "BTC-USD": _bd("BTC-USD", 0.85),
        "ETH-USD": _bd("ETH-USD", 0.80),
        "SPY": _bd("SPY", 0.01),
    }
    builder = UniverseBuilder(
        max_symbols=3,
        ranking={
            "enabled": True,
            "core_max": 1,
            "scan_max": 2,
            "max_candidates_to_score": 5,
            "yf_concurrency": 4,
            "tiers_path": str(tmp_path / "tiers.json"),
        },
    )

    tiers = asyncio.run(
        builder.build_tiered_universe(
            brokers,
            priority_scores=priority,
            target_budget=5,
            anchors=["SPY"],
        )
    )

    assert "SPY" in set(tiers.core) | set(tiers.scan)
    assert "SPY" not in tiers.light


# ---------------------------------------------------------------------------
# 2. Empty priority dict -> falls back to legacy stratified sampler
# ---------------------------------------------------------------------------


def test_no_priority_falls_back_to_stratified_sample(tmp_path, fake_brokers):
    builder = UniverseBuilder(
        max_symbols=300,
        ranking={
            "enabled": True,
            "core_max": 2,
            "scan_max": 2,
            "max_candidates_to_score": 3,  # force the sampler to truncate
            "yf_concurrency": 4,
            "tiers_path": str(tmp_path / "tiers.json"),
        },
    )
    telemetry = BuildTelemetry()
    tiers = asyncio.run(
        builder.build_tiered_universe(
            fake_brokers, telemetry=telemetry
        )
    )
    # Stratified sampler still produces a valid tier set.
    assert tiers is not None
    assert telemetry.picked > 0
    # No priority breakdowns when in fallback mode.
    assert telemetry.picks_breakdowns == {}


def test_priority_with_no_overlap_falls_back(tmp_path, fake_brokers):
    """If priority symbols are completely disjoint from the broker
    candidates, fall back to stratified sampling so we don't stall.
    The legacy sampler may pull in curated anchors from
    UniverseManager.INITIAL_UNIVERSE in addition to broker symbols."""
    priority = {f"GHOST{i}": _bd(f"GHOST{i}", 0.5) for i in range(10)}
    builder = UniverseBuilder(
        max_symbols=300,
        ranking={
            "enabled": True,
            "core_max": 1,
            "scan_max": 1,
            "max_candidates_to_score": 4,
            "yf_concurrency": 4,
            "tiers_path": str(tmp_path / "tiers.json"),
        },
    )
    telemetry = BuildTelemetry()
    tiers = asyncio.run(
        builder.build_tiered_universe(
            fake_brokers,
            priority_scores=priority,
            target_budget=4,
            telemetry=telemetry,
        )
    )
    # Fallback path engaged: tiers are populated and NO ghost symbols
    # leak through (those were never returned by the brokers and the
    # stub scorer returns 0.0 for them so they would not be in scores).
    assert len(tiers.scores) > 0
    assert not any(sym.startswith("GHOST") for sym in tiers.scores)
    # Telemetry must record that the priority path was NOT used (fallback
    # zeroes out picks_breakdowns).
    assert telemetry.picks_breakdowns == {}


# ---------------------------------------------------------------------------
# 3. Telemetry reports duration + deepest watching rank
# ---------------------------------------------------------------------------


def test_telemetry_reports_duration_and_max_watching_rank(tmp_path, fake_brokers):
    priority = {
        "AAPL": _bd("AAPL", 0.95),
        "SPY": _bd("SPY", 0.80),
        "MSFT": _bd("MSFT", 0.50),
        "BTC-USD": _bd("BTC-USD", 0.30),
        "ETH-USD": _bd("ETH-USD", 0.10),
    }
    builder = UniverseBuilder(
        max_symbols=300,
        ranking={
            "enabled": True,
            "core_max": 1,  # only 1 in core
            "scan_max": 2,  # 2 in scan -> watching set size 3
            "max_candidates_to_score": 10,
            "yf_concurrency": 4,
            "tiers_path": str(tmp_path / "tiers.json"),
        },
    )
    telemetry = BuildTelemetry()
    asyncio.run(
        builder.build_tiered_universe(
            fake_brokers,
            priority_scores=priority,
            target_budget=5,
            telemetry=telemetry,
        )
    )
    assert telemetry.measured_duration_sec >= 0.0
    # candidates sorted by priority desc:
    #   0: AAPL, 1: SPY, 2: MSFT, 3: BTC-USD, 4: ETH-USD
    # yfinance stub scores: SPY=100, BTC-USD=90, AAPL=80, ETH-USD=70, MSFT=60
    # Tier assignment (1 core + 2 scan): watching = {SPY, BTC-USD, AAPL}.
    # The DEEPEST watching member in the priority-ranked candidates is
    # BTC-USD at index 3 -> max_watching_rank = 4.
    assert telemetry.max_watching_rank == 4


# ---------------------------------------------------------------------------
# 4. Determinism: same inputs -> same selection
# ---------------------------------------------------------------------------


def test_priority_selection_is_deterministic(tmp_path, fake_brokers):
    priority = {
        "AAPL": _bd("AAPL", 0.95),
        "SPY": _bd("SPY", 0.80),
        "MSFT": _bd("MSFT", 0.50),
        "BTC-USD": _bd("BTC-USD", 0.30),
        "ETH-USD": _bd("ETH-USD", 0.10),
    }
    builder = UniverseBuilder(
        max_symbols=300,
        ranking={
            "enabled": True,
            "core_max": 2,
            "scan_max": 1,
            "max_candidates_to_score": 10,
            "yf_concurrency": 4,
            "tiers_path": str(tmp_path / "tiers.json"),
        },
    )
    tiers1 = asyncio.run(
        builder.build_tiered_universe(
            fake_brokers, priority_scores=priority, target_budget=3
        )
    )
    tiers2 = asyncio.run(
        builder.build_tiered_universe(
            fake_brokers, priority_scores=priority, target_budget=3
        )
    )
    assert tiers1.core == tiers2.core
    assert tiers1.scan == tiers2.scan
    assert tiers1.light == tiers2.light
