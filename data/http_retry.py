"""
Bounded HTTP GET retries for external data APIs (NewsAPI, FRED, etc.).

Retries on 429 (honors Retry-After when parseable), 502/503/504, and connection/timeouts.
Does not retry arbitrary 4xx (except 429).
"""

from __future__ import annotations

import os
import random
import time
from typing import Any

import httpx

# Transient server / rate-limit codes worth retrying (not generic 500 — avoid masking app bugs).
_RETRYABLE_STATUS = frozenset({429, 502, 503, 504})


def _backoff_seconds(attempt: int, response: httpx.Response | None, *, min_sec: float, max_sec: float) -> float:
    if response is not None:
        raw = response.headers.get("Retry-After")
        if raw:
            try:
                return min(max_sec, float(raw))
            except ValueError:
                pass
    base = min(max_sec, min_sec * (2 ** (attempt - 1)))
    return base + random.uniform(0, min(1.0, base * 0.1))


def httpx_get_with_retry(
    url: str,
    *,
    params: dict[str, Any] | None = None,
    timeout_sec: float = 30.0,
    max_attempts: int | None = None,
    min_backoff_sec: float = 1.0,
    max_backoff_sec: float = 60.0,
) -> httpx.Response:
    """
    Synchronous GET with retries. Returns the last :class:`httpx.Response` (caller calls ``raise_for_status()``).
    """
    if max_attempts is None:
        try:
            max_attempts = max(2, int(os.getenv("HTTP_CLIENT_MAX_ATTEMPTS", "5")))
        except ValueError:
            max_attempts = 5

    attempt = 0
    last_response: httpx.Response | None = None
    while attempt < max_attempts:
        attempt += 1
        try:
            with httpx.Client(timeout=timeout_sec) as client:
                last_response = client.get(url, params=params or {})
        except (
            httpx.TimeoutException,
            httpx.ConnectError,
            httpx.ReadError,
            httpx.RemoteProtocolError,
            OSError,
        ) as exc:
            if attempt >= max_attempts:
                raise
            delay = _backoff_seconds(attempt, None, min_sec=min_backoff_sec, max_sec=max_backoff_sec)
            time.sleep(delay)
            continue

        code = last_response.status_code
        if code in _RETRYABLE_STATUS:
            if attempt >= max_attempts:
                return last_response
            delay = _backoff_seconds(attempt, last_response, min_sec=min_backoff_sec, max_sec=max_backoff_sec)
            time.sleep(delay)
            continue

        return last_response

    assert last_response is not None
    return last_response
