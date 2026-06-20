from __future__ import annotations

import asyncio
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from risk.engine import RiskVerdict
from risk.profit_harvest import evaluate_profit_harvest, resolve_harvest_thresholds
from risk.profit_harvest import should_defer_profit_harvest_for_redeployment
from risk.profit_harvest import (
    HarvestThresholds,
    ProfitHarvestDecision,
    ProfitHarvestV2Context,
    evaluate_profit_harvest_v2,
    should_suppress_harvest_for_horizon,
)
from system.orchestrator import Orchestrator


def _decision(reason: str, profit_abs: str, profit_nav_pct: str = "0") -> ProfitHarvestDecision:
    return ProfitHarvestDecision(
        should_reduce=True,
        reason=reason,
        reduce_fraction=Decimal("1"),
        profit_absolute=Decimal(profit_abs),
        profit_pct=Decimal("0"),
        profit_pct_of_nav=Decimal(profit_nav_pct),
        peak_profit_absolute=Decimal("0"),
        giveback_fraction=Decimal("1"),
    )


_MIN_HOLD = Decimal(str(3 * 24 * 3600))  # 3 days, matching D166 default
_NAV = Decimal("1000000")


def test_harvest_guard_suppresses_young_loss_trailing_lock() -> None:
    # A trailing lock realising a LOSS on a position 30 min old → churn.
    suppress, why = should_suppress_harvest_for_horizon(
        decision=_decision("trailing_profit_lock", "-138"),
        age_sec=Decimal("1800"),
        min_hold_sec=_MIN_HOLD,
        nav=_NAV,
    )
    assert suppress is True
    assert why == "young_loss_lock"


def test_harvest_guard_allows_full_take_profit_even_when_young() -> None:
    # Banking a real winner is never churn.
    suppress, why = should_suppress_harvest_for_horizon(
        decision=_decision("full_take_profit", "5000", "0.005"),
        age_sec=Decimal("60"),
        min_hold_sec=_MIN_HOLD,
        nav=_NAV,
    )
    assert suppress is False
    assert why == "not_trailing_lock"


def test_harvest_guard_allows_trailing_lock_banking_material_profit() -> None:
    # A trailing lock that still locks a materially positive profit is a real
    # winner being protected, not churn — allow even on a young position.
    suppress, why = should_suppress_harvest_for_horizon(
        decision=_decision("trailing_profit_lock", "4000", "0.004"),
        age_sec=Decimal("60"),
        min_hold_sec=_MIN_HOLD,
        nav=_NAV,
    )
    assert suppress is False
    assert why == "locks_material_profit"


def test_harvest_guard_allows_matured_loss_lock() -> None:
    # Past the min-hold, the thesis had its chance → let the lock fire.
    suppress, why = should_suppress_harvest_for_horizon(
        decision=_decision("trailing_profit_lock", "-138"),
        age_sec=_MIN_HOLD + Decimal("1"),
        min_hold_sec=_MIN_HOLD,
        nav=_NAV,
    )
    assert suppress is False
    assert why == "matured"


def test_harvest_guard_allows_when_age_unknown() -> None:
    # No evidence to gate on → never suppress (mirrors D166 protective gate).
    suppress, why = should_suppress_harvest_for_horizon(
        decision=_decision("trailing_profit_lock", "-138"),
        age_sec=None,
        min_hold_sec=_MIN_HOLD,
        nav=_NAV,
    )
    assert suppress is False
    assert why == "age_unknown"


def test_harvest_guard_noop_when_no_reduce() -> None:
    dec = ProfitHarvestDecision(
        should_reduce=False,
        reason="below_harvest_threshold",
        reduce_fraction=Decimal("0"),
        profit_absolute=Decimal("0"),
        profit_pct=Decimal("0"),
        profit_pct_of_nav=Decimal("0"),
        peak_profit_absolute=Decimal("0"),
        giveback_fraction=Decimal("0"),
    )
    suppress, why = should_suppress_harvest_for_horizon(
        decision=dec, age_sec=Decimal("60"), min_hold_sec=_MIN_HOLD, nav=_NAV
    )
    assert suppress is False
    assert why == "no_reduce"


_DYNAMIC_CFG = {
    "enabled": True,
    "close_cooldown_sec": "5",
    "base": {
        "min_profit_pct": "0.012",
        "min_profit_nav_pct": "0.001",
        "trim_fraction": "0.50",
        "full_close_profit_pct": "0.035",
        "trailing_giveback_pct": "0.35",
    },
    "volatility": {
        "fallback_vol": "0.015",
        "vol_low": "0.005",
        "vol_high": "0.05",
        "min_profit_k": "1.5",
        "min_profit_floor": "0.004",
        "min_profit_ceil": "0.05",
        "full_close_k": "4.0",
        "full_close_floor": "0.012",
        "full_close_ceil": "0.20",
        "giveback_vol_low": "0.25",
        "giveback_vol_high": "0.55",
    },
    "mode_bias": {
        "defender": {
            "threshold_mult": "0.70",
            "giveback_mult": "0.70",
            "trim_fraction_mult": "1.20",
        },
        "trader": {
            "threshold_mult": "1.00",
            "giveback_mult": "1.00",
            "trim_fraction_mult": "1.00",
        },
        "hunter": {
            "threshold_mult": "1.50",
            "giveback_mult": "1.40",
            "trim_fraction_mult": "0.70",
        },
    },
    "bounds": {
        "trim_fraction_min": "0.10",
        "trim_fraction_max": "0.95",
        "giveback_min": "0.10",
        "giveback_max": "0.80",
    },
}


def test_profit_harvest_partial_take_profit() -> None:
    decision = evaluate_profit_harvest(
        quantity=Decimal("10"),
        avg_entry_price=Decimal("100"),
        current_price=Decimal("102"),
        nav=Decimal("10000"),
        min_profit_pct=Decimal("0.01"),
        min_profit_nav_pct=Decimal("0.001"),
        trim_fraction=Decimal("0.50"),
        full_close_profit_pct=Decimal("0.05"),
    )

    assert decision.should_reduce is True
    assert decision.reason == "partial_take_profit"
    assert decision.reduce_fraction == Decimal("0.50")


def test_profit_harvest_defers_when_underdeployed_and_redeployment_locked() -> None:
    assert should_defer_profit_harvest_for_redeployment(
        cash_deployed=Decimal("30000"),
        nav=Decimal("100000"),
        capital_pct=Decimal("1.0"),
        open_lock_active=True,
        tolerance_pct=Decimal("0.0025"),
    )
    assert not should_defer_profit_harvest_for_redeployment(
        cash_deployed=Decimal("30000"),
        nav=Decimal("100000"),
        capital_pct=Decimal("1.0"),
        open_lock_active=False,
        tolerance_pct=Decimal("0.0025"),
    )
    assert not should_defer_profit_harvest_for_redeployment(
        cash_deployed=Decimal("99900"),
        nav=Decimal("100000"),
        capital_pct=Decimal("1.0"),
        open_lock_active=True,
        tolerance_pct=Decimal("0.0025"),
    )
    assert not should_defer_profit_harvest_for_redeployment(
        cash_deployed=Decimal("30000"),
        nav=Decimal("100000"),
        capital_pct=Decimal("1.0"),
        open_lock_active=True,
        open_lock_blocks_redeployment=False,
        tolerance_pct=Decimal("0.0025"),
    )


def test_profit_harvest_trailing_lock_closes() -> None:
    decision = evaluate_profit_harvest(
        quantity=Decimal("10"),
        avg_entry_price=Decimal("100"),
        current_price=Decimal("101.20"),
        nav=Decimal("10000"),
        peak_profit_absolute=Decimal("30"),
        min_profit_pct=Decimal("0.05"),
        min_profit_nav_pct=Decimal("0.001"),
        trailing_giveback_pct=Decimal("0.35"),
    )

    assert decision.should_reduce is True
    assert decision.reason == "trailing_profit_lock"
    assert decision.reduce_fraction == Decimal("1")


def test_trailing_lock_fires_when_position_rolls_into_red() -> None:
    """+$3K peak → −$4K current must fire trailing lock, not silently skip."""
    decision = evaluate_profit_harvest(
        quantity=Decimal("100"),
        avg_entry_price=Decimal("100"),
        current_price=Decimal("60"),       # current loss = -4000
        nav=Decimal("100000"),
        peak_profit_absolute=Decimal("3000"),
        min_profit_pct=Decimal("0.05"),
        full_close_profit_pct=Decimal("0.10"),
        trailing_giveback_pct=Decimal("0.35"),
        peak_lock_min_nav_pct=Decimal("0.0005"),
    )
    assert decision.should_reduce is True
    assert decision.reason == "trailing_profit_lock"
    assert decision.reduce_fraction == Decimal("1")


def test_trailing_lock_skipped_when_peak_was_immaterial() -> None:
    """Tiny peak (well under floor) must not lock on noise."""
    decision = evaluate_profit_harvest(
        quantity=Decimal("1"),
        avg_entry_price=Decimal("100"),
        current_price=Decimal("90"),
        nav=Decimal("100000"),
        peak_profit_absolute=Decimal("5"),  # peak / NAV = 0.005% — below 0.05% floor
        peak_lock_min_nav_pct=Decimal("0.0005"),
    )
    assert decision.should_reduce is False


def test_resolve_thresholds_scales_with_volatility() -> None:
    calm = resolve_harvest_thresholds(
        config=_DYNAMIC_CFG, profile_mode="trader", volatility_pct=Decimal("0.005")
    )
    wild = resolve_harvest_thresholds(
        config=_DYNAMIC_CFG, profile_mode="trader", volatility_pct=Decimal("0.05")
    )
    assert wild.min_profit_pct > calm.min_profit_pct
    assert wild.full_close_profit_pct > calm.full_close_profit_pct
    assert wild.trailing_giveback_pct >= calm.trailing_giveback_pct
    assert wild.full_close_profit_pct > wild.min_profit_pct


def test_resolve_thresholds_mode_bias() -> None:
    vol = Decimal("0.02")
    defender = resolve_harvest_thresholds(
        config=_DYNAMIC_CFG, profile_mode="defender", volatility_pct=vol
    )
    trader = resolve_harvest_thresholds(
        config=_DYNAMIC_CFG, profile_mode="trader", volatility_pct=vol
    )
    hunter = resolve_harvest_thresholds(
        config=_DYNAMIC_CFG, profile_mode="hunter", volatility_pct=vol
    )
    assert defender.min_profit_pct < trader.min_profit_pct < hunter.min_profit_pct
    assert defender.trim_fraction > trader.trim_fraction > hunter.trim_fraction
    assert defender.trailing_giveback_pct < hunter.trailing_giveback_pct


def test_resolve_thresholds_overrides_win() -> None:
    out = resolve_harvest_thresholds(
        config=_DYNAMIC_CFG,
        profile_mode="hunter",
        volatility_pct=Decimal("0.04"),
        overrides={"min_profit_pct": "0.005", "trim_fraction": "0.90"},
    )
    assert out.min_profit_pct == Decimal("0.005")
    assert out.trim_fraction == Decimal("0.90")


def _v2_thresholds() -> HarvestThresholds:
    return HarvestThresholds(
        min_profit_pct=Decimal("0.05"),
        min_profit_nav_pct=Decimal("0.001"),
        full_close_profit_pct=Decimal("0.30"),
        trim_fraction=Decimal("0.25"),
        trailing_giveback_pct=Decimal("0.45"),
        peak_lock_min_nav_pct=Decimal("0.0002"),
    )


def test_profit_harvest_v2_holds_supported_runner() -> None:
    decision = evaluate_profit_harvest_v2(
        quantity=Decimal("100"),
        avg_entry_price=Decimal("100"),
        current_price=Decimal("108"),
        nav=Decimal("100000"),
        thresholds=_v2_thresholds(),
        peak_profit_absolute=Decimal("820"),
        context=ProfitHarvestV2Context(
            accumulator_score=Decimal("0.75"),
            ai_news_score=Decimal("0.60"),
            meta_label_kept=True,
            meta_label_probability=Decimal("0.72"),
        ),
    )

    assert decision.action == "HOLD_RUNNER"
    assert decision.reason == "supported_runner"
    assert decision.reduce_fraction == Decimal("0")
    assert decision.support_score > Decimal("0.35")


def test_profit_harvest_v2_trims_when_profit_ready_without_support() -> None:
    decision = evaluate_profit_harvest_v2(
        quantity=Decimal("100"),
        avg_entry_price=Decimal("100"),
        current_price=Decimal("108"),
        nav=Decimal("100000"),
        thresholds=_v2_thresholds(),
        peak_profit_absolute=Decimal("820"),
        context=ProfitHarvestV2Context(accumulator_score=Decimal("0.05")),
    )

    assert decision.action == "TRIM_PARTIAL"
    assert decision.reason == "bank_profit_leave_runner"
    assert decision.reduce_fraction == Decimal("0.25")


def test_profit_harvest_v2_closes_on_severe_giveback_and_bad_thesis() -> None:
    decision = evaluate_profit_harvest_v2(
        quantity=Decimal("100"),
        avg_entry_price=Decimal("100"),
        current_price=Decimal("103"),
        nav=Decimal("100000"),
        thresholds=_v2_thresholds(),
        peak_profit_absolute=Decimal("1500"),
        context=ProfitHarvestV2Context(
            accumulator_score=Decimal("-0.80"),
            ai_news_score=Decimal("-0.65"),
            meta_label_kept=False,
            meta_label_probability=Decimal("0.25"),
        ),
    )

    assert decision.action == "CLOSE_FULL"
    assert decision.reason == "dynamic_trailing_lock"
    assert decision.reduce_fraction == Decimal("1")
    assert decision.dynamic_giveback_pct < Decimal("0.45")


def test_profit_harvest_v2_closed_venue_is_shadow_only() -> None:
    decision = evaluate_profit_harvest_v2(
        quantity=Decimal("100"),
        avg_entry_price=Decimal("100"),
        current_price=Decimal("108"),
        nav=Decimal("100000"),
        thresholds=_v2_thresholds(),
        peak_profit_absolute=Decimal("820"),
        context=ProfitHarvestV2Context(session_open=False),
    )

    assert decision.action == "DO_NOT_TOUCH"
    assert decision.reason == "venue_closed_shadow_only"
    assert decision.reduce_fraction == Decimal("0")


@pytest.mark.asyncio
async def test_profit_harvest_tick_submits_reduce_only_trim(monkeypatch) -> None:
    orch = Orchestrator()

    monkeypatch.setattr(
        Orchestrator, "_read_active_profile_mode", staticmethod(lambda: "trader")
    )

    risk_engine = MagicMock()
    risk_engine.config = {"profit_harvest": _DYNAMIC_CFG}
    risk_engine.update_high_watermark = MagicMock()
    risk_engine.restore_runtime_state = MagicMock()
    risk_engine.evaluate_and_persist = AsyncMock(
        return_value=SimpleNamespace(verdict=RiskVerdict.APPROVED, reason="ok")
    )

    execution_engine = MagicMock()
    execution_engine.execute = AsyncMock(return_value=SimpleNamespace(status="filled"))
    orch._trading_loop = MagicMock(risk_engine=risk_engine, execution_engine=execution_engine)
    orch._broker_manager = MagicMock()

    monkeypatch.setattr(
        "storage.db.init_async_database",
        AsyncMock(return_value=(MagicMock(), MagicMock(name="sf"))),
    )
    monkeypatch.setattr("storage.db.dispose_engine", AsyncMock(return_value=None))
    monkeypatch.setattr(
        "system.portfolio_equity.live_portfolio_value",
        AsyncMock(return_value=Decimal("10000")),
    )
    monkeypatch.setattr(
        "run_m3._load_portfolio_state",
        AsyncMock(
            return_value={
                "portfolio_value": Decimal("10000"),
                "high_watermark_value": Decimal("10000"),
                "positions": {
                    "alpaca:SPY": {
                        "symbol": "SPY",
                        "broker": "alpaca",
                        "asset_class": "equity",
                        "quantity": Decimal("10"),
                        "avg_entry_price": Decimal("100"),
                        "current_price": Decimal("102"),
                        "instrument_metadata": {
                            "profit_harvest": {
                                "min_profit_pct": "0.01",
                                "full_close_profit_pct": "0.05",
                            }
                        },
                    }
                },
            }
        ),
    )

    await orch._run_profit_harvest_tick()

    risk_engine.evaluate_and_persist.assert_awaited_once()
    execution_engine.execute.assert_awaited_once()
    signal_sent = risk_engine.evaluate_and_persist.await_args.args[1]
    assert signal_sent.strategy == "profit_harvest_monitor"
    assert signal_sent.side == "sell"
    assert signal_sent.suggested_quantity == Decimal("5.00000000")
    assert signal_sent.metadata["reduce_only"] is True
    assert signal_sent.metadata["profit_harvest_reason"] == "partial_take_profit"


@pytest.mark.asyncio
async def test_profit_harvest_v2_active_submits_v2_trim(monkeypatch) -> None:
    orch = Orchestrator()

    monkeypatch.setattr(
        Orchestrator, "_read_active_profile_mode", staticmethod(lambda: "trader")
    )

    risk_engine = MagicMock()
    risk_engine.config = {"profit_harvest": {**_DYNAMIC_CFG, "v2": {"active": True}}}
    risk_engine.update_high_watermark = MagicMock()
    risk_engine.restore_runtime_state = MagicMock()
    risk_engine.evaluate_and_persist = AsyncMock(
        return_value=SimpleNamespace(verdict=RiskVerdict.APPROVED, reason="ok")
    )

    execution_engine = MagicMock()
    execution_engine.execute = AsyncMock(return_value=SimpleNamespace(status="filled"))
    orch._trading_loop = MagicMock(risk_engine=risk_engine, execution_engine=execution_engine)
    orch._broker_manager = MagicMock()

    monkeypatch.setattr(
        "storage.db.init_async_database",
        AsyncMock(return_value=(MagicMock(), MagicMock(name="sf"))),
    )
    monkeypatch.setattr("storage.db.dispose_engine", AsyncMock(return_value=None))
    monkeypatch.setattr(
        "system.portfolio_equity.live_portfolio_value",
        AsyncMock(return_value=Decimal("10000")),
    )
    monkeypatch.setattr(
        "run_m3._load_portfolio_state",
        AsyncMock(
            return_value={
                "portfolio_value": Decimal("10000"),
                "high_watermark_value": Decimal("10000"),
                "positions": {
                    "alpaca:SPY": {
                        "symbol": "SPY",
                        "broker": "alpaca",
                        "asset_class": "equity",
                        "quantity": Decimal("10"),
                        "avg_entry_price": Decimal("100"),
                        "current_price": Decimal("102"),
                        "instrument_metadata": {
                            "profit_harvest": {
                                "min_profit_pct": "0.01",
                                "full_close_profit_pct": "0.05",
                            }
                        },
                    }
                },
            }
        ),
    )

    await orch._run_profit_harvest_tick()

    risk_engine.evaluate_and_persist.assert_awaited_once()
    signal_sent = risk_engine.evaluate_and_persist.await_args.args[1]
    assert signal_sent.metadata["profit_harvest_policy"] == "v2_active"
    assert signal_sent.metadata["profit_harvest_reason"] == "v2:bank_profit_leave_runner"
    assert signal_sent.metadata["profit_harvest_v2_action"] == "TRIM_PARTIAL"


@pytest.mark.asyncio
async def test_profit_harvest_loop_is_cancellable(monkeypatch) -> None:
    monkeypatch.setenv("PROFIT_HARVEST_MONITOR_INTERVAL_SEC", "15")
    monkeypatch.setattr(
        "system.orchestrator.Orchestrator._sleep_cancellable",
        staticmethod(lambda total_sec, **_: asyncio.sleep(0.005)),
    )
    orch = Orchestrator()
    monkeypatch.setattr(orch, "_run_profit_harvest_tick", AsyncMock(return_value=None))

    task = asyncio.create_task(orch._profit_harvest_loop())
    await asyncio.sleep(0.05)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
