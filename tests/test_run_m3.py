from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

import pandas as pd

from run_m3 import _pick_best_signal, _rows_to_features_frame
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

