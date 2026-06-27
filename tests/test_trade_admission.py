from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from intelligence.trade_admission.feature_builder import build_features
from intelligence.trade_admission.ledger import label_due_outcomes
from intelligence.trade_admission.model import AdmissionModel
from intelligence.trade_admission.policy import decide_admission
from intelligence.trade_admission.schema import (
    AdmissionAction,
    AdmissionCandidate,
    AdmissionConfig,
)
from intelligence.trade_admission.service import TradeAdmissionService
from storage.models import Base, FeatureSnapshot, FillLog, PositionLog, PriceHistory, TradeAdmissionLog


@pytest_asyncio.fixture
async def sf():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    yield maker
    await engine.dispose()


def _signal(**metadata):
    return SimpleNamespace(
        signal_id="sig-1",
        symbol="AAPL",
        strategy="momentum_breakout",
        side="buy",
        broker="ibkr",
        asset_class="equity",
        confidence=Decimal("0.72"),
        suggested_quantity=Decimal("10"),
        suggested_price=Decimal("100"),
        metadata=dict(metadata),
    )


@pytest.mark.asyncio
async def test_trade_admission_shadow_logs_without_blocking(sf):
    svc = TradeAdmissionService(
        AdmissionConfig(enabled=True, shadow_only=True, block_new_opens=False)
    )
    sig = _signal(trade_quality_score="0.63", volume_z_score="2.0")

    decision = await svc.evaluate_signal(
        sig,
        session_factory=sf,
        portfolio_state={"portfolio_value": Decimal("100000"), "positions": {}},
        loop_iteration=7,
        source_path="test",
    )

    assert decision.action in {AdmissionAction.ALLOW, AdmissionAction.REQUIRE_MORE_EVIDENCE}
    assert not svc.should_block(decision)
    assert sig.metadata["trade_admission_id"]
    async with sf() as session:
        rows = (await session.execute(select(TradeAdmissionLog))).scalars().all()
    assert len(rows) == 1
    assert rows[0].symbol == "AAPL"
    assert rows[0].shadow_only is True
    assert rows[0].suggested_notional == Decimal("1000")


@pytest.mark.asyncio
async def test_trade_admission_preserves_reduce_only(sf):
    svc = TradeAdmissionService(
        AdmissionConfig(
            enabled=True,
            shadow_only=False,
            block_new_opens=True,
            allow_size_haircuts=True,
        )
    )
    sig = _signal(reduce_only=True, meta_label_kept=False)

    decision = await svc.evaluate_signal(
        sig,
        session_factory=sf,
        portfolio_state={"portfolio_value": Decimal("100000"), "positions": {}},
        loop_iteration=1,
        source_path="test",
    )

    assert decision.action == AdmissionAction.ALLOW
    assert decision.reason == "reduce_only_preserved"
    assert not svc.should_block(decision)


@pytest.mark.asyncio
async def test_trade_admission_can_block_bad_new_open_when_active(sf):
    svc = TradeAdmissionService(AdmissionConfig(enabled=True, shadow_only=False, block_new_opens=True))
    sig = _signal(meta_label_kept=False)

    decision = await svc.evaluate_signal(
        sig,
        session_factory=sf,
        portfolio_state={"portfolio_value": Decimal("100000"), "positions": {}},
        loop_iteration=2,
        source_path="test",
    )

    assert decision.action == AdmissionAction.REJECT
    assert svc.should_block(decision)


@pytest.mark.asyncio
async def test_trade_admission_does_not_block_shadow_meta_label_for_allocator(sf):
    svc = TradeAdmissionService(AdmissionConfig(enabled=True, shadow_only=False, block_new_opens=True))
    sig = _signal(
        meta_label_kept=False,
        meta_label_shadow=True,
        confidence="0.9",
        accumulator_score="0.4",
    )

    decision = await svc.evaluate_signal(
        sig,
        session_factory=sf,
        portfolio_state={"portfolio_value": Decimal("100000"), "positions": {}},
        loop_iteration=2,
        source_path="global",
    )

    assert decision.reason != "prior_trade_filter_drop"
    assert not svc.should_block(decision)


@pytest.mark.asyncio
async def test_trade_admission_allows_pressure_relief_meta_haircut(sf):
    svc = TradeAdmissionService(AdmissionConfig(enabled=True, shadow_only=False, block_new_opens=True))
    sig = _signal(
        meta_label_kept=True,
        meta_label_model_kept=False,
        meta_label_reason="pressure_relief_size_haircut",
        meta_label_pressure_relief=True,
        meta_label_size_multiplier="0.25",
        confidence="0.9",
        accumulator_score="0.4",
    )

    decision = await svc.evaluate_signal(
        sig,
        session_factory=sf,
        portfolio_state={"portfolio_value": Decimal("100000"), "positions": {}},
        loop_iteration=2,
        source_path="global",
    )

    assert decision.reason != "prior_trade_filter_drop"
    assert not svc.should_block(decision)


@pytest.mark.asyncio
async def test_trade_admission_sizes_down_long_against_negative_direct_news(sf):
    svc = TradeAdmissionService(
        AdmissionConfig(
            enabled=True,
            shadow_only=False,
            block_new_opens=True,
            allow_size_haircuts=True,
        )
    )
    sig = _signal(ai_news_score="-0.60", news_score="0.95", confidence="0.95")

    decision = await svc.evaluate_signal(
        sig,
        session_factory=sf,
        portfolio_state={"portfolio_value": Decimal("100000"), "positions": {}},
        loop_iteration=2,
        source_path="test",
    )

    assert decision.action == AdmissionAction.ALLOW_SMALLER
    assert decision.reason == "directional_news_size_adjustment"
    assert decision.size_multiplier == Decimal("0.60")
    assert not svc.should_block(decision)


def test_trade_admission_does_not_size_down_neutral_direct_news():
    candidate = AdmissionCandidate(
        id="adm-neutral",
        timestamp=datetime.now(timezone.utc),
        loop_iteration=1,
        signal_id="sig-neutral",
        symbol="ETH-USD",
        strategy="mean_reversion",
        side="buy",
        broker="kraken",
        asset_class="crypto",
        suggested_quantity=Decimal("1"),
        suggested_price=Decimal("100"),
        suggested_notional=Decimal("100"),
        is_reduce_only=False,
        metadata={
            "confidence": "0.9",
            "trade_quality_score": "0.7",
            "accumulator_score": "0.4",
            "ai_news_score": "0.0",
            "volume_z_score": "1.0",
        },
        source_path="test",
    )
    features = build_features(candidate, {"portfolio_value": Decimal("100000"), "positions": {}})

    decision = decide_admission(
        candidate,
        features,
        AdmissionConfig(
            enabled=True,
            shadow_only=False,
            block_new_opens=True,
            allow_size_haircuts=True,
            directional_news_weight=Decimal("1"),
            model_enabled=False,
        ),
    )

    assert decision.action == AdmissionAction.ALLOW
    assert decision.reason == "admission_ok"
    assert decision.size_multiplier is None


@pytest.mark.asyncio
async def test_trade_admission_preserves_negative_news_reduce_only_exit(sf):
    svc = TradeAdmissionService(AdmissionConfig(enabled=True, shadow_only=False, block_new_opens=True))
    sig = _signal(reduce_only=True, ai_news_score="-0.60", news_score="0.95")

    decision = await svc.evaluate_signal(
        sig,
        session_factory=sf,
        portfolio_state={"portfolio_value": Decimal("100000"), "positions": {"AAPL": {"quantity": 10}}},
        loop_iteration=2,
        source_path="test",
    )

    assert decision.action == AdmissionAction.ALLOW
    assert decision.reason == "reduce_only_preserved"


@pytest.mark.asyncio
async def test_label_due_outcomes_matches_fill_by_signal_id(sf):
    svc = TradeAdmissionService(AdmissionConfig(enabled=True, shadow_only=True))
    sig = _signal(trade_quality_score="0.7", volume_z_score="1.0")
    await svc.evaluate_signal(
        sig,
        session_factory=sf,
        portfolio_state={"portfolio_value": Decimal("100000"), "positions": {}},
        loop_iteration=3,
        source_path="test",
    )
    async with sf() as session:
        row = (await session.execute(select(TradeAdmissionLog))).scalar_one()
        row.timestamp = datetime.now(timezone.utc) - timedelta(minutes=90)
        session.add(
            FillLog(
                # Closing fill lands ~10 min after admission, inside the 60m horizon.
                timestamp=datetime.now(timezone.utc) - timedelta(minutes=80),
                broker="ibkr",
                symbol="AAPL",
                asset_class="equity",
                side="sell",
                order_type="market",
                quantity=Decimal("10"),
                signed_quantity=Decimal("-10"),
                fill_price=Decimal("103"),
                notional=Decimal("1030"),
                fee=Decimal("1"),
                realised_pnl=Decimal("30"),
                position_qty_after=Decimal("0"),
                signal_id="sig-1",
            )
        )
        await session.commit()

    updated = await label_due_outcomes(sf, horizons_minutes=(60,))

    assert updated == 1
    async with sf() as session:
        row = (await session.execute(select(TradeAdmissionLog))).scalar_one()
    assert row.outcome_label == "positive"
    assert row.outcome_net_pnl == Decimal("29")
    assert isinstance(row.outcome_horizons, dict)
    assert "60" in row.outcome_horizons
    assert isinstance(row.outcome_labels, dict)
    assert row.outcome_labels.get("better_than_book_holding") is True


@pytest.mark.asyncio
async def test_label_due_outcomes_uses_first_mature_horizon_for_fast_learning(sf):
    svc = TradeAdmissionService(AdmissionConfig(enabled=True, shadow_only=True))
    sig = _signal(trade_quality_score="0.7", volume_z_score="1.0")
    await svc.evaluate_signal(
        sig,
        session_factory=sf,
        portfolio_state={"portfolio_value": Decimal("100000"), "positions": {}},
        loop_iteration=3,
        source_path="test",
    )
    async with sf() as session:
        row = (await session.execute(select(TradeAdmissionLog))).scalar_one()
        row.timestamp = datetime.now(timezone.utc) - timedelta(minutes=90)
        session.add(
            FillLog(
                timestamp=datetime.now(timezone.utc) - timedelta(minutes=80),
                broker="ibkr",
                symbol="AAPL",
                asset_class="equity",
                side="sell",
                order_type="market",
                quantity=Decimal("10"),
                signed_quantity=Decimal("-10"),
                fill_price=Decimal("103"),
                notional=Decimal("1030"),
                fee=Decimal("1"),
                realised_pnl=Decimal("30"),
                position_qty_after=Decimal("0"),
                signal_id="sig-1",
            )
        )
        await session.commit()

    updated = await label_due_outcomes(sf, horizons_minutes=(60, 240, 1440))

    assert updated == 1
    async with sf() as session:
        row = (await session.execute(select(TradeAdmissionLog))).scalar_one()
    assert row.outcome_label == "positive"
    assert set(row.outcome_horizons) == {"60"}
    assert row.outcome_labels["outcome_maturity_minutes"] == 60


@pytest.mark.asyncio
async def test_label_due_outcomes_marks_open_trade_to_market(sf):
    svc = TradeAdmissionService(AdmissionConfig(enabled=True, shadow_only=True))
    sig = _signal(trade_quality_score="0.7", volume_z_score="1.0")
    await svc.evaluate_signal(
        sig,
        session_factory=sf,
        portfolio_state={"portfolio_value": Decimal("100000"), "positions": {}},
        loop_iteration=3,
        source_path="test",
    )
    async with sf() as session:
        row = (await session.execute(select(TradeAdmissionLog))).scalar_one()
        row.timestamp = datetime.now(timezone.utc) - timedelta(minutes=90)
        session.add(
            FillLog(
                timestamp=datetime.now(timezone.utc) - timedelta(minutes=80),
                broker="ibkr",
                symbol="AAPL",
                asset_class="equity",
                side="buy",
                order_type="market",
                quantity=Decimal("10"),
                signed_quantity=Decimal("10"),
                fill_price=Decimal("100"),
                notional=Decimal("1000"),
                fee=Decimal("1"),
                realised_pnl=Decimal("0"),
                position_qty_after=Decimal("10"),
                signal_id="sig-1",
            )
        )
        session.add(
            PriceHistory(
                timestamp=datetime.now(timezone.utc) - timedelta(minutes=40),
                symbol="AAPL",
                timeframe="1m",
                broker="test",
                open=Decimal("104"),
                high=Decimal("106"),
                low=Decimal("103"),
                close=Decimal("105"),
                volume=Decimal("1000"),
            )
        )
        await session.commit()

    updated = await label_due_outcomes(sf, horizons_minutes=(60,))

    assert updated == 1
    async with sf() as session:
        row = (await session.execute(select(TradeAdmissionLog))).scalar_one()
    assert row.outcome_label == "positive"
    assert row.outcome_net_pnl == Decimal("49.0")
    assert row.outcome_labels["mark_to_market_used"] is True


@pytest.mark.asyncio
async def test_label_due_outcomes_marks_to_market_from_feature_snapshot(sf):
    svc = TradeAdmissionService(AdmissionConfig(enabled=True, shadow_only=True))
    sig = _signal(trade_quality_score="0.7", volume_z_score="1.0")
    await svc.evaluate_signal(
        sig,
        session_factory=sf,
        portfolio_state={"portfolio_value": Decimal("100000"), "positions": {}},
        loop_iteration=3,
        source_path="test",
    )
    async with sf() as session:
        row = (await session.execute(select(TradeAdmissionLog))).scalar_one()
        row.timestamp = datetime.now(timezone.utc) - timedelta(minutes=90)
        session.add(
            FillLog(
                timestamp=datetime.now(timezone.utc) - timedelta(minutes=80),
                broker="ibkr",
                symbol="AAPL",
                asset_class="equity",
                side="buy",
                order_type="market",
                quantity=Decimal("10"),
                signed_quantity=Decimal("10"),
                fill_price=Decimal("100"),
                notional=Decimal("1000"),
                fee=Decimal("1"),
                realised_pnl=Decimal("0"),
                position_qty_after=Decimal("10"),
                signal_id="sig-1",
            )
        )
        session.add(
            FeatureSnapshot(
                symbol="AAPL",
                timeframe="1m",
                bar_timestamp=datetime.now(timezone.utc) - timedelta(minutes=40),
                open=Decimal("104"),
                high=Decimal("106"),
                low=Decimal("103"),
                close=Decimal("105"),
                volume=Decimal("1000"),
                features={},
            )
        )
        await session.commit()

    updated = await label_due_outcomes(sf, horizons_minutes=(60,))

    assert updated == 1
    async with sf() as session:
        row = (await session.execute(select(TradeAdmissionLog))).scalar_one()
    assert row.outcome_label == "positive"
    assert row.outcome_net_pnl == Decimal("49.0")
    assert row.outcome_labels["mark_to_market_used"] is True


@pytest.mark.asyncio
async def test_label_due_outcomes_marks_to_market_from_position_log(sf):
    svc = TradeAdmissionService(AdmissionConfig(enabled=True, shadow_only=True))
    sig = _signal(trade_quality_score="0.7", volume_z_score="1.0")
    await svc.evaluate_signal(
        sig,
        session_factory=sf,
        portfolio_state={"portfolio_value": Decimal("100000"), "positions": {}},
        loop_iteration=3,
        source_path="test",
    )
    async with sf() as session:
        row = (await session.execute(select(TradeAdmissionLog))).scalar_one()
        row.timestamp = datetime.now(timezone.utc) - timedelta(minutes=90)
        session.add(
            FillLog(
                timestamp=datetime.now(timezone.utc) - timedelta(minutes=80),
                broker="ibkr",
                symbol="AAPL",
                asset_class="equity",
                side="buy",
                order_type="market",
                quantity=Decimal("10"),
                signed_quantity=Decimal("10"),
                fill_price=Decimal("100"),
                notional=Decimal("1000"),
                fee=Decimal("1"),
                realised_pnl=Decimal("0"),
                position_qty_after=Decimal("10"),
                signal_id="sig-1",
            )
        )
        session.add(
            PositionLog(
                timestamp=datetime.now(timezone.utc) - timedelta(minutes=40),
                broker="ibkr",
                symbol="AAPL",
                asset_class="equity",
                quantity=Decimal("10"),
                avg_entry_price=Decimal("100"),
                current_price=Decimal("105"),
                unrealised_pnl=Decimal("50"),
            )
        )
        await session.commit()

    updated = await label_due_outcomes(sf, horizons_minutes=(60,))

    assert updated == 1
    async with sf() as session:
        row = (await session.execute(select(TradeAdmissionLog))).scalar_one()
    assert row.outcome_label == "positive"
    assert row.outcome_net_pnl == Decimal("49.0")
    assert row.outcome_labels["mark_to_market_used"] is True
    assert row.outcome_labels["horizon_price"] == 105.0


@pytest.mark.asyncio
async def test_label_due_outcomes_uses_actual_fill_broker_after_reroute(sf):
    svc = TradeAdmissionService(AdmissionConfig(enabled=True, shadow_only=True))
    sig = _signal()
    sig.symbol = "BTC-USD"
    sig.broker = "kraken"
    sig.asset_class = "crypto"
    await svc.evaluate_signal(
        sig,
        session_factory=sf,
        portfolio_state={"portfolio_value": Decimal("100000"), "positions": {}},
        loop_iteration=3,
        source_path="test",
    )
    async with sf() as session:
        row = (await session.execute(select(TradeAdmissionLog))).scalar_one()
        row.timestamp = datetime.now(timezone.utc) - timedelta(minutes=90)
        row.signal_id = "sig-rerouted"
        session.add(
            FillLog(
                timestamp=datetime.now(timezone.utc) - timedelta(minutes=80),
                broker="binance",
                symbol="BTC-USD",
                asset_class="crypto",
                side="buy",
                order_type="market",
                quantity=Decimal("10"),
                signed_quantity=Decimal("10"),
                fill_price=Decimal("100"),
                notional=Decimal("1000"),
                fee=Decimal("1"),
                realised_pnl=Decimal("0"),
                position_qty_after=Decimal("10"),
                signal_id="sig-rerouted",
            )
        )
        session.add(
            PositionLog(
                timestamp=datetime.now(timezone.utc) - timedelta(minutes=40),
                broker="binance",
                symbol="BTC-USD",
                asset_class="crypto",
                quantity=Decimal("10"),
                avg_entry_price=Decimal("100"),
                current_price=Decimal("105"),
                unrealised_pnl=Decimal("50"),
            )
        )
        await session.commit()

    updated = await label_due_outcomes(sf, horizons_minutes=(60,))

    assert updated == 1
    async with sf() as session:
        row = (await session.execute(select(TradeAdmissionLog))).scalar_one()
    assert row.outcome_label == "positive"
    assert row.outcome_net_pnl == Decimal("49.0")
    assert row.outcome_labels["mark_to_market_used"] is True
    assert row.outcome_labels["horizon_price"] == 105.0


@pytest.mark.asyncio
async def test_label_due_outcomes_does_not_train_fee_only_open_without_price(sf):
    svc = TradeAdmissionService(AdmissionConfig(enabled=True, shadow_only=True))
    sig = _signal(trade_quality_score="0.7", volume_z_score="1.0")
    await svc.evaluate_signal(
        sig,
        session_factory=sf,
        portfolio_state={"portfolio_value": Decimal("100000"), "positions": {}},
        loop_iteration=3,
        source_path="test",
    )
    async with sf() as session:
        row = (await session.execute(select(TradeAdmissionLog))).scalar_one()
        row.timestamp = datetime.now(timezone.utc) - timedelta(minutes=90)
        session.add(
            FillLog(
                timestamp=datetime.now(timezone.utc) - timedelta(minutes=80),
                broker="ibkr",
                symbol="AAPL",
                asset_class="equity",
                side="buy",
                order_type="market",
                quantity=Decimal("10"),
                signed_quantity=Decimal("10"),
                fill_price=Decimal("100"),
                notional=Decimal("1000"),
                fee=Decimal("1"),
                realised_pnl=Decimal("0"),
                position_qty_after=Decimal("10"),
                signal_id="sig-1",
            )
        )
        await session.commit()

    updated = await label_due_outcomes(sf, horizons_minutes=(60,))

    assert updated == 1
    async with sf() as session:
        row = (await session.execute(select(TradeAdmissionLog))).scalar_one()
    assert row.outcome_label == "unpriced"
    assert row.outcome_net_pnl is None


def _candidate(**md):
    return AdmissionCandidate(
        id="c1",
        timestamp=datetime.now(timezone.utc),
        loop_iteration=1,
        symbol="AAPL",
        strategy="momentum_breakout",
        side="buy",
        broker="ibkr",
        asset_class="equity",
        signal_id="sig-1",
        source_path="test",
        suggested_notional=Decimal("1000"),
        suggested_quantity=Decimal("10"),
        suggested_price=Decimal("100"),
        is_reduce_only=False,
        metadata=dict(md),
    )


def test_close_only_book_emits_close_only():
    cand = _candidate(confidence="0.7")
    feats = build_features(cand, {"portfolio_value": Decimal("100000"), "positions": {}, "kill_switch_active": True})
    decision = decide_admission(cand, feats, AdmissionConfig(shadow_only=False, block_new_opens=True))
    assert decision.action == AdmissionAction.CLOSE_ONLY
    assert decision.active_applied is True


def test_shadow_microstructure_label_feeds_active_admission_reject():
    cand = _candidate(
        confidence="0.8",
        trade_quality_score="0.8",
        microstructure_shadow_label="high_risk",
    )
    feats = build_features(cand, {"portfolio_value": Decimal("100000"), "positions": {}})
    decision = decide_admission(
        cand,
        feats,
        AdmissionConfig(shadow_only=False, block_new_opens=True),
    )
    assert feats.values["microstructure_label"] == "high_risk"
    assert decision.action == AdmissionAction.REJECT
    assert decision.active_applied is True
    assert decision.reason == "microstructure_high_risk"


def test_admission_model_abstains_when_thin():
    model = AdmissionModel.from_outcomes(
        [{"strategy": "s", "asset_class": "equity", "score": Decimal("0.5"), "win": True}],
        min_samples=25,
    )
    ms = model.evaluate(strategy="s", asset_class="equity", score=Decimal("0.5"))
    assert ms.abstain is True


def test_admission_model_flags_below_base_bucket():
    rows = []
    # Strong bucket: momentum/equity wins; weak bucket: meanrev/crypto loses.
    for _ in range(40):
        rows.append({"strategy": "mom", "asset_class": "equity", "score": Decimal("0.8"), "win": True})
    for _ in range(40):
        rows.append({"strategy": "mr", "asset_class": "crypto", "score": Decimal("0.2"), "win": False})
    model = AdmissionModel.from_outcomes(rows, min_samples=10)
    weak = model.evaluate(strategy="mr", asset_class="crypto", score=Decimal("0.2"))
    assert weak.abstain is False
    assert weak.probability < weak.base_rate

    cand = _candidate(confidence="0.2", trade_quality_score="0.2", accumulator_score="0.0")
    cand = AdmissionCandidate(**{**cand.__dict__, "strategy": "mr", "asset_class": "crypto"})
    feats = build_features(cand, {"portfolio_value": Decimal("100000"), "positions": {}})
    decision = decide_admission(
        cand,
        feats,
        AdmissionConfig(
            shadow_only=False,
            block_new_opens=True,
            allow_size_haircuts=True,
        ),
        model,
    )
    assert decision.action == AdmissionAction.ALLOW_SMALLER
    assert decision.size_multiplier is not None
    assert Decimal("0") < decision.size_multiplier < Decimal("1")
    assert decision.model_probability is not None


def test_admission_model_uses_strategy_asset_pool_for_sparse_score_band():
    rows = [
        {
            "strategy": "portfolio_orchestrator",
            "asset_class": "crypto",
            "score": Decimal("0.8"),
            "win": i < 4,
            "outcome_return": Decimal("0.01") if i < 4 else Decimal("-0.01"),
        }
        for i in range(30)
    ]
    rows.extend(
        {
            "strategy": "other",
            "asset_class": "equity",
            "score": Decimal("0.2"),
            "win": True,
            "outcome_return": Decimal("0.01"),
        }
        for _ in range(30)
    )
    model = AdmissionModel.from_outcomes(rows, min_samples=25)

    score = model.evaluate(
        strategy="portfolio_orchestrator",
        asset_class="crypto",
        score=Decimal("0.2"),
    )

    assert score.abstain is False
    assert score.bucket == "portfolio_orchestrator|crypto|all"
    assert score.samples == 30
    assert score.size_multiplier is not None
    assert Decimal("0") < score.size_multiplier < Decimal("1")


@pytest.mark.asyncio
async def test_upstream_outcome_target_is_not_haircut_twice(sf):
    svc = TradeAdmissionService(
        AdmissionConfig(
            enabled=True,
            shadow_only=False,
            block_new_opens=True,
            allow_size_haircuts=True,
            model_min_bucket_samples=10,
        )
    )
    svc._model = AdmissionModel.from_outcomes(  # noqa: SLF001 - focused wiring test
        [
            {
                "strategy": "portfolio_orchestrator",
                "asset_class": "crypto",
                "score": Decimal("0.5"),
                "win": False,
                "outcome_return": Decimal("-0.01"),
            }
            for _ in range(20)
        ],
        min_samples=10,
    )
    sig = _signal(
        trade_admission_target_multiplier_applied=True,
        confidence="0.5",
        trade_quality_score="0.5",
    )
    sig.strategy = "portfolio_orchestrator"
    sig.asset_class = "crypto"
    original_quantity = sig.suggested_quantity

    decision = await svc.evaluate_signal(
        sig,
        session_factory=sf,
        portfolio_state={"portfolio_value": Decimal("100000"), "positions": {}},
        loop_iteration=1,
        source_path="test",
    )

    assert decision.action == AdmissionAction.ALLOW_SMALLER
    assert sig.suggested_quantity == original_quantity
    assert sig.metadata["trade_admission_size_applied_upstream"] is True
