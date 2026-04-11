from decimal import Decimal

from core.signal_math import bounded_sigmoid, normalize_zscore, tanh_clip


def test_normalize_zscore_midpoint():
    assert normalize_zscore(Decimal("0")) == Decimal("0.5")


def test_tanh_clip_bounded():
    assert Decimal("-1") <= tanh_clip(Decimal("5")) <= Decimal("1")


def test_bounded_sigmoid_mid():
    s = bounded_sigmoid(Decimal("0"))
    assert Decimal("0.4") < s < Decimal("0.6")
