from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

import pytest

from risk.engine import RiskPreflightDecision
from system.trading_loop.loop import TradingLoop


class _Router:
    def route(self, asset_class, symbol, metadata=None):
        return "ibkr"


class _Risk:
    def preflight_capacity(self, signal, portfolio):
        return RiskPreflightDecision(
            ok=True,
            reason="preflight_capacity_ok",
            signal_id="s1",
            checks_passed=["single_name_notional"],
            checks_failed=[],
            effective_quantity=Decimal("0.4"),
            effective_notional=Decimal("40"),
        )


class _Execution:
    _wave9_cfg = None

    async def _get_broker(self, name):
        return object()

    def _build_order(self, signal):
        return SimpleNamespace(
            symbol=signal.symbol,
            side=signal.side,
            quantity=Decimal(str(signal.suggested_quantity)),
            limit_price=Decimal(str(signal.suggested_price)),
        )

    async def _apply_marketable_limit(self, order, signal, broker):
        return order

    async def _normalize_order_for_broker(self, order, signal, broker):
        if order.quantity < Decimal("1"):
            order.quantity = Decimal("0")
        return order

    async def _paper_stale_price_precheck(self, order, signal, *, broker=None, session_factory=None):
        return None, {}

    async def _passes_execution_limits(self, broker, order, *, broker_name):
        return True


@pytest.mark.asyncio
async def test_built_signal_preflight_uses_risk_effective_quantity_for_normalization():
    loop = TradingLoop({}, ["ibkr"], paper_mode=True)
    loop.router = _Router()
    loop.risk_engine = _Risk()
    loop.execution_engine = _Execution()

    signal = SimpleNamespace(
        signal_id="s1",
        symbol="BIL",
        strategy="mean_reversion",
        side="sell",
        confidence=Decimal("0.51"),
        broker="",
        asset_class="equity",
        suggested_price=Decimal("100"),
        suggested_quantity=Decimal("10"),
        metadata={},
    )
    action = SimpleNamespace(kind="open_strategy", priority_score=Decimal("0.1"))

    reason, meta = await loop._preflight_built_signal(
        signal,
        action,
        portfolio_dict={},
        session_factory=None,
    )

    assert reason == "invalid_quantity_after_normalization"
    assert meta["post_normalized_quantity"] == "0"
    assert meta["preflight_capacity_effective_quantity"] == "0.4"

