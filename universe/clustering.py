from __future__ import annotations


def cluster_by_correlation(
    symbols: list[str],
    corr: list[list[float]],
    *,
    threshold: float = 0.88,
) -> list[list[int]]:
    """
    Greedy single-link style clustering: merge indices when |r| >= threshold.
    Returns clusters as lists of indices into ``symbols`` (same order as corr rows).
    """
    n = len(symbols)
    if n == 0 or len(corr) != n or any(len(row) != n for row in corr):
        return [[i] for i in range(n)]

    parent = list(range(n))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    for i in range(n):
        for j in range(i + 1, n):
            if abs(corr[i][j]) >= threshold:
                union(i, j)

    buckets: dict[int, list[int]] = {}
    for i in range(n):
        r = find(i)
        buckets.setdefault(r, []).append(i)
    return list(buckets.values())
