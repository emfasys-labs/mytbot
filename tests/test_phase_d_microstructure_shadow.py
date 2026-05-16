from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from brokers.base import OrderBook, OrderStatus
from execution.engine import ExecutionEngine
from execution.microstructure_shadow import (
    MicrostructureShadowConfig,
    build_microstructure_shadow_metadata,
    score_microstructure_shadow,
)
from scripts.report_phase_d_microstructure_shadow import summarize_microstructure_rows
from scripts.report_phase_d_execution_outcomes import slippage_bps, summarize_outcomes
from scripts.phase_d_status_bundle import COMMANDS as PHASE_D_STATUS_COMMANDS
from risk.engine import RiskDecision, RiskVerdict, Signal


class _BookBroker:
    async def connect(self) -> bool:
        return True

    async def is_connected(self) -> bool:
        return True

    async def get_order_book(self, symbol: str, depth: int = 5) -> OrderBook:
        return OrderBook(
            symbol=symbol,
            timestamp=datetime.now(timezone.utc).isoformat(),
            bids=[(Decimal("100"), Decimal("10")), (Decimal("99.9"), Decimal("8"))],
            asks=[(Decimal("100.1"), Decimal("9")), (Decimal("100.2"), Decimal("7"))],
        )

    async def get_last_price(self, symbol: str) -> Decimal:
        return Decimal("100.05")


def _approved() -> RiskDecision:
    return RiskDecision(
        verdict=RiskVerdict.APPROVED,
        reason="ok",
        signal_id="s",
        checks_passed=[],
        checks_failed=[],
    )


def _crypto_signal() -> Signal:
    return Signal(
        signal_id="s",
        symbol="BTC-USD",
        side="buy",
        strategy="momentum",
        confidence=0.9,
        suggested_quantity=Decimal("0.01"),
        suggested_price=Decimal("100"),
        broker="kraken",
        asset_class="crypto",
        timestamp=datetime.now(timezone.utc).isoformat(),
        metadata={},
    )


def test_microstructure_shadow_scores_wide_spread_as_caution_or_high_risk() -> None:
    out = score_microstructure_shadow(
        {
            "spread_bps": 80.0,
            "vpin_proxy": 0.2,
            "liquidity_fragility": 0.1,
            "quote_staleness": 0.1,
            "well_formed": 1.0,
        },
        cfg=MicrostructureShadowConfig(enabled=True, max_spread_bps=25.0),
    )

    assert out["microstructure_shadow_used"] is True
    assert out["microstructure_shadow_label"] in {"caution", "high_risk"}
    assert "wide_spread" in out["microstructure_shadow_reasons"]


@pytest.mark.asyncio
async def test_microstructure_shadow_metadata_from_order_book() -> None:
    out = await build_microstructure_shadow_metadata(
        broker=_BookBroker(),
        symbol="BTC-USD",
        asset_class="crypto",
        cfg=MicrostructureShadowConfig(enabled=True, asset_classes=("crypto",), depth=2),
    )

    assert out["microstructure_shadow_used"] is True
    assert out["microstructure_shadow_label"] == "normal"
    assert out["microstructure_spread_bps"] > 0


@pytest.mark.asyncio
async def test_execution_stamps_microstructure_shadow_on_paper_crypto(monkeypatch) -> None:
    broker = _BookBroker()
    monkeypatch.setattr("execution.engine.get_broker", lambda *args, **kwargs: broker)

    engine = ExecutionEngine(broker_configs={}, paper_mode=True)
    captured = {}

    async def _capture(_session_factory, order, _result, _signal):
        captured["metadata"] = dict(order.instrument_metadata or {})

    monkeypatch.setattr(engine, "_persist_result", _capture)
    result = await engine.execute(_crypto_signal(), _approved())

    assert result is not None
    assert result.status == OrderStatus.FILLED
    assert captured["metadata"]["microstructure_shadow_used"] is True
    assert captured["metadata"]["microstructure_shadow_label"] == "normal"
    assert engine.last_skip_reason is None


def test_phase_d_report_summarizes_shadow_rows() -> None:
    out = summarize_microstructure_rows(
        [
            {"microstructure_shadow_used": True, "microstructure_shadow_label": "normal", "microstructure_shadow_risk": 0.1},
            {"microstructure_shadow_used": True, "microstructure_shadow_label": "high_risk", "microstructure_shadow_risk": 0.9},
            {"microstructure_shadow_used": False},
        ]
    )

    assert out["rows"] == 3
    assert out["used"] == 2
    assert out["labels"] == {"normal": 1, "high_risk": 1}
    assert out["avg_risk"] == 0.5


def test_phase_d_outcome_slippage_bps_is_side_aware() -> None:
    assert slippage_bps(side="buy", reference_price=100, fill_price=101) == 100.0
    assert slippage_bps(side="sell", reference_price=100, fill_price=99) == 100.0
    assert slippage_bps(side="buy", reference_price=0, fill_price=101) is None


def test_phase_d_outcome_summary_groups_by_shadow_label() -> None:
    out = summarize_outcomes(
        [
            {
                "microstructure_shadow_label": "normal",
                "filled": True,
                "realized_slippage_bps": 2.0,
                "fee": 1.5,
            },
            {
                "microstructure_shadow_label": "normal",
                "filled": False,
                "realized_slippage_bps": None,
                "fee": None,
            },
            {
                "microstructure_shadow_label": "high_risk",
                "filled": True,
                "realized_slippage_bps": 8.0,
                "fee": 2.0,
            },
        ]
    )

    assert out["rows"] == 3
    assert out["by_label"]["normal"]["count"] == 2
    assert out["by_label"]["normal"]["fill_rate"] == 0.5
    assert out["by_label"]["normal"]["avg_slippage_bps"] == 2.0
    assert out["by_label"]["high_risk"]["avg_slippage_bps"] == 8.0


def test_phase_d_status_bundle_includes_shadow_and_outcome_reports() -> None:
    joined = [" ".join(cmd) for cmd in PHASE_D_STATUS_COMMANDS]
    assert any("report_phase_d_microstructure_shadow.py" in cmd for cmd in joined)
    assert any("report_phase_d_execution_outcomes.py" in cmd for cmd in joined)
