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


def test_label_based_path_returns_neutral_for_mixed():
    """The 'mixed' label means "no clear regime signal" — the discrete
    label has no business deciding the multiplier. The live-features
    path (passing real RegimeState.components) is what drives behaviour
    in mixed regimes; the legacy label path returns neutral 1.0."""
    assert strategy_regime_multiplier("mean_reversion", "mixed") == Decimal("1.0")
    assert strategy_regime_multiplier("momentum_breakout", "mixed") == Decimal("1.0")


def test_mean_reversion_fade_in_trend_up():
    """The textbook bleed case the user complained about."""
    assert float(strategy_regime_multiplier("mean_reversion", "trend_up")) < 1.0


def test_momentum_boost_in_trend_up():
    assert float(strategy_regime_multiplier("momentum_breakout", "trend_up")) > 1.0


def test_unknown_regime_label_neutral():
    assert strategy_regime_multiplier("mean_reversion", "no_such_regime") == Decimal("1.0")


# ── live wiring: apply_regime_weighting ───────────────────────────────────


def test_apply_regime_weighting_drops_below_threshold_with_live_features():
    """A low-confidence MR signal in a strongly-trending, chaotic market
    (trend_strength high AND chaos_penalty high — both opposed by MR)
    must be dropped by the formula. Using both opposed-direction
    features pushes the fade clearly below the 0.50 floor without
    relying on a single feature's intensity."""
    sigs = [_raw("mean_reversion", 0.55, side="sell", symbol="QQQ")]
    sc_rows: list[dict] = []
    out = apply_regime_weighting(
        sigs,
        symbol="QQQ",
        market_features={
            "trend_strength": 0.95,
            "chaos_penalty": 0.95,
            "correlation_crowding": 0.95,
        },
        min_confidence=0.50,
        sc_rows=sc_rows,
        loop_iteration=42,
    )
    assert out == []
    assert len(sc_rows) == 1
    row = sc_rows[0]
    assert row["status"] == "filtered_regime_weight"
    assert "regime_fade:features" in row["reason"]
    md = row["metadata_"]
    assert md["confidence_pre_regime"] == 0.55
    assert md["confidence_post_regime"] < 0.50


def test_apply_regime_weighting_keeps_high_confidence_after_fade():
    """A 0.90-confidence MR signal gets faded but stays above the 0.50
    floor — kept at scaled confidence with metadata stamped."""
    sigs = [_raw("mean_reversion", 0.90, side="sell")]
    sc_rows: list[dict] = []
    out = apply_regime_weighting(
        sigs,
        symbol="AAPL",
        market_features={"trend_strength": 0.8},
        min_confidence=0.50,
        sc_rows=sc_rows,
        loop_iteration=1,
    )
    assert len(out) == 1
    assert sc_rows == []  # no drop row
    survivor = out[0]
    # Multiplier < 1 → scaled confidence below original.
    assert survivor.confidence < 0.90
    # But still above the floor.
    assert survivor.confidence >= 0.50
    assert survivor.metadata["regime_mult"] < 1.0
    assert survivor.metadata["confidence_pre_regime"] == 0.90


def test_apply_regime_weighting_boosts_momentum_with_live_trend_features():
    """High trend_strength must boost momentum_breakout's confidence."""
    sigs = [_raw("momentum_breakout", 0.60, side="buy")]
    sc_rows: list[dict] = []
    out = apply_regime_weighting(
        sigs,
        symbol="QQQ",
        market_features={"trend_strength": 0.9, "cross_asset_confirmation": 0.8},
        min_confidence=0.50,
        sc_rows=sc_rows,
        loop_iteration=1,
    )
    assert len(out) == 1
    assert sc_rows == []
    survivor = out[0]
    assert survivor.confidence > 0.60
    assert survivor.metadata["regime_mult"] > 1.0


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


# ── Dynamic-formula multiplier (D140 v2: computed live, not stored) ──────


def _with_yaml(yaml_text: str):
    """Helper: point ``_CONFIG_PATH`` at a tempfile containing the given
    YAML, returning a context-manager-style teardown."""
    import tempfile
    from pathlib import Path
    import system.adaptive_regime_weights as arw

    f = tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False, encoding="utf-8")
    f.write(yaml_text)
    f.close()
    p = Path(f.name)
    original = arw._CONFIG_PATH
    arw._CONFIG_PATH = p
    arw._cache = None

    def restore():
        arw._CONFIG_PATH = original
        arw._cache = None
        p.unlink(missing_ok=True)

    return restore


_DEFAULT_YAML = """
regime_weights:
  enabled: true
  sensitivity: 0.50
  bounds:
    min: 0.50
    max: 1.50
"""


def test_dynamic_multiplier_scales_with_trend_intensity():
    """The whole point of D140 v2: the multiplier varies CONTINUOUSLY
    with feature intensity, not in discrete steps. Features are
    centred on 0.5 (the natural midpoint of [0, 1]):
      * trend_strength = 0.1  → range-like  → MR boosted, momentum faded
      * trend_strength = 0.5  → neutral     → both ~1.0
      * trend_strength = 0.9  → trendy      → MR faded, momentum boosted
    The transition is smooth (tanh) so values just above / below 0.5
    differ by small continuous amounts."""
    from system.adaptive_regime_weights import compute_multiplier

    restore = _with_yaml(_DEFAULT_YAML)
    try:
        # mean_reversion: opposed to trend, so the multiplier decreases
        # MONOTONICALLY as trend_strength rises.
        mr_range = compute_multiplier("mean_reversion", {"trend_strength": 0.1})
        mr_neut = compute_multiplier("mean_reversion", {"trend_strength": 0.5})
        mr_trend = compute_multiplier("mean_reversion", {"trend_strength": 0.9})
        assert mr_range > mr_neut > mr_trend
        assert mr_range > Decimal("1.0")   # range = MR habitat → boost
        assert abs(mr_neut - Decimal("1.0")) < Decimal("0.01")  # midpoint = neutral
        assert mr_trend < Decimal("1.0")   # strong trend → fade

        # momentum: opposite sign, multiplier increases with trend.
        mo_range = compute_multiplier("momentum_breakout", {"trend_strength": 0.1})
        mo_neut = compute_multiplier("momentum_breakout", {"trend_strength": 0.5})
        mo_trend = compute_multiplier("momentum_breakout", {"trend_strength": 0.9})
        assert mo_range < mo_neut < mo_trend
        assert mo_range < Decimal("1.0")
        assert mo_trend > Decimal("1.0")

        # Continuous, not stepped: the gap between intensity 0.5 and 0.9
        # should be substantial (the formula doesn't plateau just past
        # the midpoint).
        assert (mr_neut - mr_trend) > Decimal("0.10")
    finally:
        restore()


def test_dynamic_multiplier_inverse_strategies_inverse_response():
    """Momentum and mean-reversion must respond to the SAME live
    feature in opposite directions — proving the formula respects
    each strategy's affinity sign."""
    from system.adaptive_regime_weights import compute_multiplier

    restore = _with_yaml(_DEFAULT_YAML)
    try:
        features = {"trend_strength": 0.8}
        mr = compute_multiplier("mean_reversion", features)
        mo = compute_multiplier("momentum_breakout", features)
        assert mr < Decimal("1.0")    # opposed → faded
        assert mo > Decimal("1.0")    # aligned → boosted
        # And the deviations are symmetric (same magnitude both sides
        # of 1.0) since the same feature drives both.
        assert abs((Decimal("1.0") - mr) - (mo - Decimal("1.0"))) < Decimal("0.01")
    finally:
        restore()


def test_dynamic_multiplier_multiple_features_compose():
    """When several live features overlap with a strategy's affinity row
    they should compose — strong trend AND strong chaos should fade
    mean_reversion more than either alone."""
    from system.adaptive_regime_weights import compute_multiplier

    restore = _with_yaml(_DEFAULT_YAML)
    try:
        trend_only = compute_multiplier("mean_reversion", {"trend_strength": 0.8})
        chaos_only = compute_multiplier("mean_reversion", {"chaos_penalty": 0.8})
        both = compute_multiplier(
            "mean_reversion",
            {"trend_strength": 0.8, "chaos_penalty": 0.8},
        )
        # Both negative contributions stack into a heavier fade.
        assert both < trend_only
        assert both < chaos_only
    finally:
        restore()


def test_dynamic_multiplier_yaml_sensitivity_drives_amplitude():
    """``sensitivity`` in YAML controls how strongly live features push
    the multiplier away from 1.0. Doubling sensitivity must roughly
    double the deviation (until the safety bounds clamp it)."""
    from system.adaptive_regime_weights import compute_multiplier

    features = {"trend_strength": 0.6}
    restore = _with_yaml("""
regime_weights:
  enabled: true
  sensitivity: 0.20
  bounds: {min: 0.10, max: 1.90}
""")
    try:
        low_s = compute_multiplier("mean_reversion", features)
    finally:
        restore()

    restore = _with_yaml("""
regime_weights:
  enabled: true
  sensitivity: 0.80
  bounds: {min: 0.10, max: 1.90}
""")
    try:
        high_s = compute_multiplier("mean_reversion", features)
    finally:
        restore()

    # Larger sensitivity → larger deviation from 1.0.
    assert abs(Decimal("1.0") - high_s) > abs(Decimal("1.0") - low_s)


def test_dynamic_multiplier_clamped_to_yaml_bounds():
    """No matter how strong the feature, the multiplier must stay inside
    [bounds.min, bounds.max] — even with sensitivity 1.0 and feature 1.0
    the tanh keeps it bounded, AND the post-formula clamp catches edge
    cases."""
    from system.adaptive_regime_weights import compute_multiplier

    restore = _with_yaml("""
regime_weights:
  enabled: true
  sensitivity: 0.90
  bounds: {min: 0.50, max: 1.50}
""")
    try:
        # Extreme aligned feature pushes momentum to the high end.
        mo_high = compute_multiplier(
            "momentum_breakout",
            {"trend_strength": 1.0, "cross_asset_confirmation": 1.0, "risk_on_breadth": 1.0},
        )
        # Extreme opposed features push mean_reversion to the low end.
        mr_low = compute_multiplier(
            "mean_reversion",
            {"trend_strength": 1.0, "chaos_penalty": 1.0, "correlation_crowding": 1.0},
        )
        assert mr_low >= Decimal("0.50")
        assert mo_high <= Decimal("1.50")
    finally:
        restore()


def test_dynamic_multiplier_no_overlap_is_neutral():
    """When the strategy's affinity row mentions no features that the
    caller actually supplied, the multiplier MUST be neutral (1.0). The
    formula refuses to invent contributions from missing data."""
    from system.adaptive_regime_weights import compute_multiplier

    restore = _with_yaml(_DEFAULT_YAML)
    try:
        # mean_reversion's affinity doesn't list "macro_clarity" — so
        # supplying only that feature should not move the multiplier.
        out = compute_multiplier("mean_reversion", {"macro_clarity": 1.0})
        assert out == Decimal("1.0")
    finally:
        restore()


def test_yaml_block_absent_disables_multipliers():
    """Missing or disabled block → every input returns 1.0. The module
    never invents a value when YAML is silent."""
    from system.adaptive_regime_weights import (
        compute_multiplier,
        strategy_regime_multiplier,
    )

    # Case 1: block missing entirely.
    restore = _with_yaml("strategies:\n  momentum_breakout:\n    enabled: true\n")
    try:
        assert strategy_regime_multiplier("mean_reversion", "trend_up") == Decimal("1.0")
        assert compute_multiplier("mean_reversion", {"trend_strength": 1.0}) == Decimal("1.0")
    finally:
        restore()

    # Case 2: block present but disabled.
    restore = _with_yaml(
        "regime_weights:\n"
        "  enabled: false\n"
        "  sensitivity: 0.5\n"
        "  bounds: {min: 0.5, max: 1.5}\n"
    )
    try:
        assert strategy_regime_multiplier("mean_reversion", "trend_up") == Decimal("1.0")
        assert compute_multiplier("momentum_breakout", {"trend_strength": 1.0}) == Decimal("1.0")
    finally:
        restore()


def test_yaml_missing_sensitivity_falls_to_neutral():
    """No hardcoded sensitivity default — missing key disables the formula."""
    from system.adaptive_regime_weights import compute_multiplier

    restore = _with_yaml(
        "regime_weights:\n"
        "  enabled: true\n"
        "  bounds: {min: 0.5, max: 1.5}\n"
        # No sensitivity key.
    )
    try:
        assert compute_multiplier(
            "mean_reversion", {"trend_strength": 1.0}
        ) == Decimal("1.0")
    finally:
        restore()


def test_yaml_invalid_bounds_falls_to_neutral():
    """Bounds with min > max are invalid — falls through to neutral so a
    bad config can't silently allow runaway multipliers."""
    from system.adaptive_regime_weights import compute_multiplier

    restore = _with_yaml(
        "regime_weights:\n"
        "  enabled: true\n"
        "  sensitivity: 0.5\n"
        "  bounds: {min: 1.5, max: 0.5}\n"
    )
    try:
        assert compute_multiplier(
            "mean_reversion", {"trend_strength": 1.0}
        ) == Decimal("1.0")
    finally:
        restore()


def test_apply_regime_weighting_logs_drop_with_full_context():
    """The candidate-log row for a dropped signal must carry enough
    forensic context that the operator can see exactly why it was
    dropped (which features were live, what multiplier the formula
    produced, before/after confidence)."""
    sigs = [_raw("mean_reversion", 0.55, side="sell", symbol="SPY")]
    sc_rows: list[dict] = []
    apply_regime_weighting(
        sigs,
        symbol="SPY",
        market_features={"trend_strength": 1.0, "chaos_penalty": 1.0},
        min_confidence=0.50,
        sc_rows=sc_rows,
        loop_iteration=99,
    )
    assert len(sc_rows) == 1
    md = sc_rows[0]["metadata_"]
    assert md["regime_mult"] < 1.0
    assert md["confidence_pre_regime"] == 0.55
    assert md["confidence_post_regime"] < 0.50
    assert md["min_confidence"] == 0.50
    assert set(md["regime_features_present"]) == {"trend_strength", "chaos_penalty"}
