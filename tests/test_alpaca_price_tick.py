"""Alpaca adapter — tick-size rounding for limit / stop prices.

Alpaca rejects orders whose prices violate NMS Rule 612 (422 sub-penny
increment). These tests lock down the defensive rounding helper that
quantises model-generated prices to a valid tick *before* submission.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from alpaca.trading.enums import OrderSide as AlpOrderSide

from brokers.alpaca.adapter import (
    _ALPACA_CRYPTO_TICK,
    _ALPACA_EQUITY_PENNY,
    _ALPACA_EQUITY_SUBPENNY,
    _alpaca_price_tick,
    _round_price_to_tick,
)


class TestAlpacaPriceTick:
    def test_penny_tick_for_stocks_over_one_dollar(self) -> None:
        assert _alpaca_price_tick("AAPL", Decimal("150.00")) == _ALPACA_EQUITY_PENNY
        assert _alpaca_price_tick("AAPL", Decimal("1.00")) == _ALPACA_EQUITY_PENNY
        assert _alpaca_price_tick("AAPL", Decimal("1.0001")) == _ALPACA_EQUITY_PENNY

    def test_subpenny_tick_for_stocks_under_one_dollar(self) -> None:
        assert _alpaca_price_tick("XYZ", Decimal("0.50")) == _ALPACA_EQUITY_SUBPENNY
        assert _alpaca_price_tick("XYZ", Decimal("0.9999")) == _ALPACA_EQUITY_SUBPENNY

    def test_crypto_tick_is_eight_decimals(self) -> None:
        assert _alpaca_price_tick("BTC/USD", Decimal("50000")) == _ALPACA_CRYPTO_TICK
        assert _alpaca_price_tick("ETH/USD", Decimal("0.5")) == _ALPACA_CRYPTO_TICK


class TestRoundPriceToTick:
    # ─── the original bug: 28.53499985 ──────────────────────────────────
    def test_sub_penny_buy_limit_rounds_down_to_penny(self) -> None:
        rounded = _round_price_to_tick(
            "FUTY", Decimal("28.53499985"), side=AlpOrderSide.BUY, is_stop=False
        )
        # BUY LIMIT is passive → round DOWN
        assert rounded == Decimal("28.53")

    def test_sub_penny_sell_limit_rounds_up_to_penny(self) -> None:
        rounded = _round_price_to_tick(
            "FUTY", Decimal("28.53499985"), side=AlpOrderSide.SELL, is_stop=False
        )
        # SELL LIMIT is passive → round UP
        assert rounded == Decimal("28.54")

    def test_buy_stop_rounds_up(self) -> None:
        # Breakout BUY stop needs more momentum → round UP
        rounded = _round_price_to_tick(
            "AAPL", Decimal("150.345"), side=AlpOrderSide.BUY, is_stop=True
        )
        assert rounded == Decimal("150.35")

    def test_sell_stop_rounds_down(self) -> None:
        # Protective SELL stop gives position more room → round DOWN
        rounded = _round_price_to_tick(
            "AAPL", Decimal("150.345"), side=AlpOrderSide.SELL, is_stop=True
        )
        assert rounded == Decimal("150.34")

    def test_already_valid_penny_is_unchanged(self) -> None:
        for side in (AlpOrderSide.BUY, AlpOrderSide.SELL):
            assert _round_price_to_tick(
                "AAPL", Decimal("150.00"), side=side, is_stop=False
            ) == Decimal("150.00")
            assert _round_price_to_tick(
                "AAPL", Decimal("150.99"), side=side, is_stop=False
            ) == Decimal("150.99")

    def test_penny_stock_under_one_dollar_allows_subpenny(self) -> None:
        rounded = _round_price_to_tick(
            "XYZ", Decimal("0.12345678"), side=AlpOrderSide.BUY, is_stop=False
        )
        assert rounded == Decimal("0.1234")
        rounded_sell = _round_price_to_tick(
            "XYZ", Decimal("0.12345678"), side=AlpOrderSide.SELL, is_stop=False
        )
        assert rounded_sell == Decimal("0.1235")

    def test_crypto_preserves_up_to_eight_decimals(self) -> None:
        rounded = _round_price_to_tick(
            "BTC/USD",
            Decimal("42345.123456789012"),
            side=AlpOrderSide.BUY,
            is_stop=False,
        )
        assert rounded == Decimal("42345.12345678")

    def test_zero_or_negative_price_is_passed_through(self) -> None:
        assert _round_price_to_tick(
            "AAPL", Decimal("0"), side=AlpOrderSide.BUY, is_stop=False
        ) == Decimal("0")

    def test_price_near_one_dollar_crossing_boundary(self) -> None:
        # 0.9997 is sub-penny eligible; rounding UP for SELL still keeps it
        # below $1.00 → must stay on sub-penny grid (0.9998), not jump to 1.00.
        rounded = _round_price_to_tick(
            "XYZ", Decimal("0.99975"), side=AlpOrderSide.SELL, is_stop=False
        )
        assert rounded == Decimal("0.9998")

    def test_rounded_output_has_correct_quantum(self) -> None:
        # The Decimal returned must carry the tick's exponent so it serialises
        # cleanly (no float("0.57000000000000006") slipping back in).
        rounded = _round_price_to_tick(
            "AAPL", Decimal("0.57123"), side=AlpOrderSide.BUY, is_stop=False
        )
        assert rounded.as_tuple().exponent == -4

    @pytest.mark.parametrize(
        "raw,side,expected",
        [
            ("28.534999", AlpOrderSide.BUY, "28.53"),
            ("28.535000", AlpOrderSide.BUY, "28.53"),
            ("28.535001", AlpOrderSide.SELL, "28.54"),
            ("100.0049", AlpOrderSide.BUY, "100.00"),
            ("100.0049", AlpOrderSide.SELL, "100.01"),
        ],
    )
    def test_parametric_limit_rounding(
        self, raw: str, side: AlpOrderSide, expected: str
    ) -> None:
        rounded = _round_price_to_tick(
            "AAPL", Decimal(raw), side=side, is_stop=False
        )
        assert rounded == Decimal(expected)
