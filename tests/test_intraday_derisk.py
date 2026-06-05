"""
tests/test_intraday_derisk.py
==============================
D115 — Intraday aggregate-derisk decision logic.

Today's incident: NAV bled steadily from open to close losing ~1.4% in a
session, but the static ``max_daily_loss_pct: 0.02`` kill switch only
fires at -2%. The intraday derisk monitor must fire BEFORE that, scaling
the response with severity, and must only ever emit reduce-only actions.
"""
from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

import pytest

from brokers.base import OrderStatus
from risk.drawdown_governor import (
    DrawdownOpenLockConfig,
    derisk_execution_reduced_exposure,
    should_trigger_open_lock,
)
from risk.intraday_derisk import (
    DeriskTier,
    evaluate_intraday_derisk,
    parse_position_loss_tier,
    parse_tiers,
)


def _tiers() -> list[DeriskTier]:
    return parse_tiers(
        [
            {"threshold_pct": "-0.0050", "trim_pct": "0.20", "max_actions": 2, "min_loss_pct": "0.005"},
            {"threshold_pct": "-0.0100", "trim_pct": "0.50", "max_actions": 4, "min_loss_pct": "0.003"},
            {"threshold_pct": "-0.0150", "trim_pct": "1.00", "max_actions": 6, "min_loss_pct": "0.000"},
        ]
    )


def _pos(symbol, broker, qty, entry, current, asset_class="equity") -> dict:
    return {
        "broker": broker,
        "symbol": symbol,
        "quantity": Decimal(qty),
        "avg_entry_price": Decimal(entry),
        "current_price": Decimal(current),
        "asset_class": asset_class,
        "unrealised_pnl": (Decimal(current) - Decimal(entry)) * Decimal(qty),
    }


def test_derisk_open_lock_refresh_requires_filled_reduce_result():
    assert derisk_execution_reduced_exposure(None) is False
    assert derisk_execution_reduced_exposure(
        SimpleNamespace(status=OrderStatus.REJECTED, filled_quantity=Decimal("10"))
    ) is False
    assert derisk_execution_reduced_exposure(
        SimpleNamespace(status=OrderStatus.FILLED, filled_quantity=Decimal("0"))
    ) is False
    assert derisk_execution_reduced_exposure(
        SimpleNamespace(status=OrderStatus.PARTIALLY_FILLED, filled_quantity=Decimal("0.1"))
    ) is True


def test_open_lock_tier_trigger_respects_most_severe_first_ordering():
    cfg = DrawdownOpenLockConfig(enabled=True, trigger_tier_idx=1, cooldown_sec=900)

    assert should_trigger_open_lock(tier_idx=0, config=cfg) is True
    assert should_trigger_open_lock(tier_idx=1, config=cfg) is True
    assert should_trigger_open_lock(tier_idx=2, config=cfg) is False
    assert should_trigger_open_lock(tier_idx=-1, config=cfg) is False


def test_tiers_sorted_most_severe_first():
    tiers = _tiers()
    assert tiers[0].threshold_pct == Decimal("-0.0150")
    assert tiers[-1].threshold_pct == Decimal("-0.0050")


def test_no_action_when_day_pnl_positive():
    actions, tier, idx = evaluate_intraday_derisk(
        nav=Decimal("1000000"),
        day_pnl=Decimal("5000"),
        positions=[_pos("AAPL", "ibkr", "100", "300", "295")],
        tiers=_tiers(),
        cooldown_seconds=60,
        last_action_ts={},
        now_ts=1_700_000_000,
    )
    assert actions == []
    assert tier is None
    assert idx == -1


def test_no_action_when_drawdown_below_first_tier():
    actions, tier, _ = evaluate_intraday_derisk(
        nav=Decimal("1000000"),
        day_pnl=Decimal("-3000"),  # -0.30%
        positions=[_pos("AAPL", "ibkr", "100", "300", "298")],
        tiers=_tiers(),
        cooldown_seconds=60,
        last_action_ts={},
        now_ts=1_700_000_000,
    )
    assert actions == []
    assert tier is None


def test_tier1_light_trim_at_half_percent_drawdown():
    positions = [
        _pos("AAPL", "ibkr", "100", "300", "295"),   # -$500 (1.67% loss, passes min 0.5%)
        _pos("MSFT", "ibkr", "50", "400", "397"),    # -$150 (0.75% loss, passes)
        _pos("GLD", "ibkr", "20", "200", "201"),     # +$20 (winner)
    ]
    actions, tier, idx = evaluate_intraday_derisk(
        nav=Decimal("100000"),
        day_pnl=Decimal("-600"),  # -0.60%
        positions=positions,
        tiers=_tiers(),
        cooldown_seconds=60,
        last_action_ts={},
        now_ts=1_700_000_000,
    )
    assert tier is not None
    assert idx == 2  # tier 0 in YAML order is tier 2 here (most severe first)
    assert tier.threshold_pct == Decimal("-0.0050")
    assert tier.trim_pct == Decimal("0.20")
    assert len(actions) == 2  # max_actions=2
    # Worst loser first
    assert actions[0].symbol == "AAPL"
    # 20% trim of 100 shares
    assert actions[0].reduce_quantity == Decimal("20.00000000")
    assert actions[0].side == "sell"  # closing long


def test_tier2_heavier_trim_at_one_percent_drawdown():
    positions = [
        _pos("AAPL", "ibkr", "200", "300", "295"),  # -$1000
        _pos("NVDA", "ibkr", "30", "500", "490"),   # -$300
        _pos("TSLA", "ibkr", "-10", "200", "205"),  # -$50 (short losing)
    ]
    actions, tier, idx = evaluate_intraday_derisk(
        nav=Decimal("100000"),
        day_pnl=Decimal("-1200"),  # -1.20% (above tier1=-0.5%, tier2=-1.0% — picks tier2)
        positions=positions,
        tiers=_tiers(),
        cooldown_seconds=60,
        last_action_ts={},
        now_ts=1_700_000_000,
    )
    assert tier is not None
    assert tier.trim_pct == Decimal("0.50")
    # AAPL is worst loser; trim 50% of 200 = 100
    assert actions[0].symbol == "AAPL"
    assert actions[0].reduce_quantity == Decimal("100.00000000")


def test_tier3_full_close_at_severe_drawdown():
    positions = [
        _pos("AAPL", "ibkr", "100", "300", "295"),
        _pos("MSFT", "ibkr", "50", "400", "398"),
    ]
    actions, tier, _ = evaluate_intraday_derisk(
        nav=Decimal("100000"),
        day_pnl=Decimal("-1700"),  # -1.70% → tier3
        positions=positions,
        tiers=_tiers(),
        cooldown_seconds=60,
        last_action_ts={},
        now_ts=1_700_000_000,
    )
    assert tier is not None
    assert tier.trim_pct == Decimal("1.00")
    assert any(a.reduce_quantity == Decimal("100") for a in actions)  # full AAPL
    # Short positions close with buy
    # (not in this case, but check side for AAPL long = sell)
    for a in actions:
        if a.symbol == "AAPL":
            assert a.side == "sell"


def test_short_position_closed_with_buy():
    positions = [
        _pos("AAPL", "ibkr", "-100", "300", "303"),  # short losing -$300
    ]
    actions, _, _ = evaluate_intraday_derisk(
        nav=Decimal("100000"),
        day_pnl=Decimal("-1700"),  # tier3 full close
        positions=positions,
        tiers=_tiers(),
        cooldown_seconds=60,
        last_action_ts={},
        now_ts=1_700_000_000,
    )
    assert len(actions) == 1
    assert actions[0].symbol == "AAPL"
    assert actions[0].side == "buy"
    assert actions[0].reduce_quantity == Decimal("100")


def test_cooldown_skips_recently_acted_position():
    positions = [
        _pos("AAPL", "ibkr", "100", "300", "295"),
        _pos("MSFT", "ibkr", "50", "400", "397"),
    ]
    actions, _, _ = evaluate_intraday_derisk(
        nav=Decimal("100000"),
        day_pnl=Decimal("-1200"),
        positions=positions,
        tiers=_tiers(),
        cooldown_seconds=300,
        last_action_ts={"ibkr:AAPL": 1_699_999_900},  # 100s ago
        now_ts=1_700_000_000,
    )
    # AAPL is on cooldown; MSFT becomes the only candidate
    assert all(a.symbol != "AAPL" for a in actions)


def test_min_loss_pct_filters_marginal_losers_at_light_tier():
    positions = [
        _pos("AAPL", "ibkr", "100", "300", "299.6"),  # only 0.13% loss → below tier1 min 0.5%
        _pos("MSFT", "ibkr", "50", "400", "395"),     # 1.25% loss → passes
    ]
    actions, _, _ = evaluate_intraday_derisk(
        nav=Decimal("100000"),
        day_pnl=Decimal("-600"),  # tier1
        positions=positions,
        tiers=_tiers(),
        cooldown_seconds=60,
        last_action_ts={},
        now_ts=1_700_000_000,
    )
    assert {a.symbol for a in actions} == {"MSFT"}


def test_winners_never_trimmed():
    positions = [
        _pos("AAPL", "ibkr", "100", "300", "310"),  # +$1000 winner
        _pos("MSFT", "ibkr", "50", "400", "395"),   # -$250 loser
    ]
    actions, _, _ = evaluate_intraday_derisk(
        nav=Decimal("100000"),
        day_pnl=Decimal("-1700"),
        positions=positions,
        tiers=_tiers(),
        cooldown_seconds=60,
        last_action_ts={},
        now_ts=1_700_000_000,
    )
    # Even at the severe tier, winners are not trimmed by intraday derisk.
    assert all(a.symbol == "MSFT" for a in actions)


def test_aggregate_derisk_does_not_chop_book_when_realised_loss_already_locked():
    positions = [
        _pos("SPY", "ibkr", "100", "700", "710"),   # +$1000
        _pos("QQQ", "ibkr", "1", "600", "599"),     # tiny loser
    ]
    actions, tier, idx = evaluate_intraday_derisk(
        nav=Decimal("100000"),
        day_pnl=Decimal("-1200"),  # aggregate tier breached by already-realised loss
        positions=positions,
        tiers=_tiers(),
        cooldown_seconds=60,
        last_action_ts={},
        now_ts=1_700_000_000,
    )
    assert tier is not None
    assert idx == 1
    assert actions == []


def test_aggregate_derisk_can_be_configured_to_act_on_realised_loss_day():
    positions = [
        _pos("SPY", "ibkr", "100", "700", "710"),
        _pos("QQQ", "ibkr", "1", "600", "597"),
    ]
    actions, tier, idx = evaluate_intraday_derisk(
        nav=Decimal("100000"),
        day_pnl=Decimal("-1200"),
        positions=positions,
        tiers=_tiers(),
        cooldown_seconds=60,
        last_action_ts={},
        now_ts=1_700_000_000,
        require_open_book_loss_for_aggregate_actions=False,
    )
    assert tier is not None
    assert idx == 1
    assert [a.symbol for a in actions] == ["QQQ"]


def test_empty_positions_no_action():
    actions, tier, _ = evaluate_intraday_derisk(
        nav=Decimal("100000"),
        day_pnl=Decimal("-2000"),
        positions=[],
        tiers=_tiers(),
        cooldown_seconds=60,
        last_action_ts={},
        now_ts=1_700_000_000,
    )
    assert actions == []


def test_dynamic_scalar_adjusts_thresholds():
    positions = [
        _pos("AAPL", "ibkr", "100", "300", "295"),  # -$500 loser
    ]
    # At -0.6% drawdown, normally tier 1 (-0.5%) fires.
    # But if volatility scalar is 2.0, tier 1 threshold becomes -1.0%.
    # So -0.6% should NO LONGER trigger any derisking.
    actions, tier, _ = evaluate_intraday_derisk(
        nav=Decimal("100000"),
        day_pnl=Decimal("-600"),  # -0.60%
        positions=positions,
        tiers=_tiers(),
        cooldown_seconds=60,
        last_action_ts={},
        now_ts=1_700_000_000,
        portfolio_volatility_scalar=Decimal("2.0"),
    )
    assert tier is None
    assert actions == []
    
    # However, at -1.2% drawdown with scalar 2.0, tier 1 (-1.0% scaled) SHOULD fire.
    actions, tier, _ = evaluate_intraday_derisk(
        nav=Decimal("100000"),
        day_pnl=Decimal("-1200"),  # -1.20%
        positions=positions,
        tiers=_tiers(),
        cooldown_seconds=60,
        last_action_ts={},
        now_ts=1_700_000_000,
        portfolio_volatility_scalar=Decimal("2.0"),
    )
    assert tier is not None
    assert tier.threshold_pct == Decimal("-0.0050")  # The original tier config
    assert len(actions) == 1


def test_position_loss_tier_trims_material_underwater_loser_before_nav_drawdown():
    tier0 = parse_position_loss_tier(
        {
            "enabled": True,
            "min_loss_nav_pct": "0.0005",
            "min_loss_pct": "0.005",
            "trim_pct": "0.25",
            "max_actions": 2,
        }
    )
    assert tier0 is not None

    actions, tier, idx = evaluate_intraday_derisk(
        nav=Decimal("100000"),
        day_pnl=Decimal("100"),  # aggregate day green; position still unhealthy
        positions=[
            _pos("XLE", "ibkr", "1000", "100", "99.90"),  # -$100, 0.1% NAV, 0.1% loss
            _pos("AAPL", "ibkr", "100", "300", "298"),    # -$200, 0.2% NAV, 0.67% loss
        ],
        tiers=_tiers(),
        cooldown_seconds=60,
        last_action_ts={},
        now_ts=1_700_000_000,
        position_loss_tier=tier0,
    )

    assert tier is tier0
    assert idx == -2
    assert len(actions) == 1
    assert actions[0].symbol == "AAPL"
    assert actions[0].reduce_quantity == Decimal("25.00000000")


def test_position_loss_tier_disabled_when_config_incomplete():
    assert parse_position_loss_tier({"enabled": True, "trim_pct": "0.25"}) is None


def test_dynamic_position_loss_tightens_oversized_loser_and_scales_trim():
    tier0 = parse_position_loss_tier(
        {
            "enabled": True,
            "min_loss_nav_pct": "0.0005",
            "min_loss_pct": "0.005",
            "trim_pct": "0.25",
            "max_actions": 2,
        }
    )
    assert tier0 is not None

    actions, tier, idx = evaluate_intraday_derisk(
        nav=Decimal("100000"),
        day_pnl=Decimal("100"),
        positions=[
            _pos("SOL-USD", "kraken", "1000", "100", "99.80", "crypto"),
        ],
        tiers=_tiers(),
        cooldown_seconds=60,
        last_action_ts={},
        now_ts=1_700_000_000,
        position_loss_tier=tier0,
        dynamic_position_loss=True,
        position_loss_notional_reference_pct=Decimal("0.05"),
    )

    assert tier is tier0
    assert idx == -2
    assert len(actions) == 1
    assert actions[0].symbol == "SOL-USD"
    assert actions[0].reduce_quantity == Decimal("1000")
    assert actions[0].metadata["position_dynamic_loss"] is True
    assert Decimal(actions[0].metadata["position_dynamic_threshold_pct"]).copy_abs() < Decimal("0.0005")


def test_dynamic_position_loss_gives_volatile_position_more_room():
    tier0 = parse_position_loss_tier(
        {
            "enabled": True,
            "min_loss_nav_pct": "0.0005",
            "min_loss_pct": "0.005",
            "trim_pct": "0.25",
            "max_actions": 2,
        }
    )
    assert tier0 is not None
    pos = _pos("ETH-USD", "bybit", "100", "100", "99", "crypto")
    pos["instrument_metadata"] = {"daily_volatility": "0.05"}

    actions, tier, idx = evaluate_intraday_derisk(
        nav=Decimal("100000"),
        day_pnl=Decimal("100"),
        positions=[pos],
        tiers=_tiers(),
        cooldown_seconds=60,
        last_action_ts={},
        now_ts=1_700_000_000,
        position_loss_tier=tier0,
        dynamic_position_loss=True,
        position_loss_notional_reference_pct=Decimal("0.05"),
    )

    assert tier is tier0
    assert idx == -2
    assert actions == []
