from __future__ import annotations


def select_representatives(
    clusters: list[list[int]],
    symbols: list[str],
    scores: dict[str, float],
) -> dict[int, str]:
    """
    One representative per cluster: highest liquidity / score wins.
    Keys are cluster indices (0..len(clusters)-1).
    """
    out: dict[int, str] = {}
    for ci, idxs in enumerate(clusters):
        best_sym = ""
        best_sc = float("-inf")
        for idx in idxs:
            if 0 <= idx < len(symbols):
                sym = symbols[idx]
                sc = float(scores.get(sym, 0.0))
                if sc > best_sc:
                    best_sc = sc
                    best_sym = sym
        if best_sym:
            out[ci] = best_sym
    return out


def cluster_avg_abs_correlation(
    cluster_idxs: list[int],
    corr: list[list[float]],
) -> float:
    if len(cluster_idxs) < 2:
        return 0.0
    vals: list[float] = []
    for i in range(len(cluster_idxs)):
        for j in range(i + 1, len(cluster_idxs)):
            a, b = cluster_idxs[i], cluster_idxs[j]
            vals.append(abs(corr[a][b]))
    return sum(vals) / len(vals) if vals else 0.0
