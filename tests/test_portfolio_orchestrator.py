"""
tests/test_portfolio_orchestrator.py
=====================================

Locks in the D156 portfolio netting orchestrator: the layer that stops
strategies from cancelling each other's edge and stops the rotation layer
from force-closing maturing positions.

Covers the three stages — alpha combination, portfolio construction,
edge-protected rebalance — plus the four behaviours the diagnosis demanded:
  1. Opposing strategies on the same symbol NET to one position (no long+short pair).
  2. Conviction-weighted sizing (not equal-weight).
  3. Edge protection: a profitable / young position is NOT flipped for a weak signal.
  4. Deliberate net management: |net| is capped, sub-band churn suppressed.
"""

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

from portfolio.portfolio_orchestrator import (
    BookPosition,
    OrchestratorConfig,
    StrategyIntent,
    build_intents_from_candidates,
    build_intents_from_raw_signals,
    orchestrate,
)

NAV = Decimal("1000000")


def _cfg(**over) -> OrchestratorConfig:
    base = OrchestratorConfig(
        enabled=True,
        entry_conviction_threshold=Decimal("0.15"),
        flip_conviction_threshold=Decimal("0.45"),
        hard_flip_conviction=Decimal("0.75"),
        concentration_exponent=Decimal("1.5"),
        max_position_pct_of_nav=Decimal("0.10"),
        gross_target_pct={"trader": Decimal("0.90"), "hunter": Decimal("1.30"), "defender": Decimal("0.50")},
        net_cap_pct_of_gross=Decimal("0.60"),
        rebalance_band_pct_of_nav=Decimal("0.01"),
        min_hold_sec_before_flip=Decimal("1800"),
        close_edge_floor=Decimal("0"),
    )
    if over:
        from dataclasses import replace
        return replace(base, **over)
    return base


# ── 1. ALPHA COMBINATION — opposing strategies net, no offsetting pair ───────
def test_opposing_strategies_net_to_single_position():
    intents = [
        StrategyIntent("SPY", "buy", Decimal("0.8"), "mean_reversion", "etf"),
        StrategyIntent("SPY", "sell", Decimal("0.3"), "volume_flow", "etf"),
    ]
    res = orchestrate(intents, [], nav=NAV, mode="trader", config=_cfg())
    spy = [t for t in res.targets if t.symbol == "SPY"][0]
    # Net conviction 0.8 - 0.3 = +0.5 → one LONG target, not two offsetting legs.
    assert spy.net_conviction == Decimal("0.5")
    assert spy.target_notional > 0
    assert spy.had_conflict is True
    assert res.diagnostics["conflicts_resolved"] == 1
    # Exactly one order for SPY, and it's a buy (open long).
    spy_orders = [o for o in res.orders if o.symbol == "SPY"]
    assert len(spy_orders) == 1
    assert spy_orders[0].side == "buy"


def test_perfectly_opposing_strategies_cancel_to_flat():
    intents = [
        StrategyIntent("QQQ", "buy", Decimal("0.5"), "mean_reversion", "etf"),
        StrategyIntent("QQQ", "sell", Decimal("0.5"), "volume_flow", "etf"),
    ]
    res = orchestrate(intents, [], nav=NAV, mode="trader", config=_cfg())
    # Net 0 → no position opened (below entry threshold), no order.
    assert not any(o.symbol == "QQQ" for o in res.orders)


# ── 2. PORTFOLIO CONSTRUCTION — conviction-weighted, not equal-weight ────────
def test_higher_conviction_gets_more_capital():
    # Per-name cap raised so it doesn't clamp both names to the same ceiling.
    intents = [
        StrategyIntent("AAA", "buy", Decimal("0.9"), "momentum_breakout", "equity"),
        StrategyIntent("BBB", "buy", Decimal("0.3"), "momentum_breakout", "equity"),
    ]
    res = orchestrate(intents, [], nav=NAV, mode="trader",
                      config=_cfg(max_position_pct_of_nav=Decimal("0.90")))
    by = {t.symbol: t.target_notional for t in res.targets}
    assert by["AAA"] > by["BBB"]
    # Concentration exponent > 1 → the ratio exceeds the linear conviction ratio.
    assert by["AAA"] / by["BBB"] > (Decimal("0.9") / Decimal("0.3"))


def test_per_name_concentration_cap_enforced():
    intents = [StrategyIntent("AAA", "buy", Decimal("0.95"), "momentum_breakout", "equity")]
    res = orchestrate(intents, [], nav=NAV, mode="hunter", config=_cfg(max_position_pct_of_nav=Decimal("0.05")))
    aaa = [t for t in res.targets if t.symbol == "AAA"][0]
    assert aaa.target_notional <= NAV * Decimal("0.05") + Decimal("0.01")


def test_fx_pipeline_alias_nets_against_native_book_position():
    # Pipeline candidates use yfinance-style GBPUSD=X, while the broker/local
    # book holds native GBPUSD. They must be one book slot, otherwise the
    # allocator tries to open a duplicate GBPUSD leg that final risk rejects.
    book = [
        BookPosition(
            "GBPUSD",
            Decimal("200000"),
            Decimal("1.25"),
            Decimal("1.25"),
            "forex",
            "ibkr",
            holding_sec=Decimal("99999"),
            unrealised_pnl=Decimal("0"),
        )
    ]
    intents = [StrategyIntent("GBPUSD=X", "buy", Decimal("0.80"), "mean_reversion", "forex")]
    res = orchestrate(
        intents,
        book,
        nav=NAV,
        mode="trader",
        config=_cfg(
            gross_target_pct={"trader": Decimal("0.10")},
            max_position_pct_of_nav=Decimal("0.10"),
            rebalance_band_pct_of_nav=Decimal("0.001"),
        ),
    )

    assert any(t.symbol == "GBPUSD" for t in res.targets)
    assert not any(t.symbol == "GBPUSD=X" for t in res.targets)
    assert not any(o.symbol == "GBPUSD=X" for o in res.orders)
    assert not any(o.symbol == "GBPUSD" and o.side == "buy" and not o.reduce_only for o in res.orders)
    gbp_orders = [o for o in res.orders if o.symbol == "GBPUSD"]
    assert gbp_orders and gbp_orders[0].side == "sell" and gbp_orders[0].reduce_only
    assert res.diagnostics["symbol_aliases_normalized"] >= 1


def test_candidate_adapter_normalizes_fx_pipeline_alias():
    cand = SimpleNamespace(
        symbol="EURUSD=X",
        side="long",
        confidence=Decimal("0.55"),
        strategy_name="mean_reversion",
        asset_class="forex",
        metadata={},
    )
    intents = build_intents_from_candidates([cand])
    assert len(intents) == 1
    assert intents[0].symbol == "EURUSD"


# ── 3. EDGE PROTECTION — don't flip/close a position that still has edge ─────
def test_profitable_position_not_flipped_by_weak_signal():
    book = [BookPosition("XYZ", Decimal("100"), Decimal("50"), Decimal("55"),
                         "equity", holding_sec=Decimal("99999"), unrealised_pnl=Decimal("500"))]
    # Weak opposing conviction (0.3 < hard_flip 0.75 because position is profitable).
    intents = [StrategyIntent("XYZ", "sell", Decimal("0.3"), "volume_flow", "equity")]
    res = orchestrate(intents, book, nav=NAV, mode="trader", config=_cfg())
    assert not any(o.symbol == "XYZ" for o in res.orders)
    assert res.diagnostics["protected_positions"] == 1


def test_young_position_protected_from_flip():
    # Just opened (60s old), flat P&L. A normal-strength opposing signal (0.5)
    # should NOT flip it — young positions need hard_flip conviction.
    book = [BookPosition("YNG", Decimal("100"), Decimal("50"), Decimal("50"),
                         "equity", holding_sec=Decimal("60"), unrealised_pnl=Decimal("0"))]
    intents = [StrategyIntent("YNG", "sell", Decimal("0.5"), "momentum_breakout", "equity")]
    res = orchestrate(intents, book, nav=NAV, mode="trader", config=_cfg())
    assert not any(o.symbol == "YNG" for o in res.orders)
    assert res.diagnostics["protected_positions"] == 1


def test_strong_opposing_conviction_does_flip_old_losing_position():
    # Old, losing position + strong opposing conviction (0.8 > hard_flip 0.75) → flip.
    book = [BookPosition("FLP", Decimal("100"), Decimal("50"), Decimal("45"),
                         "equity", holding_sec=Decimal("99999"), unrealised_pnl=Decimal("-500"))]
    intents = [StrategyIntent("FLP", "sell", Decimal("0.8"), "momentum_breakout", "equity")]
    res = orchestrate(intents, book, nav=NAV, mode="trader", config=_cfg())
    flp = [o for o in res.orders if o.symbol == "FLP"]
    assert len(flp) == 1
    # Flip closes to flat first (reduce_only), re-entry next tick.
    assert flp[0].close_only is True and flp[0].reduce_only is True
    assert flp[0].side == "sell"
    assert res.diagnostics["flips"] == 1


def test_flat_desire_keeps_profitable_position():
    # No fresh signal but position is profitable → don't close to "recycle".
    book = [BookPosition("WIN", Decimal("100"), Decimal("50"), Decimal("60"),
                         "equity", holding_sec=Decimal("99999"), unrealised_pnl=Decimal("1000"))]
    res = orchestrate([], book, nav=NAV, mode="trader", config=_cfg())
    assert not any(o.symbol == "WIN" for o in res.orders)
    assert res.diagnostics["protected_positions"] == 1


def test_flat_desire_keeps_losing_position_without_exit_evidence():
    # No fresh signal is silence, not evidence to crystallise a loss.
    book = [BookPosition("LOS", Decimal("100"), Decimal("50"), Decimal("40"),
                         "equity", holding_sec=Decimal("99999"), unrealised_pnl=Decimal("-1000"))]
    res = orchestrate([], book, nav=NAV, mode="trader", config=_cfg())
    assert not any(o.symbol == "LOS" for o in res.orders)
    assert res.diagnostics["silence_closes_suppressed"] == 1


def test_flat_desire_close_remains_available_as_explicit_legacy_opt_in():
    book = [BookPosition("LOS", Decimal("100"), Decimal("50"), Decimal("40"),
                         "equity", holding_sec=Decimal("99999"), unrealised_pnl=Decimal("-1000"))]
    res = orchestrate(
        [],
        book,
        nav=NAV,
        mode="trader",
        config=_cfg(close_on_signal_silence=True),
    )
    los = [o for o in res.orders if o.symbol == "LOS"]
    assert len(los) == 1
    assert los[0].close_only is True
    assert res.diagnostics["closes"] == 1


# ── 4. NET MANAGEMENT + CHURN SUPPRESSION ────────────────────────────────────
def test_net_exposure_is_capped_when_two_sided():
    # Heavily long-biased but two-sided book: 4 longs + 1 short. The net cap
    # trims the heavy (long) side so |net|/gross <= 0.60. (A one-sided book is
    # intentionally left fully directional — see test below.)
    intents = [
        StrategyIntent("L1", "buy", Decimal("0.8"), "momentum_breakout", "equity"),
        StrategyIntent("L2", "buy", Decimal("0.8"), "momentum_breakout", "equity"),
        StrategyIntent("L3", "buy", Decimal("0.8"), "momentum_breakout", "equity"),
        StrategyIntent("L4", "buy", Decimal("0.8"), "momentum_breakout", "equity"),
        StrategyIntent("S1", "sell", Decimal("0.8"), "momentum_breakout", "equity"),
    ]
    # Natural net/gross of a 4-long:1-short book is 0.60; a 0.40 cap forces a trim.
    res = orchestrate(intents, [], nav=NAV, mode="trader",
                      config=_cfg(net_cap_pct_of_gross=Decimal("0.40"), max_position_pct_of_nav=Decimal("0.90")))
    gross = sum(abs(t.target_notional) for t in res.targets)
    net = sum(t.target_notional for t in res.targets)
    assert gross > 0
    assert abs(net) <= gross * Decimal("0.40") + Decimal("1")
    assert res.diagnostics.get("net_capped") is True


def test_one_sided_book_left_fully_directional():
    # All-long, high conviction → keep it fully net (this is the desired
    # conviction expression; net cap must NOT shrink a one-way book).
    intents = [
        StrategyIntent(f"L{i}", "buy", Decimal("0.8"), "momentum_breakout", "equity")
        for i in range(5)
    ]
    res = orchestrate(intents, [], nav=NAV, mode="trader",
                      config=_cfg(net_cap_pct_of_gross=Decimal("0.60"), max_position_pct_of_nav=Decimal("0.90")))
    gross = sum(abs(t.target_notional) for t in res.targets)
    net = sum(t.target_notional for t in res.targets)
    assert net == gross  # fully directional, untouched
    assert res.diagnostics.get("net_capped") is not True


def test_sub_band_diff_suppressed():
    # Target ~= current within the rebalance band → no churn order.
    book = [BookPosition("HLD", Decimal("1000"), Decimal("100"), Decimal("100"),
                         "equity", holding_sec=Decimal("99999"), unrealised_pnl=Decimal("0"))]
    # Conviction sized so target ≈ current (100k). Band = 1% NAV = 10k.
    intents = [StrategyIntent("HLD", "buy", Decimal("0.5"), "momentum_breakout", "equity")]
    res = orchestrate(intents, book, nav=NAV, mode="trader",
                      config=_cfg(max_position_pct_of_nav=Decimal("0.10"),
                                  gross_target_pct={"trader": Decimal("0.10")}))
    # Single name, gross budget 10% NAV = 100k, target ≈ current 100k → suppressed.
    assert not any(o.symbol == "HLD" for o in res.orders)
    assert res.diagnostics["suppressed_rebalances"] >= 1


def test_strategy_trust_downweights_bleeders():
    cfg = _cfg(strategy_trust={"volume_flow": Decimal("0.25"), "momentum_breakout": Decimal("1.5")})
    intents = [
        StrategyIntent("ZZZ", "buy", Decimal("0.6"), "momentum_breakout", "equity"),
        StrategyIntent("ZZZ", "sell", Decimal("0.6"), "volume_flow", "equity"),
    ]
    res = orchestrate(intents, [], nav=NAV, mode="trader", config=cfg)
    zzz = [t for t in res.targets if t.symbol == "ZZZ"][0]
    # 0.6*1.5 - 0.6*0.25 = 0.9 - 0.15 = +0.75 → trusted strategy wins.
    assert zzz.net_conviction == Decimal("0.75")
    assert zzz.target_notional > 0


def test_disabled_config_default():
    assert OrchestratorConfig().enabled is False


def test_nav_zero_aborts_safely():
    res = orchestrate([StrategyIntent("A", "buy", Decimal("0.5"), "s")], [],
                      nav=Decimal("0"), mode="trader", config=_cfg())
    assert res.orders == []
    assert res.diagnostics.get("abort") == "nav<=0"


# ── Adapter ───────────────────────────────────────────────────────────────
def test_build_intents_from_raw_signals_skips_hold():
    class _RS:
        def __init__(self, symbol, side, confidence):
            self.symbol = symbol; self.side = side; self.confidence = confidence
            self.strategy = "s"; self.asset_class = "equity"; self.broker = "ibkr"
            self.metadata = {}
    out = build_intents_from_raw_signals([_RS("A", "buy", 0.6), _RS("B", "hold", 0.9), _RS("C", "sell", 0.4)])
    assert [i.symbol for i in out] == ["A", "C"]
    assert out[0].conviction == Decimal("0.6")


def test_from_yaml_parses_block():
    cfg = OrchestratorConfig.from_yaml({
        "enabled": True,
        "entry_conviction_threshold": 0.2,
        "gross_target_pct": {"trader": 1.0, "hunter": 1.5},
        "net_cap_pct_of_gross": 0.5,
        "close_on_signal_silence": True,
    })
    assert cfg.enabled is True
    assert cfg.entry_conviction_threshold == Decimal("0.2")
    assert cfg.gross_target_for("hunter") == Decimal("1.5")
    assert cfg.net_cap_pct_of_gross == Decimal("0.5")
    assert cfg.close_on_signal_silence is True


# ── D158 Phase 2 — temperament × threat (heterogeneous army) ─────────────────
def _temp_cfg(**over):
    from dataclasses import replace
    base = _cfg(
        max_position_pct_of_nav=Decimal("0.90"),
        temperaments={
            "sniper": {"size_mult": Decimal("1.30"), "defensive_cut": Decimal("0.30")},
            "shotgun": {"size_mult": Decimal("1.00"), "defensive_cut": Decimal("0.60")},
            "knife": {"size_mult": Decimal("0.75"), "defensive_cut": Decimal("1.00")},
        },
        weapon_temperament={"trend_breakout": "sniper", "mean_reversion": "knife",
                            "trend_following": "shotgun"},
        mode_threat={"hunter": Decimal("0"), "trader": Decimal("0.35"), "defender": Decimal("0.75")},
    )
    return replace(base, **over) if over else base


def test_threat_for_maps_mode():
    c = _temp_cfg()
    assert c.threat_for("hunter") == Decimal("0")
    assert c.threat_for("defender") == Decimal("0.75")
    assert c.threat_for("trader") == Decimal("0.35")


def test_temperament_factor_calm_market():
    c = _temp_cfg()
    threat = c.threat_for("hunter")  # 0
    # In calm: factor == size_mult (no defensive cut).
    assert c.temperament_factor("trend_breakout", threat) == Decimal("1.30")   # sniper
    assert c.temperament_factor("trend_following", threat) == Decimal("1.00")  # shotgun
    assert c.temperament_factor("mean_reversion", threat) == Decimal("0.75")   # knife


def test_temperament_factor_in_danger_cuts_knife_hardest():
    c = _temp_cfg()
    threat = c.threat_for("defender")  # 0.75
    sniper = c.temperament_factor("trend_breakout", threat)    # 1.30×(1-0.75×0.30)=1.30×0.775=1.0075
    shotgun = c.temperament_factor("trend_following", threat)  # 1.00×(1-0.75×0.60)=1.00×0.55=0.55
    knife = c.temperament_factor("mean_reversion", threat)     # 0.75×(1-0.75×1.00)=0.75×0.25=0.1875
    # The army retreats heterogeneously: knife cut hardest, sniper barely touched.
    assert sniper > shotgun > knife
    assert knife < Decimal("0.20")
    assert sniper > Decimal("1.0")


def test_unknown_weapon_factor_is_neutral():
    c = _temp_cfg()
    assert c.temperament_factor("some_unmapped_strategy", Decimal("0.75")) == Decimal("1")


def test_temperament_shifts_book_mix_in_danger():
    # A sniper and a knife, equal raw conviction. In calm the sniper already
    # sizes bigger; in danger the gap widens sharply (knife cut hardest).
    intents = [
        StrategyIntent("AAA", "buy", Decimal("0.6"), "trend_breakout", "equity"),  # sniper
        StrategyIntent("BBB", "buy", Decimal("0.6"), "mean_reversion", "equity"),  # knife
    ]
    calm = orchestrate(intents, [], nav=NAV, mode="hunter", config=_temp_cfg())
    danger = orchestrate(intents, [], nav=NAV, mode="defender", config=_temp_cfg())
    calm_by = {t.symbol: t.target_notional for t in calm.targets}
    danger_by = {t.symbol: t.target_notional for t in danger.targets}
    # Calm: both fire, sniper already sizes bigger than knife.
    assert calm_by["AAA"] > calm_by["BBB"] > 0
    # Danger: the knife is throttled so hard its net conviction falls below the
    # entry bar — it drops out entirely, while the resilient sniper stays.
    assert danger_by.get("AAA", Decimal("0")) > 0
    assert danger_by.get("BBB", Decimal("0")) == 0
    assert danger.diagnostics["threat_level"] == "0.75"
    assert "trend_breakout" in danger.diagnostics["temperament_factors"]


def test_from_yaml_parses_temperaments():
    cfg = OrchestratorConfig.from_yaml({
        "enabled": True,
        "temperaments": {"sniper": {"size_mult": 1.3, "defensive_cut": 0.3}},
        "weapon_temperament": {"trend_breakout": "sniper"},
        "mode_threat": {"hunter": 0.0, "defender": 0.8},
    })
    assert cfg.weapon_temperament["trend_breakout"] == "sniper"
    assert cfg.temperaments["sniper"]["size_mult"] == Decimal("1.3")
    assert cfg.threat_for("defender") == Decimal("0.8")


def test_no_temperament_config_is_backward_compatible():
    # The default _cfg() has no temperament config → factor 1.0 everywhere.
    c = _cfg()
    assert c.temperament_factor("trend_breakout", Decimal("0.75")) == Decimal("1")


# ── D158 Phase 3.1 — young positions held through early noise on flat-desire ──
def test_young_losing_position_held_on_flat_desire():
    # A fresh breakout sniper opened, went slightly negative, and the weapon is
    # now quiet (flat desire). Within the min-hold window it must be HELD, not
    # closed on noise — even though it currently has no edge.
    book = [BookPosition("SNP", Decimal("100"), Decimal("50"), Decimal("49"),
                         "equity", holding_sec=Decimal("100"), unrealised_pnl=Decimal("-100"))]
    res = orchestrate([], book, nav=NAV, mode="trader",
                      config=_cfg(min_hold_sec_before_flip=Decimal("259200")))
    assert not any(o.symbol == "SNP" for o in res.orders)
    assert res.diagnostics["protected_positions"] == 1


def test_young_position_is_not_trimmed_for_target_weight_noise():
    book = [
        BookPosition(
            "AUDUSD",
            Decimal("200000"),
            Decimal("0.69"),
            Decimal("0.69"),
            "forex",
            "ibkr",
            holding_sec=Decimal("300"),
            unrealised_pnl=Decimal("0"),
        )
    ]
    intents = [
        StrategyIntent(
            "AUDUSD",
            "buy",
            Decimal("0.8"),
            "trend_following",
            "forex",
            "ibkr",
        )
    ]

    res = orchestrate(
        intents,
        book,
        nav=NAV,
        mode="trader",
        config=_cfg(
            gross_target_pct={"trader": Decimal("0.10")},
            max_position_pct_of_nav=Decimal("0.10"),
            rebalance_band_pct_of_nav=Decimal("0.001"),
            min_hold_sec_before_flip=Decimal("259200"),
        ),
    )

    assert not any(order.symbol == "AUDUSD" for order in res.orders)
    assert res.diagnostics["young_reductions_suppressed"] == 1


def test_old_losing_position_still_held_without_exit_evidence():
    # Age does not turn silence into evidence to crystallise a loss.
    book = [BookPosition("OLD", Decimal("100"), Decimal("50"), Decimal("49"),
                         "equity", holding_sec=Decimal("999999"), unrealised_pnl=Decimal("-100"))]
    res = orchestrate([], book, nav=NAV, mode="trader",
                      config=_cfg(min_hold_sec_before_flip=Decimal("259200")))
    assert not any(o.symbol == "OLD" for o in res.orders)
    assert res.diagnostics["silence_closes_suppressed"] == 1


# ── D163 — dynamic per-name cap from live opportunity breadth ─────────────────
def test_per_name_cap_concentrates_when_few_edges():
    # One lone strong edge → operating per-name cap pinned to the safety
    # ceiling (concentrate into the available edge).
    intents = [StrategyIntent("AAA", "buy", Decimal("0.9"), "trend_breakout", "equity")]
    res = orchestrate(intents, [], nav=NAV, mode="trader",
                      config=_cfg(max_position_pct_of_nav=Decimal("0.20")))
    assert res.diagnostics["firing_breadth"] == 1
    # gross/breadth = 0.90/1 → clamped to ceiling 0.20.
    assert res.diagnostics["per_name_frac"] == "0.20"
    aaa = [t for t in res.targets if t.symbol == "AAA"][0]
    assert aaa.target_notional == NAV * Decimal("0.20")


def test_per_name_cap_diversifies_when_many_edges():
    # Twelve equal independent edges → operating per-name cap shrinks well
    # below the ceiling (diversify), derived purely from breadth.
    intents = [
        StrategyIntent(f"S{i}", "buy", Decimal("0.5"), "trend_breakout", "equity")
        for i in range(12)
    ]
    res = orchestrate(intents, [], nav=NAV, mode="trader",
                      config=_cfg(max_position_pct_of_nav=Decimal("0.20")))
    assert res.diagnostics["firing_breadth"] == 12
    # gross 0.90 / 12 = 0.075 < ceiling 0.20 → dynamic, not the flat cap.
    assert res.diagnostics["per_name_frac"] == "0.075"


def test_per_name_cap_respects_safety_floor():
    # Many tiny edges would drive gross/breadth below the floor; the floor
    # bound holds (per-name never collapses to dust).
    intents = [
        StrategyIntent(f"S{i}", "buy", Decimal("0.4"), "trend_breakout", "equity")
        for i in range(40)
    ]
    res = orchestrate(intents, [], nav=NAV, mode="trader",
                      config=_cfg(max_position_pct_of_nav=Decimal("0.20"),
                                  min_position_pct_of_nav=Decimal("0.03")))
    assert res.diagnostics["firing_breadth"] == 40
    # 0.90/40 = 0.0225 < floor 0.03 → clamped up to the floor.
    assert res.diagnostics["per_name_frac"] == "0.03"


def test_edge_kelly_trust_tilts_book_toward_strongest_weapon():
    # Two equal-conviction signals from different weapons; injecting an
    # edge-Kelly trust prior routes more notional to the trusted weapon.
    intents = [
        StrategyIntent("AAA", "buy", Decimal("0.6"), "trend_breakout", "equity"),
        StrategyIntent("BBB", "buy", Decimal("0.6"), "mean_reversion", "equity"),
    ]
    # Ceiling high enough that the per-name cap does not flatten the tilt.
    cfg = _cfg(
        max_position_pct_of_nav=Decimal("0.60"),
        strategy_trust={"trend_breakout": Decimal("1.5"), "mean_reversion": Decimal("1.0")},
    )
    res = orchestrate(intents, [], nav=NAV, mode="trader", config=cfg)
    by = {t.symbol: t.target_notional for t in res.targets}
    assert by["AAA"] > by["BBB"] > 0
