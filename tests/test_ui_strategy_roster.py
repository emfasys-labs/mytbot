from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MAPPING_TS = ROOT / "ui" / "src" / "app" / "redesign" / "mapping.ts"
SCREENS_TSX = ROOT / "ui" / "src" / "app" / "redesign" / "screens.tsx"


def test_strategy_dashboard_roster_includes_advanced_strategy_cards() -> None:
    src = MAPPING_TS.read_text(encoding="utf-8")
    for name in (
        "factor_sleeve",
        "stat_arb_pairs",
        "options_long_call",
        "options_long_put",
        "options_protective_put",
        "options_covered_call",
    ):
        assert f"name: '{name}'" in src


def test_strategy_dashboard_advanced_cards_default_to_enabled_for_paper() -> None:
    src = MAPPING_TS.read_text(encoding="utf-8")
    for name in (
        "factor_sleeve",
        "stat_arb_pairs",
        "options_long_call",
        "options_long_put",
        "options_protective_put",
        "options_covered_call",
    ):
        assert f"{{ name: '{name}'," in src
        assert f"{{ name: '{name}', kind:" in src
        line = next(line for line in src.splitlines() if f"name: '{name}'" in line)
        assert "enabled: true" in line


def test_strategy_dashboard_filters_internal_allocator_actions() -> None:
    src = MAPPING_TS.read_text(encoding="utf-8")
    assert "INTERNAL_ALLOCATION_ACTIONS" in src
    assert "'global_edge_trim'" in src
    assert "'trim_symbol'" in src
    assert ".filter((s) => isStrategyScreenEligible(s.name))" in src


def test_strategy_dashboard_has_human_titles_for_new_cards() -> None:
    src = SCREENS_TSX.read_text(encoding="utf-8")
    for label in (
        "Factor sleeve",
        "Stat-arb pairs",
        "Options long call",
        "Options long put",
        "Protective put",
        "Covered call",
    ):
        assert label in src
