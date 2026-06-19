"""D033 strategy_candidate_log aggregation + lifecycle rules."""

from __future__ import annotations

from decimal import Decimal

import pytest

from system.strategy_candidate_log import _blocker_hint, _json_safe, compute_lifecycle_label


def test_lifecycle_idle_when_no_rows() -> None:
    assert compute_lifecycle_label({}) == "idle"


def test_blocker_hint_prefers_risk_then_exec_then_no_setup() -> None:
    h0 = _blocker_hint(
        {"risk_rejected": 1},
        [],
        [{"reason": "size_cap", "count": 1}],
        [],
    )
    assert h0 and "Risk" in h0 and "size_cap" in h0
    h = _blocker_hint(
        {"execution_incomplete": 1, "no_setup": 3},
        [{"key": "a", "count": 2, "label": "L"}],
        [],
        [{"reason": "unfilled", "count": 1}],
    )
    assert h and "execution" in h.lower()
    h2 = _blocker_hint(
        {"no_setup": 5, "execution_incomplete": 0, "risk_rejected": 0},
        [{"key": "k", "count": 2, "label": "L"}],
        [],
        [],
    )
    assert h2 and "No setup" in h2


def test_json_safe_replaces_non_finite_numbers() -> None:
    cleaned = _json_safe(
        {
            "ok": 1.25,
            "bad_float": float("nan"),
            "bad_decimal": Decimal("Infinity"),
            "nested": [Decimal("1.5"), float("-inf")],
        }
    )

    assert cleaned == {
        "ok": 1.25,
        "bad_float": None,
        "bad_decimal": None,
        "nested": [1.5, None],
    }


def test_lifecycle_trading_when_executed() -> None:
    assert (
        compute_lifecycle_label(
            {"no_setup": 5, "generated": 2, "executed": 1, "risk_rejected": 3}
        )
        == "trading"
    )


def test_lifecycle_scanning_when_only_no_setup() -> None:
    assert compute_lifecycle_label({"no_setup": 10}) == "scanning"


def test_lifecycle_finding_setups_when_generated() -> None:
    assert compute_lifecycle_label({"generated": 3, "no_setup": 1}) == "finding_setups"


def test_lifecycle_competing_when_lost() -> None:
    assert (
        compute_lifecycle_label(
            {"generated": 1, "lost_to_strategy": 2, "selected_for_allocation": 0}
        )
        == "competing"
    )


def test_lifecycle_selected() -> None:
    assert (
        compute_lifecycle_label(
            {"selected_for_allocation": 1, "generated": 1, "lost_to_strategy": 0}
        )
        == "selected"
    )


@pytest.mark.parametrize(
    "bs,expected",
    [
        ({"risk_rejected": 5, "selected_for_allocation": 0, "generated": 0}, "blocked_by_risk"),
    ],
)
def test_lifecycle_blocked_by_risk(bs: dict, expected: str) -> None:
    assert compute_lifecycle_label(bs) == expected
