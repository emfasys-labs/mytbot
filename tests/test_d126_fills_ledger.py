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


# ── D231 (P1.5) — opening_strategy / opening_signal_id attribution ─────────


@pytest.mark.asyncio
async def test_opening_strategy_stamped_on_fresh_open(sf):
    row = await record_fill(sf, broker="ibkr", symbol="MSFT", side="buy",
                            quantity=Decimal("100"), fill_price=Decimal("100"),
                            strategy="mean_reversion", signal_id="sig-1")
    assert row.opening_strategy == "mean_reversion"
    assert row.opening_signal_id == "sig-1"


@pytest.mark.asyncio
async def test_opening_strategy_propagates_across_adds(sf):
    # Opened by mean_reversion; a later add is nominally attributed to
    # trend_following at the strategy layer but the STREAK still belongs
    # to whoever opened it.
    await record_fill(sf, broker="ibkr", symbol="MSFT", side="buy",
                      quantity=Decimal("100"), fill_price=Decimal("100"),
                      strategy="mean_reversion", signal_id="sig-1")
    add = await record_fill(sf, broker="ibkr", symbol="MSFT", side="buy",
                            quantity=Decimal("50"), fill_price=Decimal("105"),
                            strategy="trend_following", signal_id="sig-2")
    assert add.opening_strategy == "mean_reversion"
    assert add.opening_signal_id == "sig-1"
    assert add.strategy == "trend_following"        # own-fill attribution unchanged


@pytest.mark.asyncio
async def test_opening_strategy_survives_onto_exit_mechanism_close(sf):
    # This is the exact gap the review found: a close tagged with the EXIT
    # mechanism's name (stop_loss_monitor) can now still be traced back to
    # the strategy that opened the lot (mean_reversion).
    await record_fill(sf, broker="ibkr", symbol="MSFT", side="buy",
                      quantity=Decimal("100"), fill_price=Decimal("100"),
                      strategy="mean_reversion", signal_id="sig-1")
    close = await record_fill(sf, broker="ibkr", symbol="MSFT", side="sell",
                              quantity=Decimal("100"), fill_price=Decimal("90"),
                              strategy="stop_loss_monitor", signal_id="stoploss-1")
    assert close.strategy == "stop_loss_monitor"     # exit mechanism, unchanged
    assert close.opening_strategy == "mean_reversion"  # true origin, new
    assert close.opening_signal_id == "sig-1"
    assert close.realised_pnl == Decimal("-1000")


@pytest.mark.asyncio
async def test_opening_strategy_resets_after_flat_reopen(sf):
    await record_fill(sf, broker="ibkr", symbol="MSFT", side="buy",
                      quantity=Decimal("100"), fill_price=Decimal("100"),
                      strategy="mean_reversion", signal_id="sig-1")
    await record_fill(sf, broker="ibkr", symbol="MSFT", side="sell",
                      quantity=Decimal("100"), fill_price=Decimal("110"),
                      strategy="profit_harvest_monitor", signal_id="pf-1")
    reopen = await record_fill(sf, broker="ibkr", symbol="MSFT", side="buy",
                               quantity=Decimal("40"), fill_price=Decimal("108"),
                               strategy="trend_following", signal_id="sig-3")
    assert reopen.opening_strategy == "trend_following"   # new streak, new owner
    assert reopen.opening_signal_id == "sig-3"


@pytest.mark.asyncio
async def test_opening_strategy_resets_on_flip(sf):
    # Long 100 opened by mean_reversion; sell 150 flips to short 50 —
    # the flip fill becomes the new streak's opener.
    await record_fill(sf, broker="ibkr", symbol="MSFT", side="buy",
                      quantity=Decimal("100"), fill_price=Decimal("100"),
                      strategy="mean_reversion", signal_id="sig-1")
    flip = await record_fill(sf, broker="ibkr", symbol="MSFT", side="sell",
                             quantity=Decimal("150"), fill_price=Decimal("95"),
                             strategy="trend_breakout", signal_id="sig-flip")
    assert flip.position_qty_after == Decimal("-50")
    assert flip.opening_strategy == "trend_breakout"
    assert flip.opening_signal_id == "sig-flip"


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
async def test_oversell_guard_never_resurrects_flat_ledger_from_stale_snapshot(sf):
    from execution.engine import ExecutionEngine

    # Ledger is flat but a stale marked snapshot still contains the old lot.
    # The append-only ledger must win or the protective exit becomes a short.
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

    assert ok is False
    assert sig.suggested_quantity == Decimal("50")


# ── D231 (P1.3) — dust-floor close upgrade ──────────────────────────────────


@pytest.mark.asyncio
async def test_dust_floor_upgrades_partial_trim_to_full_close(sf):
    """A partial trim that would leave a sub-minimum-order-size remainder
    closes the whole position instead of stranding un-tradeable dust."""
    from execution.engine import ExecutionEngine

    # Held 100 @ $25 = $2,500. Equity floor is $65 -> 2.6 shares.
    await record_fill(sf, broker="ibkr", symbol="AAPL", side="buy",
                      quantity=Decimal("100"), fill_price=Decimal("25"))
    eng = ExecutionEngine(broker_configs={}, paper_mode=True)
    # Trim 98 -> would leave 2 shares (~$50), under the $65 equity floor.
    sig = _reduce_only_signal("AAPL", "sell", Decimal("98"))
    ok = await eng._clamp_reduce_only_to_holdings(sf, sig)

    assert ok is True
    assert sig.suggested_quantity == Decimal("100")     # upgraded to full close
    assert sig.metadata["dust_floor_close_upgrade"] is True
    assert sig.metadata["close_only"] is True
    assert eng.dust_floor_close_upgrades == 1


@pytest.mark.asyncio
async def test_dust_floor_leaves_normal_trim_untouched(sf):
    """A trim whose remainder clears the minimum order size is left alone."""
    from execution.engine import ExecutionEngine

    # Held 100 @ $25 = $2,500. Trim 50 -> leaves 50 (~$1,250), well above $65.
    await record_fill(sf, broker="ibkr", symbol="AAPL", side="buy",
                      quantity=Decimal("100"), fill_price=Decimal("25"))
    eng = ExecutionEngine(broker_configs={}, paper_mode=True)
    sig = _reduce_only_signal("AAPL", "sell", Decimal("50"))
    ok = await eng._clamp_reduce_only_to_holdings(sf, sig)

    assert ok is True
    assert sig.suggested_quantity == Decimal("50")      # untouched
    assert "dust_floor_close_upgrade" not in sig.metadata
    assert eng.dust_floor_close_upgrades == 0


@pytest.mark.asyncio
async def test_dust_floor_skipped_when_price_missing(sf):
    """No price to value the remainder with -> skip the check, don't crash."""
    from execution.engine import ExecutionEngine

    await record_fill(sf, broker="ibkr", symbol="AAPL", side="buy",
                      quantity=Decimal("100"), fill_price=Decimal("25"))
    eng = ExecutionEngine(broker_configs={}, paper_mode=True)
    sig = _reduce_only_signal("AAPL", "sell", Decimal("98"))
    sig.suggested_price = None
    ok = await eng._clamp_reduce_only_to_holdings(sf, sig)

    assert ok is True
    assert sig.suggested_quantity == Decimal("98")      # not upgraded
    assert eng.dust_floor_close_upgrades == 0


@pytest.mark.asyncio
async def test_dust_floor_does_not_affect_full_close(sf):
    """A trim that already closes the full position is a no-op for this check."""
    from execution.engine import ExecutionEngine

    await record_fill(sf, broker="ibkr", symbol="AAPL", side="buy",
                      quantity=Decimal("100"), fill_price=Decimal("25"))
    eng = ExecutionEngine(broker_configs={}, paper_mode=True)
    sig = _reduce_only_signal("AAPL", "sell", Decimal("100"))
    ok = await eng._clamp_reduce_only_to_holdings(sf, sig)

    assert ok is True
    assert sig.suggested_quantity == Decimal("100")
    assert eng.dust_floor_close_upgrades == 0
