"""
tests/test_liquidity_check_resolver.py
=======================================
D137 follow-up — extract the order-book depth cap into ``config/risk_limits.yaml::
liquidity_check`` and lock the resolver's contract.

Resolver contract:
  * When the YAML block is missing, disabled, or any required key is absent,
    the resolver MUST return the configured ``min_liquidity_usd`` unchanged
    (pre-D137 behaviour) — it must never invent a multiplier or floor.
  * When both keys are present and valid, the effective requirement is
    ``min(configured, max(order_notional × multiple, floor_usd))``.
  * Non-positive ``order_notional`` MUST fall back to the configured value.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from control.runtime import set_risk_engine
from execution.engine import ExecutionEngine


@dataclass
class _FakeRiskEngine:
    config: dict
    killed: bool = False
    disabled: set[str] = field(default_factory=set)

    def kill(self) -> None:
        self.killed = True

    def disable_broker(self, name: str) -> None:
        self.disabled.add(str(name).strip().lower())

    def reset_kill(self) -> None:
        self.killed = False
        self.disabled.clear()


def _engine(cfg: dict) -> ExecutionEngine:
    set_risk_engine(_FakeRiskEngine(cfg))
    return ExecutionEngine(broker_configs={}, paper_mode=True)


# ── 1. Resolver disabled / missing block ──────────────────────────────────


def test_resolver_returns_configured_when_block_absent():
    """No ``liquidity_check`` block at all → return configured min unchanged."""
    eng = _engine({"min_liquidity_usd": "1000000"})
    out = eng._resolve_book_liquidity_requirement(
        order_notional=Decimal("5000"),
        configured_min_liquidity_usd=Decimal("1000000"),
    )
    assert out == Decimal("1000000")


def test_resolver_returns_configured_when_block_disabled():
    eng = _engine({
        "liquidity_check": {
            "enabled": False,
            "multiple_of_order": 5,
            "floor_usd": 20000,
        },
    })
    out = eng._resolve_book_liquidity_requirement(
        order_notional=Decimal("5000"),
        configured_min_liquidity_usd=Decimal("1000000"),
    )
    assert out == Decimal("1000000")


# ── 2. Missing keys → never invent a constant ─────────────────────────────


def test_resolver_returns_configured_when_multiple_missing():
    """``multiple_of_order`` missing → cap disabled, no hardcoded 5×."""
    eng = _engine({
        "liquidity_check": {
            "enabled": True,
            "floor_usd": 20000,
        },
    })
    out = eng._resolve_book_liquidity_requirement(
        order_notional=Decimal("5000"),
        configured_min_liquidity_usd=Decimal("1000000"),
    )
    assert out == Decimal("1000000")


def test_resolver_returns_configured_when_floor_missing():
    """``floor_usd`` missing → cap disabled, no hardcoded $20k."""
    eng = _engine({
        "liquidity_check": {
            "enabled": True,
            "multiple_of_order": 5,
        },
    })
    out = eng._resolve_book_liquidity_requirement(
        order_notional=Decimal("5000"),
        configured_min_liquidity_usd=Decimal("1000000"),
    )
    assert out == Decimal("1000000")


def test_resolver_rejects_invalid_value_types():
    eng = _engine({
        "liquidity_check": {
            "enabled": True,
            "multiple_of_order": "not-a-number",
            "floor_usd": 20000,
        },
    })
    out = eng._resolve_book_liquidity_requirement(
        order_notional=Decimal("5000"),
        configured_min_liquidity_usd=Decimal("1000000"),
    )
    assert out == Decimal("1000000")


# ── 3. Active cap behaviour ────────────────────────────────────────────────


def test_resolver_caps_at_floor_for_small_order():
    """Tiny order (notional × multiple < floor) → effective requirement is floor."""
    eng = _engine({
        "liquidity_check": {
            "enabled": True,
            "multiple_of_order": 5,
            "floor_usd": 20000,
        },
    })
    # 1,000 × 5 = 5,000 < 20,000 → resolver picks floor 20,000 vs configured 1,000,000
    out = eng._resolve_book_liquidity_requirement(
        order_notional=Decimal("1000"),
        configured_min_liquidity_usd=Decimal("1000000"),
    )
    assert out == Decimal("20000")


def test_resolver_scales_with_order_notional_above_floor():
    """Big order (notional × multiple > floor) → effective requirement is the scaled value."""
    eng = _engine({
        "liquidity_check": {
            "enabled": True,
            "multiple_of_order": 5,
            "floor_usd": 20000,
        },
    })
    # 10,000 × 5 = 50,000 > 20,000 → resolver picks 50,000 vs configured 1,000,000
    out = eng._resolve_book_liquidity_requirement(
        order_notional=Decimal("10000"),
        configured_min_liquidity_usd=Decimal("1000000"),
    )
    assert out == Decimal("50000")


def test_resolver_never_exceeds_configured_min_liquidity():
    """Very large order whose scaled requirement would exceed the configured
    daily-volume threshold must still be capped at the configured value."""
    eng = _engine({
        "liquidity_check": {
            "enabled": True,
            "multiple_of_order": 5,
            "floor_usd": 20000,
        },
    })
    # 500,000 × 5 = 2,500,000 > configured 1,000,000 → resolver clamps to 1,000,000
    out = eng._resolve_book_liquidity_requirement(
        order_notional=Decimal("500000"),
        configured_min_liquidity_usd=Decimal("1000000"),
    )
    assert out == Decimal("1000000")


# ── 4. Edge cases ──────────────────────────────────────────────────────────


def test_resolver_falls_back_when_order_notional_non_positive():
    """If the caller hasn't established an order size, the scaling axis is
    meaningless — resolver must fall back to the configured value."""
    eng = _engine({
        "liquidity_check": {
            "enabled": True,
            "multiple_of_order": 5,
            "floor_usd": 20000,
        },
    })
    out = eng._resolve_book_liquidity_requirement(
        order_notional=Decimal("0"),
        configured_min_liquidity_usd=Decimal("1000000"),
    )
    assert out == Decimal("1000000")


def test_resolver_rejects_non_positive_multiple():
    eng = _engine({
        "liquidity_check": {
            "enabled": True,
            "multiple_of_order": 0,
            "floor_usd": 20000,
        },
    })
    out = eng._resolve_book_liquidity_requirement(
        order_notional=Decimal("5000"),
        configured_min_liquidity_usd=Decimal("1000000"),
    )
    assert out == Decimal("1000000")
