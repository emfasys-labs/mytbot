"""
Finnhub news endpoint client + normalization.
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


def fetch_general_news(
    api_key: str,
    *,
    category: str = "general",
    timeout_sec: float = 30.0,
    limit: int = 100,
) -> list[NormalizedArticle]:
    url = "https://finnhub.io/api/v1/news"
    params = {"category": category, "token": api_key}
    r = httpx_get_with_retry(url, params=params, timeout_sec=timeout_sec)
    r.raise_for_status()
    payload = r.json()
    if not isinstance(payload, list):
        raise RuntimeError(f"Finnhub error payload: {payload}")

    out: list[NormalizedArticle] = []
    for art in payload[: max(1, min(int(limit), 1000))]:
        u = str(art.get("url") or "").strip()
        t = str(art.get("headline") or "").strip()
        if not u or not t:
            continue
        ts_raw = art.get("datetime")
        try:
            ts = datetime.fromtimestamp(int(ts_raw), tz=timezone.utc)
        except Exception:  # noqa: BLE001
            continue
        src = str(art.get("source") or "Finnhub").strip()[:120] or "Finnhub"
        summary = art.get("summary")
        desc = str(summary).strip() if summary else None
        h = _content_hash(url=u, title=t)
        out.append(
            NormalizedArticle(
                content_hash=h,
                url=u,
                title=t,
                description=desc,
                source_name=src,
                published_at=ts,
            )
        )
    return out

