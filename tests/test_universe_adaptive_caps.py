"""D117 — Adaptive universe-tier sizing tests.

Each test exercises a single input axis (regime, signal pressure,
cluster_count, churn) so a regression on one axis cannot be hidden by
others. All policy calls are pure and deterministic; no I/O.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from universe.adaptive_caps import (
    AdaptiveCapsBase,
    AdaptiveCapsConfig,
    AdaptiveCapsContext,
    apply_churn_hysteresis,
    compute_adaptive_caps,
    load_adaptive_caps_config,
    parse_adaptive_caps_config,
)
from universe.adaptive_state import (
    AdaptiveRuntimeState,
    load_adaptive_state,
    save_adaptive_state,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _base() -> AdaptiveCapsBase:
    return AdaptiveCapsBase(candidates=400, watching=300, core=50, scan=250)


def _enabled_config() -> AdaptiveCapsConfig:
    """Build a fully-enabled config using the documented YAML defaults."""
    return parse_adaptive_caps_config(
        {
            "enabled": True,
            "bounds": {
                "candidates": {"min": 200, "max": 800},
                "watching": {"min": 150, "max": 600},
                "core": {"min": 25, "max": 100},
                "scan": {"min": 75, "max": 500},
            },
            "regime": {
                "risk_on": {"multiplier": 1.25},
                "risk_off": {"multiplier": 0.80},
                "volatile": {"multiplier": 1.30},
                "mixed": {"multiplier": 1.00},
                "crash": {"multiplier": 0.65},
                "trend_up": {"multiplier": 1.15},
                "range": {"multiplier": 0.90},
                "insufficient_data": {"multiplier": 1.00},
            },
            "signal_pressure": {
                "high_threshold": 8,
                "low_threshold": 2,
                "high_multiplier": 1.20,
                "low_multiplier": 0.80,
            },
            "cluster_aware": {
                "enabled": True,
                "watching_min_factor": 3.0,
                "watching_min_floor": 150,
            },
            "churn": {"min_consecutive_drops": 3},
        }
    )


# ---------------------------------------------------------------------------
# 1. Disabled flag is a no-op
# ---------------------------------------------------------------------------


def test_disabled_config_returns_base_unchanged():
    cfg = parse_adaptive_caps_config({"enabled": False})
    result = compute_adaptive_caps(
        base=_base(),
        context=AdaptiveCapsContext(regime_label="risk_on", signal_pressure=20),
        config=cfg,
    )
    assert result.enabled is False
    assert result.candidates == 400
    assert result.watching == 300
    assert result.core == 50
    assert result.scan == 250
    assert "adaptive_disabled" in result.reasons


def test_disabled_via_missing_blob():
    cfg = parse_adaptive_caps_config(None)
    assert cfg.enabled is False
    result = compute_adaptive_caps(base=_base(), context=AdaptiveCapsContext(), config=cfg)
    assert result.enabled is False
    assert result.watching == _base().watching


# ---------------------------------------------------------------------------
# 2. Regime axis
# ---------------------------------------------------------------------------


def test_risk_on_widens_caps():
    cfg = _enabled_config()
    ctx = AdaptiveCapsContext(regime_label="risk_on", signal_pressure=4)
    out = compute_adaptive_caps(base=_base(), context=ctx, config=cfg)
    assert out.enabled is True
    # 1.25 * neutral signal pressure (1.0) = 1.25 multiplier
    assert out.multiplier == pytest.approx(1.25)
    assert out.candidates == int(round(400 * 1.25))
    assert out.watching == int(round(300 * 1.25))


def test_crash_contracts_caps_hard():
    cfg = _enabled_config()
    ctx = AdaptiveCapsContext(regime_label="crash", signal_pressure=4)
    out = compute_adaptive_caps(base=_base(), context=ctx, config=cfg)
    assert out.multiplier == pytest.approx(0.65)
    # core min is 25; 50 * 0.65 = 32.5 → 33 which is above floor.
    assert out.core == int(round(50 * 0.65))
    # watching shrinks but stays above the lower bound (150).
    assert out.watching == max(150, int(round(300 * 0.65)))


def test_insufficient_data_neutral_multiplier():
    cfg = _enabled_config()
    out = compute_adaptive_caps(
        base=_base(),
        context=AdaptiveCapsContext(regime_label="insufficient_data", signal_pressure=4),
        config=cfg,
    )
    assert out.multiplier == pytest.approx(1.0)
    assert out.candidates == 400


def test_unknown_regime_label_uses_unknown_multiplier():
    cfg = _enabled_config()
    out = compute_adaptive_caps(
        base=_base(),
        context=AdaptiveCapsContext(regime_label="completely_made_up", signal_pressure=4),
        config=cfg,
    )
    assert out.multiplier == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# 3. Signal-pressure axis
# ---------------------------------------------------------------------------


def test_high_signal_pressure_widens_caps():
    cfg = _enabled_config()
    out = compute_adaptive_caps(
        base=_base(),
        context=AdaptiveCapsContext(regime_label="mixed", signal_pressure=12),
        config=cfg,
    )
    # mixed (1.0) * high (1.20)
    assert out.multiplier == pytest.approx(1.20)
    assert out.candidates == int(round(400 * 1.20))


def test_low_signal_pressure_contracts_caps():
    cfg = _enabled_config()
    out = compute_adaptive_caps(
        base=_base(),
        context=AdaptiveCapsContext(regime_label="mixed", signal_pressure=1),
        config=cfg,
    )
    assert out.multiplier == pytest.approx(0.80)
    assert out.candidates == int(round(400 * 0.80))


def test_missing_signal_pressure_is_neutral():
    cfg = _enabled_config()
    out = compute_adaptive_caps(
        base=_base(),
        context=AdaptiveCapsContext(regime_label="mixed", signal_pressure=None),
        config=cfg,
    )
    assert out.signal_pressure_multiplier == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# 4. Bounds enforcement
# ---------------------------------------------------------------------------


def test_combined_multiplier_clamped_to_max():
    cfg = _enabled_config()
    # risk_on (1.25) * high (1.20) = 1.50 — would push watching to 450
    # which is under the 600 bound; candidates 400 * 1.50 = 600 under 800;
    # core 50 * 1.50 = 75 under 100. So nothing clamps here. Use a
    # tighter inflated base to force the clamp:
    big_base = AdaptiveCapsBase(candidates=750, watching=560, core=95, scan=480)
    out = compute_adaptive_caps(
        base=big_base,
        context=AdaptiveCapsContext(regime_label="risk_on", signal_pressure=20),
        config=cfg,
    )
    assert out.candidates == 800  # clamped at max
    assert out.watching == 600
    assert out.core == 100


def test_combined_multiplier_clamped_to_min():
    cfg = _enabled_config()
    out = compute_adaptive_caps(
        base=_base(),
        context=AdaptiveCapsContext(regime_label="crash", signal_pressure=0),
        config=cfg,
    )
    # 0.65 * 0.80 = 0.52 -> watching=156, candidates=208, core=26, scan=130
    # All above bounds floor (150/200/25/75) so no clamping. Use a tiny
    # base to force a floor clamp:
    tiny_base = AdaptiveCapsBase(candidates=250, watching=180, core=30, scan=100)
    out = compute_adaptive_caps(
        base=tiny_base,
        context=AdaptiveCapsContext(regime_label="crash", signal_pressure=0),
        config=cfg,
    )
    # 0.65 * 0.80 = 0.52 -> 130 watching, 104 candidates, 15.6 core
    # All clamped at min: 150 / 200 / 25
    assert out.watching == 150
    assert out.candidates == 200
    assert out.core == 25


# ---------------------------------------------------------------------------
# 5. Cluster-aware floor
# ---------------------------------------------------------------------------


def test_cluster_floor_lifts_watching_to_3x_clusters():
    cfg = _enabled_config()
    out = compute_adaptive_caps(
        base=_base(),
        context=AdaptiveCapsContext(
            regime_label="risk_off",
            signal_pressure=4,
            active_cluster_count=120,
        ),
        config=cfg,
    )
    # 0.80 * 1.0 = 0.80 -> watching 240, but 3 * 120 = 360 floor
    # which is under the 600 max. So watching becomes 360.
    assert out.watching == 360
    assert out.cluster_floor_applied is True


def test_cluster_floor_respects_max_bound():
    cfg = _enabled_config()
    out = compute_adaptive_caps(
        base=_base(),
        context=AdaptiveCapsContext(
            regime_label="risk_on",
            signal_pressure=12,
            active_cluster_count=500,
        ),
        config=cfg,
    )
    # cluster_floor wants 1500 but bounds max is 600.
    assert out.watching == 600
    assert out.cluster_floor_applied is True


def test_cluster_floor_does_not_shrink_watching():
    cfg = _enabled_config()
    out = compute_adaptive_caps(
        base=_base(),
        context=AdaptiveCapsContext(
            regime_label="risk_on",
            signal_pressure=12,
            active_cluster_count=5,
        ),
        config=cfg,
    )
    # cluster floor wants max(150, 3*5)=150 but post-multiplier watching
    # is 300*1.25*1.20=450 which is bigger, so floor is NOT applied.
    assert out.cluster_floor_applied is False
    assert out.watching == 450


def test_cluster_floor_disabled_in_config():
    cfg = parse_adaptive_caps_config(
        {
            "enabled": True,
            "regime": {"mixed": {"multiplier": 1.0}},
            "cluster_aware": {"enabled": False, "watching_min_factor": 3.0, "watching_min_floor": 150},
        }
    )
    out = compute_adaptive_caps(
        base=_base(),
        context=AdaptiveCapsContext(
            regime_label="mixed",
            signal_pressure=4,
            active_cluster_count=500,
        ),
        config=cfg,
    )
    assert out.cluster_floor_applied is False


# ---------------------------------------------------------------------------
# 6. Anti-churn hysteresis
# ---------------------------------------------------------------------------


def test_hysteresis_grace_keeps_dropped_symbol_first_miss():
    out = apply_churn_hysteresis(
        new_core=["A", "B"],
        new_scan=["C"],
        new_light=["D"],
        previous_core=["A", "B"],
        previous_scan=["C", "X"],   # X dropped this build
        consecutive_misses={},
    )
    # X graced into scan tier on miss #1 (< default 3).
    assert "X" in out.scan
    assert out.consecutive_misses["X"] == 1
    assert "X" in out.grace_extended
    # X should NOT also be in light because it was promoted into scan.
    assert "X" not in out.light


def test_hysteresis_drops_symbol_after_n_misses():
    # Symbol Y has already missed 2 builds. Default policy is 3 misses,
    # so the 3rd miss must drop it.
    out = apply_churn_hysteresis(
        new_core=["A"],
        new_scan=["B"],
        new_light=["Y"],
        previous_core=["A"],
        previous_scan=["B", "Y"],
        consecutive_misses={"Y": 2},
    )
    # Y exhausted its grace -> stays in new_light, removed from misses.
    assert "Y" not in out.scan
    assert "Y" in out.light
    assert "Y" not in out.consecutive_misses


def test_hysteresis_returning_symbol_resets_counter():
    out = apply_churn_hysteresis(
        new_core=["A"],
        new_scan=["B", "Z"],
        new_light=[],
        previous_core=[],
        previous_scan=[],
        consecutive_misses={"Z": 2},
    )
    assert "Z" not in out.consecutive_misses
    assert "Z" in out.scan


def test_hysteresis_no_previous_state_passes_through():
    out = apply_churn_hysteresis(
        new_core=["A"],
        new_scan=["B"],
        new_light=["C"],
    )
    assert out.core == ("A",)
    assert out.scan == ("B",)
    assert out.light == ("C",)
    assert out.consecutive_misses == {}
    assert out.grace_extended == ()


# ---------------------------------------------------------------------------
# 7. Config loading from disk (load_adaptive_caps_config)
# ---------------------------------------------------------------------------


def test_load_from_disk_picks_up_dynamic_universe_adaptive(tmp_path: Path):
    yaml_path = tmp_path / "data_pipeline.yaml"
    yaml_path.write_text(
        "dynamic_universe:\n"
        "  adaptive:\n"
        "    enabled: true\n"
        "    regime:\n"
        "      risk_on: { multiplier: 2.0 }\n",
        encoding="utf-8",
    )
    cfg = load_adaptive_caps_config(yaml_path)
    assert cfg.enabled is True
    assert cfg.regime.risk_on == pytest.approx(2.0)


def test_load_from_disk_missing_file_returns_disabled(tmp_path: Path):
    cfg = load_adaptive_caps_config(tmp_path / "does_not_exist.yaml")
    assert cfg.enabled is False


def test_load_from_disk_malformed_yaml_returns_disabled(tmp_path: Path):
    yaml_path = tmp_path / "bad.yaml"
    yaml_path.write_text("not: valid: yaml: :", encoding="utf-8")
    cfg = load_adaptive_caps_config(yaml_path)
    assert cfg.enabled is False


# ---------------------------------------------------------------------------
# 8. Runtime state persistence
# ---------------------------------------------------------------------------


def test_runtime_state_round_trip(tmp_path: Path):
    state_path = tmp_path / "universe_adaptive_state.json"
    state = AdaptiveRuntimeState(
        enabled=True,
        resolved={"candidates": 500, "watching": 360, "core": 60, "scan": 200, "multiplier": 1.2},
        context={"regime_label": "risk_on", "signal_pressure": 9},
        consecutive_misses={"X": 1, "Y": 2},
        last_grace_extended=["X", "Y"],
    )
    save_adaptive_state(state, path=state_path)
    loaded = load_adaptive_state(state_path)
    assert loaded.enabled is True
    assert loaded.resolved["candidates"] == 500
    assert loaded.consecutive_misses == {"X": 1, "Y": 2}
    assert "X" in loaded.last_grace_extended


def test_runtime_state_missing_file_returns_empty(tmp_path: Path):
    state = load_adaptive_state(tmp_path / "missing.json")
    assert state.enabled is False
    assert state.resolved == {}
    assert state.consecutive_misses == {}


def test_runtime_state_corrupt_file_returns_empty(tmp_path: Path):
    state_path = tmp_path / "corrupt.json"
    state_path.write_text("{not json", encoding="utf-8")
    state = load_adaptive_state(state_path)
    assert state.enabled is False
    assert state.resolved == {}


# ---------------------------------------------------------------------------
# 9. Sanity: core <= watching and scan + core <= candidates
# ---------------------------------------------------------------------------


def test_resolved_invariants_hold():
    cfg = _enabled_config()
    for regime_label in ("risk_on", "risk_off", "mixed", "crash", "volatile"):
        for sp in (0, 4, 12):
            ctx = AdaptiveCapsContext(
                regime_label=regime_label,
                signal_pressure=sp,
                active_cluster_count=80,
            )
            out = compute_adaptive_caps(base=_base(), context=ctx, config=cfg)
            assert out.core <= out.watching, (regime_label, sp, out)
            assert out.scan <= max(0, out.candidates - out.core), (regime_label, sp, out)
            assert out.core >= 25
            assert out.watching >= 150
            assert out.candidates >= 200
