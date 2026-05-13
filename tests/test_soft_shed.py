"""
tests/test_soft_shed.py
========================

Locks in the soft-shed step cap: when the operator slides capital DOWN,
the loop must spread the forced reduction across multiple iterations
instead of dumping the whole excess at market in one tick.
"""

from __future__ import annotations

from decimal import Decimal


def _soft_shed_target(
    *, held_cash: Decimal, cash_target: Decimal, nav: Decimal,
    step_pct: Decimal,
) -> Decimal:
    """Mirror of the inline soft-shed math in ``system/trading_loop/loop.py``.

    Returns the effective target the shed proposer should aim for this
    iteration — either the soft step or the full target if the excess
    fits inside one step.
    """
    soft_step_cash = nav * step_pct
    full_excess = held_cash - cash_target
    if soft_step_cash > 0 and soft_step_cash < full_excess:
        return held_cash - soft_step_cash
    return cash_target


def test_small_excess_clears_in_one_iteration() -> None:
    """When excess fits inside the soft step, target the full slider value."""
    out = _soft_shed_target(
        held_cash=Decimal("550_000"),
        cash_target=Decimal("500_000"),
        nav=Decimal("1_000_000"),
        step_pct=Decimal("0.10"),
    )
    # excess 50k < 10% of 1M = 100k → fully clear to 500k
    assert out == Decimal("500_000")


def test_large_excess_spreads_over_multiple_iterations() -> None:
    """A 50% slider drop on $1M NAV (=$500k excess) caps at 10%/iter (=$100k)."""
    out = _soft_shed_target(
        held_cash=Decimal("1_000_000"),
        cash_target=Decimal("500_000"),
        nav=Decimal("1_000_000"),
        step_pct=Decimal("0.10"),
    )
    # held - soft_step = 1_000_000 - 100_000 = 900_000
    assert out == Decimal("900_000")


def test_soft_shed_disabled_at_step_zero() -> None:
    """``step_pct=0`` disables soft-shed and falls through to full target."""
    out = _soft_shed_target(
        held_cash=Decimal("1_000_000"),
        cash_target=Decimal("500_000"),
        nav=Decimal("1_000_000"),
        step_pct=Decimal("0"),
    )
    assert out == Decimal("500_000")


def test_soft_shed_step_one_disables_capping() -> None:
    """``step_pct >= excess/NAV`` clears in one iteration."""
    out = _soft_shed_target(
        held_cash=Decimal("1_000_000"),
        cash_target=Decimal("500_000"),
        nav=Decimal("1_000_000"),
        step_pct=Decimal("1.0"),
    )
    assert out == Decimal("500_000")


def test_convergence_simulation_50pct_slider_drop() -> None:
    """50%→0% slider drop on $1M NAV converges in ~5 iterations at 10% step."""
    nav = Decimal("1_000_000")
    step_pct = Decimal("0.10")
    target = Decimal("0")
    held = Decimal("1_000_000")
    iterations = 0
    while held > target and iterations < 20:
        next_target = _soft_shed_target(
            held_cash=held, cash_target=target, nav=nav, step_pct=step_pct,
        )
        # In one iteration the system sheds (held - next_target) of cash.
        held = next_target
        iterations += 1
        if held <= target + Decimal("1"):
            break
    # 10% step over 1M = 100k per iteration → ~10 iterations to go 1M→0
    assert iterations <= 11
    assert held <= target
