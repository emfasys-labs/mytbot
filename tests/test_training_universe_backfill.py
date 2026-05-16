from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest

import data.persist as persist
from data.training_universe import load_training_universe_symbols, normalize_training_symbol
from data.universe_tiers import UniverseTiers, save_universe_tiers


def test_load_training_universe_symbols_uses_requested_tiers(tmp_path) -> None:
    p = tmp_path / "tiers.json"
    save_universe_tiers(
        UniverseTiers(
            core=("SPY", "QQQ"),
            scan=("BTC-USD", "SPY"),
            light=("EURUSD=X",),
            scores={},
            updated_at="2026-05-16T00:00:00+00:00",
        ),
        path=p,
    )

    assert load_training_universe_symbols(tiers_path=p, scope="core,scan") == [
        "SPY",
        "QQQ",
        "BTC-USD",
    ]
    assert load_training_universe_symbols(tiers_path=p, scope="light") == ["EURUSD=X"]
    assert load_training_universe_symbols(tiers_path=p, scope="all", max_symbols=2) == [
        "SPY",
        "QQQ",
    ]


def test_load_training_universe_symbols_falls_back_to_static_universe(tmp_path) -> None:
    symbols = load_training_universe_symbols(tiers_path=tmp_path / "missing.json", max_symbols=5)
    assert symbols[:3] == ["SPY", "QQQ", "IWM"]
    assert len(symbols) == 5


def test_normalize_training_symbol_handles_residual_broker_shapes() -> None:
    assert normalize_training_symbol("RENDER/USD") == "RENDER-USD"
    assert normalize_training_symbol("PEOPLEUSDT") == "PEOPLE-USD"
    assert normalize_training_symbol("XRPUSDC") == "XRP-USD"
    assert normalize_training_symbol("VIX") == "^VIX"
    assert normalize_training_symbol("DXY") == "DX-Y.NYB"


@pytest.mark.asyncio
async def test_feature_snapshot_upsert_chunks_large_backfills(monkeypatch) -> None:
    class _Session:
        def __init__(self) -> None:
            self.calls = 0

        async def execute(self, _stmt) -> None:
            self.calls += 1

    rows = []
    for i in range(persist.FEATURE_SNAPSHOT_UPSERT_CHUNK_SIZE + 1):
        rows.append(
            {
                "symbol": "BTC-USD",
                "timeframe": "1h",
                "bar_timestamp": datetime(2026, 1, 1, tzinfo=timezone.utc),
                "open": Decimal("1"),
                "high": Decimal("1"),
                "low": Decimal("1"),
                "close": Decimal("1"),
                "volume": Decimal("0"),
                "features": {},
                "validation": None,
                "data_source": "yfinance",
            }
        )

    session = _Session()
    count = await persist.upsert_feature_snapshots(session, rows)  # type: ignore[arg-type]
    assert count == persist.FEATURE_SNAPSHOT_UPSERT_CHUNK_SIZE + 1
    assert session.calls == 2
