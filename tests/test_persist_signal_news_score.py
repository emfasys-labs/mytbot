"""
tests/test_persist_signal_news_score.py
========================================

Lock in the fix for ``SignalLog.news_score`` being NULL even when the AI
pipeline has scored the symbol. The persister now falls back to metadata
when the dataclass field is missing (the D015 ``risk.engine.Signal`` path)
or unset.
"""

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

from run_m3 import _resolve_signal_news_score


def _sig(**kw):
    base = {
        "signal_id": "s1",
        "symbol": "EURUSD",
        "side": "buy",
        "strategy": "x",
        "confidence": 0.5,
        "broker": "ibkr",
        "asset_class": "forex",
        "metadata": {},
    }
    base.update(kw)
    return SimpleNamespace(**base)


def test_uses_direct_news_score_attribute_when_set() -> None:
    out = _resolve_signal_news_score(_sig(news_score=0.5))
    assert out == Decimal("0.5")


def test_falls_back_to_metadata_news_score() -> None:
    out = _resolve_signal_news_score(_sig(metadata={"news_score": 0.42}))
    assert out == Decimal("0.42")


def test_falls_back_to_accumulator_score() -> None:
    out = _resolve_signal_news_score(
        _sig(metadata={"accumulator_score": "0.6012490127359147"})
    )
    assert out == Decimal("0.6012490127359147")


def test_falls_back_to_ai_news_score_when_only_ai_present() -> None:
    out = _resolve_signal_news_score(_sig(metadata={"ai_news_score": 0.8852}))
    assert out == Decimal("0.8852")


def test_direct_attribute_wins_over_metadata() -> None:
    out = _resolve_signal_news_score(
        _sig(news_score=0.9, metadata={"accumulator_score": 0.1})
    )
    assert out == Decimal("0.9")


def test_risk_engine_signal_without_news_score_field_uses_metadata() -> None:
    # ``risk.engine.Signal`` has no news_score / news_veto field; emulate that.
    sig = _sig(metadata={"accumulator_score": -0.3})
    # No news_score attribute at all
    out = _resolve_signal_news_score(sig)
    assert out == Decimal("-0.3")


def test_zero_and_negative_values_preserved() -> None:
    assert _resolve_signal_news_score(_sig(news_score=0.0)) == Decimal("0")
    assert _resolve_signal_news_score(_sig(news_score=-0.42)) == Decimal("-0.42")


def test_none_everywhere_returns_none() -> None:
    out = _resolve_signal_news_score(_sig())
    assert out is None


def test_bad_metadata_values_ignored_not_crashed() -> None:
    out = _resolve_signal_news_score(
        _sig(metadata={"accumulator_score": "not_a_number", "ai_news_score": 0.25})
    )
    assert out == Decimal("0.25")
