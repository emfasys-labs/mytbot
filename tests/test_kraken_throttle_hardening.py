from __future__ import annotations

from decimal import Decimal

import pytest

from brokers.kraken.adapter import KrakenAdapter


class _FakeUser:
    def __init__(self) -> None:
        self.balance_calls = 0
        self.balance_ex_calls = 0

    def get_account_balance(self):
        self.balance_calls += 1
        return {"ZUSD": "123.45"}

    def get_balances(self):
        self.balance_ex_calls += 1
        return {"ZUSD": {"balance": "123.45", "hold_trade": "1.23"}}


@pytest.mark.asyncio
async def test_kraken_balance_cache_reuses_private_snapshot(monkeypatch) -> None:
    monkeypatch.setenv("KRAKEN_BALANCE_CACHE_TTL_SEC", "60")
    adapter = KrakenAdapter(api_key="k", api_secret="s", paper_mode=True)
    user = _FakeUser()
    adapter._private_ok = True
    adapter._user = user

    first = await adapter.get_balance()
    second = await adapter.get_balance()

    assert user.balance_calls == 1
    assert user.balance_ex_calls == 1
    assert first == second
    assert first[0].total == Decimal("123.45")
    assert first[0].reserved == Decimal("1.23")


def test_kraken_default_rest_gap_is_conservative(monkeypatch) -> None:
    monkeypatch.delenv("KRAKEN_REST_MIN_INTERVAL_SEC", raising=False)
    adapter = KrakenAdapter(api_key="k", api_secret="s", paper_mode=True)

    assert adapter._rest_gap._min >= 1.0

