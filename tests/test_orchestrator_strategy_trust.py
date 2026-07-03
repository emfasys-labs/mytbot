"""Tests for TradingLoop._orchestrator_strategy_trust (D166 Phase 2 scoreboard gate)."""
from __future__ import annotations

from decimal import Decimal

from portfolio.portfolio_orchestrator import OrchestratorConfig
from system.trading_loop.loop import TradingLoop

trust = TradingLoop._orchestrator_strategy_trust


def _cfg(min_closes: int = 8) -> OrchestratorConfig:
    return OrchestratorConfig(
        min_trust=Decimal("0.25"),
        max_trust=Decimal("1.50"),
        min_live_closes_for_posterior=min_closes,
    )


def test_prior_alone_cannot_amplify_with_no_live_data():
    # D231 (P2) — an optimistic backtest prior with ZERO live evidence must
    # not push trust above neutral; it still trades (edge gate governs
    # allow/block), just not amplified.
    out = trust(None, _cfg(), edge_prior={"trend_breakout": Decimal("1.50")})
    assert out["trend_breakout"] == Decimal("1.0")


def test_prior_below_neutral_unaffected_by_no_live_data():
    # The amplification gate only blocks combined > 1.0 — a below-neutral
    # prior (e.g. a weak/blocked weapon) passes through untouched.
    out = trust(None, _cfg(), edge_prior={"volatility_regime": Decimal("0.40")})
    assert out["volatility_regime"] == Decimal("0.40")


def test_amplification_allowed_with_verified_positive_evidence():
    # Enough closes, net-positive, PF > 1 → amplification is earned.
    out = trust(
        {
            "trend_breakout": {
                "net_pnl": Decimal("1000"), "fills": 10, "profit_factor": 2.5,
            }
        },
        _cfg(min_closes=8),
        edge_prior={"trend_breakout": Decimal("1.50")},
    )
    # per_fill = 100 → tilt = 1 + min(0.5, 1.0) = 1.5 → combined = 1.5*1.5=2.25,
    # clamped to max_trust 1.50.
    assert out["trend_breakout"] == Decimal("1.50")


def test_amplification_blocked_when_pf_not_above_one():
    # Net-positive and enough closes, but PF <= 1 (small net win on a mostly
    # losing book) — still not verified as a real winner.
    out = trust(
        {
            "trend_breakout": {
                "net_pnl": Decimal("10"), "fills": 10, "profit_factor": 0.95,
            }
        },
        _cfg(min_closes=8),
        edge_prior={"trend_breakout": Decimal("1.50")},
    )
    assert out["trend_breakout"] == Decimal("1.0")


def test_amplification_blocked_below_min_closes_even_if_positive():
    # Positive net but too few closes to trust — amplification withheld.
    out = trust(
        {
            "trend_breakout": {
                "net_pnl": Decimal("500"), "fills": 3, "profit_factor": 3.0,
            }
        },
        _cfg(min_closes=8),
        edge_prior={"trend_breakout": Decimal("1.50")},
    )
    assert out["trend_breakout"] == Decimal("1.0")


def test_posterior_ignored_below_min_closes():
    # Strong backtest prior, but only 3 live closes (< 8) → the too-few-closes
    # posterior itself is ignored (doesn't pull trust down). D231 (P2): the
    # prior alone still can't amplify without enough VERIFIED closes, so the
    # net effect is neutral (1.0), not the raw 1.50 prior — a strengthening
    # of the original "posterior ignored" guarantee, not a contradiction of it.
    out = trust(
        {"trend_breakout": {"net_pnl": Decimal("-1000"), "fills": 3}},
        _cfg(min_closes=8),
        edge_prior={"trend_breakout": Decimal("1.50")},
    )
    assert out["trend_breakout"] == Decimal("1.0")


def test_positive_live_tilts_up():
    out = trust(
        {"mean_reversion": {"net_pnl": Decimal("1000"), "fills": 10}},
        _cfg(min_closes=8),
        edge_prior={"mean_reversion": Decimal("1.0")},
    )
    # per_fill = 100 → tilt = 1 + min(0.5, 1.0) = 1.5
    assert out["mean_reversion"] == Decimal("1.50")


def test_live_loser_capped_at_neutral():
    # Strong prior (1.5) but proven net-negative with enough closes → capped to 1.0.
    out = trust(
        {"momentum_breakout": {"net_pnl": Decimal("-10"), "fills": 10}},
        _cfg(min_closes=8),
        edge_prior={"momentum_breakout": Decimal("1.50")},
    )
    assert out["momentum_breakout"] == Decimal("1.0")


def test_live_loser_with_low_prior_pulls_below_neutral_not_raised():
    # Posterior should pull DOWN; the neutral cap must not raise it back up.
    out = trust(
        {"event_driven_news": {"net_pnl": Decimal("-500"), "fills": 10}},
        _cfg(min_closes=8),
        edge_prior={"event_driven_news": Decimal("1.0")},
    )
    # per_fill = -50 → tilt = 1 + max(-0.75, -0.5) = 0.5 → combined 0.5 (< neutral, kept)
    assert out["event_driven_news"] == Decimal("0.5")


def test_clamped_to_min_trust():
    out = trust(
        {"x": {"net_pnl": Decimal("-100000"), "fills": 50}},
        _cfg(min_closes=8),
        edge_prior={"x": Decimal("0.30")},
    )
    assert out["x"] == Decimal("0.25")  # floor


def test_disabled_gate_min_closes_zero_uses_any_fill():
    # Back-compat: min_live_closes_for_posterior=0 → any fill counts (old behaviour),
    # and the live-loser cap is not applied.
    out = trust(
        {"x": {"net_pnl": Decimal("-10"), "fills": 1}},
        _cfg(min_closes=0),
        edge_prior={"x": Decimal("1.50")},
    )
    # per_fill=-10 → tilt = 1 + max(-0.75, -0.1)=0.9 → combined 1.35 (cap not applied)
    assert out["x"] == Decimal("1.35")
