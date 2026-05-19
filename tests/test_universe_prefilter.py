"""D118 — Tests for the priority pre-filter.

Each component is exercised in isolation by holding every other input
fixed. The full weighted sum, anchor pinning, freshness decay, and the
deterministic tie-breaking are also covered.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from data.universe_prefilter import (
    AVAILABILITY_SCORE,
    AvailabilityHint,
    COMPONENT_NAMES,
    NEVER_SCORED_LIQUIDITY_PRIOR,
    PriorityBreakdown,
    compute_priority_scores,
    top_n_by_priority,
    uniform_weights,
)
from data.universe_score_ages import ScoreAges


def _utc(year: int, month: int, day: int, hour: int = 0) -> datetime:
    return datetime(year, month, day, hour, tzinfo=timezone.utc)


def _zero_weights_for(name: str) -> dict[str, float]:
    return {n: 1.0 if n == name else 0.0 for n in COMPONENT_NAMES}


# ---------------------------------------------------------------------------
# 1. Liquidity prior
# ---------------------------------------------------------------------------


def test_liquidity_prior_uses_last_score_normalised():
    ages = ScoreAges()
    ages.record_scores({"AAPL": 18.0}, now=_utc(2026, 5, 19))
    ages.record_scores({"MSFT": 9.0}, now=_utc(2026, 5, 19))
    weights = _zero_weights_for("liquidity_prior")
    out = compute_priority_scores(
        ["AAPL", "MSFT"],
        score_ages=ages,
        weights=weights,
        now=_utc(2026, 5, 19),
    )
    # AAPL has the max so its prior is 1.0; MSFT is half of that = 0.5.
    assert out["AAPL"].components["liquidity_prior"] == pytest.approx(1.0)
    assert out["MSFT"].components["liquidity_prior"] == pytest.approx(0.5)
    assert out["AAPL"].priority_score == pytest.approx(1.0)
    assert out["MSFT"].priority_score == pytest.approx(0.5)


def test_liquidity_prior_neutral_for_never_scored():
    ages = ScoreAges()
    weights = _zero_weights_for("liquidity_prior")
    out = compute_priority_scores(
        ["NEW"], score_ages=ages, weights=weights, now=_utc(2026, 5, 19)
    )
    assert out["NEW"].components["liquidity_prior"] == pytest.approx(
        NEVER_SCORED_LIQUIDITY_PRIOR
    )


# ---------------------------------------------------------------------------
# 2. Anchor pin
# ---------------------------------------------------------------------------


def test_anchor_pin_lifts_only_anchors():
    ages = ScoreAges()
    weights = _zero_weights_for("anchor_pin")
    out = compute_priority_scores(
        ["SPY", "RAND"],
        score_ages=ages,
        weights=weights,
        anchors=["spy"],  # lowercase to exercise normalisation
        now=_utc(2026, 5, 19),
    )
    assert out["SPY"].priority_score == pytest.approx(1.0)
    assert out["RAND"].priority_score == pytest.approx(0.0)


def test_anchor_pin_ignores_blank_anchors():
    ages = ScoreAges()
    weights = _zero_weights_for("anchor_pin")
    out = compute_priority_scores(
        ["SPY"],
        score_ages=ages,
        weights=weights,
        anchors=["", " ", "SPY"],
        now=_utc(2026, 5, 19),
    )
    assert out["SPY"].priority_score == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# 3. Freshness bonus
# ---------------------------------------------------------------------------


def test_freshness_bonus_never_scored_is_max():
    ages = ScoreAges()
    weights = _zero_weights_for("freshness_bonus")
    out = compute_priority_scores(
        ["NEW"], score_ages=ages, weights=weights, now=_utc(2026, 5, 19)
    )
    assert out["NEW"].components["freshness_bonus"] == pytest.approx(1.0)


def test_freshness_bonus_just_scored_is_zero():
    ages = ScoreAges()
    ages.record_scores({"AAPL": 12.0}, now=_utc(2026, 5, 19, 0))
    weights = _zero_weights_for("freshness_bonus")
    out = compute_priority_scores(
        ["AAPL"], score_ages=ages, weights=weights, now=_utc(2026, 5, 19, 0)
    )
    assert out["AAPL"].components["freshness_bonus"] == pytest.approx(0.0)


def test_freshness_bonus_half_life_yields_half():
    ages = ScoreAges()
    ages.record_scores({"AAPL": 12.0}, now=_utc(2026, 5, 19, 0))
    weights = _zero_weights_for("freshness_bonus")
    out = compute_priority_scores(
        ["AAPL"],
        score_ages=ages,
        weights=weights,
        now=_utc(2026, 5, 20, 0),  # 24h later
        freshness_half_life_sec=24 * 3600.0,
    )
    # 1 - exp(-ln 2 * 1) = 1 - 0.5 = 0.5
    assert out["AAPL"].components["freshness_bonus"] == pytest.approx(0.5, abs=1e-6)


def test_freshness_bonus_far_future_approaches_one():
    ages = ScoreAges()
    ages.record_scores({"AAPL": 12.0}, now=_utc(2026, 5, 19, 0))
    weights = _zero_weights_for("freshness_bonus")
    out = compute_priority_scores(
        ["AAPL"],
        score_ages=ages,
        weights=weights,
        now=_utc(2026, 6, 19, 0),  # ~30 days later
        freshness_half_life_sec=24 * 3600.0,
    )
    assert out["AAPL"].components["freshness_bonus"] > 0.99


# ---------------------------------------------------------------------------
# 4. Registry availability
# ---------------------------------------------------------------------------


def test_registry_availability_maps_all_statuses():
    ages = ScoreAges()
    weights = _zero_weights_for("registry_availability")
    hints = {
        "AVAIL": AvailabilityHint("AVAIL", best_status="available"),
        "QUAL": AvailabilityHint("QUAL", best_status="requires_qualification"),
        "UNK": AvailabilityHint("UNK", best_status="unknown"),
        "UNAVAIL": AvailabilityHint("UNAVAIL", best_status="unavailable"),
        "BLOCKED": AvailabilityHint("BLOCKED", best_status="blocked"),
        "NULL": AvailabilityHint("NULL", best_status=""),
    }
    out = compute_priority_scores(
        list(hints.keys()) + ["NOHINT"],
        score_ages=ages,
        weights=weights,
        availability_hints=hints,
        now=_utc(2026, 5, 19),
    )
    assert out["AVAIL"].components["registry_availability"] == pytest.approx(1.0)
    assert out["QUAL"].components["registry_availability"] == pytest.approx(0.7)
    assert out["UNK"].components["registry_availability"] == pytest.approx(0.3)
    assert out["UNAVAIL"].components["registry_availability"] == pytest.approx(0.0)
    assert out["BLOCKED"].components["registry_availability"] == pytest.approx(0.0)
    # Empty string and missing hint both fall back to "unknown"
    assert out["NULL"].components["registry_availability"] == pytest.approx(0.3)
    assert out["NOHINT"].components["registry_availability"] == pytest.approx(0.3)


# ---------------------------------------------------------------------------
# 5. Asset-class balance
# ---------------------------------------------------------------------------


def test_asset_class_balance_lifts_under_represented():
    ages = ScoreAges()
    weights = _zero_weights_for("asset_class_balance")
    hints = {
        "AAPL": AvailabilityHint("AAPL", asset_class="equity"),
        "TLT": AvailabilityHint("TLT", asset_class="bond"),
        "QQQ": AvailabilityHint("QQQ", asset_class="equity"),
        "SPY": AvailabilityHint("SPY", asset_class="equity"),
    }
    # Watching tier is 3 equities, 0 bonds -> bond should outrank equity.
    watching = ["AAPL", "QQQ", "SPY"]
    out = compute_priority_scores(
        list(hints.keys()),
        score_ages=ages,
        weights=weights,
        availability_hints=hints,
        watching_now=watching,
        now=_utc(2026, 5, 19),
    )
    assert out["TLT"].components["asset_class_balance"] == pytest.approx(1.0)
    # equity share = 3/3 = 1.0 -> balance = 1 - 1 = 0
    assert out["AAPL"].components["asset_class_balance"] == pytest.approx(0.0)


def test_asset_class_balance_unknown_bucket_gets_floor():
    ages = ScoreAges()
    weights = _zero_weights_for("asset_class_balance")
    hints = {"AAPL": AvailabilityHint("AAPL", asset_class="equity")}
    watching = ["AAPL"]
    out = compute_priority_scores(
        ["AAPL", "MYSTERY"],
        score_ages=ages,
        weights=weights,
        availability_hints=hints,
        watching_now=watching,
        now=_utc(2026, 5, 19),
    )
    # MYSTERY has no asset_class hint -> floor (0)
    assert out["MYSTERY"].components["asset_class_balance"] == pytest.approx(0.0)


def test_asset_class_balance_empty_watching_returns_neutral():
    ages = ScoreAges()
    weights = _zero_weights_for("asset_class_balance")
    hints = {"AAPL": AvailabilityHint("AAPL", asset_class="equity")}
    out = compute_priority_scores(
        ["AAPL"],
        score_ages=ages,
        weights=weights,
        availability_hints=hints,
        watching_now=[],
        now=_utc(2026, 5, 19),
    )
    assert out["AAPL"].components["asset_class_balance"] == pytest.approx(0.5)


# ---------------------------------------------------------------------------
# 6. Region balance
# ---------------------------------------------------------------------------


def test_region_balance_lifts_under_represented_region():
    ages = ScoreAges()
    weights = _zero_weights_for("region_balance")
    hints = {
        "AAPL": AvailabilityHint("AAPL", region="us"),
        "MSFT": AvailabilityHint("MSFT", region="us"),
        "HSBA": AvailabilityHint("HSBA", region="uk"),
    }
    watching = ["AAPL", "MSFT"]  # US over-represented
    out = compute_priority_scores(
        list(hints.keys()),
        score_ages=ages,
        weights=weights,
        availability_hints=hints,
        watching_now=watching,
        now=_utc(2026, 5, 19),
    )
    assert out["HSBA"].components["region_balance"] == pytest.approx(1.0)
    assert out["AAPL"].components["region_balance"] == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# 7. Weighted sum + uniform weights bootstrap
# ---------------------------------------------------------------------------


def test_uniform_weights_sum_to_one():
    w = uniform_weights()
    assert sum(w.values()) == pytest.approx(1.0)
    assert all(v == pytest.approx(1.0 / len(COMPONENT_NAMES)) for v in w.values())


def test_priority_score_is_weighted_sum_of_components():
    ages = ScoreAges()
    ages.record_scores({"AAPL": 18.0}, now=_utc(2026, 5, 19))
    weights = uniform_weights()
    out = compute_priority_scores(
        ["AAPL"],
        score_ages=ages,
        weights=weights,
        anchors=["AAPL"],
        availability_hints={"AAPL": AvailabilityHint("AAPL", best_status="available")},
        now=_utc(2026, 5, 19),
    )
    components = out["AAPL"].components
    expected = sum(weights[name] * components[name] for name in COMPONENT_NAMES)
    assert out["AAPL"].priority_score == pytest.approx(expected)


def test_weights_with_negative_values_treated_as_zero():
    ages = ScoreAges()
    ages.record_scores({"AAPL": 1.0}, now=_utc(2026, 5, 19))
    bad_weights = {name: -1.0 for name in COMPONENT_NAMES}
    bad_weights["anchor_pin"] = 1.0
    out = compute_priority_scores(
        ["AAPL"],
        score_ages=ages,
        weights=bad_weights,
        anchors=["AAPL"],
        now=_utc(2026, 5, 19),
    )
    # All negative values dropped, only anchor_pin survives; renormalised to 1.0
    assert out["AAPL"].priority_score == pytest.approx(1.0)


def test_zero_weights_fall_back_to_uniform():
    ages = ScoreAges()
    ages.record_scores({"AAPL": 18.0}, now=_utc(2026, 5, 19))
    bad_weights = {name: 0.0 for name in COMPONENT_NAMES}
    out = compute_priority_scores(
        ["AAPL"],
        score_ages=ages,
        weights=bad_weights,
        anchors=["AAPL"],
        availability_hints={"AAPL": AvailabilityHint("AAPL", best_status="available")},
        now=_utc(2026, 5, 19),
    )
    # With uniform fallback, score is the average of all six components
    components = out["AAPL"].components
    expected = sum(components.values()) / len(COMPONENT_NAMES)
    assert out["AAPL"].priority_score == pytest.approx(expected)


# ---------------------------------------------------------------------------
# 8. compute_priority_scores skips empty symbols + de-dupes
# ---------------------------------------------------------------------------


def test_compute_skips_empty_and_dedupes():
    ages = ScoreAges()
    out = compute_priority_scores(
        ["AAPL", "AAPL", "aapl", "", "  "],
        score_ages=ages,
        weights=uniform_weights(),
        now=_utc(2026, 5, 19),
    )
    assert list(out.keys()) == ["AAPL"]


# ---------------------------------------------------------------------------
# 9. top_n_by_priority — determinism + budget + pinning
# ---------------------------------------------------------------------------


def _bd(sym: str, score: float) -> PriorityBreakdown:
    return PriorityBreakdown(symbol=sym, priority_score=score, components={})


def test_top_n_takes_highest_scores():
    bd = {
        "A": _bd("A", 0.1),
        "B": _bd("B", 0.9),
        "C": _bd("C", 0.5),
    }
    out = top_n_by_priority(bd, budget=2)
    assert out == ["B", "C"]


def test_top_n_breaks_ties_by_symbol_ascending():
    bd = {
        "C": _bd("C", 0.5),
        "A": _bd("A", 0.5),
        "B": _bd("B", 0.5),
    }
    out = top_n_by_priority(bd, budget=3)
    assert out == ["A", "B", "C"]


def test_top_n_respects_budget_zero():
    bd = {"A": _bd("A", 0.5)}
    assert top_n_by_priority(bd, budget=0) == []
    assert top_n_by_priority(bd, budget=-5) == []


def test_top_n_pinned_symbols_kept():
    bd = {
        "A": _bd("A", 0.1),
        "B": _bd("B", 0.9),
        "C": _bd("C", 0.5),
    }
    out = top_n_by_priority(bd, budget=2, pinned=["A"])
    # A is pinned despite lowest score; the remaining slot goes to B.
    assert out[0] == "A"
    assert "B" in out
    assert len(out) == 2


def test_top_n_pinned_unknown_symbol_ignored():
    bd = {"A": _bd("A", 0.1)}
    out = top_n_by_priority(bd, budget=1, pinned=["GHOST"])
    assert out == ["A"]


def test_top_n_pinned_exceeds_budget_truncated_to_budget():
    bd = {"A": _bd("A", 0.1), "B": _bd("B", 0.9)}
    out = top_n_by_priority(bd, budget=1, pinned=["B", "A"])
    assert out == ["B"]


def test_top_n_dedupes_pinned():
    bd = {"A": _bd("A", 0.1)}
    out = top_n_by_priority(bd, budget=2, pinned=["A", "a", "A"])
    assert out == ["A"]


def test_top_n_deterministic_across_runs():
    bd = {f"SYM{i}": _bd(f"SYM{i}", float(i % 3)) for i in range(50)}
    first = top_n_by_priority(bd, budget=10)
    second = top_n_by_priority(bd, budget=10)
    assert first == second


# ---------------------------------------------------------------------------
# 10. Availability score table matches documented D118 mapping
# ---------------------------------------------------------------------------


def test_availability_score_table_constants():
    assert AVAILABILITY_SCORE["available"] == 1.0
    assert AVAILABILITY_SCORE["requires_qualification"] == 0.7
    assert AVAILABILITY_SCORE["unknown"] == 0.3
    assert AVAILABILITY_SCORE["unavailable"] == 0.0
    assert AVAILABILITY_SCORE["blocked"] == 0.0
