"""
tests/test_dynamic_thresholds.py
=================================
D141 — every Category-C tuning constant is now a formula of live market
features and strategy stats, not a stored value.

These tests lock the contract: thresholds vary CONTINUOUSLY with their
inputs, are clamped to YAML safety bounds, and fall back cleanly when
the ``dynamic_thresholds`` block is disabled (legacy passthrough — no
behaviour change on opt-out).
"""

from __future__ import annotations

import tempfile
from decimal import Decimal
from pathlib import Path

import pytest

import system.dynamic_thresholds as dt
from system.dynamic_thresholds import (
    base_target_notional,
    bollinger_band_epsilon,
    momentum_breakout_threshold,
    rsi_thresholds,
    volume_confirmation_multiplier,
)


# ── Fixtures ──────────────────────────────────────────────────────────────


_ENABLED_YAML = """
dynamic_thresholds:
  enabled: true
  rsi:
    base_distance: 3
    vol_weight: 250
    trend_weight: 8
    min_distance: 2
    max_distance: 30
  bollinger_epsilon:
    atr_multiple: 0.5
    min_epsilon: 0.001
    max_epsilon: 0.05
  momentum_breakout:
    atr_multiple: 0.2
    min_threshold: 0.0005
    max_threshold: 0.05
  volume_confirmation:
    base_multiplier: 1.0
    z_weight: 0.4
    min_multiplier: 1.0
    max_multiplier: 5.0
  sizing:
    base_nav_pct: 0.020
    pnl_health_weight: 0.5
    min_nav_pct: 0.002
    max_nav_pct: 0.05
"""

_DISABLED_YAML = """
dynamic_thresholds:
  enabled: false
"""


@pytest.fixture
def yaml_enabled():
    f = tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False, encoding="utf-8")
    f.write(_ENABLED_YAML)
    f.close()
    p = Path(f.name)
    original = dt._CONFIG_PATH
    dt._CONFIG_PATH = p
    dt._YAML_CACHE = None
    yield
    dt._CONFIG_PATH = original
    dt._YAML_CACHE = None
    p.unlink(missing_ok=True)


@pytest.fixture
def yaml_disabled():
    f = tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False, encoding="utf-8")
    f.write(_DISABLED_YAML)
    f.close()
    p = Path(f.name)
    original = dt._CONFIG_PATH
    dt._CONFIG_PATH = p
    dt._YAML_CACHE = None
    yield
    dt._CONFIG_PATH = original
    dt._YAML_CACHE = None
    p.unlink(missing_ok=True)


# ── RSI thresholds ────────────────────────────────────────────────────────


def test_rsi_thresholds_widen_with_volatility(yaml_enabled):
    """Higher ATR% → wider RSI band (need deeper extreme to fade)."""
    calm = rsi_thresholds(atr_pct=Decimal("0.005"), market_state_score=Decimal("0"))
    wild = rsi_thresholds(atr_pct=Decimal("0.05"), market_state_score=Decimal("0"))
    # Buy threshold drops, sell threshold rises as vol increases.
    assert calm[0] > wild[0]
    assert calm[1] < wild[1]


def test_rsi_thresholds_widen_with_trend_strength(yaml_enabled):
    """Bigger |market_state_score| → wider RSI band (don't fade a runaway)."""
    flat = rsi_thresholds(atr_pct=Decimal("0.01"), market_state_score=Decimal("0"))
    strong = rsi_thresholds(atr_pct=Decimal("0.01"), market_state_score=Decimal("1.5"))
    assert flat[0] > strong[0]
    assert flat[1] < strong[1]


def test_rsi_thresholds_symmetric_around_50(yaml_enabled):
    """Buy and sell distances from 50 must be equal (RSI is symmetric)."""
    buy, sell = rsi_thresholds(atr_pct=Decimal("0.02"), market_state_score=Decimal("0.5"))
    assert (Decimal("50") - buy) == (sell - Decimal("50"))


def test_rsi_thresholds_clamped_to_yaml_bounds(yaml_enabled):
    """At extreme inputs the formula must clamp to YAML min/max distance."""
    # Inputs that would push distance well past max_distance=30:
    buy, sell = rsi_thresholds(atr_pct=Decimal("10"), market_state_score=Decimal("10"))
    assert buy >= Decimal("20")    # 50 - 30 max distance
    assert sell <= Decimal("80")   # 50 + 30 max distance


def test_rsi_thresholds_passthrough_when_disabled(yaml_disabled):
    """When dynamic_thresholds is off, return the legacy static values."""
    buy, sell = rsi_thresholds(
        atr_pct=Decimal("0.5"),
        market_state_score=Decimal("2"),
        static_buy_threshold=42,
        static_sell_threshold=58,
    )
    assert buy == Decimal("42")
    assert sell == Decimal("58")


# ── Bollinger band epsilon ────────────────────────────────────────────────


def test_band_epsilon_scales_with_atr(yaml_enabled):
    eps_calm = bollinger_band_epsilon(atr_pct=Decimal("0.005"))
    eps_wild = bollinger_band_epsilon(atr_pct=Decimal("0.03"))
    assert eps_wild > eps_calm


def test_band_epsilon_clamped(yaml_enabled):
    eps_tiny = bollinger_band_epsilon(atr_pct=Decimal("0.00001"))
    eps_huge = bollinger_band_epsilon(atr_pct=Decimal("10"))
    assert eps_tiny >= Decimal("0.001")
    assert eps_huge <= Decimal("0.05")


def test_band_epsilon_passthrough_when_disabled(yaml_disabled):
    eps = bollinger_band_epsilon(atr_pct=Decimal("1"), static_epsilon="0.006")
    assert eps == Decimal("0.006")


# ── Momentum breakout threshold ───────────────────────────────────────────


def test_momentum_threshold_scales_with_atr(yaml_enabled):
    low = momentum_breakout_threshold(atr_pct=Decimal("0.003"))
    high = momentum_breakout_threshold(atr_pct=Decimal("0.05"))
    assert high > low


def test_momentum_threshold_floor(yaml_enabled):
    """Even at zero ATR the threshold must not collapse to 0."""
    out = momentum_breakout_threshold(atr_pct=Decimal("0"))
    assert out >= Decimal("0.0005")


def test_momentum_threshold_passthrough_when_disabled(yaml_disabled):
    out = momentum_breakout_threshold(atr_pct=Decimal("1"), static_threshold="0.001")
    assert out == Decimal("0.001")


# ── Volume confirmation multiplier ────────────────────────────────────────


def test_volume_mult_rises_with_z_score(yaml_enabled):
    """When recent volume z-score variance is high, the bar to confirm
    a setup must be HIGHER (the symbol's volume is normally jumpy)."""
    quiet = volume_confirmation_multiplier(volume_z_recent=Decimal("0"))
    jumpy = volume_confirmation_multiplier(volume_z_recent=Decimal("2"))
    assert jumpy > quiet


def test_volume_mult_clamped(yaml_enabled):
    out = volume_confirmation_multiplier(volume_z_recent=Decimal("100"))
    assert out <= Decimal("5.0")


def test_volume_mult_passthrough_when_disabled(yaml_disabled):
    out = volume_confirmation_multiplier(
        volume_z_recent=Decimal("100"),
        static_multiplier="1.2",
    )
    assert out == Decimal("1.2")


# ── Sizing: base target notional ──────────────────────────────────────────


def test_sizing_scales_with_nav(yaml_enabled):
    """A $12M account should size proportionally larger than a $1.2M account."""
    small = base_target_notional(
        nav=Decimal("1200000"),
        strategy_net_pnl_recent=Decimal("0"),
        strategy_total_fills_recent=Decimal("0"),
        regime_multiplier=Decimal("1"),
    )
    big = base_target_notional(
        nav=Decimal("12000000"),
        strategy_net_pnl_recent=Decimal("0"),
        strategy_total_fills_recent=Decimal("0"),
        regime_multiplier=Decimal("1"),
    )
    assert big == small * 10


def test_sizing_shrinks_when_strategy_is_bleeding(yaml_enabled):
    """Negative recent P&L per fill must shrink the size (P&L health)."""
    healthy = base_target_notional(
        nav=Decimal("1000000"),
        strategy_net_pnl_recent=Decimal("0"),
        strategy_total_fills_recent=Decimal("20"),
        regime_multiplier=Decimal("1"),
    )
    bleeding = base_target_notional(
        nav=Decimal("1000000"),
        strategy_net_pnl_recent=Decimal("-2000"),
        strategy_total_fills_recent=Decimal("20"),
        regime_multiplier=Decimal("1"),
    )
    assert bleeding < healthy


def test_sizing_scales_with_regime_multiplier(yaml_enabled):
    """The regime multiplier from adaptive_regime_weights scales sizing."""
    out_fade = base_target_notional(
        nav=Decimal("1000000"),
        regime_multiplier=Decimal("0.7"),
    )
    out_boost = base_target_notional(
        nav=Decimal("1000000"),
        regime_multiplier=Decimal("1.3"),
    )
    assert out_boost > out_fade


def test_sizing_composes_with_strategy_quarantine_multiplier(yaml_enabled):
    normal = base_target_notional(
        nav=Decimal("1000000"),
        strategy_net_pnl_recent=Decimal("0"),
        strategy_total_fills_recent=Decimal("20"),
        regime_multiplier=Decimal("1.2"),
        quarantine_multiplier=Decimal("1"),
    )
    reduced = base_target_notional(
        nav=Decimal("1000000"),
        strategy_net_pnl_recent=Decimal("0"),
        strategy_total_fills_recent=Decimal("20"),
        regime_multiplier=Decimal("1.2"),
        quarantine_multiplier=Decimal("0.5"),
    )
    blocked = base_target_notional(
        nav=Decimal("1000000"),
        strategy_net_pnl_recent=Decimal("0"),
        strategy_total_fills_recent=Decimal("20"),
        regime_multiplier=Decimal("1.2"),
        quarantine_multiplier=Decimal("0"),
    )

    assert reduced < normal
    assert blocked == Decimal("0")


def test_sizing_clamped_to_nav_pct_bounds(yaml_enabled):
    """No matter the inputs, sizing must stay inside [min_nav_pct, max_nav_pct]
    of NAV — the operator's hard safety band."""
    nav = Decimal("1000000")
    out_min = base_target_notional(
        nav=nav,
        strategy_net_pnl_recent=Decimal("-1000000"),  # catastrophic recent P&L
        strategy_total_fills_recent=Decimal("10"),
        regime_multiplier=Decimal("0.1"),
    )
    out_max = base_target_notional(
        nav=nav,
        strategy_net_pnl_recent=Decimal("1000000"),   # blowout recent P&L
        strategy_total_fills_recent=Decimal("10"),
        regime_multiplier=Decimal("2"),
    )
    assert out_min >= nav * Decimal("0.002")   # 0.2% floor
    assert out_max <= nav * Decimal("0.05")    # 5% ceiling


def test_sizing_passthrough_when_disabled(yaml_disabled):
    """Disabled → return the legacy static notional unchanged."""
    out = base_target_notional(
        nav=Decimal("1000000"),
        static_notional=Decimal("20000"),
    )
    assert out == Decimal("20000")


# ── Strict-validation contract (fail closed) ──────────────────────────────


def _with_partial_yaml(yaml_text: str):
    """Helper: point ``_CONFIG_PATH`` at a tempfile with the given text."""
    import tempfile
    from pathlib import Path

    f = tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False, encoding="utf-8")
    f.write(yaml_text)
    f.close()
    p = Path(f.name)
    original = dt._CONFIG_PATH
    dt._CONFIG_PATH = p
    dt._YAML_CACHE = None
    dt._LOGGED_INVALID.clear()
    return original, p


def _restore_yaml(original, p):
    dt._CONFIG_PATH = original
    dt._YAML_CACHE = None
    dt._LOGGED_INVALID.clear()
    p.unlink(missing_ok=True)


def test_strict_validation_strips_incomplete_rsi_group():
    """rsi group missing one required key → group stripped from the
    live config, formula falls back to static. The misconfig must NOT
    silently activate a half-formula with code-baked coefficients."""
    yaml_text = """
dynamic_thresholds:
  enabled: true
  rsi:
    base_distance: 3
    vol_weight: 250
    # trend_weight MISSING — should disable the rsi group entirely.
    min_distance: 2
    max_distance: 30
"""
    original, p = _with_partial_yaml(yaml_text)
    try:
        # Even at non-zero ATR + non-zero score, the formula must return
        # the static thresholds because the rsi group failed validation.
        buy, sell = rsi_thresholds(
            atr_pct=Decimal("0.05"),
            market_state_score=Decimal("1.5"),
            static_buy_threshold=42,
            static_sell_threshold=58,
        )
        assert buy == Decimal("42")
        assert sell == Decimal("58")
    finally:
        _restore_yaml(original, p)


def test_strict_validation_strips_only_the_incomplete_group():
    """When one group is incomplete the OTHER groups stay live. A
    misconfig in rsi does not kill the whole dynamic layer."""
    yaml_text = """
dynamic_thresholds:
  enabled: true
  rsi:
    # missing trend_weight, min_distance, max_distance — incomplete.
    base_distance: 3
    vol_weight: 250
  momentum_breakout:
    atr_multiple: 0.2
    min_threshold: 0.0005
    max_threshold: 0.05
"""
    original, p = _with_partial_yaml(yaml_text)
    try:
        # rsi → static (group stripped).
        buy, sell = rsi_thresholds(
            atr_pct=Decimal("0.05"),
            market_state_score=Decimal("1.5"),
            static_buy_threshold=47,
            static_sell_threshold=53,
        )
        assert buy == Decimal("47")
        assert sell == Decimal("53")
        # momentum_breakout → formula still active (group complete).
        mom = momentum_breakout_threshold(atr_pct=Decimal("0.05"))
        # 0.2 × 0.05 = 0.01, clamped within bounds.
        assert mom == Decimal("0.0100")
    finally:
        _restore_yaml(original, p)


def test_strict_validation_logs_once_per_misconfig(caplog):
    """The CRITICAL log fires once per (mtime, group), not on every
    tick. Repeated calls into the formula must NOT spam the log."""
    yaml_text = """
dynamic_thresholds:
  enabled: true
  rsi:
    base_distance: 3
    # all other rsi keys missing.
"""
    original, p = _with_partial_yaml(yaml_text)
    try:
        for _ in range(5):
            rsi_thresholds(
                atr_pct=Decimal("0.01"),
                static_buy_threshold=47,
                static_sell_threshold=53,
            )
        # Only one (mtime, "rsi") should be in the dedup set even after
        # 5 calls. That's the "log once per misconfig" contract.
        rsi_keys = [k for k in dt._LOGGED_INVALID if k[1] == "rsi"]
        assert len(rsi_keys) == 1
    finally:
        _restore_yaml(original, p)


def test_thresholds_snapshot_carries_every_live_value(yaml_enabled):
    """The dashboard snapshot builder must include every formula's
    current resolved value so the operator can see what the system
    is using right now — and the config_hash for attribution."""
    from system.dynamic_thresholds import build_thresholds_snapshot

    snap = build_thresholds_snapshot(
        market_features={
            "trend_strength": 0.6,
            "chaos_penalty": 0.2,
            "volatility_structure": 0.3,
            "liquidity_state": 0.7,
        },
        market_state_score=0.5,
        representative_atr_pct="0.015",
        median_atr_pct="0.015",
        nav="1200000",
        strategy_pnl_recent={
            "mean_reversion": {"net_pnl": -150.0, "fills": 30, "win_rate": 0.4},
            "momentum_breakout": {"net_pnl": +400.0, "fills": 15, "win_rate": 0.6},
        },
    )

    # Every formula's resolved output is present.
    assert "rsi" in snap and "buy" in snap["rsi"] and "sell" in snap["rsi"]
    assert "bollinger_epsilon" in snap
    assert "momentum_breakout_threshold" in snap
    assert "volume_confirmation_multiplier" in snap
    assert "volume_zscore_open" in snap
    assert "volume_zscore_exhaust" in snap
    assert "min_bar_return" in snap
    assert "atr_band" in snap and len(snap["atr_band"]) == 2
    assert "event_shock_threshold" in snap
    assert "pairs_zscore_open" in snap
    assert "regime_rotation_trigger" in snap
    assert "anti_churn_cooldown_sec_by_mode" in snap

    # Config hash for P&L attribution.
    assert "config_hash" in snap and len(snap["config_hash"]) == 12

    # Per-strategy block has multiplier + recent P&L + sample sizing.
    per = snap["per_strategy"]
    assert "mean_reversion" in per
    assert "regime_multiplier" in per["mean_reversion"]
    assert "recent_net_pnl" in per["mean_reversion"]
    assert "recent_fills" in per["mean_reversion"]
    assert "sample_target_notional" in per["mean_reversion"]
    # Bleeding mean_reversion should size smaller than winning momentum.
    assert per["mean_reversion"]["sample_target_notional"] < per["momentum_breakout"]["sample_target_notional"]


def test_config_version_changes_when_yaml_changes():
    """The config hash must be a stable function of YAML content —
    same content → same hash; any edit → different hash. This is the
    P&L-attribution contract."""
    from system.dynamic_thresholds import config_version

    text_a = """
dynamic_thresholds:
  enabled: true
  rsi:
    base_distance: 3
"""
    text_b = """
dynamic_thresholds:
  enabled: true
  rsi:
    base_distance: 5
"""
    original, p = _with_partial_yaml(text_a)
    try:
        hash_a = config_version()
        assert hash_a and len(hash_a) == 12
        _restore_yaml(original, p)
        _, p2 = _with_partial_yaml(text_a)
        # Same content → same hash, even from a different file.
        assert config_version() == hash_a
        _restore_yaml(original, p2)
        # Edited content → different hash.
        _, p3 = _with_partial_yaml(text_b)
        hash_b = config_version()
        assert hash_b != hash_a
    finally:
        try:
            _restore_yaml(original, p3)
        except Exception:
            pass


def test_strict_validation_unparseable_value_falls_back():
    """A YAML key present but unparseable (e.g. a typo string) must
    behave the same as missing — fall back to static, never substitute
    a code-baked default."""
    yaml_text = """
dynamic_thresholds:
  enabled: true
  bollinger_epsilon:
    atr_multiple: "not-a-number"   # parseable as string, not as Decimal
    min_epsilon: 0.001
    max_epsilon: 0.05
"""
    original, p = _with_partial_yaml(yaml_text)
    try:
        eps = bollinger_band_epsilon(
            atr_pct=Decimal("0.05"),
            static_epsilon=Decimal("0.006"),
        )
        assert eps == Decimal("0.006")
    finally:
        _restore_yaml(original, p)


def test_sizing_zero_nav_returns_static(yaml_enabled):
    """Defensive: NAV not available → static fallback so a missing
    portfolio doesn't accidentally zero-size the strategy."""
    out = base_target_notional(nav=Decimal("0"), static_notional=Decimal("5000"))
    assert out == Decimal("5000")


# ── End-to-end: mean_reversion uses the dynamic thresholds ────────────────


def test_mean_reversion_uses_dynamic_rsi(yaml_enabled, monkeypatch):
    """The strategy must consume rsi_thresholds via dynamic_thresholds —
    a high market_state_score must widen the RSI gate so a borderline
    signal stops firing."""
    import pandas as pd
    from strategies.mean_reversion import MeanReversionStrategy

    # Build a frame with RSI=48 (would fire with static 47/53 buy) and
    # a sufficient price stretch. Float dtype throughout — pandas 2.x
    # rejects in-place float assignment to int columns.
    n = 60
    df = pd.DataFrame({
        "open": [100.0] * n,
        "high": [101.0] * n,
        "low": [99.0] * n,
        "close": [100.0] * n,
        "rsi_14": [50.0] * n,
        "BBL_20_2.0": [99.0] * n,
        "BBU_20_2.0": [101.0] * n,
    })
    # Force a wide stretch so the gate's other arm doesn't block us.
    df.iloc[-1, df.columns.get_loc("close")] = 98.5
    df.iloc[-1, df.columns.get_loc("rsi_14")] = 48.0

    cfg_strong_trend = {
        "enabled": True,
        "lookback_periods": 30,
        "rsi_buy_threshold": 47,
        "rsi_sell_threshold": 53,
        "band_epsilon": 0.006,
        "base_target_notional": "20000",
        # Strong trend score → RSI gate widens → 48 no longer triggers.
        "_market_state_score": 1.5,
    }
    s = MeanReversionStrategy(cfg_strong_trend)
    sig_strong = s.generate_signal("SPY", df)

    cfg_calm = dict(cfg_strong_trend)
    cfg_calm["_market_state_score"] = 0
    s_calm = MeanReversionStrategy(cfg_calm)
    sig_calm = s_calm.generate_signal("SPY", df)

    # The strong-trend setup is gated more strictly; in trend regimes
    # mean-reversion at RSI ≈ 48 has no business firing. The dynamic
    # formula must drop it. The calm version is allowed to fire.
    assert sig_strong is None
    if sig_calm is not None:
        # If it does fire, it must carry the live thresholds in metadata.
        assert "rsi_buy_threshold_dyn" in sig_calm.metadata
        assert "band_epsilon_dyn" in sig_calm.metadata
