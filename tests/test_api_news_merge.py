"""Tests for dashboard AI news row merge (strongest |score| per symbol)."""

from datetime import datetime, timezone
from decimal import Decimal

from api.server import _pick_strongest_news_log_per_symbol


class _FakeRow:
    def __init__(self, symbol: str, score: float | None, rationale: str = ""):
        self.symbol = symbol
        self.score = Decimal(str(score)) if score is not None else None
        self.rationale = rationale
        self.event_type = "other"
        self.timestamp = datetime(2026, 4, 1, tzinfo=timezone.utc)


def test_merge_keeps_stronger_abs_score_not_latest():
    rows = [
        _FakeRow("SPY", 0.0, "latest flat"),
        _FakeRow("SPY", 0.45, "older mover"),
        _FakeRow("QQQ", -0.3, "qqq"),
    ]
    best = _pick_strongest_news_log_per_symbol(rows)
    assert len(best) == 2
    assert float(best["SPY"].score) == 0.45
    assert float(best["QQQ"].score) == -0.3


def test_merge_prefers_negative_if_larger_abs():
    rows = [
        _FakeRow("X", 0.05),
        _FakeRow("X", -0.4),
    ]
    best = _pick_strongest_news_log_per_symbol(rows)
    assert float(best["X"].score) == -0.4
