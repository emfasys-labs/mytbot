"""Unit tests for `_live_broker_prices` — the live-price resolver that feeds
`_compute_live_unrealised_mtm` in the /pnl endpoint.

These tests pin down the fallback behaviour that makes the dashboard equity
curve actually move: whichever connected broker is fastest to return a
positive `get_last_price` wins, slow or zero-returning adapters are ignored,
and a complete failure returns an empty map (so the caller falls back to
FeatureSnapshot / PositionLog).
"""

from __future__ import annotations

import asyncio
from decimal import Decimal

import pytest


@pytest.fixture(autouse=True)
def _clear_last_good_px():
    """Isolate the module-global last-good-price cache between tests so a
    quote cached by one test can't carry-forward into another."""
    import api.server as server

    server._LAST_GOOD_PX.clear()
    yield
    server._LAST_GOOD_PX.clear()


class _FakePosition:
    def __init__(self, symbol: str, broker: str = "ibkr") -> None:
        self.symbol = symbol
        self.broker = broker


class _FakeAdapter:
    def __init__(self, price: Decimal | int | float | None, delay: float = 0.0, raise_exc: bool = False) -> None:
        self._price = price
        self._delay = delay
        self._raise = raise_exc

    async def get_last_price(self, symbol: str) -> Decimal:
        if self._delay:
            await asyncio.sleep(self._delay)
        if self._raise:
            raise RuntimeError("adapter down")
        return Decimal(str(self._price)) if self._price is not None else Decimal(0)


class _FakeBM:
    def __init__(self, adapters: dict[str, _FakeAdapter]) -> None:
        self.adapters = adapters


class _FakeOrch:
    def __init__(self, bm: _FakeBM | None) -> None:
        self._broker_manager = bm


def _install_orch(monkeypatch: pytest.MonkeyPatch, bm: _FakeBM | None) -> None:
    import api.server as server

    monkeypatch.setattr(server, "_get_orchestrator", lambda: _FakeOrch(bm))


def test_no_orchestrator_returns_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    import api.server as server

    monkeypatch.setattr(server, "_get_orchestrator", lambda: None)
    out = asyncio.run(server._live_broker_prices([_FakePosition("AAPL")]))
    assert out == {}


def test_no_broker_manager_returns_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    import api.server as server

    _install_orch(monkeypatch, None)
    out = asyncio.run(server._live_broker_prices([_FakePosition("AAPL")]))
    assert out == {}


def test_deterministic_median_positive_quote(monkeypatch: pytest.MonkeyPatch) -> None:
    """``_live_broker_prices`` was deliberately changed from a 'first
    non-zero wins' latency race to a DETERMINISTIC MEDIAN of positive broker
    quotes (more robust accounting basis — see api/server.py:~496). With two
    positive quotes the result is their median, independent of adapter speed.
    """
    import api.server as server

    bm = _FakeBM(
        {
            "alpaca": _FakeAdapter(Decimal("100.25"), delay=0.0),
            "ibkr": _FakeAdapter(Decimal("100.10"), delay=0.5),
        }
    )
    _install_orch(monkeypatch, bm)
    out = asyncio.run(server._live_broker_prices([_FakePosition("AAPL", broker="ibkr")]))
    # median(100.25, 100.10) = 100.175 — no longer a speed race.
    assert out == {"AAPL": Decimal("100.175")}


def test_zero_adapter_is_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    """An adapter that returns 0 (e.g. symbol not supported) is ignored and a
    later adapter with a real price wins."""
    import api.server as server

    bm = _FakeBM(
        {
            "binance": _FakeAdapter(Decimal(0), delay=0.0),       # fastest but no price
            "bybit": _FakeAdapter(Decimal(0), delay=0.01),         # also no price
            "alpaca": _FakeAdapter(Decimal("42.13"), delay=0.05),  # real price
        }
    )
    _install_orch(monkeypatch, bm)
    out = asyncio.run(server._live_broker_prices([_FakePosition("AAPL")]))
    assert out == {"AAPL": Decimal("42.13")}


def test_exception_adapter_does_not_block(monkeypatch: pytest.MonkeyPatch) -> None:
    """Raising adapter is swallowed — the surviving adapter's price wins."""
    import api.server as server

    bm = _FakeBM(
        {
            "kraken": _FakeAdapter(None, raise_exc=True),
            "alpaca": _FakeAdapter(Decimal("77.77"), delay=0.01),
        }
    )
    _install_orch(monkeypatch, bm)
    out = asyncio.run(server._live_broker_prices([_FakePosition("COHR")]))
    assert out == {"COHR": Decimal("77.77")}


def test_all_adapters_zero_returns_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    """If every adapter returns 0 the caller must see no price (so it falls
    back to FeatureSnapshot / PositionLog)."""
    import api.server as server

    bm = _FakeBM(
        {
            "binance": _FakeAdapter(Decimal(0)),
            "bybit": _FakeAdapter(Decimal(0)),
            "kraken": _FakeAdapter(Decimal(0)),
        }
    )
    _install_orch(monkeypatch, bm)
    out = asyncio.run(server._live_broker_prices([_FakePosition("AAPL")]))
    assert out == {}


def test_last_good_price_carries_forward_on_transient_miss(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The fix for the -$5247 <-> -$245 flicker: a symbol priced moments
    ago must NOT vanish when this cycle's probes all fail/timeout — its
    last good quote carries forward within TTL so the unrealised total
    stays steady instead of collapsing to a fabricated near-flat figure."""
    import api.server as server

    pos = [_FakePosition("AAPL")]

    # Cycle 1: real price -> cached.
    _install_orch(monkeypatch, _FakeBM({"alpaca": _FakeAdapter(Decimal("100.50"))}))
    assert asyncio.run(server._live_broker_prices(pos)) == {"AAPL": Decimal("100.50")}

    # Cycle 2: every probe returns 0 (transient outage) -> carry-forward.
    _install_orch(monkeypatch, _FakeBM({"alpaca": _FakeAdapter(Decimal(0))}))
    assert asyncio.run(server._live_broker_prices(pos)) == {"AAPL": Decimal("100.50")}

    # With TTL disabled, a miss is NOT carried (old behaviour preserved).
    monkeypatch.setenv("LIVE_PX_LAST_GOOD_TTL_SEC", "0")
    assert asyncio.run(server._live_broker_prices(pos)) == {}


def test_multiple_positions_each_resolved(monkeypatch: pytest.MonkeyPatch) -> None:
    """Per-symbol resolution is independent; each position gets its own price."""
    import api.server as server

    class _PerSymAdapter:
        def __init__(self, table: dict[str, Decimal]) -> None:
            self._table = table

        async def get_last_price(self, symbol: str) -> Decimal:
            return self._table.get(symbol, Decimal(0))

    bm = _FakeBM(
        {
            "alpaca": _PerSymAdapter({"AAPL": Decimal("190.10"), "MSFT": Decimal("410.25")}),
        }
    )
    _install_orch(monkeypatch, bm)
    out = asyncio.run(
        server._live_broker_prices([_FakePosition("AAPL"), _FakePosition("MSFT"), _FakePosition("NVDA")])
    )
    assert out == {"AAPL": Decimal("190.10"), "MSFT": Decimal("410.25")}


def test_timeout_does_not_leak(monkeypatch: pytest.MonkeyPatch) -> None:
    """A hanging adapter must time out and not prevent a fast adapter from winning."""
    import api.server as server

    bm = _FakeBM(
        {
            "slow": _FakeAdapter(Decimal("1.0"), delay=5.0),
            "fast": _FakeAdapter(Decimal("2.0"), delay=0.02),
        }
    )
    _install_orch(monkeypatch, bm)
    out = asyncio.run(server._live_broker_prices([_FakePosition("X")]))
    assert out == {"X": Decimal("2.0")}


def test_paper_mtm_ignores_native_broker_unrealised(monkeypatch: pytest.MonkeyPatch) -> None:
    """Paper mode uses the paper ledger as the book of record for P&L."""
    import api.server as server

    class _Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return False

    monkeypatch.setattr(server, "APP_ENV", "paper")
    monkeypatch.setattr(server, "_live_broker_unrealised_total", lambda: _async_decimal("999"))
    monkeypatch.setattr(server, "_latest_position_log_rows", lambda *_a, **_kw: _async_value([]))

    out = asyncio.run(server._compute_live_unrealised_mtm(lambda: _Session()))
    assert out == Decimal("0")


def test_live_mtm_can_use_native_broker_unrealised(monkeypatch: pytest.MonkeyPatch) -> None:
    """Live mode still trusts broker-native unrealised P&L when available."""
    import api.server as server

    monkeypatch.setattr(server, "APP_ENV", "live")
    monkeypatch.setattr(server, "_live_broker_unrealised_total", lambda: _async_decimal("123.45"))

    out = asyncio.run(server._compute_live_unrealised_mtm(lambda: None))
    assert out == Decimal("123.45")


async def _async_decimal(value: str) -> Decimal:
    return Decimal(value)


async def _async_value(value):
    return value
