from __future__ import annotations

import math
from typing import Sequence


def pearson_correlation(a: Sequence[float], b: Sequence[float]) -> float:
    """Pearson r; returns 0.0 on degenerate input (no numpy)."""
    n = len(a)
    if n != len(b) or n < 3:
        return 0.0
    mx = sum(a) / n
    my = sum(b) / n
    num = sum((float(xi) - mx) * (float(yi) - my) for xi, yi in zip(a, b))
    denx = math.sqrt(sum((float(xi) - mx) ** 2 for xi in a))
    deny = math.sqrt(sum((float(yi) - my) ** 2 for yi in b))
    if denx <= 0 or deny <= 0:
        return 0.0
    r = num / (denx * deny)
    if math.isnan(r):
        return 0.0
    return max(-1.0, min(1.0, r))


def log_returns(closes: Sequence[float]) -> list[float]:
    out: list[float] = []
    for i in range(1, len(closes)):
        c0, c1 = float(closes[i - 1]), float(closes[i])
        if c0 <= 0 or c1 <= 0:
            out.append(0.0)
        else:
            out.append(math.log(c1 / c0))
    return out


def correlation_matrix(
    symbols: list[str],
    price_series: dict[str, list[float]],
    *,
    min_overlap: int = 10,
) -> tuple[list[list[float]], list[str]]:
    """
    Pairwise Pearson correlation on log-returns. Symbols with too-short series skipped.
    Returns (matrix, ordered_symbols_used).
    """
    usable: list[str] = []
    rets: dict[str, list[float]] = {}
    for s in symbols:
        px = price_series.get(s) or []
        if len(px) < min_overlap + 1:
            continue
        lr = log_returns(px)
        if len(lr) < min_overlap:
            continue
        usable.append(s)
        rets[s] = lr

    n = len(usable)
    mat = [[1.0 if i == j else 0.0 for j in range(n)] for i in range(n)]
    for i in range(n):
        for j in range(i + 1, n):
            ri, rj = rets[usable[i]], rets[usable[j]]
            m = min(len(ri), len(rj))
            if m < min_overlap:
                continue
            c = pearson_correlation(ri[-m:], rj[-m:])
            mat[i][j] = c
            mat[j][i] = c
    return mat, usable


def distance_from_correlation(r: float) -> float:
    """Convert correlation to distance in [0, 2]."""
    return max(0.0, min(2.0, math.sqrt(max(0.0, 2.0 * (1.0 - r)))))
