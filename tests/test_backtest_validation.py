from backtest.validation import (
    combinatorial_purged_splits,
    deflated_sharpe_ratio,
    pbo_from_path_scores,
    purged_time_series_splits,
)


def test_purged_splits_have_non_overlapping_ranges():
    splits = purged_time_series_splits(120, n_splits=6, embargo_bars=3)
    assert splits
    for train, test in splits:
        tr0, tr1 = train
        te0, te1 = test
        assert tr0 < tr1
        assert te0 < te1
        assert tr1 <= te0 or tr0 >= te1


def test_dsr_and_pbo_in_range():
    dsr = deflated_sharpe_ratio(1.1, n_trials=10, n_obs=120)
    pbo = pbo_from_path_scores([1.0, -0.2, 0.5, -0.1])
    assert 0.0 <= dsr <= 1.0
    assert 0.0 <= pbo <= 1.0


def test_combinatorial_splits_fallback_or_real():
    folds = combinatorial_purged_splits(
        120,
        n_splits=6,
        n_test_splits=2,
        embargo_bars=3,
    )
    assert folds
    train, test = folds[0]
    assert train
    assert test

