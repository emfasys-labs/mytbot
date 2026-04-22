"""End-to-end M2 pipeline integration test with disposable Postgres."""

from __future__ import annotations

import os
from datetime import datetime, timezone

import pandas as pd
import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url

from data.fred_client import FredObservation
from data.alphavantage_client import NormalizedArticle as AVNormalizedArticle
from data.newsapi_client import NormalizedArticle, headline_content_hash
from data.pipeline import run_once
from storage.db import dispose_engine, init_async_database


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.skipif(
    os.getenv("RUN_INTEGRATION_TESTS") != "1",
    reason="set RUN_INTEGRATION_TESTS=1 to run Docker-backed integration tests",
)
async def test_pipeline_run_once_backfill_with_disposable_postgres(monkeypatch: pytest.MonkeyPatch) -> None:
    tc = pytest.importorskip("testcontainers.postgres")
    PostgresContainer = tc.PostgresContainer

    container = PostgresContainer("postgres:16")
    engine = None
    try:
        container.start()
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"Docker/Postgres testcontainer unavailable: {exc}")

    try:
        sync_url = make_url(container.get_connection_url())
        monkeypatch.setenv("POSTGRES_HOST", str(sync_url.host))
        monkeypatch.setenv("POSTGRES_PORT", str(sync_url.port))
        monkeypatch.setenv("POSTGRES_DB", str(sync_url.database))
        monkeypatch.setenv("POSTGRES_USER", str(sync_url.username))
        monkeypatch.setenv("POSTGRES_PASSWORD", str(sync_url.password))

        # Enable optional feeds and mock network calls deterministically.
        monkeypatch.setenv("NEWS_API_KEY", "test-news-key")
        monkeypatch.setenv("ALPHAVANTAGE_API_KEY", "test-av-key")
        monkeypatch.setenv("FINNHUB_API_KEY", "test-fh-key")
        monkeypatch.setenv("MARKETAUX_API_TOKEN", "test-mx-key")
        monkeypatch.setenv("FRED_API_KEY", "test-fred-key")

        def fake_fetch_history(symbol: str, *, interval: str, period: str):
            _ = (symbol, interval, period)
            idx = pd.date_range("2024-01-01", periods=60, freq="D", tz="UTC")
            return pd.DataFrame(
                {
                    "Open": [100 + i for i in range(60)],
                    "High": [101 + i for i in range(60)],
                    "Low": [99 + i for i in range(60)],
                    "Close": [100.5 + i for i in range(60)],
                    "Volume": [1_000_000.0] * 60,
                },
                index=idx,
            )

        def fake_fetch_everything(api_key: str, *, q: str, language: str, page_size: int):
            _ = (api_key, q, language, page_size)
            now = datetime(2026, 4, 6, tzinfo=timezone.utc)
            url = "https://example.com/news/1"
            title = "Test headline"
            return [
                NormalizedArticle(
                    content_hash=headline_content_hash(url=url, title=title),
                    url=url,
                    title=title,
                    description="integration test",
                    source_name="example",
                    published_at=now,
                )
            ]

        def fake_fetch_series_observations(
            api_key: str, series_id: str, *, observation_start=None
        ):
            _ = (api_key, series_id, observation_start)
            return [
                FredObservation(obs_date="2026-01-01", value="4.33"),
                FredObservation(obs_date="2026-02-01", value="4.35"),
            ]

        def fake_fetch_news_sentiment(api_key: str, *, limit: int, sort: str = "LATEST"):
            _ = (api_key, limit, sort)
            now = datetime(2026, 4, 6, 1, tzinfo=timezone.utc)
            url = "https://example.com/av/1"
            title = "AV headline"
            return [
                AVNormalizedArticle(
                    content_hash=headline_content_hash(url=url, title=title),
                    url=url,
                    title=title,
                    description="integration test av",
                    source_name="alphavantage",
                    published_at=now,
                )
            ]

        def fake_fetch_general_news(api_key: str, *, category: str = "general", timeout_sec: float = 30.0, limit: int = 100):
            _ = (api_key, category, timeout_sec, limit)
            now = datetime(2026, 4, 6, 2, tzinfo=timezone.utc)
            url = "https://example.com/fh/1"
            title = "FH headline"
            return [
                AVNormalizedArticle(
                    content_hash=headline_content_hash(url=url, title=title),
                    url=url,
                    title=title,
                    description="integration test fh",
                    source_name="finnhub",
                    published_at=now,
                )
            ]

        def fake_fetch_all_news(
            api_token: str,
            *,
            language: str = "en",
            limit: int = 100,
            must_have_entities: bool = True,
            timeout_sec: float = 30.0,
        ):
            _ = (api_token, language, limit, must_have_entities, timeout_sec)
            now = datetime(2026, 4, 6, 3, tzinfo=timezone.utc)
            url = "https://example.com/mx/1"
            title = "MX headline"
            return [
                AVNormalizedArticle(
                    content_hash=headline_content_hash(url=url, title=title),
                    url=url,
                    title=title,
                    description="integration test mx",
                    source_name="marketaux",
                    published_at=now,
                )
            ]

        monkeypatch.setattr("data.pipeline.fetch_history", fake_fetch_history)
        monkeypatch.setattr("data.pipeline.fetch_everything", fake_fetch_everything)
        monkeypatch.setattr("data.pipeline.fetch_news_sentiment", fake_fetch_news_sentiment)
        monkeypatch.setattr("data.pipeline.fetch_general_news", fake_fetch_general_news)
        monkeypatch.setattr("data.pipeline.fetch_all_news", fake_fetch_all_news)
        monkeypatch.setattr(
            "data.pipeline.fetch_series_observations",
            fake_fetch_series_observations,
        )

        cfg = {
            "data_source": "yfinance",
            "symbols": ["SPY"],
            "backfill": {
                "interval": "1d",
                "period": "2y",
                "expected_interval_seconds": 86400,
                "stale_after_days": 90,
            },
            "incremental": {
                "interval": "1h",
                "period": "5d",
                "expected_interval_seconds": 3600,
                "stale_after_hours": 72,
            },
            "validation": {"max_gap_multiplier": 7.0},
            "news": {"enabled": True, "query": "SPY", "page_size": 10, "language": "en"},
            "fred": {
                "enabled": True,
                "observation_lookback_years": 1,
                "series": ["FEDFUNDS"],
            },
        }

        engine, session_factory = await init_async_database()
        assert session_factory is not None

        await run_once(session_factory, cfg, backfill=True)

        async with session_factory() as session:
            rs1 = await session.execute(text("SELECT count(*) FROM feature_snapshots"))
            rs2 = await session.execute(text("SELECT count(*) FROM news_headlines"))
            rs3 = await session.execute(text("SELECT count(*) FROM macro_observations"))
            feature_count = int(rs1.scalar_one())
            news_count = int(rs2.scalar_one())
            macro_count = int(rs3.scalar_one())

        assert feature_count >= 50
        assert news_count == 4
        assert macro_count == 2
    finally:
        if engine is not None:
            await dispose_engine(engine)
        container.stop()

