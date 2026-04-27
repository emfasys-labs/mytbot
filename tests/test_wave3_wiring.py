"""
tests/test_wave3_wiring.py
============================
Wave 3 (wiring) — verify the factor sleeve plugs into
``build_opportunities_async`` without changing default behaviour.

Coverage:

1. ``load_close_series`` reads the latest N bars from
   ``feature_snapshots`` in chronological order.
2. ``collect_factor_sleeve_candidates`` returns [] when the sleeve is
   disabled, even with valid data.
3. ``collect_factor_sleeve_candidates`` produces ``SignalCandidate``s
   when enabled, with the ``factor_sleeve`` metadata flag.
4. ``build_opportunities_async`` is a no-op when the sleeve is disabled
   (default).
5. ``build_opportunities_async`` merges factor candidates into the
   signal stream when the sleeve config is supplied with ``enabled=true``.
6. Duplicate (symbol, side, strategy_name) candidates are not added
   twice when the per-strategy stream already has them.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import numpy as np
import pandas as pd
import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from config.loaders import load_allocation
from core.models_runtime import (
    MarketStateComponents,
    RegimeState,
    SignalCandidate,
)
from signals.opportunity_engine import (
    build_opportunities_async,
    reset_factor_sleeve_cache,
)
from storage.models import Base, FeatureSnapshot
from strategies.factor_sleeve import FactorSleeveConfig
from strategies.factor_sleeve_runner import (
    collect_factor_sleeve_candidates,
    load_close_series,
)


# ── fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture
async def memory_session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


async def _seed_feature_snapshots(
    factory,
    *,
    symbol: str,
    timeframe: str = "1d",
    n: int = 250,
    drift: float = 0.0008,
    seed: int = 0,
) -> None:
    rng = np.random.default_rng(seed)
    rets = rng.normal(drift, 0.012, n)
    px = 100.0 * np.exp(np.cumsum(rets))
    start = datetime(2025, 1, 1, tzinfo=timezone.utc)
    rows = []
    for i, p in enumerate(px):
        ts = start + timedelta(days=i)
        rows.append(
            FeatureSnapshot(
                symbol=symbol,
                timeframe=timeframe,
                bar_timestamp=ts,
                open=Decimal(str(p)),
                high=Decimal(str(p * 1.005)),
                low=Decimal(str(p * 0.995)),
                close=Decimal(str(p)),
                volume=Decimal("1000"),
                features={},
            )
        )
    async with factory() as session:
        for r in rows:
            session.add(r)
        await session.commit()


def _trivial_regime() -> RegimeState:
    return RegimeState(
        timestamp=datetime.now(timezone.utc),
        regime_label="trend",  # type: ignore[arg-type]
        market_state_score=Decimal("0.5"),
        drawdown_throttle=Decimal("1.0"),
        execution_quality=Decimal("1.0"),
        breadth_score=Decimal("0.0"),
        components=MarketStateComponents(),
        metadata={"demand_score": 0.0},
    )


# ── 1. load_close_series ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_load_close_series_returns_chronological(memory_session) -> None:
    factory = memory_session
    await _seed_feature_snapshots(factory, symbol="AAA", n=50, seed=1)
    async with factory() as session:
        s = await load_close_series(session, "AAA", timeframe="1d", lookback_bars=30)
    assert s is not None
    assert len(s) == 30
    assert s.index.is_monotonic_increasing


@pytest.mark.asyncio
async def test_load_close_series_returns_none_when_no_rows(memory_session) -> None:
    factory = memory_session
    async with factory() as session:
        s = await load_close_series(session, "GHOST", timeframe="1d", lookback_bars=30)
    assert s is None


# ── 2. runner gating ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_collect_factor_sleeve_candidates_disabled_returns_empty(memory_session) -> None:
    factory = memory_session
    for sym in ("AAA", "BBB", "CCC"):
        await _seed_feature_snapshots(factory, symbol=sym, n=200, seed=hash(sym) & 0xFF)
    cfg = FactorSleeveConfig(enabled=False, long_top_n=2)
    async with factory() as session:
        cands = await collect_factor_sleeve_candidates(
            session,
            ["AAA", "BBB", "CCC"],
            timeframe="1d",
            lookback_bars=200,
            config=cfg,
        )
    assert cands == []


# ── 3. runner emits when enabled ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_collect_factor_sleeve_candidates_enabled_emits(memory_session) -> None:
    factory = memory_session
    # Seed with different drifts so the cross-section has spread.
    for sym, d in (("AAA", 0.002), ("BBB", 0.0005), ("CCC", -0.001), ("DDD", 0.0)):
        await _seed_feature_snapshots(factory, symbol=sym, n=260, drift=d, seed=hash(sym) & 0xFF)
    cfg = FactorSleeveConfig(
        enabled=True,
        long_top_n=2,
        short_bottom_n=0,
        neutralise_by_asset_class=False,
    )
    async with factory() as session:
        cands = await collect_factor_sleeve_candidates(
            session,
            ["AAA", "BBB", "CCC", "DDD"],
            timeframe="1d",
            lookback_bars=260,
            config=cfg,
            asset_class_for_symbol={s: "equity" for s in ("AAA", "BBB", "CCC", "DDD")},
        )
    assert len(cands) == 2
    for c in cands:
        assert c.metadata.get("factor_sleeve") is True
        assert c.strategy_name == "factor_sleeve"


@pytest.mark.asyncio
async def test_collect_factor_sleeve_candidates_skips_short_history(memory_session) -> None:
    factory = memory_session
    await _seed_feature_snapshots(factory, symbol="AAA", n=10, seed=0)
    await _seed_feature_snapshots(factory, symbol="BBB", n=10, seed=1)
    cfg = FactorSleeveConfig(enabled=True, long_top_n=2, neutralise_by_asset_class=False)
    async with factory() as session:
        cands = await collect_factor_sleeve_candidates(
            session, ["AAA", "BBB"], timeframe="1d", lookback_bars=200, config=cfg
        )
    # Both have <30 bars ⇒ universe empty ⇒ no candidates.
    assert cands == []


# ── 4. build_opportunities_async no-op when disabled ───────────────────────


@pytest.mark.asyncio
async def test_build_opportunities_async_disabled_factor_sleeve_unchanged(memory_session, monkeypatch) -> None:
    factory = memory_session
    # Reset the module cache so the test always exercises the load path.
    reset_factor_sleeve_cache()
    # Disabled cfg returned by the cache → no factor candidates injected.
    monkeypatch.setattr(
        "signals.opportunity_engine._get_default_factor_sleeve_config",
        lambda: FactorSleeveConfig(enabled=False),
    )

    cand = SignalCandidate(
        symbol="AAA",
        asset_class="equity",
        side="long",
        timestamp=datetime.now(timezone.utc),
        raw_signal_strength=Decimal("0.6"),
        adjusted_signal_strength=Decimal("0.6"),
        confidence=Decimal("0.6"),
        strategy_name="momentum",
        metadata={},
    )
    async with factory() as session:
        opps = await build_opportunities_async(
            signals=[cand],
            regime_state=_trivial_regime(),
            allocation_cfg=load_allocation(),
            session=session,
        )
    assert len(opps) == 1
    assert opps[0].metadata.get("strategy") == "momentum"
    # No factor metadata leaked in.
    assert "factor_sleeve" not in opps[0].metadata


# ── 5. build_opportunities_async injects factor candidates when enabled ─────


@pytest.mark.asyncio
async def test_build_opportunities_async_injects_factor_candidates(memory_session, monkeypatch) -> None:
    factory = memory_session
    for sym, d in (("AAA", 0.003), ("BBB", -0.002)):
        await _seed_feature_snapshots(factory, symbol=sym, n=260, drift=d, seed=hash(sym) & 0xFF)

    reset_factor_sleeve_cache()
    sleeve_cfg = FactorSleeveConfig(
        enabled=True,
        long_top_n=1,
        short_bottom_n=0,
        neutralise_by_asset_class=False,
    )
    monkeypatch.setattr(
        "signals.opportunity_engine._get_default_factor_sleeve_config",
        lambda: sleeve_cfg,
    )

    # Seed an unrelated strategy candidate to verify merging.
    seed_cand = SignalCandidate(
        symbol="AAA",
        asset_class="equity",
        side="long",
        timestamp=datetime.now(timezone.utc),
        raw_signal_strength=Decimal("0.5"),
        adjusted_signal_strength=Decimal("0.5"),
        confidence=Decimal("0.5"),
        strategy_name="momentum",
        metadata={},
    )
    async with factory() as session:
        opps = await build_opportunities_async(
            signals=[seed_cand],
            regime_state=_trivial_regime(),
            allocation_cfg=load_allocation(),
            session=session,
            timeframe="1d",
            factor_sleeve_lookback_bars=260,
            factor_sleeve_universe=["AAA", "BBB"],
            factor_sleeve_asset_class_for_symbol={"AAA": "equity", "BBB": "equity"},
        )

    # Two opportunities expected: original AAA momentum, plus AAA factor_sleeve
    # (top-1 with positive drift). BBB is below threshold for top-1 so doesn't
    # appear unless short_bottom_n > 0.
    strategies = sorted(o.metadata.get("strategy") for o in opps)
    assert "momentum" in strategies
    assert "factor_sleeve" in strategies


# ── 6. dedup ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_build_opportunities_async_dedups_factor_against_existing(memory_session, monkeypatch) -> None:
    factory = memory_session
    await _seed_feature_snapshots(factory, symbol="AAA", n=260, drift=0.003, seed=0)

    reset_factor_sleeve_cache()
    sleeve_cfg = FactorSleeveConfig(
        enabled=True,
        long_top_n=1,
        short_bottom_n=0,
        neutralise_by_asset_class=False,
        strategy_name="factor_sleeve",
    )
    monkeypatch.setattr(
        "signals.opportunity_engine._get_default_factor_sleeve_config",
        lambda: sleeve_cfg,
    )
    # Pre-existing factor_sleeve candidate for AAA/long: must not be duplicated.
    seed = SignalCandidate(
        symbol="AAA",
        asset_class="equity",
        side="long",
        timestamp=datetime.now(timezone.utc),
        raw_signal_strength=Decimal("0.5"),
        adjusted_signal_strength=Decimal("0.5"),
        confidence=Decimal("0.5"),
        strategy_name="factor_sleeve",
        metadata={"factor_sleeve": True},
    )
    async with factory() as session:
        opps = await build_opportunities_async(
            signals=[seed],
            regime_state=_trivial_regime(),
            allocation_cfg=load_allocation(),
            session=session,
            timeframe="1d",
            factor_sleeve_lookback_bars=260,
            factor_sleeve_universe=["AAA"],
            factor_sleeve_asset_class_for_symbol={"AAA": "equity"},
        )
    aaa_long = [o for o in opps if o.symbol == "AAA" and o.side == "long"]
    assert len(aaa_long) == 1


# ── 7. cache reset hygiene ─────────────────────────────────────────────────


def test_reset_factor_sleeve_cache_clears_loaded_value() -> None:
    # Load to populate.
    from signals.opportunity_engine import _get_default_factor_sleeve_config
    _ = _get_default_factor_sleeve_config()
    # Reset and verify next load returns the disabled YAML default.
    reset_factor_sleeve_cache()
    cfg = _get_default_factor_sleeve_config()
    assert cfg is None or cfg.enabled is False
