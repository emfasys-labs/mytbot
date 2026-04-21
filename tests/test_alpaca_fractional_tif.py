"""Alpaca adapter — fractional-order TIF forcing.

Alpaca rejects orders whose quantity is not a whole number unless TIF is
``DAY`` (error 42210000: "fractional orders must be DAY orders"). The
adapter transparently coerces the upstream TIF so sizing / execution code
doesn't have to know about that broker quirk. These tests lock in the
behaviour so a future TIF refactor can't silently re-introduce the loop.
"""

from __future__ import annotations

from decimal import Decimal

from alpaca.trading.enums import (
    OrderSide as AlpOrderSide,
    TimeInForce as AlpTIF,
)
from alpaca.trading.requests import LimitOrderRequest, MarketOrderRequest

from brokers.alpaca.adapter import AlpacaAdapter
from brokers.base import Order, OrderSide, OrderType


def _make_adapter() -> AlpacaAdapter:
    # Paper-mode, no API keys needed — ``_build_order_request`` is pure.
    return AlpacaAdapter(api_key="", api_secret="", paper_mode=True)


def _whole_share_market_buy(tif: str = "GTC") -> Order:
    return Order(
        symbol="AAPL",
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        quantity=Decimal("10"),
        time_in_force=tif,
    )


def _fractional_share_market_buy(tif: str = "GTC") -> Order:
    return Order(
        symbol="BBEU",
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        quantity=Decimal("1.234"),
        time_in_force=tif,
    )


def _fractional_share_limit_buy(tif: str = "GTC") -> Order:
    return Order(
        symbol="FMHI",
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        quantity=Decimal("0.75"),
        limit_price=Decimal("41.52"),
        time_in_force=tif,
    )


class TestAlpacaFractionalTif:
    def test_whole_share_market_keeps_gtc(self) -> None:
        adapter = _make_adapter()
        req = adapter._build_order_request(_whole_share_market_buy("GTC"))
        assert isinstance(req, MarketOrderRequest)
        assert req.time_in_force == AlpTIF.GTC

    def test_fractional_share_market_forces_day(self) -> None:
        adapter = _make_adapter()
        req = adapter._build_order_request(_fractional_share_market_buy("GTC"))
        assert isinstance(req, MarketOrderRequest)
        assert req.time_in_force == AlpTIF.DAY

    def test_fractional_share_limit_forces_day(self) -> None:
        adapter = _make_adapter()
        req = adapter._build_order_request(_fractional_share_limit_buy("GTC"))
        assert isinstance(req, LimitOrderRequest)
        assert req.time_in_force == AlpTIF.DAY

    def test_fractional_share_day_stays_day(self) -> None:
        adapter = _make_adapter()
        req = adapter._build_order_request(_fractional_share_market_buy("DAY"))
        assert req.time_in_force == AlpTIF.DAY

    def test_fractional_share_ioc_is_forced_to_day(self) -> None:
        """IOC is not valid with fractional qty either — must become DAY."""
        adapter = _make_adapter()
        req = adapter._build_order_request(_fractional_share_market_buy("IOC"))
        assert req.time_in_force == AlpTIF.DAY

    def test_whole_share_ioc_keeps_ioc(self) -> None:
        adapter = _make_adapter()
        order = Order(
            symbol="AAPL",
            side=OrderSide.SELL,
            order_type=OrderType.MARKET,
            quantity=Decimal("5"),
            time_in_force="IOC",
        )
        req = adapter._build_order_request(order)
        assert req.time_in_force == AlpTIF.IOC

    def test_trailing_zero_fractional_still_forces_day(self) -> None:
        """Decimal('1.00') is mathematically whole — must keep GTC."""
        adapter = _make_adapter()
        order = Order(
            symbol="AAPL",
            side=OrderSide.BUY,
            order_type=OrderType.MARKET,
            quantity=Decimal("1.00"),
            time_in_force="GTC",
        )
        req = adapter._build_order_request(order)
        assert req.time_in_force == AlpTIF.GTC

    def test_sub_unit_crypto_quantity_forces_day(self) -> None:
        """Crypto fractional sizes also hit the same rule — lock it down."""
        adapter = _make_adapter()
        order = Order(
            symbol="BTC/USD",
            side=OrderSide.BUY,
            order_type=OrderType.MARKET,
            quantity=Decimal("0.00012345"),
            time_in_force="GTC",
        )
        req = adapter._build_order_request(order)
        assert req.time_in_force == AlpTIF.DAY
