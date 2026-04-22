"""
Alpha Vantage NEWS_SENTIMENT endpoint client + normalization.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import urlparse

from data.http_retry import httpx_get_with_retry


def _content_hash(*, url: str, title: str) -> str:
    norm_url = urlparse(url.strip().lower().rstrip("/"))._replace(fragment="").geturl()
    raw = f"{norm_url}\n{title.strip().lower()}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


@dataclass
class NormalizedArticle:
    content_hash: str
    url: str
    title: str
    description: str | None
    source_name: str
    published_at: datetime


def fetch_news_sentiment(
    api_key: str,
    *,
    limit: int = 100,
    sort: str = "LATEST",
    timeout_sec: float = 30.0,
) -> list[NormalizedArticle]:
    url = "https://www.alphavantage.co/query"
    params = {
        "function": "NEWS_SENTIMENT",
        "apikey": api_key,
        "sort": sort,
        "limit": max(1, min(int(limit), 1000)),
    }
    r = httpx_get_with_retry(url, params=params, timeout_sec=timeout_sec)
    r.raise_for_status()
    payload = r.json()

    # Alpha Vantage error/limit payloads commonly use these keys.
    if isinstance(payload, dict):
        if payload.get("Error Message"):
            raise RuntimeError(f"Alpha Vantage error: {payload.get('Error Message')}")
        if payload.get("Information"):
            raise RuntimeError(f"Alpha Vantage info: {payload.get('Information')}")
        if payload.get("Note"):
            raise RuntimeError(f"Alpha Vantage note: {payload.get('Note')}")

    out: list[NormalizedArticle] = []
    for art in payload.get("feed") or []:
        u = (art.get("url") or "").strip()
        t = (art.get("title") or "").strip()
        if not u or not t:
            continue
        ts_raw = (art.get("time_published") or "").strip()
        if not ts_raw:
            continue
        # Format: YYYYMMDDTHHMMSS in UTC.
        try:
            ts = datetime.strptime(ts_raw, "%Y%m%dT%H%M%S").replace(tzinfo=timezone.utc)
        except ValueError:
            continue

        src = (art.get("source") or "").strip()
        if not src:
            src = "AlphaVantage"
        summary = art.get("summary")
        desc = str(summary).strip() if summary else None
        h = _content_hash(url=u, title=t)
        out.append(
            NormalizedArticle(
                content_hash=h,
                url=u,
                title=t,
                description=desc,
                source_name=str(src)[:120],
                published_at=ts,
            )
        )
    return out

