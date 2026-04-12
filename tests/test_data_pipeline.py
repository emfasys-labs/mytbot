"""Tests for M2 data helpers: HTTP clients, features, pipeline entrypoints (mocked)."""

from __future__ import annotations

from datetime import date
from unittest.mock import MagicMock

import pandas as pd
import pytest

from data.features import compute_feature_columns
from data.fred_client import fetch_series_observations
from data.newsapi_client import fetch_everything
from data import http_retry


class _FakeResponse:
    def __init__(self, status_code: int, json_data: dict | None = None, headers: dict | None = None):
        self.status_code = status_code
        self._json = json_data or {}
        self.headers = headers or {}

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            import httpx

            raise httpx.HTTPStatusError("err", request=MagicMock(), response=MagicMock())

    def json(self) -> dict:
        return self._json


def test_httpx_get_with_retry_succeeds_first_try(monkeypatch) -> None:
    calls: list[int] = []

    class _C:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, url, params=None):
            calls.append(1)
            return _FakeResponse(200, {"ok": True})

    monkeypatch.setattr(http_retry.httpx, "Client", lambda **_kw: _C())
    r = http_retry.httpx_get_with_retry("https://example.com/test", params={"a": "1"}, max_attempts=3)
    assert r.status_code == 200
    assert r.json() == {"ok": True}
    assert calls == [1]


def test_httpx_get_with_retry_retries_503(monkeypatch) -> None:
    sleeps: list[float] = []
    state = {"n": 0}

    def _client_factory(**_kwargs):
        class _C:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def get(self, url, params=None):
                state["n"] += 1
                if state["n"] < 2:
                    return _FakeResponse(503, {})
                return _FakeResponse(200, {"done": True})

        return _C()

    monkeypatch.setattr(http_retry.time, "sleep", lambda s: sleeps.append(float(s)))
    monkeypatch.setattr(http_retry.httpx, "Client", _client_factory)
    r = http_retry.httpx_get_with_retry("https://example.com/x", max_attempts=5, min_backoff_sec=0.01, max_backoff_sec=0.05)
    assert r.status_code == 200
    assert len(sleeps) >= 1


def test_fetch_everything_parses_articles(monkeypatch) -> None:
    payload = {
        "status": "ok",
        "articles": [
            {
                "url": "https://x.test/a",
                "title": "Hello",
                "description": "d",
                "publishedAt": "2024-01-15T12:00:00Z",
                "source": {"name": "Src"},
            }
        ],
    }

    monkeypatch.setattr(
        "data.newsapi_client.httpx_get_with_retry",
        lambda *a, **kw: _FakeResponse(200, payload),
    )
    arts = fetch_everything("key", q="mkt")
    assert len(arts) == 1
    assert arts[0].title == "Hello"


def test_fetch_series_observations_parses_rows(monkeypatch) -> None:
    payload = {
        "observations": [
            {"date": "2024-01-01", "value": "1.5"},
            {"date": "2024-01-02", "value": "."},
        ]
    }
    monkeypatch.setattr(
        "data.fred_client.httpx_get_with_retry",
        lambda *a, **kw: _FakeResponse(200, payload),
    )
    obs = fetch_series_observations("k", "GDP", observation_start=date(2024, 1, 1))
    assert len(obs) == 1
    assert obs[0].value == "1.5"


def test_compute_feature_columns_smoke() -> None:
    idx = pd.date_range("2024-01-01", periods=80, freq="D")
    df = pd.DataFrame(
        {
            "Open": range(100, 180),
            "High": range(101, 181),
            "Low": range(99, 179),
            "Close": range(100, 180),
            "Volume": [1_000_000] * 80,
        },
        index=idx,
    )
    out = compute_feature_columns(df)
    assert "rsi_14" in out.columns
    assert "close" in out.columns
    bb_cols = [c for c in out.columns if "BB" in c.upper() or "bb" in c.lower()]
    assert len(bb_cols) >= 1
