"""Post-open stop-loss monitor wiring (D031E runtime integration)."""

from __future__ import annotations

import asyncio
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from brokers.base import AssetClass, Position
from risk.engine import RiskVerdict
from risk.stop_loss import StopLossDecision
from system.orchestrator import Orchestrator


class _StubAdapter:
    def __init__(self, positions: list[Position]) -> None:
        self._positions = positions

    async def get_positions(self) -> list[Position]:
        return list(self._positions)


class _StubBrokerManager:
    def __init__(self, adapters: dict[str, _StubAdapter]) -> None:
        self.adapters = adapters


def _position_long_loss(symbol: str = "SPY") -> Position:
    return Position(
        symbol=symbol,
        asset_class=AssetClass.EQUITY,
        quantity=Decimal("10"),
        avg_entry_price=Decimal("100"),
        current_price=Decimal("90"),
        unrealised_pnl=Decimal("-100"),
        broker="alpaca",
        instrument_metadata={},
    )


@pytest.mark.asyncio
async def test_stop_loss_tick_submits_reduce_only_close_when_breached(monkeypatch) -> None:
    orch = Orchestrator()
    orch._broker_manager = _StubBrokerManager({"alpaca": _StubAdapter([_position_long_loss("SPY")])})

    risk_engine = MagicMock()
    risk_engine.config = {"max_loss_per_trade_pct": "0.02"}
    risk_engine.is_broker_disabled.return_value = False
    risk_engine.update_high_watermark = MagicMock()
    risk_engine.restore_runtime_state = MagicMock()
    risk_engine.evaluate_and_persist = AsyncMock(
        return_value=SimpleNamespace(verdict=RiskVerdict.APPROVED, reason="ok")
    )

    execution_engine = MagicMock()
    execution_engine.execute = AsyncMock(return_value=SimpleNamespace(status="filled"))

    orch._trading_loop = MagicMock(risk_engine=risk_engine, execution_engine=execution_engine)

    monkeypatch.setattr(
        "system.orchestrator.evaluate_stop_loss",
        lambda **_: StopLossDecision(
            should_close=True,
            reason="portfolio_loss_budget:100 > 50",
            loss_pct=Decimal("0.01"),
            loss_absolute=Decimal("100"),
            structural_stop_price=None,
            structural_stop_breached=False,
        ),
    )
    monkeypatch.setattr(
        "storage.db.init_async_database",
        AsyncMock(return_value=(MagicMock(), MagicMock(name="sf"))),
    )
    monkeypatch.setattr("storage.db.dispose_engine", AsyncMock(return_value=None))
    monkeypatch.setattr(
        "run_m3._load_portfolio_state",
        AsyncMock(return_value={"portfolio_value": Decimal("100000"), "high_watermark_value": Decimal("100000")}),
    )
    monkeypatch.setattr(
        "system.portfolio_equity.live_portfolio_value",
        AsyncMock(return_value=Decimal("100000")),
    )

    await orch._run_stop_loss_tick()

    risk_engine.evaluate_and_persist.assert_awaited_once()
    execution_engine.execute.assert_awaited_once()
    signal_sent = risk_engine.evaluate_and_persist.await_args.args[1]
    assert signal_sent.strategy == "stop_loss_monitor"
    assert signal_sent.side == "sell"
    assert signal_sent.metadata["reduce_only"] is True


@pytest.mark.asyncio
async def test_stop_loss_tick_no_order_when_not_breached(monkeypatch) -> None:
    orch = Orchestrator()
    orch._broker_manager = _StubBrokerManager({"alpaca": _StubAdapter([_position_long_loss("SPY")])})

    risk_engine = MagicMock()
    risk_engine.config = {"max_loss_per_trade_pct": "0.02"}
    risk_engine.is_broker_disabled.return_value = False
    risk_engine.evaluate_and_persist = AsyncMock()
    risk_engine.update_high_watermark = MagicMock()
    risk_engine.restore_runtime_state = MagicMock()

    execution_engine = MagicMock()
    execution_engine.execute = AsyncMock()
    orch._trading_loop = MagicMock(risk_engine=risk_engine, execution_engine=execution_engine)

    monkeypatch.setattr(
        "system.orchestrator.evaluate_stop_loss",
        lambda **_: StopLossDecision(
            should_close=False,
            reason="within_budget",
            loss_pct=Decimal("0"),
            loss_absolute=Decimal("0"),
            structural_stop_price=None,
            structural_stop_breached=False,
        ),
    )
    monkeypatch.setattr(
        "storage.db.init_async_database",
        AsyncMock(return_value=(MagicMock(), MagicMock(name="sf"))),
    )
    monkeypatch.setattr("storage.db.dispose_engine", AsyncMock(return_value=None))
    monkeypatch.setattr(
        "system.portfolio_equity.live_portfolio_value",
        AsyncMock(return_value=Decimal("100000")),
    )

    await orch._run_stop_loss_tick()

    risk_engine.evaluate_and_persist.assert_not_called()
    execution_engine.execute.assert_not_called()


@pytest.mark.asyncio
async def test_stop_loss_loop_is_cancellable(monkeypatch) -> None:
    monkeypatch.setenv("STOP_LOSS_MONITOR_INTERVAL_SEC", "15")
    monkeypatch.setattr(
        "system.orchestrator.Orchestrator._sleep_cancellable",
        staticmethod(lambda total_sec, **_: asyncio.sleep(0.005)),
    )
    orch = Orchestrator()
    monkeypatch.setattr(orch, "_run_stop_loss_tick", AsyncMock(return_value=None))

    task = asyncio.create_task(orch._stop_loss_loop())
    await asyncio.sleep(0.05)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
