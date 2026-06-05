"""D126 — fills ledger: WAC P&L, race-free position quantity, oversell guard.

The 2026-05-21 audit found the `orders` table corrupted by a
snapshot-resurrection race: 79,910 BALL shares sold against only 10,586
ever bought. The `fills` ledger is append-only and the oversell guard
reads its race-free SUM(signed_quantity), making oversell structurally
impossible.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from storage.models import Base, PositionLog
from storage.fills_ledger import (
    _compute_wac,
    available_quantity,
    position_state,
    record_fill,
)


# ── pure WAC math ─────────────────────────────────────────────────────────────


def test_wac_open_long():
    new_qty, new_avg, realised, closing = _compute_wac(
        Decimal("0"), Decimal("0"), Decimal("100"), Decimal("25")
    )
    assert new_qty == Decimal("100")
    assert new_avg == Decimal("25")
    assert realised == Decimal("0")
    assert closing is False


def test_wac_add_to_long_averages_cost():
    # Hold 100 @ 25, buy 100 more @ 27 → 200 @ 26.
    new_qty, new_avg, realised, closing = _compute_wac(
        Decimal("100"), Decimal("25"), Decimal("100"), Decimal("27")
    )
    assert new_qty == Decimal("200")
    assert new_avg == Decimal("26")
    assert realised == Decimal("0")
    assert closing is False


def test_wac_partial_close_long_realises_pnl():
    # Hold 200 @ 26, sell 50 @ 30 → realised = (30-26)*50 = 200.
    new_qty, new_avg, realised, closing = _compute_wac(
        Decimal("200"), Decimal("26"), Decimal("-50"), Decimal("30")
    )
    assert new_qty == Decimal("150")
    assert new_avg == Decimal("26")          # remainder keeps its cost
    assert realised == Decimal("200")
    assert closing is True


def test_wac_full_close_long():
    new_qty, new_avg, realised, closing = _compute_wac(
        Decimal("150"), Decimal("26"), Decimal("-150"), Decimal("28")
    )
    assert new_qty == Decimal("0")
    assert realised == Decimal("300")        # (28-26)*150
    assert closing is True


def test_wac_flip_long_to_short():
    # Hold 100 @ 25, sell 150 @ 30 → close 100 (realise (30-25)*100=500),
    # remaining 50 opens a short at 30.
    new_qty, new_avg, realised, closing = _compute_wac(
        Decimal("100"), Decimal("25"), Decimal("-150"), Decimal("30")
    )
    assert new_qty == Decimal("-50")
    assert new_avg == Decimal("30")          # new short opens at fill price
    assert realised == Decimal("500")
    assert closing is True


def test_wac_short_cover_realises_pnl():
    # Short 100 @ 30, buy 100 @ 25 to cover → realised = (30-25)*100 = 500.
    new_qty, new_avg, realised, closing = _compute_wac(
        Decimal("-100"), Decimal("30"), Decimal("100"), Decimal("25")
    )
    assert new_qty == Decimal("0")
    assert realised == Decimal("500")
    assert closing is True


# ── DB-backed ledger ──────────────────────────────────────────────────────────


@pytest_asyncio.fixture
async def sf():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    yield maker
    await engine.dispose()


@pytest.mark.asyncio
async def test_record_fill_accumulates_quantity(sf):
    await record_fill(sf, broker="ibkr", symbol="AAPL", side="buy",
                      quantity=Decimal("100"), fill_price=Decimal("25"))
    await record_fill(sf, broker="ibkr", symbol="AAPL", side="buy",
                      quantity=Decimal("50"), fill_price=Decimal("27"))
    qty = await available_quantity(sf, "ibkr", "AAPL")
    assert qty == Decimal("150")


@pytest.mark.asyncio
async def test_record_fill_realised_pnl_on_close(sf):
    await record_fill(sf, broker="ibkr", symbol="MSFT", side="buy",
                      quantity=Decimal("100"), fill_price=Decimal("100"))
    row = await record_fill(sf, broker="ibkr", symbol="MSFT", side="sell",
                            quantity=Decimal("40"), fill_price=Decimal("110"))
    assert row is not None
    assert row.realised_pnl == Decimal("400")          # (110-100)*40
    assert row.position_qty_after == Decimal("60")
    assert row.holding_period_sec is not None          # closing fill → holding period set


@pytest.mark.asyncio
async def test_record_fill_rejects_bad_input(sf):
    assert await record_fill(sf, broker="ibkr", symbol="X", side="buy",
                             quantity=Decimal("0"), fill_price=Decimal("10")) is None
    assert await record_fill(sf, broker="ibkr", symbol="X", side="buy",
                             quantity=Decimal("10"), fill_price=Decimal("0")) is None
    assert await record_fill(sf, broker="", symbol="X", side="buy",
                             quantity=Decimal("10"), fill_price=Decimal("10")) is None


@pytest.mark.asyncio
async def test_position_state_reports_count_and_qty(sf):
    qty, count = await position_state(sf, "ibkr", "TSLA")
    assert qty == Decimal("0") and count == 0          # ledger empty → no opinion
    await record_fill(sf, broker="ibkr", symbol="TSLA", side="buy",
                      quantity=Decimal("10"), fill_price=Decimal("200"))
    qty, count = await position_state(sf, "ibkr", "TSLA")
    assert qty == Decimal("10") and count == 1


@pytest.mark.asyncio
async def test_signed_quantity_sum_is_race_free_truth(sf):
    # Buy 100, sell 100 → flat. The ledger can never show a phantom
    # holding because every row is an immutable append.
    await record_fill(sf, broker="ibkr", symbol="BALL", side="buy",
                      quantity=Decimal("100"), fill_price=Decimal("25"))
    await record_fill(sf, broker="ibkr", symbol="BALL", side="sell",
                      quantity=Decimal("100"), fill_price=Decimal("26"))
    qty = await available_quantity(sf, "ibkr", "BALL")
    assert qty == Decimal("0")
    # A further sell would be caught by the oversell guard (held == 0).


@pytest.mark.asyncio
async def test_realised_pnl_sum_matches_round_trip(sf):
    # Full round trip: buy 200 @ 50, sell 200 @ 55 → $1000 gross realised.
    await record_fill(sf, broker="ibkr", symbol="NVDA", side="buy",
                      quantity=Decimal("200"), fill_price=Decimal("50"))
    r1 = await record_fill(sf, broker="ibkr", symbol="NVDA", side="sell",
                           quantity=Decimal("120"), fill_price=Decimal("55"))
    r2 = await record_fill(sf, broker="ibkr", symbol="NVDA", side="sell",
                           quantity=Decimal("80"), fill_price=Decimal("55"))
    assert r1.realised_pnl + r2.realised_pnl == Decimal("1000")
    assert await available_quantity(sf, "ibkr", "NVDA") == Decimal("0")


# ── oversell guard ────────────────────────────────────────────────────────────


def _reduce_only_signal(symbol: str, side: str, qty: Decimal):
    from risk.engine import Signal
    return Signal(
        signal_id=f"close-{symbol}",
        symbol=symbol,
        side=side,
        strategy="intraday_derisk_monitor",
        confidence=1.0,
        suggested_quantity=qty,
        suggested_price=Decimal("25"),
        broker="ibkr",
        asset_class="equity",
        timestamp="2026-05-21T12:00:00+00:00",
        metadata={"reduce_only": True},
    )


@pytest.mark.asyncio
async def test_oversell_guard_allows_when_ledger_empty(sf):
    from execution.engine import ExecutionEngine
    eng = ExecutionEngine(broker_configs={}, paper_mode=True)
    sig = _reduce_only_signal("AAPL", "sell", Decimal("50"))
    ok = await eng._clamp_reduce_only_to_holdings(sf, sig)
    assert ok is True                          # empty ledger → no opinion, allow
    assert sig.suggested_quantity == Decimal("50")


@pytest.mark.asyncio
async def test_oversell_guard_passes_within_holdings(sf):
    from execution.engine import ExecutionEngine
    await record_fill(sf, broker="ibkr", symbol="AAPL", side="buy",
                      quantity=Decimal("100"), fill_price=Decimal("25"))
    eng = ExecutionEngine(broker_configs={}, paper_mode=True)
    sig = _reduce_only_signal("AAPL", "sell", Decimal("60"))
    ok = await eng._clamp_reduce_only_to_holdings(sf, sig)
    assert ok is True
    assert sig.suggested_quantity == Decimal("60")     # within 100 held, unchanged


@pytest.mark.asyncio
async def test_oversell_guard_clamps_to_holdings(sf):
    from execution.engine import ExecutionEngine
    await record_fill(sf, broker="ibkr", symbol="BALL", side="buy",
                      quantity=Decimal("100"), fill_price=Decimal("25"))
    eng = ExecutionEngine(broker_configs={}, paper_mode=True)
    sig = _reduce_only_signal("BALL", "sell", Decimal("6598"))   # the BF-B pattern
    ok = await eng._clamp_reduce_only_to_holdings(sf, sig)
    assert ok is True
    assert sig.suggested_quantity == Decimal("100")    # clamped to actual holding
    assert eng.oversell_guard_clamped == 1


@pytest.mark.asyncio
async def test_oversell_guard_skips_when_flat(sf):
    from execution.engine import ExecutionEngine
    # Round trip → flat, but ledger has fills (authoritative).
    await record_fill(sf, broker="ibkr", symbol="BALL", side="buy",
                      quantity=Decimal("100"), fill_price=Decimal("25"))
    await record_fill(sf, broker="ibkr", symbol="BALL", side="sell",
                      quantity=Decimal("100"), fill_price=Decimal("26"))
    eng = ExecutionEngine(broker_configs={}, paper_mode=True)
    sig = _reduce_only_signal("BALL", "sell", Decimal("100"))
    ok = await eng._clamp_reduce_only_to_holdings(sf, sig)
    assert ok is False                         # nothing to reduce — skip the order


@pytest.mark.asyncio
async def test_oversell_guard_skips_wrong_direction(sf):
    from execution.engine import ExecutionEngine
    await record_fill(sf, broker="ibkr", symbol="AAPL", side="buy",
                      quantity=Decimal("100"), fill_price=Decimal("25"))
    eng = ExecutionEngine(broker_configs={}, paper_mode=True)
    # A reduce-only BUY against a long holding would ADD, not reduce.
    sig = _reduce_only_signal("AAPL", "buy", Decimal("50"))
    ok = await eng._clamp_reduce_only_to_holdings(sf, sig)
    assert ok is False


@pytest.mark.asyncio
async def test_oversell_guard_uses_position_snapshot_for_protective_exit(sf):
    from execution.engine import ExecutionEngine

    # Ledger says flat/opposite after stale rows, but the latest marked
    # position snapshot still contains the open lot the stop-loss is trying
    # to reduce. This mirrors the crypto paper derisk failure mode.
    await record_fill(sf, broker="kraken", symbol="AAVE-USD", side="buy",
                      quantity=Decimal("100"), fill_price=Decimal("100"))
    await record_fill(sf, broker="kraken", symbol="AAVE-USD", side="sell",
                      quantity=Decimal("100"), fill_price=Decimal("101"))
    async with sf() as session:
        session.add(
            PositionLog(
                timestamp=datetime.now(timezone.utc),
                symbol="AAVE-USD",
                broker="kraken",
                quantity=Decimal("42"),
                avg_entry_price=Decimal("100"),
                current_price=Decimal("90"),
                unrealised_pnl=Decimal("-420"),
                asset_class="crypto",
            )
        )
        await session.commit()

    eng = ExecutionEngine(broker_configs={}, paper_mode=True)
    sig = _reduce_only_signal("AAVE-USD", "sell", Decimal("50"))
    sig.broker = "kraken"
    sig.asset_class = "crypto"
    ok = await eng._clamp_reduce_only_to_holdings(sf, sig)

    assert ok is True
    assert sig.suggested_quantity == Decimal("42")
    assert sig.metadata["oversell_guard_snapshot_fallback"] is True
