"""Smoke: default allocation + profile YAML load through Pydantic."""

from pathlib import Path

from config.loaders import load_allocation, load_profile_modes

CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"


def test_load_profile_modes_default() -> None:
    cfg = load_profile_modes(CONFIG_DIR / "profile_modes.yaml")
    assert cfg.version == 1
    # Phase 5 (operator-mandated "hunter prevails unless market is adverse"):
    # the derived-mode default was deliberately changed trader → hunter.
    assert cfg.defaults.active_mode == "hunter"
    assert set(cfg.modes.keys()) == {"defender", "trader", "hunter"}
    assert cfg.modes["hunter"].coefficients.volume_anomaly_weight.base == 1.50
    assert "trend_strength_weight" in cfg.modes["defender"].coefficients.aggression_multiplier.dynamic


def test_load_allocation_default() -> None:
    cfg = load_allocation(CONFIG_DIR / "allocation.yaml")
    assert cfg.allocator.mode == "global_opportunity_replacement"
    assert cfg.capital_reuse.require_free_cash_to_open_new_position is False
    assert cfg.position_weights.lambda_ == 1.0
    assert cfg.opportunity_engine.components.volume_anomaly.transforms.saturating_function == "tanh"


def test_loaders_default_paths() -> None:
    load_profile_modes()
    load_allocation()
