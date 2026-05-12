from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from loguru import logger

from data.universe_tiers import UniverseTiers
from data.yfinance_fetch import fetch_history
from universe.clustering import cluster_by_correlation
from universe.correlation_graph import correlation_matrix
from universe.persistence import merge_cluster_payload, save_intelligence_state
from universe.representative_selector import select_representatives
from universe.universe_tiers import UniverseIntelligenceState


HistoryFetcher = Callable[[str], list[float]]


@dataclass(frozen=True)
class UniverseIntelligenceBuildResult:
    wrote: bool
    path: Path
    clusters: int
    symbols_scored: int
    reason: str | None = None


def _default_history_fetcher(symbol: str) -> list[float]:
    df = fetch_history(symbol.strip(), period="3mo", interval="1d")
    if df is None or df.empty or "Close" not in df.columns:
        return []
    return [float(x) for x in df["Close"].tolist() if str(x) != "nan"]


async def build_universe_intelligence_state(
    tiers: UniverseTiers,
    *,
    cfg: dict,
    history_fetcher: HistoryFetcher | None = None,
) -> UniverseIntelligenceState | None:
    """
    Build correlation clusters and representative tiers from an existing tier file.

    This is intentionally order-only/persistence-only intelligence: no orders, risk,
    or execution paths are touched.
    """
    cap = int(cfg.get("cluster_max_symbols", 120))
    threshold = float(cfg.get("correlation_cluster_threshold", 0.88))
    min_series = int(cfg.get("min_cluster_price_series", 5))
    concurrency = max(1, min(20, int(cfg.get("cluster_yf_concurrency", 8))))
    fetcher = history_fetcher or _default_history_fetcher
    sem = asyncio.Semaphore(concurrency)

    symbols = list(dict.fromkeys(list(tiers.core) + list(tiers.scan)))[: max(1, cap)]
    scores = dict(tiers.scores)
    price_series: dict[str, list[float]] = {}

    async def fetch_one(sym: str) -> tuple[str, list[float]]:
        try:
            async with sem:
                px = await asyncio.to_thread(fetcher, sym)
            return sym.upper(), list(px or [])
        except Exception as exc:  # noqa: BLE001
            logger.debug("universe_intelligence | skip {}: {}", sym, exc)
            return sym.upper(), []

    if symbols:
        rows = await asyncio.gather(*[fetch_one(s) for s in symbols])
        price_series = {sym: px for sym, px in rows if px}

    if len(price_series) < min_series:
        logger.info(
            "universe_intelligence | too few price series for clustering ({}/{})",
            len(price_series),
            min_series,
        )
        return None

    ordered = list(price_series.keys())
    mat, used = correlation_matrix(ordered, price_series)
    if len(used) < min_series:
        logger.info(
            "universe_intelligence | too few usable return series for clustering ({}/{})",
            len(used),
            min_series,
        )
        return None

    clusters_idx = cluster_by_correlation(used, mat, threshold=threshold)
    reps = select_representatives(clusters_idx, used, scores)
    clusters = merge_cluster_payload(used, mat, clusters_idx, reps)

    rep_set = {v.upper() for v in reps.values()}
    cold = [s for s in ordered if s.upper() not in rep_set]

    return UniverseIntelligenceState(
        candidate_count=max(len(tiers.core) + len(tiers.scan) + len(tiers.light), len(ordered)),
        cold_scan=cold,
        active_eval=list(dict.fromkeys(list(tiers.scan) + list(tiers.core))),
        core=sorted(rep_set),
        clusters=clusters,
        promotions=list(cfg.get("seed_promotions") or []),
        last_full_cluster_at=datetime.now(timezone.utc).isoformat(),
    )


async def build_and_save_universe_intelligence(
    tiers: UniverseTiers,
    *,
    cfg: dict,
    output_path: Path,
    history_fetcher: HistoryFetcher | None = None,
) -> UniverseIntelligenceBuildResult:
    state = await build_universe_intelligence_state(tiers, cfg=cfg, history_fetcher=history_fetcher)
    if state is None:
        return UniverseIntelligenceBuildResult(
            wrote=False,
            path=output_path,
            clusters=0,
            symbols_scored=0,
            reason="too_few_price_series",
        )
    save_intelligence_state(state, output_path)
    return UniverseIntelligenceBuildResult(
        wrote=True,
        path=output_path,
        clusters=len(state.clusters),
        symbols_scored=len(state.active_eval),
        reason=None,
    )
