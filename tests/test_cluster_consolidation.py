"""
tests/test_cluster_consolidation.py
===================================

D160 — cluster-aware construction: correlated same-direction signals (forex
by USD direction, equity-index by beta, crypto by crypto-beta) collapse into
ONE big bet in the strongest member instead of fragmenting capital across many
near-identical names.
"""

from __future__ import annotations

from decimal import Decimal

from portfolio.cluster_map import fx_orientation, theme_for
from portfolio.portfolio_orchestrator import (
    OrchestratorConfig,
    StrategyIntent,
    consolidate_clusters,
    orchestrate,
)

NAV = Decimal("1225000")


def _cfg(**over):
    base = OrchestratorConfig(
        enabled=True, cluster_consolidation=True,
        max_position_pct_of_nav=Decimal("1.0"),
        gross_target_pct={"hunter": Decimal("1.5"), "trader": Decimal("1.1")},
        concentration_exponent=Decimal("2.5"), net_cap_pct_of_gross=Decimal("1.0"),
        entry_conviction_threshold=Decimal("0.08"),
    )
    if over:
        from dataclasses import replace
        return replace(base, **over)
    return base


# ── cluster_map primitives ───────────────────────────────────────────────────
def test_fx_orientation():
    assert fx_orientation("EURUSD") == -1   # xxxUSD: long = short USD
    assert fx_orientation("AUDUSD=X") == -1
    assert fx_orientation("USDJPY") == 1     # USDxxx: long = long USD
    assert fx_orientation("USDCAD") == 1
    assert fx_orientation("EURGBP") == 0     # no USD leg


def test_theme_for_forex_usd_direction():
    # buy EURUSD = short USD (-1); sell USDJPY = short USD (-1)
    assert theme_for("EURUSD", "forex", "buy") == ("fx_usd", -1)
    assert theme_for("USDJPY", "forex", "sell") == ("fx_usd", -1)
    # sell EURUSD = long USD (+1); buy USDJPY = long USD (+1)
    assert theme_for("EURUSD", "forex", "sell") == ("fx_usd", 1)
    assert theme_for("USDJPY", "forex", "buy") == ("fx_usd", 1)


def test_theme_for_crypto_and_index():
    assert theme_for("BTC-USD", "crypto", "buy") == ("crypto_beta", 1)
    assert theme_for("ETH-USD", "crypto", "sell") == ("crypto_beta", -1)
    assert theme_for("SPY", "equity", "buy") == ("equity_index", 1)
    assert theme_for("AAPL", "equity", "buy") == (None, 0)   # single stock, not clustered


# ── consolidation ────────────────────────────────────────────────────────────
def test_five_short_usd_pairs_become_one_bet():
    intents = [
        StrategyIntent("AUDUSD", "buy", Decimal("0.30"), "mean_reversion", "forex"),
        StrategyIntent("EURUSD", "buy", Decimal("0.40"), "mean_reversion", "forex"),
        StrategyIntent("USDCAD", "sell", Decimal("0.25"), "mean_reversion", "forex"),
        StrategyIntent("USDCHF", "sell", Decimal("0.30"), "mean_reversion", "forex"),
        StrategyIntent("USDJPY", "sell", Decimal("0.35"), "mean_reversion", "forex"),
    ]
    out, n = consolidate_clusters(intents, _cfg())
    assert n == 1                       # one cluster consolidated
    assert len(out) == 1                # five pairs → one bet
    o = out[0]
    assert o.symbol == "EURUSD"         # strongest member (0.40)
    assert o.side == "buy"              # buy EURUSD = short USD (the net theme)
    # Same-strategy correlated evidence is max conviction + a small breadth
    # bonus, not five independent confirmations.
    assert o.conviction == Decimal("0.50")


def test_multi_weapon_cluster_confirmation_can_lift_conviction():
    intents = [
        StrategyIntent("EURUSD", "buy", Decimal("0.40"), "mean_reversion", "forex"),
        StrategyIntent("USDJPY", "sell", Decimal("0.50"), "trend_breakout", "forex"),
    ]
    out, n = consolidate_clusters(intents, _cfg())
    assert n == 1
    assert len(out) == 1
    # Independent strategies agreeing: 0.40 + 0.50 + 0.25 diversity bonus.
    assert out[0].conviction == Decimal("1.15")


def test_opposing_usd_signals_net_out():
    # 2 short-USD vs 2 long-USD of equal strength → theme cancels → no position
    intents = [
        StrategyIntent("EURUSD", "buy", Decimal("0.4"), "mean_reversion", "forex"),   # short USD
        StrategyIntent("USDJPY", "sell", Decimal("0.4"), "mean_reversion", "forex"),  # short USD
        StrategyIntent("GBPUSD", "sell", Decimal("0.4"), "mean_reversion", "forex"),  # long USD
        StrategyIntent("USDCAD", "buy", Decimal("0.4"), "mean_reversion", "forex"),   # long USD
    ]
    out, n = consolidate_clusters(intents, _cfg())
    assert n == 1
    assert out == []                    # net zero → express nothing


def test_single_member_cluster_passes_through():
    intents = [StrategyIntent("BTC-USD", "buy", Decimal("0.9"), "trend_breakout", "crypto")]
    out, n = consolidate_clusters(intents, _cfg())
    assert n == 0                       # only one crypto signal → nothing to consolidate
    assert len(out) == 1 and out[0].symbol == "BTC-USD"


def test_non_clustered_symbols_untouched():
    intents = [
        StrategyIntent("AAPL", "buy", Decimal("0.5"), "momentum_breakout", "equity"),
        StrategyIntent("GLD", "buy", Decimal("0.4"), "mean_reversion", "equity"),
    ]
    out, n = consolidate_clusters(intents, _cfg())
    assert n == 0
    assert {o.symbol for o in out} == {"AAPL", "GLD"}


def test_end_to_end_one_big_forex_position():
    intents = [
        StrategyIntent("AUDUSD", "buy", Decimal("0.30"), "mean_reversion", "forex"),
        StrategyIntent("EURUSD", "buy", Decimal("0.40"), "mean_reversion", "forex"),
        StrategyIntent("USDJPY", "sell", Decimal("0.35"), "mean_reversion", "forex"),
        StrategyIntent("GLD", "buy", Decimal("0.30"), "mean_reversion", "equity"),
    ]
    res = orchestrate(intents, [], nav=NAV, mode="hunter", config=_cfg())
    syms = {o.symbol for o in res.orders}
    # forex collapsed to EURUSD; GLD separate. No AUDUSD/USDJPY fragments.
    assert "EURUSD" in syms and "GLD" in syms
    assert "AUDUSD" not in syms and "USDJPY" not in syms
    assert res.diagnostics["clusters_consolidated"] == 1


def test_disabled_keeps_fragmented():
    intents = [
        StrategyIntent("AUDUSD", "buy", Decimal("0.30"), "mean_reversion", "forex"),
        StrategyIntent("EURUSD", "buy", Decimal("0.40"), "mean_reversion", "forex"),
    ]
    res = orchestrate(intents, [], nav=NAV, mode="hunter",
                      config=_cfg(cluster_consolidation=False))
    syms = {o.symbol for o in res.orders}
    assert "AUDUSD" in syms and "EURUSD" in syms   # both kept (fragmented)
