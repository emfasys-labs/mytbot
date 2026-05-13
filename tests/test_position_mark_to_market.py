"""
tests/test_position_mark_to_market.py
======================================

Locks in the two fixes for ``daily_pnl_unrealised_differs_from_open_book``:

1. ``_compute_unrealised_pnl`` produces the right number across long/short,
   missing-price, and zero-quantity cases.
2. ``_persist_position_snapshot`` writes the computed unrealised P&L to the
   ``PositionLog`` row (previously hard-coded to ``Decimal('0')``).
3. ``_refresh_position_marks_and_persist`` re-marks open positions using a
   supplied price oracle and writes fresh rows.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from run_m3 import (
    _compute_unrealised_pnl,
    _persist_position_snapshot,
    _refresh_position_marks_and_persist,
)


def test_compute_unrealised_long_position_in_profit() -> None:
    pnl = _compute_unrealised_pnl(Decimal("100"), Decimal("110"), Decimal("100"))
    assert pnl == Decimal("1000")


def test_compute_unrealised_short_position_in_profit() -> None:
    # Short 100 @ 100, current 90 → profit = (90-100) * -100 = +1000
    pnl = _compute_unrealised_pnl(Decimal("-100"), Decimal("90"), Decimal("100"))
    assert pnl == Decimal("1000")


def test_compute_unrealised_long_position_in_loss() -> None:
    pnl = _compute_unrealised_pnl(Decimal("50"), Decimal("80"), Decimal("100"))
    assert pnl == Decimal("-1000")


def test_compute_unrealised_zero_quantity_returns_zero() -> None:
    assert _compute_unrealised_pnl(Decimal("0"), Decimal("100"), Decimal("90")) == Decimal("0")


def test_compute_unrealised_missing_price_returns_zero() -> None:
    assert _compute_unrealised_pnl(Decimal("10"), Decimal("0"), Decimal("100")) == Decimal("0")
    assert _compute_unrealised_pnl(Decimal("10"), Decimal("100"), Decimal("0")) == Decimal("0")


@pytest.mark.asyncio
async def test_persist_position_snapshot_writes_computed_unrealised(monkeypatch) -> None:
    """``_persist_position_snapshot`` must put a real PnL on each row, not zero."""
    captured: list[dict] = []

    class _FakeSession:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            return False
        def add(self, row):
            captured.append({
                "symbol": row.symbol,
                "quantity": row.quantity,
                "current_price": row.current_price,
                "avg_entry_price": row.avg_entry_price,
                "unrealised_pnl": row.unrealised_pnl,
            })
        async def commit(self):
            pass

    def _factory():
        return _FakeSession()

    state = {
        "positions": {
            "AAPL": {
                "symbol": "AAPL", "broker": "ibkr", "asset_class": "equity",
                "quantity": Decimal("100"),
                "avg_entry_price": Decimal("150"),
                "current_price": Decimal("160"),
            },
            "TSLA_SHORT": {
                "symbol": "TSLA", "broker": "ibkr", "asset_class": "equity",
                "quantity": Decimal("-50"),
                "avg_entry_price": Decimal("200"),
                "current_price": Decimal("180"),
            },
        }
    }

    await _persist_position_snapshot(_factory, state)
    by_sym = {r["symbol"]: r for r in captured}
    # Long AAPL: (160-150)*100 = +1000
    assert by_sym["AAPL"]["unrealised_pnl"] == Decimal("1000")
    # Short TSLA: (180-200)*(-50) = +1000
    assert by_sym["TSLA"]["unrealised_pnl"] == Decimal("1000")


@pytest.mark.asyncio
async def test_refresh_position_marks_uses_price_oracle(monkeypatch) -> None:
    """The mark-to-market sweep must call the oracle and re-persist."""
    # Stub the DB layer: latest_rows returns two open positions.
    class _Row:
        def __init__(self, symbol, broker, qty, avg, cur, ac="equity"):
            self.symbol = symbol
            self.broker = broker
            self.quantity = qty
            self.avg_entry_price = avg
            self.current_price = cur
            self.asset_class = ac
            self.instrument_metadata = None

    fake_rows = [
        _Row("AAPL", "ibkr", Decimal("100"), Decimal("150"), Decimal("155")),
        _Row("MSFT", "ibkr", Decimal("50"), Decimal("400"), Decimal("390")),
    ]
    written: list[dict] = []

    class _Scalars:
        def __init__(self, rows): self._rows = rows
        def all(self): return self._rows
        def first(self): return self._rows[0] if self._rows else None

    class _Result:
        def __init__(self, rows): self._rows = rows
        def scalars(self): return _Scalars(self._rows)

    class _FakeSession:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def execute(self, _stmt):
            return _Result(fake_rows)
        def add(self, row):
            written.append({
                "symbol": row.symbol,
                "quantity": row.quantity,
                "current_price": row.current_price,
                "avg_entry_price": row.avg_entry_price,
                "unrealised_pnl": row.unrealised_pnl,
            })
        async def commit(self):
            pass

    def _factory():
        return _FakeSession()

    # Oracle returns fresh prices that differ from row.current_price
    async def _oracle(sym: str) -> Decimal:
        return {"AAPL": Decimal("170"), "MSFT": Decimal("420")}.get(sym, Decimal("0"))

    n = await _refresh_position_marks_and_persist(
        _factory, timeframe="1h", price_oracle=_oracle,
    )
    assert n == 2
    by_sym = {r["symbol"]: r for r in written}
    # AAPL: (170-150)*100 = 2000
    assert by_sym["AAPL"]["current_price"] == Decimal("170")
    assert by_sym["AAPL"]["unrealised_pnl"] == Decimal("2000")
    # MSFT: (420-400)*50 = 1000
    assert by_sym["MSFT"]["current_price"] == Decimal("420")
    assert by_sym["MSFT"]["unrealised_pnl"] == Decimal("1000")


@pytest.mark.asyncio
async def test_refresh_skips_zero_quantity_rows() -> None:
    """Closed positions (qty=0) must not be re-persisted."""
    class _Row:
        def __init__(self, **kw): self.__dict__.update(kw)

    fake_rows = [
        _Row(symbol="AAPL", broker="ibkr", quantity=Decimal("0"),
             avg_entry_price=Decimal("150"), current_price=Decimal("0"),
             asset_class="equity", instrument_metadata=None),
    ]
    written: list[dict] = []

    class _Scalars:
        def __init__(self, rows): self._rows = rows
        def all(self): return self._rows
    class _Result:
        def __init__(self, rows): self._rows = rows
        def scalars(self): return _Scalars(self._rows)
    class _FakeSession:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def execute(self, _stmt): return _Result(fake_rows)
        def add(self, row): written.append(row)
        async def commit(self): pass

    async def _oracle(sym): return Decimal("200")

    n = await _refresh_position_marks_and_persist(
        lambda: _FakeSession(), timeframe="1h", price_oracle=_oracle,
    )
    assert n == 0
    assert written == []
