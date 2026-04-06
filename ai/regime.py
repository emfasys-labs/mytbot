from __future__ import annotations

from typing import TypeVar

T = TypeVar("T")


def filter_by_allowed_strategies(candidates: list[T], allowed_strategy_names: set[str] | None) -> list[T]:
    if not allowed_strategy_names:
        return candidates
    out: list[T] = []
    for candidate in candidates:
        strategy = getattr(candidate, "strategy", None)
        if strategy in allowed_strategy_names:
            out.append(candidate)
    return out
