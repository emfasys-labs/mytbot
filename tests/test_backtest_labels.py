import pandas as pd

from backtest.labels import TripleBarrierSpec, triple_barrier_labels


def test_triple_barrier_labels_emit_values():
    idx = pd.date_range("2024-01-01", periods=80, freq="D", tz="UTC")
    close = pd.Series([100 + (i % 10) * 0.5 for i in range(80)], index=idx)
    labels = triple_barrier_labels(
        close,
        TripleBarrierSpec(pt_mult=1.5, sl_mult=1.0, max_horizon=5, vol_window=10),
    )
    assert len(labels) == len(close)
    assert set(labels.dropna().unique()).issubset({-1, 0, 1})

