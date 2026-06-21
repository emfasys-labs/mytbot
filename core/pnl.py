"""Profit and loss helpers shared by live views and allocation code."""

from __future__ import annotations

from decimal import Decimal
from typing import Any


_ZERO = Decimal("0")


def _decimal(value: Any, default: Decimal = _ZERO) -> Decimal:
    if value is None or value == "":
        return default
    try:
        return Decimal(str(value))
    except Exception:  # noqa: BLE001
        return default


def normalise_fx_pair(symbol: str) -> tuple[str, str] | None:
    """Return ``(base, quote)`` for common six-letter FX pair symbols."""

    raw = str(symbol or "").strip().upper()
    raw = raw.replace("/", "").replace("-", "").replace("_", "")
    if raw.endswith("=X"):
        raw = raw[:-2]
    if len(raw) != 6 or not raw.isalpha():
        return None
    return (raw[:3], raw[3:])


def unrealised_pnl_account_currency(
    *,
    symbol: str,
    asset_class: str | None,
    quantity: Any,
    avg_entry_price: Any,
    current_price: Any,
    account_currency: str = "USD",
) -> Decimal:
    """Compute unrealised P&L in account currency where the quote allows it.

    For equities, ETFs, futures, crypto quoted in USD, and FX pairs whose
    quote currency is the account currency, ``(current - avg) * quantity`` is
    already account-currency P&L. For account-currency base pairs such as
    ``USDJPY``, that raw value is quote-currency P&L and must be converted
    back through the current FX rate.
    """

    qty = _decimal(quantity)
    avg = _decimal(avg_entry_price)
    cur = _decimal(current_price)
    raw = (cur - avg) * qty

    ac = str(asset_class or "").strip().lower()
    pair = normalise_fx_pair(symbol) if ac in {"forex", "fx"} else None
    if pair is None:
        return raw

    base, quote = pair
    account = str(account_currency or "USD").strip().upper()
    if quote == account:
        return raw
    if base == account and cur != 0:
        return raw / cur
    return raw
