"""
tests/test_adaptive_mode.py
============================

Lock in the Phase 0 mode classifier semantics:

  * Default is **hunter** — the system stays aggressive unless objective
    adverse evidence is present.
  * Defender triggers on: emergency news, drawdown breach, or vol-sigma
    spike. Each one is testable in isolation.
  * Trader is the middle gear: moderate drawdown OR moderate vol spike.
  * Reasons are diagnostic strings the dashboard can render so the
    operator always understands *why* the classifier moved.

These tests pin down the override ladder. If a future change accidentally
flips a hunter case to trader, this suite breaks loudly.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from system.adaptive_mode import (
    ModeInputs,
    classify_market_mode,
    serialise_for_active_mode_json,
)


def _at(year=2026, month=5, day=14, hour=14, minute=0) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=timezone.utc)


# ── Hunter default ─────────────────────────────────────────────────────


def test_hunter_is_the_default_with_no_inputs() -> None:
    out = classify_market_mode(ModeInputs())
    assert out.mode == "hunter"
    assert out.reason == "no_adverse_evidence"


def test_hunter_when_only_benign_inputs() -> None:
    out = classify_market_mode(
        ModeInputs(
            cross_section_vol=0.02,
            cross_section_vol_baseline=0.02,
            cross_section_vol_std=0.005,
            nav_drawdown_pct=-0.001,  # -0.1% — tiny
            recent_signal_density=5.0,
            emergency_news_active=False,
        )
    )
    assert out.mode == "hunter"


def test_hunter_holds_through_normal_positive_drawdown() -> None:
    """Positive intraday P&L must not flip us defensive."""
    out = classify_market_mode(ModeInputs(nav_drawdown_pct=0.025))
    assert out.mode == "hunter"


def test_hunter_with_signal_density_but_no_loss() -> None:
    """High signal density on its own isn't a defensive trigger."""
    out = classify_market_mode(ModeInputs(recent_signal_density=50.0))
    assert out.mode == "hunter"


# ── Defender overrides ─────────────────────────────────────────────────


def test_emergency_news_triggers_defender() -> None:
    out = classify_market_mode(ModeInputs(emergency_news_active=True))
    assert out.mode == "defender"
    assert "emergency" in out.reason


def test_drawdown_breach_triggers_defender() -> None:
    out = classify_market_mode(ModeInputs(nav_drawdown_pct=-0.02))  # -2%
    assert out.mode == "defender"
    assert "drawdown" in out.reason


def test_vol_sigma_spike_triggers_defender() -> None:
    out = classify_market_mode(
        ModeInputs(
            cross_section_vol=0.05,
            cross_section_vol_baseline=0.02,
            cross_section_vol_std=0.01,
            # sigma = (0.05 - 0.02) / 0.01 = 3.0 → over 2σ threshold
        )
    )
    assert out.mode == "defender"
    assert "vol_sigma" in out.reason


def test_emergency_news_wins_over_other_triggers() -> None:
    """Emergency news is the highest-priority override."""
    out = classify_market_mode(
        ModeInputs(
            emergency_news_active=True,
            nav_drawdown_pct=-0.001,
            cross_section_vol=0.02,
            cross_section_vol_baseline=0.02,
            cross_section_vol_std=0.01,
        )
    )
    assert out.mode == "defender"
    assert out.reason == "emergency_news_event"


# ── Trader middle gear ─────────────────────────────────────────────────


def test_moderate_drawdown_triggers_trader() -> None:
    out = classify_market_mode(ModeInputs(nav_drawdown_pct=-0.008))  # -0.8%
    assert out.mode == "trader"
    assert "drawdown" in out.reason


def test_moderate_vol_sigma_triggers_trader() -> None:
    out = classify_market_mode(
        ModeInputs(
            cross_section_vol=0.035,
            cross_section_vol_baseline=0.02,
            cross_section_vol_std=0.01,
            # sigma = 1.5 → above trader floor (1.0), below defender (2.0)
        )
    )
    assert out.mode == "trader"
    assert "vol_sigma" in out.reason


def test_drawdown_just_above_trader_floor_stays_hunter() -> None:
    out = classify_market_mode(ModeInputs(nav_drawdown_pct=-0.004))  # -0.4%
    assert out.mode == "hunter"


# ── Robustness ─────────────────────────────────────────────────────────


def test_zero_vol_std_does_not_divide_by_zero() -> None:
    out = classify_market_mode(
        ModeInputs(
            cross_section_vol=0.5,
            cross_section_vol_baseline=0.02,
            cross_section_vol_std=0.0,
        )
    )
    # std is 0 → sigma branch skipped; falls through to hunter.
    assert out.mode == "hunter"


def test_serialisation_includes_reason_and_inputs() -> None:
    decision = classify_market_mode(ModeInputs(nav_drawdown_pct=-0.02, now=_at()))
    payload = serialise_for_active_mode_json(decision)
    assert payload["mode"] == "defender"
    assert payload["auto_derived"] is True
    assert "drawdown" in payload["reason"]
    assert "inputs" in payload
    assert payload["inputs"]["nav_drawdown_pct"] == pytest.approx(-0.02)
    assert payload["decided_at"].startswith("2026-05-14T")
