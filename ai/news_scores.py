"""Resolve AI pipeline news scores for signal processing."""

from __future__ import annotations

from typing import Any


def news_score_for_symbol(ai_result: Any | None, symbol: str) -> float | None:
    """Return the AI pipeline's per-symbol news score, or None if absent."""
    if ai_result is None:
        return None
    sym = (symbol or "").strip().upper()
    if not sym:
        return None
    scores = getattr(ai_result, "news_scores", None) or {}
    if sym not in scores:
        return None
    val = scores.get(sym)
    if val is None:
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None
