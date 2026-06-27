from __future__ import annotations

from unittest.mock import AsyncMock

import pandas as pd
import pytest

from data.pipeline import ingest_symbol_yfinance, run_once
from strategies.history_requirements import enabled_strategy_history_bars


def test_enabled_strategy_history_bars_uses_longest_enabled_dependency() -> None:
    cfg = {
        "strategies": {
            "short": {"enabled": True, "lookback_periods": 30},
            "trend": {"enabled": True, "slow_period": 200},
            "disabled": {"enabled": False, "entry_lookback": 500},
        }
    }

    assert enabled_strategy_history_bars(cfg) == 201


@pytest.mark.asyncio
async def test_run_once_backfills_only_requested_warmup_symbols(monkeypatch) -> None:
    ingest = AsyncMock(
        side_effect=lambda _sf, _cfg, symbol, *, backfill: {
            "symbol": symbol,
            "timeframe": "1d",
            "upserted": 1,
            "bars_total": 1,
            "rows_with_full_features": 1,
        }
    )
    monkeypatch.setattr("data.pipeline.ingest_symbol_yfinance", ingest)

    cfg = {
        "symbols": ["SPY", "NEW"],
        "backfill": {"interval": "1d", "period": "2y"},
        "incremental": {"interval": "1d", "period": "1mo"},
        "news": {"enabled": False},
        "fred": {"enabled": False},
    }
    await run_once(
        AsyncMock(),
        cfg,
        backfill=False,
        backfill_symbols={"NEW"},
        include_news=False,
        include_fred=False,
    )

    assert ingest.await_args_list[0].kwargs["backfill"] is False
    assert ingest.await_args_list[1].kwargs["backfill"] is True


@pytest.mark.asyncio
async def test_ingest_drops_provisional_rows_with_missing_ohlc(monkeypatch) -> None:
    idx = pd.date_range("2026-06-25", periods=2, freq="D", tz="UTC")
    frame = pd.DataFrame(
        {
            "Open": [100.0, float("nan")],
            "High": [101.0, float("nan")],
            "Low": [99.0, float("nan")],
            "Close": [100.5, float("nan")],
            "Volume": [10.0, 20.0],
        },
        index=idx,
    )
    monkeypatch.setattr("data.pipeline.fetch_history", lambda *args, **kwargs: frame)
    monkeypatch.setattr("data.pipeline.upsert_feature_snapshots", AsyncMock(return_value=1))

    class _Session:
        async def commit(self) -> None:
            return None

    class _Context:
        async def __aenter__(self):
            return _Session()

        async def __aexit__(self, *_args):
            return None

    class _Factory:
        def __call__(self):
            return _Context()

    cfg = {
        "data_source": "yfinance",
        "backfill": {
            "interval": "1d",
            "period": "2y",
            "expected_interval_seconds": 86400,
            "stale_after_days": 30,
        },
        "incremental": {
            "interval": "1d",
            "period": "1mo",
            "expected_interval_seconds": 86400,
            "stale_after_hours": 72,
        },
        "validation": {"max_gap_multiplier": 7.0},
    }

    result = await ingest_symbol_yfinance(_Factory(), cfg, "SPY", backfill=False)

    assert result["bars_total"] == 1
