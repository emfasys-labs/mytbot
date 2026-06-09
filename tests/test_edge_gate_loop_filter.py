"""
tests/test_edge_gate_loop_filter.py
===================================

Integration test for the D157 edge-gate enforcement in the trading loop:
``TradingLoop._apply_edge_gate_filter`` must drop ``blocked`` strategies'
candidates, scale ``reduced`` strategies' confidence, and leave ``allowed``
strategies untouched — and be a strict no-op when disabled.

Uses ``TradingLoop.__new__`` to exercise the method without the heavy
constructor (the method only needs ``iterations``, ``_swallow``, and the
edge-gate cache attribute).
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from backtest.edge_gate import EdgeGateRegistry, EdgeVerdict, VERDICT_ALLOWED, VERDICT_BLOCKED, VERDICT_REDUCED
from core.models_runtime import SignalCandidate
from system.trading_loop.loop import TradingLoop


def _cand(strategy: str, conf: str = "0.8") -> SignalCandidate:
    return SignalCandidate(
        symbol=f"SYM_{strategy}",
        asset_class="equity",
        side="long",
        timestamp=datetime.now(timezone.utc),
        raw_signal_strength=Decimal("1"),
        adjusted_signal_strength=Decimal("1"),
        confidence=Decimal(conf),
        strategy_name=strategy,
    )


def _loop_stub() -> TradingLoop:
    loop = TradingLoop.__new__(TradingLoop)
    loop.iterations = 0
    loop._edge_gate_cache = None
    return loop


def _write_registry(tmp_path):
    reg = EdgeGateRegistry(tmp_path / "verdicts.json")
    reg.set_verdict(EdgeVerdict("momentum_breakout", VERDICT_ALLOWED, Decimal("1"), True, "ok"))
    reg.set_verdict(EdgeVerdict("mean_reversion", VERDICT_BLOCKED, Decimal("0"), False, "no edge"))
    reg.set_verdict(EdgeVerdict("volume_flow", VERDICT_REDUCED, Decimal("0.5"), True, "weak"))
    reg.save()
    return tmp_path / "verdicts.json"


def _cfg(path, *, enabled=True, unproven="reduce"):
    return {
        "edge_gate": {
            "enabled": enabled,
            "state_path": str(path),
            "unproven_policy": unproven,
            "reduced_multiplier": 0.5,
            "min_trades": 30,
        }
    }


def test_disabled_is_noop(tmp_path):
    loop = _loop_stub()
    cands = [_cand("momentum_breakout"), _cand("mean_reversion")]
    out = loop._apply_edge_gate_filter(cands, {"edge_gate": {"enabled": False}}, None)
    assert out is cands  # unchanged list reference, no filtering


def test_blocks_reduces_and_allows(tmp_path):
    path = _write_registry(tmp_path)
    loop = _loop_stub()
    cands = [
        _cand("momentum_breakout", "0.8"),   # allowed → untouched
        _cand("mean_reversion", "0.8"),       # blocked → dropped
        _cand("volume_flow", "0.8"),          # reduced → confidence ×0.5
    ]
    out = loop._apply_edge_gate_filter(cands, _cfg(path), None)
    by = {c.strategy_name: c for c in out}
    assert "mean_reversion" not in by          # blocked dropped
    assert by["momentum_breakout"].confidence == Decimal("0.8")   # allowed untouched
    assert by["volume_flow"].confidence == Decimal("0.4")          # reduced halved
    assert by["volume_flow"].metadata.get("edge_gate_multiplier") == 0.5


def test_unknown_strategy_reduce_policy_passes_through_scaled(tmp_path):
    path = _write_registry(tmp_path)
    loop = _loop_stub()
    cands = [_cand("brand_new_strategy", "0.8")]
    out = loop._apply_edge_gate_filter(cands, _cfg(path, unproven="reduce"), None)
    # Unknown + reduce policy → kept but halved.
    assert len(out) == 1
    assert out[0].confidence == Decimal("0.4")


def test_unknown_strategy_block_policy_dropped(tmp_path):
    path = _write_registry(tmp_path)
    loop = _loop_stub()
    cands = [_cand("brand_new_strategy", "0.8")]
    out = loop._apply_edge_gate_filter(cands, _cfg(path, unproven="block"), None)
    assert out == []   # unknown + block policy → dropped


# ── D161 — per-side enforcement ──────────────────────────────────────────────
def _cand_side(strategy: str, side: str, conf: str = "0.8") -> SignalCandidate:
    return SignalCandidate(
        symbol=f"SYM_{strategy}_{side}",
        asset_class="equity",
        side=side,
        timestamp=datetime.now(timezone.utc),
        raw_signal_strength=Decimal("1"),
        adjusted_signal_strength=Decimal("1"),
        confidence=Decimal(conf),
        strategy_name=strategy,
    )


def _write_per_side_registry(tmp_path):
    reg = EdgeGateRegistry(tmp_path / "verdicts.json")
    # mean_reversion: long proven, short toxic (the real D161 finding).
    reg.set_verdict(EdgeVerdict("mean_reversion", VERDICT_ALLOWED, Decimal("1"), True, "long ok"))
    reg.set_verdict(EdgeVerdict(EdgeGateRegistry.short_key("mean_reversion"),
                                VERDICT_BLOCKED, Decimal("0"), False, "short toxic"))
    # trend_breakout: both sides proven.
    reg.set_verdict(EdgeVerdict("trend_breakout", VERDICT_ALLOWED, Decimal("1"), True, "ok"))
    reg.set_verdict(EdgeVerdict(EdgeGateRegistry.short_key("trend_breakout"),
                                VERDICT_ALLOWED, Decimal("1"), True, "ok"))
    reg.save()
    return tmp_path / "verdicts.json"


def test_short_blocked_strategy_keeps_longs_drops_shorts(tmp_path):
    path = _write_per_side_registry(tmp_path)
    loop = _loop_stub()
    cands = [
        _cand_side("mean_reversion", "long"),    # long proven → kept
        _cand_side("mean_reversion", "short"),   # short toxic → dropped
        _cand_side("trend_breakout", "short"),   # short proven → kept
    ]
    out = loop._apply_edge_gate_filter(cands, _cfg(path), None)
    kept = {(c.strategy_name, c.side) for c in out}
    assert ("mean_reversion", "long") in kept
    assert ("mean_reversion", "short") not in kept
    assert ("trend_breakout", "short") in kept


def test_unproven_short_side_reduced_not_dropped(tmp_path):
    # momentum has only a long verdict in this registry; its short side is
    # unproven → reduce policy halves confidence instead of dropping.
    path = _write_per_side_registry(tmp_path)
    loop = _loop_stub()
    cands = [_cand_side("momentum_breakout", "short", "0.8")]
    out = loop._apply_edge_gate_filter(cands, _cfg(path, unproven="reduce"), None)
    assert len(out) == 1
    assert out[0].confidence == Decimal("0.4")
