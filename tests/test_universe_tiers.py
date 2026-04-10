from __future__ import annotations

from pathlib import Path

import pytest

from data.universe_tiers import UniverseTiers, assign_tiers, load_universe_tiers, save_universe_tiers


def test_assign_tiers_order_and_sizes() -> None:
    scored = [("C", 1.0), ("A", 30.0), ("B", 10.0)]
    core, scan, light = assign_tiers(scored, core_max=2, scan_max=1)
    assert core == ["A", "B"]
    assert scan == ["C"]
    assert light == []


def test_assign_tiers_light_tail() -> None:
    scored = [(f"S{i}", float(i)) for i in range(10)]
    core, scan, light = assign_tiers(scored, core_max=3, scan_max=4)
    assert len(core) == 3
    assert len(scan) == 4
    assert len(light) == 3


def test_save_load_roundtrip(tmp_path: Path) -> None:
    p = tmp_path / "universe_tiers.json"
    tiers = UniverseTiers(
        core=("SPY",),
        scan=("QQQ",),
        light=("ZZZ",),
        scores={"SPY": 1.5, "QQQ": 0.5},
        updated_at="2026-01-01T00:00:00+00:00",
    )
    save_universe_tiers(tiers, path=p)
    loaded = load_universe_tiers(p)
    assert loaded is not None
    assert loaded.core == ("SPY",)
    assert loaded.scan == ("QQQ",)
    assert loaded.light == ("ZZZ",)
    assert loaded.scores["SPY"] == pytest.approx(1.5)


def test_load_missing_returns_none(tmp_path: Path) -> None:
    assert load_universe_tiers(tmp_path / "nope.json") is None


def test_load_invalid_json_returns_none(tmp_path: Path) -> None:
    p = tmp_path / "bad.json"
    p.write_text("{", encoding="utf-8")
    assert load_universe_tiers(p) is None
