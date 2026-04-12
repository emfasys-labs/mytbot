"""
NewsAPI.org everything endpoint + content hashing for deduplication.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import urlparse

from data.http_retry import httpx_get_with_retry


def headline_content_hash(*, url: str, title: str) -> str:
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


def fetch_everything(
    api_key: str,
    *,
    q: str,
    language: str = "en",
    page_size: int = 100,
    timeout_sec: float = 30.0,
) -> list[NormalizedArticle]:
    url = "https://newsapi.org/v2/everything"
    params = {
        "q": q,
        "language": language,
        "pageSize": min(page_size, 100),
        "sortBy": "publishedAt",
        "apiKey": api_key,
    }
    out: list[NormalizedArticle] = []
    r = httpx_get_with_retry(url, params=params, timeout_sec=timeout_sec)
    r.raise_for_status()
    payload = r.json()
    if payload.get("status") != "ok":
        raise RuntimeError(f"NewsAPI error: {payload}")
    for art in payload.get("articles") or []:
        u = (art.get("url") or "").strip()
        t = (art.get("title") or "").strip()
        if not u or not t:
            continue
        pub = art.get("publishedAt")
        if not pub:
            continue
        ts = datetime.fromisoformat(pub.replace("Z", "+00:00"))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        src = (art.get("source") or {}) if isinstance(art.get("source"), dict) else {}
        name = str(src.get("name") or "unknown")[:120]
        desc = art.get("description")
        desc_s = str(desc).strip() if desc else None
        h = headline_content_hash(url=u, title=t)
        out.append(
            NormalizedArticle(
                content_hash=h,
                url=u,
                title=t,
                description=desc_s,
                source_name=name,
                published_at=ts,
            )
        )
    return out
