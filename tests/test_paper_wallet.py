"""
tests/test_paper_wallet.py
===========================
Synthetic crypto paper wallet — makes Kraken/Binance/Bybit (no native
paper account) carry real (paper) capital so their P&L flows into NAV and
reconciles with the daily_pnl ledger. Off via CRYPTO_PAPER_WALLET=0.
"""

from __future__ import annotations

import asyncio
from decimal import Decimal
from pathlib import Path

import pytest

import system.paper_wallet as pw


# ── config / gating ─────────────────────────────────────────────────────


def test_enabled_default_on_and_off_switch(monkeypatch) -> None:
    monkeypatch.delenv("CRYPTO_PAPER_WALLET", raising=False)
    assert pw.crypto_paper_wallet_enabled() is True
    monkeypatch.setenv("CRYPTO_PAPER_WALLET", "0")
    assert pw.crypto_paper_wallet_enabled() is False


def test_seed_default_and_overrides(monkeypatch) -> None:
    monkeypatch.delenv("CRYPTO_PAPER_WALLET_USD", raising=False)
    monkeypatch.delenv("PAPER_WALLET_KRAKEN_USD", raising=False)
    assert pw.seed_for("kraken") == Decimal("50000")
    monkeypatch.setenv("CRYPTO_PAPER_WALLET_USD", "75000")
    assert pw.seed_for("binance") == Decimal("75000")
    monkeypatch.setenv("PAPER_WALLET_KRAKEN_USD", "120000")
    assert pw.seed_for("kraken") == Decimal("120000")  # per-venue wins
    assert pw.seed_for("binance") == Decimal("75000")
    monkeypatch.setenv("PAPER_WALLET_KRAKEN_USD", "garbage")
    assert pw.seed_for("kraken") == Decimal("50000")  # safe fallback


# ── venue_equity (file-backed read) ─────────────────────────────────────


@pytest.fixture()
def _wallet_file(tmp_path, monkeypatch):
    p = tmp_path / "paper_wallet.json"
    monkeypatch.setattr(pw, "_WALLET_FILE", Path(p))
    monkeypatch.delenv("CRYPTO_PAPER_WALLET", raising=False)
    monkeypatch.setenv("CRYPTO_PAPER_WALLET_USD", "50000")
    return p


def test_venue_equity_disabled_returns_none(_wallet_file, monkeypatch) -> None:
    monkeypatch.setenv("CRYPTO_PAPER_WALLET", "0")
    assert pw.venue_equity("kraken") is None


def test_venue_equity_non_crypto_returns_none(_wallet_file) -> None:
    assert pw.venue_equity("ibkr") is None
    assert pw.venue_equity("alpaca") is None


def test_venue_equity_no_snapshot_falls_back_to_seed(_wallet_file) -> None:
    # No snapshot file yet → NAV must still be sane (seed).
    assert pw.venue_equity("kraken") == Decimal("50000")


def test_write_then_read_roundtrip(_wallet_file) -> None:
    pw.write_snapshot(
        {
            "kraken": {"seed": "50000", "realised": "1200", "unrealised": "-300", "equity": "50900"},
            "binance": {"seed": "50000", "realised": "0", "unrealised": "0", "equity": "50000"},
        }
    )
    assert _wallet_file.exists()
    assert pw.venue_equity("kraken") == Decimal("50900")
    assert pw.venue_equity("binance") == Decimal("50000")
    # Venue present in CRYPTO set but absent from snapshot → seed.
    assert pw.venue_equity("bybit") == Decimal("50000")


# ── compute_venue_equity (ledger-derived, FIFO) ─────────────────────────


class _Row:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return self

    def all(self):
        return self._rows


class _FakeSession:
    """1st execute -> OrderLog rows; 2nd -> PositionLog (open) rows."""

    def __init__(self, orders, positions):
        self._orders = orders
        self._positions = positions
        self._n = 0

    async def execute(self, _stmt):
        self._n += 1
        return _Result(self._orders if self._n == 1 else self._positions)


def test_compute_venue_equity_seed_plus_realised_plus_unrealised(monkeypatch) -> None:
    monkeypatch.setenv("CRYPTO_PAPER_WALLET_USD", "50000")
    # Buy 10 ETH @ 100 (fee 1), sell 10 @ 90 (fee 2) -> realised = -100 - 3
    orders = [
        _Row(broker="kraken", symbol="ETH-USD", side="buy", filled_quantity=10,
             quantity=10, avg_fill_price=100, limit_price=100, fee=1,
             status="filled", timestamp=1, id=1),
        _Row(broker="kraken", symbol="ETH-USD", side="sell", filled_quantity=10,
             quantity=10, avg_fill_price=90, limit_price=90, fee=2,
             status="filled", timestamp=2, id=2),
    ]
    # One still-open position carrying -250 unrealised.
    positions = [
        _Row(symbol="SOL-USD", quantity=Decimal("5"), unrealised_pnl=Decimal("-250")),
        _Row(symbol="XRP-USD", quantity=Decimal("0"), unrealised_pnl=Decimal("999")),  # closed, ignored
    ]
    out = asyncio.run(pw.compute_venue_equity(_FakeSession(orders, positions), "kraken"))
    assert out["seed"] == "50000"
    # Only the CLOSING fill's fee (2) enters realised — identical modelling
    # to run_m3._compute_today_realised_pnl (opening-leg fee not in
    # realised), which is exactly what keeps this consistent with
    # daily_pnl. gross = (90-100)*10 = -100 ; realised = -100 - 2 = -102.
    assert round(Decimal(out["realised"]), 2) == Decimal("-102.00")
    assert round(Decimal(out["unrealised"]), 2) == Decimal("-250.00")
    # equity = 50000 - 102 - 250 = 49648
    assert round(Decimal(out["equity"]), 2) == Decimal("49648.00")


def test_compute_venue_equity_degrades_to_seed_on_error(monkeypatch) -> None:
    monkeypatch.setenv("CRYPTO_PAPER_WALLET_USD", "50000")

    class _Boom:
        async def execute(self, _):
            raise RuntimeError("db down")

    out = asyncio.run(pw.compute_venue_equity(_Boom(), "kraken"))
    assert out["equity"] == "50000"  # never breaks NAV
    assert out["realised"] == "0"


def test_negative_equity_floored_at_zero(monkeypatch) -> None:
    monkeypatch.setenv("CRYPTO_PAPER_WALLET_USD", "100")
    orders = [
        _Row(broker="kraken", symbol="ETH-USD", side="buy", filled_quantity=10,
             quantity=10, avg_fill_price=100, limit_price=100, fee=0,
             status="filled", timestamp=1, id=1),
        _Row(broker="kraken", symbol="ETH-USD", side="sell", filled_quantity=10,
             quantity=10, avg_fill_price=10, limit_price=10, fee=0,
             status="filled", timestamp=2, id=2),  # realised -900
    ]
    out = asyncio.run(pw.compute_venue_equity(_FakeSession(orders, []), "kraken"))
    assert Decimal(out["realised"]) == Decimal("-900")
    assert out["equity"] == "0"  # 100 - 900 floored to 0, never negative NAV
