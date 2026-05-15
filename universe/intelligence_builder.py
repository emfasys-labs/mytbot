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


def _normalised_convictions(scores: dict[str, float]) -> dict[str, float]:
    if not scores:
        return {}
    vals = [float(v) for v in scores.values()]
    lo = min(vals)
    hi = max(vals)
    if hi <= lo:
        return {str(k).upper(): 50.0 for k in scores}
    span = hi - lo
    return {str(k).upper(): ((float(v) - lo) / span) * 100.0 for k, v in scores.items()}


def _derive_promotions(
    tiers: UniverseTiers,
    *,
    cfg: dict,
    cold_scan: list[str],
    generated_at: datetime,
) -> list[dict]:
    """Create honest recent-promotion rows from the scored universe snapshot.

    The tier scorer emits small raw scores, while the operator-facing
    promotion threshold is on a 0-100 conviction scale. Normalize within the
    current build, promote only non-core names above threshold, and cap the
    stream so a daily rebuild cannot flood the UI.
    """
    seed = [dict(p) for p in (cfg.get("seed_promotions") or []) if isinstance(p, dict)]
    rules = dict(cfg.get("promotion") or {})
    try:
        threshold = float(rules.get("conviction_threshold", 65))
    except (TypeError, ValueError):
        threshold = 65.0
    try:
        ttl_min = int(rules.get("promotion_ttl_minutes", 240))
    except (TypeError, ValueError):
        ttl_min = 240
    try:
        max_items = int(rules.get("max_promotions_per_build", 12))
    except (TypeError, ValueError):
        max_items = 12
    max_items = max(0, min(50, max_items))

    scores = {str(k).upper(): float(v) for k, v in tiers.scores.items()}
    convictions = _normalised_convictions(scores)
    core = {str(s).upper() for s in tiers.core}
    promoted_seen = {str(p.get("symbol", "")).upper() for p in seed}
    candidates = list(dict.fromkeys([str(s).upper() for s in list(cold_scan) + list(tiers.scan) + list(tiers.light)]))
    expires_at = datetime.fromtimestamp(
        generated_at.timestamp() + max(1, ttl_min) * 60,
        tz=timezone.utc,
    ).isoformat()

    derived: list[dict] = []
    for sym in candidates:
        if not sym or sym in core or sym in promoted_seen:
            continue
        conviction = float(convictions.get(sym, 0.0))
        if conviction < threshold:
            continue
        derived.append(
            {
                "symbol": sym,
                "reason": "conviction_score",
                "tier_hint": "promoted",
                "score": round(float(scores.get(sym, 0.0)), 6),
                "conviction": round(conviction, 2),
                "promoted_at": generated_at.isoformat(),
                "expires_at": expires_at,
            }
        )
        promoted_seen.add(sym)
        if len(derived) >= max_items:
            break
    return (seed + derived)[:max_items]


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

    generated_at = datetime.now(timezone.utc)
    promotions = _derive_promotions(tiers, cfg=cfg, cold_scan=cold, generated_at=generated_at)

    return UniverseIntelligenceState(
        candidate_count=max(len(tiers.core) + len(tiers.scan) + len(tiers.light), len(ordered)),
        cold_scan=cold,
        active_eval=list(dict.fromkeys(list(tiers.scan) + list(tiers.core))),
        core=sorted(rep_set),
        clusters=clusters,
        promotions=promotions,
        last_full_cluster_at=generated_at.isoformat(),
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
