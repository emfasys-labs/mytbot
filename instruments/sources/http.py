"""Shared polite HTTP client for instrument-registry source adapters.

Goals:
- Identifying User-Agent header (``mytbot/instrument-registry``).
- Per-host rate limiting via an in-process asyncio.Semaphore + min-interval.
- ETag/Last-Modified conditional GET (cache stored under ``data/runtime/instrument_registry_cache/``).
- Retry with exponential backoff + jitter on transient failures (5xx / network / 429).
- Per-call timeout and bounded response size.

This module never raises into the trading loop; callers either ``await fetch()``
and handle ``SourceFetchError``, or use the higher-level builder which already
catches per-source.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import random
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Optional

import httpx
from loguru import logger

from instruments.sources.base import SourceFetchError


DEFAULT_USER_AGENT = "mytbot/instrument-registry (+https://github.com/emfasys-labs/mytbot)"
DEFAULT_TIMEOUT_SEC = 30.0
DEFAULT_MAX_BYTES = 50 * 1024 * 1024  # 50 MB cap; iShares CSVs are <2 MB
DEFAULT_RETRIES = 3
DEFAULT_RETRY_BASE_DELAY = 1.5
DEFAULT_MIN_INTERVAL_PER_HOST = 0.5

_RUNTIME_CACHE_DIR = Path("data/runtime/instrument_registry_cache")
_HOST_LOCKS: dict[str, asyncio.Lock] = {}
_HOST_LAST_REQUEST_AT: dict[str, float] = {}


def _host_lock(host: str) -> asyncio.Lock:
    lock = _HOST_LOCKS.get(host)
    if lock is None:
        lock = asyncio.Lock()
        _HOST_LOCKS[host] = lock
    return lock


def _cache_path(url: str) -> Path:
    digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:48]
    return _RUNTIME_CACHE_DIR / f"{digest}.json"


def _read_cache(url: str) -> Optional[dict[str, Any]]:
    path = _cache_path(url)
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _write_cache(url: str, payload: dict[str, Any]) -> None:
    try:
        _RUNTIME_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        _cache_path(url).write_text(json.dumps(payload), encoding="utf-8")
    except OSError as exc:
        logger.debug("instruments.http | cache write failed for {}: {}", url, exc)


@dataclass(frozen=True)
class FetchResponse:
    status_code: int
    content: bytes
    headers: Mapping[str, str]
    from_cache: bool = False
    cached_etag: Optional[str] = None
    cached_last_modified: Optional[str] = None


async def polite_get(
    url: str,
    *,
    method: str = "GET",
    headers: Optional[Mapping[str, str]] = None,
    timeout_sec: float = DEFAULT_TIMEOUT_SEC,
    retries: int = DEFAULT_RETRIES,
    retry_base_delay: float = DEFAULT_RETRY_BASE_DELAY,
    max_bytes: int = DEFAULT_MAX_BYTES,
    min_interval_per_host: float = DEFAULT_MIN_INTERVAL_PER_HOST,
    cache: bool = True,
    extra_user_agent: Optional[str] = None,
    json_body: Optional[Any] = None,
) -> FetchResponse:
    """Polite HTTP GET/POST with caching and retries.

    Returns a ``FetchResponse``; raises ``SourceFetchError`` only when all
    retries are exhausted or the response is unusable (e.g. exceeds
    ``max_bytes``).
    """
    if not url:
        raise SourceFetchError("polite_get called with empty URL")

    parsed = httpx.URL(url)
    host = parsed.host or "unknown"

    base_headers = {
        "User-Agent": extra_user_agent or DEFAULT_USER_AGENT,
        "Accept-Encoding": "gzip, deflate",
    }
    if headers:
        base_headers.update(headers)

    cached: Optional[dict[str, Any]] = _read_cache(url) if cache else None
    if cached:
        if cached.get("etag"):
            base_headers["If-None-Match"] = cached["etag"]
        if cached.get("last_modified"):
            base_headers["If-Modified-Since"] = cached["last_modified"]

    last_exc: Optional[Exception] = None
    for attempt in range(max(1, int(retries))):
        # Per-host min-interval pacing
        async with _host_lock(host):
            now = time.monotonic()
            last_at = _HOST_LAST_REQUEST_AT.get(host, 0.0)
            wait = max(0.0, min_interval_per_host - (now - last_at))
            if wait > 0:
                await asyncio.sleep(wait)
            _HOST_LAST_REQUEST_AT[host] = time.monotonic()

        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(timeout_sec),
                follow_redirects=True,
            ) as client:
                if method.upper() == "POST":
                    resp = await client.post(url, headers=base_headers, json=json_body)
                else:
                    resp = await client.get(url, headers=base_headers)
        except (httpx.HTTPError, asyncio.TimeoutError, OSError) as exc:
            last_exc = exc
            backoff = retry_base_delay * (2 ** attempt) + random.uniform(0.0, 0.5)
            logger.debug(
                "instruments.http | {} attempt={} transport_error={} backoff={}s",
                url, attempt + 1, exc, round(backoff, 2),
            )
            await asyncio.sleep(backoff)
            continue

        if resp.status_code == 304 and cached and "content_b64" in cached:
            import base64

            data = base64.b64decode(cached["content_b64"])
            return FetchResponse(
                status_code=304,
                content=data,
                headers=dict(cached.get("headers") or {}),
                from_cache=True,
                cached_etag=cached.get("etag"),
                cached_last_modified=cached.get("last_modified"),
            )

        if resp.status_code in (429, 500, 502, 503, 504):
            backoff = retry_base_delay * (2 ** attempt) + random.uniform(0.0, 0.5)
            logger.debug(
                "instruments.http | {} attempt={} http_status={} backoff={}s",
                url, attempt + 1, resp.status_code, round(backoff, 2),
            )
            await asyncio.sleep(backoff)
            last_exc = SourceFetchError(f"HTTP {resp.status_code}")
            continue

        if resp.status_code >= 400:
            raise SourceFetchError(f"HTTP {resp.status_code} for {url}")

        body = resp.content or b""
        if len(body) > max_bytes:
            raise SourceFetchError(
                f"Response exceeded max_bytes={max_bytes} for {url}"
            )

        # Defensive cache poisoning guard: never cache anti-bot HTML fallbacks
        # for endpoints that the caller asked for CSV/JSON via the Accept header.
        # iShares, for instance, may serve an HTML landing page instead of the
        # CSV download when the request fingerprint looks bot-like; caching that
        # would lock the source out for the entire ETag validity window.
        accept_header = (base_headers.get("Accept") or "").lower()
        non_html_expected = (
            "text/csv" in accept_header
            or "application/json" in accept_header
            or "application/octet-stream" in accept_header
        )
        body_preview = body[:128].lstrip().lower()
        looks_like_html = (
            body_preview.startswith(b"<!doctype")
            or body_preview.startswith(b"<html")
        )

        if (
            cache
            and method.upper() == "GET"
            and not (non_html_expected and looks_like_html)
        ):
            import base64

            payload = {
                "etag": resp.headers.get("ETag"),
                "last_modified": resp.headers.get("Last-Modified"),
                "fetched_at": time.time(),
                "headers": dict(resp.headers),
                "content_b64": base64.b64encode(body).decode("ascii"),
            }
            _write_cache(url, payload)

        return FetchResponse(
            status_code=resp.status_code,
            content=body,
            headers=dict(resp.headers),
            from_cache=False,
            cached_etag=resp.headers.get("ETag"),
            cached_last_modified=resp.headers.get("Last-Modified"),
        )

    raise SourceFetchError(f"All retries exhausted for {url}: {last_exc}")
