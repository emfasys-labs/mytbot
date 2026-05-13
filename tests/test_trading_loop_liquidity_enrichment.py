"""
tests/test_trading_loop_liquidity_enrichment.py
================================================

Verifies the new ADV / dollar-volume / realised-vol enrichment helpers
populate the same metadata keys the Wave 9 cost gate consumes, and that
Wave 9 stops applying the unknown-liquidity penalty when those keys are
present.
"""

from __future__ import annotations

from types import SimpleNamespace

import pandas as pd
import pytest

from execution.wave9_runtime import (
    Wave9RuntimeConfig,
    pre_flight_cost_gate,
)
from system.trading_loop.helpers import (
    enrich_candidate_liquidity,
    enrich_signal_liquidity,
)


def _df() -> pd.DataFrame:
    rows = []
    px = 100.0
    for i in range(30):
        px *= 1.001 if i % 2 == 0 else 0.999
        rows.append({"close": px, "volume": 1_000_000 + i * 100})
    return pd.DataFrame(rows)


def test_enrich_signal_liquidity_populates_wave9_keys() -> None:
    sig = SimpleNamespace(metadata={})
    enrich_signal_liquidity(sig, _df())
    md = sig.metadata
    assert md["daily_volume"] > 0
    assert md["avg_daily_volume"] == md["daily_volume"]
    assert md["daily_dollar_volume"] > md["daily_volume"]
    assert md["daily_volatility"] > 0


def test_enrich_candidate_liquidity_populates_wave9_keys() -> None:
    cand = SimpleNamespace(metadata={})
    enrich_candidate_liquidity(cand, _df())
    md = cand.metadata
    assert md["daily_volume"] > 0
    assert md["daily_volatility"] > 0


def test_enrich_skips_when_df_missing_columns() -> None:
    sig = SimpleNamespace(metadata={})
    enrich_signal_liquidity(sig, pd.DataFrame({"foo": [1, 2, 3]}))
    assert sig.metadata == {}


def test_wave9_unknown_liquidity_penalty_clears_when_known() -> None:
    cfg = Wave9RuntimeConfig.load()
    cfg.enabled = True
    cfg.unknown_liquidity_penalty_bps = 5.0

    blind = pre_flight_cost_gate(
        config=cfg,
        broker="ibkr",
        symbol="ZZZ",
        asset_class="equity",
        quantity=100.0,
        signal_metadata={"forecast_expected_return": 0.01},
    )
    assert blind.cost_breakdown["unknown_liquidity_penalty_bps"] == pytest.approx(5.0)
    assert blind.metadata["wave9_liquidity_known"] is False

    sig = SimpleNamespace(metadata={"forecast_expected_return": 0.01})
    enrich_signal_liquidity(sig, _df())

    informed = pre_flight_cost_gate(
        config=cfg,
        broker="ibkr",
        symbol="ZZZ",
        asset_class="equity",
        quantity=100.0,
        signal_metadata=sig.metadata,
    )
    assert informed.cost_breakdown["unknown_liquidity_penalty_bps"] == 0.0
    assert informed.metadata["wave9_liquidity_known"] is True
    assert informed.expected_cost_bps <= blind.expected_cost_bps
