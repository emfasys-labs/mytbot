"""
tests/test_orchestrator_capital_boot.py
========================================

Locks in the restart-safety fix for ``capital_pct``: the orchestrator must
default to 0% on boot (not 100%) so a brief window before the persisted
operator setting loads never triggers a 100% deployment + adaptive_shed
forced-exit cycle on every restart.
"""

from __future__ import annotations

from system.orchestrator import Orchestrator


def test_constructor_default_is_flat_not_full() -> None:
    """``__init__`` must NOT default to 1.0 — that caused forced shed events
    when the persisted slider value loaded after the first allocator tick.
    """
    orch = Orchestrator()
    assert orch.capital_pct == 0.0


def test_set_capital_pct_clamps_inputs() -> None:
    orch = Orchestrator()
    orch.set_capital_pct(0.5)
    assert orch.capital_pct == 0.5
    orch.set_capital_pct(1.5)
    assert orch.capital_pct == 1.0
    orch.set_capital_pct(-0.5)
    assert orch.capital_pct == 0.0
