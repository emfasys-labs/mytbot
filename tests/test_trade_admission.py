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
from storage.models import Base, FillLog, TradeAdmissionLog


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
    svc = TradeAdmissionService(AdmissionConfig(enabled=True, shadow_only=False, block_new_opens=True))
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
        cand, feats, AdmissionConfig(shadow_only=False, block_new_opens=True), model
    )
    assert decision.action == AdmissionAction.REJECT
    assert decision.model_probability is not None

