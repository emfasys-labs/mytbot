"""Futures mark-price normalization — prevents 100x / multiplier scale bugs."""

from __future__ import annotations

from decimal import Decimal

from core.instruments import normalize_futures_mark_price, pick_mark_quotes


def test_normalize_cl_futures_rejects_100x_cfd_scale() -> None:
    avg = Decimal("76.14")
    raw = Decimal("7661.947")  # ~100x the true mark
    px = normalize_futures_mark_price("CL=F", raw, avg_entry_price=avg)
    assert abs(px - avg) < Decimal("5")


def test_normalize_cl_futures_keeps_sane_per_unit_price() -> None:
    avg = Decimal("76.14")
    raw = Decimal("76.55")
    px = normalize_futures_mark_price("CL=F", raw, avg_entry_price=avg)
    assert px == raw


def test_pick_mark_quotes_rejects_outlier_median() -> None:
    avg = Decimal("76.14")
    chosen = pick_mark_quotes(
        [Decimal("76.10"), Decimal("7661.947")],
        ref_price=avg,
    )
    assert chosen == Decimal("76.10")


def test_median_of_mismatched_scales_was_the_bug() -> None:
    avg = Decimal("76.14")
    bad_median = (Decimal("76.14") + Decimal("7661.947")) / Decimal("2")
    assert bad_median > Decimal("3800")
    chosen = pick_mark_quotes(
        [Decimal("76.14"), Decimal("7661.947")],
        ref_price=avg,
    )
    assert chosen == Decimal("76.14")
