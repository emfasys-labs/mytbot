from decimal import Decimal
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from brokers.base import AssetClass, Position
from core.instrument_semantics import InstrumentRole, instrument_role
from execution.engine import ExecutionEngine
from risk.engine import RiskEngine, Signal
from run_m3 import _load_portfolio_state
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from storage.models import Base, FillLog, PositionLog
from portfolio.balance import (
    BalancePolicy,
    aggregate_book_positions,
    legacy_reconciliation_plan,
    portfolio_admission_efficiency,
    risk_balanced_weights,
)
from portfolio.cluster_map import economic_factor_loadings, factor_overlap
from portfolio.portfolio_orchestrator import BookPosition
from portfolio.target_ledger import PortfolioTargetLedger


def test_instrument_roles_separate_alpha_from_liquidity() -> None:
    assert instrument_role("FIDD-USD", asset_class="crypto") == InstrumentRole.CASH_EQUIVALENT
    assert instrument_role("BOXX", asset_class="equity") == InstrumentRole.LIQUIDITY_RESERVE
    assert instrument_role("SPY", asset_class="equity") == InstrumentRole.ALPHA


def test_etf_lookthrough_detects_overlap() -> None:
    assert economic_factor_loadings("SOXX", "equity") == {
        "semiconductors": Decimal("1")
    }
    assert factor_overlap("SOXX", "equity", "LRCX", "equity") == Decimal("1")
    assert factor_overlap("SPY", "equity", "IVV", "equity") == Decimal("1")


def test_canonical_book_aggregates_brokers_instead_of_hiding_second_row() -> None:
    rows = [
        BookPosition("AUDUSD", Decimal("100"), Decimal("0.68"), Decimal("0.69"), "forex", "ibkr"),
        BookPosition("AUDUSD=X", Decimal("50"), Decimal("0.70"), Decimal("0.69"), "forex", "capitalcom"),
    ]
    book, diagnostics = aggregate_book_positions(rows)
    assert len(book) == 1
    assert book[0].symbol == "AUDUSD"
    assert book[0].signed_qty == Decimal("150")
    assert diagnostics["duplicate_economic_positions"][0]["rows"] == 2


def test_semantic_hrp_reduces_high_volatility_crypto_weight() -> None:
    policy = BalancePolicy(enabled=True, hrp_blend=Decimal("1"))
    weights, diagnostics = risk_balanced_weights(
        [
            ("ETH-USD", "crypto", Decimal("1")),
            ("SPY", "equity", Decimal("1")),
        ],
        policy=policy,
    )
    assert diagnostics["used"] is True
    assert weights["ETH-USD"] < weights["SPY"]
    assert sum(weights.values()) == pytest.approx(Decimal("1"))


def test_portfolio_admission_blocks_incremental_crowding() -> None:
    policy = BalancePolicy(
        enabled=True,
        max_incremental_factor_share=Decimal("0.20"),
        min_efficiency=Decimal("-1"),
    )
    efficiency, metadata = portfolio_admission_efficiency(
        symbol="IVV",
        asset_class="equity",
        proposed_notional=Decimal("50000"),
        expected_edge_bps=Decimal("100"),
        current_factor_exposure={"us_equity_beta": Decimal("180000")},
        nav=Decimal("1000000"),
        policy=policy,
    )
    assert efficiency.is_finite()
    assert metadata["allow"] is False
    assert metadata["reason"] == "portfolio_overlap_or_efficiency"


def test_target_ledger_prevents_trim_reopen_on_same_feature_bar() -> None:
    ledger = PortfolioTargetLedger()
    ledger.begin_cycle(1)
    ledger.claim("HYG", Decimal("5000"), source="primary", feature_bar="2026-06-29")
    ledger.mark_reduction("HYG", feature_bar="2026-06-29")
    assert ledger.increase_allowed("HYG", feature_bar="2026-06-29") is False
    assert ledger.increase_allowed("HYG", feature_bar="2026-06-30") is True
    remaining, claim = ledger.remaining_target(
        "HYG",
        intended_sign=1,
        existing_notional=Decimal("4000"),
        fallback_target=Decimal("9000"),
        source="reserve",
        feature_bar="2026-06-29",
    )
    assert remaining == Decimal("1000")
    assert claim.source == "primary"


def test_target_ledger_allows_only_one_reserve_increase_per_feature_bar() -> None:
    ledger = PortfolioTargetLedger()
    assert ledger.increase_allowed("MARA", feature_bar="1d:2026-07-02") is True
    ledger.mark_increase("MARA", feature_bar="1d:2026-07-02")
    assert ledger.increase_allowed("MARA", feature_bar="1d:2026-07-02") is False
    assert ledger.increase_allowed("MARA", feature_bar="1d:2026-07-03") is True


def test_legacy_reconciliation_plans_duplicates_cash_and_true_substitutes() -> None:
    rows = [
        {"symbol": "CME", "broker": "alpaca", "quantity": 10, "current_price": 200, "asset_class": "equity"},
        {"symbol": "CME", "broker": "capitalcom", "quantity": 1, "current_price": 200, "asset_class": "equity"},
        {"symbol": "FIDD-USD", "broker": "binance", "quantity": 5000, "current_price": 1, "asset_class": "crypto"},
        {"symbol": "AGG", "broker": "alpaca", "quantity": 100, "current_price": 100, "asset_class": "equity"},
        {"symbol": "BND", "broker": "alpaca", "quantity": 50, "current_price": 100, "asset_class": "equity"},
    ]
    plan = legacy_reconciliation_plan(
        rows,
        nav=Decimal("1000000"),
        policy=BalancePolicy(enabled=True),
    )
    kinds = {item["kind"] for item in plan}
    assert "reduce_duplicate_venue" in kinds
    assert "close_non_alpha_cash_equivalent" in kinds
    assert "reduce_redundant_factor" in kinds


def test_legacy_reconciliation_reduces_excess_crypto_expressions() -> None:
    rows = [
        {
            "symbol": f"{symbol}-USD",
            "broker": "binance",
            "quantity": Decimal("1"),
            "current_price": Decimal(str(price)),
            "asset_class": "crypto",
        }
        for symbol, price in (
            ("BTC", 100),
            ("ETH", 90),
            ("SOL", 80),
            ("XRP", 70),
            ("ADA", 60),
            ("DOT", 50),
        )
    ]
    plan = legacy_reconciliation_plan(
        rows,
        nav=Decimal("100000"),
        policy=BalancePolicy(enabled=True),
    )
    excess = [
        item
        for item in plan
        if item["kind"] == "reduce_excess_factor_expression"
    ]
    assert len(excess) == 1
    assert excess[0]["economic_symbol"] == "DOT-USD"


@pytest.mark.asyncio
async def test_execution_consolidation_is_not_crypto_only() -> None:
    class Adapter:
        def __init__(self, positions):
            self._positions = positions

        async def get_positions(self):
            return self._positions

    held = Position(
        symbol="CME",
        asset_class=AssetClass.EQUITY,
        quantity=Decimal("10"),
        avg_entry_price=Decimal("200"),
        current_price=Decimal("205"),
        unrealised_pnl=Decimal("50"),
        broker="alpaca",
    )
    manager = SimpleNamespace(
        adapters={"alpaca": Adapter([held]), "capitalcom": Adapter([])}
    )
    engine = ExecutionEngine(
        broker_configs={},
        paper_mode=False,
        broker_manager=manager,
        allowed_brokers=["alpaca", "capitalcom"],
    )
    preferred, brokers = await engine._existing_symbol_expression("CME")
    assert preferred == "alpaca"
    assert brokers == ["alpaca"]


def test_final_risk_gate_blocks_redundant_factor_on_any_path() -> None:
    engine = RiskEngine(
        {
            "persist_runtime_state": False,
            "portfolio_balance": {
                "enabled": True,
                "max_incremental_factor_share": 1,
                "max_expressions_per_factor": 1,
            },
        }
    )
    signal = Signal(
        signal_id="balance-1",
        symbol="IVV",
        side="buy",
        strategy="trend_following",
        confidence=0.9,
        suggested_quantity=Decimal("1"),
        suggested_price=Decimal("700"),
        broker="alpaca",
        asset_class="equity",
        timestamp="2026-06-29T12:00:00+00:00",
        metadata={},
    )
    ok, label = engine._check_portfolio_balance(
        signal,
        {
            "portfolio_value": Decimal("100000"),
            "positions": {
                "SPY": {
                    "symbol": "SPY",
                    "quantity": Decimal("10"),
                    "current_price": Decimal("700"),
                    "asset_class": "equity",
                    "broker": "alpaca",
                }
            },
        },
    )
    assert ok is False
    assert label == "portfolio_balance"


@pytest.mark.asyncio
async def test_portfolio_state_holding_age_comes_from_fills_not_fresh_marks() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sf = async_sessionmaker(engine, expire_on_commit=False)
    opened = datetime.now(timezone.utc) - timedelta(days=2)
    marked = datetime.now(timezone.utc)
    async with sf() as session:
        session.add(
            FillLog(
                timestamp=opened,
                broker="alpaca",
                symbol="SPY",
                asset_class="equity",
                side="buy",
                order_type="market",
                quantity=Decimal("1"),
                signed_quantity=Decimal("1"),
                fill_price=Decimal("700"),
                notional=Decimal("700"),
                fee=Decimal("0"),
                reduce_only=False,
                realised_pnl=Decimal("0"),
                position_qty_after=Decimal("1"),
                is_paper=True,
            )
        )
        session.add(
            PositionLog(
                timestamp=marked,
                broker="alpaca",
                symbol="SPY",
                quantity=Decimal("1"),
                avg_entry_price=Decimal("700"),
                current_price=Decimal("710"),
                unrealised_pnl=Decimal("10"),
                asset_class="equity",
            )
        )
        await session.commit()
    state = await _load_portfolio_state(
        sf,
        fallback_portfolio_value=Decimal("100000"),
    )
    assert Decimal(str(state["positions"]["SPY"]["holding_sec"])) > Decimal(
        "172000"
    )
    await engine.dispose()
