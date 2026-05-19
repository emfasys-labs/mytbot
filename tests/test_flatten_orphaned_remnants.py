"""
tests/test_flatten_orphaned_remnants.py
========================================
D115 — Smoke + filter coverage for the orphaned-remnant flatten tool.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

import pytest

from scripts.flatten_orphaned_remnants import _row_loss_pct


@dataclass
class _Row:
    quantity: Decimal
    avg_entry_price: Decimal
    current_price: Decimal


def test_loss_pct_long_losing():
    row = _Row(quantity=Decimal("100"), avg_entry_price=Decimal("300"), current_price=Decimal("297"))
    assert _row_loss_pct(row) == Decimal("0.01")


def test_loss_pct_long_winning_returns_zero():
    row = _Row(quantity=Decimal("100"), avg_entry_price=Decimal("300"), current_price=Decimal("310"))
    assert _row_loss_pct(row) == Decimal("0")


def test_loss_pct_short_losing():
    row = _Row(quantity=Decimal("-100"), avg_entry_price=Decimal("300"), current_price=Decimal("306"))
    # Short loses when price moves up.
    assert _row_loss_pct(row) == Decimal("0.02")


def test_loss_pct_short_winning_returns_zero():
    row = _Row(quantity=Decimal("-100"), avg_entry_price=Decimal("300"), current_price=Decimal("295"))
    assert _row_loss_pct(row) == Decimal("0")


def test_loss_pct_zero_position():
    row = _Row(quantity=Decimal("0"), avg_entry_price=Decimal("300"), current_price=Decimal("305"))
    assert _row_loss_pct(row) == Decimal("0")
