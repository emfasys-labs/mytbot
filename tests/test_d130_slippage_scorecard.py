"""D130 — per-fill slippage capture + the /performance scorecard.

P1: ``record_fill`` now captures ``intended_price`` (the signal's target
    price) and derives a signed ``slippage_bps`` — positive = the fill was
    worse than intended (an execution cost), negative = price improvement.
P2: ``build_performance_scorecard`` aggregates the fills ledger into a
    trade-quality scorecard, and returns ``insufficient_history`` for the
    time-series risk block until ``daily_pnl`` has enough rows.
"""
from __future__ import annotations

from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from api.performance import _percentile, build_performance_scorecard
from storage.fills_ledger import _slippage_bps, record_fill
from storage.models import Base, DailyPnL


# ── P1 — pure slippage math ───────────────────────────────────────────────────


def test_slippage_buy_worse_than_intended_is_positive():
    # Buy intended 100, filled 101 → paid 1% more → +100 bps adverse.
    bps = _slippage_bps(Decimal("100"), Decimal("101"), Decimal("10"))
    assert bps == Decimal("100.0000")


def test_slippage_buy_better_than_intended_is_negative():
    # Buy intended 100, filled 99 → price improvement → -100 bps.
    bps = _slippage_bps(Decimal("100"), Decimal("99"), Decimal("10"))
    assert bps == Decimal("-100.0000")


def test_slippage_sell_worse_than_intended_is_positive():
    # Sell intended 200, filled 190 → received less → adverse → +500 bps.
    bps = _slippage_bps(Decimal("200"), Decimal("190"), Decimal("-5"))
    assert bps == Decimal("500.0000")


def test_slippage_none_when_no_intended_price():
    assert _slippage_bps(None, Decimal("100"), Decimal("1")) is None
    assert _slippage_bps(Decimal("0"), Decimal("100"), Decimal("1")) is None


def test_percentile_interpolates():
    assert _percentile([0.0, 0.0, 100.0, 500.0], 0.5) == 50.0
    assert _percentile([10.0], 0.9) == 10.0
    assert _percentile([], 0.5) is None


# ── P1 — ledger captures the columns ──────────────────────────────────────────


@pytest_asyncio.fixture
async def sf():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    yield maker
    await engine.dispose()


@pytest.mark.asyncio
async def test_record_fill_stores_intended_price_and_slippage(sf):
    row = await record_fill(
        sf, broker="ibkr", symbol="AAPL", side="buy",
        quantity=Decimal("100"), fill_price=Decimal("101"),
        intended_price=Decimal("100"),
    )
    assert row is not None
    assert row.intended_price == Decimal("100")
    assert row.slippage_bps == Decimal("100.0000")


@pytest.mark.asyncio
async def test_record_fill_null_slippage_without_intended(sf):
    row = await record_fill(
        sf, broker="ibkr", symbol="MSFT", side="buy",
        quantity=Decimal("10"), fill_price=Decimal("200"),
    )
    assert row is not None
    assert row.intended_price is None
    assert row.slippage_bps is None


# ── P2 — the performance scorecard ────────────────────────────────────────────


async def _seed_round_trips(sf):
    """Two completed round trips with known P&L and slippage."""
    # AAPL — winner. buy 100@101 (intended 100 → +100bps), sell 100@110.
    await record_fill(sf, broker="ibkr", symbol="AAPL", side="buy",
                      quantity=Decimal("100"), fill_price=Decimal("101"),
                      intended_price=Decimal("100"), strategy="momentum")
    await record_fill(sf, broker="ibkr", symbol="AAPL", side="sell",
                      quantity=Decimal("100"), fill_price=Decimal("110"),
                      intended_price=Decimal("110"), strategy="momentum")
    # MSFT — loser. buy 50@200, sell 50@190 (intended 200 → +500bps adverse).
    await record_fill(sf, broker="ibkr", symbol="MSFT", side="buy",
                      quantity=Decimal("50"), fill_price=Decimal("200"),
                      intended_price=Decimal("200"), strategy="pairs")
    await record_fill(sf, broker="ibkr", symbol="MSFT", side="sell",
                      quantity=Decimal("50"), fill_price=Decimal("190"),
                      intended_price=Decimal("200"), strategy="pairs")


@pytest.mark.asyncio
async def test_scorecard_trade_quality(sf):
    await _seed_round_trips(sf)
    card = await build_performance_scorecard(sf)

    assert card["fills"]["total"] == 4
    assert card["fills"]["opening"] == 2
    assert card["fills"]["closing"] == 2

    tq = card["trade_quality"]
    assert tq["closing_trades"] == 2
    assert tq["wins"] == 1
    assert tq["losses"] == 1
    assert tq["win_rate"] == 0.5
    assert tq["profit_factor"] == 1.8          # 900 / 500
    assert Decimal(tq["expectancy"]) == Decimal("200")   # (900 - 500) / 2

    pnl = card["pnl"]
    assert Decimal(pnl["gross_realised"]) == Decimal("400")
    assert Decimal(pnl["net_realised"]) == Decimal("400")   # no fees
    assert Decimal(pnl["turnover"]) == Decimal("40600")


@pytest.mark.asyncio
async def test_scorecard_slippage_section(sf):
    await _seed_round_trips(sf)
    card = await build_performance_scorecard(sf)

    slip = card["slippage"]
    assert slip["captured_fills"] == 4
    assert slip["coverage_pct"] == 100.0
    assert slip["mean_bps"] == 150.0           # (100 + 0 + 0 + 500) / 4
    assert slip["worst_bps"] == 500.0
    assert slip["best_bps"] == 0.0
    # cost = 100bps·10100 + 500bps·9500 = 101 + 475
    assert Decimal(slip["estimated_cost"]) == Decimal("576")


@pytest.mark.asyncio
async def test_scorecard_attribution(sf):
    await _seed_round_trips(sf)
    card = await build_performance_scorecard(sf)

    by_strategy = {r["key"]: r for r in card["attribution"]["by_strategy"]}
    assert Decimal(by_strategy["momentum"]["net_realised"]) == Decimal("900")
    assert Decimal(by_strategy["pairs"]["net_realised"]) == Decimal("-500")

    top = card["attribution"]["by_symbol_top"]
    assert top[0]["key"] == "AAPL"             # ranked by net, winner first
    assert Decimal(top[-1]["net_realised"]) <= Decimal(top[0]["net_realised"])


@pytest.mark.asyncio
async def test_scorecard_holding_period_present(sf):
    await _seed_round_trips(sf)
    card = await build_performance_scorecard(sf)
    hp = card["holding_period"]
    assert hp is not None
    assert hp["count"] == 2                    # two closing fills


@pytest.mark.asyncio
async def test_scorecard_time_series_insufficient_history(sf):
    await _seed_round_trips(sf)
    card = await build_performance_scorecard(sf)
    ts = card["time_series"]
    assert ts["status"] == "insufficient_history"
    assert ts["metrics"] is None
    assert ts["daily_rows"] == 0
    # And the data-quality block is honest about the tiny sample.
    assert card["data_quality"]["statistically_meaningful"] is False


@pytest.mark.asyncio
async def test_scorecard_time_series_available_with_history(sf):
    # 25 trading days of a steadily growing portfolio.
    async with sf() as session:
        for i in range(25):
            session.add(DailyPnL(
                date=f"2026-04-{i + 1:02d}",
                realised_pnl=Decimal("100"),
                unrealised_pnl=Decimal("0"),
                total_fees=Decimal("0"),
                trade_count=1,
                portfolio_value=Decimal("1000000") + Decimal("1000") * i,
            ))
        await session.commit()

    card = await build_performance_scorecard(sf)
    ts = card["time_series"]
    assert ts["status"] == "available"
    assert ts["daily_rows"] == 25
    m = ts["metrics"]
    assert m is not None
    assert isinstance(m["sharpe"], float)
    assert m["max_drawdown_pct"] == 0.0        # monotonically rising → no DD
    assert m["cagr_pct"] > 0
