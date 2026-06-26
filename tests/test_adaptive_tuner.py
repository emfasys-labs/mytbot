import random
from datetime import datetime, timezone
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from intelligence.adaptive_tuner.optimizer import (
    attribute_and_propose,
    current_overrides,
    empty_state,
)
from intelligence.adaptive_tuner.registry import load_tuner_config
from intelligence.adaptive_tuner.schema import TunableParam, TunerConfig
from intelligence.adaptive_tuner.service import AdaptiveTunerService
from storage.models import Base, FillLog, ParameterTuningLog


def _cfg(**kw):
    params = (
        TunableParam("entry_conviction_threshold", "portfolio_orchestrator",
                     Decimal("0.08"), Decimal("0.40"), Decimal("0.02")),
        TunableParam("gross_target_pct.trader", "portfolio_orchestrator",
                     Decimal("0.50"), Decimal("1.20"), Decimal("0.05")),
    )
    base = dict(
        enabled=True, apply_every_n_cycles=1, exploration_rate=Decimal("0.0"),
        min_samples_to_exploit=2, regime_conditioned=True, params=params,
    )
    base.update(kw)
    return TunerConfig(**base)


def test_config_loads_from_yaml():
    cfg = load_tuner_config("config/adaptive_tuner.yaml")
    assert cfg.enabled
    keys = {p.key for p in cfg.params}
    assert "portfolio_orchestrator.entry_conviction_threshold" in keys


def test_proposals_stay_within_bounds():
    cfg = _cfg(exploration_rate=Decimal("1.0"))
    state = empty_state()
    defaults = {
        "portfolio_orchestrator.entry_conviction_threshold": Decimal("0.40"),
        "portfolio_orchestrator.gross_target_pct.trader": Decimal("0.50"),
    }
    rng = random.Random(1)
    for _ in range(50):
        state, proposals = attribute_and_propose(
            state, cfg, cfg.params, reward=Decimal("-1"), regime="risk_off",
            defaults=defaults, rng=rng,
        )
        for pr in proposals:
            p = next(x for x in cfg.params if x.key == pr.param_key)
            assert p.min_value <= pr.new_value <= p.max_value


def test_negative_reward_cannot_raise_risk_when_loss_guard_points_down():
    param = TunableParam(
        "gross_target_pct.trader", "portfolio_orchestrator",
        Decimal("0.50"), Decimal("1.20"), Decimal("0.05"),
        loss_guard_direction="down",
    )
    cfg = TunerConfig(
        enabled=True,
        apply_every_n_cycles=1,
        exploration_rate=Decimal("1.0"),
        min_samples_to_exploit=2,
        regime_conditioned=True,
        params=(param,),
    )
    state, proposals = attribute_and_propose(
        empty_state(),
        cfg,
        cfg.params,
        reward=Decimal("-0.01"),
        regime="risk_off",
        defaults={"portfolio_orchestrator.gross_target_pct.trader": Decimal("0.90")},
        ai_hints={"portfolio_orchestrator.gross_target_pct.trader": "up"},
        rng=random.Random(0),
    )

    assert proposals[0].new_value == Decimal("0.85")
    assert proposals[0].source.endswith("loss_guard")


def test_negative_reward_cannot_lower_bar_when_loss_guard_points_up():
    param = TunableParam(
        "entry_conviction_threshold", "portfolio_orchestrator",
        Decimal("0.08"), Decimal("0.40"), Decimal("0.02"),
        loss_guard_direction="up",
    )
    cfg = TunerConfig(
        enabled=True,
        apply_every_n_cycles=1,
        exploration_rate=Decimal("1.0"),
        min_samples_to_exploit=2,
        regime_conditioned=True,
        params=(param,),
    )
    state, proposals = attribute_and_propose(
        empty_state(),
        cfg,
        cfg.params,
        reward=Decimal("-0.01"),
        regime="risk_off",
        defaults={"portfolio_orchestrator.entry_conviction_threshold": Decimal("0.20")},
        ai_hints={"portfolio_orchestrator.entry_conviction_threshold": "down"},
        rng=random.Random(0),
    )

    assert proposals[0].new_value == Decimal("0.22")
    assert proposals[0].source.endswith("loss_guard")


def test_exploit_moves_toward_best_reward_bucket():
    cfg = _cfg(exploration_rate=Decimal("0.0"), min_samples_to_exploit=1)
    state = empty_state()
    defaults = {"portfolio_orchestrator.entry_conviction_threshold": Decimal("0.20")}
    params = (cfg.params[0],)
    cfg2 = TunerConfig(**{**cfg.__dict__, "params": params})
    rng = random.Random(0)
    # Seed a high reward at a higher value by manually crediting the bucket,
    # then confirm the optimizer steps upward toward it.
    state, _ = attribute_and_propose(
        state, cfg2, params, reward=Decimal("0"), regime="calm", defaults=defaults, rng=rng,
    )
    ps = state["params"]["portfolio_orchestrator.entry_conviction_threshold"]
    ps["buckets"]["calm"] = {"0.300000": {"sum": 5.0, "n": 3}}
    state["last_regime"] = "calm"
    before = ps["current"]["calm"]
    state, proposals = attribute_and_propose(
        state, cfg2, params, reward=Decimal("0"), regime="calm", defaults=defaults, rng=rng,
    )
    after = state["params"]["portfolio_orchestrator.entry_conviction_threshold"]["current"]["calm"]
    assert after >= before  # stepped toward the 0.30 best bucket


def test_current_overrides_resolves_namespace():
    cfg = _cfg()
    state = empty_state()
    defaults = {
        "portfolio_orchestrator.entry_conviction_threshold": Decimal("0.20"),
        "portfolio_orchestrator.gross_target_pct.trader": Decimal("0.90"),
    }
    state, _ = attribute_and_propose(
        state, cfg, cfg.params, reward=Decimal("0"), regime="calm", defaults=defaults,
        rng=random.Random(0),
    )
    ovr = current_overrides(state, cfg.params, "calm", cfg)
    assert "portfolio_orchestrator" in ovr
    assert "entry_conviction_threshold" in ovr["portfolio_orchestrator"]
    assert "gross_target_pct.trader" in ovr["portfolio_orchestrator"]


@pytest_asyncio.fixture
async def sf():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


@pytest.mark.asyncio
async def test_service_cycle_applies_and_logs(tmp_path, sf):
    cfg = _cfg(
        state_path=str(tmp_path / "tuner_state.json"),
        exploration_rate=Decimal("1.0"),
        ai_advisor_enabled=False,
    )
    svc = AdaptiveTunerService(cfg)
    svc.defaults = {
        "portfolio_orchestrator.entry_conviction_threshold": Decimal("0.20"),
        "portfolio_orchestrator.gross_target_pct.trader": Decimal("0.90"),
    }
    async with sf() as session:
        session.add(
            FillLog(
                timestamp=datetime.now(timezone.utc),
                broker="ibkr",
                symbol="AAPL",
                asset_class="equity",
                side="sell",
                order_type="market",
                quantity=Decimal("1"),
                signed_quantity=Decimal("-1"),
                fill_price=Decimal("101"),
                notional=Decimal("101"),
                fee=Decimal("1"),
                realised_pnl=Decimal("3"),
                position_qty_after=Decimal("0"),
            )
        )
        await session.commit()
    summary = await svc.maybe_run_cycle(sf, regime="risk_off", nav=Decimal("100000"), loop_iteration=1)
    assert summary is not None
    assert summary["regime"] == "risk_off"
    ovr = svc.overrides_for("portfolio_orchestrator", "risk_off")
    assert "entry_conviction_threshold" in ovr
    # An applied change should have been written to the audit log.
    async with sf() as session:
        rows = (await session.execute(select(ParameterTuningLog))).scalars().all()
    assert len(rows) >= 1
