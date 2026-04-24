from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

import pandas as pd

from ai.regime import filter_by_allowed_strategies
from run_m3 import (
    _pick_best_signal,
    _resolve_portfolio_value_for_state,
    _rows_to_features_frame,
    main,
)
from signals.engine import RawSignal


def test_rows_to_features_frame_merges_features_payload():
    rows = [
        SimpleNamespace(
            bar_timestamp=datetime(2024, 1, 1, tzinfo=timezone.utc),
            open=Decimal("100"),
            high=Decimal("101"),
            low=Decimal("99"),
            close=Decimal("100.5"),
            volume=Decimal("1000"),
            features={"rsi_14": 55.0},
        ),
        SimpleNamespace(
            bar_timestamp=datetime(2024, 1, 2, tzinfo=timezone.utc),
            open=Decimal("101"),
            high=Decimal("102"),
            low=Decimal("100"),
            close=Decimal("101.5"),
            volume=Decimal("1100"),
            features={"rsi_14": 56.0},
        ),
    ]
    df = _rows_to_features_frame(rows)
    assert isinstance(df, pd.DataFrame)
    assert "rsi_14" in df.columns
    assert float(df.iloc[-1]["close"]) == 101.5


def test_pick_best_signal_returns_highest_confidence():
    a = RawSignal("s1", "SPY", "buy", 0.61, "ibkr", "equity", {})
    b = RawSignal("s2", "SPY", "buy", 0.77, "ibkr", "equity", {})
    best = _pick_best_signal([a, b])
    assert best is not None
    assert best.strategy == "s2"


def test_pick_best_signal_none_for_empty():
    assert _pick_best_signal([]) is None


def test_filter_by_regime_keeps_only_allowed():
    a = RawSignal("momentum_breakout", "SPY", "buy", 0.6, "ibkr", "equity", {})
    b = RawSignal("mean_reversion", "SPY", "buy", 0.6, "ibkr", "equity", {})
    out = filter_by_allowed_strategies([a, b], {"mean_reversion"})
    assert len(out) == 1
    assert out[0].strategy == "mean_reversion"


def test_main_parser_accepts_ai_config(monkeypatch):
    async def _fake_run_once(_args):  # noqa: ANN001
        return 0

    def _fake_asyncio_run(coro):  # noqa: ANN001
        coro.close()
        return 0

    monkeypatch.setattr("sys.argv", ["run_m3.py", "--ai-config", "config/ai.yaml"])
    monkeypatch.setattr("run_m3._run_once", _fake_run_once)
    monkeypatch.setattr("run_m3.asyncio.run", _fake_asyncio_run)
    try:
        main()
    except SystemExit as exc:
        assert exc.code == 0


def test_resolve_portfolio_value_live_wins_over_stale_db() -> None:
    """D031: post-allowlist live (~98K) must not max() with stale daily_pnl (~884K)."""
    assert _resolve_portfolio_value_for_state(Decimal("98000"), Decimal("884000")) == Decimal("98000")


def test_resolve_portfolio_value_falls_back_to_db_when_live_zero() -> None:
    assert _resolve_portfolio_value_for_state(Decimal("0"), Decimal("884000")) == Decimal("884000")


def test_resolve_portfolio_value_both_zero() -> None:
    assert _resolve_portfolio_value_for_state(Decimal("0"), Decimal("0")) == Decimal("0")


def test_resolve_portfolio_value_live_positive_db_zero() -> None:
    assert _resolve_portfolio_value_for_state(Decimal("1055000"), Decimal("0")) == Decimal("1055000")

