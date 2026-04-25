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
