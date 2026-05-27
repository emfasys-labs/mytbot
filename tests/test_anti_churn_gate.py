"""
tests/test_anti_churn_gate.py
=============================
D115 anti-churn gate coverage.

The gate exists to stop three production failure patterns observed on
2026-05-19 (224 fills in 8 hours, $6.0M turnover on a $1.18M NAV):

    1. dedup       — identical signals fired 11-20+ times per day
    2. contradiction — strategy A long X and strategy B short X
    3. post_fill   — re-entry on same (broker, symbol) within seconds

Each gate must REJECT what should be rejected and never silently allow it,
and must NEVER reject operator/allocator close intents (those are checked
in the engine wiring tests below).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from signals.anti_churn import AntiChurnDecision, AntiChurnGate


def _now() -> datetime:
    return datetime(2026, 5, 19, 13, 30, 0, tzinfo=timezone.utc)


def _gate(**overrides) -> AntiChurnGate:
    cfg = {
        "enabled": True,
        "dedup_enabled": True,
        "dedup_window_sec": 90,
        "dedup_confidence_dp": 2,
        "dedup_price_dp": 4,
        "contradiction_enabled": True,
        "contradiction_window_sec": 300,
        "post_fill_enabled": True,
        "post_fill_cooldown_sec": {"hunter": 120, "trader": 180, "defender": 600},
    }
    cfg.update(overrides)
    return AntiChurnGate(cfg)


# ---------------------------- dedup ----------------------------
def test_dedup_blocks_identical_signal_within_window():
    g = _gate()
    n = _now()
    first = g.check(
        strategy="volatility_regime",
        symbol="AAPL",
        side="buy",
        confidence=0.708,
        suggested_price=300.0,
        broker="ibkr",
        now=n,
    )
    assert first.allow
    g.record_signal(
        strategy="volatility_regime",
        symbol="AAPL",
        side="buy",
        confidence=0.708,
        suggested_price=300.0,
        now=n,
    )
    second = g.check(
        strategy="volatility_regime",
        symbol="AAPL",
        side="buy",
        confidence=0.708,
        suggested_price=300.0,
        broker="ibkr",
        now=n + timedelta(seconds=30),
    )
    assert not second.allow
    assert second.reason == "identical_signal_dedup"
    assert second.details["elapsed_sec"] == pytest.approx(30.0)


def test_dedup_allows_same_signal_after_window():
    g = _gate(dedup_window_sec=10)
    n = _now()
    g.record_signal(
        strategy="volatility_regime",
        symbol="AAPL",
        side="buy",
        confidence=0.708,
        suggested_price=300.0,
        now=n,
    )
    # Same symbol/strategy but the second emission must lose at least
    # the contradiction tombstone too. Use a fresh gate with contradiction
    # off to isolate dedup.
    g2 = _gate(dedup_window_sec=10, contradiction_enabled=False)
    g2.record_signal(
        strategy="volatility_regime",
        symbol="AAPL",
        side="buy",
        confidence=0.708,
        suggested_price=300.0,
        now=n,
    )
    later = g2.check(
        strategy="volatility_regime",
        symbol="AAPL",
        side="buy",
        confidence=0.708,
        suggested_price=300.0,
        broker="ibkr",
        now=n + timedelta(seconds=11),
    )
    assert later.allow


def test_dedup_distinguishes_different_strategies_same_symbol_same_side():
    g = _gate(contradiction_enabled=False, post_fill_enabled=False)
    n = _now()
    g.record_signal(
        strategy="volatility_regime",
        symbol="QQQ",
        side="buy",
        confidence=0.72,
        suggested_price=696.16,
        now=n,
    )
    # Different strategy but same symbol+side+conf+price — dedup is per
    # strategy, so the second strategy must still get through.
    other = g.check(
        strategy="momentum_breakout",
        symbol="QQQ",
        side="buy",
        confidence=0.72,
        suggested_price=696.16,
        broker="ibkr",
        now=n + timedelta(seconds=5),
    )
    assert other.allow


def test_dedup_treats_distinct_prices_as_distinct():
    g = _gate(contradiction_enabled=False, post_fill_enabled=False, dedup_window_sec=300)
    n = _now()
    g.record_signal(
        strategy="volatility_regime",
        symbol="QQQ",
        side="buy",
        confidence=0.72,
        suggested_price=696.16,
        now=n,
    )
    different_px = g.check(
        strategy="volatility_regime",
        symbol="QQQ",
        side="buy",
        confidence=0.72,
        suggested_price=697.32,
        broker="ibkr",
        now=n + timedelta(seconds=10),
    )
    assert different_px.allow


# ---------------------------- cross-strategy contradiction ----------------------------
def test_cross_strategy_contradiction_blocks_lower_confidence_side():
    g = _gate(post_fill_enabled=False, dedup_enabled=False)
    n = _now()
    g.record_signal(
        strategy="volatility_regime",
        symbol="AAPL",
        side="buy",
        confidence=0.72,
        suggested_price=297.46,
        now=n,
    )
    decision = g.check(
        strategy="volume_flow",
        symbol="AAPL",
        side="sell",
        confidence=0.65,
        suggested_price=297.46,
        broker="ibkr",
        now=n + timedelta(seconds=60),
    )
    assert not decision.allow
    assert decision.reason == "cross_strategy_contradiction"
    assert decision.details["blocking_strategy"] == "volatility_regime"
    assert decision.details["current_confidence"] == pytest.approx(0.65)


def test_cross_strategy_contradiction_tombstones_both_sides_for_window():
    g = _gate(post_fill_enabled=False, dedup_enabled=False, contradiction_window_sec=300)
    n = _now()
    g.record_signal(
        strategy="volatility_regime",
        symbol="AAPL",
        side="buy",
        confidence=0.72,
        suggested_price=297.46,
        now=n,
    )
    g.check(
        strategy="volume_flow",
        symbol="AAPL",
        side="sell",
        confidence=0.65,
        suggested_price=297.46,
        broker="ibkr",
        now=n + timedelta(seconds=60),
    )
    # Even the original (winning) side cannot re-enter while tombstoned.
    re_emit_buy = g.check(
        strategy="volatility_regime",
        symbol="AAPL",
        side="buy",
        confidence=0.72,
        suggested_price=297.46,
        broker="ibkr",
        now=n + timedelta(seconds=70),
    )
    assert not re_emit_buy.allow
    assert re_emit_buy.reason == "contradiction_tombstone"


def test_higher_confidence_current_side_still_tombstoned():
    g = _gate(post_fill_enabled=False, dedup_enabled=False)
    n = _now()
    g.record_signal(
        strategy="volume_flow",
        symbol="AAPL",
        side="sell",
        confidence=0.51,
        suggested_price=297.46,
        now=n,
    )
    decision = g.check(
        strategy="volatility_regime",
        symbol="AAPL",
        side="buy",
        confidence=0.80,
        suggested_price=297.46,
        broker="ibkr",
        now=n + timedelta(seconds=10),
    )
    assert not decision.allow
    assert decision.reason == "cross_strategy_contradiction"
    assert decision.details.get("note") == "current_won_but_tombstoned_both"


# ---------------------------- post-fill cooldown ----------------------------
def test_post_fill_cooldown_blocks_reentry_in_window():
    g = _gate(dedup_enabled=False, contradiction_enabled=False)
    n = _now()
    g.record_fill(broker="ibkr", symbol="AAPL", side="buy", now=n)
    decision = g.check(
        strategy="volatility_regime",
        symbol="AAPL",
        side="buy",
        confidence=0.72,
        suggested_price=297.50,
        broker="ibkr",
        profile_mode="hunter",
        now=n + timedelta(seconds=60),
    )
    assert not decision.allow
    assert decision.reason == "post_fill_cooldown"
    assert decision.details["cooldown_sec"] == 120.0


def test_post_fill_cooldown_allows_after_window():
    g = _gate(dedup_enabled=False, contradiction_enabled=False)
    n = _now()
    g.record_fill(broker="ibkr", symbol="AAPL", side="buy", now=n)
    after = g.check(
        strategy="volatility_regime",
        symbol="AAPL",
        side="buy",
        confidence=0.72,
        suggested_price=297.50,
        broker="ibkr",
        profile_mode="hunter",
        now=n + timedelta(seconds=121),
    )
    assert after.allow


def test_post_fill_cooldown_mode_aware_defender_longer():
    g = _gate(dedup_enabled=False, contradiction_enabled=False)
    n = _now()
    g.record_fill(broker="ibkr", symbol="AAPL", side="buy", now=n)
    # Defender cooldown is 600s.
    blocked_at_500 = g.check(
        strategy="volatility_regime",
        symbol="AAPL",
        side="buy",
        confidence=0.72,
        suggested_price=297.50,
        broker="ibkr",
        profile_mode="defender",
        now=n + timedelta(seconds=500),
    )
    assert not blocked_at_500.allow
    assert blocked_at_500.details["cooldown_sec"] == 600.0


def test_post_fill_cooldown_does_not_affect_other_broker():
    g = _gate(dedup_enabled=False, contradiction_enabled=False)
    n = _now()
    g.record_fill(broker="ibkr", symbol="QQQ", side="buy", now=n)
    other_broker = g.check(
        strategy="volatility_regime",
        symbol="QQQ",
        side="buy",
        confidence=0.72,
        suggested_price=696.16,
        broker="alpaca",
        now=n + timedelta(seconds=10),
    )
    assert other_broker.allow


# ---------------------------- safety / hygiene ----------------------------
def test_hold_side_passes_through_without_recording():
    g = _gate()
    n = _now()
    decision = g.check(
        strategy="volatility_regime",
        symbol="AAPL",
        side="hold",
        confidence=0.50,
        suggested_price=297.46,
        broker="ibkr",
        now=n,
    )
    assert decision.allow
    assert decision.reason == "hold_passthrough"


def test_disabled_gate_allows_everything():
    g = AntiChurnGate(
        {
            "dedup_enabled": False,
            "contradiction_enabled": False,
            "post_fill_enabled": False,
        }
    )
    n = _now()
    g.record_signal(
        strategy="X",
        symbol="A",
        side="buy",
        confidence=0.9,
        suggested_price=10.0,
        now=n,
    )
    decision = g.check(
        strategy="X",
        symbol="A",
        side="buy",
        confidence=0.9,
        suggested_price=10.0,
        broker="ibkr",
        now=n,
    )
    assert decision.allow


def test_snapshot_reports_state():
    g = _gate()
    snap = g.snapshot()
    assert snap["dedup_enabled"] is True
    assert snap["contradiction_enabled"] is True
    assert snap["post_fill_enabled"] is True
    assert "stats" in snap


# ---------------------------- integration smoke: engine wiring ----------------------------
class _Raw:
    def __init__(self, *, strategy, symbol, side, confidence, broker, metadata=None, asset_class="equity"):
        self.strategy = strategy
        self.symbol = symbol
        self.side = side
        self.confidence = confidence
        self.broker = broker
        self.metadata = metadata or {}
        self.asset_class = asset_class


def test_signal_engine_blocks_second_identical_signal():
    from decimal import Decimal as D

    from signals.engine import RawSignal, SignalEngine

    cfg = {
        "default_position_pct": 0.05,
        "min_quantity": "0.0001",
        "quantity_decimals": 8,
        "volatility_sizing": {"enabled": False},
        "anti_churn": {
            "enabled": True,
            "dedup_enabled": True,
            "dedup_window_sec": 90,
            "contradiction_enabled": False,
            "post_fill_enabled": False,
        },
        "use_trained_meta_labeler": False,
    }
    eng = SignalEngine(cfg)

    md = {"last_price": "300.00", "profile_mode": "hunter"}
    raw1 = RawSignal(
        strategy="volatility_regime",
        symbol="AAPL",
        side="buy",
        confidence=0.708,
        broker="ibkr",
        asset_class="equity",
        metadata=dict(md),
    )
    out1 = eng.process(raw1, portfolio_value=D("1000000"))
    assert out1 is not None, "first signal should pass"

    raw2 = RawSignal(
        strategy="volatility_regime",
        symbol="AAPL",
        side="buy",
        confidence=0.708,
        broker="ibkr",
        asset_class="equity",
        metadata=dict(md),
    )
    out2 = eng.process(raw2, portfolio_value=D("1000000"))
    assert out2 is None, "identical second signal must be blocked by anti-churn"


def test_signal_engine_exempts_operator_close_from_anti_churn():
    from decimal import Decimal as D

    from signals.engine import RawSignal, SignalEngine

    cfg = {
        "default_position_pct": 0.05,
        "min_quantity": "0.0001",
        "quantity_decimals": 8,
        "volatility_sizing": {"enabled": False},
        "anti_churn": {"enabled": True, "dedup_enabled": True, "dedup_window_sec": 600},
        "use_trained_meta_labeler": False,
    }
    eng = SignalEngine(cfg)
    md_close = {"last_price": "300.00", "reduce_only": True, "profile_mode": "hunter"}
    raw_close1 = RawSignal(
        strategy="stop_loss_monitor",
        symbol="AAPL",
        side="sell",
        confidence=1.0,
        broker="ibkr",
        asset_class="equity",
        metadata=dict(md_close),
    )
    raw_close2 = RawSignal(
        strategy="stop_loss_monitor",
        symbol="AAPL",
        side="sell",
        confidence=1.0,
        broker="ibkr",
        asset_class="equity",
        metadata=dict(md_close),
    )
    s1 = eng.process(raw_close1, portfolio_value=D("1000000"))
    s2 = eng.process(raw_close2, portfolio_value=D("1000000"))
    assert s1 is not None and s2 is not None, "operator closes must never be dedupped"


def test_signal_engine_record_fill_starts_cooldown():
    from decimal import Decimal as D

    from signals.engine import RawSignal, SignalEngine

    cfg = {
        "default_position_pct": 0.05,
        "min_quantity": "0.0001",
        "quantity_decimals": 8,
        "volatility_sizing": {"enabled": False},
        "anti_churn": {
            "enabled": True,
            "dedup_enabled": False,
            "contradiction_enabled": False,
            "post_fill_enabled": True,
            "post_fill_cooldown_sec": {"hunter": 120},
        },
        "use_trained_meta_labeler": False,
    }
    eng = SignalEngine(cfg)
    eng.record_fill(broker="ibkr", symbol="AAPL", side="buy")
    raw = RawSignal(
        strategy="volatility_regime",
        symbol="AAPL",
        side="buy",
        confidence=0.72,
        broker="ibkr",
        asset_class="equity",
        metadata={"last_price": "300.00", "profile_mode": "hunter"},
    )
    out = eng.process(raw, portfolio_value=D("1000000"))
    assert out is None, "post-fill cooldown should block re-entry"


# ── D141 Phase 4 — dynamic cooldown ───────────────────────────────────────


def test_cooldown_shorter_in_clear_regime():
    """A strong (high |market_state_score|) regime → shorter post-fill
    cooldown, so we don't miss the trend."""
    g = _gate(dedup_enabled=False, contradiction_enabled=False)
    n = _now()
    g.record_fill(broker="ibkr", symbol="AAPL", side="buy", now=n)
    # 60s after fill, mixed regime (score=0): static 120s cooldown → blocked.
    d_mixed = g.check(
        strategy="volatility_regime", symbol="AAPL", side="buy",
        confidence=0.7, suggested_price=297.0, broker="ibkr",
        profile_mode="hunter", now=n + timedelta(seconds=60),
        market_state_score=0.0, recent_fill_rate_per_min=0.0,
    )
    assert not d_mixed.allow
    # Same time, clear regime (|score|=1.5): formula subtracts up to
    # 60s × 1.5 = 90s from the 120 base → 30s effective cooldown → ALLOWED.
    d_clear = g.check(
        strategy="volatility_regime", symbol="AAPL", side="buy",
        confidence=0.7, suggested_price=297.0, broker="ibkr",
        profile_mode="hunter", now=n + timedelta(seconds=60),
        market_state_score=1.5, recent_fill_rate_per_min=0.0,
    )
    assert d_clear.allow


def test_cooldown_longer_when_symbol_is_churning():
    """High recent-fill-rate on a (broker, symbol) → extend cooldown
    to dampen overtrading."""
    g = _gate(dedup_enabled=False, contradiction_enabled=False)
    n = _now()
    g.record_fill(broker="ibkr", symbol="AAPL", side="buy", now=n)
    # 130s after fill: static 120 → allowed; dynamic with high fill rate
    # → +30 × 1.0 fill/min = 150s effective → still blocked.
    d_churn = g.check(
        strategy="volatility_regime", symbol="AAPL", side="buy",
        confidence=0.7, suggested_price=297.0, broker="ibkr",
        profile_mode="hunter", now=n + timedelta(seconds=130),
        market_state_score=0.0, recent_fill_rate_per_min=1.0,
    )
    assert not d_churn.allow
    assert d_churn.details["cooldown_sec"] > 120.0


def test_cooldown_dynamic_clamped_to_yaml_bounds():
    """Even with extreme inputs the cooldown stays within YAML
    [min_sec, max_sec] — the operator's hard safety band."""
    g = _gate(dedup_enabled=False, contradiction_enabled=False)
    n = _now()
    g.record_fill(broker="ibkr", symbol="AAPL", side="buy", now=n)
    # Wildly clear regime would push below the floor; the clamp catches it.
    d = g.check(
        strategy="volatility_regime", symbol="AAPL", side="buy",
        confidence=0.7, suggested_price=297.0, broker="ibkr",
        profile_mode="hunter", now=n + timedelta(seconds=10),
        market_state_score=10.0, recent_fill_rate_per_min=0.0,
    )
    # If clamped to min_sec=30, 10s elapsed → still blocked, but
    # cooldown_sec should be at the floor (no negative).
    assert d.details["cooldown_sec"] >= 30.0
    assert d.details["cooldown_sec"] <= 1800.0
