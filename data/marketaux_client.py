"""
Marketaux news endpoint client + normalization.
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


def fetch_all_news(
    api_token: str,
    *,
    language: str = "en",
    limit: int = 100,
    must_have_entities: bool = True,
    timeout_sec: float = 30.0,
) -> list[NormalizedArticle]:
    url = "https://api.marketaux.com/v1/news/all"
    params = {
        "api_token": api_token,
        "language": language,
        "limit": max(1, min(int(limit), 100)),
        "must_have_entities": "true" if must_have_entities else "false",
    }
    r = httpx_get_with_retry(url, params=params, timeout_sec=timeout_sec)
    r.raise_for_status()
    payload = r.json()
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        raise RuntimeError(f"Marketaux error payload: {payload}")

    out: list[NormalizedArticle] = []
    for art in payload.get("data") or []:
        u = str(art.get("url") or "").strip()
        t = str(art.get("title") or "").strip()
        if not u or not t:
            continue
        pub_raw = str(art.get("published_at") or "").strip()
        if not pub_raw:
            continue
        try:
            ts = datetime.fromisoformat(pub_raw.replace("Z", "+00:00"))
        except ValueError:
            continue
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        src = str(art.get("source") or "marketaux").strip()[:120] or "marketaux"
        desc_raw = art.get("description")
        desc = str(desc_raw).strip() if desc_raw else None
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

