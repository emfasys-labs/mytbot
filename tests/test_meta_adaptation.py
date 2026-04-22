from signals.meta_adaptation import bias_from_outcomes


def test_bias_from_outcomes_positive_and_negative() -> None:
    rows = []
    rows.extend([("momentum_breakout", "filled")] * 30)
    rows.extend([("momentum_breakout", "cancelled")] * 10)
    rows.extend([("mean_reversion", "filled")] * 10)
    rows.extend([("mean_reversion", "rejected")] * 30)
    out = bias_from_outcomes(rows, min_samples=20, max_abs_delta=0.12)
    assert "momentum_breakout" in out
    assert "mean_reversion" in out
    assert out["momentum_breakout"].bias_delta > 0
    assert out["mean_reversion"].bias_delta < 0
