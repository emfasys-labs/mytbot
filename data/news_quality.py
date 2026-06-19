"""Quality filters for raw news before scoring or dashboard display."""

from __future__ import annotations

import re
from typing import Any

# Explicit SEC form identifiers — strict patterns so these don't false-match
# inside unrelated text. These catch filings that the institutional/holdings
# heuristic below misses (e.g. "CFO files Form 4" with no holding-term).
_FILING_FORM_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\b13[DFGdfg]\b"),
    re.compile(r"\bschedule\s+13[DGdg]\b", re.IGNORECASE),
    re.compile(r"\bform\s+4\b", re.IGNORECASE),
)


_INSTITUTIONAL_HOLDING_TERMS = (
    "teacher retirement system",
    "retirement system",
    "wealth partners",
    "asset management",
    "capital management",
    "investment management",
    "securities inc",
    "bank corp",
)

_PASSIVE_FILING_TERMS = (
    "stock position",
    "stake",
    "holdings",
    "shares bought",
    "shares acquired",
    "shares sold",
    "acquires shares",
    "buys shares",
    "sells shares",
    "purchases shares",
    "cuts stake",
    "lowers stake",
    "raises stake",
    "boosts position",
    "raises position",
    "decreases stock position",
    "reduces stock position",
)


def is_low_signal_institutional_filing(
    title: str | None,
    description: str | None = None,
    *,
    source: str | None = None,
    url: str | None = None,
) -> bool:
    """Return True for routine holdings/filing articles that should not drive live signals."""
    title_s = str(title or "")
    desc_s = str(description or "")
    source_s = str(source or "")
    url_s = str(url or "")
    haystack = " ".join([title_s, desc_s, source_s, url_s]).lower()
    if "marketbeat.com/instant-alerts/filing" in haystack:
        return True
    # Explicit SEC form identifiers anywhere in the headline → filing noise.
    for pat in _FILING_FORM_PATTERNS:
        if pat.search(haystack):
            return True
    if "13f" in haystack and any(term in haystack for term in _PASSIVE_FILING_TERMS):
        return True
    return any(inst in haystack for inst in _INSTITUTIONAL_HOLDING_TERMS) and any(
        term in haystack for term in _PASSIVE_FILING_TERMS
    )


def is_displayable_news_item(row: Any) -> bool:
    return not is_low_signal_institutional_filing(
        getattr(row, "title", None),
        getattr(row, "description", None),
        source=getattr(row, "source_name", None),
        url=getattr(row, "url", None),
    )


_ANALYST_ROUNDUP_RE = re.compile(
    r"analyst research calls|research calls:|wall street analyst|best .{0,40} analyst",
    re.IGNORECASE,
)


def is_analyst_research_roundup(
    title: str | None,
    description: str | None = None,
) -> bool:
    """Multi-name analyst roundups are not macro backdrop and must not blanket every symbol."""
    hay = f"{title or ''} {description or ''}".strip()
    return bool(_ANALYST_ROUNDUP_RE.search(hay))


# Publisher-name fragments (``NewsHeadline.source_name``), not ingest-provider ids.
DEFAULT_TIER1_SOURCE_FRAGMENTS: tuple[str, ...] = (
    "reuters",
    "bloomberg",
    "cnbc",
    "wall street journal",
    "wsj",
    "financial times",
    "ft.com",
    "associated press",
    "bbc",
    "dow jones",
    "marketwatch",
    "barrons",
)

DEFAULT_TIER3_SOURCE_FRAGMENTS: tuple[str, ...] = (
    "tradingkey",
    "stock titan",
    "ad hoc news",
    "marketbeat",
    "gurufocus",
    "intellectia",
    "tipranks",
    "proactive financial",
    "streetinsider",
    "seekingalpha",
    "seeking alpha",
    "benzinga",
    "globe newswire",
    "pr newswire",
    "business wire",
    "ad hoc",
)


def normalize_news_source_name(source: str | None) -> str:
    return (source or "unknown").strip().lower()


def news_source_tier(
    source: str | None,
    *,
    tier1_fragments: tuple[str, ...] | None = None,
    tier3_fragments: tuple[str, ...] | None = None,
) -> int:
    """Return 1 (premium wire), 2 (default), or 3 (aggregator/noisy)."""
    name = normalize_news_source_name(source)
    for frag in tier1_fragments or DEFAULT_TIER1_SOURCE_FRAGMENTS:
        if frag in name:
            return 1
    for frag in tier3_fragments or DEFAULT_TIER3_SOURCE_FRAGMENTS:
        if frag in name:
            return 3
    return 2


def _row_published_ts(row: Any) -> float:
    pub = getattr(row, "published_at", None)
    if pub is None:
        return 0.0
    if getattr(pub, "tzinfo", None) is None:
        from datetime import timezone

        pub = pub.replace(tzinfo=timezone.utc)
    return float(pub.timestamp())


def select_news_rows_for_scoring(
    rows: list[Any],
    *,
    max_items: int,
    tier1_min_items: int = 20,
    tier3_max_per_source: int = 2,
    tier1_fragments: tuple[str, ...] | None = None,
    tier3_fragments: tuple[str, ...] | None = None,
) -> list[Any]:
    """
    Pick headlines for the AI scoring budget.

    Prefer tier-1 publishers first, then default sources, and cap noisy
    aggregators so they cannot dominate the per-cycle budget.
    """
    if max_items <= 0 or not rows:
        return []

    t1 = tier1_fragments or DEFAULT_TIER1_SOURCE_FRAGMENTS
    t3 = tier3_fragments or DEFAULT_TIER3_SOURCE_FRAGMENTS
    buckets: dict[int, dict[str, list[Any]]] = {1: {}, 2: {}, 3: {}}
    for row in rows:
        tier = news_source_tier(
            getattr(row, "source_name", None),
            tier1_fragments=t1,
            tier3_fragments=t3,
        )
        src = normalize_news_source_name(getattr(row, "source_name", None))
        buckets[tier].setdefault(src, []).append(row)
    for tier_map in buckets.values():
        for src in tier_map:
            tier_map[src].sort(key=_row_published_ts, reverse=True)

    selected: list[Any] = []
    tier3_counts: dict[str, int] = {}

    def _round_robin_pick(
        tier_map: dict[str, list[Any]],
        *,
        cap: int,
        per_source_cap: int | None = None,
    ) -> None:
        keys = sorted(src for src, bucket in tier_map.items() if bucket)
        picked = 0
        while keys and len(selected) < max_items and picked < cap:
            next_keys: list[str] = []
            for src in keys:
                if per_source_cap is not None and tier3_counts.get(src, 0) >= per_source_cap:
                    continue
                bucket = tier_map.get(src) or []
                if not bucket:
                    continue
                selected.append(bucket.pop(0))
                picked += 1
                if per_source_cap is not None:
                    tier3_counts[src] = tier3_counts.get(src, 0) + 1
                if bucket:
                    next_keys.append(src)
                if len(selected) >= max_items or picked >= cap:
                    break
            keys = next_keys

    tier1_target = min(max(0, tier1_min_items), max_items)
    _round_robin_pick(buckets[1], cap=tier1_target)
    if len(selected) < max_items:
        _round_robin_pick(buckets[2], cap=max_items - len(selected))
    if len(selected) < max_items:
        _round_robin_pick(
            buckets[3],
            cap=max_items - len(selected),
            per_source_cap=max(1, tier3_max_per_source),
        )
    return selected[:max_items]
