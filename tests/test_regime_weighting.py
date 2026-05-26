"""
tests/test_regime_weighting.py
================================
D140 — wire ``strategy_regime_multiplier`` into the live signal flow.

The multiplier table in ``system/adaptive_regime_weights.py`` knew that
mean-reversion bleeds in ``trend_up`` and momentum bleeds in ``range``
since Phase 4, but the per-symbol candidate collection never consulted
it — only the optional opportunity engine did. The 2026-05-26 audit
traced ~$8K of unrealised loss to mean-reversion shorting an undeclared
trend while ``strategy_regime_multiplier`` sat unused.

These tests lock in the new ``apply_regime_weighting`` contract:
  * Multiplier ≥ 1 → signal kept, confidence boosted (capped at 1.0),
    metadata stamped.
  * Multiplier < 1 with scaled confidence still above threshold →
    signal kept at scaled confidence.
  * Multiplier < 1 with scaled confidence below threshold → signal
    dropped, ``filtered_regime_weight`` row recorded.
  * Missing/empty regime label → no change (defensive — never break
    the loop on a stale dashboard snapshot).
"""

from __future__ import annotations

from decimal import Decimal

from signals.engine import RawSignal
from system.adaptive_regime_weights import strategy_regime_multiplier
from system.trading_loop.candidate_collection import apply_regime_weighting


def _raw(strategy: str, conf: float, side: str = "sell", symbol: str = "AAPL") -> RawSignal:
    return RawSignal(
        strategy=strategy,
        symbol=symbol,
        side=side,
        confidence=conf,
        broker="ibkr",
        asset_class="equity",
        metadata={},
    )


# ── multiplier table coverage ────────────────────────────────────────────


def test_mixed_row_now_present_for_directional_strategies():
    """The audit found 'mixed' was missing from the table — every strategy
    fell through to 1.0. After D140, the table must give mean_reversion a
    fade < 1 and momentum_breakout a boost ≥ 1 in 'mixed'."""
    assert float(strategy_regime_multiplier("mean_reversion", "mixed")) < 1.0
    assert float(strategy_regime_multiplier("momentum_breakout", "mixed")) >= 1.0


def test_mean_reversion_fade_in_trend_up():
    """The textbook bleed case the user complained about."""
    assert float(strategy_regime_multiplier("mean_reversion", "trend_up")) < 1.0


def test_momentum_boost_in_trend_up():
    assert float(strategy_regime_multiplier("momentum_breakout", "trend_up")) > 1.0


def test_unknown_regime_label_neutral():
    assert strategy_regime_multiplier("mean_reversion", "no_such_regime") == Decimal("1.0")


# ── live wiring: apply_regime_weighting ───────────────────────────────────


def test_apply_regime_weighting_drops_below_threshold():
    """Mean-reversion confidence 0.55 in 'mixed' (× 0.80) → 0.44, below
    the 0.50 floor → dropped with a candidate-log row."""
    sigs = [_raw("mean_reversion", 0.55, side="sell", symbol="QQQ")]
    sc_rows: list[dict] = []
    out = apply_regime_weighting(
        sigs,
        symbol="QQQ",
        regime_label="mixed",
        min_confidence=0.50,
        sc_rows=sc_rows,
        loop_iteration=42,
    )
    assert out == []
    assert len(sc_rows) == 1
    row = sc_rows[0]
    assert row["status"] == "filtered_regime_weight"
    assert "regime_fade:mixed" in row["reason"]
    md = row["metadata_"]
    assert md["regime_label"] == "mixed"
    assert md["confidence_pre_regime"] == 0.55
    assert md["confidence_post_regime"] < 0.50


def test_apply_regime_weighting_keeps_high_confidence_after_fade():
    """A 0.90-confidence mean-reversion signal × 0.80 = 0.72 → still above
    the 0.50 floor → kept, but at the lower confidence and with metadata."""
    sigs = [_raw("mean_reversion", 0.90, side="sell")]
    sc_rows: list[dict] = []
    out = apply_regime_weighting(
        sigs,
        symbol="AAPL",
        regime_label="mixed",
        min_confidence=0.50,
        sc_rows=sc_rows,
        loop_iteration=1,
    )
    assert len(out) == 1
    assert sc_rows == []  # no drop row
    survivor = out[0]
    assert abs(survivor.confidence - 0.72) < 1e-6
    assert survivor.metadata["regime_mult"] == 0.80
    assert survivor.metadata["regime_label"] == "mixed"
    assert survivor.metadata["confidence_pre_regime"] == 0.90


def test_apply_regime_weighting_boosts_momentum_in_trend_up():
    """momentum_breakout × 1.30 in trend_up — confidence boosted, cap at 1.0."""
    sigs = [_raw("momentum_breakout", 0.60, side="buy")]
    sc_rows: list[dict] = []
    out = apply_regime_weighting(
        sigs,
        symbol="QQQ",
        regime_label="trend_up",
        min_confidence=0.50,
        sc_rows=sc_rows,
        loop_iteration=1,
    )
    assert len(out) == 1
    assert sc_rows == []
    survivor = out[0]
    assert abs(survivor.confidence - 0.78) < 1e-6
    assert survivor.metadata["regime_mult"] == 1.30


def test_apply_regime_weighting_caps_confidence_at_one():
    sigs = [_raw("momentum_breakout", 0.85, side="buy")]
    sc_rows: list[dict] = []
    out = apply_regime_weighting(
        sigs,
        symbol="QQQ",
        regime_label="trend_up",
        min_confidence=0.50,
        sc_rows=sc_rows,
        loop_iteration=1,
    )
    # 0.85 × 1.30 = 1.105 → capped at 1.0
    assert out[0].confidence == 1.0


def test_apply_regime_weighting_no_label_passthrough():
    """Empty / missing regime label → defensive passthrough, no metadata
    stamp, no drops. Keeps the loop alive on a stale dashboard snapshot."""
    sigs = [_raw("mean_reversion", 0.55), _raw("momentum_breakout", 0.60)]
    sc_rows: list[dict] = []
    out = apply_regime_weighting(
        sigs,
        symbol="AAPL",
        regime_label="",
        min_confidence=0.50,
        sc_rows=sc_rows,
        loop_iteration=1,
    )
    assert len(out) == 2
    assert sc_rows == []
    assert out[0].confidence == 0.55
    assert out[1].confidence == 0.60
    # No metadata stamp when no label was supplied.
    assert "regime_mult" not in (out[0].metadata or {})


def test_apply_regime_weighting_neutral_mult_no_stamp():
    """Strategy with multiplier == 1.0 (e.g. unknown strategy) is
    passed through unchanged — no metadata stamp, no log row."""
    sigs = [_raw("strategy_not_in_table", 0.55)]
    sc_rows: list[dict] = []
    out = apply_regime_weighting(
        sigs,
        symbol="AAPL",
        regime_label="trend_up",
        min_confidence=0.50,
        sc_rows=sc_rows,
        loop_iteration=1,
    )
    assert len(out) == 1
    assert sc_rows == []
    assert out[0].confidence == 0.55


def test_apply_regime_weighting_logs_drop_with_full_context():
    """The candidate-log row for a dropped signal must carry enough
    forensic context that the operator can see exactly why it was
    dropped (which regime, which multiplier, before/after confidence)."""
    sigs = [_raw("mean_reversion", 0.55, side="sell", symbol="SPY")]
    sc_rows: list[dict] = []
    apply_regime_weighting(
        sigs,
        symbol="SPY",
        regime_label="trend_up",  # MR × 0.70 = 0.385
        min_confidence=0.50,
        sc_rows=sc_rows,
        loop_iteration=99,
    )
    assert len(sc_rows) == 1
    md = sc_rows[0]["metadata_"]
    assert md["regime_mult"] == 0.70
    assert md["regime_label"] == "trend_up"
    assert md["confidence_pre_regime"] == 0.55
    assert abs(md["confidence_post_regime"] - 0.385) < 1e-6
    assert md["min_confidence"] == 0.50
