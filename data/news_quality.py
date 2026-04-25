"""Quality filters for raw news before scoring or dashboard display."""

from __future__ import annotations

from typing import Any


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
