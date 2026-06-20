"""Tests for risk/protective_exit_gate.py (D166 Phase 2 anti-churn gate)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from risk.protective_exit_gate import (
    ProtectiveExitConfig,
    parse_protective_exit_config,
    position_age_seconds_from_fills,
    should_suppress_protective_exit,
)

NOW = datetime(2026, 6, 18, 12, 0, 0, tzinfo=timezone.utc)


# ── config parsing ──────────────────────────────────────────────────────────
def test_parse_disabled_when_missing():
    assert parse_protective_exit_config(None).enabled is False
    assert parse_protective_exit_config({"enabled": False}).enabled is False


def test_parse_full_block():
    cfg = parse_protective_exit_config(
        {
            "enabled": True,
            "min_hold_sec": 259200,
            "catastrophic_loss_pct_nav": 0.04,
            "catastrophic_loss_pct_position": 0.30,
            "always_allow_structural_stop": True,
            "always_allow_most_severe_aggregate_tier": True,
            "always_allow_asset_classes": ["crypto"],
            "always_allow_position_stop_asset_classes": ["crypto"],
        }
    )
    assert cfg.enabled is True
    assert cfg.min_hold_sec == Decimal("259200")
    assert cfg.catastrophic_loss_pct_nav == Decimal("0.04")
    assert cfg.catastrophic_loss_pct_position == Decimal("0.30")
    assert cfg.always_allow_asset_classes == frozenset({"crypto"})
    assert cfg.always_allow_position_stop_asset_classes == frozenset({"crypto"})


# ── position age from fills ──────────────────────────────────────────────────
def test_age_single_open_fill():
    fills = [{"timestamp": NOW - timedelta(hours=5), "position_qty_after": Decimal("10")}]
    age = position_age_seconds_from_fills(fills, now=NOW)
    assert age == Decimal(str(5 * 3600))


def test_age_starts_after_last_flat():
    # opened, fully closed (flat), reopened 2h ago, added 1h ago.
    fills = [
        {"timestamp": NOW - timedelta(days=10), "position_qty_after": Decimal("10")},
        {"timestamp": NOW - timedelta(days=9), "position_qty_after": Decimal("0")},  # flat
        {"timestamp": NOW - timedelta(hours=2), "position_qty_after": Decimal("5")},  # reopen
        {"timestamp": NOW - timedelta(hours=1), "position_qty_after": Decimal("8")},  # add
    ]
    age = position_age_seconds_from_fills(fills, now=NOW)
    assert age == Decimal(str(2 * 3600))


def test_age_none_when_flat_now():
    fills = [
        {"timestamp": NOW - timedelta(hours=3), "position_qty_after": Decimal("5")},
        {"timestamp": NOW - timedelta(hours=1), "position_qty_after": Decimal("0")},
    ]
    assert position_age_seconds_from_fills(fills, now=NOW) is None


def test_age_none_when_no_fills():
    assert position_age_seconds_from_fills([], now=NOW) is None


def test_age_handles_naive_timestamps():
    naive = (NOW - timedelta(hours=4)).replace(tzinfo=None)
    fills = [{"timestamp": naive, "position_qty_after": Decimal("3")}]
    age = position_age_seconds_from_fills(fills, now=NOW)
    assert age == Decimal(str(4 * 3600))


# ── suppression policy ───────────────────────────────────────────────────────
def _cfg(**kw) -> ProtectiveExitConfig:
    base = dict(
        enabled=True,
        min_hold_sec=Decimal("259200"),
        catastrophic_loss_pct_nav=Decimal("0.04"),
        catastrophic_loss_pct_position=Decimal("0.30"),
    )
    base.update(kw)
    return ProtectiveExitConfig(**base)


def test_suppress_young_soft_loss():
    suppress, why = should_suppress_protective_exit(
        config=_cfg(),
        age_sec=Decimal("3600"),          # 1h old, well under 3 days
        loss_pct_nav=Decimal("0.01"),     # mild
        loss_pct_position=Decimal("0.05"),
    )
    assert suppress is True
    assert why == "within_min_hold"


def test_crypto_position_stop_not_suppressed_when_young():
    suppress, why = should_suppress_protective_exit(
        config=_cfg(always_allow_position_stop_asset_classes=frozenset({"crypto"})),
        age_sec=Decimal("3600"),
        loss_pct_nav=Decimal("0.003"),
        loss_pct_position=Decimal("0.06"),
        asset_class="crypto",
        position_stop_breached=True,
    )
    assert suppress is False
    assert why == "position_stop_crypto"


def test_crypto_soft_derisk_not_suppressed_when_asset_class_is_exempt():
    suppress, why = should_suppress_protective_exit(
        config=_cfg(always_allow_asset_classes=frozenset({"crypto"})),
        age_sec=Decimal("3600"),
        loss_pct_nav=Decimal("0.003"),
        loss_pct_position=Decimal("0.05"),
        asset_class="crypto",
    )
    assert suppress is False
    assert why == "asset_class_crypto"


def test_non_crypto_position_stop_still_suppressed_when_young():
    suppress, why = should_suppress_protective_exit(
        config=_cfg(always_allow_position_stop_asset_classes=frozenset({"crypto"})),
        age_sec=Decimal("3600"),
        loss_pct_nav=Decimal("0.003"),
        loss_pct_position=Decimal("0.06"),
        asset_class="stock_etf",
        position_stop_breached=True,
    )
    assert suppress is True
    assert why == "within_min_hold"


def test_matured_position_not_suppressed():
    suppress, why = should_suppress_protective_exit(
        config=_cfg(),
        age_sec=Decimal(str(4 * 86400)),  # 4 days
        loss_pct_nav=Decimal("0.01"),
        loss_pct_position=Decimal("0.05"),
    )
    assert suppress is False
    assert why == "matured"


def test_catastrophic_nav_always_fires_even_if_young():
    suppress, why = should_suppress_protective_exit(
        config=_cfg(),
        age_sec=Decimal("60"),
        loss_pct_nav=Decimal("0.05"),     # >= 0.04 catastrophic
        loss_pct_position=Decimal("0.05"),
    )
    assert suppress is False
    assert why == "catastrophic_nav"


def test_catastrophic_position_always_fires_even_if_young():
    suppress, why = should_suppress_protective_exit(
        config=_cfg(),
        age_sec=Decimal("60"),
        loss_pct_nav=Decimal("0.01"),
        loss_pct_position=Decimal("0.35"),  # >= 0.30
    )
    assert suppress is False
    assert why == "catastrophic_position"


def test_structural_stop_always_fires():
    suppress, why = should_suppress_protective_exit(
        config=_cfg(),
        age_sec=Decimal("60"),
        loss_pct_nav=Decimal("0.01"),
        loss_pct_position=Decimal("0.05"),
        structural_breach=True,
    )
    assert suppress is False
    assert why == "structural_stop"


def test_most_severe_aggregate_tier_always_fires():
    suppress, why = should_suppress_protective_exit(
        config=_cfg(),
        age_sec=Decimal("60"),
        loss_pct_nav=Decimal("0.01"),
        loss_pct_position=Decimal("0.05"),
        is_most_severe_aggregate_tier=True,
    )
    assert suppress is False
    assert why == "most_severe_tier"


def test_unknown_age_does_not_suppress():
    suppress, why = should_suppress_protective_exit(
        config=_cfg(),
        age_sec=None,
        loss_pct_nav=Decimal("0.01"),
        loss_pct_position=Decimal("0.05"),
    )
    assert suppress is False
    assert why == "age_unknown"


def test_disabled_gate_never_suppresses():
    suppress, why = should_suppress_protective_exit(
        config=ProtectiveExitConfig(enabled=False),
        age_sec=Decimal("60"),
        loss_pct_nav=Decimal("0.01"),
        loss_pct_position=Decimal("0.05"),
    )
    assert suppress is False
    assert why == "gate_disabled"
