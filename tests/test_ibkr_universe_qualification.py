from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

import pytest

from brokers.base import Order, OrderSide, OrderStatus, OrderType
from brokers.ibkr.adapter import IBKRAdapter
from brokers.ibkr.qualification import IBKRQualificationCache, IBKRQualificationRecord
from brokers.ibkr.universe import ibkr_supported_symbol_seed, load_ibkr_universe


def test_ibkr_universe_config_extends_curated_seed() -> None:
    entries = load_ibkr_universe()
    symbols = {entry.broker_symbol for entry in entries}
    assert "SPY" in symbols
    assert "EUR.USD" in symbols
    assert "USD.CHF" in symbols
    assert len(symbols) >= 60
    assert ibkr_supported_symbol_seed() == list(dict.fromkeys(ibkr_supported_symbol_seed()))


def test_ibkr_qualification_cache_roundtrip(tmp_path) -> None:
    path = tmp_path / "ibkr_contracts.json"
    cache = IBKRQualificationCache(path)
    cache.upsert(
        IBKRQualificationRecord(
            symbol="SPY",
            asset_class="etf",
            status="qualified",
            broker_symbol="SPY",
            sec_type="STK",
            exchange="SMART",
            currency="USD",
            con_id=756733,
            local_symbol="SPY",
            qualified_at="2026-05-15T12:00:00Z",
        )
    )

    reloaded = IBKRQualificationCache(path)
    rec = reloaded.get("SPY", "etf")
    assert rec is not None
    assert rec.is_qualified()
    assert rec.con_id == 756733


def test_ibkr_symbol_to_contract_handles_dotted_forex() -> None:
    adapter = IBKRAdapter(paper_mode=True)
    contract = adapter._symbol_to_contract("EUR.USD")
    assert contract.secType == "CASH"
    assert contract.symbol == "EUR"
    assert contract.currency == "USD"


def test_ibkr_symbol_to_contract_handles_canonical_paxos_crypto() -> None:
    adapter = IBKRAdapter(paper_mode=True)

    contract = adapter._symbol_to_contract("BTC-USD")

    assert contract.secType == "CRYPTO"
    assert contract.symbol == "BTC"
    assert contract.exchange == "PAXOS"
    assert contract.currency == "USD"


class _FakeIB:
    def __init__(self, qualified: bool) -> None:
        self.qualified = qualified
        self.orders = []
        self.qualify_calls = []

    def isConnected(self) -> bool:
        return True

    async def qualifyContractsAsync(self, contract):
        self.qualify_calls.append(contract)
        if not self.qualified:
            return []
        contract.conId = 12345
        contract.localSymbol = contract.symbol
        contract.tradingClass = contract.symbol
        contract.primaryExchange = "ARCA"
        return [contract]

    def placeOrder(self, contract, order):
        self.orders.append((contract, order))
        return SimpleNamespace(
            order=SimpleNamespace(
                permId=99,
                orderId=1,
                orderRef="cid-1",
                totalQuantity=order.totalQuantity,
            ),
            orderStatus=SimpleNamespace(
                status="Filled",
                filled=order.totalQuantity,
                remaining=0,
                avgFillPrice=100,
            ),
            commissionReport=SimpleNamespace(commission=0),
        )

    def trades(self):
        return []


@pytest.mark.asyncio
async def test_ibkr_place_order_rejects_unqualified_contract(tmp_path) -> None:
    adapter = IBKRAdapter(paper_mode=True)
    adapter._ib = _FakeIB(qualified=False)
    adapter._qualification_cache = IBKRQualificationCache(tmp_path / "cache.json")
    order = Order(
        symbol="NOTREAL",
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        quantity=Decimal("1"),
        client_order_id="cid-1",
    )

    result = await adapter.place_order(order)

    assert result.status == OrderStatus.REJECTED
    assert adapter._ib.orders == []
    rec = adapter._qualification_cache.get("NOTREAL")
    assert rec is not None
    assert rec.status == "failed"


@pytest.mark.asyncio
async def test_ibkr_qualify_symbol_persists_success(tmp_path) -> None:
    adapter = IBKRAdapter(paper_mode=True)
    adapter._ib = _FakeIB(qualified=True)
    adapter._qualification_cache = IBKRQualificationCache(tmp_path / "cache.json")

    rec = await adapter.qualify_symbol("SPY", "etf")

    assert rec.is_qualified()
    assert rec.con_id == 12345
    assert adapter._qualification_cache.get("SPY", "etf").con_id == 12345


@pytest.mark.asyncio
async def test_ibkr_qualify_symbol_rejects_unsupported_crypto_locally(tmp_path) -> None:
    adapter = IBKRAdapter(paper_mode=True)
    adapter._ib = _FakeIB(qualified=True)
    adapter._qualification_cache = IBKRQualificationCache(tmp_path / "cache.json")

    rec = await adapter.qualify_symbol("AAVE-USD", "crypto")

    assert rec.status == "failed"
    assert rec.error == "unsupported IBKR PAXOS crypto symbol"
    assert adapter._ib.qualify_calls == []


@pytest.mark.asyncio
async def test_ibkr_last_price_skips_unsupported_crypto_locally(tmp_path) -> None:
    adapter = IBKRAdapter(paper_mode=True)
    adapter._ib = _FakeIB(qualified=True)
    adapter._qualification_cache = IBKRQualificationCache(tmp_path / "cache.json")

    px = await adapter.get_last_price("AAVE-USD")

    assert px == Decimal("0")
    assert adapter._ib.qualify_calls == []
