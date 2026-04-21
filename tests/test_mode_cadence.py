"""Mode-aware trading-loop cadence.

Hunter mode should rotate more often than trader; trader more often than
defender. These tests exercise ``TradingLoop._load_mode_cadence_map`` by
pointing ``load_yaml`` at a tempfile so we don't touch the real config.
"""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture
def loop_instance():
    from system.trading_loop.loop import TradingLoop

    return TradingLoop(broker_configs={}, available_brokers=[], paper_mode=True)


def _write(tmp: Path, body: str) -> Path:
    f = tmp / "profile_modes.yaml"
    f.write_text(body, encoding="utf-8")
    return f


def test_cadence_map_loads_from_yaml(monkeypatch, tmp_path, loop_instance):
    body = """
loop_cadence_sec:
  defender: 900
  trader: 300
  hunter: 120
"""
    target = _write(tmp_path, body)

    def _fake_load_yaml(path):
        assert "profile_modes" in str(path)
        import yaml
        return yaml.safe_load(target.read_text()) or {}

    monkeypatch.setattr("system.trading_loop.loop.load_yaml", _fake_load_yaml)

    m = loop_instance._load_mode_cadence_map()
    assert m == {"defender": 900, "trader": 300, "hunter": 120}


def test_cadence_map_missing_block_returns_empty(monkeypatch, loop_instance):
    def _fake_load_yaml(path):
        return {"modes": {"hunter": {}}}
    monkeypatch.setattr("system.trading_loop.loop.load_yaml", _fake_load_yaml)
    assert loop_instance._load_mode_cadence_map() == {}


def test_cadence_map_filters_invalid_entries(monkeypatch, loop_instance):
    def _fake_load_yaml(path):
        return {
            "loop_cadence_sec": {
                "defender": "abc",      # unparseable → dropped
                "trader": -10,          # non-positive → dropped
                "hunter": 120,
            }
        }
    monkeypatch.setattr("system.trading_loop.loop.load_yaml", _fake_load_yaml)
    assert loop_instance._load_mode_cadence_map() == {"hunter": 120}


def test_cadence_map_enforces_minimum_interval(monkeypatch, loop_instance):
    def _fake_load_yaml(path):
        return {"loop_cadence_sec": {"hunter": 5}}  # below 10s floor
    monkeypatch.setattr("system.trading_loop.loop.load_yaml", _fake_load_yaml)
    # Floor matches ``__init__`` (max(10, ...)).
    assert loop_instance._load_mode_cadence_map() == {"hunter": 10}


def test_read_active_mode_defaults_to_trader(monkeypatch, tmp_path, loop_instance):
    monkeypatch.chdir(tmp_path)
    mode = loop_instance._read_active_mode()
    assert mode == "trader"


def test_read_active_mode_reads_json(monkeypatch, tmp_path, loop_instance):
    monkeypatch.chdir(tmp_path)
    d = tmp_path / "data" / "runtime"
    d.mkdir(parents=True)
    (d / "active_mode.json").write_text('{"mode": "hunter"}', encoding="utf-8")
    assert loop_instance._read_active_mode() == "hunter"


def test_hunter_rotates_faster_than_defender(monkeypatch, loop_instance):
    """Invariant: defender ≥ trader ≥ hunter (sanity, not strict)."""
    def _fake_load_yaml(path):
        return {
            "loop_cadence_sec": {"defender": 900, "trader": 300, "hunter": 120}
        }
    monkeypatch.setattr("system.trading_loop.loop.load_yaml", _fake_load_yaml)
    m = loop_instance._load_mode_cadence_map()
    assert m["defender"] >= m["trader"] >= m["hunter"]
